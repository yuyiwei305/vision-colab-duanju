"""Offline tests for the public project H3 entry."""
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

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'scripts/h3_production.py'
IMAGE_ENTRY = ROOT / 'scripts/image_assets.py'


class H3ProductionEntryTests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, str(ENTRY), *map(str, args)],
                              capture_output=True, text=True, cwd=ROOT)

    def legacy_cli(self, *args):
        return subprocess.run(['bash', str(ROOT / 'scripts/drama'), 'h3-episode', *map(str, args)],
                              capture_output=True, text=True, cwd=ROOT)

    def image_cli(self, *args):
        return subprocess.run([sys.executable, str(IMAGE_ENTRY), *map(str, args)],
                              capture_output=True, text=True, cwd=ROOT)

    def test_review_loads_the_skill_and_all_relevant_references(self):
        result = self.cli('skill', '--stage', 'review')
        self.assertEqual(result.returncode, 0, result.stderr)
        for heading in ('# 漫剧老李 · H3 V4 专项', '# 调度与表演',
                        '# Ref2VA 正文', '# H3 专项审查与诊改'):
            self.assertIn(heading, result.stdout)

    def test_prepare_blocks_before_it_creates_a_batch_when_evidence_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for cli in (self.cli, self.legacy_cli):
                result = cli('prepare', '--project', root, '--episode', 'EP001',
                             '--batch-id', 'must-not-exist', '--pipeline', 'v4',
                             '--authorization', 'offline test', '--max-ccu', 1, '--max-seconds', 60)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('缺少 H3 专项检查记录', result.stderr)
            self.assertFalse((root / '.short-drama').exists())

    def test_location_prop_style_and_composition_previews_use_their_own_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            episode = root / '剧集/EP001'
            episode.mkdir(parents=True)
            (episode / '视觉设定.md').write_text(
                '# 视觉方向\n写实都市。\n## 地点 · 房间\n- 门在北侧。\n## 道具 · 杯子\n- 白色陶瓷。\n')
            (episode / '分镜.md').write_text('## SHOT-EP001-001 · 杯子近景\n- 起点：杯子在桌上。\n')
            (episode / '图片提示词.md').write_text('\n'.join(
                f'## IMG-{entry} · 参考\n- 用途：{purpose}\n- 视觉依据：{origin}\n- 参考：无外部参考\n'
                '### 可复制提示词\n> A readable reference of the specified asset.\n'
                for entry, purpose, origin in (
                    ('ROOM', '地理', '地点 · 房间'), ('CUP', '身份', '道具 · 杯子'),
                    ('STYLE', '风格', '项目视觉方向'), ('COMP', '构图', 'SHOT-EP001-001'))))
            scope = ('--project', root, '--episode', 'EP001')
            preview = self.image_cli('preview', *scope)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(len(json.loads(preview.stdout)), 4)
            (root / 'composition.png').write_bytes(b'offline-composition')
            fingerprint_args = ('fingerprint', *scope, '--path', 'composition.png',
                                '--purpose', '构图', '--source', 'IMG-COMP')
            fingerprint = self.image_cli(*fingerprint_args)
            self.assertEqual(fingerprint.returncode, 0, fingerprint.stderr)
            board = episode / '分镜.md'
            board.write_text(board.read_text() + '- 输入参考图：无\n')
            rebound = self.image_cli(*fingerprint_args)
            self.assertEqual(rebound.returncode, 0, rebound.stderr)
            self.assertEqual(fingerprint.stdout, rebound.stdout)

    def test_offline_preflight_accepts_advice_but_rejects_incomplete_or_stale_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            root = base / 'work'
            episode = root / '剧集/EP001'
            episode.mkdir(parents=True)
            (root / 'short-drama.json').write_text(json.dumps({
                'format': {'aspect_ratio': '16:9'},
                'creator_authority': {'production_profile': {'status': 'accepted',
                    'choices': {'video_prompt_dialect': 'minimax-h3'}}}}))
            ref = '剧集/EP001/制作成果/images/IMG-IDENTITY.png'
            (root / ref).parent.mkdir(parents=True)
            (root / ref).write_bytes(b'fixture-identity')
            (root / 'composition.png').write_bytes(b'fixture-composition')
            (episode / '剧本.md').write_text('# 测试')
            visual_file = episode / '视觉设定.md'
            visual_text = (
                '## 人物 · 苏蔓\n- 识别锚点：低盘发\n\n## 造型 · 红裙\n- 所属人物：苏蔓\n'
                '- 连续性锁：LOCK-DRESS《红裙》（镜头：入画；图片提示词项：IMG-IDENTITY）· 锁面：red satin ankle-length dress\n')
            visual_file.write_text(visual_text)
            prompt_file = episode / '图片提示词.md'
            image_prompt = ('## IMG-IDENTITY · 苏蔓身份参考\n- 用途：人物和造型参考\n'
                            '- 视觉依据：人物 · 苏蔓；造型 · 红裙\n- 参考：无外部参考\n'
                            '### 可复制提示词\n> Full figure in a red satin ankle-length dress.\n')
            prompt_file.write_text(image_prompt)
            scope = ('--project', root, '--episode', 'EP001')
            for prompt, error in (
                (image_prompt.replace('；造型 · 红裙', ''), '一套属于该人物的当前造型'),
                (image_prompt.replace('red satin ankle-length dress', 'red shirt'), '缺少锁面'),
            ):
                prompt_file.write_text(prompt)
                preview = self.image_cli('preview', *scope, '--entry', 'IMG-IDENTITY')
                self.assertNotEqual(preview.returncode, 0, preview.stdout)
                self.assertIn(error, preview.stderr)
            prompt_file.write_text(image_prompt)
            for changed, error in (
                (visual_text.replace('所属人物：苏蔓', '所属人物：别人'), '一套属于该人物的当前造型'),
                (visual_text.replace('- 所属人物：苏蔓\n', ''), '须写明唯一所属人物'),
            ):
                visual_file.write_text(changed)
                preview = self.image_cli('preview', *scope, '--entry', 'IMG-IDENTITY')
                self.assertNotEqual(preview.returncode, 0, preview.stdout)
                self.assertIn(error, preview.stderr)
            visual_file.write_text(visual_text)
            preview = self.image_cli('preview', *scope, '--entry', 'IMG-IDENTITY')
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn('Full figure in a red satin ankle-length dress', json.loads(preview.stdout)[0]['提示词'])
            reused = self.image_cli('fingerprint', *scope, '--path', ref, '--purpose', '身份', '--source', '复用')
            self.assertNotEqual(reused.returncode, 0, reused.stdout)
            self.assertIn('须使用本集来源 IMG-IDENTITY', reused.stderr)
            wrong_role = self.image_cli('fingerprint', *scope, '--path', ref, '--purpose', '地理',
                                        '--source', 'IMG-IDENTITY')
            self.assertNotEqual(wrong_role.returncode, 0, wrong_role.stdout)
            self.assertIn('视觉依据不足以承担「地理」用途', wrong_role.stderr)
            board = episode / '分镜.md'
            ref_line = (
                f'- 输入参考图：REF-A（顺序：1）· {ref}《身份板》（用途：身份；控制：身份；不得控制：动作）；'
                'REF-COMP（顺序：2）· composition.png《构图参考》（用途：构图；控制：站位；不得控制：身份、动作）\n'
            )
            board_text = '## SHOT-EP001-001 · 测试\n- 起点：杯子在桌上\n' + ref_line
            board.write_text(board_text)
            (episode / '视频提示词.md').write_text(
                '## MOTION-EP001-001 · 测试\n- 分镜：SHOT-EP001-001\n- 时长：5s\n- 续接来源：无\n'
                + ref_line + '### 可复制提示词\n> subject_definitions: A woman.\n> summary: She turns.\n'
                '> retention_analysis: Identity.\n> detailed_description: The woman in <Picture 3> says <d>Hello.</d>\n'
                '> overall_soundscape: Room tone and three knocks.\n> non_diegetic_music: N/A\n')
            index = root / '创作者决策/项目约定.md'
            index.parent.mkdir()
            snap = self.cli('snapshot', '--project', root, '--episode', 'EP001')
            self.assertEqual(snap.returncode, 0, snap.stderr)
            receipt = '### forest-laoli-h3 · EP001\n' + snap.stdout
            for stage, anchor in (('storyboard', 'SHOT'), ('prompts', 'MOTION'), ('review', 'MOTION')):
                loaded = self.cli('skill', '--stage', stage)
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                token = loaded.stdout.splitlines()[-1].split()[-1]
                receipt += f'- {stage}：{token} {anchor}-EP001-001 离线夹具检查。\n'
            index.write_text(receipt)
            args = ['prepare', '--project', str(root), '--episode', 'EP001', '--batch-id', 'offline-test',
                    '--pipeline', 'v4', '--authorization', '仅离线测试，不授权GPU',
                    '--max-ccu', '1', '--max-seconds', '60']
            for cli in (self.cli, self.legacy_cli):
                denied = cli(*args)
                self.assertNotEqual(denied.returncode, 0, denied.stdout)
                self.assertIn('缺少图片参考验收', denied.stderr)
            self.assertFalse((root / '.short-drama').exists())
            image_reviews = '### 图片参考验收 · EP001\n'
            for path, purpose, source in ((ref, '身份', 'IMG-IDENTITY'), ('composition.png', '构图', '复用')):
                fingerprint = self.image_cli('fingerprint', *scope, '--path', path, '--purpose', purpose,
                                             '--source', source)
                self.assertEqual(fingerprint.returncode, 0, fingerprint.stderr)
                image_reviews += fingerprint.stdout.replace(' | 待核对 | ', ' | 通过 | ').rstrip('\n') + '离线夹具检查。\n'
            receipt = image_reviews + '\n' + receipt
            index.write_text(receipt)
            with (patch.object(Path, 'home', return_value=base / 'config'),
                  patch.object(sys, 'argv', [str(ENTRY), *args]),
                  redirect_stdout(StringIO()), redirect_stderr(StringIO())):
                runpy.run_path(str(ENTRY), run_name='__main__')
            batch = root / '.short-drama/production/colab/offline-test/batch.json'
            def preflight():
                return self.cli('preflight', '--batch', batch, '--offline')
            clean = preflight()
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            self.assertFalse(json.loads(clean.stdout)['gpu_allocated'])
            self.assertIn('MOTION-EP001-001 H4', clean.stderr)
            self.assertIn('MOTION-EP001-001 H36', clean.stderr)
            for code in ('H37', 'H38', 'H39', 'H40', 'H41', 'H31'):
                self.assertIn(f'MOTION-EP001-001 {code}', clean.stderr)
            self.assertNotIn('MOTION-EP001-001 H1', clean.stderr)
            self.assertFalse((batch.parent / 'resources.json').exists())
            legacy = self.legacy_cli('preflight', '--batch', batch, '--offline')
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertIn('MOTION-EP001-001 H38', legacy.stderr)
            index.unlink()
            legacy = self.legacy_cli('preflight', '--batch', batch, '--offline')
            self.assertNotEqual(legacy.returncode, 0)
            self.assertIn('缺少 H3 专项检查记录', legacy.stderr)
            index.write_text(receipt)
            for name, error, update, restore in (
                ('missing stage', '过期、阶段不全或范围不符',
                 lambda: index.write_text(receipt.rsplit('- review：', 1)[0]),
                 lambda: index.write_text(receipt)),
                ('wrong scope', '过期、阶段不全或范围不符',
                 lambda: index.write_text(receipt.replace('- 生成段：MOTION-EP001-001',
                                                                          '- 生成段：MOTION-EP001-002')),
                 lambda: index.write_text(receipt)),
                ('changed asset', '验收证据缺失或已过期', lambda: (root / ref).write_bytes(b'changed-asset'),
                 lambda: (root / ref).write_bytes(b'fixture-identity')),
                ('changed image prompt', '验收证据缺失或已过期',
                 lambda: prompt_file.write_text(image_prompt.replace('Full figure', 'Side view')),
                 lambda: prompt_file.write_text(image_prompt)),
                ('changed visual source', '验收证据缺失或已过期',
                 lambda: visual_file.write_text(visual_text.replace('低盘发', '短发')),
                 lambda: visual_file.write_text(visual_text)),
                ('missing pixel observation', '验收证据缺失或已过期',
                 lambda: index.write_text(receipt.replace(' | 离线夹具检查。', ' | ')),
                 lambda: index.write_text(receipt)),
                ('pending pixel inspection', '验收证据缺失或已过期',
                 lambda: index.write_text(receipt.replace(' | 通过 | ', ' | 待核对 | ')),
                 lambda: index.write_text(receipt)),
                ('failed pixel inspection', '验收证据缺失或已过期',
                 lambda: index.write_text(receipt.replace(' | 通过 | ', ' | 未通过 | ')),
                 lambda: index.write_text(receipt)),
                ('mismatched image purpose', '图片参考及用途必须与',
                 lambda: board.write_text(board_text.replace('用途：身份', '用途：造型状态')),
                 lambda: board.write_text(board_text)),
                ('changed storyboard', '过期、阶段不全或范围不符',
                 lambda: board.write_text(board_text.replace('桌上', '手里')), lambda: None),
            ):
                with self.subTest(name=name):
                    update()
                    rejected = preflight()
                    self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
                    self.assertIn(error, rejected.stderr)
                    restore()

            # Preflight must reject policy violations before a new record can be created.
            video = episode / '视频提示词.md'
            video_text = video.read_text()
            for name, changed_board, changed_video, error in (
                ('opening frame role', board_text.replace('用途：构图', '用途：起始帧'), video_text, '首尾帧图片用途已禁用'),
                ('ending frame role', board_text, video_text.replace('用途：构图', '用途：结束帧'), '首尾帧图片用途已禁用'),
                ('two composition images', board_text, video_text.replace('用途：身份', '用途：构图'), '最多允许一张'),
                ('conflicting bindings', board_text, video_text.replace('用途：构图', '用途：地理'), '构图参考必须与'),
                ('non-reference generation', board_text,
                 video_text.replace('- 时长：5s', '- 时长：5s\n- 生成方式：首尾帧'), '生成方式必须是'),
                ('continuation segment', board_text,
                 video_text.replace('续接来源：无', '续接来源：MOTION-EP001-002'), '只有不参与 AV latent 续接'),
                ('chain head outside selected child', board_text,
                 video_text + '\n## MOTION-EP001-002 · 续接\n- 续接来源：MOTION-EP001-001\n',
                 '只有不参与 AV latent 续接'),
            ):
                with self.subTest(name=name):
                    board.write_text(changed_board)
                    video.write_text(changed_video)
                    denied = self.cli('snapshot', '--project', root, '--episode', 'EP001',
                                      '--entry', 'MOTION-EP001-001')
                    self.assertNotEqual(denied.returncode, 0, denied.stdout)
                    self.assertIn(error, denied.stderr)

            board.write_text(board_text)
            video.write_text(video_text.split('### 可复制提示词')[0] +
                '### 可复制提示词\n> subject_definitions: <Subject 1> is the woman in <Picture 1>.\n'
                '> <Picture 2> guides the foreground placement in [Shot 1].\n'
                '> summary: [reference generation] <Subject 1> waits by the table.\n'
                '> retention_analysis: <Subject 1> (appears in [Shot 1]): fully_preserved - identity.\n'
                '> <Picture 2> ([Shot 1] composition): partially_preserved - foreground placement.\n'
                '> detailed_description: Soft window light. [Shot 1] <Subject 1> stands as shown in <Picture 2>. '
                'At 1.2 seconds, three knocks sound from the hallway. She pauses. '
                'A voice in the hallway (S1) says off-screen, <d>[English] May I come in?</d>\n'
                '> overall_soundscape: Quiet room tone.\n> non_diegetic_music: N/A\n')
            checked = self.cli('lint', '--project', root, '--episode', 'EP001')
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn('0 条 lint 提示', checked.stderr)


if __name__ == '__main__':
    unittest.main()
