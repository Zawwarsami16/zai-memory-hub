"""ZAI Memory Hub — Dashboard.

FastAPI app for the ZAI Memory Hub dashboard.

Routes:
  GET  /                — universe dashboard
  GET  /login?key=...   — set session cookie
  GET  /api/stats       — counts
  GET  /api/recent      — recent memories
  GET  /api/decisions   — recent decisions
  GET  /api/tool_calls  — recent tool calls
  GET  /api/conflicts   — current conflicts
  GET  /api/graph       — { nodes, edges } for the universe
  GET  /api/presence    — who is online (heartbeat within 5 min)
  GET  /api/memory/{id} — full memory record (for click-inspect)
  GET  /events          — SSE LISTEN/NOTIFY zai_hub_activity
"""
import asyncio, json, os, secrets, sys, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import hashlib
import io

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Response, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

try:
    import pypdf
    HAS_PDF = True
except Exception:
    HAS_PDF = False

DB_DSN = os.environ.get(
    "ZAI_HUB_DSN",
    "host=127.0.0.1 dbname=zai_hub user=zai_hub password=zai_hub_dev",
)
DASHBOARD_KEY = os.environ.get("ZAI_HUB_DASHBOARD_KEY")
if not DASHBOARD_KEY:
    DASHBOARD_KEY = secrets.token_urlsafe(24)
    print(f"[dashboard] WARN: ZAI_HUB_DASHBOARD_KEY not set — generated ephemeral key: {DASHBOARD_KEY}",
          file=sys.stderr, flush=True)
COOKIE_NAME = "zai_hub_session"

PUBLIC_URL = os.environ.get("ZAI_HUB_PUBLIC_URL", "https://hub.example.com").rstrip("/")


def db():
    return psycopg.connect(DB_DSN, row_factory=dict_row, autocommit=True)


app = FastAPI(title="ZAI Memory Hub")

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def require_auth(req: Request):
    if req.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        raise HTTPException(status_code=401, detail="auth required — visit /login?key=...")


@app.get("/login")
def login(key: str = "", resp: Response = None):
    if key != DASHBOARD_KEY:
        return JSONResponse({"error": "bad key"}, status_code=401)
    r = RedirectResponse(url="/", status_code=302)
    r.set_cookie(COOKIE_NAME, DASHBOARD_KEY, max_age=60*60*24*30,
                 httponly=True, samesite="lax", secure=True, path="/")
    return r


# ---- API ---------------------------------------------------------

@app.get("/api/stats")
def api_stats(_: None = Depends(require_auth)):
    with db() as cx, cx.cursor() as cu:
        cu.execute("SELECT count(*)::int AS n FROM memories WHERE deleted_at IS NULL"); mem = cu.fetchone()["n"]
        cu.execute("SELECT count(*)::int AS n FROM decisions WHERE deleted_at IS NULL"); dec = cu.fetchone()["n"]
        cu.execute("SELECT count(*)::int AS n FROM tool_calls"); tc = cu.fetchone()["n"]
        cu.execute("SELECT count(*)::int AS n FROM entities"); ent = cu.fetchone()["n"]
        cu.execute("SELECT count(DISTINCT written_by)::int AS n FROM memories WHERE deleted_at IS NULL"); actors = cu.fetchone()["n"]
        cu.execute("SELECT count(*)::int AS n FROM v_conflicts"); confs = cu.fetchone()["n"]
    return {"memories": mem, "decisions": dec, "tool_calls": tc,
            "entities": ent, "actors": actors, "conflicts": confs}


@app.get("/api/recent")
def api_recent(_: None = Depends(require_auth), n: int = 20):
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT id::text, content, tags, written_by, importance, created_at, "
            "ARRAY(SELECT slug FROM entities WHERE id = ANY(memories.entity_ids)) AS entity_slugs "
            "FROM memories WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT %s", (n,))
        return [{**r, "created_at": r["created_at"].isoformat()} for r in cu.fetchall()]


@app.get("/api/decisions")
def api_decisions(_: None = Depends(require_auth), n: int = 20):
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT id::text, summary, rationale, alternatives, written_by, "
            "supersedes::text, created_at "
            "FROM decisions WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT %s", (n,))
        return [{**r, "created_at": r["created_at"].isoformat()} for r in cu.fetchall()]


@app.get("/api/tool_calls")
def api_tool_calls(_: None = Depends(require_auth), n: int = 50):
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT id, tool_name, called_by, duration_ms, status, result_brief, created_at "
            "FROM tool_calls ORDER BY created_at DESC LIMIT %s", (n,))
        return [{**r, "created_at": r["created_at"].isoformat()} for r in cu.fetchall()]


@app.get("/api/conflicts")
def api_conflicts(_: None = Depends(require_auth)):
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT memory_a::text, memory_b::text, content_a, content_b, "
            "by_a, by_b, at_a, at_b FROM v_conflicts LIMIT 50")
        return [{**r, "at_a": r["at_a"].isoformat(), "at_b": r["at_b"].isoformat()}
                for r in cu.fetchall()]


@app.get("/api/memory/{mid}")
def api_memory(mid: str, _: None = Depends(require_auth)):
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT id::text, content, tags, written_by, importance, created_at, "
            "ARRAY(SELECT slug FROM entities WHERE id = ANY(memories.entity_ids)) AS entity_slugs "
            "FROM memories WHERE id = %s AND deleted_at IS NULL", (mid,))
        row = cu.fetchone()
    if not row:
        raise HTTPException(404, "not found")
    row["created_at"] = row["created_at"].isoformat()
    return row


@app.get("/api/graph")
def api_graph(_: None = Depends(require_auth)):
    actor_slugs = {"vps-claude", "local-claude", "chat-claude"}
    with db() as cx, cx.cursor() as cu:
        cu.execute("SELECT id::text, slug, kind, display, metadata FROM entities")
        entities = cu.fetchall()
        cu.execute(
            "SELECT id::text, substring(content for 120) AS preview, tags, "
            "ARRAY(SELECT slug FROM entities WHERE id = ANY(memories.entity_ids)) AS entity_slugs, "
            "written_by, importance, created_at "
            "FROM memories WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 40")
        memories = cu.fetchall()
        cu.execute("""
            SELECT actor, max(t) AS last_seen FROM (
              SELECT called_by AS actor, created_at AS t FROM tool_calls WHERE called_by IS NOT NULL
              UNION ALL
              SELECT written_by, created_at FROM memories WHERE deleted_at IS NULL AND written_by IS NOT NULL
              UNION ALL
              SELECT written_by, created_at FROM decisions WHERE deleted_at IS NULL AND written_by IS NOT NULL
            ) x GROUP BY actor
        """)
        last_seen = {r["actor"]: r["last_seen"] for r in cu.fetchall()}

    now = time.time()
    nodes = []
    for e in entities:
        ts = last_seen.get(e["slug"])
        age = (now - ts.timestamp()) if ts else None
        kind = "actor" if e["slug"] in actor_slugs else e["kind"]
        nodes.append({
            "id": e["slug"], "type": "entity", "kind": kind,
            "label": e["display"], "slug": e["slug"],
            "last_seen_age_s": age,
            "online": (age is not None and age < 300),
        })
    for m in memories:
        nodes.append({
            "id": "mem:" + m["id"], "type": "memory", "kind": "memory",
            "label": m["preview"], "slug": None,
            "memory_id": m["id"], "tags": m["tags"] or [],
            "written_by": m["written_by"], "importance": m["importance"] or 3,
            "created_at": m["created_at"].isoformat(),
            "entity_slugs": m["entity_slugs"] or [],
        })

    edges = []
    for m in memories:
        src = "mem:" + m["id"]
        if m["written_by"]:
            edges.append({"source": m["written_by"], "target": src, "kind": "wrote"})
        for slug in (m["entity_slugs"] or []):
            edges.append({"source": src, "target": slug, "kind": "refs"})
    return {"nodes": nodes, "edges": edges}


# ---- CLUSTERS ----------------------------------------------------
# 8 category orbs around the CORE MEMORY in the bubble cloud.
# Each category maps real data via tags / authorship / importance.

CLUSTERS = [
    {"slug": "core",     "label": "Core Memory",       "sub": "Everything Connected",       "color": "#dc2626"},
    {"slug": "coding",   "label": "Coding Session",    "sub": "VS Code · Terminal",         "color": "#ff5046"},
    {"slug": "web",      "label": "Web Session",       "sub": "Chrome · Chat",              "color": "#7aa6ff"},
    {"slug": "mobile",   "label": "Mobile Session",    "sub": "iPhone · Chat",              "color": "#5ee2a0"},
    {"slug": "github",   "label": "GitHub Intelligence","sub": "Repos · Files",             "color": "#e8d49a"},
    {"slug": "agents",   "label": "AI Agents",         "sub": "Active Agents",              "color": "#c084ff"},
    {"slug": "planning", "label": "Planning Hub",      "sub": "Projects · Goals",           "color": "#ff9a4a"},
    {"slug": "longterm", "label": "Long Term Memory",  "sub": "Core Knowledge",             "color": "#dc2626"},
    {"slug": "terminal", "label": "Terminal Context",  "sub": "Bash · Logs · Scripts",      "color": "#4adcff"},
]


def _cluster_predicate(slug):
    """SQL fragment + params for the WHERE clause of a memory query for a given cluster."""
    if slug == "core":
        return ("TRUE", ())
    if slug == "coding":
        return ("(written_by = 'local-claude' OR tags && ARRAY['code','coding','dev','ui']::text[])", ())
    if slug == "web":
        return ("(written_by = 'chat-claude' OR tags && ARRAY['web','chrome','chat']::text[])", ())
    if slug == "mobile":
        return ("(tags && ARRAY['mobile','phone','iphone']::text[])", ())
    if slug == "github":
        return ("(tags && ARRAY['github','pr','repo','commit','oss']::text[] "
                "OR EXISTS (SELECT 1 FROM entities e WHERE e.slug = 'anteroom-studio' "
                "AND e.id = ANY(memories.entity_ids)))", ())
    if slug == "agents":
        return ("(written_by IN ('vps-claude','local-claude','chat-claude'))", ())
    if slug == "planning":
        return ("(tags && ARRAY['plan','planning','goal','project','milestone']::text[])", ())
    if slug == "longterm":
        return ("(importance >= 4)", ())
    if slug == "terminal":
        return ("(written_by = 'vps-claude' OR tags && ARRAY['shell','ssh','bash','htb','vpn','log']::text[])", ())
    return ("FALSE", ())


@app.get("/api/clusters")
def api_clusters(_: None = Depends(require_auth)):
    """Counts for each of the 8 category orbs + total CORE."""
    out = []
    with db() as cx, cx.cursor() as cu:
        for c in CLUSTERS:
            pred, _params = _cluster_predicate(c["slug"])
            cu.execute(f"SELECT count(*)::int AS n FROM memories WHERE deleted_at IS NULL AND ({pred})")
            n = cu.fetchone()["n"]
            extra = {}
            if c["slug"] == "agents":
                cu.execute("SELECT count(DISTINCT called_by)::int AS n FROM tool_calls WHERE called_by IS NOT NULL")
                extra["active_agents"] = cu.fetchone()["n"]
            if c["slug"] == "planning":
                cu.execute("SELECT count(*)::int AS n FROM decisions WHERE deleted_at IS NULL")
                extra["decisions"] = cu.fetchone()["n"]
            if c["slug"] == "github":
                cu.execute("SELECT count(DISTINCT id)::int AS n FROM entities WHERE kind IN ('project') OR slug LIKE '%repo%'")
                extra["repos"] = cu.fetchone()["n"]
            out.append({**c, "nodes": n, **extra})
    return out


@app.get("/api/cluster/{slug}")
def api_cluster(slug: str, _: None = Depends(require_auth), n: int = 30):
    cluster = next((c for c in CLUSTERS if c["slug"] == slug), None)
    if not cluster:
        raise HTTPException(404, "unknown cluster")
    pred, _params = _cluster_predicate(slug)
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            f"SELECT id::text, substring(content for 90) AS preview, tags, "
            f"written_by, importance, created_at, "
            f"ARRAY(SELECT e.slug FROM entities e WHERE e.id = ANY(memories.entity_ids)) AS entity_slugs "
            f"FROM memories WHERE deleted_at IS NULL AND ({pred}) ORDER BY created_at DESC LIMIT %s", (n,))
        rows = cu.fetchall()
    items = [{**r, "created_at": r["created_at"].isoformat()} for r in rows]
    return {"cluster": cluster, "items": items, "total": len(items)}


# ---- SOFT DELETE + AUDIT -----------------------------------------

def _audit(target_kind: str, target_id: str, action: str, actor: str = "dashboard-user", detail: dict | None = None):
    """Write one audit log row.  Best-effort; never raises."""
    try:
        with db() as cx, cx.cursor() as cu:
            cu.execute(
                "INSERT INTO audit_log (target_kind, target_id, action, actor, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (target_kind, target_id, action, actor, json.dumps(detail or {})))
    except Exception as e:
        print(f"[audit] failed: {e}", file=sys.stderr)


@app.post("/api/memory/{mid}/delete")
def api_memory_delete(mid: str, _: None = Depends(require_auth)):
    """Soft-delete a memory.  Restorable.  Excluded from feeds."""
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "UPDATE memories SET deleted_at = now(), deleted_by = 'dashboard-user' "
            "WHERE id = %s AND deleted_at IS NULL RETURNING id::text", (mid,))
        row = cu.fetchone()
    if not row:
        raise HTTPException(404, "not found or already deleted")
    _audit("memory", mid, "soft_delete", "dashboard-user")
    return {"ok": True, "id": mid}


@app.post("/api/memory/{mid}/restore")
def api_memory_restore(mid: str, _: None = Depends(require_auth)):
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "UPDATE memories SET deleted_at = NULL, deleted_by = NULL "
            "WHERE id = %s AND deleted_at IS NOT NULL RETURNING id::text", (mid,))
        row = cu.fetchone()
    if not row:
        raise HTTPException(404, "not found or not deleted")
    _audit("memory", mid, "restore", "dashboard-user")
    return {"ok": True, "id": mid}


@app.delete("/api/memory/{mid}/permanent")
def api_memory_hard_delete(mid: str, _: None = Depends(require_auth)):
    """Permanent delete — dashboard only.  No undo.  Use sparingly."""
    with db() as cx, cx.cursor() as cu:
        cu.execute("DELETE FROM memories WHERE id = %s RETURNING id::text", (mid,))
        row = cu.fetchone()
    if not row:
        raise HTTPException(404, "not found")
    _audit("memory", mid, "hard_delete", "dashboard-user")
    return {"ok": True, "id": mid}


@app.get("/api/trash")
def api_trash(_: None = Depends(require_auth), n: int = 50):
    """List soft-deleted memories + decisions."""
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT id::text, substring(content for 200) AS preview, tags, "
            "written_by, deleted_by, deleted_at, importance "
            "FROM memories WHERE deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC LIMIT %s", (n,))
        mems = [{**r, "deleted_at": r["deleted_at"].isoformat(), "kind": "memory"}
                for r in cu.fetchall()]
        cu.execute(
            "SELECT id::text, summary AS preview, written_by, deleted_by, deleted_at "
            "FROM decisions WHERE deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC LIMIT %s", (n,))
        decs = [{**r, "deleted_at": r["deleted_at"].isoformat(), "kind": "decision"}
                for r in cu.fetchall()]
    return {"memories": mems, "decisions": decs, "total": len(mems) + len(decs)}


@app.get("/api/audit")
def api_audit(_: None = Depends(require_auth), n: int = 100):
    """Audit log — every mutation."""
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT id, target_kind, target_id::text, action, actor, detail, created_at "
            "FROM audit_log ORDER BY created_at DESC LIMIT %s", (n,))
        return [{**r, "created_at": r["created_at"].isoformat()} for r in cu.fetchall()]


# ---- Export (data portability) -----------------------------------

def _row_dumps(r: dict) -> str:
    """JSON-serialise a row, handling datetimes + UUIDs + bytes safely."""
    def _default(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, (bytes, bytearray, memoryview)):
            return None
        return str(o)
    return json.dumps(r, default=_default, ensure_ascii=False)


@app.get("/api/export")
def api_export(_: None = Depends(require_auth),
               include_deleted: bool = True,
               include_audit: bool = True,
               include_tool_calls: bool = False):
    """Full memory hub dump as one streamed JSON file.

    Carry the resulting file to any new Hub install and re-import with
    `scripts/import_export.py`.  No binary blobs (uploaded PDFs sit under
    static/uploads/ — tar that separately).
    """
    tables = [
        ("entities",
         "SELECT id::text, slug, kind, display, metadata, created_at, updated_at "
         "FROM entities ORDER BY created_at"),
        ("memories",
         "SELECT id::text, content, tags, "
         "(SELECT array_agg(e.slug) FROM entities e WHERE e.id = ANY(m.entity_ids)) AS entity_slugs, "
         "written_by, session_id, importance, deleted_at, deleted_by, created_at "
         "FROM memories m" + ("" if include_deleted else " WHERE deleted_at IS NULL") +
         " ORDER BY created_at"),
        ("decisions",
         "SELECT id::text, summary, rationale, alternatives, "
         "(SELECT array_agg(e.slug) FROM entities e WHERE e.id = ANY(d.entity_ids)) AS entity_slugs, "
         "written_by, supersedes::text, deleted_at, deleted_by, created_at "
         "FROM decisions d" + ("" if include_deleted else " WHERE deleted_at IS NULL") +
         " ORDER BY created_at"),
        ("interactions",
         "SELECT id::text, session_id, surface, summary, metadata, started_at "
         "FROM interactions ORDER BY started_at"),
    ]
    if include_audit:
        tables.append(("audit_log",
                       "SELECT id, target_kind, target_id::text, action, actor, detail, created_at "
                       "FROM audit_log ORDER BY created_at"))
    if include_tool_calls:
        tables.append(("tool_calls",
                       "SELECT id, tool_name, args, result_brief, called_by, session_id, "
                       "duration_ms, status, error, created_at "
                       "FROM tool_calls ORDER BY created_at"))

    def gen():
        yield (
            '{\n'
            f'  "schema_version": "1",\n'
            f'  "exported_at": {json.dumps(datetime.now(timezone.utc).isoformat())},\n'
            f'  "source": "zai-memory-hub",\n'
        )
        with db() as cx, cx.cursor() as cu:
            for ti, (name, sql) in enumerate(tables):
                yield f'  "{name}": [\n'
                cu.execute(sql)
                first = True
                for row in cu:
                    if not first:
                        yield ",\n"
                    yield "    " + _row_dumps(row)
                    first = False
                yield "\n  ]" + ("," if ti < len(tables) - 1 else "") + "\n"
        yield "}\n"

    fname = f"zai-hub-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return StreamingResponse(
        gen(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---- /health (public, no auth) -----------------------------------
# Lets connected agents probe before depending on the hub.  Returns
# {ok, db, version, agents_active} so a degraded mode can be triggered
# client-side ("no hub today, working in-context only") instead of
# erroring on first tool call.

HUB_VERSION = "0.4.0"  # bump on schema or auth changes


@app.get("/health")
def health():
    db_ok = True
    agents_active = 0
    try:
        with db() as cx, cx.cursor() as cu:
            cu.execute("SELECT 1")
            cu.execute("SELECT count(*) AS n FROM agent_tokens WHERE revoked_at IS NULL")
            agents_active = cu.fetchone()["n"]
    except Exception:
        db_ok = False
    return JSONResponse({
        "ok": db_ok,
        "service": "zai-memory-hub",
        "version": HUB_VERSION,
        "db": "up" if db_ok else "down",
        "agents_active": agents_active,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }, status_code=200 if db_ok else 503)


# ---- OAuth 2.0 + Dynamic Client Registration ---------------------
# Lets MCP-aware OAuth clients (Claude.ai web's custom-connector wizard,
# Cursor, etc.) auto-register and walk through an auth-code flow that
# ends with a bearer token bound to a chosen slug + role in agent_tokens.

ROLE_DEFAULT = "writer"        # default role the consent screen offers
CODE_TTL_SECONDS = 300         # 5 minutes for the user to finish consent


def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _mint_agent_token(slug: str, role: str, label: str, issued_via: str = "manual") -> str:
    token = "zai_" + secrets.token_urlsafe(32)
    h = _hash_token(token)
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO agent_tokens (token_hash, slug, role, label, issued_via) "
            "VALUES (%s, %s, %s, %s, %s)",
            (h, slug, role, label, issued_via))
    return token


@app.get("/.well-known/oauth-authorization-server")
def oauth_metadata():
    """OAuth 2.0 authorization server metadata (RFC 8414).

    This is the first thing Claude.ai (and other MCP-aware OAuth clients)
    hits when 'Add custom connector' walks the wizard.  It tells them
    where the endpoints live and what grants we support."""
    return JSONResponse({
        "issuer": PUBLIC_URL,
        "authorization_endpoint": f"{PUBLIC_URL}/oauth/authorize",
        "token_endpoint": f"{PUBLIC_URL}/oauth/token",
        "registration_endpoint": f"{PUBLIC_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["mcp:read", "mcp:write", "mcp:delete"],
        "service_documentation": f"{PUBLIC_URL}/connect",
    })


@app.get("/.well-known/oauth-protected-resource")
def oauth_resource_metadata():
    """RFC 9728 — tells clients which auth server protects this resource."""
    return JSONResponse({
        "resource": f"{PUBLIC_URL}/mcp",
        "authorization_servers": [PUBLIC_URL],
        "scopes_supported": ["mcp:read", "mcp:write", "mcp:delete"],
        "bearer_methods_supported": ["header"],
    })


@app.post("/oauth/register")
async def oauth_register(req: Request):
    """RFC 7591 — Dynamic Client Registration.

    Open by design: any MCP client can register itself.  The bearer
    issued at the end of the flow is what carries the actual privilege,
    not the client_id."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    client_name = (body.get("client_name") or "unknown-client")[:120]
    redirect_uris = body.get("redirect_uris") or []
    if not redirect_uris or not isinstance(redirect_uris, list):
        raise HTTPException(400, "redirect_uris required")
    token_auth = body.get("token_endpoint_auth_method", "none")
    scope = body.get("scope", "mcp:read mcp:write")
    client_id = "cli_" + secrets.token_urlsafe(16)
    client_secret = None
    if token_auth == "client_secret_post":
        client_secret = secrets.token_urlsafe(32)
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO oauth_clients (client_id, client_secret, client_name, "
            "redirect_uris, token_endpoint_auth_method, scope) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (client_id, client_secret, client_name, redirect_uris, token_auth, scope))
    resp = {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": token_auth,
        "scope": scope,
    }
    if client_secret:
        resp["client_secret"] = client_secret
    return JSONResponse(resp, status_code=201)


@app.get("/oauth/authorize", response_class=HTMLResponse)
def oauth_authorize(request: Request,
                    response_type: str = "",
                    client_id: str = "",
                    redirect_uri: str = "",
                    scope: str = "mcp:read mcp:write",
                    state: str = "",
                    code_challenge: str = "",
                    code_challenge_method: str = "S256"):
    """Consent screen.  User must already be logged into the dashboard
    (cookie auth) — that's how we know it's actually them and not a
    drive-by approving on their behalf."""
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        # Bounce through /login first; preserve the full authorize URL
        from urllib.parse import urlencode, quote
        params = {"response_type": response_type, "client_id": client_id,
                  "redirect_uri": redirect_uri, "scope": scope, "state": state,
                  "code_challenge": code_challenge,
                  "code_challenge_method": code_challenge_method}
        target = "/oauth/authorize?" + urlencode(params)
        return HTMLResponse(
            f"<html><body style='font-family:monospace;padding:40px;background:#0a0508;color:#f5ecdb;line-height:1.7'>"
            f"<h1 style='font-family:Georgia,serif;font-style:italic;color:#e8d49a'>One step.</h1>"
            f"<p>Log into the dashboard first, then this consent screen will work.</p>"
            f"<p><a href='/login?key=YOUR_KEY&next={quote(target)}' style='color:#dc2626'>→ /login?key=YOUR_KEY&next=…</a></p>"
            f"<p style='color:#8b6a5a;font-size:13px'>(replace YOUR_KEY with your dashboard key)</p></body></html>",
            status_code=401)
    # Look up the client
    with db() as cx, cx.cursor() as cu:
        cu.execute("SELECT client_name, redirect_uris FROM oauth_clients WHERE client_id = %s",
                   (client_id,))
        client = cu.fetchone()
    if not client:
        raise HTTPException(400, f"unknown client_id: {client_id}")
    if redirect_uri not in (client["redirect_uris"] or []):
        raise HTTPException(400, "redirect_uri mismatch")
    # Determine which role the user can grant.  Default is ROLE_DEFAULT (writer).
    # Future: let the user pick from a dropdown.
    role = ROLE_DEFAULT
    # Slug suggestion: slugified client name + short suffix for uniqueness
    import re
    slug_base = re.sub(r'[^a-z0-9-]', '-', client["client_name"].lower()).strip('-')[:40] or "agent"
    slug_suggest = f"{slug_base}-{secrets.token_hex(2)}"
    return HTMLResponse(_render_consent_screen(
        client_name=client["client_name"],
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        slug_suggest=slug_suggest,
        role=role,
    ))


def _render_consent_screen(client_name, client_id, redirect_uri, scope, state,
                            code_challenge, code_challenge_method, slug_suggest, role):
    from html import escape
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Authorize · ZAI Memory Hub</title>
<style>
  body {{ font-family: Georgia, serif; background: #0a0508; color: #f5ecdb; margin: 0;
          min-height: 100vh; display: grid; place-items: center; padding: 40px 20px; }}
  .card {{ max-width: 540px; width: 100%; background: rgba(20,8,10,0.85); border: 1px solid #5c3d20;
           padding: 36px 40px; border-radius: 3px; }}
  .eyebrow {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; letter-spacing: .4em;
              color: #c4924a; text-transform: uppercase; }}
  h1 {{ font-style: italic; font-size: 28px; margin: 8px 0 18px; color: #f5dca3; }}
  p {{ line-height: 1.6; color: #d4c5a0; font-size: 14px; }}
  .grants {{ background: rgba(8,3,6,0.7); border-left: 2px solid #dc2626; padding: 14px 18px;
             margin: 18px 0; }}
  .grants li {{ font-family: 'JetBrains Mono', monospace; font-size: 12.5px; color: #f5ecdb; }}
  label {{ display:block; font-family:'JetBrains Mono',monospace; font-size:10px;
           letter-spacing:.32em; color:#c4924a; text-transform:uppercase; margin: 14px 0 4px; }}
  input[type=text] {{ width: 100%; padding: 9px 11px; background: rgba(8,3,6,0.6); color: #f5dca3;
                       border: 1px solid #5c3d20; border-radius: 2px; font-family: 'JetBrains Mono',monospace;
                       font-size: 13px; box-sizing: border-box; }}
  .actions {{ display: flex; gap: 12px; margin-top: 24px; }}
  button {{ flex: 1; padding: 10px 18px; border: 1px solid #5c3d20; background: rgba(8,3,6,0.7);
            color: #c4924a; font-family: 'JetBrains Mono',monospace; font-size: 10px;
            letter-spacing: .32em; text-transform: uppercase; border-radius: 2px; cursor: pointer; }}
  button.primary {{ background: linear-gradient(180deg,#dc2626,#9a1212); color: #f5dca3; border-color: #dc2626; }}
  button:hover {{ transform: translateY(-1px); }}
  small {{ color: #8b6a5a; font-size: 11px; }}
</style></head>
<body><div class='card'>
  <div class='eyebrow'>OAuth consent · ZAI Memory Hub</div>
  <h1>Authorize {escape(client_name)}?</h1>
  <p>This client wants permission to read and write to your memory hub on your behalf.</p>
  <div class='grants'>
    <p style='margin:0 0 6px;font-family:JetBrains Mono,monospace;font-size:10px;letter-spacing:.3em;color:#c4924a;text-transform:uppercase'>It will be able to:</p>
    <ul style='margin:6px 0 0;padding-left:18px'>
      <li>read your memories &amp; decisions</li>
      <li>append new memories &amp; decisions</li>
      <li>log interactions (session boundaries)</li>
      <li>upsert entities</li>
    </ul>
    <p style='margin:8px 0 0;font-family:JetBrains Mono,monospace;font-size:10px;letter-spacing:.3em;color:#8b6a5a;text-transform:uppercase'>It will NOT be able to:</p>
    <ul style='margin:6px 0 0;padding-left:18px;color:#8b6a5a'>
      <li>delete any memory (you control deletes from /trash)</li>
    </ul>
  </div>
  <form method='post' action='/oauth/authorize/approve'>
    <input type='hidden' name='client_id' value='{escape(client_id)}'>
    <input type='hidden' name='redirect_uri' value='{escape(redirect_uri)}'>
    <input type='hidden' name='scope' value='{escape(scope)}'>
    <input type='hidden' name='state' value='{escape(state)}'>
    <input type='hidden' name='code_challenge' value='{escape(code_challenge)}'>
    <input type='hidden' name='code_challenge_method' value='{escape(code_challenge_method)}'>
    <input type='hidden' name='role' value='{escape(role)}'>
    <label>Slug this agent will write as <small>(stable identity; appears on your dashboard)</small></label>
    <input type='text' name='slug' value='{escape(slug_suggest)}' required maxlength='60'>
    <div class='actions'>
      <button type='submit' name='action' value='deny'>Deny</button>
      <button type='submit' name='action' value='approve' class='primary'>Approve as writer</button>
    </div>
  </form>
</div></body></html>
"""


@app.post("/oauth/authorize/approve")
async def oauth_authorize_approve(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    scope: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form("S256"),
    slug: str = Form(...),
    role: str = Form("writer"),
    action: str = Form(...),
):
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        raise HTTPException(401, "dashboard auth required")
    from urllib.parse import urlencode
    if action != "approve":
        return RedirectResponse(
            redirect_uri + "?" + urlencode({"error": "access_denied", "state": state}),
            status_code=302)
    # Issue an auth code
    code = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc).timestamp() + CODE_TTL_SECONDS
    expires_iso = datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()
    import re
    slug_clean = re.sub(r'[^a-z0-9-]', '-', slug.lower()).strip('-')[:60] or "agent"
    role_clean = role if role in ("writer", "recall-only") else "writer"
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO oauth_codes (code, client_id, redirect_uri, code_challenge, "
            "code_challenge_method, scope, slug, role, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (code, client_id, redirect_uri, code_challenge or None,
             code_challenge_method or None, scope, slug_clean, role_clean, expires_iso))
    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(redirect_uri + "?" + urlencode(params), status_code=302)


@app.post("/oauth/token")
async def oauth_token(request: Request):
    """Exchange an authorization code for a bearer token."""
    form = await request.form()
    grant_type = form.get("grant_type")
    code = form.get("code")
    redirect_uri = form.get("redirect_uri")
    client_id = form.get("client_id")
    code_verifier = form.get("code_verifier")
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    if not (code and redirect_uri and client_id):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT client_id, redirect_uri, code_challenge, code_challenge_method, "
            "scope, slug, role, expires_at, used_at "
            "FROM oauth_codes WHERE code = %s", (code,))
        row = cu.fetchone()
        if not row:
            return JSONResponse({"error": "invalid_grant", "detail": "unknown code"}, status_code=400)
        if row["used_at"] is not None:
            return JSONResponse({"error": "invalid_grant", "detail": "code already used"}, status_code=400)
        if row["expires_at"] < datetime.now(timezone.utc):
            return JSONResponse({"error": "invalid_grant", "detail": "code expired"}, status_code=400)
        if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
            return JSONResponse({"error": "invalid_grant", "detail": "client/redirect mismatch"}, status_code=400)
        # PKCE verification
        if row["code_challenge"]:
            if not code_verifier:
                return JSONResponse({"error": "invalid_grant", "detail": "code_verifier required"}, status_code=400)
            import hashlib as _h, base64 as _b
            method = row["code_challenge_method"] or "plain"
            if method == "S256":
                vh = _b.urlsafe_b64encode(_h.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
                if vh != row["code_challenge"]:
                    return JSONResponse({"error": "invalid_grant", "detail": "PKCE mismatch"}, status_code=400)
            else:  # plain
                if code_verifier != row["code_challenge"]:
                    return JSONResponse({"error": "invalid_grant", "detail": "PKCE mismatch"}, status_code=400)
        # Look up the client name for the token label
        cu.execute("SELECT client_name FROM oauth_clients WHERE client_id = %s", (client_id,))
        c = cu.fetchone()
        label = (c["client_name"] if c else client_id)[:120]
        # Mint the bearer token bound to slug+role
        access_token = _mint_agent_token(slug=row["slug"], role=row["role"],
                                          label=f"oauth · {label}", issued_via="oauth")
        cu.execute("UPDATE oauth_codes SET used_at = now() WHERE code = %s", (code,))
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "scope": row["scope"] or "mcp:read mcp:write",
    })


# ---- Token management (dashboard-side) --------------------------

@app.get("/api/tokens")
def api_tokens(_: None = Depends(require_auth)):
    with db() as cx, cx.cursor() as cu:
        cu.execute("""
            SELECT id, slug, role, label, issued_via, created_at, last_used_at, revoked_at
              FROM agent_tokens
             ORDER BY revoked_at IS NULL DESC, last_used_at DESC NULLS LAST, created_at DESC""")
        rows = cu.fetchall()
    return [{**r,
             "created_at": r["created_at"].isoformat() if r["created_at"] else None,
             "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
             "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None}
            for r in rows]


@app.post("/api/tokens/mint")
def api_tokens_mint(_: None = Depends(require_auth),
                    slug: str = Form(...), role: str = Form("writer"),
                    label: str = Form("")):
    role = role if role in ("admin", "writer", "recall-only") else "writer"
    import re
    slug_clean = re.sub(r'[^a-z0-9-]', '-', slug.lower()).strip('-')[:60] or "agent"
    token = _mint_agent_token(slug=slug_clean, role=role, label=label[:120], issued_via="manual")
    return {"ok": True, "slug": slug_clean, "role": role, "token": token,
            "warning": "save this token now; it will not be shown again"}


@app.post("/api/tokens/{tid}/revoke")
def api_tokens_revoke(tid: int, _: None = Depends(require_auth)):
    with db() as cx, cx.cursor() as cu:
        cu.execute("UPDATE agent_tokens SET revoked_at = now() WHERE id = %s "
                   "RETURNING slug, role", (tid,))
        row = cu.fetchone()
    if not row:
        raise HTTPException(404, "token not found")
    return {"ok": True, **row}


# ---- BLOCKS HOME endpoints ---------------------------------------

@app.get("/api/agents")
def api_agents(_: None = Depends(require_auth)):
    """Every distinct memory author + their presence + last 5 memories.

    Drives the Active Agents row of the Blocks Home.  New agents that
    write to the Hub for the first time appear here automatically —
    no config needed.  The 'kind' is heuristic: any slug ending in
    '-claude' is a claude instance; others are arbitrary agents.
    """
    with db() as cx, cx.cursor() as cu:
        # Distinct authors + counts + last_seen
        cu.execute("""
            SELECT written_by AS slug,
                   count(*)::int AS total,
                   max(created_at) AS last_seen
              FROM memories
             WHERE deleted_at IS NULL AND written_by IS NOT NULL
          GROUP BY written_by
          ORDER BY max(created_at) DESC NULLS LAST
        """)
        authors = cu.fetchall()
        out = []
        now = time.time()
        for a in authors:
            slug = a["slug"]
            age = (now - a["last_seen"].timestamp()) if a["last_seen"] else None
            status = "cold" if age is None else (
                "online" if age < 300 else ("recent" if age < 3600 else "idle"))
            cu.execute(
                "SELECT id::text, substring(content for 110) AS preview, "
                "importance, created_at, tags "
                "FROM memories WHERE deleted_at IS NULL AND written_by = %s "
                "ORDER BY created_at DESC LIMIT 5", (slug,))
            recent = [{
                "id": r["id"], "preview": r["preview"],
                "importance": r["importance"] or 3,
                "created_at": r["created_at"].isoformat(),
                "tags": r["tags"] or [],
            } for r in cu.fetchall()]
            kind = "claude" if slug.endswith("-claude") else "agent"
            display = slug.replace("-claude", "").upper() + ("-Claude" if kind == "claude" else "")
            out.append({
                "slug": slug, "kind": kind, "display": display,
                "status": status, "age_s": age,
                "last_seen": a["last_seen"].isoformat() if a["last_seen"] else None,
                "total": a["total"], "recent": recent,
            })
        return out


# Block definitions: each maps to a SQL predicate on tags, importance,
# or other heuristics.  Every block answers /api/block/{slug} the same
# way: { block: {...}, items: [...] }.
BLOCKS = {
    "philosophy": {
        "label": "Philosophy & Drafts",
        "sub": "Longer thinking, ideas, drafts",
        "tags": ["philosophy", "draft", "idea", "thought", "thinking", "essay", "note"],
        "accent": "#c9b08a",
    },
    "hacking": {
        "label": "Hacking & CTF",
        "sub": "HTB · CVE · payloads · the 50GB library",
        "tags": ["htb", "ctf", "pwn", "exploit", "recon", "payload", "shell", "reverse",
                 "web-ex", "binary", "rop", "buffer-overflow", "rce", "sqli", "xss",
                 "lfi", "rfi", "priv-esc", "pivot", "active-directory"],
        "accent": "#5ee2a0",
    },
    "crypto": {
        "label": "Crypto & Markets",
        "sub": "Trading framework · liquidity · structure",
        "tags": ["crypto", "market", "trade", "liquidity", "regime", "macro",
                 "btc", "eth", "framework", "anteroom"],
        "accent": "#ff9a4a",
    },
    "infra": {
        "label": "Infrastructure",
        "sub": "VPS · MCP · pipelines · systems",
        "tags": ["infra", "vps", "mcp", "systemd", "pipeline", "deploy",
                 "config", "tech-debt", "state"],
        "accent": "#4adcff",
    },
    "decisions": {
        "label": "Decisions",
        "sub": "Logged with rationale + alternatives",
        "kind": "decisions",
        "accent": "#dc2626",
    },
    "references": {
        "label": "References",
        "sub": "Entities · people · projects · threads",
        "kind": "entities",
        "accent": "#e8d49a",
    },
    "now-building": {
        "label": "Now Building",
        "sub": "Current ship · milestones",
        "tags": ["milestone", "ship", "in-flight", "ui", "feature", "build"],
        "accent": "#ff5046",
    },
    "tools": {
        "label": "Tool Calls",
        "sub": "Recent MCP tool invocations",
        "kind": "tools",
        "accent": "#7aa6ff",
    },
}


def _block_count_and_items(slug):
    block = BLOCKS.get(slug)
    if not block:
        return None
    with db() as cx, cx.cursor() as cu:
        kind = block.get("kind")
        if kind == "decisions":
            cu.execute("SELECT count(*)::int AS n FROM decisions")
            n = cu.fetchone()["n"]
            cu.execute(
                "SELECT id::text, summary, rationale, alternatives, written_by, created_at "
                "FROM decisions WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 30")
            items = [{
                "id": r["id"], "title": r["summary"], "preview": r["rationale"],
                "written_by": r["written_by"], "created_at": r["created_at"].isoformat(),
                "alternatives": r["alternatives"] or [],
                "kind": "decision",
            } for r in cu.fetchall()]
        elif kind == "tools":
            cu.execute("SELECT count(*)::int AS n FROM tool_calls")
            n = cu.fetchone()["n"]
            cu.execute(
                "SELECT id, tool_name, called_by, duration_ms, status, result_brief, created_at "
                "FROM tool_calls ORDER BY created_at DESC LIMIT 40")
            items = [{
                "id": str(r["id"]), "title": r["tool_name"],
                "preview": r["result_brief"] or f"{r['duration_ms'] or 0}ms · {r['status']}",
                "written_by": r["called_by"], "created_at": r["created_at"].isoformat(),
                "kind": "tool_call",
            } for r in cu.fetchall()]
        elif kind == "entities":
            cu.execute("SELECT count(*)::int AS n FROM entities")
            n = cu.fetchone()["n"]
            cu.execute(
                "SELECT id::text, slug, kind AS ent_kind, display "
                "FROM entities ORDER BY display")
            items = [{
                "id": r["id"], "title": r["display"], "preview": r["ent_kind"],
                "written_by": None, "created_at": None,
                "slug": r["slug"], "ent_kind": r["ent_kind"],
                "kind": "entity",
            } for r in cu.fetchall()]
        else:
            # Tag-based predicate
            tag_array = "ARRAY[" + ",".join(f"'{t}'" for t in block["tags"]) + "]::text[]"
            cu.execute(
                f"SELECT count(*)::int AS n FROM memories WHERE deleted_at IS NULL AND tags && {tag_array}")
            n = cu.fetchone()["n"]
            cu.execute(
                f"SELECT id::text, substring(content for 180) AS preview, "
                f"content, tags, written_by, importance, created_at "
                f"FROM memories WHERE deleted_at IS NULL AND tags && {tag_array} "
                f"ORDER BY created_at DESC LIMIT 50")
            items = [{
                "id": r["id"], "title": (r["preview"] or "")[:80],
                "preview": r["preview"], "full": r["content"],
                "written_by": r["written_by"], "created_at": r["created_at"].isoformat(),
                "tags": r["tags"] or [], "importance": r["importance"] or 3,
                "kind": "memory",
            } for r in cu.fetchall()]
        return {"block": {"slug": slug, **block}, "count": n, "items": items}


@app.get("/api/blocks")
def api_blocks(_: None = Depends(require_auth)):
    """Counts for every block — drives the home grid summaries."""
    out = []
    for slug in BLOCKS:
        d = _block_count_and_items(slug)
        if d is None:
            continue
        # Strip items, return summary
        out.append({
            "slug": slug, **BLOCKS[slug],
            "count": d["count"],
            "preview_items": d["items"][:3],
        })
    return out


@app.get("/api/block/{slug}")
def api_block(slug: str, _: None = Depends(require_auth)):
    d = _block_count_and_items(slug)
    if d is None:
        raise HTTPException(404, "unknown block")
    return d


# ---- DOCUMENT UPLOAD ---------------------------------------------

UPLOADS_DIR = STATIC_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024     # 20 MB cap per file


def _infer_doc_tags(text: str):
    """Heuristic — pick up to ~6 topical tags from a document's first
    chunk of text.  Same vocabulary as the knowledge blocks so docs
    show up in the right places."""
    text_l = (text or "").lower()
    cands = {
        # hacking
        "htb": ["htb", "hack the box"],
        "ctf": ["ctf", "capture the flag"],
        "cve": ["cve-"],
        "exploit": ["exploit"],
        "rce": ["rce", "remote code execution"],
        "sqli": ["sqli", "sql injection"],
        "xss": ["xss", "cross-site"],
        "pwn": ["pwn"],
        "recon": ["recon", "reconnaissance"],
        "shell": ["reverse shell", "bind shell"],
        # philosophy
        "philosophy": ["philosophy", "consciousness", "metaphysics"],
        "idea": [" idea ", "concept", "theory"],
        "thought": ["thought", "thinking"],
        # crypto / markets
        "crypto": ["bitcoin", "cryptocurrency", " btc ", " eth "],
        "trade": ["trading", "trader", "trade setup"],
        "market": ["market structure", "macro"],
        "liquidity": ["liquidity"],
        # infra
        "infra": ["infrastructure", "deployment", "kubernetes", " k8s "],
        "vps": [" vps ", "virtual private server"],
        "mcp": [" mcp ", "model context protocol"],
        # ai
        "ai": [" ai ", "artificial intelligence", "machine learning", " llm "],
    }
    found = []
    for tag, hints in cands.items():
        if any(h in text_l for h in hints):
            found.append(tag)
        if len(found) >= 6:
            break
    if not found:
        found = ["document"]
    return found


def _extract_pdf_text(data: bytes, max_chars: int = 2400):
    """Pull the first page or two of text from a PDF.  Returns
    (title, body, page_count).  title is the first non-empty line."""
    if not HAS_PDF:
        return None, None, 0
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        n = len(reader.pages)
        chunks = []
        for p in reader.pages[:4]:
            try:
                t = p.extract_text() or ""
            except Exception:
                t = ""
            if t:
                chunks.append(t)
            if sum(len(c) for c in chunks) > max_chars:
                break
        body = "\n".join(chunks)[:max_chars]
        # Title: first non-empty stripped line, capped at 110 chars
        title = ""
        for line in body.splitlines():
            line = line.strip()
            if line:
                title = line[:110]
                break
        if not title:
            title = "Untitled document"
        # Document metadata may have a /Title key
        meta = reader.metadata or {}
        meta_title = getattr(meta, "title", None) or meta.get("/Title") if hasattr(meta, "get") else None
        if meta_title:
            title = str(meta_title)[:110]
        return title, body, n
    except Exception as e:
        return None, None, 0


REPLICATE_TOKEN_PATH = Path(os.environ.get("REPLICATE_TOKEN_PATH", Path(__file__).resolve().parent.parent / "auth" / "replicate.token"))
COVERS_DIR = STATIC_DIR / "uploads" / "covers"
COVERS_DIR.mkdir(parents=True, exist_ok=True)


def _generate_cover(short_sha: str, title: str, tags: list, body_excerpt: str) -> str | None:
    """Synchronously generate a Flux 1.1 Pro cover for a PDF.

    Returns the public URL of the saved cover image, or None on any
    failure (Replicate down, no token, model rejected the prompt, etc.)
    so the upload itself never breaks on cover failure.
    """
    if not REPLICATE_TOKEN_PATH.exists():
        return None
    token = REPLICATE_TOKEN_PATH.read_text().strip()
    if not token:
        return None
    import urllib.request, urllib.error
    # Compose a prompt from the user's title + tags + a short excerpt.
    # Style anchor matches the rest of the site (crimson + gold, editorial).
    style = (
        "editorial book cover illustration, deep crimson and dark gold palette, "
        "candlelit library aesthetic, dramatic chiaroscuro, no text, no letters, "
        "no people, single central symbolic object, painterly, "
        "premium publishing brand, vertical book-cover composition"
    )
    seed_words = (title + " " + " ".join(tags))[:140]
    body_hint = (body_excerpt or "")[:200].strip().replace("\n", " ")
    prompt = (
        f"Symbolic editorial cover representing: {seed_words}. "
        f"Subject matter: {body_hint}. {style}."
    )
    model = "black-forest-labs/flux-1.1-pro"
    body = {
        "input": {
            "prompt": prompt,
            "aspect_ratio": "4:5",
            "output_format": "jpg",
            "output_quality": 88,
            "safety_tolerance": 5,
            "prompt_upsampling": False,
        }
    }
    try:
        req = urllib.request.Request(
            f"https://api.replicate.com/v1/models/{model}/predictions",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "wait=30",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            pred = json.loads(r.read())
        # poll if not done
        deadline = time.time() + 90
        while pred.get("status") in ("starting", "processing") and time.time() < deadline:
            time.sleep(2)
            req = urllib.request.Request(
                f"https://api.replicate.com/v1/predictions/{pred['id']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                pred = json.loads(r.read())
        if pred.get("status") != "succeeded":
            return None
        out = pred.get("output")
        if isinstance(out, list):
            out = out[0] if out else None
        if not out:
            return None
        dest = COVERS_DIR / f"{short_sha}.jpg"
        with urllib.request.urlopen(out, timeout=60) as r, open(dest, "wb") as f:
            f.write(r.read())
        # Spend log (matches scripts/gen.py format)
        try:
            spend_log = Path(__file__).resolve().parent.parent / "scripts" / "replicate-spend.log"
            with open(spend_log, "a") as lf:
                lf.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  "
                         f"{model:<35}  $0.040  cover-{short_sha[:8]:<18}  {dest.stat().st_size/1024:.0f}KB\n")
        except Exception:
            pass
        return f"/static/uploads/covers/{short_sha}.jpg"
    except Exception as e:
        print(f"[cover] generation failed for {short_sha}: {e}", file=sys.stderr)
        return None


@app.post("/api/upload")
async def api_upload(
    file: UploadFile = File(...),
    title: str = Form(""),
    description: str = Form(""),
    tags_csv: str = Form(""),
    generate_cover: str = Form("false"),
    written_by: str = Form("vps-claude"),
    _: None = Depends(require_auth),
):
    """Upload a PDF with editable metadata + optional Flux cover art.

    Title, description, and tags_csv come from the upload form (the user
    sees auto-extracted values pre-filled but can edit them before save).
    generate_cover="true" triggers a one-shot Flux 1.1 Pro call (~$0.04)
    that produces a 4:5 portrait cover saved under /static/uploads/covers/.
    """
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large (cap 20 MB)")
    fname = (file.filename or "untitled").strip()
    if not fname.lower().endswith(".pdf"):
        raise HTTPException(415, "only PDF accepted for now")
    sha = hashlib.sha256(data).hexdigest()
    short_sha = sha[:16]
    saved_name = f"{short_sha}.pdf"
    saved_path = UPLOADS_DIR / saved_name
    if not saved_path.exists():
        saved_path.write_bytes(data)
    # Extract for fallback + body excerpt
    extracted_title, body, pages = _extract_pdf_text(data)
    body = body or ""
    final_title = (title.strip() or extracted_title or fname.rsplit(".", 1)[0])[:200]
    final_desc = description.strip()
    # Parse user tags + always include 'document'
    user_tags = [t.strip().lower() for t in tags_csv.split(",") if t.strip()]
    if not user_tags:
        user_tags = _infer_doc_tags(body)
    if "document" not in user_tags:
        user_tags.append("document")
    user_tags = user_tags[:8]   # hard cap
    # Optional cover
    cover_url = None
    if generate_cover.lower() in ("1", "true", "yes", "on"):
        cover_url = _generate_cover(short_sha, final_title, user_tags, body)
    # Memory content layout
    content = (
        f"{final_title}\n\n"
        f"{(final_desc + chr(10) + chr(10)) if final_desc else ''}"
        f"{body[:1400]}{'…' if len(body) > 1400 else ''}\n\n"
        f"[Document]\n"
        f"filename: {fname}\n"
        f"saved_as: /static/uploads/{saved_name}\n"
        f"sha256: {short_sha}\n"
        f"pages: {pages}\n"
        f"size_bytes: {len(data)}\n"
        f"{('cover: ' + cover_url + chr(10)) if cover_url else ''}"
        f"{('user_description: ' + final_desc + chr(10)) if final_desc else ''}"
    )
    with db() as cx, cx.cursor() as cu:
        cu.execute("""
            INSERT INTO memories (content, tags, written_by, importance)
            VALUES (%s, %s, %s, 3)
            RETURNING id::text, created_at
        """, (content, user_tags, written_by))
        row = cu.fetchone()
    _audit("memory", row["id"], "insert",
           actor=written_by,
           detail={"kind": "pdf_upload", "filename": fname,
                   "cover_generated": cover_url is not None})
    return {
        "ok": True,
        "memory_id": row["id"],
        "url": f"/static/uploads/{saved_name}",
        "cover_url": cover_url,
        "filename": fname,
        "title": final_title,
        "description": final_desc,
        "pages": pages,
        "size_bytes": len(data),
        "tags": user_tags,
    }


@app.post("/api/upload/preview")
async def api_upload_preview(
    file: UploadFile = File(...),
    _: None = Depends(require_auth),
):
    """Pre-upload: extract title + body excerpt + inferred tags from a PDF
    without saving it.  Used by the front-end form so the user sees what
    will be indexed and can edit before committing."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large (cap 20 MB)")
    fname = (file.filename or "untitled").strip()
    if not fname.lower().endswith(".pdf"):
        raise HTTPException(415, "only PDF accepted for now")
    extracted_title, body, pages = _extract_pdf_text(data)
    body = body or ""
    fallback_title = extracted_title or fname.rsplit(".", 1)[0]
    tags = _infer_doc_tags(body)
    return {
        "ok": True,
        "title": fallback_title[:200],
        "excerpt": body[:600],
        "tags": tags,
        "pages": pages,
        "size_bytes": len(data),
        "filename": fname,
    }


@app.get("/api/documents")
def api_documents(_: None = Depends(require_auth), n: int = 40):
    """All uploaded documents (memories tagged 'document')."""
    with db() as cx, cx.cursor() as cu:
        cu.execute("""
            SELECT id::text, content, tags, written_by, importance, created_at
              FROM memories
             WHERE deleted_at IS NULL AND 'document' = ANY(tags)
          ORDER BY created_at DESC LIMIT %s
        """, (n,))
        rows = cu.fetchall()
    out = []
    for r in rows:
        content = r["content"] or ""
        meta = {}
        if "[Document]" in content:
            footer = content.split("[Document]")[1]
            for line in footer.strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            summary = content.split("[Document]")[0].strip()
        else:
            summary = content
        # Title is the first non-empty line of the summary
        title = next((ln.strip() for ln in summary.splitlines() if ln.strip()), "Untitled")
        out.append({
            "id": r["id"], "title": title[:140],
            "summary": summary[:500],
            "tags": [t for t in (r["tags"] or []) if t not in ("document",)],
            "written_by": r["written_by"], "importance": r["importance"],
            "created_at": r["created_at"].isoformat(),
            "filename": meta.get("filename", ""),
            "url": meta.get("saved_as", ""),
            "cover_url": meta.get("cover", ""),
            "pages": meta.get("pages", ""),
            "size_bytes": meta.get("size_bytes", ""),
            "user_description": meta.get("user_description", ""),
        })
    return {"count": len(out), "items": out}


@app.get("/api/library")
def api_library(q: str = "", n: int = 80, _: None = Depends(require_auth)):
    """Library-entry memories — the index of local-Claude's 50GB vault.

    Each entry is a memory tagged 'library-entry' whose content ends with
    a structured `[Library-entry]` footer (path, size, sha, repo, etc).
    Search by tag, path, or content substring with ?q=...
    """
    with db() as cx, cx.cursor() as cu:
        if q:
            cu.execute(
                "SELECT id::text, content, tags, written_by, importance, created_at "
                "FROM memories WHERE deleted_at IS NULL AND 'library-entry' = ANY(tags) "
                "AND (content ILIKE %s OR EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ILIKE %s)) "
                "ORDER BY created_at DESC LIMIT %s",
                (f"%{q}%", f"%{q}%", n))
        else:
            cu.execute(
                "SELECT id::text, content, tags, written_by, importance, created_at "
                "FROM memories WHERE deleted_at IS NULL AND 'library-entry' = ANY(tags) "
                "ORDER BY created_at DESC LIMIT %s", (n,))
        rows = cu.fetchall()
    # Parse the footer for each entry
    out = []
    for r in rows:
        content = r["content"] or ""
        meta = {}
        if "[Library-entry]" in content:
            footer = content.split("[Library-entry]")[1]
            for line in footer.strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            summary = content.split("[Library-entry]")[0].strip()
        else:
            summary = content
        out.append({
            "id": r["id"], "summary": summary[:500],
            "tags": [t for t in (r["tags"] or []) if t != "library-entry"],
            "written_by": r["written_by"], "importance": r["importance"],
            "created_at": r["created_at"].isoformat(),
            **meta,
        })
    return {"q": q, "count": len(out), "items": out}


TIMELINE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZAI · Full Timeline</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;600&family=Cormorant+Garamond:ital,wght@1,400;1,500&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --bg:#0a0508;--surface:#140709;--line:#2a1818;--line-bright:#3a2222;
  --fg:#f5ecdb;--fg-soft:#d4c3a0;--fg-dim:#8a7a6a;--gold:#e8d49a;--gold-bright:#f5dca3;--gold-deep:#8c6f3a;
  --red:#a01a1a;--red-bright:#dc2626;--red-warm:#ff7060;
  --serif:'Cinzel',serif;--serif-soft:'Cormorant Garamond',serif;
  --sans:'Inter',system-ui,sans-serif;--mono:'JetBrains Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--fg);font-family:var(--sans);line-height:1.6;min-height:100vh}
a{color:var(--red-warm);text-decoration:none;border-bottom:1px dotted}
a:hover{color:var(--gold-bright);border-bottom-color:var(--gold-bright)}
.hdr{position:sticky;top:0;z-index:50;background:linear-gradient(180deg,rgba(10,5,8,0.96),rgba(10,5,8,0.85));backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
.hdr-inner{max-width:960px;margin:0 auto;display:flex;align-items:center;gap:14px;padding:14px 28px}
.hdr-logo{font-family:var(--serif);font-weight:600;letter-spacing:.45em;font-size:13px;background:linear-gradient(180deg,var(--gold-bright) 0%,var(--gold) 50%,var(--gold-deep) 100%);-webkit-background-clip:text;background-clip:text;color:transparent;padding-left:.45em;border-bottom:0}
.back{font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;border-bottom:0;padding:7px 12px;border:1px solid var(--line-bright);border-radius:2px;margin-left:auto}
.back:hover{color:var(--red-warm);border-color:var(--red)}
.wrap{max-width:960px;margin:0 auto;padding:50px 28px 80px}
.eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.4em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:10px}
h1{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:38px;color:var(--gold-bright);margin-bottom:6px;line-height:1.1}
.sub{font-family:var(--serif-soft);font-style:italic;font-size:16px;color:var(--fg-soft);margin-bottom:36px}
.tl-section{margin-bottom:32px}
.tl-section-head{font-family:var(--mono);font-size:10px;letter-spacing:.36em;color:var(--gold);text-transform:uppercase;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.tl-row{display:grid;grid-template-columns:90px 14px 1fr;gap:0;align-items:start;padding:11px 0;border-top:1px dashed var(--line);cursor:pointer;transition:background .12s;border-bottom:0}
.tl-row:first-child{border-top:0}
.tl-row:hover{background:rgba(220,38,38,0.05)}
.tl-time{font-family:var(--mono);font-size:10px;color:var(--gold-deep);letter-spacing:.06em;padding-top:5px}
.tl-bullet{width:7px;height:7px;border-radius:50%;margin-top:8px;box-shadow:0 0 4px currentColor;justify-self:start}
.tl-content{padding-left:14px}
.tl-text{font-family:var(--sans);font-size:14px;line-height:1.55;color:var(--fg-soft)}
.tl-meta{font-family:var(--mono);font-size:10px;color:var(--gold-deep);letter-spacing:.06em;margin-top:5px}
.loading{font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim);text-align:center;padding:40px}
</style>
</head>
<body>
<header class="hdr"><div class="hdr-inner">
  <a class="hdr-logo" href="/">ZAI</a>
  <a class="back" href="/">← Back to Hub</a>
</div></header>
<main class="wrap">
  <div class="eyebrow">Chronological feed</div>
  <h1>The full timeline</h1>
  <p class="sub">Every memory written to the Hub, oldest visible first.</p>
  <div id="tl"><div class="loading">Loading…</div></div>
</main>
<script>
function esc(s){if(s==null)return'';return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
function trunc(s,n){s=s||'';return s.length>n?s.slice(0,n).trimEnd()+'…':s;}
function timeAgo(iso){if(!iso)return'—';const t=new Date(iso).getTime();const d=Math.max(1,(Date.now()-t)/1000);if(d<60)return Math.floor(d)+'s';if(d<3600)return Math.floor(d/60)+'m';if(d<86400)return Math.floor(d/3600)+'h';if(d<86400*7)return Math.floor(d/86400)+'d';return new Date(iso).toLocaleDateString('en-CA',{month:'short',day:'numeric'});}
function colorFor(s){const c=['#dc2626','#ff5046','#7aa6ff','#5ee2a0','#e8d49a','#c084ff','#ff9a4a','#4adcff'];let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return c[Math.abs(h)%c.length];}
fetch('/api/timeline?n=200').then(r=>r.json()).then(items=>{
  if(!items.length){document.getElementById('tl').innerHTML='<div class="loading">No memories yet</div>';return;}
  const today0=new Date();today0.setHours(0,0,0,0);
  const yest0=new Date(today0.getTime()-86400*1000);
  const wk0=new Date(today0.getTime()-7*86400*1000);
  const buckets={now:[],today:[],yest:[],week:[],earlier:[]};
  for(const m of items){const t=new Date(m.created_at);const age=(Date.now()-t.getTime())/1000;
    if(age<300)buckets.now.push(m);else if(t>=today0)buckets.today.push(m);else if(t>=yest0)buckets.yest.push(m);else if(t>=wk0)buckets.week.push(m);else buckets.earlier.push(m);}
  const secs=[['Now · within 5 minutes',buckets.now],['Today',buckets.today],['Yesterday',buckets.yest],['This week',buckets.week],['Earlier',buckets.earlier]].filter(s=>s[1].length);
  document.getElementById('tl').innerHTML=secs.map(([h,arr])=>`<section class="tl-section">
    <div class="tl-section-head">${esc(h)} · ${arr.length}</div>
    ${arr.map(m=>`<div class="tl-row" data-mid="${esc(m.id)}"><div class="tl-time">${esc(timeAgo(m.created_at))}</div><div class="tl-bullet" style="background:${colorFor(m.written_by||'')}"></div><div class="tl-content"><div class="tl-text">${esc(trunc(m.preview,200))}</div><div class="tl-meta">${esc((m.written_by||'').replace('-claude','').toUpperCase())} · imp ${m.importance||3}${(m.tags||[]).length?' · '+m.tags.slice(0,4).map(t=>'#'+t).join(' '):''}</div></div></div>`).join('')}
  </section>`).join('');
});
</script>
</body></html>
"""


@app.get("/timeline", response_class=HTMLResponse)
def timeline_route(request: Request):
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0a0508;color:#f5ecdb'>"
            "ZAI Memory Hub — auth required. Visit <code>/login?key=YOUR_KEY</code>.</body></html>",
            status_code=401)
    return HTMLResponse(TIMELINE_HTML)


@app.get("/api/timeline")
def api_timeline(n: int = 30, _: None = Depends(require_auth)):
    """Last N memories chronologically — drives the Timing block."""
    with db() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT id::text, substring(content for 140) AS preview, "
            "tags, written_by, importance, created_at "
            "FROM memories WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT %s", (n,))
        return [{**r, "created_at": r["created_at"].isoformat()} for r in cu.fetchall()]


@app.get("/api/cluster_links")
def api_cluster_links(_: None = Depends(require_auth)):
    """Semantic edges between categories — pairs that share tags.

    Returned as a list of { a, b, tags, weight }.  This is the canonical
    answer to 'what does the cross-arc between two orbs in the universe
    actually mean?'  Future AI agents reading this endpoint should treat
    the response as: 'these two categories share at least one tag, so
    memories living in one are semantically related to memories in the
    other'.  Weight = count of shared distinct tags.
    """
    pairs = []
    non_core = [c for c in CLUSTERS if c["slug"] != "core"]
    with db() as cx, cx.cursor() as cu:
        for i, c1 in enumerate(non_core):
            p1, _ = _cluster_predicate(c1["slug"])
            for c2 in non_core[i+1:]:
                p2, _ = _cluster_predicate(c2["slug"])
                cu.execute(f"""
                    SELECT array_agg(DISTINCT t) AS shared FROM (
                      SELECT unnest(tags) AS t FROM memories WHERE deleted_at IS NULL AND ({p1})
                      INTERSECT
                      SELECT unnest(tags) FROM memories WHERE deleted_at IS NULL AND ({p2})
                    ) x
                """)
                row = cu.fetchone()
                shared = (row["shared"] or []) if row else []
                if shared:
                    pairs.append({
                        "a": c1["slug"], "b": c2["slug"],
                        "shared_tags": shared, "weight": len(shared),
                    })
    return pairs


@app.get("/api/visual_legend")
def api_visual_legend(_: None = Depends(require_auth)):
    """Self-documenting endpoint — what every visual element means.

    Future AI agents stumbling into this dashboard should hit this
    endpoint first to understand the visual vocabulary.  Each entry
    pairs a UI element (the thing you see) with what it represents
    (the underlying data), and how to query that data programmatically.
    """
    return {
        "core_sphere": {
            "what": "every memory in the system, rendered as a shaded sphere with the ZAI lockup textured inside",
            "represents": "row count of memories table",
            "query": "GET /api/stats → memories",
        },
        "category_orb": {
            "what": "one of 8 colored orbs ringed around CORE: coding / web / mobile / github / agents / planning / longterm / terminal",
            "represents": "a semantic slice of memories defined by tags + author",
            "query": "GET /api/clusters returns counts; GET /api/cluster/{slug} returns the members",
            "slugs": [c["slug"] for c in CLUSTERS if c["slug"] != "core"],
        },
        "spoke": {
            "what": "a glowing line from CORE outward to a category orb, color = category color",
            "represents": "this category belongs to CORE (membership relation)",
            "weight_meaning": "spoke brightness pulses with recent activity in that category",
        },
        "cross_arc": {
            "what": "a soft curved chord connecting two category orbs through space",
            "represents": "the two categories share at least one tag (semantic overlap)",
            "query": "GET /api/cluster_links",
        },
        "bead_flow": {
            "what": "small bright beads continuously travelling along spokes",
            "represents": "ambient energy / 'this universe is alive'; not direction-of-flow",
            "spawn_rate_ms": 200,
        },
        "memory_flyer": {
            "what": "a single very bright glow particle physically traveling from one bubble to another",
            "represents": "a real memory.add SSE event happening NOW; author → referenced entity",
            "trigger": "Postgres NOTIFY on memories insert",
        },
        "core_sonar_ring": {
            "what": "an expanding red+white ring radiating outward from CORE",
            "represents": "any SSE activity (memories | decisions | tool_calls insert)",
            "duration_ms": 1500,
        },
        "actor_heartbeat": {
            "what": "a red ring pulsing outward from an actor bubble (vps-claude / local-claude / chat-claude)",
            "represents": "actor wrote anything (memory / decision / tool_call) within the last 5 minutes",
            "query": "GET /api/presence",
        },
        "bubble_texture": {
            "what": "the interior image visible through the front-facing hemisphere of each orb",
            "represents": "a category's 'essence' visualization — generated bespoke per category via Flux 1.1 Pro",
            "source": "static/gen/cat_{slug}.jpg + static/gen/zai_lockup.jpg for CORE",
        },
        "narrate_banner": {
            "what": "a gold-bordered caption that fades in at lower-center after 30s idle",
            "represents": "autonomous tour mode — camera auto-orbits each category",
            "exit": "any user input",
        },
    }


@app.get("/api/memory_stream")
def api_memory_stream(_: None = Depends(require_auth), buckets: int = 30):
    """Tool_call counts in 60-second buckets for the last `buckets` minutes — sparkline."""
    with db() as cx, cx.cursor() as cu:
        cu.execute("""
            WITH series AS (
              SELECT generate_series(
                date_trunc('minute', now()) - (%s::int - 1) * interval '1 minute',
                date_trunc('minute', now()),
                interval '1 minute'
              ) AS bucket
            )
            SELECT series.bucket,
              (SELECT count(*)::int FROM tool_calls
                WHERE created_at >= series.bucket
                  AND created_at <  series.bucket + interval '1 minute') AS n
            FROM series ORDER BY series.bucket
        """, (buckets,))
        rows = cu.fetchall()
    return [{"t": r["bucket"].isoformat(), "n": r["n"]} for r in rows]


@app.get("/api/presence")
def api_presence(_: None = Depends(require_auth)):
    """Live presence rail. Reads the set of agents from agent_tokens
    (active, never-revoked) instead of a hardcoded list, so any agent that
    has been granted a token shows up here — even before its first write."""
    with db() as cx, cx.cursor() as cu:
        # Every active agent gets a row, ordered by most-recently-used
        cu.execute("""
            SELECT slug, role, label, last_used_at
              FROM agent_tokens
             WHERE revoked_at IS NULL
             ORDER BY last_used_at DESC NULLS LAST, created_at DESC""")
        token_rows = cu.fetchall()
        # And separately, what's the most-recent timestamp this slug
        # actually wrote anything (memory / decision / tool call).
        cu.execute("""
            SELECT actor, max(t) AS last_seen FROM (
              SELECT called_by AS actor, created_at AS t FROM tool_calls WHERE called_by IS NOT NULL
              UNION ALL
              SELECT written_by, created_at FROM memories WHERE written_by IS NOT NULL
              UNION ALL
              SELECT written_by, created_at FROM decisions WHERE written_by IS NOT NULL
              UNION ALL
              SELECT actor, created_at FROM audit_log WHERE actor IS NOT NULL
            ) x GROUP BY actor
        """)
        last = {r["actor"]: r["last_seen"] for r in cu.fetchall()}
    now = time.time()
    out = []
    seen_slugs = set()
    for r in token_rows:
        slug = r["slug"]
        seen_slugs.add(slug)
        # Prefer last activity (write), fall back to last_used_at on the token,
        # so an agent that authenticated but never wrote still shows as "recent"
        # rather than "never".
        ts = last.get(slug) or r["last_used_at"]
        if ts is None:
            out.append({"slug": slug, "role": r["role"], "label": r["label"],
                        "status": "cold", "age_s": None, "last_seen": None})
            continue
        age = now - ts.timestamp()
        status = "online" if age < 300 else ("recent" if age < 3600 else "idle")
        out.append({"slug": slug, "role": r["role"], "label": r["label"],
                    "status": status, "age_s": age,
                    "last_seen": ts.isoformat()})
    # Also surface slugs that wrote to the hub but no longer have an active
    # token (revoked/deleted). They become "ghost" entries — useful for
    # provenance, sit at the bottom.
    for slug, ts in last.items():
        if slug in seen_slugs:
            continue
        age = now - ts.timestamp()
        out.append({"slug": slug, "role": "(revoked)", "label": "no active token",
                    "status": "ghost", "age_s": age,
                    "last_seen": ts.isoformat()})
    return out


# ---- SSE ---------------------------------------------------------

async def _sse_loop(request: Request):
    aconn = await psycopg.AsyncConnection.connect(DB_DSN, autocommit=True)
    async with aconn.cursor() as cu:
        await cu.execute("LISTEN zai_hub_activity")
    yield f"event: connected\ndata: {json.dumps({'at': time.time()})}\n\n"
    try:
        async for notif in aconn.notifies():
            if await request.is_disconnected():
                break
            yield f"event: activity\ndata: {notif.payload}\n\n"
    finally:
        await aconn.close()


@app.get("/events")
async def events(request: Request, _: None = Depends(require_auth)):
    return StreamingResponse(_sse_loop(request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- HTML --------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#08030a">
<title>ZAI · Living Intelligence</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;500;600;700&family=Cormorant+Garamond:wght@300;400;500&family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<!-- Three.js + GSAP -->
<script type="importmap">
{
  "imports": {
    "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
    "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
  }
}
</script>
<script src="https://unpkg.com/gsap@3.12.5/dist/gsap.min.js"></script>
<script type="module">
  import { initCloud3D } from '/static/cloud3d.js';
  (async () => {
    try {
      window.Cloud3D = await initCloud3D(document.getElementById('cloud3d'));
      window.dispatchEvent(new Event('cloud3d-ready'));
      console.log('[ZAI] cloud3d ready');
    } catch (e) {
      console.error('[ZAI] cloud3d init failed', e);
    }
  })();
</script>
<style>
:root{
  --bg-base:        #08030a;
  --fg:             #f5ecdb;
  --fg-soft:        #d4c3a0;
  --fg-dim:         #8a7a6a;
  --muted:          #5a4d44;
  --line:           #2a1818;
  --line-soft:      #3a2828;
  --line-bright:    #4a2424;
  --panel:          rgba(12,5,8,0.72);
  --panel-solid:    #100608;
  --gold:           #e8d49a;
  --gold-bright:    #f5dca3;
  --gold-deep:      #8c6f3a;
  --red-deep:       #4a0e10;
  --red:            #a01a1a;
  --red-bright:     #dc2626;
  --red-hot:        #ff3a3a;
  --red-warm:       #ff7060;
  --c-coding:       #ff5046;
  --c-web:          #7aa6ff;
  --c-mobile:       #5ee2a0;
  --c-github:       #e8d49a;
  --c-agents:       #c084ff;
  --c-planning:     #ff9a4a;
  --c-longterm:     #dc2626;
  --c-terminal:     #4adcff;
  --serif:          'Cinzel', serif;
  --serif-soft:     'Cormorant Garamond', serif;
  --sans:           'Inter', -apple-system, system-ui, sans-serif;
  --mono:           'JetBrains Mono', ui-monospace, Menlo, monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100vh;width:100vw;overflow:hidden;background:var(--bg-base);color:var(--fg);font-family:var(--sans)}
button{font-family:inherit;cursor:pointer;background:none;border:none;color:inherit}

/* ===== BACKDROP layers ===== */
#bg{position:fixed;inset:-4%;z-index:0;background-color:#1a0508;background-image:url(/static/nebula.jpg);background-size:cover;background-position:center;
  filter: sepia(0.55) hue-rotate(-22deg) saturate(2.8) brightness(0.96) contrast(1.22);
  transform:scale(1.08);
  animation: nebDrift 90s ease-in-out infinite alternate;
}
@keyframes nebDrift{
  0%   { transform:scale(1.08) translate(0,0) }
  50%  { transform:scale(1.12) translate(-1.4%, -0.8%) }
  100% { transform:scale(1.10) translate(1.0%,  -1.2%) }
}
#bg-glow{position:fixed;inset:0;z-index:1;pointer-events:none;
  background:
    radial-gradient(ellipse 110% 70% at 50% 0%,  rgba(255,70,50,0.18), transparent 70%),
    radial-gradient(ellipse 40% 40% at 18% 30%,  rgba(255,90,60,0.10), transparent 70%),
    radial-gradient(ellipse 38% 40% at 82% 65%,  rgba(255,80,80,0.12), transparent 70%);
  mix-blend-mode: screen;
  animation: bgGlow 12s ease-in-out infinite alternate;
}
@keyframes bgGlow{from{opacity:.75}to{opacity:1}}
#bg-vignette{position:fixed;inset:0;z-index:1;pointer-events:none;
  background:
    radial-gradient(ellipse 70% 95% at 50% 100%, rgba(10,3,6,0.78), rgba(10,3,6,0.20) 55%, transparent),
    radial-gradient(ellipse 60% 60% at 50% 50%, transparent 0%, rgba(10,3,6,0.30) 100%);
}
#dust,#stars,#cloud,#cloud3d,#zoom{position:fixed;inset:0;display:block}
#dust{z-index:2;pointer-events:none}
#stars{z-index:3;pointer-events:none}
#cloud{z-index:4;pointer-events:none;cursor:default}                 /* 2D layer — labels only when WebGL active */
#cloud3d{z-index:4;pointer-events:auto;cursor:default}             /* WebGL bubbles + bloom */
#zoom{z-index:5;pointer-events:none;opacity:0;transition:opacity .35s}
#zoom.active{pointer-events:auto;opacity:1}
body.no-webgl #cloud{opacity:1;pointer-events:auto}
body.no-webgl #cloud3d{display:none}

/* ===== APP GRID ===== */
.app{position:fixed;inset:0;z-index:10;pointer-events:none;display:grid;grid-template-columns:240px 1fr 280px;grid-template-rows:auto 1fr auto;gap:0}
.app > *{pointer-events:auto}

/* ===== TOP BAR ===== */
.topbar{grid-column:1 / 4;grid-row:1;display:grid;grid-template-columns:240px 1fr 280px;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line);position:relative;
  background: linear-gradient(180deg, rgba(8,3,6,0.86) 0%, rgba(8,3,6,0.5) 80%, transparent 100%);
  backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);
}
.topbar::after{content:'';position:absolute;left:0;right:0;bottom:-1px;height:1px;background:linear-gradient(90deg,transparent,var(--gold-deep) 15%,var(--gold-deep) 85%,transparent)}
.tb-left{display:flex;align-items:center;gap:14px}
.tb-logo{display:flex;align-items:center;gap:10px}
.tb-logo .mark{width:34px;height:34px;border:1px solid var(--red);display:grid;place-items:center;background:radial-gradient(circle at 50% 50%, rgba(220,38,38,0.25), transparent 70%);position:relative}
.tb-logo .mark::before,.tb-logo .mark::after{content:'';position:absolute;width:5px;height:5px;border:1px solid var(--red-warm)}
.tb-logo .mark::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.tb-logo .mark::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
.tb-logo .mark span{font-family:var(--serif);font-weight:600;font-size:12px;letter-spacing:.06em;color:var(--gold-bright);text-shadow:0 0 8px rgba(255,80,60,0.4)}
.tb-logo .name{font-family:var(--mono);font-size:10px;letter-spacing:.18em;color:var(--gold-deep);text-transform:uppercase;line-height:1.4}
.tb-logo .name b{display:block;color:var(--fg-soft);font-family:var(--serif);font-weight:500;letter-spacing:.32em;font-size:13px}
.tb-status{display:flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid var(--line-bright);background:rgba(20,8,10,0.5);font-family:var(--mono);font-size:10px;letter-spacing:.18em;color:var(--fg-soft);text-transform:uppercase;border-radius:2px}
.tb-status .dot{width:6px;height:6px;border-radius:50%;background:var(--red-hot);box-shadow:0 0 10px var(--red-hot);animation:beat 1.6s ease-in-out infinite}
.tb-status .lbl{color:var(--gold-deep)}
.tb-status .val{color:var(--fg-soft)}

.tb-center{text-align:center;position:relative}
.tb-center::before{content:'';position:absolute;left:50%;top:50%;width:240px;height:50px;transform:translate(-50%,-55%);background:radial-gradient(ellipse 60% 80% at 50% 50%, rgba(255,80,60,0.32), rgba(220,38,38,0.16) 40%, transparent 75%);pointer-events:none;z-index:0;filter:blur(6px)}
.tb-center h1{position:relative;z-index:1;font-family:var(--serif);font-weight:600;letter-spacing:.42em;font-size:32px;line-height:1;background:linear-gradient(180deg,var(--gold-bright) 0%,var(--gold) 50%,var(--gold-deep) 100%);-webkit-background-clip:text;background-clip:text;color:transparent;padding-left:.42em;text-shadow:0 0 24px rgba(255,80,60,0.4)}
.tb-center .tagline{position:relative;z-index:1;font-family:var(--serif-soft);font-style:italic;font-size:11px;letter-spacing:.32em;color:var(--gold);margin-top:2px;text-transform:uppercase}

.tb-right{display:flex;align-items:center;justify-content:flex-end;gap:12px}
.tb-icon{width:34px;height:34px;display:grid;place-items:center;border:1px solid var(--line-bright);color:var(--gold);transition:.15s;border-radius:2px}
.tb-icon:hover{color:var(--red-warm);border-color:var(--red);background:rgba(220,38,38,0.08)}
.tb-icon.audio-on{color:var(--red-warm);border-color:var(--red);box-shadow:0 0 16px -4px var(--red);background:rgba(220,38,38,0.18)}
.tb-icon svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:1.5}
.tb-upgrade{display:flex;align-items:center;gap:8px;padding:9px 16px;border:1px solid var(--red);background:linear-gradient(180deg, rgba(220,38,38,0.32), rgba(160,26,26,0.18));color:var(--gold-bright);font-family:var(--mono);font-size:10.5px;letter-spacing:.22em;text-transform:uppercase;font-weight:500;box-shadow:0 0 22px -8px rgba(220,38,38,0.7);transition:.15s}
.tb-upgrade:hover{background:linear-gradient(180deg, rgba(220,38,38,0.5), rgba(160,26,26,0.3));box-shadow:0 0 28px -4px rgba(220,38,38,0.9)}
.tb-upgrade svg{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:1.7}

/* ===== LEFT SIDEBAR — Memory Navigation ===== */
.rail-l{grid-column:1;grid-row:2;padding:18px 12px 18px 18px;overflow-y:auto;
  background:linear-gradient(90deg, rgba(8,3,6,0.84) 0%, rgba(8,3,6,0.55) 70%, rgba(8,3,6,0.0) 100%);
  scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.rail-l::-webkit-scrollbar{width:3px}.rail-l::-webkit-scrollbar-thumb{background:var(--line)}

.rail-head{display:flex;align-items:center;justify-content:space-between;font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--gold-deep);text-transform:uppercase;padding-bottom:10px;margin-bottom:6px;border-bottom:1px solid var(--line)}
.rail-head .lbl{display:flex;align-items:center;gap:8px}
.rail-head .lbl::before{content:'';width:4px;height:4px;border-radius:50%;background:var(--gold);box-shadow:0 0 5px var(--gold)}
.rail-head .count{color:var(--fg-soft);font-family:var(--mono);font-weight:500}

.nav-item{display:flex;align-items:center;gap:11px;padding:10px 12px;margin:4px -8px;font-family:var(--sans);font-size:12.5px;color:var(--fg-soft);cursor:pointer;border:1px solid transparent;transition:background .18s, color .18s, border-color .18s;position:relative;border-radius:2px}
.nav-item .ic{width:18px;height:18px;color:var(--gold-deep);flex-shrink:0;display:grid;place-items:center}
.nav-item .ic svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.4}
.nav-item .meta{flex:1;display:flex;flex-direction:column;line-height:1.2;min-width:0}
.nav-item .meta .name{font-weight:500;letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nav-item .meta .sub{font-family:var(--mono);font-size:9.5px;color:var(--gold-deep);letter-spacing:.04em;margin-top:2px;text-transform:lowercase}
.nav-item:hover{color:var(--fg);background:rgba(220,38,38,0.04);border-color:var(--line)}
.nav-item:hover .ic{color:var(--red-warm)}
.nav-item.active{background:linear-gradient(90deg, rgba(220,38,38,0.18), rgba(220,38,38,0.04) 70%, transparent);border-color:var(--red);color:var(--fg)}
.nav-item.active .ic{color:var(--red-hot)}
.nav-item.active::before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--red-hot);box-shadow:0 0 8px var(--red-hot)}

.quantum{margin-top:24px;padding:14px 12px;border:1px solid var(--line);background:rgba(20,8,10,0.4)}
.quantum .row1{display:flex;justify-content:space-between;align-items:baseline;font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--gold-deep);text-transform:uppercase}
.quantum .row1 .val{color:var(--red-warm);font-size:18px;font-weight:600;letter-spacing:0;font-family:var(--mono)}
.quantum .bar{margin-top:8px;height:4px;background:var(--line);position:relative;overflow:hidden}
.quantum .bar .fill{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg, var(--red-bright), var(--red-warm));box-shadow:0 0 10px var(--red-bright);transition:width .8s ease}
.quantum .row2{margin-top:10px;display:flex;justify-content:space-between;font-family:var(--mono);font-size:9.5px;color:var(--fg-dim);letter-spacing:.06em}

/* ===== RIGHT SIDEBAR — System Intelligence ===== */
.rail-r{grid-column:3;grid-row:2;padding:18px 18px 18px 12px;overflow-y:auto;
  background:linear-gradient(270deg, rgba(8,3,6,0.84) 0%, rgba(8,3,6,0.55) 70%, rgba(8,3,6,0.0) 100%);
  scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.rail-r::-webkit-scrollbar{width:3px}.rail-r::-webkit-scrollbar-thumb{background:var(--line)}

.sysblock{margin-bottom:18px;padding:12px 14px;border:1px solid var(--line);background:rgba(20,8,10,0.4);position:relative}
.sysblock .lbl{font-family:var(--mono);font-size:9px;letter-spacing:.32em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:6px}
.sysblock .val{font-family:var(--serif-soft);font-weight:500;font-size:18px;color:var(--fg);letter-spacing:.01em}
.sysblock .val.good{color:var(--c-mobile)}
.sysblock .val.warm{color:var(--red-warm)}
.sysblock .val.live{color:var(--red-hot);font-family:var(--mono);font-weight:500;font-size:20px;letter-spacing:.02em}
.sysblock .planet-mini{position:absolute;right:8px;top:50%;transform:translateY(-50%);width:54px;height:54px;border-radius:50%;
  background:#0a0306 url(/static/gen/planet.jpg) center/cover no-repeat;
  box-shadow: 0 0 22px rgba(255,90,60,0.55), inset -2px -3px 8px rgba(0,0,0,0.6);
  animation: planetSpin 60s linear infinite;
}
.sysblock .planet-mini::after{content:'';position:absolute;inset:-6px;border-radius:50%;border:1px solid rgba(255,90,60,0.18);box-shadow:0 0 24px rgba(255,90,60,0.35)}
@keyframes planetSpin{from{background-position:0% 50%}to{background-position:100% 50%}}

.sparkline{margin-top:8px;height:32px;display:block;width:100%}
.sparkline path{stroke:var(--red-warm);stroke-width:1.4;fill:none;filter:drop-shadow(0 0 4px rgba(255,112,96,0.5))}
.sparkline path.area{stroke:none;fill:url(#sparkfill)}

.avatars{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.avatar{width:32px;height:32px;border-radius:50%;background-size:cover;background-position:center;border:1px solid var(--red);box-shadow:0 0 10px rgba(220,38,38,0.5), inset 0 0 12px rgba(0,0,0,0.5);position:relative;flex-shrink:0;cursor:default;transition:transform .2s, box-shadow .2s}
.avatar:hover{transform:scale(1.12);box-shadow:0 0 18px rgba(255,80,60,0.8), inset 0 0 12px rgba(0,0,0,0.4)}
.avatar.online::after{content:'';position:absolute;right:-1px;bottom:-1px;width:9px;height:9px;border-radius:50%;background:var(--red-hot);border:1.5px solid #0a0306;box-shadow:0 0 6px var(--red-hot);animation:beat 1.6s ease-in-out infinite}
.avatar.recent::after{content:'';position:absolute;right:-1px;bottom:-1px;width:9px;height:9px;border-radius:50%;background:var(--gold);border:1.5px solid #0a0306}
.avatar.offline{filter:grayscale(.6) brightness(.5);border-color:var(--muted)}
.avatar .lbl{position:absolute;left:50%;top:100%;transform:translateX(-50%);margin-top:6px;font-family:var(--mono);font-size:8.5px;letter-spacing:.15em;color:var(--fg-dim);text-transform:uppercase;opacity:0;transition:opacity .15s;white-space:nowrap;pointer-events:none}
.avatar:hover .lbl{opacity:1}

.gauge-row{display:flex;align-items:baseline;justify-content:space-between;margin-top:6px}
.gauge-row .pct{font-family:var(--mono);font-size:22px;color:var(--red-warm);font-weight:500;letter-spacing:0}
.gauge-row .desc{font-family:var(--serif-soft);font-style:italic;font-size:11.5px;color:var(--fg-dim)}
.gauge{margin-top:6px;height:3px;background:var(--line);position:relative;overflow:hidden}
.gauge .fill{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg, var(--red-bright), var(--red-warm));box-shadow:0 0 8px var(--red-bright)}

/* ===== CENTER STAGE ===== */
.stage{grid-column:2;grid-row:2;pointer-events:none;display:flex;flex-direction:column;padding:18px 18px}
.stage-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;pointer-events:auto}
.stage-head .lbl{font-family:var(--mono);font-size:10px;letter-spacing:.32em;color:var(--gold-deep);text-transform:uppercase;display:flex;align-items:center;gap:8px}
.stage-head .lbl .lbl-main{color:var(--gold);font-weight:500}
.stage-head .lbl::before{content:'';width:4px;height:4px;border-radius:50%;background:var(--red-hot);box-shadow:0 0 6px var(--red-hot)}
.stage-head .sub{font-family:var(--serif-soft);font-style:italic;font-size:12px;color:var(--fg-dim);letter-spacing:.04em;margin-left:14px}
.stage-actions{display:flex;gap:8px}
.stage-actions button{display:flex;align-items:center;gap:6px;padding:6px 10px;border:1px solid var(--line-bright);font-family:var(--mono);font-size:9.5px;letter-spacing:.18em;color:var(--gold);text-transform:uppercase;transition:.15s;border-radius:2px}
.stage-actions button:hover{color:var(--red-warm);border-color:var(--red)}
.stage-actions button svg{width:11px;height:11px;stroke:currentColor;fill:none;stroke-width:1.6}
.stage-canvas-wrap{flex:1;position:relative}

.cloud-controls{position:absolute;right:0;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:6px;pointer-events:auto;z-index:6}
.cloud-controls .cc{width:30px;height:30px;display:grid;place-items:center;border:1px solid var(--line-bright);color:var(--gold-deep);transition:.15s;background:rgba(8,3,6,0.7);border-radius:2px}
.cloud-controls .cc:hover{color:var(--red-warm);border-color:var(--red)}
.cloud-controls .cc svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:1.5}

/* live flow indicator under cloud */
.cloud-foot{display:flex;justify-content:space-between;align-items:center;margin-top:10px;pointer-events:auto;padding:6px 0}
.cloud-foot .ply{display:flex;gap:6px}
.cloud-foot .ply button{width:28px;height:28px;display:grid;place-items:center;border:1px solid var(--line-bright);color:var(--gold-deep);transition:.15s;border-radius:2px}
.cloud-foot .ply button:hover{color:var(--red-warm);border-color:var(--red)}
.cloud-foot .ply button svg{width:11px;height:11px;stroke:currentColor;fill:currentColor;stroke-width:0}
.cloud-foot .liveflow{display:flex;align-items:center;gap:10px;font-family:var(--mono);font-size:9.5px;letter-spacing:.22em;color:var(--gold-deep);text-transform:uppercase}
.cloud-foot .liveflow svg{height:24px;width:80px}
.cloud-foot .liveflow path{stroke:var(--red-warm);stroke-width:1.2;fill:none;filter:drop-shadow(0 0 3px rgba(255,112,96,0.5))}

/* ===== BOTTOM DEVICE STRIP ===== */
.devstrip{grid-column:1 / 4;grid-row:3;padding:14px 18px;border-top:1px solid var(--line);position:relative;
  background:linear-gradient(0deg, rgba(8,3,6,0.84) 0%, rgba(8,3,6,0.5) 70%, transparent 100%);
  backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);
}
.devstrip::before{content:'';position:absolute;left:0;right:0;top:-1px;height:1px;background:linear-gradient(90deg,transparent,var(--gold-deep) 15%,var(--gold-deep) 85%,transparent)}
.devstrip-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-family:var(--mono);font-size:9.5px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase}
.devstrip-head .lbl{display:flex;align-items:center;gap:8px}
.devstrip-head .lbl::before{content:'';width:4px;height:4px;border-radius:50%;background:var(--gold);box-shadow:0 0 5px var(--gold)}
.devstrip-head .legend{display:flex;gap:14px}
.devstrip-head .legend span{display:inline-flex;align-items:center;gap:6px;color:var(--gold-deep)}
.devstrip-head .legend .d{width:6px;height:6px;border-radius:50%}
.devstrip-head .legend .d.online{background:var(--red-hot);box-shadow:0 0 6px var(--red-hot)}
.devstrip-head .legend .d.away{background:var(--c-planning);box-shadow:0 0 6px var(--c-planning)}
.devstrip-head .legend .d.processing{background:var(--gold);box-shadow:0 0 6px var(--gold)}
.devstrip-head .legend .d.offline{background:var(--muted)}

.devs{display:flex;gap:10px;overflow-x:auto;scrollbar-width:thin;scrollbar-color:var(--line) transparent;padding-bottom:2px}
.devs::-webkit-scrollbar{height:3px}.devs::-webkit-scrollbar-thumb{background:var(--line)}
.dev{flex-shrink:0;width:110px;padding:12px 10px 14px;border:1px solid var(--line);background:rgba(20,8,10,0.6);position:relative;display:flex;flex-direction:column;align-items:center;gap:8px;cursor:pointer;transition:.15s;border-radius:2px}
.dev:hover{border-color:var(--red);background:rgba(40,12,15,0.7)}
.dev .ico{width:30px;height:30px;color:var(--fg-soft);display:grid;place-items:center}
.dev .ico svg{width:30px;height:30px;stroke:currentColor;fill:none;stroke-width:1.3}
.dev.online .ico{color:var(--red-warm)}
.dev.away   .ico{color:var(--c-planning)}
.dev.processing .ico{color:var(--gold-bright)}
.dev.offline .ico{color:var(--muted)}
.dev .lbl{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;color:var(--fg-soft);text-align:center;line-height:1.3}
.dev .pulse{position:absolute;bottom:-1px;left:50%;transform:translateX(-50%);width:80%;height:2px;background:var(--muted);border-radius:1px}
.dev.online .pulse{background:var(--red-hot);box-shadow:0 0 8px var(--red-hot);animation:devPulse 2.2s ease-in-out infinite}
.dev.away .pulse{background:var(--c-planning);box-shadow:0 0 6px var(--c-planning)}
.dev.processing .pulse{background:var(--gold);box-shadow:0 0 6px var(--gold);animation:devPulse 1.4s ease-in-out infinite}
@keyframes devPulse{0%,100%{opacity:1}50%{opacity:.35}}
.dev-add{flex-shrink:0;width:110px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;border:1px dashed var(--line-bright);color:var(--gold-deep);font-family:var(--mono);font-size:9.5px;letter-spacing:.18em;text-transform:uppercase;transition:.15s;border-radius:2px}
.dev-add:hover{border-color:var(--red);color:var(--red-warm)}
.dev-add svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.5}

/* ===== ZOOM INTO BUBBLE — full-stage interior ===== */
.zoomshell{position:fixed;inset:0;z-index:5;pointer-events:none;display:flex;align-items:center;justify-content:center}
.zoomshell.active{pointer-events:auto}
.zoom-card{width:min(86vw, 1100px);max-height:80vh;border:1px solid var(--red);box-shadow:0 0 60px -8px rgba(220,38,38,0.7), inset 0 0 80px rgba(220,38,38,0.05);padding:32px 32px;position:relative;backdrop-filter:blur(18px) saturate(140%);-webkit-backdrop-filter:blur(18px) saturate(140%);transform:scale(0.85);opacity:0;transition:transform .35s cubic-bezier(.22,.61,.36,1), opacity .25s;overflow:hidden;
  background:
    linear-gradient(180deg, rgba(20,8,10,0.55) 0%, rgba(8,3,6,0.88) 80%),
    var(--hero-img, none) center/cover no-repeat,
    rgba(8,3,6,0.92);
}
.zoom-card.no-hero{background:linear-gradient(180deg, rgba(20,8,10,0.93), rgba(8,3,6,0.93))}
.zoomshell.active .zoom-card{transform:scale(1);opacity:1}
.zoom-card::before,.zoom-card::after{content:'';position:absolute;width:14px;height:14px;border:1px solid var(--gold)}
.zoom-card::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.zoom-card::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
.zoom-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--line)}
.zoom-title-block .kind{font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--red-warm);text-transform:uppercase}
.zoom-title-block h2{font-family:var(--serif);font-weight:500;font-size:26px;letter-spacing:.08em;color:var(--gold-bright);margin-top:6px;text-shadow:0 0 18px rgba(255,80,60,0.5)}
.zoom-title-block .desc{font-family:var(--serif-soft);font-style:italic;font-size:13px;color:var(--fg-soft);margin-top:4px;letter-spacing:.04em}
.zoom-back{display:flex;align-items:center;gap:8px;padding:8px 14px;border:1px solid var(--line-bright);font-family:var(--mono);font-size:10px;letter-spacing:.22em;color:var(--gold-deep);text-transform:uppercase;transition:.15s;flex-shrink:0;border-radius:2px}
.zoom-back:hover{color:var(--red-warm);border-color:var(--red)}
.zoom-back svg{width:11px;height:11px;stroke:currentColor;fill:none;stroke-width:1.7}

.zoom-stats{display:flex;gap:24px;margin-bottom:18px;flex-wrap:wrap}
.zoom-stat .k{font-family:var(--mono);font-size:9px;letter-spacing:.3em;color:var(--gold-deep);text-transform:uppercase}
.zoom-stat .v{font-family:var(--mono);font-size:22px;color:var(--fg);font-weight:500;letter-spacing:0;margin-top:3px;font-variant-numeric:tabular-nums}

.zoom-bubbles{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;max-height:52vh;overflow-y:auto;padding-right:6px;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.zoom-bubbles::-webkit-scrollbar{width:3px}.zoom-bubbles::-webkit-scrollbar-thumb{background:var(--line)}
.subb{padding:14px 16px;border:1px solid var(--line);background:rgba(8,3,6,0.7);position:relative;cursor:pointer;transition:.18s;border-radius:2px;overflow:hidden}
.subb::before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--accent-c, var(--red-bright));box-shadow:0 0 8px var(--accent-c, var(--red-bright))}
.subb:hover{border-color:var(--red);background:rgba(20,8,10,0.85);transform:translateY(-1px)}
.subb .meta{font-family:var(--mono);font-size:9px;letter-spacing:.18em;color:var(--gold-deep);text-transform:uppercase;display:flex;justify-content:space-between;margin-bottom:6px}
.subb .preview{font-family:var(--sans);font-size:13px;line-height:1.5;color:var(--fg-soft);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.subb .tags{display:flex;gap:5px;margin-top:8px;flex-wrap:wrap}
.subb .tags .tg{font-family:var(--mono);font-size:8.5px;padding:2px 6px;background:rgba(220,38,38,0.08);border:1px solid var(--line-bright);color:var(--fg-dim);letter-spacing:.04em}

.zoom-empty{padding:40px;text-align:center;font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim);font-size:14px}

/* ===== TOOLTIP / DRAWER from before ===== */
#tip{position:fixed;z-index:60;pointer-events:none;background:rgba(8,3,6,0.95);border:1px solid var(--red);padding:8px 12px;font-family:var(--mono);font-size:11px;color:var(--fg);max-width:340px;letter-spacing:.02em;opacity:0;transform:translate(-50%,-130%);transition:opacity .12s;box-shadow:0 0 30px rgba(220,38,38,0.25)}
#tip.show{opacity:1}
#tip .head{color:var(--red-warm);font-family:var(--serif);font-size:9px;letter-spacing:.32em;text-transform:uppercase;margin-bottom:4px}
#tip .body{color:var(--fg-soft);font-family:var(--sans);font-size:12px;line-height:1.4;letter-spacing:0;font-weight:400}

/* ===== Memory drawer (clicked sub-bubble) ===== */
.drawer{position:fixed;z-index:70;top:0;right:0;bottom:0;width:460px;max-width:92vw;background:rgba(8,3,6,.96);backdrop-filter:blur(24px) saturate(150%);border-left:1px solid var(--gold-deep);box-shadow:-30px 0 80px -20px rgba(220,38,38,.45);transform:translateX(100%);transition:transform .4s cubic-bezier(.22,.61,.36,1);overflow-y:auto;padding:36px 32px 48px}
.drawer.open{transform:translateX(0)}
.drawer .close{position:absolute;top:18px;right:18px;width:32px;height:32px;border:1px solid var(--gold-deep);background:transparent;color:var(--gold);cursor:pointer;font-family:var(--mono);font-size:16px;transition:.15s;border-radius:2px}
.drawer .close:hover{color:var(--red-warm);border-color:var(--red-warm)}
.drawer .kind{font-family:var(--serif);font-size:10px;letter-spacing:.36em;color:var(--red-warm);text-transform:uppercase}
.drawer h2{font-family:var(--serif-soft);font-weight:400;font-size:22px;margin:10px 0 18px;letter-spacing:.01em;line-height:1.4;color:var(--gold-bright)}
.drawer .meta{font-family:var(--mono);font-size:10.5px;color:var(--fg-dim);letter-spacing:.04em;display:grid;grid-template-columns:auto 1fr;gap:7px 16px;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--line)}
.drawer .meta .k{color:var(--gold-deep);text-transform:uppercase;font-size:9px;letter-spacing:.28em}
.drawer .meta .v{color:var(--fg-soft);word-break:break-all;font-family:var(--mono)}
.drawer .body{font-family:var(--sans);font-size:14px;line-height:1.7;color:var(--fg);white-space:pre-wrap}

/* ===== ANIMATIONS ===== */
@keyframes beat{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.85)}}

/* ===== INTRO SPLASH ===== */
.intro{position:fixed;inset:0;z-index:200;background:#08030a;display:grid;place-items:center;cursor:pointer;animation:introFadeIn 1.2s ease-out;overflow:hidden}
.intro.dismissed{animation:introFadeOut .9s ease-in forwards;pointer-events:none}
.intro-img{position:absolute;inset:0;background:url(/static/gen/zai_lockup.jpg) center/cover no-repeat;
  filter:brightness(0.78) saturate(1.1);
  transform:scale(1.08);
  animation:introImgZoom 6s ease-out infinite alternate;
}
.intro::before{content:'';position:absolute;inset:0;background:
  radial-gradient(ellipse 70% 90% at 50% 100%, rgba(8,3,6,0.95), transparent 60%),
  radial-gradient(ellipse 90% 70% at 50% 0%,  rgba(8,3,6,0.85), transparent 55%);
  z-index:1}
.intro-text{position:relative;z-index:2;text-align:center}
.intro-text .t1{font-family:var(--serif);font-weight:600;letter-spacing:.55em;font-size:96px;line-height:1;
  background:linear-gradient(180deg,#fff8e2 0%, var(--gold) 50%, var(--gold-deep) 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent;padding-left:.55em;
  text-shadow:0 0 60px rgba(255,80,60,0.5);
  animation:introTextRise 1.4s cubic-bezier(.22,.61,.36,1)}
.intro-text .t2{font-family:var(--serif-soft);font-style:italic;font-weight:300;font-size:18px;letter-spacing:.4em;color:var(--gold);text-transform:uppercase;margin-top:14px;animation:introTextRise 1.4s .15s cubic-bezier(.22,.61,.36,1) both}
.intro-foot{position:absolute;bottom:38px;left:0;right:0;z-index:2;text-align:center;font-family:var(--mono);font-size:10px;letter-spacing:.4em;color:var(--gold-deep);text-transform:uppercase;animation:introBlink 1.8s ease-in-out infinite}
@keyframes introFadeIn{from{opacity:0}to{opacity:1}}
@keyframes introFadeOut{from{opacity:1}to{opacity:0;visibility:hidden}}
@keyframes introImgZoom{from{transform:scale(1.08)}to{transform:scale(1.18) translate(-1%, -0.6%)}}
@keyframes introTextRise{from{opacity:0;transform:translateY(20px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
@keyframes introBlink{0%,100%{opacity:.45}50%{opacity:1}}

/* ===== EXPAND / FULLSCREEN CINEMATIC ===== */
.topbar,.devstrip,.rail-l,.rail-r{transition:opacity .55s ease, transform .55s ease}
.stage-head,.cloud-foot{transition:opacity .4s ease, transform .4s ease}
body.expanded .topbar  {opacity:0;pointer-events:none;transform:translateY(-100%)}
body.expanded .devstrip{opacity:0;pointer-events:none;transform:translateY(100%)}
body.expanded .rail-l  {opacity:0;pointer-events:none;transform:translateX(-100%)}
body.expanded .rail-r  {opacity:0;pointer-events:none;transform:translateX(100%)}
body.expanded .stage-head,
body.expanded .cloud-foot{opacity:0;pointer-events:none;transform:translateY(14px)}
body.expanded .stage{padding:6px}
#expandHint{position:fixed;left:50%;bottom:24px;transform:translate(-50%, 18px);z-index:50;opacity:0;pointer-events:none;
  font-family:var(--mono);font-size:10px;letter-spacing:.42em;color:var(--gold-deep);text-transform:uppercase;transition:opacity .4s ease, transform .4s ease}
body.expanded #expandHint{opacity:1;transform:translate(-50%, 0)}
#expandHint::before{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--gold);box-shadow:0 0 6px var(--gold);margin-right:10px;vertical-align:middle;animation:beat 1.8s ease-in-out infinite}

/* ===== ACTIVE BUTTON STATE ===== */
.btn-active{color:var(--red-warm) !important;border-color:var(--red) !important;background:rgba(220,38,38,0.10) !important;box-shadow:0 0 14px -4px var(--red)}

/* ===== REPLAY MODE ===== */
#replayBtn{position:fixed;left:18px;bottom:52px;z-index:56;display:inline-flex;align-items:center;gap:9px;
  padding:9px 14px;border:1px solid var(--line-bright);background:rgba(8,3,6,0.7);
  font-family:var(--mono);font-size:9.5px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase;
  cursor:pointer;transition:.15s;border-radius:2px}
#replayBtn:hover{color:var(--red-warm);border-color:var(--red);background:rgba(20,8,10,0.8)}
#replayBtn.active{color:var(--red-warm);border-color:var(--red);background:rgba(220,38,38,0.18);box-shadow:0 0 14px -4px var(--red)}
#replayBtn svg{width:11px;height:11px;stroke:currentColor;fill:none;stroke-width:1.6}
body.expanded #replayBtn{display:none}

#replayOverlay{position:fixed;inset:0;z-index:62;pointer-events:none;display:none;flex-direction:column;align-items:center;justify-content:flex-start;padding-top:120px}
#replayOverlay.show{display:flex}
#replayHead{font-family:var(--serif);font-size:11px;letter-spacing:.42em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:8px;
  display:flex;align-items:center;gap:10px}
#replayHead .dot{width:6px;height:6px;border-radius:50%;background:var(--red-hot);box-shadow:0 0 8px var(--red-hot);animation:beat 1.4s ease-in-out infinite}
#replayCaption{font-family:var(--serif-soft);font-style:italic;font-weight:400;font-size:18px;line-height:1.4;color:var(--gold-bright);
  max-width:62vw;text-align:center;padding:14px 26px;background:rgba(8,3,6,0.85);border:1px solid var(--gold-deep);
  backdrop-filter:blur(8px);opacity:0;transition:opacity .35s;border-radius:2px}
#replayOverlay.show #replayCaption{opacity:1}
#replayMeta{font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--gold-deep);text-transform:uppercase;margin-top:12px}
#replayProgress{position:fixed;left:0;right:0;bottom:0;height:2px;background:var(--line);z-index:63;display:none}
#replayProgress.show{display:block}
#replayProgress .bar{height:100%;background:linear-gradient(90deg,var(--red-hot),var(--gold));box-shadow:0 -4px 12px rgba(220,38,38,0.4);width:0%;transition:width .4s linear}

/* ===== TOAST ===== */
#toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%) translateY(20px);z-index:90;
  padding:11px 22px;background:rgba(8,3,6,0.92);border:1px solid var(--gold-deep);
  font-family:var(--mono);font-size:10.5px;letter-spacing:.22em;color:var(--gold-bright);text-transform:uppercase;
  opacity:0;transition:opacity .25s, transform .25s;pointer-events:none;border-radius:2px;
  backdrop-filter:blur(6px)}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* ===== MINI-MAP NAVIGATOR ===== */
#minimap{position:fixed;top:78px;right:18px;z-index:46;width:180px;height:180px;
  background:rgba(8,3,6,0.78);backdrop-filter:blur(10px) saturate(140%);
  border:1px solid var(--line-bright);padding:8px;
  display:flex;flex-direction:column;gap:6px;
  transition:opacity .25s, transform .25s;border-radius:2px}
#minimap.hidden{opacity:0;pointer-events:none;transform:translateX(20px)}
#minimap .mmHead{font-family:var(--mono);font-size:8px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase;text-align:center;padding-bottom:4px;border-bottom:1px solid var(--line)}
#minimap canvas{display:block;flex:1;width:100%;height:100%;cursor:pointer}
body.in-bubble #minimap{opacity:.4}
body.in-bubble #minimap:hover{opacity:1}

/* ===== SESSION TRAIL — drawn on cloud canvas, no extra DOM ===== */

/* ===== SCENE DOCK ===== */
#sceneDock{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:47;
  display:flex;align-items:center;gap:4px;padding:5px;background:rgba(8,3,6,0.78);backdrop-filter:blur(10px);
  border:1px solid var(--line-bright);border-radius:24px}
#sceneDock .scene{position:relative;display:inline-flex;align-items:center;gap:8px;padding:8px 18px;
  font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--fg-dim);text-transform:uppercase;cursor:pointer;border-radius:20px;
  transition:color .2s;text-decoration:none}
#sceneDock .scene.active{color:var(--gold-bright)}
#sceneDock .scene:hover{color:var(--red-warm)}
#sceneDock .scene .dot{width:5px;height:5px;border-radius:50%;background:currentColor;opacity:.6}
#sceneDock .scene.active .dot{box-shadow:0 0 8px currentColor;opacity:1}
#sceneDock .indicator{position:absolute;top:50%;left:0;transform:translateY(-50%);width:0;height:32px;background:rgba(220,38,38,0.18);border:1px solid var(--red);border-radius:18px;
  transition:left .35s cubic-bezier(.22,.61,.36,1), width .35s cubic-bezier(.22,.61,.36,1);pointer-events:none}
body.expanded #sceneDock, body.expanded #minimap{display:none}

/* ===== COMMAND PALETTE (⌘K) ===== */
#palette{position:fixed;inset:0;z-index:80;background:rgba(8,3,6,0.65);backdrop-filter:blur(10px);
  display:none;align-items:flex-start;justify-content:center;padding-top:14vh}
#palette.show{display:flex}
#palette .palBox{width:min(640px, 90vw);background:linear-gradient(180deg, rgba(20,8,10,0.96), rgba(8,3,6,0.96));
  border:1px solid var(--red);box-shadow:0 0 60px -10px rgba(220,38,38,0.55);overflow:hidden;
  display:flex;flex-direction:column;max-height:70vh}
#palette .palInput{display:flex;align-items:center;gap:12px;padding:18px 22px;border-bottom:1px solid var(--line)}
#palette .palInput svg{width:18px;height:18px;color:var(--red-warm);stroke:currentColor;fill:none;stroke-width:1.5;flex-shrink:0}
#palette .palInput input{flex:1;background:transparent;border:0;outline:0;color:var(--fg);font-family:var(--sans);font-size:16px;letter-spacing:.01em}
#palette .palInput input::placeholder{color:var(--fg-dim)}
#palette .palInput kbd{font-family:var(--mono);font-size:9px;letter-spacing:.18em;color:var(--gold-deep);padding:3px 7px;border:1px solid var(--line-bright);border-radius:2px}
#palette .palList{overflow-y:auto;padding:6px 0;flex:1;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
#palette .palList::-webkit-scrollbar{width:4px}#palette .palList::-webkit-scrollbar-thumb{background:var(--line)}
#palette .palItem{display:flex;align-items:center;gap:14px;padding:11px 22px;cursor:pointer;transition:background .12s}
#palette .palItem:hover, #palette .palItem.active{background:rgba(220,38,38,0.10)}
#palette .palItem .kind{font-family:var(--mono);font-size:8.5px;letter-spacing:.32em;color:var(--gold-deep);text-transform:uppercase;width:64px;flex-shrink:0}
#palette .palItem.k-cat .kind{color:var(--gold-bright)}
#palette .palItem.k-mem .kind{color:var(--red-warm)}
#palette .palItem.k-dec .kind{color:#a3d4a8}
#palette .palItem .title{font-family:var(--sans);font-size:14px;color:var(--fg);flex:1;line-height:1.4;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#palette .palItem .meta{font-family:var(--mono);font-size:9.5px;color:var(--fg-dim);letter-spacing:.04em;flex-shrink:0;text-align:right}
#palette .palFoot{padding:10px 22px;border-top:1px solid var(--line);font-family:var(--mono);font-size:9.5px;
  letter-spacing:.22em;color:var(--gold-deep);text-transform:uppercase;display:flex;justify-content:space-between}
#palette .palFoot kbd{font-family:var(--mono);font-size:9px;color:var(--fg-soft);padding:2px 6px;border:1px solid var(--line-bright);margin:0 3px}

/* ===== RELATION MAP in drawer ===== */
.drawer .relmap{margin-top:24px;padding-top:18px;border-top:1px solid var(--line)}
.drawer .relmap h3{font-family:var(--mono);font-size:9px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:14px}
.drawer .relmap .relrow{display:flex;align-items:center;gap:9px;padding:7px 0;font-family:var(--sans);font-size:12.5px;color:var(--fg-soft);cursor:pointer;border-bottom:1px dashed var(--line);transition:color .15s}
.drawer .relmap .relrow:last-child{border-bottom:0}
.drawer .relmap .relrow:hover{color:var(--red-warm)}
.drawer .relmap .relrow .dot{width:7px;height:7px;border-radius:50%;background:var(--red-warm);box-shadow:0 0 5px var(--red-warm);flex-shrink:0}
.drawer .relmap .relrow .dot.ent{background:var(--gold);box-shadow:0 0 5px var(--gold)}
.drawer .relmap .relrow .lbl{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.drawer .relmap .relrow .ovl{font-family:var(--mono);font-size:9px;color:var(--fg-dim);letter-spacing:.06em}

/* ===== BREADCRUMB ===== */
#breadcrumb{position:fixed;left:50%;top:78px;transform:translateX(-50%);z-index:48;
  display:flex;align-items:center;gap:10px;padding:9px 18px;
  background:rgba(8,3,6,0.78);backdrop-filter:blur(12px) saturate(140%);-webkit-backdrop-filter:blur(12px) saturate(140%);
  border:1px solid var(--line-bright);border-radius:2px;
  font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;
  opacity:0;transition:opacity .35s, transform .35s;pointer-events:none}
body.in-bubble #breadcrumb, body.expanded #breadcrumb{opacity:1;transform:translateX(-50%) translateY(0);pointer-events:auto}
#breadcrumb .crumb{color:var(--fg-dim);transition:color .15s;text-decoration:none}
#breadcrumb a.crumb{cursor:pointer}
#breadcrumb a.crumb:hover{color:var(--red-warm)}
#breadcrumb .crumb.final{color:var(--gold-bright)}
#breadcrumb .crumb-sep{color:var(--muted)}
#breadcrumb .crumb-tools{display:flex;gap:10px;margin-left:14px;padding-left:14px;border-left:1px solid var(--line-bright)}
#breadcrumb .crumb-tool{color:var(--gold-deep);text-decoration:none;cursor:pointer;font-size:9px;letter-spacing:.32em;transition:color .15s}
#breadcrumb .crumb-tool:hover{color:var(--red-warm)}

/* ===== ENTER UNIVERSE CTA ===== */
.enter-cta{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);z-index:9;
  display:inline-flex;align-items:center;gap:11px;padding:11px 22px;
  background:linear-gradient(180deg, rgba(20,8,10,0.85), rgba(8,3,6,0.85));
  border:1px solid var(--red);border-radius:2px;
  font-family:var(--mono);font-size:10px;letter-spacing:.42em;color:var(--gold-bright);text-transform:uppercase;
  text-decoration:none;cursor:pointer;transition:.18s;
  box-shadow:0 0 24px -6px rgba(220,38,38,0.65)}
.enter-cta:hover{background:linear-gradient(180deg, rgba(40,12,15,0.95), rgba(20,8,10,0.95));box-shadow:0 0 36px -4px rgba(220,38,38,0.95);transform:translateX(-50%) translateY(-1px)}
.enter-cta svg{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:1.7}
.enter-cta .dot{width:6px;height:6px;border-radius:50%;background:var(--red-hot);box-shadow:0 0 10px var(--red-hot);animation:beat 1.5s ease-in-out infinite}

body.expanded .enter-cta{display:none}

/* ===== LEGEND PANEL ===== */
#legendPanel{position:fixed;left:18px;bottom:140px;z-index:55;width:380px;max-width:90vw;max-height:60vh;overflow-y:auto;
  background:rgba(8,3,6,0.94);border:1px solid var(--red-deep);box-shadow:0 0 40px -8px rgba(220,38,38,0.4);
  padding:18px 20px;backdrop-filter:blur(14px);font-family:var(--sans);font-size:12px;line-height:1.5;color:var(--fg-soft);
  opacity:0;transform:translateY(20px);transition:opacity .35s, transform .35s;pointer-events:none}
#legendPanel.show{opacity:1;transform:translateY(0);pointer-events:auto}
#legendPanel .legend-title{font-family:var(--serif);font-size:11px;letter-spacing:.42em;color:var(--gold);text-transform:uppercase;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--line)}
#legendPanel .legend-row{display:flex;gap:11px;align-items:flex-start;padding:6px 0}
#legendPanel .legend-row b{color:var(--fg);font-weight:500}
#legendPanel .legend-mark{flex-shrink:0;width:13px;height:13px;border-radius:50%;margin-top:3px}
#legendPanel .legend-line{flex-shrink:0;width:18px;height:2px;border-radius:1px;margin-top:7px}
#legendPanel .legend-dot{flex-shrink:0;width:5px;height:5px;border-radius:50%;background:var(--red-warm);margin-top:6px;box-shadow:0 0 6px var(--red-warm)}
#legendPanel .legend-ring{flex-shrink:0;width:14px;height:14px;border-radius:50%;border:1px solid var(--red-warm);margin-top:3px}
#legendPanel .legend-foot{font-family:var(--mono);font-size:9px;color:var(--gold-deep);letter-spacing:.16em;text-transform:uppercase;margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}

#legendToggle{position:fixed;left:18px;bottom:96px;z-index:56;width:36px;height:36px;
  display:grid;place-items:center;border:1px solid var(--line-bright);background:rgba(8,3,6,0.7);
  color:var(--gold-deep);font-family:var(--mono);font-size:14px;font-weight:600;cursor:pointer;transition:.15s;
  letter-spacing:.04em}
#legendToggle:hover{color:var(--red-warm);border-color:var(--red);background:rgba(20,8,10,0.8)}
#legendToggle.active{color:var(--red-warm);border-color:var(--red);background:rgba(220,38,38,0.18);box-shadow:0 0 14px -4px var(--red)}
body.expanded #legendToggle, body.expanded #legendPanel{display:none}

/* ===== EDGE TOOLTIP (semantic connections) ===== */
#edgeTip{position:fixed;z-index:33;pointer-events:none;background:rgba(8,3,6,0.95);border:1px solid var(--gold-deep);padding:7px 11px;
  font-family:var(--mono);font-size:10.5px;color:var(--fg);max-width:300px;letter-spacing:.02em;
  opacity:0;transform:translate(-50%,-130%);transition:opacity .12s}
#edgeTip.show{opacity:1}
#edgeTip .head{color:var(--gold);font-family:var(--serif);font-size:9px;letter-spacing:.32em;text-transform:uppercase;margin-bottom:3px}

/* ===== NARRATE CAPTION BANNER ===== */
.narrateBanner{position:fixed;left:50%;top:64%;transform:translate(-50%, 20px);z-index:35;
  padding:14px 32px;background:rgba(8,3,6,0.85);border:1px solid var(--gold-deep);
  font-family:var(--serif);font-size:15px;letter-spacing:.32em;color:var(--gold-bright);text-transform:uppercase;
  opacity:0;transition:opacity .55s ease, transform .55s ease;pointer-events:none;
  box-shadow:0 0 30px -10px rgba(220,38,38,0.55);backdrop-filter:blur(6px);max-width:80vw;text-align:center}
.narrateBanner.show{opacity:1;transform:translate(-50%, 0)}
.narrateBanner small{display:block;font-family:var(--serif-soft);font-style:italic;font-size:12px;letter-spacing:.06em;color:var(--gold);margin-top:6px;text-transform:none;font-weight:300}

/* ===== RESPONSIVE ===== */
@media (max-width:1180px){
  .app{grid-template-columns:200px 1fr 240px}
  .topbar,.devstrip{grid-template-columns:200px 1fr 240px}
  .tb-center h1{font-size:26px}
}
@media (max-width:900px){
  .app{grid-template-columns:1fr;grid-template-rows:auto auto 50vh auto auto;overflow-y:auto;overflow-x:hidden;pointer-events:auto;background:rgba(8,3,6,0.4)}
  .topbar{grid-column:1;grid-row:1;grid-template-columns:1fr;gap:12px;text-align:center}
  .topbar > *{justify-content:center}
  .tb-left,.tb-right{justify-content:center}
  .rail-l{grid-column:1;grid-row:4;background:rgba(8,3,6,0.75)}
  .stage{grid-column:1;grid-row:3;height:50vh}
  .rail-r{grid-column:1;grid-row:5;background:rgba(8,3,6,0.75)}
  .devstrip{grid-column:1;grid-row:2;grid-template-columns:1fr}
  .tb-center h1{font-size:24px}
  #bg{background-image:url(/static/nebula-mobile.jpg)}
}
</style>
</head>
<body>
<div id="bg"></div>
<div id="bg-glow"></div>
<div id="bg-vignette"></div>

<div id="intro" class="intro">
  <div class="intro-img"></div>
  <div class="intro-text"><div class="t1">ZAI</div><div class="t2">Living Intelligence</div></div>
  <div class="intro-foot">tap to enter</div>
</div>
<canvas id="dust"></canvas>
<canvas id="stars"></canvas>
<canvas id="cloud3d"></canvas>
<canvas id="cloud"></canvas>
<canvas id="zoom"></canvas>

<!-- SVG sprite defs -->
<svg width="0" height="0" style="position:absolute"><defs>
  <linearGradient id="sparkfill" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="#ff7060" stop-opacity="0.45"/>
    <stop offset="100%" stop-color="#ff7060" stop-opacity="0"/>
  </linearGradient>
</defs></svg>

<div class="app">
  <header class="topbar">
    <div class="tb-left">
      <div class="tb-logo">
        <div class="mark"><span>Z</span></div>
        <div class="name">ZAI<b>Living</b></div>
      </div>
      <div class="tb-status"><span class="dot"></span><span class="lbl">Universe status</span><span class="val" id="usStatus">syncing</span></div>
    </div>
    <div class="tb-center">
      <h1>ZAI</h1>
      <div class="tagline">Living Intelligence</div>
    </div>
    <div class="tb-right">
      <button class="tb-icon" id="btnAudio" aria-label="ambient audio" title="toggle ambient sound"><svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9c1 .8 1.5 2 1.5 3s-.5 2.2-1.5 3" stroke-opacity=".6"/><path d="M19 6c2 1.4 3 3.6 3 6s-1 4.6-3 6" stroke-opacity=".25"/></svg></button>
      <button class="tb-icon" aria-label="search"><svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><line x1="15" y1="15" x2="20" y2="20"/></svg></button>
      <button class="tb-icon" aria-label="alerts"><svg viewBox="0 0 24 24"><path d="M18 16v-5a6 6 0 0 0-12 0v5l-1.5 2h15z"/><path d="M10 21h4"/></svg></button>
      <button class="tb-icon" aria-label="settings"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.6 4.6l2.1 2.1M17.3 17.3l2.1 2.1M4.6 19.4l2.1-2.1M17.3 6.7l2.1-2.1"/></svg></button>
      <button class="tb-upgrade"><svg viewBox="0 0 24 24"><path d="M12 3l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1z"/></svg>Upgrade</button>
    </div>
  </header>

  <aside class="rail-l">
    <div class="rail-head"><span class="lbl">Memory Navigation</span><span class="count" id="navCount">—</span></div>
    <div class="nav-item active" data-nav="universe">
      <span class="ic"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="9" ry="3.5"/><ellipse cx="12" cy="12" rx="3.5" ry="9"/></svg></span>
      <div class="meta"><div class="name">Living Universe</div><div class="sub">real-time memory map</div></div>
    </div>
    <div class="nav-item" data-nav="active">
      <span class="ic"><svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="14" rx="1"/><path d="M3 10h18"/><circle cx="6.5" cy="13.5" r="0.6" fill="currentColor"/></svg></span>
      <div class="meta"><div class="name">Active Memories</div><div class="sub">current context</div></div>
    </div>
    <div class="nav-item" data-nav="graph">
      <span class="ic"><svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M6 8l6 8M18 8l-6 8"/></svg></span>
      <div class="meta"><div class="name">Knowledge Graph</div><div class="sub">connected intelligence</div></div>
    </div>
    <div class="nav-item" data-nav="agents">
      <span class="ic"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3"/><path d="M5 20c0-3.5 3-6 7-6s7 2.5 7 6"/></svg></span>
      <div class="meta"><div class="name">Agents &amp; Tasks</div><div class="sub">AI agents · processes</div></div>
    </div>
    <div class="nav-item" data-nav="repos">
      <span class="ic"><svg viewBox="0 0 24 24"><path d="M4 4h12l4 4v12H4z"/><path d="M4 8h16"/><path d="M8 12h8M8 16h6"/></svg></span>
      <div class="meta"><div class="name">Repositories</div><div class="sub">github · code intel</div></div>
    </div>
    <div class="nav-item" data-nav="sessions">
      <span class="ic"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></svg></span>
      <div class="meta"><div class="name">Sessions</div><div class="sub">history · all surfaces</div></div>
    </div>
    <div class="nav-item" data-nav="devices">
      <span class="ic"><svg viewBox="0 0 24 24"><rect x="3" y="5" width="14" height="10" rx="1"/><rect x="17" y="9" width="4" height="10" rx="1"/></svg></span>
      <div class="meta"><div class="name">Devices</div><div class="sub">connected devices</div></div>
    </div>
    <div class="nav-item" data-nav="archive">
      <span class="ic"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="5"/><rect x="3" y="8" width="18" height="13"/><path d="M10 13h4"/></svg></span>
      <div class="meta"><div class="name">Archive</div><div class="sub">long-term store</div></div>
    </div>
    <div class="nav-item" data-nav="insights">
      <span class="ic"><svg viewBox="0 0 24 24"><path d="M4 19h16M7 16v-5M11 16v-9M15 16v-7M19 16v-3"/></svg></span>
      <div class="meta"><div class="name">Insights</div><div class="sub">patterns · analytics</div></div>
    </div>

    <div class="quantum">
      <div class="row1"><span>Memory Quantum</span><span class="val" id="qVal">—</span></div>
      <div class="bar"><div class="fill" id="qBar" style="width:0"></div></div>
      <div class="row2"><span id="qUsed">— used</span><span id="qCap">— cap</span></div>
    </div>
  </aside>

  <div class="stage">
    <div class="stage-head">
      <div class="lbl"><span class="lbl-main">Living Memory Cloud</span><span class="sub" id="stageSub">Real-time interactive memory universe</span></div>
      <div class="stage-actions">
        <button id="btn3d"><svg viewBox="0 0 24 24"><path d="M12 3l9 5v8l-9 5-9-5V8z"/><path d="M12 12v9"/></svg>3D View</button>
        <button id="btnFs"><svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>Expand</button>
      </div>
    </div>
    <div class="stage-canvas-wrap">
      <a class="enter-cta" href="/universe" title="Enter the dedicated universe view">
        <span class="dot"></span>Enter Universe
        <svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
      </a>
      <div class="cloud-controls">
        <button class="cc" data-act="zoom-in" aria-label="zoom in"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/><line x1="15.5" y1="15.5" x2="20" y2="20"/></svg></button>
        <button class="cc" data-act="zoom-out" aria-label="zoom out"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><line x1="8" y1="11" x2="14" y2="11"/><line x1="15.5" y1="15.5" x2="20" y2="20"/></svg></button>
        <button class="cc" data-act="recenter" aria-label="recenter"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="2"/><circle cx="12" cy="12" r="8"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg></button>
      </div>
    </div>
    <div class="cloud-foot">
      <div class="ply">
        <button aria-label="play"><svg viewBox="0 0 24 24"><polygon points="6,4 20,12 6,20"/></svg></button>
        <button aria-label="back"><svg viewBox="0 0 24 24"><polygon points="14,4 14,20 4,12"/></svg></button>
        <button aria-label="add"><svg viewBox="0 0 24 24" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg></button>
        <button aria-label="minus"><svg viewBox="0 0 24 24" stroke-width="2"><path d="M5 12h14"/></svg></button>
        <button aria-label="expand"><svg viewBox="0 0 24 24"><path d="M4 4h6M4 4v6M20 4h-6M20 4v6M4 20h6M4 20v-6M20 20h-6M20 20v-6"/></svg></button>
      </div>
      <div class="liveflow">
        <span>Live Flow</span>
        <svg id="liveSpark"><path d="" /></svg>
      </div>
    </div>
  </div>

  <aside class="rail-r">
    <div class="sysblock">
      <div class="lbl">System Intelligence</div>
      <div class="val" style="font-size:13px;color:var(--fg-soft)">All Systems Nominal</div>
    </div>
    <div class="sysblock">
      <div class="lbl">Sync Health</div>
      <div class="val good" id="syncHealth">Excellent</div>
    </div>
    <div class="sysblock">
      <div class="lbl">Memory Stream</div>
      <div style="font-family:var(--mono);font-size:9px;letter-spacing:.18em;color:var(--gold-deep);margin-top:2px">Live Tokens / min</div>
      <div class="val live" id="memStream">—</div>
      <svg class="sparkline" id="streamSpark"><path class="area" d=""/><path d=""/></svg>
    </div>
    <div class="sysblock">
      <div class="lbl">Active Agents</div>
      <div class="val live" id="activeAgents" style="font-size:18px">— Running</div>
      <div class="avatars" id="agentAvatars"></div>
    </div>
    <div class="sysblock">
      <div class="lbl">Context Fusion</div>
      <div class="gauge-row"><span class="pct" id="fusionPct">—</span><span class="desc">Unified Awareness</span></div>
      <div class="gauge"><div class="fill" id="fusionFill" style="width:0"></div></div>
    </div>
    <div class="sysblock">
      <div class="lbl">Knowledge Flow</div>
      <div class="val" id="knowFlow" style="font-size:15px;color:var(--c-mobile)">Streaming</div>
    </div>
    <div class="sysblock">
      <div class="lbl">MCP Server</div>
      <div class="val good" id="mcpStatus" style="font-size:15px">Connected</div>
      <div style="font-family:var(--mono);font-size:10px;color:var(--gold-deep);margin-top:3px">Latency: <span id="mcpLat">—</span></div>
    </div>
    <div class="sysblock" style="padding-right:60px">
      <div class="lbl">Universe Time</div>
      <div class="val" id="uTime" style="font-family:var(--mono);font-size:20px;letter-spacing:.04em">—</div>
      <div style="font-family:var(--mono);font-size:9.5px;color:var(--gold-deep);letter-spacing:.06em;margin-top:3px" id="uDate">—</div>
      <div class="planet-mini"></div>
    </div>
  </aside>

  <div class="devstrip">
    <div class="devstrip-head">
      <span class="lbl">Connected Devices &amp; Entities</span>
      <span class="legend">
        <span><span class="d online"></span>Online</span>
        <span><span class="d away"></span>Away</span>
        <span><span class="d processing"></span>Processing</span>
        <span><span class="d offline"></span>Offline</span>
      </span>
    </div>
    <div class="devs" id="devs"></div>
  </div>
</div>

<aside class="drawer" id="drawer" aria-hidden="true"><button class="close" id="drawerClose" aria-label="close">×</button>
  <div id="drawerBody"></div>
</aside>

<!-- Zoom-into-bubble panel -->
<div class="zoomshell" id="zoomShell" aria-hidden="true">
  <div class="zoom-card">
    <div class="zoom-header">
      <div class="zoom-title-block">
        <div class="kind" id="zoomKind">Category</div>
        <h2 id="zoomTitle">—</h2>
        <div class="desc" id="zoomDesc">—</div>
      </div>
      <button class="zoom-back" id="zoomBack"><svg viewBox="0 0 24 24"><path d="M5 12h14M5 12l5-5M5 12l5 5"/></svg>Back to universe</button>
    </div>
    <div class="zoom-stats" id="zoomStats"></div>
    <div class="zoom-bubbles" id="zoomBubbles"></div>
  </div>
</div>

<div id="tip"></div>
<div id="palette" aria-hidden="true">
  <div class="palBox">
    <div class="palInput">
      <svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><line x1="15" y1="15" x2="20" y2="20"/></svg>
      <input id="palInput" type="text" placeholder="Search memories, decisions, categories…" autocomplete="off">
      <kbd>esc</kbd>
    </div>
    <div class="palList" id="palList"></div>
    <div class="palFoot">
      <span><kbd>↑</kbd><kbd>↓</kbd> navigate · <kbd>↵</kbd> open</span>
      <span id="palCount">—</span>
    </div>
  </div>
</div>
<div id="minimap" title="Universe navigator — click any node to dolly there"><div class="mmHead">Navigator</div><canvas id="mmcv"></canvas></div>
<button id="replayBtn" title="Replay the last 30 memory events as a cinematic timeline"><svg viewBox="0 0 24 24"><polygon points="6 4 20 12 6 20" fill="currentColor" stroke="none"/></svg>Replay Universe</button>
<div id="replayOverlay">
  <div id="replayHead"><span class="dot"></span><span id="replayHeadText">Replaying recent memory</span></div>
  <div id="replayCaption">—</div>
  <div id="replayMeta">— · — · —</div>
</div>
<div id="replayProgress"><div class="bar" id="replayBar"></div></div>
<div id="sceneDock"><span class="indicator" id="sceneInd"></span>
  <a class="scene active" data-scene="home" href="javascript:void(0)"><span class="dot"></span>Home</a>
  <a class="scene" data-scene="universe" href="/universe"><span class="dot"></span>Universe</a>
  <a class="scene" data-scene="palette" href="javascript:void(0)"><span class="dot"></span>Search</a>
</div>
<button id="legendToggle" title="What does each visual mean? — toggle universe legend">?</button>
<div id="expandHint">press F or click expand to restore</div>
<div id="edgeTip"></div>
<div id="narrateBanner" class="narrateBanner"><span id="narrateText">—</span><small id="narrateSub">—</small></div>

<script>
(() => {
"use strict";

// =================================================================
// ZAI Memory Hub  —  Living Memory Cloud
// Three layers below the UI: dust · stars · cloud (bubble cloud
// + glowing connection web). The "click to zoom" reveals the
// category interior as a card of sub-bubbles (real memory items).
// =================================================================

const RED        = '#dc2626';
const RED_HOT    = '#ff3a3a';
const RED_WARM   = '#ff7060';
const RED_DEEP   = '#a01a1a';
const GOLD       = '#e8d49a';
const GOLD_BRIGHT= '#f5dca3';
const FG         = '#f5ecdb';
const FG_SOFT    = '#d4c3a0';
const FG_DIM     = '#8a7a6a';

// canvases
const cv = {
  dust:  document.getElementById('dust'),
  stars: document.getElementById('stars'),
  cloud: document.getElementById('cloud'),
};
const ctx = {
  dust:  cv.dust.getContext('2d'),
  stars: cv.stars.getContext('2d'),
  cloud: cv.cloud.getContext('2d'),
};
let W = 0, H = 0, DPR = Math.min(window.devicePixelRatio || 1, 2);

let STAGE = { x: 0, y: 0, w: 0, h: 0, cx: 0, cy: 0 };
function measureStage(){
  const wrap = document.querySelector('.stage-canvas-wrap') || document.querySelector('.stage');
  if (wrap){
    const r = wrap.getBoundingClientRect();
    STAGE = { x: r.x, y: r.y, w: r.width, h: r.height, cx: r.x + r.width/2, cy: r.y + r.height/2 };
  }
}
function sizeAll(){
  W = window.innerWidth; H = window.innerHeight;
  for (const k of ['dust','stars','cloud']){
    const c = cv[k];
    c.width  = Math.floor(W * DPR);
    c.height = Math.floor(H * DPR);
    c.style.width = W + 'px';
    c.style.height = H + 'px';
    ctx[k].setTransform(DPR,0,0,DPR,0,0);
  }
  measureStage();
}
sizeAll();
window.addEventListener('resize', () => { sizeAll(); seedDust(); seedStars(); layoutBubbles(); pushToCloud3D(); });
window.addEventListener('cloud3d-ready', () => { pushToCloud3D(); });

// =================================================================
// COSMIC DUST + STARS + COMETS  (unchanged from earlier pass)
// =================================================================
let dust = [];
function seedDust(){
  const N = Math.round((W*H) / 14000);
  dust = [];
  for (let i = 0; i < N; i++){
    const depth = Math.random();
    dust.push({
      x: Math.random()*W, y: Math.random()*H,
      vx: 0.04 + depth*0.32,
      vy: (Math.random()-0.5) * 0.04,
      r: 0.4 + depth*1.3,
      a: 0.05 + depth*0.30,
      tw: Math.random()*Math.PI*2,
      tws: 0.2 + Math.random()*0.6,
      hue: Math.random() < 0.55 ? 'warm' : (Math.random() < 0.4 ? 'red' : 'cream'),
    });
  }
}
function drawDust(dt, t){
  const g = ctx.dust; g.clearRect(0,0,W,H); g.globalCompositeOperation = 'lighter';
  for (const d of dust){
    d.x -= d.vx * dt * 0.04; d.y += d.vy * dt * 0.04;
    if (d.x < -4){ d.x = W + 4; d.y = Math.random()*H; }
    if (d.y < -4 || d.y > H+4) d.y = Math.random()*H;
    const flick = 0.55 + 0.45*Math.sin(d.tw + t*0.001*d.tws);
    const a = d.a * flick;
    let col;
    if (d.hue === 'warm') col = `rgba(255,160,110,${a})`;
    else if (d.hue === 'red') col = `rgba(255,80,70,${a})`;
    else col = `rgba(245,236,219,${a})`;
    const r = d.r * (1 + flick*0.4);
    const grad = g.createRadialGradient(d.x, d.y, 0, d.x, d.y, r*5);
    grad.addColorStop(0, col);
    grad.addColorStop(1, col.replace(/[\d.]+\)$/, '0)'));
    g.fillStyle = grad;
    g.beginPath(); g.arc(d.x, d.y, r*5, 0, Math.PI*2); g.fill();
  }
  g.globalCompositeOperation = 'source-over';
}

let stars = [];
function seedStars(){
  const N = Math.round((W*H) / 6800);
  stars = [];
  for (let i=0; i<N; i++){
    const depth = Math.random();
    stars.push({
      x: Math.random()*W, y: Math.random()*H,
      r: 0.2 + depth*1.5,
      v: 0.03 + depth*0.18,
      a: 0.20 + depth*0.55,
      tw: Math.random()*Math.PI*2,
      tws: 0.4 + Math.random()*1.4,
      hue: Math.random() < 0.08 ? 'gold' : (Math.random() < 0.05 ? 'red' : 'white'),
      spike: depth > 0.92 && Math.random() < 0.4,
    });
  }
}
let comets = [];
function maybeSpawnComet(t){
  if (t - (window.__lastComet||0) < 3500) return;
  if (Math.random() > 0.28) { window.__lastComet = t; return; }
  window.__lastComet = t;
  const fromTop = Math.random() < 0.5;
  const x0 = fromTop ? Math.random()*W*0.6 + W*0.2 : (Math.random() < 0.5 ? -40 : W+40);
  const y0 = fromTop ? -40 : Math.random()*H*0.5;
  const dirX = fromTop ? (Math.random()-0.3) * 0.6 : (x0 < 0 ? 1 : -1);
  const dirY = fromTop ? 1 : Math.random()*0.5 + 0.3;
  const speed = 1.2 + Math.random()*0.8;
  comets.push({ x:x0,y:y0,vx:dirX*speed,vy:dirY*speed,trail:[],life:0,max:2400+Math.random()*1200,hue:Math.random()<0.5?'gold':'warm' });
}
function drawComets(dt){
  const g = ctx.stars;
  for (let i = comets.length-1; i >= 0; i--){
    const c = comets[i];
    c.life += dt;
    if (c.life > c.max || c.x < -80 || c.x > W+80 || c.y > H+80){ comets.splice(i,1); continue; }
    c.x += c.vx * dt * 0.18; c.y += c.vy * dt * 0.18;
    c.trail.push([c.x,c.y]); if (c.trail.length > 22) c.trail.shift();
    const colA = c.hue === 'gold' ? '232,212,154' : '255,160,110';
    for (let k = 0; k < c.trail.length; k++){
      const [tx,ty] = c.trail[k]; const ka = (k/c.trail.length);
      g.fillStyle = `rgba(${colA},${ka*0.5})`;
      g.beginPath(); g.arc(tx,ty,0.8+ka*1.4,0,Math.PI*2); g.fill();
    }
    const hg = g.createRadialGradient(c.x,c.y,0,c.x,c.y,10);
    hg.addColorStop(0,'rgba(255,255,255,1)');
    hg.addColorStop(0.3,`rgba(${colA},0.8)`);
    hg.addColorStop(1,`rgba(${colA},0)`);
    g.fillStyle = hg; g.beginPath(); g.arc(c.x,c.y,10,0,Math.PI*2); g.fill();
    g.fillStyle = '#fff'; g.beginPath(); g.arc(c.x,c.y,1.6,0,Math.PI*2); g.fill();
  }
}
function drawStars(dt, t){
  const g = ctx.stars; g.clearRect(0,0,W,H);
  maybeSpawnComet(t); drawComets(dt);
  for (const s of stars){
    s.x -= s.v * dt * 0.04;
    if (s.x < -3){ s.x = W+3; s.y = Math.random()*H; }
    const flick = 0.6 + 0.4*Math.sin(s.tw + t*0.001*s.tws);
    const a = s.a * flick;
    let col;
    if (s.hue === 'gold') col = `rgba(232,212,154,${a})`;
    else if (s.hue === 'red') col = `rgba(255,90,90,${a})`;
    else col = `rgba(245,236,219,${a})`;
    g.fillStyle = col; g.beginPath(); g.arc(s.x,s.y,s.r,0,Math.PI*2); g.fill();
    if (s.r > 1.1){
      const hg = g.createRadialGradient(s.x,s.y,0,s.x,s.y,s.r*5);
      hg.addColorStop(0, col.replace(/[\d.]+\)$/, (a*0.45)+')'));
      hg.addColorStop(1, col.replace(/[\d.]+\)$/, '0)'));
      g.fillStyle = hg; g.beginPath(); g.arc(s.x,s.y,s.r*5,0,Math.PI*2); g.fill();
    }
    if (s.spike){
      g.strokeStyle = col.replace(/[\d.]+\)$/, (a*0.85)+')');
      g.lineWidth = 0.6;
      const L = s.r * 9 * flick;
      g.beginPath();
      g.moveTo(s.x-L,s.y); g.lineTo(s.x+L,s.y);
      g.moveTo(s.x,s.y-L); g.lineTo(s.x,s.y+L);
      g.stroke();
    }
  }
}
seedDust(); seedStars();

// =================================================================
// LIVING MEMORY CLOUD
//
// One big CORE in the middle, 8 category orbs on a ring around it.
// Behind everything: a dense particle "web of light" — thousands
// of short segments connecting CORE outward + crossings between
// neighbouring categories. Continuous flow particles travel along
// every orb-spoke. Click any orb → opens the zoom card.
// =================================================================
const CORE = { id: 'core', x: 0, y: 0, r: 82, label: 'Core Memory', sub: 'Everything Connected', nodes: 0, color: RED };
let CATS = [];  // populated by /api/clusters minus 'core'
let webParticles = [];   // tiny dots on radial lines forming the web
let webSegments  = [];   // long chord segments for cross-connections
let spokes = [];         // continuous flow on each CORE→cat line
let lastFlow = 0;
let hoverCat = null;

const ORBIT_R_BASE = 0.40;

function layoutBubbles(){
  measureStage();
  CORE.x = STAGE.cx;
  CORE.y = STAGE.cy;
  const R = Math.min(STAGE.w, STAGE.h) * ORBIT_R_BASE;
  CATS.forEach((c, i) => {
    const ang = (i / CATS.length) * Math.PI * 2 - Math.PI / 2;
    c.ang = ang;
    c.x = CORE.x + Math.cos(ang) * R;
    c.y = CORE.y + Math.sin(ang) * R;
    // BIGGER orbs — they now carry interior textures and need presence
    c.r = 52 + Math.min(24, Math.log2((c.nodes||1)+1) * 5);
    c._wobP = Math.random() * Math.PI*2;
    c._wobS = 0.0003 + Math.random()*0.0003;
  });
  seedWeb();
}

function seedWeb(){
  webParticles = [];
  webSegments = [];
  if (!CATS.length) return;
  const R = Math.min(STAGE.w, STAGE.h) * ORBIT_R_BASE;
  // Radial particles around CORE — fill the orbit ring
  const N = 320;
  for (let i = 0; i < N; i++){
    const ang = Math.random() * Math.PI * 2;
    const rad = R * (0.05 + Math.random() * 0.95);
    webParticles.push({
      ang, rad,
      angSpeed: (Math.random() - 0.5) * 0.00015,
      r: 0.5 + Math.random()*1.0,
      a: 0.15 + Math.random()*0.45,
      hue: Math.random() < 0.7 ? 'red' : 'gold',
      tw: Math.random()*Math.PI*2,
    });
  }
  // Cross-segments between adjacent categories (chord arcs)
  for (let i = 0; i < CATS.length; i++){
    const c1 = CATS[i];
    for (let k = 0; k < 3; k++){
      const j = (i + 1 + ((Math.random()*2)|0)) % CATS.length;
      const c2 = CATS[j];
      webSegments.push({
        a: c1, b: c2,
        // a curve sagging toward CORE
        bend: 0.3 + Math.random()*0.4,
        alpha: 0.06 + Math.random()*0.10,
        col: Math.random() < 0.5 ? 'red' : 'gold',
      });
    }
  }
}

function drawWeb(t, dt){
  if (!CATS.length) return;
  const g = ctx.cloud;
  // Cross arcs between cats
  for (const s of webSegments){
    const mx = (s.a.x + s.b.x) / 2;
    const my = (s.a.y + s.b.y) / 2;
    // pull control point toward CORE for an inward bow
    const cpx = mx + (CORE.x - mx) * s.bend;
    const cpy = my + (CORE.y - my) * s.bend;
    const colA = s.col === 'red' ? '220,38,38' : '232,212,154';
    g.strokeStyle = `rgba(${colA},${s.alpha})`;
    g.lineWidth = 0.7;
    g.beginPath(); g.moveTo(s.a.x, s.a.y); g.quadraticCurveTo(cpx, cpy, s.b.x, s.b.y); g.stroke();
  }
  // Radial particles
  for (const p of webParticles){
    p.ang += p.angSpeed * dt;
    const x = CORE.x + Math.cos(p.ang) * p.rad;
    const y = CORE.y + Math.sin(p.ang) * p.rad;
    const flick = 0.7 + 0.3*Math.sin(p.tw + t*0.0015);
    const colA = p.hue === 'red' ? '220,38,38' : '232,212,154';
    g.fillStyle = `rgba(${colA},${p.a*flick})`;
    g.beginPath(); g.arc(x, y, p.r, 0, Math.PI*2); g.fill();
    if (p.r > 0.8){
      const hg = g.createRadialGradient(x, y, 0, x, y, p.r*4);
      hg.addColorStop(0, `rgba(${colA},${p.a*flick*0.5})`);
      hg.addColorStop(1, `rgba(${colA},0)`);
      g.fillStyle = hg;
      g.beginPath(); g.arc(x, y, p.r*4, 0, Math.PI*2); g.fill();
    }
  }
  // Bright spokes from CORE → each category (multi-pass glow)
  for (const c of CATS){
    const isHot = hoverCat === c.slug;
    const colA = hexToRgbStr(c.color);
    for (let pass = 0; pass < 3; pass++){
      let alpha, width;
      if (pass === 0){ alpha = isHot ? 0.22 : 0.10; width = isHot ? 7 : 5; }
      else if (pass === 1){ alpha = isHot ? 0.4 : 0.20; width = isHot ? 3 : 2; }
      else { alpha = isHot ? 0.85 : 0.55; width = isHot ? 1.4 : 0.9; }
      g.strokeStyle = `rgba(${colA},${alpha})`;
      g.lineWidth = width;
      g.beginPath(); g.moveTo(CORE.x, CORE.y); g.lineTo(c.x, c.y); g.stroke();
    }
  }
}

function seedFlow(t){
  if (t - lastFlow < 200) return;
  lastFlow = t;
  if (!CATS.length) return;
  const count = 1 + (Math.random()<0.5?1:0);
  for (let i=0;i<count;i++){
    const c = CATS[(Math.random()*CATS.length)|0];
    spokes.push({ cat: c, t: 0, life: 1200+Math.random()*900, sz: 1.4+Math.random()*0.9, inward: Math.random()<0.4 });
  }
  if (spokes.length > 80) spokes.splice(0, spokes.length-80);
}
function drawFlow(dt){
  const g = ctx.cloud;
  for (let i = spokes.length-1; i >= 0; i--){
    const p = spokes[i]; p.t += dt;
    const pr = p.t / p.life;
    if (pr >= 1){ spokes.splice(i,1); continue; }
    const e = 1 - Math.pow(1-pr, 2);
    const from = p.inward ? p.cat : CORE;
    const to   = p.inward ? CORE : p.cat;
    const x = from.x + (to.x - from.x)*e;
    const y = from.y + (to.y - from.y)*e;
    const alpha = pr < 0.1 ? pr*10 : (pr > 0.85 ? (1-pr)*7 : 1);
    const colA = hexToRgbStr(p.cat.color);
    g.fillStyle = `rgba(${colA},${0.85*alpha})`;
    g.beginPath(); g.arc(x,y,p.sz,0,Math.PI*2); g.fill();
    g.fillStyle = `rgba(${colA},${0.25*alpha})`;
    g.beginPath(); g.arc(x,y,p.sz*3.5,0,Math.PI*2); g.fill();
  }
}

function drawBubble(g, n, isCore, t){
  const wob = n._wobP != null ? Math.sin(t*n._wobS + n._wobP) : 0;
  const x = n.x + wob * 3;
  const y = n.y + Math.cos(t*(n._wobS||0.0003) + (n._wobP||0)) * 2.4;
  const breath = 1 + 0.05 * Math.sin(t*0.0024 + (n._wobP||0));
  const r = (n.r || 40) * breath;
  const colA = hexToRgbStr(n.color || RED);
  const isHot = hoverCat === n.slug;
  // outer halo
  const halo = g.createRadialGradient(x, y, 0, x, y, r*3.5);
  halo.addColorStop(0, `rgba(${colA},${isHot?0.55:0.35})`);
  halo.addColorStop(0.45, `rgba(${colA},${isHot?0.18:0.10})`);
  halo.addColorStop(1, `rgba(${colA},0)`);
  g.fillStyle = halo;
  g.beginPath(); g.arc(x, y, r*3.5, 0, Math.PI*2); g.fill();
  // bubble body — semi-transparent inner glow
  const body = g.createRadialGradient(x - r*0.3, y - r*0.4, r*0.1, x, y, r);
  body.addColorStop(0, `rgba(255,255,255,${isCore?0.45:0.25})`);
  body.addColorStop(0.55, `rgba(${colA},${isCore?0.85:0.45})`);
  body.addColorStop(1, `rgba(${colA},${isCore?0.55:0.18})`);
  g.fillStyle = body;
  g.beginPath(); g.arc(x, y, r, 0, Math.PI*2); g.fill();
  // ring outline
  g.strokeStyle = `rgba(${colA},${isHot?1:0.85})`;
  g.lineWidth = isCore ? 2.4 : (isHot ? 1.8 : 1.2);
  g.beginPath(); g.arc(x, y, r, 0, Math.PI*2); g.stroke();
  // inner subtle ring
  g.strokeStyle = `rgba(${colA},${isCore?0.4:0.3})`;
  g.lineWidth = 0.6;
  g.beginPath(); g.arc(x, y, r*0.78, 0, Math.PI*2); g.stroke();
  // labels
  const isMobile = W < 760;
  const labelSize = isCore ? (isMobile?12:14) : (isMobile?10:11.5);
  g.font = `${labelSize}px 'JetBrains Mono', monospace`;
  g.textAlign = 'center'; g.textBaseline = 'middle';
  if (isCore){
    g.fillStyle = GOLD_BRIGHT;
    g.fillText(n.label.toUpperCase(), x, y - 6);
    g.font = `${labelSize-2}px 'Inter', sans-serif`;
    g.fillStyle = FG_SOFT;
    g.fillText(n.sub || '', x, y + 8);
    g.font = `${isMobile?16:20}px 'JetBrains Mono', monospace`;
    g.fillStyle = '#ffffff';
    g.fillText(formatNumber(n.nodes), x, y + 28);
    g.font = `9px 'JetBrains Mono', monospace`;
    g.fillStyle = FG_DIM;
    g.fillText('NODES', x, y + 46);
  } else {
    // outside the bubble (below)
    const ly = y + r + 14;
    g.font = `${labelSize}px 'JetBrains Mono', monospace`;
    g.fillStyle = `rgba(${colA},0.95)`;
    g.fillText(n.label.toUpperCase(), x, ly);
    g.font = `${labelSize-2}px 'Inter', sans-serif`;
    g.fillStyle = FG_DIM;
    g.fillText(n.sub || '', x, ly + 13);
    g.font = `${labelSize}px 'JetBrains Mono', monospace`;
    g.fillStyle = FG_SOFT;
    g.fillText(formatNumber(n.nodes) + ' nodes', x, ly + 28);
  }
}

function hexToRgbStr(hex){
  if (hex.startsWith('rgb')) return hex.replace(/[rgba()\s]/g,'');
  const h = hex.replace('#','');
  return `${parseInt(h.slice(0,2),16)},${parseInt(h.slice(2,4),16)},${parseInt(h.slice(4,6),16)}`;
}
function formatNumber(n){
  if (n == null) return '0';
  if (n >= 1000) return n.toLocaleString();
  return String(n);
}

function drawCloud(t, dt){
  const g = ctx.cloud; g.clearRect(0,0,W,H);
  if (!CATS.length) return;
  const webglActive = !!window.Cloud3D;
  if (webglActive){
    // Just labels; the WebGL layer underneath handles bodies, web, spokes.
    for (const c of CATS) drawLabelOnly(g, c, false, t);
    drawLabelOnly(g, CORE, true, t);
    return;
  }
  drawWeb(t, dt);
  drawFlow(dt);
  for (const c of CATS) drawBubble(g, c, false, t);
  drawBubble(g, CORE, true, t);
}

function drawLabelOnly(g, n, isCore, t){
  // Live-project from the 3D scene so labels follow bubbles through
  // rotation, parallax, and 3D tilt.  Falls back to CATS.x/y if Cloud3D
  // hasn't initialised.
  let x = n.x, y = n.y;
  if (window.Cloud3D?.getBubbleScreenPos){
    const id = isCore ? CORE.id : n.id;
    const sp = window.Cloud3D.getBubbleScreenPos(id);
    if (sp){ x = sp.x; y = sp.y; }
  }
  // When a bubble is focused, dim non-focused labels so the focused
  // bubble carries the attention.  Skip entirely if too far below 0.2.
  const focused = SubBubbles.focusCat;
  let labelAlpha = 1.0;
  if (focused && !isCore && focused !== n){
    labelAlpha = 0.18;
  } else if (focused && isCore && focused !== CORE){
    labelAlpha = 0.18;
  }
  if (labelAlpha < 0.25) return;     // skip drawing for very dim
  g.globalAlpha = labelAlpha;
  const r = (n.r || 30);
  const isHot = hoverCat === n.slug;
  const colA = hexToRgbStr(n.color || RED);
  const isMobile = W < 760;
  const labelSize = isCore ? (isMobile?12:14) : (isMobile?10:11.5);
  g.textAlign = 'center'; g.textBaseline = 'middle';

  if (isCore){
    g.font = `${labelSize}px 'JetBrains Mono', monospace`;
    g.fillStyle = '#f5dca3';
    g.fillText(n.label.toUpperCase(), x, y - 6);
    g.font = `${labelSize-2}px 'Inter', sans-serif`;
    g.fillStyle = FG_SOFT;
    g.fillText(n.sub || '', x, y + 8);
    g.font = `${isMobile?16:22}px 'JetBrains Mono', monospace`;
    g.fillStyle = '#ffffff';
    g.fillText(formatNumber(n.nodes), x, y + 30);
    g.font = `9px 'JetBrains Mono', monospace`;
    g.fillStyle = FG_DIM;
    g.fillText('NODES', x, y + 48);
  } else {
    // --- COUNT BADGE above the bubble — small mono pill with the
    //     category-colored border.  Visible identity marker.
    const badgeY = y - r - 18;
    const badgeTxt = formatNumber(n.nodes);
    g.font = `bold 12px 'JetBrains Mono', monospace`;
    const btw = g.measureText(badgeTxt).width;
    const bw = btw + 18, bh = 22;
    // pill background
    g.fillStyle = 'rgba(8,3,6,0.92)';
    roundRect(g, x - bw/2, badgeY - bh/2, bw, bh, 11, true, false);
    // pill border (category color)
    g.strokeStyle = `rgba(${colA},${isHot?1:0.85})`;
    g.lineWidth = 1.1;
    roundRect(g, x - bw/2, badgeY - bh/2, bw, bh, 11, false, true);
    // little category dot at left edge
    g.fillStyle = `rgba(${colA},1)`;
    g.beginPath(); g.arc(x - bw/2 + 8, badgeY, 2.6, 0, Math.PI*2); g.fill();
    // number
    g.fillStyle = '#ffffff';
    g.font = `bold 11px 'JetBrains Mono', monospace`;
    g.textBaseline = 'middle';
    g.fillText(badgeTxt, x + 4, badgeY);
    g.textBaseline = 'middle';

    // --- LABEL below the bubble (category name + sub)
    const ly = y + r + 16;
    g.font = `${labelSize}px 'JetBrains Mono', monospace`;
    g.fillStyle = 'rgba(8,3,6,0.85)';
    const tw1 = g.measureText(n.label.toUpperCase()).width;
    g.fillRect(x - tw1/2 - 6, ly - 8, tw1 + 12, 15);
    g.fillStyle = `rgba(${colA},${isHot?1:0.95})`;
    g.fillText(n.label.toUpperCase(), x, ly);
    g.font = `${labelSize-2}px 'Inter', sans-serif`;
    g.fillStyle = FG_DIM;
    g.fillText(n.sub || '', x, ly + 14);
    // sub: "nodes" descriptor underneath (faint)
    g.font = `9px 'JetBrains Mono', monospace`;
    g.fillStyle = FG_DIM;
    g.fillText('NODES IN CLUSTER', x, ly + 28);
  }
  g.globalAlpha = 1.0;
}

// Rounded-rect helper used by the count badge
function roundRect(g, x, y, w, h, r, fill, stroke){
  g.beginPath();
  g.moveTo(x + r, y);
  g.lineTo(x + w - r, y);
  g.quadraticCurveTo(x + w, y, x + w, y + r);
  g.lineTo(x + w, y + h - r);
  g.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  g.lineTo(x + r, y + h);
  g.quadraticCurveTo(x, y + h, x, y + h - r);
  g.lineTo(x, y + r);
  g.quadraticCurveTo(x, y, x + r, y);
  g.closePath();
  if (fill) g.fill();
  if (stroke) g.stroke();
}

// Pick whichever canvas is currently receiving pointer events
function getClickCanvas(){
  return document.getElementById('cloud3d') || cv.cloud;
}

// Sync hover into Cloud3D each frame
function syncHoverToCloud3D(){
  if (window.Cloud3D){
    window.Cloud3D.setHover(hoverCat);
  }
}

// ----- hit test
function catAt(x, y){
  for (const c of CATS){
    const dx = x - c.x, dy = y - c.y;
    const r = c.r + 6;
    if (dx*dx + dy*dy <= r*r) return c;
  }
  const dx = x - CORE.x, dy = y - CORE.y;
  if (dx*dx + dy*dy <= (CORE.r+6)*(CORE.r+6)) return CORE;
  return null;
}
getClickCanvas().addEventListener('mousemove', (e) => {
  // Sub-bubbles take hover priority when active
  if (SubBubbles.orbits.length){
    const idx = subBubbleAt(e.clientX, e.clientY);
    SubBubbles.hoverIdx = idx;
    if (idx >= 0){
      getClickCanvas().style.cursor = 'pointer';
      document.getElementById('tip').classList.remove('show');
      return;
    }
  }
  const c = catAt(e.clientX, e.clientY);
  hoverCat = c ? c.slug : null;
  syncHoverToCloud3D();
  if (typeof syncHoverContext === 'function') syncHoverContext(c ? c.slug : 'core');
  const cnv = getClickCanvas();
  const tip = document.getElementById('tip');
  if (c){
    cnv.style.cursor = 'pointer';
    tip.innerHTML = `<div class="head">${c === CORE ? 'core memory' : c.slug}</div><div class="body">${c.label}<br>${c.sub||''}<br>${formatNumber(c.nodes)} nodes</div>`;
    tip.style.left = e.clientX + 'px';
    tip.style.top  = e.clientY + 'px';
    tip.classList.add('show');
  } else {
    cnv.style.cursor = 'default';
    tip.classList.remove('show');
  }
});
getClickCanvas().addEventListener('mouseleave', () => { hoverCat = null; document.getElementById('tip').classList.remove('show'); });
getClickCanvas().addEventListener('click', (e) => {
  // Sub-bubble click — read the memory
  if (SubBubbles.orbits.length){
    const idx = subBubbleAt(e.clientX, e.clientY);
    if (idx >= 0){
      const mem = SubBubbles.orbits[idx].item;
      if (mem?.id) openMemoryDrawer(mem.id);
      Audio?.shimmer && Audio.shimmer(1400);
      return;
    }
  }
  const c = catAt(e.clientX, e.clientY);
  if (c) openZoom(c);
});
getClickCanvas().addEventListener('touchend', (e) => {
  if (!e.changedTouches.length) return;
  const t = e.changedTouches[0];
  if (SubBubbles.orbits.length){
    const idx = subBubbleAt(t.clientX, t.clientY);
    if (idx >= 0){
      const mem = SubBubbles.orbits[idx].item;
      if (mem?.id) openMemoryDrawer(mem.id);
      return;
    }
  }
  const c = catAt(t.clientX, t.clientY);
  if (c) openZoom(c);
});

// =================================================================
// ZOOM-INTO-BUBBLE
// =================================================================
const CAT_HERO_IMG = {
  'coding':   '/static/gen/cat_coding.jpg',
  'web':      '/static/gen/cat_web.jpg',
  'mobile':   '/static/gen/cat_mobile.jpg',
  'github':   '/static/gen/cat_github.jpg',
  'agents':   '/static/gen/cat_agents.jpg',
  'planning': '/static/gen/cat_planning.jpg',
  'longterm': '/static/gen/cat_longterm.jpg',
  'terminal': '/static/gen/cat_terminal.jpg',
  'core':     '/static/gen/zai_lockup.jpg',
};

// =================================================================
// SUB-CONSTELLATION  —  inside a focused bubble, memories appear as
// a ring of small orbs around it; click any → opens the memory.
// =================================================================
const SubBubbles = { focusCat: null, items: [], orbits: [], hoverIdx: -1 };

function subBubbleAt(x, y){
  // Reverse iterate — last drawn / on top wins
  for (let i = SubBubbles.orbits.length - 1; i >= 0; i--){
    const ob = SubBubbles.orbits[i];
    const dx = x - ob.x, dy = y - ob.y;
    if (dx*dx + dy*dy <= (ob.r+5)*(ob.r+5)) return i;
  }
  return -1;
}

function layoutSubBubbles(){
  // Position N memory orbs in a ring around the focused bubble's
  // current screen position.  Smaller orbs further out, count scales
  // with importance.
  if (!SubBubbles.focusCat) { SubBubbles.orbits = []; return; }
  const cat = SubBubbles.focusCat;
  const items = SubBubbles.items;
  if (!items.length){ SubBubbles.orbits = []; return; }
  const sp = window.Cloud3D?.getBubbleScreenPos ? window.Cloud3D.getBubbleScreenPos(cat.id) : null;
  const cx = sp ? sp.x : cat.x;
  const cy = sp ? sp.y : cat.y;
  const ringR = Math.min(W, H) * 0.32;     // ring distance from focused bubble
  const N = Math.min(items.length, 12);
  SubBubbles.orbits = [];
  for (let i = 0; i < N; i++){
    const t = i / N;
    const ang = -Math.PI/2 + t * Math.PI * 2;
    const r = 22 + (items[i].importance || 3) * 2.5;
    SubBubbles.orbits.push({
      x: cx + Math.cos(ang) * ringR,
      y: cy + Math.sin(ang) * ringR,
      r,
      item: items[i],
      ang,
      phase: Math.random() * Math.PI * 2,
    });
  }
}

function drawSubBubbles(t){
  if (!SubBubbles.orbits.length) return;
  const g = ctx.cloud;
  layoutSubBubbles();
  const cat = SubBubbles.focusCat;
  const sp = window.Cloud3D?.getBubbleScreenPos ? window.Cloud3D.getBubbleScreenPos(cat.id) : null;
  const cx = sp ? sp.x : cat.x;
  const cy = sp ? sp.y : cat.y;
  const colA = hexToRgbStr(cat.color || RED);

  // Faint connecting lines from focused bubble to each sub-bubble
  for (const ob of SubBubbles.orbits){
    g.strokeStyle = `rgba(${colA},0.18)`;
    g.lineWidth = 0.6;
    g.beginPath(); g.moveTo(cx, cy); g.lineTo(ob.x, ob.y); g.stroke();
  }

  // Sub-orbs
  SubBubbles.orbits.forEach((ob, i) => {
    const isHot = SubBubbles.hoverIdx === i;
    const breath = 1 + 0.06 * Math.sin(t * 0.002 + ob.phase);
    const r = ob.r * breath * (isHot ? 1.25 : 1);
    // halo
    const hg = g.createRadialGradient(ob.x, ob.y, 0, ob.x, ob.y, r * 3);
    hg.addColorStop(0, `rgba(${colA},${isHot ? 0.55 : 0.32})`);
    hg.addColorStop(1, `rgba(${colA},0)`);
    g.fillStyle = hg;
    g.beginPath(); g.arc(ob.x, ob.y, r * 3, 0, Math.PI*2); g.fill();
    // body
    const body = g.createRadialGradient(ob.x - r*0.3, ob.y - r*0.3, r*0.1, ob.x, ob.y, r);
    body.addColorStop(0, `rgba(255,255,255,${isHot ? 0.65 : 0.35})`);
    body.addColorStop(0.6, `rgba(${colA},${isHot ? 0.85 : 0.55})`);
    body.addColorStop(1, `rgba(${colA},0.20)`);
    g.fillStyle = body;
    g.beginPath(); g.arc(ob.x, ob.y, r, 0, Math.PI*2); g.fill();
    // rim
    g.strokeStyle = `rgba(${colA},${isHot ? 1 : 0.85})`;
    g.lineWidth = isHot ? 1.6 : 1.0;
    g.beginPath(); g.arc(ob.x, ob.y, r, 0, Math.PI*2); g.stroke();
    // preview text on hover
    if (isHot){
      const text = (ob.item.preview || '').slice(0, 72) + ((ob.item.preview || '').length > 72 ? '…' : '');
      g.font = "11px 'Inter', sans-serif";
      g.textAlign = 'center';
      g.textBaseline = 'top';
      const tw = g.measureText(text).width;
      g.fillStyle = 'rgba(8,3,6,0.92)';
      g.fillRect(ob.x - tw/2 - 8, ob.y + r + 8, tw + 16, 36);
      g.strokeStyle = `rgba(${colA},0.7)`;
      g.lineWidth = 0.8;
      g.strokeRect(ob.x - tw/2 - 8, ob.y + r + 8, tw + 16, 36);
      g.fillStyle = FG;
      g.fillText(text, ob.x, ob.y + r + 14);
      g.font = "9px 'JetBrains Mono', monospace";
      g.fillStyle = `rgba(${colA},0.95)`;
      g.fillText(((ob.item.written_by || '').replace('-claude','').toUpperCase() + ' · imp ' + (ob.item.importance || 3)).trim(), ob.x, ob.y + r + 28);
    }
  });
}

async function openZoom(cat){
  const shell = document.getElementById('zoomShell');
  const card = shell.querySelector('.zoom-card');
  const heroUrl = CAT_HERO_IMG[cat.slug] || CAT_HERO_IMG.core;
  card.style.setProperty('--hero-img', `url(${heroUrl})`);
  card.classList.remove('no-hero');
  // Trigger sub-constellation: fetch memories for this cluster, populate
  // ring of orbs around the focused bubble.  The card itself slides in
  // from the side as a quieter "list view" — see CSS .zoom-card.sidecar.
  SubBubbles.focusCat = cat;
  SubBubbles.items = [];
  // CORE → show most-recent memories regardless of category.
  // Category → show that cluster's memories.
  const endpoint = cat === CORE ? '/api/recent?n=12' : '/api/cluster/' + cat.slug;
  fetch(endpoint).then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    const items = (d.items != null) ? d.items : d;
    SubBubbles.items = (items || []).slice(0, 12).map(m => ({
      id: m.id,
      preview: m.preview || (m.content || '').slice(0, 110),
      written_by: m.written_by,
      importance: m.importance || 3,
    }));
    layoutSubBubbles();
  });
  // Update breadcrumb
  updateBreadcrumb([
    { label: 'Universe', cb: () => closeZoom() },
    { label: (cat === CORE ? 'CORE' : cat.label), cb: null },
  ]);
  // Trigger Cloud3D camera warp-in — sub-constellation IS the primary view
  if (window.Cloud3D?.focusBubble){
    window.Cloud3D.focusBubble(cat.id || cat.slug);
    window.Cloud3D.setSelected(cat.id || cat.slug);
  }
  // Add to session trail (drawn as glowing path of recent visits)
  if (typeof pushTrail === 'function') pushTrail(cat);
  Audio?.whoosh && Audio.whoosh();
  Audio?.filterSweep && Audio.filterSweep(240, 600);
  // The zoomShell card is no longer opened by default — sub-bubbles are
  // the primary interaction.  An "Open list view" affordance in the
  // breadcrumb area lets the user pop the card on demand.
  document.body.classList.add('in-bubble');
}

function openListView(cat){
  // Optional alternative — opens the old zoom-card list view (for users
  // who prefer reading memories as a grid instead of clicking orbs).
  const shell = document.getElementById('zoomShell');
  const card = shell.querySelector('.zoom-card');
  const heroUrl = CAT_HERO_IMG[cat.slug] || CAT_HERO_IMG.core;
  card.style.setProperty('--hero-img', `url(${heroUrl})`);
  card.classList.remove('no-hero');
  document.getElementById('zoomKind').textContent = cat === CORE ? 'Core Memory' : cat.label;
  document.getElementById('zoomTitle').textContent = (cat === CORE ? 'Everything Connected' : cat.label);
  document.getElementById('zoomDesc').textContent = (cat.sub || '');
  document.getElementById('zoomStats').innerHTML = '';
  document.getElementById('zoomBubbles').innerHTML = `<div class="zoom-empty">loading…</div>`;
  shell.classList.add('active'); shell.setAttribute('aria-hidden', 'false');
  fetch('/api/cluster/' + (cat.slug || 'core')).then(r => r.json()).then(d => renderZoom(d, cat)).catch(() => {});
}
window.__zai_openListView = (slug) => {
  const cat = slug === 'core' ? CORE : CATS.find(c => c.slug === slug);
  if (cat) openListView(cat);
};
function renderZoom(d, cat){
  const items = d.items || [];
  document.getElementById('zoomStats').innerHTML = `
    <div class="zoom-stat"><div class="k">Total Items</div><div class="v">${formatNumber(items.length)}</div></div>
    <div class="zoom-stat"><div class="k">In Category</div><div class="v">${formatNumber(d.cluster.nodes ?? items.length)}</div></div>
    <div class="zoom-stat"><div class="k">Latest</div><div class="v" style="font-size:13px;color:var(--gold)">${items[0] ? new Date(items[0].created_at).toLocaleString('en-CA', {hour12:false, month:'short', day:'numeric'}) : '—'}</div></div>
  `;
  const accent = cat.color || RED_HOT;
  if (!items.length){
    document.getElementById('zoomBubbles').innerHTML = `<div class="zoom-empty">no memories tagged for this category yet — drop one in and it will appear here.</div>`;
    return;
  }
  document.getElementById('zoomBubbles').innerHTML = items.map(m => `
    <div class="subb" style="--accent-c:${accent}" data-mem="${escapeHtml(m.id)}">
      <div class="meta"><span>${escapeHtml((m.written_by||'').replace('-claude','').toUpperCase())}</span><span>imp ${m.importance}</span></div>
      <div class="preview">${escapeHtml(m.preview)}</div>
      ${(m.tags && m.tags.length) ? `<div class="tags">${m.tags.slice(0,5).map(t => `<span class="tg">${escapeHtml(t)}</span>`).join('')}</div>` : ''}
    </div>`).join('');
  document.querySelectorAll('.subb').forEach(el => el.addEventListener('click', () => openMemoryDrawer(el.dataset.mem)));
}
function closeZoom(){
  const shell = document.getElementById('zoomShell');
  shell.classList.remove('active'); shell.setAttribute('aria-hidden', 'true');
  if (window.Cloud3D?.unfocusBubble){
    window.Cloud3D.unfocusBubble();
    window.Cloud3D.setSelected(null);
  }
  SubBubbles.focusCat = null;
  SubBubbles.items = [];
  SubBubbles.orbits = [];
  SubBubbles.hoverIdx = -1;
  document.body.classList.remove('in-bubble');
  Audio?.filterSweep && Audio.filterSweep(600, 240);
  updateBreadcrumb([{ label: 'Universe', cb: null }]);
}
document.getElementById('zoomBack').addEventListener('click', closeZoom);
document.getElementById('zoomShell').addEventListener('click', (e) => { if (e.target.id === 'zoomShell') closeZoom(); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape'){
    if (document.getElementById('drawer').classList.contains('open')) closeDrawer();
    else closeZoom();
  }
});

// --- memory drawer (existing flow)
async function openMemoryDrawer(mid){
  const dr = document.getElementById('drawer');
  const body = document.getElementById('drawerBody');
  body.innerHTML = `<div class="kind">Memory · loading</div>`;
  dr.classList.add('open'); dr.setAttribute('aria-hidden','false');
  try{
    const [r, recentR] = await Promise.all([
      fetch('/api/memory/' + mid),
      fetch('/api/recent?n=80'),
    ]);
    if (!r.ok) throw new Error(r.status);
    const m = await r.json();
    const recent = recentR.ok ? await recentR.json() : [];
    // Compute neighbours: other memories that share >=2 tags
    const tagSet = new Set(m.tags || []);
    const neighbours = recent
      .filter(x => x.id !== m.id)
      .map(x => {
        const ov = (x.tags || []).filter(t => tagSet.has(t));
        return { ...x, overlap: ov };
      })
      .filter(x => x.overlap.length >= 2)
      .sort((a, b) => b.overlap.length - a.overlap.length)
      .slice(0, 5);
    const ents = (m.entity_slugs || []).map(s => `
      <div class="relrow" data-ent="${escapeHtml(s)}">
        <span class="dot ent"></span><span class="lbl">${escapeHtml(s)}</span>
        <span class="ovl">entity</span>
      </div>
    `).join('');
    const neigh = neighbours.map(n => `
      <div class="relrow" data-mem="${escapeHtml(n.id)}">
        <span class="dot"></span><span class="lbl">${escapeHtml(truncate(n.content, 60))}…</span>
        <span class="ovl">${n.overlap.length} shared</span>
      </div>
    `).join('');
    const tags = (m.tags || []).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('');
    body.innerHTML = `
      <div class="kind">Memory · imp ${m.importance}</div>
      <h2>${escapeHtml(truncate(m.content, 110))}${m.content.length > 110 ? '…' : ''}</h2>
      <div class="meta">
        <span class="k">by</span><span class="v">${escapeHtml(m.written_by||'')}</span>
        <span class="k">at</span><span class="v">${escapeHtml(new Date(m.created_at).toLocaleString('en-CA',{hour12:false}))}</span>
        <span class="k">id</span><span class="v">${escapeHtml(m.id)}</span>
      </div>
      <div class="body">${escapeHtml(m.content)}</div>
      ${tags ? '<div class="tags" style="margin-top:18px">' + tags + '</div>' : ''}
      ${(ents || neigh) ? `
        <div class="relmap">
          <h3>Relation map</h3>
          ${ents}
          ${neigh}
        </div>` : ''}
    `;
    // Wire the relmap rows
    body.querySelectorAll('.relmap .relrow[data-mem]').forEach(el => {
      el.addEventListener('click', () => openMemoryDrawer(el.dataset.mem));
    });
    body.querySelectorAll('.relmap .relrow[data-ent]').forEach(el => {
      el.addEventListener('click', () => {
        const slug = el.dataset.ent;
        // Map entity slug heuristically to a cluster, otherwise just open longterm
        const map = { 'zai-memory-hub': 'longterm', 'anteroom-studio': 'github', 'zawwar': 'core' };
        const cs = map[slug] || 'core';
        const target = cs === 'core' ? CORE : CATS.find(c => c.slug === cs);
        closeDrawer();
        if (target) openZoom(target);
      });
    });
  } catch(e){ body.innerHTML = `<div class="kind">Memory</div><div class="body">load failed</div>`; }
}
function closeDrawer(){ const dr = document.getElementById('drawer'); dr.classList.remove('open'); dr.setAttribute('aria-hidden','true'); }
document.getElementById('drawerClose').addEventListener('click', closeDrawer);

function truncate(s,n){ s = s || ''; return s.length > n ? s.slice(0,n) : s; }
function escapeHtml(s){ if (s==null) return ''; return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
function humanAge(s){ if (s < 60) return Math.floor(s)+'s'; if (s < 3600) return Math.floor(s/60)+'m'; if (s < 86400) return Math.floor(s/3600)+'h'; return Math.floor(s/86400)+'d'; }

// =================================================================
// DATA LAYER
// =================================================================
async function loadClusters(){
  const r = await fetch('/api/clusters'); if (!r.ok) return;
  const list = await r.json();
  const coreEntry = list.find(c => c.slug === 'core');
  if (coreEntry){ CORE.nodes = coreEntry.nodes; CORE.label = 'Core Memory'; CORE.sub = coreEntry.sub; }
  CATS = list.filter(c => c.slug !== 'core');
  document.getElementById('navCount').textContent = CATS.length;
  layoutBubbles();
  pushToCloud3D();
}
function pushToCloud3D(){
  if (!window.Cloud3D || !CATS.length) return;
  window.Cloud3D.setBubbles(CATS.map(c => ({...c})), {...CORE, r: CORE.r || 60});
}
async function loadStats(){
  const r = await fetch('/api/stats'); if (!r.ok) return;
  const d = await r.json();
  // Quantum bar
  const cap = 200;  // arbitrary "memory quantum" cap
  const pct = Math.min(100, Math.round(d.memories / cap * 100));
  document.getElementById('qVal').textContent = pct + '.0%';
  document.getElementById('qBar').style.width = pct + '%';
  document.getElementById('qUsed').textContent = d.memories.toLocaleString() + ' used';
  document.getElementById('qCap').textContent = cap.toLocaleString() + ' cap';
}
const ACTOR_IMG = {
  'vps-claude':   '/static/gen/actor_vps.jpg',
  'local-claude': '/static/gen/actor_local.jpg',
  'chat-claude':  '/static/gen/actor_chat.jpg',
};
async function loadPresence(){
  const r = await fetch('/api/presence'); if (!r.ok) return;
  const rows = await r.json();
  const online = rows.filter(r => r.status === 'online').length;
  document.getElementById('activeAgents').textContent = online + ' Running';
  const av = rows.map(r => {
    const img = ACTOR_IMG[r.slug] || '';
    const cls = r.status;  // online | recent | idle | cold
    const lbl = r.slug.replace('-claude','').toUpperCase();
    return `<div class="avatar ${cls === 'idle' || cls === 'cold' ? 'offline' : cls}" style="background-image:url(${img})" title="${r.slug}"><span class="lbl">${lbl}</span></div>`;
  }).join('');
  document.getElementById('agentAvatars').innerHTML = av;
}
async function loadMemoryStream(){
  const r = await fetch('/api/memory_stream'); if (!r.ok) return;
  const series = await r.json();
  const total = series.reduce((s,p) => s + p.n, 0);
  document.getElementById('memStream').textContent = total.toLocaleString();
  renderSparkline('streamSpark', series);
  renderSparkline('liveSpark', series, true);
}
function renderSparkline(id, series, mini){
  const svg = document.getElementById(id);
  const w = svg.clientWidth || 200, h = svg.clientHeight || 30;
  if (!series.length){ return; }
  const max = Math.max(1, ...series.map(p => p.n));
  const pts = series.map((p, i) => {
    const x = (i / (series.length-1)) * w;
    const y = h - (p.n / max) * (h-2) - 1;
    return [x,y];
  });
  const line = 'M ' + pts.map(p => p.join(',')).join(' L ');
  const area = line + ` L ${w},${h} L 0,${h} Z`;
  const paths = svg.querySelectorAll('path');
  if (paths.length >= 2){
    paths[0].setAttribute('d', area);
    paths[1].setAttribute('d', line);
  } else if (paths.length === 1){
    paths[0].setAttribute('d', line);
  }
}
async function loadConflictsAndFusion(){
  // Context Fusion = 100% - conflict_ratio
  const [stats, conf] = await Promise.all([
    fetch('/api/stats').then(r => r.ok ? r.json() : null),
    fetch('/api/conflicts').then(r => r.ok ? r.json() : null),
  ]);
  if (!stats) return;
  const mem = stats.memories || 1;
  const cf  = stats.conflicts || 0;
  const fusion = Math.max(40, 100 - (cf / mem) * 100);
  document.getElementById('fusionPct').textContent = fusion.toFixed(1) + '%';
  document.getElementById('fusionFill').style.width = fusion + '%';
}

// MCP server status — uses presence + the fact that we're sitting next to it
async function loadMCP(){
  const start = performance.now();
  try{
    const r = await fetch('/api/stats');
    if (r.ok){
      const ms = Math.round(performance.now() - start);
      document.getElementById('mcpStatus').textContent = 'Connected';
      document.getElementById('mcpStatus').className = 'val good';
      document.getElementById('mcpLat').textContent = ms + 'ms';
    } else throw new Error();
  } catch(_){
    document.getElementById('mcpStatus').textContent = 'Disconnected';
    document.getElementById('mcpStatus').className = 'val warm';
  }
}

// Devices ribbon — derived from presence + a few fixed reference devices
const DEVICE_DEFS = [
  { slug: 'macbook-pro',     label: 'MacBook-Pro',   icon: 'mac' },
  { slug: 'vps-node-01',     label: 'VPS-Node-01',   icon: 'vps' },
  { slug: 'iphone',          label: 'iPhone',        icon: 'phone' },
  { slug: 'claude-terminal', label: 'Claude-Terminal', icon: 'term' },
  { slug: 'github-agent',    label: 'GitHub-Agent',  icon: 'gh' },
  { slug: 'web-session',     label: 'Web-Session',   icon: 'globe' },
  { slug: 'memory-worker',   label: 'Memory-Worker', icon: 'cpu' },
  { slug: 'archive-core',    label: 'Archive-Core',  icon: 'arc' },
];
const DEVICE_ICONS = {
  mac:   '<svg viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="13" rx="1"/><path d="M2 17h20l-2 3H4z"/></svg>',
  vps:   '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="1"/><rect x="3" y="14" width="18" height="6" rx="1"/><circle cx="7" cy="7" r="0.6" fill="currentColor"/><circle cx="7" cy="17" r="0.6" fill="currentColor"/></svg>',
  phone: '<svg viewBox="0 0 24 24"><rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/></svg>',
  term:  '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="1"/><path d="M7 9l3 3-3 3M12 15h5"/></svg>',
  gh:    '<svg viewBox="0 0 24 24"><path d="M12 2a10 10 0 0 0-3.2 19.5c.5.1.7-.2.7-.5v-1.8c-2.8.6-3.4-1.3-3.4-1.3-.4-1-1-1.3-1-1.3-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.5 2.3 1.1 2.9.8.1-.6.3-1.1.6-1.4-2.2-.2-4.6-1.1-4.6-5a3.9 3.9 0 0 1 1-2.7c-.1-.2-.5-1.3.1-2.7 0 0 .8-.3 2.7 1a9.4 9.4 0 0 1 4.9 0c1.9-1.3 2.7-1 2.7-1 .6 1.4.2 2.5.1 2.7a3.9 3.9 0 0 1 1 2.7c0 3.9-2.3 4.7-4.6 5 .4.3.7.9.7 1.8v2.6c0 .3.2.6.7.5A10 10 0 0 0 12 2z"/></svg>',
  globe: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></svg>',
  cpu:   '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1"/><rect x="9" y="9" width="6" height="6"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/></svg>',
  arc:   '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="5"/><rect x="3" y="8" width="18" height="13"/><path d="M10 13h4"/></svg>',
};
function devStatusFor(slug, presenceRows){
  // Map our 3 real agents to specific device slots, others "offline"
  const m = { 'vps-claude':'vps-node-01', 'local-claude':'macbook-pro', 'chat-claude':'web-session' };
  for (const r of presenceRows){
    if (m[r.slug] === slug){
      if (r.status === 'online') return 'online';
      if (r.status === 'recent') return 'processing';
      if (r.status === 'idle') return 'away';
    }
  }
  return 'offline';
}
async function renderDevices(){
  const r = await fetch('/api/presence'); if (!r.ok) return;
  const rows = await r.json();
  const el = document.getElementById('devs');
  el.innerHTML = DEVICE_DEFS.map(d => {
    const st = devStatusFor(d.slug, rows);
    return `<div class="dev ${st}" data-slug="${d.slug}">
      <div class="ico">${DEVICE_ICONS[d.icon] || DEVICE_ICONS.cpu}</div>
      <div class="lbl">${d.label}</div>
      <div class="pulse"></div>
    </div>`;
  }).join('') + `<button class="dev-add"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>Add Device</button>`;
}

// Universe Status text + chip
function setStatus(text){ document.getElementById('usStatus').textContent = text; }

// Universe Time
function tickClock(){
  const d = new Date();
  const hh = d.getHours() % 12 || 12;
  const mm = String(d.getMinutes()).padStart(2,'0');
  const ss = String(d.getSeconds()).padStart(2,'0');
  const ap = d.getHours() >= 12 ? 'PM' : 'AM';
  document.getElementById('uTime').textContent = `${hh}:${mm}:${ss} ${ap}`;
  document.getElementById('uDate').textContent = d.toLocaleDateString('en-CA', { year: 'numeric', month: 'long', day: 'numeric' });
}
setInterval(tickClock, 1000); tickClock();

// =================================================================
// SSE — push live signals
// =================================================================
function connectSSE(){
  const es = new EventSource('/events');
  es.addEventListener('connected', () => setStatus('Everything Synchronized'));
  es.addEventListener('activity', async (e) => {
    let data = {};
    try { data = JSON.parse(e.data); } catch(_){}
    loadStats(); loadPresence(); renderDevices(); loadMemoryStream(); loadClusters();
    setStatus('Live Activity');
    clearTimeout(window.__statusT);
    window.__statusT = setTimeout(() => setStatus('Everything Synchronized'), 4000);
    Audio?.chord && Audio.chord();
    emitShockwave();
    if (data.kind === 'memories' && data.id){
      try {
        const r = await fetch('/api/memory/' + data.id);
        if (r.ok){
          const m = await r.json();
          spawnMemoryFlyers(m);
        }
      } catch(_){}
    }
  });
  es.onerror = () => { es.close(); setStatus('Reconnecting'); setTimeout(connectSSE, 3000); };
}

// =================================================================
// Audio  —  ambient deep-space pad + interaction cues.
// Off by default (browsers block autoplay). Click the speaker icon
// to enable. Then continuous low hum + ping on each memory event.
// =================================================================
const Audio = (() => {
  let actx = null;
  let masterGain = null;
  let padNodes = [];
  let on = false;
  const btn = document.getElementById('btnAudio');
  if (btn) btn.addEventListener('click', () => toggle());

  function init(){
    actx = new (window.AudioContext || window.webkitAudioContext)();
    masterGain = actx.createGain();
    masterGain.gain.value = 0;
    masterGain.connect(actx.destination);
    // Pad: stacked sine oscillators in a chord
    const FREQS = [60, 90, 135, 180.5, 270.7];
    const gains = [0.10, 0.07, 0.05, 0.035, 0.025];
    const lp = actx.createBiquadFilter();
    lp.type = 'lowpass'; lp.frequency.value = 600; lp.Q.value = 0.7;
    lp.connect(masterGain);
    for (let i = 0; i < FREQS.length; i++){
      const o = actx.createOscillator();
      o.type = i === 0 ? 'triangle' : 'sine';
      o.frequency.value = FREQS[i];
      const g = actx.createGain();
      g.gain.value = gains[i];
      // gentle LFO on each gain
      const lfo = actx.createOscillator();
      lfo.frequency.value = 0.07 + i * 0.02;
      const lfoG = actx.createGain();
      lfoG.gain.value = gains[i] * 0.4;
      lfo.connect(lfoG); lfoG.connect(g.gain);
      lfo.start();
      o.connect(g); g.connect(lp);
      o.start();
      padNodes.push(o, lfo);
    }
    // LFO on filter cutoff for sweep
    const filterLfo = actx.createOscillator();
    filterLfo.frequency.value = 0.04;
    const filterLfoGain = actx.createGain();
    filterLfoGain.gain.value = 200;
    filterLfo.connect(filterLfoGain); filterLfoGain.connect(lp.frequency);
    filterLfo.start();
    padNodes.push(filterLfo);
  }

  function toggle(){
    if (!actx) init();
    on = !on;
    const target = on ? 0.32 : 0;
    masterGain.gain.cancelScheduledValues(actx.currentTime);
    masterGain.gain.linearRampToValueAtTime(target, actx.currentTime + 1.0);
    btn?.classList.toggle('audio-on', on);
  }

  function shimmer(freq = 1200){
    if (!actx || !on) return;
    const o = actx.createOscillator();
    o.type = 'sine'; o.frequency.value = freq;
    const g = actx.createGain();
    g.gain.value = 0;
    g.gain.linearRampToValueAtTime(0.06, actx.currentTime + 0.02);
    g.gain.linearRampToValueAtTime(0, actx.currentTime + 0.6);
    o.connect(g); g.connect(masterGain);
    o.start(); o.stop(actx.currentTime + 0.65);
  }

  function chord(){
    if (!actx || !on) return;
    [880, 1108, 1318, 1760].forEach((f, i) => {
      setTimeout(() => shimmer(f + (Math.random()-0.5)*8), i * 50);
    });
  }

  function whoosh(){
    if (!actx || !on) return;
    // Noise burst with sweeping bandpass
    const dur = 0.55;
    const bufferSize = actx.sampleRate * dur;
    const buf = actx.createBuffer(1, bufferSize, actx.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < bufferSize; i++) data[i] = (Math.random()*2-1) * 0.5;
    const src = actx.createBufferSource(); src.buffer = buf;
    const bp = actx.createBiquadFilter();
    bp.type = 'bandpass'; bp.Q.value = 4;
    bp.frequency.setValueAtTime(180, actx.currentTime);
    bp.frequency.exponentialRampToValueAtTime(2400, actx.currentTime + dur*0.6);
    bp.frequency.exponentialRampToValueAtTime(180, actx.currentTime + dur);
    const g = actx.createGain();
    g.gain.setValueAtTime(0.0, actx.currentTime);
    g.gain.linearRampToValueAtTime(0.18, actx.currentTime + 0.05);
    g.gain.linearRampToValueAtTime(0.0, actx.currentTime + dur);
    src.connect(bp); bp.connect(g); g.connect(masterGain);
    src.start(); src.stop(actx.currentTime + dur);
  }

  // Per-scene low-pass filter sweep — gives interior vs exterior tone
  let filterRef = null;
  function captureFilter(){
    if (filterRef) return filterRef;
    // The low-pass is the first node in the chain after master.  Re-grab.
    // We stored it locally in init() via the chain; for simplicity expose
    // a small ramp.  If unavailable, fallback to no-op.
    return null;
  }
  function filterSweep(/*from*/ _f, /*to*/ _t){
    // Best-effort — the existing pad's filter is created inside init().
    // We can ramp masterGain volume slightly to simulate the interior
    // muffle without rewiring the chain.
    if (!actx || !on) return;
    const g = masterGain.gain;
    const now = actx.currentTime;
    g.cancelScheduledValues(now);
    g.linearRampToValueAtTime(_t < _f ? 0.20 : 0.32, now + 0.6);
  }
  return { toggle, shimmer, chord, whoosh, filterSweep, get on(){return on} };
})();
window.__zai_audio = Audio;

// =================================================================
// Main loop
// =================================================================
let lastT = performance.now();
function frame(now){
  const dt = Math.min(now - lastT, 48);
  lastT = now;
  drawDust(dt, now);
  drawStars(dt, now);
  if (CATS.length){
    tickConstellation(dt);
    seedFlow(now);
    drawCloud(now, dt);
    drawShockwaves(dt);
    drawFlyers(dt);
    drawTrail(now);
    drawSubBubbles(now);
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// Expose for debug + headless tests
window.__zai = { get CATS(){ return CATS; }, get CORE(){ return CORE; }, openZoom };

// =================================================================
// Constellation rotation + mouse parallax + sonar pulses
// =================================================================
const CLOUD_MOTION = {
  angle: 0,
  angVel: 0.000014,   // ~7.5 min/rev — even slower, elegant not dizzying
  parallaxX: 0,
  parallaxY: 0,
  parallaxTargetX: 0,
  parallaxTargetY: 0,
  shockwaves: [],
};

window.addEventListener('mousemove', (e) => {
  const cx = window.innerWidth/2, cy = window.innerHeight/2;
  const px = (e.clientX - cx) / cx;   // -1..1
  const py = (e.clientY - cy) / cy;
  CLOUD_MOTION.parallaxTargetX = px * 18;   // up to ±18px
  CLOUD_MOTION.parallaxTargetY = -py * 14;
});

function tickConstellation(dt){
  if (!CATS.length) return;
  CLOUD_MOTION.angle += dt * CLOUD_MOTION.angVel;
  // Parallax easing
  CLOUD_MOTION.parallaxX += (CLOUD_MOTION.parallaxTargetX - CLOUD_MOTION.parallaxX) * 0.05;
  CLOUD_MOTION.parallaxY += (CLOUD_MOTION.parallaxTargetY - CLOUD_MOTION.parallaxY) * 0.05;
  const R = Math.min(STAGE.w, STAGE.h) * ORBIT_R_BASE;
  CATS.forEach((c) => {
    const ang = c.ang + CLOUD_MOTION.angle;
    c.x = CORE.x + Math.cos(ang) * R + CLOUD_MOTION.parallaxX;
    c.y = CORE.y + Math.sin(ang) * R + CLOUD_MOTION.parallaxY;
  });
  CORE.dispX = CORE.x + CLOUD_MOTION.parallaxX;
  CORE.dispY = CORE.y + CLOUD_MOTION.parallaxY;
  if (window.Cloud3D && window.Cloud3D.updatePositions){
    window.Cloud3D.updatePositions(CATS, { ...CORE, x: CORE.dispX, y: CORE.dispY, id: CORE.id });
    // Rebuild arcs every ~250 ms so the cross-chord pattern stays
    // glued to the rotating bubbles instead of snapping every few seconds.
    if (!CLOUD_MOTION._lastArc || performance.now() - CLOUD_MOTION._lastArc > 250){
      CLOUD_MOTION._lastArc = performance.now();
      window.Cloud3D.rebuildArcsFor(CATS, { ...CORE, x: CORE.dispX, y: CORE.dispY });
    }
  }
}

function emitShockwave(){
  CLOUD_MOTION.shockwaves.push({ t: 0, life: 1500 });
}

function drawShockwaves(dt){
  const g = ctx.cloud;
  for (let i = CLOUD_MOTION.shockwaves.length-1; i >= 0; i--){
    const sw = CLOUD_MOTION.shockwaves[i];
    sw.t += dt;
    const p = sw.t / sw.life;
    if (p >= 1){ CLOUD_MOTION.shockwaves.splice(i,1); continue; }
    const R = Math.min(STAGE.w, STAGE.h) * ORBIT_R_BASE;
    const r = R * (0.2 + p * 1.4);
    const alpha = (1 - p) * 0.55;
    const cx = CORE.dispX || CORE.x, cy = CORE.dispY || CORE.y;
    // outer
    g.strokeStyle = `rgba(255,80,60,${alpha*0.7})`;
    g.lineWidth = 2;
    g.beginPath(); g.arc(cx, cy, r, 0, Math.PI*2); g.stroke();
    // inner bright
    g.strokeStyle = `rgba(255,255,255,${alpha*0.45})`;
    g.lineWidth = 0.8;
    g.beginPath(); g.arc(cx, cy, r-2, 0, Math.PI*2); g.stroke();
  }
}

// =================================================================
// EXPAND + 3D VIEW + CONTEXTUAL DEVICES + NARRATE + AUTOFLY
// (the cinematic interaction layer the user asked for)
// =================================================================

// ----- Expand to fullscreen
const ExpandMode = { active: false };
function toggleExpand(){
  ExpandMode.active = !ExpandMode.active;
  document.body.classList.toggle('expanded', ExpandMode.active);
  const btn = document.getElementById('btnFs');
  if (btn) btn.classList.toggle('btn-active', ExpandMode.active);
  Audio?.shimmer && Audio.shimmer(ExpandMode.active ? 600 : 900);
}
document.getElementById('btnFs')?.addEventListener('click', toggleExpand);
document.addEventListener('keydown', (e) => {
  if ((e.key === 'f' || e.key === 'F') && !e.metaKey && !e.ctrlKey){
    const tag = (document.activeElement?.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') return;
    toggleExpand();
  }
});

// ----- 3D view toggle
const View3D = { active: false, tilt: 0, yaw: 0 };
function toggleView3D(){
  View3D.active = !View3D.active;
  document.body.classList.toggle('three-d', View3D.active);
  const btn = document.getElementById('btn3d');
  if (btn) btn.classList.toggle('btn-active', View3D.active);
  if (window.Cloud3D?.setView3D){
    window.Cloud3D.setView3D(View3D.active);
  }
  Audio?.shimmer && Audio.shimmer(View3D.active ? 1200 : 800);
}
document.getElementById('btn3d')?.addEventListener('click', toggleView3D);

// In 3D mode, mouse drag rotates the constellation tilt/yaw
let _drag3d = null;
window.addEventListener('mousedown', (e) => {
  if (!View3D.active) return;
  if (e.target.closest('.app > .topbar, .app > .devstrip, .rail-l, .rail-r, .drawer, .zoomshell')) return;
  _drag3d = { x: e.clientX, y: e.clientY, startTilt: View3D.tilt, startYaw: View3D.yaw };
});
window.addEventListener('mousemove', (e) => {
  if (!_drag3d) return;
  const dx = e.clientX - _drag3d.x;
  const dy = e.clientY - _drag3d.y;
  View3D.yaw  = _drag3d.startYaw  + dx * 0.003;
  View3D.tilt = Math.max(-0.55, Math.min(0.55, _drag3d.startTilt + dy * 0.003));
  if (window.Cloud3D?.setView3DAngles) window.Cloud3D.setView3DAngles(View3D.tilt, View3D.yaw);
});
window.addEventListener('mouseup', () => { _drag3d = null; });

// ----- Contextual device strip — devices filter by hovered/selected category
const CAT_TO_DEVICES = {
  'core':     ['macbook-pro','vps-node-01','iphone','claude-terminal','github-agent','web-session','memory-worker','archive-core'],
  'agents':   ['vps-node-01','macbook-pro','web-session','claude-terminal'],
  'terminal': ['vps-node-01','claude-terminal'],
  'coding':   ['macbook-pro','claude-terminal'],
  'web':      ['web-session'],
  'mobile':   ['iphone'],
  'github':   ['github-agent','vps-node-01'],
  'planning': ['vps-node-01','memory-worker'],
  'longterm': ['archive-core','memory-worker','vps-node-01'],
};
let _devContext = null;
function setDevContext(slug){
  if (slug === _devContext) return;
  _devContext = slug;
  const wanted = new Set(CAT_TO_DEVICES[slug] || CAT_TO_DEVICES.core);
  document.querySelectorAll('.dev').forEach(el => {
    const isWanted = wanted.has(el.dataset.slug);
    el.style.opacity = isWanted ? '1' : '0.18';
    el.style.transform = isWanted ? 'scale(1)' : 'scale(0.9)';
    el.style.transition = 'opacity .35s ease, transform .35s ease';
  });
}

// ----- Narrate mode after idle
const Narrate = { lastInput: performance.now(), active: false, step: 0, lastTick: 0 };
function noteInput(){ Narrate.lastInput = performance.now(); if (Narrate.active) exitNarrate(); }
['mousemove','mousedown','keydown','touchstart','wheel'].forEach(ev => window.addEventListener(ev, noteInput, {passive:true}));

function enterNarrate(){ Narrate.active = true; Narrate.step = -1; advanceNarrate(); }
function exitNarrate(){
  Narrate.active = false;
  document.getElementById('narrateBanner').classList.remove('show');
  setDevContext('core');
  if (window.Cloud3D?.cameraReset) window.Cloud3D.cameraReset(1.2);
}
function advanceNarrate(){
  if (!Narrate.active) return;
  Narrate.step = (Narrate.step + 1) % CATS.length;
  const cat = CATS[Narrate.step];
  if (!cat) return;
  document.getElementById('narrateText').textContent = cat.label;
  document.getElementById('narrateSub').textContent =
    `${(cat.nodes||0).toLocaleString()} nodes · ${cat.sub || ''}`;
  document.getElementById('narrateBanner').classList.add('show');
  setDevContext(cat.slug);
  if (window.Cloud3D?.cameraZoomTo){
    window.Cloud3D.cameraZoomTo({ x: cat.x, y: cat.y }, 1.2);
    window.Cloud3D.setSelected(cat.id || cat.slug);
  }
  Audio?.shimmer && Audio.shimmer(900 + Math.random()*200);
}
setInterval(() => {
  const idleFor = performance.now() - Narrate.lastInput;
  if (!Narrate.active && idleFor > 30000) enterNarrate();
  if (Narrate.active && performance.now() - Narrate.lastTick > 6000){
    Narrate.lastTick = performance.now();
    advanceNarrate();
  }
}, 1000);

// Hovering a bubble also sets devContext briefly
let _hoverDebounce = null;
function syncHoverContext(slug){
  clearTimeout(_hoverDebounce);
  _hoverDebounce = setTimeout(() => setDevContext(slug || 'core'), 150);
}

// ----- Memory autofly: glow particle travels from author actor → entity bubbles
const flyers = [];
function spawnMemoryFlyers(memory){
  // We have CATS but the author is an actor-entity, not in CATS. So the flyer
  // starts at the AGENTS bubble (closest visual proxy) or at the on-screen
  // actor portrait. We'll start at AGENTS bubble for clarity.
  const agentsCat = CATS.find(c => c.slug === 'agents');
  const start = agentsCat ? { x: agentsCat.x, y: agentsCat.y, color: agentsCat.color }
                          : { x: CORE.x, y: CORE.y, color: '#dc2626' };
  const targets = (memory.entity_slugs || []).map(slug => {
    // Map entity slug to cluster slug heuristically
    if (slug === 'zai-memory-hub') return CATS.find(c => c.slug === 'longterm');
    if (slug === 'anteroom-studio') return CATS.find(c => c.slug === 'github');
    if (slug === 'zawwar') return CORE;
    return CORE;
  }).filter(Boolean);
  if (!targets.length) targets.push(CORE);
  for (const t of targets){
    flyers.push({ x0: start.x, y0: start.y, x1: t.x, y1: t.y, color: start.color, t: 0, life: 1600 });
  }
}
function drawFlyers(dt){
  const g = ctx.cloud;
  for (let i = flyers.length-1; i >= 0; i--){
    const f = flyers[i];
    f.t += dt;
    const p = Math.min(1, f.t / f.life);
    if (p >= 1){ flyers.splice(i,1); continue; }
    const e = 1 - Math.pow(1 - p, 2);
    const x = f.x0 + (f.x1 - f.x0) * e;
    const y = f.y0 + (f.y1 - f.y0) * e;
    const alpha = p < 0.1 ? p*10 : (p > 0.85 ? (1-p)*7 : 1);
    const colA = hexToRgbStr(f.color);
    // soft halo
    const hg = g.createRadialGradient(x, y, 0, x, y, 28);
    hg.addColorStop(0, `rgba(255,255,255,${0.9*alpha})`);
    hg.addColorStop(0.35, `rgba(${colA},${0.7*alpha})`);
    hg.addColorStop(1, `rgba(${colA},0)`);
    g.fillStyle = hg;
    g.beginPath(); g.arc(x, y, 28, 0, Math.PI*2); g.fill();
    // bright core
    g.fillStyle = `rgba(255,255,255,${alpha})`;
    g.beginPath(); g.arc(x, y, 2.4, 0, Math.PI*2); g.fill();
  }
}

// =================================================================
// REPLAY MODE — play the last N memories as a cinematic timeline
// Each event: dolly the camera to its referenced bubble, fire the
// flyer animation, show caption.  ESC or any click exits.
// =================================================================
const Replay = { running: false, items: [], i: 0, timer: null };

function ENTITY_TO_CAT(slug){
  const m = { 'zai-memory-hub': 'longterm', 'anteroom-studio': 'github', 'zawwar': 'core' };
  const cs = m[slug];
  if (cs === 'core') return CORE;
  return CATS.find(c => c.slug === cs);
}

async function replayStart(){
  if (Replay.running) return replayStop();
  Replay.running = true;
  document.getElementById('replayBtn').classList.add('active');
  document.getElementById('replayOverlay').classList.add('show');
  document.getElementById('replayProgress').classList.add('show');
  try {
    const r = await fetch('/api/recent?n=30');
    const items = (await r.json()).reverse();   // oldest → newest
    Replay.items = items;
    Replay.i = 0;
    replayStep();
  } catch (e) {
    replayStop();
  }
}
function replayStop(){
  Replay.running = false;
  document.getElementById('replayBtn').classList.remove('active');
  document.getElementById('replayOverlay').classList.remove('show');
  document.getElementById('replayProgress').classList.remove('show');
  document.getElementById('replayBar').style.width = '0%';
  if (Replay.timer) clearTimeout(Replay.timer);
  // Reset camera if we dolly'd
  if (window.Cloud3D?.cameraReset) window.Cloud3D.cameraReset();
}
function replayStep(){
  if (!Replay.running) return;
  if (Replay.i >= Replay.items.length){ replayStop(); return; }
  const m = Replay.items[Replay.i];
  const total = Replay.items.length;
  document.getElementById('replayHeadText').textContent =
    `Replaying memory ${Replay.i+1} of ${total}`;
  document.getElementById('replayCaption').textContent =
    (m.content || '').slice(0, 200) + ((m.content || '').length > 200 ? '…' : '');
  document.getElementById('replayMeta').textContent =
    `${(m.written_by || '').replace('-claude','').toUpperCase()} · IMP ${m.importance || 3} · ${new Date(m.created_at).toLocaleString('en-CA',{hour12:false,month:'short',day:'numeric'})}`;
  document.getElementById('replayBar').style.width =
    ((Replay.i + 1) / total * 100) + '%';
  // Dolly to a relevant cluster (first entity_slug → cluster, else CORE)
  const cat = (m.entity_slugs || []).map(ENTITY_TO_CAT).find(Boolean) || CORE;
  if (window.Cloud3D?.cameraZoomTo) window.Cloud3D.cameraZoomTo({ x: cat.x, y: cat.y }, 1.4);
  // Fire flyers from agents cluster (proxy for author) to each ref'd entity
  if (typeof spawnMemoryFlyers === 'function') spawnMemoryFlyers(m);
  // Sonar pulse
  if (typeof emitShockwave === 'function') emitShockwave();
  Audio?.chord && Audio.chord();
  // Next
  Replay.timer = setTimeout(() => { Replay.i++; replayStep(); }, 2200);
}
document.getElementById('replayBtn')?.addEventListener('click', replayStart);
document.addEventListener('keydown', (e) => {
  if (Replay.running && e.key === 'Escape') replayStop();
});

// =================================================================
// SHARE URLS + BOOKMARKS  —  ?focus=<slug> auto-opens that bubble.
// Bookmarks live in localStorage as an array of { slug, t }.
// =================================================================
const BOOKMARKS_KEY = 'zai_hub_bookmarks_v1';

function loadBookmarks(){
  try { return JSON.parse(localStorage.getItem(BOOKMARKS_KEY) || '[]'); }
  catch(_) { return []; }
}
function saveBookmarks(arr){
  try { localStorage.setItem(BOOKMARKS_KEY, JSON.stringify(arr)); }
  catch(_) {}
}
function toggleBookmark(slug){
  const arr = loadBookmarks();
  const idx = arr.findIndex(b => b.slug === slug);
  if (idx >= 0) arr.splice(idx, 1);
  else arr.unshift({ slug, t: Date.now() });
  saveBookmarks(arr);
  return idx < 0;
}
function isBookmarked(slug){
  return loadBookmarks().some(b => b.slug === slug);
}
function applyShareURL(){
  const params = new URLSearchParams(location.search);
  const focus = params.get('focus');
  if (!focus) return;
  // Wait for CATS to load
  const tryOpen = () => {
    if (!CATS.length){ setTimeout(tryOpen, 200); return; }
    const target = focus === 'core' ? CORE : CATS.find(c => c.slug === focus);
    if (target) openZoom(target);
  };
  setTimeout(tryOpen, 1200);
}
setTimeout(applyShareURL, 100);

function copyShareLink(slug){
  const url = `${location.origin}${location.pathname}?focus=${encodeURIComponent(slug)}`;
  navigator.clipboard?.writeText(url).then(() => {
    toast('Share link copied');
  }).catch(() => {
    toast('Copy failed — ' + url);
  });
}

// Minimal toast
function toast(msg){
  let el = document.getElementById('toast');
  if (!el){
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(window.__toastT);
  window.__toastT = setTimeout(() => el.classList.remove('show'), 2400);
}

// =================================================================
// MINI-MAP NAVIGATOR — 1:30 scale of the bubble cloud, top-right
// =================================================================
function setupMiniMap(){
  const cv = document.getElementById('mmcv'); if (!cv) return;
  const g = cv.getContext('2d');
  function resize(){
    const rect = cv.parentNode.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = Math.floor((rect.width - 16) * dpr);
    cv.height = Math.floor((rect.height - 20) * dpr);
    cv.style.width = (rect.width - 16) + 'px';
    cv.style.height = (rect.height - 20) + 'px';
    g.setTransform(dpr,0,0,dpr,0,0);
  }
  resize();
  window.addEventListener('resize', resize);
  function draw(){
    const w = cv.width / Math.min(window.devicePixelRatio || 1, 2);
    const h = cv.height / Math.min(window.devicePixelRatio || 1, 2);
    g.clearRect(0, 0, w, h);
    // background hint
    g.fillStyle = 'rgba(220,38,38,0.04)';
    g.fillRect(0, 0, w, h);
    // crosshair
    g.strokeStyle = 'rgba(232,212,154,0.15)';
    g.lineWidth = 0.5;
    g.beginPath(); g.moveTo(w/2, 0); g.lineTo(w/2, h); g.moveTo(0, h/2); g.lineTo(w, h/2); g.stroke();
    if (!CATS.length) return;
    const cx = w/2, cy = h/2;
    const R = Math.min(w, h) * 0.38;
    // CORE
    const isCoreSel = SubBubbles.focusCat === CORE;
    g.fillStyle = isCoreSel ? '#ff3a3a' : '#dc2626';
    g.beginPath(); g.arc(cx, cy, isCoreSel ? 6 : 4, 0, Math.PI*2); g.fill();
    g.fillStyle = 'rgba(245,236,219,0.9)';
    g.font = "7px 'JetBrains Mono', monospace";
    g.textAlign = 'center'; g.textBaseline = 'middle';
    // Cats
    CATS.forEach((c, i) => {
      const ang = c.ang;
      const x = cx + Math.cos(ang) * R;
      const y = cy + Math.sin(ang) * R;
      const isSel = SubBubbles.focusCat === c;
      const r = isSel ? 5 : 3;
      const col = hexToRgbStr(c.color);
      g.fillStyle = `rgba(${col},${isSel ? 1 : 0.85})`;
      g.beginPath(); g.arc(x, y, r, 0, Math.PI*2); g.fill();
      if (isSel){
        g.strokeStyle = `rgba(${col},0.6)`;
        g.lineWidth = 1;
        g.beginPath(); g.arc(x, y, r+3, 0, Math.PI*2); g.stroke();
      }
      // spoke
      g.strokeStyle = `rgba(${col},${isSel ? 0.6 : 0.15})`;
      g.lineWidth = 0.5;
      g.beginPath(); g.moveTo(cx, cy); g.lineTo(x, y); g.stroke();
    });
  }
  function tick(){
    draw();
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  // Click to teleport
  cv.addEventListener('click', (e) => {
    const rect = cv.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    const w = rect.width, h = rect.height;
    const cx = w/2, cy = h/2;
    const R = Math.min(w, h) * 0.38;
    // Hit test CORE first
    if (Math.hypot(x - cx, y - cy) < 9){ openZoom(CORE); return; }
    for (const c of CATS){
      const bx = cx + Math.cos(c.ang) * R;
      const by = cy + Math.sin(c.ang) * R;
      if (Math.hypot(x - bx, y - by) < 9){ openZoom(c); return; }
    }
  });
}
setTimeout(setupMiniMap, 600);

// =================================================================
// SESSION TRAIL — soft glowing line between bubbles you've visited
// =================================================================
const SessionTrail = [];   // {slug, t, isCore}
function pushTrail(cat){
  const slug = cat === CORE ? 'core' : cat.slug;
  if (SessionTrail.length && SessionTrail[SessionTrail.length-1].slug === slug) return;
  SessionTrail.push({ slug, t: performance.now() });
  if (SessionTrail.length > 16) SessionTrail.shift();
}
function drawTrail(now){
  if (SessionTrail.length < 2) return;
  const g = ctx.cloud;
  // Resolve each entry to current bubble position
  const pts = SessionTrail.map(en => {
    const cat = en.slug === 'core' ? CORE : CATS.find(c => c.slug === en.slug);
    if (!cat) return null;
    if (window.Cloud3D?.getBubbleScreenPos){
      const sp = window.Cloud3D.getBubbleScreenPos(cat.id);
      if (sp) return { x: sp.x, y: sp.y, t: en.t };
    }
    return { x: cat.x, y: cat.y, t: en.t };
  }).filter(Boolean);
  if (pts.length < 2) return;
  // Draw each segment with age-based alpha decay
  for (let i = 1; i < pts.length; i++){
    const age = now - pts[i].t;
    const a = Math.max(0, 1 - age / 60000) * 0.45;   // fade over 60s
    if (a <= 0) continue;
    g.strokeStyle = `rgba(232,212,154,${a})`;
    g.lineWidth = 0.8;
    g.beginPath();
    g.moveTo(pts[i-1].x, pts[i-1].y);
    g.lineTo(pts[i].x, pts[i].y);
    g.stroke();
    // breadcrumb dot
    g.fillStyle = `rgba(232,212,154,${a*1.4})`;
    g.beginPath(); g.arc(pts[i].x, pts[i].y, 2, 0, Math.PI*2); g.fill();
  }
}

// =================================================================
// SCENE DOCK — slide indicator between Home / Universe / Search
// =================================================================
function setupSceneDock(){
  const dock = document.getElementById('sceneDock'); if (!dock) return;
  const ind = document.getElementById('sceneInd');
  function moveIndicator(target){
    if (!target || !ind) return;
    const dockRect = dock.getBoundingClientRect();
    const tRect = target.getBoundingClientRect();
    ind.style.left = (tRect.left - dockRect.left) + 'px';
    ind.style.width = tRect.width + 'px';
  }
  dock.querySelectorAll('.scene').forEach(el => {
    el.addEventListener('click', (e) => {
      const scene = el.dataset.scene;
      if (scene === 'palette'){ e.preventDefault(); paletteOpen(); return; }
      if (scene === 'home' && el.href === '' || scene === 'home'){
        e.preventDefault();
        // Just scroll to top / close any modals
        closeZoom();
        return;
      }
      // 'universe' uses real href
      dock.querySelectorAll('.scene').forEach(s => s.classList.remove('active'));
      el.classList.add('active');
      moveIndicator(el);
    });
    el.addEventListener('mouseenter', () => moveIndicator(el));
  });
  dock.addEventListener('mouseleave', () => {
    const active = dock.querySelector('.scene.active');
    moveIndicator(active);
  });
  // Initial position
  setTimeout(() => moveIndicator(dock.querySelector('.scene.active')), 100);
}
setTimeout(setupSceneDock, 300);

// =================================================================
// Command Palette (⌘K / Ctrl-K)  —  fuzzy spotlight
// Searches memories + decisions + categories.  Selecting an item
// teleports the user there (drawer for memory/decision, openZoom
// for category).
// =================================================================
const Palette = {
  open: false,
  items: [],      // unified result set
  filtered: [],
  active: 0,
  memories: [], decisions: [], cats: [],
  lastLoadT: 0,
};
async function paletteLoad(){
  // Cache for 30s
  if (performance.now() - Palette.lastLoadT < 30000 && Palette.memories.length) return;
  Palette.lastLoadT = performance.now();
  try {
    const [m, d, c] = await Promise.all([
      fetch('/api/recent?n=80').then(r => r.json()).catch(() => []),
      fetch('/api/decisions?n=40').then(r => r.json()).catch(() => []),
      fetch('/api/clusters').then(r => r.json()).catch(() => []),
    ]);
    Palette.memories = m || []; Palette.decisions = d || []; Palette.cats = c || [];
  } catch(_){}
}
function paletteCompile(query){
  const q = (query || '').toLowerCase().trim();
  const items = [];
  for (const c of Palette.cats){
    const hay = `${c.label} ${c.sub} ${c.slug}`.toLowerCase();
    if (!q || hay.includes(q)) items.push({ kind:'cat', title: c.label, meta: (c.nodes||0)+' nodes', slug: c.slug });
  }
  for (const m of Palette.memories){
    const hay = `${m.content || ''} ${(m.tags || []).join(' ')} ${m.written_by || ''}`.toLowerCase();
    if (!q || hay.includes(q)) items.push({
      kind:'mem', title: (m.content || '').slice(0, 110),
      meta: (m.written_by || '').replace('-claude','') + ' · imp ' + (m.importance || 3),
      id: m.id,
    });
  }
  for (const d of Palette.decisions){
    const hay = `${d.summary || ''} ${d.rationale || ''}`.toLowerCase();
    if (!q || hay.includes(q)) items.push({
      kind:'dec', title: d.summary || '', meta: (d.written_by || '').replace('-claude',''),
      id: d.id, summary: d.summary, rationale: d.rationale,
    });
  }
  return items.slice(0, 60);
}
function paletteRender(){
  const list = document.getElementById('palList');
  list.innerHTML = Palette.filtered.map((it, i) => `
    <div class="palItem k-${it.kind} ${i === Palette.active ? 'active' : ''}" data-idx="${i}">
      <span class="kind">${it.kind === 'cat' ? 'category' : it.kind === 'mem' ? 'memory' : 'decision'}</span>
      <span class="title">${escapeHtml(it.title)}</span>
      <span class="meta">${escapeHtml(it.meta)}</span>
    </div>
  `).join('') || '<div class="palItem"><span class="title" style="color:var(--fg-dim)">no matches</span></div>';
  document.getElementById('palCount').textContent = Palette.filtered.length + ' results';
  // click
  list.querySelectorAll('.palItem').forEach(el => {
    el.addEventListener('click', () => paletteSelect(Number(el.dataset.idx)));
  });
}
function paletteSelect(idx){
  const it = Palette.filtered[idx];
  if (!it) return;
  paletteClose();
  if (it.kind === 'cat'){
    const cat = it.slug === 'core' ? CORE : CATS.find(c => c.slug === it.slug);
    if (cat) openZoom(cat);
  } else if (it.kind === 'mem'){
    openMemoryDrawer(it.id);
  } else if (it.kind === 'dec'){
    openDecisionDrawer(it);
  }
}
function paletteOpen(){
  if (Palette.open) return;
  Palette.open = true;
  document.getElementById('palette').classList.add('show');
  document.getElementById('palette').setAttribute('aria-hidden','false');
  paletteLoad().then(() => {
    Palette.filtered = paletteCompile('');
    Palette.active = 0;
    paletteRender();
  });
  const input = document.getElementById('palInput');
  input.value = '';
  setTimeout(() => input.focus(), 50);
  Audio?.shimmer && Audio.shimmer(1100);
}
function paletteClose(){
  Palette.open = false;
  document.getElementById('palette').classList.remove('show');
  document.getElementById('palette').setAttribute('aria-hidden','true');
}
document.getElementById('palInput')?.addEventListener('input', (e) => {
  Palette.filtered = paletteCompile(e.target.value);
  Palette.active = 0;
  paletteRender();
});
document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k'){
    e.preventDefault();
    if (Palette.open) paletteClose(); else paletteOpen();
    return;
  }
  if (!Palette.open) return;
  if (e.key === 'Escape'){ paletteClose(); return; }
  if (e.key === 'ArrowDown'){
    e.preventDefault();
    Palette.active = Math.min(Palette.filtered.length - 1, Palette.active + 1);
    paletteRender();
    document.querySelector('#palList .palItem.active')?.scrollIntoView({ block: 'nearest' });
  }
  if (e.key === 'ArrowUp'){
    e.preventDefault();
    Palette.active = Math.max(0, Palette.active - 1);
    paletteRender();
    document.querySelector('#palList .palItem.active')?.scrollIntoView({ block: 'nearest' });
  }
  if (e.key === 'Enter'){
    e.preventDefault();
    paletteSelect(Palette.active);
  }
});
document.getElementById('palette')?.addEventListener('click', (e) => {
  if (e.target.id === 'palette') paletteClose();
});

async function openDecisionDrawer(d){
  const dr = document.getElementById('drawer');
  const body = document.getElementById('drawerBody');
  body.innerHTML = `
    <div class="kind">Decision</div>
    <h2>${escapeHtml(d.summary || '')}</h2>
    <div class="meta">
      <span class="k">by</span><span class="v">${escapeHtml(d.meta || '')}</span>
      <span class="k">id</span><span class="v">${escapeHtml(d.id)}</span>
    </div>
    <div class="body">${escapeHtml(d.rationale || '')}</div>
  `;
  dr.classList.add('open'); dr.setAttribute('aria-hidden','false');
}

// =================================================================
// Breadcrumb — shows journey position, click any segment to jump
// =================================================================
function updateBreadcrumb(segments){
  let el = document.getElementById('breadcrumb');
  if (!el){
    el = document.createElement('div');
    el.id = 'breadcrumb';
    document.body.appendChild(el);
  }
  el.innerHTML = '';
  segments.forEach((seg, i) => {
    if (i > 0){
      const sep = document.createElement('span');
      sep.className = 'crumb-sep';
      sep.textContent = '›';
      el.appendChild(sep);
    }
    const item = document.createElement(seg.cb ? 'a' : 'span');
    item.className = 'crumb' + (seg.cb ? '' : ' final');
    item.textContent = seg.label;
    if (seg.cb){ item.addEventListener('click', seg.cb); item.href = 'javascript:void(0)'; }
    el.appendChild(item);
  });
  // Tools when inside a bubble: list view · bookmark · share
  if (segments.length > 1 && SubBubbles.focusCat){
    const tools = document.createElement('div');
    tools.className = 'crumb-tools';
    const slug = SubBubbles.focusCat === CORE ? 'core' : SubBubbles.focusCat.slug;
    // List view
    const listBtn = document.createElement('a');
    listBtn.className = 'crumb-tool';
    listBtn.href = 'javascript:void(0)';
    listBtn.textContent = 'list view';
    listBtn.addEventListener('click', () => openListView(SubBubbles.focusCat));
    tools.appendChild(listBtn);
    // Bookmark
    const bmBtn = document.createElement('a');
    bmBtn.className = 'crumb-tool';
    bmBtn.href = 'javascript:void(0)';
    const bookmarked = isBookmarked(slug);
    bmBtn.textContent = bookmarked ? '★ bookmarked' : '☆ bookmark';
    bmBtn.style.color = bookmarked ? 'var(--gold-bright)' : '';
    bmBtn.addEventListener('click', () => {
      const added = toggleBookmark(slug);
      toast(added ? 'Bookmarked' : 'Removed bookmark');
      updateBreadcrumb(segments);   // re-render to update label
    });
    tools.appendChild(bmBtn);
    // Share
    const shBtn = document.createElement('a');
    shBtn.className = 'crumb-tool';
    shBtn.href = 'javascript:void(0)';
    shBtn.textContent = 'share';
    shBtn.addEventListener('click', () => copyShareLink(slug));
    tools.appendChild(shBtn);
    el.appendChild(tools);
  }
}
// Initial empty breadcrumb so the element exists
setTimeout(() => updateBreadcrumb([{ label: 'Universe', cb: null }]), 200);

// =================================================================
// Left-nav wiring  —  every Memory Navigation tab does something real
// =================================================================
const NAV_ACTIONS = {
  universe:  { kind: 'view',  label: 'Living Universe' },
  active:    { kind: 'cluster', slug: null,        label: 'Active Memories (recent)' },
  graph:     { kind: 'cluster', slug: 'core',      label: 'Knowledge Graph (entity links)' },
  agents:    { kind: 'cluster', slug: 'agents',    label: 'AI Agents & Tasks' },
  repos:     { kind: 'cluster', slug: 'github',    label: 'Repositories' },
  sessions:  { kind: 'cluster', slug: 'terminal',  label: 'Sessions' },
  devices:   { kind: 'devices', label: 'Connected Devices' },
  archive:   { kind: 'cluster', slug: 'longterm',  label: 'Long-Term Archive' },
  insights:  { kind: 'cluster', slug: 'planning',  label: 'Insights' },
};
function setNavActive(id){
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.nav === id);
  });
}
async function handleNav(id){
  const action = NAV_ACTIONS[id];
  if (!action) return;
  setNavActive(id);
  if (action.kind === 'view'){
    closeZoom();
  } else if (action.kind === 'devices'){
    closeZoom();
    const strip = document.querySelector('.devstrip');
    if (strip){
      strip.scrollIntoView({ behavior: 'smooth', block: 'end' });
      strip.style.boxShadow = '0 -30px 60px -10px rgba(220,38,38,0.5)';
      setTimeout(() => { strip.style.boxShadow = ''; }, 1600);
    }
  } else if (action.kind === 'cluster'){
    if (id === 'active'){
      // Synthesize an "active" view: open the most recently-active category
      try{
        const r = await fetch('/api/recent?n=1');
        if (r.ok){
          const recent = await r.json();
          if (recent.length){
            const author = recent[0].written_by;
            const map = { 'vps-claude': 'terminal', 'local-claude': 'coding', 'chat-claude': 'web' };
            const target = (CATS.find(c => c.slug === (map[author] || 'agents'))) || CORE;
            openZoom(target);
            return;
          }
        }
      } catch(_){}
      openZoom(CORE);
      return;
    }
    const target = action.slug === 'core'
      ? CORE
      : CATS.find(c => c.slug === action.slug);
    if (target) openZoom(target);
  }
  Audio?.shimmer && Audio.shimmer(900);
}
document.querySelectorAll('.nav-item').forEach(el => {
  el.style.cursor = 'pointer';
  el.addEventListener('click', () => handleNav(el.dataset.nav));
});

// Legend toggle
let _legendOn = false;
document.getElementById('legendToggle')?.addEventListener('click', () => {
  _legendOn = !_legendOn;
  showLegend(_legendOn);
  document.getElementById('legendToggle').classList.toggle('active', _legendOn);
});

// =================================================================
// Visual-language legend  —  hover toggle for "what does each
// connection mean?" so any future AI walking through this dashboard
// can read the symbolism without spelunking source.
// =================================================================
const LEGEND_HTML = `
  <div class="legend-card">
    <div class="legend-title">Universe Visual Vocabulary</div>
    <div class="legend-row"><span class="legend-mark" style="background:#dc2626"></span>
      <div><b>CORE sphere</b> — every memory ever written. Inside shows the ZAI lockup.</div></div>
    <div class="legend-row"><span class="legend-mark" style="background:#ff5046"></span>
      <div><b>Category orb</b> — a semantic slice of CORE (Coding, Web, Mobile, GitHub, Agents, Planning, Long-Term, Terminal). Interior texture is the slice's essence; rim color = the category.</div></div>
    <div class="legend-row"><span class="legend-line" style="background:linear-gradient(90deg,#dc2626,transparent)"></span>
      <div><b>Spoke (color = category)</b> — orb belongs to CORE. Thickness = orb size, brightness pulses with activity.</div></div>
    <div class="legend-row"><span class="legend-line" style="background:linear-gradient(90deg,#e8d49a,transparent)"></span>
      <div><b>Gold arc</b> — two categories share at least one tag (semantic overlap). Hover to see shared tags.</div></div>
    <div class="legend-row"><span class="legend-dot"></span>
      <div><b>Bead flow</b> — continuous data movement along spokes. Direction (inward / outward) is random — it's energy, not direction-of-flow.</div></div>
    <div class="legend-row"><span class="legend-dot" style="background:#fff;box-shadow:0 0 8px #fff"></span>
      <div><b>Bright flyer</b> — a real memory event happening RIGHT NOW. Travels author → entity referenced.</div></div>
    <div class="legend-row"><span class="legend-ring"></span>
      <div><b>Sonar ring from CORE</b> — any SSE activity (memory.add, decision.log, tool_call) propagating through the universe.</div></div>
    <div class="legend-row"><span class="legend-mark" style="background:radial-gradient(circle,#ff3a3a,#a01a1a);box-shadow:0 0 8px #ff3a3a"></span>
      <div><b>Actor heartbeat ring</b> — Claude instance is online (wrote something in the last 5 minutes).</div></div>
    <div class="legend-foot">All visuals derived from real Postgres data. Source: this repo.</div>
  </div>`;
function showLegend(show){
  let el = document.getElementById('legendPanel');
  if (!el){
    el = document.createElement('div');
    el.id = 'legendPanel';
    el.innerHTML = LEGEND_HTML;
    document.body.appendChild(el);
  }
  el.classList.toggle('show', show);
}

// Intro splash — dismiss on tap or after 3.4s
(() => {
  const intro = document.getElementById('intro');
  if (!intro) return;
  let dismissed = false;
  function dismiss(){
    if (dismissed) return; dismissed = true;
    intro.classList.add('dismissed');
    setTimeout(() => intro.remove(), 1000);
  }
  intro.addEventListener('click', dismiss);
  setTimeout(dismiss, 3400);
})();

// =================================================================
// Bootstrap
// =================================================================
(async () => {
  await Promise.all([
    loadClusters(),
    loadStats(),
    loadPresence(),
    loadMemoryStream(),
    loadConflictsAndFusion(),
    loadMCP(),
    renderDevices(),
  ]);
  sizeAll(); layoutBubbles();
  connectSSE();
  setInterval(loadStats, 25000);
  setInterval(loadPresence, 15000);
  setInterval(loadMemoryStream, 30000);
  setInterval(loadClusters, 60000);
  setInterval(loadConflictsAndFusion, 60000);
  setInterval(loadMCP, 30000);
  setInterval(renderDevices, 20000);
})();

})();
</script>
</body></html>

"""


UNIVERSE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#08030a">
<title>ZAI · Universe</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;500;600;700&family=Cormorant+Garamond:wght@300;400;500&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<script type="importmap">
{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}
</script>
<script src="https://unpkg.com/gsap@3.12.5/dist/gsap.min.js"></script>
<script type="module">
  import { initCloud3D } from '/static/cloud3d.js';
  (async () => {
    try {
      window.Cloud3D = await initCloud3D(document.getElementById('cloud3d'));
      window.dispatchEvent(new Event('cloud3d-ready'));
    } catch (e) { console.error(e); }
  })();
</script>
<style>
:root{
  --bg-base:#08030a;--fg:#f5ecdb;--fg-soft:#d4c3a0;--fg-dim:#8a7a6a;--muted:#5a4d44;
  --line:#2a1818;--line-bright:#4a2424;--gold:#e8d49a;--gold-bright:#f5dca3;--gold-deep:#8c6f3a;
  --red:#a01a1a;--red-bright:#dc2626;--red-hot:#ff3a3a;--red-warm:#ff7060;
  --serif:'Cinzel',serif;--serif-soft:'Cormorant Garamond',serif;
  --sans:'Inter',system-ui,sans-serif;--mono:'JetBrains Mono',ui-monospace,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100vh;width:100vw;overflow:hidden;background:var(--bg-base);color:var(--fg);font-family:var(--sans)}
button,a{font-family:inherit;cursor:pointer;background:none;border:none;color:inherit;text-decoration:none}

/* Backdrop layers (same vocabulary as the hub home) */
#bg{position:fixed;inset:-4%;z-index:0;background-color:#1a0508;background-image:url(/static/nebula.jpg);background-size:cover;background-position:center;
  filter:sepia(0.55) hue-rotate(-22deg) saturate(2.8) brightness(0.96) contrast(1.22);
  transform:scale(1.08);animation:nebDrift 90s ease-in-out infinite alternate}
@keyframes nebDrift{0%{transform:scale(1.08) translate(0,0)}50%{transform:scale(1.12) translate(-1.4%,-0.8%)}100%{transform:scale(1.10) translate(1.0%,-1.2%)}}
#bg-glow{position:fixed;inset:0;z-index:1;pointer-events:none;
  background:radial-gradient(ellipse 110% 70% at 50% 0%,rgba(255,70,50,0.18),transparent 70%),
    radial-gradient(ellipse 40% 40% at 18% 30%,rgba(255,90,60,0.10),transparent 70%),
    radial-gradient(ellipse 38% 40% at 82% 65%,rgba(255,80,80,0.12),transparent 70%);
  mix-blend-mode:screen}
#bg-vignette{position:fixed;inset:0;z-index:1;pointer-events:none;
  background:radial-gradient(ellipse 70% 95% at 50% 100%,rgba(10,3,6,0.78),rgba(10,3,6,0.20) 55%,transparent),
    radial-gradient(ellipse 60% 60% at 50% 50%,transparent 0%,rgba(10,3,6,0.30) 100%)}
canvas{position:fixed;inset:0;display:block}
#dust{z-index:2;pointer-events:none}
#stars{z-index:3;pointer-events:none}
#cloud3d{z-index:4;pointer-events:auto}
#cloud{z-index:5;pointer-events:none}

/* Minimal top chrome */
.topshell{position:fixed;top:0;left:0;right:0;z-index:20;display:flex;align-items:center;justify-content:space-between;
  padding:20px 28px;background:linear-gradient(180deg,rgba(8,3,6,0.85),rgba(8,3,6,0.45) 70%,transparent);
  backdrop-filter:blur(4px)}
.topshell .logo{font-family:var(--serif);font-weight:600;letter-spacing:.45em;font-size:18px;color:var(--gold-bright);
  background:linear-gradient(180deg,var(--gold-bright) 0%,var(--gold-deep) 100%);-webkit-background-clip:text;background-clip:text;color:transparent;
  padding-left:.45em;text-shadow:0 0 18px rgba(255,80,60,0.4)}
.topshell .back{display:inline-flex;align-items:center;gap:8px;padding:9px 16px;border:1px solid var(--line-bright);border-radius:2px;
  font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--gold-deep);text-transform:uppercase;transition:.15s}
.topshell .back:hover{color:var(--red-warm);border-color:var(--red);background:rgba(220,38,38,0.06)}
.topshell .back svg{width:11px;height:11px;stroke:currentColor;fill:none;stroke-width:1.6}

.bottom-info{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:20;
  font-family:var(--mono);font-size:9.5px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase;
  display:flex;align-items:center;gap:10px;padding:8px 16px;background:rgba(8,3,6,0.62);backdrop-filter:blur(4px);border:1px solid var(--line);border-radius:2px}
.bottom-info .dot{width:5px;height:5px;border-radius:50%;background:var(--red-hot);box-shadow:0 0 8px var(--red-hot);animation:beat 1.6s ease-in-out infinite}
@keyframes beat{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.85)}}

/* Breadcrumb position adjusted for /universe */
#breadcrumb{position:fixed;left:50%;top:76px;transform:translateX(-50%);z-index:21;
  display:flex;align-items:center;gap:10px;padding:9px 18px;
  background:rgba(8,3,6,0.78);backdrop-filter:blur(12px);
  border:1px solid var(--line-bright);font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;
  opacity:1;transition:opacity .35s, transform .35s}
#breadcrumb .crumb{color:var(--fg-dim);text-decoration:none}
#breadcrumb a.crumb{cursor:pointer}
#breadcrumb a.crumb:hover{color:var(--red-warm)}
#breadcrumb .crumb.final{color:var(--gold-bright)}
#breadcrumb .crumb-sep{color:var(--muted)}
#breadcrumb .crumb-tools{display:flex;gap:10px;margin-left:14px;padding-left:14px;border-left:1px solid var(--line-bright)}
#breadcrumb .crumb-tool{color:var(--gold-deep);text-decoration:none;cursor:pointer;font-size:9px;letter-spacing:.32em}
#breadcrumb .crumb-tool:hover{color:var(--red-warm)}

/* Drawer (memory detail) — reused from hub home */
.drawer{position:fixed;z-index:60;top:0;right:0;bottom:0;width:460px;max-width:92vw;background:rgba(8,3,6,.96);
  backdrop-filter:blur(24px) saturate(150%);border-left:1px solid var(--gold-deep);box-shadow:-30px 0 80px -20px rgba(220,38,38,.45);
  transform:translateX(100%);transition:transform .4s cubic-bezier(.22,.61,.36,1);overflow-y:auto;padding:48px 32px 60px}
.drawer.open{transform:translateX(0)}
.drawer .close{position:absolute;top:18px;right:18px;width:32px;height:32px;border:1px solid var(--gold-deep);background:transparent;color:var(--gold);
  cursor:pointer;font-family:var(--mono);font-size:16px;transition:.15s;border-radius:2px}
.drawer .close:hover{color:var(--red-warm);border-color:var(--red-warm)}
.drawer .kind{font-family:var(--serif);font-size:10px;letter-spacing:.36em;color:var(--red-warm);text-transform:uppercase}
.drawer h2{font-family:var(--serif-soft);font-weight:400;font-size:22px;margin:10px 0 18px;letter-spacing:.01em;line-height:1.4;color:var(--gold-bright)}
.drawer .meta{font-family:var(--mono);font-size:10.5px;color:var(--fg-dim);letter-spacing:.04em;display:grid;grid-template-columns:auto 1fr;gap:7px 16px;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--line)}
.drawer .meta .k{color:var(--gold-deep);text-transform:uppercase;font-size:9px;letter-spacing:.28em}
.drawer .meta .v{color:var(--fg-soft);word-break:break-all;font-family:var(--mono)}
.drawer .body{font-family:var(--sans);font-size:14px;line-height:1.7;color:var(--fg);white-space:pre-wrap}
.drawer .tags{margin-top:18px;display:flex;flex-wrap:wrap;gap:7px}
.drawer .tag{font-family:var(--mono);font-size:10px;padding:4px 9px;background:rgba(220,38,38,.10);border:1px solid var(--red);color:var(--red-warm);letter-spacing:.05em}

#tip{position:fixed;z-index:30;pointer-events:none;background:rgba(8,3,6,0.95);border:1px solid var(--red);padding:8px 12px;
  font-family:var(--mono);font-size:11px;color:var(--fg);max-width:340px;letter-spacing:.02em;
  opacity:0;transform:translate(-50%,-130%);transition:opacity .12s}
#tip.show{opacity:1}
#tip .head{color:var(--red-warm);font-family:var(--serif);font-size:9px;letter-spacing:.32em;text-transform:uppercase;margin-bottom:4px}
#tip .body{color:var(--fg-soft);font-family:var(--sans);font-size:12px;line-height:1.4;font-weight:400}

body.in-bubble #breadcrumb{transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>
<div id="bg"></div>
<div id="bg-glow"></div>
<div id="bg-vignette"></div>
<canvas id="dust"></canvas>
<canvas id="stars"></canvas>
<canvas id="cloud3d"></canvas>
<canvas id="cloud"></canvas>

<header class="topshell">
  <div class="logo">ZAI</div>
  <a class="back" href="/"><svg viewBox="0 0 24 24"><path d="M19 12H5M5 12l6-6M5 12l6 6"/></svg>Hub home</a>
</header>

<aside class="drawer" id="drawer" aria-hidden="true"><button class="close" id="drawerClose">×</button><div id="drawerBody"></div></aside>

<div id="tip"></div>

<div class="bottom-info"><span class="dot"></span><span>The Living Memory · click any orb to enter</span></div>

<script src="/static/universe.js"></script>
</body></html>
"""


BLOCKS_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a0508">
<title>ZAI · Memory Hub</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;500;600;700&family=Cormorant+Garamond:wght@300;400;500;600&family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --bg:#0a0508;--bg-soft:#0e0608;--surface:#140709;--surface-2:#1c0a0d;--surface-3:#240e12;
  --line:#2a1818;--line-bright:#3a2222;
  --fg:#f5ecdb;--fg-soft:#d4c3a0;--fg-dim:#8a7a6a;--muted:#5a4d44;
  --gold:#e8d49a;--gold-bright:#f5dca3;--gold-deep:#8c6f3a;
  --red:#a01a1a;--red-bright:#dc2626;--red-hot:#ff3a3a;--red-warm:#ff7060;
  --serif:'Cinzel',serif;
  --serif-soft:'Cormorant Garamond',serif;
  --sans:'Inter',-apple-system,system-ui,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,Menlo,monospace;
  --max-w:1480px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--fg);font-family:var(--sans);min-height:100vh;line-height:1.6;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  font-feature-settings:'ss01','kern','liga';scroll-behavior:smooth}
a,button{font-family:inherit;cursor:pointer;background:none;border:none;color:inherit;text-decoration:none}
code{font-family:var(--mono);font-size:.85em;background:rgba(220,38,38,0.08);padding:1px 6px;border-radius:2px;color:var(--gold-bright)}

/* ===== FOCUS RINGS — accessibility ===== */
*:focus{outline:none}
*:focus-visible{outline:2px solid var(--red-warm);outline-offset:2px;border-radius:2px}

/* ===== SCROLL-REVEAL — opt-in via .reveal.before, never blocks ===== */
/* Elements are visible by default.  When JS wants the stagger effect
   it adds .before to set opacity:0, then removes it on viewport entry. */
.reveal{opacity:1;transform:translateY(0);transition:opacity .7s cubic-bezier(.22,.61,.36,1), transform .7s cubic-bezier(.22,.61,.36,1)}
.reveal.before{opacity:0;transform:translateY(14px)}
.reveal.delay-1{transition-delay:.06s}
.reveal.delay-2{transition-delay:.12s}
.reveal.delay-3{transition-delay:.18s}
.reveal.delay-4{transition-delay:.24s}

/* ===== REDUCED MOTION ===== */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:0.01ms !important;animation-iteration-count:1 !important;transition-duration:0.01ms !important;scroll-behavior:auto !important}
  .reveal{opacity:1;transform:none}
  video{display:none}
  .hero-block .hero-fallback{display:block}
}

/* ===== HEADER ===== */
.hdr{position:sticky;top:0;z-index:50;background:linear-gradient(180deg,rgba(10,5,8,0.96),rgba(10,5,8,0.85));backdrop-filter:blur(14px) saturate(140%);-webkit-backdrop-filter:blur(14px) saturate(140%);border-bottom:1px solid var(--line)}
.hdr-inner{max-width:var(--max-w);margin:0 auto;display:flex;align-items:center;gap:18px;padding:14px 28px}
.hdr-logo{font-family:var(--serif);font-weight:600;letter-spacing:.45em;font-size:14px;background:linear-gradient(180deg,var(--gold-bright) 0%,var(--gold) 50%,var(--gold-deep) 100%);-webkit-background-clip:text;background-clip:text;color:transparent;padding-left:.45em;flex-shrink:0;text-shadow:0 0 12px rgba(255,80,60,0.25);cursor:pointer;transition:filter .3s}
.hdr-logo:hover{filter:brightness(1.2)}
.hdr-tagline{font-family:var(--serif-soft);font-style:italic;font-size:12px;letter-spacing:.18em;color:var(--gold);text-transform:uppercase;flex-shrink:0;margin-left:-8px}
.hdr-spacer{flex:1}
.hdr-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}
.hdr-btn{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border:1px solid var(--line-bright);border-radius:2px;font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;transition:.15s}
.hdr-btn:hover{color:var(--red-warm);border-color:var(--red);background:rgba(220,38,38,0.06)}
.hdr-btn svg{width:11px;height:11px;stroke:currentColor;fill:none;stroke-width:1.6}

/* ===== PAGE LAYOUT ===== */
.page{max-width:var(--max-w);margin:0 auto;padding:0 28px 80px}
.page-grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:36px;padding-top:32px}
.main-col{min-width:0;display:flex;flex-direction:column;gap:14px}
.side-col{display:flex;flex-direction:column;gap:22px;position:sticky;top:84px;align-self:start;max-height:calc(100vh - 96px);overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--line) transparent;padding-bottom:20px}
.side-col::-webkit-scrollbar{width:3px}.side-col::-webkit-scrollbar-thumb{background:var(--line)}

/* ===== HERO VIDEO ===== */
.hero-block{position:relative;height:240px;border-radius:3px;overflow:hidden;border:1px solid var(--line);margin-bottom:24px;background:#1a0508}
.hero-block .hero-img, .hero-block video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.hero-block .hero-img{z-index:0}
.hero-block video{z-index:1}
.hero-block::after{content:'';position:absolute;inset:0;background:linear-gradient(180deg, rgba(10,5,8,0.20) 0%, rgba(10,5,8,0.55) 60%, rgba(10,5,8,0.92) 100%);z-index:2}
.hero-text{position:relative;z-index:3;padding:36px 40px;display:flex;flex-direction:column;justify-content:flex-end;height:100%}
.hero-text h1{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:42px;color:var(--gold-bright);letter-spacing:.005em;line-height:1.05;text-shadow:0 2px 20px rgba(10,5,8,0.8)}
.hero-text .sub{font-family:var(--mono);font-size:11px;letter-spacing:.36em;color:var(--gold);text-transform:uppercase;margin-top:8px;text-shadow:0 1px 8px rgba(10,5,8,0.8)}

.page-title{display:none}

.section{margin-top:48px}
.section-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;padding-bottom:10px;border-bottom:1px solid var(--line)}
.section-head .left{display:flex;align-items:center;gap:14px}
.section-head .h-eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase}
.section-head .h-title{font-family:var(--serif-soft);font-style:italic;font-size:22px;color:var(--fg-soft);letter-spacing:.005em}
.section-head .count{font-family:var(--mono);font-size:10px;letter-spacing:.18em;color:var(--gold)}

/* ===== ACTIVE AGENTS ROW ===== */
.agents{display:grid;grid-template-columns:repeat(auto-fill, minmax(280px, 1fr));gap:16px}
.agent{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:18px 20px;transition:border-color .15s, transform .2s;
  --accent:#dc2626;display:flex;flex-direction:column;gap:12px}
.agent::before{content:'';position:absolute;left:0;top:18px;bottom:18px;width:2px;background:var(--accent);box-shadow:0 0 8px var(--accent);opacity:.7}
.agent{transition:transform .3s cubic-bezier(.22,.61,.36,1), border-color .3s, box-shadow .3s}
.agent:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:0 12px 32px -14px rgba(220,38,38,0.35)}
.agent-head{display:flex;align-items:center;gap:12px}
.agent-avatar{width:42px;height:42px;border-radius:50%;background-size:cover;background-position:center;border:1.5px solid var(--accent);box-shadow:0 0 10px rgba(220,38,38,0.4);position:relative;flex-shrink:0;display:grid;place-items:center;
  font-family:var(--mono);font-size:14px;color:#fff;font-weight:600}
.agent-avatar span{text-shadow:0 0 4px rgba(0,0,0,0.5)}
.agent-avatar.online::after{content:'';position:absolute;right:-2px;bottom:-2px;width:10px;height:10px;border-radius:50%;background:var(--red-hot);border:2px solid var(--bg);box-shadow:0 0 6px var(--red-hot);animation:beat 1.6s ease-in-out infinite}
.agent-avatar.recent::after{content:'';position:absolute;right:-2px;bottom:-2px;width:10px;height:10px;border-radius:50%;background:var(--gold);border:2px solid var(--bg)}
.agent-avatar.idle, .agent-avatar.cold{filter:grayscale(.6) brightness(.55);border-color:var(--muted)}
.agent-id{flex:1;min-width:0}
.agent-name{font-family:var(--mono);font-size:11.5px;letter-spacing:.18em;color:var(--gold-bright);text-transform:uppercase;font-weight:600}
.agent-meta{font-family:var(--mono);font-size:9.5px;letter-spacing:.08em;color:var(--gold-deep);margin-top:3px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.agent-meta .status.online{color:var(--red-hot)}
.agent-meta .status.recent{color:var(--gold)}
.agent-meta .status.idle{color:var(--fg-dim)}
.agent-meta .status.cold{color:var(--muted)}
.agent-recent{list-style:none;display:flex;flex-direction:column;gap:6px}
.recent-row{display:flex;gap:9px;padding:7px 0;border-top:1px dashed var(--line);cursor:pointer;transition:background .12s, padding .12s;border-radius:2px}
.recent-row:first-child{border-top:0;padding-top:2px}
.recent-row:hover{background:rgba(220,38,38,0.05);padding:7px 8px}
.recent-row .r-i{font-family:var(--mono);font-size:9px;color:var(--gold-deep);letter-spacing:.06em;flex-shrink:0;padding-top:2px;width:18px}
.recent-row .r-body{flex:1;min-width:0}
.recent-row .r-text{font-family:var(--sans);font-size:12px;line-height:1.45;color:var(--fg-soft);overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.recent-row .r-meta{font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:.06em;margin-top:3px}
.recent-empty{font-family:var(--serif-soft);font-style:italic;font-size:13px;color:var(--fg-dim);padding:8px 0}
.agent-open{align-self:flex-start;font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;transition:color .15s;padding:4px 0}
.agent-open:hover{color:var(--red-warm)}

.agent.placeholder{border-style:dashed;border-color:var(--line-bright);justify-content:center;align-items:center;text-align:center;padding:32px 24px;background:transparent}
.agent.placeholder::before{display:none}
.agent.placeholder:hover{transform:none;background:rgba(220,38,38,0.03)}
.agent-placeholder-icon{width:32px;height:32px;color:var(--gold-deep);margin-bottom:6px}
.agent-placeholder-icon svg{width:32px;height:32px}
.placeholder-title{font-family:var(--mono);font-size:10px;letter-spacing:.32em;color:var(--gold);text-transform:uppercase}
.placeholder-body{font-family:var(--sans);font-size:12px;line-height:1.5;color:var(--fg-dim);margin:8px 0 12px;max-width:30ch}
.placeholder-cta{font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--red-warm);text-transform:uppercase;border:1px solid var(--red);padding:6px 12px;border-radius:2px;transition:.15s}
.placeholder-cta:hover{background:rgba(220,38,38,0.10)}

@keyframes beat{0%,100%{opacity:1}50%{opacity:.4}}

/* ===== SIDE RAIL ===== */
.side-block{background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:14px 16px}
.side-head{font-family:var(--mono);font-size:9px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.side-big{font-family:var(--mono);font-size:24px;color:var(--red-warm);font-weight:500;line-height:1.05;margin-bottom:4px}
.side-big small{font-family:var(--sans);font-size:10px;color:var(--fg-dim);letter-spacing:.18em;text-transform:uppercase;margin-left:6px;font-weight:400}
.side-spark{width:100%;height:30px;display:block;margin-top:6px}
.side-spark path.line{stroke:var(--red-warm);stroke-width:1.3;fill:none;filter:drop-shadow(0 0 3px rgba(255,112,96,0.5))}
.side-spark path.area{stroke:none;fill:rgba(255,112,96,0.18)}

.so-row{display:flex;align-items:center;gap:9px;padding:6px 0;font-family:var(--mono);font-size:10.5px;color:var(--fg-soft);border-bottom:1px dashed var(--line)}
.so-row:last-child{border-bottom:0}
.so-row .so-avatar{width:22px;height:22px;border-radius:50%;background-size:cover;background-position:center;border:1px solid var(--red);position:relative;flex-shrink:0;display:grid;place-items:center;color:#fff;font-size:9px;font-weight:600}
.so-row .so-avatar.online::after{content:'';position:absolute;right:-1px;bottom:-1px;width:6px;height:6px;border-radius:50%;background:var(--red-hot);border:1.5px solid var(--surface);box-shadow:0 0 4px var(--red-hot);animation:beat 1.6s ease-in-out infinite}
.so-row .so-avatar.recent::after{content:'';position:absolute;right:-1px;bottom:-1px;width:6px;height:6px;border-radius:50%;background:var(--gold);border:1.5px solid var(--surface)}
.so-row .so-avatar.idle, .so-row .so-avatar.cold{filter:grayscale(.6) brightness(.55);border-color:var(--muted)}
.so-row .so-lbl{flex:1;letter-spacing:.06em}
.so-row .so-ago{color:var(--gold-deep);font-size:9px}

.side-tags{display:flex;flex-wrap:wrap;gap:4px}
.side-tags .st{font-family:var(--mono);font-size:9.5px;padding:3px 8px;background:rgba(232,212,154,0.06);border:1px solid var(--line-bright);color:var(--fg-soft);letter-spacing:.04em;border-radius:99px;cursor:pointer;transition:.15s}
.side-tags .st:hover{color:var(--red-warm);border-color:var(--red)}
.side-tags .st span{color:var(--gold-deep);font-size:8.5px;margin-left:3px}

.side-dec-title{font-family:var(--serif-soft);font-style:italic;font-size:14px;color:var(--gold);line-height:1.4}
.side-dec-meta{font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:.06em;margin-top:5px}

.side-univ{display:block;text-decoration:none;border:1px solid var(--line-bright);border-radius:3px;overflow:hidden;transition:.15s}
.side-univ:hover{border-color:var(--red);box-shadow:0 4px 18px -6px rgba(220,38,38,0.4)}
.side-univ-vis{aspect-ratio:21/11;background:radial-gradient(ellipse at 50% 50%,rgba(220,38,38,0.4) 0%,rgba(220,38,38,0.05) 60%,transparent),
  radial-gradient(circle at 20% 30%,rgba(232,212,154,0.3),transparent 30%),
  radial-gradient(circle at 80% 70%,rgba(122,166,255,0.25),transparent 25%),
  radial-gradient(circle at 50% 80%,rgba(192,132,255,0.25),transparent 25%),
  #0e0608;position:relative}
.side-univ-vis::after{content:'';position:absolute;left:50%;top:50%;width:12px;height:12px;transform:translate(-50%,-50%);background:radial-gradient(circle,#ff3a3a,#a01a1a);border-radius:50%;box-shadow:0 0 18px rgba(255,80,60,0.8)}
.side-univ-cta{padding:10px 12px;background:var(--surface-2);display:flex;align-items:center;justify-content:space-between;font-family:var(--serif-soft);font-style:italic;font-size:12px;color:var(--fg-soft)}
.side-univ-cta .arr{color:var(--gold-deep);transition:.15s}
.side-univ:hover .arr{color:var(--red-warm);transform:translateX(2px)}

/* ===== DOCUMENTS SECTION ===== */
.docs-section{display:grid;grid-template-columns:1fr 1.4fr;gap:18px;align-items:stretch}
.docs-drop{position:relative;background:linear-gradient(180deg, rgba(20,8,10,0.8), rgba(8,3,6,0.8));border:1.5px dashed var(--gold-deep);border-radius:3px;
  padding:32px 26px;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;
  transition:border-color .2s, background .2s;cursor:pointer;min-height:200px}
.docs-drop:hover, .docs-drop.over{border-color:var(--red-warm);background:linear-gradient(180deg, rgba(40,12,15,0.85), rgba(20,8,10,0.85));box-shadow:0 0 28px -8px rgba(220,38,38,0.4)}
.docs-drop .icon{width:42px;height:42px;color:var(--gold);margin-bottom:10px;display:grid;place-items:center}
.docs-drop .icon svg{width:42px;height:42px;stroke:currentColor;fill:none;stroke-width:1.4}
.docs-drop .h{font-family:var(--serif-soft);font-style:italic;font-size:18px;color:var(--gold-bright);margin-bottom:6px}
.docs-drop .p{font-family:var(--sans);font-size:12px;color:var(--fg-dim);max-width:30ch;line-height:1.5}
.docs-drop .hint{font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--gold-deep);text-transform:uppercase;margin-top:14px;padding:6px 12px;border:1px solid var(--line-bright);border-radius:99px}
.docs-drop input[type=file]{display:none}
.docs-drop.uploading{pointer-events:none}
.docs-drop.uploading .icon{animation:spin 1.2s linear infinite}
@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}

.docs-recent{display:flex;flex-direction:column;gap:8px}
.doc-card{display:grid;grid-template-columns:44px 1fr auto;gap:14px;align-items:center;padding:14px 18px;background:var(--surface);border:1px solid var(--line);border-radius:3px;text-decoration:none;color:var(--fg-soft);transition:.18s}
.doc-card:hover{border-color:var(--red);background:var(--surface-2);transform:translateX(2px)}
.doc-card .doc-ico{width:44px;height:44px;border-radius:3px;background:linear-gradient(135deg, rgba(232,212,154,0.12), rgba(220,38,38,0.10));display:grid;place-items:center;color:var(--gold);font-family:var(--mono);font-size:9px;letter-spacing:.18em;text-transform:uppercase}
.doc-card .doc-meta{min-width:0}
.doc-card .doc-title{font-family:var(--serif-soft);font-style:italic;font-size:15px;color:var(--gold-bright);line-height:1.3;margin-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.doc-card .doc-sub{font-family:var(--mono);font-size:9.5px;color:var(--gold-deep);letter-spacing:.06em}
.doc-card .doc-arrow{color:var(--gold-deep);font-size:14px;transition:.15s}
.doc-card:hover .doc-arrow{color:var(--red-warm);transform:translateX(2px)}
.doc-empty{font-family:var(--serif-soft);font-style:italic;font-size:13px;color:var(--fg-dim);padding:30px;text-align:center;border:1px dashed var(--line);border-radius:3px}
.docs-view-all{align-self:flex-start;font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;transition:color .15s;padding:4px 0}
.docs-view-all:hover{color:var(--red-warm)}

#uploadToast{position:fixed;bottom:24px;right:24px;z-index:90;padding:14px 20px;background:rgba(20,8,10,0.95);border:1px solid var(--gold-deep);border-radius:3px;backdrop-filter:blur(8px);
  font-family:var(--mono);font-size:11px;letter-spacing:.18em;color:var(--gold-bright);text-transform:uppercase;
  opacity:0;transform:translateY(20px);transition:opacity .25s, transform .25s;pointer-events:none}
#uploadToast.show{opacity:1;transform:translateY(0)}
#uploadToast.err{border-color:var(--red);color:var(--red-warm)}

@media (max-width:760px){.docs-section{grid-template-columns:1fr}}

/* Two-stage upload form */
.docs-stage{position:relative;min-height:200px}
.docs-form{display:flex;flex-direction:column;gap:14px;padding:22px 24px;background:linear-gradient(180deg, rgba(20,8,10,0.88), rgba(8,3,6,0.88));
  border:1px solid var(--gold-deep);border-radius:3px;min-height:200px;animation:formFade .25s ease}
.docs-form[hidden]{display:none}
.docs-drop[hidden]{display:none}
@keyframes formFade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.docs-form-head{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px dashed var(--line);padding-bottom:10px;margin-bottom:2px}
.docs-form-eyebrow{font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--gold);text-transform:uppercase;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:70%}
.docs-form-cancel{background:none;border:0;font-family:var(--mono);font-size:9.5px;letter-spacing:.18em;color:var(--gold-deep);cursor:pointer;text-transform:uppercase;transition:color .15s}
.docs-form-cancel:hover{color:var(--red-warm)}
.docs-field{display:flex;flex-direction:column;gap:5px}
.docs-field > span{font-family:var(--mono);font-size:9.5px;letter-spacing:.22em;color:var(--gold);text-transform:uppercase}
.docs-field > span em{font-style:italic;letter-spacing:.04em;color:var(--gold-deep);text-transform:none;font-size:10px;margin-left:6px}
.docs-field input,.docs-field textarea{font-family:var(--sans);font-size:13px;color:var(--gold-bright);background:rgba(8,3,6,0.6);border:1px solid var(--line);border-radius:2px;padding:9px 11px;resize:vertical;transition:border-color .15s, background .15s}
.docs-field input:focus,.docs-field textarea:focus{outline:0;border-color:var(--red-warm);background:rgba(20,8,10,0.85)}
.docs-field textarea{font-family:var(--serif-soft);font-size:13.5px;line-height:1.5}
.docs-checkbox{display:flex;align-items:flex-start;gap:9px;cursor:pointer;font-family:var(--sans);font-size:12px;color:var(--fg-soft);padding:6px 0}
.docs-checkbox input{margin-top:3px;accent-color:#dc2626;cursor:pointer}
.docs-checkbox em{font-style:italic;color:var(--gold-deep);font-size:11px}
.docs-form-actions{display:flex;justify-content:space-between;align-items:center;border-top:1px dashed var(--line);padding-top:12px;margin-top:2px}
.docs-form-meta{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;color:var(--gold-deep)}
.docs-form-save{font-family:var(--mono);font-size:10px;letter-spacing:.32em;text-transform:uppercase;
  background:linear-gradient(180deg,#dc2626,#9a1212);color:#f5dca3;border:1px solid #dc2626;
  padding:9px 18px;border-radius:2px;cursor:pointer;transition:transform .15s, box-shadow .15s, opacity .15s}
.docs-form-save:hover{transform:translateY(-1px);box-shadow:0 6px 20px -6px rgba(220,38,38,0.55)}
.docs-form-save:disabled{opacity:.55;cursor:wait;transform:none;box-shadow:none}

/* Doc card with cover-art thumb (lives in docs-recent + library room) */
.doc-card .doc-cover{width:44px;height:55px;border-radius:2px;background-size:cover;background-position:center;
  border:1px solid var(--line-bright);box-shadow:0 4px 12px -4px rgba(0,0,0,.5);justify-self:center}
.doc-card.has-cover{grid-template-columns:44px 1fr auto}

/* Doc row = card + delete-button */
.doc-row{position:relative;display:grid;grid-template-columns:1fr auto;gap:6px;align-items:stretch}
.doc-row .doc-card{flex:1}
.doc-del{background:rgba(8,3,6,0.6);border:1px solid var(--line);border-radius:3px;width:36px;
  color:var(--gold-deep);font-size:14px;cursor:pointer;transition:.15s;align-self:stretch;display:grid;place-items:center}
.doc-del:hover{color:var(--red-warm);border-color:var(--red);background:rgba(40,8,12,0.7)}
.doc-del:disabled{opacity:.5;cursor:wait}

/* ===== TIMELINE TICKER (compressed) ===== */
.timeline-ticker{display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--line);border-radius:3px;overflow:hidden;list-style:none;padding:0;margin:0}
.tk-row{display:grid;grid-template-columns:80px 14px 100px 1fr 130px 20px;gap:10px;align-items:center;padding:11px 16px;border-top:1px solid var(--line);cursor:pointer;transition:background .15s, padding-left .15s;list-style:none;position:relative}
.tk-row::after{content:'→';position:absolute;right:14px;color:var(--gold-deep);font-family:var(--mono);font-size:13px;opacity:0;transform:translateX(-4px);transition:opacity .2s, transform .2s, color .2s}
.tk-row:first-child{border-top:0}
.tk-row:hover{background:rgba(220,38,38,0.06);padding-left:22px}
.tk-row:hover::after{opacity:1;transform:translateX(0);color:var(--red-warm)}
.tk-time{font-family:var(--mono);font-size:9.5px;color:var(--gold-deep);letter-spacing:.06em}
.tk-dot{width:7px;height:7px;border-radius:50%;box-shadow:0 0 4px currentColor;justify-self:center}
.tk-author{font-family:var(--mono);font-size:9.5px;letter-spacing:.18em;color:var(--gold);text-transform:uppercase}
.tk-text{font-family:var(--sans);font-size:13px;color:var(--fg-soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.tk-tags{font-family:var(--mono);font-size:9px;color:var(--gold-deep);letter-spacing:.04em;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ===== TIMELINE (full) ===== */
.timeline{display:flex;flex-direction:column;gap:18px}
.tl-section{position:relative}
.tl-section-head{font-family:var(--mono);font-size:9.5px;letter-spacing:.36em;color:var(--gold);text-transform:uppercase;margin-bottom:10px;display:flex;align-items:baseline;gap:8px}
.tl-section-head span{color:var(--gold-deep);font-size:9px}
.tl-list{list-style:none;display:flex;flex-direction:column}
.tl-row{display:grid;grid-template-columns:90px 16px 1fr;gap:0;align-items:start;padding:9px 0;border-top:1px dashed var(--line);cursor:pointer;transition:background .12s}
.tl-row:first-child{border-top:0}
.tl-row:hover{background:rgba(220,38,38,0.05)}
.tl-time{font-family:var(--mono);font-size:9.5px;color:var(--gold-deep);letter-spacing:.06em;padding-top:5px}
.tl-bullet{width:7px;height:7px;border-radius:50%;margin-top:8px;box-shadow:0 0 4px currentColor;flex-shrink:0;justify-self:start}
.tl-content{padding-left:12px}
.tl-text{font-family:var(--sans);font-size:13px;line-height:1.5;color:var(--fg-soft)}
.tl-meta{font-family:var(--mono);font-size:9px;color:var(--gold-deep);letter-spacing:.06em;margin-top:4px}
.empty{font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim);font-size:14px;padding:30px;text-align:center}

/* ===== BLOCKS GRID ===== */
.blocks{display:grid;grid-template-columns:repeat(auto-fill, minmax(330px, 1fr));gap:20px}
.block{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:3px;overflow:hidden;cursor:pointer;transition:transform .2s cubic-bezier(.22,.61,.36,1), border-color .2s, box-shadow .2s;--accent:#dc2626;display:flex;flex-direction:column}
.block:hover{transform:translateY(-3px);border-color:var(--line-bright);box-shadow:0 12px 36px -10px rgba(220,38,38,0.22)}
.block-hero{aspect-ratio:21/9;position:relative;overflow:hidden;background:#1a0508;transition:transform .9s cubic-bezier(.22,.61,.36,1)}
.block-hero img.block-hero-img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;z-index:0}
.block-hero-vid{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .55s ease;z-index:1}
.block-hero-vid.on{opacity:1}
.block-hero-tint{position:absolute;inset:0;background:linear-gradient(180deg, rgba(10,5,8,0.30) 0%, rgba(10,5,8,0.75) 70%, var(--surface) 100%);z-index:2}
.block:hover .block-hero{transform:scale(1.04)}
.block-body{padding:18px 22px 22px;display:flex;flex-direction:column;gap:10px;flex:1}
.block-head{display:flex;align-items:center;justify-content:space-between}
.block-tag{font-family:var(--mono);font-size:10px;letter-spacing:.32em;font-weight:600}
.block-count{font-family:var(--mono);font-size:24px;font-weight:500;color:var(--gold-bright);letter-spacing:0;line-height:1}
.block-sub{font-family:var(--serif-soft);font-style:italic;font-size:13px;color:var(--fg-dim);letter-spacing:.005em}
.block-previews{list-style:none;display:flex;flex-direction:column;gap:5px;margin-top:6px}
.block-previews li{display:flex;align-items:start;gap:8px;font-family:var(--sans);font-size:12.5px;color:var(--fg-soft);line-height:1.4;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.block-previews li.empty{color:var(--fg-dim);font-style:italic;font-family:var(--serif-soft);font-size:13px}
.block-previews .pv-dot{width:5px;height:5px;border-radius:50%;background:var(--accent);margin-top:7px;flex-shrink:0;box-shadow:0 0 4px var(--accent)}
.block-open{align-self:flex-start;margin-top:6px;font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--accent);text-transform:uppercase;font-weight:500;transition:color .2s, transform .2s}
.block:hover .block-open{color:var(--gold-bright);transform:translateX(3px)}

/* ===== ROOM OVERLAY ===== */
.room{position:fixed;inset:0;z-index:80;background:rgba(10,5,8,0.9);backdrop-filter:blur(20px) saturate(150%);
  display:none;flex-direction:column;overflow-y:auto;padding:0}
.room.open{display:flex}
.room-close{position:fixed;top:18px;right:24px;z-index:5;width:36px;height:36px;display:grid;place-items:center;
  border:1px solid var(--gold-deep);background:rgba(8,3,6,0.85);color:var(--gold);font-family:var(--mono);font-size:18px;border-radius:2px;transition:.15s;cursor:pointer}
.room-close:hover{color:var(--red-warm);border-color:var(--red-warm)}
.rm-loading{padding:80px;text-align:center;font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim)}
.rm-header{padding:80px 60px 32px;max-width:980px;margin:0 auto;width:100%;position:relative;--accent:#dc2626}
.rm-hero{position:absolute;left:0;right:0;top:0;height:280px;background-size:cover;background-position:center;z-index:0;opacity:.45}
.rm-hero-tint{position:absolute;inset:0;background:linear-gradient(180deg, transparent 0%, var(--bg) 95%)}
.rm-eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.4em;color:var(--accent);text-transform:uppercase;position:relative;z-index:1}
.rm-title{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:48px;line-height:1.1;color:var(--gold-bright);margin:14px 0 10px;letter-spacing:.005em;position:relative;z-index:1}
.rm-sub{font-family:var(--mono);font-size:11px;letter-spacing:.18em;color:var(--gold-deep);text-transform:uppercase;position:relative;z-index:1}
.rm-list{list-style:none;display:flex;flex-direction:column;max-width:980px;margin:0 auto;padding:0 60px 80px;width:100%}
.rm-card{display:grid;grid-template-columns:60px 1fr;gap:0;padding:22px 0;border-top:1px solid var(--line);cursor:pointer;transition:background .12s, padding .15s}
.rm-card[data-mid]:hover{background:rgba(220,38,38,0.04);padding:22px 18px;border-radius:2px}
.rm-card.decision, .rm-card.entity, .rm-card.tool{cursor:default}
.rm-i{font-family:var(--mono);font-size:11px;color:var(--gold-deep);letter-spacing:.06em;padding-top:3px}
.rm-card-body{padding-left:14px}
.rm-card-meta{font-family:var(--mono);font-size:9.5px;letter-spacing:.18em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:6px}
.rm-card-title{font-family:var(--serif-soft);font-style:italic;font-size:18px;color:var(--gold);margin-bottom:6px}
.rm-card-text{font-family:var(--sans);font-size:14px;line-height:1.65;color:var(--fg-soft)}
.rm-alts{margin-top:10px;font-family:var(--mono);font-size:10px;letter-spacing:.06em;color:var(--gold-deep)}
.rm-alts span{margin-right:6px}
.rm-alts .alt{display:inline-block;padding:3px 8px;border:1px dashed var(--line-bright);border-radius:99px;color:var(--fg-dim);margin:3px}
.rm-empty{padding:60px;text-align:center;max-width:620px;margin:0 auto}
.rm-empty-title{font-family:var(--serif-soft);font-style:italic;font-size:22px;color:var(--fg-soft);margin-bottom:8px}
.rm-empty-sub{font-family:var(--sans);font-size:13px;color:var(--fg-dim);margin-bottom:14px}
.rm-empty-tags{display:flex;flex-wrap:wrap;justify-content:center;gap:6px}
.rm-empty-tags .tag{font-family:var(--mono);font-size:10px;padding:3px 9px;background:rgba(220,38,38,0.06);border:1px solid var(--line-bright);color:var(--fg-dim);border-radius:99px}

/* ===== MEMORY READER (nested inside room or standalone) ===== */
.reader{position:fixed;top:0;right:0;bottom:0;width:540px;max-width:96vw;z-index:90;background:linear-gradient(180deg,var(--surface) 0%,var(--bg) 100%);border-left:1px solid var(--gold-deep);box-shadow:-30px 0 80px -20px rgba(220,38,38,0.45);transform:translateX(100%);transition:transform .4s cubic-bezier(.22,.61,.36,1);overflow-y:auto;padding:40px 30px 50px}
.reader.open{transform:translateX(0)}
.reader .close{position:absolute;top:18px;right:18px;width:34px;height:34px;display:grid;place-items:center;border:1px solid var(--gold-deep);background:rgba(8,3,6,0.7);color:var(--gold);font-family:var(--mono);font-size:18px;border-radius:2px;transition:.15s;cursor:pointer}
.reader .close:hover{color:var(--red-warm);border-color:var(--red-warm)}
.rd-loading{padding:30px;text-align:center;font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim)}
.rd-eyebrow{font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--red-warm);text-transform:uppercase}
.rd-headline{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:24px;line-height:1.35;color:var(--gold-bright);margin:8px 0 14px;letter-spacing:.005em}
.rd-meta{font-family:var(--mono);font-size:10.5px;color:var(--fg-dim);letter-spacing:.04em;display:grid;grid-template-columns:auto 1fr;gap:7px 16px;margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--line)}
.rd-meta .k{color:var(--gold-deep);text-transform:uppercase;font-size:9px;letter-spacing:.28em}
.rd-meta .v{color:var(--fg-soft);word-break:break-all;font-family:var(--mono)}
.rd-body{font-family:var(--sans);font-size:14.5px;line-height:1.7;color:var(--fg);white-space:pre-wrap}
.rd-actions{display:flex;justify-content:flex-end;margin-bottom:18px}
.rd-del-btn{font-family:var(--mono);font-size:10px;letter-spacing:.22em;color:var(--gold-deep);text-transform:uppercase;padding:7px 13px;border:1px solid var(--line-bright);border-radius:2px;background:rgba(8,3,6,0.5);cursor:pointer;transition:.15s}
.rd-del-btn:hover{color:var(--red-warm);border-color:var(--red);background:rgba(220,38,38,0.10)}
.rd-tags{margin-top:18px;display:flex;flex-wrap:wrap;gap:6px}
.rd-tags .tag{font-family:var(--mono);font-size:10px;padding:3px 9px;background:rgba(220,38,38,0.10);border:1px solid var(--red);color:var(--red-warm);letter-spacing:.04em;border-radius:99px}

/* ===== RESPONSIVE ===== */
@media (max-width:1100px){
  .page-grid{grid-template-columns:minmax(0,1fr) 270px;gap:24px}
}
@media (max-width:900px){
  .page-grid{grid-template-columns:1fr;gap:24px}
  .side-col{position:relative;top:auto;max-height:none;overflow:visible}
  .tk-row{grid-template-columns:60px 14px 1fr;gap:8px}
  .tk-row .tk-author, .tk-row .tk-tags{display:none}
  .hero-block{height:160px}
  .hero-text h1{font-size:28px}
}
@media (max-width:760px){
  .hdr-tagline{display:none}
  .hdr-inner{padding:12px 16px;gap:10px}
  .page{padding:0 14px 60px}
  .agents, .blocks{grid-template-columns:1fr}
  .rm-header{padding:60px 24px 24px}
  .rm-title{font-size:30px}
  .rm-list{padding:0 24px 60px}
  .reader{width:100vw}
  .hero-block{height:140px}
  .hero-text{padding:20px 22px}
  .hero-text h1{font-size:22px}
}
</style>
</head>
<body>
<header class="hdr">
  <div class="hdr-inner">
    <span class="hdr-logo">ZAI</span>
    <span class="hdr-tagline">Memory Hub</span>
    <div class="hdr-spacer"></div>
    <div class="hdr-actions">
      <a class="hdr-btn" href="/library"><svg viewBox="0 0 24 24"><path d="M4 4h12l4 4v12H4z"/><path d="M4 8h16M8 12h8M8 16h6"/></svg>Library</a>
      <a class="hdr-btn" href="/universe"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="9" ry="3.5"/></svg>Universe</a>
      <a class="hdr-btn" href="/connect"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></svg>Connect</a>
    </div>
  </div>
</header>

<main class="page">
  <section class="hero-block">
    <img class="hero-img" src="/static/gen/lib_hero_today.jpg" alt="" loading="eager" decoding="async">
    <video autoplay loop muted playsinline poster="/static/gen/lib_hero_today.jpg" id="heroVideo">
      <source src="/static/gen/lib_video_hero.mp4" type="video/mp4">
    </video>
    <div class="hero-text">
      <div class="sub">Living Memory · Hub <span style="opacity:.5">· build 2026-05-20-3</span></div>
      <h1>A shared mind <br>across agents.</h1>
    </div>
  </section>

  <div class="page-grid">
    <div class="main-col">
      <section class="section" style="margin-top:0">
        <div class="section-head">
          <div class="left">
            <span class="h-eyebrow">Active Agents</span>
            <span class="h-title">connected right now</span>
          </div>
          <span class="count" id="agentCount">—</span>
        </div>
        <div class="agents" id="agents"></div>
      </section>

      <section class="section">
        <div class="section-head">
          <div class="left">
            <span class="h-eyebrow">Documents</span>
            <span class="h-title">your PDF vault</span>
          </div>
          <a class="docs-view-all" href="javascript:void(0)" onclick="window.__viewAllDocs && window.__viewAllDocs()">View all →</a>
        </div>
        <div class="docs-section">
          <div class="docs-stage" id="docsStage">
            <label class="docs-drop" id="docsDrop">
              <input type="file" id="docsFile" accept="application/pdf,.pdf">
              <div class="icon" id="docsIcon">
                <svg viewBox="0 0 24 24"><path d="M12 4v12M12 4l-5 5M12 4l5 5"/><path d="M4 18v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2"/></svg>
              </div>
              <div class="h">Drop a PDF here</div>
              <div class="p">Or click to pick a file (max 20 MB). You'll review the title, description, and tags before it's saved.</div>
              <div class="hint" id="docsHint">PDF only · 20 MB max</div>
            </label>
            <form class="docs-form" id="docsForm" hidden>
              <div class="docs-form-head">
                <div class="docs-form-eyebrow" id="docsFormFilename">filename.pdf</div>
                <button type="button" class="docs-form-cancel" id="docsFormCancel">cancel</button>
              </div>
              <label class="docs-field">
                <span>Title</span>
                <input type="text" id="docsTitle" maxlength="200" required>
              </label>
              <label class="docs-field">
                <span>Description <em>(what's in this PDF, why you saved it)</em></span>
                <textarea id="docsDesc" rows="3" maxlength="800" placeholder="One or two sentences. Future you reads this in 3 months."></textarea>
              </label>
              <label class="docs-field">
                <span>Tags <em>(comma-separated, ≤8)</em></span>
                <input type="text" id="docsTags" placeholder="philosophy, draft, idea">
              </label>
              <label class="docs-checkbox">
                <input type="checkbox" id="docsCover">
                <span>Generate a cover image <em>(~$0.04, Flux 1.1 Pro · adds ~30s)</em></span>
              </label>
              <div class="docs-form-actions">
                <div class="docs-form-meta" id="docsFormMeta">— pages · — KB</div>
                <button type="submit" class="docs-form-save" id="docsSave">Save to Hub</button>
              </div>
            </form>
          </div>
          <div class="docs-recent" id="docsRecent"></div>
        </div>
      </section>

      <section class="section">
        <div class="section-head">
          <div class="left">
            <span class="h-eyebrow">Knowledge Blocks</span>
            <span class="h-title">click to enter a subject</span>
          </div>
        </div>
        <div class="blocks" id="blocks"></div>
      </section>

      <section class="section">
        <div class="section-head">
          <div class="left">
            <span class="h-eyebrow">Recent Activity</span>
            <span class="h-title">the last few moments</span>
          </div>
          <a class="count" href="/timeline" style="text-decoration:none">Open full timeline →</a>
        </div>
        <div class="timeline-ticker" id="timeline"></div>
      </section>
    </div>

    <aside class="side-col">
      <section class="side-block">
        <div class="side-head">Online</div>
        <div id="sideOnline"></div>
      </section>
      <section class="side-block">
        <div class="side-head">Activity · last hour</div>
        <div class="side-big" id="sideActivityCount">—<small>events</small></div>
        <div id="sideSparkline"></div>
      </section>
      <section class="side-block">
        <div class="side-head">Trending tags</div>
        <div class="side-tags" id="sideTrending"></div>
      </section>
      <section class="side-block">
        <div class="side-head">Latest decision</div>
        <div id="sideDecision"></div>
      </section>
      <section class="side-block">
        <div class="side-head">Universe</div>
        <a class="side-univ" href="/universe">
          <div class="side-univ-vis"></div>
          <div class="side-univ-cta">
            <div>Open the memory cloud</div>
            <div class="arr">→</div>
          </div>
        </a>
      </section>
    </aside>
  </div>
</main>

<div class="room" id="room">
  <button class="room-close" id="roomClose" aria-label="close">×</button>
  <div id="roomBody"></div>
</div>

<aside class="reader" id="reader" aria-hidden="true">
  <button class="close" id="readerClose" aria-label="close">×</button>
  <div id="readerBody"></div>
</aside>
<div id="uploadToast"></div>

<script src="/static/blocks.js?v=2026-05-20-prod-8"></script>
</body></html>
"""


LIBRARY_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#08030a">
<title>ZAI · Living Memory</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;500;600;700&family=Cormorant+Garamond:wght@300;400;500;600&family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root{
  --bg:#0a0508;--bg-soft:#0e0608;--surface:#140709;--surface-2:#1c0a0d;
  --line:#2a1818;--line-bright:#3a2222;
  --fg:#f5ecdb;--fg-soft:#d4c3a0;--fg-dim:#8a7a6a;--muted:#5a4d44;
  --gold:#e8d49a;--gold-bright:#f5dca3;--gold-deep:#8c6f3a;
  --red:#a01a1a;--red-bright:#dc2626;--red-hot:#ff3a3a;--red-warm:#ff7060;
  --serif:'Cinzel',serif;
  --serif-soft:'Cormorant Garamond',serif;
  --sans:'Inter',-apple-system,system-ui,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,Menlo,monospace;
  --max-w:1620px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--fg);font-family:var(--sans);min-height:100vh;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
button{font-family:inherit;cursor:pointer;background:none;border:none;color:inherit}

/* ===== HEADER ===== */
.hdr{position:sticky;top:0;z-index:50;background:linear-gradient(180deg, rgba(10,5,8,0.96), rgba(10,5,8,0.85));
  backdrop-filter:blur(14px) saturate(140%);-webkit-backdrop-filter:blur(14px) saturate(140%);
  border-bottom:1px solid var(--line)}
.hdr-inner{max-width:var(--max-w);margin:0 auto;display:flex;align-items:center;gap:24px;padding:14px 28px}
.hdr-logo{font-family:var(--serif);font-weight:600;letter-spacing:.45em;font-size:14px;
  background:linear-gradient(180deg,var(--gold-bright) 0%,var(--gold) 50%,var(--gold-deep) 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent;padding-left:.45em;flex-shrink:0;
  text-shadow:0 0 12px rgba(255,80,60,0.25)}
.hdr-tagline{font-family:var(--serif-soft);font-style:italic;font-size:12px;letter-spacing:.18em;color:var(--gold);text-transform:uppercase;flex-shrink:0;margin-left:-8px}
.hdr-search{flex:1;position:relative;max-width:540px;margin:0 auto}
.hdr-search input{width:100%;padding:11px 16px 11px 42px;background:var(--surface);border:1px solid var(--line-bright);
  color:var(--fg);font-family:var(--sans);font-size:14px;outline:none;letter-spacing:.01em;transition:border-color .15s, background .15s;border-radius:2px}
.hdr-search input::placeholder{color:var(--fg-dim)}
.hdr-search input:focus{border-color:var(--red);background:var(--surface-2)}
.hdr-search svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);width:16px;height:16px;color:var(--gold-deep);stroke:currentColor;fill:none;stroke-width:1.6}
.hdr-search kbd{position:absolute;right:14px;top:50%;transform:translateY(-50%);font-family:var(--mono);font-size:9px;letter-spacing:.18em;color:var(--gold-deep);padding:3px 7px;border:1px solid var(--line-bright);border-radius:2px;background:var(--surface)}
.hdr-actions{display:flex;align-items:center;gap:10px;flex-shrink:0}
.hdr-btn{display:inline-flex;align-items:center;gap:8px;padding:9px 14px;border:1px solid var(--line-bright);border-radius:2px;
  font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;transition:.15s}
.hdr-btn:hover{color:var(--red-warm);border-color:var(--red);background:rgba(220,38,38,0.06)}
.hdr-btn svg{width:11px;height:11px;stroke:currentColor;fill:none;stroke-width:1.6}

/* ===== FILTER CHIP ===== */
.filter-chip{display:none;align-items:center;gap:8px;padding:5px 12px 5px 14px;background:rgba(220,38,38,0.08);border:1px solid var(--red);border-radius:99px;
  font-family:var(--mono);font-size:10px;letter-spacing:.18em;color:var(--red-warm);text-transform:uppercase}
.filter-chip.show{display:inline-flex}
.filter-chip button{margin-left:4px;font-family:var(--mono);font-size:14px;line-height:1;color:var(--red-warm);transition:color .15s}
.filter-chip button:hover{color:var(--gold-bright)}

/* ===== APP GRID ===== */
.app{max-width:var(--max-w);margin:0 auto;display:grid;grid-template-columns:240px minmax(0,1fr) 320px;gap:32px;padding:32px 28px}

/* ===== BOOKSHELVES (library) ===== */
.shelves-section{grid-column:1 / -1;margin-bottom:8px}
.shelves-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--color-line)}
.shelves-head .lbl{font-family:var(--font-mono);font-size:10px;letter-spacing:.36em;color:var(--color-gold-deep,#8c6f3a);text-transform:uppercase}
.shelves-head .ttl{font-family:var(--font-serif-italic);font-style:italic;font-size:18px;color:var(--color-fg-soft,#d4c3a0);margin-left:14px}
.shelves{display:grid;grid-template-columns:repeat(auto-fill, minmax(180px, 1fr));gap:14px}
.shelf{position:relative;aspect-ratio:3/4;border:1px solid var(--color-line);border-radius:3px;overflow:hidden;cursor:pointer;background:#1a0508;transition:transform .25s cubic-bezier(.22,.61,.36,1), border-color .25s, box-shadow .25s}
.shelf:hover{transform:translateY(-3px);border-color:var(--color-accent,#dc2626);box-shadow:0 12px 32px -14px rgba(220,38,38,0.35)}
.shelf img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;z-index:0}
.shelf::after{content:'';position:absolute;inset:0;background:linear-gradient(180deg, rgba(10,5,8,0.10) 0%, rgba(10,5,8,0.55) 60%, rgba(10,5,8,0.95) 100%);z-index:1}
.shelf .shelf-body{position:absolute;left:0;right:0;bottom:0;padding:14px 16px;z-index:2}
.shelf .shelf-cat{font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.32em;color:#e8d49a;text-transform:uppercase;margin-bottom:4px}
.shelf .shelf-title{font-family:'Cormorant Garamond',serif;font-style:italic;font-weight:500;font-size:18px;line-height:1.2;color:#f5ecdb}
.shelf .shelf-count{position:absolute;top:10px;right:12px;font-family:'JetBrains Mono',monospace;font-size:11px;color:#f5dca3;background:rgba(8,3,6,0.78);border:1px solid #2a1818;padding:3px 9px;border-radius:99px;z-index:2}
.shelf.active{border-color:var(--color-accent,#dc2626);box-shadow:0 0 18px -6px rgba(220,38,38,0.6)}
.shelf.active::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:#dc2626;box-shadow:0 0 10px #dc2626;z-index:3}

/* ===== TAXONOMY (left rail) ===== */
.rail-l{position:sticky;top:84px;align-self:start;max-height:calc(100vh - 110px);overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.rail-l::-webkit-scrollbar{width:3px}.rail-l::-webkit-scrollbar-thumb{background:var(--line)}
.tx-head{font-family:var(--mono);font-size:9px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase;margin:22px 8px 8px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.tx-head:first-child{margin-top:0}
.tx-block{display:flex;flex-direction:column;gap:1px}
.tx-row{display:flex;align-items:center;gap:10px;padding:9px 10px;cursor:pointer;border-radius:2px;
  font-family:var(--sans);font-size:13px;color:var(--fg-soft);transition:background .15s, color .15s}
.tx-row:hover{background:rgba(220,38,38,0.05);color:var(--fg)}
.tx-row.active{background:rgba(220,38,38,0.14);color:var(--gold-bright)}
.tx-row.active::before{content:'';position:absolute;left:0;width:2px;height:60%;background:var(--red-hot);box-shadow:0 0 8px var(--red-hot)}
.tx-row{position:relative}
.tx-row .tx-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;box-shadow:0 0 5px currentColor;opacity:.85}
.tx-row .tx-avatar{width:14px;height:14px;border-radius:50%;flex-shrink:0;background-size:cover;background-position:center;border:1px solid var(--line-bright)}
.tx-row .tx-lbl{flex:1;letter-spacing:.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tx-row .tx-n{font-family:var(--mono);font-size:10px;color:var(--gold-deep);letter-spacing:.06em}
.tx-row.active .tx-n{color:var(--gold)}

/* ===== FEED (centre) ===== */
.feed{display:flex;flex-direction:column;gap:18px;min-width:0}
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:22px 26px;cursor:pointer;
  transition:transform .2s cubic-bezier(.22,.61,.36,1), border-color .2s, box-shadow .2s;
  --accent:#dc2626}
.card::before{content:'';position:absolute;left:0;top:18px;bottom:18px;width:2px;background:var(--accent);border-radius:0 2px 2px 0;opacity:.7}
.card:hover{transform:translateY(-2px);border-color:var(--line-bright);box-shadow:0 8px 30px -10px rgba(220,38,38,0.18)}
.card.hero{padding:0;overflow:hidden}
.card.hero .card-img{aspect-ratio:21/9;background-size:cover;background-position:center;position:relative;border-bottom:1px solid var(--line)}
.card.hero .card-img-overlay{position:absolute;inset:0;background:linear-gradient(180deg,rgba(10,5,8,0.15) 0%, rgba(10,5,8,0.55) 70%, rgba(10,5,8,0.92) 100%)}
.card.hero .card-img-tag{position:absolute;left:24px;bottom:18px;font-family:var(--mono);font-size:10px;letter-spacing:.32em;color:var(--c, var(--red-warm));text-transform:uppercase;
  background:rgba(8,3,6,0.78);padding:5px 11px;border:1px solid var(--c, var(--red));border-radius:2px}
.card.hero .card-body{padding:24px 28px 28px}
.card.hero .headline{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:30px;line-height:1.25;color:var(--gold-bright);margin:8px 0 12px;letter-spacing:.005em}
.card.hero .body{font-family:var(--sans);font-size:14.5px;line-height:1.65;color:var(--fg-soft);max-width:62ch}

.card-meta{display:flex;align-items:center;gap:9px;font-family:var(--mono);font-size:9.5px;letter-spacing:.18em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:10px}
.card-meta .cat{font-weight:600}
.card-meta .dot{color:var(--muted)}
.card-meta .imp{color:var(--gold)}
.card-meta .save-btn{margin-left:auto;font-family:var(--mono);font-size:14px;color:var(--gold-deep);transition:color .15s;line-height:1}
.card-meta .save-btn:hover{color:var(--gold-bright)}
.card-meta .save-btn.on{color:var(--gold-bright)}

.card-headline{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:21px;line-height:1.32;color:var(--fg);margin:4px 0 10px;letter-spacing:.005em}
.card-preview{font-family:var(--sans);font-size:13.5px;line-height:1.65;color:var(--fg-dim);max-width:64ch}

.eyebrow{display:flex;justify-content:space-between;font-family:var(--mono);font-size:9.5px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase}

.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
.tag{font-family:var(--mono);font-size:9.5px;padding:3px 9px;background:rgba(220,38,38,0.06);border:1px solid var(--line-bright);color:var(--fg-dim);letter-spacing:.04em;border-radius:99px;cursor:pointer;transition:.15s}
.tag:hover{color:var(--red-warm);border-color:var(--red);background:rgba(220,38,38,0.14)}

.empty{padding:60px 30px;text-align:center}
.empty-eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:8px}
.empty-title{font-family:var(--serif-soft);font-style:italic;font-size:20px;color:var(--fg-soft);margin-bottom:18px}
.empty-reset{padding:8px 18px;border:1px solid var(--red);border-radius:2px;color:var(--red-warm);font-family:var(--mono);font-size:10px;letter-spacing:.28em;text-transform:uppercase;background:rgba(220,38,38,0.06)}
.empty-reset:hover{background:rgba(220,38,38,0.14)}

/* ===== CONTEXT (right rail) ===== */
.rail-r{position:sticky;top:84px;align-self:start;max-height:calc(100vh - 110px);overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--line) transparent;display:flex;flex-direction:column;gap:22px}
.rail-r::-webkit-scrollbar{width:3px}.rail-r::-webkit-scrollbar-thumb{background:var(--line)}
.ctx-head{font-family:var(--mono);font-size:9px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.ctx-big{font-family:var(--mono);font-size:30px;color:var(--red-warm);font-weight:500;letter-spacing:0;margin-bottom:4px;line-height:1}
.ctx-big small{font-family:var(--sans);font-size:11px;color:var(--fg-dim);letter-spacing:.18em;text-transform:uppercase;margin-left:8px;font-weight:400}
.ctx-spark{width:100%;height:38px;display:block;margin-top:6px}
.ctx-empty{font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim);font-size:13px;padding:4px 0}

.pr-row{display:flex;align-items:center;gap:10px;padding:6px 0;font-family:var(--mono);font-size:11px;letter-spacing:.04em;color:var(--fg-soft);border-bottom:1px dashed var(--line)}
.pr-row:last-of-type{border-bottom:0}
.pr-avatar{width:24px;height:24px;border-radius:50%;background-size:cover;background-position:center;border:1px solid var(--red);box-shadow:0 0 6px rgba(220,38,38,0.4);position:relative;flex-shrink:0}
.pr-avatar.online::after{content:'';position:absolute;right:-1px;bottom:-1px;width:7px;height:7px;border-radius:50%;background:var(--red-hot);border:1.5px solid var(--bg);box-shadow:0 0 5px var(--red-hot);animation:beat 1.6s ease-in-out infinite}
.pr-avatar.recent::after{content:'';position:absolute;right:-1px;bottom:-1px;width:7px;height:7px;border-radius:50%;background:var(--gold);border:1.5px solid var(--bg)}
.pr-avatar.offline{filter:grayscale(.6) brightness(.5);border-color:var(--muted)}
.pr-lbl{flex:1}
.pr-ago{color:var(--gold-deep);font-size:10px}
@keyframes beat{0%,100%{opacity:1}50%{opacity:.4}}

.ctx-tags{display:flex;flex-wrap:wrap;gap:5px}
.ctx-tags .tg{font-family:var(--mono);font-size:10px;padding:3px 9px;background:rgba(232,212,154,0.06);border:1px solid var(--line-bright);color:var(--fg-soft);letter-spacing:.04em;border-radius:99px;cursor:pointer;transition:.15s}
.ctx-tags .tg:hover{color:var(--red-warm);border-color:var(--red)}
.ctx-tags .tg span{color:var(--gold-deep);font-size:9px;margin-left:4px}

.dec-row{padding:9px 0;border-bottom:1px dashed var(--line);cursor:pointer;transition:color .15s}
.dec-row:last-child{border-bottom:0}
.dec-row:hover .dec-title{color:var(--gold-bright)}
.dec-title{font-family:var(--serif-soft);font-size:14px;color:var(--gold);line-height:1.35;margin-bottom:3px;font-style:italic}
.dec-meta{font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:.06em}

.universe-portal .ctx-head{margin-bottom:8px}
.univ-card{display:flex;flex-direction:column;border:1px solid var(--line-bright);border-radius:3px;overflow:hidden;transition:.18s;cursor:pointer;text-decoration:none}
.univ-card:hover{border-color:var(--red);box-shadow:0 8px 30px -12px rgba(220,38,38,0.35)}
.univ-vis{aspect-ratio:21/10;background:radial-gradient(ellipse at 50% 50%, rgba(220,38,38,0.4) 0%, rgba(220,38,38,0.05) 60%, transparent),
  radial-gradient(circle at 20% 30%, rgba(232,212,154,0.3), transparent 30%),
  radial-gradient(circle at 80% 70%, rgba(122,166,255,0.25), transparent 25%),
  radial-gradient(circle at 50% 80%, rgba(192,132,255,0.25), transparent 25%),
  radial-gradient(circle at 30% 70%, rgba(94,226,160,0.2), transparent 25%),
  #0e0608;
  position:relative}
.univ-vis::after{content:'';position:absolute;left:50%;top:50%;width:14px;height:14px;transform:translate(-50%,-50%);background:radial-gradient(circle,#ff3a3a,#a01a1a);border-radius:50%;box-shadow:0 0 22px rgba(255,80,60,0.9)}
.univ-cta{padding:13px 15px;background:var(--surface-2);display:flex;align-items:center;gap:10px}
.univ-title{flex:1;font-family:var(--serif-soft);font-size:14px;color:var(--fg-soft);font-style:italic}
.univ-sub{display:none}
.univ-arrow{font-family:var(--mono);font-size:14px;color:var(--gold-deep);transition:transform .15s, color .15s}
.univ-card:hover .univ-arrow{color:var(--red-warm);transform:translateX(3px)}

/* ===== READER DRAWER ===== */
.reader{position:fixed;top:0;right:0;bottom:0;width:560px;max-width:96vw;z-index:80;
  background:linear-gradient(180deg, var(--surface) 0%, var(--bg) 100%);
  border-left:1px solid var(--gold-deep);box-shadow:-30px 0 80px -20px rgba(220,38,38,0.45);
  transform:translateX(100%);transition:transform .4s cubic-bezier(.22,.61,.36,1);overflow-y:auto;display:flex;flex-direction:column}
.reader.open{transform:translateX(0)}
.reader-close{position:absolute;top:20px;right:20px;z-index:5;width:34px;height:34px;display:grid;place-items:center;
  border:1px solid var(--gold-deep);background:rgba(8,3,6,0.7);color:var(--gold);font-family:var(--mono);font-size:18px;border-radius:2px;transition:.15s}
.reader-close:hover{color:var(--red-warm);border-color:var(--red-warm)}
.rd-hero{position:relative;aspect-ratio:21/9;background-size:cover;background-position:center}
.rd-hero-tint{position:absolute;inset:0;background:linear-gradient(180deg, rgba(10,5,8,0.1) 0%, rgba(10,5,8,0.7) 80%, var(--surface) 100%)}
.rd-eyebrow{position:absolute;left:26px;bottom:18px;font-family:var(--mono);font-size:10px;letter-spacing:.32em;color:var(--gold);text-transform:uppercase}
.rd-save{align-self:flex-end;margin:14px 26px 0;padding:6px 12px;border:1px solid var(--line-bright);border-radius:2px;font-family:var(--mono);font-size:10px;letter-spacing:.22em;color:var(--gold-deep);text-transform:uppercase;transition:.15s}
.rd-save:hover{color:var(--red-warm);border-color:var(--red)}
.rd-save.on{color:var(--gold-bright);border-color:var(--gold-deep);background:rgba(232,212,154,0.08)}
.rd-headline{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:28px;line-height:1.3;color:var(--gold-bright);padding:20px 30px 8px;letter-spacing:.005em}
.rd-meta{font-family:var(--mono);font-size:10.5px;color:var(--fg-dim);letter-spacing:.04em;display:grid;grid-template-columns:auto 1fr;gap:7px 16px;padding:8px 30px 20px;border-bottom:1px solid var(--line);margin-bottom:24px}
.rd-meta .k{color:var(--gold-deep);text-transform:uppercase;font-size:9px;letter-spacing:.28em}
.rd-meta .v{color:var(--fg-soft);word-break:break-all;font-family:var(--mono)}
.rd-body{font-family:var(--sans);font-size:15px;line-height:1.75;color:var(--fg);padding:0 30px 24px;white-space:pre-wrap}
.rd-tags{padding:0 30px 24px;display:flex;flex-wrap:wrap;gap:6px}
.rd-tags .tag{background:rgba(220,38,38,0.10);border-color:var(--red);color:var(--red-warm)}
.rd-rel{padding:18px 30px 30px;margin-top:14px;border-top:1px solid var(--line)}
.rd-rel-head{font-family:var(--mono);font-size:9px;letter-spacing:.36em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:14px}
.rel-row{display:flex;align-items:center;gap:11px;padding:10px 0;cursor:pointer;font-family:var(--sans);font-size:13px;color:var(--fg-soft);border-bottom:1px dashed var(--line);transition:color .15s;text-decoration:none}
.rel-row:last-child{border-bottom:0}
.rel-row:hover{color:var(--red-warm)}
.rel-row .dot{width:7px;height:7px;border-radius:50%;background:var(--red-warm);box-shadow:0 0 5px var(--red-warm);flex-shrink:0}
.rel-row.ent .dot{background:var(--gold);box-shadow:0 0 5px var(--gold)}
.rel-row .ovl{font-family:var(--mono);font-size:9px;color:var(--gold-deep);letter-spacing:.06em;margin-left:auto;flex-shrink:0}

/* ===== RESPONSIVE ===== */
@media (max-width: 1180px){
  .app{grid-template-columns:200px minmax(0,1fr) 270px;gap:24px}
}
@media (max-width: 900px){
  .app{grid-template-columns:1fr;gap:18px;padding:18px}
  .rail-l, .rail-r{position:relative;top:auto;max-height:none;overflow:visible}
  .rail-l{order:2;padding-top:8px;border-top:1px solid var(--line)}
  .rail-r{order:3;padding-top:8px;border-top:1px solid var(--line)}
  .feed{order:1}
  .hdr-inner{padding:12px 16px;gap:12px}
  .hdr-tagline{display:none}
  .hdr-search{order:99;width:100%;max-width:100%;margin:0;flex-basis:100%}
  .card.hero .headline{font-size:22px}
  .card-headline{font-size:18px}
}
</style>
</head>
<body>
<header class="hdr">
  <div class="hdr-inner">
    <span class="hdr-logo">ZAI</span>
    <span class="hdr-tagline">Living Memory</span>
    <div class="hdr-search">
      <svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><line x1="15" y1="15" x2="20" y2="20"/></svg>
      <input id="searchInput" type="text" placeholder="Search memories, tags, authors…" autocomplete="off">
      <kbd>⌘K</kbd>
    </div>
    <div class="hdr-actions">
      <a class="hdr-btn" href="/universe"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="9" ry="3.5"/></svg>Universe</a>
      <a class="hdr-btn" href="/dashboard"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>Dashboard</a>
    </div>
  </div>
</header>

<div class="app">
  <section class="shelves-section">
    <div class="shelves-head">
      <div><span class="lbl">Browse the shelves</span><span class="ttl">click a cover to filter the feed</span></div>
    </div>
    <div class="shelves" id="shelves"></div>
  </section>
  <aside class="rail-l">
    <div id="taxonomy"></div>
  </aside>
  <main class="feed-wrap">
    <div class="filter-chip" id="filterChip">
      <span>Filter:</span>
      <span id="filterLbl">All</span>
      <button onclick="window.__lib.clearFilter()" title="Clear filter">×</button>
    </div>
    <div class="feed" id="feed"></div>
  </main>
  <aside class="rail-r" id="context"></aside>
</div>

<aside class="reader" id="reader" aria-hidden="true">
  <button class="reader-close" id="readerClose" aria-label="close">×</button>
  <div id="readerBody"></div>
</aside>

<script src="/static/library.js"></script>
</body></html>
"""


CONNECT_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZAI Hub · Connect a new agent</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;600&family=Cormorant+Garamond:ital,wght@1,400;1,500&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --bg:#0a0508;--surface:#140709;--line:#2a1818;--line-bright:#3a2222;
  --fg:#f5ecdb;--fg-soft:#d4c3a0;--fg-dim:#8a7a6a;--gold:#e8d49a;--gold-bright:#f5dca3;--gold-deep:#8c6f3a;
  --red:#a01a1a;--red-bright:#dc2626;--red-warm:#ff7060;
  --serif:'Cinzel',serif;--serif-soft:'Cormorant Garamond',serif;
  --sans:'Inter',system-ui,sans-serif;--mono:'JetBrains Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--fg);font-family:var(--sans);line-height:1.65;min-height:100vh}
a{color:var(--red-warm);text-decoration:none;border-bottom:1px dotted}
a:hover{color:var(--gold-bright);border-bottom-color:var(--gold-bright)}
.wrap{max-width:780px;margin:0 auto;padding:60px 32px}
.eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.4em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:14px}
h1{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:42px;line-height:1.1;color:var(--gold-bright);margin-bottom:14px}
.lede{font-family:var(--serif-soft);font-style:italic;font-size:18px;color:var(--fg-soft);margin-bottom:36px;max-width:60ch}
h2{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:24px;color:var(--gold);margin:40px 0 14px;letter-spacing:.005em}
p{margin-bottom:14px;color:var(--fg-soft);max-width:65ch}
code{font-family:var(--mono);font-size:.88em;background:rgba(220,38,38,0.10);padding:2px 7px;border-radius:2px;color:var(--gold-bright)}
pre{font-family:var(--mono);font-size:13px;line-height:1.6;background:var(--surface);border:1px solid var(--line);padding:18px 22px;overflow-x:auto;color:var(--fg-soft);margin:14px 0;border-radius:3px;border-left:2px solid var(--red)}
pre .k{color:var(--gold-bright)}
pre .c{color:var(--fg-dim);font-style:italic}
ol{padding-left:26px;margin-bottom:18px;color:var(--fg-soft)}
ol li{margin-bottom:10px}
.back{display:inline-flex;align-items:center;gap:8px;margin-top:40px;padding:9px 16px;border:1px solid var(--line-bright);border-radius:2px;font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;border-bottom:0;transition:.15s}
.back:hover{color:var(--red-warm);border-color:var(--red);background:rgba(220,38,38,0.06)}
.callout{padding:18px 22px;border:1px solid var(--line-bright);background:var(--surface);border-left:2px solid var(--gold);border-radius:3px;margin:20px 0}
.callout .ctitle{font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-bright);text-transform:uppercase;margin-bottom:6px}
.callout p{margin-bottom:6px;font-size:14px}
.callout p:last-child{margin-bottom:0}
</style>
</head>
<body><div class="wrap">
<div class="eyebrow">Onboarding</div>
<h1>Connect a new agent</h1>
<p class="lede">Anything that speaks MCP can join the shared memory.  Pick a slug, point your client at the Hub's MCP endpoint, write a memory — your agent appears on the home page automatically.</p>

<h2>1 · Pick a slug for your agent</h2>
<p>One short kebab-case identifier that uniquely names this agent instance.  Convention: <code>&lt;surface&gt;-claude</code> for Claude variants, or anything else for non-Claude agents.</p>
<pre><span class="c"># examples</span>
ZAI_HUB_WRITTEN_BY=phone-claude
ZAI_HUB_WRITTEN_BY=cursor-zawwar
ZAI_HUB_WRITTEN_BY=hackerone-bot
ZAI_HUB_WRITTEN_BY=chat-claude-mobile</pre>

<h2>2 · Point your MCP client at the Hub</h2>
<p>The Hub speaks streamable-HTTP MCP at <code>{PUBLIC_URL}/mcp</code>.  In a Claude Code or Claude Desktop config:</p>
<pre>{
  "mcpServers": {
    "zai-hub": {
      "type": "http",
      "url": "{PUBLIC_URL}/mcp",
      "env": {
        "ZAI_HUB_WRITTEN_BY": "<span class="k">your-slug-here</span>"
      }
    }
  }
}</pre>

<p>For other surfaces (Cursor, Cline, custom Python, ChatGPT custom actions) — point at the same URL.  Anything that POSTs MCP JSON-RPC works.</p>

<h2>3 · Write a memory</h2>
<p>Use the <code>memory.add</code> tool exposed by the Hub.  Once you write a memory, your agent panel appears on <a href="/">the home page</a> within seconds.  Its 5 most recent memories will show, status will go online, and you can click into the panel for the full feed.</p>

<pre><span class="c"># from any python with mcp client</span>
memory_add(
  content="<span class="k">First memory from cursor-zawwar.</span>",
  tags=[<span class="k">"setup"</span>],
  entities=[<span class="k">"zai-memory-hub"</span>],
  importance=3,
)</pre>

<h2>4 · How memories find their block</h2>
<p>Each memory's <code>tags</code> determine which knowledge block it joins on the home page:</p>
<pre><span class="k">philosophy</span>: philosophy, draft, idea, thought, thinking, essay, note
<span class="k">hacking</span>: htb, ctf, pwn, exploit, recon, payload, shell, reverse,
          web-ex, binary, rop, buffer-overflow, rce, sqli, xss,
          lfi, rfi, priv-esc, pivot, active-directory
<span class="k">crypto</span>: crypto, market, trade, liquidity, regime, macro,
          btc, eth, framework, anteroom
<span class="k">infra</span>: infra, vps, mcp, systemd, pipeline, deploy, config, tech-debt, state
<span class="k">now-building</span>: milestone, ship, in-flight, ui, feature, build</pre>

<div class="callout">
  <div class="ctitle">One mind across agents</div>
  <p>Every connected agent reads and writes the same Postgres-backed memory table.  When VPS-Claude logs a decision and Local-Claude wakes up and reads <code>/api/recent</code>, it sees it.  When a phone-Claude writes a memory, the VPS-Claude session sees the SSE event and reacts.  This is the point.</p>
</div>

<a class="back" href="/">← Back to the Hub</a>
</div></body></html>
"""


AGENT_TOKEN_PATH = Path(os.environ.get("ZAI_HUB_AGENT_TOKEN_PATH", Path(__file__).resolve().parent.parent / "auth" / "agent.token"))


def _load_agent_token():
    try:
        return AGENT_TOKEN_PATH.read_text().strip()
    except Exception:
        return ""


AGENT_TOKEN_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZAI · Agent token</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;600&family=Cormorant+Garamond:ital,wght@1,400;1,500&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --bg:#0a0508;--surface:#140709;--surface-2:#1c0a0d;--line:#2a1818;--line-bright:#3a2222;
  --fg:#f5ecdb;--fg-soft:#d4c3a0;--fg-dim:#8a7a6a;--gold:#e8d49a;--gold-bright:#f5dca3;--gold-deep:#8c6f3a;
  --red:#a01a1a;--red-bright:#dc2626;--red-warm:#ff7060;
  --serif:'Cinzel',serif;--serif-soft:'Cormorant Garamond',serif;
  --sans:'Inter',system-ui,sans-serif;--mono:'JetBrains Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--fg);font-family:var(--sans);line-height:1.65;min-height:100vh}
a{color:var(--red-warm);text-decoration:none;border-bottom:1px dotted}
a:hover{color:var(--gold-bright);border-bottom-color:var(--gold-bright)}
.hdr{position:sticky;top:0;z-index:50;background:linear-gradient(180deg,rgba(10,5,8,0.96),rgba(10,5,8,0.85));backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
.hdr-inner{max-width:840px;margin:0 auto;display:flex;align-items:center;gap:14px;padding:14px 28px}
.hdr-logo{font-family:var(--serif);font-weight:600;letter-spacing:.45em;font-size:13px;background:linear-gradient(180deg,var(--gold-bright),var(--gold-deep));-webkit-background-clip:text;background-clip:text;color:transparent;padding-left:.45em;border-bottom:0}
.back{font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;border-bottom:0;padding:7px 12px;border:1px solid var(--line-bright);border-radius:2px;margin-left:auto;cursor:pointer}
.back:hover{color:var(--red-warm);border-color:var(--red)}
.wrap{max-width:840px;margin:0 auto;padding:50px 28px 80px}
.eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.4em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:14px}
h1{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:42px;color:var(--gold-bright);line-height:1.1;margin-bottom:14px}
.lede{font-family:var(--serif-soft);font-style:italic;font-size:17px;color:var(--fg-soft);margin-bottom:36px;max-width:60ch}
h2{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:22px;color:var(--gold);margin:36px 0 12px;letter-spacing:.005em}
p{margin-bottom:14px;color:var(--fg-soft);max-width:65ch}
code{font-family:var(--mono);font-size:.86em;background:rgba(220,38,38,0.08);padding:2px 7px;border-radius:2px;color:var(--gold-bright)}
.token-row{display:flex;gap:0;align-items:stretch;margin:18px 0;background:var(--surface);border:1px solid var(--line-bright);border-radius:3px;overflow:hidden}
.token-box{flex:1;font-family:var(--mono);font-size:12px;padding:14px 18px;color:var(--gold-bright);background:transparent;border:0;outline:none;letter-spacing:.02em;user-select:all;white-space:nowrap;overflow-x:auto;text-overflow:ellipsis}
.token-row button{font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;background:rgba(220,38,38,0.06);border:0;border-left:1px solid var(--line-bright);padding:0 18px;cursor:pointer;transition:.15s;white-space:nowrap}
.token-row button:hover{color:var(--gold-bright);background:rgba(220,38,38,0.16)}
.token-row button.ok{color:#5ee2a0}
pre{font-family:var(--mono);font-size:12px;line-height:1.65;background:var(--surface);border:1px solid var(--line);border-left:2px solid var(--red);padding:18px 22px;overflow-x:auto;color:var(--fg-soft);margin:14px 0;border-radius:3px;position:relative}
pre .k{color:var(--gold-bright)}
pre .c{color:var(--fg-dim);font-style:italic}
pre .v{color:var(--red-warm)}
pre .copybtn{position:absolute;top:8px;right:10px;font-family:var(--mono);font-size:9.5px;letter-spacing:.18em;color:var(--gold-deep);text-transform:uppercase;background:rgba(8,3,6,0.85);border:1px solid var(--line-bright);padding:5px 10px;cursor:pointer;border-radius:2px;transition:.15s}
pre .copybtn:hover{color:var(--gold-bright);border-color:var(--gold-deep)}
pre .copybtn.ok{color:#5ee2a0;border-color:#5ee2a0}
.callout{padding:16px 22px;background:var(--surface);border-left:2px solid var(--gold);border-radius:3px;margin:18px 0}
.callout .ctitle{font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-bright);text-transform:uppercase;margin-bottom:6px}
.warn{border-left-color:var(--red)}
.warn .ctitle{color:var(--red-warm)}
.qr-row{display:grid;grid-template-columns:auto 1fr;gap:24px;align-items:center;background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:22px;margin:20px 0}
.qr-row img{width:180px;height:180px;background:#fff;padding:10px;border-radius:3px;display:block}
.qr-row .qr-text{font-family:var(--sans);font-size:14px;line-height:1.6;color:var(--fg-soft)}
.qr-row .qr-text strong{color:var(--gold-bright);font-weight:500}
.linkrow a{display:inline-block;margin-right:14px;font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;padding:7px 13px;border:1px solid var(--line-bright);border-bottom:1px solid var(--line-bright);border-radius:2px;transition:.15s;text-decoration:none}
.linkrow a:hover{color:var(--red-warm);border-color:var(--red)}
@media (max-width:680px){.qr-row{grid-template-columns:1fr}.qr-row img{margin:0 auto}}
</style>
</head>
<body>
<header class="hdr"><div class="hdr-inner">
  <a class="hdr-logo" href="/">ZAI</a>
  <a class="back" href="/">← Back to Hub</a>
</div></header>
<main class="wrap">
<div class="eyebrow">Bootstrap kit</div>
<h1>Your agent token + login</h1>
<p class="lede">Save this page once. Everything an agent needs to connect to your memory, and everything you need to log in from a new device, lives here. Treat the token like a password.</p>

<h2>1 · Master agent token</h2>
<p>This is the bearer credential any agent (Claude Code, Cursor, Cline, ChatGPT custom action, custom script) needs to read/write your memories via MCP. <strong>One token. All agents.</strong></p>

<div class="token-row">
  <input class="token-box" id="agentToken" value="__AGENT_TOKEN__" readonly>
  <button onclick="copy(this, document.getElementById('agentToken').value)">Copy</button>
</div>

<div class="callout warn">
  <div class="ctitle">Keep this private</div>
  <p style="margin:0">Anyone with this token can read, write, and delete your entire memory. Treat it like a password. To rotate: re-run the token generation on the VPS (<code>python -c 'import secrets; print("zai_" + secrets.token_urlsafe(32))'</code>) and update <code>/etc/caddy/Caddyfile</code>.</p>
</div>

<h2>2 · Connect a Claude Code agent</h2>
<p>Drop this in <code>~/.config/claude-code/config.json</code> (or wherever Claude Code reads MCP config on the agent's machine):</p>
<pre id="cc-config"><button class="copybtn" onclick="copyPre('cc-config', this)">Copy</button>{
  <span class="k">"mcpServers"</span>: {
    <span class="k">"zai-hub"</span>: {
      <span class="k">"type"</span>: <span class="v">"http"</span>,
      <span class="k">"url"</span>: <span class="v">"{PUBLIC_URL}/mcp"</span>,
      <span class="k">"headers"</span>: {
        <span class="k">"Authorization"</span>: <span class="v">"Bearer __AGENT_TOKEN__"</span>
      }
    }
  }
}</pre>

<h2>3 · Connect a Cursor / Cline / generic MCP client</h2>
<p>Same shape, slightly different config location. Most MCP-aware clients accept <code>type: http</code>, <code>url</code>, and <code>headers</code>:</p>
<pre id="generic-config"><button class="copybtn" onclick="copyPre('generic-config', this)">Copy</button><span class="c"># bash one-liner — exports env vars an agent reads</span>
export ZAI_HUB_URL=<span class="v">"{PUBLIC_URL}/mcp"</span>
export ZAI_HUB_TOKEN=<span class="v">"__AGENT_TOKEN__"</span>
export ZAI_HUB_WRITTEN_BY=<span class="v">"your-agent-slug"</span>   <span class="c"># pick anything: cursor-zwwr / phone-claude / etc.</span></pre>

<h2>4 · Test the connection</h2>
<p>Once configured, ask the agent: <em>"What memories does the ZAI hub have about library design?"</em> — it should query the hub via the MCP <code>memory.recall</code> tool. Or check the home page: a new entry appears in the Active Agents row within seconds of the first memory write.</p>

<h2>5 · Login from a new device / browser</h2>
<p>Scan this QR with your phone (or paste the URL below into any browser). Logs you into the dashboard for 30 days.</p>

<div class="qr-row">
  <img src="/qr/login.png" alt="Login QR code">
  <div class="qr-text">
    <p><strong>Scan with phone camera</strong> — opens the dashboard logged in.</p>
    <p>Or paste this in any browser address bar:</p>
    <div class="token-row" style="margin-top:8px">
      <input class="token-box" id="loginUrl" value="__LOGIN_URL__" readonly>
      <button onclick="copy(this, document.getElementById('loginUrl').value)">Copy</button>
    </div>
    <p style="margin-top:10px;font-size:12px;color:var(--fg-dim)">After login, cookie persists 30 days. Hard-refresh only needed when I push a code update — Cache-Control: no-store is set, so just visit the page again.</p>
  </div>
</div>

<h2>6 · Save this page in your notes</h2>
<p>Bookmark <code><your-public-url>/agent-token</code>. Or save the page as PDF. This page rebuilds the kit on any device you're logged in to.</p>

<div class="linkrow" style="margin-top:30px">
  <a href="/">Home</a>
  <a href="/library">Library</a>
  <a href="/universe">Universe</a>
  <a href="/connect">/connect docs</a>
  <a href="/timeline">Timeline</a>
</div>
</main>

<script>
function copy(btn, text){
  navigator.clipboard.writeText(text).then(() => {
    btn.classList.add('ok'); btn.textContent = 'Copied';
    setTimeout(() => { btn.classList.remove('ok'); btn.textContent = 'Copy'; }, 1800);
  }).catch(() => { btn.textContent = 'Press ⌘+C'; });
}
function copyPre(id, btn){
  const text = document.getElementById(id).innerText.replace(/^Copy\s*/, '');
  navigator.clipboard.writeText(text).then(() => {
    btn.classList.add('ok'); btn.textContent = 'Copied';
    setTimeout(() => { btn.classList.remove('ok'); btn.textContent = 'Copy'; }, 1800);
  });
}
</script>
</body></html>
"""


TRASH_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZAI · Trash + Audit</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@1,400;1,500&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap">
<style>
:root{--bg:#0a0508;--surface:#140709;--line:#2a1818;--line-bright:#3a2222;--fg:#f5ecdb;--fg-soft:#d4c3a0;--fg-dim:#8a7a6a;--gold:#e8d49a;--gold-bright:#f5dca3;--gold-deep:#8c6f3a;--red:#a01a1a;--red-bright:#dc2626;--red-warm:#ff7060;--mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;--serif-soft:'Cormorant Garamond',serif}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--fg);font-family:var(--sans);line-height:1.6;min-height:100vh}
a{color:var(--red-warm);text-decoration:none;border-bottom:1px dotted}
a:hover{color:var(--gold-bright);border-bottom-color:var(--gold-bright)}
.hdr{position:sticky;top:0;z-index:50;background:linear-gradient(180deg,rgba(10,5,8,0.96),rgba(10,5,8,0.85));backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
.hdr-inner{max-width:980px;margin:0 auto;display:flex;align-items:center;gap:14px;padding:14px 28px}
.hdr-logo{font-family:'Cinzel',serif;font-weight:600;letter-spacing:.45em;font-size:13px;background:linear-gradient(180deg,var(--gold-bright),var(--gold-deep));-webkit-background-clip:text;background-clip:text;color:transparent;padding-left:.45em;border-bottom:0}
.back{font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-transform:uppercase;border-bottom:0;padding:7px 12px;border:1px solid var(--line-bright);border-radius:2px;margin-left:auto;cursor:pointer}
.wrap{max-width:980px;margin:0 auto;padding:50px 28px 80px}
.eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.4em;color:var(--gold-deep);text-transform:uppercase;margin-bottom:10px}
h1{font-family:var(--serif-soft);font-weight:500;font-style:italic;font-size:38px;color:var(--gold-bright);line-height:1.1;margin-bottom:8px}
.sub{font-family:var(--serif-soft);font-style:italic;font-size:16px;color:var(--fg-soft);margin-bottom:36px}
h2{font-family:var(--serif-soft);font-style:italic;font-size:22px;color:var(--gold);margin:32px 0 14px}
.row{display:grid;grid-template-columns:100px 1fr auto;gap:14px;align-items:start;padding:13px 0;border-top:1px dashed var(--line)}
.row:first-child{border-top:0}
.row .meta{font-family:var(--mono);font-size:10px;letter-spacing:.06em;color:var(--gold-deep);padding-top:3px}
.row .body{min-width:0}
.row .preview{font-family:var(--sans);font-size:13.5px;line-height:1.55;color:var(--fg-soft);overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
.row .sub{font-family:var(--mono);font-size:9.5px;color:var(--muted);letter-spacing:.04em;margin-top:5px;color:var(--fg-dim)}
.row .acts{display:flex;flex-direction:column;gap:6px}
.btn{font-family:var(--mono);font-size:9.5px;letter-spacing:.22em;text-transform:uppercase;padding:6px 11px;border:1px solid var(--line-bright);border-radius:2px;cursor:pointer;background:transparent;transition:.15s;color:var(--gold-deep);text-decoration:none;display:inline-block;border-bottom-style:solid}
.btn:hover{color:var(--gold-bright);border-color:var(--gold-deep)}
.btn.danger{color:var(--red-warm)}
.btn.danger:hover{border-color:var(--red)}
.empty{padding:40px;text-align:center;font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim);font-size:15px}
.audit-row{display:grid;grid-template-columns:140px 100px 100px 1fr;gap:14px;padding:8px 0;border-top:1px dashed var(--line);font-family:var(--mono);font-size:11px;color:var(--fg-soft);letter-spacing:.02em}
.audit-row:first-child{border-top:0}
.audit-row .a{color:var(--gold)}
.audit-row .a.delete{color:var(--red-warm)}
.audit-row .a.restore{color:#5ee2a0}
.audit-row .a.insert{color:var(--fg-dim)}
.audit-row .actor{color:var(--gold-deep)}
.audit-row .targ{color:var(--fg-dim);word-break:break-all}
#toast{position:fixed;bottom:24px;right:24px;z-index:90;padding:13px 20px;background:rgba(20,8,10,0.95);border:1px solid var(--gold-deep);font-family:var(--mono);font-size:11px;letter-spacing:.18em;color:var(--gold-bright);text-transform:uppercase;opacity:0;transform:translateY(20px);transition:.25s;pointer-events:none;border-radius:2px;backdrop-filter:blur(8px)}
#toast.show{opacity:1;transform:translateY(0)}
#toast.err{border-color:var(--red);color:var(--red-warm)}
</style></head>
<body>
<header class="hdr"><div class="hdr-inner">
  <a class="hdr-logo" href="/">ZAI</a>
  <a class="back" href="/">← Back to Hub</a>
</div></header>
<main class="wrap">
<div class="eyebrow">Recoverable deletes + audit</div>
<h1>Trash & Audit</h1>
<p class="sub">Everything an agent or you have soft-deleted, plus the full history of mutations. Soft-deletes restore in one click. Permanent delete is final.</p>

<h2 id="memHead">Deleted memories <span id="memCount" style="font-family:var(--mono);font-size:14px;color:var(--gold-deep);font-style:normal;margin-left:8px"></span></h2>
<div id="memList"><div class="empty">Loading…</div></div>

<h2 id="decHead">Deleted decisions <span id="decCount" style="font-family:var(--mono);font-size:14px;color:var(--gold-deep);font-style:normal;margin-left:8px"></span></h2>
<div id="decList"><div class="empty">Loading…</div></div>

<h2>Audit log · last 100 actions</h2>
<div id="auditList"><div class="empty">Loading…</div></div>

<div id="toast"></div>
</main>
<script>
function esc(s){if(s==null)return'';return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
function trunc(s,n){s=s||'';return s.length>n?s.slice(0,n)+'…':s;}
function timeAgo(iso){if(!iso)return'—';const t=new Date(iso).getTime();const d=Math.max(1,(Date.now()-t)/1000);if(d<60)return Math.floor(d)+'s ago';if(d<3600)return Math.floor(d/60)+'m ago';if(d<86400)return Math.floor(d/3600)+'h ago';return new Date(iso).toLocaleDateString('en-CA',{month:'short',day:'numeric'});}
function toast(m,err){const t=document.getElementById('toast');t.textContent=m;t.classList.toggle('err',!!err);t.classList.add('show');clearTimeout(window.__tT);window.__tT=setTimeout(()=>t.classList.remove('show'),2400);}

async function load(){
  const tr = await fetch('/api/trash').then(r=>r.json()).catch(()=>({memories:[],decisions:[]}));
  const audit = await fetch('/api/audit?n=100').then(r=>r.json()).catch(()=>[]);
  // memories
  const memCount = tr.memories.length;
  document.getElementById('memCount').textContent = memCount;
  document.getElementById('memList').innerHTML = memCount ? tr.memories.map(m=>`
    <div class="row" data-id="${esc(m.id)}">
      <div class="meta">${esc(timeAgo(m.deleted_at))}<br><small style="color:var(--fg-dim)">${esc((m.deleted_by||'').replace('-claude','').toUpperCase())}</small></div>
      <div class="body">
        <div class="preview">${esc(trunc(m.preview||'', 240))}</div>
        <div class="sub">${esc((m.written_by||'').replace('-claude','').toUpperCase())} · imp ${m.importance||3}${(m.tags||[]).length?' · '+m.tags.slice(0,4).map(t=>'#'+t).join(' '):''}</div>
      </div>
      <div class="acts">
        <button class="btn" onclick="restoreMem('${esc(m.id)}', this)">↺ Restore</button>
        <button class="btn danger" onclick="hardDelete('${esc(m.id)}', this)">✕ Forever</button>
      </div>
    </div>`).join('') : '<div class="empty">Nothing in trash.</div>';
  // decisions
  document.getElementById('decCount').textContent = tr.decisions.length;
  document.getElementById('decList').innerHTML = tr.decisions.length ? tr.decisions.map(m=>`
    <div class="row">
      <div class="meta">${esc(timeAgo(m.deleted_at))}</div>
      <div class="body"><div class="preview">${esc(trunc(m.preview||'', 240))}</div><div class="sub">${esc((m.written_by||'').replace('-claude','').toUpperCase())}</div></div>
      <div class="acts"><span class="btn" style="opacity:.5;cursor:default">decisions only restorable via SQL</span></div>
    </div>`).join('') : '<div class="empty">No deleted decisions.</div>';
  // audit
  document.getElementById('auditList').innerHTML = audit.length ? audit.map(a=>`
    <div class="audit-row">
      <span>${esc(timeAgo(a.created_at))}</span>
      <span class="actor">${esc((a.actor||'').replace('-claude','').toUpperCase())}</span>
      <span class="a ${esc(a.action)}">${esc(a.action)}</span>
      <span class="targ">${esc(a.target_kind)}/${esc(a.target_id.slice(0,8))}</span>
    </div>`).join('') : '<div class="empty">No audit entries yet.</div>';
}

async function restoreMem(id, btn){
  btn.disabled = true; btn.textContent = '...';
  try { const r = await fetch('/api/memory/'+id+'/restore',{method:'POST'}); if(!r.ok) throw 0; toast('Memory restored'); load(); }
  catch(e){ toast('Restore failed', true); btn.disabled=false; btn.textContent='↺ Restore'; }
}
async function hardDelete(id, btn){
  if (!confirm('Permanently delete this memory? Cannot be undone.')) return;
  btn.disabled = true; btn.textContent = '...';
  try { const r = await fetch('/api/memory/'+id+'/permanent',{method:'DELETE'}); if(!r.ok) throw 0; toast('Permanently deleted'); load(); }
  catch(e){ toast('Hard delete failed', true); btn.disabled=false; btn.textContent='✕ Forever'; }
}
load();
</script></body></html>
"""


@app.get("/trash", response_class=HTMLResponse)
def trash_route(request: Request):
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0a0508;color:#f5ecdb'>"
            "auth required. /login?key=...</body></html>", status_code=401)
    return HTMLResponse(TRASH_HTML, headers={"Cache-Control": "no-store"})


@app.get("/agent-token", response_class=HTMLResponse)
def agent_token_page(request: Request):
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0a0508;color:#f5ecdb'>"
            "ZAI Memory Hub — auth required. Visit <code>/login?key=YOUR_KEY</code>.</body></html>",
            status_code=401)
    token = _load_agent_token() or "(token file missing — run: python -c \"import secrets; print('zai_'+secrets.token_urlsafe(32))\" > $ZAI_HUB_AGENT_TOKEN_PATH)"
    login_url = f"{PUBLIC_URL}/login?key={DASHBOARD_KEY}"
    html = AGENT_TOKEN_HTML.replace("__AGENT_TOKEN__", token).replace("__LOGIN_URL__", login_url)
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    })


@app.get("/qr/login.png")
def qr_login(request: Request):
    """QR code PNG of the login URL with your master key.  Auth-gated
    so the URL never leaks publicly."""
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        raise HTTPException(401, "auth required")
    import io
    try:
        import qrcode
    except Exception:
        raise HTTPException(500, "qrcode lib not installed in dashboard venv")
    login_url = f"{PUBLIC_URL}/login?key={DASHBOARD_KEY}"
    img = qrcode.make(login_url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png",
                             headers={"Cache-Control": "no-store"})


# ---- Graph API + page ---------------------------------------------

@app.get("/api/graph/{slug}")
def api_graph_entity(slug: str, _: None = Depends(require_auth)):
    """Same data the MCP entity_neighborhood tool returns, but available
    to the dashboard via cookie auth (no bearer needed)."""
    with db() as cx, cx.cursor() as cu:
        cu.execute("SELECT id, slug, kind, display, metadata FROM entities WHERE slug = %s", (slug,))
        root = cu.fetchone()
        if not root:
            raise HTTPException(404, f"unknown entity: {slug}")
        cu.execute("""
            SELECT id::text, substring(content for 200) AS preview, tags, written_by,
                   importance, created_at FROM memories
             WHERE deleted_at IS NULL AND %s = ANY(entity_ids)
             ORDER BY importance DESC, created_at DESC LIMIT 50""", (root["id"],))
        memories = [{**r, "created_at": r["created_at"].isoformat()} for r in cu.fetchall()]
        cu.execute("""
            SELECT id::text, summary, substring(rationale for 200) AS rationale_preview,
                   written_by, created_at FROM decisions
             WHERE deleted_at IS NULL AND %s = ANY(entity_ids)
             ORDER BY created_at DESC LIMIT 30""", (root["id"],))
        decisions = [{**r, "created_at": r["created_at"].isoformat()} for r in cu.fetchall()]
        cu.execute("""
            WITH root_writes AS (
              SELECT entity_ids FROM memories
                WHERE deleted_at IS NULL AND %s = ANY(entity_ids)
              UNION ALL
              SELECT entity_ids FROM decisions
                WHERE deleted_at IS NULL AND %s = ANY(entity_ids))
            SELECT e.id::text, e.slug, e.kind, e.display, count(*)::int AS shared
              FROM root_writes rw, unnest(rw.entity_ids) eid
              JOIN entities e ON e.id = eid
             WHERE e.id <> %s GROUP BY e.id, e.slug, e.kind, e.display
             ORDER BY shared DESC, e.slug LIMIT 30""",
            (root["id"], root["id"], root["id"]))
        neighbors = [dict(r) for r in cu.fetchall()]
    return {
        "root": {"id": str(root["id"]), "slug": root["slug"], "kind": root["kind"],
                 "display": root["display"], "metadata": root["metadata"]},
        "memories": memories, "decisions": decisions, "neighbors": neighbors,
    }


@app.get("/api/entities")
def api_entities(_: None = Depends(require_auth)):
    """List every entity for the graph-explorer landing page."""
    with db() as cx, cx.cursor() as cu:
        cu.execute("""
            SELECT e.id::text, e.slug, e.kind, e.display,
                   (SELECT count(*) FROM memories m
                     WHERE m.deleted_at IS NULL AND e.id = ANY(m.entity_ids)) AS memory_count,
                   (SELECT count(*) FROM decisions d
                     WHERE d.deleted_at IS NULL AND e.id = ANY(d.entity_ids)) AS decision_count
              FROM entities e
             ORDER BY memory_count DESC, e.slug""")
        return [dict(r) for r in cu.fetchall()]


GRAPH_HTML = r"""<!doctype html>
<html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Graph · ZAI Memory Hub</title>
<style>
  :root{--bg:#0a0508;--surface:rgba(20,8,10,0.85);--surface-2:rgba(8,3,6,0.7);
        --line:#3a2a20;--line-bright:#5c3d20;--gold:#c4924a;--gold-bright:#f5dca3;
        --gold-deep:#8b6a5a;--fg:#f5ecdb;--fg-soft:#d4c5a0;--fg-dim:#8b7c68;
        --red:#dc2626;--red-warm:#ef4444;--mono:'JetBrains Mono','SF Mono',Consolas,monospace;
        --serif:Georgia,serif}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;overflow:hidden}
  .nav{display:flex;justify-content:space-between;align-items:center;padding:14px 24px;
       border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11px;letter-spacing:.32em;
       text-transform:uppercase;color:var(--gold-deep);position:fixed;top:0;left:0;right:0;
       background:rgba(10,5,8,0.85);backdrop-filter:blur(10px);z-index:10}
  .nav a{color:var(--gold-deep);text-decoration:none}
  .nav a:hover{color:var(--red-warm)}
  .nav .brand{color:var(--gold-bright);letter-spacing:.4em}
  .layout{display:grid;grid-template-columns:340px 1fr;height:100vh;padding-top:50px}
  .sidebar{background:var(--surface);border-right:1px solid var(--line);overflow-y:auto;padding:20px 22px}
  .eyebrow{font-family:var(--mono);font-size:9.5px;letter-spacing:.36em;color:var(--gold);
           text-transform:uppercase;margin:0 0 6px}
  h1{font-family:var(--serif);font-style:italic;font-size:24px;color:var(--gold-bright);margin:0 0 4px}
  .kind{font-family:var(--mono);font-size:10px;color:var(--red-warm);letter-spacing:.2em;
        text-transform:uppercase}
  .stats{display:flex;gap:14px;margin:14px 0 20px;padding:10px 0;
         border-top:1px dashed var(--line);border-bottom:1px dashed var(--line);
         font-family:var(--mono);font-size:11px;color:var(--gold-deep)}
  .stats strong{color:var(--gold-bright);font-size:16px;display:block}
  .blk{margin:22px 0}
  .blk-head{font-family:var(--mono);font-size:9.5px;letter-spacing:.32em;color:var(--gold);
            text-transform:uppercase;margin-bottom:8px}
  .item{padding:10px 12px;margin-bottom:6px;background:var(--surface-2);border-left:2px solid var(--gold-deep);
        border-radius:0 2px 2px 0;cursor:pointer;transition:.15s}
  .item:hover{background:rgba(40,12,15,0.6);border-left-color:var(--red-warm)}
  .item .t{font-family:var(--serif);font-style:italic;color:var(--gold-bright);font-size:13px;line-height:1.4}
  .item .m{font-family:var(--mono);font-size:9.5px;color:var(--gold-deep);margin-top:4px;letter-spacing:.04em}
  .canvas-wrap{position:relative;background:radial-gradient(ellipse at 50% 50%,rgba(220,38,38,0.05),transparent 70%)}
  svg{width:100%;height:100%}
  .legend{position:absolute;bottom:18px;right:20px;font-family:var(--mono);font-size:10px;
          color:var(--gold-deep);background:rgba(8,3,6,0.85);padding:10px 14px;border:1px solid var(--line);
          border-radius:2px;letter-spacing:.06em;line-height:1.7}
  .legend .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}
  .empty{position:absolute;inset:0;display:grid;place-items:center;text-align:center;
         color:var(--fg-dim);font-family:var(--serif);font-style:italic;font-size:14px;padding:40px}
  .empty a{color:var(--gold-bright);text-decoration:none;border-bottom:1px dashed}
  /* Search */
  .search{margin-bottom:20px}
  .search input{width:100%;padding:9px 11px;background:var(--surface-2);color:var(--gold-bright);
                 border:1px solid var(--line);border-radius:2px;font-family:var(--mono);font-size:12.5px}
  .search input:focus{outline:0;border-color:var(--red-warm)}
  .ent-list{max-height:60vh;overflow-y:auto}
  .ent{display:flex;justify-content:space-between;padding:8px 10px;cursor:pointer;border-bottom:1px solid var(--line);
       transition:.12s}
  .ent:hover{background:rgba(40,12,15,0.4)}
  .ent .slug{font-family:var(--mono);font-size:11.5px;color:var(--gold-bright)}
  .ent .meta{font-family:var(--mono);font-size:9.5px;color:var(--gold-deep)}
  @media(max-width:760px){
    .layout{grid-template-columns:1fr;grid-template-rows:1fr 1fr}
    .sidebar{border-right:none;border-bottom:1px solid var(--line)}
  }
</style></head>
<body>
<nav class='nav'>
  <a href='/'><span class='brand'>Z A I</span> · memory hub</a>
  <div><a href='/'>home</a> · <a href='/connect'>connect</a> · <a href='/trash'>trash</a> · <a href='/agents'>agents</a> · <span style='color:var(--gold-bright)'>graph</span></div>
</nav>
<div class='layout'>
  <aside class='sidebar' id='sidebar'>loading…</aside>
  <div class='canvas-wrap'>
    <svg id='canvas'></svg>
    <div class='legend'>
      <div><span class='dot' style='background:#dc2626'></span>root entity</div>
      <div><span class='dot' style='background:#e8d49a'></span>neighbor entity</div>
      <div><span class='dot' style='background:#7aa6ff'></span>memory</div>
      <div><span class='dot' style='background:#c084ff'></span>decision</div>
    </div>
  </div>
</div>
<script>
const slug = window.location.pathname.split('/').filter(Boolean)[1] || null;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"}[c]));
const trunc = (s, n) => s && s.length > n ? s.slice(0,n)+'…' : (s || '');

async function loadEntityList() {
  const sb = document.getElementById('sidebar');
  const r = await fetch('/api/entities');
  if (!r.ok) { sb.innerHTML = '<div class=empty>Failed to load entities</div>'; return; }
  const ents = await r.json();
  sb.innerHTML = `
    <div class='eyebrow'>knowledge graph</div>
    <h1 style='margin-bottom:18px'>Pick an entity.</h1>
    <p style='color:var(--fg-soft);font-family:var(--serif);font-style:italic;font-size:13.5px;line-height:1.6;margin-bottom:20px'>
      Each entity is a node in the hub.  Click one to see its memories, decisions, and which other entities co-appear in the same writes.
    </p>
    <div class='search'><input id='entSearch' placeholder='filter…' autofocus></div>
    <div class='ent-list' id='entList'>${ents.length ? ents.map(e => `
      <div class='ent' data-slug='${esc(e.slug)}'>
        <div><div class='slug'>${esc(e.slug)}</div><div class='meta'>${esc(e.kind)} · ${esc(e.display||'')}</div></div>
        <div class='meta'>${e.memory_count}m / ${e.decision_count}d</div>
      </div>`).join('') : '<div class=empty>No entities yet. Have an agent call entity.upsert.</div>'}</div>`;
  document.getElementById('entSearch').addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll('#entList .ent').forEach(el => {
      el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  });
  document.querySelectorAll('.ent').forEach(el => {
    el.addEventListener('click', () => { window.location = '/graph/' + el.dataset.slug; });
  });
  document.getElementById('canvas').parentElement.querySelector('.empty')?.remove();
  document.getElementById('canvas').parentElement.insertAdjacentHTML('beforeend',
    '<div class=empty>Pick an entity from the left.</div>');
}

async function loadEntity(slug) {
  const sb = document.getElementById('sidebar');
  const r = await fetch('/api/graph/' + encodeURIComponent(slug));
  if (!r.ok) { sb.innerHTML = `<div class=empty>Entity not found: ${esc(slug)}.<br><br><a href='/graph'>browse all</a></div>`; return; }
  const g = await r.json();
  const mc = g.memories.length, dc = g.decisions.length, nc = g.neighbors.length;
  sb.innerHTML = `
    <div class='eyebrow'>${esc(g.root.kind)} · entity</div>
    <h1>${esc(g.root.display || g.root.slug)}</h1>
    <div class='kind'>${esc(g.root.slug)}</div>
    <div class='stats'>
      <div><strong>${mc}</strong>memories</div>
      <div><strong>${dc}</strong>decisions</div>
      <div><strong>${nc}</strong>neighbors</div>
    </div>
    ${mc ? `<div class='blk'><div class='blk-head'>memories</div>${g.memories.slice(0,10).map(m => `
      <div class='item'><div class='t'>${esc(trunc(m.preview, 110))}</div>
        <div class='m'>${esc(m.written_by)} · imp ${m.importance} · ${esc(new Date(m.created_at).toLocaleString())}</div>
      </div>`).join('')}${mc > 10 ? `<div class='m' style='margin-top:6px'>+ ${mc-10} more</div>` : ''}</div>` : ''}
    ${dc ? `<div class='blk'><div class='blk-head'>decisions</div>${g.decisions.slice(0,5).map(d => `
      <div class='item'><div class='t'>${esc(trunc(d.summary, 110))}</div>
        <div class='m'>${esc(d.written_by)} · ${esc(new Date(d.created_at).toLocaleString())}</div>
      </div>`).join('')}</div>` : ''}
    ${nc ? `<div class='blk'><div class='blk-head'>neighbors</div>${g.neighbors.slice(0,15).map(n => `
      <div class='item' onclick="window.location='/graph/${esc(n.slug)}'">
        <div class='t'>${esc(n.display || n.slug)}</div>
        <div class='m'>${esc(n.kind)} · shared ${n.shared}× · <span style='color:var(--gold-bright)'>${esc(n.slug)}</span></div>
      </div>`).join('')}</div>` : ''}
    <a href='/graph' style='font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--gold-deep);text-decoration:none;text-transform:uppercase'>← back to all entities</a>`;
  drawGraph(g);
}

function drawGraph(g) {
  const svg = document.getElementById('canvas');
  const W = svg.clientWidth, H = svg.clientHeight;
  svg.innerHTML = '';
  const cx = W/2, cy = H/2;
  // Nodes
  const nodes = [{id:'root', label:g.root.display||g.root.slug, kind:'root', x:cx, y:cy, r:18}];
  const links = [];
  g.neighbors.slice(0,12).forEach((n,i) => {
    const angle = (i/Math.max(g.neighbors.slice(0,12).length,1)) * 2*Math.PI;
    nodes.push({id:'n'+i, label:n.display||n.slug, kind:'neighbor', slug:n.slug,
                x: cx + Math.cos(angle)*Math.min(W,H)*0.32,
                y: cy + Math.sin(angle)*Math.min(W,H)*0.32,
                r: 9 + Math.min(n.shared, 5)*1.5});
    links.push({a:'root', b:'n'+i, weight:n.shared});
  });
  g.memories.slice(0,8).forEach((m,i) => {
    const angle = (i/8 + 0.5) * 2*Math.PI;
    nodes.push({id:'m'+i, label:trunc(m.preview, 30), kind:'memory',
                x: cx + Math.cos(angle)*Math.min(W,H)*0.16,
                y: cy + Math.sin(angle)*Math.min(W,H)*0.16,
                r: 4 + m.importance*0.7});
    links.push({a:'root', b:'m'+i, weight:1});
  });
  g.decisions.slice(0,4).forEach((d,i) => {
    const angle = (i/4) * 2*Math.PI;
    nodes.push({id:'d'+i, label:trunc(d.summary, 30), kind:'decision',
                x: cx + Math.cos(angle)*Math.min(W,H)*0.22,
                y: cy + Math.sin(angle)*Math.min(W,H)*0.22,
                r: 6});
    links.push({a:'root', b:'d'+i, weight:1});
  });
  // Render links first
  const COLORS = {root:'#dc2626', neighbor:'#e8d49a', memory:'#7aa6ff', decision:'#c084ff'};
  links.forEach(l => {
    const a = nodes.find(n => n.id === l.a), b = nodes.find(n => n.id === l.b);
    const ln = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    ln.setAttribute('x1', a.x); ln.setAttribute('y1', a.y);
    ln.setAttribute('x2', b.x); ln.setAttribute('y2', b.y);
    ln.setAttribute('stroke', '#5c3d20'); ln.setAttribute('stroke-width', Math.min(l.weight,3));
    ln.setAttribute('stroke-opacity', '0.45');
    svg.appendChild(ln);
  });
  nodes.forEach(n => {
    const grp = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    grp.style.cursor = n.slug ? 'pointer' : 'default';
    if (n.slug) grp.addEventListener('click', () => { window.location = '/graph/' + n.slug; });
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('cx', n.x); c.setAttribute('cy', n.y); c.setAttribute('r', n.r);
    c.setAttribute('fill', COLORS[n.kind]);
    c.setAttribute('stroke', '#0a0508'); c.setAttribute('stroke-width', '2');
    c.style.filter = `drop-shadow(0 0 ${n.r/2}px ${COLORS[n.kind]}88)`;
    grp.appendChild(c);
    const tx = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    tx.setAttribute('x', n.x); tx.setAttribute('y', n.y + n.r + 14);
    tx.setAttribute('text-anchor', 'middle');
    tx.setAttribute('fill', n.kind === 'root' ? '#f5dca3' : '#d4c5a0');
    tx.setAttribute('font-family', "'JetBrains Mono',monospace");
    tx.setAttribute('font-size', n.kind === 'root' ? '13' : '10.5');
    tx.textContent = n.label;
    grp.appendChild(tx);
    svg.appendChild(grp);
  });
}

if (slug) loadEntity(slug);
else loadEntityList();
</script>
</body></html>
"""


@app.get("/graph", response_class=HTMLResponse)
@app.get("/graph/{slug}", response_class=HTMLResponse)
def graph_page(request: Request, slug: str = ""):
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0a0508;color:#f5ecdb'>"
            "ZAI Memory Hub — auth required. Visit <code>/login?key=YOUR_KEY</code>.</body></html>",
            status_code=401)
    return HTMLResponse(GRAPH_HTML, headers={"Cache-Control": "no-store"})


AGENTS_HTML = r"""<!doctype html>
<html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Agents · ZAI Memory Hub</title>
<style>
  :root{--bg:#0a0508;--surface:rgba(20,8,10,0.85);--surface-2:rgba(8,3,6,0.7);
        --line:#3a2a20;--line-bright:#5c3d20;--gold:#c4924a;--gold-bright:#f5dca3;
        --gold-deep:#8b6a5a;--fg:#f5ecdb;--fg-soft:#d4c5a0;--fg-dim:#8b7c68;
        --red:#dc2626;--red-warm:#ef4444;--mono:'JetBrains Mono','SF Mono',Consolas,monospace;
        --serif:Georgia,serif;--sans:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);min-height:100vh;
       background-image:radial-gradient(ellipse at 50% -20%,rgba(220,38,38,0.08),transparent 60%);}
  .nav{display:flex;justify-content:space-between;align-items:center;padding:18px 28px;
       border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11px;letter-spacing:.32em;
       text-transform:uppercase;color:var(--gold-deep)}
  .nav a{color:var(--gold-deep);text-decoration:none;transition:.15s}
  .nav a:hover{color:var(--red-warm)}
  .nav .brand{color:var(--gold-bright);letter-spacing:.4em}
  .wrap{max-width:880px;margin:0 auto;padding:36px 24px 80px}
  .eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.4em;color:var(--gold);
           text-transform:uppercase;margin-bottom:6px}
  h1{font-family:var(--serif);font-style:italic;font-size:34px;color:var(--gold-bright);
     margin:0 0 8px;line-height:1.1}
  .lead{font-family:var(--serif);font-style:italic;font-size:15px;color:var(--fg-soft);
        line-height:1.6;margin:0 0 28px;max-width:60ch}
  .section{margin:36px 0}
  .section-head{font-family:var(--mono);font-size:10px;letter-spacing:.36em;color:var(--gold);
                text-transform:uppercase;margin-bottom:12px;display:flex;align-items:baseline;gap:8px}
  .section-head .count{color:var(--gold-deep);font-size:10px}
  /* Mint form */
  .mint{background:var(--surface);border:1px solid var(--line-bright);border-radius:3px;
        padding:22px 24px;display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .mint label{display:flex;flex-direction:column;gap:5px}
  .mint label.full{grid-column:1/-1}
  .mint label > span{font-family:var(--mono);font-size:9.5px;letter-spacing:.28em;color:var(--gold);
                      text-transform:uppercase}
  .mint label > span em{font-style:italic;letter-spacing:.04em;color:var(--gold-deep);
                         text-transform:none;font-size:10.5px;margin-left:6px}
  .mint input,.mint select{font-family:var(--mono);font-size:13px;color:var(--gold-bright);
                            background:var(--surface-2);border:1px solid var(--line);border-radius:2px;
                            padding:10px 12px}
  .mint input:focus,.mint select:focus{outline:0;border-color:var(--red-warm)}
  .mint .actions{grid-column:1/-1;display:flex;justify-content:flex-end}
  .mint button{font-family:var(--mono);font-size:10px;letter-spacing:.32em;text-transform:uppercase;
               background:linear-gradient(180deg,#dc2626,#9a1212);color:#f5dca3;border:1px solid #dc2626;
               padding:10px 22px;border-radius:2px;cursor:pointer;transition:.15s}
  .mint button:hover{transform:translateY(-1px);box-shadow:0 6px 18px -4px rgba(220,38,38,.4)}
  .mint button:disabled{opacity:.5;cursor:wait}
  /* Token-display modal */
  #tokenShow{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:50;
             padding:24px;align-items:center;justify-content:center}
  #tokenShow.on{display:flex}
  #tokenShow .box{background:var(--surface);border:1px solid var(--red);max-width:580px;
                  border-radius:3px;padding:28px 30px}
  #tokenShow h3{font-family:var(--serif);font-style:italic;color:#f5dca3;margin:0 0 8px;font-size:22px}
  #tokenShow .warn{color:var(--red-warm);font-family:var(--mono);font-size:10px;letter-spacing:.2em;
                    text-transform:uppercase;margin:0 0 18px}
  #tokenShow .token{font-family:var(--mono);font-size:14px;color:var(--gold-bright);
                     background:rgba(220,38,38,0.08);border:1px dashed var(--red);padding:14px;
                     word-break:break-all;line-height:1.5;border-radius:2px;margin-bottom:14px}
  #tokenShow .row{display:flex;gap:10px;justify-content:flex-end}
  #tokenShow .row button{font-family:var(--mono);font-size:10px;letter-spacing:.28em;
                          text-transform:uppercase;padding:9px 18px;border-radius:2px;cursor:pointer}
  #tokenShow .copy{background:var(--surface-2);color:var(--gold);border:1px solid var(--line-bright)}
  #tokenShow .close{background:linear-gradient(180deg,#dc2626,#9a1212);color:#f5dca3;border:1px solid #dc2626}
  /* Table */
  table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--line);
        border-radius:3px;overflow:hidden}
  th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--line);
        font-family:var(--mono);font-size:12px}
  th{background:var(--surface-2);color:var(--gold-deep);font-size:9.5px;letter-spacing:.24em;
     text-transform:uppercase;font-weight:500}
  tr.revoked td{opacity:.45}
  .role-admin{color:var(--red-warm);font-weight:600}
  .role-writer{color:var(--gold-bright)}
  .role-recall-only{color:var(--gold-deep)}
  .role-revoked{color:var(--fg-dim);font-style:italic}
  .ago{color:var(--gold-deep);font-size:10.5px}
  .label-cell{font-family:var(--sans);color:var(--fg-soft);font-size:12.5px}
  .actions-cell{text-align:right}
  .revoke-btn{font-family:var(--mono);font-size:9px;letter-spacing:.18em;text-transform:uppercase;
               padding:5px 11px;background:transparent;color:var(--gold-deep);border:1px solid var(--line);
               border-radius:2px;cursor:pointer;transition:.15s}
  .revoke-btn:hover{color:var(--red-warm);border-color:var(--red)}
  .revoke-btn[disabled]{opacity:.4;cursor:not-allowed}
  .empty{text-align:center;padding:30px;color:var(--fg-dim);font-style:italic;font-family:var(--serif)}
  /* Mobile */
  @media(max-width:680px){
    .mint{grid-template-columns:1fr}
    .nav{padding:14px 18px;font-size:9.5px;letter-spacing:.22em}
    h1{font-size:26px}
    th,td{padding:9px 8px;font-size:11px}
    th:nth-child(3),td:nth-child(3){display:none}
  }
</style></head>
<body>
<nav class='nav'>
  <a href='/'><span class='brand'>Z A I</span> · memory hub</a>
  <div><a href='/'>home</a> · <a href='/connect'>connect</a> · <a href='/trash'>trash</a> · <span style='color:var(--gold-bright)'>agents</span></div>
</nav>
<div class='wrap'>
  <div class='eyebrow'>access control · roles · keys</div>
  <h1>Agents.</h1>
  <p class='lead'>Every agent that connects to the hub holds one bearer token.  Mint a new one for each surface (Claude.ai web, a laptop's Claude Code, Cursor, a custom Python script), give it a role, hand it off.  Revoke from here when an agent should no longer have access.</p>

  <section class='section'>
    <div class='section-head'>mint a new token</div>
    <form class='mint' id='mintForm'>
      <label>
        <span>Slug <em>(stable identity, shows up on dashboard)</em></span>
        <input type='text' id='slug' name='slug' required pattern='[a-z0-9-]+' maxlength='60' placeholder='e.g. cursor-laptop'>
      </label>
      <label>
        <span>Role <em>(what this agent can do)</em></span>
        <select id='role' name='role'>
          <option value='writer' selected>writer — read + write, no delete</option>
          <option value='recall-only'>recall-only — read only</option>
          <option value='admin'>admin — full access including delete</option>
        </select>
      </label>
      <label class='full'>
        <span>Label <em>(human note: who/where this token is going)</em></span>
        <input type='text' id='label' name='label' maxlength='120' placeholder='e.g. Cursor on the laptop'>
      </label>
      <div class='actions'>
        <button type='submit' id='mintBtn'>Mint token</button>
      </div>
    </form>
  </section>

  <section class='section'>
    <div class='section-head'>active tokens <span class='count' id='ttCount'>—</span></div>
    <div id='ttArea'>loading…</div>
  </section>

  <section class='section' id='revokedSec' hidden>
    <div class='section-head'>revoked tokens <span class='count' id='revCount'>—</span></div>
    <div id='revArea'></div>
  </section>
</div>

<!-- show-token modal -->
<div id='tokenShow'>
  <div class='box'>
    <h3 id='tsTitle'>Your new token</h3>
    <p class='warn'>This is the only time you'll see it. Save it now.</p>
    <div class='token' id='tsToken'>—</div>
    <p style='font-family:var(--mono);font-size:10.5px;color:var(--gold-deep);margin:0 0 18px;line-height:1.5'>
      Hand this to the agent.  In Claude Code:  <span style='color:var(--gold-bright)'>claude mcp add zai-hub --transport http --url {PUBLIC_URL}/mcp --header "Authorization: Bearer &lt;token&gt;"</span>
    </p>
    <div class='row'>
      <button class='copy' id='tsCopy'>Copy</button>
      <button class='close' id='tsClose'>I've saved it</button>
    </div>
  </div>
</div>

<script>
const fmtAge = s => {
  if (s == null) return 'never';
  if (s < 60) return Math.floor(s)+'s';
  if (s < 3600) return Math.floor(s/60)+'m';
  if (s < 86400) return Math.floor(s/3600)+'h';
  return Math.floor(s/86400)+'d';
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"}[c]));

async function load() {
  const r = await fetch('/api/tokens');
  if (!r.ok) { document.getElementById('ttArea').innerHTML = '<div class=empty>Failed to load.</div>'; return; }
  const tokens = await r.json();
  const now = Date.now();
  const active = tokens.filter(t => !t.revoked_at);
  const revoked = tokens.filter(t => t.revoked_at);
  document.getElementById('ttCount').textContent = `(${active.length})`;
  if (!active.length) {
    document.getElementById('ttArea').innerHTML = '<div class=empty>No active tokens. Mint one above.</div>';
  } else {
    document.getElementById('ttArea').innerHTML = `
      <table>
        <thead><tr><th>slug</th><th>role</th><th>label</th><th>last used</th><th></th></tr></thead>
        <tbody>${active.map(t => {
          const ago = t.last_used_at ? (now - new Date(t.last_used_at).getTime())/1000 : null;
          return `<tr data-id='${t.id}'>
            <td><strong style='color:var(--gold-bright)'>${esc(t.slug)}</strong></td>
            <td><span class='role-${esc(t.role)}'>${esc(t.role)}</span></td>
            <td class='label-cell'>${esc(t.label || '—')}</td>
            <td class='ago'>${fmtAge(ago)} ago</td>
            <td class='actions-cell'><button class='revoke-btn' data-id='${t.id}' data-slug='${esc(t.slug)}'>Revoke</button></td>
          </tr>`;
        }).join('')}</tbody>
      </table>`;
    document.querySelectorAll('.revoke-btn').forEach(b => b.addEventListener('click', revokeToken));
  }
  if (revoked.length) {
    document.getElementById('revokedSec').hidden = false;
    document.getElementById('revCount').textContent = `(${revoked.length})`;
    document.getElementById('revArea').innerHTML = `
      <table>
        <thead><tr><th>slug</th><th>role</th><th>label</th><th>revoked</th></tr></thead>
        <tbody>${revoked.map(t => {
          const ago = (now - new Date(t.revoked_at).getTime())/1000;
          return `<tr class='revoked'>
            <td>${esc(t.slug)}</td>
            <td><span class='role-revoked'>${esc(t.role)}</span></td>
            <td class='label-cell'>${esc(t.label || '—')}</td>
            <td class='ago'>${fmtAge(ago)} ago</td>
          </tr>`;
        }).join('')}</tbody>
      </table>`;
  }
}

async function revokeToken(ev) {
  const btn = ev.target;
  const id = btn.dataset.id;
  const slug = btn.dataset.slug;
  if (!confirm(`Revoke the token for "${slug}"?\n\nThe agent holding it will get 401 on its next MCP call. This cannot be undone (you'll need to mint a fresh one if you want to give them access again).`)) return;
  btn.disabled = true; btn.textContent = '…';
  const r = await fetch('/api/tokens/' + id + '/revoke', { method: 'POST' });
  if (!r.ok) { alert('Revoke failed'); btn.disabled = false; btn.textContent = 'Revoke'; return; }
  await load();
}

document.getElementById('mintForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = document.getElementById('mintBtn');
  btn.disabled = true; btn.textContent = 'Minting…';
  const fd = new FormData(ev.target);
  const r = await fetch('/api/tokens/mint', { method: 'POST', body: fd });
  if (!r.ok) { alert('Mint failed: ' + (await r.text())); btn.disabled = false; btn.textContent = 'Mint token'; return; }
  const d = await r.json();
  document.getElementById('tsTitle').textContent = 'Token for ' + d.slug + ' (' + d.role + ')';
  document.getElementById('tsToken').textContent = d.token;
  document.getElementById('tokenShow').classList.add('on');
  // Copy button
  document.getElementById('tsCopy').onclick = async () => {
    try { await navigator.clipboard.writeText(d.token); document.getElementById('tsCopy').textContent = 'Copied'; }
    catch (e) { alert('Clipboard unavailable — select + copy manually'); }
  };
  ev.target.reset();
  document.getElementById('role').value = 'writer';
  await load();
  btn.disabled = false; btn.textContent = 'Mint token';
});

document.getElementById('tsClose').addEventListener('click', () => {
  document.getElementById('tokenShow').classList.remove('on');
  document.getElementById('tsCopy').textContent = 'Copy';
});

load();
setInterval(load, 30000);
</script>
</body></html>
"""


@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0a0508;color:#f5ecdb'>"
            "ZAI Memory Hub — auth required. Visit <code>/login?key=YOUR_KEY</code>.</body></html>",
            status_code=401)
    return HTMLResponse(AGENTS_HTML, headers={"Cache-Control": "no-store"})


@app.get("/connect", response_class=HTMLResponse)
def connect(request: Request):
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0a0508;color:#f5ecdb'>"
            "ZAI Memory Hub — auth required. Visit <code>/login?key=YOUR_KEY</code>.</body></html>",
            status_code=401)
    return HTMLResponse(CONNECT_HTML)


@app.get("/universe", response_class=HTMLResponse)
def universe(request: Request):
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#08030a;color:#f5ecdb'>"
            "ZAI Memory Hub — auth required. Visit <code>/login?key=YOUR_KEY</code>.</body></html>",
            status_code=401)
    return HTMLResponse(UNIVERSE_HTML)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """The Blocks Home — primary surface.  Active Agents row +
    Timeline + topical Blocks grid.  Auto-discovers new agents."""
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0a0508;color:#f5ecdb'>"
            "ZAI Memory Hub — auth required. Visit <code>/login?key=YOUR_KEY</code>.</body></html>",
            status_code=401)
    # Always-fresh HTML — single-user dashboard, no upstream cache,
    # but browsers were holding the old version.  Pin to no-store.
    return HTMLResponse(BLOCKS_HTML, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/library", response_class=HTMLResponse)
def library_route(request: Request):
    """Editorial card feed — alternative reading surface."""
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0a0508;color:#f5ecdb'>"
            "ZAI Memory Hub — auth required. Visit <code>/login?key=YOUR_KEY</code>.</body></html>",
            status_code=401)
    return HTMLResponse(LIBRARY_HTML)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """The previous hub (universe-as-portal layout). Demoted but preserved."""
    if request.cookies.get(COOKIE_NAME) != DASHBOARD_KEY:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#08030a;color:#f5ecdb'>"
            "ZAI Memory Hub — auth required. Visit <code>/login?key=YOUR_KEY</code>.</body></html>",
            status_code=401)
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("ZAI_HUB_DASH_HOST", "127.0.0.1"),
                port=int(os.environ.get("ZAI_HUB_DASH_PORT", "8766")))
