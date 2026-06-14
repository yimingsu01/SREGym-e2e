import asyncio
import logging
import os
import threading

import pyfiglet
from fastapi import FastAPI, HTTPException
from fastmcp import FastMCP
from fastmcp.server.http import create_sse_app
from pydantic import BaseModel
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from starlette.routing import Mount
from uvicorn import Config, Server

_conductor = None

submit_mcp = FastMCP("Submit MCP Server")


@submit_mcp.tool(name="submit")
async def submit_via_conductor(ans: str) -> dict[str, str]:
    """Submit task result to benchmark

    Args:
        ans (str): task result that the agent submits

    Returns:
        dict[str]: acknowledgment of submission status
    """
    if _conductor is None or _conductor.submission_stage not in {"diagnosis", "mitigation"}:
        stage = _conductor.submission_stage if _conductor else None
        if stage == "done" and _conductor is not None:
            return {
                "status": "done",
                "text": "All stages have been completed and graded. No further submissions are needed.",
            }
        return {"status": "error", "text": f"Cannot submit at stage: {stage!r}"}

    wrapped = f"```\nsubmit({repr(ans)})\n```"
    max_wait = 60
    for attempt in range(max_wait):
        try:
            await _conductor.submit(wrapped)
            return {"status": "200", "text": "Submission received"}
        except RuntimeError:
            if attempt < max_wait - 1:
                await asyncio.sleep(1)
                continue
            return {"status": "error", "text": "Previous stage is still being evaluated. Try again later."}
        except Exception as e:
            return {"status": "error", "text": f"Grading error: {e}"}


app = FastAPI(
    routes=[
        Mount("/submit_mcp", app=create_sse_app(submit_mcp, "/messages/", "/sse")),
    ]
)

_server: Server | None = None
_shutdown_event = threading.Event()

logger = logging.getLogger("all.sregym.conductor_api")


class _ShutdownNoiseFilter(logging.Filter):
    """Suppress expected CancelledError tracebacks from uvicorn during shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Case 1: exc_info carries the exception object directly.
        if record.exc_info and record.exc_info[1] is not None:
            import asyncio

            if isinstance(record.exc_info[1], asyncio.CancelledError):
                return False
        # Case 2: uvicorn formats the traceback as a plain string message
        # (e.g. logger.error(traceback.format_exc())) with no exc_info.
        # The string will end with "asyncio.exceptions.CancelledError".
        return "CancelledError" not in record.getMessage()


def request_shutdown():
    """
    Signal the API server to shut down.
    Safe to call from any thread and idempotent.
    """
    logger.warning("Shutting down API server...")

    # Suppress expected CancelledError noise from uvicorn tearing down
    # long-lived SSE connections during shutdown
    for name in ("uvicorn.error", "uvicorn"):
        logging.getLogger(name).addFilter(_ShutdownNoiseFilter())

    _shutdown_event.set()
    if _server is not None:
        # force_exit skips waiting for long-lived connections (like MCP SSE)
        # to close gracefully — the agent is already cleaned up at this point
        _server.force_exit = True
        _server.should_exit = True


def set_conductor(c):
    """Inject the shared Conductor instance."""
    global _conductor
    _conductor = c


class SubmitRequest(BaseModel):
    solution: str


@app.post("/submit")
async def submit_solution(req: SubmitRequest):
    allowed = {"diagnosis", "mitigation"}
    if _conductor is None or _conductor.submission_stage not in allowed:
        stage = _conductor.submission_stage if _conductor else None
        if stage == "done" and _conductor is not None:
            logger.debug("Submit received at stage 'done' — problem already graded, returning final results")
            return {
                "status": "done",
                "message": "All stages have been completed and graded. No further submissions are needed.",
            }
        logger.error(f"Cannot submit at stage: {stage!r}")
        raise HTTPException(status_code=400, detail=f"Cannot submit at stage: {stage!r}")

    # Use repr() to properly escape special characters in the solution string
    wrapped = f"```\nsubmit({repr(req.solution)})\n```"
    logger.debug(f"Wrapped submit content: {wrapped}")

    # The conductor evaluates submissions asynchronously. If a previous stage
    # is still being evaluated, waiting_for_agent will be False and submit()
    # raises RuntimeError.  Retry for up to 60s to handle this race.
    max_wait = 60
    for attempt in range(max_wait):
        try:
            await _conductor.submit(wrapped)
            return {"status": "200", "message": "Submission received"}
        except RuntimeError:
            if attempt < max_wait - 1:
                logger.debug("Conductor not ready for submission yet, retrying in 1s...")
                await asyncio.sleep(1)
                continue
            logger.error("Conductor did not become ready for submission within timeout")
            raise HTTPException(
                status_code=503,
                detail="Previous stage is still being evaluated. Try again later.",
            ) from None
        except Exception as e:
            logger.error(f"Grading error: {e}")
            raise HTTPException(status_code=400, detail=f"Grading error: {e}") from e


@app.get("/status")
async def get_status():
    if _conductor is None:
        logger.error("No problem has been started")
        raise HTTPException(status_code=400, detail="No problem has been started")
    stage = _conductor.submission_stage
    logger.debug(f"API returns Current stage: {stage}")
    return {"stage": stage}


@app.get("/get_app")
async def get_app():
    if _conductor is None:
        logger.error("No problem has been started")
        raise HTTPException(status_code=400, detail="No problem has been started")
    app_inst = _conductor.app
    logger.debug(f"API returns App instance: {app_inst}")
    return {"app_name": app_inst.app_name, "namespace": app_inst.namespace, "descriptions": str(app_inst.description)}


@app.post("/cassandra/rebuild")
async def rebuild_cassandra():
    """Backward-compatible alias for ``POST /db/rebuild`` (see that endpoint).

    Retained so existing Cassandra agents and tooling that call
    ``/cassandra/rebuild`` keep working. New code should call the DB-agnostic
    ``/db/rebuild`` endpoint instead.
    """
    return await rebuild_database()


@app.post("/db/rebuild")
async def rebuild_database():
    """Compile the agent-modified source at ``/opt/source`` and redeploy the cluster.

    Database-agnostic: works for any problem that sets ``allows_rebuild = True``
    and provides a source tree. The agent should call this endpoint after editing
    files under ``/opt/source``. The endpoint:
      1. Recompiles the (edited) source tree into a database artifact.
      2. Packages it into a Docker image and loads it into the cluster.
      3. Rolls the running cluster to the rebuilt image and waits for it to be Ready.

    A source fix is ALWAYS compiled — even for problems originally deployed from a
    prebuilt stock image — so the agent's edits actually take effect.

    Returns ``{"status": "deployed", "image": "<image:tag>"}`` on success.

    Example::

        curl -s -X POST http://localhost:8000/db/rebuild | jq .
    """
    if _conductor is None:
        raise HTTPException(status_code=400, detail="No problem is currently active")

    problem = _conductor.problem
    if not getattr(problem, "allows_rebuild", False):
        raise HTTPException(
            status_code=403,
            detail="This problem does not support source rebuild (allows_rebuild is False)",
        )

    source_path = getattr(problem, "source_code_path", None)
    if not source_path:
        raise HTTPException(
            status_code=400,
            detail="This problem has no source tree (source_code_path is not set)",
        )

    loop = asyncio.get_event_loop()
    try:
        new_image = await loop.run_in_executor(None, _rebuild_active_problem, _conductor)
    except _RebuildUnsupported as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Database rebuild failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "deployed", "image": new_image}


class _RebuildUnsupported(Exception):
    """Raised when the active app cannot be rebuilt from source."""


def _rebuild_active_problem(conductor) -> str:
    """Recompile the agent-edited source and roll the live cluster to the result.

    DB-agnostic: dispatches on the active app. For any database deployed through
    GenericDBApplication (driven by DB_REGISTRY) it compiles via GenericDBBuildManager
    and rolls the cluster with deploy_rebuilt_image(). For the legacy Cassandra app it
    uses CassandraBuildManager + update_server_image(). Returns the deployed image tag.
    """
    from pathlib import Path

    problem = conductor.problem
    app_inst = conductor.app
    source_path = Path(problem.source_code_path)

    # Anti-cheat audit: record what the agent changed under /opt/source (vs the pristine
    # baseline) BEFORE compiling, so the diff is source edits rather than build artifacts.
    import contextlib

    with contextlib.suppress(Exception):
        from sregym.service.source_audit import record_rebuild

        problem_id = getattr(problem, "problem_id", None) or problem.__class__.__name__
        record_rebuild(problem_id, source_path)

    spec = getattr(app_inst, "spec", None)
    if spec is not None and hasattr(app_inst, "deploy_rebuilt_image"):
        import dataclasses

        from sregym.service.generic_db_build_manager import GenericDBBuildManager

        # Force a real compile of the agent's edits even if the problem originally
        # deployed a prebuilt stock image — a source fix must be compiled, not re-tagged.
        compile_spec = dataclasses.replace(spec, prebuilt_from_stock=False)
        version = getattr(app_inst, "version", None) or getattr(problem, "db_version", None)
        if not version:
            raise _RebuildUnsupported("Cannot determine database version for rebuild")
        build_mgr = GenericDBBuildManager(compile_spec, source_path, version)
        new_image = build_mgr.build_from_directory()
        app_inst.deploy_rebuilt_image(new_image)
        return new_image

    cassandra_version = getattr(app_inst, "cassandra_version", None)
    if cassandra_version:
        from sregym.service.cassandra_build_manager import CassandraBuildManager

        build_mgr = CassandraBuildManager(source_path, cassandra_version)
        new_image = build_mgr.build_from_directory()
        app_inst.update_server_image(new_image)
        return new_image

    raise _RebuildUnsupported("Active app does not support rebuild (no GenericDBApplication spec or cassandra_version)")


def _rebuild_ready_state() -> dict:
    """Shared readiness payload for the rebuild status endpoints."""
    if _conductor is None:
        return {"ready": False, "reason": "no active problem"}
    problem = _conductor.problem
    app_inst = _conductor.app
    allows = bool(getattr(problem, "allows_rebuild", False))
    has_source = bool(getattr(problem, "source_code_path", None))
    is_generic = getattr(app_inst, "spec", None) is not None and hasattr(app_inst, "deploy_rebuilt_image")
    has_cassandra = bool(getattr(app_inst, "cassandra_version", None))
    buildable = is_generic or has_cassandra
    return {
        "ready": allows and has_source and buildable,
        "allows_rebuild": allows,
        "has_source": has_source,
        "buildable": buildable,
        "has_cassandra": has_cassandra,
    }


@app.get("/cassandra/rebuild/status")
async def rebuild_status():
    """Backward-compatible alias for ``GET /db/rebuild/status``."""
    return _rebuild_ready_state()


@app.get("/db/rebuild/status")
async def db_rebuild_status():
    """Quick health-check: confirms the rebuild endpoint is reachable and the active
    problem supports source rebuild. Does not trigger a build."""
    return _rebuild_ready_state()


@app.get("/get_problem")
async def get_problem():
    if _conductor is None:
        logger.error("No problem has been started")
        raise HTTPException(status_code=400, detail="No problem has been started")
    problem_id = _conductor.problem_id
    logger.debug(f"API returns Problem ID: {problem_id}")
    return {"problem_id": problem_id}


def run_api(conductor):
    """
    Start the API server and block until request_shutdown() is called.
    """
    global _server
    set_conductor(conductor)
    logger.debug(f"API server is binded to the conductor {conductor}")

    # Load from .env with defaults
    host = os.getenv("API_BIND_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))

    logger.debug(f"API server starting on http://{host}:{port}")

    console = Console()
    art = pyfiglet.figlet_format("SREGym")
    console.print(Panel(art, title="SREGym API Server", subtitle=f"http://{host}:{port}", style="bold green"))
    console.print(
        Markdown(
            """
**Available Endpoints**
- **POST /submit**: `{ "solution": "<your-solution>" }` → grades the current stage
- **GET /status**: returns `{ "stage": "setup" | "diagnosis" | "mitigation" | "tearing_down" | "done" }`
- **POST /db/rebuild**: recompile modified source at `/opt/source`, build a new image, roll the cluster → `{ "status": "deployed", "image": "..." }` (alias: `/cassandra/rebuild`)
"""
        )
    )

    config = Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=5,
    )
    config.install_signal_handlers = False
    server = Server(config)
    _server = server  # expose to request_shutdown()

    # watcher thread: when _shutdown_event is set, flip server.should_exit
    def _watch():
        _shutdown_event.wait()
        logger.debug("API server shutdown event received")
        server.should_exit = True

    threading.Thread(target=_watch, name="api-shutdown-watcher", daemon=True).start()

    try:
        logger.debug("API server is running")
        server.run()  # blocks until should_exit becomes True
    finally:
        # cleanup for potential reuse
        _shutdown_event.clear()
        _server = None
