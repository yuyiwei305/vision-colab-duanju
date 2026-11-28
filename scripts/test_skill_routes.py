"""Check the 18-step CLI from an environment without installed host skills."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from drama_factory.core import ROOT


class SkillRoutesTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.env = {**os.environ, 'CODEX_HOME': home.name, 'HOME': home.name}
        self.env.pop('DRAMA_IMAGEGEN_SKILL', None)

    def drama(self, *args):
        return json.loads(subprocess.check_output(
            ['bash', str(ROOT / 'scripts/drama'), *args], cwd=ROOT, env=self.env,
            text=True, encoding='utf-8'))

    def test_lists_all_steps_without_machine_installed_skills(self):
        report = self.drama('skills', '--check')
        self.assertEqual(report['bundled_drama_skills'], 12)
        self.assertEqual([row['step'] for row in report['steps']], list(range(1, 19)))
        images = report['steps'][6]
        self.assertEqual(images['host_requirements'][0]['tool'], 'image_gen')
        self.assertFalse(images['host_requirements'][0]['skill_file_found'])
        self.assertIn('short-drama-produce', {file['skill'] for file in images['skill_files']})

    def test_loads_imagegen_from_explicit_host_skill(self):
        skill = Path(self.env['HOME']) / 'SKILL.md'
        skill.write_text('# Image fixture\n', encoding='utf-8')
        self.env['DRAMA_IMAGEGEN_SKILL'] = str(skill)
        route = self.drama('skills', '--step', '8')
        image = next(row for row in route['skill_files'] if row['skill'] == 'imagegen')
        self.assertEqual(image['text'], '# Image fixture\n')

    def test_reads_picture_in_cut_order_then_restores_sound(self):
        project = Path(self.env['HOME']) / 'fixture'
        self.drama('project', 'init', str(project), '--title', 'Fixture')
        folder = project / '剧集/EP001'
        folder.mkdir(parents=True, exist_ok=True)
        board = folder / '分镜.md'
        sequence = 'SEQUENCE-EP001-001'
        motion = 'MOTION-EP001-001'
        board.write_text(
            f'## {sequence} · 选择\n- 计划时长：5s\n'
            f'## {motion} · 对话\n- 段落：{sequence}\n- 段落区间：0s-5s\n- 入段：独立\n'
            f'## SHOT-EP001-001 · 决定离开\n- 生成段：{motion}\n- 段内区间：0s-3s\n'
            '- 职责：揭开去意\n- 起点：钥匙在手。\n- 唯一动作：她松手。\n- 终点：钥匙落在桌上。\n'
            '- 声音：两人仍在交谈。\n'
            f'## SHOT-EP001-002 · 后果\n- 生成段：{motion}\n- 段内区间：3s-5s\n'
            '- 起点：钥匙在桌上。\n- 唯一动作：他收回手。\n- 终点：钥匙留在桌上。\n'
            '- 声音：门外一声响。\n- 画面文字：打烊\n', encoding='utf-8')
        command = ('project', 'read-through', str(project), '--episode', 'EP001')
        before = board.read_bytes()
        visual = self.drama(*command)
        self.assertEqual(visual['issues'], [])
        first, second = visual['shots']
        self.assertEqual(first, {
            'sequence': sequence, 'motion': motion, 'interval': [0, 3], 'shot': 'SHOT-EP001-001',
            '起点': '钥匙在手。', '唯一动作': '她松手。', '终点': '钥匙落在桌上。'})
        self.assertEqual((second['shot'], second['起点'], second['画面文字']),
                         ('SHOT-EP001-002', '钥匙在桌上。', '打烊'))
        restored = self.drama(*command, '--with-sound')['shots']
        self.assertEqual([row.pop('声音') for row in restored], ['两人仍在交谈。', '门外一声响。'])
        self.assertEqual(restored, visual['shots'])
        self.assertEqual(board.read_bytes(), before)

    def test_read_through_keeps_partial_shot_on_legacy_board(self):
        project = Path(self.env['HOME']) / 'fixture'
        self.drama('project', 'init', str(project), '--title', 'Fixture')
        folder = project / 'episodes/EP001'
        folder.mkdir(parents=True)
        (folder / '分镜.md').write_text(
            '## SHOT-EP001-001 · 陌生人\n- 起点：门虚掩。\n- 唯一动作：门开了。\n',
            encoding='utf-8')
        result = self.drama('project', 'read-through', str(project), '--episode', 'EP001')
        self.assertEqual(result['source'], 'episodes/EP001/分镜.md')
        self.assertEqual(result['shots'], [{'shot': 'SHOT-EP001-001', '起点': '门虚掩。',
                                            '唯一动作': '门开了。', '终点': ''}])
        self.assertEqual(result['issues'], ['SHOT-EP001-001: 缺少终点'])

    def test_reads_storyboard_and_h3_docs_in_work_order(self):
        project = Path(self.env['HOME']) / 'fixture'
        self.drama('project', 'init', str(project), '--title', 'Fixture', '--aspect-ratio', '9:16')
        order = self.drama('episode', 'work', str(project), '--episode', 'EP001', '--stage', 'storyboard')
        route = self.drama('skills', '--step', '6')
        self.assertEqual(order['sop_step'], 6)
        self.assertEqual(order['skill_files'],
                         [{k: v for k, v in row.items() if k != 'text'} for row in route['skill_files']])
        refs = {Path(row['path']).name: row['text'] for row in route['references']}
        self.assertIn('## 6. 设计分镜', refs['剧集制作总SOP.md'])
        self.assertNotIn('## 7. 图片提示词', refs['剧集制作总SOP.md'])
        self.assertIn('shot-motion-contract.md', refs)
        self.assertIn('storyboard.md', refs)
