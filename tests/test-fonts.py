import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('fonts', Path(__file__).resolve().parents[1] / 'scripts/google-fonts.py')
fonts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fonts)


class FontSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'sources'
        self.config = patch.multiple(fonts, ROOT=self.root, FAMILIES={'test': 'Test'})
        self.config.start()
        self.data = b'\x00\x01\x00\x00verified-font-data'
        self.entries = [
            {'name': name, 'type': 'file', 'sha': fonts.git_blob_sha(self.data)}
            for name in ['Test[wght].ttf', 'Test-Italic[wght].ttf', 'OFL.txt']
        ]
        self.responses = [[{'sha': 'a' * 40}], self.entries]

    def tearDown(self):
        self.config.stop()
        self.temp.cleanup()

    def test_preview_does_not_download_or_write(self):
        with patch.object(fonts, 'api', side_effect=self.responses), patch.object(fonts, 'fetch') as fetch:
            result = fonts.synchronize(check=True)
            self.assertEqual(result['state'], 'update-available')
            fetch.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_bad_blob_leaves_all_sources_untouched(self):
        with patch.object(fonts, 'api', side_effect=self.responses), patch.object(fonts, 'fetch', side_effect=[self.data, b'bad-data']):
            with self.assertRaises(RuntimeError):
                fonts.synchronize()
        self.assertFalse(self.root.exists())

    def test_valid_files_are_staged_and_next_run_downloads_nothing(self):
        with patch.object(fonts, 'api', side_effect=self.responses), patch.object(fonts, 'fetch', return_value=self.data):
            self.assertEqual(fonts.synchronize()['state'], 'updated')
        self.assertEqual((self.root / 'fonts/Test[wght].ttf').read_bytes(), self.data)
        with patch.object(fonts, 'api', side_effect=self.responses), patch.object(fonts, 'fetch') as fetch:
            self.assertEqual(fonts.synchronize()['state'], 'current')
            fetch.assert_not_called()

    def test_unexpected_paths_and_missing_styles_are_rejected(self):
        for name in ['../Test.ttf', 'Other[wght].ttf']:
            entries = [*self.entries, {'name': name, 'type': 'file', 'sha': 'a' * 40}]
            with self.assertRaises(RuntimeError):
                fonts.selected_files(entries, 'Test')
        with self.assertRaises(RuntimeError):
            fonts.selected_files([self.entries[0], self.entries[2]], 'Test')


if __name__ == '__main__':
    unittest.main(verbosity=2)
