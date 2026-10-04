#!/usr/bin/env python3
"""Bootstrap ZAI Memory Hub on an empty managed Postgres database.

Idempotent:
  1) apply db/001..004 migrations
  2) import the private recovery snapshot only when memories is empty
  3) seed the owner's admin bearer from ZAI_HUB_AGENT_TOKEN

The recovery snapshot stays in the private zai-personal-hub repo and is fetched
at boot with a repo-scoped GitHub token.  No private memory data is committed
to the public skeleton repository.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("ZAI_HUB_DSN") or os.environ.get("DATABASE_URL")
if not DSN:
    raise SystemExit("ZAI_HUB_DSN or DATABASE_URL is required")

RECOVERY_REPO = os.environ.get("ZAI_HUB_RECOVERY_REPO", "Zawwarsami16/zai-personal-hub")
RECOVERY_REF = os.environ.get("ZAI_HUB_RECOVERY_REF", "main")
RECOVERY_PATH = os.environ.get("ZAI_HUB_RECOVERY_PATH", "state/hub-export.json")
GH_TOKEN = os.environ.get("ZAI_HUB_GITHUB_TOKEN", "")
ADMIN_TOKEN = os.environ.get("ZAI_HUB_AGENT_TOKEN", "")


def apply_migrations() -> None:
    migrations = sorted((ROOT / "db").glob("00*.sql"))
    with psycopg.connect(DSN, autocommit=True) as cx:
        for path in migrations:
            sql = path.read_text()
            print(f"[bootstrap] applying {path.name}", flush=True)
            # No parameters => psycopg can use the simple protocol for the
            # migration scripts, including PL/pgSQL bodies with semicolons.
            cx.execute(sql, prepare=False)


def memory_count() -> int:
    with psycopg.connect(DSN) as cx, cx.cursor() as cu:
        cu.execute("SELECT count(*) FROM memories")
        return int(cu.fetchone()[0])


def fetch_recovery_snapshot() -> Path:
    if not GH_TOKEN:
        raise RuntimeError("ZAI_HUB_GITHUB_TOKEN is required for first restore")
    from urllib.parse import quote
    url = (
        f"https://api.github.com/repos/{RECOVERY_REPO}/contents/"
        f"{quote(RECOVERY_PATH, safe='/')}?ref={quote(RECOVERY_REF)}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github.raw+json",
            "User-Agent": "zai-memory-hub-render-restore",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        blob = resp.read()
    if len(blob) < 1000:
        raise RuntimeError("recovery snapshot response is unexpectedly small")
    fd, tmp = tempfile.mkstemp(prefix="zai-hub-export-", suffix=".json")
    os.close(fd)
    p = Path(tmp)
    p.write_bytes(blob)
    print(f"[bootstrap] downloaded recovery snapshot: {len(blob):,} bytes", flush=True)
    return p


def import_snapshot(path: Path) -> None:
    env = os.environ.copy()
    env["ZAI_HUB_DSN"] = DSN
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "import_export.py"), str(path)],
        cwd=str(ROOT),
        env=env,
        check=True,
    )


def seed_admin() -> None:
    if not ADMIN_TOKEN:
        print("[bootstrap] WARN: ZAI_HUB_AGENT_TOKEN not set; no bootstrap admin token seeded", flush=True)
        return
    digest = hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest()
    with psycopg.connect(DSN, autocommit=True) as cx, cx.cursor() as cu:
        cu.execute(
            """
            INSERT INTO agent_tokens(token_hash, slug, role, label, issued_via)
            VALUES (%s, 'zawwar-admin', 'admin', 'owner bootstrap token', 'bootstrap')
            ON CONFLICT (token_hash) DO UPDATE
              SET revoked_at = NULL,
                  role = 'admin',
                  slug = 'zawwar-admin',
                  label = EXCLUDED.label
            """,
            (digest,),
        )


def main() -> None:
    apply_migrations()
    n = memory_count()
    print(f"[bootstrap] database currently contains {n} memories", flush=True)
    if n == 0:
        p = fetch_recovery_snapshot()
        try:
            import_snapshot(p)
        finally:
            p.unlink(missing_ok=True)
    else:
        print('[bootstrap] preserving live data; old snapshot import skipped', flush=True)
    seed_admin()
    from scripts.restore_assets import restore_assets
    restore_assets()
    print("[bootstrap] ready", flush=True)


if __name__ == "__main__":
    main()
