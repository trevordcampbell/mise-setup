#!/usr/bin/env python3
"""Update this Mac's global mise tools, packages, and official MarkText release."""
import argparse
from datetime import datetime, timezone, timedelta
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import tomllib
from urllib.request import Request, urlopen

HOME = Path.home()
CONFIG = HOME / '.config/mise/config.toml'
MISE = HOME / '.local/bin/mise'
STATE = HOME / '.local/state/mise/maintenance'
LOG = HOME / 'Library/Logs/mise-maintenance.log'
ENV = dict(os.environ, PATH=f'{HOME}/.local/bin:{HOME}/.local/share/mise/shims:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin', MISE_YES='1')
# Machine maintenance must never inherit a project's config or version overrides.
for key in list(ENV):
    if key.startswith('MISE_') and key != 'MISE_YES':
        del ENV[key]


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, content):
    if path.is_symlink():
        path = path.resolve(strict=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(content)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run(args, log, timeout=3600):
    log.write(f'\n[{now()}] {" ".join(map(str, args))}\n')
    log.flush()
    result = subprocess.run(list(map(str, args)), cwd=HOME, env=ENV,
                            stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'Command exited {result.returncode}: {" ".join(map(str, args))}')


def is_running(app):
    result = subprocess.run(['/usr/bin/pgrep', '-f', re.escape(str(app)) + '/'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode not in (0, 1):
        raise RuntimeError(f'Could not check whether {app} is running')
    return result.returncode == 0


def cask_apps(name):
    receipts = list((Path('/opt/homebrew/Caskroom') / name).glob('*/.mise-cask.toml'))
    if not receipts:
        raise RuntimeError(f'No mise ownership receipt found for cask {name}')
    apps = set()
    for receipt in receipts:
        record = tomllib.loads(receipt.read_text())
        apps.update(record.get('apps', []))
    return [Path(app) for app in apps]


def json_url(url):
    req = Request(url, headers={'User-Agent': 'mise-local-maintenance', 'Accept': 'application/vnd.github+json'})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def release_version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    if not match:
        raise ValueError(f'Unexpected stable release version: {value}')
    return tuple(map(int, match.groups()))


def marktext(check_only, log):
    key = 'macos-app:marktext'
    config = tomllib.loads(CONFIG.read_text())
    entry = config['bootstrap']['packages'].get(key)
    if not entry:
        return {'state': 'not-configured'}
    releases = json_url('https://api.github.com/repos/marktext/marktext/releases?per_page=30')
    candidates = []
    missing_artifacts = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for release in releases:
        if release.get('draft') or release.get('prerelease'):
            continue
        tag = release['tag_name']
        if not re.fullmatch(r'v?\d+\.\d+\.\d+', tag):
            continue
        published = datetime.fromisoformat(release['published_at'].replace('Z', '+00:00'))
        if published > cutoff:
            continue
        version = tag.removeprefix('v')
        expected = f'marktext-mac-arm64-{version}.dmg'
        found_asset = False
        for asset in release['assets']:
            if asset['name'] == expected:
                found_asset = True
                candidates.append((release_version(version), version, tag, asset))
        if not found_asset:
            missing_artifacts.append((release_version(version), version))
    if not candidates:
        raise RuntimeError('No stable Apple Silicon MarkText release older than 24h found')
    _, version, tag, asset = max(candidates, key=lambda item: item[0])
    if missing_artifacts and max(missing_artifacts)[0] > release_version(version):
        raise RuntimeError(f'Newer MarkText {max(missing_artifacts)[1]} has no expected Apple Silicon DMG; review its release assets')
    app = Path('/Applications') / entry['artifact']
    with (app / 'Contents/Info.plist').open('rb') as source:
        installed = plistlib.load(source)['CFBundleShortVersionString']
    if release_version(version) <= release_version(installed):
        if not check_only:
            run(['/usr/bin/codesign', '--verify', '--strict', str(app)], log, timeout=120)
        return {'state': 'current', 'installed': installed, 'eligible_release': version}
    url = asset['browser_download_url']
    expected_url = f'https://github.com/marktext/marktext/releases/download/{tag}/{asset["name"]}'
    if url != expected_url:
        raise RuntimeError('MarkText download is outside its official release path')
    digest = asset.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest or ''):
        raise RuntimeError('Official MarkText artifact has no valid SHA-256 digest; update deferred')
    if check_only:
        return {'state': 'update-available', 'installed': installed, 'available': version}
    if is_running(app):
        return {'state': 'deferred-running-app', 'installed': installed, 'available': version}
    before = CONFIG.read_text()
    pattern = r'(?ms)^\[bootstrap\.packages\."macos-app:marktext"\]\n.*?(?=^\[|\Z)'
    found = re.search(pattern, before)
    if not found:
        raise RuntimeError('Cannot locate the MarkText config section safely')
    block = found.group()
    for field, value in {'version': version, 'url': url, 'sha256': digest[7:]}.items():
        block, count = re.subn(rf'(?m)^{field}\s*=.*$', field + ' = ' + json.dumps(value), block)
        if count != 1:
            raise RuntimeError(f'Cannot safely replace MarkText {field}')
    after = before[:found.start()] + block + before[found.end():]
    tomllib.loads(after)
    atomic_write(STATE / 'config-before-marktext-update.toml', before)
    atomic_write(CONFIG, after)
    try:
        run([MISE, 'bootstrap', 'packages', 'apply', key, '-y'], log)
        run(['/usr/bin/codesign', '--verify', '--strict', str(app)], log, timeout=120)
        with (app / 'Contents/Info.plist').open('rb') as source:
            actual = plistlib.load(source)['CFBundleShortVersionString']
        if actual != version:
            raise RuntimeError(f'MarkText reports {actual}, expected {version}')
    except Exception:
        # Preserve unrelated concurrent edits rather than overwriting them.
        with (app / 'Contents/Info.plist').open('rb') as source:
            actual = plistlib.load(source)['CFBundleShortVersionString']
        if actual != version and CONFIG.read_text() == after:
            atomic_write(CONFIG, before)
        raise
    return {'state': 'updated', 'installed': version, 'sha256': digest[7:]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Preview updates without applying them')
    parser.add_argument('--status', action='store_true', help='Print the most recent scheduled/manual run')
    args = parser.parse_args()
    if args.status:
        path = STATE / 'last-run.json'
        print(path.read_text() if path.exists() else '{"state":"not-run-yet"}')
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another maintenance run is active; skipped.')
            return 0
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 5_000_000:
            os.replace(LOG, LOG.with_suffix('.previous.log'))
        report = {'started_at': now(), 'check_only': args.check, 'results': {}, 'errors': []}
        with LOG.open('a', buffering=1) as log:
            def step(name, action):
                try:
                    result = action()
                    report['results'][name] = result or {'state': 'success'}
                except Exception as error:
                    report['results'][name] = {'state': 'failed', 'error': str(error)}
                    report['errors'].append(name)
                    log.write(f'[{now()}] {name}: {error}\n')
            if not args.check:
                step('mise', lambda: run([MISE, 'self-update', '-y', '--no-plugins'], log, timeout=600))
            step('tools', lambda: run([MISE, 'upgrade', '--global', '-y'] + (['--dry-run'] if args.check else []), log))
            if 'rust' in tomllib.loads(CONFIG.read_text()).get('tools', {}):
                step('rustup', lambda: run([HOME / '.cargo/bin/rustup', 'check'] if args.check else [HOME / '.cargo/bin/rustup', 'self', 'update'], log, timeout=600))
            step('shell-repositories', lambda: run([MISE, 'bootstrap', 'repos', 'update', '--skip-dirty', '-y'] + (['--dry-run'] if args.check else []), log, timeout=600))
            if not args.check:
                step('shell-completions', lambda: run([MISE, 'run', 'shell:completions'], log, timeout=120))
            step('variable-fonts', lambda: run([MISE, 'run', 'fonts:check' if args.check else 'fonts:update'], log, timeout=300))
            step('marktext', lambda: marktext(args.check, log))
            packages = tomllib.loads(CONFIG.read_text())['bootstrap']['packages']
            for key in packages:
                if key.startswith('macos-app:') and key != 'macos-app:marktext':
                    report['results'][key] = {'state': 'failed', 'error': 'Direct app downloads require a release resolver; prefer brew-cask when available'}
                    report['errors'].append(key)
            managers = sorted({key.split(':', 1)[0] for key in packages} - {'brew-cask', 'macos-app'})
            for manager in managers:
                step(manager, lambda m=manager: run([MISE, 'bootstrap', 'packages', 'upgrade', '--manager', m, '-y'] + (['--dry-run'] if args.check else []), log))
            for key in packages:
                if not key.startswith('brew-cask:'):
                    continue
                def cask_update(k=key):
                    if not args.check and any(is_running(app) for app in cask_apps(k.split(':', 1)[1])):
                        return {'state': 'deferred-running-app'}
                    run([MISE, 'bootstrap', 'packages', 'upgrade', k, '-y'] + (['--dry-run'] if args.check else []), log)
                    return {'state': 'checked' if args.check else 'success'}
                step(key, cask_update)
            report['finished_at'] = now()
            report['state'] = 'failed' if report['errors'] else 'success'
            atomic_write(STATE / ('last-check.json' if args.check else 'last-run.json'), json.dumps(report, indent=2) + '\n')
            print(json.dumps(report, indent=2))
            print(f'Detailed log: {LOG}')
            return 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
