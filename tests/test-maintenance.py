import copy
from datetime import datetime, timezone, timedelta
import importlib.util
import io
import json
from pathlib import Path
import tempfile
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
