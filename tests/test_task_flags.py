"""Exercise Mise's help and argument forwarding without invoking host maintenance."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('mise'), 'Mise CLI required for task invocation')
class TaskFlagsTests(unittest.TestCase):
    def test_help_and_forwarding_use_the_same_flags(self):
        tasks = tomllib.loads((ROOT / 'config.toml').read_text())['tasks']
        cases = {
            'maintenance:update': ([], ['--verbose', '--json']),
            'setup:sync': ([], ['--verbose', '--json']),
            'maintenance:check': (['--check'], ['--verbose', '--json']),
            'maintenance:status': (['--status'], ['--verbose', '--json']),
            'maintenance:health': (['--health'], ['--json']),
            'maintenance:history': (['--history'], ['--json', '--limit=2']),
            'maintenance:dependencies': (['--dependencies'], ['--verbose', '--json', '--tool=Example Tool', '--review=abcde1234567', '--assessment=outside', '--reason=Reviewed build']),
        }
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / '.config/mise'
            (config / 'scripts').mkdir(parents=True)
            shutil.copyfile(ROOT / 'scripts/run-maintenance.sh', config / 'scripts/run-maintenance.sh')
            # Load the real task definitions in an isolated home with no tool installs.
            lines = []
            for name in cases:
                lines.append(f'[tasks."{name}"]')
                lines.extend(f'{key} = {json.dumps(value)}' for key, value in tasks[name].items())
            (config / 'config.toml').write_text('\n'.join(lines))
            probe = home / '.local/bin/mise'
            probe.parent.mkdir(parents=True)
            probe.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            probe.chmod(0o755)
            env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'NO_COLOR': '1',
                   'MISE_TRUSTED_CONFIG_PATHS': str(home), 'MISE_CACHE_DIR': str(home / 'cache')}
            def run(name, *args):
                return subprocess.run([shutil.which('mise'), '-C', str(home), 'run', name, *args],
                                      cwd=home, env=env, text=True, capture_output=True, timeout=20)
            for name, (mode, flags) in cases.items():
                with self.subTest(name=name):
                    help_result = run(name, '--help')
                    self.assertEqual(help_result.returncode, 0, help_result.stderr)
                    for flag in flags:
                        self.assertIn(flag.split('=')[0], help_result.stdout)
                    for cli_flags in ([], flags):
                        result = run(name, *cli_flags)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.splitlines(), ['-C', str(home), 'exec', '--', 'python',
                                         str(config / 'scripts/maintenance.py'), *mode, *cli_flags])
