import copy
import io
import json
from pathlib import Path
import platform
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import advisory_fixes
import audit_reviews
import dependency_audit
import maintenance


def record(events, eco='crates.io'):
    return {'affected': [{'package': {'name': 'lib', 'ecosystem': eco},
                          'ranges': [{'type': 'ECOSYSTEM', 'events': events}]}]}


def finding():
    return {'id': 'OSV-A', 'aliases': ['OTHER'], 'package': 'lib', 'version': '7.2.4',
            'ecosystem': 'crates.io', 'sources': [{'kind': 'installed files', 'path': 'bin/thing'}],
            'record_hashes': ['old'], 'fixed_versions': ['7.4.0'], 'line_fixes': ['7.4.0']}


def owner():
    return {'tool': 'demo', 'version': '1.0.0', 'coverage': 'inventory found', 'findings': [finding()]}


class ReleaseTests(unittest.TestCase):
    def test_current_interval_and_version_line(self):
        events = [{'fixed': '8.2.0'}, {'introduced': '7.0.0'}, {'fixed': '7.4.0'}, {'introduced': '8.0.0-0'}]
        for v, line in [('7.2.4', ['7.4.0']), ('8.0.0', ['8.2.0']), ('7.5.0', []), ('7.4.0', [])]:
            self.assertEqual(advisory_fixes.fixes_for(('crates.io', 'lib', v), [record(events)])[1], line)
        events = [{'introduced': '0'}, {'last_affected': '1.0.0'}, {'introduced': '2.0.0'}, {'fixed': '2.2.0'}]
        self.assertEqual(advisory_fixes.fixes_for(('npm','lib','1.0.0'),[record(events,'npm')])[1], [])
        for eco, old, fixed in [('Go','0.2.3-0.20240110-a','0.2.3'), ('PyPI','1.0rc1','1.0.post1')]:
            self.assertEqual(advisory_fixes.fixes_for((eco, 'lib', old), [record([{'introduced':'0'},{'fixed':fixed}],eco)])[1], [fixed])
        self.assertEqual(advisory_fixes.fixes_for(('other','lib','8.0.0'),[record(events,'other')])[1], [])

    def test_owner_update_needs_positive_source_and_clean_advisory_result(self):
        root = Path('/candidate')
        def classify(pkg='lib', path='bin/thing', versions=('7.4.0',), ids=(), kind='installed files'):
            o = owner()
            o['findings'][0]['sources'][0]['kind'] = kind
            rows = [{'source': {'path': str(root/path)}, 'packages': [
                {'package': {'name':pkg,'ecosystem':'crates.io','version':v},
                 'vulnerabilities':[{'id':i} for i in ids]} for v in versions]}]
            dependency_audit.candidate_outcomes(o, '1.1.0', root, rows)
            return o['findings'][0].get('owner_fix')
        self.assertEqual(classify(), 'verified')
        self.assertEqual(classify(ids=['OTHER']), 'still-matches')
        for kwargs in ({'pkg':'different'}, {'path':'bin/else'}, {'versions':('7.2.4','7.4.0')},
                       {'kind':'binary paths (partial)'}, {'versions':('unknown',)}, {'versions':('7.3.0',)}):
            self.assertIsNone(classify(**kwargs))


class ReviewTests(unittest.TestCase):
    def test_signatures_invalidate_on_each_review_input(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); binary = root/'bin'; binary.write_bytes(b'build')
            o = owner()
            audit_reviews.sign_findings(o, root)
            f=o['findings'][0]
            token=f['review_token']
            entries={token:audit_reviews.entry(f,'outside','Verified excluded feature','today')}
            def status(current):
                audit_reviews.sign_findings(current, root)
                report={'owners':{'demo':current}}
                audit_reviews.apply(report,entries)
                return current['findings'][0]['review']['assessment']
            self.assertEqual(status(copy.deepcopy(o)), 'outside')
            for target, key, val in [('owner','version','1.0.1'),('finding','version','7.2.5'),
                                     ('finding','record_hashes',['new']),('finding','sources',[])]:
                changed=copy.deepcopy(o)
                (changed if target=='owner' else changed['findings'][0])[key]=val
                self.assertEqual(status(changed),'changed')
            with patch.object(platform,'machine',return_value='other-arch'):
                self.assertEqual(status(copy.deepcopy(o)),'changed')
            binary.write_bytes(b'another build')
            self.assertEqual(status(copy.deepcopy(o)),'changed')

    def test_record_is_atomic_local_and_stale_tokens_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(maintenance,'STATE',Path(tmp)):
            root=Path(tmp); o=owner(); build=root/'tool'; build.mkdir()
            audit_reviews.sign_findings(o,build)
            f=o['findings'][0]; token=f['review_token'][:16]
            scan={'state':'advisories','owners':{'demo':o}}
            path=root/'last-run.json'
            path.write_text(json.dumps({'finished_at':'2026-11-24','results':{'dependency-audit':scan}}))
            self.assertEqual(maintenance.record_review(token,'relevant','Observed caller'),f['id'])
            entries=audit_reviews.load(root/audit_reviews.REVIEW_FILE)
            audit_reviews.apply(scan,entries)
            self.assertEqual(scan['review_counts'],{'relevant':1})
            self.assertEqual((root/audit_reviews.REVIEW_FILE).stat().st_mode & 0o777,0o600)
            with self.assertRaises(ValueError):
                maintenance.record_review(token,'wrong','some reason')
            (root/'last-check.json').write_text(json.dumps({'finished_at':'2026-11-25','results':{'dependency-audit':{'state':'failed'}}}))
            with self.assertRaises(ValueError):
                maintenance.record_review(token,'outside','invalid scan')

    def test_summary_highlights_verified_fixes_and_reopened_reviews(self):
        o=owner(); f=o['findings'][0]; f.update(owner_fix='verified',replacement_versions=['7.4.0'],review={'assessment':'changed'})
        o['update']={'state':'eligible','version':'2.0.0'}
        lines=maintenance.review_overview({'review_counts':{'changed':1},'owners':{'demo':o}})
        self.assertIn('Verified update available',lines[0])
        self.assertIn('Reopened',lines[1])
        self.assertIn('lib',lines[1])
        self.assertIn('ships 7.4.0',maintenance.owner_fix_text(o,f))
