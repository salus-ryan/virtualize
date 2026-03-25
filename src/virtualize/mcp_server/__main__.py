"""Allow running MCP server via: python -m virtualize.mcp_server"""

import asyncio
from virtualize.mcp_server.server import run_mcp_server

if __name__ == "__main__":
    asyncio.run(run_mcp_server())
