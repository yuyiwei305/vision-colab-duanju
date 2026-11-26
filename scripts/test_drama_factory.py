import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from drama_factory import core, planning, runtime


PROMPT = ('subject_definitions: S1\nsummary: person talks\nretention_analysis: identity\n'
          'detailed_description: person talks\noverall_soundscape: wind\nnon_diegetic_music: N/A')


class FactoryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = {'format': {'aspect_ratio': '9:16'}, 'creator_authority': {
            'production_profile': {'status': 'accepted', 'choices': {'video_prompt_dialect': 'minimax-h3'}}}}
        core.write_json(self.root / 'short-drama.json', self.config)
        (self.root / 'ref.png').write_bytes(b'fixture-reference')

    def motion_document(self, count=1, parents=None, voices=None, episode='EP001'):
        path = self.root / f'剧集/{episode}/视频提示词.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        parents, voices = parents or {}, voices or {}
        rows = []
        for index in range(1, count + 1):
            entry = f'MOTION-{episode}-{index:03}'
            rows.append(f'## {entry} · 测试\n- 分镜：SHOT-{episode}-{index:03}\n- 时长：6s\n'
                        f'- 续接来源：{parents.get(index, "无")}\n'
                        '- 输入参考图：REF-HERO（顺序：1）· ref.png《人物板》（用途：身份；控制：身份；不得控制：构图）\n'
                        + (f'- 原生音色参考：{voices[index]}\n' if index in voices else '')
                        + '### 可复制提示词\n' + '\n'.join('> ' + line for line in PROMPT.splitlines()) + '\n')
        path.write_text('\n'.join(rows))
        return path


class SpecTests(FactoryFixture):
    def test_frozen_episode_survives_other_episode_orientation_change(self):
        one = core.freeze_spec(self.root, 'EP001', authorization='user requested portrait')
        config = core.read_json(self.root / 'short-drama.json')
        config['format']['aspect_ratio'] = '16:9'
        core.write_json(self.root / 'short-drama.json', config)
        two = core.freeze_spec(self.root, 'EP002', authorization='user requested landscape')
        self.assertEqual(core.resolve_spec(self.root, 'EP001'), one)
        self.assertEqual(one['production']['native_canvas'], [768, 1344])
        self.assertEqual(two['production']['native_canvas'], [1344, 768])

    def test_budget_cannot_be_inherited_via_episode_spec(self):
        with self.assertRaisesRegex(core.FactoryError, 'invalid_episode_spec'):
            core.resolve_spec(self.root, 'EP001', override={'max_ccu': 50}, authorization='scope')

    def test_crop_requires_explicit_policy_and_override_requires_authority(self):
        with self.assertRaisesRegex(core.FactoryError, 'authorization'):
            core.resolve_spec(self.root, 'EP001', override={'delivery': {'fps': 60}})
        with self.assertRaisesRegex(core.FactoryError, 'fit_policy'):
            core.resolve_spec(self.root, 'EP001', override={'delivery': {'canvas': [1080, 1920]}}, authorization='scope')


class RevisionTests(FactoryFixture):
    def test_concurrent_editor_is_rejected_and_old_full_text_retained(self):
        name = '剧集/EP001/剧本.md'
        core.revise(self.root, {name: 'before'}, {name: None}, reason='create')
        old_sha = core.file_sha(self.root / name)
        receipt = core.revise(self.root, {name: 'after'}, {name: old_sha}, reason='edit')
        with self.assertRaisesRegex(core.FactoryError, 'source_revision_changed'):
            core.revise(self.root, {name: 'concurrent'}, {name: old_sha}, reason='edit')
        saved = self.root / core.LEDGER / 'revisions' / receipt['id'] / '0-before'
        self.assertEqual(saved.read_text(), 'before')

    def test_interrupted_multifile_commit_recovers_without_partial_binding(self):
        names = ['剧集/EP001/分镜.md', '剧集/EP001/视频提示词.md']
        core.revise(self.root, {n: 'old' for n in names}, {n: None for n in names}, reason='create')
        original = core.atomic_bytes
        calls = 0
        def interrupted(path, raw, **kwargs):
            nonlocal calls
            if str(path).endswith(tuple(names)):
                calls += 1
                if calls == 2:
                    raise RuntimeError('power_loss')
            return original(path, raw, **kwargs)
        with patch.object(core, 'atomic_bytes', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'power_loss'):
                core.revise(self.root, {n: 'new' for n in names},
                            {n: core.file_sha(self.root / n) for n in names}, reason='bind')
        core.recover_revisions(self.root)
        self.assertEqual([(self.root / n).read_text() for n in names], ['new', 'new'])

    def test_project_path_rejects_escape(self):
        with self.assertRaises(core.FactoryError):
            core.project_path(self.root, '../outside')


class PlanTests(FactoryFixture):
    def test_43_motions_are_preserved_and_split_into_finite_batches(self):
        self.motion_document(43)
        value = planning.plan(self.root, 'EP001')
        batches = value['schedule']['batches']
        self.assertEqual(len(value['motions']), 43)
        self.assertEqual(sum(len(b['entries']) for b in batches), 43)
        self.assertTrue(all(len(b['entries']) <= 32 for b in batches))
        self.assertEqual(value['schedule']['instances'], 2)
        self.assertEqual(value['schedule']['requested_instances'], 4)

    def test_voice_and_latent_dependencies_stay_in_one_partition(self):
        self.motion_document(24, parents={3: 'MOTION-EP001-002'},
                             voices={2: 'MOTION-EP001-001/HERO/0/2'})
        value = planning.plan(self.root, 'EP001')
        batches = value['schedule']['batches']
        group = next(b['entries'] for b in batches if 'MOTION-EP001-001' in b['entries'])
        for entry in ['MOTION-EP001-002', 'MOTION-EP001-003']:
            self.assertIn(entry, group)
        self.assertLess(group.index('MOTION-EP001-001'), group.index('MOTION-EP001-002'))

    def test_cycle_and_missing_voice_source_fail_before_allocation(self):
        self.motion_document(2, voices={1: 'MOTION-EP001-002/HERO/0/2', 2: 'MOTION-EP001-001/HERO/0/2'})
        with self.assertRaisesRegex(core.FactoryError, 'cycle'):
            planning.plan(self.root, 'EP001')
        self.motion_document(1, voices={1: 'MOTION-EP001-002/HERO/0/2'})
        with self.assertRaises(ValueError):
            planning.plan(self.root, 'EP001')

    def test_selected_child_includes_complete_chain_and_audio_source(self):
        self.motion_document(3, parents={3: 'MOTION-EP001-002'}, voices={2: 'MOTION-EP001-001/HERO/0/2'})
        value = planning.plan(self.root, 'EP001', only=['MOTION-EP001-003'])
        self.assertEqual(value['dependency_added_entries'], ['MOTION-EP001-001', 'MOTION-EP001-002'])

    def test_plan_has_full_snapshot_and_detects_asset_changes(self):
        self.motion_document(1)
        value = planning.plan(self.root, 'EP001')
        snapshot = self.root / core.LEDGER / 'plans' / value['plan_id'] / 'sources/剧集/EP001/视频提示词.md'
        self.assertTrue(snapshot.is_file())
        planning.validate_plan(self.root, value)
        (self.root / 'ref.png').write_bytes(b'changed')
        with self.assertRaisesRegex(core.FactoryError, 'reference_changed'):
            planning.validate_plan(self.root, value)

    def test_local_prompt_change_does_not_invalidate_unrelated_motion(self):
        path = self.motion_document(3, parents={2: 'MOTION-EP001-001'})
        old = planning.plan(self.root, 'EP001')
        path.write_text(path.read_text().replace('summary: person talks', 'summary: person whispers', 1))
        new = planning.plan(self.root, 'EP001')
        diff = planning.impact(old, new)
        self.assertEqual(diff['affected_motions'], ['MOTION-EP001-001', 'MOTION-EP001-002'])
        self.assertEqual(diff['unchanged_motions'], ['MOTION-EP001-003'])

    def test_missing_script_produces_host_work_order_not_fake_completion(self):
        value = planning.plan(self.root, 'EP001', goal='script')
        self.assertEqual(value['stages'][0]['state'], 'host_work_required')
        self.assertEqual(value['motions'], [])


class TaskV2Tests(FactoryFixture):
    def prepare_task(self, count=22):
        import colab_h3 as h
        self.motion_document(count)
        value = planning.plan(self.root, 'EP001')
        runtime.create(self.root, 'fixture', value, authorization='test production scope', max_ccu=50, max_seconds=7200)
        with patch('h3_production.guard', return_value={}):
            wave = runtime.append_wave(self.root, 'fixture', value, reason='initial')
        return h, value, wave, [core.read_json(p) for p in wave['batches']]

    def test_two_partitions_share_contract_and_register_without_gpu(self):
        h, value, wave, batches = self.prepare_task()
        self.assertEqual(len(batches), 2)
        for batch in batches:
            _, task, members = h.task_members(batch)
            self.assertEqual(task['max_ccu'], 50)
            self.assertEqual(len(members), 2)
            self.assertFalse(Path(batch['resource_state']).exists())
        with patch('h3_production.guard', return_value={}):
            again = runtime.append_wave(self.root, 'fixture', value, reason='initial')
        self.assertEqual(again['batches'], wave['batches'])

    def test_unfinished_wave_cannot_be_replaced_by_new_budget_or_attempt(self):
        _, value, _, batches = self.prepare_task(1)
        core.write_json(batches[0]['resource_state'], {'state':'ready'})
        with patch('h3_production.guard', return_value={}):
            with self.assertRaisesRegex(core.FactoryError, 'reconciliation'):
                runtime.append_wave(self.root, 'fixture', value, reason='repair')

    def test_follow_up_wave_reuses_only_an_owned_runtime_with_no_pending_requests(self):
        h, _, _, (parent,) = self.prepare_task(1)
        state = Path(parent['resource_state'])
        core.write_json(state, {'state': 'paused_recoverable', 'endpoint': 'owned'})
        before = Path(parent['batch_file']).read_bytes()
        self.motion_document(2)
        plan = planning.plan(self.root, 'EP001', only=['MOTION-EP001-002'])
        with patch('h3_production.guard', return_value={}):
            with patch.object(h, 'engine') as fake, self.assertRaisesRegex(core.FactoryError, 'retained_runtime'):
                fake.return_value._read_run_history.return_value = [{'status': 'running'}]
                runtime.append_wave(self.root, 'fixture', plan, reason='next', reuse_runtime=True)
            wave = runtime.append_wave(self.root, 'fixture', plan, reason='next', reuse_runtime=True)
        child = core.read_json(wave['batches'][0])
        self.assertEqual(child['runtime_from'], parent['batch_file'])
        self.assertEqual(child['session'], parent['session'])
        self.assertEqual(child['task_budget'], parent['task_budget'])
        self.assertEqual(Path(parent['batch_file']).read_bytes(), before)

    def test_released_lane_can_replan_pre_submission_failure_only(self):
        from unittest.mock import Mock
        job = 'episode-001'
        request = '842a2e74-210b-4781-b9bc-72597be56059'
        state = self.root / 'runtime' / 'resources.json'
        batch = {'project': str(self.root), 'jobs': [job], 'resource_state': str(state)}
        production = Mock()
        production._job_key.return_value = job
        production._read_run_history.return_value = [{
            'status': 'failed', 'adapter': 'h3-colab', 'run_id': 'one',
            'provider_job_id': request, 'error': {'code': 'colab_command_failed'},
        }]
        core.write_json(state, {'state': 'released'})
        graph = state.parent / 'graphs' / (request + '.json')
        self.assertFalse(runtime._terminal_batch(batch, production))
        graph.parent.mkdir()
        self.assertTrue(runtime._terminal_batch(batch, production))
        self.assertEqual(runtime.job_state(self.root, batch, job, production)['state'], 'not_submitted')
        core.write_json(graph, {})
        self.assertFalse(runtime._terminal_batch(batch, production))
        self.assertEqual(runtime.job_state(self.root, batch, job, production)['state'], 'unknown')

    def test_released_runtime_needs_fresh_provider_readback_and_no_recoverable_output(self):
        from unittest.mock import Mock
        job = 'episode-001'
        request = '842a2e74-210b-4781-b9bc-72597be56059'
        state = self.root / 'resources.json'
        batch = {'project': str(self.root), 'jobs': [job], 'resource_state': str(state)}
        production = Mock()
        production._job_key.return_value = job
        production._read_run_history.return_value = [{
            'status': 'failed', 'adapter': 'h3-colab', 'run_id': 'one',
            'provider_job_id': request, 'error': {'code': 'colab_command_failed'},
        }]
        graph = self.root / 'graphs' / (request + '.json')
        core.write_json(graph, {})
        resources = {'state': 'released', 'endpoint': 'owned-instance', 'finished_at': 12,
                     'remote_directory': '/content/vision-h3-fixture',
                     'release_requested_at': 10, 'usage_after': {'count': 1}}
        core.write_json(state, resources)
        self.assertFalse(runtime._terminal_batch(batch, production))
        self.assertEqual(runtime.job_state(self.root, batch, job, production)['state'], 'unknown')
        transport = Mock()
        transport.control.return_value = [{'endpoint': 'owned-instance'}]
        transport.remote.return_value = {'state': 'running'}
        self.assertEqual(runtime.job_state(self.root, batch, job, production, transport)['state'], 'running')
        transport.control.return_value = [{'endpoint': 'other-instance'}]
        row = runtime.job_state(self.root, batch, job, production, transport)
        self.assertEqual(row['state'], 'runtime_released_uncollected')
        self.assertEqual(row['provider_acceptance'], 'unknown')
        core.write_json(self.root / 'reconciliation.json', {'jobs': [row]})
        self.assertTrue(runtime._terminal_batch(batch, production))
        self.assertEqual(runtime.job_state(self.root, batch, job, production), row)
        evidence = self.root / 'remote-evidence.json'
        core.write_json(evidence, {'receipts': [{'request_id': request, 'state': 'succeeded'}]})
        self.assertFalse(runtime._terminal_batch(batch, production))
        self.assertEqual(runtime.job_state(self.root, batch, job, production, transport)['state'], 'unknown')

    def test_upgrade_keeps_original_contract_and_accounting(self):
        path = runtime.task_path(self.root, 'fixture')
        core.write_json(path, {'schema_version': 2, 'release': 'factory-v2',
                              'release_sha256': 'old', 'max_ccu': 50})
        account = path.with_name('accounting.json')
        core.write_json(account, {'task_sha256': core.file_sha(path), 'deadline': 7300})
        before = path.read_bytes(), account.read_bytes()
        state = self.root / 'resources.json'
        batch = self.root / 'batch.json'
        core.write_json(batch, {'project': str(self.root), 'jobs': [], 'resource_state': str(state)})
        core.write_json(path.with_name('wave-0001.json'), {'previous': None,
            'task_sha256': core.file_sha(path), 'batches': {str(batch): core.file_sha(batch)}})
        from drama_factory import releases
        manifest = self.root / 'fixture-release.json'
        core.write_json(manifest, {'release': 'factory-v4'})
        with patch.object(releases, 'verify_release'), patch.object(releases, 'release_path', return_value=manifest):
            core.write_json(state, {'state': 'ready'})
            with self.assertRaisesRegex(core.FactoryError, 'release_all_instances'):
                runtime.upgrade_execution(self.root, 'fixture', authorization='user approved')
            core.write_json(state, {'state': 'released'})
            record = runtime.upgrade_execution(self.root, 'fixture', authorization='user approved')
        self.assertEqual(record['release'], 'factory-v4')
        self.assertEqual((path.read_bytes(), account.read_bytes()), before)
        self.assertEqual(runtime.membership(path), {str(batch): core.file_sha(batch)})

    def test_upgrades_chain_through_prior_releases_and_preserves_original_budget(self):
        from drama_factory import RELEASE, releases
        if not (core.ROOT / '.local/factory-releases/factory-v17').is_dir():
            self.skipTest('requires the local historical execution snapshots')
        path = runtime.task_path(self.root, 'fixture')
        core.write_json(path, {'schema_version': 2, 'release': 'factory-v17',
                               'release_sha256': core.file_sha(releases.release_path('factory-v17'))})
        accounting = path.with_name('accounting.json')
        core.write_json(accounting, {'deadline': 7300, 'authorized_max_ccu': 15})
        before = path.read_bytes(), accounting.read_bytes()
        previous = None
        for target in ('factory-v18', 'factory-v19', 'factory-v20', RELEASE):
            record = runtime.upgrade_execution(self.root, 'fixture', authorization='approved')
            self.assertEqual(record['release'], target)
            self.assertEqual(record['previous'], previous)
            self.assertEqual(runtime.execution_release(path, core.read_json(path))[0], target)
            previous = core.file_sha(path.with_name(f'execution-upgrade-{target}.json'))
        self.assertEqual(runtime.upgrade_execution(self.root, 'fixture', authorization='approved'), record)
        self.assertEqual((path.read_bytes(), accounting.read_bytes()), before)

    def test_never_started_wave_can_be_revised_without_new_task_or_budget(self):
        h, _, _, _ = self.prepare_task(1)
        path=self.root/'剧集/EP001/视频提示词.md'
        path.write_text(path.read_text().replace('person talks','person whispers'))
        value=planning.plan(self.root,'EP001')
        with patch('h3_production.guard',return_value={}):
            wave=runtime.append_wave(self.root,'fixture',value,reason='corrected before allocation')
        batch=core.read_json(wave['batches'][0])
        from unittest.mock import Mock
        transport=Mock()
        transport.control.side_effect=lambda action,*args: [] if action=='assignments' else {'balance':1000,'count':0,'rate':0}
        state=h.prepare_task_runtime(batch,transport,h.engine())
        self.assertEqual(state['state'],'allocating')
        self.assertEqual(len(runtime.membership(batch['task_budget'])),2)

    def test_reopen_expired_task_uses_only_the_approved_new_interval(self):
        path = runtime.task_path(self.root, 'fixture')
        core.write_json(path, {'schema_version': 2, 'max_ccu': 30, 'max_seconds': 10800, 'max_instances': 2})
        account = path.with_name('accounting.json')
        baseline = {'balance': 90, 'count': 0, 'rate': 0}
        core.write_json(account, {'started_at': 100, 'deadline': 10900, 'usage_before': baseline})
        approved = dict(max_ccu=30, additional_seconds=2700, authorization='user approved 45 minutes', from_now=True)
        with patch.object(runtime.time, 'time', return_value=10899):
            with self.assertRaisesRegex(core.FactoryError, 'expired_deadline'):
                runtime.extend(self.root, 'fixture', **approved)
        with patch.object(runtime.time, 'time', return_value=44000):
            event = runtime.extend(self.root, 'fixture', **approved)
        self.assertEqual((event['old_deadline'], event['window_started_at'], event['deadline']), (10900, 44000, 46700))
        record = core.read_json(account)
        self.assertEqual((record['started_at'], record['usage_before'], record['authorized_max_ccu']), (100, baseline, 30))

    def test_single_baseline_and_budget_for_two_instances(self):
        h, _, _, batches = self.prepare_task()
        class Provider:
            def __init__(self, endpoints=()): self.endpoints = endpoints
            def control(self, action, value=''):
                if action == 'assignments': return [{'endpoint': e} for e in self.endpoints]
                if action == 'usage': return {'balance': 1000, 'rate': 15 * len(self.endpoints), 'count': len(self.endpoints)}
                raise AssertionError(action)
        with patch.object(h.time, 'time', return_value=100):
            state = h.prepare_task_runtime(batches[0], Provider(), h.engine())
        state['endpoint'] = 'first'
        core.write_json(batches[0]['resource_state'], state)
        with patch.object(h.time, 'time', return_value=200):
            h.prepare_task_runtime(batches[1], Provider(['first']), h.engine())
        accounting = core.read_json(Path(batches[0]['task_budget']).with_name('accounting.json'))
        self.assertEqual(accounting['started_at'], 100)
        self.assertEqual(accounting['deadline'], 7300)
        for batch in batches:
            with self.assertRaisesRegex(h.H3Error, 'task_budget_or_deadline'):
                h.task_accounting(batch, now=7300)

    def test_active_deadline_extension_leaves_old_guard_contract_untouched(self):
        h, _, _, batches = self.prepare_task(1)
        path = Path(batches[0]['task_budget'])
        accounting = {'task_sha256': core.file_sha(path), 'started_at': 100, 'deadline': 7300,
                      'usage_before': {'balance': 1000}}
        core.write_json(path.with_name('accounting.json'), accounting)
        core.write_json(batches[0]['resource_state'], {'state': 'ready', 'deadline': 7300})
        with self.assertRaisesRegex(core.FactoryError, 'active_lease'):
            runtime.extend(self.root, 'fixture', max_ccu=60, additional_seconds=1000, authorization='extra scope')
        self.assertEqual(core.read_json(path.with_name('accounting.json')), accounting)

    def test_stopped_task_is_not_restarted(self):
        _, _, _, batches = self.prepare_task(1)
        runtime.stop(self.root, 'fixture', reason='user stop')
        with self.assertRaisesRegex(core.FactoryError, 'stopped'):
            runtime.run(self.root, 'fixture', resume=True)

    def test_active_production_rejects_formal_source_edits(self):
        _, _, _, batches = self.prepare_task(1)
        core.write_json(batches[0]['resource_state'], {'state':'ready'})
        name = '剧集/EP001/视频提示词.md'
        with self.assertRaisesRegex(core.FactoryError, 'frozen_until_release'):
            core.revise(self.root, {name:'changed'}, {name:core.file_sha(self.root/name)}, reason='fixture')

    def test_large_document_compiles_sequential_batches_without_truncating_source(self):
        h, value, wave, batches = self.prepare_task(67)
        self.assertEqual(sum(len(b['jobs']) for b in batches), 67)
        self.assertEqual(len(batches), 4)
        for batch in batches:
            _, _, members = h.task_members(batch)
            self.assertEqual(len(members), 4)
        self.assertEqual(len(planning.sections((self.root/'剧集/EP001/视频提示词.md').read_text())), 67)

    def test_interrupted_prepare_recovers_original_jobs_and_request_scope(self):
        import h3_episode
        self.motion_document(22)
        value = planning.plan(self.root, 'EP001')
        runtime.create(self.root, 'recover', value, authorization='fixture', max_ccu=50, max_seconds=7200)
        original, calls = h3_episode.prepare, []
        def fail_second(*args, **kwargs):
            calls.append(args[2])
            if len(calls) == 2:
                raise RuntimeError('interruption')
            return original(*args, **kwargs)
        with patch('h3_production.guard', return_value={}), patch.object(h3_episode, 'prepare', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                runtime.append_wave(self.root, 'recover', value, reason='initial')
        before = {p.name: core.file_sha(p) for p in (self.root/core.LEDGER/'jobs').glob('*.json')}
        with patch('h3_production.guard', return_value={}):
            wave = runtime.append_wave(self.root, 'recover', value, reason='initial')
        after = {p.name: core.file_sha(p) for p in (self.root/core.LEDGER/'jobs').glob('*.json')}
        self.assertTrue(before)
        self.assertTrue(before.items() <= after.items())
        self.assertEqual(len(wave['batches']), 2)

    def test_revision_wave_retains_task_contract_and_spent_budget(self):
        h, _, wave, batches = self.prepare_task(1)
        batch = batches[0]
        production = h.engine()
        for job_id in batch['jobs']:
            job = production._read_job(self.root, job_id)
            files = []
            for name in job['outputs']:
                path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'completed fixture output')
                files.append({'path': name, 'sha256': core.file_sha(path)})
            production._write_run(self.root, job_id, {'run_id': 'fixture-success', 'status': 'succeeded',
                'job_id': job_id, 'fingerprint': job['fingerprint'],
                'outputs': files, 'started_at': '2026-11-13T00:00:00Z', 'finished_at': '2026-11-13T00:00:01Z'})
        core.write_json(batch['resource_state'], {'state': 'released', 'started_at': 100, 'finished_at': 200})
        task = Path(batch['task_budget'])
        accounting = {'task_sha256': core.file_sha(task), 'started_at': 100, 'deadline': 7300,
                      'usage_before': {'balance': 1000}}
        core.write_json(task.with_name('accounting.json'), accounting)
        task_sha = core.file_sha(task)
        path = self.root/'剧集/EP001/视频提示词.md'
        path.write_text(path.read_text().replace('person talks', 'person whispers'))
        new_plan = planning.plan(self.root, 'EP001')
        with patch('h3_production.guard', return_value={}):
            revision = runtime.append_wave(self.root, 'fixture', new_plan, reason='observed dialogue repair')
        new_batch = core.read_json(revision['batches'][0])
        self.assertEqual(core.file_sha(task), task_sha)
        self.assertEqual(core.read_json(task.with_name('accounting.json')), accounting)
        self.assertEqual(len(h.task_members(new_batch)[2]), 2)
        self.assertAlmostEqual(h.task_accounting(new_batch, now=300)['spent_ccu_upper_estimate'],
                               batch['max_rate'] * 100 / 3600)


class RecoveryTests(FactoryFixture):
    def test_remote_lost_submit_response_does_not_post_twice(self):
        import colab_h3_remote as remote
        import uuid
        directory = self.root / 'remote'
        directory.mkdir()
        remote.write(directory / 'setup.json', {'inference_deadline': 99999999999, 'required_nodes': ['Fixture']})
        payload = {'request_id': str(uuid.uuid4()), 'graph': {'1': {'class_type': 'Fixture'}}}
        posts = []
        def http(path, data=None):
            if path == '/queue': return {}
            posts.append(data)
            raise TimeoutError('response lost')
        with patch.object(remote, 'http', side_effect=http):
            self.assertEqual(remote.submit(directory, payload)['state'], 'unknown')
            self.assertEqual(remote.submit(directory, payload)['state'], 'unknown')
        self.assertEqual(len(posts), 1)

    def test_unknown_attempt_never_calls_submit_path(self):
        import colab_h3 as h
        from unittest.mock import Mock
        state = self.root / 'resources.json'
        core.write_json(state, {'remote_directory': '/content/vision-h3-fixture'})
        production = Mock()
        production._read_run_history.return_value = [{'run_id': 'one', 'provider_job_id': 'same-request'}]
        provider = Mock()
        provider.remote.return_value = {'state': 'unknown'}
        with patch.object(h, 'run_adapter') as submit:
            h.recover_unsubmitted(self.root, {'resource_state': str(state)}, 'job', production, provider)
        submit.assert_not_called()

    def test_proven_unsubmitted_uses_original_provider_request_and_official_attempt(self):
        import colab_h3 as h
        from unittest.mock import Mock
        state = self.root / 'resources.json'
        core.write_json(state, {'remote_directory': '/content/fixture', 'endpoint': 'owned'})
        production = Mock()
        production._read_run_history.return_value = [{'run_id': 'same-attempt', 'provider_job_id': 'same-request'}]
        production._read_job.return_value = {'job_id': 'one'}
        transport = Mock()
        transport.remote.return_value = {'state': 'not_submitted'}
        with patch.object(h, 'run_adapter', return_value={}) as submit:
            h.recover_unsubmitted(self.root, {'resource_state': str(state)}, 'one', production, transport)
        payload = submit.call_args.args[0]
        self.assertEqual(payload['resume_provider_job_id'], 'same-request')
        self.assertEqual(payload['run_id'], 'same-attempt')

    def test_completed_but_missing_output_never_becomes_new_generation(self):
        from unittest.mock import Mock
        production = Mock()
        production._read_run_history.return_value = [{'status': 'succeeded', 'outputs': [
            {'path': 'deleted.mp4', 'sha256': '0' * 64}]}]
        value = runtime.job_state(self.root, {'jobs': ['one']}, 'one', production)
        self.assertEqual(value['state'], 'output_missing_or_changed')
        self.assertEqual(value['action'], 'review_retention_no_regeneration')


if __name__ == '__main__':
    unittest.main()
