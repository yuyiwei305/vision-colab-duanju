"""V4 scene routing, production snapshots and deployment contracts; no GPU/network."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import colab_h3 as h
import colab_h3_remote as remote
import h3_episode
import h3_workflow_v4 as v4


def prompt(action):
    return ('subject_definitions: <Subject 1> holds a sword.\nsummary: ' + action +
            '\nretention_analysis: identity\ndetailed_description: ' + action +
            '\noverall_soundscape: wind\nnon_diegetic_music: N/A')


class RoutingTests(unittest.TestCase):
    def test_dialogue_with_weapon_identity_is_drama(self):
        self.assertEqual(v4.select_scene('', prompt('Two people talk quietly.'))['scene_profile'], 'drama')

    def test_performed_chinese_and_english_combat_is_action(self):
        for action in ['He parries and slashes toward his opponent.', '两人近身搏斗，挥砍交锋。', 'She sprints after the escaping opponent.']:
            with self.subTest(action=action):
                self.assertEqual(v4.select_scene('', prompt(action))['scene_profile'], 'action')

    def test_negative_directions_and_quoted_dialogue_do_not_activate_combat_loras(self):
        for action in ['No fighting; two people sit quietly.', '她说：“我们要打斗吗？”随后坐下。',
                       'She says "I will fight you" and remains seated.', 'They talk about fighting.']:
            with self.subTest(action=action):
                self.assertEqual(v4.select_scene('', prompt(action))['scene_profile'], 'drama')

    def test_explicit_formal_source_override_is_honored(self):
        self.assertEqual(v4.select_scene('- H3档位：文戏', prompt('He fights.'))['scene_profile'], 'drama')
        self.assertTrue(v4.select_scene('- H3档位：打斗', prompt('He sits.'))['explicit'])

    def test_unknown_override_fails_instead_of_silently_auto_routing(self):
        with self.assertRaisesRegex(ValueError, 'invalid_scene_profile_override'):
            v4.select_scene('- H3档位：wrong', prompt('He sits.'))

    def test_entire_continuation_chain_uses_action_if_any_segment_requires_it(self):
        routes = v4.route_chains([('ONE', '无', v4.select_scene('', prompt('He sits.'))),
                                  ('TWO', 'ONE', v4.select_scene('', prompt('He fights.')))])
        self.assertEqual([r['scene_profile'] for r in routes.values()], ['action', 'action'])

    def test_conflicting_explicit_chain_profiles_fail_before_prepare(self):
        with self.assertRaisesRegex(ValueError, 'continuation_scene_profile_conflict'):
            v4.route_chains([('ONE', '无', v4.select_scene('- H3档位：文戏', prompt('He sits.'))),
                            ('TWO', 'ONE', v4.select_scene('- H3档位：打斗', prompt('He fights.')))])


class GraphTests(unittest.TestCase):
    def job(self, scene='drama', parent=None, duration=5):
        return {'modality': 'video', 'outputs': ['native.mp4', 'master.mp4'] if parent else ['native.mp4'],
                'parameters': v4.parameters(scene, 42, duration, '16:9', parent),
                'prompt': prompt('He talks.' if scene == 'drama' else 'He fights.'),
                'reference_bindings': [{'role': 'reference_image', 'label': '身份',
                                        'may_control': ['身份'], 'must_not_control': ['动作']}]}

    def reference_job(self, images, audio):
        job = self.job('action', parent='previous')
        picture = job['reference_bindings'][0]
        job['reference_bindings'] = [picture.copy() for _ in range(images)]
        job['reference_bindings'] += [{'role': 'reference_audio'} for _ in range(audio)]
        names = [f'{index:064x}.png' for index in range(images)]
        names += [f'{index:064x}.wav' for index in range(audio)]
        return job, names

    def test_large_reference_graph_preserves_measurement_and_all_media_links(self):
        for images in (7, 9):
            with self.subTest(images=images):
                job, names = self.reference_job(images, 3)
                graph = v4.build_graph(job, names, 'preflight')
                bridge = graph['41']['inputs']
                self.assertEqual(graph['31']['class_type'], 'H3V4Measure')
                self.assertEqual(graph['9']['inputs']['model'], ['31', 0])
                self.assertEqual(graph['11']['inputs']['low_res_model'], ['31', 0])
                self.assertEqual((bridge['image_count'], bridge['audio_count']), (images, 3))
                media = [(f'image_{i}', 'LoadImage', 'image') for i in range(1, images + 1)]
                media += [(f'audio_{i}', 'LoadAudio', 'audio') for i in range(1, 4)]
                linked_ids = {bridge[slot][0] for slot, _, _ in media}
                self.assertEqual(len(linked_ids), images + 3)
                self.assertEqual(linked_ids, {node for node, value in graph.items()
                                            if value['class_type'] in ('LoadImage', 'LoadAudio')})
                for (slot, kind, field), name in zip(media, names):
                    self.assertEqual(graph[bridge[slot][0]], {'class_type': kind, 'inputs': {field: name}})

    def test_reference_capacity_rejects_image_audio_and_combined_overflow(self):
        for images, audio in ((0, 0), (10, 0), (7, 4), (9, 4)):
            with self.subTest(images=images, audio=audio):
                job, names = self.reference_job(images, audio)
                with self.assertRaisesRegex(ValueError, 'unsupported_v4_job_or_references'):
                    v4.build_graph(job, names, 'preflight')

    def test_native_voice_uses_a_slot_and_counts_toward_the_combined_limit(self):
        job, names = self.reference_job(9, 2)
        job['parameters']['native_voice_references'] = [{}]
        graph = v4.build_graph(job, names, 'preflight')
        self.assertEqual(graph['41']['inputs']['audio_count'], 3)
        self.assertEqual(graph['41']['inputs']['audio_3'], ['70', 0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            runtime = h.read_json(v4.RUNTIME_PATH)
            runtime['capabilities']['max_total_references'] = 11
            path.write_text(json.dumps(runtime))
            with patch.object(v4, 'RUNTIME_PATH', path):
                with self.assertRaisesRegex(ValueError, 'too_many_native_voice_references'):
                    v4.build_graph(job, names, 'preflight')
                job['parameters'].pop('native_voice_references')
                job['reference_bindings'].append({'role': 'reference_audio'})
                with self.assertRaisesRegex(ValueError, 'unsupported_v4_job_or_references'):
                    v4.build_graph(job, names + ['c'*64+'.wav'], 'preflight')

    def test_documented_lora_stacks_are_mutually_exclusive(self):
        expected = {'drama': [('minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors', .7),
                             ('h3-realism-people-t2v-i2v-r2v.safetensors', .8),
                             ('Minimax H3真实电影质感V0.1.safetensors', .3)],
                    'action': [('minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors', .7),
                               ('H3_Combat_V2.safetensors', .8), ('FrameRush-Minimax-V2_c2-st1500.safetensors', .5),
                               ('H3_speed_slider_1.1.safetensors', 5.0)]}
        for scene, loras in expected.items():
            graph = h.build_graph(self.job(scene), ['a'*64+'.png'], 'preflight')
            actual = [(n['inputs']['lora_name'], n['inputs']['strength_model']) for n in graph.values()
                      if n['class_type'] == 'LoraLoaderModelOnly']
            self.assertEqual(actual, loras)
            self.assertEqual(graph['31']['inputs']['expected_nfe'], 9 if scene == 'drama' else 13)
            self.assertEqual(graph['11']['inputs']['transition_step'], 6 if scene == 'drama' else 8)

    def test_reference_numbering_audio_and_fullframe_encoding_survive_compilation(self):
        job = self.job()
        job['reference_bindings'].append({'role': 'reference_audio'})
        graph = h.build_graph(job, ['a'*64+'.png', 'b'*64+'.wav'], 'preflight')
        self.assertEqual(graph['41']['inputs']['image_1'], ['200', 0])
        self.assertEqual(graph['41']['inputs']['audio_1'], ['201', 0])
        self.assertEqual(graph['6']['inputs']['ref_image_size'], '2k')
        self.assertFalse(graph['11']['inputs']['highres_tiling'])
        self.assertEqual(graph['17']['inputs']['crf'], 12)
        self.assertTrue(graph['6']['inputs']['prompt'].startswith('r34l1sm\n'))
        classes = {n['class_type'] for n in graph.values()}
        self.assertFalse(classes & {'H3PikaFinalize', 'MiniMaxLowVRAMAttention', 'MiniMaxChunkFeedForward',
                                    'GoohaiMinimaxH3TiledSampler', 'ImageCrop', 'ImageScale'})

    def test_continuation_uses_real_latent_and_trims_same_audio_and_video_head(self):
        graph = h.build_graph(self.job('action', parent='previous'), ['a'*64+'.png'], 'preflight',
                             {'path': 'context-00000000-0000-4000-8000-000000000001_00001.safetensors'})
        self.assertEqual(graph['11']['inputs']['positive'], ['92', 0])
        self.assertEqual(graph['17']['inputs']['images'], ['93', 0])
        self.assertEqual(graph['17']['inputs']['audio'], ['93', 1])
        self.assertEqual(graph['19']['inputs']['images'], ['16', 0])
        self.assertEqual(graph['90']['inputs']['latent'], ['11', 0])

    def test_parameter_drift_and_unsafe_assets_fail_before_submission(self):
        for key, value in [('highres_tiling', True), ('temporal_chunking', True), ('steps', 6)]:
            job = self.job(); job['parameters'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(h.H3Error, 'unsupported_v4_parameters'):
                h.build_graph(job, ['a'*64+'.png'], 'preflight')
        with self.assertRaisesRegex(h.H3Error, 'unsafe_reference_filename'):
            h.build_graph(self.job(), ['../private.png'], 'preflight')

    def test_easy_nearest_frame_grid_is_not_old_pika_ceiling(self):
        self.assertEqual(v4.parameters('drama', 1, 6)['frame_count'], 141)
        self.assertEqual(v4.parameters('drama', 1, 15)['frame_count'], 362)


class DeploymentTests(unittest.TestCase):
    def test_exact_source_patch_applies_once_and_rejects_source_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'node.py'; path.write_text('chunked = True\n')
            patch_spec = {'path': 'node.py', 'before_sha256': h.digest(path),
                          'after_sha256': hashlib.sha256(b'chunked = False\n').hexdigest(),
                          'replacements': [{'before': 'chunked = True', 'after': 'chunked = False'}]}
            remote.apply_source_patches(directory, [patch_spec])
            self.assertEqual(path.read_text(), 'chunked = False\n')
            with self.assertRaisesRegex(RuntimeError, 'plugin_patch_source_mismatch'):
                remote.apply_source_patches(directory, [patch_spec])

    def test_manifest_covers_both_scene_stacks_and_removes_hidden_temporal_chunking(self):
        runtime = h.read_json(h.ROOT/v4.RUNTIME)
        paths = {Path(m['path']).name for m in runtime['models']}
        self.assertIn('H3_Combat_V2.safetensors', paths)
        self.assertIn('Minimax H3真实电影质感V0.1.safetensors', paths)
        self.assertNotIn('pika_h3_flex_ref2va_v1_bf16.safetensors', paths)
        self.assertEqual(len(paths), 11)
        selflift = next(p for p in runtime['custom_archives'] if p['name']=='selflift-Avatar')
        replacements = selflift['patches'][0]['replacements']
        self.assertTrue(any('enable_chunking=False' in r['after'] for r in replacements))
        self.assertTrue(any(r['after']=='temporal_window = T' for r in replacements))


class OfficialPreparationTests(unittest.TestCase):
    def test_v4_cannot_borrow_another_projects_historical_authorization(self):
        with self.assertRaisesRegex(h.H3Error, 'current_task_authorization_basis_required'):
            h3_episode.prepare('/unused', 'EP001', 'offline-v4', pipeline='v4')

    def test_episode_prepare_freezes_scene_routes_without_colab_access(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            root = Path(directory)
            (root/'short-drama.json').write_text(json.dumps({'format': {'aspect_ratio': '16:9'},
                'creator_authority': {'production_profile': {'status': 'accepted',
                    'choices': {'video_prompt_dialect': 'minimax-h3'}}}}))
            (root/'ref.png').write_bytes(b'fixture-identity')
            source = root/'剧集/EP001/视频提示词.md'; source.parent.mkdir(parents=True)
            sections = []
            for index, action in [(1, 'He talks quietly.'), (2, 'He parries and slashes.')]:
                sections.append(f'## MOTION-EP001-00{index} · 测试\n- 分镜：SHOT-EP001-00{index}\n- 时长：5s\n- 续接来源：无\n'
                    '- 输入参考图：REF-A（顺序：1）· ref.png《身份板》（用途：身份；控制：身份；不得控制：动作）\n'
                    '### 可复制提示词\n'+'\n'.join('> '+line for line in prompt(action).splitlines()))
            source.write_text('\n\n'.join(sections))
            with patch.object(Path, 'home', return_value=Path(config).resolve()), patch.object(h.Colab, 'control', side_effect=AssertionError('network forbidden')):
                result = h3_episode.prepare(root, 'EP001', 'offline-v4', pipeline='v4',
                    authorization_basis='离线夹具，不授权GPU', max_ccu=1, max_seconds=300)
            batch = h.read_json(result['batch'])
            jobs = [h.engine()._read_job(root, key) for key in batch['jobs']]
            self.assertEqual([j['parameters']['scene_profile'] for j in jobs], ['drama', 'action'])
            self.assertTrue(all('scene_routing' in j['parameters'] for j in jobs))
            self.assertEqual(batch['authorization_basis'], '离线夹具，不授权GPU')
            self.assertFalse(Path(batch['resource_state']).exists())
            check = subprocess.run([sys.executable, str(h.SELF), 'preflight', '--offline', '--batch', result['batch']],
                                   capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stdout+check.stderr)
            self.assertIn('offline_preflight_pass', check.stdout)


if __name__ == '__main__': unittest.main()
