import unittest
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

import colab_h3 as h
import h3_episode


class NativeEpisodeTests(unittest.TestCase):
    def test_compiler_previews_one_independent_output_and_separate_continuation_master(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'short-drama.json').write_text(json.dumps({'format': {'aspect_ratio': '9:16'},
                'creator_authority': {'production_profile': {'status': 'accepted',
                    'choices': {'video_prompt_dialect': 'minimax-h3'}}}}))
            (root/'ref.png').write_bytes(b'reference')
            source = root/'剧集/EP001/视频提示词.md'; source.parent.mkdir(parents=True)
            prompt = self.job(6)['prompt']
            source.write_text('\n'.join(f'## MOTION-EP001-00{i} · 测试\n- 时长：6s\n'
                f'- 续接来源：{parent}\n'
                '- 输入参考图：REF-HERO（顺序：1）· ref.png《人物板》（用途：身份；控制：身份；不得控制：构图）\n'
                '### 可复制提示词\n' + '\n'.join('> '+line for line in prompt.splitlines())
                for i, parent in [(1, '无'), (2, 'MOTION-EP001-001')]))
            actual_home = root / 'home'
            actual_home.mkdir()
            linked_home = root / 'linked-home'
            linked_home.symlink_to(actual_home, target_is_directory=True)
            with patch.object(Path, 'home', return_value=linked_home), \
                    patch.object(h, 'Colab', side_effect=AssertionError('No provider during preview')):
                prepared = h3_episode.prepare(root, 'EP001', 'test-output')
            batch = h.read_json(prepared['batch']); engine = h.engine()
            self.assertTrue(Path(batch['adapter_config']).is_relative_to(actual_home))
            jobs = [engine._read_job(root, job) for job in batch['jobs']]
            self.assertEqual(len(jobs[0]['outputs']), 1)
            self.assertEqual(len(jobs[1]['outputs']), 2)
            self.assertIn('/context-masters/', jobs[1]['outputs'][1])

    def job(self, duration, parent=None, ratio='16:9'):
        return {'modality': 'video', 'outputs': ['native.mp4', 'master.mp4'] if parent else ['native.mp4'],
            'prompt': 'subject_definitions: S1\nsummary: action\nretention_analysis: identity\ndetailed_description: action\noverall_soundscape: wind\nnon_diegetic_music: N/A',
            'parameters': h.episode_parameters('comfy_kitchen_int8', 42, duration, ratio, parent),
            'reference_bindings': [{'role':'reference_image','label':'hero','may_control':['identity'],'must_not_control':['pose']}]}

    def test_fifteen_second_landscape_is_native_and_has_no_spatial_edit(self):
        job = self.job(15)
        graph = h.build_graph(job, ['a'*64+'.png'], 'test')
        self.assertEqual((graph['6']['inputs']['width'], graph['6']['inputs']['height']), (1344,768))
        self.assertEqual(graph['6']['inputs']['length'], 362)
        self.assertTrue(all(n['class_type'] not in ('ImageScale','ImageCrop') for n in graph.values()))
        self.assertEqual(graph['16']['inputs']['images'], ['13',0])
        self.assertEqual(sum(n['class_type'] == 'CreateVideo' for n in graph.values()), 1)
        self.assertEqual(sum(n['class_type'] == 'SaveVideo' for n in graph.values()), 1)
        self.assertIn('90', graph)

    def test_independent_clip_rejects_reintroduced_duplicate_output(self):
        job = self.job(10)
        job['outputs'].append('context-master.mp4')
        with self.assertRaisesRegex(h.H3Error, 'video_outputs_required'):
            h.build_graph(job, ['a'*64+'.png'], 'test')

    def test_continuation_master_is_not_put_in_the_viewing_directory(self):
        stem = '剧集/EP001/制作成果/video/MOTION-EP001-002-batch'
        self.assertEqual(h.episode_outputs(stem), [stem + '-native.mp4'])
        self.assertEqual(h.episode_outputs(stem, 'previous'), [stem + '-native.mp4',
            '剧集/EP001/制作成果/context-masters/MOTION-EP001-002-batch-context-master.mp4'])

    def test_shorter_portrait_uses_its_own_valid_grid_without_forcing_five_seconds(self):
        job=self.job(6,ratio='9:16')
        graph=h.build_graph(job,['a'*64+'.png'],'test')
        self.assertEqual((graph['6']['inputs']['width'],graph['6']['inputs']['height']), (768,1344))
        self.assertEqual(graph['6']['inputs']['length'],158)

    def test_continuation_removes_only_the_same_five_frame_head_from_audio_and_video(self):
        graph=h.build_graph(self.job(12,parent='prior-job'),['a'*64+'.png'],'test',
            context={'path':'context-00000000-0000-4000-8000-000000000001_00001.safetensors'})
        self.assertEqual(graph['92']['inputs']['context_length'],'5')
        self.assertEqual(graph['93']['inputs']['trim_frames'],['92',1])
        self.assertEqual(graph['16']['inputs']['images'],['93',0])
        self.assertEqual(graph['16']['inputs']['audio'],['93',1])
        self.assertEqual(graph['18']['inputs']['images'],['13',0])
        self.assertEqual(sum(n['class_type'] == 'SaveVideo' for n in graph.values()), 2)

    def test_audio_and_image_reference_numbers_are_independent(self):
        job=self.job(11)
        job['reference_bindings'].append({'role':'reference_audio','label':'voice','may_control':['timbre'],'must_not_control':['words']})
        graph=h.build_graph(job,['a'*64+'.png','b'*64+'.wav'],'test')
        self.assertEqual(graph['6']['inputs']['ref_audios.ref_audio_0'],['21',0])
        self.assertNotIn('<Picture 2>',graph['6']['inputs']['prompt'])

    def test_duration_limits_are_checked_before_gpu_allocation(self):
        for invalid in [3,16,True,5.5]:
            with self.subTest(invalid=invalid), self.assertRaises(h.H3Error):
                self.job(invalid)


class EpisodeLifecycleTests(unittest.TestCase):
    def test_failed_query_preserves_vm_and_resume_collects_without_allocating_or_resubmitting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'short-drama.json').write_text('{}')
            (root/'ref.png').write_bytes(b'fixture-reference')
            setup=root/'setup.json';setup.write_text('{}')
            job=NativeEpisodeTests().job(11)
            job['reference_bindings'][0]['path']='ref.png'
            job.update(job_id='ONE',fingerprint='current')
            batch={'schema_version':1,'profile':h.EPISODE_PROFILE,'batch_id':'test',
                'batch_file':str(root/'batch.json'),'project':str(root),'jobs':['ONE'],
                'gpu':'G4','session':'one-runtime','max_seconds':7200,'max_ccu':30,
                'max_rate':15,'max_attempts_per_job':1,'resource_state':str(root/'resources.json'),
                'setup_manifest':str(setup),'setup_sha256':h.digest(setup),
                'implementation_sha256':h.implementation_digest(),'adapter_config':str(root/'adapter.json')}

            class Provider:
                session = batch['session']
                allocated=0; released=0
                def control(self,action,*args):
                    if action=='usage':return {'balance':100,'rate':0,'count':0}
                    if action=='assignments':return [{'endpoint':'owned'}] if self.allocated and not self.released else []
                    if action=='session':return {'endpoint':'owned'}
                    if action=='unassign':self.released+=1;return {'requested':True}
                def allocation_request(self, operation, gpu, request_id, deadline, resource):
                    assert operation == 'attempt'
                    self.allocated += 1
                    return {'state': 'assigned', 'endpoint': 'owned'}
                def execute(self,*args,**kwargs):return {}
                def upload(self,*args):pass
                def remote(self,directory,action,*args):
                    return {'state':'ready','hardware':'fixture'} if action=='boot' else {}

            class Production:
                MAX_RUN_RECORD_BYTES=1024
                history=[];submitted=0;collected=0
                def _read_job(self,*args):return job
                def _inputs_current(self,*args):return True
                def _read_run_history(self,*args):return self.history
                def _job_key(self,*args):return 'key'
                def _metadata_read_json(self,*args,**kwargs):return {'fingerprint':'current'}
                def run_job(self,*args,**kwargs):
                    self.submitted+=1
                    self.history=[{'status':'failed','provider_job_id':'original-request'}]
                    raise RuntimeError('temporary-query-failure')
                def collect_job(self,*args,**kwargs):
                    self.collected+=1
                    if self.collected==1:raise RuntimeError('still-unavailable')
                    outputs=[]
                    for name in job['outputs']:
                        file=root/name;file.write_bytes(b'verified-provider-output')
                        outputs.append({'path':name,'sha256':h.digest(file)})
                    self.history=[{'status':'succeeded','outputs':outputs}]
                    return {'job_id':'ONE'}

            provider=Provider();production=Production()
            with patch.object(h.subprocess,'Popen') as supervisor:
                first=h.run_batch(batch,provider,production)
                self.assertEqual(first['state'],'paused_recoverable')
                self.assertEqual(provider.released,0)
                supervisor.return_value.terminate.assert_not_called()
                deadline=h.read_json(batch['resource_state'])['deadline']
                final=h.run_batch(batch,provider,production,resume=True)
            self.assertEqual(final['state'],'released')
            self.assertEqual(final['completed_jobs'],1)
            self.assertEqual(provider.allocated,1)
            self.assertEqual(provider.released,1)
            self.assertEqual(production.submitted,1)
            self.assertEqual(h.read_json(batch['resource_state'])['deadline'],deadline)

    def test_resume_finishes_unverified_allocation_release_with_original_request_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            resource = root / 'resources.json'
            request_id = 'b0c2a4f5-4edf-4fb9-8d35-936ee19dde49'
            h.write_json(resource, {'state': 'release_unverified', 'baseline_endpoints': ['unrelated']})
            h.write_json(root / 'allocation-request.json', {
                'session': 'session', 'gpu': 'G4', 'request_id': request_id, 'attempts': 12,
            })
            batch = {'resource_state': str(resource), 'session': 'session',
                     'profile': h.EPISODE_PROFILE, 'jobs': []}
            from unittest.mock import Mock
            provider = Mock()
            provider.session = 'session'
            provider.allocation_request.return_value = {'state': 'assigned', 'endpoint': 'owned'}
            endpoints = {'unrelated', 'owned'}
            def control(action, value=''):
                if action == 'assignments':
                    return [{'endpoint': endpoint} for endpoint in endpoints]
                if action == 'usage':
                    return {'balance': 100, 'count': len(endpoints), 'rate': 15}
                self.assertEqual((action, value), ('unassign', 'owned'))
                endpoints.remove(value)
            provider.control.side_effect = control
            with patch.object(h, 'validate_batch', return_value=root):
                result = h.run_batch(batch, provider, Mock(), resume=True)
            self.assertEqual(result['action'], 'allocation_reconciled_no_submission')
            self.assertEqual(result['state'], 'released')
            self.assertEqual(endpoints, {'unrelated'})
            provider.allocation_request.assert_called_once()
            self.assertEqual(provider.allocation_request.call_args.args[:3], ('lookup', 'G4', request_id))

    def test_read_timeout_retries_status_but_never_repeats_submit(self):
        client=h.Colab.__new__(h.Colab)
        with patch.object(client,'execute',side_effect=[h.H3Error('colab_response_unknown'),{'state':'succeeded'}]) as execute, patch.object(h.time,'sleep'):
            self.assertEqual(client.remote('/content/vision-h3-test','status',{})['state'],'succeeded')
            self.assertEqual(execute.call_count,2)
        with patch.object(client,'execute',side_effect=h.H3Error('colab_response_unknown')) as execute:
            with self.assertRaises(h.H3Error):client.remote('/content/vision-h3-test','submit',{})
            self.assertEqual(execute.call_count,1)


if __name__ == '__main__':
    unittest.main()
