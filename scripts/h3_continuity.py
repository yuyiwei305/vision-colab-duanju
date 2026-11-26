"""Plan H3 continuation from the episode's existing Markdown, without media calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile

CUT_CUE = re.compile(r'硬切|跳切|切至|切到|反打|正反打|切换机位|切换到|闪回|转场|次日|翌日|数小时后')
NEGATED_CUT = re.compile(r'(?:不|无|勿|避免|不要|禁止|无需|并非).{0,8}$')


def _sections(document: str, prefix: str, episode: str) -> list[tuple[str, str]]:
    titles = list(re.finditer(r'^## ([^\n]+)$', document, re.M))
    pattern = re.compile(rf'^({prefix}-{re.escape(episode)}-\d{{3}}) ·')
    matches = [(i, pattern.match(m[1])) for i, m in enumerate(titles) if pattern.match(m[1])]
    if not matches or len({m[1] for _, m in matches}) != len(matches):
        raise ValueError(f'{prefix} 条目缺失或重复')
    return [(m[1], document[titles[i].start():titles[i + 1].start() if i + 1 < len(titles) else len(document)])
            for i, m in matches]


def _field(section: str, name: str) -> str:
    values = re.findall(rf'^- {re.escape(name)}[：:][ \t]*(.*?)[ \t]*$', section, re.M)
    if len(values) > 1:
        raise ValueError(f'字段「{name}」重复')
    return values[0] if values else ''


def _normal(value: str) -> str:
    return re.sub(r'\s+', '', value).casefold().strip('，。；：、,.!！?？')


def _usable(value: str) -> bool:
    return _normal(value) not in {'', '无', '同上', '同前', '同前镜', '见上', '不变',
                                  '保持', '保持不变', '延续', '待定', '未知', 'na', 'n/a'}


def _cut_cue(value: str) -> bool:
    for clause in re.split(r'[，；。,.!！\n]', value):
        for match in CUT_CUE.finditer(clause):
            if not NEGATED_CUT.search(clause[:match.start()]):
                return True
    return False


def _geography(section: str) -> set[str]:
    refs = _field(section, '输入参考图')
    return set(re.findall(r'·\s*([^；\n]+?)《[^》\n]+》（用途：地理[；）]', refs))


def plan(project: Path, episode: str, only: list[str] | None = None) -> dict:
    if not re.fullmatch(r'EP\d{3}', episode):
        raise ValueError('无效剧集 ID')
    root = project.resolve(strict=True)
    folder = root / '剧集' / episode
    board_file, video_file = folder / '分镜.md', folder / '视频提示词.md'
    for path in (board_file, video_file):
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError('正式文档必须是本作品内的真实文件')
    board, video = board_file.read_text(encoding='utf-8'), video_file.read_text(encoding='utf-8')
    shots = dict(_sections(board, 'SHOT', episode))
    motions = _sections(video, 'MOTION', episode)
    from h3_shot_contract import checker, validate
    coverage = validate(board, video, only=None if checker()['has_sequence'](board, video) else only,
                        allow_auto=True)
    ids = [entry for entry, _ in motions]
    if only and (len(set(only)) != len(only) or set(only) - set(ids)):
        raise ValueError('续接规划范围包含重复或不存在的镜头')
    rows = []
    previous = None
    for entry, section in motions:
        spans = coverage.get(entry, {}).get('shots')
        shot_id = spans[0]['shot'] if spans else _field(section, '分镜')
        if shot_id not in shots:
            raise ValueError(f'{entry} 未绑定唯一分镜')
        shot = shots[shot_id]
        source = _field(shot, '来源')
        scene = re.match(rf'{re.escape(episode)}-SC\d{{3}}\b', source)
        current_source = _field(section, '续接来源')
        stamp = _field(section, 'H3自动续接')
        if stamp:
            recorded = re.fullmatch(rf'v1\|(无|MOTION-{re.escape(episode)}-\d{{3}})\|([^|\n]+)', stamp)
            if not recorded or recorded[1] != current_source:
                raise ValueError(f'{entry} 的续接来源已人工修改或自动记录无效，请先由创作代理处理')
        modern = entry in coverage
        continuing = bool(modern and previous and coverage[entry]['expected_parent'] == previous['entry'])
        row = {'entry': entry, 'shot': shot_id, 'scene': scene[0] if scene else '',
               'camera': _field(section, '景别/机位') or _field(shot, '景别/机位'),
               'start': _field(section, '起始帧') if modern else _field(shot, '起点'),
               'end': _field(section, '终点') if modern else _field(shot, '终点'), 'source': current_source,
               'scene_group': _field(shot, 'H3连续场景'),
               'geography': sorted(_geography(section) or _geography(shot)),
               'cut_cue': any(_cut_cue(_field(section if modern else shot, key))
                              for key in ('景别/机位', '运镜', '起始帧' if modern else '起点')),
               'strategy': (_field(section, 'H3接续策略') or ('续接' if continuing else '切镜')) if modern else (_field(shot, 'H3接续策略') or '自动'),
               'strategy_reason': (_field(section, 'H3接续理由') or ('分镜明确续接前一 MOTION' if spans and continuing else ('同一 SHOT 连续生成' if continuing else '独立起段'))) if modern else _field(shot, 'H3接续理由'),
               'composition': bool(re.search(r'用途：构图', _field(section, '输入参考图')
                                             + _field(shot, '输入参考图'))),
               'planned': (current_source == '自动' or bool(stamp)) and (not only or entry in only)}
        if spans:
            row.update(sequence=coverage[entry]['sequence'], shots=[span['shot'] for span in spans])
        if row['strategy'] not in ('自动', '续接', '切镜'):
            raise ValueError(f'{shot_id} 的 H3接续策略无效')
        if row['strategy'] != '自动' and not row['strategy_reason']:
            raise ValueError(f'{shot_id} 的 H3接续策略必须说明理由')
        group_conflict = bool(previous and row['scene_group'] and previous['scene_group'] and
                              row['scene_group'] != previous['scene_group'])
        same_scene = bool(previous and not group_conflict and ((row['scene'] and row['scene'] == previous['scene']) or
                          (row['scene_group'] and row['scene_group'] == previous['scene_group'])))
        if spans and row['strategy'] != ('续接' if continuing else '切镜'):
            raise ValueError(f'{entry}: H3接续策略与段落边界冲突')
        if continuing:
            same_scene = True
            if row['strategy'] == '切镜' or row['cut_cue']:
                raise ValueError(f'{entry}: 续接入段不能同时声明切镜')
        selected = not only or entry in only
        if selected and not row['planned'] and (
                (row['source'] == '无' and row['strategy'] == '续接') or
                (row['source'].startswith('MOTION-') and row['strategy'] == '切镜')):
            raise ValueError(f'{entry} 的既有续接来源与分镜策略冲突，请先由创作代理处理')
        row['needs_review'] = bool(selected and not row['planned'] and row['source'] == '无'
                                   and same_scene and row['strategy'] == '自动' and not row['cut_cue']
                                   and not row['composition'] and not previous['composition'])
        if row['planned']:
            if row['strategy'] == '切镜':
                row.update(decision='无', reason=row['strategy_reason'])
            elif row['strategy'] == '续接':
                if not same_scene or row['cut_cue']:
                    raise ValueError(f'{shot_id} 续接必须有同场紧邻前段')
                row.update(decision=previous['entry'], reason=row['strategy_reason'])
            elif previous is None:
                row.update(decision='无', reason='首段')
            elif row['cut_cue']:
                row.update(decision='无', reason='分镜明确换机位或跳转')
            elif group_conflict:
                row.update(decision=None, reason='前后连续场景标识不同，需判断时间和切点')
            elif previous['scene'] and row['scene'] and previous['scene'] != row['scene'] and not same_scene:
                row.update(decision=None, reason='场次编号不同，需判断地点、时间和机位')
            elif (same_scene and _usable(row['camera']) and _usable(previous['camera'])
                  and _normal(row['camera']) == _normal(previous['camera']) and row['start']
                  and _usable(row['start']) and _usable(previous['end'])
                  and _normal(row['start']) == _normal(previous['end'])):
                row.update(decision=previous['entry'], reason='同场同机位且起止状态一致')
            else:
                row.update(decision=None, reason='场次、机位或起止状态不足以自动判断')
            if previous and row['decision'] == previous['entry'] and (row['composition'] or previous['composition']):
                row.update(decision=None, reason='构图参考仅适用于不参与续接的独立镜头')
            if previous and row['decision'] == previous['entry'] and only and previous['entry'] not in only and (
                    not spans or previous['source'] != previous.get('decision') or previous['source'] == '自动'):
                row.update(decision=None, reason='同一批次的续接前段不在选定范围内')
        else:
            row.update(decision=row['source'], reason='保留既有决定')
        rows.append(row)
        previous = row
    return {'rows': rows, 'video': video, 'board': board, 'video_file': video_file,
            'board_file': board_file, 'project': root, 'episode': episode}


def apply(result: dict) -> dict:
    pending = [r for r in result['rows'] if r['planned'] and r['decision'] is None]
    if pending:
        raise ValueError('续接需由当前创作代理处理：' + '；'.join(r["entry"] + ' ' + r['reason'] for r in pending))
    replacements = {r['entry']: r for r in result['rows'] if r['planned'] and r['decision'] is not None}
    if not replacements:
        return {'updated': 0, 'rows': result['rows']}
    target = result['video_file']
    if target.read_text(encoding='utf-8') != result['video'] or result['board_file'].read_text(encoding='utf-8') != result['board']:
        raise ValueError('规划期间分镜或视频提示词已变化，请重新规划')
    document = result['video']
    for entry, row in replacements.items():
        pattern = re.compile(rf'^## {re.escape(entry)} ·[^\n]*(?:\n(?!## ).*)*', re.M)
        def update(match):
            section = match[0]
            source_line = '- 续接来源：' + row['decision']
            marker_line = '- H3自动续接：v1|' + row['decision'] + '|' + row['reason'].replace('|', '｜')
            section, source_count = re.subn(r'^- 续接来源：[^\n]*$', lambda _: source_line, section, count=1, flags=re.M)
            if source_count != 1:
                raise ValueError(f'{entry} 续接来源无法安全更新')
            if re.search(r'^- H3自动续接：', section, re.M):
                section, count = re.subn(r'^- H3自动续接：[^\n]*$', lambda _: marker_line, section, count=1, flags=re.M)
            else:
                section, count = re.subn(r'^- 续接来源：[^\n]*$', lambda _: source_line + '\n' + marker_line, section, count=1, flags=re.M)
            if count != 1:
                raise ValueError(f'{entry} 自动续接记录无法安全更新')
            return section
        document, count = pattern.subn(update, document, count=1)
        if count != 1:
            raise ValueError(f'{entry} 续接来源无法安全更新')
    if document == result['video']:
        return {'updated': 0, 'rows': result['rows']}
    for resource in (result['project'] / '.short-drama/production/colab').glob('*/resources.json'):
        try:
            state = json.loads(resource.read_text(encoding='utf-8'))['state']
            if state in ('released', 'transferred'):
                continue
            batch = resource.with_name('batch.json')
            jobs = json.loads(batch.read_text(encoding='utf-8'))['jobs'] if batch.is_file() else []
            if not isinstance(jobs, list) or any(not isinstance(job, str) for job in jobs):
                raise ValueError('无效的活动批次任务列表')
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError('无法核实活动批次，暂不修改正式文档') from exc
        if (not jobs or any(not re.match(r'^EP\d{3}-', job) for job in jobs) or
                any(job.startswith(result['episode'] + '-') for job in jobs)):
            raise ValueError('本集存在活动或释放状态未确认的批次，不能修改正式文档')
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, name = tempfile.mkstemp(prefix='.h3-continuity-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(document)
        os.chmod(name, mode)
        if target.read_text(encoding='utf-8') != result['video'] or result['board_file'].read_text(encoding='utf-8') != result['board']:
            raise ValueError('规划期间分镜或视频提示词已变化，请重新规划')
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return {'updated': len(replacements), 'rows': result['rows']}
