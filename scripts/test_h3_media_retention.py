"""Explicit deletions, audit accuracy and loss-free cache compaction; no GPU."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import h3_media_retention as r


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'production').mkdir()
        self.native = self.root/'production/native.mp4'; self.native.write_bytes(b'paid-video')
        self.digest = r.sha256(self.native); self.size = self.native.stat().st_size
        self.history = [{'status': 'succeeded', 'job_id': 'JOB', 'run_id': 'RUN',
                         'adapter': 'h3-colab', 'provider_job_id': '00000000-0000-4000-8000-000000000001',
                         'outputs': [{'path': 'production/native.mp4', 'sha256': self.digest, 'bytes': self.size},
                                     {'path': 'production/master.mp4', 'sha256': self.digest, 'bytes': self.size}]}]
        self.manifest = self.root/'.short-drama/production/retention/cleanup/manifest.json'
        self.manifest.parent.mkdir(parents=True)
        self.record = {'schema_version': 1, 'state': 'completed', 'user_authorized_deletion': True,
                       'deleted_files': [{'path': 'production/master.mp4', 'sha256': self.digest,
                                          'bytes': self.size, 'job_id': 'JOB', 'run_id': 'RUN'}]}
        r.write_json(self.manifest, self.record)
        self.raw_audit = {'status': 'attention', 'outputs': {'missing': 2}, 'problems': [
            {'code': 'output_missing_or_unsafe', 'path': 'production/master.mp4', 'job_id': 'JOB', 'run_id': 'RUN'},
            {'code': 'output_missing_or_unsafe', 'path': 'production/unexpected.mp4', 'job_id': 'OTHER', 'run_id': 'R2'}]}
        def forbidden(*args, **kwargs): raise AssertionError('must not restore or generate')
        self.engine = SimpleNamespace(find_project=lambda p: Path(p).resolve(),
            _read_run_history=lambda root, job_id: self.history if job_id == 'JOB' else [],
            audit_project=lambda root: copy.deepcopy(self.raw_audit),
            job_status=lambda root, job_id: {'job_id': job_id, 'state': 'succeeded'},
            collect_job=forbidden, run_job=forbidden)

    def test_audit_acknowledges_only_exact_authorized_deletion_and_keeps_real_loss(self):
        r.install(self.engine)
        report = self.engine.audit_project(self.root)
        self.assertEqual(report['outputs']['intentionally_deleted'], 1)
        self.assertEqual(report['outputs']['missing'], 1)
        self.assertEqual(report['problems'][0]['path'], 'production/unexpected.mp4')
        self.assertEqual(report['status'], 'attention')

    def test_wrong_hash_in_manifest_cannot_hide_missing_output(self):
        self.record['deleted_files'][0]['sha256'] = '0' * 64
        r.write_json(self.manifest, self.record); r.install(self.engine)
        self.assertEqual(self.engine.audit_project(self.root)['outputs']['intentionally_deleted'], 0)

    def test_planned_but_unexecuted_deletion_is_not_an_excuse_for_missing_files(self):
        self.record['state'] = 'planned'; r.write_json(self.manifest, self.record)
        r.install(self.engine)
        self.assertEqual(self.engine.audit_project(self.root)['outputs']['missing'], 2)

    def test_pruned_job_cannot_be_collected_or_run_again(self):
        r.install(self.engine)
        for action in (self.engine.collect_job, self.engine.run_job):
            with self.assertRaisesRegex(ValueError, 'intentionally_deleted'):
                action(self.root, job_id='JOB', adapter_config=Path('unused'))
        self.assertEqual(self.engine.job_status(self.root, job_id='JOB')['retention']['action'],
                         'do_not_restore_or_regenerate')

    def cache_fixture(self, second_bad=False):
        self.history[0]['outputs'] = self.history[0]['outputs'][:1 if not second_bad else 2]
        cache = self.root/'.short-drama/production/colab/batch/results'; cache.mkdir(parents=True)
        files = []
        for i, output in enumerate(self.history[0]['outputs']):
            name = f'copy-{i}.mp4'; (cache/name).write_bytes(b'paid-video')
            files.append({'target': output['path'], 'cache_name': name,
                          'sha256': self.digest, 'bytes': self.size})
        if second_bad: (self.root/'production/master.mp4').write_bytes(b'corrupt')
        file = cache/(self.history[0]['provider_job_id']+'.json')
        r.write_json(file, {'job_id': 'JOB', 'files': files})
        return cache, file

    def test_verified_commit_releases_duplicate_cache_and_keeps_canonical_recovery_reference(self):
        cache, receipt = self.cache_fixture()
        r.compact_cache(self.engine, self.root, 'JOB')
        self.assertFalse((cache/'copy-0.mp4').exists())
        self.assertEqual(json.loads(receipt.read_text())['files'][0]['retained_path'], 'production/native.mp4')
        self.assertEqual(r.sha256(self.native), self.digest)

    def test_one_bad_canonical_output_preserves_every_recovery_copy(self):
        cache, receipt = self.cache_fixture(second_bad=True)
        with self.assertRaisesRegex(ValueError, 'canonical_output_unverified'):
            r.compact_cache(self.engine, self.root, 'JOB')
        self.assertTrue((cache/'copy-0.mp4').exists())
        self.assertTrue((cache/'copy-1.mp4').exists())
        self.assertNotIn('retained_path', json.loads(receipt.read_text())['files'][0])

    def test_retention_rejects_paths_outside_project_and_symlinks(self):
        with self.assertRaisesRegex(ValueError, 'unsafe_retention_path'):
            r.project_path(self.root, '../outside.mp4')
        (self.root/'production/link.mp4').symlink_to(self.native)
        with self.assertRaisesRegex(ValueError, 'unsafe_retention_path'):
            r.project_path(self.root, 'production/link.mp4')


if __name__ == '__main__': unittest.main()
