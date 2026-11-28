"""Concurrent allocation, shared accounting and ownership; no network or GPU."""
import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import colab_h3 as h


class TaskBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'short-drama.json').write_text('{}')
        (self.root / 'ref.png').write_bytes(b'identity-reference')
        setup = self.root / 'setup.json'; setup.write_text('{}')
        directory = self.root / '.short-drama/production/colab'
        self.task = directory / 'task/task.json'
        self.production = h.engine()
        self.batches = []
        prompt = ('subject_definitions: hero\nsummary: action\nretention_analysis: identity\n'
                  'detailed_description: action\noverall_soundscape: wind\nnon_diegetic_music: N/A')
        source = self.root / '剧集/EP001/视频提示词.md'
        source.parent.mkdir(parents=True)
        source.write_text('\n'.join(f'## MOTION-EP001-00{i} · 测试\n'
            '- 输入参考图：REF-A（顺序：1）· ref.png《身份板》（用途：身份；控制：身份；不得控制：构图）\n'
            '### 可复制提示词\n' + '\n'.join('> ' + line for line in prompt.splitlines())
            for i in (1, 2)))
        for i in (1, 2):
            folder = directory / f'p{i}'
            job = {'job_id': f'JOB-{i}', 'modality': 'video', 'adapter': 'h3-colab',
                   'source': '剧集/EP001/视频提示词.md', 'source_entry': f'MOTION-EP001-00{i}',
                   'prompt': prompt, 'reference_bindings': [{'slot_id': 'REF-A', 'order': 1,
                       'path': 'ref.png', 'label': '身份板', 'role': 'reference_image',
                       'may_control': ['身份'], 'must_not_control': ['构图']}],
                   'outputs': [f'剧集/EP001/制作成果/video/video-{i}.mp4'], 'overwrite': False,
                   'parameters': h.episode_parameters('comfy_kitchen_int8', i, 5, '9:16')}
            input_file = folder / 'input.json'; h.write_json(input_file, job)
            preview = self.production.prepare_job(self.root, input_file)
            self.production.confirm_job(self.root, job_id=job['job_id'], confirmation=preview['confirmation'])
            batch = {'schema_version': 1, 'profile': h.EPISODE_PROFILE,
                     'batch_id': f'p{i}', 'batch_file': str(folder / 'batch.json'),
                     'project': str(self.root), 'jobs': [job['job_id']], 'gpu': 'G4',
                     'session': f'part-{i}', 'max_seconds': 28800, 'max_ccu': 50,
                     'max_rate': 15, 'max_attempts_per_job': 1,
                     'resource_state': str(folder / 'resources.json'), 'setup_manifest': str(setup),
                     'adapter_config': str(folder / 'adapter.json'),
                     'setup_sha256': h.digest(setup), 'implementation_sha256': h.implementation_digest(),
                     'task_budget': str(self.task)}
            h.write_json(batch['batch_file'], batch)
            self.batches.append(batch)
        self.contract = {'schema_version': 1, 'project': str(self.root),
                         'max_instances': 2, 'max_ccu': 100, 'max_seconds': 28800,
                         'batches': {b['batch_file']: h.digest(b['batch_file']) for b in self.batches}}
        h.write_json(self.task, self.contract)

    def provider(self, assignments=(), balance=1000, rate=0):
        class Provider:
            def control(_, action, value=''):
                if action == 'assignments': return [{'endpoint': e} for e in assignments]
                if action == 'usage': return {'balance': balance, 'rate': rate, 'count': len(assignments)}
                raise AssertionError(action)
        return Provider()

    def start_two(self):
        with patch.object(h.time, 'time', return_value=100):
            first = h.prepare_task_runtime(self.batches[0], self.provider(), self.production)
        first['endpoint'] = 'first'; h.write_json(self.batches[0]['resource_state'], first)
        with patch.object(h.time, 'time', return_value=200):
            second = h.prepare_task_runtime(self.batches[1], self.provider(['first'], rate=15), self.production)
        return first, second

    def test_fifty_ccu_eight_hours_per_partition_is_valid(self):
        for batch in self.batches: h.validate_batch(batch)

    def test_ccu_only_wait_does_not_reduce_the_paid_lease(self):
        batch = self.batches[0]
        task = {**self.contract, 'schema_version': 2, 'max_ccu': 10, 'max_instances': 1}
        h.write_json(self.task.with_name('accounting.json'),
                     {'started_at': 100, 'deadline': None, 'task_sha256': h.digest(self.task)})
        resource = Path(batch['resource_state'])
        with patch.object(h, 'task_members', return_value=(self.task, task, [batch])):
            with patch.object(h.time, 'time', return_value=100):
                state = h.prepare_task_runtime(batch, self.provider(), self.production)
            self.assertIsNone(state['deadline'])
            h.ensure_budget(batch, state, now=10900, reserve=240)
            self.assertEqual(h.task_accounting(batch, now=10900)['spent_ccu_upper_estimate'], 0)

            h.write_json(resource.with_name('allocation-request.json'),
                         {'request_id': 'owned-request', 'endpoint': 'owned-g4'})
            h.write_json(resource.with_name('allocation-clock.json'),
                         {'request_id': 'owned-request', 'possible_assignment_at': 10900})
            self.assertEqual(h.task_accounting(batch, now=11140)['spent_ccu_upper_estimate'], 1)
            with self.assertRaisesRegex(h.H3Error, 'task_budget_or_deadline_exhausted'):
                h.ensure_budget(batch, state, now=13300, reserve=0)

    def test_two_live_instances_use_their_own_time_and_original_task_deadline(self):
        first, second = self.start_two()
        self.assertLessEqual(second['deadline'], 100 + 28800)
        usage = {'balance': 980, 'rate': 30, 'count': 2}
        result = h.task_accounting(self.batches[0], now=3700)
        self.assertAlmostEqual(result['spent_ccu_upper_estimate'], 15 + 3500 * 15 / 3600)
        self.assertEqual(result['active_instances'], 2)
        h.ensure_budget(self.batches[0], first, usage, now=3700)

    def test_actual_task_lease_cost_stops_both_partitions_at_shared_total(self):
        states = self.start_two()
        for batch, state in zip(self.batches, states):
            with self.assertRaisesRegex(h.H3Error, 'task_budget_or_deadline'):
                h.ensure_budget(batch, state, now=12200, reserve=0)

    def test_released_instance_stops_accruing_conservative_cost(self):
        first, _ = self.start_two()
        first.update(state='released', finished_at=300)
        h.write_json(self.batches[0]['resource_state'], first)
        value = h.task_accounting(self.batches[1], now=1000)
        self.assertAlmostEqual(value['spent_ccu_upper_estimate'], (200 + 800) * 15 / 3600)
        self.assertEqual(value['active_instances'], 1)

    def test_single_instance_resume_uses_settled_balance_and_caps_new_lease(self):
        old, new = self.batches
        task = {**self.contract, 'schema_version': 2, 'max_ccu': 10, 'max_instances': 1}
        self.task = self.task.resolve()
        h.write_json(self.task.with_name('accounting.json'), {'started_at': 100, 'deadline': 7300})
        settled = {'state': 'released', 'started_at': 100, 'finished_at': 1680,
                   'endpoint': 'old', 'baseline_endpoints': [], 'release_requested_at': 1660,
                   'usage_before': {'balance': 100, 'rate': 0, 'count': 0},
                   'usage_after': {'balance': 96.7, 'rate': 0, 'count': 0}}
        h.write_json(old['resource_state'], settled)
        with patch.object(h, 'task_members', return_value=(self.task, task, self.batches)), \
                patch.object(h, 'task_allocation_lock', return_value=h.contextlib.nullcontext()), \
                patch.object(h.time, 'time', return_value=1800):
            ledger = h.task_accounting(new, now=1800)
            self.assertAlmostEqual(ledger['spent_ccu_upper_estimate'], 3.3)
            state = h.prepare_task_runtime(new, self.provider(balance=96.7), self.production)
            self.assertEqual(state['closeout_reserve_seconds'], 240)
            self.assertAlmostEqual(state['deadline'], 1800 + 6.7 * 3600 / 15 - 120)
            h.ensure_budget(new, state, now=1801, reserve=240)

    def test_unsettled_or_shared_release_keeps_upper_estimate(self):
        old, new = self.batches
        h.write_json(self.task.with_name('accounting.json'), {'started_at': 100, 'deadline': 7300})
        settled = {'state': 'released', 'started_at': 100, 'finished_at': 1680,
                   'endpoint': 'old', 'baseline_endpoints': [], 'release_requested_at': 1660,
                   'usage_before': {'balance': 100, 'rate': 0, 'count': 0},
                   'usage_after': {'balance': 96.7, 'rate': 0, 'count': 0}}
        for changes in ({'baseline_endpoints': ['other']},
                        {'usage_after': {'balance': 100, 'rate': 0, 'count': 0}},
                        {'usage_after': {'balance': 96.7, 'rate': 10, 'count': 1}},
                        {'previous_allocations': [{'endpoint': 'earlier'}]}):
            with self.subTest(changes=changes):
                h.write_json(old['resource_state'], {**settled, **changes})
                task = {**self.contract, 'schema_version': 2, 'max_ccu': 10, 'max_instances': 1}
                with patch.object(h, 'task_members', return_value=(self.task, task, self.batches)):
                    value = h.task_accounting(new, now=1800)
                self.assertAlmostEqual(value['spent_ccu_upper_estimate'], 1580 * 15 / 3600)

    def test_failed_allocation_with_equal_idle_bookends_costs_zero(self):
        idle = {'balance': 100, 'rate': 0, 'count': 0}
        failed = {'state': 'released', 'failure': 'colab_command_failed',
                  'baseline_endpoints': [], 'usage_before': idle, 'usage_after': idle,
                  'previous_allocations': [{'finished_at': 500, 'failure': 'colab_command_failed'}]}
        self.assertEqual(h._settled_lease_charge(failed), 0)
        self.assertIsNone(h._settled_lease_charge({**failed, 'previous_allocations': [{'endpoint': 'earlier'}]}))

    def test_settled_transferred_runtime_counts_its_debit_once(self):
        first, last = self.batches
        task = {**self.contract, 'schema_version': 2, 'max_instances': 1}
        h.write_json(self.task.with_name('accounting.json'), {'started_at': 100, 'deadline': 7300})
        idle = {'balance': 100, 'rate': 0, 'count': 0}
        initial = {'state': 'transferred', 'endpoint': 'same', 'started_at': 100,
                   'finished_at': 800, 'baseline_endpoints': [], 'usage_before': idle}
        closing = {'state': 'released', 'endpoint': 'same', 'started_at': 800, 'finished_at': 1300,
                   'release_requested_at': 1290, 'baseline_endpoints': [], 'usage_before': idle,
                   'usage_after': {**idle, 'balance': 97}}
        h.write_json(first['resource_state'], initial)
        h.write_json(last['resource_state'], closing)
        with patch.object(h, 'task_members', return_value=(self.task, task, self.batches)):
            value = h.task_accounting(last, now=1700)
            self.assertAlmostEqual(value['spent_ccu_upper_estimate'], 3)
            h.write_json(last['resource_state'], {**closing, 'endpoint': 'other'})
            value = h.task_accounting(last, now=1700)
            self.assertAlmostEqual(value['spent_ccu_upper_estimate'], 3 + 700 * 15 / 3600)

    def test_one_batch_follow_up_in_two_instance_task_gets_a_balance_bounded_deadline(self):
        old, new = ({**b, 'wave': i} for i, b in enumerate(self.batches, start=1))
        task = {**self.contract, 'schema_version': 2, 'max_ccu': 30}
        h.write_json(self.task.with_name('accounting.json'), {'started_at': 100, 'deadline': 15000})
        h.write_json(old['resource_state'], {'state': 'released', 'started_at': 100, 'finished_at': 6100})
        with patch.object(h, 'task_members', return_value=(self.task, task, [old, new])), \
                patch.object(h, 'task_allocation_lock', return_value=h.contextlib.nullcontext()), \
                patch.object(h.time, 'time', return_value=6500):
            state = h.prepare_task_runtime(new, self.provider(), self.production)
            self.assertEqual(state['closeout_reserve_seconds'], 240)
            self.assertEqual(state['deadline'], 6500 + 5 * 3600 / 15 - 120)
            h.ensure_budget(new, state, now=7100, reserve=240)
            with self.assertRaisesRegex(h.H3Error, 'task_budget_or_deadline'):
                h.ensure_budget(new, state, now=7370, reserve=240)

    def test_closed_concurrent_wave_counts_one_shared_balance_debit(self):
        task = {**self.contract, 'schema_version': 2, 'max_ccu': 30}
        h.write_json(self.task.with_name('accounting.json'), {'started_at': 100, 'deadline': 10800})
        members = [{**batch, 'wave': 1} for batch in self.batches]
        baseline = {'balance': 100, 'rate': 0, 'count': 0}
        first, second = ({'state': 'released', 'endpoint': str(i), 'baseline_endpoints': [],
                          'started_at': 100 + i, 'release_requested_at': finished - 10,
                          'finished_at': finished, 'usage_before': baseline,
                          'usage_after': balance}
                         for i, (finished, balance) in enumerate([
                             (2500, {'balance': 91, 'rate': 10, 'count': 1}),
                             (3500, {'balance': 85, 'rate': 0, 'count': 0})]))
        with patch.object(h, 'task_members', return_value=(self.task, task, members)):
            h.write_json(members[0]['resource_state'], first)
            h.write_json(members[1]['resource_state'], second)
            self.assertAlmostEqual(h.task_accounting(members[0], now=3600)['spent_ccu_upper_estimate'], 15)
            h.write_json(members[1]['resource_state'], {**second, 'state': 'ready'})
            ledger = h.task_accounting(members[0], now=3600)
            self.assertEqual(ledger['active_instances'], 1)
            self.assertAlmostEqual(ledger['spent_ccu_upper_estimate'], (2400 + 3499) * 15 / 3600)

    def test_unrelated_instance_neither_blocks_allocation_nor_counts_towards_budget(self):
        batch = self.batches[0]
        state = h.prepare_task_runtime(batch, self.provider(['someone-else'], rate=40), self.production)
        self.assertEqual(state['baseline_endpoints'], ['someone-else'])
        self.assertNotIn('endpoint', state)
        h.ensure_budget(batch, state, {'balance': 500, 'rate': 55}, reserve=0)

    def test_task_limit_mutation_after_start_is_rejected(self):
        self.start_two()
        self.contract['max_seconds'] = 28000; h.write_json(self.task, self.contract)
        with self.assertRaisesRegex(h.H3Error, 'task_contract_changed'):
            h.task_accounting(self.batches[0], now=300)

    def test_every_partition_must_be_registered_before_first_allocation(self):
        original = self.production._metadata_read_json
        def read_metadata(root, parts, name, **kwargs):
            return {} if parts == ('confirmations',) else original(root, parts, name, **kwargs)
        with patch.object(self.production, '_metadata_read_json', side_effect=read_metadata):
            with self.assertRaisesRegex(h.H3Error, 'all_task_inputs_must_be_registered'):
                h.prepare_task_runtime(self.batches[0], self.provider(), self.production)
        self.assertFalse(self.task.with_name('accounting.json').exists())

    def test_two_job_ids_cannot_assign_the_same_shot_to_two_instances(self):
        original = self.production._read_job
        def read_job(root, job_id):
            job = dict(original(root, job_id))
            job['source_entry'] = 'MOTION-EP001-001'
            return job
        with patch.object(h, 'engine', return_value=self.production), \
                patch.object(self.production, '_read_job', side_effect=read_job):
            with self.assertRaisesRegex(h.H3Error, 'duplicate_task_source_entry'):
                h.task_members(self.batches[0])

    def test_concurrent_reservations_share_one_start_and_preserve_both_resource_states(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            states = list(executor.map(lambda b: h.prepare_task_runtime(b, self.provider(), self.production),
                                       self.batches))
        self.assertEqual(len(states), 2)
        accounting = h.read_json(self.task.with_name('accounting.json'))
        self.assertTrue(all(s['deadline'] <= accounting['deadline'] for s in states))
        self.assertTrue(all(Path(b['resource_state']).exists() for b in self.batches))

    def test_replacing_released_allocation_keeps_original_budget_clock_and_deadline(self):
        first, _ = self.start_two()
        first.update(state='released', finished_at=300)
        h.write_json(self.batches[0]['resource_state'], first)
        with patch.object(h.time, 'time', return_value=1000):
            replacement = h.prepare_task_runtime(self.batches[0], self.provider(), self.production,
                                                 previous=first)
        self.assertEqual(replacement['started_at'], first['started_at'])
        self.assertEqual(replacement['deadline'], first['deadline'])
        self.assertEqual(replacement['previous_allocations'][0]['endpoint'], 'first')

    def test_two_batches_generate_concurrently_and_release_only_their_own_endpoint(self):
        endpoints, history, submitted = {'unrelated'}, {}, []
        mutex, together = threading.Lock(), threading.Barrier(2, timeout=10)
        peak = [0]
        real, root = self.production, self.root
        failures = []
        class Production:
            def __getattr__(_, name): return getattr(real, name)
            def _read_run_history(_, root, job_id): return history.get(job_id, [])
            def run_job(_, root, job_id, **kwargs):
                submitted.append(job_id)
                together.wait()
                outputs = []
                try:
                    for name in real._read_job(root, job_id)['outputs']:
                        file = root / name; file.parent.mkdir(parents=True, exist_ok=True)
                        file.write_bytes(b'collected-video')
                        outputs.append({'path': name, 'sha256': h.digest(file)})
                except Exception as exc:
                    failures.append(repr(exc)); raise
                history[job_id] = [{'status': 'succeeded', 'outputs': outputs}]
                return {'job_id': job_id}
        class Provider:
            def __init__(_, batch): _.session = _.endpoint = batch['session']
            def control(_, action, value=''):
                with mutex:
                    if action == 'usage': return {'balance': 1000, 'rate': 15 * len(endpoints), 'count': len(endpoints)}
                    if action == 'assignments': return [{'endpoint': e} for e in endpoints]
                    if action == 'session': return {'endpoint': _.endpoint}
                    if action == 'unassign': endpoints.discard(value); return {'requested': True}
                raise AssertionError(action)
            def allocation_request(_, operation, gpu, request_id, deadline, resource):
                with mutex:
                    endpoints.add(_.endpoint); peak[0] = max(peak[0], len(endpoints))
                return {'state': 'assigned', 'endpoint': _.endpoint}
            def execute(_, *args, **kwargs): return {}
            def upload(_, *args): pass
            def remote(_, *args): return {'state': 'ready'}
        with patch.object(h.subprocess, 'Popen'), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda b: h.run_batch(b, Provider(b), Production()), self.batches))
        self.assertEqual(peak[0], 3)
        self.assertTrue(all(r['state'] == 'released' for r in results),
                        [failures, *[h.read_json(b['resource_state']) for b in self.batches]])
        self.assertEqual(endpoints, {'unrelated'})
        self.assertEqual(sorted(submitted), ['JOB-1', 'JOB-2'])
        self.assertTrue(all(r['state'] == 'released' and r['completed_jobs'] == 1 for r in results))

    def test_failed_generation_and_unavailable_readback_keep_the_owned_runtime(self):
        batch = self.batches[0]
        transport = Mock()
        transport.session = batch['session']
        transport.allocation_request.return_value = {'state': 'assigned', 'endpoint': 'mine'}
        def control(action, value=''):
            if action == 'usage': return {'balance': 1000, 'rate': 15, 'count': 1}
            if action == 'session': return {'endpoint': 'mine'}
            if action == 'assignments':
                if not transport.allocation_request.called: return [{'endpoint': 'unrelated'}]
                raise h.H3Error('colab_command_failed')
            raise AssertionError(action)
        transport.control.side_effect = control
        transport.remote.return_value = {'state': 'ready'}
        with patch.object(h.subprocess, 'Popen'), patch.object(
                self.production, 'run_job', side_effect=h.H3Error('colab_response_unknown')):
            result = h.run_batch(batch, transport, self.production)
        self.assertEqual(result['state'], 'paused_recoverable')
        self.assertEqual(h.read_json(batch['resource_state'])['endpoint'], 'mine')


if __name__ == '__main__':
    unittest.main()
