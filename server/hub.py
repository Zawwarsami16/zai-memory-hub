"""ZAI Memory Hub — FastMCP server (v0 scaffold).

Tools:
  memory.recall       semantic recall by query (until embeddings are wired: ILIKE fallback)
  memory.add          insert a memory, optional tags + entities + importance
  memory.get_recent   last N memories (optionally filtered by tag or written_by)
  decision.log        durable choice with rationale
  entity.upsert       create/update an entity by slug
  interaction.log     mark a session start/end / turning point

Connection: STDIO by default (Claude Code MCP). HTTP transport added later for remote consumers.
Database: Postgres at 127.0.0.1:5432, db `zai_hub`, user `zai_hub`.
"""
import json, os, sys, time, uuid
from datetime import datetime, timezone
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from fastmcp import FastMCP

DB_DSN = os.environ.get(
    "ZAI_HUB_DSN",
    "host=127.0.0.1 dbname=zai_hub user=zai_hub password=zai_hub_dev",
)
SESSION_ID = os.environ.get("ZAI_HUB_SESSION_ID", f"vps-{int(time.time())}")
WRITTEN_BY = os.environ.get("ZAI_HUB_WRITTEN_BY", "vps-claude")

mcp = FastMCP("zai-memory-hub")


def _conn():
    return psycopg.connect(DB_DSN, row_factory=dict_row, autocommit=True)


def _log_tool_call(tool: str, args: dict, brief: str, duration_ms: int, status: str = "ok", error: Optional[str] = None):
    try:
        with _conn() as cx, cx.cursor() as cu:
            cu.execute(
                "INSERT INTO tool_calls(tool_name, args, result_brief, called_by, session_id, duration_ms, status, error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (tool, json.dumps(args), brief[:500] if brief else None, WRITTEN_BY, SESSION_ID, duration_ms, status, error),
            )
    except Exception as e:
        # never let logging failures break the tool itself
        print(f"[hub] tool_call log failed: {e}", file=sys.stderr)


def _resolve_entities(slugs: list[str]) -> list[str]:
    """Resolve entity slugs to UUIDs, creating unknown ones as 'thread' kind on the fly."""
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


# ---------------- MCP tools -----------------------------------

@mcp.tool()
def memory_add(content: str, tags: Optional[list[str]] = None,
               entities: Optional[list[str]] = None, importance: int = 3) -> dict:
    """Add a memory. `entities` are slugs; unknown ones are auto-created."""
    t0 = time.time()
    tags = tags or []
    entity_ids = _resolve_entities(entities or [])
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO memories(content, tags, entity_ids, written_by, session_id, importance) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, created_at",
            (content, tags, entity_ids, WRITTEN_BY, SESSION_ID, importance),
        )
        row = cu.fetchone()
    ms = int((time.time() - t0) * 1000)
    out = {"id": str(row["id"]), "created_at": row["created_at"].isoformat()}
    _log_tool_call("memory.add", {"tags": tags, "entities": entities, "importance": importance,
                                  "content_preview": content[:80]}, f"id={out['id']}", ms)
    return out


@mcp.tool()
def memory_recall(query: str, k: int = 5, tags: Optional[list[str]] = None,
                  entity: Optional[str] = None) -> dict:
    """Recall memories. v0: ILIKE substring + tag/entity filter. Semantic embedding recall lands week 2."""
    t0 = time.time()
    sql = ["SELECT id, content, tags, written_by, importance, created_at FROM memories WHERE content ILIKE %s"]
    params: list = [f"%{query}%"]
    if tags:
        sql.append("AND tags && %s")
        params.append(tags)
    if entity:
        ids = _resolve_entities([entity])
        if ids:
            sql.append("AND entity_ids && %s")
            params.append(ids)
    sql.append("ORDER BY importance DESC, created_at DESC LIMIT %s")
    params.append(k)
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(" ".join(sql), params)
        rows = cu.fetchall()
    ms = int((time.time() - t0) * 1000)
    result = [{"id": str(r["id"]), "content": r["content"], "tags": r["tags"],
               "written_by": r["written_by"], "importance": r["importance"],
               "created_at": r["created_at"].isoformat()} for r in rows]
    _log_tool_call("memory.recall", {"query": query, "k": k, "tags": tags, "entity": entity},
                   f"hits={len(result)}", ms)
    return {"hits": result}


@mcp.tool()
def memory_get_recent(n: int = 10, written_by: Optional[str] = None,
                      tag: Optional[str] = None) -> dict:
    """Most recent memories, optionally filtered."""
    t0 = time.time()
    sql = ["SELECT id, content, tags, written_by, importance, created_at FROM memories WHERE TRUE"]
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
    result = [{"id": str(r["id"]), "content": r["content"], "tags": r["tags"],
               "written_by": r["written_by"], "importance": r["importance"],
               "created_at": r["created_at"].isoformat()} for r in rows]
    _log_tool_call("memory.get_recent", {"n": n, "written_by": written_by, "tag": tag},
                   f"returned={len(result)}", ms)
    return {"memories": result}


@mcp.tool()
def decision_log(summary: str, rationale: str, alternatives: Optional[str] = None,
                 entities: Optional[list[str]] = None, supersedes: Optional[str] = None) -> dict:
    """Log a durable decision. If revising, pass the prior decision's id as `supersedes`."""
    t0 = time.time()
    entity_ids = _resolve_entities(entities or [])
    sup = uuid.UUID(supersedes) if supersedes else None
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO decisions(summary, rationale, alternatives, entity_ids, written_by, supersedes) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, created_at",
            (summary, rationale, alternatives, entity_ids, WRITTEN_BY, sup),
        )
        row = cu.fetchone()
    ms = int((time.time() - t0) * 1000)
    out = {"id": str(row["id"]), "created_at": row["created_at"].isoformat()}
    _log_tool_call("decision.log",
                   {"summary": summary[:80], "entities": entities, "supersedes": supersedes},
                   f"id={out['id']}", ms)
    return out


@mcp.tool()
def entity_upsert(slug: str, kind: str, display: str,
                  metadata: Optional[dict] = None) -> dict:
    """Create or update an entity by slug."""
    t0 = time.time()
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO entities(slug, kind, display, metadata) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (slug) DO UPDATE SET kind=EXCLUDED.kind, display=EXCLUDED.display, "
            "metadata=EXCLUDED.metadata, updated_at=now() RETURNING id, created_at, updated_at",
            (slug, kind, display, json.dumps(metadata or {})),
        )
        row = cu.fetchone()
    ms = int((time.time() - t0) * 1000)
    out = {"id": str(row["id"]), "slug": slug, "kind": kind, "display": display}
    _log_tool_call("entity.upsert", {"slug": slug, "kind": kind}, f"id={out['id']}", ms)
    return out


@mcp.tool()
def interaction_log(surface: str, summary: Optional[str] = None,
                    metadata: Optional[dict] = None) -> dict:
    """Mark an interaction (session start, key turning point). `surface` = vps-cli / local-cli / claude.ai-web / mobile."""
    t0 = time.time()
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(
            "INSERT INTO interactions(session_id, surface, summary, metadata) "
            "VALUES (%s,%s,%s,%s) RETURNING id, started_at",
            (SESSION_ID, surface, summary, json.dumps(metadata or {})),
        )
        row = cu.fetchone()
    ms = int((time.time() - t0) * 1000)
    out = {"id": str(row["id"]), "session_id": SESSION_ID, "started_at": row["started_at"].isoformat()}
    _log_tool_call("interaction.log", {"surface": surface, "summary": summary[:80] if summary else None},
                   f"id={out['id']}", ms)
    return out


@mcp.tool()
def memory_delete(id: str, reason: Optional[str] = None) -> dict:
    """Soft-delete a memory.  ALWAYS soft — restorable via the dashboard /trash.

    Use this ONLY for memories *you* wrote that turned out to be wrong
    or test noise.  Do NOT delete memories written by other agents
    without coordinating via a decision_log first.  Always include
    `reason`.  Soft-deleted memories are excluded from feeds but the
    full row stays in Postgres until a human hard-deletes it from
    /trash.
    """
    t0 = time.time()
    with _conn() as cx, cx.cursor() as cu:
        cu.execute(
            "UPDATE memories SET deleted_at = now(), deleted_by = %s "
            "WHERE id = %s AND deleted_at IS NULL "
            "RETURNING id::text, written_by",
            (WRITTEN_BY, id))
        row = cu.fetchone()
        if not row:
            return {"ok": False, "error": "memory not found or already deleted"}
        cu.execute(
            "INSERT INTO audit_log (target_kind, target_id, action, actor, detail) "
            "VALUES ('memory', %s, 'soft_delete', %s, %s)",
            (id, WRITTEN_BY,
             json.dumps({"reason": reason or "", "original_author": row["written_by"]})))
    ms = int((time.time() - t0) * 1000)
    out = {"ok": True, "id": id, "soft_deleted": True}
    _log_tool_call("memory.delete",
                   {"id": id, "reason": (reason or "")[:80]}, "ok", ms)
    return out


if __name__ == "__main__":
    mcp.run()  # STDIO transport
