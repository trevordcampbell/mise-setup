import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import desktop_releases as feeds, vendor_install as vendor


class VendorInstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.app = self.root / 'Editor.app'
        self.app.mkdir()
        (self.app / 'version').write_text('1')
        self.receipts = self.root / 'cask'
        self.receipt = self.receipts / '1/.mise-cask.toml'
        self.receipt.parent.mkdir(parents=True)
        self.receipt.write_text(f'auto_updates = true\napps = ["{self.app}"]\nmetadata_only_apps = ["{self.app}"]\n')
        self.artifact = {'url': 'https://example.org/a.dmg', 'algorithm': 'sha256', 'digest': 'a' * 64}

    @staticmethod
    def assess(app):
        version = (app / 'version').read_text()
        return {'installed': version, 'state': {'1': 'update-available', '2': 'current', '3': 'newer'}[version]}

    @staticmethod
    def stage(artifact, candidate, run):
        candidate.mkdir()
        (candidate / 'version').write_text('2')

    @staticmethod
    def exchange(a, b):
        temp = a.with_name('swap')
        a.rename(temp)
        b.rename(a)
        temp.rename(b)

    def install(self, **overrides):
        with patch.object(vendor, 'stage', side_effect=overrides.get('stage', self.stage)), \
             patch.object(vendor, 'verify_candidate', side_effect=overrides.get('verify')), \
             patch.object(vendor, 'exchange', side_effect=self.exchange):
            return vendor.install(self.app, self.receipts, self.artifact, overrides.get('assess', self.assess),
                                  overrides.get('running', lambda app: False), Mock())

    def test_advance_preserves_ownership_and_removes_staging(self):
        original = self.receipt.read_bytes()
        result = self.install()
        self.assertEqual(result['vendor_upgrade'], dict(self.artifact, before='1', after='2'))
        self.assertEqual(self.assess(self.app)['state'], 'current')
        self.assertEqual(self.receipt.read_bytes(), original)
        self.assertFalse(list(self.root.glob('.mise-vendor-*')))

    def test_running_and_racing_updater(self):
        self.assertEqual(self.install(running=lambda app: True)['state'], 'deferred-running-app')
        self.assertEqual(self.assess(self.app)['installed'], '1')
        def other_updater(*args):
            (self.app / 'version').write_text('3')
        self.assertEqual(self.install(verify=other_updater)['state'], 'newer')
        self.assertEqual(self.assess(self.app)['installed'], '3')

    def test_damaged_signature_wrong_version_or_missing_ownership_prevent_replacement(self):
        with self.assertRaisesRegex(ValueError, 'signature'):
            self.install(verify=ValueError('signature rejected'))
        def wrong(*args):
            self.stage(*args)
            (args[1] / 'version').write_text('3')
        with self.assertRaisesRegex(ValueError, 'advertised'):
            self.install(stage=wrong)
        self.receipt.write_text(self.receipt.read_text().replace('true', 'false'))
        self.assertEqual(self.install(stage=AssertionError('download attempted'))['state'], 'update-available')
        self.assertEqual(self.assess(self.app)['installed'], '1')

    def test_post_install_failure_restores_original_and_propagates_error(self):
        def assess(app):
            result = self.assess(app)
            if app == self.app and result['installed'] == '2':
                raise RuntimeError('failed postcheck')
            return result
        with self.assertRaisesRegex(RuntimeError, 'failed postcheck'):
            self.install(assess=assess)
        self.assertEqual(self.assess(self.app)['installed'], '1')
        self.assertFalse(list(self.root.glob('.mise-vendor-*')))

    def test_digest_must_match_download_and_specific_asset(self):
        blob = self.root / 'asset'
        blob.write_bytes(b'archive')
        digest = hashlib.sha512(b'archive').digest()
        vendor.verify_artifact(blob, 'sha512', digest.hex())
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            vendor.verify_artifact(blob, 'sha512', '0' * 128)
        text = f'  - url: one.dmg\n    sha512: {base64.b64encode(digest).decode()}\n  - url: two.dmg\n'
        self.assertEqual(feeds.electron_asset(text, 'one.dmg', 'one')['artifact']['digest'], digest.hex())
        self.assertEqual(feeds.electron_asset(text, 'two.dmg', 'two'), {})
