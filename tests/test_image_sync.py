import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('image_sync', Path(__file__).resolve().parents[1] / 'scripts/sync_images_to_r2.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Missing(Exception):
    response = {'Error': {'Code': 'NoSuchKey'}}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.writes = []

    def get_paginator(self, operation):
        assert operation == 'list_objects_v2'
        return self

    def paginate(self, **kwargs):
        # Deliberately paginate one object per page.
        for key, value in self.objects.items():
            if key.startswith(kwargs['Prefix']):
                yield {'Contents': [{'Key': key, 'Size': len(value['body']), 'ETag': value['etag']}]}

    def get_object(self, Key, **kwargs):
        if Key not in self.objects:
            raise Missing()
        return {'Body': io.BytesIO(self.objects[Key]['body'])}

    def put_object(self, Key, Body, **kwargs):
        if kwargs.get('IfNoneMatch') == '*':
            assert Key not in self.objects
        if 'IfMatch' in kwargs:
            assert kwargs['IfMatch'] == self.objects[Key]['etag']
        self.writes.append((Key, kwargs))
        etag = '"' + hashlib.md5(Body).hexdigest() + '"'
        self.objects[Key] = {'body': Body, 'etag': etag}
        return {'ETag': etag}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def source(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return {'path': path, 'size': len(body), 'md5': hashlib.md5(body).hexdigest(),
                'sha256': hashlib.sha256(body).hexdigest(), 'uploaded': '2026-09-15T10:00:00+00:00'}

    def remote(self, body, etag=None):
        return {'body': body, 'etag': etag or '"' + hashlib.md5(body).hexdigest() + '"'}

    def image_writes(self, s3):
        return [key for key, _ in s3.writes if key.startswith('images/')]

    def test_one_upload_does_not_touch_other_images(self):
        old = 'images/World/USA/cover.webp'
        new = 'images/World/South Korea/backdrop.webp'
        s3 = FakeS3({old: self.remote(b'old')})
        result = module.sync(s3, 'bucket', {old: self.source(old, b'old'), new: self.source(new, b'new')})
        self.assertEqual(self.image_writes(s3), [new])
        self.assertEqual(result['unchanged'], 1)

    def test_identical_content_and_rerun_do_not_write_images(self):
        key = 'images/World/USA/cover.webp'
        s3 = FakeS3({key: self.remote(b'same')})
        sources = {key: self.source(key, b'same')}
        module.sync(s3, 'bucket', sources)
        s3.writes.clear()
        result = module.sync(s3, 'bucket', sources)
        self.assertEqual(s3.writes, [])
        self.assertFalse(result['historyUpdated'])

    def test_same_size_replacement_is_detected(self):
        key = 'images/World/South Korea/backdrop.webp'
        s3 = FakeS3({key: self.remote(b'old')})
        module.sync(s3, 'bucket', {key: self.source(key, b'new')})
        self.assertEqual(self.image_writes(s3), [key])
        self.assertIn('IfMatch', s3.writes[0][1])
        self.assertEqual(s3.writes[0][1]['Metadata']['artwork-uploaded-at'], '2026-09-15T10:00:00+00:00')

    def test_multipart_same_content_is_not_reuploaded(self):
        key = 'images/World/South Korea/backdrop.webp'
        s3 = FakeS3({key: self.remote(b'large', '"multipart-2"')})
        module.sync(s3, 'bucket', {key: self.source(key, b'large')})
        self.assertEqual(self.image_writes(s3), [])
        history = json.loads(s3.objects[module.HISTORY_KEY]['body'])
        self.assertEqual(history['images'][key]['etag'], 'multipart-2')

    def test_remote_only_artwork_is_preserved(self):
        key = 'images/World/USA/cover.webp'
        extra = 'images/World/Mexico/backdrop.webp'
        s3 = FakeS3({extra: self.remote(b'newer upload')})
        module.sync(s3, 'bucket', {key: self.source(key, b'data')})
        self.assertIn(extra, s3.objects)

    def test_stale_checkout_does_not_write(self):
        key = 'images/World/USA/cover.webp'
        s3 = FakeS3()
        def stale():
            raise RuntimeError('new commit')
        with self.assertRaises(RuntimeError):
            module.sync(s3, 'bucket', {key: self.source(key, b'data')}, stale)
        self.assertEqual(s3.writes, [])

    def test_dates_come_from_git_not_checkout_time(self):
        def git(*args, **kwargs):
            return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True, **kwargs)
        git('init', '-q')
        git('config', 'user.email', 'test@example.invalid')
        git('config', 'user.name', 'Test')
        key = 'images/World/South Korea/backdrop.webp'
        self.source(key, b'data')
        git('add', '.')
        import os
        env = {**os.environ, 'GIT_AUTHOR_DATE': '2026-09-15T10:00:00Z', 'GIT_COMMITTER_DATE': '2026-09-15T10:00:00Z'}
        git('commit', '-qm', 'Add image', env=env)
        sources = module.source_files(self.root)
        self.assertEqual(sources[key]['uploaded'].replace('Z', '+00:00'), '2026-09-15T10:00:00+00:00')
        self.assertEqual(list(sources), [key])


if __name__ == '__main__':
    unittest.main()
