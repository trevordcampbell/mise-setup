import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from scripts import desktop_releases as releases, maintenance


class DesktopReleaseTests(unittest.TestCase):
    def resolve(self, name, payload):
        data = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        with patch.object(releases.platform, 'machine', return_value='arm64'), patch.object(releases, 'fetch', return_value=data):
            return maintenance.vendor_release('brew-cask:' + name)

    def test_claude_uses_current_release_and_requires_matching_mac_artifact(self):
        def row(v):
            return {'version': v, 'updateTo': {'version': v, 'url': f'https://downloads.claude.ai/releases/darwin/universal/{v}/Claude-abcdef.zip'}}
        feed = {'currentRelease': '7.1.0', 'releases': [row('8.0.0'), row('7.1.0')]}
        self.assertEqual(self.resolve('claude', feed)['version'], '7.1.0')
        feed['releases'][1]['updateTo']['url'] = feed['releases'][0]['updateTo']['url']
        with self.assertRaises(ValueError):
            self.resolve('claude', feed)

    def test_github_release_requires_stable_matching_installer(self):
        for name, (repo, template) in releases.GITHUB_RELEASES.items():
            v = '1.2.3'
            asset = template.format(v=v, arch='arm64')
            data = {'tag_name': 'v' + v, 'draft': False, 'prerelease': False,
                    'assets': [{'name': asset, 'browser_download_url': f'https://github.com/{repo}/releases/download/v{v}/{asset}'}]}
            with patch.object(releases, 'github_json', return_value=data):
                self.assertEqual(releases.github_api_release(name, 'arm64')['version'], v)
            for override in ({'draft': True}, {'prerelease': True}, {'assets': []}):
                with self.subTest(name=name, override=override), patch.object(releases, 'github_json', return_value=dict(data, **override)), self.assertRaises(ValueError):
                    releases.github_api_release(name, 'arm64')

    def test_publisher_routes_do_not_need_github_api_quota(self):
        for name in ('t3-code', 'bruno'):
            _, template = releases.GITHUB_RELEASES[name]
            asset = template.format(v='1.2.3', arch='arm64')
            data = f"version: '1.2.3'\nfiles:\n  - url: {asset}\n".encode()
            with patch.object(releases, 'github_api_release', side_effect=AssertionError('REST API used')):
                self.assertEqual(self.resolve(name, data)['version'], '1.2.3')
                with self.assertRaises(ValueError):
                    self.resolve(name, data.replace(b'arm64', b'x64'))
        targets = ['https://github.com/crystalidea/macs-fan-control/releases/download/v1.5.23/macsfancontrol.zip',
                   'https://release-assets.githubusercontent.com/signed-asset']
        with patch.object(releases, 'first_redirect', side_effect=targets), patch.object(releases, 'published_zip', return_value={}):
            self.assertEqual(releases.github_release('macs-fan-control', 'arm64')['version'], '1.5.23')

    def test_github_zip_is_pinned_to_exact_published_tag_and_filename(self):
        url = 'https://github.com/desktop/desktop/releases/download/release-3.1/GitHub.Desktop-arm64.zip'
        asset = {'name': 'GitHub.Desktop-arm64.zip', 'browser_download_url': url, 'digest': 'sha256:' + '9' * 64}
        data = {'tag_name': 'release-3.1', 'draft': False, 'prerelease': False, 'assets': [asset]}
        def match(response):
            with patch.object(releases, 'github_json', return_value=response):
                return releases.published_zip('desktop/desktop', 'release-3.1', asset['name'])
        self.assertEqual(match(data)['artifact']['url'], url)
        for changed in [{'tag_name': 'release-3.0'}, {'prerelease': True}, {'assets': [asset, asset]},
                        {'assets': [dict(asset, browser_download_url=url + '.sig')]}]:
            self.assertEqual(match(dict(data, **changed)), {})

    def test_sparkle_signature_belongs_to_one_full_unrestricted_archive(self):
        item = ET.Element('item')
        attrs = {'url': 'https://example.org/file.zip', 'length': '32', releases.S + 'edSignature': 'A' * 86 + '=='}
        enc = ET.SubElement(item, 'enclosure', attrs)
        self.assertEqual(releases.sparkle_asset(item, enc)['artifact']['size'], 32)
        for change in [{releases.S + 'deltaFrom': '123'}, {releases.S + 'installationType': 'guided-package'},
                       {'length': '-1'}, {releases.S + 'edSignature': ''}]:
            self.assertEqual(releases.sparkle_asset(item, ET.Element('enclosure', dict(attrs, **change))), {})
        ET.SubElement(item, releases.S + 'phasedRolloutInterval').text = '86400'
        self.assertEqual(releases.sparkle_asset(item, enc), {})

    def test_opera_sidecar_digest_is_for_the_selected_installer(self):
        source = 'https://ftp.opera.com/pub/opera/desktop/'
        name = 'Opera_1.2.3.4_Setup.dmg'
        mac = source + '1.2.3.4/mac/'
        files = {source: b'href="1.2.3.4/"', mac: f'href="{name}" href="{name}.sha256sum"'.encode()}
        for suffix, valid in [('', True), ('  ' + name, True), ('  another.dmg', False)]:
            files[mac + name + '.sha256sum'] = ('c' * 64 + suffix).encode()
            with patch.object(releases, 'fetch', side_effect=files.__getitem__):
                self.assertEqual('artifact' in releases.opera_release(), valid)

    def test_discord_postman_and_ledger_stable_macos_releases(self):
        discord = {'full': {'host_version': [0, 0, 424],
                            'url': 'https://stable.dl2.discordapp.net/distro/app/stable/osx/universal/0.0.424/full.distro'}}
        target = 'https://stable.dl2.discordapp.net/distro/app/stable/osx/universal/0.0.425/Discord.dmg'
        with patch.object(releases, 'first_redirect', return_value=target):
            self.assertEqual(self.resolve('discord', {})['version'], '0.0.425')
        with patch.object(releases, 'first_redirect', side_effect=OSError('unavailable')):
            self.assertEqual(self.resolve('discord', discord)['version'], '0.0.424')
            discord['full']['url'] += '?signed=example'
            self.assertEqual(self.resolve('discord', discord)['version'], '0.0.424')
            discord['full']['host_version'] = [9, 9, 9]
            with self.assertRaises(ValueError):
                self.resolve('discord', discord)
        self.assertEqual(self.resolve('postman', {'version': '12.38.6'})['version'], '12.38.6')
        with self.assertRaises(ValueError):
            self.resolve('postman', {'version': '13.0.0-beta'})
        body = b'version: 4.30.0\nfiles:\n  - url: ledger-live-desktop-4.30.0-mac.zip\n'
        self.assertEqual(self.resolve('ledger-wallet', body)['version'], '4.30.0')
        with self.assertRaises(ValueError):
            self.resolve('ledger-wallet', body + b'stagingPercentage: 10\n')

    def test_editor_checks_choose_the_app_version_and_validate_artifact(self):
        stable = 'https://windsurf-stable.codeiumdata.com/darwin-arm64-dmg/stable/abcdef-123/Devin-darwin-arm64-3.15.14.dmg'
        devin = {'windsurfVersion': '3.15.14', 'productVersion': '1.141.2', 'url': stable}
        vscode = {'productVersion': '1.148.0', 'url': 'https://vscode.download.prss.microsoft.com/dbazure/download/stable/abcdef/VSCode-darwin-arm64.zip'}
        self.assertEqual(self.resolve('devin-desktop', devin)['version'], '3.15.14')
        self.assertEqual(self.resolve('visual-studio-code', vscode)['version'], '1.148.0')
        for k, row in [('devin-desktop', devin), ('visual-studio-code', vscode)]:
            with self.assertRaises(ValueError):
                self.resolve(k, dict(row, url=row['url'].replace('arm64', 'x64')))

    def test_redirects_verify_mac_architecture_and_release(self):
        for name, url, version in [
                ('github', 'https://desktop.githubusercontent.com/releases/3.7.1-abcdef/GitHubDesktop-arm64.zip', '3.7.1'),
                ('slack', 'https://downloads.slack-edge.com/desktop-releases/mac/arm64/4.53.292/Slack-4.53.292-macOS.dmg', '4.53.292')]:
            with patch.object(releases, 'urlopen') as fetch, patch.object(releases, 'published_zip', return_value={}):
                response = fetch.return_value.__enter__.return_value
                response.geturl.return_value = url
                self.assertEqual(releases.redirected_release(name, 'arm64')['version'], version)
                self.assertEqual(fetch.call_args.args[0].get_method(), 'HEAD')
                response.geturl.return_value = url.replace('arm64', 'x64')
                with self.assertRaises(ValueError):
                    releases.redirected_release(name, 'arm64')

    def test_sparkle_selects_latest_compatible_stable_build(self):
        for name, (_, hosts) in releases.SPARKLE_FEEDS.items():
            feed = ET.Element('channel')
            for build, constraints in [(9, {}), (10, {}), (11, {'minimumSystemVersion': '27'}),
                                       (12, {'maximumSystemVersion': '12'}), (13, {'channel': 'beta'}),
                                       (14, {'hardwareRequirements': 'x86_64'})]:
                item = ET.SubElement(feed, 'item')
                for tag, text in constraints.items():
                    ET.SubElement(item, releases.S + tag).text = text
                ET.SubElement(item, 'enclosure', {'url': f'https://{sorted(hosts)[0]}/app.zip',
                              releases.S + 'shortVersionString': '1.1', releases.S + 'version': str(build)})
            data = b'<rss>' + ET.tostring(feed) + b'</rss>'
            with patch.object(releases.platform, 'mac_ver', return_value=('26.1', '', '')):
                self.assertEqual(self.resolve(name, data)['build'], '10')

    def test_opera_requires_mac_installer_and_compares_full_bundle_build(self):
        root = 'https://ftp.opera.com/pub/opera/desktop/'
        files = {root: b'<a href="142.0.0.1/">new</a><a href="141.0.6089.33/">stable</a>',
                 root + '142.0.0.1/mac/': b'<a href="README">README</a>',
                 root + '141.0.6089.33/mac/': b'<a href="Opera_141.0.6089.33_Setup.dmg">dmg</a>'}
        with patch.object(releases, 'fetch', side_effect=files.__getitem__):
            latest = releases.opera_release()
        with tempfile.TemporaryDirectory() as directory, patch.object(maintenance, 'APPLICATIONS', Path(directory)):
            path = Path(directory) / 'Opera.app/Contents/Info.plist'
            path.parent.mkdir(parents=True)
            path.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.operasoftware.Opera',
                              'CFBundleShortVersionString': '141.0', 'CFBundleVersion': '141.0.6089.32'}))
            self.assertEqual(maintenance.assess_vendor_release('brew-cask:opera', latest)['state'], 'update-available')

    def test_spotify_only_reinspects_changed_installer_and_never_reuses_stale_version_on_failure(self):
        with tempfile.TemporaryDirectory() as d, patch.object(releases, 'urlopen') as head, patch.object(releases, 'spotify_version_from_image', return_value='1.3.11.2657') as inspect:
            path = Path(d)
            response = head.return_value.__enter__.return_value
            response.headers = {'ETag': '"one"'}
            first = releases.spotify_release('arm64', path, maintenance.atomic_write)
            self.assertEqual(releases.spotify_release('arm64', path, maintenance.atomic_write), first)
            inspect.assert_called_once()
            response.headers = {'ETag': '"two"'}
            inspect.side_effect = ValueError('unreadable installer')
            with self.assertRaisesRegex(ValueError, 'unreadable'):
                releases.spotify_release('arm64', path, maintenance.atomic_write)
            self.assertEqual(json.loads((path / 'spotify-arm64-release.json').read_text())['etag'], '"one"')
