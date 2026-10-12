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
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr), patch.object(maintenance, 'run', side_effect=command, return_value=None), patch.object(maintenance, 'marktext', return_value={'state': 'current', 'installed': '0.21.2'}), patch.object(maintenance, 'cask_apps', return_value=[Path('/Applications/Google Chrome.app')]), patch.object(maintenance, 'is_running', return_value=running):
            return maintenance.main(args)

    def test_progress_is_visible_before_commands_start_and_failures_continue(self):
        calls = []
        def command(args, log, **kwargs):
            calls.append(list(map(str, args)))
            if 'upgrade' in args:
                self.assertIn('Processing Global tools', self.stdout.getvalue())
            if 'upgrade' in args:
                raise RuntimeError('simulated update failure')
        self.assertEqual(self.invoke([], command=command, running=True), 1)
        output = self.stdout.getvalue()
        self.assertIn('Global tools: simulated update failure', output)
        self.assertIn('MarkText: Current (0.21.2)', output)
        self.assertIn('Google Chrome: Deferred', output)
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
        self.assertIn('Checking Global tools', output)
        self.assertIn('Google Chrome: Check completed', output)
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
        self.assertIn('Maintenance completed in 2.0s', self.stdout.getvalue())

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
