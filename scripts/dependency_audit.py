"""Read-only dependency inventory for the active, user-managed Mise install roots."""
import hashlib
import io
import json
import mmap
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import tomllib
from urllib.parse import quote
from urllib.request import Request, urlopen

PUBLIC_REGISTRIES = {'registry+https://github.com/rust-lang/crates.io-index', 'sparse+https://index.crates.io/'}
REGISTRY_SOURCE = re.compile(
    rb'registry[/\\]src[/\\](?:index\.crates\.io-[a-f0-9]+|github\.com-1ecc6299db9ec823)[/\\]'
    rb'([a-zA-Z0-9_-]{1,64}?)-(\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.+-]+)?)[/\\]')
NATIVE_MAGIC = {bytes.fromhex(m) for m in (
    '7f454c46', 'cffaedfe', 'feedfacf', 'cefaedfe', 'feedface', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def fetch(url, limit):
    with urlopen(Request(url, headers={'User-Agent': 'mise-host-dependency-audit'}), timeout=25) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'Download exceeds the audit size limit: {url}')
    return data


def locked_crates(root):
    options, receipts = root / '.mise-cargo-install.toml', root / '.crates2.json'
    if not (options.is_file() and receipts.is_file()) or options.is_symlink() or receipts.is_symlink():
        return
    if tomllib.loads(options.read_text()).get('locked') is not True:
        return
    for key in json.loads(receipts.read_text())['installs']:
        match = re.fullmatch(r'([A-Za-z0-9_-]+) (\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]+)?) \(([^)]+)\)', key)
        if match and match[3] in PUBLIC_REGISTRIES:
            yield match[1], match[2]


def published_lock(name, version, cargo_home):
    crate = f'{name}-{version}'
    release = json.loads(fetch(f'https://crates.io/api/v1/crates/{name}/{version}', 2_000_000))['version']
    if release['crate'] != name or release['num'] != version or not re.fullmatch(r'[a-f0-9]{64}', release['checksum']):
        raise ValueError('Unexpected crates.io release metadata')
    cache = cargo_home / 'registry/cache'
    archive = next((p for prefix in ('index.crates.io-', 'github.com-')
                    for p in cache.glob(f'{prefix}*/{crate}.crate') if p.is_file()), None)
    if archive is None:
        data = fetch(f'https://static.crates.io/crates/{name}/{crate}.crate', 64 << 20)
    else:
        with archive.open('rb') as handle:
            data = handle.read((64 << 20) + 1)
    if hashlib.sha256(data).hexdigest() != release['checksum']:
        raise ValueError(f'Crate checksum mismatch for {crate}')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r|gz') as package:
        for member in package:
            if member.name == f'{crate}/Cargo.lock':
                if not member.isfile() or member.size > 16 << 20:
                    raise ValueError('Invalid packaged Cargo.lock')
                return package.extractfile(member).read()
    return None


def native_crate_paths(path):
    # Only package identities from public Cargo registry paths. These strings can
    # survive stripping but are partial evidence, never a complete dependency graph.
    with path.open('rb') as handle:
        magic = handle.read(4)
        if magic not in NATIVE_MAGIC and magic[:2] != b'MZ':
            return set()
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as image:
            return {(a.decode(), b.decode()) for a, b in REGISTRY_SOURCE.findall(image)}


def stage_evidence(root, dest, cargo_home):
    sources = {}
    errors = []
    for n, (crate, version) in enumerate(locked_crates(root)):
        try:
            data = published_lock(crate, version, cargo_home)
            if data is None:
                errors.append(f'{crate} {version} has no published lockfile')
                continue
            path = dest / f'crate-{n}/Cargo.lock'
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
            sources[path] = ('published lockfile', f'{crate} {version} / Cargo.lock')
        except Exception as error:
            errors.append(f'{crate} {version}: {error}')
    def walk_error(error):
        raise error
    for folder, _, files in os.walk(root, onerror=walk_error, followlinks=False):
        for filename in files:
            path = Path(folder) / filename
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            pairs = native_crate_paths(path)
            if pairs:
                sbom = dest / f'recovered-{len(sources)}.cdx.json'
                sbom.write_text(json.dumps({
                    'bomFormat': 'CycloneDX', 'specVersion': '1.5', 'version': 1,
                    'components': [{'type': 'library', 'name': n, 'version': v, 'purl': f'pkg:cargo/{n}@{quote(v)}'}
                                   for n, v in sorted(pairs)]}))
                sources[sbom] = ('binary paths (partial)', str(path.relative_to(root)))
    return sources, errors


def summarize_sources(rows, roots, staged, owners):
    inventories = {owner: set() for owner in owners}
    matches = {owner: {} for owner in owners}
    # If OSV found embedded Rust metadata in this binary, prefer its package list.
    embedded = {Path(row['source']['path']) for row in rows if row['source']['type'] == 'artifact'
                and any(p['package'].get('ecosystem') == 'crates.io' for p in row['packages'])}
    for row in rows:
        path = Path(row['source']['path'])
        if path in staged:
            owner, evidence, location = staged[path]
            if evidence == 'binary paths (partial)' and roots[owner] / location in embedded:
                continue
        else:
            owner = next((key for key, root in roots.items() if path.is_relative_to(root)), None)
            if owner is None:
                raise ValueError(f'Unattributed scanner source: {path}')
            evidence, location = 'installed files', str(path.relative_to(roots[owner]))
        for pkg in row['packages']:
            p = pkg['package']
            identity = (p.get('ecosystem', ''), p.get('name', ''), p.get('version', '') or p.get('commit', ''))
            if not all(identity):
                owners[owner].setdefault('unresolved_sources', []).append(location)
                continue
            inventories[owner].add(identity)
            owners[owner]['evidence'].add(evidence)
            records = {v['id']: v for v in pkg.get('vulnerabilities', [])}
            groups = pkg.get('groups') or [{'ids': [i]} for i in records]
            for group in groups:
                ids = sorted(group['ids'])
                v = records[ids[0]]
                key = (*identity, ids[0])
                detail = matches[owner].setdefault(key, {
                    'ecosystem': identity[0], 'package': identity[1], 'version': identity[2], 'id': ids[0],
                    'aliases': group.get('aliases', ids), 'severity': group.get('max_severity', ''),
                    'summary': v.get('summary') or '', 'url': 'https://osv.dev/vulnerability/' + ids[0],
                    'sources': []})
                fixes = {event['fixed'] for identifier in ids for aff in records[identifier].get('affected', [])
                         if (aff.get('package') or {}).get('name') == identity[1]
                         and aff['package'].get('ecosystem') == identity[0] for rng in aff.get('ranges', [])
                         if rng.get('type') in ('SEMVER', 'ECOSYSTEM') for event in rng['events'] if 'fixed' in event}
                detail['fixed_versions'] = sorted(fixes | set(detail.get('fixed_versions', [])))
                source = {'kind': evidence, 'path': location}
                if source not in detail['sources']:
                    detail['sources'].append(source)
    for owner, record in owners.items():
        record['packages'] = [dict(ecosystem=e, name=n, version=v) for e, n, v in sorted(inventories[owner])]
        record['evidence'] = sorted(record['evidence'])
        record['package_count'] = len(inventories[owner])
        record['findings'] = [matches[owner][k] for k in sorted(matches[owner])]
        if 'unresolved_sources' in record:
            record['unresolved_sources'] = sorted(set(record['unresolved_sources']))
        record['coverage'] = ('no inventory' if not record['package_count'] else 'partial'
                              if record.get('unresolved_sources') or record['evidence'] == ['binary paths (partial)']
                              else 'inventory found')
        if not record['package_count'] and not record.get('coverage_note'):
            record['coverage_note'] = 'No identifiable dependency inventory'


def scan(mise, home, env, log):
    def mise_output(*args):
        proc = subprocess.run([mise, '-C', home, *args], cwd=home, env=env, text=True, capture_output=True, timeout=30)
        if proc.returncode:
            raise RuntimeError(f"Mise {' '.join(args)} failed. {proc.stderr.strip()}")
        return proc.stdout.strip()
    active = json.loads(mise_output('ls', '--global', '--current', '--json'))
    store = (Path(env.get('XDG_DATA_HOME') or home / '.local/share') / 'mise/installs').resolve()
    cargo_home = Path(env.get('CARGO_HOME') or home / '.cargo')
    roots, owners, staged, errors = {}, {}, {}, []
    with tempfile.TemporaryDirectory(prefix='mise-dependency-audit-') as tmp:
        scratch = Path(tmp).resolve()
        evidence_dir = scratch / 'inputs'
        evidence_dir.mkdir()
        for tool, versions in active.items():
            for r in versions:
                owner = f"{tool}@{r['version']}"
                record = owners[owner] = {'tool': tool, 'version': r['version'], 'evidence': set()}
                if not r.get('installed'):
                    record['coverage_note'] = 'Version is not installed'
                    continue
                root = Path(r['install_path']).resolve()
                if not root.is_relative_to(store) or root == store:
                    record['coverage_note'] = 'Linked outside the Mise install store'
                    continue
                if not root.is_dir():
                    errors.append(f'{owner}: install directory missing')
                    continue
                shared = next((name for name, existing in roots.items() if existing == root), None)
                if shared:
                    record['coverage_note'] = f'Same installation as {shared}'
                    continue
                roots[owner] = root
                dest = evidence_dir / str(len(owners))
                dest.mkdir()
                try:
                    sources, problems = stage_evidence(root, dest, cargo_home)
                    staged.update({path: (owner, *source) for path, source in sources.items()})
                    errors.extend(f'{owner}: {issue}' for issue in problems)
                except Exception as error:
                    errors.append(f'{owner}: {error}')

        rows = []
        if roots:
            config = scratch / 'osv-scanner.toml'
            config.touch()
            output = scratch / 'report.json'
            scanner = mise_output('which', 'osv-scanner')
            cmd = [scanner, 'scan', 'source', '--recursive', '--no-ignore', '--no-resolve', '--allow-no-lockfiles',
                   '--all-packages', '--no-call-analysis=rust', '--no-call-analysis=go', '--verbosity=warn',
                   '--x-no-default-plugins', '--x-plugins', 'artifact,lockfile,sbom,javascript/packagejson,python/wheelegg', '--config', config,
                   '--format=json', '--output-file', output, *dict.fromkeys(roots.values()), evidence_dir]
            result = subprocess.run(cmd, cwd=scratch, env=env, stdin=subprocess.DEVNULL, text=True,
                                    capture_output=True, timeout=180)
            log.write(result.stderr + '\n')
            if result.returncode not in (0, 1):
                raise RuntimeError(f'OSV-Scanner exited {result.returncode}. {result.stderr.strip()[-500:]}')
            if result.stderr.strip():
                errors.append('OSV-Scanner reported diagnostics: ' + result.stderr.strip()[-500:])
            rows = json.loads(output.read_text())['results']
            if not isinstance(rows, list):
                raise ValueError('OSV-Scanner returned an invalid source list')
            has_findings = any(pkg.get('vulnerabilities') for row in rows for pkg in row['packages'])
            if bool(result.returncode) != has_findings:
                raise ValueError('OSV-Scanner exit status disagrees with its vulnerability report')
        summarize_sources(rows, roots, staged, owners)
    count = sum(len(row['findings']) for row in owners.values())
    limited = any(row['coverage'] != 'inventory found' for row in owners.values())
    state = 'advisories' if count else 'limited' if limited else 'clear'
    report = {'state': state, 'owners': owners, 'match_count': count}
    if errors:
        report.update(state='failed', error=' / '.join(errors))
    return report
