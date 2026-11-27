"""Advance a self-updating, Mise-owned app from a verified stable vendor artifact."""
import base64
import ctypes
import hashlib
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
import tomllib
from urllib.parse import urlsplit


if __package__:
    from . import staged_archive
else:
    import staged_archive


def ownership(receipts, app):
    paths = list(receipts.glob('*/.mise-cask.toml'))
    if len(paths) != 1 or app.is_symlink() or not app.is_dir():
        return None
    raw = paths[0].read_bytes()
    record = tomllib.loads(raw.decode())
    if (record.get('auto_updates') is not True or record.get('apps') != [str(app)]
            or record.get('metadata_only_apps') != [str(app)]):
        return None
    return paths[0], raw, record.get('binaries', [])


def command_output(args):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if proc.returncode:
        raise RuntimeError(f'{Path(args[0]).name} failed: {proc.stderr.strip() or proc.stdout.strip()}')
    return proc.stdout + proc.stderr


def verify_artifact(path, algorithm, expected):
    with path.open('rb') as f:
        digest = hashlib.file_digest(f, algorithm).hexdigest()
    if digest != expected:
        raise ValueError(f'Vendor installer {algorithm.upper()} checksum mismatch')


def copy_bundle(source, target, run):
    bundles = [p for p in source.glob('*.app') if not p.is_symlink() and p.is_dir()]
    if len(bundles) != 1:
        raise ValueError('Expected one application at the root of the vendor installer')
    run(['/usr/bin/ditto', bundles[0], target], timeout=300)
    if target.name == 'Google Chrome.app':
        clear_chrome_finder_info(target, run)


def clear_chrome_finder_info(copy, run):
    # Only use on private copies. -s acts on symbolic links themselves; quarantine and
    # every other extended attribute are preserved for signature and Gatekeeper checks.
    run(['/usr/bin/xattr', '-rds', 'com.apple.FinderInfo', copy], timeout=120)


def publisher_requirement(app, run):
    developer_id = 'anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
    try:
        run(['/usr/bin/codesign', '--verify', '--strict', '-R', '=' + developer_id, app], timeout=120)
    except RuntimeError:
        if app.name != 'Google Chrome.app':
            raise
        # Google's old helper bundles can retain Finder display metadata. Verify the
        # live code and sealed symlinks, then verify every framework under full strict
        # rules on a private clone with just that metadata removed.
        google = f'={developer_id} and certificate leaf[subject.OU] = "EQHXZ8M8AV"'
        requirement = google + ' and identifier "com.google.Chrome"'
        run(['/usr/bin/codesign', '--verify', '--deep', '--strict=symlinks', '-R', requirement, app], timeout=180)
        with tempfile.TemporaryDirectory(prefix='mise-chrome-signature-') as tmp:
            copy = Path(tmp) / app.name
            run(['/usr/bin/ditto', '--clone', app, copy], timeout=300)
            clear_chrome_finder_info(copy, run)
            run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', requirement, copy], timeout=180)
        return google
    details = command_output(['/usr/bin/codesign', '-dv', '--verbose=4', app])
    team = re.search(r'(?m)^TeamIdentifier=([A-Z0-9]{10})$', details)
    if not team:
        raise ValueError('Could not verify the installed Developer ID team and bundle identity')
    return f'={developer_id} and certificate leaf[subject.OU] = "{team[1]}"'


def verify_archive(path, artifact, app, run):
    method = artifact['algorithm']
    if method in ('sha256', 'sha512'):
        verify_artifact(path, method, artifact['digest'])
        return
    requirement = publisher_requirement(app, run)
    if method == 'developer-id-dmg':
        run(['/usr/bin/codesign', '--verify', '--strict', '-R', requirement, path], timeout=180)
        return
    installed = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    key = installed.get('SUPublicEDKey', '')
    if (installed.get('SURequireSignedFeed') or not re.fullmatch(r'[A-Za-z0-9+/]{43}=', key)
            or path.stat().st_size != artifact['size']):
        raise ValueError('Sparkle signing key, feed policy or archive length precludes this update')
    run(['node', Path(__file__).with_name('verify-sparkle.cjs'), key, artifact['signature'], path], timeout=120)


def stage_isolated(artifact, archive, output, target, installed, run):
    if installed.name == 'Spotify.app':
        app = staged_archive.spotify(archive, output, run)
    else:
        if installed.name == 'DaisyDisk.app':
            publisher_requirement(installed, run)
            # This legacy signature is supplemental. The signed bundle also has to pass
            # strict Developer ID matching and Gatekeeper before it leaves staging.
            signature = output.parent / 'archive.sig'
            signature.write_bytes(base64.b64decode(artifact['signature'], validate=True))
            # Sparkle 1's sign_update signs the binary SHA-1 digest with DSA-SHA1.
            digest = output.parent / 'archive.sha1'
            with archive.open('rb') as f:
                digest.write_bytes(hashlib.file_digest(f, 'sha1').digest())
            run(['/usr/bin/openssl', 'dgst', '-sha1', '-verify', installed / 'Contents/Resources/dsa_pub.pem',
                 '-signature', signature, digest], timeout=60)
        app = staged_archive.app_zip(archive, output, installed.name, run)
    verify_candidate(installed, app, [], run)
    # The caller checks the version and repeats signing checks on the final staged copy.
    run(['/usr/bin/ditto', app, target], timeout=300)


def isolated_format(app, url):
    if app.name == 'Postman.app' and re.fullmatch(r'https://dl\.pstmn\.io/download/version/[0-9.]+/osx(?:_arm64|64)', url):
        return '.zip'
    if app.name == 'DaisyDisk.app' and re.fullmatch(r'https://daisydiskapp\.com/+download/DaisyDisk\.zip', url):
        return '.zip'
    if app.name == 'Spotify.app' and url in ('https://download.scdn.co/Spotify.dmg', 'https://download.scdn.co/SpotifyARM64.dmg'):
        return '.dmg'
    raise ValueError('Unrecognized publisher download for isolated extraction')


def stage(artifact, target, run, installed):
    url, algorithm = artifact['url'], artifact['algorithm']
    parts = urlsplit(url)
    suffix = Path(parts.path).suffix
    if algorithm == 'isolated-app':
        suffix = isolated_format(installed, url)
    if suffix == '.distro' and (installed.name != 'Discord.app' or parts.hostname != 'stable.dl2.discordapp.net'
            or algorithm != 'sha256' or not parts.path.endswith('/full.distro')):
        raise ValueError('Expected a checksummed full Discord distribution')
    if parts.scheme != 'https' or not parts.hostname or suffix not in ('.dmg', '.zip', '.distro'):
        raise ValueError('Vendor installer requires a supported HTTPS archive')
    if algorithm in ('sha256', 'sha512'):
        if not re.fullmatch(r'[0-9a-f]{%d}' % (hashlib.new(algorithm).digest_size * 2), artifact.get('digest', '')):
            raise ValueError('Invalid publisher checksum')
    elif algorithm == 'sparkle-ed25519':
        if not re.fullmatch(r'[A-Za-z0-9+/]{86}==', artifact.get('signature', '')) or not 0 < artifact['size'] <= 2**31:
            raise ValueError('Invalid Sparkle signature or size')
    elif algorithm != 'isolated-app' and (algorithm != 'developer-id-dmg' or suffix != '.dmg'):
        raise ValueError('Unsupported vendor archive verification method')
    with tempfile.TemporaryDirectory(prefix='mise-vendor-download-') as tmp:
        archive = Path(tmp) / ('release' + suffix)
        headers = []
        if installed.name == 'Spotify.app' and algorithm == 'isolated-app':
            etag = artifact.get('etag', '')
            if not re.fullmatch(r'"[a-zA-Z0-9._-]+"', etag):
                raise ValueError('Missing Spotify installer ETag')
            headers = ['--header', 'If-Match: ' + etag, '--dump-header', Path(tmp) / 'headers']
        run(['/usr/bin/curl', '-qfsSL', *headers, '--proto', '=https', '--proto-redir', '=https', '--retry', '2',
             '--connect-timeout', '15', '--max-time', '300', '--max-filesize', str(artifact.get('size', 2**31)), '--output', archive, url], timeout=330)
        if headers and staged_archive.response_etag(Path(tmp) / 'headers') != etag:
            raise ValueError('Spotify installer changed since the vendor check')
        if artifact.get('size') and archive.stat().st_size != artifact['size']:
            raise ValueError('Downloaded archive size disagrees with the release manifest')
        if algorithm == 'isolated-app':
            output = Path(tmp) / 'extracted'
            output.mkdir()
            stage_isolated(artifact, archive, output, target, installed, run)
            return
        verify_archive(archive, artifact, installed, run)
        if suffix == '.distro':
            source = Path(tmp) / 'extracted'
            source.mkdir()
            tarball = Path(tmp) / 'discord.tar'
            run(['node', Path(__file__).with_name('unpack-discord.cjs'), archive, tarball], timeout=120)
            run(['/usr/bin/tar', '-xf', tarball, '-C', source, 'files/Discord.app'], timeout=180)
            app = source / 'files/Discord.app'
            # The publisher's tar gives framework symlinks mode 000. Restore access
            # to the links themselves; all link targets and signed file contents stay intact.
            for directory, dirs, files in os.walk(app):
                for name in dirs + files:
                    link = Path(directory) / name
                    if link.is_symlink():
                        os.lchmod(link, 0o755)
            staged_archive.check_tree(app)
            run(['/usr/bin/ditto', app, target], timeout=300)
        elif suffix == '.zip':
            source = Path(tmp) / 'extracted'
            source.mkdir()
            # libarchive's default extraction rejects parent traversal and writes through symlinks.
            run(['/usr/bin/tar', '-xf', archive, '-C', source], timeout=300)
            copy_bundle(source, target, run)
        else:
            # Keep the mount outside the recursively cleaned download and app directories.
            mount = Path(tempfile.mkdtemp(prefix='mise-vendor-image-'))
            try:
                run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-noautoopen',
                     '-mountpoint', mount, archive], timeout=90)
                copy_bundle(mount, target, run)
            finally:
                if os.path.ismount(mount):
                    run(['/usr/bin/hdiutil', 'detach', mount], timeout=60)
                mount.rmdir()


def verify_candidate(app, staged, binaries, run):
    info = plistlib.loads((staged / 'Contents/Info.plist').read_bytes())
    # Bind the new signature to the trusted, currently installed publisher and bundle.
    identifier = info['CFBundleIdentifier']
    old_id = plistlib.loads((app / 'Contents/Info.plist').read_bytes())['CFBundleIdentifier']
    if identifier != old_id or not re.fullmatch(r'[A-Za-z0-9.-]+', identifier):
        raise ValueError('Invalid bundle identifier')
    requirement = publisher_requirement(app, run) + f' and identifier "{identifier}"'
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', requirement, staged], timeout=180)
    run(['/usr/sbin/spctl', '--assess', '--type', 'execute', staged], timeout=120)
    executable = info['CFBundleExecutable']
    if Path(executable).name != executable or executable in ('', '.', '..'):
        raise ValueError('Invalid app executable')
    arch = platform.machine()
    run(['/usr/bin/lipo', '-verify_arch', arch, staged / 'Contents/MacOS' / executable], timeout=30)
    minimum = info.get('LSMinimumSystemVersionByArchitecture', {}).get(arch) or info.get('LSMinimumSystemVersion')
    if minimum:
        def version(text):
            if not re.fullmatch(r'\d+(?:\.\d+){0,3}', text):
                raise ValueError(f'Unknown macOS version {text!r}')
            return (tuple(map(int, text.split('.'))) + (0,) * 4)[:4]
        if version(minimum) > version(platform.mac_ver()[0]):
            raise ValueError(f'Vendor release requires macOS {minimum}')
    for binary in binaries:
        # Walk the receipt's symlinks until entering the app. The final target could
        # otherwise hide a renamed launcher behind a symlink inside the old bundle.
        link = Path(binary)
        for _ in range(40):
            if link.is_relative_to(app):
                replacement = staged / link.relative_to(app)
                if (not replacement.resolve(strict=True).is_relative_to(staged.resolve())
                        or not replacement.is_file() or not os.access(replacement, os.X_OK)):
                    raise ValueError(f'Vendor release does not preserve the {Path(binary).name} launcher')
                break
            if not link.is_symlink():
                raise ValueError(f'Cannot verify cask launcher {binary}')
            link = Path(os.path.normpath(link.parent / os.readlink(link)))
        else:
            raise ValueError(f'Cannot resolve cask launcher {binary}')


def exchange(left, right):
    # Darwin renamex_np(RENAME_SWAP) replaces two directories atomically on the same volume.
    swap = ctypes.CDLL(None, use_errno=True).renamex_np
    swap.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    swap.restype = ctypes.c_int
    if swap(os.fsencode(left), os.fsencode(right), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(right))


def install(app, receipts, artifact, assess, running, run):
    owner = ownership(receipts, app)
    if owner is None:
        return {'state': 'update-available', 'notes': ['Vendor install requires a single Mise receipt for a self-updating app.']}
    directory = Path(tempfile.mkdtemp(prefix='.mise-vendor-', dir=app.parent))
    candidate = directory / app.name
    preserve = False
    try:
        stage(artifact, candidate, run, app)
        if assess(candidate)['state'] != 'current':
            raise ValueError('Staged app does not match the advertised stable release')
        verify_candidate(app, candidate, owner[2], run)
        if owner != ownership(receipts, app):
            raise RuntimeError('Mise ownership changed during the download')
        before = assess(app)
        if before['state'] in ('current', 'newer'):
            return {'state': before['state'], 'vendor_check': before}
        if running(app):
            return {'state': 'deferred-running-app', 'vendor_check': before}
        # Preserve the previous bundle through all post-installation checks, including interruption.
        preserve = True
        try:
            exchange(candidate, app)
        except OSError:
            preserve = False
            raise
        try:
            displaced = assess(candidate)
            if displaced['state'] != 'update-available':
                raise RuntimeError('Installed app changed during replacement')
            after = assess(app)
            if after['state'] != 'current' or ownership(receipts, app) != owner or not all(os.access(b, os.X_OK) for b in owner[2]):
                raise RuntimeError('Installed version or cask launcher failed verification')
        except BaseException:
            try:
                exchange(candidate, app)
            except OSError as error:
                raise RuntimeError(f'App rollback failed. Previous bundle retained at {candidate}') from error
            preserve = False
            raise
        preserve = False
        return {'state': 'updated', 'vendor_check': after, 'vendor_upgrade': {
            'before': displaced['installed'], 'after': after['installed'], **artifact}}
    finally:
        if not preserve:
            shutil.rmtree(directory)
