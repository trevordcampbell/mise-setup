"""Host dependency discovery uses installed artifacts and never needs a project tree."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import dependency_audit as audit


class DependencyAuditTests(unittest.TestCase):
    def test_locked_registry_receipts_select_arbitrary_crates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = root / '.mise-cargo-install.toml'
            options.write_text('locked = true')
            (root / '.crates2.json').write_text(json.dumps({'installs': {
                'future-cli 2.1.3 (registry+https://github.com/rust-lang/crates.io-index)': {},
                'private 1.0.0 (registry+https://example.test)': {},
                'from-git 1.0.0 (git+https://example.test)': {}}}))
            self.assertEqual(list(audit.locked_crates(root)), [('future-cli', '2.1.3')])
            options.write_text('locked = false')
            self.assertEqual(list(audit.locked_crates(root)), [])

    def test_published_lock_requires_matching_release_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'registry/cache/index.crates.io-abcd/future-1.2.3.crate'
            archive.parent.mkdir(parents=True)
            with tarfile.open(archive, 'w:gz') as f:
                entry = tarfile.TarInfo('future-1.2.3/Cargo.lock')
                entry.size = 7
                f.addfile(entry, io.BytesIO(b'example'))
            meta = {'version': {'crate': 'future', 'num': '1.2.3', 'checksum': hashlib.sha256(archive.read_bytes()).hexdigest()}}
            with patch.object(audit, 'fetch', return_value=json.dumps(meta).encode()):
                self.assertEqual(audit.published_lock('future', '1.2.3', root), b'example')
                archive.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    audit.published_lock('future', '1.2.3', root)

    def test_scan_discovers_future_tools_and_excludes_external_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            store = home / 'data/mise/installs'
            tool = store / 'a/1'
            tool.mkdir(parents=True)
            external = home / 'my-project'
            external.mkdir()
            (tool / 'linked-project').symlink_to(external)
            (external / 'x').write_bytes(b'\x7fELF/registry/src/index.crates.io-abcd/unrelated-1.0.0/src/')
            (tool / 'tool').write_bytes(b'\x7fELF/registry/src/index.crates.io-abcd/any-crate-1.2.3/src/')
            def toolrow(path):
                return [{'version': '1', 'install_path': str(path), 'installed': True}]
            inventory = {'new-name': toolrow(tool), 'alias': toolrow(tool), 'linked': toolrow(external)}
            def fake_run(cmd, **kwargs):
                if 'ls' in cmd:
                    self.assertIn('--global', cmd)
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(inventory), '')
                if 'which' in cmd:
                    return subprocess.CompletedProcess(cmd, 0, '/scanner', '')
                self.assertNotIn(external, cmd)
                temp = Path(cmd[-1])
                sboms = list(temp.rglob('*.cdx.json'))
                self.assertEqual(len(sboms), 1)
                component, = json.loads(sboms[0].read_text())['components']
                self.assertEqual(component['purl'], 'pkg:cargo/any-crate@1.2.3')
                pkg = {'package': {'ecosystem': 'crates.io', 'name': 'any-crate', 'version': '1.2.3'}}
                payload = {'results': [{'source': {'type': 'sbom', 'path': str(sboms[0])}, 'packages': [pkg]}]}
                Path(cmd[cmd.index('--output-file') + 1]).write_text(json.dumps(payload))
                return subprocess.CompletedProcess(cmd, 0, '', '')
            with patch.object(audit.subprocess, 'run', side_effect=fake_run):
                result = audit.scan(Path('/mise'), home, {'XDG_DATA_HOME': str(home/'data')}, io.StringIO())
            self.assertEqual(result['owners']['new-name@1']['coverage'], 'partial')
            self.assertEqual(result['owners']['new-name@1']['package_count'], 1)
            self.assertIn('Same installation', result['owners']['alias@1']['coverage_note'])
            self.assertIn('outside', result['owners']['linked@1']['coverage_note'])

    def test_components_follow_evidence_for_arbitrary_bundles_and_scoped_packages(self):
        cases = [
            ('lib/dist-packages/sample/_vendor/bom.cdx.json', 'urllib3', ('sample',)),
            ('lib/site-packages/foo_bar-1.2.dist-info/sboms/contents.cdx.json', 'pkg', ('foo_bar',)),
            ('lib/node_modules/@org/app/node_modules/nested/node_modules/@team/pkg/package.json',
             '@team/pkg', ('@org/app', 'nested')),
            ('lib/node_modules/@team/pkg/package.json', '@team/pkg', ()),
            ('bin/pnpm', 'helper', ()),
        ]
        for path, package, expected in cases:
            with self.subTest(path=path):
                src = {'kind': 'installed files', 'path': path}
                self.assertEqual(audit.shipping_components(src, package), expected)
                self.assertEqual(audit.shipping_components(dict(src, kind='published lockfile'), package), ())

    def test_aliases_multiple_copies_and_partial_evidence_do_not_inflate_findings(self):
        root = Path('/tools/example/1')
        pkg = {'package': {'name': 'helper', 'version': '1.0.0', 'ecosystem': 'crates.io'},
               'groups': [{'ids': ['A', 'B'], 'aliases': ['A', 'B']}],
               'vulnerabilities': [{'id': 'A', 'summary': 'Example', 'affected': [
                   {'package': {'name': 'helper', 'ecosystem': 'crates.io'}, 'ranges': [
                       {'type': 'SEMVER', 'events': [{'fixed': '1.0.1'}]}]}]}, {'id': 'B'}]}
        def row(path, kind):
            return {'source': {'path': str(path), 'type': kind}, 'packages': [pkg]}
        embedded = root / 'bin/a'
        guess = Path('/tmp/generated.cdx.json')
        owners = {'tool': {'evidence': set()}}
        audit.summarize_sources([row(embedded, 'artifact'), row(root/'bin/b', 'artifact'), row(guess, 'sbom')],
                                {'tool': root}, {guess: ('tool', 'binary paths (partial)', 'bin/a')}, owners)
        result = owners['tool']
        finding, = result['findings']
        self.assertEqual(result['package_count'], 1)
        self.assertEqual(result['evidence'], ['installed files'])
        self.assertEqual(len(finding['sources']), 2)
        self.assertEqual(finding['fixed_versions'], ['1.0.1'])
        self.assertEqual(finding['aliases'], ['A', 'B'])
        with self.assertRaisesRegex(ValueError, 'Unattributed'):
            audit.summarize_sources([row(Path('/project/test'), 'lockfile')], {'tool': root}, {}, owners)
