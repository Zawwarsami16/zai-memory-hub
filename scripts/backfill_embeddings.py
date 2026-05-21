#!/usr/bin/env python3
"""Generate Voyage embeddings for every memory that doesn't have one.

Runs once after dropping voyage.token, plus any time the embedding model
or dimension changes.  Batches 8 memories per Voyage call (API accepts
up to 128 inputs per request; 8 is friendly on smaller hubs).

Usage:  scripts/backfill_embeddings.py
        scripts/backfill_embeddings.py --force   # re-embed everything
"""
import json, os, sys, time
from pathlib import Path
import urllib.request, urllib.error

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get(
    "ZAI_HUB_DSN",
    "host=127.0.0.1 dbname=zai_hub user=zai_hub password=zai_hub_dev")
TOKEN_PATH = Path(os.environ.get(
    "VOYAGE_TOKEN_PATH",
    Path(__file__).resolve().parent.parent / "auth" / "voyage.token"))
MODEL = os.environ.get("VOYAGE_MODEL", "voyage-3.5-lite")
BATCH = 8

force = "--force" in sys.argv

if not TOKEN_PATH.exists():
    print(f"FATAL: {TOKEN_PATH} missing", file=sys.stderr); sys.exit(1)
KEY = TOKEN_PATH.read_text().strip()


def embed_batch(texts):
    body = json.dumps({"input": texts, "model": MODEL, "input_type": "document"}).encode()
    req = urllib.request.Request(
        "https://api.voyageai.com/v1/embeddings",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    return [d["embedding"] for d in resp["data"]], resp.get("usage", {})


def to_vec(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as cx:
    with cx.cursor() as cu:
        where = "WHERE deleted_at IS NULL"
        if not force:
            where += " AND embedding IS NULL"
        cu.execute(f"SELECT id, content FROM memories {where} ORDER BY created_at")
        rows = cu.fetchall()
    print(f"[backfill] {len(rows)} memories to embed (model={MODEL}, batch={BATCH})")
    total_tokens = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        texts = [(r["content"] or "")[:8000] for r in chunk]
        try:
            vectors, usage = embed_batch(texts)
        except urllib.error.HTTPError as e:
            print(f"[backfill] HTTP {e.code} on batch {i}: {e.read().decode()[:200]}")
            time.sleep(2)
            continue
        total_tokens += usage.get("total_tokens", 0)
        with cx.cursor() as cu:
            for r, v in zip(chunk, vectors):
                cu.execute(
                    "UPDATE memories SET embedding = %s::vector WHERE id = %s",
                    (to_vec(v), r["id"]))
        done = min(i + BATCH, len(rows))
        print(f"[backfill] {done}/{len(rows)}  batch_tokens={usage.get('total_tokens','?')}  cumulative={total_tokens}")
    print(f"[backfill] done.  total tokens used: {total_tokens}")
