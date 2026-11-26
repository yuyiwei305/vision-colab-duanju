"""Public-CLI checks for automatic H3 scene continuity planning; no GPU."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import colab_h3


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'scripts/h3_production.py'


class ContinuityPlanningTests(unittest.TestCase):
    def test_prepare_runs_automatic_planning_before_requiring_fresh_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (root / '创作者决策').mkdir()
            (root / '创作者决策/项目约定.md').write_text('# 项目\n', encoding='utf-8')
            (root / 'short-drama.json').write_text(json.dumps({
                'creator_authority': {'production_profile': {'status': 'accepted',
                    'choices': {'video_prompt_dialect': 'minimax-h3'}}}}))
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 测试\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)), encoding='utf-8')
            result = subprocess.run([sys.executable, str(ENTRY), 'prepare', '--project', str(root),
                '--episode', 'EP001', '--pipeline', 'v4', '--batch-id', 'auto-test',
                '--authorization', 'offline test', '--max-ccu', '1', '--max-seconds', '60'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('专项检查记录', result.stderr)
            self.assertIn('- 续接来源：MOTION-EP001-001', video.read_text())
            self.assertFalse((root / '.short-drama').exists())

    def test_prepare_does_not_rewrite_an_unaccepted_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (root / '创作者决策').mkdir()
            (root / '创作者决策/项目约定.md').write_text('# 项目\n')
            (root / 'short-drama.json').write_text(json.dumps({
                'creator_authority': {'production_profile': {'status': 'unset', 'choices': {}}}}))
            (ep / '分镜.md').write_text('## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n')
            video = ep / '视频提示词.md'
            original = '## MOTION-EP001-001 · 一\n- 分镜：SHOT-EP001-001\n- 续接来源：自动\n'
            video.write_text(original)
            result = subprocess.run([sys.executable, str(ENTRY), 'prepare', '--project', str(root),
                '--episode', 'EP001', '--pipeline', 'v4', '--batch-id', 'auto-test',
                '--authorization', 'offline test', '--max-ccu', '1', '--max-seconds', '60'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('H3', result.stderr)
            self.assertEqual(video.read_text(), original)

    def test_strategy_reason_with_separator_is_safe_to_replan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n'
                '- H3接续策略：续接\n- H3接续理由：同机位 | 动作继续。\n')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)))
            command = [sys.executable, str(ENTRY), 'continuity', '--project', str(root), '--episode', 'EP001', '--apply']
            first = subprocess.run(command, text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(first.returncode, 0, first.stderr)
            before = video.read_text()
            second = subprocess.run(command, text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(video.read_text(), before)

    def test_snapshot_can_select_a_planned_shot_while_another_is_not_yet_planned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (root / 'short-drama.json').write_text(json.dumps({
                'creator_authority': {'production_profile': {'status': 'accepted',
                    'choices': {'video_prompt_dialect': 'minimax-h3'}}}}))
            for name in ('剧本.md', '视觉设定.md'):
                (ep / name).write_text('# 测试\n')
            (ep / '分镜.md').write_text('## SHOT-EP001-001 · 一\n- 起点：她站着。\n')
            (ep / '视频提示词.md').write_text(
                '## MOTION-EP001-001 · 一\n- 分镜：SHOT-EP001-001\n- 时长：6s\n- 续接来源：无\n- 输入参考图：无\n'
                '### 可复制提示词\n> summary: action\n'
                '## MOTION-EP001-002 · 二\n- 续接来源：自动\n')
            result = subprocess.run([sys.executable, str(ENTRY), 'snapshot', '--project', str(root),
                '--episode', 'EP001', '--entry', 'MOTION-EP001-001'], text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_automatic_plan_compiles_to_a_real_v4_context_dependency_without_gpu(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (root / 'short-drama.json').write_text(json.dumps({
                'format': {'aspect_ratio': '16:9'},
                'creator_authority': {'production_profile': {'status': 'accepted',
                    'choices': {'video_prompt_dialect': 'minimax-h3'}}}}))
            (root / 'ref.png').write_bytes(b'offline-reference')
            for name in ('剧本.md', '视觉设定.md'):
                (ep / name).write_text('# 离线夹具\n')
            ref = '- 输入参考图：REF-HERO（顺序：1）· ref.png《人物板》（用途：身份；控制：身份；不得控制：构图）\n'
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n' + ref +
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n' + ref)
            (ep / '视频提示词.md').write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 时长：6s\n'
                '- H3上下文：22v18a\n- 续接来源：自动\n' + ref +
                '### 可复制提示词\n> subject_definitions: S1\n> summary: action\n'
                '> retention_analysis: identity\n> detailed_description: action\n'
                '> overall_soundscape: wind\n> non_diegetic_music: N/A\n'
                for i in (1, 2)))
            planned = subprocess.run([sys.executable, str(ENTRY), 'continuity', '--project', str(root),
                '--episode', 'EP001', '--apply'], text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(planned.returncode, 0, planned.stderr)
            scope = ['--project', str(root), '--episode', 'EP001']
            snap = subprocess.run([sys.executable, str(ENTRY), 'snapshot', *scope],
                                  text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(snap.returncode, 0, snap.stderr)
            fingerprint = subprocess.run([sys.executable, str(ROOT / 'scripts/image_assets.py'),
                'fingerprint', *scope, '--path', 'ref.png', '--purpose', '身份', '--source', '复用'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(fingerprint.returncode, 0, fingerprint.stderr)
            receipt = ('### 图片参考验收 · EP001\n' +
                       fingerprint.stdout.replace(' | 待核对 | ', ' | 通过 | ').rstrip('\n') +
                       '离线夹具检查。\n\n### forest-laoli-h3 · EP001\n' + snap.stdout)
            for stage, anchor in (('storyboard', 'SHOT'), ('prompts', 'MOTION'), ('review', 'MOTION')):
                loaded = subprocess.run([sys.executable, str(ENTRY), 'skill', '--stage', stage],
                                        text=True, capture_output=True, cwd=ROOT)
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                token = loaded.stdout.splitlines()[-1].split()[-1]
                receipt += f'- {stage}：{token} {anchor}-EP001-001 离线夹具检查。\n'
            index = root / '创作者决策/项目约定.md'
            index.parent.mkdir()
            index.write_text(receipt)
            args = ['prepare', *scope, '--batch-id', 'auto-offline', '--pipeline', 'v4',
                    '--authorization', '仅离线测试', '--max-ccu', '1', '--max-seconds', '60']
            output = StringIO()
            with (patch.object(Path, 'home', return_value=root),
                  patch.object(colab_h3, 'Colab', side_effect=AssertionError('No GPU provider')),
                  patch.object(sys, 'argv', [str(ENTRY), *args]),
                  redirect_stdout(output), redirect_stderr(StringIO())):
                runpy.run_path(str(ENTRY), run_name='__main__')
            prepared = json.loads(output.getvalue())
            batch = colab_h3.read_json(prepared['batch'])
            jobs = [colab_h3.engine()._read_job(root, job) for job in batch['jobs']]
            self.assertIsNone(jobs[0]['parameters']['continue_from_job'])
            self.assertEqual(jobs[1]['parameters']['continue_from_job'], jobs[0]['job_id'])
            self.assertEqual(jobs[1]['parameters']['context_frames'], 22)
            self.assertEqual(jobs[1]['parameters']['context_audio_frames'], 18)
            self.assertIn('/context-masters/', jobs[1]['outputs'][1])
            graph = colab_h3.build_graph(jobs[1], [colab_h3.digest(root / 'ref.png') + '.png'], 'preflight')
            self.assertEqual(graph['91']['class_type'], 'MiniMaxH3MotionContextLoadLatent')
            self.assertEqual(graph['93']['class_type'], 'MiniMaxH3MotionContextTrim')
            preflight = subprocess.run([sys.executable, str(ENTRY), 'preflight', '--batch',
                prepared['batch'], '--offline'], text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            self.assertFalse(json.loads(preflight.stdout)['gpu_allocated'])

    def test_manual_change_to_planned_source_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 测试\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)), encoding='utf-8')
            command = [sys.executable, str(ENTRY), 'continuity', '--project', str(root), '--episode', 'EP001', '--apply']
            first = subprocess.run(command, text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(first.returncode, 0, first.stderr)
            video.write_text(video.read_text().replace('- 续接来源：MOTION-EP001-001', '- 续接来源：无'))
            changed = video.read_text()
            second = subprocess.run(command, text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('人工修改', second.stderr)
            self.assertEqual(video.read_text(), changed)

    def test_existing_explicit_choices_are_not_silently_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：无\n'
                for i in (1, 2)))
            original = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity', '--project', str(root),
                '--episode', 'EP001', '--apply'], text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(video.read_text(), original)
            self.assertTrue(json.loads(result.stdout)['rows'][1]['needs_review'])

    def test_prepare_requires_a_reason_for_a_new_batch_of_unmarked_same_scene_cuts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (root / '创作者决策').mkdir()
            (root / '创作者决策/项目约定.md').write_text('# 项目\n')
            (root / 'short-drama.json').write_text(json.dumps({
                'creator_authority': {'production_profile': {'status': 'accepted',
                    'choices': {'video_prompt_dialect': 'minimax-h3'}}}}))
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：无\n'
                for i in (1, 2)))
            original = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'prepare', '--project', str(root),
                '--episode', 'EP001', '--pipeline', 'v4', '--batch-id', 'legacy-test',
                '--authorization', 'offline test', '--max-ccu', '1', '--max-seconds', '60'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('同场独立', result.stderr)
            self.assertEqual(video.read_text(), original)
            self.assertFalse((root / '.short-drama').exists())
            board = ep / '分镜.md'
            board.write_text(board.read_text() + '- H3接续策略：切镜\n- H3接续理由：独立反应镜。\n')
            resolved = subprocess.run([sys.executable, str(ENTRY), 'prepare', '--project', str(root),
                '--episode', 'EP001', '--pipeline', 'v4', '--batch-id', 'legacy-test',
                '--authorization', 'offline test', '--max-ccu', '1', '--max-seconds', '60'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(resolved.returncode, 0)
            self.assertNotIn('同场独立', resolved.stderr)
            self.assertEqual(video.read_text(), original)

    def test_running_batch_prevents_changing_its_source_documents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text('## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            video.write_text('## MOTION-EP001-001 · 一\n- 分镜：SHOT-EP001-001\n- 续接来源：自动\n', encoding='utf-8')
            batch = root / '.short-drama/production/colab/active'
            batch.mkdir(parents=True)
            (batch / 'resources.json').write_text(json.dumps({'state': 'ready'}))
            (batch / 'batch.json').write_text(json.dumps({'jobs': ['EP001-001-active']}))
            before = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity',
                '--project', str(root), '--episode', 'EP001', '--apply'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('活动', result.stderr)
            self.assertEqual(video.read_text(), before)

    def test_explicit_camera_cut_overrides_matching_state_and_camera_label(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 运镜：硬切至另一侧。\n- 起点：她站着。\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 测试\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)), encoding='utf-8')
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity',
                '--project', str(root), '--episode', 'EP001', '--apply'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('- 续接来源：无', video.read_text().split('## MOTION-EP001-002')[1])

    def test_replans_an_automated_link_after_storyboard_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            board = ep / '分镜.md'
            board.write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 测试\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)), encoding='utf-8')
            command = [sys.executable, str(ENTRY), 'continuity', '--project', str(root), '--episode', 'EP001', '--apply']
            first = subprocess.run(command, text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn('- 续接来源：MOTION-EP001-001', video.read_text())
            board.write_text(board.read_text() + '- H3接续策略：切镜\n- H3接续理由：改为反应镜。\n')
            second = subprocess.run(command, text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn('- 续接来源：无', video.read_text().split('## MOTION-EP001-002')[1])

    def test_shared_location_across_different_scene_ids_is_not_silently_treated_as_a_cut(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC002\n- 景别/机位：固定中景\n- 起点：她站着。\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            ref = 'REF-ROOM（顺序：1）· room.png《房间》（用途：地理；控制：位置；不得控制：人）'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 测试\n- 分镜：SHOT-EP001-00{i}\n- 输入参考图：{ref}\n- 续接来源：自动\n'
                for i in (1, 2)), encoding='utf-8')
            before = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity',
                '--project', str(root), '--episode', 'EP001', '--apply'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('场次', result.stderr)
            self.assertEqual(video.read_text(), before)
            board = ep / '分镜.md'
            board.write_text(board.read_text().replace('- 来源：EP001-SC001\n',
                '- 来源：EP001-SC001\n- H3连续场景：酒吧同一时刻\n').replace('- 来源：EP001-SC002\n',
                '- 来源：EP001-SC002\n- H3连续场景：酒吧同一时刻\n')
                + '- H3接续策略：续接\n- H3接续理由：创作判断同一机位承接动作。\n')
            resolved = subprocess.run([sys.executable, str(ENTRY), 'continuity',
                '--project', str(root), '--episode', 'EP001', '--apply'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertIn('- 续接来源：MOTION-EP001-001', video.read_text().split('## MOTION-EP001-002')[1])

    def test_scene_id_change_without_location_evidence_is_not_assumed_a_cut(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC002\n- 景别/机位：固定中景\n- 起点：她站着。\n')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)))
            before = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity', '--project', str(root),
                '--episode', 'EP001', '--apply'], text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('场次', result.stderr)
            self.assertEqual(video.read_text(), before)

    def test_placeholder_camera_and_state_are_not_evidence_of_continuity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：同上\n- 终点：保持不变。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：同上\n- 起点：保持不变。\n')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)))
            before = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity', '--project', str(root),
                '--episode', 'EP001', '--apply'], text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('状态', result.stderr)
            self.assertEqual(video.read_text(), before)

    def test_internal_punctuation_is_not_erased_when_comparing_states(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：不，站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：不站着。\n')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)))
            before = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity', '--project', str(root),
                '--episode', 'EP001', '--apply'], text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('状态', result.stderr)
            self.assertEqual(video.read_text(), before)

    def test_camera_cut_inside_a_sentence_is_not_linked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n'
                '- 运镜：镜头切到背面。\n- 起点：她站着。\n')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)))
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity', '--project', str(root),
                '--episode', 'EP001', '--apply'], text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('- 续接来源：无', video.read_text().split('## MOTION-EP001-002')[1])

    def test_negated_cut_does_not_prevent_matching_continuity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n'
                '- 运镜：镜头不做硬切。\n- 起点：她站着。\n')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)))
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity', '--project', str(root),
                '--episode', 'EP001', '--apply'], text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('- 续接来源：MOTION-EP001-001', video.read_text().split('## MOTION-EP001-002')[1])

    def test_conflicting_continuous_scene_ids_require_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- H3连续场景：吧台上午\n'
                '- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- H3连续场景：吧台晚上\n'
                '- 景别/机位：固定中景\n- 起点：她站着。\n')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 一\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)))
            before = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity', '--project', str(root),
                '--episode', 'EP001', '--apply'], text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('场景', result.stderr)
            self.assertEqual(video.read_text(), before)

    def test_scoped_plan_never_links_to_an_unselected_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 测试\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)), encoding='utf-8')
            before = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity',
                '--project', str(root), '--episode', 'EP001', '--entry', 'MOTION-EP001-002', '--apply'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('前段', result.stderr)
            self.assertEqual(video.read_text(), before)

    def test_composition_reference_prevents_using_that_shot_in_a_chain(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            video.write_text(
                '## MOTION-EP001-001 · 一\n- 分镜：SHOT-EP001-001\n- 续接来源：自动\n'
                '## MOTION-EP001-002 · 二\n- 分镜：SHOT-EP001-002\n- 输入参考图：REF-C（顺序：1）· c.png《位置》（用途：构图；控制：站位；不得控制：动作）\n'
                '- 续接来源：自动\n', encoding='utf-8')
            before = video.read_text()
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity',
                '--project', str(root), '--episode', 'EP001', '--apply'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('构图参考', result.stderr)
            self.assertEqual(video.read_text(), before)

    def test_explicit_cut_wins_even_when_camera_and_state_match(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            (ep / '分镜.md').write_text(
                '## SHOT-EP001-001 · 一\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 终点：她站着。\n'
                '## SHOT-EP001-002 · 二\n- 来源：EP001-SC001\n- 景别/机位：固定中景\n- 起点：她站着。\n'
                '- H3接续策略：切镜\n- H3接续理由：此处需要独立反应镜。\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 测试\n- 分镜：SHOT-EP001-00{i}\n- 续接来源：自动\n'
                for i in (1, 2)), encoding='utf-8')
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity',
                '--project', str(root), '--episode', 'EP001', '--apply'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('- 续接来源：无', video.read_text().split('## MOTION-EP001-002')[1])

    def test_same_camera_and_matching_state_continue_but_new_scene_starts_fresh(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ep = root / '剧集/EP001'
            ep.mkdir(parents=True)
            board = ep / '分镜.md'
            board.write_text('\n'.join([
                '## SHOT-EP001-001 · 等候', '- 来源：EP001-SC001',
                '- 景别/机位：吧台固定中景', '- 起点：老J在吧台。', '- 终点：老J拿起眼镜。',
                '## SHOT-EP001-002 · 戴眼镜', '- 来源：EP001-SC001',
                '- 景别/机位：吧台固定中景', '- 起点：老J拿起眼镜。', '- 终点：老J戴好眼镜。',
                '## SHOT-EP001-003 · 上车', '- 来源：EP001-SC002',
                '- 景别/机位：车外远景', '- 运镜：切至车外远景。',
                '- 起点：老J走到车旁。', '- 终点：老J上车。',
            ]) + '\n', encoding='utf-8')
            video = ep / '视频提示词.md'
            video.write_text('\n'.join(
                f'## MOTION-EP001-00{i} · 测试\n- 分镜：SHOT-EP001-00{i}\n'
                '- 时长：5s\n- 续接来源：自动\n### 可复制提示词\n> summary: test\n'
                for i in (1, 2, 3)), encoding='utf-8')
            result = subprocess.run([sys.executable, str(ENTRY), 'continuity',
                '--project', str(root), '--episode', 'EP001', '--apply'],
                text=True, capture_output=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            sections = video.read_text(encoding='utf-8').split('## MOTION-EP001-')
            self.assertIn('- 续接来源：无', sections[1])
            self.assertIn('- 续接来源：MOTION-EP001-001', sections[2])
            self.assertIn('- 续接来源：无', sections[3])
            self.assertIn('### 可复制提示词\n> summary: test', sections[2])


if __name__ == '__main__':
    unittest.main()
