import copy
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
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
        self.original = maintenance.CONFIG.read_text()
        self.config.write_text(self.original)
        self.settings = patch.multiple(maintenance, CONFIG=self.config, STATE=Path(self.temp.name) / 'state')
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

    def test_install_failure_restores_original_config(self):
        with patch.object(maintenance, 'is_running', return_value=False), patch.object(maintenance, 'run', side_effect=RuntimeError('download failed')):
            with self.assertRaises(RuntimeError):
                self.evaluate()
        self.assertEqual(self.config.read_text(), self.original)

    def test_success_changes_only_marktext_download_fields(self):
        before = tomllib.loads(self.original)
        with patch.object(maintenance, 'is_running', return_value=False), patch.object(maintenance, 'run') as run, patch.object(maintenance.plistlib, 'load', side_effect=[{'CFBundleShortVersionString': '0.21.2'}, {'CFBundleShortVersionString': '0.99.0'}]):
            self.assertEqual(self.evaluate()['state'], 'updated')
            self.assertEqual(run.call_count, 2)
        after = tomllib.loads(self.config.read_text())
        entry = after['bootstrap']['packages']['macos-app:marktext']
        self.assertEqual(entry['version'], '0.99.0')
        self.assertEqual(entry['sha256'], 'a' * 64)
        after['bootstrap']['packages']['macos-app:marktext'] = before['bootstrap']['packages']['macos-app:marktext']
        self.assertEqual(after, before)


    def test_atomic_write_keeps_a_dotfile_symlink(self):
        source = Path(self.temp.name) / 'tracked.toml'
        source.write_text('before')
        link = Path(self.temp.name) / 'linked.toml'
        link.symlink_to(source)
        maintenance.atomic_write(link, 'after')
        self.assertTrue(link.is_symlink())
        self.assertEqual(source.read_text(), 'after')


class MaintenanceOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / 'config.toml'
        config.write_text('[tools]\n[bootstrap.packages]\n"brew-cask:google-chrome" = "latest"\n')
        self.state = root / 'state'
        self.settings = patch.multiple(maintenance, CONFIG=config, STATE=self.state, LOG=root / 'maintenance.log')
        self.settings.start()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def tearDown(self):
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
            if 'upgrade' in args:
                raise RuntimeError('simulated update failure')
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
            self.assertEqual(set(report['results']), {'mise', 'tools'})
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
            with patch.object(maintenance, 'PREFIX', root):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
