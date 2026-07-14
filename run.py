"""
Convenience launcher: loads .env into the process environment, then starts
server.py. Use this locally; in Docker/K8s inject env vars directly and run
`python server.py` instead.

    python run.py
"""

from dotenv import load_dotenv

# override=True so editing .env and restarting actually takes effect, even if
# a stale value is already exported in your shell.
load_dotenv(override=True)

# Import AFTER load_dotenv — server.py reads env vars at import time and will
# hard-exit if AHREFS_API_KEY is missing.
import server  # noqa: E402

if __name__ == "__main__":
    import os

    server.mcp.run(
        transport="http",  # Streamable HTTP
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8080")),
        path="/mcp",
    )
