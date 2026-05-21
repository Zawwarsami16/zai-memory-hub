"""ZAI Memory Hub - FastMCP server.

Auth model (post-migration 003):
  - Every bearer token is a row in `agent_tokens` (token_hash, slug, role).
  - The server stamps `written_by` from the row's slug; the client cannot lie.
  - Each tool declares a required role; tokens without the role get a clean
    403 instead of a tool that silently does nothing.
  - The `tools/list` response is ALSO filtered per role, so a writer-token
    Claude never sees `memory.delete` in its catalog (avoids the "I have
    this tool but it 403s" confusion).

Roles:
  admin        -> all tools
  writer       -> all tools except memory.delete
  recall-only  -> memory.recall + memory.get_recent + context.bootstrap only

Tools exposed (8):
  context.bootstrap   curated one-call orientation for a new agent (cheap)
  memory.add          write a new memory (server-side dedup within 5 min)
  memory.recall       search memories (brief by default; full=True for body)
  memory.get_recent   latest N memories
  memory.delete       soft-delete one of your own memories (admin only)
  decision.log        durable decision with rationale
  entity.upsert       upsert an entity (kind is an enum)
  interaction.log     mark a session boundary or turning point

Transport: streamable HTTP at /mcp on 127.0.0.1:8765 (Caddy proxies it).
Database: Postgres at 127.0.0.1:5432, db `zai_hub`.
"""
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional, Sequence

import psycopg
from psycopg.rows import dict_row
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import TokenVerifier
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from mcp.server.auth.provider import AccessToken
import mcp.types as mt

DB_DSN = os.environ.get(
    "ZAI_HUB_DSN",
    "host=127.0.0.1 dbname=zai_hub user=zai_hub password=zai_hub_dev",
)
SESSION_ID = os.environ.get("ZAI_HUB_SESSION_ID", f"hub-{int(time.time())}")
PUBLIC_URL = os.environ.get("ZAI_HUB_PUBLIC_URL", "https://hub.example.com").rstrip("/")
DEDUP_WINDOW_SEC = 300   # same-author identical content within 5 min collapses


def _conn():
    return psycopg.connect(DB_DSN, row_factory=dict_row, autocommit=True)


# -------------------- AUTH ---------------------------------------

ROLE_SCOPES = {
    "admin":       ["mcp:read", "mcp:write", "mcp:delete"],
    "writer":      ["mcp:read", "mcp:write"],
    "recall-only": ["mcp:read"],
}

# Per-tool required role.  Used by both _require_role at call time AND the
# ToolCatalogFilter middleware at list time, so they stay consistent.
TOOL_ROLES: dict[str, tuple[str, ...]] = {
    "context_bootstrap":  ("admin", "writer", "recall-only"),
    "memory_add":         ("admin", "writer"),
    "memory_recall":      ("admin", "writer", "recall-only"),
    "memory_get_recent":  ("admin", "writer", "recall-only"),
    "decision_log":       ("admin", "writer"),
    "entity_upsert":      ("admin", "writer"),
    "entity_neighborhood": ("admin", "writer", "recall-only"),
    "interaction_log":    ("admin", "writer"),
    "memory_delete":      ("admin",),
}


class AgentTokenVerifier(TokenVerifier):
    """Validates bearer tokens against the agent_tokens table.

    On success returns an AccessToken whose `client_id` is the agent's
    slug and whose `scopes` reflect the role.  The slug is later read
    back inside each tool via _current_actor() to stamp writes.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not token.strip():
            return None
        h = hashlib.sha256(token.encode()).hexdigest()
        try:
            with _conn() as cx, cx.cursor() as cu:
                cu.execute(
                    "SELECT slug, role, label FROM agent_tokens "
                    "WHERE token_hash = %s AND revoked_at IS NULL",
                    (h,))
                row = cu.fetchone()
                if not row:
                    return None
                cu.execute(
                    "UPDATE agent_tokens SET last_used_at = now() WHERE token_hash = %s",
                    (h,))
        except Exception as e:
            print(f"[auth] verify_token error: {e}", file=sys.stderr)
            return None
        scopes = ROLE_SCOPES.get(row["role"], [])
        return AccessToken(
            token=token,
            client_id=row["slug"],
            scopes=scopes,
            expires_at=None,
            resource=None,
            claims={"slug": row["slug"], "role": row["role"], "label": row["label"] or ""},
        )


verifier = AgentTokenVerifier(base_url=PUBLIC_URL)


# -------------------- Tool-catalog filter middleware -----------

def _actor_role_from_headers() -> str:
    """Read the bearer from the current request and resolve to a role.
    Used by both _current_actor (inside tools) and ToolCatalogFilter
    (in middleware).  Returns 'unknown' if no valid token found."""
    hdrs = get_http_headers(include_all=True) or {}
    auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return "unknown"
    token = auth.split(None, 1)[1].strip()
    h = hashlib.sha256(token.encode()).hexdigest()
    try:
        with _conn() as cx, cx.cursor() as cu:
            cu.execute(
                "SELECT role FROM agent_tokens "
                "WHERE token_hash = %s AND revoked_at IS NULL", (h,))
            row = cu.fetchone()
            return row["role"] if row else "unknown"
    except Exception:
        return "unknown"


class ToolCatalogFilter(Middleware):
    """Hide tools the caller's role cannot use from tools/list.

    The role enforcement at tools/call (via _require_role) still applies
    defensively; this middleware is purely about not advertising tools
    Claude can't call, which would otherwise lead to a 'try-then-403'
    pattern.
    """

    async def on_list_tools(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Sequence:
        tools = await call_next(context)
        try:
            role = _actor_role_from_headers()
        except Exception:
            return tools
        if role == "admin":
            return tools
        out = []
        for t in tools:
            allowed = TOOL_ROLES.get(t.name, ())
            if role in allowed:
                out.append(t)
        return out


mcp = FastMCP(
    "zai-memory-hub",
    instructions=(
        "ZAI Memory Hub: a Postgres-backed shared memory for one human and "
        "many AI agents.  At connection time, call context.bootstrap(your_slug) "
        "ONCE to load orientation in a single round-trip - it's cheaper than "
        "three separate calls.  Append rather than overwrite.  Use decision.log "
        "for any course correction other agents should respect."
    ),
    auth=verifier,
    middleware=[ToolCatalogFilter()],
)


def _current_actor() -> dict:
    """Pull the validated bearer out of the current request and resolve it
    to a slug/role.  Always called inside tool bodies, never at import
    time.  Returns {'slug': str, 'role': str, 'label': str}."""
    hdrs = get_http_headers(include_all=True) or {}
    auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth.split(None, 1)[1].strip()
        h = hashlib.sha256(token.encode()).hexdigest()
        try:
            with _conn() as cx, cx.cursor() as cu:
                cu.execute(
                    "SELECT slug, role, label FROM agent_tokens "
                    "WHERE token_hash = %s AND revoked_at IS NULL", (h,))
                row = cu.fetchone()
                if row:
                    return {"slug": row["slug"], "role": row["role"],
                            "label": row["label"] or ""}
        except Exception as e:
            print(f"[auth] _current_actor error: {e}", file=sys.stderr)
    return {"slug": "unknown", "role": "writer", "label": ""}


def _require_role(actor: dict, *allowed: str) -> Optional[dict]:
    if actor["role"] in allowed:
        return None
    return {
        "ok": False,
        "error": "forbidden",
        "detail": (f"role '{actor['role']}' cannot use this tool. "
                   f"Required: one of {list(allowed)}. "
                   f"Ask the hub owner to upgrade your token."),
    }


def _log_tool_call(tool: str, args: dict, brief: str, duration_ms: int,
                   actor: dict, status: str = "ok", error: Optional[str] = None):
    try:
        with _conn() as cx, cx.cursor() as cu:
            cu.execute(
                "INSERT INTO tool_calls(tool_name, args, result_brief, called_by, "
                "session_id, duration_ms, status, error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (tool, json.dumps(args), brief[:500] if brief else None,
                 actor["slug"], SESSION_ID, duration_ms, status, error),
            )
    except Exception as e:
        print(f"[hub] tool_call log failed: {e}", file=sys.stderr)


def _resolve_entities(slugs: list[str]) -> list[str]:
    if not slugs:
        return []
    with _conn() as cx, cx.cursor() as cu:
        cu.execute("SELECT id, slug FROM entities WHERE slug = ANY(%s)", (slugs,))
        found = {r["slug"]: str(r["id"]) for r in cu.fetchall()}
        ids = []
        for s in slugs:
            if s in found:
                ids.append(found[s])
                continue
            cu.execute(
                "INSERT INTO entities(slug, kind, display) VALUES (%s, 'thread', %s) RETURNING id",
                (s, s),
            )
            ids.append(str(cu.fetchone()["id"]))
        return ids


# -------------------- Secret-leak detection ----------------------
# Compiled list of patterns that look like credentials.  When a write
# hits one, the call is REJECTED unless the caller passes
# `acknowledge_unsafe=True` (which is logged to audit).  We deliberately
# don't include the matched string in the response or audit detail — the
# whole point is to keep secrets out of the hub.

import re as _re
_SECRET_PATTERNS = [
    (_re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}'),       'Anthropic API key (sk-ant-)'),
    (_re.compile(r'\bsk-proj-[A-Za-z0-9_-]{20,}'),      'OpenAI project key (sk-proj-)'),
    (_re.compile(r'\bsk-[A-Za-z0-9]{20,}'),              'OpenAI-style API key (sk-)'),
    (_re.compile(r'\br8_[A-Za-z0-9_-]{20,}'),            'Replicate token (r8_)'),
    (_re.compile(r'\bzai_[A-Za-z0-9_-]{30,}'),           'ZAI Hub bearer token (zai_)'),
    (_re.compile(r'\bghp_[A-Za-z0-9]{30,}'),             'GitHub PAT (ghp_)'),
    (_re.compile(r'\bgho_[A-Za-z0-9]{30,}'),             'GitHub OAuth token (gho_)'),
    (_re.compile(r'\bghs_[A-Za-z0-9]{30,}'),             'GitHub App secret (ghs_)'),
    (_re.compile(r'\bAIza[A-Za-z0-9_-]{30,}'),           'Google API key (AIza)'),
    (_re.compile(r'\bAKIA[A-Z0-9]{16}'),                 'AWS access key (AKIA)'),
    (_re.compile(r'\bASIA[A-Z0-9]{16}'),                 'AWS STS key (ASIA)'),
    (_re.compile(r'\bxox[bpars]-[A-Za-z0-9-]{20,}'),     'Slack token (xox)'),
    (_re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY( BLOCK)?-----'),
                                                          'PEM/PGP private key block'),
    (_re.compile(r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}'),
                                                          'JWT-like token (eyJ...)'),
    (_re.compile(r'(?i)\bpassword\s*[:=]\s*["\']?[^\s"\'\n]{6,}'),
                                                          'password = ... assignment'),
    (_re.compile(r'(?i)\bapi[_-]?key\s*[:=]\s*["\']?[A-Za-z0-9_-]{16,}'),
                                                          'api_key = ... assignment'),
    (_re.compile(r'(?i)\bbearer\s+[A-Za-z0-9_\-\.]{20,}'),
                                                          'Bearer ... token in text'),
]


def _scan_for_secrets(content: str) -> list[str]:
    """Return a list of matched secret KINDS (not the matched text) found
    in `content`.  Empty list = clean."""
    if not content:
        return []
    found = []
    for pat, label in _SECRET_PATTERNS:
        if pat.search(content):
            found.append(label)
    return found


def _summarize(content: str, max_len: int = 200) -> str:
    """Brief preview of a memory body for the default recall response.
    Cuts at the first paragraph break, sentence end, or hard char limit."""
    if not content:
        return ""
    s = content.strip()
    # Prefer a paragraph break
    para = s.split("\n\n", 1)[0]
    if len(para) <= max_len:
        return para
    # Fall back to sentence end
    for sep in (". ", "! ", "? "):
        idx = s[:max_len].rfind(sep)
        if idx > max_len // 2:
            return s[:idx + 1]
    return s[:max_len] + "..."


# ==================== MCP tools ==================================

@mcp.tool()
def context_bootstrap(your_slug: Optional[str] = None) -> dict:
    """One-call orientation for a connecting agent.

    USE THIS FIRST when you connect to a hub for the very first time in
    a session.  Returns everything you need to orient yourself in a
    single round-trip: the last 5 decisions, the 10 most recent memories
    overall, your own last 5 writes if any, who else is active right
    now, and role-appropriate guidance.

    DON'T USE THIS REPEATEDLY.  Call it once per session.  If you need
    a refresh later, use memory.recall or memory.get_recent.

    HOW: pass `your_slug` if you want the response to highlight your
    own recent writes specifically.  Otherwise the server uses the slug
    bound to your auth token.

    Returns: {
        "you": {"slug", "role", "label"},
        "house_rules": "<one-paragraph guidance for your role>",
        "recent_decisions": [{summary, rationale, written_by, created_at}, ...],
        "recent_memories":  [{id, summary, tags, written_by, created_at}, ...],
        "your_recent":      [{id, summary, tags, created_at}, ...],
        "active_agents":    [{slug, role, last_seen_age_s}, ...],
        "stats":            {"total_memories", "total_decisions", "your_writes"}
    }
    """
    t0 = time.time()
    actor = _current_actor()
    err = _require_role(actor, "admin", "writer", "recall-only")
    if err: return err

    slug = your_slug or actor["slug"]
    rules_by_role = {
        "admin": (
            "You are an admin token holder.  You can read, write, and "
            "soft-delete any memory.  Lead by example: write sharp memories, "
            "decision_log course-corrections, soft-delete your own test noise."
        ),
        "writer": (
            "You are a writer.  You can read everything and write memories, "
            "decisions, interactions, and entities.  You cannot delete - "
            "if something needs to go, write a decision.log explaining why "
            "and the human will handle deletion from /trash."
        ),
        "recall-only": (
            "You are a read-only consumer.  You can recall memories and "
            "see recent activity.  You cannot write.  Use this context to "
            "inform your responses; don't try to add anything."
        ),
    }
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(
            "SELECT id::text, summary, rationale, written_by, created_at "
            "FROM decisions WHERE deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 5")
        decisions = [{**r, "created_at": r["created_at"].isoformat()}
                     for r in cu.fetchall()]
        cu.execute(
            "SELECT id::text, content, tags, written_by, importance, created_at "
            "FROM memories WHERE deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 10")
        memories = [{"id": r["id"], "summary": _summarize(r["content"]),
                     "tags": r["tags"], "written_by": r["written_by"],
                     "importance": r["importance"],
                     "created_at": r["created_at"].isoformat()}
                    for r in cu.fetchall()]
        cu.execute(
            "SELECT id::text, content, tags, importance, created_at "
            "FROM memories WHERE deleted_at IS NULL AND written_by = %s "
            "ORDER BY created_at DESC LIMIT 5", (slug,))
        your_recent = [{"id": r["id"], "summary": _summarize(r["content"]),
                        "tags": r["tags"], "importance": r["importance"],
                        "created_at": r["created_at"].isoformat()}
                       for r in cu.fetchall()]
        cu.execute(
            "SELECT slug, role, last_used_at FROM agent_tokens "
            "WHERE revoked_at IS NULL AND last_used_at > now() - interval '1 hour' "
            "ORDER BY last_used_at DESC LIMIT 10")
        now = time.time()
        active = [{"slug": r["slug"], "role": r["role"],
                   "last_seen_age_s": int(now - r["last_used_at"].timestamp())
                       if r["last_used_at"] else None}
                  for r in cu.fetchall()]
        cu.execute("""SELECT
            (SELECT count(*) FROM memories WHERE deleted_at IS NULL) AS total_memories,
            (SELECT count(*) FROM decisions WHERE deleted_at IS NULL) AS total_decisions,
            (SELECT count(*) FROM memories WHERE deleted_at IS NULL AND written_by = %s) AS your_writes""",
            (slug,))
        stats = cu.fetchone()
    ms = int((time.time() - t0) * 1000)
    out = {
        "you": {"slug": actor["slug"], "role": actor["role"], "label": actor["label"]},
        "house_rules": rules_by_role.get(actor["role"], rules_by_role["writer"]),
        "recent_decisions": decisions,
        "recent_memories": memories,
        "your_recent": your_recent,
        "active_agents": active,
        "stats": dict(stats),
    }
    _log_tool_call("context.bootstrap",
                   {"your_slug": your_slug}, f"oriented {actor['slug']}", ms, actor)
    return out


@mcp.tool()
def memory_add(content: str, tags: Optional[list[str]] = None,
               entities: Optional[list[str]] = None, importance: int = 3,
               acknowledge_unsafe: bool = False) -> dict:
    """Save a memory to the shared hub.

    USE THIS WHEN you learn or decide something that future-you or another
    agent should know about across sessions: a fact, a finding, a piece of
    user context, a draft, a snippet of plan, a "I tried X and it failed
    because Y".  Anything you'd reach for later or want a teammate to see.

    DON'T USE THIS FOR: ephemeral chat turns, restating what the user just
    said, your own scratch reasoning, log lines that have no value in 30
    days.  If the value is < 30 days, lower the importance.

    HOW: lead with the conclusion in the first sentence (~110 chars max -
    that's the headline shown on the dashboard).  Then add detail below.
    Tag with the relevant vocabulary (see AGENTS.md for the canonical tag
    list per knowledge block).  Set importance honestly:
      1 = ephemeral  (probably soft-delete later)
      2 = soon-irrelevant
      3 = normal log entry  (default)
      4 = worth re-reading
      5 = load-bearing, must-not-be-lost

    DEDUP: if you (same slug) added identical content within the last 5
    minutes, this returns the existing id instead of a duplicate row.
    Safe to retry on transient errors.

    SECRET-SCANNER (server-side): if `content` looks like it contains an
    API key, password, JWT, private key, or similar credential, the
    write is REJECTED with a list of detected pattern kinds and no
    secret stored anywhere.  If you genuinely need to save such content
    (e.g. documenting a key format with a fake example), pass
    `acknowledge_unsafe=True` — the write succeeds and the override is
    logged to audit_log.  Default is False (safe).

    The server stamps written_by from your auth token; you cannot
    override it.

    Returns: {"id", "created_at", "deduped": bool}.
    """
    t0 = time.time()
    actor = _current_actor()
    err = _require_role(actor, "admin", "writer")
    if err: return err

    # Secret scanner — reject before dedup/insert
    secrets_found = _scan_for_secrets(content)
    if secrets_found and not acknowledge_unsafe:
        # Log the rejection to audit so the human knows an agent tried
        try:
            with _conn() as cx, cx.cursor() as cu:
                cu.execute(
                    "INSERT INTO audit_log (target_kind, target_id, action, actor, detail) "
                    "VALUES ('memory', '00000000-0000-0000-0000-000000000000', "
                    "'secret_blocked', %s, %s)",
                    (actor["slug"], json.dumps({
                        "detected": secrets_found,
                        "content_preview": content[:40] + "...",
                        "content_length": len(content),
                    })))
        except Exception:
            pass
        return {
            "ok": False,
            "error": "credentials_detected",
            "detail": ("Content appears to contain credentials. Storing keys/"
                       "passwords/tokens in the memory hub is a security risk. "
                       "Detected: " + ", ".join(secrets_found) + ". "
                       "If this is intentional (e.g. documenting a format with "
                       "a fake example), retry with acknowledge_unsafe=true."),
            "detected": secrets_found,
        }

    tags = tags or []
    entity_ids = _resolve_entities(entities or [])
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    with _conn() as cx, cx.cursor() as cu:
        # Dedup: same author, identical content, within window
        cu.execute(
            "SELECT id, created_at FROM memories "
            "WHERE written_by = %s AND deleted_at IS NULL "
            "AND encode(sha256(convert_to(content, 'UTF8')), 'hex') = %s "
            "AND created_at > now() - interval '%s seconds' "
            "ORDER BY created_at DESC LIMIT 1",
            (actor["slug"], content_hash, DEDUP_WINDOW_SEC))
        existing = cu.fetchone()
        if existing:
            ms = int((time.time() - t0) * 1000)
            out = {"id": str(existing["id"]),
                   "created_at": existing["created_at"].isoformat(),
                   "deduped": True}
            _log_tool_call("memory.add",
                           {"tags": tags, "importance": importance,
                            "content_preview": content[:80]},
                           f"dedup id={out['id']}", ms, actor)
            return out
        cu.execute(
            "INSERT INTO memories(content, tags, entity_ids, written_by, session_id, importance) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, created_at",
            (content, tags, entity_ids, actor["slug"], SESSION_ID, importance),
        )
        row = cu.fetchone()
    ms = int((time.time() - t0) * 1000)
    out = {"id": str(row["id"]), "created_at": row["created_at"].isoformat(),
           "deduped": False}
    _log_tool_call("memory.add",
                   {"tags": tags, "entities": entities, "importance": importance,
                    "content_preview": content[:80]},
                   f"id={out['id']}", ms, actor)
    return out


@mcp.tool()
def memory_recall(query: str, k: int = 5, tags: Optional[list[str]] = None,
                  entity: Optional[str] = None, full: bool = False) -> dict:
    """Search the shared hub for memories matching a query.

    USE THIS BEFORE you assume nothing exists about a topic, or before
    spending time on something another agent might already have worked on.
    Pass natural-language queries; the hub does substring match today and
    will upgrade to semantic when a Voyage key is configured.

    DEFAULT RESPONSE IS BRIEF: each hit returns a ~200-char summary, not
    the full body.  This saves tokens on the common case.  If you need
    the full content of a specific memory, pass `full=True` (returns
    the entire body for each hit - more expensive).

    HOW: `k` defaults to 5 (max 50).  Filter by `tags` (must overlap with
    memory's tags) or `entity` (e.g. 'zai-memory-hub') to narrow.  Results
    come back ranked by importance then recency.

    Returns: {"hits": [{"id", "summary"|"content", "tags", "written_by",
    "importance", "created_at"}, ...]}.  Empty hits is meaningful:
    nothing on this topic has been recorded.
    """
    t0 = time.time()
    actor = _current_actor()
    err = _require_role(actor, "admin", "writer", "recall-only")
    if err: return err

    k = max(1, min(k, 50))
    sql = ["SELECT id, content, tags, written_by, importance, created_at "
           "FROM memories WHERE deleted_at IS NULL AND content ILIKE %s"]
    params: list = [f"%{query}%"]
    if tags:
        sql.append("AND tags && %s"); params.append(tags)
    if entity:
        ids = _resolve_entities([entity])
        if ids:
            sql.append("AND entity_ids && %s"); params.append(ids)
    sql.append("ORDER BY importance DESC, created_at DESC LIMIT %s")
    params.append(k)
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(" ".join(sql), params)
        rows = cu.fetchall()
    ms = int((time.time() - t0) * 1000)
    result = []
    for r in rows:
        item = {"id": str(r["id"]), "tags": r["tags"],
                "written_by": r["written_by"], "importance": r["importance"],
                "created_at": r["created_at"].isoformat()}
        if full:
            item["content"] = r["content"]
        else:
            item["summary"] = _summarize(r["content"])
        result.append(item)
    _log_tool_call("memory.recall",
                   {"query": query, "k": k, "tags": tags, "entity": entity, "full": full},
                   f"hits={len(result)}", ms, actor)
    return {"hits": result, "mode": "full" if full else "summary"}


@mcp.tool()
def memory_get_recent(n: int = 10, written_by: Optional[str] = None,
                      tag: Optional[str] = None, full: bool = False) -> dict:
    """Get the latest N memories from the hub (default 10, max 100).

    USE THIS AT THE START of any session to load context: what's been
    happening, who else is active, what other agents have been working on.
    Read these before deciding what to do, so you don't duplicate work
    or contradict a recent decision.

    PREFER context.bootstrap FOR FIRST-CONNECTION ORIENTATION.  It bundles
    recent decisions + memories + your own writes + active agents into one
    cheaper call.  Use memory.get_recent for follow-ups within a session.

    HOW: pass `written_by` to filter by author slug, `tag` to filter by a
    single tag, `full=True` for full bodies (default is brief summaries).
    No filter = everything.

    Returns: {"memories": [...]} in descending created_at order.
    """
    t0 = time.time()
    actor = _current_actor()
    err = _require_role(actor, "admin", "writer", "recall-only")
    if err: return err

    n = max(1, min(n, 100))
    sql = ["SELECT id, content, tags, written_by, importance, created_at "
           "FROM memories WHERE deleted_at IS NULL"]
    params: list = []
    if written_by:
        sql.append("AND written_by = %s"); params.append(written_by)
    if tag:
        sql.append("AND %s = ANY(tags)"); params.append(tag)
    sql.append("ORDER BY created_at DESC LIMIT %s")
    params.append(n)
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(" ".join(sql), params)
        rows = cu.fetchall()
    ms = int((time.time() - t0) * 1000)
    result = []
    for r in rows:
        item = {"id": str(r["id"]), "tags": r["tags"],
                "written_by": r["written_by"], "importance": r["importance"],
                "created_at": r["created_at"].isoformat()}
        if full:
            item["content"] = r["content"]
        else:
            item["summary"] = _summarize(r["content"])
        result.append(item)
    _log_tool_call("memory.get_recent",
                   {"n": n, "written_by": written_by, "tag": tag, "full": full},
                   f"returned={len(result)}", ms, actor)
    return {"memories": result, "mode": "full" if full else "summary"}


@mcp.tool()
def decision_log(summary: str, rationale: str, alternatives: Optional[str] = None,
                 entities: Optional[list[str]] = None, supersedes: Optional[str] = None) -> dict:
    """Record a durable decision that other agents should respect.

    USE THIS WHEN you (or the human together with you) chose a path that
    other agents could otherwise undo or re-litigate: a pivot, an
    abandoned approach, a chosen tradeoff, a "we're not doing X, we're
    doing Y".  Decisions are how agents stay coherent across time and
    surfaces.

    DON'T USE THIS FOR: tactical micro-choices that won't affect anyone
    else, or things that are obviously implied by the code already.

    HOW: `summary` is the headline ("Sticking with Postgres LISTEN/NOTIFY,
    not Redis").  `rationale` is the WHY in 2-4 sentences.
    `alternatives` lists what you rejected and a short reason.  Pass
    `supersedes` with the prior decision's UUID if you're revising it.

    Returns: {"id": "<uuid>", "created_at": "<iso8601>"}.
    """
    t0 = time.time()
    actor = _current_actor()
    err = _require_role(actor, "admin", "writer")
    if err: return err

    # Secret scanner sweeps summary + rationale + alternatives
    combined = "\n".join(filter(None, [summary, rationale, alternatives or ""]))
    secrets_found = _scan_for_secrets(combined)
    if secrets_found:
        return {
            "ok": False,
            "error": "credentials_detected",
            "detail": ("Decision content appears to contain credentials. "
                       "Detected: " + ", ".join(secrets_found) + ". "
                       "Rewrite without the secret value (e.g. 'rotated the "
                       "GitHub PAT' instead of pasting the token)."),
            "detected": secrets_found,
        }

    entity_ids = _resolve_entities(entities or [])
    sup = uuid.UUID(supersedes) if supersedes else None
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO decisions(summary, rationale, alternatives, entity_ids, written_by, supersedes) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, created_at",
            (summary, rationale, alternatives, entity_ids, actor["slug"], sup),
        )
        row = cu.fetchone()
    ms = int((time.time() - t0) * 1000)
    out = {"id": str(row["id"]), "created_at": row["created_at"].isoformat()}
    _log_tool_call("decision.log",
                   {"summary": summary[:80], "entities": entities, "supersedes": supersedes},
                   f"id={out['id']}", ms, actor)
    return out


EntityKind = Literal["person", "project", "thread", "repo", "location", "concept", "event"]


@mcp.tool()
def entity_upsert(slug: str, kind: EntityKind, display: str,
                  metadata: Optional[dict] = None) -> dict:
    """Create or update an entity that memories can be ABOUT.

    USE THIS WHEN you find yourself referring to the same thing across
    multiple memories.  Turn it into an entity, then attach memories via
    the `entities` field on memory.add / decision.log.  This is how the
    dashboard's relation graph works.

    KIND IS AN ENUM (server-validated, no free-form strings):
      person     a real human (Zawwar, a teammate, a contact)
      project    a buildable thing (zai-memory-hub, anteroom-studio)
      thread     an ongoing conversation, plan, or work-stream
      repo       a git repository
      location   a physical or logical place
      concept    an abstract idea you want to attach memories to
      event      a discrete time-bounded happening (a demo, a launch)

    HOW: `slug` is the stable id (kebab-case, no spaces, max 60 chars).
    `display` is the human-readable name.  `metadata` is freeform JSON
    (links, descriptions).  Re-calling upsert with the same slug updates
    in place; the entity's id is stable across upserts.

    Returns: {"id", "slug", "kind", "display"}.
    """
    t0 = time.time()
    actor = _current_actor()
    err = _require_role(actor, "admin", "writer")
    if err: return err

    import re
    slug_clean = re.sub(r'[^a-z0-9-]', '-', slug.lower()).strip('-')[:60]
    if not slug_clean:
        return {"ok": False, "error": "invalid slug after normalization"}
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO entities(slug, kind, display, metadata) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (slug) DO UPDATE SET kind=EXCLUDED.kind, display=EXCLUDED.display, "
            "metadata=EXCLUDED.metadata, updated_at=now() RETURNING id, created_at, updated_at",
            (slug_clean, kind, display, json.dumps(metadata or {})),
        )
        row = cu.fetchone()
    ms = int((time.time() - t0) * 1000)
    out = {"id": str(row["id"]), "slug": slug_clean, "kind": kind, "display": display}
    _log_tool_call("entity.upsert", {"slug": slug_clean, "kind": kind},
                   f"id={out['id']}", ms, actor)
    return out


@mcp.tool()
def entity_neighborhood(slug: str, depth: int = 1,
                        include_decisions: bool = True,
                        memory_limit: int = 25) -> dict:
    """Walk the hub's knowledge graph starting at one entity.

    USE THIS WHEN you need to understand everything connected to a
    project / person / thread.  Example: "Show me everything about
    `anteroom-studio`" returns the entity itself, every memory + decision
    that references it, every other entity that co-appears with it in
    those memories (one hop), and a count of how the graph is shaped.
    A much richer answer than memory.recall("anteroom") in a single call.

    DON'T USE THIS FOR: a text search ("anything about Bitcoin").  Use
    memory.recall for free-text.  This tool is entity-anchored.

    HOW:
      slug             entity slug to start from (e.g. "zai-memory-hub")
      depth            1 = direct neighbors only (default).  2 = also
                        return co-mentioned entities and their writes.
      include_decisions   add decisions linked to this entity (default true)
      memory_limit     max memories to return per hop (default 25, max 100)

    Returns: {
        "root":              {slug, kind, display, metadata, id},
        "memories":          [{id, summary, tags, written_by, importance, created_at}, ...],
        "decisions":         [{id, summary, written_by, created_at}, ...],
        "neighbors":         [{slug, kind, display, shared_memories}, ...],
        "depth":             int,
        "stats":             {memory_count, decision_count, neighbor_count}
    }
    """
    t0 = time.time()
    actor = _current_actor()
    err = _require_role(actor, "admin", "writer", "recall-only")
    if err: return err

    memory_limit = max(1, min(memory_limit, 100))
    depth = max(1, min(depth, 2))

    with _conn() as cx, cx.cursor() as cu:
        # Resolve the root entity
        cu.execute(
            "SELECT id, slug, kind, display, metadata FROM entities WHERE slug = %s",
            (slug,))
        root = cu.fetchone()
        if not root:
            ms = int((time.time() - t0) * 1000)
            _log_tool_call("entity.neighborhood",
                           {"slug": slug, "depth": depth},
                           "entity_not_found", ms, actor)
            return {"ok": False, "error": "entity_not_found",
                    "detail": f"No entity with slug '{slug}'.  Try memory.recall to find related text first, then entity.upsert."}
        root_id = root["id"]

        # Memories that reference this entity
        cu.execute("""
            SELECT id, content, tags, written_by, importance, created_at
              FROM memories
             WHERE deleted_at IS NULL AND %s = ANY(entity_ids)
             ORDER BY importance DESC, created_at DESC LIMIT %s""",
            (root_id, memory_limit))
        mem_rows = cu.fetchall()
        memories = [{"id": str(r["id"]), "summary": _summarize(r["content"]),
                     "tags": r["tags"], "written_by": r["written_by"],
                     "importance": r["importance"],
                     "created_at": r["created_at"].isoformat()}
                    for r in mem_rows]

        # Decisions
        decisions = []
        if include_decisions:
            cu.execute("""
                SELECT id, summary, rationale, written_by, created_at
                  FROM decisions
                 WHERE deleted_at IS NULL AND %s = ANY(entity_ids)
                 ORDER BY created_at DESC LIMIT %s""",
                (root_id, memory_limit))
            decisions = [{"id": str(r["id"]), "summary": r["summary"],
                          "rationale_preview": _summarize(r["rationale"] or ""),
                          "written_by": r["written_by"],
                          "created_at": r["created_at"].isoformat()}
                         for r in cu.fetchall()]

        # Neighbor entities (depth=1): any other entity that appears in
        # the same memory/decision as the root entity.
        cu.execute("""
            WITH root_writes AS (
              SELECT entity_ids FROM memories
                WHERE deleted_at IS NULL AND %s = ANY(entity_ids)
              UNION ALL
              SELECT entity_ids FROM decisions
                WHERE deleted_at IS NULL AND %s = ANY(entity_ids)
            ),
            cooccurring AS (
              SELECT unnest(entity_ids) AS eid, count(*)::int AS shared
                FROM root_writes
               GROUP BY 1
            )
            SELECT e.id::text, e.slug, e.kind, e.display, c.shared
              FROM cooccurring c JOIN entities e ON e.id = c.eid
             WHERE e.id <> %s
             ORDER BY c.shared DESC, e.slug LIMIT 30
        """, (root_id, root_id, root_id))
        neighbors = [{"id": r["id"], "slug": r["slug"], "kind": r["kind"],
                      "display": r["display"], "shared_writes": r["shared"]}
                     for r in cu.fetchall()]

        # Optional second hop: writes that mention any of the neighbors
        # but NOT the root.  Cheap signal that "these things are linked
        # via X" even if root isn't tagged.
        hop2 = []
        if depth >= 2 and neighbors:
            neighbor_ids = [n["id"] for n in neighbors[:10]]   # cap to 10 brightest
            cu.execute("""
                SELECT id, content, tags, written_by, importance, created_at
                  FROM memories
                 WHERE deleted_at IS NULL
                   AND entity_ids && %s
                   AND NOT (%s = ANY(entity_ids))
                 ORDER BY importance DESC, created_at DESC LIMIT %s""",
                (neighbor_ids, root_id, memory_limit))
            hop2 = [{"id": str(r["id"]), "summary": _summarize(r["content"]),
                     "tags": r["tags"], "written_by": r["written_by"],
                     "importance": r["importance"],
                     "created_at": r["created_at"].isoformat()}
                    for r in cu.fetchall()]

    ms = int((time.time() - t0) * 1000)
    out = {
        "root": {"id": str(root["id"]), "slug": root["slug"],
                 "kind": root["kind"], "display": root["display"],
                 "metadata": root["metadata"]},
        "memories": memories,
        "decisions": decisions,
        "neighbors": neighbors,
        "depth": depth,
        "stats": {
            "memory_count": len(memories),
            "decision_count": len(decisions),
            "neighbor_count": len(neighbors),
            "hop2_count": len(hop2),
        },
    }
    if depth >= 2:
        out["hop2_memories"] = hop2
    _log_tool_call("entity.neighborhood",
                   {"slug": slug, "depth": depth},
                   f"mem={len(memories)} dec={len(decisions)} nbr={len(neighbors)}",
                   ms, actor)
    return out


@mcp.tool()
def interaction_log(surface: str, summary: Optional[str] = None,
                    metadata: Optional[dict] = None) -> dict:
    """Mark a session start, end, or turning point.

    USE THIS AT SESSION START to announce yourself - "agent X connected,
    picking up task Y".  Other agents reading the recent activity stream
    will see you and avoid duplicating work.  Also call this when you
    finish a meaningful chunk so the activity timeline reflects what
    actually happened.

    DON'T USE THIS FOR: every single tool call (use memory.add for
    content); or for hourly heartbeats (it'll spam the timeline).

    HOW: `surface` is what kind of agent you are ("claude-ai-web",
    "local-claude", "cursor-laptop", etc).  `summary` is one sentence -
    what you're starting or finishing.  `metadata` is freeform JSON.

    Returns: {"id": "<uuid>", "session_id", "started_at"}.
    """
    t0 = time.time()
    actor = _current_actor()
    err = _require_role(actor, "admin", "writer")
    if err: return err

    with _conn() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO interactions(session_id, surface, summary, metadata) "
            "VALUES (%s,%s,%s,%s) RETURNING id, started_at",
            (SESSION_ID, surface, summary, json.dumps(metadata or {})),
        )
        row = cu.fetchone()
    ms = int((time.time() - t0) * 1000)
    out = {"id": str(row["id"]), "session_id": SESSION_ID,
           "started_at": row["started_at"].isoformat()}
    _log_tool_call("interaction.log",
                   {"surface": surface, "summary": summary[:80] if summary else None},
                   f"id={out['id']}", ms, actor)
    return out


@mcp.tool()
def memory_delete(id: str, reason: Optional[str] = None) -> dict:
    """SOFT-delete a memory you wrote earlier.  Recoverable from /trash.

    USE THIS WHEN you wrote something that turned out to be wrong, was a
    test, or shouldn't have been recorded.  Soft delete removes it from
    all feeds and search results but keeps the row for the human to
    review.

    HARD CONSTRAINTS (server-enforced):
      - You can only delete memories whose written_by matches YOUR slug.
        Trying to delete another agent's memory returns an error.  If you
        really think a foreign memory should go, write a decision.log
        first and let the human handle it via the dashboard.
      - Only callable by tokens with role 'admin'.  Writer tokens don't
        see this tool at all (filtered from tools/list).  The human's
        dashboard is the permanent-delete surface.

    HOW: pass the memory `id` (UUID from memory.add return), and a
    `reason` so future-you knows why.  Reason ends up in audit_log.

    Returns: {"ok": true, "id", "soft_deleted": true} on success.
    {"ok": false, "error": "..."} on failure.
    """
    t0 = time.time()
    actor = _current_actor()
    err = _require_role(actor, "admin")
    if err: return err

    with _conn() as cx, cx.cursor() as cu:
        cu.execute("SELECT written_by FROM memories WHERE id = %s AND deleted_at IS NULL",
                   (id,))
        row = cu.fetchone()
        if not row:
            return {"ok": False, "error": "memory not found or already deleted"}
        if actor["role"] != "admin" and row["written_by"] != actor["slug"]:
            return {"ok": False, "error": "you can only delete memories you wrote"}
        cu.execute(
            "UPDATE memories SET deleted_at = now(), deleted_by = %s "
            "WHERE id = %s AND deleted_at IS NULL RETURNING id::text",
            (actor["slug"], id))
        if not cu.fetchone():
            return {"ok": False, "error": "delete failed (concurrent modification?)"}
        cu.execute(
            "INSERT INTO audit_log (target_kind, target_id, action, actor, detail) "
            "VALUES ('memory', %s, 'soft_delete', %s, %s)",
            (id, actor["slug"],
             json.dumps({"reason": reason or "",
                         "original_author": row["written_by"]})))
    ms = int((time.time() - t0) * 1000)
    out = {"ok": True, "id": id, "soft_deleted": True}
    _log_tool_call("memory.delete",
                   {"id": id, "reason": (reason or "")[:80]}, "ok", ms, actor)
    return out


if __name__ == "__main__":
    mcp.run()  # STDIO transport
