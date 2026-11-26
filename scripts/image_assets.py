#!/usr/bin/env python3
"""Read-only preview and reference acceptance checks for project image assets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
INDEX = Path('创作者决策/项目约定.md')
IMAGE_ID = r'IMG-[A-Z0-9]+(?:-[A-Z0-9]+)*'
IMAGE_PURPOSES = {'身份', '造型状态', '地理', '构图', '尺度', '效果', '风格'}
PURPOSE_ORIGINS = {'身份': ('人物 · ', '道具 · '), '地理': ('地点 · ',), '构图': ('SHOT-',)}
PURPOSE = re.compile(r'》（用途：([^；）\n]+)；')
CHECKER = ROOT / 'ref/drama-skills/skills/short-drama/scripts/creator_markdown_check.py'


def visual_dependency(section):
    """Only explicit nonvisual fields are excluded; prose remains conservative.

    Prompt text itself is never filtered. This is a dependency signature, not
    semantic classification or permission to skip an actual image observation.
    """
    return re.sub(r'^- (?:\*\*)?(?:声线|声音方向|声音参考|原生音色参考|参考音频|时长|对白时长|语速)(?:\*\*)?：[^\n]*\n?',
                  '', section, flags=re.M).strip()


class ImageAssets:
    def __init__(self, production, project, episode):
        if not re.fullmatch(r'EP\d{3}', episode):
            raise ValueError('无效剧集 ID，应为 EP001 格式')
        self.production = production
        self.project = project
        self.episode = episode
        self.directory = Path('剧集') / episode
        self._sources = {}

    def read(self, path):
        with self.production._open_project_input(self.project, path.as_posix()) as stream:
            return stream.read().decode('utf-8')

    def entries(self):
        document = self.read(self.directory / '图片提示词.md')
        return re.findall(rf'^## `?({IMAGE_ID})`? ·', document, re.M)

    def source(self, entry):
        if not re.fullmatch(IMAGE_ID, entry):
            raise ValueError(f'{entry} 不是正式 IMG 条目')
        if entry not in self._sources:
            self._sources[entry] = self._read_source(entry)
        return self._sources[entry]

    def _read_source(self, entry):
        production = self.production
        document = self.read(self.directory / '图片提示词.md')
        section = production._markdown_section(document, entry)
        prompt = production._copyable_prompt(section)
        purposes = production._field_values(section, '用途')
        if len(purposes) != 1:
            raise ValueError(f'{entry} 缺少唯一「用途」')
        visual = self.read(self.directory / '视觉设定.md')
        fields = production._field_values(section, '视觉依据')
        if len(fields) != 1:
            raise ValueError(f'{entry} 缺少唯一「视觉依据」；人物图须同时指向人物和当前造型')
        origins = [part.strip() for part in fields[0].split('；')]
        if len(origins) != len(set(origins)):
            raise ValueError(f'{entry} 视觉依据重复')
        visual_sections = {}
        for block in re.split(r'(?=^## )', visual, flags=re.M):
            if re.match(r'^## (人物|造型|地点|道具) · ', block):
                heading = block.splitlines()[0][3:].strip()
                if heading in visual_sections:
                    raise ValueError(f'视觉设定条目重复：{heading}')
                visual_sections[heading] = block.strip()
        sources = {}
        for origin in origins:
            if origin == '项目视觉方向':
                sources[origin] = re.split(r'^## (?:人物|造型|地点|道具) · ', visual, maxsplit=1, flags=re.M)[0].strip()
            elif re.fullmatch(rf'SHOT-{self.episode}-\d{{3}}', origin):
                shot = production._markdown_section(self.read(self.directory / '分镜.md'), origin)
                sources[origin] = re.sub(r'^- (?:\*\*)?(?:输入参考图|图片提示词项)(?:\*\*)?：.*\n?', '',
                                         shot, flags=re.M).strip()
            elif origin in visual_sections:
                sources[origin] = visual_sections[origin]
            else:
                raise ValueError(f'{entry} 视觉依据未定位到正式条目：{origin}')
        people = {origin.removeprefix('人物 · ') for origin in origins if origin.startswith('人物 · ')}
        look_owners = []
        for origin in origins:
            if origin.startswith('造型 · '):
                owners = production._field_values(sources[origin], '所属人物')
                if len(owners) != 1:
                    raise ValueError(f'{entry} 的「{origin}」须写明唯一所属人物')
                look_owners.append(owners[0].strip())
        if set(look_owners) != people or len(look_owners) != len(people):
            raise ValueError(f'{entry} 须为每位出图人物指向一套属于该人物的当前造型')

        checker = runpy.run_path(str(CHECKER))
        errors = []
        locks = checker['_continuity_locks'](visual, errors)
        relevant_locks = [lock for lock in locks if entry in lock.images]
        for lock in relevant_locks:
            if not checker['_carries_surface'](prompt, lock.surface):
                errors.append(f'{lock.lock_id} 缺少锁面「{lock.surface}」')
            if not lock.entry or ' · '.join(lock.entry) not in sources:
                errors.append(f'{lock.lock_id} 的所属条目未列入视觉依据')
        if errors:
            raise ValueError(f'{entry}：' + '；'.join(errors))

        bindings = production._markdown_reference_bindings(section, field_name='参考')
        if [binding['order'] for binding in bindings] != list(range(1, len(bindings) + 1)):
            raise ValueError(f'{entry} 参考顺序须从 1 连续编号')
        for key in ('slot_id', 'path'):
            if len({binding[key] for binding in bindings}) != len(bindings):
                raise ValueError(f'{entry} 参考图的槽位和路径须各自唯一')
        references = production._field_values(section, '参考')[0]
        reference_purposes = PURPOSE.findall(references)
        if len(reference_purposes) != len(bindings) or set(reference_purposes) - IMAGE_PURPOSES:
            raise ValueError(f'{entry} 参考图用途缺失或不适用')
        inputs = {binding['path']: production._hash_project_file(self.project, binding['path']) for binding in bindings}
        identity = [section.strip(), sources, inputs]
        legacy_digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        visual_identity = [section.strip(), {key: visual_dependency(value) for key, value in sources.items()}, inputs]
        digest = hashlib.sha256(json.dumps(visual_identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {'IMG': entry, '用途': purposes[0], '视觉依据': sources,
                '提示词': prompt, '参考': references, '参考文件 SHA256': inputs, '来源 SHA256': digest,
                '来源旧 SHA256': legacy_digest}

    def fingerprint(self, path, purpose, entry, image_sha=None):
        path = self.production._relative_path(path)
        if purpose not in IMAGE_PURPOSES or not re.search(r'\.(?:png|jpe?g|webp)$', path, re.I) or ' | ' in path:
            raise ValueError(f'无效的参考图片路径或用途：{path} · {purpose}')
        generated = re.fullmatch(rf'({IMAGE_ID})(?:-v[1-9]\d*)?', Path(path).stem)
        if generated and Path(path).is_relative_to(self.directory / '制作成果/images') and entry != generated[1]:
            raise ValueError(f'{path} 须使用本集来源 {generated[1]} 验收')
        source_sha = '-'
        if entry != '复用':
            source = self.source(entry)
            required_origins = PURPOSE_ORIGINS.get(purpose)
            if required_origins and not any(origin.startswith(required_origins) for origin in source['视觉依据']):
                raise ValueError(f'{entry} 的视觉依据不足以承担「{purpose}」用途')
            source_sha = source['来源 SHA256']
        image_sha = image_sha or self.production._hash_project_file(self.project, path)
        return path, purpose, image_sha, entry, source_sha

    def require_review(self, index, path, purpose, image_sha):
        from drama_factory.assets import has_current_review
        if has_current_review(self.project, self.episode, path, purpose, image_sha):
            return
        reviews = []
        for block in re.split(r'(?=^#{1,6} )', index, flags=re.M):
            if block.splitlines()[:1] == [f'### 图片参考验收 · {self.episode}']:
                reviews.extend(line.removeprefix('- 图片：').split(' | ', 6)
                               for line in block.splitlines()[1:] if line.startswith('- 图片：'))
        for review in reversed(reviews):
            if len(review) == 7 and review[:2] == [path, purpose]:
                current = self.fingerprint(path, purpose, review[3], image_sha)
                legacy = (*current[:4], self.source(review[3])['来源旧 SHA256']) if review[3] != '复用' else current
                if (review[5] == '通过' and review[6].strip()
                        and tuple(review[:5]) in (current, legacy)):
                    return
                raise ValueError(f'{path}（{purpose}）的验收证据缺失或已过期；按现图和来源复查')
        raise ValueError(f'{path}（{purpose}）缺少图片参考验收；查看实际文件后在项目约定记录')


def main():
    from colab_h3 import engine

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('preview', 'fingerprint'):
        sub = commands.add_parser(command)
        sub.add_argument('--project', type=Path, required=True)
        sub.add_argument('--episode', required=True)
        if command == 'preview':
            sub.add_argument('--entry', action='append')
        else:
            sub.add_argument('--path', required=True)
            sub.add_argument('--purpose', choices=sorted(IMAGE_PURPOSES), required=True)
            sub.add_argument('--source', required=True, help='本集 IMG ID 或「复用」')
    args = parser.parse_args()
    try:
        images = ImageAssets(engine(), args.project.resolve(strict=True), args.episode)
        if args.command == 'fingerprint':
            print('- 图片：' + ' | '.join(images.fingerprint(args.path, args.purpose, args.source)) + ' | 待核对 | ')
            return
        entries = args.entry or images.entries()
        if not entries or len(entries) != len(set(entries)):
            raise ValueError('预检范围需要至少一个唯一 IMG 条目')
        preview, errors = [], []
        for entry in entries:
            try:
                preview.append(images.source(entry))
            except (OSError, ValueError) as exc:
                errors.append(f'{entry}：{exc}' if not str(exc).startswith(entry) else str(exc))
        if errors:
            raise ValueError('\n'.join(errors))
        print(json.dumps(preview, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f'图片预检：{exc}\n')


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
