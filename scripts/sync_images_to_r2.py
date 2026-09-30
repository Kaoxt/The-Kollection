#!/usr/bin/env python3
"""Sync actual image changes, never checkout timestamps, and rebuild artwork dates."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

HISTORY_KEY = '_kollection/artwork-history.json'


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(root), *args]).decode('utf-8')


def source_files(root: Path) -> dict[str, dict[str, Any]]:
    # Full history is required: a shallow checkout invents dates for old files.
    if git(root, 'rev-parse', '--is-shallow-repository').strip() != 'false':
        raise RuntimeError('Image sync requires fetch-depth: 0.')
    result = {}
    for key in git(root, 'ls-files', '-z', '--', 'images/').split('\0'):
        if not key:
            continue
        path = root / key
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f'Refusing non-regular image source: {key}')
        body = path.read_bytes()
        uploaded = git(root, 'log', '-1', '--format=%cI', '--', key).strip()
        if not uploaded:
            raise RuntimeError(f'No Git history for {key}')
        result[key] = {
            'path': path,
            'size': len(body),
            'md5': hashlib.md5(body, usedforsecurity=False).hexdigest(),
            'sha256': hashlib.sha256(body).hexdigest(),
            'uploaded': uploaded,
        }
    if not result:
        raise RuntimeError('No tracked images found; refusing an empty sync.')
    return result


def normalized_etag(value: Any) -> str:
    return str(value or '').strip('"')


def read_object(s3: Any, bucket: str, key: str, **kwargs: Any) -> bytes:
    stream = s3.get_object(Bucket=bucket, Key=key, **kwargs)['Body']
    try:
        return stream.read()
    finally:
        stream.close()


def same_content(s3: Any, bucket: str, key: str, local: dict,
                 remote: dict | None) -> bool:
    if not remote or int(remote['Size']) != local['size']:
        return False
    etag = normalized_etag(remote['ETag'])
    if re.fullmatch(r'[0-9a-fA-F]{32}', etag):
        return etag.lower() == local['md5']
    # Older AWS CLI uploads can be multipart. Their ETag is not an MD5.
    body = read_object(s3, bucket, key, IfMatch=remote['ETag'])
    return hashlib.sha256(body).hexdigest() == local['sha256']


def sync(s3: Any, bucket: str, sources: dict[str, dict[str, Any]],
         assert_current: Callable[[], None] = lambda: None) -> dict:
    remote = {}
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix='images/'):
        remote.update((item['Key'], item) for item in page.get('Contents', []))

    changed = []
    history = {}
    for key, local in sorted(sources.items()):
        existing = remote.get(key)
        identical = same_content(s3, bucket, key, local, existing)
        history[key] = {
            'etag': normalized_etag(existing['ETag']) if identical else local['md5'],
            'size': local['size'],
            'uploaded': local['uploaded'],
        }
        if not identical:
            changed.append((key, local, existing))

    # A site upload commits to main before writing R2. Never overwrite it from
    # a queued, older checkout. Conditional writes also protect later races.
    assert_current()
    for key, local, existing in changed:
        body = local['path'].read_bytes()
        if hashlib.sha256(body).hexdigest() != local['sha256']:
            raise RuntimeError(f'Source changed during sync: {key}')
        condition = {'IfMatch': existing['ETag']} if existing else {'IfNoneMatch': '*'}
        written = s3.put_object(
            Bucket=bucket, Key=key, Body=body,
            ContentType=mimetypes.guess_type(key)[0] or 'application/octet-stream',
            ContentMD5=base64.b64encode(bytes.fromhex(local['md5'])).decode('ascii'),
            Metadata={'artwork-uploaded-at': local['uploaded'], 'artwork-source': 'github'},
            **condition,
        )
        history[key]['etag'] = normalized_etag(written.get('ETag') or local['md5'])
        print(f'Uploaded changed image: {key}')

    # Repair misleading legacy dates WITHOUT rewriting any unchanged image.
    # This index sits outside images/ and is read only by the authenticated API.
    payload = (json.dumps({'version': 1, 'images': history}, sort_keys=True,
                          separators=(',', ':'), ensure_ascii=False) + '\n').encode('utf-8')
    try:
        previous = read_object(s3, bucket, HISTORY_KEY)
    except Exception as error:
        code = str(getattr(error, 'response', {}).get('Error', {}).get('Code', ''))
        if code not in ('NoSuchKey', '404', 'NotFound'):
            raise
        previous = None
    assert_current()
    if previous != payload:
        s3.put_object(Bucket=bucket, Key=HISTORY_KEY, Body=payload,
                      ContentType='application/json', CacheControl='no-store')

    # Do not bulk-delete remote-only artwork: it may belong to an upload
    # arriving during this run or an existing legacy URL.
    summary = {
        'uploaded': len(changed),
        'unchanged': len(sources) - len(changed),
        'historyEntries': len(history),
        'historyUpdated': previous != payload,
        'uploadedPaths': [key for key, _, _ in changed],
    }
    print(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    import boto3
    from botocore.config import Config

    for name in ('R2_ENDPOINT', 'R2_BUCKET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        if not os.environ.get(name):
            raise RuntimeError(f'Missing {name}')
    root = Path(__file__).resolve().parents[1]
    head = git(root, 'rev-parse', 'HEAD').strip()

    def assert_current() -> None:
        latest = git(root, 'ls-remote', 'origin', 'refs/heads/main').split()
        if not latest or latest[0] != head:
            raise RuntimeError('main changed during sync; a newer workflow must sync its current images.')

    assert_current()
    s3 = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'], region_name='auto',
                      config=Config(signature_version='s3v4',
                                    request_checksum_calculation='when_required',
                                    response_checksum_validation='when_required',
                                    retries={'mode': 'standard', 'max_attempts': 3}))
    summary = sync(s3, os.environ['R2_BUCKET'], source_files(root), assert_current)
    Path('image-sync-result.json').write_text(json.dumps(summary, indent=2) + '\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write(f"## Image sync\n\n{summary['uploaded']} changed images uploaded; "
                      f"{summary['unchanged']} unchanged images left untouched.\n\n"
                      f"{summary['historyEntries']} image dates indexed from Git history.\n")


if __name__ == '__main__':
    main()
