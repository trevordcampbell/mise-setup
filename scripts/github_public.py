"""Read public GitHub repository metadata, with a CLI fallback on rate limits."""
import json
import os
import shutil
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def github_json(url, timeout=30):
    prefix = 'https://api.github.com/repos/'
    if not url.startswith(prefix):
        raise ValueError('Expected a public GitHub repository API URL')
    request = Request(url, headers={'User-Agent': 'mise-local-maintenance', 'Accept': 'application/vnd.github+json'})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        with error:
            if error.code not in (403, 429):
                raise
            limited = (error.code == 429 or error.headers.get('X-RateLimit-Remaining') == '0'
                       or 'rate limit' in str(error.reason).lower() or b'rate limit' in error.read(4096).lower())
            gh = shutil.which('gh') if limited else None
            if not gh:
                raise
            # gh handles authentication directly. No token is read, logged or added to the environment.
            endpoint = url.removeprefix('https://api.github.com')
            result = subprocess.run([gh, 'api', '--hostname', 'github.com', '--method', 'GET', endpoint],
                                    env=dict(os.environ, GH_PROMPT_DISABLED='1'), stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError('GitHub rate-limited the public lookup and the read-only gh api retry failed') from error
            return json.loads(result.stdout)
