#!/usr/bin/env python3
"""Re-import a `zai-hub-export-*.json` dump into a fresh Hub.

Usage:
    ZAI_HUB_DSN='host=... dbname=... user=... password=...' \\
    ./import_export.py path/to/zai-hub-export-YYYYMMDD-HHMMSS.json

What it does:
    1.  Loads the JSON.
    2.  Validates schema_version.
    3.  Inserts entities first (so memories/decisions can refer to slugs).
    4.  Inserts memories + decisions (resolving entity slugs back to UUIDs).
    5.  Inserts interactions + audit_log + tool_calls if present.
    6.  Skips rows whose id already exists (idempotent — safe to re-run).

What it deliberately does NOT do:
    -  Touch static/uploads/.  Tar that directory separately on export
       and untar it on the new host.
    -  Re-create embeddings.  Those rebuild on first memory.recall once
       the new Hub has its Voyage key set.

Run it against a *new* Hub.  Importing into a populated Hub is
supported (UUID uniqueness handles it) but un-tested at scale.
"""
import json, os, sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("ZAI_HUB_DSN")
if not DSN:
    print("ERROR: ZAI_HUB_DSN env var required.", file=sys.stderr)
    sys.exit(1)

if len(sys.argv) != 2:
    print(__doc__, file=sys.stderr)
    sys.exit(1)

path = Path(sys.argv[1])
if not path.exists():
    print(f"ERROR: {path} not found.", file=sys.stderr)
    sys.exit(1)

print(f"[import] reading {path} ({path.stat().st_size:,} bytes)…")
with open(path) as f:
    blob = json.load(f)

if blob.get("schema_version") != "1":
    print(f"ERROR: unsupported schema_version {blob.get('schema_version')!r}",
          file=sys.stderr)
    sys.exit(2)

print(f"[import] exported_at={blob.get('exported_at')} source={blob.get('source')}")


def upsert_entities(cx, rows):
    n = 0
    with cx.cursor() as cu:
        for r in rows:
            cu.execute(
                "INSERT INTO entities(id, slug, kind, display, metadata, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (r["id"], r["slug"], r["kind"], r["display"],
                 json.dumps(r.get("metadata") or {}),
                 r.get("created_at"), r.get("updated_at")))
            n += cu.rowcount
    return n


def slug_to_ids(cu, slugs):
    if not slugs:
        return []
    cu.execute("SELECT id::text, slug FROM entities WHERE slug = ANY(%s)", (slugs,))
    found = {r["slug"]: r["id"] for r in cu.fetchall()}
    return [found[s] for s in slugs if s in found]


def upsert_memories(cx, rows):
    n = 0
    with cx.cursor() as cu:
        for r in rows:
            eids = slug_to_ids(cu, r.get("entity_slugs") or [])
            cu.execute(
                "INSERT INTO memories(id, content, tags, entity_ids, written_by, "
                "session_id, importance, deleted_at, deleted_by, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (r["id"], r["content"], r.get("tags") or [], eids,
                 r["written_by"], r.get("session_id"), r.get("importance") or 3,
                 r.get("deleted_at"), r.get("deleted_by"), r.get("created_at")))
            n += cu.rowcount
    return n


def upsert_decisions(cx, rows):
    n = 0
    with cx.cursor() as cu:
        for r in rows:
            eids = slug_to_ids(cu, r.get("entity_slugs") or [])
            cu.execute(
                "INSERT INTO decisions(id, summary, rationale, alternatives, entity_ids, "
                "written_by, supersedes, deleted_at, deleted_by, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (r["id"], r["summary"], r["rationale"], r.get("alternatives"), eids,
                 r["written_by"], r.get("supersedes"),
                 r.get("deleted_at"), r.get("deleted_by"), r.get("created_at")))
            n += cu.rowcount
    return n


def upsert_interactions(cx, rows):
    n = 0
    with cx.cursor() as cu:
        for r in rows:
            cu.execute(
                "INSERT INTO interactions(id, session_id, surface, summary, metadata, started_at) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s) ON CONFLICT (id) DO NOTHING",
                (r["id"], r.get("session_id"), r["surface"], r.get("summary"),
                 json.dumps(r.get("metadata") or {}), r.get("started_at")))
            n += cu.rowcount
    return n


def append_audit(cx, rows):
    n = 0
    with cx.cursor() as cu:
        for r in rows:
            cu.execute(
                "INSERT INTO audit_log(target_kind, target_id, action, actor, detail, created_at) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
                (r["target_kind"], r["target_id"], r["action"], r.get("actor"),
                 json.dumps(r.get("detail") or {}), r.get("created_at")))
            n += cu.rowcount
    return n


with psycopg.connect(DSN, row_factory=dict_row, autocommit=False) as cx:
    e = upsert_entities(cx, blob.get("entities", []))
    print(f"[import] entities   +{e}")
    m = upsert_memories(cx, blob.get("memories", []))
    print(f"[import] memories   +{m}")
    d = upsert_decisions(cx, blob.get("decisions", []))
    print(f"[import] decisions  +{d}")
    i = upsert_interactions(cx, blob.get("interactions", []))
    print(f"[import] interactions +{i}")
    a = append_audit(cx, blob.get("audit_log", []))
    print(f"[import] audit_log  +{a}")
    cx.commit()

print("[import] done.")
