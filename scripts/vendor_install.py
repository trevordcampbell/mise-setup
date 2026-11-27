"""Advance a self-updating, Mise-owned app from a verified stable vendor artifact."""
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


def stage(artifact, target, run):
    url, algorithm, expected = (artifact[k] for k in ('url', 'algorithm', 'digest'))
    parts = urlsplit(url)
    suffix = Path(parts.path).suffix
    if (parts.scheme != 'https' or not parts.hostname or suffix not in ('.dmg', '.zip')
            or algorithm not in ('sha256', 'sha512')
            or not re.fullmatch(r'[0-9a-f]{%d}' % (hashlib.new(algorithm).digest_size * 2), expected)):
        raise ValueError('Vendor installer requires a supported HTTPS archive and publisher checksum')
    with tempfile.TemporaryDirectory(prefix='mise-vendor-download-') as tmp:
        archive = Path(tmp) / ('release' + suffix)
        run(['/usr/bin/curl', '-qfsSL', '--proto', '=https', '--proto-redir', '=https', '--retry', '2',
             '--connect-timeout', '15', '--max-time', '300', '--output', archive, url], timeout=330)
        verify_artifact(archive, algorithm, expected)
        if suffix == '.zip':
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
    developer_id = 'anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
    run(['/usr/bin/codesign', '--verify', '--strict', '-R', '=' + developer_id, app], timeout=120)
    details = command_output(['/usr/bin/codesign', '-dv', '--verbose=4', app])
    team = re.search(r'(?m)^TeamIdentifier=([A-Z0-9]{10})$', details)
    identifier = info['CFBundleIdentifier']
    if not team or not re.fullmatch(r'[A-Za-z0-9.-]+', identifier):
        raise ValueError('Could not verify the installed Developer ID team and bundle identity')
    requirement = f'={developer_id} and certificate leaf[subject.OU] = "{team[1]}" and identifier "{identifier}"'
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
        stage(artifact, candidate, run)
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
