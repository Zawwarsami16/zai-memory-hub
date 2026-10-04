"""Portable asset storage; static disk files are disposable cached copies."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import secrets
from pathlib import Path, PurePosixPath

import psycopg
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.staticfiles import StaticFiles


def clean_path(path: str) -> str:
    parts = PurePosixPath(path).parts
    if (not parts or parts[0] not in ('uploads', 'gen') or len(parts) < 2
            or '\\' in path or any(p in ('.', '..') for p in path.split('/'))
            or path.startswith('/') or '\x00' in path):
        raise ValueError('invalid asset path')
    return str(PurePosixPath(path))


def dsn() -> str:
    return os.environ.get('ZAI_HUB_DSN') or os.environ['DATABASE_URL']


def insert_asset(cx, path: str, data: bytes) -> None:
    """Never silently replace a different historical file at the same path."""
    path = clean_path(path)
    sha = hashlib.sha256(data).hexdigest()
    media = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    cx.execute(
        'INSERT INTO hub_assets(path,data,sha256,media_type) VALUES (%s,%s,%s,%s) '
        'ON CONFLICT (path) DO NOTHING', (path, data, sha, media))
    row = cx.execute('SELECT sha256 FROM hub_assets WHERE path=%s', (path,)).fetchone()
    actual = row['sha256'] if isinstance(row, dict) else row[0]
    if actual != sha:
        raise ValueError('asset path already contains different bytes')


def save_asset(path: str, data: bytes) -> None:
    # Commit before the calling upload endpoint creates a memory reference.
    with psycopg.connect(dsn()) as cx:
        insert_asset(cx, path, data)


def materialize(path: str, directory: str) -> bool:
    path = clean_path(path)
    dest = Path(directory) / path
    if dest.is_file():
        return True
    with psycopg.connect(dsn()) as cx:
        row = cx.execute('SELECT data,sha256 FROM hub_assets WHERE path=%s', (path,)).fetchone()
    if row is None:
        return False
    data = bytes(row[0])
    if hashlib.sha256(data).hexdigest() != row[1]:
        raise ValueError('stored asset checksum mismatch')
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return True


class DurableStaticFiles(StaticFiles):
    def __init__(self, *, directory: str, dashboard_key: str, cookie_name: str):
        super().__init__(directory=directory)
        self.dashboard_key = dashboard_key
        self.cookie_name = cookie_name

    async def get_response(self, path: str, scope):
        if path.startswith(('uploads/', 'gen/')):
            try:
                path = clean_path(path)
            except ValueError:
                raise HTTPException(404)
            if path.startswith('uploads/'):
                supplied = Request(scope).cookies.get(self.cookie_name, '')
                if not secrets.compare_digest(supplied, self.dashboard_key):
                    raise HTTPException(401, 'dashboard login required')
            await run_in_threadpool(materialize, path, str(self.directory))
        response = await super().get_response(path, scope)
        if path.startswith('uploads/'):
            response.headers['Cache-Control'] = 'private, no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            # Companions may contain arbitrary HTML; never grant same-origin scripts.
            if path.endswith('.html'):
                response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        return response
