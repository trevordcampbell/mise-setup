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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

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


class VendorReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.apps = Path(self.temp.name)
        self.settings = patch.object(maintenance, 'APPLICATIONS', self.apps)
        self.settings.start()
        self.key = 'brew-cask:google-chrome'
        self.release = {'version': '156.0.8078.48', 'source': 'https://example.invalid/test', 'channel': 'stable'}
        self.write_app(self.key, '155.0.8059.85', '8059.85')

    def tearDown(self):
        self.settings.stop()
        self.temp.cleanup()

    def write_app(self, key, version, build, **extras):
        filename, identifier = maintenance.VENDOR_APPS[key]
        app = self.apps / filename
        info = app / 'Contents/Info.plist'
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(plistlib.dumps(dict(CFBundleShortVersionString=version,
                        CFBundleVersion=build, CFBundleIdentifier=identifier, **extras)))
        if key == 'brew-cask:cursor':
            product = app / 'Contents/Resources/app/product.json'
            product.parent.mkdir(parents=True, exist_ok=True)
            product.write_text('{"quality": "stable", "commit": "different-source-hash"}')
        return app

    def test_chrome_uses_full_marketing_version_not_incompatible_build(self):
        self.assertEqual(maintenance.assess_vendor_release(self.key, self.release)['state'], 'update-available')
        self.write_app(self.key, self.release['version'], '8078.48')
        self.assertEqual(maintenance.assess_vendor_release(self.key, self.release)['state'], 'current')

    def test_current_and_newer_apps_never_trigger_native_replacement(self):
        for version, state in [('156.0.8078.48', 'current'), ('157.0.8000.1', 'newer')]:
            self.write_app(self.key, version, 'unrelated-build')
            with patch.object(maintenance, 'vendor_release', return_value=self.release), patch.object(maintenance, 'native_cask_update') as native:
                result = maintenance.cask_update(self.key, False, io.StringIO(), None)
            self.assertEqual(result['state'], state)
            native.assert_not_called()

    def test_outdated_running_app_is_confirmed_pending_and_not_replaced(self):
        app = self.apps / 'Google Chrome.app'
        calls = []
        def command(args, log, **kwargs):
            calls.append(args)
            log.write('skipped: installed app is current, newer, or incomparable\n')
        with patch.object(maintenance, 'vendor_release', return_value=self.release), patch.object(maintenance, 'cask_apps', return_value=[app]), patch.object(maintenance, 'is_running', return_value=True):
            result = maintenance.cask_update(self.key, False, io.StringIO(), command)
        self.assertEqual(result['state'], 'deferred-running-app')
        self.assertEqual(result['plans'][0]['after'], self.release['version'])
        self.assertTrue(all('--dry-run' in args for args in calls))

    def test_native_metadata_lag_does_not_hide_vendor_update(self):
        with patch.object(maintenance, 'vendor_release', return_value=self.release), patch.object(maintenance, 'native_cask_update', return_value={'state': 'current'}), patch.object(maintenance, 'is_running', return_value=False):
            result = maintenance.cask_update(self.key, False, io.StringIO(), None)
        self.assertEqual(result['state'], 'update-available')
        self.assertIn('did not apply', result['notes'][0])

    def test_success_rereads_installed_version(self):
        def install(*args):
            self.write_app(self.key, self.release['version'], '8078.48')
            return {'state': 'success'}
        with patch.object(maintenance, 'vendor_release', return_value=self.release), patch.object(maintenance, 'native_cask_update', side_effect=install):
            result = maintenance.cask_update(self.key, False, io.StringIO(), None)
        self.assertEqual(result['vendor_check']['state'], 'current')

    def test_missing_app_preview_reports_install_without_claiming_current(self):
        info = self.apps / 'Google Chrome.app/Contents/Info.plist'
        info.unlink(); info.parent.rmdir(); info.parent.parent.rmdir()
        with patch.object(maintenance, 'vendor_release', return_value=self.release), patch.object(maintenance, 'native_cask_update', return_value={'state': 'checked'}):
            result = maintenance.cask_update(self.key, True, io.StringIO(), None)
        self.assertEqual(result['state'], 'update-available')
        self.assertIsNone(result['plans'][0]['before'])

    def test_summary_shows_current_and_newer_without_ambiguous_warning(self):
        self.write_app(self.key, self.release['version'], '8078.48')
        assessment = maintenance.assess_vendor_release(self.key, self.release)
        report = {'state': 'success', 'schema_version': 3, 'errors': [], 'changes': [],
                  'results': {self.key: {'state': 'current', 'vendor_check': assessment},
                              'brew-cask:cursor': {'state': 'newer', 'vendor_check':
                                  dict(assessment, state='newer', installed='3.27.0', available='3.26.13')}}}
        output = io.StringIO()
        with redirect_stdout(output): maintenance.Console().summary(report)
        self.assertNotIn('Chrome', output.getvalue())
        self.assertNotIn('Cursor', output.getvalue())
        self.assertEqual(report['results']['brew-cask:cursor']['vendor_check']['available'], '3.26.13')
        self.assertNotIn('verification unavailable', output.getvalue())
        self.assertNotIn('incomparable', output.getvalue())

    def test_feed_failure_is_explicit_even_if_native_says_current(self):
        with patch.object(maintenance, 'vendor_release', side_effect=RuntimeError('offline')), patch.object(maintenance, 'native_cask_update', return_value={'state': 'current'}):
            result = maintenance.cask_update(self.key, False, io.StringIO(), None)
        self.assertEqual(result['state'], 'availability-unknown')
        self.assertEqual(result['vendor_check']['error'], 'offline')

    def test_unsupported_formats_and_identities_preserve_the_app(self):
        for version, identifier in [('156.0-preview', 'com.google.Chrome'), ('156.0.8078.48', 'unexpected.id')]:
            app = self.write_app(self.key, version, '1')
            info = app / 'Contents/Info.plist'
            data = plistlib.loads(info.read_bytes()); data['CFBundleIdentifier'] = identifier
            info.write_bytes(plistlib.dumps(data))
            with patch.object(maintenance, 'vendor_release', return_value=self.release), patch.object(maintenance, 'native_cask_update') as native:
                result = maintenance.cask_update(self.key, False, io.StringIO(), None)
            self.assertEqual(result['state'], 'availability-unknown')
            native.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'Incompatible'):
            maintenance.compare_numeric_versions('155.1', '156.0.8078.48')

    def test_chrome_filters_partial_and_ended_rollouts_and_empty_feed(self):
        records = {'releases': [
            {'version': '156.0.8078.48', 'fraction': 1, 'serving': {}},
            {'version': '157.0.9000.1', 'fraction': 0.1},
            {'version': '158.0.9000.1', 'fraction': 1, 'serving': {'endTime': 'past'}}]}
        with patch.object(maintenance, 'json_url', return_value=records):
            self.assertEqual(maintenance.vendor_release(self.key)['version'], '156.0.8078.48')
        for records in ({'releases': []}, {'releases': [], 'nextPageToken': 'more'}):
            with patch.object(maintenance, 'json_url', return_value=records), self.assertRaises(ValueError):
                maintenance.vendor_release(self.key)

    def test_cursor_stable_version_is_independent_of_artifact_hash(self):
        key = 'brew-cask:cursor'
        self.write_app(key, '3.26.13', '3.26.13')
        payload = {'name': '3.26.13', 'url': 'https://downloads.cursor.com/production/' + 'a'*40 + '/darwin/arm64/Cursor-darwin-arm64.zip'}
        with patch.object(maintenance.platform, 'machine', return_value='arm64'), patch.object(maintenance, 'json_url', return_value=payload) as fetch:
            release = maintenance.vendor_release(key)
        self.assertIn('/cursor/0.0.0/stable', fetch.call_args.args[0])
        self.assertEqual(maintenance.assess_vendor_release(key, release)['state'], 'current')
        payload['url'] = 'https://example.invalid/file.zip'
        with patch.object(maintenance.platform, 'machine', return_value='arm64'), patch.object(maintenance, 'json_url', return_value=payload), self.assertRaises(ValueError):
            maintenance.vendor_release(key)

    def test_chatgpt_compares_same_version_builds_and_rejects_conflicts(self):
        key = 'brew-cask:chatgpt'
        release = dict(self.release, version='26.1013.40934', build='20770')
        for build, state in [('20769', 'update-available'), ('20770', 'current'), ('20771', 'newer')]:
            self.write_app(key, release['version'], build)
            self.assertEqual(maintenance.assess_vendor_release(key, release)['state'], state)
        self.write_app(key, '26.1014.40934', '20769')
        with self.assertRaisesRegex(ValueError, 'disagree'):
            maintenance.assess_vendor_release(key, release)

    def test_appcast_selects_compatible_stable_build(self):
        def item(version, build, extra=''):
            return (f'<item><s:shortVersionString>{version}</s:shortVersionString>'
                    f'<s:version>{build}</s:version>{extra}'
                    f'<enclosure url="https://persistent.oaistatic.com/codex-app-prod/ChatGPT-darwin-arm64-{version}.zip"/></item>')
        xml = ('<rss xmlns:s="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel>' +
               item('26.1013.40934', '20770', '<s:minimumSystemVersion>13.0</s:minimumSystemVersion>') +
               item('26.1014.40934', '20771', '<s:channel>beta</s:channel>') +
               item('26.1015.40934', '20772', '<s:minimumSystemVersion>99.0</s:minimumSystemVersion>') +
               item('26.1016.40934', '20773', '<s:hardwareRequirements>x86_64</s:hardwareRequirements>') +
               '</channel></rss>').encode()
        with patch.object(maintenance.platform, 'machine', return_value='arm64'), patch.object(maintenance, 'read_command', return_value='26.0'), patch.object(maintenance, 'urlopen', return_value=io.BytesIO(xml)):
            self.assertEqual(maintenance.vendor_release('brew-cask:chatgpt')['build'], '20770')
        with patch.object(maintenance, 'read_command', return_value='26.0'), patch.object(maintenance, 'urlopen', return_value=io.BytesIO(b'<rss><channel/></rss>')), self.assertRaises(ValueError):
            maintenance.vendor_release('brew-cask:chatgpt')

    def test_preview_retains_vendor_plan_and_suppresses_only_resolved_warning(self):
        fixture = MaintenanceOutputTests(); fixture.setUp()
        try:
            def command(args, log, **kwargs):
                if 'outdated' in args: log.write('{}\n')
                elif '--check' in args: log.write('{"files": [], "revisions": {}}\n')
                elif 'brew-cask:google-chrome' in args:
                    log.write('skipped: installed app is current, newer, or incomparable\n')
                else: log.write('already up to date\n')
            with patch.dict(maintenance.VENDOR_APPS, {self.key: ('Google Chrome.app', 'com.google.Chrome')}), patch.object(maintenance, 'vendor_release', return_value=self.release):
                self.assertEqual(fixture.invoke(['--check', '--json'], command=command, running=True), 0)
            report = json.loads(fixture.stdout.getvalue())
            result = report['results'][self.key]
            self.assertEqual(result['state'], 'update-available')
            self.assertEqual(len(report['planned_changes']), 1)
            self.assertNotIn('notes', result)
            self.assertNotIn('plan_unverified', result)
        finally:
            fixture.tearDown()


class OrbStackUpdateTests(unittest.TestCase):
    key = 'brew-cask:orbstack'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        apps = patch.object(maintenance, 'APPLICATIONS', root)
        apps.start()
        self.addCleanup(apps.stop)
        self.app = root / 'OrbStack.app'
        (self.app / 'Contents').mkdir(parents=True)
        self.info = self.app / 'Contents/Info.plist'
        self.info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'dev.kdrag0n.MacVirt',
                              'CFBundleShortVersionString': '2.2.5', 'CFBundleVersion': '21124'}))

    def evaluate(self, codes, *, running=False, preview=False):
        answers = [maintenance.subprocess.CompletedProcess([], n, 'vendor message', '') for n in codes]
        with patch.object(maintenance.subprocess, 'run', side_effect=answers) as cli, \
             patch.object(maintenance, 'is_running', return_value=running), \
             patch.object(maintenance, 'native_cask_update') as native:
            result = maintenance.cask_update(self.key, preview, io.StringIO(), None)
        for call in cli.call_args_list:
            self.assertEqual(call.args[0], [str(self.app / 'Contents/MacOS/bin/orbctl'), 'update', '--check'])
        return result, native.call_count

    def test_official_check_skips_replacement_and_does_not_execute_unknown_bundles(self):
        result, applied = self.evaluate([0])
        self.assertEqual(result['vendor_check']['installed'], '2.2.5 (build 21124)')
        self.assertEqual((result['state'], applied), ('current', 0))
        result, applied = self.evaluate([1])
        self.assertEqual((result['state'], applied), ('availability-unknown', 0))
        self.info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'other'}))
        result, applied = self.evaluate([])
        self.assertIn('identity', result['vendor_check']['error'])
        self.assertEqual(applied, 0)

    def test_preview_and_running_use_no_target_guess_and_idle_rechecks(self):
        for opts, expected in [({'running': True}, 'deferred-running-app'), ({'preview': True}, 'update-available')]:
            result, applied = self.evaluate([3], **opts)
            self.assertEqual((result['state'], applied), (expected, 0))
            self.assertIn('update available', maintenance.format_change(result['plans'][0], planned=True))
        for final, expected in [(0, 'current'), (3, 'update-available'), (7, 'availability-unknown')]:
            result, applied = self.evaluate([3, final])
            self.assertEqual((result['state'], applied), (expected, 1))


class RustAuditTests(unittest.TestCase):
    @staticmethod
    def advisory(repo, requirement, ident='GHSA-example', **extra):
        record = {'ghsa_id': ident, 'cve_id': None, 'summary': 'Example', 'severity': 'low',
                  'html_url': f'https://github.com/rust-lang/{repo}/security/advisories/{ident}',
                  'state': 'published', 'withdrawn_at': None, 'vulnerabilities': [
                      {'package': {'name': 'std' if repo == 'rust' else 'cargo'},
                       'vulnerable_version_range': requirement, 'patched_versions': None}]}
        return dict(record, **extra)

    def test_real_published_range_boundaries_and_cargo_numbering(self):
        component = {'version': '1.71.0', 'crate_version': '0.72.0'}
        for req, match, version in [('>= 1.60.0, < 1.72', True, '1.71.0'), ('<= 0.72.1', True, '0.72.0'),
                                    ('< 0.72.0', False, '0.72.0'), ('>= 1.87.0, < 1.89.0', False, '1.71.0')]:
            self.assertEqual(maintenance.affected_rust_version(req, component), (match, version))
        for req in ['unknown', '<= 1.72', '>= 0.70.0, < 1.75.0']:
            with self.assertRaises(ValueError):
                maintenance.affected_rust_version(req, component)

    def test_supplement_merges_findings_and_keeps_unrelated_coverage(self):
        rust = {'tool': 'rust', 'version': '1.88.0'}
        result = {'vulnerabilities': [AuditTests.finding], 'unchecked': [rust, {'tool': 'zig'}]}
        def source(url):
            if '/rust/security' in url:
                return [self.advisory('rust', '>= 1.87.0, < 1.89.0')]
            return [self.advisory('cargo', '< 0.97.0', 'GHSA-cargo'),
                    self.advisory('cargo', '< 0.99.0', 'GHSA-withdrawn', withdrawn_at='2025-01-01')]
        with patch.object(maintenance, 'rust_component', side_effect=lambda name: {'version': '1.88.0', 'commit': 'a'*40}), \
             patch.object(maintenance, 'cargo_crate_version', return_value='0.89.0'), \
             patch.object(maintenance, 'json_url', side_effect=source):
            maintenance.supplement_rust_audit(result, rust)
        self.assertEqual(result['state'], 'advisories')
        self.assertEqual([f['tool'] for f in result['vulnerabilities']], ['python', 'rust', 'cargo'])
        self.assertEqual(result['vulnerabilities'][-1]['version_scheme'], 'crate')
        self.assertEqual(result['unchecked'], [{'tool': 'zig'}])
        self.assertEqual(result['rust_checks']['cargo']['advisories_checked'], 1)

    def test_partial_source_failure_retains_other_findings(self):
        rust = {'tool': 'rust', 'version': '1.71.0'}
        result = {'vulnerabilities': [AuditTests.finding], 'unchecked': [rust]}
        with patch.object(maintenance, 'rust_component', side_effect=lambda _: {'version': '1.71.0', 'commit': 'a'*40}), \
             patch.object(maintenance, 'cargo_crate_version', side_effect=OSError('offline')), \
             patch.object(maintenance, 'json_url', return_value=[self.advisory('rust', '< 1.89.0')]):
            maintenance.supplement_rust_audit(result, rust)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(len(result['vulnerabilities']), 2)
        self.assertIn('offline', result['error'])
        self.assertEqual(result['unchecked'], [rust])

    def test_pagination_and_unsupported_metadata(self):
        first = [self.advisory('rust', '< 1.0.0', f'GHSA-{i}') for i in range(100)]
        findings, record = [], {'version': '1.100.0'}
        with patch.object(maintenance, 'json_url', side_effect=[first, [self.advisory('rust', '= 1.100.0')]]) as fetch:
            maintenance.rust_repository_audit('rust', record, findings)
        self.assertIn('page=2', fetch.call_args.args[0])
        self.assertEqual(record['advisories_checked'], 101)
        self.assertEqual(len(findings), 1)
        with patch.object(maintenance, 'json_url', return_value=[self.advisory('rust', 'pending review')]), self.assertRaises(ValueError):
            maintenance.rust_repository_audit('rust', record, [])


class AuditTests(unittest.TestCase):
    finding = {'tool': 'python', 'version': '3.15.2', 'id': 'CVE-example', 'summary': 'Cookie policy',
               'severity': None, 'fixed': []}
    unchecked = [{'tool': 'zig', 'version': '1.0'}]

    def audit(self, code, stdout, stderr=''):
        result = maintenance.subprocess.CompletedProcess([], code, stdout, stderr)
        log = io.StringIO()
        with patch.object(maintenance.subprocess, 'run', return_value=result) as cmd:
            report = maintenance.audit_tools(log, maintenance.Console())
        self.assertEqual(cmd.call_args.args[0], [str(maintenance.MISE), '-C', str(maintenance.HOME), 'audit', '--json'])
        self.assertIn(stdout, log.getvalue())
        return report

    def test_clean_and_positive_exits_preserve_checked_time_and_scope(self):
        for code, findings, state in [(0, [], 'clear'), (1, [self.finding], 'advisories')]:
            with self.subTest(code=code):
                result = self.audit(code, json.dumps({'vulnerabilities': findings, 'unchecked': self.unchecked}))
                self.assertEqual(result['state'], state)
                self.assertEqual(result['vulnerabilities'], findings)
                self.assertEqual(result['unchecked'], self.unchecked)
                self.assertIn('checked_at', result)

    def test_failures_and_inconsistent_reports_cannot_pass_as_clean(self):
        empty = json.dumps({'vulnerabilities': [], 'unchecked': []})
        cases = [(1, '', 'mise ERROR could not reach OSV'), (2, empty, 'service failure'), (1, empty, ''),
                 (0, '{}', ''), (0, 'not JSON', ''),
                 (1, json.dumps({'vulnerabilities': [{}], 'unchecked': []}), ''),
                 (0, json.dumps({'vulnerabilities': [], 'unchecked': ['rust']}), '')]
        for args in cases:
            with self.subTest(args=args), self.assertRaisesRegex(RuntimeError, 'Audit unavailable'):
                self.audit(*args)

    def test_summary_uses_one_row_per_decision_even_for_old_reports(self):
        pending = {'name': 'Google Chrome', 'before': '1', 'after': '2', 'kind': 'version',
                   'stage': 'brew-cask:google-chrome'}
        report = {'state': 'success', 'schema_version': 3, 'changes': [], 'planned_changes': [pending],
                  'results': {'brew-cask:google-chrome': {'state': 'deferred-running-app', 'plans': [pending],
                              'vendor_check': {'state': 'update-available', 'installed': '1', 'available': '2'}},
                              'brew-cask:claude': {'state': 'deferred-running-app',
                              'notes': [' installed app is running and updates itself']},
                              'brew-cask:orbstack': {'state': 'availability-unknown', 'notes': [
                                  'installed app is current, newer, or incomparable']},
                              'audit': {'state': 'advisories', 'vulnerabilities': [dict(self.finding, fixed=['3.15.3'])],
                                        'unchecked': [{'tool': 'rust', 'version': '1.0'}]}}}
        output = io.StringIO()
        with redirect_stdout(output):
            maintenance.Console().summary(report)
        text = output.getvalue()
        for phrase in ('Google Chrome', 'close the app', 'OrbStack', 'CVE-example', '3.15.3', 'Rust'):
            self.assertEqual(text.count(phrase), 1, text)
        self.assertNotIn('incomparable', text)
        self.assertNotIn('Claude Desktop', text)


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
        self.vendor_patch = patch.dict(maintenance.VENDOR_APPS, {}, clear=True)
        self.vendor_patch.start()
        self.audit = patch.object(maintenance, 'audit_tools', return_value={
            'state': 'clear', 'vulnerabilities': [], 'unchecked': [], 'checked_at': maintenance.now()})
        self.mock_audit = self.audit.start()
        self.deps = patch.object(maintenance.dependency_audit, 'scan', return_value={'state': 'clear', 'owners': {}, 'match_count': 0})
        self.mock_dependencies = self.deps.start()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def tearDown(self):
        self.deps.stop()
        self.audit.stop()
        self.vendor_patch.stop()
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
            if 'upgrade' in args and '--global' in args:
                raise RuntimeError('simulated update failure')
            if '--dry-run' in args:
                log.write('skipped: installed app is running and updates itself\n')
        self.assertEqual(self.invoke([], command=command, running=True), 1)
        output = self.stdout.getvalue()
        self.assertIn('Global tools: simulated update failure', output)
        self.assertNotIn('MarkText: Current', output)
        self.assertNotIn('close the app', output)
        self.assertNotIn('Google Chrome', output)
        self.assertIn('1 error', output)
        report = json.loads((self.state / 'last-run.json').read_text())
        self.assertEqual(report['errors'], ['tools'])
        self.assertIn('elapsed_seconds', report['results']['tools'])
        self.assertTrue(any('shell:completions' in call for call in calls))

    def test_audit_is_persisted_for_manual_scheduled_and_preview_runs(self):
        finding = AuditTests.finding
        self.mock_audit.return_value = {'state': 'advisories', 'vulnerabilities': [finding], 'unchecked': []}
        for args, filename in [([], 'last-manual'), (['--scheduled'], 'last-scheduled'), (['--check'], 'last-check')]:
            with self.subTest(args=args):
                self.stdout.seek(0)
                self.stdout.truncate()
                self.assertEqual(self.invoke(args + ['--json']), 0)
                report = json.loads(self.stdout.getvalue())
                self.assertLess(list(report['results']).index('tools'), list(report['results']).index('audit'))
                self.assertEqual(report, maintenance.load_report(self.state / (filename + '.json')))
                self.assertEqual(report['results']['audit']['vulnerabilities'], [finding])
                self.assertEqual(report['errors'], [])

    def test_partial_rust_audit_fails_run_without_losing_mise_findings(self):
        self.mock_audit.return_value = {'state': 'failed', 'error': 'Rust advisory coverage incomplete',
                                       'vulnerabilities': [AuditTests.finding], 'unchecked': [],
                                       'rust_checks': {'cargo': {'state': 'unverified'}}}
        self.assertEqual(self.invoke([]), 1)
        self.assertIn('CVE-example', self.stdout.getvalue())
        self.assertIn('Rust advisory coverage incomplete', self.stdout.getvalue())
        self.assertEqual(maintenance.load_report(self.state / 'last-run.json')['errors'], ['audit'])

    def test_dependency_audit_runs_for_manual_scheduled_and_preview_and_survives_in_status(self):
        for args in [[], ['--scheduled'], ['--check']]:
            self.stdout.seek(0)
            self.stdout.truncate()
            self.assertEqual(self.invoke(args + ['--json']), 0)
            self.assertEqual(json.loads(self.stdout.getvalue())['results']['dependency-audit']['match_count'], 0)
        self.assertEqual(self.mock_dependencies.call_count, 3)
        self.stdout.seek(0)
        self.stdout.truncate()
        with redirect_stdout(self.stdout):
            self.assertEqual(maintenance.main(['--dependencies', '--json']), 0)
        self.assertEqual(json.loads(self.stdout.getvalue())['state'], 'clear')

    def test_dependency_scanner_failure_is_distinct_from_the_tool_version_audit(self):
        self.mock_dependencies.side_effect = RuntimeError('OSV-Scanner offline')
        self.assertEqual(self.invoke(['--json']), 1)
        report = json.loads(self.stdout.getvalue())
        self.assertEqual(report['errors'], ['dependency-audit'])
        self.assertEqual(report['results']['audit']['state'], 'clear')
        self.assertIn('marktext', report['results'])

    def test_unavailable_audit_is_an_error_and_later_checks_continue(self):
        self.mock_audit.side_effect = RuntimeError('Audit unavailable. OSV offline')
        self.assertEqual(self.invoke([]), 1)
        report = maintenance.load_report(self.state / 'last-run.json')
        self.assertEqual(report['errors'], ['audit'])
        self.assertIn('marktext', report['results'])
        self.assertIn('OSV offline', self.stdout.getvalue())
        self.assertNotIn('0 advisories', self.stdout.getvalue())

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
        self.assertIn('Preview only. No updates applied.', output)
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
        self.assertIn('Maintenance complete  2.0s', self.stdout.getvalue())

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


class DependencyReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        state = patch.object(maintenance, 'STATE', self.state)
        state.start()
        self.addCleanup(state.stop)
        def owner(tool, findings=(), coverage='inventory found', note=None):
            row = {'tool': tool, 'version': '1.0', 'package_count': int(coverage != 'no inventory'),
                   'findings': list(findings), 'coverage': coverage, 'evidence': ['installed files']}
            if note:
                row['coverage_note'] = note
            return row
        def finding(name, score, fixes):
            return {'id': f'OSV-{name}', 'package': name, 'version': '1', 'summary': f'Details of {name}',
                    'severity': score, 'ecosystem': 'npm', 'fixed_versions': fixes,
                    'url': 'https://osv.dev/' + name, 'sources': [{'kind': 'published lockfile', 'path': 'Cargo.lock'}]}
        self.owners = {'python@1.0': owner('python', [finding('beta', '', []), finding('alpha', '8.9', ['2'])]),
                       'pnpm@1.0': owner('pnpm', [finding('gamma', '5.0', [])], 'partial'),
                       'uv@1.0': owner('uv'), 'bun@1.0': owner('bun', coverage='no inventory', note='No identifiable dependency inventory')}
        self.report = {'state': 'advisories', 'owners': self.owners, 'match_count': 3}
        self.saved = {'finished_at': '2026-11-24T21:00:00+00:00', 'results': {'dependency-audit': self.report}}

    def render(self, *args, columns=80):
        (self.state / 'last-run.json').write_text(json.dumps(self.saved))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.dict(os.environ, {'COLUMNS': str(columns)}):
            code = maintenance.main(['--dependencies', *args])
        return code, out.getvalue(), err.getvalue()

    def test_overview_ranks_matches_without_dumping_findings(self):
        code, text, err = self.render()
        self.assertEqual((code, err), (0, ''))
        self.assertIn('3 advisory matches across 2 tools', text)
        self.assertIn('3 of 4 tools · 1 match unscored', text)
        self.assertLess(text.index('High 8.9'), text.index('Medium 5.0'))
        self.assertRegex(text, r'High 8\.9\s+2\s+1\n')
        self.assertIn('No inventory: Bun', text)
        self.assertIn('Partial inventory: pnpm', text)
        self.assertIn('No matches in checked packages: uv', text)
        self.assertIn('mise run maintenance:dependencies --tool python', text)
        self.assertNotIn('OSV-alpha', text)
        self.assertNotIn('\x1b', text)
        _, narrow, _ = self.render(columns=37)
        self.assertIn('2 matches · 1 with fixes', narrow)
        self.assertLess(max(map(len, narrow.splitlines())), 37)

    def test_old_report_attributes_findings_without_changing_the_saved_json(self):
        findings = self.owners['python@1.0']['findings']
        source = {'kind': 'installed files', 'path': 'lib/site-packages/a_tool/_vendor/bom.cdx.json'}
        for finding in findings:
            finding['sources'] = [source]
        self.assertIn('Python → a_tool', self.render()[1])
        self.assertIn('Python → a_tool → alpha 1', self.render('--tool', 'python')[1])
        self.assertEqual(json.loads(self.render('--json')[1]), self.report)
        findings[1]['sources'].append({'kind': 'installed files', 'path': 'bin/standalone'})
        self.assertNotIn('Python → a_tool', self.render()[1])
        _, text, _ = self.render('--tool', 'python')
        self.assertIn('Python → alpha', text)
        self.assertIn('Python → a_tool → alpha', text)

    def test_scoped_detail_verbose_and_json(self):
        _, text, _ = self.render('--tool', 'PY')
        self.assertLess(text.index('OSV-alpha'), text.index('OSV-beta'))
        self.assertIn('Fix listed: 2', text)
        self.assertIn('No dependency fix listed', text)
        self.assertIn('lockfile only', text)
        self.assertNotIn('OSV-gamma', text)
        self.assertNotIn('https://', text)
        _, full, _ = self.render('--verbose')
        for value in ('OSV-alpha', 'OSV-beta', 'OSV-gamma', 'Cargo.lock', 'https://osv.dev/alpha'):
            self.assertIn(value, full)
        self.assertEqual(json.loads(self.render('--json')[1]), self.report)
        self.assertEqual(json.loads(self.render('--tool', 'python@1.0', '--json')[1]), self.owners['python@1.0'])
        for query, reason in [('p', 'Ambiguous tool'), ('missing', 'Unknown tool')]:
            code, out, err = self.render('--tool', query, '--json')
            self.assertEqual((code, out), (2, ''))
            self.assertIn(reason, err)

    def test_coverage_and_failure_do_not_read_as_all_clear(self):
        self.report.update(state='failed', error='Scanner timed out')
        _, text, _ = self.render()
        self.assertIn('Scan incomplete', text)
        self.assertIn('Scanner timed out', text)
        self.assertNotIn('No matches in checked packages', text)
        self.saved['results']['dependency-audit'] = {'state': 'interrupted', 'error': 'Interrupted'}
        self.assertIn('Scan incomplete', self.render()[1])
        self.saved['results']['dependency-audit'] = {'state': 'limited', 'owners': {'bun@1.0': self.owners['bun@1.0']}}
        self.assertIn('No package versions available to check', self.render()[1])
        self.saved['results']['dependency-audit'] = {'state': 'clear', 'owners': {'uv@1.0': self.owners['uv@1.0']}}
        self.assertIn('No advisory matches in the packages checked', self.render()[1])
        self.saved['results'] = {}
        self.assertIn('No recorded dependency scan', self.render()[1])

    def test_compare_matches_ignores_owner_upgrades_and_handles_lost_coverage(self):
        old = json.loads(json.dumps(self.saved))
        self.owners['python@1.0']['version'] = '2.0'
        compare = lambda: maintenance.compare_dependency_matches(old, self.saved)
        self.assertEqual(compare(), {'added': 0, 'no_longer_reported': 0})
        self.owners['python@1.0']['findings'][0]['id'] = 'OSV-new'
        self.saved['dependency_match_changes'] = compare()
        self.assertIn('1 newly reported · 1 no longer reported', self.render()[1])
        self.owners['python@1.0']['coverage'] = 'partial'
        self.assertIsNone(compare())
        self.assertIsNone(maintenance.compare_dependency_matches({}, self.saved))


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
        self.assertIn('No changes this run.', output.getvalue())
        self.assertNotIn('completions', output.getvalue())
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
        self.assertIn('Check coverage', text)
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


    def test_setup_compares_readable_sections_when_others_fail_before_or_after(self):
        fixture = MaintenanceOutputTests('test_json_stdout_is_exactly_the_saved_report')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        def values(**versions):
            return {f'{section}/pkg': maintenance.entity(section, version, stage=section)
                    for section, version in versions.items()}
        old = values(tools='1', brew='1', marktext='1')
        new = values(tools='2', rustup='1', marktext='1')
        reads = [(old, {}), (old, {'rustup': 'temporarily unavailable'}),
                 (new, {'brew': 'permission denied'}), (new, {})]
        with patch.object(maintenance, 'inventory', side_effect=reads):
            self.assertEqual(fixture.invoke(['--json']), 0)
        report = json.loads(fixture.stdout.getvalue())
        setup = report['results']['setup']
        self.assertEqual([(c['id'], c['action']) for c in setup['changes']], [('tools/pkg', 'updated')])
        self.assertEqual(setup['observation_errors'],
                         ['before: Rustup: temporarily unavailable', 'after: Native packages: permission denied'])
        self.assertEqual(report['changes'], [dict(setup['changes'][0], stage='setup')])
        self.assertEqual(setup['before'], old)
        self.assertEqual(setup['after'], new)
        fixture.stdout.seek(0)
        fixture.stdout.truncate()
        with redirect_stdout(fixture.stdout):
            maintenance.main(['--status'])
        self.assertIn('change verification incomplete', fixture.stdout.getvalue())

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

    def test_only_the_post_upgrade_actions_are_deferred_during_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake = home / '.local/bin/mise'
            fake.parent.mkdir(parents=True)
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
            fake.chmod(0o755)
            hooks = tomllib.loads((Path(__file__).resolve().parents[1] / 'config.toml').read_text())['bootstrap']['hooks']
            env = {'HOME': str(home), 'PATH': os.environ['PATH']}
            def execute(cmd, env):
                return maintenance.subprocess.check_output(['sh', '-ec', cmd], env=env, text=True)
            fresh = execute(hooks['final'], env).splitlines()
            self.assertEqual(len(fresh), 2)
            self.assertIn('--marktext', fresh[0])
            self.assertIn('shell:completions', fresh[1])
            def command(args, log, **kwargs):
                if '--skip' in args:
                    env['HOST_MAINTENANCE_POST_UPGRADE'] = kwargs['env']['HOST_MAINTENANCE_POST_UPGRADE']
                    self.assertEqual(execute(hooks['final'], env), '')
                    self.assertIn('github:credentials', execute(hooks['post-tools'], env))
            self.assertEqual(self.fixture.invoke([], command=command), 0)
            self.assertEqual(env['HOST_MAINTENANCE_POST_UPGRADE'], '1')

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
        main_phase = next(c for c in calls if '--skip' in c)
        self.assertEqual(set(main_phase[main_phase.index('--skip') + 1].split(',')),
                         {'packages', 'dotfiles', 'macos-launchd-agents'})
        self.assertEqual(sum('--only' in c and 'dotfiles' in c for c in calls), 1)
        self.assertEqual(sum('shell:completions' in c for c in calls), 1)
        self.assertFalse(any('--deploy' in c for c in calls))

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
        self.assertIn('Changed before this run', self.fixture.stdout.getvalue())

    def test_removed_declaration_is_not_reported_as_app_uninstall(self):
        previous = {'inventory_after': {'brew-cask:example/app': maintenance.entity('Example', '1', stage='brew-cask:example')}}
        self.assertEqual(maintenance.background_changes(previous, {}, {}), [])

    def test_running_current_pending_and_unknown_are_distinct(self):
        self.assertEqual(maintenance.cask_result('already up to date', True)['state'], 'current')
        self.assertEqual(maintenance.cask_result('install cask test/2.0', True)['state'], 'deferred-running-app')
        self.assertEqual(maintenance.cask_result('skipped: installed app is running and updates itself', True)['state'], 'self-updating')
        self.assertEqual(maintenance.cask_result('skipped: installed app is current, newer, or incomparable', True)['state'], 'availability-unknown')
        self.assertEqual(maintenance.cask_result('unrecognized output', True)['state'], 'availability-unknown')

    def test_running_app_preview_never_applies_and_tracks_repeated_deferrals(self):
        def command(args, log, **kwargs):
            if 'google-chrome' in ' '.join(map(str, args)):
                self.assertIn('--dry-run', args)
                log.write('install cask google-chrome/156.0.1\n')
        for i in range(2):
            self.fixture.stdout.seek(0);self.fixture.stdout.truncate()
            self.assertEqual(self.fixture.invoke(['--json'], command=command, running=True), 0)
        result = maintenance.load_report(maintenance.STATE / 'last-run.json')['results']['brew-cask:google-chrome']
        self.assertEqual(result['consecutive_deferrals'], 2)
        self.assertEqual(result['plans'][0]['after'], '156.0.1')
        self.assertIn('deferred_since', result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
