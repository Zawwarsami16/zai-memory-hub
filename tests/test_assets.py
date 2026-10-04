"""Recovery integrity, private serving, ranges, and lost-cache regression tests."""
import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient
from server.assets import DurableStaticFiles, clean_path, insert_asset, materialize
from scripts import restore_assets


class FakeDB:
    def __init__(self):
        self.rows = {}
        self.result = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def fetchone(self): return self.result
    def execute(self, sql, params):
        path = params[0]
        if sql.startswith('INSERT'):
            self.rows.setdefault(path, (params[1], params[2]))
        elif sql.startswith('SELECT sha256'):
            self.result = (self.rows[path][1],)
        else:
            self.result = self.rows.get(path)
        return self


class AssetTests(unittest.TestCase):
    def test_paths_reject_escape_and_other_trees(self):
        for path in ['/uploads/a', 'uploads/../secret', 'uploads/./a', 'gen/../../x',
                     'uploads/a\\b', 'auth/key', 'uploads', 'uploads/a\x00']:
            with self.assertRaises(ValueError): clean_path(path)

    def test_existing_history_cannot_be_overwritten(self):
        db = FakeDB()
        insert_asset(db, 'uploads/a.pdf', b'original')
        insert_asset(db, 'uploads/a.pdf', b'original')
        with self.assertRaises(ValueError): insert_asset(db, 'uploads/a.pdf', b'replacement')
        self.assertEqual(db.rows['uploads/a.pdf'][0], b'original')

    def test_auth_ranges_html_sandbox_and_restart_rehydration(self):
        db = FakeDB()
        for path, data in [('uploads/a.pdf', b'%PDF-1.7\n1234567890'),
                           ('uploads/a.html', b'<script>alert(1)</script>'),
                           ('gen/a.jpg', b'jpeg')]:
            insert_asset(db, path, data)
        with tempfile.TemporaryDirectory() as directory, \
             patch('server.assets.psycopg.connect', return_value=db), \
             patch('server.assets.dsn', return_value='unused'):
            app = Starlette()
            app.mount('/static', DurableStaticFiles(directory=directory,
                      dashboard_key='test-key', cookie_name='session'))
            with TestClient(app) as client:
                self.assertEqual(client.get('/static/uploads/a.pdf').status_code, 401)
                self.assertEqual(client.get('/static/gen/a.jpg').content, b'jpeg')
                client.cookies.set('session', 'test-key')
                response = client.get('/static/uploads/a.pdf', headers={'Range': 'bytes=0-3'})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, b'%PDF')
                self.assertEqual(response.headers['cache-control'], 'private, no-store')
                Path(directory, 'uploads/a.pdf').unlink()
                self.assertEqual(client.get('/static/uploads/a.pdf').content, db.rows['uploads/a.pdf'][0])
                html = client.get('/static/uploads/a.html')
                self.assertIn('sandbox', html.headers['content-security-policy'])
                self.assertEqual(client.get('/static/uploads/missing.pdf').status_code, 404)

    def test_corrupt_stored_bytes_are_not_served(self):
        db = FakeDB()
        db.rows['uploads/a.pdf'] = (b'bad', hashlib.sha256(b'good').hexdigest())
        with tempfile.TemporaryDirectory() as directory, \
             patch('server.assets.psycopg.connect', return_value=db), \
             patch('server.assets.dsn', return_value='unused'):
            with self.assertRaises(ValueError): materialize('uploads/a.pdf', directory)

    def test_archive_rejects_links(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as tar:
            member = tarfile.TarInfo('uploads/link')
            member.type = tarfile.SYMTYPE
            member.linkname = '/etc/passwd'
            tar.addfile(member)
        data = stream.getvalue()
        with patch.object(restore_assets, 'ARCHIVE_SHA256', hashlib.sha256(data).hexdigest()):
            with self.assertRaises(ValueError): list(restore_assets.archive_assets(data))


if __name__ == '__main__': unittest.main()
