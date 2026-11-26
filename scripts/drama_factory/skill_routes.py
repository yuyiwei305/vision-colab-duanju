"""Resolve the actual reading list for an 18-step SOP handoff."""
import os
from pathlib import Path
import re

from .core import ROOT, FactoryError, file_sha, read_json
from h3_production import STAGES as H3_REFERENCES


SUITE = ROOT / 'ref/drama-skills/skills'
PROJECT_SKILLS = ROOT / '.agents/skills'
SOP = ROOT / 'docs/剧集制作总SOP.md'
H3_CONTRACT = 'docs/h3/shot-motion-contract.md'
H3_WORKFLOW = 'docs/h3/forest-laoli-workflow.md'
BOARD_TRANSITIONS = '.agents/skills/storyboard-logic/references/scene-transitions.md'
EDIT = '创作者决策/剪辑SOP.md'
IMAGES = ('docs/image-generation-sop.md', 'docs/image-craft-sop.md')

# The main skill and the project SOP are shared by every step.
STEPS = {
    1: ((), ('short-drama:runtime-preflight',)),
    2: ((), ('short-drama:runtime-preflight', 'docs/factory/README.md')),
    3: (('short-drama-develop',), ('short-drama-develop:story-craft', 'short-drama-develop:episode-design')),
    4: (('short-drama-write',), ('short-drama-write:screenplay-format', 'short-drama-write:script-craft')),
    5: (('short-drama-assets',), ('short-drama-assets:voice-direction', 'docs/Breeze-TTS-角色音色SOP.md')),
    6: (('short-drama-storyboard', 'storyboard-logic', 'forest-laoli-h3:storyboard'),
        (BOARD_TRANSITIONS, H3_CONTRACT)),
    7: (('short-drama-image-prompts', 'short-drama-produce', 'imagegen'), IMAGES),
    8: (('short-drama-storyboard', 'short-drama-image-prompts', 'short-drama-produce', 'imagegen'),
        (H3_CONTRACT, *IMAGES)),
    9: (('short-drama-video-prompts', 'forest-laoli-h3:prompts'), (H3_CONTRACT, H3_WORKFLOW)),
    10: (('short-drama-review', 'storyboard-logic', 'forest-laoli-h3:review', 'short-drama-produce'),
         (BOARD_TRANSITIONS, H3_CONTRACT, H3_WORKFLOW, 'short-drama-review:production-quality-gates')),
    11: (('short-drama-produce',), (H3_WORKFLOW, 'docs/factory/README.md')),
    12: (('short-drama-review', 'storyboard-logic', 'forest-laoli-h3:review'),
         (BOARD_TRANSITIONS, H3_CONTRACT, 'short-drama-review:generated-media')),
    13: (('short-drama-edit',), (EDIT, H3_CONTRACT, 'short-drama-edit:cut-craft')),
    14: (('short-drama-edit', 'short-drama-produce'), (EDIT, 'docs/成品画质增强SOP.md')),
    15: (('short-drama-edit',), (EDIT, 'short-drama-edit:sound-and-subtitles')),
    16: (('short-drama-edit',), (EDIT, 'short-drama-edit:delivery-verify')),
    17: (('short-drama-review', 'short-drama-edit'), (EDIT, 'short-drama-review:generated-media')),
    18: (('short-drama-edit', 'short-drama-review'), (EDIT, 'short-drama-edit:delivery-verify')),
}


def skill_names(step):
    return ['vision-media-workflow', 'short-drama', *STEPS[step][0]]


def _file(path):
    if not path.is_file():
        raise FactoryError(f'缺少阶段资料：{path}')
    return {'path': str(path), 'sha256': file_sha(path)}


def verify_bundle():
    """Verify the vendored version and all agent discovery links."""
    lock = read_json(ROOT / 'drama-skills.lock.json')
    vendor = SUITE.parent
    for filename, expected in lock['files_sha256'].items():
        if _file(vendor / filename)['sha256'] != expected:
            raise FactoryError(f'技能文件与版本锁不一致：{filename}')
    for name in lock['skills']:
        installed = PROJECT_SKILLS / name / 'SKILL.md'
        source = SUITE / name / 'SKILL.md'
        if not installed.is_file() or installed.resolve() != source.resolve():
            raise FactoryError(f'技能入口未指向仓库版本：{installed}')
    return len(lock['skills'])


def resolve(step, *, with_text=False):
    """Validate bundled files and identify host tools separately from reading material."""
    sop = SOP.read_text(encoding='utf-8')
    headings = list(re.finditer(r'^## (\d+)\. (.+)$', sop, re.M))
    if [int(heading[1]) for heading in headings] != list(STEPS):
        raise FactoryError('技能路由步骤与总 SOP 不一致')
    skills = skill_names(step)
    local_writing = PROJECT_SKILLS / 'charlie-screenwriting-guide'
    if step in (3, 4) and (local_writing / 'SKILL.md').is_file():
        skills.append('charlie-screenwriting-guide')
    codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
    imagegen_override = os.environ.get('DRAMA_IMAGEGEN_SKILL')
    imagegen = (Path(imagegen_override).expanduser() if imagegen_override
                else codex_home / 'skills/.system/imagegen/SKILL.md')
    host_requirements = []
    if step in (13, 15, 16):
        host_requirements.append({'tool': 'ChatCut', 'skill': 'chatcut-plugin-basics',
                                  'note': '使用 ChatCut 时从当前宿主加载该技能及对应操作技能'})
    skill_files = []
    references = [ROOT / 'docs/autonomous-execution-sop.md', SOP]
    for name in skills:
        base, _, phase = name.partition(':')
        if base == 'imagegen':
            host_requirements.append({'tool': 'image_gen', 'skill': base, 'skill_file_found': imagegen.is_file()})
            if imagegen.is_file() or imagegen_override:
                skill_files.append({'skill': name, **_file(imagegen)})
            continue
        directory = (SUITE if base.startswith('short-drama') else PROJECT_SKILLS) / base
        skill_files.append({'skill': name, **_file(directory / 'SKILL.md')})
        if phase:
            references.extend(directory / 'references' / f'{part}.md' for part in H3_REFERENCES[phase])
        if base == 'charlie-screenwriting-guide':
            references.append(directory / 'references/编剧课完整指南.md')
    for ref in STEPS[step][1]:
        owner, delimiter, slug = ref.partition(':')
        references.append(SUITE / owner / 'references' / f'{slug}.md' if delimiter else ROOT / ref)
    title = headings[step - 1][2]
    result = {'step': step, 'title': title, 'skills': skills, 'skill_files': skill_files,
              'references': [_file(path) for path in dict.fromkeys(references)],
              'host_requirements': host_requirements}
    if with_text:
        for item in result['skill_files'] + result['references']:
            path = Path(item['path'])
            if path == SOP:
                start = headings[step - 1].start()
                end = re.search(r'^## ', sop[headings[step - 1].end():], re.M)
                end = headings[step - 1].end() + end.start() if end else len(sop)
                item['text'] = sop[:headings[0].start()] + sop[start:end]
            else:
                item['text'] = path.read_text(encoding='utf-8')
    return result
