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


@tool("rebuild_cassandra")
def rebuild_cassandra(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Rebuild Cassandra from modified source code.

    After editing files under /opt/source, call this tool to:
    1. Recompile the source with 'ant jar' (~5 minutes)
    2. Build a new Docker image with the compiled JAR
    3. Rolling restart the Cassandra cluster with the fixed code (~7 minutes)

    Total time: ~12-15 minutes. Be patient and wait for completion.

    Returns:
        str: JSON string with {"status": "deployed", "image": "<tag>"} on success,
             or error message on failure
    """
    logger.info("[rebuild_cassandra] Triggering Cassandra rebuild from modified source...")

    url = f"{CONDUCTOR_BASE_URL}/cassandra/rebuild"
    headers = {"Content-Type": "application/json"}

    try:
        # Long timeout for compilation + deployment (15 minutes)
        response = requests.post(url, headers=headers, timeout=900)
        logger.info(f"[rebuild_cassandra] Response status: {response.status_code}, text: {response.text}")

        if response.status_code == 200:
            result = response.json()
            return _make_command(
                f"Rebuild successful! Status: {result.get('status', 'unknown')}, Image: {result.get('image', 'unknown')}",
                tool_call_id,
            )
        else:
            return _make_command(
                f"Rebuild failed with HTTP {response.status_code}: {response.text}",
                tool_call_id,
            )

    except requests.Timeout:
        logger.error("[rebuild_cassandra] Request timed out after 15 minutes")
        return _make_command(
            "Rebuild timed out after 15 minutes. The build may still be running. Check cluster status.",
            tool_call_id,
        )
    except Exception as e:
        logger.error(f"[rebuild_cassandra] Rebuild request failed: {e}")
        return _make_command(f"Rebuild request failed: {e}", tool_call_id)


@tool("rebuild_status")
def rebuild_status(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Check if Cassandra rebuild is available for this problem.

    Returns:
        Command: A Command object with the status information.
    """
    logger.info("[rebuild_status] Checking rebuild availability...")

    url = f"{CONDUCTOR_BASE_URL}/cassandra/rebuild/status"

    try:
        response = requests.get(url, timeout=30)
        logger.info(f"[rebuild_status] Response status: {response.status_code}, text: {response.text}")

        if response.status_code == 200:
            result = response.json()
            ready = result.get("ready", False)
            allows_rebuild = result.get("allows_rebuild", False)
            has_source = result.get("has_source", False)
            has_cassandra = result.get("has_cassandra", False)

            if ready:
                return _make_command(
                    f"Rebuild is available! allows_rebuild={allows_rebuild}, has_source={has_source}, has_cassandra={has_cassandra}",
                    tool_call_id,
                )
            else:
                return _make_command(
                    f"Rebuild NOT available. allows_rebuild={allows_rebuild}, has_source={has_source}, has_cassandra={has_cassandra}",
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
