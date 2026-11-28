"""Full-film ReCaVSR preparation and finishing recovery."""
from __future__ import annotations

from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from . import RELEASE
from .core import (LEDGER, ROOT, FactoryError, episode_id, file_sha, fingerprint,
                   project, project_path, read_json, write_json)
from .delivery import frames, probe, rational

PROFILE = ROOT / 'docs/finishing/recavsr-runtime.json'
WORKER = ROOT / 'scripts/recavsr_remote.py'
ADAPTER = 'recavsr-comfy-colab'
SUPPORTED_FPS = (24, 25, 30, 50, 60)


def geometry(width, height, short_edge):
    if type(short_edge) is not int or short_edge < 16 or short_edge % 2 or min(width, height) < 16:
        raise FactoryError('finishing_requires_even_target_short_edge')
    scale = Fraction(short_edge, min(width, height))
    dimensions = tuple(2 * frames(side * scale / 2, 1) for side in (width, height))
    if not Fraction(1, 2) <= scale <= 8 or max(dimensions) > 5120 or min(dimensions) > 3072:
        raise FactoryError('finishing_geometry_outside_g4_profile')
    # ComfyUI rounds scaled width and height to whole pixels. When necessary,
    # choose a single scale within both even-pixel rounding intervals.
    if tuple(frames(side * scale, 1) for side in (width, height)) != dimensions:
        intervals = [(Fraction(2 * target - 1, 2 * side), Fraction(2 * target + 1, 2 * side))
                     for side, target in zip((width, height), dimensions)]
        lower = max(Fraction(1, 2), *(start for start, _ in intervals))
        upper = min(Fraction(8), *(end for _, end in intervals))
        if lower >= upper:
            raise FactoryError('finishing_geometry_cannot_use_one_scale')
        scale = (lower + upper) / 2
    return *dimensions, float(scale)


def master_receipts(root):
    for path in (root / LEDGER / 'colab').glob('*/receipt.json'):
        job_path = path.with_name('job.json')
        if not job_path.is_file():
            continue
        job = read_json(job_path)
        if job.get('adapter') == ADAPTER:
            spec = job['parameters']
            receipt = read_json(path).get('files', {}).get(spec['master_filename'])
            if receipt:
                yield spec, receipt


def output_frames(spec, name):
    return spec['target_frames'] if name == 'final.mp4' else spec['frames']


def require_frame_clock(path, video, count, fps):
    time_base = rational(video['time_base'])
    packets = subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
        '-show_entries', 'packet=pts', '-of', 'json', str(path),
    ], timeout=120)
    timestamps = sorted(p.get('pts', -1) for p in json.loads(packets)['packets'])
    ticks_per_frame = 1 / (fps * time_base)
    if len(timestamps) != count or any(
        abs(timestamp - index * ticks_per_frame) > Fraction(1, 2)
        for index, timestamp in enumerate(timestamps)
    ):
        raise FactoryError('finishing_requires_zero_based_constant_frame_clock')


def verify_original_audio(source, final):
    def tracks(path):
        data = subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                                        '-of', 'json', str(path)], timeout=60)
        streams = json.loads(data)['streams']
        video_start = rational(next(s for s in streams if s['codec_type'] == 'video')['start_time'])
        return [(s['codec_name'], s['sample_rate'], s['channels'],
                 rational(s.get('start_time', '0')) - video_start)
                for s in streams if s['codec_type'] == 'audio']

    def stream_hash(path, codec):
        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a',
                                        '-c:a', codec, '-f', 'streamhash', '-hash', 'sha256', '-'], timeout=600)

    source_tracks = tracks(source)
    if source_tracks != tracks(final):
        raise FactoryError('output_audio_streams_or_start_times_changed')
    if source_tracks:
        if stream_hash(source, 'copy') != stream_hash(final, 'copy'):
            raise FactoryError('output_original_audio_packets_changed')
        if stream_hash(source, 'pcm_s16le') != stream_hash(final, 'pcm_s16le'):
            raise FactoryError('output_original_audio_samples_changed')
    return 'identical' if source_tracks else 'no_source_audio'


def prepare(root, episode, task_id, *, source, target_fps=60, cuts, authorization,
            max_ccu, cuts_evidence, max_seconds=None, short_edge=2160, rife_zip=None, master=None):
    import relay_vsr_comfy_colab as relay
    from .releases import release_path, verify_release
    verify_release(RELEASE)
    root = project(root)
    episode = episode_id(episode)
    if not re.fullmatch(r'[a-z0-9-]{1,35}', task_id) or not authorization or not cuts_evidence:
        raise FactoryError('finishing_identity_authorization_and_cut_evidence_required')
    if (type(max_ccu) not in (int, float) or not math.isfinite(max_ccu) or not 0 < max_ccu <= 50
            or (max_seconds is not None and (type(max_seconds) is not int or not 0 < max_seconds <= 28800))):
        raise FactoryError('finite_finishing_budget_required')
    path = project_path(root, source, exists=True)
    info = probe(path)
    source_fps = rational(info['fps'])
    if source_fps not in SUPPORTED_FPS:
        raise FactoryError('finishing_requires_supported_integer_source_fps')
    if type(target_fps) is not int or target_fps not in SUPPORTED_FPS or target_fps < source_fps:
        raise FactoryError('unsupported_finishing_target_fps')
    if not isinstance(cuts, list) or any(type(c) is not int for c in cuts) or cuts != sorted(set(cuts)) or any(
            c <= 0 or c >= info['frames'] for c in cuts):
        raise FactoryError('verified_ordered_interior_cut_frames_required')
    width, height, scale = geometry(info['width'], info['height'], short_edge)
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    if video.get('pix_fmt') != 'yuv420p' or video.get('color_transfer') not in (
            None, 'unknown', 'bt709', 'iec61966-2-1'):
        raise FactoryError('finishing_requires_8bit_sdr_yuv420p')
    if video.get('sample_aspect_ratio') not in (None, 'N/A', '0:1', '1:1') or any(
            side.get('rotation', 0) for side in video.get('side_data_list', [])):
        raise FactoryError('finishing_requires_upright_square_pixels')
    if not info['frames'] or video.get('r_frame_rate') != f'{source_fps}/1' or rational(
            video.get('start_time', '0')) != 0:
        raise FactoryError('finishing_requires_zero_based_constant_frame_clock')
    require_frame_clock(path, video, info['frames'], source_fps)
    profile = read_json(PROFILE)
    use_rife = target_fps != source_fps
    master_info = None
    if master:
        master_path = project_path(root, master, exists=True)
        master_info = probe(master_path)
        if (master_info['audio'] or
                (master_info['width'], master_info['height'], master_info['fps'], master_info['frames']) !=
                (width, height, str(source_fps), info['frames'])):
            raise FactoryError('existing_master_must_match_target_geometry_and_source_frame_clock')
        # Only a completed ReCaVSR master with a matching source can take this path.
        if not any(
            prior.get('input_sha256') == info['sha256']
            and prior.get('recavsr_commit') == profile['recavsr_commit']
            and receipt.get('sha256') == master_info['sha256']
            for prior, receipt in master_receipts(root)
        ):
            raise FactoryError('matching_recavsr_master_receipt_required')
        reserve = master_path.stat().st_size + 2 * relay.MAX_RESUME_OUTPUT_BYTES
    else:
        reserve = max(2 * 1024**3, path.stat().st_size * 16)
    if shutil.disk_usage(root).free < reserve:
        raise FactoryError('insufficient_local_finishing_space')
    archive = Path(rife_zip).resolve() if rife_zip else ROOT / '.local/finishing/rife-4.25.zip'
    if use_rife and (not archive.is_file() or file_sha(archive) != profile['rife_zip_sha256']):
        raise FactoryError('pinned_rife_weights_required')
    directory = project_path(root, LEDGER / 'colab' / task_id)
    batch_path = directory / 'batch.json'
    spec = {k: v for k, v in profile.items() if k not in ('schema_version', 'profile')}
    spec.update(finishing_protocol='recavsr-whole-film-v1', source_fps=int(source_fps), target_fps=target_fps,
                output_width=width, output_height=height, frames=info['frames'], cuts=cuts, scale=scale,
                target_frames=frames(info['duration'], target_fps), input_sha256=info['sha256'],
                has_audio=info['audio'], prompt_language='zh-CN',
                ratio=str(Fraction(info['width'], info['height'])).replace('/', ':'),
                master_filename=f'recavsr-{width}x{height}-{source_fps}fps-master.mp4',
                color_transfer=video.get('color_transfer'))
    stem = f'剧集/{episode}/制作成果/超分/{task_id}'
    job = {'job_id': task_id, 'adapter': ADAPTER, 'modality': 'video',
           'source': f'剧集/{episode}/剪辑单.md', 'references': [source],
           'reference_bindings': [{'slot_id': 'REF-FINISH-SOURCE', 'role': 'reference_video',
                'path': source, 'order': 1, 'label': '锁定全片', 'may_control': ['全部画面','原剪辑','原声'],
                'must_not_control': ['新增剧情']}],
           'prompt': f'用 ComfyUI ReCaVSR 一次处理完整原片，等比输出 {width}×{height}。'
                     f'目标 {target_fps} fps，升帧时用 RIFE 并跳过跨切插值，回封完整原声。',
           'parameters': spec, 'outputs': [stem + f'-{target_fps}fps.mp4'],
           'overwrite': False}
    if master_info:
        job['references'].append(master)
        job['reference_bindings'].append({'slot_id': 'REF-FINISH-MASTER', 'role': 'reference_video',
            'path': master, 'order': 2, 'label': '已校验的 ReCaVSR 无声母版',
            'may_control': ['补帧输入'], 'must_not_control': ['原声']})
        job['prompt'] = '复用同一源片的 ReCaVSR 母版。升帧时用 RIFE 跳过跨切插值，回封完整原声。'
        spec['resume_master_sha256'] = master_info['sha256']
    namespace = fingerprint(str(root))[:10]
    adapter_path = Path.home().resolve() / '.config/vision-media' / f'factory-finish-{namespace}-{task_id}.json'
    batch = {'schema_version': 2, 'execution_release': RELEASE, 'finishing_backend': ADAPTER,
             'execution_release_sha256': file_sha(release_path(RELEASE)), 'project': str(root), 'episode': episode,
             'batch_file': str(batch_path), 'job_id': task_id, 'session': 'finish-' + namespace + '-' + task_id,
             'state_file': str(directory / 'resource.json'), 'durable_root': str(directory / 'results'),
             'adapter_config': str(adapter_path), 'gpu': 'G4', 'max_rate': 15,
             'max_ccu': max_ccu, 'max_seconds': max_seconds, 'authorization': authorization,
             'rife_zip': str(archive), 'worker_sha256': file_sha(WORKER),
             'preflight': {'source': info, 'cuts_evidence': cuts_evidence,
                           'local_disk_reserve_bytes': reserve}}
    if master_info:
        batch['preflight']['master'] = master_info
    if batch_path.exists():
        if read_json(batch_path) != batch or read_json(directory / 'job.json') != job:
            raise FactoryError('existing_finishing_scope_changed_create_explicit_revision')
        return {'state': 'existing_preparation', 'batch': str(batch_path),
                'preview': read_json(directory / 'preview.json'), 'preflight': batch['preflight']}
    job_path = directory / 'job.json'
    if job_path.exists() and read_json(job_path) != job:
        raise FactoryError('interrupted_finishing_prepare_scope_changed')
    if not job_path.exists():
        write_json(job_path, job, exclusive=True)
    preview = relay.engine().prepare_job(root, directory / 'job.json')
    adapter = {'adapters': {ADAPTER: {
        'command': [sys.executable, str(ROOT / 'scripts/relay_vsr_comfy_colab.py'), 'adapter', '--batch', str(batch_path)],
        'timeout_seconds': None if max_seconds is None else max_seconds + 120, 'reference_roles': ['reference_video']}}}
    if adapter_path.exists() and read_json(adapter_path) != adapter:
        raise FactoryError('finishing_adapter_scope_conflict')
    if not adapter_path.exists():
        write_json(adapter_path, adapter, exclusive=True)
    write_json(directory / 'preview.json', preview)
    write_json(batch_path, batch, exclusive=True)
    return {'state': 'prepared_no_gpu', 'batch': str(batch_path), 'preview': preview, 'preflight': batch['preflight']}


def recover_local_outputs(job, batch):
    """Return cached outputs for official collect after a lost acknowledgement/release."""
    import relay_vsr_colab as relay
    from relay_vsr_comfy_colab import stage_copy
    receipt_path = Path(batch['state_file']).with_name('receipt.json')
    if not receipt_path.exists():
        return None
    receipt = read_json(receipt_path)
    spec = job['parameters']
    path = Path(batch['durable_root']) / 'final.mp4'
    expected = receipt.get('files', {}).get(path.name)
    if not expected or not path.is_file() or file_sha(path) != expected['sha256']:
        return None
    relay.verify_media(path, frames=spec['target_frames'], fps=spec['target_fps'],
                       width=spec['output_width'], height=spec['output_height'], audio=spec['has_audio'])
    destination = Path(job['output_root']) / path.name
    stage_copy(path, destination)
    verification = Path(batch['state_file']).with_name('verification.json')
    if not verification.exists():
        source = project_path(batch['project'], job['references'][0], exists=True)
        if file_sha(source) != spec['input_sha256']:
            raise FactoryError('finishing_source_changed')
        audio = verify_original_audio(source, path)
        write_json(verification, {'original_audio_packets': audio,
                                 'recovery': 'local_full_decode_sha_and_audio'})
    return {'outputs': [{'target': job['outputs'][0], 'source': str(destination)}],
            'provider_job_id': job['collect_provider_job_id']}


def resume_allocation(batch, state, transport, spec, existing_master=None, *, start_guard=None):
    """Replace a released instance using the remaining original budget."""
    import relay_vsr_colab as relay
    import colab_h3 as h
    if state['state'] != 'released':
        raise FactoryError('released_finishing_allocation_required')
    live = {v['endpoint'] for v in transport.control('assignments')}
    if state.get('endpoint') in live:
        raise FactoryError('release_not_confirmed')
    usage = transport.control('usage')
    paid_start = state.get('paid_started_at', state['started_at'])
    last_lease_cost = batch['max_rate'] * max(0, state.get('released_at', time.time()) - paid_start) / 3600
    spent = max(state['usage_before']['balance'] - usage['balance'],
                state.get('previous_lease_ccu', 0) + last_lease_cost)
    if ((not state.get('ccu_only') and time.time() + 900 >= state['deadline'])
            or spent + batch['max_rate'] * 900 / 3600 >= batch['max_ccu']):
        raise FactoryError('original_finishing_budget_or_deadline_exhausted')
    master = existing_master if existing_master is not None else Path(batch['durable_root']) / spec['master_filename']
    if not master.is_file():
        raise FactoryError('no_verified_master_reconcile_original_compute_before_new_generation')
    relay.verify_media(master, frames=spec['frames'], fps=spec['source_fps'], width=spec['output_width'], height=spec['output_height'])
    expected_sha = (spec['resume_master_sha256'] if existing_master is not None
                    else state.get('preserved', {}).get(master.name, {}).get('sha256'))
    if expected_sha != file_sha(master):
        raise FactoryError('preserved_master_identity_missing')
    state.setdefault('previous_allocations', []).append({'endpoint': state.get('endpoint'), 'finished_at': state.get('finished_at')})
    previous_endpoint = state.pop('endpoint', None)
    state.update(state='allocating', resume_master_sha256=file_sha(master), worker_started=False)
    if state.get('ccu_only'):
        state.pop('paid_started_at', None)
        state.update(deadline=None, started_at=time.time(), previous_lease_ccu=spent,
                     ccu_remaining_at_reservation=batch['max_ccu'] - spent, controller_pid=os.getpid())
    h.write_json(batch['state_file'], state)
    if start_guard:
        start_guard(batch)
    endpoint = relay.allocate(transport, batch, previous_endpoint=previous_endpoint)
    if h.read_json(batch['state_file'])['state'] != 'allocating':
        raise FactoryError('colab_allocation_stopped')
    state.update(state='uploading', endpoint=endpoint)
    if state.get('ccu_only'):
        h.colab_allocation.claim_paid_lease(batch['state_file'], state, batch['max_rate'])
    state.pop('finished_at', None)
    h.write_json(batch['state_file'], state)
    return state
