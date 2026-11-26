import base64
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from test_drama_factory import FactoryFixture
from drama_factory import core, assets, delivery, finishing


class AssetTests(FactoryFixture):
    def fixture(self):
        directory = self.root / '剧集/EP001'
        directory.mkdir(parents=True)
        (directory / '视觉设定.md').write_text('## 地点 · 房间\n- 墙面：白色。\n- 声音方向：安静。\n')
        (directory / '图片提示词.md').write_text('## IMG-ROOM · 房间\n- 用途：地理\n- 视觉依据：地点 · 房间\n- 参考：无外部参考\n### 可复制提示词\n> A white room.\n')
        for name, entry in [('分镜.md', 'SHOT-EP001-001'), ('视频提示词.md', 'MOTION-EP001-001')]:
            (directory / name).write_text(f'## {entry} · 房间\n- 输入参考图：REF-ROOM（顺序：1）· old.png《房间》（用途：地理；控制：空间；不得控制：动作）\n')
        path = directory / '制作成果/images/IMG-ROOM.png'
        path.parent.mkdir(parents=True)
        path.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC'))
        return str(path.relative_to(self.root))

    def test_native_tool_unknown_cannot_be_dispatched_twice(self):
        self.fixture()
        assets.image_call(self.root, 'EP001', 'IMG-ROOM')
        assets.image_call(self.root, 'EP001', 'IMG-ROOM', action='dispatch')
        with self.assertRaisesRegex(core.FactoryError, 'already_dispatched'):
            assets.image_call(self.root, 'EP001', 'IMG-ROOM', action='dispatch')

    def test_only_known_technical_failure_can_retry_and_history_is_retained(self):
        self.fixture()
        assets.image_call(self.root, 'EP001', 'IMG-ROOM')
        assets.image_call(self.root, 'EP001', 'IMG-ROOM', action='dispatch')
        with self.assertRaisesRegex(core.FactoryError, 'definite_technical'):
            assets.image_call(self.root, 'EP001', 'IMG-ROOM', action='retry', reason='timeout is unknown')
        assets.image_call(self.root, 'EP001', 'IMG-ROOM', action='failed', failure='provider says not accepted', failure_kind='technical')
        retry = assets.image_call(self.root, 'EP001', 'IMG-ROOM', action='retry', reason='retry known transient rejection')
        self.assertEqual(retry['attempt'], 2)
        self.assertEqual(retry['prior_attempts'][0]['state'], 'failed')
        assets.image_call(self.root, 'EP001', 'IMG-ROOM', action='dispatch')
        assets.image_call(self.root, 'EP001', 'IMG-ROOM', action='failed', failure='policy rejection', failure_kind='policy')
        with self.assertRaisesRegex(core.FactoryError, 'definite_technical'):
            assets.image_call(self.root, 'EP001', 'IMG-ROOM', action='retry', reason='not permitted')

    def test_audio_only_change_preserves_visual_observation_but_wall_change_invalidates_it(self):
        path = self.fixture()
        version = assets.register(self.root, 'EP001', entry='IMG-ROOM', path=path, purpose='地理', provenance='synthetic fixture')['version']
        assets.observe(self.root, 'EP001', version, result='passed', method='actual_image_view', notes='synthetic fixture test')
        assets.select(self.root, 'EP001', version, slots=['REF-ROOM'], authorization='fixture')
        visual = self.root / '剧集/EP001/视觉设定.md'
        visual.write_text(visual.read_text().replace('安静', '雨声'))
        self.assertTrue(assets.has_current_review(self.root, 'EP001', path, '地理', core.file_sha(self.root / path)))
        visual.write_text(visual.read_text().replace('白色', '黑色'))
        self.assertFalse(assets.has_current_review(self.root, 'EP001', path, '地理', core.file_sha(self.root / path)))

    def test_selection_requires_observation_and_updates_both_owners(self):
        path = self.fixture()
        version = assets.register(self.root, 'EP001', entry='IMG-ROOM', path=path, purpose='地理', provenance='synthetic fixture')['version']
        with self.assertRaisesRegex(core.FactoryError, 'actual_review'):
            assets.select(self.root, 'EP001', version, slots=['REF-ROOM'], authorization='fixture')
        assets.observe(self.root, 'EP001', version, result='passed', method='actual_image_view', notes='synthetic fixture')
        selection = assets.select(self.root, 'EP001', version, slots=['REF-ROOM'], authorization='fixture')
        self.assertEqual(len(selection['consumers']), 2)
        for name in ('分镜.md', '视频提示词.md'):
            self.assertIn(path, (self.root / '剧集/EP001' / name).read_text())


class TimelineTests(FactoryFixture):
    def fixture(self):
        directory = self.root / '剧集/EP001'; directory.mkdir(parents=True)
        (directory / 'clip.mp4').write_bytes(b'media fixture')
        (directory / '剪辑单.md').write_text('''# EP001 剪辑单
- 成片目标时长：1秒
- 画幅与帧率：9:16 · 768×1344 · 24 fps
- 交付响度：-16 LUFS
- 字幕：中文
- 工程：OpenChatCut Pro
## CUT-EP001-001 · fixture
- 来源：MOTION-EP001-001 · 剧集/EP001/clip.mp4
- 入点：0.50
- 出点：1.50
- 时长：1.00
- 取舍：入点=fixture；出点=fixture
- 声音：原声
- 画面：不校
- 帧位：源 [12, 36)，成片 [0, 24)。
- 字幕 1：0.00-1.00 测试
''')
        info = {'width':768,'height':1344,'fps':'24','frames':48,'sha256':core.file_sha(directory/'clip.mp4')}
        with patch.object(delivery,'probe',return_value=info):
            return delivery.compile_timeline(self.root,'EP001')

    def test_cut_list_compiles_half_open_source_and_target_frames(self):
        value = self.fixture()
        self.assertEqual((value['clips'][0]['source_in'],value['clips'][0]['source_out']), (12,36))
        self.assertEqual(value['duration_frames'],24)
        self.assertEqual(value['spec']['editor'],'openchatcut')

    def test_readback_detects_ripple_caption_and_trim_changes(self):
        value=self.fixture()
        bindings={'elements':{'CUT-EP001-001':{'element_id':'clip'},'CUT-EP001-001-SUB-1':{'element_id':'caption'}}}
        snapshot={'editor':'openchatcut','fps':'24','canvas':[768,1344],'elements':[
            {'id':'clip','start':0,'duration':24,'source_in_seconds':'0.5','source_out_seconds':'1.5','params':{}},
            {'id':'caption','start':0,'duration':24,'params':{'content':'测试'}}]}
        self.assertTrue(delivery.compare(value,snapshot,bindings)['timing_matches'])
        snapshot['elements'][1]['start']=2
        self.assertFalse(delivery.compare(value,snapshot,bindings)['timing_matches'])
        with self.assertRaisesRegex(core.FactoryError,'editor_changed'):
            delivery.editor_actions(value,snapshot,bindings,expected_readback_sha='old')

    def test_fractional_rates_do_not_accumulate_float_error(self):
        self.assertEqual(delivery.frames('1001','30000/1001'),30000)
        self.assertEqual(delivery.frames('0.0416666667',24),1)

    def test_cli_subtitle_crosses_cut_and_stays_inside_film(self):
        directory = self.root / '剧集/EP001'
        directory.mkdir(parents=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=black:s=64x112:r=24:d=2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(directory / 'clip.mp4')], check=True)
        header = '# EP001 剪辑单\n- 画幅与帧率：768×1344 · 24 fps\n'
        blocks = []
        for index, start, end in [(1, '0.00', '1.00'), (2, '1.00', '2.00')]:
            blocks.append(f'''## CUT-EP001-{index:03d} · 字幕跨镜检查
- 来源：MOTION-EP001-001 · 剧集/EP001/clip.mp4
- 入点：{start}
- 出点：{end}
- 时长：1.00
- 取舍：入点=测试画面边界；出点=测试画面边界
- 声音：原声
- 画面：不校
''')
        document = header + blocks[0] + '- 字幕 1：0.75-1.25 跨镜字幕\n' + blocks[1]
        cut_list = directory / '剪辑单.md'
        cut_list.write_text(document)
        command = ['bash', str(core.ROOT / 'scripts/drama'), 'timeline', 'prepare',
                   str(self.root), '--episode', 'EP001']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        timeline = json.loads(result.stdout)
        self.assertEqual([(c['start'], c['duration']) for c in timeline['clips']], [(0, 24), (24, 24)])
        self.assertEqual((timeline['captions'][0]['start'], timeline['captions'][0]['end']), (18, 30))
        cut_list.write_text(document.replace('0.75-1.25', '0.75-2.25'))
        self.assertNotEqual(subprocess.run(command, capture_output=True, text=True).returncode, 0)

    def test_scene_binding_reconciles_inconsistent_editor_source_duration(self):
        settings={'fps':24,'width':768,'height':1344}
        snapshot={'tracks':[],'elements':[{'id':'clip-id','trackId':'v','type':'video'}]}
        details={'elements':[{'id':'clip-id','startTimeSeconds':0,'durationSeconds':1,
            'sourceDurationSeconds':1,'trimStartSeconds':0.5,'trimEndSeconds':0.5}]}
        result=delivery.openchatcut_readback(settings,snapshot,details,
            media={'assets':[{'id':'media','name':'clip.mp4','duration':2,'fps':24}]},
            scene_source='media clip.mp4 as sample on V1 from 0s len 1s id=clip-id-full')
        self.assertEqual(result['elements'][0]['source_out_seconds'],'3/2')
        self.assertEqual(len(result['metadata_warnings']),1)

    def test_chatcut_captions_use_native_track_items_and_shared_track_style(self):
        value = self.fixture(); value['spec']['editor'] = 'chatcut'
        snapshot = {'editor': 'chatcut', 'fps': '24', 'canvas': [768,1344], 'elements': []}
        bindings = {'media': {value['clips'][0]['sha256']: {'id': 'media'}}, 'video_track': 'video',
                    'project_id': 'project', 'timeline_id': 'timeline', 'caption_track': 'captions',
                    'caption_style_adopted': True}
        packet = delivery.editor_actions(value, snapshot, bindings, expected_readback_sha=core.fingerprint(snapshot))
        caption = next(a for a in packet['actions'] if a['entry'].endswith('SUB-1'))
        self.assertEqual(caption['tool'], 'mcp__chatcut__edit_captions')
        self.assertNotIn('appearance', caption['arguments']['json'])
        self.assertIn('create-item', caption['arguments']['json'])

    def test_existing_keyframed_volume_is_not_overwritten_by_static_gain(self):
        value = self.fixture()
        snapshot = {'editor': 'openchatcut', 'fps':'24','canvas':[768,1344], 'elements': [
            {'id':'clip', 'keyframes': {'volume':[{'timeSeconds':0,'value':-2}]}, 'params':{}}]}
        bindings = {'media': {value['clips'][0]['sha256']: {'id': 'media'}}, 'video_track':'v', 'caption_track':'c',
                    'elements':{'CUT-EP001-001':{'element_id':'clip','track_id':'v'}}, 'caption_params':{'fontFamily':'Fixture'}}
        packet = delivery.editor_actions(value, snapshot, bindings, expected_readback_sha=core.fingerprint(snapshot))
        update = next(a for a in packet['actions'] if a['entry'] == 'CUT-EP001-001')
        self.assertNotIn('volume', update['arguments']['updates'][0]['patch']['params'])
        self.assertTrue(any('关键帧' in r['reason'] for r in packet['host_work']))

    def test_lost_editor_write_blocks_duplicate_insert_across_rebuilt_packets(self):
        value = self.fixture()
        snapshot = {'editor':'openchatcut','fps':'24','canvas':[768,1344],'elements':[]}
        bindings = {'media': {value['clips'][0]['sha256']: {'id':'media'}}, 'video_track':'v',
                    'caption_track':'c','caption_params':{'fontFamily':'Fixture'}}
        packet = delivery.prepare_exchange(self.root,'EP001',value,snapshot,bindings,expected_readback_sha=core.fingerprint(snapshot))
        action_id = packet['actions'][0]['action_id']
        delivery.exchange_action(self.root, packet['id'], action_id, state='dispatch', readback=snapshot)
        with self.assertRaisesRegex(core.FactoryError, 'already_dispatched'):
            delivery.exchange_action(self.root, packet['id'], action_id, state='dispatch', readback=snapshot)
        snapshot['unrelated_change'] = True
        new = delivery.prepare_exchange(self.root,'EP001',value,snapshot,bindings,expected_readback_sha=core.fingerprint(snapshot))
        with self.assertRaisesRegex(core.FactoryError, 'earlier_editor_write_unknown'):
            delivery.exchange_action(self.root,new['id'],new['actions'][0]['action_id'],state='dispatch',readback=snapshot)


class FinishingTests(FactoryFixture):
    def fixture(self):
        directory=self.root/'剧集/EP001';directory.mkdir(parents=True)
        (directory/'剪辑单.md').write_text('# 用户锁定成片，仅做2倍增强。\n')
        source=directory/'film.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=96x64:r=24',
                        '-t','1','-c:v','libx264','-pix_fmt','yuv420p','-y',str(source)],check=True)
        return str(source.relative_to(self.root))

    def test_original_fps_prepares_full_film_without_rife_weights_or_gpu(self):
        source=self.fixture()
        value=finishing.prepare(self.root,'EP001','finish-fixture',source=source,target_fps=24,cuts=[],
            authorization='fixture',max_ccu=2,max_seconds=600,cuts_evidence='synthetic single shot',
            short_edge=128,rife_zip='/missing.zip')
        self.assertEqual(value['state'],'prepared_no_gpu')
        job=core.read_json(Path(value['batch']).with_name('job.json'))
        self.assertEqual(job['parameters']['output_width'],192)
        self.assertEqual(job['parameters']['frames'],24)
        self.assertEqual(job['parameters']['target_fps'],24)
        self.assertFalse(Path(value['batch']).with_name('resource.json').exists())

    def test_invalid_cuts_fail_before_any_task_created(self):
        source=self.fixture()
        with self.assertRaisesRegex(core.FactoryError,'cut_frames'):
            finishing.prepare(self.root,'EP001','invalid',source=source,target_fps=24,cuts=[12,12],
                authorization='fixture',max_ccu=2,max_seconds=600,cuts_evidence='fixture')
        self.assertFalse((self.root/core.LEDGER/'colab/invalid').exists())

    def test_existing_final_recovers_lost_receipt_without_model_loading(self):
        import relay_vsr_comfy_remote as remote
        source = self.fixture()
        directory = self.root/'remote'; directory.mkdir()
        import shutil
        shutil.copyfile(self.root/source, directory/'input.mp4')
        subprocess.run(['ffmpeg','-v','error','-i',str(directory/'input.mp4'),'-vf','scale=192:128',
                        '-c:v','libx264',str(directory/'master.mp4')], check=True)
        spec = {'source_fps':24, 'target_fps':24, 'frames':24, 'output_width':192, 'output_height':128,
                'master_filename':'master.mp4', 'input_sha256':core.file_sha(directory/'input.mp4')}
        with patch.object(remote,'setup_comfy',side_effect=AssertionError('must reuse master')):
            remote.main(directory,spec)
            final_sha=core.file_sha(directory/'final.mp4')
            (directory/'receipt.json').unlink()
            remote.main(directory,spec)
        self.assertEqual(core.file_sha(directory/'final.mp4'),final_sha)
        self.assertTrue(core.read_json(directory/'receipt.json')['recovered_existing_final'])

    def test_deadline_guard_releases_only_owned_endpoint_even_when_salvage_fails(self):
        import relay_vsr_comfy_colab as controller
        from unittest.mock import Mock
        path=self.root/'resource.json'
        core.write_json(path,{'state':'running','endpoint':'owned','deadline':0,'worker_started':False})
        batch={'state_file':str(path),'session':'fixture','execution_release':'factory-v1'}
        provider=Mock()
        live = {'owned', 'unrelated'}
        def control(action, value=''):
            if action == 'assignments':
                return [{'endpoint': endpoint} for endpoint in live]
            self.assertEqual((action, value), ('unassign', 'owned'))
            live.remove(value)
        provider.control.side_effect = control
        with patch.object(controller.h,'Colab',return_value=provider), patch.object(controller,'recover_and_release',side_effect=RuntimeError('transport_lost')):
            result=controller.guard(batch)
        self.assertEqual(result['state'],'released')
        self.assertTrue(result['artifacts_may_be_missing'])
        provider.control.assert_any_call('unassign','owned')
        self.assertEqual(live, {'unrelated'})
