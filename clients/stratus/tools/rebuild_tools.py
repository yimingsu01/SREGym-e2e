"""Rebuild tools for Cassandra code-level bug fixing."""

import os
from typing import Annotated

import requests
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from clients.stratus.stratus_utils.get_logger import get_logger

logger = get_logger()

# Build the conductor API URL from environment
API_HOSTNAME = os.getenv("API_HOSTNAME", "localhost")
API_PORT = os.getenv("API_PORT", "8000")
CONDUCTOR_BASE_URL = f"http://{API_HOSTNAME}:{API_PORT}"


def _make_command(content: str, tool_call_id: str) -> Command:
    """Helper to create a Command object with a ToolMessage."""
    return Command(update={"messages": [ToolMessage(content=content, tool_call_id=tool_call_id)]})


def _trigger_rebuild(tool_call_id: str, log_prefix: str) -> Command:
    """POST the conductor's DB-agnostic rebuild endpoint and wrap the result."""
    url = f"{CONDUCTOR_BASE_URL}/db/rebuild"
    headers = {"Content-Type": "application/json"}
    try:
        # Long timeout for compilation + deployment (15 minutes)
        response = requests.post(url, headers=headers, timeout=900)
        logger.info(f"[{log_prefix}] Response status: {response.status_code}, text: {response.text}")
        if response.status_code == 200:
            result = response.json()
            return _make_command(
                f"Rebuild successful! Status: {result.get('status', 'unknown')}, Image: {result.get('image', 'unknown')}",
                tool_call_id,
            )
        return _make_command(
            f"Rebuild failed with HTTP {response.status_code}: {response.text}",
            tool_call_id,
        )
    except requests.Timeout:
        logger.error(f"[{log_prefix}] Request timed out after 15 minutes")
        return _make_command(
            "Rebuild timed out after 15 minutes. The build may still be running. Check cluster status.",
            tool_call_id,
        )
    except Exception as e:
        logger.error(f"[{log_prefix}] Rebuild request failed: {e}")
        return _make_command(f"Rebuild request failed: {e}", tool_call_id)


@tool("rebuild_database")
def rebuild_database(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Rebuild the database from modified source code.

    After editing files under /opt/source, call this tool to:
    1. Recompile the edited source into a database artifact (minutes)
    2. Build a new Docker image with the compiled artifact
    3. Rolling restart the cluster with the fixed code and wait until Ready

    Be patient and wait for completion.

    Returns:
        str: status + image tag on success, or an error message on failure
    """
    logger.info("[rebuild_database] Triggering database rebuild from modified source...")
    return _trigger_rebuild(tool_call_id, "rebuild_database")


@tool("rebuild_cassandra")
def rebuild_cassandra(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Rebuild the database from modified source code (alias of rebuild_database).

    Retained for backward compatibility with Cassandra agents. Prefer
    ``rebuild_database`` for new problems.

    Returns:
        str: status + image tag on success, or an error message on failure
    """
    logger.info("[rebuild_cassandra] Triggering rebuild from modified source (alias)...")
    return _trigger_rebuild(tool_call_id, "rebuild_cassandra")


@tool("rebuild_status")
def rebuild_status(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Check whether source rebuild is available for this problem.

    Returns:
        Command: A Command object with the status information.
    """
    logger.info("[rebuild_status] Checking rebuild availability...")

    url = f"{CONDUCTOR_BASE_URL}/db/rebuild/status"

    try:
        response = requests.get(url, timeout=30)
        logger.info(f"[rebuild_status] Response status: {response.status_code}, text: {response.text}")

        if response.status_code == 200:
            result = response.json()
            ready = result.get("ready", False)
            allows_rebuild = result.get("allows_rebuild", False)
            has_source = result.get("has_source", False)
            buildable = result.get("buildable", result.get("has_cassandra", False))

            if ready:
                return _make_command(
                    f"Rebuild is available! allows_rebuild={allows_rebuild}, has_source={has_source}, buildable={buildable}",
                    tool_call_id,
                )
            else:
                return _make_command(
                    f"Rebuild NOT available. allows_rebuild={allows_rebuild}, has_source={has_source}, buildable={buildable}",
                    tool_call_id,
                )
        else:
            return _make_command(
                f"Status check failed with HTTP {response.status_code}: {response.text}",
                tool_call_id,
            )

    except Exception as e:
        logger.error(f"[rebuild_status] Status check failed: {e}")
        return _make_command(f"Status check failed: {e}", tool_call_id)
