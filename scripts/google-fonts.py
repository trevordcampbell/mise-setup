#!/usr/bin/env python3
"""Stage official Google variable fonts for mise's dotfile deployment."""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path.home() / '.local/share/mise/font-sources'
FAMILIES = {'opensans': 'OpenSans', 'rubik': 'Rubik', 'spacegrotesk': 'SpaceGrotesk'}


def fetch(url):
    request = Request(url, headers={'User-Agent': 'mise-google-fonts', 'Accept': 'application/vnd.github+json'})
    with urlopen(request, timeout=45) as response:
        return response.read()


def api(path):
    return json.loads(fetch('https://api.github.com/repos/google/fonts/' + path))


def git_blob_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def selected_files(entries, prefix, require_italic=True):
    fonts = [e for e in entries if e['type'] == 'file' and e['name'].endswith('.ttf')]
    if not fonts or any(not re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_,\[\]-]*\.ttf', e['name']) for e in fonts):
        raise RuntimeError('Unexpected font artifact names; review upstream before updating')
    if require_italic and not any('Italic' in e['name'] for e in fonts):
        raise RuntimeError('The family has no italic artifact; review upstream')
    licenses = [e for e in entries if e['name'] == 'OFL.txt' and e['type'] == 'file']
    if len(licenses) != 1:
        raise RuntimeError('The official font license is missing')
    return fonts, licenses[0]


def synchronize(check=False):
    # Use the newest family revision older than the same 24-hour release window.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    pending = []
    revisions = {}
    for family, prefix in FAMILIES.items():
        query = urlencode({'path': 'ofl/' + family, 'until': cutoff, 'per_page': 1})
        commits = api('commits?' + query)
        if not commits:
            raise RuntimeError('No eligible official revision found for ' + family)
        revision = commits[0]['sha']
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise RuntimeError('Unexpected Git revision')
        entries = api('contents/ofl/' + family + '?ref=' + revision)
        fonts, license_entry = selected_files(entries, prefix, require_italic=family != 'spacegrotesk')
        revisions[family] = revision
        for entry in [*fonts, license_entry]:
            target = ROOT / ('licenses/' + family + '/OFL.txt' if entry['name'] == 'OFL.txt' else 'fonts/' + entry['name'])
            if target.exists() and git_blob_sha(target.read_bytes()) == entry['sha']:
                continue
            pending.append((family, revision, entry, target))
    if check:
        return {'state': 'update-available' if pending else 'current',
                'files': [e['name'] for _, _, e, _ in pending], 'revisions': revisions}
    # Download and verify every replacement before writing any live source file.
    downloads = []
    for family, revision, entry, target in pending:
        url = 'https://raw.githubusercontent.com/google/fonts/' + revision + '/ofl/' + family + '/' + quote(entry['name'], safe='')
        data = fetch(url)
        if git_blob_sha(data) != entry['sha']:
            raise RuntimeError('Official Git blob verification failed for ' + entry['name'])
        if entry['name'].endswith('.ttf') and data[:4] not in (b'\x00\x01\x00\x00', b'OTTO'):
            raise RuntimeError('The download is not a font: ' + entry['name'])
        downloads.append((target, data))
    for target, data in downloads:
        atomic_bytes(target, data)
    atomic_bytes(ROOT / 'revisions.json', (json.dumps(revisions, indent=2) + '\n').encode())
    return {'state': 'updated' if pending else 'current',
            'files': [e['name'] for _, _, e, _ in pending], 'revisions': revisions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--check', action='store_true')
    actions.add_argument('--deploy', action='store_true')
    args = parser.parse_args()
    if args.check:
        print(json.dumps(synchronize(check=True), indent=2))
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / 'sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(synchronize(), indent=2))
        if args.deploy:
            targets = [str(Path.home() / 'Library/Fonts' / p.name) for p in sorted((ROOT / 'fonts').glob('*.ttf'))]
            if not targets:
                raise RuntimeError('No verified font sources to deploy')
            env = dict(os.environ, MISE_NO_HOOKS='1')
            subprocess.run([str(Path.home() / '.local/bin/mise'), '-C', str(Path.home()), 'dotfiles', 'apply', *targets, '--force', '-y'], env=env, check=True)


if __name__ == '__main__':
    main()
