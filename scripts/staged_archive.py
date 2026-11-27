"""Read a first-party download in an isolated workspace before trusting the signed app."""
import os
from pathlib import Path
import plistlib
import re
import stat
import selectors
import time
import subprocess
import zipfile


# The signed runtime and extractor may run. Archive content gets no network, execution,
# child-process or write permission outside the one private extraction directory.
PROFILE = '''(version 1)(deny default)(import "system.sb")
(deny network*)(deny process-fork)
(allow process-exec file-map-executable (literal (param "EXE")))
(allow file-read-metadata)
(allow file-read* (literal (param "EXE")) (literal (param "ARCHIVE")))
(allow file-read* file-write* (subpath (param "OUT")))'''
LIMIT = 2**31
FILE_LIMIT = 50000


def restricted(exe, archive, directory, *args):
    return ['/usr/bin/sandbox-exec', '-D', f'EXE={exe}', '-D', f'ARCHIVE={archive.resolve()}',
            '-D', f'OUT={directory.resolve()}', '-p', PROFILE, exe, *args]


def response_etag(headers):
    # curl includes redirects and retries. Only the final response identifies the bytes received.
    final = headers.read_bytes().decode('latin1').strip().split('\r\n\r\n')[-1]
    values = re.findall(r'(?im)^etag:[ \t]*([^\r\n]+)', final)
    return values[0] if len(values) == 1 else None


def seven_zip():
    # Resolve the actual Mise executable. Executing a shim would require spawning another process.
    mise = Path.home() / '.local/bin/mise'
    result = subprocess.run([mise, '-C', Path.home(), 'which', '7zz'], capture_output=True, text=True,
                            timeout=30, check=True)
    exe = Path(result.stdout.strip()).resolve(strict=True)
    if not exe.is_file() or not os.access(exe, os.X_OK):
        raise ValueError('Mise could not resolve its 7zz executable')
    return str(exe)


def check_tree(app):
    if app.is_symlink() or not app.is_dir():
        raise ValueError('Installer lacks the expected app directory')
    root = app.resolve()
    # Never pass externally pointing links, device nodes or pipes to ditto or codesign.
    size = count = 0
    for directory, dirs, files in os.walk(app):
        for name in dirs + files:
            path = Path(directory) / name
            entry = path.lstat()
            count += 1
            size += entry.st_size
            if count > FILE_LIMIT or size > LIMIT:
                raise ValueError('Application exceeds extraction limits')
            if stat.S_ISLNK(entry.st_mode):
                if os.path.isabs(os.readlink(path)) or not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError('Application contains an external symbolic link')
            elif not (stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)):
                raise ValueError('Unexpected file type in app')


def limited_listing(args):
    # Stop the extractor as soon as either limit is reached. A malformed image
    # cannot fill the disk or pipe an arbitrarily large inventory into Python.
    data = bytearray()
    deadline = time.monotonic() + 45
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not ready.select(remaining):
                        raise TimeoutError('Spotify image inventory timed out')
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > 12 * 1024**2:
                        raise ValueError('Spotify image inventory is too large')
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                raise ValueError('Sandboxed extractor could not read the Spotify image inventory')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return data


def spotify(archive, directory, run, *, plist_only=False):
    exe = seven_zip()
    member = 'Spotify/Spotify.app' + ('/Contents/Info.plist' if plist_only else '')
    # Inspect the selected entries inside the sandbox before extracting them.
    args = restricted(exe, archive, directory, 'l', '-ba', '-slt', archive, member)
    rows = limited_listing(args)
    sizes = re.findall(rb'(?m)^Size = ([0-9]+)\r?$', rows)
    if not sizes or rows.count(b'Path = ') > FILE_LIMIT or sum(map(int, sizes)) > (1048576 if plist_only else LIMIT):
        raise ValueError('Unexpected Spotify image contents')
    run(restricted(exe, archive, directory, 'x', '-bd', '-bb0', '-y', f'-o{directory}', archive, member), timeout=240)
    app = directory / 'Spotify/Spotify.app'
    if app.parent.is_symlink() or not app.resolve().is_relative_to(directory.resolve()):
        raise ValueError('Spotify image redirects its app directory')
    check_tree(app)
    if plist_only:
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        if info.get('CFBundleIdentifier') != 'com.spotify.client':
            raise ValueError('Unexpected Spotify bundle identifier')
        return info['CFBundleShortVersionString']
    return app


def app_zip(archive, directory, name, run):
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > FILE_LIMIT or sum(x.file_size for x in entries) > LIMIT:
            raise ValueError('ZIP exceeds extraction limits')
    run(restricted('/usr/bin/bsdtar', archive, directory, '-xf', archive, '-C', directory, name), timeout=240)
    app = directory / name
    check_tree(app)
    return app
