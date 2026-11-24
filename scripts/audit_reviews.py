"""Applicability decisions are local and bound to the evidence actually reviewed."""
from collections import Counter
from pathlib import Path
import hashlib
import json
import os
import platform

ASSESSMENTS = {
    'relevant': 'Relevant to this Mac',
    'unconfirmed': 'Reachability unconfirmed',
    'outside': 'Outside reviewed scope',
    'notice': 'Maintenance notice',
}
REVIEW_FILE = 'dependency-reviews.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def build_digest(root):
    """Hash installed content and symlink targets, ignoring generated Python bytecode."""
    h = hashlib.sha256()
    def fail(error):
        raise error
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for name in sorted(files + [d for d in dirs if (Path(folder) / d).is_symlink()]):
            path = Path(folder) / name
            if name.endswith(('.pyc', '.pyo')):
                continue
            if path.is_symlink():
                content = ['link', os.readlink(path)]
            elif path.is_file():
                with path.open('rb') as stream:
                    content = ['file', path.stat().st_mode & 0o111, hashlib.file_digest(stream, 'sha256').hexdigest()]
            else:
                continue
            h.update(bytes.fromhex(digest([str(path.relative_to(root)), content])))
    return h.hexdigest()


def sign_findings(owner, root):
    if not owner['findings']:
        return
    build = build_digest(root)
    target = [platform.system(), platform.machine(), platform.mac_ver()[0] or platform.release()]
    owner['build_sha256'] = build
    for f in owner['findings']:
        f['review_key'] = digest([owner['tool'], f['ecosystem'], f['package'], f['id']])
        f['review_token'] = digest([f['review_key'], owner['version'], f['version'], target, build,
                                    sorted(f['record_hashes']), sorted(f['sources'], key=lambda s: (s['kind'], s['path'])),
                                    owner['coverage'], owner.get('unresolved_sources', [])])


def load(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get('version') != 1 or not isinstance(data.get('entries'), dict):
        raise ValueError('Unrecognized dependency review file')
    for token, record in data['entries'].items():
        if (not isinstance(record, dict) or record.get('assessment') not in ASSESSMENTS
                or not all(isinstance(record.get(k), str) and record[k] for k in ('key', 'reason', 'reviewed_at'))):
            raise ValueError(f'Invalid dependency review entry {token[:16]}')
    return data['entries']


def apply(report, entries):
    known = {r['key'] for r in entries.values()}
    counts = Counter()
    for owner in report['owners'].values():
        for f in owner['findings']:
            token = f.get('review_token')
            entry = entries.get(token)
            if entry and entry['key'] == f['review_key'] and entry['assessment'] in ASSESSMENTS:
                f['review'] = {k: entry[k] for k in ('assessment', 'reason', 'reviewed_at')}
            else:
                f['review'] = {'assessment': 'changed' if f.get('review_key') in known else 'unreviewed'}
            counts[f['review']['assessment']] += 1
    report['review_counts'] = dict(counts)


def apply_local(report, path):
    try:
        entries = load(path)
    except (OSError, ValueError, AttributeError) as error:
        # A broken local annotation file must not hide any scanner findings.
        entries = {}
        report['review_error'] = str(error)
    else:
        report.pop('review_error', None)
    apply(report, entries)


def entry(finding, assessment, reason, date):
    if assessment not in ASSESSMENTS or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise ValueError('Choose a listed assessment and give a reason between 1 and 2000 characters.')
    return {'key': finding['review_key'], 'assessment': assessment, 'reason': reason.strip(), 'reviewed_at': date}
