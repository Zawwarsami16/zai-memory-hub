"""Import an immutable private asset snapshot once, transactionally.

Does not extract archive paths to disk and never fetches recovery credentials.
All restored bytes stay in Postgres and private exports, not the public repo.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import psycopg
from server.assets import clean_path, dsn, insert_asset

SNAPSHOT_REF = '29376a5d02cd3b0ca37c4dbd6106804cd57d6982'
ARCHIVE_SHA256 = '6547ec17964a39ea9e9b68b3840df4e42f04cf116a4665e184219c1753ceb063'
SOURCE = 'private-recovery-assets:' + SNAPSHOT_REF


def archive_assets(blob: bytes):
    if hashlib.sha256(blob).hexdigest() != ARCHIVE_SHA256:
        raise ValueError('recovery archive checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as archive:
        total = 0
        for member in archive:
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError('archive links and special files are not allowed')
            path = clean_path(member.name)
            total += member.size
            if member.size > 20 * 1024**2 or total > 100 * 1024**2:
                raise ValueError('archive exceeds recovery size limit')
            yield path, archive.extractfile(member).read()


def fetch(path: str, *, api=False) -> bytes:
    repo = os.environ.get('ZAI_HUB_RECOVERY_REPO', 'Zawwarsami16/zai-personal-hub')
    token = os.environ['ZAI_HUB_GITHUB_TOKEN']
    url = (f'https://api.github.com/repos/{repo}/{path}' if api else
           f'https://api.github.com/repos/{repo}/contents/{quote(path, safe="/")}?ref={SNAPSHOT_REF}')
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json' if api else 'application/vnd.github.raw+json',
        'User-Agent': 'zai-hub-asset-recovery',
    })
    with urllib.request.urlopen(req, timeout=90) as response:
        data = response.read(100 * 1024**2 + 1)
    if len(data) > 100 * 1024**2:
        raise ValueError('recovery response too large')
    return data


def restore_assets() -> None:
    with psycopg.connect(dsn()) as cx:
        if cx.execute('SELECT 1 FROM hub_asset_imports WHERE source=%s', (SOURCE,)).fetchone():
            print('[assets] verified recovery already imported', flush=True)
            return
    # Download and verify outside the transaction, then atomically insert all files.
    assets = list(archive_assets(fetch('state/uploads.tgz')))
    tree = json.loads(fetch(f'git/trees/{SNAPSHOT_REF}?recursive=1', api=True))
    if tree.get('truncated'):
        raise ValueError('recovery tree was truncated')
    prefix = 'code/dashboard/static/'
    entries = [x for x in tree['tree'] if x['type'] == 'blob' and x['path'].startswith(prefix + 'gen/')]

    def download(entry):
        data = fetch(entry['path'])
        git_sha = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if git_sha != entry['sha']:
            raise ValueError('generated media checksum mismatch')
        return clean_path(entry['path'][len(prefix):]), data

    with ThreadPoolExecutor(max_workers=4) as pool:
        assets.extend(pool.map(download, entries))
    with psycopg.connect(dsn()) as cx:
        for path, data in assets:
            insert_asset(cx, path, data)
        cx.execute('INSERT INTO hub_asset_imports(source,asset_count) VALUES (%s,%s) '
                   'ON CONFLICT (source) DO NOTHING', (SOURCE, len(assets)))
    print(f'[assets] restored and verified {len(assets)} files', flush=True)


if __name__ == '__main__':
    restore_assets()
