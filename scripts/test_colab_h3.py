"""Behavior at the Colab adapter and finite batch boundaries; no GPU calls."""
import tempfile
import json
import os
import time
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import colab_h3
import colab_h3_remote


def batch_fixture(root):
    (root / 'short-drama.json').write_text('{}')
    setup = root / 'setup.json'; setup.write_text('{}')
    return {'schema_version': 1, 'jobs': ['TEST-001'], 'gpu': 'G4',
            'max_seconds': 3600, 'max_ccu': 15, 'max_rate': 15,
            'max_attempts_per_job': 1, 'project': str(root),
            'session': 'h3-test', 'batch_id': 'test',
            'batch_file': str(root / 'batch.json'),
            'resource_state': str(root / 'state.json'),
            'setup_manifest': str(setup), 'setup_sha256': colab_h3.digest(setup),
            'implementation_sha256': colab_h3.implementation_digest()}


class GraphContractTests(unittest.TestCase):
    def test_pika_reference_job_keeps_all_inputs_and_the_published_six_nfe_contract(self):
        job = {'modality': 'video', 'outputs': ['delivery.mp4', 'master.mp4'],
               'prompt': 'subject_definitions: a\nsummary: a\nretention_analysis: a\ndetailed_description: a\noverall_soundscape: room\nnon_diegetic_music: N/A',
               'reference_bindings': [{'role': 'reference_image', 'label': str(i),
                   'may_control': ['identity'], 'must_not_control': ['composition']} for i in range(4)],
               'parameters': colab_h3.pika_parameters('sage3', 42)}
        graph = colab_h3.build_graph(job, [str(i)*64+'.png' for i in range(4)],
                                     '00000000-0000-4000-8000-000000000001')
        nodes = list(graph.values())
        self.assertEqual([n['inputs']['sigmas'] for n in nodes if n['class_type']=='ManualSigmas'],
                         ['1.0, 0.972973, 0.923077, 0.8, 0.571429, 0.333333, 0.0'])
        self.assertEqual([n['inputs']['sampler_name'] for n in nodes if n['class_type']=='KSamplerSelect'], ['lcm'])
        self.assertEqual([n['inputs']['strength_model'] for n in nodes if n['class_type']=='LoraLoaderModelOnly'], [1.0])
        self.assertEqual(len([n for n in nodes if n['class_type']=='LoadImage']), 4)
        self.assertEqual(len([n for n in nodes if n['class_type']=='SaveVideo']), 2)
        self.assertFalse(any(n['class_type']=='BasicScheduler' for n in nodes))

    def test_unsupported_reference_is_rejected_before_submission(self):
        job = {'modality': 'video', 'outputs': ['剧集/EP001/制作成果/video/a.mp4'],
               'prompt': 'complete prompt', 'parameters': {'seed': 123},
               'reference_bindings': [{'role': 'reference_video'}]}
        with self.assertRaisesRegex(colab_h3.H3Error, 'unsupported_reference'):
            colab_h3.build_graph(job, ['existing.mp4'], 'test')


class PreparationTests(unittest.TestCase):
    def test_five_case_batch_has_three_same_seed_backends_and_two_missing_shots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); (root/'short-drama.json').write_text('{}')
            source = root/'剧集/EP001/视频提示词.md'; source.parent.mkdir(parents=True)
            (root/'ref.png').write_bytes(b'image')
            prompt = 'subject_definitions: a\nsummary: a\nretention_analysis: a\ndetailed_description: a\noverall_soundscape: room\nnon_diegetic_music: N/A'
            source.write_text('\n'.join('## MOTION-EP001-'+entry+' · fixture\n- 输入参考图：REF-A（顺序：1）· ref.png《角色板》（用途：身份；控制：身份；不得控制：构图）\n### 可复制提示词\n'+ '\n'.join('> '+s for s in prompt.splitlines())+'\n' for entry in ['025','030','033']))
            setup = root/'pika.json'; setup.write_text('{"profile":"pika-ref-v1"}')
            path = root/'.short-drama/production/colab/test-five/batch.json'
            with patch.object(Path, 'home', return_value=root):
                result = colab_h3.prepare_batch(root,
                    ['MOTION-EP001-033']*3+['MOTION-EP001-025','MOTION-EP001-030'], path, setup,
                    backends=['pytorch','comfy_kitchen_int8','sage3','comfy_kitchen_int8','comfy_kitchen_int8'])
            production = colab_h3.engine()
            jobs = [production._read_job(root, key) for key in result['jobs']]
            self.assertEqual(len(set(j['job_id'] for j in jobs)), 5)
            self.assertEqual(len(set(j['parameters']['seed'] for j in jobs[:3])), 1)
            self.assertEqual(len(set(j['prompt'] for j in jobs[:3])), 1)
            self.assertTrue(all(j['parameters']['steps']==6 and len(j['outputs'])==2 for j in jobs))


class BudgetTests(unittest.TestCase):
    def test_stale_balance_cannot_hide_wall_time_cost(self):
        with self.assertRaisesRegex(colab_h3.H3Error, 'budget_or_deadline'):
            colab_h3.ensure_budget({'max_seconds': 3600, 'max_ccu': 2, 'max_rate': 15},
                                  {'started_at': 100, 'usage_before': {'balance': 100}},
                                  {'rate': 10, 'balance': 100}, now=800, reserve=120)

    def test_delayed_zero_rate_uses_explicit_conservative_bound(self):
        colab_h3.ensure_budget({'max_seconds': 3600, 'max_ccu': 15, 'max_rate': 15},
                              {'started_at': 100, 'usage_before': {'balance': 100}},
                              {'rate': 0, 'balance': 100}, now=110)

    def test_last_ten_minutes_are_reserved_for_collection_and_release(self):
        with self.assertRaisesRegex(colab_h3.H3Error, 'budget_or_deadline'):
            colab_h3.ensure_budget({'max_seconds': 3600, 'max_ccu': 15, 'max_rate': 15},
                                  {'started_at': 100, 'usage_before': {'balance': 100}},
                                  {'rate': 10, 'balance': 99}, now=3100)


class RecoveryTests(unittest.TestCase):
    def test_recovery_uses_verified_native_after_duplicate_cache_was_released(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); batch = batch_fixture(root)
            request = '00000000-0000-4000-8000-000000000003'
            cache = root/'results'; cache.mkdir()
            native = root/'production/native.mp4'; native.parent.mkdir(); native.write_bytes(b'kept-native')
            colab_h3.write_json(cache/(request+'.json'), {'job_id': 'TEST-001', 'files': [{
                'target': 'production/native.mp4', 'retained_path': 'production/native.mp4',
                'bytes': native.stat().st_size, 'sha256': colab_h3.digest(native)}]})
            staging = root/'staging'; staging.mkdir()
            result = colab_h3.run_adapter({'job_id': 'TEST-001', 'adapter': 'h3-colab',
                'collect_provider_job_id': request, 'outputs': ['production/native.mp4'],
                'output_root': str(staging)}, batch, None)
            self.assertEqual(Path(result['outputs'][0]['source']).read_bytes(), b'kept-native')

    def test_remote_single_output_does_not_require_or_return_a_master(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'output').mkdir()
            (root/'output/h3-test.mp4').write_bytes(b'video')
            entry = {'outputs': {'17': {'images': [{'filename': 'h3-test.mp4', 'subfolder': ''}]}}}
            with patch.object(colab_h3_remote, 'COMFY', root):
                result = colab_h3_remote.find_result(entry, 'test')
            self.assertEqual(len(result['files']), 1)

    def test_remote_result_returns_delivery_then_master_without_resampling(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'output').mkdir()
            entry={'outputs': {}}
            for node, name in [('17','h3-test.mp4'),('19','master-test.mp4')]:
                (root/'output'/name).write_bytes(name.encode())
                entry['outputs'][node]={'images':[{'filename':name,'subfolder':''}]}
            with patch.object(colab_h3_remote,'COMFY',root):
                result=colab_h3_remote.find_result(entry,'test')
            self.assertEqual([Path(v['path']).name for v in result['files']],
                             ['h3-test.mp4','master-test.mp4'])

    def test_collect_restores_both_pika_outputs_after_release_and_code_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); batch = batch_fixture(root)
            batch['implementation_sha256'] = 'previous-code-version'
            request = '00000000-0000-4000-8000-000000000002'
            cache = root / 'results'; cache.mkdir()
            files = []
            for i, target in enumerate(['delivery.mp4', 'master.mp4']):
                clip = cache / (request + f'-{i}.mp4'); clip.write_bytes(target.encode())
                files.append({'target': target, 'cache_name': clip.name,
                              'bytes': clip.stat().st_size, 'sha256': colab_h3.digest(clip)})
            colab_h3.write_json(cache / (request + '.json'), {'job_id': 'TEST-001', 'files': files})
            colab_h3.write_json(batch['resource_state'], {'state': 'released'})
            output = root / 'staging'; output.mkdir()
            job = {'job_id': 'TEST-001', 'adapter': 'h3-colab', 'collect_provider_job_id': request,
                   'outputs': ['delivery.mp4', 'master.mp4'], 'output_root': str(output)}
            result = colab_h3.run_adapter(job, batch, None)
            self.assertEqual([Path(o['source']).read_bytes() for o in result['outputs']],
                             [b'delivery.mp4', b'master.mp4'])

    def test_lost_submit_response_is_never_posted_again(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            (d / 'setup.json').write_text(json.dumps({'inference_deadline': time.time() + 60,
                                                     'required_nodes': ['SaveVideo']}))
            posts = []
            def api(path, body=None):
                if body is None: return {'queue_running': [], 'queue_pending': []}
                posts.append(body)
                raise TimeoutError('response lost after acceptance')
            data = {'request_id': '00000000-0000-4000-8000-000000000001',
                    'graph': {'1': {'class_type': 'SaveVideo', 'inputs': {}}}}
            with patch.object(colab_h3_remote, 'http', side_effect=api):
                self.assertEqual(colab_h3_remote.submit(d, data)['state'], 'unknown')
                self.assertEqual(colab_h3_remote.submit(d, data)['state'], 'unknown')
            self.assertEqual(len(posts), 1)

    def test_collect_uses_verified_local_copy_after_gpu_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); batch = batch_fixture(root)
            request = '00000000-0000-4000-8000-000000000001'
            cache = root / 'results'; cache.mkdir()
            clip = cache / (request + '.mp4'); clip.write_bytes(b'known-complete-video')
            target = '剧集/EP001/制作成果/video/test.mp4'
            colab_h3.write_json(cache / (request + '.json'),
                                {'job_id': 'TEST-001', 'target': target,
                                 'bytes': 20, 'sha256': colab_h3.digest(clip)})
            colab_h3.write_json(batch['resource_state'], {'state': 'released'})
            output = root / 'staging'; output.mkdir()
            job = {'job_id': 'TEST-001', 'adapter': 'h3-colab', 'collect_provider_job_id': request,
                   'outputs': [target], 'output_root': str(output)}
            result = colab_h3.run_adapter(job, batch, None)
            self.assertEqual(Path(result['outputs'][0]['source']).read_bytes(), b'known-complete-video')


class ReleaseTests(unittest.TestCase):
    def test_managed_remote_guard_keeps_uncollected_results_until_hard_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            colab_h3_remote.write(directory / 'setup.json',
                                  {'deadline': 410, 'retain_for_collection': True})
            heartbeat = directory / 'heartbeat'
            heartbeat.touch()
            os.utime(heartbeat, (0, 0))
            current = 400
            requested_at = []
            def advance(seconds):
                nonlocal current
                current += seconds
            def unassign(*args, **kwargs):
                requested_at.append(current)
                response = MagicMock()
                response.__enter__.return_value.status = 200
                return response
            with patch.dict(os.environ, {'TBE_RUNTIME_ADDR': 'manager:80'}), \
                    patch.object(colab_h3_remote.time, 'time', side_effect=lambda: current), \
                    patch.object(colab_h3_remote.time, 'sleep', side_effect=advance), \
                    patch.object(colab_h3_remote.urllib.request, 'urlopen', side_effect=unassign):
                colab_h3_remote.guard(directory)
            self.assertEqual(requested_at, [410])

    def test_bootstrap_failure_still_releases_the_allocated_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); batch = batch_fixture(root)
            production = colab_h3.engine()
            job_file = root / 'job.json'
            source = root / '剧集/EP001/视频提示词.md'
            source.parent.mkdir(parents=True)
            ref = root / '输入/ref.png'; ref.parent.mkdir(); ref.write_bytes(b'fixture-image')
            prompt = 'subject_definitions: a\nsummary: a\nretention_analysis: a\ndetailed_description: a\noverall_soundscape: a\nnon_diegetic_music: N/A'
            source.write_text('## MOTION-EP001-001 · test\n- 输入参考图：REF-HERO（顺序：1）· 输入/ref.png《角色板》（用途：身份；控制：身份；不得控制：构图）\n\n### 可复制提示词\n' + '\n'.join('> '+s for s in prompt.splitlines()) + '\n')
            job_file.write_text(json.dumps({'job_id': 'TEST-001', 'modality': 'video',
                 'adapter': 'h3-colab', 'prompt': prompt,
                 'reference_bindings': [{'slot_id': 'REF-HERO', 'order': 1, 'path': '输入/ref.png',
                                        'label': '角色板', 'role': 'reference_image',
                                        'may_control': ['身份'], 'must_not_control': ['构图']}],
                 'source': '剧集/EP001/视频提示词.md', 'source_entry': 'MOTION-EP001-001',
                 'outputs': ['剧集/EP001/制作成果/video/test.mp4'],
                 'parameters': {'ratio': '9:16', 'width': 720, 'height': 1280, 'internal_width': 736,
                                'frame_rate': 24, 'frame_count': 124, 'duration': 5, 'steps': 20,
                                'sampler': 'res_multistep', 'scheduler': 'simple', 'seed': 1,
                                'prompt_language': 'en', 'model': 'minimax-h3-ref2va-pruned-int8-convrot'},
                 'overwrite': False}))
            preview = production.prepare_job(root, job_file)
            production.confirm_job(root, job_id='TEST-001', confirmation=preview['confirmation'])
            calls = []
            class Transport:
                session = batch['session']
                active = False
                def control(self, action, value=''):
                    calls.append((action, value))
                    if action == 'usage': return {'balance': 100, 'rate': 0, 'count': 0}
                    if action == 'assignments': return [{'endpoint': 'our-batch'}] if self.active else []
                    if action == 'session': return {'endpoint': 'our-batch'}
                    if action == 'unassign': self.active = False
                    return {'requested': True}
                def allocation_request(self, operation, gpu, request_id, deadline, resource):
                    calls.append((operation, gpu))
                    self.active = True
                    return {'state': 'assigned', 'endpoint': 'our-batch'}
                def execute(self, *args, **kwargs): return {}
                def upload(self, *args): pass
                def remote(self, directory, action, data=None):
                    return {'state': 'failed'} if action == 'boot' else {}
            with patch('subprocess.Popen'), self.assertRaisesRegex(colab_h3.H3Error, 'bootstrap_failed'):
                colab_h3.run_batch(batch, Transport(), production)
            self.assertIn(('unassign', 'our-batch'), calls)
            self.assertEqual(colab_h3.read_json(batch['resource_state'])['state'], 'released')

    def test_only_batch_endpoint_is_released_with_other_sessions_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); batch = batch_fixture(root)
            calls = []
            class Transport:
                endpoints = {'our-batch', 'someone-else'}
                def control(self, action, value=''):
                    calls.append((action, value))
                    if action == 'assignments': return [{'endpoint': endpoint} for endpoint in self.endpoints]
                    if action == 'usage': return {'balance': 98, 'rate': 10, 'count': 1}
                    if action == 'unassign': self.endpoints.remove(value)
                    return {'requested': True}
            state = {'endpoint': 'our-batch', 'baseline_endpoints': ['someone-else']}
            result = colab_h3.release_batch(batch, Transport(), state)
            self.assertEqual(result['state'], 'released')
            self.assertEqual([v for a, v in calls if a == 'unassign'], ['our-batch'])

    def test_unconfirmed_release_never_becomes_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); batch = batch_fixture(root)
            class Transport:
                def control(self, action, value=''):
                    if action == 'assignments': return [{'endpoint': 'our-batch'}]
                    if action == 'usage': return {'balance': 98, 'rate': 10, 'count': 1}
                    raise colab_h3.H3Error('network_unknown')
            with patch('time.sleep'), self.assertRaisesRegex(colab_h3.H3Error, 'release_unverified'):
                colab_h3.release_batch(batch, Transport(), {'endpoint': 'our-batch'})
            self.assertEqual(colab_h3.read_json(batch['resource_state'])['state'], 'release_unverified')


if __name__ == '__main__':
    unittest.main()
