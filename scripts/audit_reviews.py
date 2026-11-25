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
EVIDENCE_LABELS = {'tool_version': 'tool version', 'build': 'installed build', 'dependency_version': 'dependency version',
                   'advisory': 'advisory record', 'sources': 'evidence sources', 'coverage': 'scan coverage',
                   'platform': 'platform'}


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
    owner['review_target'] = target
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
        if 'evidence' in record:
            evidence = record['evidence']
            if not isinstance(evidence, dict) or not all(isinstance(evidence.get(k), str)
                    for k in EVIDENCE_LABELS if k != 'platform'):
                raise ValueError(f'Invalid review evidence {token[:16]}')
    return data['entries']


def review_evidence(owner, finding):
    """Comparable inputs to review_token; omit platform data absent in older reports."""
    evidence = {'tool_version': owner['version'], 'build': owner['build_sha256'], 'dependency_version': finding['version'],
                'advisory': digest(sorted(finding['record_hashes'])),
                'sources': digest(sorted(finding['sources'], key=lambda s: (s['kind'], s['path']))),
                'coverage': digest([owner['coverage'], owner.get('unresolved_sources', [])])}
    if 'review_target' in owner:
        evidence['platform'] = owner['review_target']
    return evidence


def recover_evidence(entries, directory):
    missing = {token for token, row in entries.items() if 'evidence' not in row}
    if not missing:
        return False
    recovered = False
    for path in sorted(directory.glob('*.json'), reverse=True):
        if not missing:
            break
        try:
            archived = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        for owner in archived.get('results', {}).get('dependency-audit', {}).get('owners', {}).values():
            for finding in owner.get('findings', []):
                token = finding.get('review_token')
                if token in missing and entries[token]['key'] == finding.get('review_key'):
                    try:
                        entries[token]['evidence'] = review_evidence(owner, finding)
                    except (KeyError, TypeError):
                        continue  # An incomplete legacy snapshot cannot explain the review.
                    missing.remove(token)
                    recovered = True
    return recovered


def apply(report, entries):
    previous = {}
    for entry in sorted(entries.values(), key=lambda r: r['reviewed_at']):
        previous[entry['key']] = entry
    counts = Counter()
    for owner in report['owners'].values():
        for f in owner['findings']:
            token = f.get('review_token')
            entry = entries.get(token)
            if entry and entry['key'] == f['review_key'] and entry['assessment'] in ASSESSMENTS:
                f['review'] = {k: entry[k] for k in ('assessment', 'reason', 'reviewed_at')}
            elif prior := previous.get(f.get('review_key')):
                review = f['review'] = {'assessment': 'changed', 'previous': {
                    k: prior[k] for k in ('assessment', 'reason', 'reviewed_at')}}
                if 'evidence' in prior:
                    old, current = prior['evidence'], review_evidence(owner, f)
                    review['previous']['tool_version'] = old['tool_version']
                    review['changed_inputs'] = [k for k in EVIDENCE_LABELS if k in old and k in current and old[k] != current[k]]
                    review['uncompared_inputs'] = [k for k in EVIDENCE_LABELS if k not in old or k not in current]
                    if 'dependency_version' in review['changed_inputs']:
                        review['previous']['dependency_version'] = old['dependency_version']
            else:
                f['review'] = {'assessment': 'unreviewed'}
            counts[f['review']['assessment']] += 1
    report['review_counts'] = dict(counts)


def apply_local(report, path, *, persist=None):
    try:
        entries = load(path)
    except (OSError, ValueError, AttributeError) as error:
        # A broken local annotation file must not hide any scanner findings.
        entries = {}
        report['review_error'] = str(error)
    else:
        report.pop('review_error', None)
        recovered = recover_evidence(entries, path.parent / 'history')
        if recovered and persist is not None:
            # The maintenance writer already holds update.lock. Status readers never write.
            try:
                persist(path, json.dumps({'version': 1, 'entries': entries}, indent=2) + '\n')
            except OSError as error:
                report['review_error'] = f'Could not save historical review evidence: {error}'
    apply(report, entries)


def entry(finding, assessment, reason, date, *, owner=None):
    if assessment not in ASSESSMENTS or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise ValueError('Choose a listed assessment and give a reason between 1 and 2000 characters.')
    record = {'key': finding['review_key'], 'assessment': assessment, 'reason': reason.strip(), 'reviewed_at': date}
    if owner is not None:
        record['evidence'] = review_evidence(owner, finding)
    return record
