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
import operator
from pathlib import Path
import plistlib
import platform
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import threading
import tomllib
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen

if __package__:
    from . import dependency_audit, audit_reviews, desktop_releases, vendor_install
    from .github_public import github_json
else:
    import dependency_audit, audit_reviews, desktop_releases, vendor_install
    from github_public import github_json

HOME = Path.home()
CONFIG = HOME / '.config/mise/config.toml'
MISE = HOME / '.local/bin/mise'
STATE = HOME / '.local/state/mise/maintenance'
SCHEDULED_LOCK_WAIT = 120
LOG = HOME / 'Library/Logs/mise-maintenance.log'
APPLICATIONS = Path('/Applications')
ENV = dict(os.environ, PATH=f'{HOME}/.local/bin:{HOME}/.local/share/mise/shims:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin', MISE_YES='1')
# Machine maintenance must never inherit a project's config or version overrides.
for key in list(ENV):
    if key.startswith('MISE_') and key != 'MISE_YES':
        del ENV[key]


def now():
    return datetime.now(timezone.utc).isoformat()


def acquire_update_lock(lock, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(1, remaining))


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
        'shell-completions': 'Shell completions', 'variable-fonts': 'Variable fonts and dotfiles',
        'marktext': 'MarkText', 'brew': 'Native packages', 'setup': 'Declared setup',
        'visual-studio-code': 'Visual Studio Code', 'google-chrome': 'Google Chrome',
        'github': 'GitHub Desktop', 'iterm2': 'iTerm2', 'orbstack': 'OrbStack',
        'chatgpt': 'ChatGPT', 'codexbar': 'CodexBar', 'daisydisk': 'DaisyDisk',
        'devutils': 'DevUtils', 'macs-fan-control': 'Macs Fan Control',
        'font-poppins': 'Poppins fonts', 'node': 'Node.js', 'python': 'Python',
        'gh': 'GitHub CLI', 'cargo:zpl_toolchain_cli': 'ZPL toolchain',
        'brew-cask:claude': 'Claude Desktop', 'claude': 'Claude Code', 'audit': 'Security audit', 'dependency-audit': 'Tool dependencies', 'osv-scanner': 'OSV-Scanner',
        'brew-cask:antigravity': 'Antigravity Desktop', 'agy': 'Antigravity CLI',
        'github:skw398/scap': 'Scap', 'uv': 'uv', 'pnpm': 'pnpm',
    }
    short = name.removeprefix('brew-cask:').removeprefix('brew:')
    return labels.get(name) or labels.get(short, short.replace('-', ' ').title())


def format_change(change, *, planned=False):
    name, kind = change['name'], change['kind']
    if name == '.oh-my-zsh':
        name = 'Oh My Zsh'
    old, new = change.get('before'), change.get('after')
    if kind == 'availability' and planned:
        return f'{name}: update available for {old}'
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
    if 'match_count' in result:
        return '·', plural(result['match_count'], 'dependency match', 'dependency matches'), '33' if result['match_count'] else '32'
    if state in ('clear', 'advisories'):
        count = len(result['vulnerabilities'])
        return ('!', plural(count, 'advisory', 'advisories'), '33') if count else ('✓', 'No known advisories for checked tools', '32')
    if state == 'self-updating':
        return '·', 'Uses its own updater while open', '2'
    if state == 'deferred-running-app':
        return '↷', 'Deferred — app is running', '33'
    if result.get('changes'):
        return '✓', '; '.join(format_change(c) for c in result['changes']), '32'
    if result.get('plans'):
        return '↑', '; '.join(format_change(c, planned=True) for c in result['plans']), '33'
    if state == 'availability-unknown':
        return '!', 'Update availability unverified', '33'
    if result.get('plan_issues'):
        return '!', 'Update preview incomplete', '33'
    if result.get('observation_errors'):
        return '!', 'Completed; change verification unavailable', '33'
    if state == 'updated':
        return '✓', 'Mise reported a native upgrade', '32'
    if state == 'update-available':
        return '↑', f"Update available: {result.get('installed', '?')} → {result.get('available', '?')}", '33'
    vendor = result.get('vendor_check', {})
    if vendor.get('state') in ('current', 'newer'):
        return '✓', f"{'Current' if vendor['state'] == 'current' else 'Newer than stable'} ({vendor['installed']})", '32'
    return '✓', 'Unchanged' if result.get('before') is not None else 'Check completed', '32'


def plural(count, singular, multiple=None):
    return f'{count} {singular if count == 1 else multiple or singular + "s"}'


def changed_rows(changes):
    rows, families = [], {}
    for change in changes:
        if change['kind'] != 'font':
            rows.append(format_change(change))
            continue
        family = next((name for prefix, name in [('Poppins', 'Poppins'), ('OpenSans', 'Open Sans'),
                      ('Rubik', 'Rubik'), ('SpaceGrotesk', 'Space Grotesk')] if change['name'].startswith(prefix)), change['name'])
        families.setdefault(family, []).append(change.get('action', 'updated'))
    for name, actions in families.items():
        verb = actions[0] if len(set(actions)) == 1 else 'changed'
        rows.append(f"{name}: {plural(len(actions), 'font file')} {verb}")
    return rows


def plain(text):
    # Prevent terminal controls in upstream diagnostics from affecting the layout.
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(text))
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())


class Console:
    def __init__(self, *, verbose=False, json_output=False):
        self.verbose, self.json_output = verbose, json_output
        self.stdout, self.stderr = sys.stdout, sys.stderr
        self.terminal = self.stdout.isatty() and os.environ.get('TERM') != 'dumb'
        self.color = self.terminal and 'NO_COLOR' not in os.environ
        self._stop = threading.Event()
        self._thread = None
        self._width = 0
        self.completed_steps = 0
        self.detail = ''

    def paint(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def say(self, text='', force=False):
        if force or not self.json_output:
            print(text, file=self.stdout, flush=True)

    @property
    def columns(self):
        return max(12, min(100, shutil.get_terminal_size((88, 24)).columns - 1))

    def line(self, text, *, indent=0, color=None):
        wrapped = textwrap.fill(plain(text), width=self.columns, initial_indent=' ' * indent,
                                subsequent_indent=' ' * indent, break_on_hyphens=False)
        self.say(self.paint(wrapped, color) if color else wrapped)

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
                    text = f'{frames[index % len(frames)]} Step {self.completed_steps + 1} · {label(name)}{detail}  {time.monotonic() - start:.0f}s'
                    columns = shutil.get_terminal_size((80, 24)).columns
                    text = text[:max(1, columns - 1)]
                    self.stdout.write('\r\033[2K' + text)
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
            self.stdout.write('\r\033[2K')
            self.stdout.flush()
            self._width = 0

    def finished(self, name, result):
        self.stop_progress()
        self.completed_steps += 1
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
        if report['state'] == 'skipped-busy':
            self.say(f"Scheduled maintenance skipped after waiting {report['waited_seconds']:.0f}s for another run to finish.")
            return
        if show_steps:
            stamp = report.get('started_at')
            self.say('Last run: ' + (datetime.fromisoformat(stamp).astimezone().strftime('%Y-%m-%d %H:%M %Z') if stamp else 'unknown'))
            if self.verbose:
                for name, result in report.get('results', {}).items():
                    self.finished(name, result)
        results = report.get('results', {})
        failures, security, waiting, limits, notes, available = [], [], [], [], [], []
        replaced_apps = []
        plans = report.get('planned_changes', [])
        preview = report.get('check_only', False)
        for name, result in results.items():
            title, state = label(name), result['state']
            vendor = result.get('vendor_check', {})
            stage_plans = [p for p in plans if p.get('stage') == name] or result.get('plans', [])
            messages = [msg.strip() for msg in result.get('notes', [])]
            # Older reports counted a running self-updater as a confirmed deferral.
            self_updating = state == 'self-updating' or (state in ('checked', 'deferred-running-app') and not stage_plans
                            and not vendor and 'installed app is running and updates itself' in messages)
            if state in ('failed', 'interrupted'):
                failures.append(f"{title}: {result.get('error', 'Failed')}")
            elif self_updating:
                if self.verbose:
                    notes.append(f'{title} uses its own updater while open.')
            elif state == 'deferred-running-app':
                target = ', '.join(format_change(p, planned=True) for p in stage_plans) or title
                waiting.append(f'{target} — close the app before the next run.')
            else:
                available.extend(format_change(p, planned=True) for p in stage_plans)
            ambiguous = 'installed app is current, newer, or incomparable' in messages and not stage_plans and not vendor
            if ambiguous or state == 'availability-unknown' or result.get('plan_unverified') or vendor.get('state') == 'unverified':
                reason = vendor.get('error') or 'Mise could not confirm whether an update is available.'
                limits.append(f'{title}: {reason}')
            if result.get('observation_errors'):
                limits.append(f"{title}: change verification incomplete ({', '.join(result['observation_errors'])}).")
            limits.extend(result.get('plan_issues', []))
            for note in messages:
                if note in ('installed app is running and updates itself', 'installed app is current, newer, or incomparable',
                            NATIVE_AVAILABILITY_NOTE) or (
                            note.startswith('Official vendor check unavailable:') and vendor.get('error')):
                    continue
                if not self.verbose and replacement_app(note):
                    replaced_apps.append(title)
                    continue
                if not self.verbose and result.get('native_upgrade') and retry_notice(note):
                    continue
                hold = re.fullmatch(r'newer (.+?) release (\S+) \(released .+?, eligible (.+?)\) ignored by minimum_release_age .*', note)
                if hold:
                    waiting.append(f'{label(hold[1])} {hold[2]} becomes eligible {hold[3]}.')
                else:
                    notes.append(f'{title}: {note}')
        available.extend(format_change(p, planned=True) for p in plans if p.get('stage') not in results)
        if report.get('inventory_errors_before') or report.get('inventory_errors_after'):
            for stage in sorted(set(report.get('inventory_errors_before', {})) | set(report.get('inventory_errors_after', {}))):
                if not results.get(stage, {}).get('observation_errors'):
                    limits.append(f'{label(stage)}: incomplete inventory for comparisons between runs.')

        audit = results.get('audit', {})
        for finding in audit.get('vulnerabilities', []):
            fixes = ', '.join(finding.get('fixed', []))
            advice = f'Fix listed in {fixes}. Check for an eligible update.' if fixes else 'No fixed version listed. Maintenance will recheck.'
            severity = f" · {finding['severity']}" if finding.get('severity') else ''
            description = finding['summary'].replace('`', '').rstrip('.')
            tool = 'Cargo crate' if finding.get('version_scheme') == 'crate' else label(finding['tool'])
            security.append((f"{tool} {finding['version']}  ·  {finding['id']}{severity}",
                             f"{description}. {advice}"))
        unchecked = ', '.join(label(item['tool']) for item in audit.get('unchecked', [])
                              if not (item['tool'] == 'rust' and audit.get('rust_checks')))
        if unchecked:
            limits.append(f'No advisory source for {unchecked}.')
        dependencies = results.get('dependency-audit', {})
        owners = dependencies.get('owners', {}).values()
        affected = [owner for owner in owners if owner.get('findings')]
        coverage = dependency_coverage(owners)
        elapsed = report.get('elapsed_seconds')
        if elapsed is None and report.get('finished_at') and report.get('started_at'):
            elapsed = (datetime.fromisoformat(report['finished_at']) - datetime.fromisoformat(report['started_at'])).total_seconds()
        changes = report.get('changes', [])
        title = 'Update preview' if preview else 'Maintenance'
        if report['state'] == 'interrupted':
            title, color = title + ' interrupted', '31'
        elif failures or report['state'] == 'failed':
            title, color = title + ' finished with errors', '31'
        elif security or affected or limits or coverage or waiting:
            title, color = title + ' complete', '36'
        else:
            title, color = title + ' complete', '32'
        self.say(self.paint(title, '1;' + color) + (f'  {elapsed:.1f}s' if elapsed is not None else ''))
        counts = [plural(len(results) - len(failures), 'step') + ' completed', plural(len(failures), 'error')]
        if audit.get('state') in ('clear', 'advisories'):
            counts.append(plural(len(security), 'advisory', 'advisories'))
        elif not audit and report.get('schema_version', 0) >= 4:
            counts.append('audit did not run')
        if 'match_count' in dependencies:
            counts.append(plural(dependencies['match_count'], 'dependency match', 'dependency matches'))
        self.say(' · '.join(counts))
        if report.get('schema_version', 0) < 2:
            self.say('This older report did not record installed changes.')
        elif preview:
            self.say('Preview only. No updates applied.')
        elif not changes:
            incomplete = failures or report.get('inventory_errors_before') or report.get('inventory_errors_after') or any(
                r.get('observation_errors') for r in results.values())
            self.say('No changes verified.' if incomplete else 'No changes this run.')
        self.section('Failed', failures, '31')
        self.section('Security advisories', security, '33')
        if affected:
            overview = f"Advisories matched packages in {plural(len(affected), 'tool')}."
            review = (review_overview(dependencies) if 'review_counts' in dependencies else [overview]) + dependency_trend(report)
            if not preview:
                review.append('Review by tool: mise run maintenance:dependencies')
            self.section('Dependency advisories', review, '33')
        self.section('Waiting', waiting, '33')
        self.section('Available to update', available, '1')
        if preview and results.get('mise', {}).get('plans'):
            self.say('  Mise updates automatically. To update now, run mise run maintenance:update.')
        heading = 'Observed during preview' if preview else 'Changed this run'
        self.section(heading + (' (partial results)' if failures else ''), changed_rows(changes), '32')
        self.section('Changed before this run', changed_rows(report.get('since_previous_run', [])), '1')
        self.section('Check coverage', limits, '33')
        self.section('Dependency audit coverage', [f"{reason}: {', '.join(names)}." for reason, names in coverage.items()], '33')
        if replaced_apps:
            apps = list(dict.fromkeys(replaced_apps))
            names = ' and '.join(filter(None, [', '.join(apps[:-1]), apps[-1]]))
            notes.append(f"macOS may ask you to grant permissions again for {names}.")
        self.section('Other notes', notes, '2')
        if failures or self.verbose:
            self.say(self.paint(f"Log  {report.get('log', str(LOG))}", '2'))

    def section(self, heading, rows, color):
        if not rows:
            return
        self.say()
        self.say(self.paint(heading, color))
        width = max(20, min(96, shutil.get_terminal_size((88, 24)).columns - 1))
        for row in dict.fromkeys(rows):
            if isinstance(row, tuple):
                title, detail = row
                self.say(textwrap.fill(plain(title), width=width, initial_indent='  ', subsequent_indent='    '))
                indent = '    '
            else:
                detail, indent = row, '  '
            self.say(textwrap.fill(plain(detail), width=width, initial_indent=indent,
                                   subsequent_indent='    ', break_on_hyphens=False))


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


def command_failure(command, code, tail):
    lines = [plain(line) for line in tail.splitlines() if line.strip()]
    footer = re.compile(r'^mise\s+(?:ERROR\s+)?(?:Version:|Run with --verbose\b)', re.I)
    lines = [line for line in lines if not footer.match(line)]
    mise_errors = [m[1] for line in lines if (m := re.match(r'^mise\s+ERROR\s+(.+)$', line))]
    detail = next((line for line in reversed(lines) if re.search(r'error|failed|denied', line, re.I)), None)
    cause = mise_errors[-1] if mise_errors else detail or (lines[-1] if lines else 'No diagnostic output')
    context = next((line for line in mise_errors if re.match(r'(?:failed|unable) to (?:install|update|upgrade|download)\b', line, re.I)), '')
    if context and context != cause:
        cause = context if cause.casefold() in context.casefold() else f'{context} · {cause}'
    operation = Path(command[0]).name
    if operation == 'mise':
        # Only include Mise's command words, never dump arbitrary shell arguments.
        words = []
        for arg in command[1:]:
            if not re.fullmatch(r'[a-z][a-z0-9:-]*', arg):
                break
            words.append(arg)
        operation = ' '.join([operation, *words])
    return f'Command exited {code}. {operation}: {cause}'


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
    output_tail = ''
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
                        output_tail = (output_tail + text)[-4096:]
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
        raise RuntimeError(command_failure(command, returncode, output_tail))


def rust_component(binary):
    output = read_command([MISE, '-C', HOME, 'exec', '--', binary, '-Vv'])
    version = re.search(r'^release: (\d+\.\d+\.\d+)$', output, re.M)
    commit = re.search(r'^commit-hash: ([0-9a-f]{40})$', output, re.M)
    if not version or not commit:
        raise ValueError(f'Could not identify the stable {binary} release and build commit')
    return {'version': version[1], 'commit': commit[1]}


def cargo_crate_version(commit):
    source = f'https://raw.githubusercontent.com/rust-lang/cargo/{commit}/Cargo.toml'
    with urlopen(source, timeout=20) as response:
        package = tomllib.load(response)['package']
    if package['name'] != 'cargo' or not re.fullmatch(r'0\.\d+\.\d+', package['version']):
        raise ValueError('Unrecognized Cargo crate version scheme at the installed commit')
    return package['version']


def affected_rust_version(requirement, component):
    """GitHub repository advisories use comma-separated inclusive or exclusive bounds."""
    operators = {'<': operator.lt, '<=': operator.le, '=': operator.eq, '>=': operator.ge, '>': operator.gt}
    matches = []
    versions = set()
    for clause in requirement.split(','):
        bound = re.fullmatch(r'\s*(<=|>=|<|>|=)?\s*(\d+\.\d+(?:\.\d+)?)\s*', clause)
        if not bound:
            raise ValueError(f'Unrecognized affected range {requirement!r}')
        operation, limit = bound[1] or '=', numeric_version(bound[2])
        if len(limit) == 2:
            if operation not in ('<', '>='):
                raise ValueError(f'Ambiguous partial version in {requirement!r}')
            limit += (0,)
        # Cargo advisories publish both 0.x crate and 1.x CLI release ranges.
        version = component['crate_version'] if 'crate_version' in component and limit[0] == 0 else component['version']
        versions.add(version)
        matches.append(operators[operation](numeric_version(version), limit))
    if len(versions) != 1:
        raise ValueError(f'Mixed version schemes in {requirement!r}')
    return all(matches), versions.pop()


def rust_repository_audit(repo, evidence, findings):
    source = f'https://api.github.com/repos/rust-lang/{repo}/security-advisories'
    evidence.update(source=source, advisories_checked=0)
    page, seen = 1, set()
    while True:
        records = json_url(f'{source}?state=published&per_page=100&page={page}')
        if not isinstance(records, list):
            raise ValueError('Expected a list of published advisories')
        for record in records:
            ghsa = record['ghsa_id']
            url = f'https://github.com/rust-lang/{repo}/security/advisories/{ghsa}'
            if ghsa in seen or record['html_url'] != url or record['state'] != 'published':
                raise ValueError(f'Unexpected or repeated advisory {ghsa}')
            seen.add(ghsa)
            if record['withdrawn_at']:
                continue
            if not record['vulnerabilities']:
                raise ValueError(f'{ghsa} has no affected version data')
            for affected in record['vulnerabilities']:
                package = (affected['package'].get('name') or '').lower()
                expected = ('cargo',) if repo == 'cargo' else ('rust', 'rustc', 'std')
                if package not in expected:
                    raise ValueError(f'{ghsa} uses an unmapped component {package!r}')
                requirement = affected['vulnerable_version_range']
                applies, version = affected_rust_version(requirement, evidence)
                if applies:
                    identifier = record['cve_id'] or ghsa
                    if not isinstance(record['summary'], str) or not record['summary']:
                        raise ValueError(f'{ghsa} has no advisory summary')
                    if not any(f['id'] == identifier and f.get('url') == url for f in findings):
                        fixes = affected['patched_versions']
                        if fixes is not None and not isinstance(fixes, str):
                            raise ValueError(f'{ghsa} has malformed patched versions')
                        findings.append({'tool': repo, 'version': version, 'component': package,
                                         'version_scheme': 'crate' if version == evidence.get('crate_version') else 'release',
                                         'id': identifier, 'aliases': [ghsa] if identifier != ghsa else [],
                                         'severity': record['severity'], 'summary': record['summary'], 'url': url,
                                         'affected_range': requirement, 'fixed': [fixes] if fixes else []})
            evidence['advisories_checked'] += 1
        if len(records) < 100:
            break
        page += 1


def supplement_rust_audit(result, rust):
    checks = {}
    for repo, binary in [('rust', 'rustc'), ('cargo', 'cargo')]:
        check = checks[repo] = {}
        try:
            check.update(rust_component(binary))
            if repo == 'rust' and check['version'] != rust['version']:
                raise ValueError(f"Active rustc {check['version']} differs from Mise's audited {rust['version']}")
            if repo == 'cargo':
                check['crate_version'] = cargo_crate_version(check['commit'])
            rust_repository_audit(repo, check, result['vulnerabilities'])
            check['state'] = 'checked'
        except Exception as error:
            # One unavailable source must preserve independent, confirmed findings.
            check.update(state='unverified', error=str(error))
    result['rust_checks'] = checks
    errors = [f"{repo}: {c['error']}" for repo, c in checks.items() if c['state'] != 'checked']
    if errors:
        result.update(state='failed', error='Rust advisory coverage incomplete. ' + ' / '.join(errors))
    else:
        result['unchecked'].remove(rust)
        result['state'] = 'advisories' if result['vulnerabilities'] else 'clear'


def audit_tools(log, console):
    """Keep audit exit 1 with a valid finding distinct from an OSV or CLI failure."""
    cmd = [str(MISE), '-C', str(HOME), 'audit', '--json']
    log.write(f'\n[{now()}] {" ".join(cmd)}\n')
    if console.verbose:
        print('$ ' + ' '.join(cmd), file=console.stderr, flush=True)
    completed = subprocess.run(cmd, cwd=HOME, env=ENV, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=120)
    for stream in (completed.stderr, completed.stdout):
        log.write(stream)
        if console.verbose and stream:
            print(stream, end='', file=console.stderr, flush=True)
    try:
        data = json.loads(completed.stdout)
        findings, unchecked = data['vulnerabilities'], data['unchecked']
        if not isinstance(findings, list) or not isinstance(unchecked, list):
            raise ValueError('expected advisory and coverage lists')
        for row in findings + unchecked:
            if not all(isinstance(row[k], str) and row[k] for k in ('tool', 'version')):
                raise ValueError('missing tool or version')
        for row in findings:
            if not all(isinstance(row[k], str) and row[k] for k in ('id', 'summary')):
                raise ValueError('missing advisory ID or summary')
            if not isinstance(row['fixed'], list) or not all(isinstance(v, str) for v in row['fixed']):
                raise ValueError('invalid fixed versions')
        if completed.returncode != int(bool(findings)):
            raise ValueError('exit status disagrees with the reported findings')
    except (ValueError, TypeError, KeyError) as error:
        detail = next((line for line in completed.stderr.splitlines() if 'ERROR' in line), completed.stderr.strip())
        raise RuntimeError(f'Audit unavailable (exit {completed.returncode}). {plain(detail or error)}') from error
    result = {'state': 'advisories' if findings else 'clear', 'vulnerabilities': findings,
              'unchecked': unchecked, 'checked_at': now()}
    rust = next((row for row in unchecked if row['tool'] == 'rust'), None)
    if rust:
        supplement_rust_audit(result, rust)
        detail = json.dumps(result['rust_checks'])
        log.write(f'[{now()}] Rust repository advisory checks: {detail}\n')
        if console.verbose:
            print('Rust repository advisory checks: ' + detail, file=console.stderr, flush=True)
    return result


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
    if url.startswith('https://api.github.com/repos/'):
        return github_json(url)
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
    if name in ('audit', 'dependency-audit'):
        return {}  # Read-only advisory lookup. No installed artifacts to compare.
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
            if not directory.exists() or (directory.is_dir() and not any(directory.iterdir())):
                continue
            result[path] = entity('Mise setup' if directory == CONFIG.parent else directory.name,
                                  read_command(['git', '-C', directory, 'rev-parse', 'HEAD']), 'revision',
                                  dirty=bool(read_command(['git', '-C', directory, 'status', '--porcelain'])))
        return result
    if name == 'shell-completions':
        directory = Path(os.environ.get('XDG_DATA_HOME') or HOME / '.local/share') / 'zsh/site-functions'
        return {'completions': entity('Mise completions', digest(directory / '_mise'), 'content'),
                'codex-completions': entity('Codex completions', digest(directory / '_codex'), 'content')}
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


def compare(before, after, *, unverified_stages=()):
    changes = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if (new or old).get('stage') in unverified_stages:
            continue
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
        plans.append({'name': label('brew-cask:' + package), 'kind': 'version', 'before': values or None, 'after': version})
    return plans


def json_document(text):
    # The font helper may share a log with a nested mise deployment message.
    for match in re.finditer(r'(?m)^\{', text):
        try:
            return json.JSONDecoder().raw_decode(text[match.start():])[0]
        except json.JSONDecodeError:
            continue
    raise RuntimeError('No structured update plan was returned')


def branch_merge_base(path, current, target_ref, target):
    git = ['git', '-C', path]
    try:
        return read_command([*git, 'merge-base', current, target])
    except RuntimeError:
        pass  # Shallow checkouts often lack the new objects.
    url = read_command([*git, 'remote', 'get-url', 'origin'])
    if url.startswith(('./', '../')):
        url = str((path / url).resolve())
    # Keep even FETCH_HEAD and the checkout's remote-tracking refs untouched.
    with tempfile.TemporaryDirectory() as scratch:
        probe = ['git', '-C', scratch]
        read_command([*probe, 'init', '--bare', '-q'])
        read_command([*probe, 'fetch', '--no-tags', '--depth=64', '--filter=tree:0', '--', url, target_ref])
        if read_command([*probe, 'rev-parse', 'FETCH_HEAD']) != target:
            raise ValueError('origin changed during the check; retry the preview')
        if current in read_command([*probe, 'rev-list', 'FETCH_HEAD']).splitlines():
            return current
    return None


def repository_plan(row):
    """Resolve the requested revision without changing refs in the managed checkout."""
    path = Path(row['path'])
    missing = row['state'] == 'missing'
    current = row['current_sha']
    ref = row['ref'] or ('HEAD' if missing else row['current_ref'])
    if not ref or (ref == 'HEAD' and not missing):
        raise ValueError('detached checkout has no branch to update')
    git = ['git'] if missing else ['git', '-C', path]
    remote = row['url'] if missing else 'origin'
    branch, tag = 'refs/heads/' + ref, 'refs/tags/' + ref
    refs = [ref] if ref.startswith('refs/') or ref == 'HEAD' else [branch, tag]
    listed = read_command([*git, 'ls-remote', '--', remote, *refs, *(r + '^{}' for r in refs)])
    tips = {name: sha for sha, name in (line.split('\t') for line in listed.splitlines())}
    target_ref = next((r for r in refs if r in tips), None)
    if target_ref is None and not missing and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', ref):
        target = read_command([*git, 'rev-parse', '--verify', ref + '^{commit}'])
    elif target_ref is None:
        raise ValueError(f'origin did not advertise {ref} as a branch or tag')
    else:
        target = tips.get(target_ref + '^{}', tips[target_ref])
    if not missing and target_ref and target_ref.startswith('refs/heads/'):
        if target_ref.removeprefix('refs/heads/') != row['current_ref']:
            raise ValueError(f'checkout would switch to {ref}; its fast-forward is unverified')
        if current != target:
            base = branch_merge_base(path, current, target_ref, target)
            if base == target:
                return None  # Local branch is already ahead.
            if base != current:
                raise ValueError(f'could not verify a fast-forward from {current[:8]} to {target[:8]}')
    if target == current:
        return None
    return {'name': 'Mise setup' if path == CONFIG.parent else path.name,
            'kind': 'revision', 'before': current, 'after': target, 'id': row['path_raw']}


def repository_preview(rows):
    plans, issues = [], []
    for row in rows:
        state = row['state']
        if state == 'dirty' or (state == 'current' and row['ref']):
            continue
        try:
            if state not in ('missing', 'current', 'differs'):
                raise ValueError(row.get('reason') or f'unrecognized repository status {state!r}')
            plan = repository_plan(row)
            if plan:
                plans.append(plan)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            issues.append(f"{row['path_raw']}: {error}")
    return {'plans': plans, 'plan_issues': issues}


def preview_result(name, text, before):
    if name == 'mise':
        version = json_document(text)
        current, latest = version['version'].split()[0], version['latest']
        if latest is None:
            return {'plan_issues': ['Mise could not look up its latest eligible release.']}
        if release_version(latest) > release_version(current):
            return {'plans': [{'name': 'Mise', 'kind': 'version', 'before': current, 'after': latest}]}
        return {'plans': []}
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
        return repository_preview(json_document(text)['repos'])
    if name == 'brew' or name.startswith('brew-cask:'):
        notes = [note.strip() for note in re.findall(r'(?m)^.*(?:skipped:|WARN\s+)(.+)$', text)]
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
    last_scan = STATE / 'last-completed-scan.json'
    if complete_dependency_scan(report):
        atomic_write(last_scan, text)
    elif not last_scan.exists():
        # Seed from the pre-upgrade state before an interrupted attempt rotates it away.
        previous_scan, _ = dependency_report_source()
        if complete_dependency_scan(previous_scan):
            atomic_write(last_scan, json.dumps(previous_scan, indent=2) + '\n')
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


NATIVE_AVAILABILITY_NOTE = 'Native check could not prove an eligible update; see the detailed log.'


def replacement_app(note):
    match = re.match(r'brew-cask: replacing (/.+?\.app) — macOS may revoke Privacy & Security permissions for this app\b', note)
    return match[1] if match else None


def retry_notice(note):
    return re.fullmatch(r'HTTP GET https://\S+ attempt \d+ failed after \S+ \(transient\): .+; retrying in \S+', note)


def cask_result(text, running, *, applied_cask=None):
    """Use native platform/version checks; do not guess cask metadata versions."""
    if running and re.search(r'install cask [^/\s]+/[^\s]+', text):
        return {'state': 'deferred-running-app', 'availability': 'confirmed'}
    if 'running and updates itself' in text:
        return {'state': 'self-updating'}
    if applied_cask and 'skipped:' not in text:
        upgrade = re.search(rf'(?m)^(?:mise )?brew-cask: upgraded {re.escape(applied_cask)} (\S+) -> (\S+)[ \t]*$', text)
        warnings = re.finditer(r'(?m)\bWARN[ \t]+([^\r\n]+)', text)
        if upgrade and all(replacement_app(w[1]) or (w.start() < upgrade.start() and retry_notice(w[1])) for w in warnings):
            return {'state': 'updated', 'native_upgrade': {'before': upgrade[1], 'after': upgrade[2]}}
    if 'skipped:' in text or 'WARN' in text:
        unknown = {'state': 'availability-unknown', 'notes': [NATIVE_AVAILABILITY_NOTE]}
        if applied_cask and 'WARN' not in text:
            skips = [line.strip() for line in text.splitlines() if 'skipped:' in line]
            pattern = (rf'(?:mise )?brew-cask:{re.escape(applied_cask)}(?::| ✓) skipped: '
                       r'installed app is current, newer, or incomparable')
            if skips and all(re.fullmatch(pattern, line) for line in skips):
                unknown['native_version_skip'] = True
        return unknown
    if 'already up to date' in text or 'already installed' in text:
        return {'state': 'current'}
    if running:
        return {'state': 'availability-unknown', 'notes': [NATIVE_AVAILABILITY_NOTE]}
    return {'state': 'success'}


VENDOR_APPS = {
    **{'brew-cask:' + name: app for name, app in desktop_releases.APPS.items()},
    'brew-cask:antigravity': ('Antigravity.app', 'com.google.antigravity'),
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


def compare_numeric_versions(installed, available, *, pad=False):
    old, new = numeric_version(installed), numeric_version(available)
    if pad:
        width = max(len(old), len(new))
        old, new = old + (0,) * (width - len(old)), new + (0,) * (width - len(new))
    elif len(old) != len(new):
        raise ValueError(f'Incompatible version formats: {installed!r} and {available!r}')
    return (old > new) - (old < new)


def vendor_release(key):
    name = key.removeprefix('brew-cask:')
    if name in desktop_releases.APPS:
        return desktop_releases.latest(name, STATE, atomic_write)
    arm = platform.machine() in ('arm64', 'aarch64')
    if key == 'brew-cask:antigravity':
        arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64'}.get(platform.machine())
        if not arch:
            raise ValueError('Unsupported architecture for the Antigravity desktop feed')
        source = f'https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/manifest/latest-{arch}-mac.yml'
        with urlopen(Request(source, headers={'User-Agent': 'mise-maintenance'}), timeout=30) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError('Unexpectedly large Antigravity desktop manifest')
        feed = payload.decode().replace('\r\n', '\n') + '\n'
        # Read only the two fields needed from Google's Electron updater manifest.
        versions = re.findall(r'(?m)^version:[ \t]+([^\n]+)$', feed)
        files = re.findall(r'(?m)^files:[ \t]*\n((?:[ \t].*\n)+)', feed)
        if len(versions) != 1 or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', versions[0]) or len(files) != 1:
            raise ValueError('Unrecognized Antigravity desktop release manifest')
        if re.search(r'(?m)^minimumSystemVersion:|^stagingPercentage:(?![ \t]+100[ \t]*$)', feed):
            raise ValueError('Antigravity desktop release eligibility is restricted by the vendor')
        version = versions[0]
        artifact_arch = 'arm' if arch == 'arm64' else 'x64'
        artifact = (r'https://storage\.googleapis\.com/antigravity-public/antigravity-hub/'
                    + re.escape(version) + rf'-[0-9]+/darwin-{artifact_arch}/Antigravity\.zip')
        urls = re.findall(r'(?m)^[ \t]+- url:[ \t]+(\S+)[ \t]*$', files[0])
        matched = [url for url in urls if re.fullmatch(artifact, url)]
        if len(matched) != 1:
            raise ValueError('Antigravity desktop manifest lacks a matching Google macOS update artifact')
        return desktop_releases.release(source, version, **desktop_releases.electron_asset(files[0], matched[0], matched[0]))
    if key == 'brew-cask:cursor':
        arch = 'arm64' if arm else 'x64'
        source = f'https://api2.cursor.sh/updates/api/update/darwin-{arch}/cursor/0.0.0/stable'
        data = json_url(source)
        version = data['name']
        numeric_version(version)
        if not re.fullmatch(r'https://downloads\.cursor\.com/production/[0-9a-f]{40}/darwin/' + arch + r'/Cursor-darwin-' + arch + r'\.zip', data['url']):
            raise ValueError('Unexpected Cursor stable artifact')
        artifact = desktop_releases.checksum_asset(data['url'], data.get('sha256hash')) or desktop_releases.signed_image(data['url'][:-4] + '.dmg')
        return desktop_releases.release(source, version, **artifact)
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
        return {'version': max(versions, key=numeric_version), 'source': source, 'channel': 'stable (fully rolled out)',
                **desktop_releases.signed_image('https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg')}
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
            maximum = item.findtext(SPARKLE + 'maximumSystemVersion')
            if maximum and compare_numeric_versions('.'.join(map(str, os_version)), maximum, pad=True) > 0:
                continue
            releases.append(desktop_releases.release(source, version, build=build, **desktop_releases.sparkle_asset(item, enclosure)))
        if not releases:
            raise ValueError('No compatible stable ChatGPT release in official feed')
        return max(releases, key=lambda r: numeric_version(r['build']))
    return None


def assess_vendor_release(key, release, app=None):
    filename, identifier = VENDOR_APPS[key]
    if app is None:
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
    if key in ('brew-cask:cursor', 'brew-cask:devin-desktop', 'brew-cask:visual-studio-code'):
        product = json.loads((app / 'Contents/Resources/app/product.json').read_text())
        if product.get('quality') != 'stable':
            raise ValueError(f'Installed {label(key)} is not on the stable channel')
    installed, available = info['CFBundleShortVersionString'], release['version']
    if key == 'brew-cask:opera':
        installed = info['CFBundleVersion']
    pad = key.removeprefix('brew-cask:') in desktop_releases.SPARKLE_FEEDS
    comparison = compare_numeric_versions(installed, available, pad=pad)
    if release.get('build'):
        build_comparison = compare_numeric_versions(info['CFBundleVersion'], release['build'], pad=pad)
        if comparison and comparison != build_comparison:
            raise ValueError(f'{label(key)} marketing and build versions disagree on ordering')
        comparison = build_comparison
        installed += f" (build {info['CFBundleVersion']})"
        available += f" (build {release['build']})"
    state = 'current' if comparison == 0 else 'newer' if comparison > 0 else 'update-available'
    return {'state': state, 'installed': installed, 'available': available,
            'release': release, 'checked_at': now()}


def orbstack_assessment(app):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info['CFBundleIdentifier'] != 'dev.kdrag0n.MacVirt':
        raise ValueError('Unexpected OrbStack bundle identity')
    version, build = info['CFBundleShortVersionString'], info['CFBundleVersion']
    numeric_version(version)
    numeric_version(build)
    cli = app / 'Contents/MacOS/bin/orbctl'
    result = subprocess.run([str(cli), 'update', '--check'], cwd=HOME, env=ENV, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=45)
    if result.returncode not in (0, 3):
        raise RuntimeError(f'orbctl check exited {result.returncode}. {plain(result.stderr or result.stdout)}')
    return {'state': 'current' if result.returncode == 0 else 'update-available',
            'installed': f'{version} (build {build})', 'source': str(cli), 'command': ['update', '--check'],
            'exit_code': result.returncode, 'message': plain(result.stdout), 'checked_at': now()}


def native_allows_vendor_update(result):
    return result['state'] in ('current', 'updated') or result.get('native_version_skip') is True


def orbstack_update(key, check, log, command):
    app = APPLICATIONS / 'OrbStack.app'

    def assess():
        try:
            result = orbstack_assessment(app)
        except (OSError, ValueError, KeyError, plistlib.InvalidFileException, RuntimeError, subprocess.TimeoutExpired) as error:
            result = {'state': 'unverified', 'error': f'OrbStack update check unavailable. {error}', 'checked_at': now()}
        log.write(f'[{now()}] OrbStack update check: {json.dumps(result)}\n')
        return result

    if app.exists():
        vendor = assess()
        if vendor['state'] == 'update-available' and not check and not is_running(app):
            native = native_cask_update(key, False, log, command)
            vendor = assess()
            receipts = PREFIX / 'Caskroom/orbstack'
            if (vendor['state'] == 'update-available' and native_allows_vendor_update(native)
                    and (owner := vendor_install.ownership(receipts, app)) and not is_running(app)):
                cli = app / 'Contents/MacOS/bin/orbctl'
                engine = subprocess.run([str(cli), 'status'], cwd=HOME, env=ENV, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=30)
                if (engine.returncode, engine.stdout.strip().lower()) in ((0, 'running'), (2, 'starting')):
                    return {'state': 'deferred-running-app', 'vendor_check': vendor, 'availability': 'confirmed',
                            'notes': ['OrbStack will update when its engine is stopped.']}
                if (engine.returncode, engine.stdout.strip().lower()) != (1, 'stopped'):
                    raise RuntimeError('Could not confirm that the OrbStack engine is stopped')
                if not is_running(app):
                    before = vendor['installed']
                    command([str(cli), 'update'], log, timeout=600)
                    if vendor_install.ownership(receipts, app) != owner or not all(os.access(b, os.X_OK) for b in owner[2]):
                        raise RuntimeError('OrbStack updater did not preserve its Mise receipt and launchers')
                    vendor = assess()
                    if vendor['state'] == 'current' and vendor['installed'] != before:
                        return {'state': 'updated', 'vendor_check': vendor,
                                'vendor_upgrade': {'before': before, 'after': vendor['installed'], 'updater': 'orbctl'}}
    else:
        native = native_cask_update(key, check, log, command)
        if check or not app.exists():
            return native
        vendor = assess()
    if vendor['state'] == 'current':
        return {'state': 'current', 'vendor_check': vendor}
    if vendor['state'] == 'unverified':
        return {'state': 'availability-unknown', 'vendor_check': vendor}
    # orbctl documents availability via exit status and may omit the target version.
    state = 'deferred-running-app' if not check and is_running(app) else 'update-available'
    result = {'state': state, 'vendor_check': vendor, 'availability': 'confirmed', 'plans': [
        {'name': 'OrbStack', 'kind': 'availability', 'before': vendor['installed'], 'after': None}]}
    if not check and state == 'update-available':
        result['notes'] = ['OrbStack still offers an update. Use its app updater or retry Mise later.']
    return result


def cask_update(key, check, log, command):
    if key == 'brew-cask:orbstack':
        return orbstack_update(key, check, log, command)
    release, vendor_error, verified = None, None, None
    if key in VENDOR_APPS:
        try:
            release = vendor_release(key)
            app = APPLICATIONS / VENDOR_APPS[key][0]
            if app.exists():
                assessment = assess_vendor_release(key, release)
                log.write(f'[{now()}] {key} official vendor check: {json.dumps(assessment)}\n')
                if assessment['state'] in ('current', 'newer'):
                    verified = {'state': assessment['state'], 'vendor_check': assessment}
        except Exception as error:
            vendor_error = str(error)
            log.write(f'[{now()}] {key} vendor check unavailable: {error}\n')
            if release is not None:
                # Preserve unexpected channels, identities, and version formats.
                return {'state': 'availability-unknown', 'vendor_check':
                        {'state': 'unverified', 'error': vendor_error, 'checked_at': now()}}
    if verified:
        # A restored Mac may already have the current app but no ownership receipt.
        receipts = PREFIX / 'Caskroom' / key.removeprefix('brew-cask:')
        if not any(receipts.glob('*/.mise-cask.toml')):
            declaration = tomllib.loads(CONFIG.read_text()).get('bootstrap', {}).get('packages', {}).get(key)
            if isinstance(declaration, dict) and declaration.get('adopt') is True:
                if check:
                    verified['notes'] = ['The next update will register this installed app with Mise.']
                else:
                    command([MISE, 'bootstrap', 'packages', 'apply', key, '-y'], log, timeout=1800)
        return verified
    result = native_cask_update(key, check, log, command)
    if release is not None and vendor_error is None:
        # Re-read the live bundle after native installation; reuse this run's
        # release evidence rather than racing a second network metadata fetch.
        native_finished = native_allows_vendor_update(result)
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
                if release.get('artifact') and native_finished and assessment['installed']:
                    outcome = vendor_install.install(app, PREFIX / 'Caskroom' / key.removeprefix('brew-cask:'),
                        release['artifact'], partial(assess_vendor_release, key, release), is_running,
                        lambda args, **kw: command(args, log, **kw))
                    result.update(outcome)
                    if result['state'] in ('current', 'newer', 'updated'):
                        result.pop('plans', None)
                    if 'vendor_upgrade' in result:
                        log.write(f'[{now()}] {key} verified vendor upgrade: {json.dumps(result["vendor_upgrade"])}\n')
                    return result
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
    if key in VENDOR_APPS:
        # An existing app can be running even before bootstrap writes its first receipt.
        bundle = APPLICATIONS / VENDOR_APPS[key][0]
        if bundle.exists() and bundle not in apps:
            apps.append(bundle)
    running = any(is_running(app) for app in apps)
    start = log.tell()
    if check or running:
        if check:
            command([MISE, 'bootstrap', 'packages', 'apply', key, '-y', '--dry-run'], log, timeout=120)
        command([MISE, 'bootstrap', 'packages', 'upgrade', key, '-y', '--dry-run'], log, timeout=120)
        log.flush(); log.seek(start); text = log.read(); log.seek(0, 2)
        result = cask_result(text, running)
        if check and result['state'] in ('current', 'success'):
            result['state'] = 'checked'
        return result
    command([MISE, 'bootstrap', 'packages', 'apply', key, '-y'], log,
            timeout=1800, progress=CaskDownloadProgress(key))
    start = log.tell()
    command([MISE, 'bootstrap', 'packages', 'upgrade', key, '-y'], log,
            timeout=1800, progress=CaskDownloadProgress(key))
    log.flush(); log.seek(start); text = log.read(); log.seek(0, 2)
    return cask_result(text, False, applied_cask=key.removeprefix('brew-cask:'))


def restore_cask_outcomes(report):
    """Interpret older saved runs with a verified bundle change and only replacement notices."""
    if report.get('schema_version', 0) >= 5 or report.get('check_only'):
        return
    for key, result in report.get('results', {}).items():
        if not key.startswith('brew-cask:') or result['state'] != 'availability-unknown' or result.get('vendor_check'):
            continue
        messages = result.get('notes', [])
        if (NATIVE_AVAILABILITY_NOTE not in messages or result.get('observation_errors')
                or result.get('plans') or result.get('plan_unverified')):
            continue
        changed = {c['id'] for c in result.get('changes', []) if c.get('id') and c.get('kind') == 'version'
                   and c.get('action') == 'updated' and c.get('before') and c.get('after') and c['before'] != c['after']}
        notices = [note for note in messages if note != NATIVE_AVAILABILITY_NOTE]
        if notices and all(replacement_app(note) in changed for note in notices):
            result.update(state='updated', notes=notices)


def doctor_health():
    result = subprocess.run([MISE, 'doctor', '--json'], cwd=HOME, env=ENV,
                            capture_output=True, text=True, timeout=30)
    # Doctor returns 1 for diagnosed errors and still writes a valid JSON report.
    # Keep only the diagnostics; the full output includes the process environment.
    if not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or f'Mise doctor returned no report (exit {result.returncode})')
    data = json.loads(result.stdout)
    installed = data['version'].split()[0]
    errors, warnings, pending = data.get('errors', []), [], None
    for warning in data.get('warnings', []):
        match = re.fullmatch(r'new mise version (\d+\.\d+\.\d+) available, currently on (\d+\.\d+\.\d+)', warning)
        if (match and data.get('self_update_available') is True and match[2] == installed
                and release_version(match[1]) > release_version(installed)):
            pending = {'installed': installed, 'eligible': match[1]}
        else:
            warnings.append(warning)
    details = [*errors, *warnings]
    if result.returncode and not errors:
        details.append(f'Mise doctor exited with status {result.returncode}')
    if pending:
        details.append(f"Mise {installed} → {pending['eligible']} on the next maintenance run")
    state = 'failed' if result.returncode or errors else 'warning' if warnings else 'healthy'
    return {'state': state, 'detail': ' · '.join(details) or 'No Mise doctor errors or warnings',
            'errors': errors, 'warnings': warnings, 'pending_update': pending}


def health():
    checks = {}
    def check(name, action):
        try:
            result = action()
            if result is not None:
                checks[name] = result
        except Exception as error:
            checks[name] = {'state': 'failed', 'detail': str(error)}
    check('mise-doctor', doctor_health)
    def last_attempt():
        skip = load_report(STATE / 'last-scheduled-skip.json')
        if skip:
            last = load_report(STATE / 'last-scheduled.json')
            when = datetime.fromisoformat(skip['finished_at'])
            if not last or when > datetime.fromisoformat(last['finished_at']):
                stamp = when.astimezone().strftime('%Y-%m-%d %H:%M %Z')
                return {'state': 'warning', 'detail': f"Skipped at {stamp} after waiting {skip['waited_seconds']:.0f}s for another run to finish.",
                        'last_skip': skip}
    check('scheduled-attempt', last_attempt)
    def service(name):
        output = read_command(['/bin/launchctl', 'print', f'gui/{os.getuid()}/dev.mise.{name}'])
        code = re.search(r'last exit code = (\d+)', output)
        if name == 'mise-maintenance':
            if '--scheduled' not in output:
                raise RuntimeError('Loaded maintenance agent lacks scheduled-run tracking; run setup:sync')
            if code and int(code[1]) == os.EX_TEMPFAIL and checks.get('scheduled-attempt', {}).get('last_skip'):
                return {'state': 'healthy', 'detail': 'loaded'}
        if code and int(code[1]):
            raise RuntimeError(f'Last service exit code: {code[1]}')
        if name == 'mise-tool-update' and 'state = running' not in output:
            raise RuntimeError('Tool update watcher is loaded but not running')
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
    latest = load_report(STATE / 'last-run.json') or {}
    audit = latest.get('results', {}).get('audit')
    if audit:
        report['latest_update_audit'] = audit
    deps = latest.get('results', {}).get('dependency-audit', {})
    if deps:
        report['latest_dependency_audit'] = {k: deps[k] for k in ('state', 'match_count', 'error') if k in deps}
    if console.json_output:
        console.say(json.dumps(report, indent=2), force=True)
    else:
        console.say('Automatic update health: ' + report['state'])
        for name, result in report['checks'].items():
            console.say(f"  {'✓' if result['state'] == 'healthy' else '✗'} {name}: {result['detail']}")
        if audit:
            detail = (plural(len(audit['vulnerabilities']), 'advisory', 'advisories')
                      if audit.get('state') in ('clear', 'advisories') else 'audit incomplete')
            console.say(f'Last update security audit  {detail}' + (' · mise run maintenance:status' if audit['state'] != 'clear' else ''))
        if deps:
            detail = 'scan incomplete' if deps['state'] in ('failed', 'interrupted') else plural(deps['match_count'], 'match', 'matches')
            console.say(f'Last dependency audit  {detail} · mise run maintenance:dependencies')
    return 0 if report['state'] == 'healthy' else 1


def dependency_coverage(owners):
    reasons = {'No identifiable dependency inventory': 'No inventory',
               'Linked outside the Mise install store': 'Outside the Mise install folder'}
    groups = {}
    for owner in owners:
        note = owner.get('coverage_note')
        reason = reasons.get(note, note)
        if not reason and owner.get('coverage') == 'partial':
            reason = 'Partial inventory'
        if reason:
            groups.setdefault(reason, []).append(label(owner['tool']))
    return groups


def compare_dependency_matches(previous, current):
    def snapshot(run):
        audit = run.get('results', {}).get('dependency-audit', {})
        if audit.get('state') not in ('clear', 'limited', 'advisories'):
            return None
        scope, matches = [], set()
        for owner in audit.get('owners', {}).values():
            scope.append((owner['tool'], owner['coverage'], owner.get('coverage_note'), tuple(owner['evidence'])))
            for f in owner['findings']:
                matches.add((owner['tool'], f['ecosystem'], f['package'], f['version'], f['id']))
        # With multiple active versions the match count includes each installation.
        if len({s[0] for s in scope}) != len(scope):
            return None
        return sorted(scope), matches
    old, new = snapshot(previous), snapshot(current)
    if old is None or new is None or old[0] != new[0]:
        return None
    added = new[1] - old[1]
    for owner in current['results']['dependency-audit']['owners'].values():
        for f in owner['findings']:
            if 'review_token' in f:
                f['new_since_previous'] = (owner['tool'], f['ecosystem'], f['package'], f['version'], f['id']) in added
    return {'added': len(added), 'no_longer_reported': len(old[1] - new[1])}


def dependency_trend(saved):
    changes = saved.get('dependency_match_changes')
    if changes is None:
        return []
    added, gone = changes['added'], changes['no_longer_reported']
    if added or gone:
        return [f'Since the previous update: {added} newly reported · {gone} no longer reported.']
    return ['Same matches as the previous update.']


def advisory_score(finding):
    try:
        value = float(finding.get('severity'))
    except (ValueError, TypeError):
        return -1
    return value if 0 <= value <= 10 else -1


def severity_display(score):
    if score < 0:
        return 'Unscored', '2'
    for minimum, name, color in ((9, 'Critical', '1;31'), (7, 'High', '1;33'), (4, 'Medium', '36'),
                                 (0.1, 'Low', '0'), (0, 'None', '0')):
        if score >= minimum:
            return f'{name} {score:.1f}', color


def affected_tools(owners):
    rows = []
    for owner in owners:
        findings = owner['findings']
        if findings:
            rows.append((owner, max(map(advisory_score, findings)), len(findings),
                         sum(bool(f.get('fixed_versions')) for f in findings)))
    return sorted(rows, key=lambda row: (-row[1], -row[2], label(row[0]['tool']), row[0]['version']))


def component_routes(finding):
    # Older saved reports retain the same evidence paths even without the components field.
    return {tuple(src['components']) if 'components' in src else
            dependency_audit.shipping_components(src, finding['package']) for src in finding['sources']}


def dependency_tool_label(owner, *, with_version=False):
    routes = {route for f in owner['findings'] for route in (component_routes(f) or {()})}
    name = label(owner['tool'])
    if with_version:
        name += ' ' + owner['version']
    if len(routes) == 1:
        return ' → '.join((name, *next(iter(routes))))
    return name


def dependency_table(console, rows):
    names = [plain(dependency_tool_label(owner)) for owner, *_ in rows]
    name_width = max(len('Dependency source'), *map(len, names))
    version_width = max(len('Tool version'), *(len(plain(row[0]['version'])) for row in rows))
    score_width = max(len('Top score'), *(len(severity_display(score)[0]) for _, score, _, _ in rows))
    header = f"  {'Dependency source':<{name_width}}  {'Tool version':<{version_width}}  {'Top score':<{score_width}}  {'Matches':>7}  {'With a fix':>10}"
    wide = len(header) <= console.columns
    if wide:
        console.say(console.paint(header, '1'))
    for name, (owner, score, count, fixes) in zip(names, rows):
        version = plain(owner['version'])
        severity, color = severity_display(score)
        if wide:
            colored = console.paint(severity.ljust(score_width), color)
            console.say(f'  {name:<{name_width}}  {version:<{version_width}}  {colored}  {count:>7}  {fixes:>10}')
        else:
            console.line(f'{dependency_tool_label(owner, with_version=True)} · {severity}', indent=2, color='1')
            console.line(f"{plural(count, 'match', 'matches')} · {fixes} with fixes", indent=4)


def reopened_reviews(owners):
    groups = {}
    for owner in owners:
        for finding in owner['findings']:
            review = finding.get('review', {})
            if review.get('assessment') == 'changed':
                prior = review.get('previous', {}).get('tool_version')
                groups.setdefault((label(owner['tool']), owner['version'], prior), []).append(finding)
    rows = []
    for (name, version, prior), findings in sorted(groups.items(), key=lambda item: (-len(item[1]), str(item[0]))):
        versions = f'{prior} → {version}' if prior and prior != version else version
        detail = [f'Reopened · {name} {versions}', plural(len(findings), 'match', 'matches') + ' to recheck']
        package_fields = {'dependency_version', 'advisory', 'sources'}
        comparable = [f['review']['changed_inputs'] for f in findings if 'changed_inputs' in f['review']
                      and not package_fields.intersection(f['review']['uncompared_inputs'])]
        same = sum(not package_fields.intersection(fields) for fields in comparable)
        if same:
            detail.append(f'{same} with unchanged package version, advisory and sources')
        for field in ('dependency_version', 'advisory', 'sources', 'coverage', 'platform', 'build'):
            if field == 'build' and prior != version:
                continue
            n = sum(field in f['review'].get('changed_inputs', []) for f in findings)
            if n:
                detail.append(f"{audit_reviews.EVIDENCE_LABELS[field]} changed for {n}")
        if len(comparable) < len(findings):
            detail.append('previous evidence unavailable for ' + str(len(findings) - len(comparable)))
        if len(findings) == 1:
            detail.append(findings[0]['package'])
        rows.append(' · '.join(detail))
    return rows


def review_overview(report):
    counts = report['review_counts']
    owners = report['owners'].values()
    lines = []
    if report.get('review_error'):
        lines.append('Local review history: ' + report['review_error'])
    verified = [f"{label(o['tool'])} {o['update']['version']}" for o in owners
                if any(f.get('owner_fix') == 'verified' for f in o['findings'])]
    if verified:
        lines.append('Verified update available: ' + ', '.join(verified) + '. Run mise run maintenance:update.')
    def priority(f):
        assessment = f['review']['assessment']
        return 'new' if assessment == 'unreviewed' and f.get('new_since_previous') else assessment

    lines.extend(reopened_reviews(owners))
    for status, heading in [('new', 'New since the previous update'),
                            ('unreviewed', 'Awaiting review'), ('relevant', 'Relevant to this Mac')]:
        groups = []
        for owner in owners:
            findings = [f for f in owner['findings'] if priority(f) == status]
            if findings:
                names = sorted({f['package'].split('/')[-1] for f in findings})
                if status == 'relevant' or len(findings) <= 3:
                    detail = ', '.join(names)
                else:
                    detail = f'{len(findings)} matches'
                groups.append((len(findings), label(owner['tool']), detail))
        if not groups:
            continue
        groups.sort(key=lambda row: (-row[0], row[1]))
        total = sum(count for count, _, _ in groups)
        lines.append(f"{heading} ({total}): " + ' · '.join(f'{name} ({detail})' for _, name, detail in groups) + '.')
    other = [(counts.get(k, 0), wording) for k, wording in
             [('unconfirmed', 'unconfirmed'), ('outside', 'outside scope'), ('notice', 'maintenance notices')]]
    if any(c for c, _ in other):
        lines.append('Previously reviewed: ' + ' · '.join(f'{c} {word}' for c, word in other if c) + '.')
    eligible = [o for o in owners if o.get('update', {}).get('state') == 'eligible' and o['findings']]
    for o in eligible:
        findings = o['findings']
        still = sum(f.get('owner_fix') == 'still-matches' for f in findings)
        unknown = sum('owner_fix' not in f for f in findings)
        states = [f'{n} {name}' for n, name in ((still, 'still match'), (unknown, 'unverified')) if n]
        if states:
            lines.append(f"{label(o['tool'])} {o['update']['version']} is eligible: " + ', '.join(states) + '.')
    if any(o.get('update', {}).get('state') == 'unknown' for o in owners):
        lines.append('Mise could not confirm update eligibility for some tools.')
    elif not eligible and any(o.get('update', {}).get('state') == 'current' for o in owners):
        lines.append('No eligible owning-tool update is available for these matches.')
    return lines


def owner_fix_text(owner, finding):
    update = owner.get('update', {})
    state = update.get('state')
    if state == 'current':
        return 'Waiting for an eligible release of ' + label(owner['tool']) + '.'
    if state != 'eligible':
        return 'Owning-tool update eligibility unverified.'
    candidate = f"{label(owner['tool'])} {update['version']}"
    if finding.get('owner_fix') == 'verified':
        return f"Verified fix in {candidate} (ships {', '.join(finding['replacement_versions'])}). Run mise run maintenance:update."
    if finding.get('owner_fix') == 'still-matches':
        return f'{candidate} is eligible and still matches this advisory.'
    return f'{candidate} is eligible. A fix in that release is unverified.'


def dependency_detail(console, owner):
    findings = owner['findings']
    console.say()
    console.line(f"{label(owner['tool'])} {owner['version']}", color='1')
    if not owner['package_count']:
        console.line(owner.get('coverage_note') or 'No package versions identified.')
        return
    info = f"{plural(owner['package_count'], 'package version')} checked"
    if owner['coverage'] == 'partial':
        info += ' · partial inventory'
    console.line(info + ' · ' + plural(len(findings), 'advisory match', 'advisory matches'))
    for f in sorted(findings, key=lambda f: (-advisory_score(f), f['package'], f['version'], f['id'])):
        severity, _ = severity_display(advisory_score(f))
        console.say()
        routes = component_routes(f)
        package = f"{f['package']} {f['version']}"
        if len(routes) == 1 and () not in routes:
            package = ' → '.join((label(owner['tool']), *next(iter(routes)), package))
        console.line(f"{package}  ·  {severity}", indent=2, color='1')
        if len(routes) > 1:
            for route in sorted(routes):
                console.line(' → '.join((label(owner['tool']), *route, f['package'])), indent=4)
        if f.get('summary'):
            console.line(f['summary'].replace('`', ''), indent=4)
        fixes = ', '.join(f.get('fixed_versions', []))
        if 'line_fixes' in f:
            line = ', '.join(f['line_fixes'])
            fix = ('Dependency fix on this release line: ' + line if line else
                   'No fix verified on this dependency release line' if fixes else 'No dependency fix listed')
        else:
            fix = f'Fix listed: {fixes} (release line unverified)' if fixes else 'No dependency fix listed'
        console.line(f"{f['id']}  ·  {fix}", indent=4)
        if 'review' in f:
            review = f['review']
            assessment = review['assessment']
            title = audit_reviews.ASSESSMENTS.get(assessment, 'Reopened' if assessment == 'changed' else 'Awaiting review')
            console.line(title + (' · ' + review['reason'] if review.get('reason') else ''), indent=4)
            if assessment == 'changed' and 'previous' in review:
                prior = review['previous']
                console.line(f"Prior assessment · {audit_reviews.ASSESSMENTS[prior['assessment']]} · {prior['reason']}", indent=4)
                reasons = []
                for field in review.get('changed_inputs', []):
                    if field == 'tool_version':
                        reasons.append(f"tool {prior['tool_version']} → {owner['version']}")
                    elif field == 'dependency_version':
                        reasons.append(f"dependency {prior['dependency_version']} → {f['version']}")
                    else:
                        reasons.append(audit_reviews.EVIDENCE_LABELS[field])
                console.line('Changed · ' + ', '.join(reasons) if reasons else 'Exact reason unavailable from the saved evidence.', indent=4)
            console.line(owner_fix_text(owner, f), indent=4)
            if console.verbose:
                console.line('Review ID: ' + f['review_token'][:16], indent=4)
        if console.verbose:
            for src in f.get('sources', []):
                console.line(f"{src['kind']} · {src['path']}", indent=4, color='2')
            console.line(f.get('url', ''), indent=4)
        elif {s['kind'] for s in f.get('sources', [])} == {'published lockfile'}:
            console.line('Evidence from the lockfile only', indent=4, color='2')


def latest_dependency_snapshot():
    reports = (load_report(STATE / name) or {} for name in ('last-run.json', 'last-check.json'))
    return max(reports, key=lambda r: r.get('finished_at', ''))


def complete_dependency_scan(run):
    return run.get('results', {}).get('dependency-audit', {}).get('state') in ('advisories', 'limited', 'clear')


def dependency_report_source():
    latest = latest_dependency_snapshot()
    if complete_dependency_scan(latest):
        return latest, None
    saved = load_report(STATE / 'last-completed-scan.json')
    if not latest:
        return saved or {}, None
    scan = latest.get('results', {}).get('dependency-audit', {})
    attempt = {'finished_at': latest.get('finished_at'), 'origin': latest.get('origin'),
               'run_state': latest.get('state'), 'scan_state': scan.get('state', 'not-started')}
    if scan:
        attempt['partial_scan'] = scan
    if saved and complete_dependency_scan(saved) and saved['finished_at'] > latest['finished_at']:
        return saved, None  # A completed report was published while reading the latest pointers.
    if saved and complete_dependency_scan(saved) and saved['finished_at'] <= latest['finished_at']:
        return saved, attempt
    # Existing installs have no dedicated snapshot until their next completed scan.
    candidates = [load_report(STATE / name) or {} for name in
                  ('last-manual.json', 'last-scheduled.json', 'last-run.json', 'last-check.json')]
    for path in sorted((STATE / 'history').glob('*.json'), reverse=True):
        archived = load_report(path) or {}
        if complete_dependency_scan(archived) and archived['finished_at'] <= latest['finished_at']:
            candidates.append(archived)
            break
    eligible = [r for r in candidates if complete_dependency_scan(r) and r['finished_at'] <= latest['finished_at']]
    return max(eligible, key=lambda r: r['finished_at'], default=latest), attempt


def record_review(token, assessment, reason):
    # Serialize read-modify-write with other reviewers and scans.
    with (STATE / 'update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = latest_dependency_snapshot().get('results', {}).get('dependency-audit', {})
        if report.get('state') not in ('advisories', 'clear', 'limited'):
            raise ValueError('A completed scan is required to record a review.')
        matches = [(o, f) for o in report['owners'].values() for f in o['findings']
                   if re.fullmatch(r'[0-9a-f]{12,64}', token) and f.get('review_token', '').startswith(token)]
        if len(matches) != 1:
            raise ValueError('This review ID does not uniquely match the latest scan. Check --verbose.')
        owner, f = matches[0]
        path = STATE / audit_reviews.REVIEW_FILE
        entries = audit_reviews.load(path)
        entries[f['review_token']] = audit_reviews.entry(f, assessment, reason, now(), owner=owner)
        atomic_write(path, json.dumps({'version': 1, 'entries': entries}, indent=2) + '\n')
    return f['id']


def show_dependencies(console, tool=None):
    saved, attempt = dependency_report_source()
    report = saved.get('results', {}).get('dependency-audit') or {'state': 'not-run-yet'}
    if 'review_counts' in report:
        audit_reviews.apply_local(report, STATE / audit_reviews.REVIEW_FILE)
    owners = report.get('owners', {})
    selected = None
    if tool is not None and owners:
        query = tool.strip().casefold()
        names = {key: (key.casefold(), owner['tool'].casefold(), label(owner['tool']).casefold())
                 for key, owner in owners.items()}
        keys = [k for k, aliases in names.items() if query in aliases]
        if not keys and query:
            keys = [k for k, aliases in names.items() if any(s.startswith(query) for s in aliases)]
        if len(keys) != 1:
            reason = 'Ambiguous tool' if keys else 'Unknown tool'
            print(f"{reason} {plain(tool)!r}. Choose from: {', '.join(map(plain, keys or names))}.", file=console.stderr)
            return 2
        selected = owners[keys[0]]
    if console.json_output:
        output = selected if selected is not None else report
        if attempt:
            output = dict(output, latest_attempt=attempt, last_completed_at=report.get('checked_at') if complete_dependency_scan(saved) else None,
                          last_completed_run_at=saved.get('finished_at') if complete_dependency_scan(saved) else None)
        console.say(json.dumps(output, indent=2), force=True)
        return int(bool(attempt))
    title = 'Dependency audit'
    stamp = report.get('checked_at') or saved.get('finished_at')
    date = datetime.fromisoformat(stamp).astimezone().strftime('%b %d, %Y at %I:%M %p %Z').replace(' 0', ' ') if stamp else ''
    if date and len(title) + len(date) + 3 <= console.columns:
        console.say(console.paint(title, '1') + '   ' + console.paint(date, '2'))
    else:
        console.line(title, color='1')
        if date:
            console.line(date, color='2')
    if attempt:
        when = datetime.fromisoformat(attempt['finished_at']).astimezone().strftime('%b %d at %I:%M %p %Z')
        origin = {'manual': 'manual update', 'scheduled': 'scheduled update', 'preview': 'preview'}.get(attempt['origin'], 'run')
        reason = ('stopped before the dependency scan' if attempt['scan_state'] == 'not-started' else
                  'did not finish the dependency scan')
        console.line(f'The {origin} on {when} {reason}.', color='33')
        if complete_dependency_scan(saved):
            console.line(f'Showing the last completed scan from {date}.' if report.get('checked_at') else
                         f'Showing the last completed scan from the run that finished {date}.')
            partial = attempt.get('partial_scan', {})
            if partial.get('error'):
                console.line(plain(partial['error']), color='33')
            if partial.get('match_count'):
                console.line(f"The incomplete scan also recorded {plural(partial['match_count'], 'match', 'matches')}. See --json for its findings.")
    if report['state'] == 'not-run-yet':
        console.line('No recorded dependency scan. Run mise run maintenance:check.')
        return int(bool(attempt))
    incomplete = report['state'] in ('failed', 'interrupted')
    if incomplete:
        console.section('Scan incomplete', [report.get('error') or 'The scan stopped early.',
                        'Retry with mise run maintenance:check.'], '1;31')
    if selected is not None:
        dependency_detail(console, selected)
        if selected['findings']:
            console.say()
            console.line('Dependency fixes arrive with a release of ' + label(selected['tool']) + '.')
            if not console.verbose:
                console.line('Add --verbose for evidence paths and advisory links.', color='2')
        return int(bool(attempt))
    if not owners:
        if not incomplete:
            console.line('No active tools were recorded for this scan.')
        return int(bool(attempt))

    rows = affected_tools(owners.values())
    checked = sum(bool(o['package_count']) for o in owners.values())
    findings = [f for o in owners.values() for f in o['findings']]
    if rows:
        console.line(f"{plural(len(findings), 'advisory match', 'advisory matches')} across {plural(len(rows), 'tool')}", color='1')
    elif checked and not incomplete:
        console.line('No advisory matches in the packages checked.', color='1;32')
    elif not checked:
        console.line('No package versions available to check.', color='33')
    subtitle = f'Package inventory found for {checked} of {len(owners)} tools'
    unscored = sum(advisory_score(f) < 0 for f in findings)
    if unscored:
        subtitle += f" · {plural(unscored, 'match', 'matches')} unscored"
    console.line(subtitle)
    for line in dependency_trend(saved):
        console.line(line)
    if 'review_counts' in report:
        direct = (saved.get('results', {}).get('audit') or {}).get('vulnerabilities', [])
        if direct:
            console.section('Installed tool advisories', [f"{label(f['tool'])} {f['version']} · {f['id']}"
                            for f in direct], '33')
        if rows:
            console.section('What needs attention', review_overview(report), '1')
            console.line('Use --tool NAME for findings or --verbose for all findings.', color='2')
    elif rows:
        console.say()
        dependency_table(console, rows)
        console.say()
        name = rows[0][0]['tool']
        if sum(o['tool'] == name for o in owners.values()) > 1:
            name += '@' + rows[0][0]['version']
        console.line(f"Inspect {label(rows[0][0]['tool'])}", color='1')
        console.line('mise run maintenance:dependencies --tool ' + shlex.quote(name), indent=2, color='36')
        console.line('Dependency fixes arrive with eligible tool updates.')
    coverage = dependency_coverage(owners.values())
    no_matches = [label(o['tool']) for o in owners.values() if o['package_count'] and not o['findings']]
    if no_matches and not incomplete:
        coverage = {'No matches in checked packages': no_matches, **coverage}
    console.section('Coverage', [f"{reason}: {', '.join(names)}" for reason, names in coverage.items()], '1')
    if console.verbose:
        for owner, *_ in rows:
            dependency_detail(console, owner)
    return int(bool(attempt))


def show_history(console, limit):
    reports = [load_report(path) for path in sorted((STATE / 'history').glob('*.json'), reverse=True)[:limit]]
    if console.json_output:
        console.say(json.dumps(reports, indent=2), force=True)
    elif not reports:
        console.say('No maintenance history recorded yet.')
    else:
        for report in reports:
            local = datetime.fromisoformat(report['started_at']).astimezone().strftime('%Y-%m-%d %H:%M')
            audit = report.get('results', {}).get('audit', {})
            audit_status = (' · ' + plural(len(audit['vulnerabilities']), 'advisory', 'advisories')
                            if audit.get('state') in ('clear', 'advisories') else ' · audit incomplete' if audit else '')
            deps = report.get('results', {}).get('dependency-audit', {})
            if deps:
                audit_status += ' · ' + ('dependency scan incomplete' if deps['state'] in ('failed', 'interrupted') else
                                         plural(deps['match_count'], 'dependency match', 'dependency matches'))
            changes = plural(len(report.get('changes', [])), 'change')
            errors = plural(len(report.get('errors', [])), 'error')
            between = len(report.get('since_previous_run', []))
            console.say(f"{local}  {report['origin']:9} {report['state']:11} {changes} · {between} between runs · {errors}{audit_status}")
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Preview updates without applying them')
    parser.add_argument('--status', action='store_true', help='Print the most recent scheduled/manual run')
    parser.add_argument('--verbose', action='store_true', help='Stream command output as well as saving the log')
    parser.add_argument('--json', action='store_true', dest='json_output', help='Print only the structured report on stdout')
    parser.add_argument('--scheduled', action='store_true', help='Tag this invocation as a launchd run')
    parser.add_argument('--dependencies', action='store_true', help='Read dependency findings from the last update')
    parser.add_argument('--tool', metavar='NAME', help='Show dependency findings for one tool')
    parser.add_argument('--health', action='store_true', help='Verify doctor, services, and scheduled freshness')
    parser.add_argument('--history', action='store_true', help='Show bounded local report history')
    parser.add_argument('--limit', type=int, default=10, choices=range(1, 61), metavar='1..60')
    parser.add_argument('--marktext', action='store_true', help='Resolve and apply MarkText for bootstrap')
    parser.add_argument('--review', metavar='ID')
    parser.add_argument('--assessment', choices=audit_reviews.ASSESSMENTS)
    parser.add_argument('--reason')
    args = parser.parse_args(argv)
    if any(v is not None for v in (args.review, args.assessment, args.reason)):
        if not (args.dependencies and args.review and args.assessment and args.reason) or args.tool:
            parser.error('Use --review ID --assessment VALUE --reason TEXT with maintenance:dependencies')
        try:
            advisory = record_review(args.review, args.assessment, args.reason)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps({'reviewed': advisory}) if args.json_output else f'Review saved for {advisory}.')
        return 0
    if args.tool is not None and not args.dependencies:
        parser.error('--tool requires the dependency report')
    console = Console(verbose=args.verbose, json_output=args.json_output)
    command = partial(run, console=console)
    if args.dependencies:
        return show_dependencies(console, args.tool)
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
        saved = load_report(STATE / 'last-run.json') or {'state': 'not-run-yet'}
        restore_cask_outcomes(saved)
        deps = saved.get('results', {}).get('dependency-audit', {})
        if 'review_counts' in deps:
            audit_reviews.apply_local(deps, STATE / audit_reviews.REVIEW_FILE)
        console.summary(saved, show_steps=True)
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'update.lock').open('a') as lock:
        scheduled_update = args.scheduled and not args.check
        attempted_at, attempted = now(), time.monotonic()
        if not acquire_update_lock(lock, SCHEDULED_LOCK_WAIT if scheduled_update else 0):
            if not scheduled_update:
                console.summary({'state': 'already-running'})
                return 0
            skip = {'origin': 'scheduled', 'state': 'skipped-busy', 'started_at': attempted_at,
                    'finished_at': now(), 'waited_seconds': round(time.monotonic() - attempted, 3)}
            atomic_write(STATE / 'last-scheduled-skip.json', json.dumps(skip, indent=2) + '\n')
            console.summary(skip)
            return os.EX_TEMPFAIL
        LOG.parent.mkdir(parents=True, exist_ok=True)
        rotate_logs()
        started = time.monotonic()
        report = {'started_at': now(), 'check_only': args.check, 'results': {}, 'errors': [], 'log': str(LOG), 'schema_version': 5, 'origin': 'preview' if args.check else 'scheduled' if args.scheduled else 'manual'}
        previous = load_report(STATE / 'last-run.json')
        report['inventory_before'], report['inventory_errors_before'] = inventory()
        report['since_previous_run'] = background_changes(previous, report['inventory_before'], report['inventory_errors_before'])
        if console.verbose:
            console.say('Previewing maintenance updates' if args.check else 'Running maintenance updates')
        with LOG.open('a+', buffering=1) as log:
            def step(name, action):
                if report.get('interrupted'):
                    return
                console.starting(name, args.check)
                stage_started = time.monotonic()
                observation_errors = []
                unverified_stages = set()
                def observe(when):
                    try:
                        if name == 'setup':
                            values, errors = inventory()
                            unverified_stages.update(errors)
                            for section, error in errors.items():
                                detail = f'{when}: {label(section)}: {error}'
                                observation_errors.append(detail)
                                log.write(f'[{now()}] {name} observation: {detail}\n')
                            return values
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
                        if name == 'dependency-audit' and result['state'] in ('advisories', 'clear', 'limited'):
                            result['checked_at'] = now()
                        if result['state'] == 'failed':
                            report['errors'].append(name)
                            log.write(f"[{now()}] {name}: {result['error']}\n")
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
                    result['changes'] = (compare(before, after, unverified_stages=unverified_stages)
                                         if before is not None and after is not None else [])
                    # A failed install can still have changed a subset of artifacts.
                    if name.startswith('brew-cask:') and result['state'] == 'deferred-running-app' and not result.get('plans'):
                        log.seek(output_start)
                        result['plans'] = native_preview(log.read(), before or {})
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
            repo_args = ['status', '--json'] if args.check else ['update', '--skip-dirty', '-y']
            step('shell-repositories', lambda: command([MISE, 'bootstrap', 'repos', *repo_args], log, timeout=600))
            if not args.check:
                # A fresh process reads the pulled manifest before applying it.
                # Native packages use their guarded per-package stages below.
                # Reloading our own LaunchAgent would interrupt this process.
                # Dotfiles (including their font hook) have a dedicated stage.
                # Apply the final MarkText and completion hooks after upgrading tools.
                skip = 'packages,dotfiles'
                if args.scheduled:
                    skip += ',macos-launchd-agents'
                setup_env = dict(ENV, HOST_MAINTENANCE_POST_UPGRADE='1')
                step('setup', lambda: command([MISE, 'bootstrap', '--skip', skip, '--skip-dirty', '-y'], log, env=setup_env))
                step('mise', lambda: command([MISE, 'self-update', '-y', '--no-plugins'], log, timeout=600))
            else:
                step('mise', lambda: command([MISE, 'version', '--json'], log))
            def global_tools():
                if not args.check:
                    return command([MISE, 'upgrade', '--global', '-y'], log)
                command([MISE, 'outdated', '--global', '--json'], log)
                # The structured plan lists eligible targets. The dry run also explains release-age holds.
                try:
                    command([MISE, 'upgrade', '--global', '--dry-run'], log, timeout=120)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    return {'state': 'checked', 'plan_issues': [f'Could not check global tool release-age holds: {error}']}
            step('tools', global_tools)
            step('audit', lambda: audit_tools(log, console))
            step('dependency-audit', lambda: dependency_audit.scan(MISE, HOME, ENV, log, persist_reviews=atomic_write))
            if 'rust' in tomllib.loads(CONFIG.read_text()).get('tools', {}):
                step('rustup', lambda: command([HOME / '.cargo/bin/rustup', 'check'] if args.check else [HOME / '.cargo/bin/rustup', 'self', 'update'], log, timeout=600))

            if not args.check:
                step('shell-completions', lambda: command([MISE, 'run', 'shell:completions'], log, timeout=120))
            if args.check:
                step('variable-fonts', lambda: command([sys.executable, CONFIG.parent / 'scripts/google-fonts.py', '--check'], log, timeout=300))
            else:
                step('variable-fonts', lambda: command([MISE, 'bootstrap', '--only', 'dotfiles', '--skip-dirty', '-y'], log, timeout=300))
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
            changes = compare_dependency_matches(previous or {}, report)
            if changes is not None:
                report['dependency_match_changes'] = changes
            save_report(report, args.scheduled)
            console.summary(report)
            return 130 if report.get('interrupted') else 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
