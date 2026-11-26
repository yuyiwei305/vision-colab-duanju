"""Sequence: direct multi-shot requests and AV continuation use the same timeline."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import colab_h3
import h3_continuity
import h3_episode
import h3_production
import h3_shot_contract
import h3_workflow_v4 as v4
from drama_factory import core, delivery, planning, releases, runtime

ROOT = Path(__file__).resolve().parents[1]
REF = 'REF-ROOM（顺序：1）· room.png《庭院》（用途：地理；控制：空间；不得控制：动作）'
S2 = 'REF-A（顺序：2）· hero.png《甲》（用途：身份；控制：服装；不得控制：站位）'
S1 = S2.replace('顺序：2', '顺序：1')
SEQUENCE = 'SEQUENCE-EP001-001'
SHOTS = [(0, 2.5), (2.5, 4), (4, 7), (7, 10)]


def shot(i):
    return f'SHOT-EP001-{i:03}'


def motion(i):
    return f'MOTION-EP001-{i:03}'


class SequenceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ep = self.root / '剧集/EP001'
        self.ep.mkdir(parents=True)
        self.board = self.ep / '分镜.md'
        self.video = self.ep / '视频提示词.md'
        self.cut_list = self.ep / '剪辑单.md'
        (self.root / 'short-drama.json').write_text(json.dumps({
            'format': {'aspect_ratio': '16:9'}, 'creator_authority': {'production_profile': {
                'status': 'accepted', 'choices': {'video_prompt_dialect': 'minimax-h3'}}}}))
        for filename in ('room.png', 'hero.png'):
            (self.root / filename).write_bytes(filename.encode())
        (self.ep / '剧本.md').write_text('## EP001-SC001 内 · 庭院 · 日\n两人交手。\n')
        (self.ep / '视觉设定.md').write_text('# 视觉设定\n')

    def write_board(self, shots, motions, refs=None):
        rows = [f'## {SEQUENCE} · 庭院\n- 计划时长：{motions[-1][1]}s\n']
        seen = 0
        for i, (a, b, boundary) in enumerate(motions, 1):
            rows.append(f'## {motion(i)} · 生成第{i}段\n- 段落：{SEQUENCE}\n'
                        f'- 段落区间：{a}s-{b}s\n- 入段：{boundary}\n')
            for index, left, right in shots:
                if index != i:
                    continue
                seen += 1
                rows.append(f'## {shot(seen)} · 交手\n- 生成段：{motion(i)}\n- 段内区间：{left}s-{right}s\n'
                            '- 来源：EP001-SC001\n- 图片提示词项：无\n- 视觉依据：无\n'
                            f'- 输入参考图：{(refs or {}).get(seen, REF)}\n### 冻结关键帧提示词\n> A wide view.\n')
        self.board.write_text('\n'.join(rows))

    def block(self, i, *, requested=10, parent='无', refs=REF, cuts=(), preset='5v24a'):
        return (f'## {motion(i)} · 庭院交手\n- 段落：{SEQUENCE}\n'
                f'- 时长：{requested}s\n- 续接来源：{parent}\n- H3上下文：{preset}\n'
                '- 起始帧：人物持刀。\n- 终点：对手后退。\n- 生成方式：图生视频\n'
                f'- 输入参考图：{refs}\n### 可复制提示词\n'
                '> subject_definitions: Courtyard.\n> summary: [reference generation] Sword fight.\n'
                '> retention_analysis: Location.\n> detailed_description:\n> [Shot 1] Wide view.\n'
                + ''.join(f'> [Shot {n}] At 00:{at:06.3f}, cut to the next angle.\n'
                          for n, at in enumerate(cuts, 2))
                + '> overall_soundscape: Wind.\n> non_diegetic_music: N/A\n')

    def plan(self, **kwargs):
        return planning.plan(self.root, 'EP001', **kwargs)

    def compile(self, plan, batch_id):
        with patch.object(Path, 'home', return_value=self.root), patch.object(
                colab_h3, 'Colab', side_effect=AssertionError('offline only')):
            batch = core.read_json(h3_episode.prepare(
                self.root, 'EP001', batch_id, pipeline='v4', authorization_basis='offline fixture',
                only=[n['entry'] for n in plan['motions']])['batch'])
            runtime.validate_compilation(self.root, batch, plan)
        return [colab_h3.engine()._read_job(self.root, j) for j in batch['jobs']]

    def graph(self, job):
        refs = [colab_h3.digest(self.root / r['path']) + '.png' for r in job['reference_bindings']]
        return colab_h3.build_graph(job, refs, 'preflight')

    def test_one_motion_then_four_independent_motions(self):
        self.write_board([(1, a, b) for a, b in SHOTS], [(0, 10, '独立')], {3: S1})
        self.video.write_text(self.block(1, refs=REF + '；' + S2, cuts=(2.5, 4, 7)))
        one = self.plan()
        self.assertEqual(self.plan(), one)
        self.assertEqual(one['counts'], {'shot_entries': 4, 'generation_segments': 1, 'continuation_chains': 1})
        self.assertEqual(one['shot_motions'], {shot(i): [motion(1)] for i in range(1, 5)})
        rows, _ = h3_production.snapshot(self.root, 'EP001', None)
        self.assertEqual(rows[motion(1)]['shots'], [shot(i) for i in range(1, 5)])
        job, = self.compile(one, 'multi')
        graph = self.graph(job)
        self.assertIn('[Shot 4] At 00:07.000,', graph['6']['inputs']['prompt'])
        self.assertNotIn('91', graph)
        self.write_board([(i, 0, b-a) for i, (a, b) in enumerate(SHOTS, 1)],
                         [(a, b, '独立') for a, b in SHOTS], {3: S1})
        self.video.write_text('\n'.join(self.block(i, requested=4, refs=S1 if i == 3 else REF)
                                        for i in range(1, 5)))
        separate = self.plan(persist=False)
        self.assertEqual(separate['shot_motions'], {shot(i): [motion(i)] for i in range(1, 5)})
        jobs = self.compile(separate, 'separate')
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all('91' not in self.graph(j) for j in jobs))

    def test_long_take_selecting_tail_includes_chain_and_uses_real_latent_nodes(self):
        self.write_board([(i, 0, 10) for i in range(1, 4)],
                         [(0, 10, '独立'), (10, 20, '续接'), (20, 30, '续接')])
        self.video.write_text('\n'.join(self.block(i, requested=11, parent='自动', preset='22v18a')
                                        for i in range(1, 4)))
        h3_continuity.apply(h3_continuity.plan(self.root, 'EP001'))
        plan = self.plan(only=[motion(3)], persist=False)
        self.assertEqual(plan['shot_motions'], {shot(i): [motion(i)] for i in range(1, 4)})
        self.assertEqual(plan['dependency_added_entries'], [motion(1), motion(2)])
        jobs = self.compile(plan, 'long')
        for previous, job in zip(jobs, jobs[1:]):
            self.assertEqual(job['parameters']['continue_from_job'], previous['job_id'])
            self.assertEqual(self.graph(job)['93']['class_type'], 'MiniMaxH3MotionContextTrim')

    def test_motion_can_continue_previous_motion_and_cut_later_in_same_request(self):
        self.write_board([(1, 0, 8), (2, 0, 4), (2, 4, 8)], [(0, 8, '独立'), (8, 16, '续接')])
        self.video.write_text(self.block(1) + '\n' + self.block(
            2, parent=motion(1), cuts=(4 + 5/24,)))
        plan = self.plan(persist=False)
        self.assertEqual(plan['shot_motions'], {shot(1): [motion(1)], shot(2): [motion(2)], shot(3): [motion(2)]})
        job = self.compile(plan, 'mixed')[1]
        graph = self.graph(job)
        self.assertIn('[Shot 2] At 00:04.208,', graph['6']['inputs']['prompt'])
        self.assertIn('91', graph)
        self.video.write_text(self.video.read_text().replace('00:04.208,', '00:04.000,'))
        with self.assertRaisesRegex(ValueError, '原始时间'):
            self.plan(persist=False)

    def test_structure_rejects_gap_overlap_wrong_parent_and_old_groups(self):
        self.write_board([(1, a, b) for a, b in SHOTS], [(0, 10, '独立')])
        valid = self.block(1, cuts=(2.5, 4, 7))
        board = self.board.read_text()
        for invalid, reason in (
            (valid.replace('续接来源：无', f'续接来源：{motion(2)}'), '续接来源'),
            (valid + '\n## GROUP-EP001-001 · 旧分组\n', '不使用 GROUP'),
            (valid.replace('时长：10s', '时长：9s'), '净时长'),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                h3_shot_contract.validate(board, invalid)
        with self.assertRaisesRegex(ValueError, '一一对应'):
            h3_shot_contract.validate(board, valid.replace(motion(1), motion(2), 1))
        for invalid, reason in (
            (board.replace('0s-10s', '1s-10s'), '空洞'),
            (board.replace('7s-10s', '7s-9s'), '完整覆盖'),
            (board.replace('2.5s-4s', '2s-4s'), '重叠'),
            (board.replace(f'生成段：{motion(1)}', f'生成段：{motion(2)}', 1), '紧邻的上级'),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                h3_shot_contract.validate(invalid, valid)

    def test_public_partial_check_accepts_storyboard_before_video_prompts(self):
        self.write_board([(1, a, b) for a, b in SHOTS], [(0, 10, '独立')])
        checker = h3_shot_contract.checker()
        self.assertEqual(checker['validate_episode'](self.ep, self.root, partial=True), [])

    def test_public_partial_check_reports_missing_prompt_entry_instead_of_crashing(self):
        self.write_board([(1, 0, 5), (2, 0, 5)], [(0, 5, '独立'), (5, 10, '独立')])
        self.video.write_text(self.block(1))
        checker = h3_shot_contract.checker()
        errors = checker['validate_episode'](self.ep, self.root, partial=True)
        self.assertTrue(any('一一对应' in error for error in errors), errors)

    def test_public_checker_and_dashboard_use_the_same_parent(self):
        self.write_board([(1, a, b) for a, b in SHOTS], [(0, 10, '独立')])
        self.video.write_text(self.block(1, cuts=(2.5, 4, 7)))
        checker = h3_shot_contract.checker()
        errors = checker['validate_episode'](self.ep, self.root, partial=True)
        self.assertEqual(errors, [])
        views = runpy.run_path(str(ROOT / 'ref/drama-skills/skills/short-drama/scripts/creator_views.py'))
        result = views['read_episode']('EP001', {'分镜.md': self.board.read_text(),
                                               '视频提示词.md': self.video.read_text()}, None)
        self.assertEqual([r['motion'] for r in result['board']['shots']], [motion(1)] * 4)
        self.assertEqual([r['shot'] for r in result['videoPrompts'][0]['shots']], [shot(i) for i in range(1, 5)])

    def test_runtime_image_limit_changes_document_check_planning_and_graph(self):
        refs = REF + '；' + S2
        self.write_board([(1, 0, 5)], [(0, 5, '独立')], {1: refs})
        self.video.write_text(self.block(1, refs=refs))
        (self.ep / '图片提示词.md').write_text('# 图片提示词\n')
        manifest = self.root / v4.RUNTIME
        manifest.parent.mkdir(parents=True)
        config = core.read_json(v4.RUNTIME_PATH)
        checker_path = Path('ref/drama-skills/skills/short-drama/scripts/creator_markdown_check.py')
        installed_checker = self.root / checker_path
        installed_checker.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / checker_path, installed_checker)
        check = runpy.run_path(str(installed_checker))['validate_episode']

        with patch.object(v4, 'RUNTIME_PATH', manifest):
            config['capabilities']['reference_image_count']['max'] = 2
            core.write_json(manifest, config)
            self.assertEqual(check(self.ep, self.root), [])
            job, = self.compile(self.plan(persist=False), 'reference-capacity')
            self.assertEqual(self.graph(job)['41']['inputs']['image_count'], 2)

            config['capabilities']['reference_image_count']['max'] = 1
            core.write_json(manifest, config)
            self.assertEqual(len(check(self.ep, self.root)), 1)
            with self.assertRaisesRegex(ValueError, motion(1)):
                self.plan(persist=False)
            with self.assertRaisesRegex(colab_h3.H3Error, 'unsupported_v4_job_or_references'):
                self.graph(job)

            config['capabilities']['reference_image_count']['max'] = 2
            config['capabilities']['max_total_references'] = 1
            core.write_json(manifest, config)
            with self.assertRaisesRegex(core.FactoryError, 'reference_capacity_exceeded'):
                self.plan(persist=False)

    def test_cut_uses_observed_frames_not_the_prompt_timestamps(self):
        self.write_board([(1, a, b) for a, b in SHOTS], [(0, 10, '独立')])
        self.video.write_text(self.block(1, cuts=(2.5, 4, 7)))
        (self.root / 'clip.mp4').touch()
        self.cut_list.write_text('# 剪辑单\n\n' + '\n'.join(
            f'## CUT-EP001-{i:03} · 采用\n- 来源：{motion(1)} · clip.mp4\n- 分镜：{shot(i)}\n'
            f'- 入点：{a/24:.6f}\n- 出点：{b/24:.6f}\n- 时长：{(b-a)/24:.6f}\n'
            for i, (a, b) in enumerate(zip([0, 62, 97, 172], [62, 97, 172, 240]), 1)))
        with patch.object(delivery, 'probe', return_value={
                'fps': '24', 'frames': 243, 'sha256': 'test', 'width': 1344, 'height': 768}):
            timeline = delivery.compile_timeline(self.root, 'EP001', persist=False)
            self.assertEqual([row['source_in'] for row in timeline['clips']], [0, 62, 97, 172])
            self.assertEqual([row['shot'] for row in timeline['clips']], [shot(i) for i in range(1, 5)])
            self.assertEqual({row['source_entry'] for row in timeline['clips']}, {motion(1)})
            self.cut_list.write_text(self.cut_list.read_text().replace(f'分镜：{shot(4)}', f'分镜：{shot(3)}'))
            with self.assertRaisesRegex(core.FactoryError, shot(4)):
                delivery.compile_timeline(self.root, 'EP001', persist=False)

    def test_archive_can_load_old_runner_and_engine(self):
        if not (ROOT / '.local/factory-releases/factory-v1-20261113').is_dir():
            self.skipTest('local v1 runtime archive unavailable')
        old = releases.verify_release('factory-v1')
        self.assertEqual(old['release'], 'factory-v1')
        with patch.object(releases, '_exec_archived') as launch:
            releases.dispatch_archived('factory-v1', core.file_sha(releases.release_path('factory-v1')),
                                       'scripts/colab_h3.py', ['preflight'])
        self.assertEqual(launch.call_args.args[0].name, 'factory-v1-20261113')

    def test_archived_preflight_uses_original_runtime_at_frozen_live_path(self):
        if not (ROOT / '.local/factory-releases/factory-v17').is_dir():
            self.skipTest('local v17 runtime archive unavailable')
        archive = releases.archived_root('factory-v17')
        self.write_board([(1, 0, 5)], [(0, 5, '独立')])
        self.video.write_text(self.block(1))
        prepare = subprocess.run(
            [sys.executable, '-B', '-c',
             'import sys; from pathlib import Path; from unittest.mock import patch; '
             'from h3_episode import prepare; root = Path(sys.argv[1]); '
             'patch.object(Path, "home", return_value=root).start(); '
             'prepare(root, "EP001", "archived-v17", pipeline="v4", authorization_basis="offline fixture")',
             str(self.root)], cwd=archive / 'scripts', capture_output=True, text=True)
        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        batch_path = self.root / core.LEDGER / 'colab/archived-v17/batch.json'
        batch = core.read_json(batch_path)
        batch['setup_manifest'] = str(ROOT / v4.RUNTIME)
        core.write_json(batch_path, batch)
        original_batch_sha = core.file_sha(batch_path)
        self.assertNotEqual(batch['setup_sha256'], core.file_sha(batch['setup_manifest']))
        check = subprocess.run([sys.executable, '-B', str(colab_h3.SELF), 'preflight',
                                '--offline', '--batch', str(batch_path)],
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(json.loads(check.stdout)['state'], 'offline_preflight_pass')
        self.assertEqual(core.file_sha(batch_path), original_batch_sha)
