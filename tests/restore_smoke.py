"""Exercise the bootstrap plan and both real bundle repairs on a disposable Mac home."""
import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib


def run(args, env, *, check=True):
    proc = subprocess.run(args, cwd=env['HOME'], env=env, text=True, capture_output=True, timeout=240)
    if check and proc.returncode:
        raise RuntimeError(f'{args[0]} failed ({proc.returncode}): {proc.stdout}\n{proc.stderr}')
    return proc


def smoke(mise):
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='mise-restore-') as temp:
        root = Path(temp).resolve()
        home = root / 'home'
        cfg = home / '.config/mise'
        shutil.copytree(repo, cfg, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        binary = home / '.local/bin/mise'
        binary.parent.mkdir(parents=True)
        binary.symlink_to(mise)
        env = {'HOME': str(home), 'PATH': f'{binary.parent}:/usr/bin:/bin:/usr/sbin:/sbin',
               'TMPDIR': str(root), 'MISE_TRUSTED_CONFIG_PATHS': str(cfg),
               'MISE_SELF_UPDATE_AUTO': 'false', 'MISE_TOOL_UPDATE_GLOBAL_AUTO': 'false'}
        api = {'GITHUB_TOKEN': os.environ['GITHUB_TOKEN']} if os.environ.get('GITHUB_TOKEN') else {}
        # The manifest manages its own origin. Model the checkout created by --adopt.
        run(['git', '-C', cfg, 'init', '-q', '-b', 'main'], env)
        run(['git', '-C', cfg, 'remote', 'add', 'origin', 'https://github.com/trevordcampbell/mise-setup.git'], env)
        run(['git', '-C', cfg, 'add', '.'], env)
        run(['git', '-C', cfg, '-c', 'user.name=Restore test', '-c', 'user.email=test@localhost',
             'commit', '-qm', 'Disposable restore fixture'], env)
        plan = run([mise, 'bootstrap', '--dry-run', '--yes'], dict(env, **api))
        plan_text = plan.stdout + plan.stderr
        for expected in ('mise-maintenance', 'shell:completions', 'fonts:sync'):
            if expected not in plan_text:
                raise RuntimeError(f'{expected} missing from the restore plan:\n{plan_text}')
        if (home / '.zshrc').exists() or (home / 'Library/LaunchAgents/dev.mise.mise-maintenance.plist').exists():
            raise RuntimeError('The bootstrap preview applied resources')
        print('Fresh-home bootstrap plan verified.', flush=True)
        stages = [('python', '3.15.2', '26.2.2', '26.3'), ('node', '26.13.1', '11.20.0', '11.21.3')]
        tools = tomllib.loads((cfg / 'config.toml').read_text())['tools']
        downloads = root / 'download-shim'
        downloads.mkdir()
        fake_curl = downloads / 'curl'
        runtime_home = root / 'runtime-home'
        runtime_home.mkdir()
        for tool, runtime, before, after in stages:
            prefix = root / tool
            run([mise, 'install-into', f'core:{tool}@{runtime}', prefix], dict(env, HOME=str(runtime_home), **api))
            query = ([prefix/'bin/python3', '-I', '-c', 'from importlib.metadata import version; print(version("pip"))']
                     if tool == 'python' else [prefix/'bin/node', prefix/'lib/node_modules/npm/bin/npm-cli.js', '--version'])
            hook_env = dict(env, MISE_TOOL_INSTALL_PATH=str(prefix))
            hook = ['/bin/sh', '-ec', tools[tool]['postinstall']]
            shadow_env = dict(hook_env, PATH=f'{downloads}:{env["PATH"]}')
            if tool == 'python':
                actual = run([prefix/'bin/python3', '-I', '-c', 'import sys; print(sys.prefix)'], env).stdout.strip()
                if Path(actual).resolve() != prefix:
                    raise RuntimeError('The disposable Python prefix is not isolated')
            if run(query, env).stdout.strip() != before:
                # Stock runtime rebuilds may bundle a different manager. Recreate
                # the original affected pair only inside this disposable install.
                if tool == 'python':
                    url = ('https://files.pythonhosted.org/packages/3f/06/'
                           '3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl')
                    checksum = 'fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70'
                    algorithm = 'sha256'
                    install = [prefix/'bin/python3', '-Im', 'pip', 'install', '--prefix', prefix, '--no-index', '--no-deps']
                else:
                    url = 'https://registry.npmjs.org/npm/-/npm-11.20.0.tgz'
                    checksum = base64.b64decode('dF3EDFwbYN+N5RUip+ZYDe0NeURK5BgqKOcvT1iNtUYhTMTl0FwWhBuXrS7KtXyduqyTMS5aaQaregnHDAxNgw==').hex()
                    algorithm = 'sha512'
                    install = [*query[:-1], 'install', '--global', '--prefix', prefix, '--offline', '--ignore-scripts', '--no-audit', '--no-fund']
                artifact = root / url.rsplit('/', 1)[-1]
                run(['curl', '-qfsSL', '--max-time', '90', '-o', artifact, url], env)
                if hashlib.new(algorithm, artifact.read_bytes()).hexdigest() != checksum:
                    raise RuntimeError('Invalid checksum for the original manager fixture')
                run([*install, artifact], env)
                if run(query, env).stdout.strip() != before:
                    raise RuntimeError(f'Could not prepare {tool} bundle {before}')
            fake_curl.write_text('#!/bin/sh\nwhile [ "$1" != --output ]; do shift; done\nprintf corrupt > "$2"\n')
            fake_curl.chmod(0o755)
            rejected = run(hook, shadow_env, check=False)
            if not rejected.returncode or run(query, env).stdout.strip() != before:
                raise RuntimeError(f'{tool} accepted a corrupt download or changed the original manager')
            run(hook, hook_env)
            if run(query, env).stdout.strip() != after:
                raise RuntimeError(f'{tool} failed to install its verified bundle')
            fake_curl.write_text('#!/bin/sh\nexit 97\n')
            run(hook, shadow_env)
            if run(query, env).stdout.strip() != after:
                raise RuntimeError(f'Repeated {tool} hook changed the bundle')
            print(f'{tool} {runtime}: rejected corrupt bytes, updated to {after}, repeat run skipped download.', flush=True)


if __name__ == '__main__':
    smoke(Path(sys.argv[1] if len(sys.argv) > 1 else shutil.which('mise')).resolve())
