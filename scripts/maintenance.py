#!/usr/bin/env python3
"""Update this Mac's global mise tools, packages, and official MarkText release."""
import argparse
import codecs
from datetime import datetime, timezone, timedelta
import fcntl
from functools import partial
import json
import os
from pathlib import Path
import plistlib
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
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
        'font-poppins': 'Poppins fonts',
    }
    name = name.removeprefix('brew-cask:')
    return labels.get(name, name.replace('-', ' ').title())


def outcome(result):
    state = result['state']
    if state == 'failed':
        return '✗', result.get('error', 'Failed'), '31'
    if state == 'deferred-running-app':
        return '↷', 'Deferred — app is running', '33'
    if state == 'update-available':
        return '↑', f"Update available: {result.get('installed', '?')} → {result.get('available', '?')}", '33'
    if state == 'updated':
        return '✓', f"Updated to {result.get('installed', '?')}", '32'
    if state == 'current':
        return '✓', f"Current ({result.get('installed', '?')})", '32'
    if state == 'not-configured':
        return '–', 'Not configured', '33'
    # A successful command may check or install; do not claim a version changed.
    return '✓', 'Check completed' if state == 'checked' else 'Completed', '32'


class Console:
    def __init__(self, *, verbose=False, json_output=False):
        self.verbose = verbose
        self.json_output = json_output
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        self.color = self.stdout.isatty() and os.environ.get('TERM') != 'dumb' and 'NO_COLOR' not in os.environ

    def paint(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def say(self, text=''):
        if not self.json_output:
            print(text, file=self.stdout, flush=True)

    def starting(self, name, check_only):
        self.say(f"… {'Checking' if check_only else 'Processing'} {label(name)}…")

    def finished(self, name, result):
        symbol, detail, color = outcome(result)
        elapsed = f"  {result['elapsed_seconds']:.1f}s" if 'elapsed_seconds' in result else ''
        self.say(f"{self.paint(symbol, color)} {label(name)}: {detail}{elapsed}")

    def summary(self, report, *, show_steps=False):
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
            self.say(f"Last maintenance run: {report.get('started_at', 'unknown')}")
            for name, result in report.get('results', {}).items():
                self.finished(name, result)
        results = report.get('results', {})
        failed = [name for name, result in results.items() if result['state'] == 'failed']
        deferred = [name for name, result in results.items() if result['state'] == 'deferred-running-app']
        available = [name for name, result in results.items() if result['state'] == 'update-available']
        completed = sum(result['state'] in ('success', 'checked', 'current', 'updated') for result in results.values())
        elapsed = report.get('elapsed_seconds')
        if elapsed is None and report.get('finished_at') and report.get('started_at'):
            elapsed = (datetime.fromisoformat(report['finished_at']) - datetime.fromisoformat(report['started_at'])).total_seconds()
        duration = f' in {elapsed:.1f}s' if elapsed is not None else ''
        preview = 'Preview' if report.get('check_only') else 'Maintenance'
        good = report['state'] == 'success'
        self.say()
        self.say(self.paint(f"{'✓' if good else '✗'} {preview} {'completed' if good else 'finished with errors'}{duration}", '32' if good else '31'))
        error_label = "error" if len(failed) == 1 else "errors"
        self.say(f"  {completed} stages completed · {len(failed)} {error_label} · {len(deferred)} deferred")
        if deferred:
            self.say('  Deferred: ' + ', '.join(map(label, deferred)) + ' — close these apps and retry, or wait for a later run.')
        if available:
            self.say('  Updates available: ' + ', '.join(map(label, available)))
        if failed and not show_steps:
            for name in failed:
                self.say(f"  {label(name)}: {results[name].get('error', 'Failed')}")
        self.say(f"  Detailed log: {report.get('log', str(LOG))}")


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
        report = {'started_at': now(), 'check_only': args.check, 'results': {}, 'errors': [], 'log': str(LOG)}
        console.say('Previewing maintenance updates' if args.check else 'Running maintenance updates')
        console.say()
        with LOG.open('a', buffering=1) as log:
            def step(name, action):
                console.starting(name, args.check)
                stage_started = time.monotonic()
                try:
                    result = action()
                    report['results'][name] = result or {'state': 'success'}
                except Exception as error:
                    report['results'][name] = {'state': 'failed', 'error': str(error)}
                    report['errors'].append(name)
                    log.write(f'[{now()}] {name}: {error}\n')
                report['results'][name]['elapsed_seconds'] = round(time.monotonic() - stage_started, 3)
                console.finished(name, report['results'][name])
            if not args.check:
                step('mise', lambda: command([MISE, 'self-update', '-y', '--no-plugins'], log, timeout=600))
            step('tools', lambda: command([MISE, 'upgrade', '--global', '-y'] + (['--dry-run'] if args.check else []), log))
            if 'rust' in tomllib.loads(CONFIG.read_text()).get('tools', {}):
                step('rustup', lambda: command([HOME / '.cargo/bin/rustup', 'check'] if args.check else [HOME / '.cargo/bin/rustup', 'self', 'update'], log, timeout=600))
            step('shell-repositories', lambda: command([MISE, 'bootstrap', 'repos', 'update', '--skip-dirty', '-y'] + (['--dry-run'] if args.check else []), log, timeout=600))
            if not args.check:
                step('shell-completions', lambda: command([MISE, 'run', 'shell:completions'], log, timeout=120))
            step('variable-fonts', lambda: command([MISE, 'run', 'fonts:check' if args.check else 'fonts:update'], log, timeout=300))
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
            report['state'] = 'failed' if report['errors'] else 'success'
            atomic_write(STATE / ('last-check.json' if args.check else 'last-run.json'), json.dumps(report, indent=2) + '\n')
            console.summary(report)
            return 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
