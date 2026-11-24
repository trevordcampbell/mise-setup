"""Read OSV intervals, preserving uncertainty for unsupported version schemes."""
import re


def version_key(ecosystem, value):
    if value == '0':
        return (0,)
    if ecosystem == 'PyPI':
        try:
            from pip._vendor.packaging.version import InvalidVersion, Version
        except ImportError:
            return None
        try:
            return (1, Version(value))
        except InvalidVersion:
            return None
    if ecosystem not in ('npm', 'crates.io', 'Go'):
        return None
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?', value)
    if not match:
        return None
    parts = tuple(map(int, match.group(1, 2, 3)))
    pre = match[4]
    return (1, parts, pre is None, tuple((0, int(s)) if s.isdigit() else (1, s) for s in (pre or '').split('.')))


def release_line(ecosystem, value):
    k = version_key(ecosystem, value)
    if not k or len(k) == 1:
        return None
    parts = k[1].release if ecosystem == 'PyPI' else k[1]
    return parts[:2] if parts[0] == 0 else parts[:1]


def fixes_for(identity, vulnerabilities):
    eco, package, installed = identity
    current = version_key(eco, installed)
    listed, applicable = set(), set()
    for advisory in vulnerabilities:
        for aff in advisory.get('affected', []):
            meta = aff.get('package', {})
            if (meta.get('ecosystem'), meta.get('name')) != (eco, package):
                continue
            for rng in aff.get('ranges', []):
                if rng['type'] not in ('SEMVER', 'ECOSYSTEM'):
                    continue
                events = rng['events']
                listed.update(e['fixed'] for e in events if 'fixed' in e)
                # OSV events can be unordered. Each boundary ends the preceding interval.
                bounds = [(version_key(eco, val), kind, val) for event in events for kind, val in event.items()]
                if not current or any(key is None for key, _, _ in bounds):
                    continue
                start = None
                for key, kind, val in sorted(bounds, key=lambda b: (b[0], b[1] == 'introduced')):
                    if kind == 'introduced':
                        start = key
                    else:
                        if kind == 'fixed' and start is not None and start <= current < key:
                            applicable.add(val)
                        start = None
    line = release_line(eco, installed)
    on_line = {v for v in applicable if line is not None and release_line(eco, v) == line}
    return sorted(listed), sorted(on_line, key=lambda v: version_key(eco, v))
