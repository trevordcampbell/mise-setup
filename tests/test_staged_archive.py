import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from scripts import desktop_releases as feeds, maintenance, staged_archive as archive, vendor_install


class ReleaseTests(unittest.TestCase):
    def test_discord_pairs_only_the_same_stable_version_with_the_full_digest(self):
        url = 'https://stable.dl2.discordapp.net/distro/app/stable/osx/universal/0.0.9/full.distro'
        full = {'host_version': [0, 0, 9], 'package_sha256': '1' * 64, 'url': url, 'package_size': 1234}
        with patch.object(feeds, 'first_redirect', return_value=url.replace('full.distro', 'Discord.dmg')), \
             patch.object(feeds, 'fetch', return_value=json.dumps({'full': full}).encode()):
            artifact = feeds.latest('discord', None, None)['artifact']
            self.assertEqual((artifact['url'], artifact['digest'], artifact['size']), (url, '1' * 64, 1234))
        with patch.object(feeds, 'first_redirect', return_value=url.replace('0.0.9/full.distro', '0.0.10/Discord.dmg')), \
             patch.object(feeds, 'fetch', return_value=json.dumps({'full': full}).encode()):
            self.assertNotIn('artifact', feeds.latest('discord', None, None))

    def test_blender_selects_a_final_mac_release_and_requires_its_exact_hash(self):
        root = 'https://mirror.blender.org/release/'
        name = 'blender-5.3.1-macos-arm64.dmg'
        entries = {root: b'href="./Blender5.4/" href="./Blender5.3/"',
                   root + 'Blender5.4/': b'href="./blender-5.4.0-beta-macos-arm64.dmg"',
                   root + 'Blender5.3/': f'href="./{name}"'.encode()}
        checksum = root + 'Blender5.3/blender-5.3.1.sha256'
        entries[checksum] = ('a' * 64 + '  ' + name).encode()
        with patch.object(feeds, 'fetch', side_effect=entries.__getitem__):
            self.assertEqual(feeds.blender_release('arm64')['artifact']['url'], root + 'Blender5.3/' + name)
            entries[checksum] = entries[checksum].replace(b'arm64', b'x64')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                feeds.blender_release('arm64')

    def test_publisher_tap_uses_the_same_native_receipt_and_display_name(self):
        name = 'brew-cask:steipete/tap/codexbar'
        self.assertEqual(maintenance.cask_token(name), 'codexbar')
        self.assertEqual(maintenance.label(name), 'CodexBar')
        self.assertEqual(maintenance.VENDOR_APPS[name], feeds.APPS['codexbar'])

    def test_switching_tap_is_not_reported_as_an_app_install(self):
        old, new = 'brew-cask:codexbar', 'brew-cask:steipete/tap/codexbar'
        suffix = '//Applications/CodexBar.app'
        row = {'name': 'CodexBar', 'kind': 'version', 'value': '1'}
        previous = {'inventory_after': {old + suffix: dict(row, stage=old)}}
        before = {new + suffix: dict(row, stage=new)}
        self.assertEqual(maintenance.background_changes(previous, before, {}), [])
        before[new + suffix]['value'] = '2'
        self.assertEqual(maintenance.background_changes(previous, before, {})[0]['action'], 'updated')
        self.assertEqual(maintenance.background_changes(previous, before, {new: 'unreadable'}), [])

    def test_daisydisk_legacy_signature_does_not_enable_other_feeds(self):
        from xml.etree.ElementTree import Element
        item = Element('item')
        enc = Element('enclosure', {'url': 'https://daisydiskapp.com//download/DaisyDisk.zip',
                                    'length': '1234', feeds.S + 'dsaSignature': 'A' * 64})
        self.assertEqual(feeds.sparkle_asset(item, enc)['artifact']['algorithm'], 'isolated-app')
        enc.set('url', 'https://example.org/download/DaisyDisk.zip')
        self.assertEqual(feeds.sparkle_asset(item, enc), {})


class PrivateStagingTests(unittest.TestCase):
    def test_format_requires_the_publishers_expected_artifact(self):
        app = Path('/Applications/Postman.app')
        self.assertEqual(vendor_install.isolated_format(app, 'https://dl.pstmn.io/download/version/1.2.3/osx_arm64'), '.zip')
        with self.assertRaises(ValueError):
            vendor_install.isolated_format(app, 'https://example.org/download/version/1.2.3/osx_arm64')

    def test_zip_is_bounded_before_sandbox_and_extract_failure_never_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'a.zip'
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('Example.app/test', b'abc')
            run = Mock(side_effect=OSError('sandbox unavailable'))
            with patch.object(archive, 'LIMIT', 2), self.assertRaisesRegex(ValueError, 'limits'):
                archive.app_zip(path, root, 'Example.app', run)
            run.assert_not_called()
            with self.assertRaisesRegex(OSError, 'sandbox'):
                archive.app_zip(path, root, 'Example.app', run)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][0], '/usr/bin/sandbox-exec')

    def test_external_links_rejected_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / 'Example.app'
            app.mkdir()
            (root / 'sibling').touch()
            (app / 'link').symlink_to('../sibling')
            with self.assertRaisesRegex(ValueError, 'external'):
                archive.check_tree(app)

    def test_spotify_uses_only_the_final_unique_etag(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'headers'
            p.write_bytes(b'HTTP/1.1 302 Found\r\nETag: "old"\r\n\r\nHTTP/2 200\r\nETag: "new"\r\n\r\n')
            self.assertEqual(archive.response_etag(p), '"new"')
            p.write_bytes(b'HTTP/2 200\r\nETag: "new"\r\nETag: "other"\r\n\r\n')
            self.assertIsNone(archive.response_etag(p))
