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
import platform
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
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen

HOME = Path.home()
CONFIG = HOME / '.config/mise/config.toml'
MISE = HOME / '.local/bin/mise'
STATE = HOME / '.local/state/mise/maintenance'
LOG = HOME / 'Library/Logs/mise-maintenance.log'
APPLICATIONS = Path('/Applications')
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
        'marktext': 'MarkText', 'brew': 'Native packages', 'setup': 'Declared setup',
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
    if state == 'availability-unknown':
        return '!', 'Update availability unverified', '33'
    if result.get('observation_errors'):
        return '!', 'Completed; change verification unavailable', '33'
    if state == 'update-available':
        return '↑', f"Update available: {result.get('installed', '?')} → {result.get('available', '?')}", '33'
    vendor = result.get('vendor_check', {})
    if vendor.get('state') in ('current', 'newer'):
        return '✓', f"{'Current' if vendor['state'] == 'current' else 'Newer than stable'} ({vendor['installed']})", '32'
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
        self.detail = ''

    def paint(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def say(self, text='', force=False):
        if force or not self.json_output:
            print(text, file=self.stdout, flush=True)

    def starting(self, name, check_only):
        self.stop_progress()
        self.detail = ''
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
                    detail = f' · {self.detail}' if self.detail else ''
                    text = f'{frames[index % len(frames)]} {label(name)}{detail} · {time.monotonic() - start:.1f}s'
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
        unverified = [name for name, r in results.items() if r.get('observation_errors') or r.get('plan_unverified') or r.get('state') == 'availability-unknown' or r.get('vendor_check', {}).get('state') == 'unverified']
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
        if report.get('since_previous_run'):
            self.say()
            self.say(self.paint('Changed since the previous run (outside this run)', '1'))
            for change in report['since_previous_run']:
                self.say('  ' + format_change(change))
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
            for name in deferred:
                count = results[name].get('consecutive_deferrals', 1)
                self.say(f"  {label(name)}: update pending — close and retry" + (f' ({count} consecutive runs)' if count > 1 else ''))
        verified_apps = [(name, r['vendor_check']) for name, r in results.items()
                         if r.get('vendor_check', {}).get('state') in ('current', 'newer', 'update-available')]
        if verified_apps:
            self.say()
            self.say(self.paint('Official stable release checks', '1'))
            for name, vendor in verified_apps:
                if vendor['state'] == 'current':
                    detail = f"current ({vendor['installed']})"
                elif vendor['state'] == 'newer':
                    detail = f"installed {vendor['installed']}; stable feed {vendor['available']} — keeping newer app"
                else:
                    detail = f"{vendor['installed']} → {vendor['available']} available"
                self.say(f'  {label(name)}: {detail}')
        if notes:
            self.say()
            self.say(self.paint('Skipped or noted', '33'))
            for name, note in notes:
                self.say(f'  {label(name)}: {note}')
        if unverified:
            self.say()
            self.say(self.paint('Change verification unavailable', '33'))
            for name in unverified:
                detail = '; '.join(results[name].get('observation_errors', [])) or results[name].get('vendor_check', {}).get('error') or 'See the native plan in the detailed log.'
                self.say(f'  {label(name)}: {detail}')
        if failed:
            self.say()
            self.say(self.paint('Errors', '31'))
            for name in failed:
                self.say(f"  {label(name)}: {results[name].get('error', 'Failed')}")
        self.say()
        self.say(f"{len(failed)} {'error' if len(failed) == 1 else 'errors'} · {len(deferred)} deferred")
        self.say(self.paint(f"Detailed log: {report.get('log', str(LOG))}", '2'))


class CaskDownloadProgress:
    """Observe native resumable downloads; never modify Mise's cache or receipts."""
    def __init__(self, key, *, root=None, idle_timeout=120):
        self.root = root or HOME / 'Library/Caches/mise/system-brew/casks'
        self.pattern = '.' + key.split(':', 1)[1] + '-*.mise-part'
        self.idle_timeout = idle_timeout
        self.previous = self.files()
        self.active = None
        self.last_activity = time.monotonic()
        self.sample = None
        self.speed = ''

    def files(self):
        records = {}
        for path in self.root.glob(self.pattern):
            try:
                stat = path.stat()
                records[path] = (stat.st_size, stat.st_mtime_ns)
            except FileNotFoundError:
                pass  # Native finalization can rename a file between glob and stat.
        return records

    def __call__(self):
        current, stamp = self.files(), time.monotonic()
        changed = [p for p, stat in current.items() if self.previous.get(p) != stat]
        self.previous = current
        if changed:
            self.active = max(changed, key=lambda p: current[p][1])
            self.last_activity = stamp
        if self.active not in current:
            self.sample, self.active, self.speed = None, None, ''
            return ''
        size = current[self.active][0]
        if stamp - self.last_activity >= self.idle_timeout:
            raise RuntimeError(f'Download made no progress for {self.idle_timeout}s; stopped updater. '
                               'Partial download retained by Mise; retry maintenance later.')
        if self.sample is None or size < self.sample[1]:
            self.sample, self.speed = (stamp, size), ''
        elif stamp - self.sample[0] >= 2:
            rate = (size - self.sample[1]) / (stamp - self.sample[0]) / 1024
            self.speed = f' · {rate:.0f} KiB/s'
            self.sample = (stamp, size)
        try:
            total = json.loads(Path(str(self.active) + '.json').read_text())['total_size']
            if not isinstance(total, int) or total <= 0 or size > total:
                raise ValueError('Unknown download size')
            amount = f'{size / total:.0%} ({size / 1048576:.1f}/{total / 1048576:.1f} MiB)'
        except (OSError, ValueError, KeyError, TypeError):
            amount = f'{size / 1048576:.1f} MiB'
        return f'Downloading {amount}{self.speed}'


def run(args, log, timeout=3600, console=None, env=None, progress=None):
    command = list(map(str, args))
    log.write(f'\n[{now()}] {" ".join(command)}\n')
    log.flush()
    verbose = console is not None and console.verbose
    if console:
        console.detail = ''
    if verbose:
        # Stream to the terminal and keep the full log. A pipe avoids ANSI progress
        # controls in the saved log; selectors also handle output without newlines.
        print('$ ' + ' '.join(command), file=console.stderr, flush=True)
    # All modes use the same process group and deadline. Quiet mode must also
    # kill descendants on interruption; subprocess.run only kills its direct child.
    deadline = time.monotonic() + timeout
    progress_logged = -float('inf')
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    with subprocess.Popen(command, cwd=HOME, env=env or ENV, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if progress:
                        detail = progress()
                        if console:
                            console.detail = detail
                        if detail and time.monotonic() - progress_logged >= 5:
                            log.write(f'[{now()}] {detail}\n'); log.flush()
                            if verbose:
                                print(detail, file=console.stderr, flush=True)
                            progress_logged = time.monotonic()
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
                        if verbose:
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
    entry = {'artifact': config.get('vars', {}).get('marktext_artifact')}
    if not entry['artifact']:
        entry = config.get('bootstrap', {}).get('packages', {}).get(key)
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
    app = APPLICATIONS / entry['artifact']
    info = app / 'Contents/Info.plist'
    installed = plistlib.loads(info.read_bytes())['CFBundleShortVersionString'] if info.exists() else None
    url = asset['browser_download_url']
    expected_url = f'https://github.com/marktext/marktext/releases/download/{tag}/{asset["name"]}'
    if url != expected_url:
        raise RuntimeError('MarkText download is outside its official release path')
    digest = asset.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest or ''):
        raise RuntimeError('Official MarkText artifact has no valid SHA-256 digest; update deferred')
    if installed and release_version(version) <= release_version(installed):
        if not check_only:
            command(['/usr/bin/codesign', '--verify', '--strict', str(app)], log, timeout=120)
        if not check_only:
            resolved = {'version': version, 'url': url, 'sha256': digest[7:], 'artifact': entry['artifact'], 'adopt': True, 'os': 'macos/arm64'}
            generated_marktext(resolved)
            atomic_write(STATE / 'marktext/release.json', json.dumps(dict(resolved, installed=installed, verified_at=now()), indent=2) + '\n')
        return {'state': 'current', 'installed': installed, 'eligible_release': version}
    if check_only:
        return {'state': 'update-available', 'installed': installed, 'available': version}
    if is_running(app):
        return {'state': 'deferred-running-app', 'installed': installed, 'available': version}
    # The immutable manifest describes the resolver; machine release metadata
    # lives outside Git. A separate invocation reads this isolated config, so a
    # fresh bootstrap never applies stale release fields cached by its parent.
    resolved = {'version': version, 'url': url, 'sha256': digest[7:],
                'artifact': entry['artifact'], 'adopt': True, 'os': 'macos/arm64'}
    isolated = generated_marktext(resolved)
    command([MISE, 'bootstrap', 'packages', 'apply', key, '-y'], log, env=isolated)
    command(['/usr/bin/codesign', '--verify', '--strict', str(app)], log, timeout=120)
    actual = plistlib.loads(info.read_bytes())['CFBundleShortVersionString']
    if actual != version:
        raise RuntimeError(f'MarkText reports {actual}, expected {version}')
    atomic_write(STATE / 'marktext/release.json', json.dumps(dict(resolved, verified_at=now()), indent=2) + '\n')
    return {'state': 'updated', 'installed': version, 'sha256': digest[7:]}


def generated_marktext(resolved):
    config_dir = STATE / 'marktext'
    generated = config_dir / 'config.toml'
    text = '[bootstrap.packages."macos-app:marktext"]\n' + ''.join(
        f'{key} = {json.dumps(value)}\n' for key, value in resolved.items())
    tomllib.loads(text)
    atomic_write(generated, text)
    return dict(ENV, MISE_CONFIG_DIR=str(config_dir),
                MISE_GLOBAL_CONFIG_FILE=str(generated),
                MISE_SYSTEM_CONFIG_FILE=str(config_dir / 'absent-system.toml'),
                MISE_OVERRIDE_CONFIG_FILENAMES='__no_project_config__.toml',
                MISE_OVERRIDE_TOOL_VERSIONS_FILENAMES='__no_tool_versions__')


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
    if name == 'setup':
        return inventory()[0]
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
        artifact = config.get('vars', {}).get('marktext_artifact') or config.get('bootstrap', {}).get('packages', {}).get('macos-app:marktext', {}).get('artifact')
        return app_snapshot(APPLICATIONS / artifact, 'MarkText') if artifact else {}
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



def inventory():
    """Observed host state; failures remain explicit and never imply removal."""
    config = tomllib.loads(CONFIG.read_text())
    stages = ['mise', 'tools', 'brew', 'shell-repositories', 'shell-completions', 'variable-fonts', 'marktext']
    if 'rust' in config.get('tools', {}):
        stages.append('rustup')
    stages += [key for key in config.get('bootstrap', {}).get('packages', {}) if key.startswith('brew-cask:')]
    values, errors = {}, {}
    for stage in stages:
        try:
            for key, value in snapshot(stage).items():
                values[stage + '/' + key] = dict(value, stage=stage)
        except Exception as error:
            errors[stage] = str(error)
    return values, errors


def load_report(path):
    if not path.exists():
        return None
    return json.loads(path.read_text())


def rotate_logs(limit=5_000_000):
    # Copy and truncate: launchd's open stdout/stderr descriptors keep writing
    # into the same inode. Renaming would leave an unbounded orphaned log.
    for path in [LOG, LOG.parent / 'mise-maintenance-agent.log', LOG.parent / 'mise-maintenance-agent.err.log']:
        if path.exists() and path.stat().st_size > limit:
            shutil.copyfile(path, path.with_suffix('.previous.log'))
            with path.open('r+') as output:
                output.truncate()


def save_report(report, scheduled=False, keep=60):
    destination = 'last-check.json' if report['check_only'] else 'last-run.json'
    text = json.dumps(report, indent=2) + '\n'
    atomic_write(STATE / destination, text)
    if not report['check_only']:
        atomic_write(STATE / ('last-scheduled.json' if scheduled else 'last-manual.json'), text)
    directory = STATE / 'history'
    stamp = datetime.fromisoformat(report['started_at']).strftime('%Y%m%dT%H%M%S.%fZ')
    atomic_write(directory / (stamp + '-' + report['origin'] + '.json'), text)
    for old in sorted(directory.glob('*.json'), reverse=True)[keep:]:
        old.unlink()


def background_changes(previous, before, errors):
    if not previous or 'inventory_after' not in previous:
        return []
    old = previous['inventory_after']
    invalid = set(errors) | set(previous.get('inventory_errors_after', {}))
    observed_stages = {v['stage'] for v in before.values()}
    # Ceasing to declare an app is not proof that it was uninstalled.
    old = {k: v for k, v in old.items() if v['stage'] not in invalid and v['stage'] in observed_stages}
    before = {k: v for k, v in before.items() if v['stage'] not in invalid}
    return compare(old, before)


def cask_result(text, running):
    """Use native platform/version checks; do not guess cask metadata versions."""
    if 'running and updates itself' in text:
        return {'state': 'deferred-running-app', 'availability': 'confirmed'}
    if running and re.search(r'install cask [^/\s]+/[^\s]+', text):
        return {'state': 'deferred-running-app', 'availability': 'confirmed'}
    if 'skipped:' in text or 'WARN' in text:
        return {'state': 'availability-unknown', 'notes': ['Native check could not prove an eligible update; see the detailed log.']}
    if 'already up to date' in text or 'already installed' in text:
        return {'state': 'current'}
    if running:
        return {'state': 'availability-unknown', 'notes': ['Native check could not prove an eligible update; see the detailed log.']}
    return {'state': 'success'}


VENDOR_APPS = {
    'brew-cask:cursor': ('Cursor.app', 'com.todesktop.230313mzl4w4u92'),
    'brew-cask:google-chrome': ('Google Chrome.app', 'com.google.Chrome'),
    'brew-cask:chatgpt': ('ChatGPT.app', 'com.openai.codex'),
}
SPARKLE = '{http://www.andymatuschak.org/xml-namespaces/sparkle}'


def numeric_version(value):
    # Compare only known numeric vendor formats, never a marketing version
    # against an unrelated bundle build or Cursor's artifact/source hashes.
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', value):
        raise ValueError(f'Unsupported vendor version: {value!r}')
    return tuple(map(int, value.split('.')))


def compare_numeric_versions(installed, available):
    old, new = numeric_version(installed), numeric_version(available)
    if len(old) != len(new):
        raise ValueError(f'Incompatible version formats: {installed!r} and {available!r}')
    return (old > new) - (old < new)


def vendor_release(key):
    arm = platform.machine() in ('arm64', 'aarch64')
    if key == 'brew-cask:cursor':
        arch = 'arm64' if arm else 'x64'
        source = f'https://api2.cursor.sh/updates/api/update/darwin-{arch}/cursor/0.0.0/stable'
        data = json_url(source)
        version = data['name']
        numeric_version(version)
        if not re.fullmatch(r'https://downloads\.cursor\.com/production/[0-9a-f]{40}/darwin/' + arch + r'/Cursor-darwin-' + arch + r'\.zip', data['url']):
            raise ValueError('Unexpected Cursor stable artifact')
        return {'version': version, 'source': source, 'channel': 'stable'}
    if key == 'brew-cask:google-chrome':
        # A partial Chrome rollout is not the release the stable DMG serves.
        source = 'https://versionhistory.googleapis.com/v1/chrome/platforms/mac/channels/stable/versions/all/releases?filter=endtime%3Dnone,fraction%3D1'
        data = json_url(source)
        if data.get('nextPageToken'):
            raise ValueError('Google stable release response was paginated; latest release is unverified')
        versions = [r['version'] for r in data['releases']
                    if r.get('fraction') == 1 and not r.get('serving', {}).get('endTime')]
        if not versions:
            raise ValueError('Google returned no fully rolled-out macOS stable release')
        return {'version': max(versions, key=numeric_version), 'source': source, 'channel': 'stable (fully rolled out)'}
    if key == 'brew-cask:chatgpt':
        suffix = '' if arm else '-x64'
        source = f'https://persistent.oaistatic.com/codex-app-prod/appcast{suffix}.xml'
        req = Request(source, headers={'User-Agent': 'mise-maintenance'})
        with urlopen(req, timeout=30) as response:
            feed = ET.fromstring(response.read(2_000_000))
        os_version = numeric_version(read_command(['/usr/bin/sw_vers', '-productVersion']))
        releases = []
        for item in feed.findall('./channel/item'):
            if item.findtext(SPARKLE + 'channel', 'stable') not in ('stable', 'release'):
                continue
            hardware = item.findtext(SPARKLE + 'hardwareRequirements')
            if hardware and hardware != ('arm64' if arm else 'x86_64'):
                continue
            minimum = item.findtext(SPARKLE + 'minimumSystemVersion', '0')
            target = numeric_version(minimum)
            width = max(len(os_version), len(target))
            if os_version + (0,) * (width - len(os_version)) < target + (0,) * (width - len(target)):
                continue
            enclosure = item.find('enclosure')
            if enclosure is None or enclosure.get('url') is None:
                continue
            version = item.findtext(SPARKLE + 'shortVersionString')
            build = item.findtext(SPARKLE + 'version')
            numeric_version(version); numeric_version(build)
            arch = 'arm64' if arm else 'x64'
            if enclosure.get('url') != f'https://persistent.oaistatic.com/codex-app-prod/ChatGPT-darwin-{arch}-{version}.zip':
                raise ValueError('Unexpected ChatGPT stable artifact')
            releases.append({'version': version, 'build': build, 'source': source, 'channel': 'stable'})
        if not releases:
            raise ValueError('No compatible stable ChatGPT release in official feed')
        return max(releases, key=lambda r: numeric_version(r['build']))
    return None


def assess_vendor_release(key, release):
    filename, identifier = VENDOR_APPS[key]
    app = APPLICATIONS / filename
    if not app.exists():
        available = release['version']
        if release.get('build'):
            available += f" (build {release['build']})"
        return {'state': 'update-available', 'installed': None, 'available': available,
                'release': release, 'checked_at': now()}
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != identifier:
        raise ValueError(f'Unexpected bundle identity for {filename}')
    if key == 'brew-cask:cursor':
        product = json.loads((app / 'Contents/Resources/app/product.json').read_text())
        if product.get('quality') != 'stable':
            raise ValueError('Installed Cursor is not on the stable channel')
    installed, available = info['CFBundleShortVersionString'], release['version']
    comparison = compare_numeric_versions(installed, available)
    # Sparkle orders ChatGPT builds, so a same-version rebuild is an update.
    if key == 'brew-cask:chatgpt':
        build_comparison = compare_numeric_versions(info['CFBundleVersion'], release['build'])
        if comparison and comparison != build_comparison:
            raise ValueError('ChatGPT marketing and build versions disagree on ordering')
        comparison = build_comparison
        installed += f" (build {info['CFBundleVersion']})"
        available += f" (build {release['build']})"
    state = 'current' if comparison == 0 else 'newer' if comparison > 0 else 'update-available'
    return {'state': state, 'installed': installed, 'available': available,
            'release': release, 'checked_at': now()}


def cask_update(key, check, log, command):
    release, vendor_error = None, None
    if key in VENDOR_APPS:
        try:
            release = vendor_release(key)
            app = APPLICATIONS / VENDOR_APPS[key][0]
            if app.exists():
                assessment = assess_vendor_release(key, release)
                log.write(f'[{now()}] {key} official vendor check: {json.dumps(assessment)}\n')
                if assessment['state'] in ('current', 'newer'):
                    return {'state': assessment['state'], 'vendor_check': assessment}
        except Exception as error:
            vendor_error = str(error)
            log.write(f'[{now()}] {key} vendor check unavailable: {error}\n')
            if release is not None:
                # Preserve unexpected channels, identities, and version formats.
                return {'state': 'availability-unknown', 'vendor_check':
                        {'state': 'unverified', 'error': vendor_error, 'checked_at': now()}}
    result = native_cask_update(key, check, log, command)
    if release is not None and vendor_error is None:
        # Re-read the live bundle after native installation; reuse this run's
        # release evidence rather than racing a second network metadata fetch.
        assessment = assess_vendor_release(key, release)
        result['vendor_check'] = assessment
        result['availability'] = 'confirmed'
        result.pop('notes', None)
        if assessment['state'] in ('current', 'newer'):
            result['state'] = assessment['state']
        else:
            app = APPLICATIONS / VENDOR_APPS[key][0]
            result['state'] = 'update-available' if check or not is_running(app) else 'deferred-running-app'
            result['plans'] = [{'name': label(key), 'kind': 'version',
                                'before': assessment['installed'], 'after': assessment['available']}]
            if not check and result['state'] == 'update-available':
                result['notes'] = ['Vendor confirms an update, but the native installer did not apply it. '
                                   'Its cask may lag the vendor release; use the app updater or retry later.']
    elif vendor_error:
        result['vendor_check'] = {'state': 'unverified', 'error': vendor_error, 'checked_at': now()}
        result.setdefault('notes', []).append(f'Official vendor check unavailable: {vendor_error}')
        if result['state'] in ('current', 'success'):
            result['state'] = 'availability-unknown'
    return result


def native_cask_update(key, check, log, command):
    try:
        apps = cask_apps(key.split(':', 1)[1])
    except RuntimeError:
        # Missing packages are installed by apply. Existing ownership errors
        # are still caught by the observer and the native mutation checks.
        apps = []
    running = any(is_running(app) for app in apps)
    start = log.tell()
    if check or running:
        if check:
            command([MISE, 'bootstrap', 'packages', 'apply', key, '-y', '--dry-run'], log, timeout=120)
        command([MISE, 'bootstrap', 'packages', 'upgrade', key, '-y', '--dry-run'], log, timeout=120)
        log.flush(); log.seek(start); text = log.read(); log.seek(0, 2)
        result = cask_result(text, running)
        if check:
            result['state'] = 'checked'
        return result
    command([MISE, 'bootstrap', 'packages', 'apply', key, '-y'], log,
            timeout=1800, progress=CaskDownloadProgress(key))
    start = log.tell()
    command([MISE, 'bootstrap', 'packages', 'upgrade', key, '-y'], log,
            timeout=1800, progress=CaskDownloadProgress(key))
    log.flush(); log.seek(start); text = log.read(); log.seek(0, 2)
    return cask_result(text, False)


def health():
    checks = {}
    def check(name, action):
        try:
            checks[name] = action()
        except Exception as error:
            checks[name] = {'state': 'failed', 'detail': str(error)}
    def doctor():
        output = read_command([MISE, 'doctor'])
        warning = re.search(r'\d+ warnings? found:\s*(.*?)\n\nNo problems found', output, re.S)
        return {'state': 'warning' if warning else 'healthy', 'detail': warning[1].strip() if warning else 'No Mise doctor errors or warnings', 'output': output}
    check('mise-doctor', doctor)
    def service(name):
        output = read_command(['/bin/launchctl', 'print', f'gui/{os.getuid()}/dev.mise.{name}'])
        code = re.search(r'last exit code = (\d+)', output)
        if code and int(code[1]):
            raise RuntimeError(f'Last service exit code: {code[1]}')
        if name == 'mise-tool-update' and 'state = running' not in output:
            raise RuntimeError('Tool update watcher is loaded but not running')
        if name == 'mise-maintenance' and '--scheduled' not in output:
            raise RuntimeError('Loaded maintenance agent lacks scheduled-run tracking; run setup:sync')
        return {'state': 'healthy', 'detail': 'running' if 'state = running' in output else 'loaded; last exit successful' if code else 'loaded; has not exited yet'}
    for name in ['mise-tool-update', 'mise-maintenance']:
        check(name, lambda n=name: service(n))
    def freshness():
        report = load_report(STATE / 'last-scheduled.json')
        if not report:
            raise RuntimeError('No scheduled run recorded; manual runs do not count')
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(report['finished_at'])).total_seconds()
        if report['state'] != 'success':
            raise RuntimeError(f"Last scheduled run {report['state']}: {report.get('errors', [])}")
        if age < 0 or age > 36 * 3600:
            raise RuntimeError(f'Last scheduled run is {age / 3600:.1f} hours old (limit 36h)')
        return {'state': 'healthy', 'detail': f'{age / 3600:.1f} hours ago', 'finished_at': report['finished_at']}
    check('scheduled-freshness', freshness)
    return {'state': 'healthy' if all(c['state'] == 'healthy' for c in checks.values()) else 'attention-needed', 'checked_at': now(), 'checks': checks}


def show_health(console):
    report = health()
    if console.json_output:
        console.say(json.dumps(report, indent=2), force=True)
    else:
        console.say('Automatic update health: ' + report['state'])
        for name, result in report['checks'].items():
            console.say(f"  {'✓' if result['state'] == 'healthy' else '✗'} {name}: {result['detail']}")
    return 0 if report['state'] == 'healthy' else 1


def show_history(console, limit):
    reports = [load_report(path) for path in sorted((STATE / 'history').glob('*.json'), reverse=True)[:limit]]
    if console.json_output:
        console.say(json.dumps(reports, indent=2), force=True)
    elif not reports:
        console.say('No maintenance history recorded yet.')
    else:
        for report in reports:
            local = datetime.fromisoformat(report['started_at']).astimezone().strftime('%Y-%m-%d %H:%M')
            console.say(f"{local}  {report['origin']:9} {report['state']:11} {len(report.get('changes', []))} changes · {len(report.get('since_previous_run', []))} between runs · {len(report.get('errors', []))} errors")
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Preview updates without applying them')
    parser.add_argument('--status', action='store_true', help='Print the most recent scheduled/manual run')
    parser.add_argument('--verbose', action='store_true', help='Stream command output as well as saving the log')
    parser.add_argument('--json', action='store_true', dest='json_output', help='Print only the structured report on stdout')
    parser.add_argument('--scheduled', action='store_true', help='Tag this invocation as a launchd run')
    parser.add_argument('--health', action='store_true', help='Verify doctor, services, and scheduled freshness')
    parser.add_argument('--history', action='store_true', help='Show bounded local report history')
    parser.add_argument('--limit', type=int, default=10, choices=range(1, 61), metavar='1..60')
    parser.add_argument('--marktext', action='store_true', help='Resolve and apply MarkText for bootstrap')
    args = parser.parse_args(argv)
    console = Console(verbose=args.verbose, json_output=args.json_output)
    command = partial(run, console=console)
    if args.health:
        return show_health(console)
    if args.history:
        return show_history(console, args.limit)
    if args.marktext:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open('a+', buffering=1) as log:
            result = marktext(args.check, log, console)
        console.say(json.dumps(result, indent=2), force=True)
        return 0
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
        rotate_logs()
        started = time.monotonic()
        report = {'started_at': now(), 'check_only': args.check, 'results': {}, 'errors': [], 'log': str(LOG), 'schema_version': 3, 'origin': 'preview' if args.check else 'scheduled' if args.scheduled else 'manual'}
        previous = load_report(STATE / 'last-run.json')
        report['inventory_before'], report['inventory_errors_before'] = inventory()
        report['since_previous_run'] = background_changes(previous, report['inventory_before'], report['inventory_errors_before'])
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
                    if args.check and not result.get('vendor_check') and result['state'] not in ('failed', 'interrupted'):
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
                                            'before': result.get('installed'), 'after': result['available']}]
                    if name == 'shell-repositories' and before is not None:
                        dirty = [record for record in before.values() if record.get('dirty')]
                        result.setdefault('notes', []).extend(f"{record['name']} preserved because it has local edits" for record in dirty)
                        if dirty:
                            native_notes = [note for note in native_notes if 'has local changes' not in note]
                    if result.get('vendor_check', {}).get('state') in ('current', 'newer', 'update-available'):
                        native_notes = [note for note in native_notes if note not in
                                        ('installed app is current, newer, or incomparable',
                                         'installed app is running and updates itself')]
                    if native_notes:
                        result['notes'] = list(dict.fromkeys(result.get('notes', []) + native_notes))
                    if observation_errors:
                        result['observation_errors'] = observation_errors
                    result['elapsed_seconds'] = round(time.monotonic() - stage_started, 3)
                    report['results'][name] = result
                    console.finished(name, result)
                finally:
                    console.stop_progress()
            step('shell-repositories', lambda: command([MISE, 'bootstrap', 'repos', 'update', '--skip-dirty', '-y'] + (['--dry-run'] if args.check else []), log, timeout=600))
            if not args.check:
                # A fresh process reads the pulled manifest before applying it.
                # Native packages use their guarded per-package stages below.
                # Reloading our own LaunchAgent would interrupt this process.
                skip = 'packages,macos-launchd-agents' if args.scheduled else 'packages'
                step('setup', lambda: command([MISE, 'bootstrap', '--skip', skip, '--skip-dirty', '-y'], log))
                step('mise', lambda: command([MISE, 'self-update', '-y', '--no-plugins'], log, timeout=600))
            step('tools', lambda: command([MISE, 'outdated', '--global', '--json'] if args.check else [MISE, 'upgrade', '--global', '-y'], log))
            if 'rust' in tomllib.loads(CONFIG.read_text()).get('tools', {}):
                step('rustup', lambda: command([HOME / '.cargo/bin/rustup', 'check'] if args.check else [HOME / '.cargo/bin/rustup', 'self', 'update'], log, timeout=600))

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
                def native_update(m=manager):
                    command([MISE, 'bootstrap', 'packages', 'apply', '--manager', m, '-y'] + (['--dry-run'] if args.check else []), log)
                    command([MISE, 'bootstrap', 'packages', 'upgrade', '--manager', m, '-y'] + (['--dry-run'] if args.check else []), log)
                step(manager, native_update)
            for key in packages:
                if not key.startswith('brew-cask:'):
                    continue
                step(key, lambda k=key: cask_update(k, args.check, log, command))
            report['finished_at'] = now()
            report['elapsed_seconds'] = round(time.monotonic() - started, 3)
            report['state'] = 'interrupted' if report.get('interrupted') else 'failed' if report['errors'] else 'success'
            report['changes'] = [dict(change, stage=name) for name, result in report['results'].items() for change in result.get('changes', [])]
            report['planned_changes'] = [dict(plan, stage=name) for name, result in report['results'].items() for plan in result.get('plans', [])]
            report['inventory_after'], report['inventory_errors_after'] = inventory()
            previous_results = (previous or {}).get('results', {})
            for name, result in report['results'].items():
                if result['state'] == 'deferred-running-app':
                    old = previous_results.get(name, {})
                    result['deferred_since'] = old.get('deferred_since', report['started_at']) if old.get('state') == 'deferred-running-app' else report['started_at']
                    result['consecutive_deferrals'] = old.get('consecutive_deferrals', 0) + 1 if old.get('state') == 'deferred-running-app' else 1
            save_report(report, args.scheduled)
            console.summary(report)
            return 130 if report.get('interrupted') else 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
