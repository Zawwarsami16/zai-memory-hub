#!/usr/bin/env python3
"""Nightly off-host backup of the ZAI Memory Hub to Backblaze B2.

Runs from cron. What it does:
  1. pg_dump | gzip -> /tmp/zai_hub-YYYY-MM-DD.sql.gz
  2. Uploads to b2://<bucket>/postgres/
  3. Mirrors dashboard/static/uploads/ + /gen/ into the same bucket
  4. Optional Telegram ping with a one-line summary
  5. Deletes postgres dumps older than RETENTION_DAYS

Reads all credentials and paths from environment variables, which can
be set via .env or a systemd EnvironmentFile=. See .env.example.
"""
import os, sys, subprocess, time, json
from datetime import datetime, timezone
from pathlib import Path

import urllib.request


def env_or_die(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.stderr.write(f"FATAL: env var {name} is required\n")
        sys.exit(2)
    return v


B2_KEY_ID   = env_or_die("B2_KEY_ID")
B2_APP_KEY  = env_or_die("B2_APPLICATION_KEY")
B2_BUCKET   = env_or_die("B2_BUCKET_NAME")

# Repo root resolved relative to this script (script lives at scripts/).
REPO_ROOT   = Path(__file__).resolve().parent.parent
UPLOADS_DIR = Path(os.environ.get("ZAI_HUB_UPLOADS_DIR", REPO_ROOT / "dashboard" / "static" / "uploads"))
GEN_DIR     = Path(os.environ.get("ZAI_HUB_GEN_DIR",     REPO_ROOT / "dashboard" / "static" / "gen"))
LOG         = Path(os.environ.get("ZAI_HUB_BACKUP_LOG",  REPO_ROOT / "logs" / "backup.log"))
LOG.parent.mkdir(parents=True, exist_ok=True)

RETENTION_DAYS = int(os.environ.get("ZAI_HUB_BACKUP_RETENTION_DAYS", "30"))
TS = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def log(msg: str):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n"
    sys.stdout.write(line)
    with open(LOG, "a") as f:
        f.write(line)


from b2sdk.v2 import InMemoryAccountInfo, B2Api  # type: ignore

info = InMemoryAccountInfo()
b2_api = B2Api(info)
b2_api.authorize_account("production", B2_KEY_ID, B2_APP_KEY)
bucket = b2_api.get_bucket_by_name(B2_BUCKET)
log(f"authorized B2, bucket={B2_BUCKET}")


def upload_local_file(local_path: Path, remote_key: str):
    ctype = "application/octet-stream"
    ext = local_path.suffix.lower()
    if ext in (".sql.gz", ".gz"): ctype = "application/gzip"
    elif ext == ".pdf":           ctype = "application/pdf"
    elif ext in (".jpg", ".jpeg"): ctype = "image/jpeg"
    elif ext == ".png":           ctype = "image/png"
    elif ext == ".mp4":           ctype = "video/mp4"
    elif ext == ".json":          ctype = "application/json"
    bucket.upload_local_file(
        local_file=str(local_path),
        file_name=remote_key,
        content_type=ctype,
    )


def b2_list(prefix=""):
    for vf, _ in bucket.ls(folder_to_list=prefix, latest_only=True, recursive=True):
        yield vf.file_name, vf.id_, vf.upload_timestamp, vf.size


def delete_older_than(prefix: str, days: int):
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    deleted = 0; freed = 0
    for name, fid, ts, size in b2_list(prefix):
        if ts < cutoff_ms:
            log(f"  delete (age>{days}d): {name}")
            bucket.delete_file_version(fid, name)
            deleted += 1; freed += size
    return deleted, freed


# ----- 1. pg_dump -------------------------------------------------------
DUMP_LOCAL = Path(f"/tmp/zai_hub-{TS}.sql.gz")
env_pg = os.environ.copy()
# Use the same DSN parts the dashboard uses; falls back to localhost+default.
pg_host = os.environ.get("ZAI_HUB_PG_HOST", "127.0.0.1")
pg_user = os.environ.get("ZAI_HUB_PG_USER", "zai_hub")
pg_db   = os.environ.get("ZAI_HUB_PG_DB",   "zai_hub")
pg_pass = os.environ.get("ZAI_HUB_PG_PASSWORD")
if pg_pass:
    env_pg["PGPASSWORD"] = pg_pass
log(f"pg_dump {pg_host}/{pg_db} as {pg_user} -> {DUMP_LOCAL}")
with open(DUMP_LOCAL, "wb") as out:
    p1 = subprocess.Popen(
        ["pg_dump", "-h", pg_host, "-U", pg_user, "-d", pg_db, "--no-owner", "--no-acl"],
        env=env_pg, stdout=subprocess.PIPE)
    p2 = subprocess.Popen(["gzip", "-9"], stdin=p1.stdout, stdout=out)
    p1.stdout.close()
    p2.communicate()
    if p1.wait() != 0:
        log("FATAL: pg_dump failed"); sys.exit(1)

dump_bytes = DUMP_LOCAL.stat().st_size
log(f"  dump size: {dump_bytes/1024:.1f} KB")

remote_key = f"postgres/zai_hub-{TS}.sql.gz"
upload_local_file(DUMP_LOCAL, remote_key)
log(f"  uploaded -> b2://{B2_BUCKET}/{remote_key}")
DUMP_LOCAL.unlink(missing_ok=True)


# ----- 2. Mirror uploads/ + gen/ ---------------------------------------
def mirror_dir(local_dir: Path, remote_prefix: str):
    if not local_dir.exists():
        return 0, 0
    existing = set()
    for name, _fid, _ts, _size in b2_list(remote_prefix):
        existing.add(name)
    uploaded = 0; total_bytes = 0
    for p in local_dir.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        remote_key = f"{remote_prefix}{p.name}"
        if remote_key in existing:
            continue
        try:
            upload_local_file(p, remote_key)
            uploaded += 1
            total_bytes += p.stat().st_size
            log(f"  +{remote_key} ({p.stat().st_size/1024:.0f} KB)")
        except Exception as e:
            log(f"  FAILED {remote_key}: {e}")
    return uploaded, total_bytes


log("mirroring uploads/ + gen/")
up_n, up_b   = mirror_dir(UPLOADS_DIR, "uploads/")
gen_n, gen_b = mirror_dir(GEN_DIR,     "gen/")
log(f"  uploads/: +{up_n} files ({up_b/1024:.0f} KB)")
log(f"  gen/:     +{gen_n} files ({gen_b/1024:.0f} KB)")


# ----- 3. Purge old postgres dumps -------------------------------------
log(f"purging postgres dumps older than {RETENTION_DAYS} days")
del_n, del_b = delete_older_than("postgres/", RETENTION_DAYS)
log(f"  purged {del_n} dump(s), freed {del_b/1024:.0f} KB")


# ----- 4. Optional Telegram summary ------------------------------------
def telegram(msg: str):
    tok  = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        body = json.dumps({"chat_id": chat, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log(f"telegram ping failed: {e}")


summary = (
    f"ZAI Hub backup OK | {TS} | "
    f"db={dump_bytes/1024:.0f}KB uploads=+{up_n} gen=+{gen_n} purged={del_n}"
)
log(summary)
telegram(summary)
