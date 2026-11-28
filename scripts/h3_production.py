#!/usr/bin/env python3
"""Vision Media 的 H3 V4 项目入口：加载技能、冻结检查范围、生产前核对。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

from image_assets import IMAGE_PURPOSES, INDEX, PURPOSE, ImageAssets

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / '.agents/skills/forest-laoli-h3'
STAGES = {'storyboard': ('storyboard',), 'prompts': ('prompts',),
          'review': ('storyboard', 'prompts', 'review')}
EP = r'EP\d{3}'
ENTRY = rf'MOTION-{EP}-\d{{3}}'
V4 = 'h3-v4-g4-fullframe-v1'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def skill_text(stage):
    files = ['SKILL.md', *(f'references/{name}.md' for name in STAGES[stage])]
    return '\n\n'.join(f'<!-- {name} -->\n{(SKILL / name).read_text(encoding="utf-8")}'
                       for name in files)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def check_profile(config):
    profile = config['creator_authority']['production_profile']
    if profile.get('status') != 'accepted' or profile['choices'].get('video_prompt_dialect') != 'minimax-h3':
        raise ValueError('项目须已登记 H3 提示词档案')


def composition_refs(production, section, entry):
    compositions = []
    for value in production._field_values(section, '输入参考图'):
        for slot in production.REFERENCE_LINE_RE.finditer(value):
            purpose = PURPOSE.search(slot[0])
            if not purpose:
                continue
            if purpose[1] not in IMAGE_PURPOSES:
                raise ValueError(f'{entry} 图片用途「{purpose[1]}」不适用于 V4 Ref2VA。首尾帧图片用途已禁用')
            if purpose[1] == '构图':
                compositions.append((slot[1], int(slot[2]), slot[3]))
    if len(compositions) > 1:
        raise ValueError(f'{entry} 最多允许一张构图参考')
    return compositions


def picture_purposes(production, section, bindings, entry):
    fields = production._field_values(section, '输入参考图')
    purposes = PURPOSE.findall(fields[0]) if bindings else []
    if len(purposes) != len(bindings) or set(purposes) - IMAGE_PURPOSES:
        raise ValueError(f'{entry} 的每张图片参考都须明确填写适用的用途')
    return purposes


def snapshot(project, episode, only, *, review_index=None):
    if not re.fullmatch(EP, episode):
        raise ValueError('无效剧集 ID，应为 EP001 格式')
    from colab_h3 import engine
    production = engine()
    directory = Path('剧集') / episode
    sources = {}
    for path in (Path('short-drama.json'), *(directory / name for name in
                 ('剧本.md', '视觉设定.md', '分镜.md', '视频提示词.md'))):
        with production._open_project_input(project, path.as_posix()) as stream:
            sources[path] = stream.read()
    config = json.loads(sources[Path('short-drama.json')])
    check_profile(config)
    board = sources[directory / '分镜.md'].decode('utf-8')
    video = sources[directory / '视频提示词.md'].decode('utf-8')
    ids = re.findall(rf'^## (MOTION-{episode}-\d{{3}}) ·', video, re.M)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('正式视频提示词需有唯一的本集 MOTION 条目；32段限制仅用于生产批次')
    if only and (len(only) != len(set(only)) or set(only) - set(ids)):
        raise ValueError('范围包含重复或不存在的 MOTION 条目')
    sections = {entry: production._markdown_section(video, entry) for entry in ids}
    parents = {entry: production._field_values(section, '续接来源')
               for entry, section in sections.items()}
    if any(value == ['自动'] for entry, value in parents.items() if not only or entry in only):
        raise ValueError('视频提示词包含未完成的自动续接规划；先运行 continuity --apply，再重新核对阶段记录')
    preceding_entries = {parent for fields in parents.values() for parent in fields if parent != '无'}
    ids = [entry for entry in ids if not only or entry in only]
    from h3_shot_contract import validate
    coverage = validate(board, video, only=ids)
    rows, assets = {}, {}
    image_assets = ImageAssets(production, project, episode)
    reviewed = set()
    for entry in ids:
        section = sections[entry]
        spans = coverage.get(entry, {}).get('shots')
        shots = [span['shot'] for span in spans] if spans else production._field_values(section, '分镜')
        if not shots or (spans is None and len(shots) != 1) or any(
                not re.fullmatch(rf'SHOT-{episode}-\d{{3}}', item) for item in shots):
            raise ValueError(f'{entry} 未绑定本集 SHOT')
        shot = production._markdown_section(board, shots[0])
        video_composition = composition_refs(production, section, entry)
        if spans is None and composition_refs(production, shot, shots[0]) != video_composition:
            raise ValueError(f'{entry} 的构图参考必须与 {shots[0]} 一致')
        if video_composition and (parents[entry] != ['无'] or entry in preceding_entries):
            raise ValueError(f'{entry} 只有不参与 AV latent 续接的独立镜头可使用构图参考')
        mode = production._field_values(section, '生成方式')
        if mode and mode != ['图生视频']:
            raise ValueError(f'{entry} 的生成方式必须是 V4 Ref2VA 对应的「图生视频」')
        durations = production._field_values(section, '时长')
        if len(durations) != 1 or not re.fullmatch(r'\d+s', durations[0]):
            raise ValueError(f'{entry} 缺少生成请求时长')
        images = production._markdown_reference_bindings(section, field_name='输入参考图')
        audio = production._markdown_audio_bindings(section)
        for binding in images + audio:
            path = binding['path']
            if path not in assets:
                assets[path] = production._hash_project_file(project, path)
        if review_index is not None:
            purposes = picture_purposes(production, section, images, entry)
            if spans is None:
                board_images = production._markdown_reference_bindings(shot, field_name='输入参考图')
                board_purposes = picture_purposes(production, shot, board_images, shots[0])
                if board_images != images or board_purposes != purposes:
                    raise ValueError(f'{entry} 的图片参考及用途必须与 {shots[0]} 一致')
            for binding, purpose in zip(images, purposes):
                path = binding['path']
                if (path, purpose) not in reviewed:
                    image_assets.require_review(review_index, path, purpose, assets[path])
                    reviewed.add((path, purpose))
        native = production._field_values(section, '原生音色参考')
        rows[entry] = {'prompt': production._copyable_prompt(section),
                       'duration': int(durations[0][:-1]), 'images': len(images),
                       'composition': [slot[1] for slot in video_composition],
                       'audio': len(audio) + sum(len(line.split('；')) for line in native)}
        rows[entry].update({'shots': shots} if spans is not None else {'shot': shots[0]})
        if entry in coverage:
            rows[entry]['coverage'] = coverage[entry]
    identity = {'sources': {str(path): sha(raw) for path, raw in sources.items()},
                'scope': ids, 'assets': assets}
    return rows, sha(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode())


def records(index, episode):
    blocks = re.split(r'(?=^#{1,6} )', index, flags=re.M)
    for block in reversed(blocks):
        if block.splitlines()[:1] != [f'### forest-laoli-h3 · {episode}']:
            continue
        fields = {}
        for line in block.splitlines()[1:]:
            match = re.fullmatch(r'- ([^：]+)：(.+)', line)
            if match:
                if match[1] in fields:
                    break
                fields[match[1]] = match[2].strip()
        else:
            yield fields


def guard(project, episode, only):
    path = project / INDEX
    index = path.read_text(encoding='utf-8') if path.is_file() else ''
    candidates = list(records(index, episode))
    if not candidates:
        raise ValueError(f'{episode} 缺少 H3 专项检查记录；先实际调用三个阶段并在 {INDEX} 记录')
    rows, input_sha = snapshot(project, episode, only, review_index=index)
    stage_shas = {stage: sha(skill_text(stage).encode()) for stage in STAGES}
    # A partition can reuse a larger review only when that entire original scope
    # still matches its input fingerprint and all three real stage receipts.
    for rec in candidates:
        if '生成段' in rec and '镜头' in rec and rec['生成段'] != rec['镜头']:
            continue
        reviewed_entries = rec.get('生成段', rec.get('镜头', '')).split()
        if not set(rows) <= set(reviewed_entries):
            continue
        checked_rows, checked_sha = rows, input_sha
        if reviewed_entries != list(rows):
            try:
                checked_rows, checked_sha = snapshot(project, episode, reviewed_entries, review_index=index)
            except (ValueError, OSError):
                continue
        if rec.get('输入 SHA256') != checked_sha:
            continue
        allowed_shots = {shot for row in checked_rows.values() for shot in (row['shots'] if 'shots' in row else [row['shot']])}
        for stage, stage_sha in stage_shas.items():
            note = rec.get(stage, '')
            signature, _, body = note.partition(' ')
            anchor, _, detail = body.partition(' ')
            allowed = allowed_shots if stage == 'storyboard' else checked_rows.keys()
            if signature != stage_sha or anchor not in allowed or not detail.strip():
                break
        else:
            return rows
    raise ValueError(f'{episode} H3 专项检查记录过期、阶段不全或范围不符；按当前输入重新核对')


def advise(rows):
    try:
        spec = importlib.util.spec_from_file_location('forest_laoli_h3_lint', SKILL / 'lint.py')
        lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lint)
        warnings = 0
        for entry, row in rows.items():
            for code, note in lint.inspect(row['prompt'], row['duration'], row['images'], row['audio'],
                                           row['composition']):
                warnings += 1
                print(f'提示 {entry} {code}: {note}', file=sys.stderr)
        print(f'forest-laoli-h3：检查 {len(rows)} 段，{warnings} 条 lint 提示（不阻断）',
              file=sys.stderr)
    except Exception as exc:
        print(f'forest-laoli-h3 lint 未完成（不阻断）：{type(exc).__name__}', file=sys.stderr)


def batch_scope(path):
    from colab_h3 import engine
    batch = read_json(path)
    if batch.get('profile') != V4:
        raise ValueError('新 H3 批次只接受 V4；历史任务走原批次恢复')
    project = Path(batch['project']).resolve(strict=True)
    if Path(batch['batch_file']).resolve() != path or not path.is_relative_to(
            project / '.short-drama/production/colab'):
        raise ValueError('必须使用当前作品官方账本中的 batch.json')
    production = engine()
    jobs = [production._read_job(project, job_id) for job_id in batch['jobs']]
    entries = [job.get('source_entry') or '' for job in jobs]
    episode = re.fullmatch(rf'MOTION-({EP})-\d{{3}}', entries[0]) if entries else None
    if not episode or any(not re.fullmatch(rf'MOTION-{episode[1]}-\d{{3}}', entry) for entry in entries):
        raise ValueError('批次必须绑定同一集正式 MOTION 条目')
    if any(job['source'] != f'剧集/{episode[1]}/视频提示词.md' for job in jobs):
        raise ValueError('批次来源不是正式视频提示词')
    return project, episode[1], entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    skill = commands.add_parser('skill', help='加载指定阶段的技能正文和必读资料')
    skill.add_argument('--stage', required=True, choices=STAGES)
    for name in ('snapshot', 'lint', 'prepare', 'continuity'):
        sub = commands.add_parser(name)
        sub.add_argument('--project', required=True, type=Path)
        sub.add_argument('--episode', required=True)
        sub.add_argument('--entry', action='append')
        if name == 'continuity':
            sub.add_argument('--apply', action='store_true', help='将已确定的续接关系写回正式视频提示词')
        if name == 'prepare':
            sub.add_argument('--pipeline', required=True, choices=('v4',))
            sub.add_argument('--batch-id', required=True)
            sub.add_argument('--authorization', required=True)
            sub.add_argument('--max-ccu', type=float, required=True)
            sub.add_argument('--max-seconds', type=int, help=argparse.SUPPRESS)
            sub.add_argument('--runtime-from')
    for name in ('preflight', 'register', 'run', 'resume', 'release'):
        sub = commands.add_parser(name)
        sub.add_argument('--batch', required=True, type=Path)
        if name == 'preflight':
            sub.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'skill':
            text = skill_text(args.stage)
            print(text)
            print(f'\n{args.stage} SHA256: {sha(text.encode())}')
        elif args.command in ('snapshot', 'lint', 'prepare', 'continuity'):
            project = args.project.resolve(strict=True)
            if args.command == 'continuity':
                import h3_continuity
                result = h3_continuity.plan(project, args.episode, args.entry)
                if args.apply:
                    result = h3_continuity.apply(result)
                print(json.dumps({'updated': result.get('updated', 0), 'rows': result['rows']}, ensure_ascii=False))
            elif args.command == 'snapshot':
                rows, digest = snapshot(project, args.episode, args.entry)
                print(f'- 生成段：{" ".join(rows)}\n- 输入 SHA256：{digest}')
            elif args.command == 'lint':
                rows, _ = snapshot(project, args.episode, args.entry)
                advise(rows)
            else:
                if not (project / INDEX).is_file():
                    raise ValueError(f'{args.episode} 缺少 H3 专项检查记录；先实际调用三个阶段并在 {INDEX} 记录')
                if not re.fullmatch(EP, args.episode) or not re.fullmatch(r'[a-z0-9-]{1,35}', args.batch_id):
                    raise ValueError('无效剧集或批次 ID')
                if (project / '.short-drama/production/colab' / args.batch_id).exists():
                    raise ValueError('批次已存在，先核对原批次，不重复准备')
                from colab_h3 import engine
                with engine()._open_project_input(project, 'short-drama.json') as stream:
                    check_profile(json.load(stream))
                import h3_continuity
                plan = h3_continuity.plan(project, args.episode, args.entry)
                review = [row['entry'] for row in plan['rows'] if row['needs_review']]
                if review:
                    raise ValueError('同场独立镜头缺少可核对的切镜理由；请由当前创作代理检查分镜：' + '、'.join(review))
                outcome = h3_continuity.apply(plan)
                if outcome['updated']:
                    print('已自动更新续接规划；将按新输入核对 H3 专项记录。', file=sys.stderr)
                rows = guard(project, args.episode, args.entry)
                advise(rows)
                from h3_episode import prepare
                result = prepare(project, args.episode, args.batch_id, max_seconds=None,
                                 max_ccu=args.max_ccu, runtime_from=args.runtime_from, only=args.entry,
                                 pipeline=args.pipeline, authorization_basis=args.authorization)
                print(json.dumps(result, ensure_ascii=False))
        else:
            batch = args.batch.resolve(strict=True)
            from drama_factory import RELEASE
            from drama_factory.releases import dispatch_archived, dispatch_legacy
            saved = read_json(batch)
            legacy_args = [args.command, '--batch', str(batch)]
            if getattr(args, 'offline', False): legacy_args.append('--offline')
            if saved.get('execution_release') and saved['execution_release'] != RELEASE:
                dispatch_archived(saved['execution_release'], saved.get('execution_release_sha256'),
                                  'scripts/colab_h3.py', legacy_args)
            if not saved.get('execution_release'):
                dispatch_legacy(saved, legacy_args)
            if args.command in ('preflight', 'register', 'run'):
                rows = guard(*batch_scope(batch))
                if args.command == 'preflight':
                    advise(rows)
            command = [sys.executable, '-B', str(ROOT / 'scripts/colab_h3.py'), args.command, '--batch', str(batch)]
            if getattr(args, 'offline', False):
                command.append('--offline')
            sys.stderr.flush()
            os.execv(sys.executable, command)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        parser.exit(1, f'H3 预检：{exc}\n')


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
