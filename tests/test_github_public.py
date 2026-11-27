import io
import subprocess
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from scripts import github_public as api


class PublicRepositoryTests(unittest.TestCase):
    url = 'https://api.github.com/repos/marktext/marktext/releases?per_page=30'

    def test_rate_limit_uses_read_only_github_cli(self):
        error = HTTPError(self.url, 403, 'Forbidden', {'X-RateLimit-Remaining': '0'}, io.BytesIO())
        with patch.object(api, 'urlopen', side_effect=error), patch.object(api.shutil, 'which', return_value='/tools/gh'), patch.object(api.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'[]')) as run:
            self.assertEqual(api.github_json(self.url), [])
        self.assertEqual(run.call_args.args[0], ['/tools/gh', 'api', '--hostname', 'github.com', '--method', 'GET', '/repos/marktext/marktext/releases?per_page=30'])
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_other_hosts_and_non_rate_limit_errors_never_use_credentials(self):
        with patch.object(api.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                api.github_json('https://example.com/repos/release')
            error = HTTPError(self.url, 403, 'Forbidden', {}, io.BytesIO(b'access denied'))
            with patch.object(api, 'urlopen', side_effect=error), self.assertRaises(HTTPError):
                api.github_json(self.url)
            run.assert_not_called()
