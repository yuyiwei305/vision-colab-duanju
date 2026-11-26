"""H3 production checks for Sequence timelines and historical shot mappings."""
from functools import lru_cache
from pathlib import Path
import re
import runpy

from image_assets import IMAGE_PURPOSES
import h3_workflow_v4 as v4


@lru_cache(maxsize=1)
def checker():
    path = Path(__file__).resolve().parents[1] / 'ref/drama-skills/skills/short-drama/scripts/creator_markdown_check.py'
    return runpy.run_path(str(path))


def _sequence_coverage(check, board, video, only, allow_auto):
    layout, errors = check['sequence_layout'](board, video, only=only)
    if board is None:
        errors.append('段落制 H3 生产必须有正式分镜')
    if errors:
        return {}, errors
    blocks = check['_sections'](video, 'MOTION')
    shots = check['_sections'](board or '', 'SHOT')
    rows = layout['motions']
    limits = v4.reference_capabilities()['reference_image_count']
    active_parents = {rows[e]['sequence'] for e in (only if only is not None else rows) if e in rows}
    def fields(block, entry):
        return {k: check['_plain'](v) for k, v in check['_fields'](block, owner=entry, errors=[]).items()}
    chain_parents = {r['expected_parent'] for r in rows.values()} - {'无'}
    result = {}
    for entry, row in rows.items():
        if row['sequence'] not in active_parents:
            continue
        data = fields(blocks[entry], entry)
        parent = row['expected_parent']
        if data.get('续接来源') != parent and not (allow_auto and data.get('续接来源') == '自动'):
            errors.append(f'{entry}: 依据分镜的入段方式，续接来源应为 {parent}')
        if data.get('H3接续策略', '切镜' if parent == '无' else '续接') != ('切镜' if parent == '无' else '续接'):
            errors.append(f'{entry}: H3接续策略与分镜的入段方式冲突')
        for key in ('起始帧', '终点'):
            if not data.get(key):
                errors.append(f'{entry}: 须写本段{key}文字')
        raw = data.get('时长', '')
        if not re.fullmatch(r'\d+s', raw):
            errors.append(f'{entry}: 生成时长须为 4–15s 整数请求')
            continue
        try:
            params = v4.parameters('drama', 0, int(raw[:-1]), parent=None if parent == '无' else parent,
                                   context_preset=data.get('H3上下文', '5v24a'))
        except ValueError as exc:
            errors.append(f'{entry}: {exc}')
            continue
        net_frames = params['frame_count'] - params['context_frames']
        a, b = row['interval']
        if b - a > min(int(raw[:-1]), net_frames / 24) + 1e-6:
            errors.append(f'{entry}: 段落区间超过请求或去重净时长 {net_frames}/24s')
        spans = row['shots']
        if not spans:
            errors.append(f'{entry}: MOTION 下没有 SHOT')
        prompt = check['_copyable_prompt'](blocks[entry])
        if not prompt:
            errors.append(f'{entry}: 缺少可复制提示词')
        elif spans:
            check['sequence_prompt_parts'](prompt, spans, entry, errors,
                                           context_offset=params['context_frames'] / 24)
        reference = data.get('输入参考图', '')
        check['_references'](reference, entry, None, errors)
        own = check['reference_intents'](reference)
        shot_refs = [check['reference_intents'](fields(shots[span['shot']], span['shot']).get('输入参考图', ''))
                     for span in spans]
        if (not limits['min'] <= len(own) <= limits['max']
                or any(ref.kind != 'REF' or ref.purpose not in IMAGE_PURPOSES for ref in own)
                or len({ref.path for ref in own}) != len(own)
                or own != set().union(*shot_refs)):
            errors.append(f"{entry}: 绑定所属 SHOT 的图片用途并集，每个文件一个槽，"
                          f"使用 {limits['min']}–{limits['max']} 张实图")
        composition = {ref for ref in own if ref.purpose == '构图'}
        if composition and (len(composition) != 1 or parent != '无' or entry in chain_parents or not shot_refs
                            or not composition <= shot_refs[0]):
            errors.append(f'{entry}: 构图参考只用于不续接的 MOTION 起镜；内部切镜用参考资产与文字')
        result[entry] = {**row, 'net_frames': net_frames}
    return result, errors


def validate(board, video, *, only=None, allow_auto=False):
    """Legacy 1:1 sources keep their contract; new shots are continuous chains.

    Coverage uses planned SHOT time. Capacity comes from the actual V4 frame
    calculation, so repeated context never counts as new story time.
    """
    check = checker()
    if check['has_sequence'](board, video):
        result, errors = _sequence_coverage(check, board, video, only, allow_auto)
        if errors:
            raise ValueError('\n'.join(dict.fromkeys(errors)))
        return result
    groups, errors = check['shot_motion_contract'](board, video, only=only)
    shots = check['_sections'](board or '', 'SHOT')
    motions = check['_sections'](video, 'MOTION')
    motion_fields = {entry: check['_fields'](body, owner=entry, errors=[])
                     for entry, body in motions.items()}
    result = {}
    for shot, entries in groups.items():
        fields = check['_fields'](shots.get(shot, ''), owner=shot, errors=[])
        modern = bool(fields.get('计划时长') or len(entries) > 1 or
                      any('镜内区间' in motion_fields[e] for e in entries))
        if not modern:
            continue
        if board is None:
            errors.append(f'{shot}: 新镜头生产必须有正式分镜')
        for index, entry in enumerate(entries):
            row = motion_fields[entry]
            if not re.fullmatch(r'MOTION-EP\d{3}-\d{3}', entry) or not re.fullmatch(r'SHOT-EP\d{3}-\d{3}', shot):
                errors.append(f'{entry}: H3 ID 须含三位剧集号和序号')
                continue
            if entry.split('-')[1] != shot.split('-')[1]:
                errors.append(f'{entry}: 不得跨集引用 SHOT')
            parent = entries[index - 1] if index else '无'
            declared = row.get('续接来源', '')
            if declared != parent and not (allow_auto and declared == '自动'):
                errors.append(f'{entry}: 同一 SHOT 必须连续生成；续接来源应为 {parent}')
            if not row.get('起始帧', '').strip() or not row.get('终点', '').strip():
                errors.append(f'{entry}: 须写本段起始帧和终点文字，不可用整镜状态代替')
            raw_duration = row.get('时长', '')
            if not re.fullmatch(r'\d+s', raw_duration):
                errors.append(f'{entry}: 生成时长须为 4–15s 整数请求')
                continue
            try:
                params = v4.parameters('drama', 0, int(raw_duration[:-1]), parent=None if index == 0 else parent,
                                       context_preset=row.get('H3上下文', '5v24a'))
            except ValueError as exc:
                errors.append(f'{entry}: {exc}')
                continue
            interval = check['motion_interval'](row.get('镜内区间', ''))
            net_frames = params['frame_count'] - params['context_frames']
            if interval and interval[1] - interval[0] > net_frames / 24 + 1e-6:
                errors.append(f'{entry}: 镜内区间超过去重净时长 {net_frames}/24s')
            result[entry] = {'shot': shot, 'interval': list(interval) if interval else None, 'net_frames': net_frames,
                             'expected_parent': parent}
    if errors:
        raise ValueError('\n'.join(dict.fromkeys(errors)))
    return result
