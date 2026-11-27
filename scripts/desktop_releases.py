"""Read stable macOS release metadata for self-updating desktop applications."""
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import xml.etree.ElementTree as ET


if __package__:
    from .github_public import github_json
else:
    from github_public import github_json


APPS = {
    'bruno': ('Bruno.app', 'com.usebruno.app'),
    'discord': ('Discord.app', 'com.hnc.Discord'),
    'github': ('GitHub Desktop.app', 'com.github.GitHubClient'),
    'iterm2': ('iTerm.app', 'com.googlecode.iterm2'),
    'macs-fan-control': ('Macs Fan Control.app', 'com.crystalidea.macsfancontrol'),
    'postman': ('Postman.app', 'com.postmanlabs.mac'),
    'slack': ('Slack.app', 'com.tinyspeck.slackmacgap'),
    'visual-studio-code': ('Visual Studio Code.app', 'com.microsoft.VSCode'),
    'devin-desktop': ('Devin.app', 'com.exafunction.windsurf'),
    'daisydisk': ('DaisyDisk.app', 'com.daisydiskapp.DaisyDiskStandAlone'),
    'devutils': ('DevUtils.app', 'tonyapp.devutils'),
    'opera': ('Opera.app', 'com.operasoftware.Opera'),
    'spotify': ('Spotify.app', 'com.spotify.client'),
    'ledger-wallet': ('Ledger Wallet.app', 'com.ledger.live'),
    'claude': ('Claude.app', 'com.anthropic.claudefordesktop'),
    't3-code': ('T3 Code (Beta).app', 'com.t3tools.t3code'),
}

GITHUB_RELEASES = {
    't3-code': ('pingdotgg/t3code', 'T3-Code-{v}-{arch}.dmg'),
    'bruno': ('usebruno/bruno', 'bruno_{v}_{arch}_mac.dmg'),
    'macs-fan-control': ('crystalidea/macs-fan-control', 'macsfancontrol.zip'),
}
SPARKLE_FEEDS = {
    'iterm2': ('https://iterm2.com/appcasts/final_modern.xml', {'iterm2.com'}),
    'daisydisk': ('https://daisydiskapp.com/downloads/appcastFeed.php', {'daisydiskapp.com'}),
    'devutils': ('https://devutils.com/archives/appcast.xml', {'devutils.app', 'devutils.com'}),
}
S = '{http://www.andymatuschak.org/xml-namespaces/sparkle}'


def numbers(version):
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', version):
        raise ValueError(f'Unexpected release version {version!r}')
    return tuple(map(int, version.split('.')))


def release(source, version, **details):
    numbers(version)
    return {'version': version, 'source': source, 'channel': 'stable', **details}


def request(url, method='GET'):
    return Request(url, method=method, headers={'User-Agent': 'mise-maintenance', 'Cache-Control': 'no-cache'})


def fetch(url):
    with urlopen(request(url), timeout=15) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError('Unexpectedly large vendor release response')
    return data


def first_redirect(url):
    """Read a publisher's Location header without fetching an installer or signed asset."""
    class StopAtRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        with build_opener(StopAtRedirect()).open(request(url, 'HEAD'), timeout=15):
            raise ValueError('Expected a versioned installer redirect')
    except HTTPError as error:
        with error:
            if error.code not in (301, 302, 303, 307, 308):
                raise
            return error.headers['Location'] or ''


def electron_release(source, template, arch):
    feed = fetch(source).decode().replace('\r\n', '\n') + '\n'
    versions = re.findall(r'''(?m)^version: (['"]?)([0-9.]+)\1[ \t]*$''', feed)
    files = re.findall(r'(?m)^files:[ \t]*\n((?:[ \t].*\n)+)', feed)
    if len(versions) != 1 or len(files) != 1:
        raise ValueError('Unrecognized macOS release manifest')
    version = versions[0][1]
    asset = template.format(v=version, arch=arch)
    if asset not in re.findall(r'(?m)^[ \t]+- url: (\S+)[ \t]*$', files[0]):
        raise ValueError('Missing release artifact for this Mac')
    if re.search(r'(?m)^minimumSystemVersion:|^stagingPercentage:(?! 100[ \t]*$)', feed):
        raise ValueError('Release eligibility is restricted by the vendor')
    return release(source, version)


def github_api_release(name, arch):
    repo, template = GITHUB_RELEASES[name]
    source = f'https://api.github.com/repos/{repo}/releases/latest'
    data = github_json(source)
    tag = data['tag_name']
    version = tag.removeprefix('v')
    asset = template.format(v=version, arch=arch)
    expected = f'https://github.com/{repo}/releases/download/{tag}/{asset}'
    if data['draft'] or data['prerelease'] or not any(a['name'] == asset and a['browser_download_url'] == expected
                                                     for a in data['assets']):
        raise ValueError('The GitHub release lacks a published stable macOS installer for this architecture')
    return release(source, version)


def github_release(name, arch):
    repo, template = GITHUB_RELEASES[name]
    latest = f'https://github.com/{repo}/releases/latest/download/'
    try:
        if name != 'macs-fan-control':
            return electron_release(latest + 'latest-mac.yml', template, arch)
        source = latest + template
        target = first_redirect(source)
        match = re.fullmatch(re.escape(f'https://github.com/{repo}/releases/download/') + r'v([0-9.]+)/macsfancontrol\.zip', target)
        if not match or urlsplit(first_redirect(target)).hostname != 'release-assets.githubusercontent.com':
            raise ValueError('Missing published Macs Fan Control installer')
        return release(source, match[1])
    except OSError:
        # The public release/download routes do not consume REST API quota.
        return github_api_release(name, arch)


def sparkle_release(name, arch):
    source, hosts = SPARKLE_FEEDS[name]
    system = numbers(platform.mac_ver()[0]) + (0, 0)
    choices = []
    for item in ET.fromstring(fetch(source)).findall('./channel/item'):
        def field(key):
            return (item.findtext(S + key) or '').strip()
        if field('channel') not in ('', 'stable', 'release'):
            continue
        minimum, maximum = field('minimumSystemVersion'), field('maximumSystemVersion')
        if minimum and system[:3] < (numbers(minimum) + (0, 0))[:3]:
            continue
        if maximum and system[:3] > (numbers(maximum) + (0, 0))[:3]:
            continue
        if field('hardwareRequirements') not in ('', 'arm64' if arch == 'arm64' else 'x86_64'):
            continue
        for enclosure in item.findall('enclosure'):
            url = urlsplit(enclosure.get('url', ''))
            if url.scheme != 'https' or url.hostname not in hosts or enclosure.get(S + 'os', 'macos') != 'macos':
                continue
            if enclosure.get(S + 'deltaFrom'):
                continue
            build = field('version') or enclosure.get(S + 'version')
            version = field('shortVersionString') or enclosure.get(S + 'shortVersionString') or build
            numbers(build)
            choices.append(release(source, version, build=build))
    if not choices:
        raise ValueError('No compatible stable macOS release in the vendor appcast')
    return max(choices, key=lambda row: numbers(row['build']))


def redirected_release(name, arch):
    if name == 'github':
        target = 'darwin-arm64' if arch == 'arm64' else 'darwin'
        source = f'https://central.github.com/deployments/desktop/desktop/latest/{target}'
        pattern = rf'https://desktop\.githubusercontent\.com/releases/([0-9.]+)-[0-9a-f]+/GitHubDesktop-{arch}\.zip'
    else:
        source = 'https://slack.com/ssb/download-osx' + ('-silicon' if arch == 'arm64' else '')
        pattern = rf'https://downloads\.slack-edge\.com/desktop-releases/mac/{arch}/([0-9.]+)/Slack-\1-macOS\.dmg'
    with urlopen(request(source, 'HEAD'), timeout=15) as response:
        match = re.fullmatch(pattern, response.geturl())
    if not match:
        raise ValueError('Vendor redirect did not identify the expected stable macOS artifact')
    return release(source, match[1])


def editor_release(name, arch):
    if name == 'devin-desktop':
        target = f'darwin-{arch}-dmg'
        source = f'https://windsurf-stable.codeium.com/api/update/{target}/stable/latest'
        data = json.loads(fetch(source))
        version = data['windsurfVersion']
        artifact = rf'https://windsurf-stable\.codeiumdata\.com/{target}/stable/[0-9a-f]+(?:-[0-9a-f]+)?/Devin-darwin-{arch}-{re.escape(version)}\.dmg'
    else:
        target = 'darwin-arm64' if arch == 'arm64' else 'darwin'
        source = f'https://update.code.visualstudio.com/api/update/{target}/stable/latest'
        data = json.loads(fetch(source))
        version = data['productVersion']
        artifact = rf'https://vscode\.download\.prss\.microsoft\.com/dbazure/download/stable/[0-9a-f]+/VSCode-{target}\.zip'
    if not re.fullmatch(artifact, data['url']):
        raise ValueError('Unexpected installer in the stable editor release feed')
    return release(source, version)


def opera_release():
    source = 'https://ftp.opera.com/pub/opera/desktop/'
    versions = set(re.findall(rb'href="([0-9]+(?:\.[0-9]+){3})/"', fetch(source)))
    # Windows and Linux releases can appear in the same directory before macOS.
    for version in sorted((v.decode() for v in versions), key=numbers, reverse=True)[:10]:
        try:
            listing = fetch(f'{source}{version}/mac/')
        except HTTPError as error:
            if error.code == 404:
                continue
            raise
        if f'href="Opera_{version}_Setup.dmg"'.encode() in listing:
            return release(source, version)
    raise ValueError('Could not identify a recent stable Opera release with a macOS installer')


def spotify_version_from_image(source, etag):
    # The vendor publishes an unversioned disk image. Inspect its plist without launching the app.
    with tempfile.TemporaryDirectory() as directory:
        image = Path(directory) / 'spotify.dmg'
        headers = Path(directory) / 'response-headers'
        subprocess.run(['/usr/bin/curl', '-qfsSL', '--proto', '=https', '--proto-redir', '=https',
                        '--max-time', '180', '--max-filesize', '536870912', '--header', f'If-Match: {etag}',
                        '--dump-header', str(headers), '--output', str(image), source], check=True, timeout=185, capture_output=True)
        final_headers = headers.read_bytes().decode('latin1').strip().split('\r\n\r\n')[-1]
        if re.findall(r'(?im)^etag:[ \t]*([^\r\n]+)', final_headers) != [etag]:
            raise ValueError('Spotify installer changed during the release check')
        # The mount point is separate from the recursively cleaned download directory.
        mount = Path(tempfile.mkdtemp(prefix='mise-spotify-'))
        try:
            subprocess.run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-noautoopen',
                            '-mountpoint', str(mount), str(image)], check=True, timeout=60, capture_output=True)
            with (mount / 'Spotify.app/Contents/Info.plist').open('rb') as handle:
                info = plistlib.load(handle)
            if info['CFBundleIdentifier'] != APPS['spotify'][1]:
                raise ValueError('Unexpected app in the Spotify installer')
            return info['CFBundleShortVersionString']
        finally:
            if os.path.ismount(mount):
                subprocess.run(['/usr/bin/hdiutil', 'detach', str(mount)], check=True, timeout=45, capture_output=True)
            mount.rmdir()


def spotify_release(arch, state, write):
    suffix = 'ARM64' if arch == 'arm64' else ''
    source = f'https://download.scdn.co/Spotify{suffix}.dmg'
    with urlopen(request(source, 'HEAD'), timeout=15) as response:
        etag = response.headers.get('ETag', '')
    if not re.fullmatch(r'"[a-zA-Z0-9._-]+"', etag):
        raise ValueError('Spotify did not supply a strong installer identifier')
    path = state / f'spotify-{arch}-release.json'
    try:
        cached = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        cached = {}
    if isinstance(cached, dict) and cached.get('etag') == etag and isinstance(cached.get('version'), str):
        if re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', cached['version']):
            return release(source, cached['version'], etag=etag)
    result = release(source, spotify_version_from_image(source, etag), etag=etag)
    write(path, json.dumps(result) + '\n')
    return result


def latest(name, state, write):
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64'}.get(platform.machine())
    if not arch:
        raise ValueError('Unsupported Mac architecture')
    if name in GITHUB_RELEASES:
        return github_release(name, arch)
    if name in SPARKLE_FEEDS:
        return sparkle_release(name, arch)
    if name in ('slack', 'github'):
        return redirected_release(name, arch)
    if name in ('devin-desktop', 'visual-studio-code'):
        return editor_release(name, arch)
    if name == 'opera':
        return opera_release()
    if name == 'spotify':
        return spotify_release(arch, state, write)
    if name == 'postman':
        target = 'osx_arm64' if arch == 'arm64' else 'osx64'
        source = f'https://dl.pstmn.io/api/version/latest?platform={target}&channel=stable'
        return release(source, json.loads(fetch(source))['version'])
    if name == 'discord':
        source = f'https://discord.com/api/downloads/distributions/app/installers/latest?arch={arch}&channel=stable&platform=osx'
        try:
            target = first_redirect(source)
        except OSError:
            target = ''
        match = re.fullmatch(r'https://stable\.dl2\.discordapp\.net/distro/app/stable/osx/(?:universal|'
                             + arch + r')/([0-9.]+)/Discord\.dmg(?:\?[^#]*)?', target)
        if match:
            return release(source, match[1])
        source = f'https://updates.discord.com/distributions/app/manifests/latest?channel=stable&platform=osx&arch={arch}'
        full = json.loads(fetch(source))['full']
        if not isinstance(full['host_version'], list) or not all(type(i) is int for i in full['host_version']):
            raise ValueError('Invalid Discord host version')
        version = '.'.join(map(str, full['host_version']))
        artifact = urlsplit(full['url'])
        if (artifact.scheme != 'https' or artifact.hostname != 'stable.dl2.discordapp.net'
                or artifact.path not in {f'/distro/app/stable/osx/{a}/{version}/full.distro' for a in ('universal', arch)}):
            raise ValueError('Unexpected stable Discord macOS artifact')
        return release(source, version)
    if name == 'claude':
        source = 'https://downloads.claude.ai/releases/darwin/universal/RELEASES.json'
        data = json.loads(fetch(source))
        version = data['currentRelease']
        pattern = rf'https://downloads\.claude\.ai/releases/darwin/universal/{re.escape(version)}/Claude-[a-f0-9]+\.zip'
        if not any(row.get('version') == version and row['updateTo']['version'] == version
                   and re.fullmatch(pattern, row['updateTo']['url']) for row in data['releases']):
            raise ValueError('Missing current universal macOS release in the Claude Desktop feed')
        return release(source, version)
    if name == 'ledger-wallet':
        return electron_release('https://download.live.ledger.com/latest-mac.yml', 'ledger-live-desktop-{v}-mac.zip', arch)
    raise ValueError(f'Unknown desktop release source for {name}')
