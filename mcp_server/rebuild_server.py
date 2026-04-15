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


@mcp.tool(name="rebuild_cassandra")
def rebuild_cassandra() -> dict[str, str]:
    """Rebuild Cassandra from modified source code.

    After editing files under /opt/source, call this tool to:
    1. Recompile the source with 'ant jar' (~5 minutes)
    2. Build a new Docker image with the compiled JAR
    3. Rolling restart the Cassandra cluster with the fixed code (~7 minutes)

    Total time: ~12-15 minutes. Be patient and wait for completion.

    Returns:
        dict: {"status": "deployed", "image": "<tag>"} on success,
              or {"status": "error", "message": "<error>"} on failure
    """
    logger.info("[rebuild_cassandra] Triggering Cassandra rebuild from modified source...")

    url = f"{CONDUCTOR_BASE_URL}/cassandra/rebuild"
    headers = {"Content-Type": "application/json"}

    try:
        # Long timeout for compilation + deployment (15 minutes)
        response = requests.post(url, headers=headers, timeout=900)
        logger.info(f"[rebuild_cassandra] Response status: {response.status_code}, text: {response.text}")

        if response.status_code == 200:
            return response.json()
        else:
            return {"status": "error", "message": f"HTTP {response.status_code}: {response.text}"}

    except requests.Timeout:
        logger.error("[rebuild_cassandra] Request timed out after 15 minutes")
        return {"status": "error", "message": "Rebuild timed out after 15 minutes. The build may still be running."}
    except Exception as e:
        logger.error(f"[rebuild_cassandra] Rebuild request failed: {e}")
        return {"status": "error", "message": f"Rebuild request failed: {e}"}


@mcp.tool(name="rebuild_status")
def rebuild_status() -> dict[str, str]:
    """Check if Cassandra rebuild is available for this problem.

    Returns:
        dict: {"ready": bool, "allows_rebuild": bool, "has_source": bool, "has_cassandra": bool}
    """
    logger.info("[rebuild_status] Checking rebuild availability...")

    url = f"{CONDUCTOR_BASE_URL}/cassandra/rebuild/status"

    try:
        response = requests.get(url, timeout=30)
        logger.info(f"[rebuild_status] Response status: {response.status_code}, text: {response.text}")

        if response.status_code == 200:
            return response.json()
        else:
            return {"ready": False, "error": f"HTTP {response.status_code}: {response.text}"}

    except Exception as e:
        logger.error(f"[rebuild_status] Status check failed: {e}")
        return {"ready": False, "error": f"Status check failed: {e}"}
