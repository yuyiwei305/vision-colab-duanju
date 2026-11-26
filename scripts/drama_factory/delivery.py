"""Frame-exact cut-list exchange and readback for the two existing editors."""
from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path
import re
import shlex
import subprocess
import time

from .core import (LEDGER, ROOT, FactoryError, field, file_sha, fingerprint, project,
                   project_path, read_json, resolve_spec, sections, write_json)
from .core import project_lock


def rational(value):
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise FactoryError('invalid_rational_time') from exc


def frames(seconds, fps):
    value = rational(seconds) * rational(fps)
    return (value.numerator * 2 + value.denominator) // (value.denominator * 2)


def probe(path):
    data = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
        '-show_format', '-of', 'json', str(path)], timeout=60))
    video = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    if not video:
        raise FactoryError('video_stream_required')
    fps = rational(video['avg_frame_rate'])
    if fps <= 0:
        raise FactoryError('valid_video_fps_required')
    count = video.get('nb_frames')
    if count in (None, 'N/A'):
        counted = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-count_frames',
            '-select_streams', 'v:0', '-show_entries', 'stream=nb_read_frames', '-of', 'json', str(path)], timeout=600))
        count = counted['streams'][0]['nb_read_frames']
    return {'width': video['width'], 'height': video['height'], 'fps': str(fps),
            'frames': int(count), 'duration': str(Fraction(int(count), 1) / fps),
            'audio': any(s['codec_type'] == 'audio' for s in data['streams']),
            'streams': data['streams'], 'sha256': file_sha(path)}


def edit_parser():
    from colab_h3 import module
    from h3_shot_contract import checker
    parser = module(ROOT / 'ref/drama-skills/skills/short-drama-edit/scripts/edit_tool.py', 'factory_edit_parser')
    parser.SEQUENCE_CHECKS = checker()
    return parser


def compile_timeline(root, episode, *, persist=True):
    root = project(root)
    spec = resolve_spec(root, episode)
    filename = f'剧集/{episode}/剪辑单.md'
    path = project_path(root, filename, exists=True)
    parser = edit_parser()
    try:
        delivery, cuts, unused = parser.parse_cut_list(path)
    except parser.EditError as exc:
        raise FactoryError(str(exc)) from exc
    cut_layout, layout_errors = parser.sequence_cut_layout(path.parent, cuts, unused)
    if layout_errors:
        raise FactoryError('\n'.join(layout_errors))
    fps = spec['delivery']['fps']
    if delivery.fps is not None and rational(delivery.fps) != rational(fps):
        raise FactoryError('cut_list_fps_conflicts_with_episode_spec')
    if delivery.frame_size and list(delivery.frame_size) != spec['delivery']['canvas']:
        raise FactoryError('cut_list_canvas_conflicts_with_episode_spec')
    blocks = sections(path.read_text())
    header = path.read_text().split('\n## ', 1)[0]
    header_scale = re.search(r'缩放\s*(\d+(?:\.\d+)?)', header)
    rows, captions, effects, assets, position = [], [], [], {}, 0
    for cut in cuts:
        source = project_path(root, cut.media, exists=True)
        if cut.still:
            raise FactoryError('still_cut_requires_host_camera_adapter:' + cut.cut_id)
        if cut.media not in assets:
            assets[cut.media] = probe(source)
        media = assets[cut.media]
        source_fps = rational(media['fps'])
        start, end = frames(cut.start, source_fps), frames(cut.end, source_fps)
        block = blocks[cut.cut_id]
        exact = field(block, '帧位')
        expected_target = None
        if exact:
            match = re.fullmatch(r'源 \[(\d+),\s*(\d+)[)），],?\s*[，,]?\s*成片 \[(\d+),\s*(\d+)[)），][。.]?', exact)
            if not match:
                raise FactoryError('unsupported_frame_range_notation:' + cut.cut_id)
            explicit = tuple(int(match[i]) for i in range(1, 5))
            if (start, end) != explicit[:2]:
                raise FactoryError('seconds_and_source_frames_disagree:' + cut.cut_id)
            start, end, *expected_target = explicit
        if not 0 <= start < end <= media['frames']:
            raise FactoryError('cut_outside_source:' + cut.cut_id)
        rate = rational(field(block, '变速', default='1'))
        if not Fraction(1, 100) <= rate <= 5:
            raise FactoryError('unsupported_speed')
        duration = frames(Fraction(end - start, 1) / source_fps / rate, fps)
        if duration <= 0:
            raise FactoryError('empty_cut')
        if expected_target and expected_target != [position, position + duration]:
            raise FactoryError('declared_target_frames_disagree:' + cut.cut_id)
        gain = re.search(r'静态增益\s*([+-]?\d+(?:\.\d+)?)\s*dB', field(block, '声音', default=''))
        row = {'id': cut.cut_id, 'source_entry': cut.motion, 'path': cut.media, 'sha256': media['sha256'],
               'source_fps': str(source_fps), 'source_in': start, 'source_out': end,
               'source_total_frames': media['frames'], 'start': position, 'duration': duration,
               'rate': str(rate), 'audio_gain_db': float(gain[1]) if gain else 0,
               'audio_notes': field(block, '声音', default=''), 'picture': cut.picture,
               'visual_scale': float(header_scale[1]) if header_scale else None,
               'width': media['width'], 'height': media['height']}
        row.update((cut_layout or {}).get(cut.cut_id, {}))
        rows.append(row)
        for index, (begin, finish, text, emphasis) in enumerate(cut.subtitles, 1):
            begin = 0 if begin is None else begin
            finish = float(Fraction(end - start, 1) / source_fps) if finish is None else finish
            begin_frame, end_frame = frames(rational(begin) / rate, fps), frames(rational(finish) / rate, fps)
            # Caption tails may cross picture cuts; the whole-film check below bounds their end.
            if not 0 <= begin_frame < min(end_frame, duration):
                raise FactoryError('subtitle_outside_cut:' + cut.cut_id)
            captions.append({'id': cut.cut_id + f'-SUB-{index}', 'text': text, 'emphasis': list(emphasis),
                             'start': position + begin_frame, 'end': position + end_frame})
        for kind, values in [('screen_text', cut.screen_texts), ('sound_effect', cut.sound_effects), ('voice', cut.voices)]:
            effects.extend({'cut': cut.cut_id, 'kind': kind, 'spec': v._asdict()} for v in values)
        if cut.bed:
            effects.append({'cut': cut.cut_id, 'kind': 'environment', 'spec': cut.bed._asdict()})
        position += duration
    if not rows:
        raise FactoryError('nonempty_cut_list_required')
    if any(c['start'] < 0 or c['end'] > position or c['end'] <= c['start'] for c in captions):
        raise FactoryError('subtitle_outside_timeline')
    value = {'schema_version': 1, 'episode': episode, 'project': str(root), 'source': filename,
             'source_sha256': file_sha(path), 'spec': spec['delivery'], 'fps': str(fps),
             'duration_frames': position, 'clips': rows, 'captions': captions, 'effects': effects,
             'unused': unused, 'intervals': 'half_open',
             'quality': {'technical': 'compiled_only', 'continuous_av': 'unreviewed', 'user_confirmation': 'not_requested'}}
    if cut_layout is not None:
        value['planning_sources'] = {f'剧集/{episode}/{name}': file_sha(path.parent / name)
                                     for name in ('分镜.md', '视频提示词.md')}
    value['id'] = fingerprint(value)
    if persist:
        write_json(project_path(root, LEDGER / 'timelines' / (value['id'] + '.json')), value)
    return value


def openchatcut_readback(settings, snapshot, details, *, media=None, scene_source=None):
    if snapshot.get('elementsTruncated') or details.get('truncated') or details.get('notFound'):
        raise FactoryError('complete_editor_readback_required')
    fps = str(rational(settings['fps']))
    detail_rows = details.get('elements', details.get('details', []))
    lookup = {r['id']: r for r in detail_rows}
    source_bindings, media_by_name = {}, {}
    if media is not None and scene_source is not None:
        if media.get('truncated'):
            raise FactoryError('complete_media_readback_required')
        for asset in media['assets']:
            media_by_name.setdefault(asset['name'], []).append(asset)
        for line in scene_source.splitlines():
            if not line.startswith('media '):
                continue
            tokens = shlex.split(line)
            if 'as' not in tokens:
                continue
            name = ' '.join(tokens[1:tokens.index('as')])
            identity = next((t[3:] for t in tokens if t.startswith('id=')), None)
            if identity:
                source_bindings[identity] = name
    elements, warnings = [], []
    for item in snapshot['elements']:
        extra = lookup.get(item['id'])
        if not extra:
            raise FactoryError('element_details_missing:' + item['id'])
        source_duration = extra.get('sourceDurationSeconds')
        source_metadata = {}
        if source_bindings and item['type'] in ('video', 'audio'):
            names = [name for key, name in source_bindings.items() if key.startswith(item['id'])]
            if len(names) != 1 or len(media_by_name.get(names[0], [])) != 1:
                raise FactoryError('unambiguous_scene_asset_mapping_required:' + item['id'])
            asset = media_by_name[names[0]][0]
            if source_duration is not None and rational(source_duration) != rational(asset['duration']):
                warnings.append({'element': item['id'], 'kind': 'source_duration_metadata_conflict',
                                 'element_detail': source_duration, 'asset_library': asset['duration'],
                                 'resolution': 'explicit_scene_media_binding_and_asset_library'})
            source_duration = asset['duration']
            source_metadata = {'source_name': asset['name'], 'source_media_id': asset['id'],
                               'source_fps': str(asset['fps']) if asset.get('fps') else None}
        elements.append({'id': item['id'], 'track_id': item['trackId'], 'type': item['type'],
            'name': item.get('name'), 'start': frames(extra['startTimeSeconds'], fps),
            'duration': frames(extra['durationSeconds'], fps),
            'source_in_seconds': str(extra.get('trimStartSeconds', 0)),
            'source_out_seconds': str(rational(source_duration) - rational(extra.get('trimEndSeconds', 0)))
                if source_duration is not None else None,
            'source_duration_seconds': str(source_duration) if source_duration is not None else None,
            'params': extra.get('params', {}), 'keyframes': extra.get('keyframes', {}),
            'effects': extra.get('effects', []), 'masks': extra.get('masks', []),
            'animated_paths': extra.get('animatedPaths', []), 'param_keys': extra.get('paramKeys', []), **source_metadata})
    return {'editor': 'openchatcut', 'fps': fps, 'canvas': [settings['width'], settings['height']],
            'elements': elements, 'tracks': snapshot['tracks'], 'scene': snapshot.get('sceneId'), 'metadata_warnings': warnings}


def compare(timeline, readback, bindings):
    differences = []
    if rational(readback['fps']) != rational(timeline['fps']):
        differences.append({'kind': 'fps', 'expected': timeline['fps'], 'actual': readback['fps']})
    if readback['canvas'] != timeline['spec']['canvas']:
        differences.append({'kind': 'canvas', 'expected': timeline['spec']['canvas'], 'actual': readback['canvas']})
    actual = {item['id']: item for item in readback['elements']}
    for expected in [*timeline['clips'], *timeline['captions']]:
        bound = bindings.get('elements', {}).get(expected['id'])
        item = actual.get(bound.get('element_id')) if bound else None
        if not item:
            differences.append({'entry': expected['id'], 'kind': 'missing'})
            continue
        desired = {'start': expected['start'], 'duration': expected.get('duration', expected.get('end', 0) - expected['start'])}
        for key, target in desired.items():
            if item[key] != target:
                differences.append({'entry': expected['id'], 'kind': key, 'expected': target, 'actual': item[key]})
        if 'text' in expected and item['params'].get('content') != expected['text']:
            differences.append({'entry': expected['id'], 'kind': 'caption_text'})
        if 'source_in' in expected:
            if item.get('source_name') and item['source_name'] != Path(expected['path']).name:
                differences.append({'entry': expected['id'], 'kind': 'source_asset_name'})
            if item.get('source_duration_seconds') and frames(item['source_duration_seconds'], expected['source_fps']) != expected['source_total_frames']:
                differences.append({'entry': expected['id'], 'kind': 'source_duration_metadata_conflict'})
            for field_name, key in [('source_in_seconds', 'source_in'), ('source_out_seconds', 'source_out')]:
                if item.get(field_name) is None or frames(item[field_name], expected['source_fps']) != expected[key]:
                    differences.append({'entry': expected['id'], 'kind': field_name})
    return {'differences': differences, 'timing_matches': not differences,
            'content_review': 'not_inferred', 'readback_sha256': fingerprint(readback)}


def editor_actions(timeline, readback, bindings, *, expected_readback_sha):
    """Emit fixed adapter calls for the host; no mutation occurs in this function."""
    if fingerprint(readback) != expected_readback_sha:
        raise FactoryError('editor_changed_read_and_reconcile_before_write')
    editor = timeline['spec']['editor']
    if editor != readback['editor']:
        raise FactoryError('editor_binding_mismatch')
    fps = rational(timeline['fps'])
    actions = []
    host_work = [{'owner': 'short-drama-edit', 'source': timeline['source'], 'source_sha256': timeline['source_sha256'],
                  'reason': '按正式剪辑单落实文字、音效、环境声或配音；采用当前编辑器原生工具并回读',
                  'effects': timeline['effects']}] if timeline['effects'] else []
    if readback['canvas'] != timeline['spec']['canvas'] or rational(readback['fps']) != fps:
        if readback['elements']:
            raise FactoryError('existing_project_spec_change_requires_host_review')
        if editor == 'openchatcut':
            actions.append({'tool': 'mcp__openchatcut__update_project_settings', 'arguments': {
                'width': timeline['spec']['canvas'][0], 'height': timeline['spec']['canvas'][1],
                'fps': float(fps), 'canvasSizeMode': 'custom'}})
        else:
            raise FactoryError('set_chatcut_timeline_spec_before_assembly')
    existing = {item['id']: item for item in readback['elements']}
    for clip in timeline['clips']:
        asset = bindings.get('media', {}).get(clip['sha256'])
        if not asset:
            raise FactoryError('import_real_asset_and_bind_sha:' + clip['path'])
        bound = bindings.get('elements', {}).get(clip['id'])
        source_fps = rational(clip['source_fps'])
        common = {'startTime': float(Fraction(clip['start'], 1) / fps),
                  'duration': float(Fraction(clip['duration'], 1) / fps)}
        if editor == 'openchatcut':
            width, height = timeline['spec']['canvas']
            ratios = [width / clip['width'], height / clip['height']]
            scale = clip.get('visual_scale') or (max(ratios) if timeline['spec']['fit_policy'] == 'explicit_crop' else min(ratios))
            patch = {**common, 'trimStart': float(Fraction(clip['source_in'], 1) / source_fps),
                     'trimEnd': float(Fraction(clip['source_total_frames'] - clip['source_out'], 1) / source_fps),
                     'retime': {'rate': float(rational(clip['rate'])), 'maintainPitch': True},
                     'params': {'volume': clip['audio_gain_db'], 'transform.scaleX': scale, 'transform.scaleY': scale}}
            if bound:
                if bound['element_id'] not in existing:
                    raise FactoryError('bound_element_missing_reconcile_before_insert')
                actual = existing[bound['element_id']]
                animated = set(actual.get('animated_paths', actual.get('keyframes', {}).keys()))
                affected = animated & set(patch['params'])
                for key in affected:
                    patch['params'].pop(key)
                if affected or actual.get('keyframes'):
                    host_work.append({'entry': clip['id'], 'owner': 'short-drama-edit',
                        'reason': '保持并核对既有关键帧；静态参数不能覆盖关键帧，移动片段时需同步绝对帧位',
                        'animated_paths': sorted(animated)})
                actions.append({'entry': clip['id'], 'tool': 'mcp__openchatcut__update_elements',
                    'arguments': {'updates': [{'elementId': bound['element_id'], 'trackId': bound['track_id'], 'patch': patch}]}})
            else:
                actions.append({'entry': clip['id'], 'tool': 'mcp__openchatcut__insert_element', 'arguments': {
                    'type': 'video', 'name': clip['id'], 'mediaId': asset['id'], 'trackId': bindings['video_track'],
                    'followFirstMedia': False, **common}, 'after_receipt': {
                        'bind_returned_element_id': clip['id'], 'then_update_patch': patch}})
        elif editor == 'chatcut':
            item = {'trackId': bindings['video_track'], 'fromFrame': clip['start'],
                    'durationInFrames': clip['duration'], 'sourceStartFromInSeconds': float(Fraction(clip['source_in'], 1) / source_fps),
                    'playbackRate': float(rational(clip['rate'])), 'decibelAdjustment': clip['audio_gain_db'],
                    'fit': 'cover' if timeline['spec']['fit_policy'] == 'explicit_crop' else 'contain'}
            operation = {'updates': [{**item, 'id': bound['element_id']}]} if bound else {
                'adds': [{**item, 'type': 'video', 'assetId': asset['id']}]}
            actions.append({'entry': clip['id'], 'tool': 'mcp__chatcut__edit_item', 'arguments': {
                **operation, 'projectId': bindings['project_id'], 'timelineId': bindings['timeline_id'],
                'idempotencyKey': fingerprint([timeline['id'], clip['id'], operation])}})
        if clip.get('audio_notes') or clip.get('picture') not in ('不校', '', None):
            host_work.append({'entry': clip['id'], 'owner': 'short-drama-edit', 'audio': clip.get('audio_notes'),
                              'picture': clip.get('picture'), 'reason': '增益已编译；其余声音/画面指示按正文落实并实际检查'})
    for caption in timeline['captions']:
        bound = bindings.get('elements', {}).get(caption['id'])
        if bound and bound.get('element_ids'):
            host_work.append({'entry': caption['id'], 'owner': 'short-drama-edit', 'element_ids': bound['element_ids'],
                              'reason': '此字幕已在编辑器拆分，保留分组并逐项核对，不自动改成单条'})
            continue
        if editor == 'chatcut':
            if not bindings.get('caption_track') or not bindings.get('caption_style_adopted'):
                raise FactoryError('native_caption_track_and_adopted_style_required')
            data = {'operation': 'update-item', 'itemId': bound['element_id']} if bound else {
                'operation': 'create-item', 'trackId': bindings['caption_track']}
            data.update({'from': caption['start'], 'durationInFrames': caption['end'] - caption['start'], 'text': caption['text']})
            actions.append({'entry': caption['id'], 'tool': 'mcp__chatcut__edit_captions', 'arguments': {
                'action': 'track_items', 'projectId': bindings['project_id'], 'timelineId': bindings['timeline_id'],
                'json': json.dumps(data, ensure_ascii=False)},
                'before_dispatch': '读取当前字幕 revision 并填入 json；每次成功写入后使用返回的新 revision',
                'after_receipt': {'bind_returned_element_id': caption['id']} if not bound else {}})
            continue
        style = bindings.get('caption_params')
        if not style:
            raise FactoryError('adopted_caption_style_binding_required')
        params = {**style, 'content': caption['text']}
        timing = {'startTime': float(Fraction(caption['start'], 1) / fps),
                  'duration': float(Fraction(caption['end'] - caption['start'], 1) / fps)}
        if bound:
            actions.append({'entry': caption['id'], 'tool': 'mcp__openchatcut__update_elements', 'arguments': {
                'updates': [{'elementId': bound['element_id'], 'trackId': bound['track_id'],
                             'patch': {**timing, 'params': params}}]}})
        else:
            actions.append({'entry': caption['id'], 'tool': 'mcp__openchatcut__insert_element', 'arguments': {
                'type': 'text', 'name': caption['id'], 'trackId': bindings['caption_track'],
                'content': caption['text'], 'params': style, **timing},
                'after_receipt': {'bind_returned_element_id': caption['id']}})
    return {'timeline_id': timeline['id'], 'baseline_sha256': expected_readback_sha, 'actions': actions,
            'host_work': host_work, 'ready_for_export': False,
            'mandatory_after': '回读全部元素及源入出点；compare通过后另作实际视听；未知写入先查回执/元素，禁止重复插入'}


def _check_planning_sources(root, record):
    for source, sha in record.get('planning_sources', {}).items():
        if file_sha(project_path(root, source, exists=True)) != sha:
            raise FactoryError('storyboard_or_motion_plan_changed_recompile')


def prepare_exchange(root, episode, timeline, readback, bindings, *, expected_readback_sha):
    root = project(root)
    if timeline['project'] != str(root) or timeline['episode'] != episode:
        raise FactoryError('timeline_project_or_episode_mismatch')
    if file_sha(project_path(root, timeline['source'], exists=True)) != timeline['source_sha256']:
        raise FactoryError('cut_list_changed_recompile')
    _check_planning_sources(root, timeline)
    for clip in timeline['clips']:
        if file_sha(project_path(root, clip['path'], exists=True)) != clip['sha256']:
            raise FactoryError('selected_clip_changed_recompile')
    packet = editor_actions(timeline, readback, bindings, expected_readback_sha=expected_readback_sha)
    packet.update(episode=episode, project=str(root), editor=timeline['spec']['editor'],
                  editor_project=bindings.get('project_id'), editor_timeline=bindings.get('timeline_id', readback.get('scene')),
                  source=timeline['source'], source_sha256=timeline['source_sha256'])
    if 'planning_sources' in timeline:
        packet['planning_sources'] = timeline['planning_sources']
    for action in packet['actions']:
        action['action_id'] = fingerprint(action)
    packet['id'] = fingerprint(packet)
    path = project_path(root, LEDGER / 'editor-exchanges' / (packet['id'] + '.json'))
    write_json(path, packet)
    return {**packet, 'packet_path': str(path)}


def exchange_action(root, packet_id, action_id, *, state='status', receipt=None, readback=None):
    root = project(root)
    if any(not re.fullmatch(r'[a-f0-9]{64}', x) for x in (packet_id, action_id)):
        raise FactoryError('invalid_exchange_identity')
    packet = read_json(project_path(root, LEDGER / 'editor-exchanges' / (packet_id + '.json'), exists=True))
    operation = next((a for a in packet['actions'] if a['action_id'] == action_id), None)
    if operation is None:
        raise FactoryError('action_not_in_packet')
    directory = root / LEDGER / 'editor-receipts'
    path = directory / (fingerprint([packet_id, action_id]) + '.json')
    scope = fingerprint([packet['episode'], packet['editor'], packet.get('editor_project'),
                         packet.get('editor_timeline'), operation.get('entry', 'project-settings')])
    with project_lock(root):
        record = read_json(path) if path.exists() else {'packet': packet_id, 'action': action_id, 'scope': scope, 'state': 'prepared'}
        if state == 'dispatch':
            if record['state'] != 'prepared':
                raise FactoryError('editor_operation_already_dispatched_reconcile_first')
            pending = [read_json(p) for p in directory.glob('*.json')]
            if any(r['scope'] == scope and r['state'] == 'dispatched_unknown' for r in pending):
                raise FactoryError('earlier_editor_write_unknown_reconcile_first')
            if readback is None or fingerprint(readback) != packet['baseline_sha256']:
                raise FactoryError('fresh_editor_readback_required_reprepare_exchange')
            if file_sha(project_path(root, packet['source'], exists=True)) != packet['source_sha256']:
                raise FactoryError('cut_list_changed_recompile')
            _check_planning_sources(root, packet)
            record.update(state='dispatched_unknown', dispatched_at=time.time())
        elif state in ('completed', 'failed'):
            if record['state'] not in ('dispatched_unknown', state) or not receipt:
                raise FactoryError('actual_editor_receipt_or_reconciled_readback_required')
            result = {'path': str(receipt), 'sha256': file_sha(Path(receipt))}
            if record['state'] == state and record.get('receipt') != result:
                raise FactoryError('editor_receipt_conflict')
            record.update(state=state, receipt=result, recorded_at=time.time())
        elif state != 'status':
            raise FactoryError('invalid_editor_operation_state')
        if state != 'status':
            write_json(path, record)
    return {**record, 'operation': operation,
            'next': '实际调用原生工具；每次写后回读，重建下一份派生包；未知结果先查元素/回执，禁止重插'}


def verify_file(root, episode, path, *, source_audio=None):
    root = project(root)
    actual = project_path(root, path, exists=True)
    info = probe(actual)
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(actual), '-f', 'null', '-'],
                   check=True, capture_output=True, timeout=1800)
    result = {'file': path, 'probe': info, 'full_decode': 'passed', 'continuous_av': 'unreviewed',
              'content': 'unreviewed', 'selection': 'not_recorded', 'user_confirmation': 'not_requested'}
    if source_audio:
        source = project_path(root, source_audio, exists=True)
        def pcm(value):
            return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(value), '-map', '0:a:0',
                '-c:a', 'pcm_s16le', '-f', 'hash', '-hash', 'sha256', '-'], timeout=600).decode().strip()
        result['audio_pcm_equal'] = pcm(source) == pcm(actual)
    write_json(project_path(root, LEDGER / 'deliveries' / episode / (info['sha256'] + '.json')), result)
    return result
