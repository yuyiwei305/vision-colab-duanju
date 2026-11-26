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
