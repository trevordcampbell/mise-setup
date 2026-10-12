#!/usr/bin/env python3
"""Update this Mac's global mise tools, packages, and official MarkText release."""
import argparse
import codecs
from datetime import datetime, timezone, timedelta
import fcntl
from functools import partial
import json
import hashlib
import os
from pathlib import Path
import plistlib
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading
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


def label(name):
    labels = {
        'mise': 'Mise', 'tools': 'Global tools', 'rustup': 'Rustup',
        'shell-repositories': 'Configuration and shell repositories',
        'shell-completions': 'Shell completions', 'variable-fonts': 'Variable fonts',
        'marktext': 'MarkText', 'brew': 'Native packages',
        'visual-studio-code': 'Visual Studio Code', 'google-chrome': 'Google Chrome',
        'github': 'GitHub Desktop', 'iterm2': 'iTerm2', 'orbstack': 'OrbStack',
        'chatgpt': 'ChatGPT', 'codexbar': 'CodexBar', 'daisydisk': 'DaisyDisk',
        'devutils': 'DevUtils', 'macs-fan-control': 'Macs Fan Control',
        'font-poppins': 'Poppins fonts', 'node': 'Node.js', 'python': 'Python',
        'gh': 'GitHub CLI', 'cargo:zpl_toolchain_cli': 'ZPL toolchain',
        'github:skw398/scap': 'Scap', 'uv': 'uv', 'pnpm': 'pnpm',
    }
    name = name.removeprefix('brew-cask:').removeprefix('brew:')
    return labels.get(name, name.replace('-', ' ').title())


def format_change(change, *, planned=False):
    name, kind = change['name'], change['kind']
    old, new = change.get('before'), change.get('after')
    if kind in ('font', 'content') and planned:
        return f'{name}: update available'
    if kind in ('font', 'content'):
        return f'{name}: {"removed" if new is None else "installed" if old is None else "content updated"}'
    if kind == 'revision':
        old, new = old[:8] if old else None, new[:8] if new else None
    def short(value):
        if value is None:
            return value
        return re.sub(r'\(build ([0-9a-f]{12,})\)', lambda m: '(build ' + (m[1][:8] if not m[1].isdigit() else m[1]) + ')', value)
    old, new = short(old), short(new)
    if old is None:
        return f'{name}: not installed → {new}' if planned else f'{name}: installed {new}'
    if new is None:
        return f'{name}: removed {old}'
    return f'{name}: {old} → {new}'


def outcome(result):
    state = result['state']
    if state in ('failed', 'interrupted'):
        return '✗', result.get('error', 'Failed'), '31'
    if state == 'deferred-running-app':
        return '↷', 'Deferred — app is running', '33'
    if result.get('changes'):
        return '✓', '; '.join(format_change(c) for c in result['changes']), '32'
    if result.get('plans'):
        return '↑', '; '.join(format_change(c, planned=True) for c in result['plans']), '33'
    if result.get('observation_errors'):
        return '!', 'Completed; change verification unavailable', '33'
    if state == 'update-available':
        return '↑', f"Update available: {result.get('installed', '?')} → {result.get('available', '?')}", '33'
    return '✓', 'Unchanged' if result.get('before') is not None else 'Check completed', '32'


class Console:
    def __init__(self, *, verbose=False, json_output=False):
        self.verbose, self.json_output = verbose, json_output
        self.stdout, self.stderr = sys.stdout, sys.stderr
        self.terminal = self.stdout.isatty() and os.environ.get('TERM') != 'dumb'
        self.color = self.terminal and 'NO_COLOR' not in os.environ
        self._stop = threading.Event()
        self._thread = None
        self._width = 0

    def paint(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def say(self, text=''):
        if not self.json_output:
            print(text, file=self.stdout, flush=True)

    def starting(self, name, check_only):
        self.stop_progress()
        if self.json_output:
            return
        if self.verbose:
            self.say(f'Checking {label(name)}…')
        elif self.terminal:
            self._stop.clear()
            start = time.monotonic()
            def animate():
                frames = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
                index = 0
                while not self._stop.is_set():
                    text = f'{frames[index % len(frames)]} {label(name)} · {time.monotonic() - start:.1f}s'
                    columns = shutil.get_terminal_size((80, 24)).columns
                    text = text[:max(1, columns - 1)]
                    self.stdout.write('\r' + text.ljust(self._width))
                    self.stdout.flush()
                    self._width = len(text)
                    index += 1
                    self._stop.wait(0.08)
            self._thread = threading.Thread(target=animate, daemon=True)
            self._thread.start()

    def stop_progress(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None
        if self._width:
            self.stdout.write('\r' + ' ' * self._width + '\r')
            self.stdout.flush()
            self._width = 0

    def finished(self, name, result):
        self.stop_progress()
        if self.verbose:
            symbol, detail, color = outcome(result)
            elapsed = f"  {result['elapsed_seconds']:.1f}s" if 'elapsed_seconds' in result else ''
            self.say(f'{self.paint(symbol, color)} {label(name)}: {detail}{elapsed}')

    def summary(self, report, *, show_steps=False):
        self.stop_progress()
        if self.json_output:
            print(json.dumps(report, indent=2), file=self.stdout, flush=True)
            return
        if report['state'] == 'not-run-yet':
            self.say('No maintenance run has been recorded yet.')
            return
        if report['state'] == 'already-running':
            self.say('Another maintenance run is active; skipped.')
            return
        if show_steps:
            stamp = report.get('started_at')
            self.say('Last run: ' + (datetime.fromisoformat(stamp).astimezone().strftime('%Y-%m-%d %H:%M %Z') if stamp else 'unknown'))
            if self.verbose:
                for name, result in report.get('results', {}).items():
                    self.finished(name, result)
        results = report.get('results', {})
        failed = [name for name, r in results.items() if r['state'] in ('failed', 'interrupted')]
        deferred = [name for name, r in results.items() if r['state'] == 'deferred-running-app']
        unverified = [name for name, r in results.items() if r.get('observation_errors') or r.get('plan_unverified')]
        changes = report.get('changes', [])
        plans = report.get('planned_changes', [])
        notes = [(name, note) for name, r in results.items() for note in r.get('notes', [])]
        elapsed = report.get('elapsed_seconds')
        if elapsed is None and report.get('finished_at') and report.get('started_at'):
            elapsed = (datetime.fromisoformat(report['finished_at']) - datetime.fromisoformat(report['started_at'])).total_seconds()
        duration = f' in {elapsed:.1f}s' if elapsed is not None else ''
        preview = report.get('check_only', False)
        good = report['state'] == 'success'
        status = 'interrupted' if report['state'] == 'interrupted' else 'finished' if good else 'finished with errors'
        self.say()
        self.say(self.paint(f"{'✓' if good else '✗'} {'Preview' if preview else 'Maintenance'} {status}{duration}", '32' if good else '31'))
        self.say()
        if changes:
            self.say(self.paint(('Observed changes during preview' if preview else 'Installed changes') + (' (including partial results)' if failed else ''), '1'))
            font_groups = {}
            for change in changes:
                if change['kind'] == 'font':
                    family = next((display for prefix, display in [('Poppins', 'Poppins'), ('OpenSans', 'Open Sans'), ('Rubik', 'Rubik'), ('SpaceGrotesk', 'Space Grotesk')] if change['name'].startswith(prefix)), change['name'])
                    font_groups.setdefault(family, []).append(change)
                else:
                    self.say('  ' + format_change(change))
            for family, files in font_groups.items():
                actions = set(c.get('action', 'updated') for c in files)
                action = next(iter(actions)) if len(actions) == 1 else 'changed'
                self.say(f"  {family}: {len(files)} font {'file' if len(files) == 1 else 'files'} {action}")
        elif report.get('schema_version', 0) < 2:
            self.say('This older report did not record before/after changes.')
        elif failed or unverified:
            self.say('No changes verified; some results could not be confirmed.')
        elif preview:
            self.say('No changes applied — preview only.')
        else:
            self.say('No installed versions or contents changed.')
        if plans:
            self.say()
            self.say(self.paint('Updates available (not applied)', '1'))
            for plan in plans:
                self.say('  ' + format_change(plan, planned=True))
        elif preview and not unverified and not failed:
            self.say('No tool, app, package, or font updates were reported by the preview.')
        activity = results.get('shell-completions', {})
        if activity.get('state') == 'success':
            detail = 'change not verified' if activity.get('observation_errors') else 'change details not recorded' if report.get('schema_version', 0) < 2 else 'content unchanged' if not activity.get('changes') else 'content updated'
            self.say(f'Shell completions regenerated ({detail}).')
        if deferred:
            self.say()
            self.say(self.paint('Deferred while running', '33'))
            self.say('  ' + ' · '.join(map(label, deferred)))
        if notes:
            self.say()
            self.say(self.paint('Skipped or noted', '33'))
            for name, note in notes:
                self.say(f'  {label(name)}: {note}')
        if unverified:
            self.say()
            self.say(self.paint('Change verification unavailable', '33'))
            for name in unverified:
                detail = '; '.join(results[name].get('observation_errors', [])) or 'See the native plan in the detailed log.'
                self.say(f'  {label(name)}: {detail}')
        if failed:
            self.say()
            self.say(self.paint('Errors', '31'))
            for name in failed:
                self.say(f"  {label(name)}: {results[name].get('error', 'Failed')}")
        self.say()
        self.say(f"{len(failed)} {'error' if len(failed) == 1 else 'errors'} · {len(deferred)} deferred")
        self.say(self.paint(f"Detailed log: {report.get('log', str(LOG))}", '2'))


def run(args, log, timeout=3600, console=None):
    command = list(map(str, args))
    log.write(f'\n[{now()}] {" ".join(command)}\n')
    log.flush()
    if console is None or not console.verbose:
        result = subprocess.run(command, cwd=HOME, env=ENV,
                                stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, timeout=timeout)
        returncode = result.returncode
    else:
        # Stream to the terminal and keep the full log. A pipe avoids ANSI progress
        # controls in the saved log; selectors also handle output without newlines.
        print('$ ' + ' '.join(command), file=console.stderr, flush=True)
        deadline = time.monotonic() + timeout
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        with subprocess.Popen(command, cwd=HOME, env=ENV, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              start_new_session=True) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, timeout)
                        for key, _ in selector.select(min(remaining, 0.2)):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                            text = decoder.decode(chunk, final=not chunk)
                            log.write(text)
                            log.flush()
                            console.stderr.write(text)
                            console.stderr.flush()
                    remaining = deadline - time.monotonic()
                    returncode = process.wait(timeout=max(remaining, 0))
            except BaseException:
                # A timed-out or interrupted command must not continue updating
                # in the background, including children holding the output pipe.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
    if returncode:
        raise RuntimeError(f'Command exited {returncode}: {" ".join(command)}')


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


def marktext(check_only, log, console=None):
    command = partial(run, console=console)
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
            command(['/usr/bin/codesign', '--verify', '--strict', str(app)], log, timeout=120)
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
        command([MISE, 'bootstrap', 'packages', 'apply', key, '-y'], log)
        command(['/usr/bin/codesign', '--verify', '--strict', str(app)], log, timeout=120)
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


# Installed-state observers are read-only and avoid network metadata refreshes.
PREFIX = Path('/opt/homebrew')
FONT_ROOT = HOME / '.local/share/mise/font-sources'


def read_command(args):
    result = subprocess.run(list(map(str, args)), cwd=HOME, env=ENV,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'Could not read state: {args[0]}')
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def entity(name, value, kind='version', **details):
    return {'name': name, 'value': value, 'kind': kind, **details}


def snapshot(name):
    config = tomllib.loads(CONFIG.read_text())
    if name == 'mise':
        return {'mise': entity('Mise', read_command([MISE, '--version']).split()[0])}
    if name == 'rustup':
        return {'rustup': entity('Rustup', read_command([HOME / '.cargo/bin/rustup', '--version']).split()[1])}
    if name == 'tools':
        records = json.loads(read_command([MISE, 'ls', '--global', '--current', '--json']))
        return {key: entity(label(key), ', '.join(sorted(r['version'] for r in values if r.get('installed'))) or None)
                for key, values in records.items()}
    if name == 'brew':
        result = {}
        for path in sorted((PREFIX / 'opt').iterdir()):
            if path.is_symlink():
                target = path.resolve()
                if target.is_relative_to((PREFIX / 'Cellar').resolve()):
                    if not target.is_dir():
                        raise RuntimeError(f'Broken native package link: {path.name}')
                    result['brew:' + path.name] = entity(path.name, target.name)
        return result
    if name == 'shell-repositories':
        result = {}
        for path in config['bootstrap']['repos']:
            directory = Path(path).expanduser()
            result[path] = entity('Mise setup' if directory == CONFIG.parent else directory.name,
                                  read_command(['git', '-C', directory, 'rev-parse', 'HEAD']), 'revision',
                                  dirty=bool(read_command(['git', '-C', directory, 'status', '--porcelain'])))
        return result
    if name == 'shell-completions':
        return {'completions': entity('Shell completions', digest(HOME / '.local/share/zsh/site-functions/_mise'), 'content')}
    if name == 'variable-fonts':
        revisions_path = FONT_ROOT / 'revisions.json'
        revisions = json.loads(revisions_path.read_text()) if revisions_path.exists() else {}
        result = {}
        for source in sorted((FONT_ROOT / 'fonts').glob('*.ttf')):
            name = source.name
            family = next((family for family, prefix in [('opensans', 'OpenSans'), ('rubik', 'Rubik'), ('spacegrotesk', 'SpaceGrotesk')]
                           if name.startswith(prefix)), None)
            result[name] = entity(name, digest(HOME / 'Library/Fonts' / name), 'font', source_revision=revisions.get(family))
        if not result:
            raise RuntimeError('No installed variable fonts could be inspected')
        return result
    if name == 'marktext':
        artifact = config['bootstrap']['packages'].get('macos-app:marktext', {}).get('artifact')
        return app_snapshot(Path('/Applications') / artifact, 'MarkText') if artifact else {}
    if name.startswith('brew-cask:'):
        receipts = list((PREFIX / 'Caskroom' / name.split(':', 1)[1]).glob('*/.mise-cask.toml'))
        if not receipts:
            raise RuntimeError(f'No ownership receipt for {label(name)}')
        result = {}
        for path in receipts:
            receipt = tomllib.loads(path.read_text())
            for app in receipt.get('apps', []):
                result.update(app_snapshot(Path(app), label(name)))
            for font in receipt.get('fonts', []):
                result[font] = entity(Path(font).name, digest(Path(font)), 'font')
        if not result:
            raise RuntimeError(f'No installed artifacts could be inspected for {label(name)}')
        return result
    raise RuntimeError(f'No change observer for {name}')


def app_snapshot(app, name):
    path = app / 'Contents/Info.plist'
    if not path.exists():
        raise RuntimeError(f'{name} app bundle is missing')
    info = plistlib.loads(path.read_bytes())
    version = str(info.get('CFBundleShortVersionString') or '')
    build = str(info.get('CFBundleVersion') or '')
    if not version and not build:
        raise RuntimeError(f'{name} does not expose an installed version')
    value = version or build
    if version and build and version != build:
        value += f' (build {build})'
    return {str(app): entity(name, value)}


def compare(before, after):
    changes = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        old_value, new_value = old.get('value') if old else None, new.get('value') if new else None
        if old_value == new_value:
            continue
        record = new or old
        changes.append({'id': key, 'name': record['name'], 'kind': record['kind'],
                        'before': old_value, 'after': new_value,
                        'action': 'installed' if old_value is None else 'removed' if new_value is None else 'updated'})
    return changes


def native_preview(text, before):
    # Exact native plan forms from mise v2026.10.8; unknown forms are retained as
    # notes, not promoted to confirmed updates. Actual updates use snapshots.
    plans = []
    for match in re.finditer(r'(?m)^(?:pour|build) ([^\s/]+)/([^\s]+)', text):
        package, version = match.groups()
        old = before.get('brew:' + package, {}).get('value')
        plans.append({'name': package, 'kind': 'version', 'before': old, 'after': version})
    for match in re.finditer(r'(?m)^install cask ([^\s/]+)/([^\s]+)', text):
        package, version = match.groups()
        values = ', '.join(record['value'] for record in before.values() if record.get('value'))
        plans.append({'name': label(package), 'kind': 'version', 'before': values or None, 'after': version})
    return plans


def json_document(text):
    # The font helper may share a log with a nested mise deployment message.
    for match in re.finditer(r'(?m)^\{', text):
        try:
            return json.JSONDecoder().raw_decode(text[match.start():])[0]
        except json.JSONDecodeError:
            continue
    raise RuntimeError('No structured update plan was returned')


def preview_result(name, text, before):
    if name == 'tools':
        records = json_document(text)
        return {'plans': [{'name': label(key), 'kind': 'version', 'before': r['current'], 'after': r['latest']}
                          for key, r in records.items()]}
    if name == 'variable-fonts':
        plan = json_document(text)
        return {'plans': [{'name': filename, 'kind': 'font', 'before': None, 'after': 'available'}
                          for filename in plan['files']], 'eligible_revisions': plan['revisions']}
    if name == 'rustup':
        matches = re.findall(r'(?mi)^rustup\s+-\s+Update available\s*:\s*(\S+)\s+->\s*(\S+)', text)
        return {'plans': [{'name': 'Rustup', 'kind': 'version', 'before': old, 'after': new} for old, new in matches],
                'plan_unverified': 'update available' in text.lower() and not matches}
    if name == 'shell-repositories':
        return {'notes': re.findall(r'(?mi)^(?:mise\s+)?(.*(?:would update|would fast-forward|would clone).*)$', text)}
    if name == 'brew' or name.startswith('brew-cask:'):
        notes = re.findall(r'(?m)^.*(?:skipped:|WARN\s+)(.+)$', text)
        plans = native_preview(text, before)
        # An unrecognized nonempty plan must not masquerade as "nothing available".
        known = ('already up to date', 'all formulae already poured', 'skipped:')
        unknown = bool(text.strip()) and not plans and not any(s in text for s in known)
        return {'plans': plans, 'notes': notes, 'plan_unverified': unknown}
    return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Preview updates without applying them')
    parser.add_argument('--status', action='store_true', help='Print the most recent scheduled/manual run')
    parser.add_argument('--verbose', action='store_true', help='Stream command output as well as saving the log')
    parser.add_argument('--json', action='store_true', dest='json_output', help='Print only the structured report on stdout')
    args = parser.parse_args(argv)
    console = Console(verbose=args.verbose, json_output=args.json_output)
    command = partial(run, console=console)
    if args.status:
        path = STATE / 'last-run.json'
        console.summary(json.loads(path.read_text()) if path.exists() else {'state': 'not-run-yet'}, show_steps=True)
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            console.summary({'state': 'already-running'})
            return 0
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 5_000_000:
            os.replace(LOG, LOG.with_suffix('.previous.log'))
        started = time.monotonic()
        report = {'started_at': now(), 'check_only': args.check, 'results': {}, 'errors': [], 'log': str(LOG), 'schema_version': 2}
        console.say('Previewing maintenance updates' if args.check else 'Running maintenance updates')
        console.say()
        with LOG.open('a+', buffering=1) as log:
            def step(name, action):
                if report.get('interrupted'):
                    return
                console.starting(name, args.check)
                stage_started = time.monotonic()
                observation_errors = []
                def observe(when):
                    try:
                        return snapshot(name)
                    except Exception as error:
                        detail = f'{when}: {error}'
                        observation_errors.append(detail)
                        log.write(f'[{now()}] {name} observation: {detail}\n')
                        return None
                try:
                    before = observe('before')
                    log.flush()
                    output_start = log.tell()
                    try:
                        result = action() or {'state': 'checked' if args.check else 'success'}
                    except KeyboardInterrupt:
                        result = {'state': 'interrupted', 'error': 'Interrupted by user'}
                        report['interrupted'] = True
                        report['errors'].append(name)
                    except Exception as error:
                        result = {'state': 'failed', 'error': str(error)}
                        report['errors'].append(name)
                        log.write(f'[{now()}] {name}: {error}\n')
                    log.flush()
                    log.seek(output_start)
                    native_notes = [m[1].strip() for line in log for m in [re.search(r'(?:skipped:|WARN\s+)(.+)$', line)] if m]
                    log.seek(0, 2)
                    after = observe('after')
                    result['before'], result['after'] = before, after
                    result['changes'] = compare(before, after) if before is not None and after is not None else []
                    # A failed install can still have changed a subset of artifacts.
                    if args.check and result['state'] not in ('failed', 'interrupted'):
                        log.flush()
                        log.seek(output_start)
                        text = log.read()
                        log.seek(0, 2)
                        try:
                            result.update(preview_result(name, text, before or {}))
                        except Exception as error:
                            observation_errors.append(f'preview: {error}')
                    if result['state'] == 'update-available' and name == 'marktext':
                        result['plans'] = [{'name': 'MarkText', 'kind': 'version',
                                            'before': result['installed'], 'after': result['available']}]
                    if name == 'shell-repositories' and before is not None:
                        dirty = [record for record in before.values() if record.get('dirty')]
                        result.setdefault('notes', []).extend(f"{record['name']} preserved because it has local edits" for record in dirty)
                        if dirty:
                            native_notes = [note for note in native_notes if 'has local changes' not in note]
                    if native_notes:
                        result['notes'] = list(dict.fromkeys(result.get('notes', []) + native_notes))
                    if observation_errors:
                        result['observation_errors'] = observation_errors
                    result['elapsed_seconds'] = round(time.monotonic() - stage_started, 3)
                    report['results'][name] = result
                    console.finished(name, result)
                finally:
                    console.stop_progress()
            if not args.check:
                step('mise', lambda: command([MISE, 'self-update', '-y', '--no-plugins'], log, timeout=600))
            step('tools', lambda: command([MISE, 'outdated', '--global', '--json'] if args.check else [MISE, 'upgrade', '--global', '-y'], log))
            if 'rust' in tomllib.loads(CONFIG.read_text()).get('tools', {}):
                step('rustup', lambda: command([HOME / '.cargo/bin/rustup', 'check'] if args.check else [HOME / '.cargo/bin/rustup', 'self', 'update'], log, timeout=600))
            step('shell-repositories', lambda: command([MISE, 'bootstrap', 'repos', 'update', '--skip-dirty', '-y'] + (['--dry-run'] if args.check else []), log, timeout=600))
            if not args.check:
                step('shell-completions', lambda: command([MISE, 'run', 'shell:completions'], log, timeout=120))
            step('variable-fonts', lambda: command([sys.executable, CONFIG.parent / 'scripts/google-fonts.py', '--check' if args.check else '--deploy'], log, timeout=300))
            step('marktext', lambda: marktext(args.check, log, console=console))
            packages = tomllib.loads(CONFIG.read_text())['bootstrap']['packages']
            for key in packages:
                if key.startswith('macos-app:') and key != 'macos-app:marktext':
                    def unsupported_app():
                        raise RuntimeError('Direct app downloads require a release resolver; prefer brew-cask when available')
                    step(key, unsupported_app)
            managers = sorted({key.split(':', 1)[0] for key in packages} - {'brew-cask', 'macos-app'})
            for manager in managers:
                step(manager, lambda m=manager: command([MISE, 'bootstrap', 'packages', 'upgrade', '--manager', m, '-y'] + (['--dry-run'] if args.check else []), log))
            for key in packages:
                if not key.startswith('brew-cask:'):
                    continue
                def cask_update(k=key):
                    if not args.check and any(is_running(app) for app in cask_apps(k.split(':', 1)[1])):
                        return {'state': 'deferred-running-app'}
                    command([MISE, 'bootstrap', 'packages', 'upgrade', k, '-y'] + (['--dry-run'] if args.check else []), log)
                    return {'state': 'checked' if args.check else 'success'}
                step(key, cask_update)
            report['finished_at'] = now()
            report['elapsed_seconds'] = round(time.monotonic() - started, 3)
            report['state'] = 'interrupted' if report.get('interrupted') else 'failed' if report['errors'] else 'success'
            report['changes'] = [dict(change, stage=name) for name, result in report['results'].items() for change in result.get('changes', [])]
            report['planned_changes'] = [dict(plan, stage=name) for name, result in report['results'].items() for plan in result.get('plans', [])]
            atomic_write(STATE / ('last-check.json' if args.check else 'last-run.json'), json.dumps(report, indent=2) + '\n')
            console.summary(report)
            return 130 if report.get('interrupted') else 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
