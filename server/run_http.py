"""Runner that serves the Hub MCP server over streamable-http on localhost.
Caddy reverse-proxies hub.example.com/mcp -> 127.0.0.1:8765/mcp."""
import os
from hub import mcp

PORT = int(os.environ.get("ZAI_HUB_PORT", "8765"))
HOST = os.environ.get("ZAI_HUB_HOST", "127.0.0.1")

if __name__ == "__main__":
    mcp.run(transport="http", host=HOST, port=PORT, path="/mcp")
