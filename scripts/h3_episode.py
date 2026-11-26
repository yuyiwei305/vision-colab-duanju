#!/usr/bin/env python3
"""Compile the official episode Markdown into a finite native-canvas H3 batch.

Creative truth stays in the five Markdown documents. JSON files written here
are immutable production inputs and previews in the existing official ledger.
"""
import argparse
import json
from pathlib import Path
import re
import sys

import colab_h3 as h
import h3_workflow_v4 as v4
import h3_native_voice as native_voice


def prepare(project, episode, batch_id, *, max_seconds=7200, max_ccu=30, runtime_from=None, only=None,
            pipeline='pika', authorization_basis=None, ordered_entries=None, episode_spec=None):
    if pipeline not in ('pika', 'v4'):
        raise h.H3Error('invalid_h3_pipeline')
    is_v4 = pipeline == 'v4'
    if is_v4 and not authorization_basis:
        raise h.H3Error('current_task_authorization_basis_required')
    root = Path(project).resolve(strict=True)
    if not re.fullmatch(r'EP\d{3}', episode) or not re.fullmatch(r'[a-z0-9-]{1,35}', batch_id):
        raise h.H3Error('invalid_episode_or_batch_id')
    directory = root / '.short-drama/production/colab' / batch_id
    destination = directory / 'batch.json'
    if destination.exists(): raise h.H3Error('existing_batch_reconcile_before_prepare')
    source = f'剧集/{episode}/视频提示词.md'
    document = (root / source).read_text()
    if is_v4:
        from h3_shot_contract import validate
        board_path = root / '剧集' / episode / '分镜.md'
        validate(board_path.read_text() if board_path.exists() else None, document, only=only)
    entries = re.findall(r'^## (MOTION-EP\d{3}-\d{3}) ·', document, re.M)
    if (not entries or len(set(entries)) != len(entries)
            or any(not e.startswith('MOTION-' + episode + '-') for e in entries)):
        raise h.H3Error('finite_unique_episode_entries_required')
    if only and (len(set(only)) != len(only) or any(e not in entries for e in only)):
        raise h.H3Error('selected_entries_must_exist_and_be_unique')
    source_order = {entry: index for index, entry in enumerate(entries)}
    selected = [entry for entry in entries if not only or entry in only]
    if not 1 <= len(selected) <= 32:
        raise h.H3Error('finite_batch_requires_1_to_32_selected_entries')
    if ordered_entries is not None:
        if len(ordered_entries) != len(selected) or set(ordered_entries) != set(selected):
            raise h.H3Error('ordered_entries_must_match_selection')
        entries = list(ordered_entries)
    profile = json.loads((root / 'short-drama.json').read_text())
    ratio = profile['format']['aspect_ratio']
    if is_v4:
        from drama_factory.core import resolve_spec, validate_spec
        episode_spec = validate_spec(episode_spec) if episode_spec is not None else resolve_spec(root, episode)
        if episode_spec['episode'] != episode:
            raise h.H3Error('episode_spec_identity_mismatch')
        ratio = '16:9' if episode_spec['production']['native_canvas'] == [1344, 768] else '9:16'
    authority = profile['creator_authority']['production_profile']
    if authority.get('status') != 'accepted' or authority['choices'].get('video_prompt_dialect') != 'minimax-h3':
        raise h.H3Error('accepted_h3_profile_required')
    production = h.engine()
    jobs, previews, identities = [], [], {}
    inference_profile = v4.PROFILE if is_v4 else h.EPISODE_PROFILE
    setup = h.ROOT / (v4.RUNTIME if is_v4 else 'docs/h3/pika-ref-episode-v2-runtime.json')
    if h.read_json(setup)['profile'] != inference_profile:
        raise h.H3Error('episode_runtime_required')
    routes = {}
    if is_v4:
        records = []
        for entry in entries:
            if only and entry not in only: continue
            section = production._markdown_section(document, entry)
            parent = re.search(r'^- 续接来源：(无|MOTION-EP\d{3}-\d{3})$', section, re.M)
            if not parent: raise h.H3Error('duration_and_context_source_required')
            try: route = v4.select_scene(section, production._copyable_prompt(section))
            except ValueError as exc: raise h.H3Error(str(exc)) from exc
            records.append((entry, parent[1], route))
        try: routes = v4.route_chains(records)
        except ValueError as exc: raise h.H3Error(str(exc)) from exc
    for i, entry in enumerate(entries):
        if only and entry not in only: continue
        section = production._markdown_section(document, entry)
        duration = re.search(r'^- 时长：(\d+)s$', section, re.M)
        parent = re.search(r'^- 续接来源：(无|MOTION-EP\d{3}-\d{3})$', section, re.M)
        if not duration or not parent: raise h.H3Error('duration_and_context_source_required')
        parent = parent[1]
        if parent != '无' and (i == 0 or parent != entries[i-1] or parent not in identities):
            raise h.H3Error('context_must_reference_previous_entry')
        bindings = production._markdown_reference_bindings(section, field_name='输入参考图')
        for b in bindings: b['role'] = 'reference_image'
        audio = production._markdown_audio_bindings(section)
        for j, b in enumerate(audio):
            b.update(role='reference_audio', order=len(bindings) + j + 1)
        bindings += audio
        job_id = entry.replace('MOTION-', '') + '-' + batch_id
        stem = f'剧集/{episode}/制作成果/video/{entry}-{batch_id}'
        preset = re.search(r'^- H3上下文：(\S+)\s*$', section, re.M)
        seed_field = re.search(r'^- Seed：(\d+)\s*$', section, re.M)
        if re.search(r'^- Seed：', section, re.M) and not seed_field:
            raise h.H3Error('nonnegative_integer_seed_required')
        seed = int(seed_field[1]) if seed_field else 2026110940+source_order[entry]
        parameters = (v4.parameters(routes[entry]['scene_profile'], seed,
                                   int(duration[1]), ratio, identities.get(parent),
                                   preset[1] if preset else '5v24a') if is_v4 else
                      h.episode_parameters('comfy_kitchen_int8', 2026110940+source_order[entry],
                                           int(duration[1]), ratio, identities.get(parent)))
        if is_v4:
            parameters['scene_routing'] = routes[entry]
            try:
                recipes = native_voice.parse(section, identities)
            except ValueError as exc:
                raise h.H3Error(str(exc)) from exc
            if recipes:
                parameters['native_voice_references'] = recipes
        job = {'job_id': job_id, 'modality': 'video', 'adapter': 'h3-colab',
               'source': source, 'source_entry': entry,
               'prompt': production._copyable_prompt(section),
               'reference_bindings': bindings,
               'outputs': h.episode_outputs(stem, identities.get(parent)),
               'overwrite': False,
               'parameters': {**parameters,
                    'implementation_sha256': h.implementation_digest(), 'setup_sha256': h.digest(setup)}}
        names = [h.digest(root/b['path']) + Path(b['path']).suffix.lower() for b in bindings]
        for b in audio:
            import subprocess
            probe = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries',
                'format=duration','-of','json',str(root/b['path'])]))
            if not 2 <= float(probe['format']['duration']) <= 15:
                raise h.H3Error('voice_reference_duration_outside_contract')
        h.build_graph(job, names, 'preflight')
        path = directory / ('input-' + entry + '.json')
        if path.exists():
            if h.read_json(path) != job:
                raise h.H3Error('partial_prepare_inputs_changed')
        else:
            h.write_json(path, job, exclusive=True)
        previews.append(production.prepare_job(root, path))
        jobs.append(job_id); identities[entry] = job_id
    import hashlib
    namespace = hashlib.sha256(str(root).encode()).hexdigest()[:10]
    config = Path.home().resolve() / '.config/vision-media' / ('drama-adapters-colab-' + namespace + '-' + batch_id + '.json')
    batch = {'schema_version':1, 'profile':inference_profile, 'batch_id':batch_id,
        'batch_file':str(destination), 'project':str(root), 'jobs':jobs, 'gpu':'G4',
        'session':'h3-'+namespace+'-'+batch_id, 'max_seconds':max_seconds, 'max_ccu':max_ccu,
        'max_rate':15, 'max_attempts_per_job':1,
        'resource_state':str(directory/'resources.json'), 'setup_manifest':str(setup),
        'setup_sha256':h.digest(setup), 'implementation_sha256':h.implementation_digest(),
        'adapter_config':str(config)}
    if authorization_basis: batch['authorization_basis'] = authorization_basis
    if is_v4:
        from drama_factory import RELEASE
        from drama_factory.releases import release_path
        batch.update(execution_release=RELEASE, execution_release_sha256=h.digest(release_path(RELEASE)),
                     episode_spec=episode_spec)
    if runtime_from:
        parent_path = Path(runtime_from).resolve(strict=True)
        if not parent_path.is_relative_to(root / '.short-drama/production/colab'):
            raise h.H3Error('runtime_must_belong_to_current_project')
        parent = h.read_json(parent_path)
        batch.update(runtime_from=str(parent_path), session=parent['session'],
                     max_seconds=min(max_seconds,parent['max_seconds']),max_ccu=min(max_ccu,parent['max_ccu']))
    h.validate_batch(batch)
    adapter = {'adapters':{'h3-colab':{
        'command':[sys.executable,str(h.SELF),'adapter','--batch',str(destination)],
        'timeout_seconds':3300,'reference_roles':['reference_image','reference_audio']}}}
    if config.exists() and h.read_json(config) != adapter:
        raise h.H3Error('adapter_config_conflict')
    if not config.exists(): h.write_json(config, adapter, exclusive=True)
    h.write_json(destination,batch,exclusive=True)
    h.write_json(directory/'previews.json',previews,exclusive=True)
    paragraphs = [f'# {episode} H3 原生尺寸整集批量预览',
        f'本批 {len(jobs)} 段；各段一次推理，单台 G4，保守资源上限 {max_seconds/60:g} 分钟 / {max_ccu:g} CCU。费用按实际观测，余额不是预算。',
        'Pika Ref2VA 六步，4–15 秒按内容变化；帧数自动上取到 17k+5，实际时长以输出为准，不为凑整数裁尾。',
        '横屏1344×768 / 竖屏768×1344，不裁边、不缩放。独立段只编码保存一份native；真实续接段去除前部5帧重复上下文，并在context-masters目录保留完整母版。AV latent留在运行账本。',
        ('仅使用源文档实际绑定的音色参考；H3原生生成对白及口型。' if any(b['role']=='reference_audio' for p in previews for b in p['reference_bindings']) else '本批无参考音频；用户已暂缓Breeze接入，使用H3原生声音。') + '不保证逐字准确或跨镜音色完全一致。无后期配音、字幕、响度重写或公开发布。',
        '较长时长、音频参考与潜空间组合尚待本批实测，技术失败即停止后续提交并释放GPU，恢复先查原任务。']
    if is_v4:
        paragraphs[2] = ('V4 Cobijada Hybrid；按镜头本地动作规则自动选择文戏/打斗，同一续接链固定一档；'
                         'Euler + Simple 8/12步 + Sigma精修1步；Self-Lift 低分6/8步后整幅续采。')
        paragraphs[3] = ('原生画布1344×768或768×1344，无裁边缩放；帧数按Easy原生17k+5最近网格；'
                         '上下文档位逐段见参数：22v18a续接比旧5帧基线补17原始帧，去22帧重复头；'
                         '保留音轨、完整母版与真实AV latent。')
        paragraphs[4] = ('沿用本镜真实图片与角色WAV；两组LoRA互斥，Lightx2v共用；'
                         '显式省显存补丁、高清分块、latent时间分片关闭；无OOM省显存回退。')
        paragraphs[5] = ('GPU兼容性、画质、口型与续接尚待本次有限任务验证。原生音色依赖先冻结来源及截取窗口，'
                         '源段取回验SHA后自动提取WAV并绑定；无额外TTS、不保证无底噪，不冒称已听审。')
        paragraphs.insert(1, '任务授权来源：' + authorization_basis)
    for p in previews:
        paragraphs += ['## '+p['source_entry'], p['prompt'],
            '```json\n'+json.dumps({k:p[k] for k in ['reference_bindings','parameters','outputs']},ensure_ascii=False,indent=2)+'\n```']
    preview = directory/'生产预览.md';preview.write_text('\n\n'.join(paragraphs)+'\n')
    return {'state':'prepared','batch':str(destination),'preview':str(preview),'jobs':len(jobs)}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare'])
    parser.add_argument('--project',required=True);parser.add_argument('--episode',required=True)
    parser.add_argument('--batch-id',required=True)
    parser.add_argument('--max-seconds',type=int,default=7200)
    parser.add_argument('--max-ccu',type=float,default=30)
    parser.add_argument('--runtime-from',help='Reuse an owned paused batch VM without reallocating or reloading models.')
    parser.add_argument('--entry',action='append',help='Only these source entries, for explicit recovery of unfinished clips.')
    parser.add_argument('--pipeline', choices=['pika', 'v4'], default='pika',
                        help='The factory uses V4. Pika is the legacy compatibility mode.')
    parser.add_argument('--authorization', help='Current task scope/source; required for V4, never inherited from another work.')
    args=parser.parse_args()
    print(json.dumps(prepare(args.project,args.episode,args.batch_id,
                            max_seconds=args.max_seconds,max_ccu=args.max_ccu,
                            runtime_from=args.runtime_from,only=args.entry,pipeline=args.pipeline,
                            authorization_basis=args.authorization),ensure_ascii=False))
