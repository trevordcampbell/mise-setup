import base64
import hashlib
import plistlib
import shutil
import subprocess
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
    def stage(artifact, candidate, run, installed):
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

    def test_signed_image_must_pass_installed_team_requirement_before_mounting(self):
        run = Mock()
        def command(args, **kwargs):
            if args[0] == '/usr/bin/codesign':
                raise RuntimeError('wrong publisher')
        run.side_effect = command
        with patch.object(vendor, 'publisher_requirement', return_value='=same team'), self.assertRaisesRegex(RuntimeError, 'wrong publisher'):
            vendor.stage(feeds.signed_image('https://example.org/a.dmg')['artifact'], self.root / 'stage', run, self.app)
        self.assertFalse(any(call.args[0][0] == '/usr/bin/hdiutil' for call in run.call_args_list))
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            vendor.stage(feeds.signed_image('https://example.org/a.zip')['artifact'], self.root / 'stage', run, self.app)

    def test_chrome_finder_metadata_is_cleared_only_on_a_private_copy(self):
        chrome = self.root / 'Google Chrome.app'
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[-1] == chrome and '--strict' in args:
                raise RuntimeError('unexpected Finder metadata')
        result = vendor.publisher_requirement(chrome, run)
        self.assertIn('EQHXZ8M8AV', result)
        clear = next(c for c in calls if c[0] == '/usr/bin/xattr')
        self.assertEqual(clear[1:3], ['-rds', 'com.apple.FinderInfo'])
        self.assertNotEqual(clear[-1], chrome)
        self.assertIn('--strict', calls[-1])
        self.assertEqual(calls[-1][-1], clear[-1])
        def bad_copy(args, **kwargs):
            if '--strict' in args:
                raise RuntimeError('signature rejected')
        with self.assertRaisesRegex(RuntimeError, 'signature rejected'):
            vendor.publisher_requirement(chrome, bad_copy)

    @unittest.skipUnless(shutil.which('node'), 'Node verifies Sparkle archives')
    def test_sparkle_accepts_only_signature_from_installed_app(self):
        # RFC 8032 test 1, the signature of the empty message.
        key = bytes.fromhex('d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')
        signature = bytes.fromhex('e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155'
                                  '5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b')
        info = self.app / 'Contents/Info.plist'
        info.parent.mkdir()
        info.write_bytes(plistlib.dumps({'SUPublicEDKey': base64.b64encode(key).decode()}))
        path = self.root / 'archive'
        path.touch()
        artifact = {'algorithm': 'sparkle-ed25519', 'signature': base64.b64encode(signature).decode(), 'size': 0}
        def run(args, **kwargs):
            subprocess.run(args, check=True, capture_output=True, **kwargs)
        with patch.object(vendor, 'publisher_requirement'):
            vendor.verify_archive(path, artifact, self.app, run)
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'length'):
                vendor.verify_archive(path, artifact, self.app, run)
            with self.assertRaises(subprocess.CalledProcessError):
                vendor.verify_archive(path, dict(artifact, size=7), self.app, run)

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
