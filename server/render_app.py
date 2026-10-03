"""Single-process Render entrypoint for ZAI Memory Hub.

Render exposes one public HTTP port.  The original VPS deployment used Caddy
to split /mcp to FastMCP and everything else to the FastAPI dashboard.  This
entrypoint mounts FastMCP inside the existing dashboard FastAPI app so both
surfaces share one Render hostname.
"""
import os

# On managed hosting, restore the database automatically once a managed
# DATABASE_URL/ZAI_HUB_DSN has been attached. The bootstrap is idempotent.
if os.environ.get("ZAI_HUB_DSN") or os.environ.get("DATABASE_URL"):
    from scripts.render_bootstrap import main as _bootstrap
    _bootstrap()

from dashboard.app import app
from server.hub import mcp

# Mount at /mcp; path="/" avoids a duplicated /mcp/mcp route.
# Stateless HTTP matches the current MCP transport and avoids affinity issues
# on free/serverless-style hosts.
mcp_app = mcp.http_app(path="/", stateless_http=True)

# FastMCP's lifespan must drive the parent ASGI app.
app.router.lifespan_context = mcp_app.lifespan
app.mount("/mcp", mcp_app)
