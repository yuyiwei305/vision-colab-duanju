"""Exercise 1:N through public checks, planning and real offline batch compilation."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import colab_h3
import h3_continuity
import h3_episode
import h3_production
import h3_shot_contract
from drama_factory import planning

ROOT = Path(__file__).resolve().parents[1]
REF = '- 输入参考图：REF-ROOM（顺序：1）· ref.png《房间》（用途：地理；控制：空间；不得控制：动作）\n'


class H3ShotMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ep = self.root / '剧集/EP001'
        self.ep.mkdir(parents=True)
        (self.root / 'ref.png').write_bytes(b'offline-reference')
        (self.root / 'short-drama.json').write_text(json.dumps({
            'format': {'aspect_ratio': '16:9'}, 'creator_authority': {
                'production_profile': {'status': 'accepted', 'choices': {'video_prompt_dialect': 'minimax-h3'}}}}))
        (self.ep / '剧本.md').write_text('## EP001-SC001 内 · 房间 · 日\n空房间里光线缓缓移动。\n')
        (self.ep / '视觉设定.md').write_text('# 视觉设定\n')
        self.board = ('## SHOT-EP001-007 · 连续跟拍\n- 来源：EP001-SC001\n- 计划时长：15s\n'
                      '- 景别/机位：连续移动\n- 起点：门口\n- 终点：窗前\n- 视觉依据：无\n- 图片提示词项：无\n'
                      + REF + '### 冻结关键帧提示词\n> An empty room.\n')
        self.video = '\n'.join(
            f'## MOTION-EP001-{i:03} · 跟拍第{i}段\n- 分镜：SHOT-EP001-007\n'
            f'- 时长：6s\n- 镜内区间：{(i-1)*5}s-{i*5}s\n- 续接来源：自动\n'
            f'- 起始帧：位置{i}\n- 终点：位置{i+1}\n- H3上下文：22v18a\n- 生成方式：图生视频\n'
            + REF + '### 可复制提示词\n> subject_definitions: An empty room.\n'
            '> summary: [reference generation] The camera moves forward.\n'
            '> retention_analysis: Room geography.\n> detailed_description: [Shot 1] A continuous tracking shot.\n'
            '> overall_soundscape: Room tone.\n> non_diegetic_music: N/A\n' for i in (1, 2, 3))
        (self.ep / '分镜.md').write_text(self.board)
        (self.ep / '视频提示词.md').write_text(self.video)

    def resolve(self):
        return h3_continuity.apply(h3_continuity.plan(self.root, 'EP001'))

    def test_public_check_plan_snapshot_and_real_v4_jobs(self):
        checked = subprocess.run(['bash', str(ROOT / 'scripts/drama'), 'check', str(self.ep), '--partial'],
                                 text=True, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.resolve()
        rows, _ = h3_production.snapshot(self.root, 'EP001', None)
        self.assertEqual({r['shot'] for r in rows.values()}, {'SHOT-EP001-007'})
        plan = planning.plan(self.root, 'EP001', persist=False)
        self.assertEqual(len(plan['motions']), 3)
        self.assertEqual(len(plan['schedule']['batches']), 1)
        self.assertEqual([r['coverage']['interval'] for r in plan['motions']], [[0, 5], [5, 10], [10, 15]])
        with patch.object(Path, 'home', return_value=self.root), patch.object(colab_h3, 'Colab', side_effect=AssertionError('No GPU')):
            prepared = h3_episode.prepare(self.root, 'EP001', 'mapping-test', pipeline='v4',
                                          authorization_basis='offline regression', max_ccu=1, max_seconds=60)
        batch = colab_h3.read_json(prepared['batch'])
        jobs = [colab_h3.engine()._read_job(self.root, j) for j in batch['jobs']]
        self.assertEqual(len(jobs), 3)
        self.assertEqual([j['source_entry'] for j in jobs], [f'MOTION-EP001-{i:03}' for i in (1, 2, 3)])
        for i, job in enumerate(jobs):
            self.assertEqual(job['parameters']['continue_from_job'], jobs[i-1]['job_id'] if i else None)
            if i:
                graph = colab_h3.build_graph(job, [colab_h3.digest(self.root / 'ref.png') + '.png'], 'preflight')
                self.assertEqual(graph['91']['class_type'], 'MiniMaxH3MotionContextLoadLatent')
                self.assertEqual(graph['93']['class_type'], 'MiniMaxH3MotionContextTrim')

    def test_selection_expands_the_whole_shot_chain(self):
        self.resolve()
        plan = planning.plan(self.root, 'EP001', only=['MOTION-EP001-002'], persist=False)
        self.assertEqual(len(plan['motions']), 3)
        self.assertEqual(set(plan['dependency_added_entries']), {'MOTION-EP001-001', 'MOTION-EP001-003'})

    def test_wrong_parent_gap_and_net_capacity_fail_before_preparation(self):
        self.resolve()
        valid = (self.ep / '视频提示词.md').read_text()
        mutations = [valid.replace('续接来源：MOTION-EP001-002', '续接来源：无'),
                     valid.replace('镜内区间：5s-10s', '镜内区间：6s-10s'),
                     valid.replace('时长：6s', '时长：5s')]
        for video in mutations:
            with self.subTest(video=video[:60]):
                (self.ep / '视频提示词.md').write_text(video)
                with self.assertRaises(ValueError):
                    h3_production.snapshot(self.root, 'EP001', None)
                with self.assertRaises(ValueError):
                    planning.plan(self.root, 'EP001', persist=False)
        self.assertFalse((self.root / '.short-drama').exists())

    def test_segment_state_is_used_instead_of_whole_shot_state(self):
        rows = self.resolve()['rows']
        self.assertEqual([r['start'] for r in rows], ['位置1', '位置2', '位置3'])
        self.assertEqual([r['decision'] for r in rows], ['无', 'MOTION-EP001-001', 'MOTION-EP001-002'])

    def test_cross_episode_binding_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '跨集'):
            h3_shot_contract.validate(self.board.replace('SHOT-EP001', 'SHOT-EP002'),
                                      self.video.replace('SHOT-EP001', 'SHOT-EP002'), allow_auto=True)

    def test_emphasized_interval_keeps_native_capacity_validation(self):
        board = self.board.replace('计划时长：15s', '时长：6s')
        video = self.video.split('## MOTION-EP001-002')[0].replace(
            '镜内区间：0s-5s', '**镜内区间**：0s-6s')
        with self.assertRaisesRegex(ValueError, '去重净时长'):
            h3_shot_contract.validate(board, video, allow_auto=True)


if __name__ == '__main__':
    unittest.main()
