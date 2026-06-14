"""MCP server for triggering Cassandra rebuild from modified source code."""

import os

import requests
from fastmcp import FastMCP

from clients.stratus.stratus_utils.get_logger import get_logger

logger = get_logger()
logger.info("Starting Rebuild MCP Server")

mcp = FastMCP("Rebuild MCP Server")

# Build the conductor API URL from environment
API_HOSTNAME = os.getenv("API_HOSTNAME", "localhost")
API_PORT = os.getenv("API_PORT", "8000")
CONDUCTOR_BASE_URL = f"http://{API_HOSTNAME}:{API_PORT}"


def _post_rebuild() -> dict[str, str]:
    """POST the conductor's generic rebuild endpoint and normalise the response."""
    url = f"{CONDUCTOR_BASE_URL}/db/rebuild"
    headers = {"Content-Type": "application/json"}
    try:
        # Long timeout for compilation + deployment (15 minutes)
        response = requests.post(url, headers=headers, timeout=900)
        logger.info(f"[rebuild_database] Response status: {response.status_code}, text: {response.text}")
        if response.status_code == 200:
            return response.json()
        return {"status": "error", "message": f"HTTP {response.status_code}: {response.text}"}
    except requests.Timeout:
        logger.error("[rebuild_database] Request timed out after 15 minutes")
        return {"status": "error", "message": "Rebuild timed out after 15 minutes. The build may still be running."}
    except Exception as e:
        logger.error(f"[rebuild_database] Rebuild request failed: {e}")
        return {"status": "error", "message": f"Rebuild request failed: {e}"}


def _get_rebuild_status() -> dict[str, str]:
    url = f"{CONDUCTOR_BASE_URL}/db/rebuild/status"
    try:
        response = requests.get(url, timeout=30)
        logger.info(f"[rebuild_status] Response status: {response.status_code}, text: {response.text}")
        if response.status_code == 200:
            return response.json()
        return {"ready": False, "error": f"HTTP {response.status_code}: {response.text}"}
    except Exception as e:
        logger.error(f"[rebuild_status] Status check failed: {e}")
        return {"ready": False, "error": f"Status check failed: {e}"}


@mcp.tool(name="rebuild_database")
def rebuild_database() -> dict[str, str]:
    """Rebuild the database from modified source code.

    After editing files under /opt/source, call this tool to:
    1. Recompile the edited source into a database artifact (minutes)
    2. Build a new Docker image with the compiled artifact
    3. Rolling-restart the cluster with the fixed code and wait until Ready

    Total time: several minutes — be patient and wait for completion.

    Returns:
        dict: {"status": "deployed", "image": "<tag>"} on success,
              or {"status": "error", "message": "<error>"} on failure
    """
    logger.info("[rebuild_database] Triggering database rebuild from modified source...")
    return _post_rebuild()


@mcp.tool(name="rebuild_cassandra")
def rebuild_cassandra() -> dict[str, str]:
    """Rebuild the database from modified source code (alias of rebuild_database).

    Retained for backward compatibility with Cassandra agents. Prefer
    ``rebuild_database`` for new problems. See ``rebuild_database`` for details.
    """
    logger.info("[rebuild_cassandra] Triggering rebuild from modified source (alias)...")
    return _post_rebuild()


@mcp.tool(name="rebuild_status")
def rebuild_status() -> dict[str, str]:
    """Check if source rebuild is available for this problem.

    Returns:
        dict: {"ready": bool, "allows_rebuild": bool, "has_source": bool, "buildable": bool}
    """
    logger.info("[rebuild_status] Checking rebuild availability...")
    return _get_rebuild_status()
