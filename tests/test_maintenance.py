import copy
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone, timedelta
import importlib.util
import io
import json
import os
import plistlib
from pathlib import Path
import tempfile
import threading
import time
import sys
import tomllib
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('maintenance', Path(__file__).resolve().parents[1] / 'scripts/maintenance.py')
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


class UpdateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / 'config.toml'
        self.original = '[vars]\nmarktext_artifact = "MarkText.app"\n[bootstrap.packages]\n'
        self.config.write_text(self.original)
        self.apps = Path(self.temp.name) / 'Applications'
        self.info = self.apps / 'MarkText.app/Contents/Info.plist'
        self.info.parent.mkdir(parents=True)
        self.info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '0.21.2'}))
        self.settings = patch.multiple(maintenance, CONFIG=self.config, STATE=Path(self.temp.name) / 'state', APPLICATIONS=self.apps)
        self.settings.start()
        version = '0.99.0'
        filename = f'marktext-mac-arm64-{version}.dmg'
        self.release = {'draft': False, 'prerelease': False, 'tag_name': 'v' + version,
                        'published_at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
                        'assets': [{'name': filename, 'digest': 'sha256:' + 'a' * 64,
                                    'browser_download_url': f'https://github.com/marktext/marktext/releases/download/v{version}/{filename}'}]}

    def tearDown(self):
        self.settings.stop()
        self.temp.cleanup()

    def evaluate(self, release=None, check=False):
        with patch.object(maintenance, 'json_url', return_value=[release or self.release]):
            return maintenance.marktext(check, io.StringIO())

    def test_running_app_is_deferred_without_editing_config(self):
        with patch.object(maintenance, 'is_running', return_value=True), patch.object(maintenance, 'run') as run:
            self.assertEqual(self.evaluate()['state'], 'deferred-running-app')
            run.assert_not_called()
        self.assertEqual(self.config.read_text(), self.original)

    def test_preview_never_installs_or_edits(self):
        with patch.object(maintenance, 'run') as run:
            self.assertEqual(self.evaluate(check=True)['state'], 'update-available')
            run.assert_not_called()
        self.assertEqual(self.config.read_text(), self.original)

    def test_official_path_and_checksum_are_required(self):
        for field, value in [('browser_download_url', 'https://example.com/app.dmg'), ('digest', None)]:
            release = copy.deepcopy(self.release)
            release['assets'][0][field] = value
            with self.assertRaises(RuntimeError), patch.object(maintenance, 'run') as run:
                self.evaluate(release)
            run.assert_not_called()
            self.assertEqual(self.config.read_text(), self.original)

    def test_young_or_prerelease_builds_are_never_installed(self):
        for mode in ['young', 'prerelease']:
            release = copy.deepcopy(self.release)
            if mode == 'young':
                release['published_at'] = datetime.now(timezone.utc).isoformat()
            else:
                release['prerelease'] = True
            with self.assertRaises(RuntimeError), patch.object(maintenance, 'run') as run:
                self.evaluate(release)
            run.assert_not_called()
            self.assertEqual(self.config.read_text(), self.original)

    def test_install_failure_preserves_manifest_and_no_verified_success_state(self):
        with patch.object(maintenance, 'is_running', return_value=False), patch.object(maintenance, 'run', side_effect=RuntimeError('download failed')):
            with self.assertRaises(RuntimeError):
                self.evaluate()
        self.assertEqual(self.config.read_text(), self.original)

    def test_success_generates_only_local_verified_release_state(self):
        def command(args, log, **kwargs):
            if 'apply' in args:
                isolated = kwargs['env']
                self.assertNotEqual(isolated['MISE_CONFIG_DIR'], str(self.config.parent))
                generated = tomllib.loads(Path(isolated['MISE_GLOBAL_CONFIG_FILE']).read_text())
                self.assertEqual(generated['bootstrap']['packages']['macos-app:marktext']['sha256'], 'a' * 64)
                self.info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '0.99.0'}))
        with patch.object(maintenance, 'is_running', return_value=False), patch.object(maintenance, 'run', side_effect=command) as run:
            self.assertEqual(self.evaluate()['state'], 'updated')
            self.assertEqual(run.call_count, 2)
        self.assertEqual(self.config.read_text(), self.original)
        metadata = json.loads((maintenance.STATE / 'marktext/release.json').read_text())
        self.assertEqual(metadata['version'], '0.99.0')

    def test_bootstrap_installs_marktext_when_app_is_missing(self):
        self.info.unlink()
        with patch.object(maintenance, 'is_running', return_value=False), patch.object(maintenance, 'run', side_effect=lambda *args, **kwargs: self.info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '0.99.0'}))):
            self.assertEqual(self.evaluate()['state'], 'updated')
        self.assertEqual(self.config.read_text(), self.original)

    def test_atomic_write_keeps_a_dotfile_symlink(self):
        source = Path(self.temp.name) / 'tracked.toml'
        source.write_text('before')
        link = Path(self.temp.name) / 'linked.toml'
        link.symlink_to(source)
        maintenance.atomic_write(link, 'after')
        self.assertTrue(link.is_symlink())
        self.assertEqual(source.read_text(), 'after')


class DownloadProgressTests(unittest.TestCase):
    def test_resumed_download_reports_bytes_and_stalls_without_discarding_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            partial = root / '.opera-2-id-setup.dmg.mise-part'
            partial.write_bytes(b'a' * 1024)
            Path(str(partial) + '.json').write_text('{"total_size": 4096}')
            with patch.object(maintenance.time, 'monotonic', return_value=0) as clock:
                progress = maintenance.CaskDownloadProgress('brew-cask:opera', root=root, idle_timeout=10)
                self.assertEqual(progress(), '')  # Old cache alone is not live progress.
                clock.return_value = 2
                partial.write_bytes(b'a' * 2048)
                self.assertIn('50%', progress())
                clock.return_value = 4
                partial.write_bytes(b'a' * 3072)
                self.assertIn('75%', progress())
                self.assertIn('KiB/s', progress())
                clock.return_value = 15
                with self.assertRaisesRegex(RuntimeError, 'no progress for 10s'):
                    progress()
                self.assertEqual(partial.stat().st_size, 3072)
                partial.rename(root / 'finished.dmg')
                self.assertEqual(progress(), '')

    def test_other_apps_and_unknown_metadata_do_not_break_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            progress = maintenance.CaskDownloadProgress('brew-cask:opera', root=root)
            (root / '.spotify-2.dmg.mise-part').write_bytes(b'x')
            self.assertEqual(progress(), '')
            partial = root / '.opera-2.dmg.mise-part'
            partial.write_bytes(b'x' * 1048576)
            Path(str(partial) + '.json').write_text('incomplete')
            self.assertEqual(progress(), 'Downloading 1.0 MiB')

    def test_timeout_kills_descendants_in_quiet_and_verbose_modes(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose), tempfile.TemporaryDirectory() as temp:
                marker = Path(temp) / 'orphan-survived'
                child = f'import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).write_text("bad")'
                parent = ('import subprocess, sys, time; '
                          f'subprocess.Popen([sys.executable, "-c", {child!r}]); '
                          'time.sleep(30)')
                with redirect_stderr(io.StringIO()):
                    console = maintenance.Console(verbose=verbose)
                with self.assertRaises(maintenance.subprocess.TimeoutExpired):
                    maintenance.run([sys.executable, '-c', parent], io.StringIO(), timeout=0.3, console=console)
                time.sleep(1.2)
                self.assertFalse(marker.exists(), 'Updater child continued after timeout')

    def test_quiet_interrupt_also_stops_process_group(self):
        real_popen = maintenance.subprocess.Popen
        processes = []
        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(maintenance.subprocess, 'Popen', side_effect=spawn), patch.object(maintenance.selectors.DefaultSelector, 'select', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                maintenance.run([sys.executable, '-c', 'import time; time.sleep(30)'], io.StringIO())
        self.assertIsNotNone(processes[0].poll())

    def test_casks_bound_checks_and_updates_and_observe_downloads(self):
        calls = []
        def command(args, log, **kwargs):
            calls.append((args, kwargs))
            log.write('already up to date\n')
        with patch.object(maintenance, 'cask_apps', return_value=[]):
            maintenance.cask_update('brew-cask:opera', False, io.StringIO(), command)
            maintenance.cask_update('brew-cask:opera', True, io.StringIO(), command)
        self.assertEqual([kw['timeout'] for _, kw in calls], [1800, 1800, 120, 120])
        self.assertTrue(all(isinstance(kw['progress'], maintenance.CaskDownloadProgress) for _, kw in calls[:2]))
        self.assertTrue(all('progress' not in kw for _, kw in calls[2:]))


class MaintenanceOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / 'config.toml'
        config.write_text('[tools]\n[bootstrap.packages]\n"brew-cask:google-chrome" = "latest"\n')
        self.state = root / 'state'
        self.settings = patch.multiple(maintenance, CONFIG=config, STATE=self.state, LOG=root / 'maintenance.log')
        self.settings.start()
        self.inventory_patch = patch.object(maintenance, 'inventory', return_value=({}, {}))
        self.inventory_patch.start()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def tearDown(self):
        self.inventory_patch.stop()
        self.settings.stop()
        self.temp.cleanup()

    def invoke(self, args, command=None, running=False):
        if command is None:
            def command(args, log, **kwargs):
                if 'outdated' in args:
                    log.write('{}\n')
                elif '--check' in args:
                    log.write('{"files": [], "revisions": {}}\n')
                else:
                    log.write('mise already up to date\n')
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr), patch.object(maintenance, 'snapshot', return_value={'test': maintenance.entity('test', '1.0')}), patch.object(maintenance, 'run', side_effect=command, return_value=None), patch.object(maintenance, 'marktext', return_value={'state': 'current', 'installed': '0.21.2'}), patch.object(maintenance, 'cask_apps', return_value=[Path('/Applications/Google Chrome.app')]), patch.object(maintenance, 'is_running', return_value=running):
            return maintenance.main(args)

    def test_progress_is_visible_before_commands_start_and_failures_continue(self):
        calls = []
        def command(args, log, **kwargs):
            calls.append(list(map(str, args)))
            if 'upgrade' in args:
                self.assertIn('Running maintenance updates', self.stdout.getvalue())
            if 'upgrade' in args and '--global' in args:
                raise RuntimeError('simulated update failure')
            if '--dry-run' in args:
                log.write('skipped: installed app is running and updates itself\n')
        self.assertEqual(self.invoke([], command=command, running=True), 1)
        output = self.stdout.getvalue()
        self.assertIn('Global tools: simulated update failure', output)
        self.assertNotIn('MarkText: Current', output)
        self.assertIn('Deferred while running', output)
        self.assertIn('Google Chrome', output)
        self.assertIn('1 error · 1 deferred', output)
        report = json.loads((self.state / 'last-run.json').read_text())
        self.assertEqual(report['errors'], ['tools'])
        self.assertIn('elapsed_seconds', report['results']['tools'])
        self.assertTrue(any('shell:completions' in call for call in calls))

    def test_json_stdout_is_exactly_the_saved_report(self):
        self.assertEqual(self.invoke(['--json']), 0)
        report = json.loads(self.stdout.getvalue())
        self.assertEqual(report, json.loads((self.state / 'last-run.json').read_text()))
        self.assertEqual(report['state'], 'success')
        self.assertEqual(self.stderr.getvalue(), '')
        self.assertIn('log', report)

    def test_preview_and_status_do_not_claim_versions_changed(self):
        self.assertEqual(self.invoke(['--check']), 0)
        output = self.stdout.getvalue()
        self.assertNotIn('Checking Global tools', output)
        self.assertNotIn('Google Chrome: Check completed', output)
        self.assertIn('No changes applied — preview only.', output)
        self.assertNotIn('Updated to', output)
        self.assertTrue((self.state / 'last-check.json').exists())
        self.assertFalse((self.state / 'last-run.json').exists())
        self.stdout.seek(0); self.stdout.truncate()
        with redirect_stdout(self.stdout):
            self.assertEqual(maintenance.main(['--status', '--json']), 0)
        self.assertEqual(json.loads(self.stdout.getvalue()), {'state': 'not-run-yet'})

    def test_status_reads_older_report_format(self):
        self.state.mkdir()
        (self.state / 'last-run.json').write_text(json.dumps({'state': 'success', 'started_at': '2026-10-12T16:00:00+00:00', 'finished_at': '2026-10-12T16:00:02+00:00', 'results': {'tools': {'state': 'success'}}, 'errors': []}))
        with redirect_stdout(self.stdout), patch.object(maintenance, 'run') as command:
            self.assertEqual(maintenance.main(['--status']), 0)
        command.assert_not_called()
        self.assertIn('Maintenance finished in 2.0s', self.stdout.getvalue())

    def test_lock_contention_still_emits_valid_json(self):
        self.state.mkdir()
        with (self.state / 'update.lock').open('a') as lock:
            maintenance.fcntl.flock(lock, maintenance.fcntl.LOCK_EX | maintenance.fcntl.LOCK_NB)
            self.assertEqual(self.invoke(['--json']), 0)
        self.assertEqual(json.loads(self.stdout.getvalue()), {'state': 'already-running'})
        self.assertFalse((self.state / 'last-run.json').exists())

    def test_verbose_streams_partial_output_before_process_finishes(self):
        visible = threading.Event()
        class ObservedOutput(io.StringIO):
            def write(self, text):
                result = super().write(text)
                if 'live-output' in self.getvalue():
                    visible.set()
                return result
        stderr = ObservedOutput()
        with redirect_stdout(self.stdout), redirect_stderr(stderr):
            console = maintenance.Console(verbose=True)
        log = io.StringIO()
        errors = []
        def worker():
            try:
                maintenance.run([sys.executable, '-c', 'import time; print("live-output", end="", flush=True); time.sleep(0.6)'], log, timeout=5, console=console)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(visible.wait(3), 'Partial output was not streamed')
            self.assertTrue(thread.is_alive(), 'Output appeared only after completion')
        finally:
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIn('live-output', log.getvalue())
        self.assertIn('live-output', stderr.getvalue())

    def test_verbose_timeout_and_failure_keep_diagnostics(self):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            console = maintenance.Console(verbose=True)
        with self.assertRaises(maintenance.subprocess.TimeoutExpired):
            maintenance.run([sys.executable, '-c', 'import time; print("waiting", flush=True); time.sleep(30)'], io.StringIO(), timeout=0.2, console=console)
        log = io.StringIO()
        with self.assertRaisesRegex(RuntimeError, 'Command exited 7'):
            maintenance.run([sys.executable, '-c', 'print("failure detail", flush=True); raise SystemExit(7)'], log, timeout=3, console=console)
        self.assertIn('failure detail', log.getvalue())
        self.assertIn('failure detail', self.stderr.getvalue())

    def test_verbose_json_keeps_command_output_off_stdout(self):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            console = maintenance.Console(verbose=True, json_output=True)
        maintenance.run([sys.executable, '-c', 'print("command detail")'], io.StringIO(), timeout=3, console=console)
        console.starting('tools', False)
        report = {'state': 'success', 'results': {}, 'errors': []}
        console.summary(report)
        self.assertEqual(json.loads(self.stdout.getvalue()), report)
        self.assertIn('command detail', self.stderr.getvalue())

    def test_no_color_is_honored_even_on_a_terminal(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        terminal = Terminal()
        with redirect_stdout(terminal), patch.dict(os.environ, {'NO_COLOR': '', 'TERM': 'xterm-256color'}):
            console = maintenance.Console()
        console.finished('tools', {'state': 'success'})
        self.assertNotIn('\033[', terminal.getvalue())


class ChangeReportingTests(unittest.TestCase):
    def test_comparison_includes_installs_removals_and_commits_but_not_metadata(self):
        before = {'app': maintenance.entity('Postman', '1.0'),
                  'repo': maintenance.entity('repo', 'a' * 40, 'revision', dirty=False),
                  'old': maintenance.entity('old', '1.0'),
                  'font': maintenance.entity('font', 'a', 'font', source_revision='old')}
        after = {'app': maintenance.entity('Postman', '2.0'),
                 'repo': maintenance.entity('repo', 'b' * 40, 'revision', dirty=True),
                 'new': maintenance.entity('new', '1.0'),
                 'font': maintenance.entity('font', 'a', 'font', source_revision='new')}
        changes = maintenance.compare(before, after)
        self.assertEqual({c['id'] for c in changes}, {'app', 'repo', 'old', 'new'})
        self.assertEqual(next(c['action'] for c in changes if c['id'] == 'old'), 'removed')
        self.assertEqual(next(c['action'] for c in changes if c['id'] == 'new'), 'installed')

    def test_native_preview_preserves_versions_and_dependencies(self):
        before = {'brew:ffmpeg': maintenance.entity('ffmpeg', '9.0'),
                  'app': maintenance.entity('Postman', '1.0')}
        text = 'pour ffmpeg/9.1_1 (requested)\nbuild xz/5.9 from source (dependency, no bottle)\ninstall cask postman/2.0\nlink app /Applications/Postman.app\n'
        plans = maintenance.native_preview(text, before)
        self.assertEqual([(p['name'], p['after']) for p in plans], [('ffmpeg', '9.1_1'), ('xz', '5.9'), ('Postman', '2.0')])
        self.assertEqual(plans[0]['before'], '9.0')
        self.assertEqual(maintenance.native_preview('mise already up to date', before), [])

    def test_missing_and_build_only_app_versions_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / 'App.app'; (app / 'Contents').mkdir(parents=True)
            path = app / 'Contents/Info.plist'
            path.write_bytes(maintenance.plistlib.dumps({'CFBundleShortVersionString': '1.0', 'CFBundleVersion': 'abc'}))
            record = maintenance.app_snapshot(app, 'App')[str(app)]
            self.assertEqual(record['value'], '1.0 (build abc)')
            path.unlink()
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                maintenance.app_snapshot(app, 'App')

    def test_unchanged_summary_is_compact_and_never_lists_checks(self):
        output = io.StringIO()
        with redirect_stdout(output): console = maintenance.Console()
        report = {'schema_version': 2, 'state': 'success', 'elapsed_seconds': 10.2,
                  'results': {'tools': {'state': 'success'}, 'shell-completions': {'state': 'success', 'changes': []}},
                  'changes': [], 'planned_changes': [], 'errors': []}
        console.summary(report)
        self.assertIn('No installed versions or contents changed.', output.getvalue())
        self.assertIn('Shell completions regenerated (content unchanged).', output.getvalue())
        self.assertNotIn('Global tools', output.getvalue())
        self.assertLess(len(output.getvalue().splitlines()), 12)

    def test_changes_survive_a_failed_stage_and_unknown_is_not_unchanged(self):
        output = io.StringIO()
        with redirect_stdout(output): console = maintenance.Console()
        report = {'schema_version': 2, 'state': 'failed', 'errors': ['tools'],
                  'changes': [{'name': 'Node.js', 'kind': 'version', 'before': '26.1', 'after': '26.2'}],
                  'results': {'tools': {'state': 'failed', 'error': 'second install failed'},
                              'brew': {'state': 'success', 'observation_errors': ['before: unavailable']}}}
        console.summary(report)
        text = output.getvalue()
        self.assertIn('Node.js: 26.1 → 26.2', text)
        self.assertIn('partial results', text)
        self.assertIn('Change verification unavailable', text)
        self.assertNotIn('No installed versions', text)

    def test_unknown_native_preview_does_not_claim_nothing_available(self):
        self.assertTrue(maintenance.preview_result('brew', 'new unfamiliar plan format', {})['plan_unverified'])
        self.assertFalse(maintenance.preview_result('brew', 'mise brew: all formulae already poured', {})['plan_unverified'])

    def test_live_progress_reuses_one_line_and_cleans_up(self):
        class Terminal(io.StringIO):
            def isatty(self): return True
        output = Terminal()
        with redirect_stdout(output), patch.dict(os.environ, {'TERM': 'xterm-256color', 'NO_COLOR': ''}):
            console = maintenance.Console()
        console.starting('tools', False)
        console.finished('tools', {'state': 'success'})
        console.starting('brew-cask:postman', False)
        console.finished('brew-cask:postman', {'state': 'success'})
        self.assertIn('Global tools', output.getvalue())
        self.assertIn('Postman', output.getvalue())
        self.assertNotIn('\n', output.getvalue())
        self.assertIsNone(console._thread)
        self.assertEqual(console._width, 0)

    def test_partial_changes_are_saved_in_json_after_command_failure(self):
        fixture = MaintenanceOutputTests('test_json_stdout_is_exactly_the_saved_report');fixture.setUp()
        counts = {}
        def snapshot(name):
            counts[name] = counts.get(name, 0) + 1
            version = '2.0' if name == 'tools' and counts[name] == 2 else '1.0'
            return {name: maintenance.entity(name, version)}
        def command(args, log, **kwargs):
            if 'upgrade' in args: raise RuntimeError('partial install failed')
        try:
            with redirect_stdout(fixture.stdout), patch.object(maintenance, 'snapshot', side_effect=snapshot), patch.object(maintenance, 'run', side_effect=command), patch.object(maintenance, 'marktext', return_value={'state': 'current', 'installed': '0.21.2'}), patch.object(maintenance, 'cask_apps', return_value=[]):
                code = maintenance.main(['--json'])
            report = json.loads(fixture.stdout.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(report['changes'][0]['stage'], 'tools')
            self.assertEqual(report['changes'][0]['before'], '1.0')
            self.assertEqual(report['changes'][0]['after'], '2.0')
            self.assertEqual(report['results']['tools']['state'], 'failed')
        finally: fixture.tearDown()


    def test_unreadable_state_does_not_skip_updates_or_claim_no_changes(self):
        fixture = MaintenanceOutputTests('test_json_stdout_is_exactly_the_saved_report');fixture.setUp()
        def snapshot(name):
            if name == 'tools': raise RuntimeError('state unavailable')
            return {name: maintenance.entity(name, '1.0')}
        try:
            with redirect_stdout(fixture.stdout), patch.object(maintenance, 'snapshot', side_effect=snapshot), patch.object(maintenance, 'run', return_value=None) as command, patch.object(maintenance, 'marktext', return_value={'state': 'current', 'installed': '0.21.2'}), patch.object(maintenance, 'cask_apps', return_value=[]):
                self.assertEqual(maintenance.main([]), 0)
            self.assertTrue(any('upgrade' in call.args[0] for call in command.call_args_list))
            report = json.loads((fixture.state / 'last-run.json').read_text())
            self.assertEqual(len(report['results']['tools']['observation_errors']), 2)
            self.assertIn('No changes verified', fixture.stdout.getvalue())
            self.assertNotIn('No installed versions or contents changed', fixture.stdout.getvalue())
        finally: fixture.tearDown()

    def test_interrupt_stops_later_updates_and_keeps_partial_changes(self):
        fixture = MaintenanceOutputTests('test_json_stdout_is_exactly_the_saved_report');fixture.setUp()
        count = {}
        def snapshot(name):
            count[name] = count.get(name, 0) + 1
            return {name: maintenance.entity(name, '2.0' if name == 'tools' and count[name] == 2 else '1.0')}
        def command(args, log, **kwargs):
            if 'upgrade' in args: raise KeyboardInterrupt()
        try:
            with redirect_stdout(fixture.stdout), patch.object(maintenance, 'snapshot', side_effect=snapshot), patch.object(maintenance, 'run', side_effect=command), patch.object(maintenance, 'marktext') as marktext:
                self.assertEqual(maintenance.main(['--json']), 130)
            marktext.assert_not_called()
            report = json.loads(fixture.stdout.getvalue())
            self.assertEqual(report['state'], 'interrupted')
            self.assertEqual(set(report['results']), {'shell-repositories', 'setup', 'mise', 'tools'})
            self.assertEqual(report['changes'][0]['after'], '2.0')
        finally: fixture.tearDown()

    def test_changed_font_styles_are_grouped_without_losing_json_details(self):
        output = io.StringIO()
        with redirect_stdout(output): console = maintenance.Console()
        changes = [{'name': name, 'kind': 'font', 'before': 'a', 'after': 'b', 'action': 'updated'}
                   for name in ['Poppins-Regular.otf', 'Poppins-Bold.otf']]
        report = {'schema_version': 2, 'state': 'success', 'results': {}, 'changes': changes, 'errors': []}
        console.summary(report)
        self.assertIn('Poppins: 2 font files updated', output.getvalue())
        self.assertNotIn('Poppins-Regular.otf', output.getvalue())
        self.assertEqual(len(report['changes']), 2)

    def test_broken_native_link_cannot_look_like_a_successful_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);(root / 'opt').mkdir();(root / 'Cellar').mkdir()
            (root / 'opt/git').symlink_to(root / 'Cellar/git/1.0')
            disposable_config = root / 'config.toml'
            disposable_config.write_text('[bootstrap.packages]\n')
            with patch.object(maintenance, 'PREFIX', root), patch.object(maintenance, 'CONFIG', disposable_config):
                with self.assertRaisesRegex(RuntimeError, 'Broken native package link'):
                    maintenance.snapshot('brew')


    def test_preview_wording_never_says_a_font_was_installed(self):
        change = {'name': 'OpenSans.ttf', 'kind': 'font', 'before': None, 'after': 'available'}
        self.assertEqual(maintenance.format_change(change, planned=True), 'OpenSans.ttf: update available')
        change = {'name': 'uv', 'kind': 'version', 'before': None, 'after': '0.14.0'}
        self.assertEqual(maintenance.format_change(change, planned=True), 'uv: not installed → 0.14.0')

    def test_repository_observer_detects_real_commits_without_counting_dirty_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repo = root / 'checkout';repo.mkdir()
            maintenance.read_command(['git', '-C', repo, 'init', '-q'])
            path = repo / 'README';path.write_text('before')
            def commit():
                maintenance.read_command(['git', '-C', repo, 'add', 'README'])
                maintenance.read_command(['git', '-C', repo, '-c', 'commit.gpgsign=false', '-c', 'user.name=Maintenance fixture', '-c', 'user.email=maintenance@example.invalid', 'commit', '-qm', 'fixture'])
            commit()
            disposable_config = root / 'disposable-config.toml'
            disposable_config.write_text('[bootstrap.repos]\n' + json.dumps(str(repo)) + ' = {url = "https://example.invalid/fixture", ref = "main"}\n')
            with patch.object(maintenance, 'CONFIG', disposable_config):
                before = maintenance.snapshot('shell-repositories')
                path.write_text('edited but not committed')
                dirty = maintenance.snapshot('shell-repositories')
                self.assertEqual(maintenance.compare(before, dirty), [])
                self.assertTrue(dirty[str(repo)]['dirty'])
                commit()
                changes = maintenance.compare(before, maintenance.snapshot('shell-repositories'))
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]['kind'], 'revision')
            self.assertEqual(len(changes[0]['before']), 40)
            self.assertNotEqual(changes[0]['before'], changes[0]['after'])


class SyncHealthHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MaintenanceOutputTests('test_json_stdout_is_exactly_the_saved_report')
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_pulled_declaration_is_applied_before_upgrade(self):
        calls = []
        def command(args, log, **kwargs):
            calls.append(list(map(str, args)))
            if 'repos' in args:
                maintenance.CONFIG.write_text('[tools]\n[bootstrap.packages]\n"brew:new-tool" = "latest"\n')
            log.write('already up to date\n')
        self.assertEqual(self.fixture.invoke([], command=command), 0)
        repo = next(i for i, c in enumerate(calls) if 'repos' in c)
        bootstrap = next(i for i, c in enumerate(calls) if '--skip' in c)
        apply = next(i for i, c in enumerate(calls) if 'apply' in c and '--manager' in c)
        upgrade = next(i for i, c in enumerate(calls) if 'upgrade' in c and '--manager' in c)
        self.assertLess(repo, bootstrap);self.assertLess(bootstrap, apply);self.assertLess(apply, upgrade)
        self.assertIn('--skip-dirty', calls[repo]);self.assertIn('brew', calls[apply])

    def test_manual_success_does_not_overwrite_scheduled_failure(self):
        scheduled = {'state': 'failed', 'finished_at': maintenance.now(), 'errors': ['tools']}
        maintenance.atomic_write(maintenance.STATE / 'last-scheduled.json', json.dumps(scheduled))
        self.assertEqual(self.fixture.invoke(['--json']), 0)
        self.assertEqual(maintenance.load_report(maintenance.STATE / 'last-scheduled.json'), scheduled)
        self.assertEqual(maintenance.load_report(maintenance.STATE / 'last-manual.json')['state'], 'success')
        with patch.object(maintenance, 'read_command', return_value='state = running\n--scheduled\nlast exit code = 0'):
            report = maintenance.health()
        self.assertEqual(report['state'], 'attention-needed')
        self.assertEqual(report['checks']['scheduled-freshness']['state'], 'failed')

    def test_scheduled_run_has_separate_report_and_does_not_reload_itself(self):
        calls = []
        def command(args, log, **kwargs):
            calls.append(list(map(str, args))); log.write('already up to date\n')
        self.assertEqual(self.fixture.invoke(['--scheduled', '--json'], command=command), 0)
        report = maintenance.load_report(maintenance.STATE / 'last-scheduled.json')
        self.assertEqual(report['origin'], 'scheduled')
        self.assertFalse((maintenance.STATE / 'last-manual.json').exists())
        self.assertIn('packages,macos-launchd-agents', next(c for c in calls if '--skip' in c))

    def test_health_detects_missing_stale_and_failed_services(self):
        with patch.object(maintenance, 'read_command', side_effect=RuntimeError('not loaded')):
            self.assertEqual(maintenance.health()['state'], 'attention-needed')
        maintenance.atomic_write(maintenance.STATE / 'last-scheduled.json', json.dumps({'state': 'success', 'finished_at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}))
        with patch.object(maintenance, 'read_command', return_value='state = running\n--scheduled\nlast exit code = 0'):
            self.assertEqual(maintenance.health()['checks']['scheduled-freshness']['state'], 'failed')
        maintenance.atomic_write(maintenance.STATE / 'last-scheduled.json', json.dumps({'state': 'success', 'finished_at': maintenance.now()}))
        with patch.object(maintenance, 'read_command', return_value='state = running\n--scheduled\nlast exit code = 0'):
            self.assertEqual(maintenance.health()['state'], 'healthy')
        with patch.object(maintenance, 'read_command', return_value='state = running\n--scheduled\nlast exit code = 2'):
            self.assertEqual(maintenance.health()['checks']['mise-maintenance']['state'], 'failed')

    def test_doctor_warnings_are_not_silently_healthy(self):
        with patch.object(maintenance, 'read_command', return_value='1 warning found:\n\n1. last auto_update failed\n\nNo problems found'):
            report = maintenance.health()
        self.assertEqual(report['checks']['mise-doctor']['state'], 'warning')
        self.assertEqual(report['state'], 'attention-needed')

    def test_history_is_bounded_and_keeps_latest_reports(self):
        for i in range(7):
            report = {'started_at': (datetime.now(timezone.utc) + timedelta(seconds=i)).isoformat(), 'origin': 'manual', 'check_only': False, 'state': 'success', 'results': {}, 'changes': [], 'errors': [], 'number': i}
            maintenance.save_report(report, keep=3)
        files = sorted((maintenance.STATE / 'history').glob('*.json'))
        self.assertEqual([json.loads(p.read_text())['number'] for p in files], [4, 5, 6])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(maintenance.main(['--history', '--json', '--limit', '2']), 0)
        self.assertEqual([r['number'] for r in json.loads(output.getvalue())], [6, 5])

    def test_rotation_preserves_launchd_open_file_descriptor(self):
        path = maintenance.LOG.parent / 'mise-maintenance-agent.log'
        path.write_text('old output')
        with path.open('a') as agent:
            inode = path.stat().st_ino
            maintenance.rotate_logs(limit=3)
            agent.write('new');agent.flush()
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(path.read_text(), 'new')
        self.assertEqual(path.with_suffix('.previous.log').read_text(), 'old output')

    def test_background_changes_are_separate_and_unknown_is_not_removal(self):
        previous = {'inventory_after': {'tools/uv': maintenance.entity('uv', '1', stage='tools'), 'brew/git': maintenance.entity('git', '1', stage='brew')}}
        before = {'tools/uv': maintenance.entity('uv', '2', stage='tools')}
        changes = maintenance.background_changes(previous, before, {'brew': 'unreadable'})
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['name'], 'uv')
        report = {'state': 'success', 'schema_version': 3, 'results': {}, 'errors': [], 'changes': [], 'since_previous_run': changes}
        with redirect_stdout(self.fixture.stdout): maintenance.Console().summary(report)
        self.assertIn('outside this run', self.fixture.stdout.getvalue())

    def test_removed_declaration_is_not_reported_as_app_uninstall(self):
        previous = {'inventory_after': {'brew-cask:example/app': maintenance.entity('Example', '1', stage='brew-cask:example')}}
        self.assertEqual(maintenance.background_changes(previous, {}, {}), [])

    def test_running_current_pending_and_unknown_are_distinct(self):
        self.assertEqual(maintenance.cask_result('already up to date', True)['state'], 'current')
        self.assertEqual(maintenance.cask_result('install cask test/2.0', True)['state'], 'deferred-running-app')
        self.assertEqual(maintenance.cask_result('skipped: installed app is running and updates itself', True)['availability'], 'confirmed')
        self.assertEqual(maintenance.cask_result('skipped: installed app is current, newer, or incomparable', True)['state'], 'availability-unknown')
        self.assertEqual(maintenance.cask_result('unrecognized output', True)['state'], 'availability-unknown')

    def test_running_app_preview_never_applies_and_tracks_repeated_deferrals(self):
        def command(args, log, **kwargs):
            if 'google-chrome' in ' '.join(map(str, args)):
                self.assertIn('--dry-run', args)
                log.write('skipped: installed app is running and updates itself\n')
        for i in range(2):
            self.fixture.stdout.seek(0);self.fixture.stdout.truncate()
            self.assertEqual(self.fixture.invoke(['--json'], command=command, running=True), 0)
        result = maintenance.load_report(maintenance.STATE / 'last-run.json')['results']['brew-cask:google-chrome']
        self.assertEqual(result['consecutive_deferrals'], 2)
        self.assertIn('deferred_since', result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
