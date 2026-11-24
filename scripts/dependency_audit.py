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

if __package__:
    from . import advisory_fixes, audit_reviews
else:
    import advisory_fixes, audit_reviews

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


def shipping_components(source, package):
    """Identify package containers from standard installed layouts; paths do not prove runtime calls."""
    if source.get('kind') != 'installed files':
        return ()
    parts = Path(source['path']).parts
    chain = []
    for i, part in enumerate(parts[:-1]):
        if part == 'node_modules':
            name = parts[i + 1]
            if name.startswith('@') and i + 2 < len(parts):
                name += '/' + parts[i + 2]
            if re.fullmatch(r'(?:@[\w-][\w.-]*/)?[\w-][\w.-]*', name):
                chain.append(name)
        if part in ('site-packages', 'dist-packages'):
            container = parts[i + 1]
            if i + 2 < len(parts) and parts[i + 2] in ('_vendor', 'vendor', 'vendored'):
                chain.append(container)
            elif container.endswith('.dist-info') and i + 2 < len(parts) and parts[i + 2] == 'sboms':
                chain.append(container.removesuffix('.dist-info').split('-', 1)[0])
    if chain and chain[-1].casefold() == package.casefold():
        chain.pop()
    return tuple(chain)


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
                advisories = [records[i] for i in ids]
                listed, on_line = advisory_fixes.fixes_for(identity, advisories)
                for field, values in [('fixed_versions', listed), ('line_fixes', on_line),
                                      ('record_hashes', [audit_reviews.digest(record) for record in advisories]),
                                      ('record_modified', [record.get('modified', '') for record in advisories])]:
                    detail[field] = sorted(set(values) | set(detail.get(field, [])))
                source = {'kind': evidence, 'path': location}
                components = shipping_components(source, identity[1])
                if components:
                    source['components'] = list(components)
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


def scanner_rows(scanner, scratch, paths, env, log, errors):
    config = scratch / 'osv-scanner.toml'
    config.write_text('')
    output = scratch / 'report.json'
    cmd = [scanner, 'scan', 'source', '--recursive', '--no-ignore', '--no-resolve', '--allow-no-lockfiles',
           '--all-packages', '--no-call-analysis=rust', '--no-call-analysis=go', '--verbosity=warn',
           '--x-no-default-plugins', '--x-plugins', 'artifact,lockfile,sbom,javascript/packagejson,python/wheelegg', '--config', config,
           '--format=json', '--output-file', output, *paths]
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
    return rows


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
            scanner = mise_output('which', 'osv-scanner')
            rows = scanner_rows(scanner, scratch, [*dict.fromkeys(roots.values()), evidence_dir], env, log, errors)
        summarize_sources(rows, roots, staged, owners)
        affected = [key for key in roots if owners[key]['findings']]
        if affected:
            try:
                outdated = json.loads(mise_output('outdated', '--global', '--json'))
                if not isinstance(outdated, dict):
                    raise ValueError('Invalid Mise outdated result')
            except Exception as exc:
                outdated = None
                errors.append(f'Eligible tool versions unavailable: {exc}')
            for pos, key in enumerate(affected):
                owner = owners[key]
                audit_reviews.sign_findings(owner, roots[key])
                if outdated is None:
                    owner['update'] = {'state': 'unknown'}
                    continue
                candidate = outdated.get(owner['tool'])
                if candidate is None:
                    owner['update'] = {'state': 'current'}
                elif candidate.get('current') != owner['version'] or not candidate.get('latest'):
                    owner['update'] = {'state': 'unknown'}
                else:
                    release = candidate['latest']
                    owner['update'] = {'state': 'eligible', 'version': release}
                    try:
                        check_candidate(mise_output, mise, home, env, log, scanner, scratch / f'candidate-{pos}',
                                        roots[key], owner, release)
                    except Exception as exc:
                        owner['update']['detail'] = f'Candidate dependencies unverified: {exc}'
    count = sum(len(row['findings']) for row in owners.values())
    limited = any(row['coverage'] != 'inventory found' for row in owners.values())
    state = 'advisories' if count else 'limited' if limited else 'clear'
    report = {'state': state, 'owners': owners, 'match_count': count}
    if errors:
        report.update(state='failed', error=' / '.join(errors))
    audit_reviews.apply_local(report, home / '.local/state/mise/maintenance' / audit_reviews.REVIEW_FILE)
    return report


def candidate_outcomes(owner, release, root, rows, staged=None):
    """Require affirmative replacement evidence in every originally matched inventory."""
    def normalized(path, version):
        return re.sub(r'(?<![0-9.])' + re.escape(version) + r'(?![0-9.])', '{tool-version}', str(path))

    packages = {}
    matches = {}
    for row in rows:
        path = Path(row['source']['path'])
        # Recovered binary paths and published locks can establish a continued
        # match. Only corresponding installed inventories verify replacement.
        location = None if path in (staged or {}) else normalized(path.relative_to(root), release)
        for entry in row['packages']:
            p = entry['package']
            key = (p.get('ecosystem'), p.get('name'))
            versions = packages.setdefault((location, *key), set())
            versions.add(p.get('version', ''))
            ids = matches.setdefault(key, set())
            for group in entry.get('groups', []):
                ids.update(group.get('aliases', []))
                ids.update(group.get('ids', []))
            for record in entry.get('vulnerabilities', []):
                ids.update([record['id'], *record.get('aliases', [])])
    for f in owner['findings']:
        key = (f['ecosystem'], f['package'])
        if matches.get(key, set()).intersection([f['id'], *f.get('aliases', [])]):
            f['owner_fix'] = 'still-matches'
            continue
        evidence = set()
        for source in f['sources']:
            path = normalized(source['path'], owner['version'])
            versions = packages.get((path, *key))
            if source['kind'] != 'installed files' or not versions:
                break
            old = advisory_fixes.version_key(key[0], f['version'])
            def passed_fix(version):
                value = advisory_fixes.version_key(key[0], version)
                line = advisory_fixes.release_line(key[0], version)
                return old is not None and value is not None and value > old and any(
                    advisory_fixes.release_line(key[0], fixed) == line
                    and (end := advisory_fixes.version_key(key[0], fixed)) is not None and end <= value
                    for fixed in f.get('fixed_versions', []))
            if not all(passed_fix(v) for v in versions):
                break
            evidence.update(versions)
        else:
            if evidence:
                f.update(owner_fix='verified', replacement_versions=sorted(evidence))


def check_candidate(mise_output, mise, home, env, log, scanner, scratch, installed, owner, release):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.+_-]*', release):
        raise ValueError('Unexpected version identity')
    scratch.mkdir()
    root = installed.parent / release
    if not root.is_dir() or root.is_symlink():
        # Only stage stock, prebuilt registry assets. A preview must not run user
        # hooks, build a tool from source, or activate an alternate host runtime.
        info = json.loads(mise_output('tool', owner['tool'], '--json'))
        backend = info['backend']
        opts = info.get('tool_options', {})
        source = info.get('config_source') or {}
        config_path = Path(source['path']) if source.get('path') else None
        configured = tomllib.loads(config_path.read_text())['tools'].get(owner['tool']) if config_path else None
        stock = isinstance(configured, str) or (isinstance(configured, dict) and set(configured) == {'version'})
        if (not stock or not backend.startswith('aqua:') or any(v for v in opts.values())
                or not re.fullmatch(r'aqua:[\w.-]+/[\w.-]+', backend)):
            owner['update']['detail'] = 'Eligible release found. Its packaged dependencies have not been verified.'
            return
        root = scratch / 'release'
        clean = {k: v for k, v in env.items() if not k.startswith(('MISE_', 'XDG_'))}
        clean.update(HOME=str(scratch), MISE_DATA_DIR=str(scratch / 'mise-data'),
                     MISE_STATE_DIR=str(scratch / 'state'), MISE_CONFIG_DIR=str(scratch / 'config'),
                     MISE_SYSTEM_CONFIG_FILE=str(scratch / 'no-system.toml'), MISE_YES='1',
                     MISE_SELF_UPDATE_AUTO='0', MISE_TOOL_UPDATE_GLOBAL_AUTO='0')
        proc = subprocess.run([mise, '-C', scratch, 'install-into', f'{backend}@{release}', root],
                              cwd=scratch, env=clean, text=True, capture_output=True, timeout=120)
        if proc.returncode:
            raise ValueError(f'Mise could not stage the release (exit {proc.returncode})')
    issues = []
    evidence_dir = scratch / 'inputs'
    evidence_dir.mkdir()
    staged, problems = stage_evidence(root, evidence_dir, Path(env.get('CARGO_HOME') or home / '.cargo'))
    issues.extend(problems)
    rows = scanner_rows(scanner, scratch, [root, evidence_dir], env, log, issues)
    if issues:
        raise ValueError(issues[0])
    candidate_outcomes(owner, release, root, rows, staged)
