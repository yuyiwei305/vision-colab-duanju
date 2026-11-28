#!/usr/bin/env python3
"""Finite H3 batches using the existing Colab login and drama production ledger.

No command creates a GPU except `run`. Adapter collection never submits work.
The official CLI runs in its installed Python environment; interactive OAuth
fallback is disabled without modifying that installation or its credentials.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import h3_media_retention as media_retention

import h3_workflow_v4 as v4
import h3_native_voice as native_voice
from drama_factory import colab_allocation
from drama_factory.core import FactoryError

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
REMOTE = SELF.with_name('colab_h3_remote.py')
PIKA_NODES = SELF.with_name('h3_pika_nodes.py')
MARKER = 'H3_RESULT='
MODEL_NAMES = {
    'unet': 'minimax_h3_ref2va_pruned_int8_convrot.safetensors',
    'clip': 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
    'video_vae': 'minimax_h3_video_vae_fp16.safetensors',
    'audio_vae': 'minimax_h3_audio_vae_fp32.safetensors',
}
PIKA_SIGMAS = '1.0, 0.972973, 0.923077, 0.8, 0.571429, 0.333333, 0.0'
PIKA_BACKENDS = ('pytorch', 'comfy_kitchen_int8', 'sage3')
EPISODE_PROFILE = 'pika-ref-episode-v2'
EPISODE_PROFILES = (EPISODE_PROFILE, v4.PROFILE)


def episode_outputs(stem, parent=None):
    outputs = [stem + '-native.mp4']
    if parent:
        path = Path(stem)
        outputs.append(str(path.parent.parent / 'context-masters' /
                           (path.name + '-context-master.mp4')))
    return outputs


def episode_parameters(backend, seed, duration, ratio='16:9', parent=None):
    if backend not in PIKA_BACKENDS or type(duration) is not int or not 4 <= duration <= 15:
        raise H3Error('unsupported_episode_duration')
    if ratio not in ('16:9', '9:16'):
        raise H3Error('unsupported_episode_ratio')
    frames = duration * 24
    while frames % 17 != 5: frames += 1
    width, height = (1344, 768) if ratio == '16:9' else (768, 1344)
    return {**pika_parameters(backend, seed), 'profile': EPISODE_PROFILE,
            'ratio': ratio, 'width': width, 'height': height,
            'internal_width': width, 'internal_height': height,
            'duration': duration, 'frame_count': frames,
            'continue_from_job': parent, 'context_frames': 5 if parent else 0}


def pika_parameters(backend, seed):
    if backend not in PIKA_BACKENDS: raise H3Error('unsupported_attention_backend')
    return {'profile': 'pika-ref-v1', 'prompt_language': 'en',
            'model': 'minimax-h3-pika-flex-ref2va-v1', 'ratio': '9:16',
            'width': 720, 'height': 1280, 'internal_width': 768, 'internal_height': 1344,
            'frame_rate': 24, 'frame_count': 124, 'duration': 5,
            'steps': 6, 'sampler': 'lcm', 'scheduler': 'pika-manual',
            'sigmas': PIKA_SIGMAS, 'lora_strength': 1.0,
            'attention_backend': backend, 'seed': seed}


class H3Error(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def read_json(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise H3Error('unsafe_metadata')
    return json.loads(path.read_bytes())


def write_json(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise H3Error('unsafe_metadata')
    fd, tmp = tempfile.mkstemp(prefix='.h3-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(canonical(value)); f.flush(); os.fsync(f.fileno())
        if exclusive:
            os.link(tmp, path)
        else:
            os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(tmp).unlink(missing_ok=True)


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def engine():
    return module(ROOT / 'scripts/drama-group-production.py', 'h3_production').load_engine()


def build_graph(job, filenames, request_id, context=None):
    if job.get('parameters', {}).get('profile') == v4.PROFILE:
        try:
            return v4.build_graph(job, filenames, request_id, context)
        except ValueError as exc:
            raise H3Error(str(exc)) from exc
    bindings = job.get('reference_bindings', [])
    episode = job.get('parameters', {}).get('profile') == EPISODE_PROFILE
    pika = episode or job.get('parameters', {}).get('profile') == 'pika-ref-v1'
    pictures = [b for b in bindings if b.get('role') == 'reference_image']
    audios = [b for b in bindings if b.get('role') == 'reference_audio']
    if (not 1 <= len(pictures) <= (4 if pika else 2) or len(filenames) != len(bindings)
            or len(pictures) + len(audios) != len(bindings)
            or (audios and (not episode or len(audios) > 3))):
        raise H3Error('unsupported_reference')
    expected_outputs = (2 if job.get('parameters', {}).get('continue_from_job') else 1) if episode else (2 if pika else 1)
    if job.get('modality') != 'video' or len(job.get('outputs', [])) != expected_outputs:
        raise H3Error('video_outputs_required')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', request_id):
        raise H3Error('invalid_request_id')
    p = job['parameters']
    fixed = {'ratio': '9:16', 'width': 720, 'height': 1280, 'internal_width': 736,
             'frame_rate': 24, 'frame_count': 124, 'duration': 5, 'steps': 20,
             'sampler': 'res_multistep', 'scheduler': 'simple', 'prompt_language': 'en',
             'model': 'minimax-h3-ref2va-pruned-int8-convrot'}
    if pika: fixed = pika_parameters(p.get('attention_backend'), p.get('seed'))
    if episode:
        fixed = episode_parameters(p.get('attention_backend'), p.get('seed'),
                                   p.get('duration'), p.get('ratio'), p.get('continue_from_job'))
    if any(p.get(key) != value for key, value in fixed.items()):
        raise H3Error('unsupported_parameters')
    if not isinstance(p.get('seed'), int) or isinstance(p['seed'], bool) or not 0 <= p['seed'] < 2**63:
        raise H3Error('invalid_seed')
    contract = ' '.join(
        f'<Picture {i}>: label={b["label"]}; allowed={json.dumps(b["may_control"], ensure_ascii=False)}; '
        f'not controlled={json.dumps(b["must_not_control"], ensure_ascii=False)}.'
        for i, b in enumerate(pictures, 1))
    prompt = job['prompt'].replace('\ndetailed_description:', '\n' + contract + '\ndetailed_description:', 1)
    if prompt == job['prompt']:
        raise H3Error('h3_full_reference_prompt_required')
    def n(kind, **inputs): return {'class_type': kind, 'inputs': inputs}
    g = {
        '1': n('UNETLoader', unet_name=MODEL_NAMES['unet'], weight_dtype='default'),
        '2': n('CLIPLoader', clip_name=MODEL_NAMES['clip'], type='minimax', device='default'),
        '3': n('VAELoader', vae_name=MODEL_NAMES['video_vae']),
        '4': n('VAELoader', vae_name=MODEL_NAMES['audio_vae']),
        '5': n('MiniMaxH3SigmaShift', model=['1', 0], shift_video=12.0, shift_audio=3.0),
        '6': n('MiniMaxH3ReferenceToVideo', clip=['2', 0], vae=['3', 0], audio_vae=['4', 0],
               prompt=prompt, width=736, height=1280, length=124, ref_image_size='match'),
        '7': n('RandomNoise', noise_seed=p['seed']),
        '8': n('KSamplerSelect', sampler_name='res_multistep'),
        '9': n('BasicScheduler', model=['5', 0], scheduler='simple', steps=20, denoise=1.0),
        '10': n('BasicGuider', model=['5', 0], conditioning=['6', 0]),
        '11': n('SamplerCustomAdvanced', noise=['7', 0], guider=['10', 0], sampler=['8', 0],
                sigmas=['9', 0], latent_image=['6', 1]),
        '12': n('LTXVSeparateAVLatent', av_latent=['11', 0]),
        '13': n('VAEDecode', samples=['12', 0], vae=['3', 0]),
        '14': n('VAEDecodeAudio', samples=['12', 1], vae=['4', 0]),
        '15': n('ImageCrop', image=['13', 0], width=720, height=1280, x=8, y=0),
        '16': n('CreateVideo', images=['15', 0], audio=['14', 0], fps=24.0),
        '17': n('SaveVideo', video=['16', 0], filename_prefix='h3-' + request_id, format='mp4', codec='h264'),
    }
    image_index = audio_index = 0
    for i, (binding, filename) in enumerate(zip(bindings, filenames)):
        audio = binding['role'] == 'reference_audio'
        pattern = r'[0-9a-f]{64}\.(wav|mp3|flac|m4a|aac)' if audio else r'[0-9a-f]{64}\.(png|jpg|jpeg|webp)'
        if not re.fullmatch(pattern, filename):
            raise H3Error('unsafe_reference_filename')
        node = str(20 + i)
        if audio:
            g[node] = n('LoadAudio', audio=filename)
            g['6']['inputs'][f'ref_audios.ref_audio_{audio_index}'] = [node, 0]
            audio_index += 1
        else:
            g[node] = n('LoadImage', image=filename)
            g['6']['inputs'][f'ref_images.ref_image_{image_index}'] = [node, 0]
            image_index += 1
    if pika:
        g['3']['inputs']['vae_name'] = 'minimax_h3_video_vae_int8_convrot.safetensors'
        g['30'] = n('LoraLoaderModelOnly', model=['1', 0],
                    lora_name='pika_h3_flex_ref2va_v1_bf16.safetensors', strength_model=1.0)
        g['5']['inputs']['model'] = ['30', 0]
        g['31'] = n('H3PikaAttention', model=['5', 0], backend=p['attention_backend'], request_id=request_id)
        g['6']['inputs'].update(width=768, height=1344)
        g['8']['inputs']['sampler_name'] = 'lcm'
        g['9'] = n('ManualSigmas', sigmas=PIKA_SIGMAS)
        g['10']['inputs']['model'] = ['31', 0]
        g['15']['inputs'].update(width=756, height=1344, x=6)
        g['32'] = n('ImageScale', image=['15', 0], upscale_method='lanczos',
                    width=720, height=1280, crop='disabled')
        g['16']['inputs']['images'] = ['32', 0]
        g['18'] = n('CreateVideo', images=['13', 0], audio=['14', 0], fps=24.0)
        g['19'] = n('SaveVideo', video=['18', 0], filename_prefix='master-' + request_id,
                    format='mp4', codec='h264')
        g['40'] = n('H3PikaFinalize', video=['16', 0], request_id=request_id)
        g['17']['inputs']['video'] = ['40', 0]
    if episode:
        # v2 preserves the native canvas; no ImageCrop or ImageScale in this graph.
        g.pop('15'); g.pop('32')
        g['6']['inputs'].update(width=p['width'], height=p['height'], length=p['frame_count'])
        g['16']['inputs']['images'] = ['13', 0]
        g['90'] = n('MiniMaxH3MotionContextSaveLatent', latent=['11', 0],
                    filename_prefix='context-' + request_id, clip_index=1)
        if not p['continue_from_job']:
            # An independent clip is already the full native output: encode it once.
            g.pop('18'); g.pop('19')
        if p['continue_from_job']:
            if context is None:
                # Offline graph preflight checks topology without pretending a file exists.
                context = {'path': 'preflight-context.safetensors'}
            if not re.fullmatch(r'(?:context-[a-f0-9-]+_00001|preflight-context)\.safetensors', context['path']):
                raise H3Error('invalid_context_path')
            g['91'] = n('MiniMaxH3MotionContextLoadLatent', latent_path=context['path'], clip_index=1)
            g['92'] = n('MiniMaxH3MotionContext', conditioning=['6', 0], vae=['3', 0],
                        latent=['6', 1], context_latent=['91', 0], context_length='5', audio_context_length=24)
            g['10']['inputs']['conditioning'] = ['92', 0]
            g['93'] = n('MiniMaxH3MotionContextTrim', images=['13', 0], audio=['14', 0],
                        trim_frames=['92', 1], fps=24.0, match_tail=True)
            g['16']['inputs'].update(images=['93', 0], audio=['93', 1])
    return g


def _cli_timeout(name, default):
    try:
        value = int(os.environ.get(name, '') or 0)
    except ValueError:
        value = 0
    return value if value > 0 else default


class Colab:
    def __init__(self, session, *, timeout=None):
        self.session = session
        self.timeout = timeout if timeout is not None else _cli_timeout('COLAB_CLI_TIMEOUT', 600)
        cli = os.environ.get('COLAB_CLI') or shutil.which('colab')
        if not cli:
            raise H3Error('colab_cli_required: install google-colab-cli or set COLAB_CLI')
        launcher = Path(cli).expanduser().resolve(strict=True)
        python = launcher.with_name('python')
        if not python.is_file():
            raise H3Error('colab_python_required: use a Python environment containing google-colab-cli')
        self.python = str(python)

    def invoke(self, args, *, timeout=None):
        try:
            result = subprocess.run([self.python, str(SELF), *args], stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            raise H3Error('colab_response_unknown') from None
        if result.returncode:
            if args[:3] == ['_cli', '--auth=oauth2', 'new'] and b'Allocation refused (precondition failed)' in result.stderr:
                raise H3Error('colab_allocation_limit_or_capacity')
            # CLI exception bodies may contain runtime credentials or OAuth URLs.
            raise H3Error('colab_command_failed')
        return result.stdout.decode('utf-8', errors='replace')

    def control(self, action, value=''):
        return self.parse(self.invoke(['_control', action, value]))

    def allocation_request(self, operation, gpu, request_id, deadline, resource):
        timeout = min(2 * colab_allocation.HTTP_TIMEOUT + 3, max(0.01, deadline - time.time()))
        try:
            return self.parse(self.invoke(
                ['_allocation', operation, self.session, gpu, request_id, str(deadline), str(resource)],
                timeout=timeout))
        except H3Error as exc:
            if str(exc) != 'colab_response_unknown':
                raise
            return {'state': 'retry', 'reason': 'process_timeout'}

    @staticmethod
    def parse(output):
        lines = [s[len(MARKER):] for s in output.splitlines() if s.startswith(MARKER)]
        if len(lines) != 1:
            raise H3Error('invalid_colab_response')
        return json.loads(lines[0])

    def cli(self, *args, timeout=None):
        # Independent CLI processes share one Jupyter kernel. Serialize them
        # so a diagnostic execution cannot consume another request's replies.
        directory = Path.home() / '.cache/vision-media/colab-locks'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(self.session.encode()).hexdigest()
        with (directory / (key + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self.invoke(['_cli', '--auth=oauth2', *args], timeout=timeout)

    def execute(self, code, *, timeout=None):
        timeout = timeout if timeout is not None else _cli_timeout('COLAB_EXEC_TIMEOUT', 300)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', encoding='utf-8') as f:
            f.write(code); f.flush()
            return self.parse(self.cli('exec', '-s', self.session, '-f', f.name,
                                       '--timeout', str(timeout), timeout=timeout + 60))

    def remote(self, directory, action, data=None):
        code = ("import sys,json\n"
                f"sys.path.insert(0,{directory!r})\nimport colab_h3_remote as h\n"
                f"v=h.dispatch({directory!r},{action!r},json.loads({canonical(data or {}).decode()!r}))\n"
                f"print({MARKER!r}+json.dumps(v))\n")
        attempts = 5 if action in ('boot', 'status', 'evidence') else 1
        for attempt in range(attempts):
            try:
                return self.execute(code, timeout=_cli_timeout('COLAB_REMOTE_TIMEOUT', 300))
            except H3Error as exc:
                if (str(exc) not in ('colab_response_unknown', 'colab_command_failed', 'invalid_colab_response')
                        or attempt == attempts - 1):
                    raise
                time.sleep(min(30, 3 * (attempt + 1)))

    def upload(self, source, target):
        self.cli('upload', '-s', self.session, str(source), target, timeout=_cli_timeout('COLAB_TRANSFER_TIMEOUT', 600))

    def download(self, source, target):
        self.cli('download', '-s', self.session, source, str(target), timeout=_cli_timeout('COLAB_TRANSFER_TIMEOUT', 600))


def existing_login_only():
    import colab_cli.auth as auth
    def deny(*args, **kwargs):
        raise H3Error('existing_login_unavailable_no_interactive_login')
    auth._run_remote_flow = deny


def control_main(action, value):
    existing_login_only()
    from colab_cli.common import state
    if action == 'usage':
        v = state.client.get_consumption_user_info()
        result = {'balance': float(v.paid_compute_units_balance),
                  'rate': float(v.consumption_rate_hourly), 'count': int(v.assignments_count)}
    elif action == 'assignments':
        assignments = state.client.list_assignments()
        result = [{'endpoint': a.endpoint, 'accelerator': str(getattr(a, 'accelerator', ''))}
                  for a in assignments]
    elif action == 'session':
        s = state.store.get(value)
        result = {'endpoint': s.endpoint, 'session': s.name} if s else None
    elif action == 'unassign':
        if not value or len(value) > 300: raise H3Error('invalid_endpoint')
        state.client.unassign(value)
        result = {'requested': True}
    else:
        raise H3Error('invalid_control_action')
    print(MARKER + json.dumps(result))


def validate_batch(batch, *, historical_collect=False):
    jobs = batch.get('jobs', [])
    episode = batch.get('profile') in EPISODE_PROFILES
    for key, cap in [('max_seconds', 28800 if episode else 3600),
                     ('max_ccu', 50 if episode else 15), ('max_rate', 15)]:
        value = batch.get(key)
        if key == 'max_seconds' and value is None and batch.get('profile') == v4.PROFILE:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= cap:
            raise H3Error('invalid_finite_batch')
    if (batch.get('schema_version') != 1 or not 1 <= len(jobs) <= (32 if episode else 5)
            or len(set(jobs)) != len(jobs) or batch.get('gpu') != 'G4'
            or batch.get('max_attempts_per_job') != 1):
        raise H3Error('invalid_finite_batch')
    root = Path(batch['project']).resolve(strict=True)
    if not (root / 'short-drama.json').is_file(): raise H3Error('project_required')
    if not historical_collect and batch.get('implementation_sha256') != implementation_digest():
        raise H3Error('implementation_changed_reprepare')
    if batch.get('setup_sha256') != digest(batch['setup_manifest']):
        raise H3Error('setup_changed_reprepare')
    if not historical_collect:
        setup = read_json(batch['setup_manifest'])
        if setup.get('profile') in ('pika-ref-v1', EPISODE_PROFILE):
            if digest(PIKA_NODES) != setup['custom_nodes_sha256']:
                raise H3Error('custom_nodes_changed_reprepare')
            if digest(ROOT / setup['constraints']['path']) != setup['constraints']['sha256']:
                raise H3Error('constraints_changed_reprepare')
        elif setup.get('profile') == v4.PROFILE:
            if batch.get('profile') != v4.PROFILE:
                raise H3Error('runtime_profile_mismatch')
            measurement = setup['measurement_nodes']
            if digest(ROOT / measurement['path']) != measurement['sha256']:
                raise H3Error('custom_nodes_changed_reprepare')
            if digest(ROOT / setup['constraints']['path']) != setup['constraints']['sha256']:
                raise H3Error('constraints_changed_reprepare')
            if batch.get('execution_release'):
                from drama_factory.releases import verify_release
                verify_release(batch['execution_release'], batch.get('execution_release_sha256'))
            elif setup.get('foundation_lock'):
                foundation = read_json(ROOT / setup['foundation_lock'])
                if any(digest(ROOT / path) != sha for path, sha in foundation['files_sha256'].items()):
                    raise H3Error('v4_foundation_changed_reprepare')
        if batch.get('profile') == v4.PROFILE and setup.get('profile') != v4.PROFILE:
            raise H3Error('v4_runtime_required')
    if batch.get('task_budget') and not historical_collect:
        task_spec(batch)
    return root


def implementation_digest():
    paths = [SELF, REMOTE, Path(media_retention.__file__)]
    if PIKA_NODES.exists(): paths.append(PIKA_NODES)
    paths.extend([ROOT / 'scripts/h3_workflow_v4.py', ROOT / 'scripts/h3_v4_nodes.py',
                  Path(native_voice.__file__)])
    return hashlib.sha256(canonical({p.name: digest(p) for p in paths})).hexdigest()


def task_spec(batch):
    """One immutable task contract, with mutable accounting kept separately."""
    root = Path(batch['project']).resolve(strict=True)
    path = Path(batch['task_budget']).resolve(strict=True)
    if not path.is_relative_to(root / '.short-drama/production/colab'):
        raise H3Error('task_must_belong_to_project')
    task = read_json(path)
    accounting_path = path.with_name('accounting.json')
    if accounting_path.exists() and read_json(accounting_path).get('task_sha256') != digest(path):
        raise H3Error('task_contract_changed_after_start')
    if task.get('schema_version') == 2:
        from drama_factory.runtime import membership
        if path.with_name('stop.json').exists():
            raise H3Error('task_stopped')
        task['batches'] = membership(path)
        if accounting_path.exists():
            accounting = read_json(accounting_path)
            task['max_ccu'] = accounting.get('authorized_max_ccu', task['max_ccu'])
    if (task.get('schema_version') not in (1, 2) or task.get('project') != str(root)
            or type(task.get('max_instances')) is not int or not 1 <= task['max_instances'] <= 2
            or not isinstance(task.get('batches'), dict)
            or str(Path(batch['batch_file']).resolve()) not in task['batches']):
        raise H3Error('invalid_task_contract')
    for key, cap in [('max_ccu', 100), ('max_seconds', 28800)]:
        value = task.get(key)
        if key == 'max_seconds' and value is None and task['schema_version'] == 2:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= cap:
            raise H3Error('invalid_task_limits')
    return path, task


def task_members(batch):
    path, task = task_spec(batch)
    members, jobs, entries, sessions, states, outputs = [], set(), set(), set(), set(), set()
    root = Path(task['project'])
    production = engine()
    for filename, sha in task['batches'].items():
        file = Path(filename).resolve(strict=True)
        if not file.is_relative_to(root / '.short-drama/production/colab') or digest(file) != sha:
            raise H3Error('task_batch_snapshot_changed')
        member = read_json(file)
        if (member.get('project') != str(root) or member.get('task_budget') != str(path)
                or member.get('batch_file') != str(file)
                or member.get('profile') not in EPISODE_PROFILES
                or not 0 < member.get('max_rate', 0) <= 15
                or member['max_ccu'] > task['max_ccu']
                or member['max_seconds'] > task['max_seconds']
                or (member['session'] in sessions and not (task['schema_version'] == 2 and member.get('runtime_from') in task['batches']))
                or member['resource_state'] in states):
            raise H3Error('task_ownership_or_limits_mismatch')
        state_path = Path(member['resource_state']).resolve()
        if state_path.parent != file.parent:
            raise H3Error('task_resource_state_must_belong_to_batch')
        sessions.add(member['session']); states.add(member['resource_state'])
        for job_id in member['jobs']:
            if job_id in jobs: raise H3Error('duplicate_task_job')
            jobs.add(job_id)
            # Stable source entry, rather than job ID alone, prevents racing the same shot.
            job = production._read_job(root, job_id)
            entry = (job['source'], job['source_entry'])
            if entry in entries and task['schema_version'] == 1: raise H3Error('duplicate_task_source_entry')
            if entry in entries and task['schema_version'] == 2:
                from drama_factory.runtime import _terminal_batch
                prior = [m for m in members if any(
                    (production._read_job(root, j)['source'], production._read_job(root, j)['source_entry']) == entry
                    for j in m['jobs'])]
                if any(not _terminal_batch(m, production) for m in prior):
                    raise H3Error('duplicate_active_task_source_entry')
            entries.add(entry)
            parent = job.get('parameters', {}).get('continue_from_job')
            if parent and parent not in member['jobs']:
                raise H3Error('task_continuation_chain_must_stay_in_one_partition')
            for output in job['outputs']:
                if output in outputs: raise H3Error('duplicate_task_output')
                outputs.add(output)
        members.append(member)
    if task['schema_version'] == 1 and sum(m['max_ccu'] for m in members) > task['max_ccu']:
        raise H3Error('task_reservations_exceed_total_budget')
    return path, task, members


def _settled_lease_charge(state):
    """Use verified idle bookends to settle a release or a failed allocation."""
    if (state['state'] != 'released' or state.get('baseline_endpoints') != []
            or any(a.get('endpoint') for a in state.get('previous_allocations', []))):
        return None
    snapshots = (state.get('usage_before', {}), state.get('usage_after', {}))
    if any(item.get('count') != 0 or item.get('rate') != 0
           or type(item.get('balance')) not in (int, float)
           or not math.isfinite(item['balance']) or item['balance'] < 0 for item in snapshots):
        return None
    charge = snapshots[0]['balance'] - snapshots[1]['balance']
    if not state.get('endpoint'):
        if charge == 0 and state.get('failure') in (
                'colab_command_failed', 'colab_allocation_limit_or_capacity', 'colab_allocation_deadline'):
            return 0.0
        return None
    if not state['started_at'] <= state.get('release_requested_at', -1) <= state['finished_at']:
        return None
    return charge if charge > 0 else None


def _settled_wave_charge(states):
    if len(states) == 1:
        return _settled_lease_charge(states[0])
    initial = states[0].get('usage_before', {})
    balance = initial.get('balance')
    if (initial.get('count') != 0 or initial.get('rate') != 0
            or type(balance) not in (int, float) or not math.isfinite(balance) or balance < 0):
        return None
    endpoints = {state.get('endpoint') for state in states}
    if (None in endpoints or '' in endpoints or len(endpoints) != len(states)
            or any(state['state'] != 'released' or state.get('baseline_endpoints') != []
                   or state.get('previous_allocations') or state.get('usage_before') != initial
                   or not state['started_at'] <= state.get('release_requested_at', -1) <= state['finished_at']
                   for state in states)):
        return None
    for index, state in enumerate(sorted(states, key=lambda state: state['finished_at'])):
        usage = state.get('usage_after', {})
        remaining = len(states) - index - 1
        latest, count, rate = usage.get('balance'), usage.get('count'), usage.get('rate')
        if (type(latest) not in (int, float) or not math.isfinite(latest) or not 0 <= latest <= balance
                or type(count) is not int or not 0 <= count <= remaining
                or type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0
                or ((rate == 0) != (count == 0))):
            return None
        balance = latest
    charge = initial['balance'] - balance
    return charge if charge > 0 else None


def task_accounting(batch, *, now=None, reserve=0):
    path, task, members = task_members(batch)
    accounting = read_json(path.with_name('accounting.json'))
    now = time.time() if now is None else now
    estimated, active_rate, active = 0.0, 0.0, 0
    single_lane = task['schema_version'] == 2 and task['max_instances'] == 1
    ccu_only = accounting['deadline'] is None
    if ccu_only and task['schema_version'] != 2:
        raise H3Error('ccu_only_requires_factory_task')
    leases = []
    settled_endpoints = {}
    for member in members:
        file = Path(member['resource_state'])
        if not file.exists(): continue
        state = read_json(file)
        end = state.get('finished_at', now) if state['state'] in ('released', 'transferred') else now
        settlement = accounting.get('late_usage_after', {}).get(str(file))
        settled_state = {**state, 'usage_after': settlement['usage']} if (
            settlement and state['state'] == 'released' and digest(file) == settlement['resource_sha256']) else state
        settled = _settled_lease_charge(settled_state) if single_lane else None
        started = state['started_at']
        if state.get('ccu_only'):
            placement = state.get('paid_started_at')
            if placement is None:
                placement = colab_allocation.possible_assignment_at(file)
            if placement is not None:
                started = max(started, placement)
            elif not state.get('endpoint'):
                started = end
        cost = member['max_rate'] * max(0, end - started) / 3600
        estimated += settled if settled is not None else cost
        if settled is not None and state.get('endpoint'):
            settled_endpoints[state['endpoint']] = state
        leases.append((member.get('wave'), state, end, cost))
        if state['state'] not in ('released', 'transferred'):
            active_rate += member['max_rate']; active += 1
    if single_lane:
        for _, state, end, cost in leases:
            closed = settled_endpoints.get(state.get('endpoint'))
            if (state['state'] == 'transferred' and closed and end <= closed['started_at']
                    and state.get('usage_before') == closed.get('usage_before')):
                # The successor retains the original balance baseline, so its debit includes this interval.
                estimated -= cost
    elif task['schema_version'] == 2:
        for wave in {member.get('wave') for member in members} - {None}:
            group = [lease for lease in leases if lease[0] == wave]
            if len(group) != sum(member.get('wave') == wave for member in members):
                continue
            start = min(state['started_at'] for _, state, _, _ in group)
            finish = max(end for _, _, end, _ in group)
            if any(other != wave and state['started_at'] < finish and end > start
                   for other, state, end, _ in leases):
                continue
            settled = _settled_wave_charge([state for _, state, _, _ in group])
            if settled is not None:
                estimated += settled - sum(cost for _, _, _, cost in group)
    if (active > task['max_instances']
            or (not ccu_only and now + reserve >= accounting['deadline'])
            or estimated + active_rate * reserve / 3600 >= task['max_ccu']):
        raise H3Error('task_budget_or_deadline_exhausted')
    return {'spent_ccu_upper_estimate': estimated, 'deadline': accounting['deadline'],
            'started_at': accounting['started_at'], 'active_instances': active}


@contextlib.contextmanager
def task_allocation_lock(batch):
    path, _ = task_spec(batch)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def prepare_task_runtime(batch, transport, production, *, previous=None):
    """Reserve once under the task lock, before requesting any new GPU."""
    with task_allocation_lock(batch):
        path, task, members = task_members(batch)
        usage = transport.control('usage')
        active = 0
        for member in members:
            file = Path(member['resource_state'])
            if file.exists():
                state = read_json(file)
                if state['state'] not in ('released', 'transferred'):
                    active += 1
        if active >= task['max_instances']: raise H3Error('task_instance_limit')
        accounting_path = path.with_name('accounting.json')
        now = time.time()
        if not accounting_path.exists():
            # Every partition must be registered and frozen before the first allocation.
            for member in members:
                if task['schema_version'] == 2 and member['wave'] != max(m['wave'] for m in members):
                    continue  # Earlier never-dispatched waves are immutable history.
                validate_batch(member)
                for job_id in member['jobs']:
                    job = production._read_job(Path(task['project']), job_id)
                    receipt = production._metadata_read_json(Path(task['project']), ('confirmations',),
                        production._job_key(job_id) + '.json', maximum=production.MAX_RUN_RECORD_BYTES)
                    if (not production._inputs_current(Path(task['project']), job)
                            or production._read_run_history(Path(task['project']), job_id)
                            or receipt.get('fingerprint') != job['fingerprint'] or receipt.get('consumed_at')):
                        raise H3Error('all_task_inputs_must_be_registered_before_allocation')
            seconds = task['max_seconds']
            write_json(accounting_path, {'task_sha256': digest(path),
                                        'started_at': now, 'deadline': None if seconds is None else now + seconds,
                                        'usage_before': usage}, exclusive=True)
        accounting = read_json(accounting_path)
        ccu_only = accounting['deadline'] is None
        if ccu_only:
            if previous and previous.get('endpoint'):
                raise H3Error('released_paid_lease_requires_new_wave')
            # The provider may settle after the release check. Use the next idle
            # bookend for the most recent lease, before sending another allocation.
            for member in reversed(members):
                file = Path(member['resource_state'])
                if not file.exists():
                    continue
                last = read_json(file)
                if (_settled_lease_charge(last) is None
                        and _settled_lease_charge({**last, 'usage_after': usage}) is not None):
                    accounting.setdefault('late_usage_after', {}).setdefault(
                        str(file), {'resource_sha256': digest(file), 'usage': usage, 'observed_at': now})
                    write_json(accounting_path, accounting)
                break
        wave = batch.get('wave')
        isolated_wave = (wave is not None and active == 0
                         and sum(member.get('wave') == wave for member in members) == 1)
        single_lane = (task['schema_version'] == 2 and (task['max_instances'] == 1 or isolated_wave)
                       and (previous is None or previous.get('closeout_reserve_seconds') == 240))
        reserve = 240 if single_lane or ccu_only else 900
        budget = task_accounting(batch, now=now, reserve=reserve)
        remaining = task['max_ccu'] - budget['spent_ccu_upper_estimate']
        if usage['balance'] < remaining:
            raise H3Error('insufficient_balance')
        deadline = None if ccu_only else min(
            now + batch['max_seconds'], now + batch['max_ccu'] / batch['max_rate'] * 3600,
            accounting['deadline']) - 30
        if single_lane and not ccu_only:
            # Hard release runs independently of the notebook. Leave time for provider unassignment.
            deadline = min(deadline, now + remaining / batch['max_rate'] * 3600 - 120)
            if deadline <= now + reserve:
                raise H3Error('task_budget_or_deadline_exhausted')
        lease_ccu = remaining / (1 if isolated_wave else task['max_instances'])
        if ccu_only and lease_ccu * 3600 / batch['max_rate'] <= reserve + 120:
            raise H3Error('task_budget_or_deadline_exhausted')
        state = {'state': 'allocating', 'started_at': now, 'usage_before': usage,
                 'baseline_endpoints': sorted(a['endpoint'] for a in transport.control('assignments')),
                 'deadline': deadline,
                 'remote_directory': '/content/vision-h3-' + batch['batch_id']}
        if single_lane or ccu_only:
            state['closeout_reserve_seconds'] = reserve
        if ccu_only:
            state.update(ccu_only=True, ccu_remaining_at_reservation=lease_ccu, controller_pid=os.getpid())
        if previous:
            # A known released allocation may be replaced, without resetting either budget or deadline.
            state.update(started_at=previous['started_at'], usage_before=previous['usage_before'],
                         deadline=None if ccu_only else min(deadline, previous['deadline']),
                         previous_allocations=[*previous.get('previous_allocations', []),
                             {k: previous[k] for k in ('endpoint', 'finished_at', 'failure') if k in previous}])
        write_json(batch['resource_state'], state, exclusive=previous is None)
        return state


def ensure_budget(batch, start, usage=None, *, now=None, reserve=600):
    now = time.time() if now is None else now
    elapsed = max(0, now - start['started_at'])
    if batch.get('task_budget'):
        deadline = start['deadline']
        if start.get('closeout_reserve_seconds') and deadline is not None and now + reserve >= deadline:
            raise H3Error('task_budget_or_deadline_exhausted')
        task_accounting(batch, now=now, reserve=reserve)
        if start.get('ccu_only'):
            return
        # The provider reports account-wide usage. Charge only this task's leases.
        rate = batch['max_rate']
        cost = rate * elapsed / 3600
    elif start.get('ccu_only'):
        deadline = colab_allocation.paid_deadline(batch['resource_state'], start, batch['max_rate'])
        if deadline is not None and now + reserve >= deadline:
            raise H3Error('budget_or_deadline_exhausted')
        return
    else:
        rate = usage['rate'] or batch['max_rate']  # API updates can lag allocation.
        cost = max(0, start['usage_before']['balance'] - usage['balance'],
                   batch['max_rate'] * elapsed / 3600)
    if (not math.isfinite(rate) or not 0 < rate <= batch['max_rate'] or elapsed + reserve >= batch['max_seconds']
            or cost + batch['max_rate'] * reserve / 3600 >= batch['max_ccu']):
        raise H3Error('budget_or_deadline_exhausted')


def check_runtime_budget(batch, state, transport, *, reserve=600):
    usage = None if batch.get('task_budget') else transport.control('usage')
    ensure_budget(batch, state, usage, reserve=reserve)


def check_native_media(path, parameters, *, master=False):
    data = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))
    videos = [s for s in data['streams'] if s['codec_type'] == 'video']
    audios = [s for s in data['streams'] if s['codec_type'] == 'audio']
    expected_frames = parameters['frame_count'] - (0 if master else parameters['context_frames'])
    if (len(videos) != 1 or not audios
            or (videos[0]['width'], videos[0]['height']) != (parameters['width'], parameters['height'])
            or videos[0]['r_frame_rate'] != '24/1'
            or int(videos[0].get('nb_frames', -1)) != expected_frames):
        raise H3Error('native_media_contract_failed_collect_before_retry')
    return data


def run_adapter(job, batch, transport):
    collected = job.get('collect_provider_job_id')
    resuming = job.get('resume_provider_job_id')
    root = validate_batch(batch, historical_collect=bool(collected))
    if job.get('job_id') not in batch['jobs'] or job.get('adapter') != 'h3-colab':
        raise H3Error('job_outside_batch')
    if collected:
        request_id = str(uuid.UUID(collected))
        cache = Path(batch['resource_state']).parent / 'results'
        receipt = cache / (request_id + '.json')
        if receipt.exists():
            saved = read_json(receipt)
            files = saved.get('files', [{**saved, 'cache_name': request_id + '.mp4'}])
            if saved['job_id'] != job['job_id'] or [v['target'] for v in files] != job['outputs']:
                raise H3Error('cached_output_integrity_mismatch')
            outputs = []
            for i, value in enumerate(files):
                if value.get('retained_path'):
                    if value['retained_path'] != value['target']:
                        raise H3Error('cached_output_integrity_mismatch')
                    clip = media_retention.project_path(root, value['retained_path'])
                else:
                    if Path(value['cache_name']).name != value['cache_name']:
                        raise H3Error('cached_output_integrity_mismatch')
                    clip = cache / value['cache_name']
                if (clip.is_symlink() or clip.stat().st_size != value['bytes']
                        or digest(clip) != value['sha256']):
                    raise H3Error('cached_output_integrity_mismatch')
                out = Path(job['output_root']) / f'clip-{i}.mp4'
                shutil.copyfile(clip, out)
                outputs.append({'target': value['target'], 'source': str(out)})
            return {'outputs': outputs, 'provider_job_id': request_id}
    elif resuming:
        request_id = str(uuid.UUID(resuming))
        stored = engine()._read_job(root, job['job_id'])
        for binding in job['reference_bindings']:
            if digest(root / binding['path']) != stored['inputs'].get(binding['path']):
                raise H3Error('resume_reference_changed')
    else:
        request_id = str(uuid.uuid4())
    run = read_json(batch['resource_state'])
    if run.get('state') != 'ready' and not (collected and run.get('state') == 'paused_recoverable'):
        raise H3Error('batch_not_ready')
    remote = run['remote_directory']
    if not collected:
        if job['parameters'].get('implementation_sha256') != batch['implementation_sha256']:
            raise H3Error('job_implementation_mismatch')
        if job['parameters'].get('setup_sha256') != batch['setup_sha256']:
            raise H3Error('job_setup_mismatch')
        # The official engine's handle path belongs to the live cwd, not snapshot.
        key = hashlib.sha256(job['job_id'].encode()).hexdigest()[:24]
        expected = root / '.short-drama/production/handles' / key / (job['run_id'] + '.json')
        if Path(job['handle_path']) != expected: raise H3Error('invalid_handle_path')
        if resuming:
            if read_json(expected).get('provider_job_id') != request_id:
                raise H3Error('resume_handle_mismatch')
            observed = transport.remote(remote, 'status', {'request_id': request_id})
            if observed['state'] != 'not_submitted':
                raise H3Error('resume_not_proven_unsubmitted')
        else:
            write_json(expected, {'provider_job_id': request_id}, exclusive=True)
        snapshot = Path(job['project_root']).resolve(strict=True)
        filenames = []
        for binding in job['reference_bindings']:
            file = (snapshot / binding['path']).resolve(strict=True)
            if not file.is_relative_to(snapshot) or not file.is_file():
                raise H3Error('invalid_snapshot_reference')
            name = digest(file) + file.suffix.lower()
            transport.upload(file, '/content/ComfyUI/input/' + name)
            filenames.append(name)
        context = None
        parent = job['parameters'].get('continue_from_job')
        if parent:
            if parent not in batch['jobs'][:batch['jobs'].index(job['job_id'])]:
                raise H3Error('context_parent_must_precede_child')
            receipts = [read_json(p) for p in (Path(batch['resource_state']).parent / 'results').glob('*.json')]
            matches = [r for r in receipts if r.get('job_id') == parent and r.get('latent')]
            if len(matches) != 1: raise H3Error('context_parent_not_collected')
            context = matches[0]['latent']
            local_latent = Path(batch['resource_state']).parent / 'results' / context['path']
            if digest(local_latent) != context['sha256']:
                raise H3Error('context_integrity_mismatch')
        recipes = job['parameters'].get('native_voice_references', [])
        if recipes:
            if job['parameters'].get('profile') != v4.PROFILE:
                raise H3Error('native_voice_requires_v4')
            earlier = batch['jobs'][:batch['jobs'].index(job['job_id'])]
            if any(recipe.get('source_job') not in earlier for recipe in recipes):
                raise H3Error('native_voice_source_must_precede_consumer')
            try:
                voice_assets = native_voice.resolve(root, Path(batch['resource_state']).parent / 'results', recipes)
            except ValueError as exc:
                raise H3Error(str(exc)) from exc
            voice_filenames = []
            for asset in voice_assets:
                name = asset['sha256'] + '.wav'
                transport.upload(root / asset['path'], '/content/ComfyUI/input/' + name)
                voice_filenames.append(name)
            context = {**(context or {}), 'voice_filenames': voice_filenames}
            write_json(Path(batch['resource_state']).parent / 'graphs' / (request_id + '-voices.json'), voice_assets)
        graph = build_graph(job, filenames, request_id, context=context)
        write_json(Path(batch['resource_state']).parent / 'graphs' / (request_id + '.json'), graph)
        # Remote receipt is created before POST; repeating submit cannot enqueue twice.
        try:
            transport.remote(remote, 'submit', {'request_id': request_id, 'graph': graph})
        except H3Error as exc:
            if str(exc) not in ('colab_response_unknown', 'colab_command_failed', 'invalid_colab_response'):
                raise
            # A lost submit response is reconciled by the same correlation ID.
            # Never repeat submit/POST to recover it.
    while time.time() < run['deadline'] - 90:
        status = transport.remote(remote, 'status', {'request_id': request_id})
        if status['state'] == 'succeeded':
            if run.get('ccu_only') and run.get('progress_job') == job['job_id']:
                current = read_json(batch['resource_state'])
                if current['state'] in ('ready', 'paused_recoverable'):
                    current['progress_phase'] = 'collecting'
                    write_json(batch['resource_state'], current)
            cache = Path(batch['resource_state']).parent / 'results'
            cache.mkdir(mode=0o700, exist_ok=True)
            files = status.get('files', [status])
            if len(files) != len(job['outputs']): raise H3Error('output_count_mismatch')
            outputs, saved_files = [], []
            for i, (value, target) in enumerate(zip(files, job['outputs'])):
                out = Path(job['output_root']) / f'clip-{i}.mp4'
                transport.download(value['path'], out)
                if digest(out) != value['sha256'] or out.stat().st_size != value['bytes']:
                    raise H3Error('output_integrity_mismatch')
                if job['parameters'].get('profile') in EPISODE_PROFILES:
                    probe = check_native_media(out, job['parameters'], master=i == 1)
                    write_json(cache / (request_id + f'-{i}-probe.json'), probe)
                cached = cache / (request_id + f'-{i}.mp4')
                if not cached.exists(): shutil.copyfile(out, cached)
                saved_files.append({'target': target, 'cache_name': cached.name,
                                    'bytes': value['bytes'], 'sha256': value['sha256']})
                outputs.append({'target': target, 'source': str(out)})
            if status.get('latent'):
                latent = status['latent']
                if not re.fullmatch(r'context-[a-f0-9-]+_00001\.safetensors', latent['path']):
                    raise H3Error('invalid_context_path')
                local_latent = cache / latent['path']
                transport.download('/content/ComfyUI/output/' + latent['path'], local_latent)
                if digest(local_latent) != latent['sha256'] or local_latent.stat().st_size != latent['bytes']:
                    raise H3Error('context_integrity_mismatch')
            if job['parameters'].get('profile') in EPISODE_PROFILES and not status.get('latent'):
                raise H3Error('context_latent_missing_collect_before_retry')
            write_json(cache / (request_id + '.json'),
                       {'job_id': job['job_id'], 'files': saved_files, 'metrics': status.get('metrics'),
                        'latent': status.get('latent')})
            return {'outputs': outputs, 'provider_job_id': request_id}
        if status['state'] in ('failed', 'unknown', 'not_submitted'):
            raise H3Error('provider_' + status['state'])
        check_runtime_budget(batch, run, transport, reserve=90 if run.get('closeout_reserve_seconds') else 300)
        time.sleep(5)
    raise H3Error('pending_collect_before_retry')


def recover_unsubmitted(root, batch, job_id, production, transport):
    """Resume the SAME official attempt only after the live receipt journal proves no POST.

    Generic collect remains read-only. Unknown requests never enter this branch.
    """
    history = production._read_run_history(root, job_id)
    if not history:
        return
    run = history[-1]
    request = run.get('provider_job_id') or production._read_provider_handle(root, job_id, str(run['run_id']))
    if not request:
        raise H3Error('attempt_without_handle_requires_reconciliation')
    state = read_json(batch['resource_state'])
    observed = transport.remote(state['remote_directory'], 'status', {'request_id': request})
    if observed['state'] != 'not_submitted':
        return
    job = production._read_job(root, job_id)
    key = hashlib.sha256(job_id.encode()).hexdigest()[:24]
    handle = root / '.short-drama/production/handles' / key / (str(run['run_id']) + '.json')
    with tempfile.TemporaryDirectory(prefix='h3-resume-') as output:
        payload = {**job, 'resume_provider_job_id': request, 'run_id': str(run['run_id']),
                   'handle_path': str(handle), 'project_root': str(root), 'output_root': output}
        write_json(Path(batch['resource_state']).with_name('safe-resume.json'),
                   {'job_id': job_id, 'request_id': request, 'evidence': observed,
                    'endpoint': state['endpoint'], 'at': time.time()})
        run_adapter(payload, batch, transport)


def release_batch(batch, transport, state):
    state['state'] = 'release_requested'
    state.setdefault('release_requested_at', time.time())
    write_json(batch['resource_state'], state)
    endpoint = state.get('endpoint')
    if not endpoint:
        try:
            endpoint = colab_allocation.recover(transport, batch['resource_state'])
        except (ValueError, H3Error):
            state['state'] = 'release_unverified'
            write_json(batch['resource_state'], state)
            raise H3Error('release_unverified') from None
        if endpoint:
            state['endpoint'] = endpoint
    if not endpoint:
        # Do not stop an unrelated newly-created session on an ambiguous allocation.
        actual = transport.control('assignments')
        endpoints = {a['endpoint'] for a in actual}
        baseline = set(state['baseline_endpoints'])
        safe = endpoints <= baseline if batch.get('task_budget') else endpoints == baseline
        state['state'] = ('released' if safe or state.get('failure') == 'colab_allocation_limit_or_capacity'
                          else 'release_unverified')
    else:
        for _ in range(3):
            actual = transport.control('assignments')
            if endpoint not in {a['endpoint'] for a in actual}:
                state['state'] = 'released'; break
            try: transport.control('unassign', endpoint)
            except H3Error: pass
            if endpoint not in {a['endpoint'] for a in transport.control('assignments')}:
                state['state'] = 'released'; break
            time.sleep(2)
        else: state['state'] = 'release_unverified'
    state['usage_after'] = transport.control('usage')
    state['finished_at'] = time.time()
    write_json(batch['resource_state'], state)
    if state['state'] != 'released': raise H3Error('release_unverified')
    return state


def completed_jobs(root, batch, production):
    completed = []
    for job_id in batch['jobs']:
        history = production._read_run_history(root, job_id)
        if not history or history[-1].get('status') != 'succeeded': continue
        outputs = history[-1].get('outputs', [])
        if outputs and all((root / o['path']).is_file() and digest(root / o['path']) == o['sha256'] for o in outputs):
            completed.append(job_id)
    return completed


def inherit_runtime(batch, transport):
    parent = read_json(batch['runtime_from'])
    if (batch.get('profile') not in EPISODE_PROFILES or parent.get('profile') != batch.get('profile')
            or parent['project'] != batch['project'] or parent['session'] != batch['session']
            or parent['setup_sha256'] != batch['setup_sha256']
            or parent['implementation_sha256'] != batch['implementation_sha256']
            or batch['max_ccu'] > parent['max_ccu']
            or (parent['max_seconds'] is not None and (
                batch['max_seconds'] is None or batch['max_seconds'] > parent['max_seconds']))):
        raise H3Error('runtime_ownership_or_contract_mismatch')
    previous = read_json(parent['resource_state'])
    if previous['state'] != 'paused_recoverable': raise H3Error('only_paused_owned_runtime_can_transfer')
    if previous['endpoint'] not in {a['endpoint'] for a in transport.control('assignments')}:
        raise H3Error('original_runtime_unavailable_no_new_allocation')
    check_runtime_budget(batch, previous, transport)
    state = {**previous, 'state': 'ready', 'completed_jobs': [], 'runtime_from': batch['runtime_from']}
    if batch.get('execution_release'):
        # Transfer closes one accounting interval; the shared task still owns the
        # original balance baseline and deadline. Never double-count the same VM.
        state['started_at'] = time.time()
    state.pop('failure', None)
    write_json(batch['resource_state'], state, exclusive=True)
    previous.update(state='transferred', finished_at=state['started_at'], runtime_owner_batch=batch['batch_file'])
    write_json(parent['resource_state'], previous)
    return state


def run_batch(batch, transport, production, *, resume=False):
    root = validate_batch(batch)
    episode = batch.get('profile') in EPISODE_PROFILES
    state_path = Path(batch['resource_state'])
    if state_path.exists() and not resume: raise H3Error('existing_batch_reconcile_before_run')
    if resume and (not episode or not state_path.exists()): raise H3Error('paused_episode_required')
    previous = read_json(state_path) if resume else None
    if (previous and previous['state'] in ('release_requested', 'release_unverified')
            and not previous.get('endpoint') and state_path.with_name('allocation-request.json').is_file()):
        settled = release_batch(batch, transport, previous)
        return {'state': settled['state'], 'completed_jobs': len(completed_jobs(root, batch, production)),
                'resource_state': str(state_path), 'action': 'allocation_reconciled_no_submission'}
    allocation_retry = bool(previous and batch.get('task_budget') and previous['state'] == 'released')
    allocating = bool(previous and previous['state'] == 'allocating' and not previous.get('endpoint'))
    # Validate every official confirmation and fingerprint before allocating.
    for job_id in batch['jobs']:
        job = production._read_job(root, job_id)
        if batch.get('profile') == v4.PROFILE:
            if job.get('parameters', {}).get('profile') != v4.PROFILE:
                raise H3Error('job_runtime_profile_mismatch')
            parent = job['parameters'].get('continue_from_job')
            if parent:
                parent_job = production._read_job(root, parent)
                if parent_job['parameters'].get('scene_profile') != job['parameters']['scene_profile']:
                    raise H3Error('continuation_scene_profile_conflict')
        history = production._read_run_history(root, job_id)
        if not (resume and history) and not production._inputs_current(root, job):
            raise H3Error('prepared_inputs_changed')
        if history:
            if not resume: raise H3Error('existing_attempt_collect_before_retry')
            if allocation_retry and job_id not in completed_jobs(root, batch, production):
                raise H3Error('released_uncollected_job_requires_reconciliation')
            # Existing attempts are collected or verified below, never resubmitted.
            continue
        if any((root / o).exists() for o in job['outputs']): raise H3Error('output_exists')
        receipt = production._metadata_read_json(root, ('confirmations',), production._job_key(job_id) + '.json', maximum=production.MAX_RUN_RECORD_BYTES)
        if receipt.get('fingerprint') != job['fingerprint'] or receipt.get('consumed_at'):
            raise H3Error('confirmation_not_current')
        filenames = [digest(root / b['path']) + Path(b['path']).suffix.lower()
                     for b in job['reference_bindings']]
        build_graph(job, filenames, 'preflight')
    reused = bool((resume and not (allocation_retry or allocating)) or batch.get('runtime_from'))
    if allocation_retry:
        state = prepare_task_runtime(batch, transport, production, previous=previous)
    elif allocating:
        state = previous
        if state.get('ccu_only'):
            state['controller_pid'] = os.getpid()
            write_json(state_path, state)
    elif resume:
        state = read_json(state_path)
        if state['state'] not in ('paused_recoverable', 'ready'):
            raise H3Error('paused_episode_required')
        if state['endpoint'] not in {a['endpoint'] for a in transport.control('assignments')}:
            raise H3Error('original_runtime_unavailable_no_new_allocation')
        check_runtime_budget(batch, state, transport, reserve=0)
        state['state'] = 'ready'; write_json(state_path, state)
    elif batch.get('runtime_from'):
        state = inherit_runtime(batch, transport)
    elif batch.get('task_budget'):
        state = prepare_task_runtime(batch, transport, production)
    else:
        usage = transport.control('usage')
        if usage['balance'] < batch['max_ccu']: raise H3Error('insufficient_balance')
        assignments = transport.control('assignments')
        if assignments or usage['count'] or usage['rate']:
            raise H3Error('first_validation_requires_idle_account')
        started = time.time()
        ccu_only = batch['max_seconds'] is None
        deadline = None if ccu_only else started + min(
            batch['max_seconds'], batch['max_ccu'] / batch['max_rate'] * 3600) - 30
        state = {'state': 'allocating', 'started_at': started, 'usage_before': usage,
                 'baseline_endpoints': [a['endpoint'] for a in assignments], 'deadline': deadline,
                 'remote_directory': '/content/vision-h3-' + batch['batch_id']}
        if ccu_only:
            if batch['max_ccu'] * 3600 / batch['max_rate'] <= 360:
                raise H3Error('budget_or_deadline_exhausted')
            state.update(ccu_only=True, ccu_remaining_at_reservation=batch['max_ccu'],
                         controller_pid=os.getpid(), closeout_reserve_seconds=240)
        write_json(state_path, state, exclusive=True)
    # Independent local supervisor survives parent death; it never allocates.
    guard = None if resume and not (allocation_retry or (allocating and state.get('ccu_only'))) else subprocess.Popen(
        [sys.executable, str(SELF), 'guard', '--batch', batch['batch_file']],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    error = None
    results = []
    try:
        closeout = state.get('closeout_reserve_seconds', 600)
        submit_reserve = state.get('closeout_reserve_seconds', 900)
        if not reused:
            ccu_only = state.get('ccu_only')
            # The single-instance reservation already owns the slot. Keep stop
            # and reconciliation available while waiting across free windows.
            gate = task_allocation_lock(batch) if batch.get('task_budget') and not ccu_only else contextlib.nullcontext()
            with gate:
                if batch.get('task_budget'):
                    check_runtime_budget(batch, state, transport, reserve=submit_reserve)
                state['endpoint'] = colab_allocation.allocate(
                    transport, state_path, batch['gpu'], math.inf if ccu_only else state['deadline'] - submit_reserve,
                    max_wait=_cli_timeout('COLAB_ALLOC_TIMEOUT', colab_allocation.MAX_WAIT),
                    previous_endpoint=previous.get('endpoint') if allocation_retry else None,
                    check_budget=lambda: check_runtime_budget(batch, state, transport, reserve=submit_reserve))
                commit = task_allocation_lock(batch) if ccu_only and batch.get('task_budget') else contextlib.nullcontext()
                with commit:
                    if read_json(state_path)['state'] != 'allocating':
                        raise H3Error('colab_allocation_stopped')
                    if ccu_only:
                        colab_allocation.claim_paid_lease(state_path, state, batch['max_rate'])
                    state['progress_phase'] = 'bootstrapping'
                    write_json(state_path, state)
                check_runtime_budget(batch, state, transport, reserve=submit_reserve)
            remote = state['remote_directory']
            transport.execute(f'import os,json\nos.makedirs({remote!r},exist_ok=True)\nprint({MARKER!r}+"{{}}")')
            transport.upload(REMOTE, remote + '/colab_h3_remote.py')
            setup = read_json(batch['setup_manifest'])
            if setup.get('profile') in ('pika-ref-v1', EPISODE_PROFILE):
                transport.upload(PIKA_NODES, remote + '/h3_pika_nodes.py')
                constraints = ROOT / setup['constraints']['path']
                if digest(constraints) != setup['constraints']['sha256']:
                    raise H3Error('constraints_digest_mismatch')
                transport.upload(constraints, remote + '/constraints.txt')
            elif setup.get('profile') == v4.PROFILE:
                measurement = setup['measurement_nodes']
                transport.upload(ROOT / measurement['path'], remote + '/h3_v4_nodes.py')
                transport.upload(ROOT / setup['constraints']['path'], remote + '/constraints.txt')
            setup['deadline'] = state['deadline']
            setup['inference_deadline'] = state['deadline'] - closeout
            if batch.get('task_budget'):
                setup['retain_for_collection'] = True
            transport.remote(remote, 'start', setup)
            while time.time() < setup['inference_deadline']:
                boot = transport.remote(remote, 'boot')
                if boot['state'] == 'ready': break
                if boot['state'] == 'failed': raise H3Error('bootstrap_failed')
                check_runtime_budget(batch, state, transport, reserve=closeout)
                print(json.dumps({'phase': boot.get('phase', 'setup'), 'elapsed_seconds': int(time.time() - state['started_at'])}), flush=True)
                time.sleep(10)
            else: raise H3Error('bootstrap_deadline')
            state['state'] = 'ready'; state['hardware'] = boot.get('hardware')
            write_json(state_path, state)
        for job_id in batch['jobs']:
            if job_id in completed_jobs(root, batch, production): continue
            history = production._read_run_history(root, job_id)
            check_runtime_budget(batch, state, transport, reserve=0 if history else submit_reserve)
            state.update(progress_phase='collecting' if history else 'generating', progress_job=job_id)
            write_json(state_path, state)
            print(json.dumps({'phase': 'generating', 'job_id': job_id}), flush=True)
            if history:
                if resume and batch.get('execution_release'):
                    recover_unsubmitted(root, batch, job_id, production, transport)
                result = production.collect_job(root, job_id=job_id, adapter_config=Path(batch['adapter_config']))
            else:
                result = production.run_job(root, job_id=job_id, adapter_config=Path(batch['adapter_config']))
            results.append(result)
            state['completed_jobs'] = completed_jobs(root, batch, production)
            state['last_collected_at'] = time.time()
            state.pop('progress_job', None)
            write_json(state_path, state)
            print(json.dumps({'phase': 'collected', 'job_id': job_id}), flush=True)
    except BaseException as exc:
        error = exc
        state['failure'] = str(exc) if isinstance(exc, (H3Error, FactoryError)) else type(exc).__name__
    finally:
        if error and state.get('state') == 'ready':
            # Collection is read-only provider recovery. Attempt it before
            # releasing a VM that may already contain a completed clip.
            for job_id in batch['jobs']:
                history = production._read_run_history(root, job_id)
                if history and history[-1].get('provider_job_id') and history[-1].get('status') != 'succeeded':
                    try:
                        recovered = production.collect_job(root, job_id=job_id,
                            adapter_config=Path(batch['adapter_config']))
                        state.setdefault('recovered_before_release', []).append(recovered['job_id'])
                    except Exception:
                        pass
        # Capture diagnostic receipts before deciding whether this runtime can end.
        if state.get('endpoint'):
            try:
                evidence = transport.remote(state['remote_directory'], 'evidence')
                write_json(state_path.with_name('remote-evidence.json'), evidence)
            except Exception: pass
        state['completed_jobs'] = completed_jobs(root, batch, production)
        if len(state['completed_jobs']) == len(batch['jobs']): error = None
        preserve = episode and state.get('state') == 'ready' and (
            error is not None or batch.get('keep_runtime', False))
        if preserve:
            try:
                check_runtime_budget(batch, state, transport, reserve=0)
            except H3Error as exc:
                if str(exc) in ('task_stopped', 'budget_or_deadline_exhausted', 'task_budget_or_deadline_exhausted'):
                    preserve = False
        if preserve:
            try:
                preserve = state['endpoint'] in {a['endpoint'] for a in transport.control('assignments')}
            except H3Error:
                pass  # An unavailable readback is not proof the runtime or video is gone.
        if preserve:
            state['state'] = 'paused_recoverable'
            write_json(state_path, state)
        else:
            try: release_batch(batch, transport, state)
            finally:
                if state.get('state') == 'released' and guard is not None: guard.terminate()
    if state['state'] == 'paused_recoverable':
        return {'state': state['state'], 'completed_jobs': len(state['completed_jobs']),
                'resource_state': str(state_path), 'action': 'recover_or_reprepare_on_same_runtime'}
    if error: raise error
    return {'state': state['state'], 'completed_jobs': len(state['completed_jobs']), 'resource_state': str(state_path)}


def guard_batch(batch):
    state = read_json(batch['resource_state'])
    transport = Colab(batch['session'], timeout=15 if state.get('closeout_reserve_seconds') else None)
    heartbeat = budget_heartbeat = 0
    while True:
        state = read_json(batch['resource_state'])
        if state['state'] in ('released', 'transferred'): return
        deadline = colab_allocation.paid_deadline(batch['resource_state'], state, batch['max_rate'])
        if deadline is not None and time.time() >= deadline:
            break
        if colab_allocation.free_wait_orphaned(state):
            break
        if batch.get('task_budget') and time.time() - budget_heartbeat >= 30:
            try:
                check_runtime_budget(batch, state, transport, reserve=0)
            except H3Error as exc:
                if str(exc) in ('budget_or_deadline_exhausted', 'task_budget_or_deadline_exhausted', 'task_stopped'):
                    release_batch(batch, transport, state)
                    return
            budget_heartbeat = time.time()
        # Factory jobs retain remote outputs until their deadline. A kernel ping is both
        # redundant there and can hold the submission/collection lock for minutes.
        if (not batch.get('task_budget') and batch.get('profile') in EPISODE_PROFILES
                and state['state'] in ('ready', 'paused_recoverable')
                and time.time() - heartbeat >= 30):
            try: transport.remote(state['remote_directory'], 'boot')
            except H3Error: pass
            heartbeat = time.time()
        time.sleep(5 if deadline is None else min(5, max(0, deadline - time.time())))
    release_batch(batch, transport, state)


def prepare_batch(project, entries, destination, setup_manifest, *, backends=None):
    root = Path(project).resolve(strict=True)
    destination = Path(destination).absolute()
    batch_id = destination.parent.name
    expected = root / '.short-drama/production/colab' / batch_id / 'batch.json'
    if destination != expected or not re.fullmatch('[a-z0-9-]{1,50}', batch_id):
        raise H3Error('batch_path_must_be_in_production_metadata')
    setup_manifest = Path(setup_manifest).resolve(strict=True)
    pika = read_json(setup_manifest).get('profile') == 'pika-ref-v1'
    backends = backends or (['comfy_kitchen_int8'] * len(entries) if pika else ['pytorch'] * len(entries))
    if (destination.exists() or not 1 <= len(entries) <= (5 if pika else 3)
            or len(backends) != len(entries) or len(set(zip(entries, backends))) != len(entries)
            or any(b not in PIKA_BACKENDS for b in backends)
            or (not pika and any(b != 'pytorch' for b in backends))):
        raise H3Error('new_finite_batch_required')
    production = engine()
    jobs, previews = [], []
    seeds = {entry: 2026110900 + i for i, entry in enumerate(dict.fromkeys(entries))}
    for i, entry in enumerate(entries):
        if not re.fullmatch(r'MOTION-EP\d{3}-\d{3}', entry): raise H3Error('invalid_source_entry')
        episode = entry.split('-')[1]
        source = f'剧集/{episode}/视频提示词.md'
        section = production._markdown_section((root / source).read_text(), entry)
        prompt = production._copyable_prompt(section)
        bindings = production._markdown_reference_bindings(section, field_name='输入参考图')
        for b in bindings: b['role'] = 'reference_image'
        job = {'job_id': entry.replace('MOTION-', '') + '-h3-' + batch_id,
               'modality': 'video', 'adapter': 'h3-colab', 'source': source,
               'source_entry': entry, 'prompt': prompt, 'reference_bindings': bindings,
               'outputs': [f'剧集/{episode}/制作成果/video/{entry}-h3-colab-v1.mp4'],
               'overwrite': False,
               'parameters': {'prompt_language': 'en', 'model': 'minimax-h3-ref2va-pruned-int8-convrot',
                              'ratio': '9:16', 'width': 720, 'height': 1280, 'internal_width': 736,
                              'frame_rate': 24, 'frame_count': 124, 'duration': 5,
                              'steps': 20, 'sampler': 'res_multistep', 'scheduler': 'simple',
                              'seed': 2026110900 + i,
                              'implementation_sha256': implementation_digest(),
                              'setup_sha256': digest(setup_manifest)}}
        if pika:
            backend = backends[i]
            job['job_id'] = entry.replace('MOTION-', '') + '-pika-' + backend.replace('_', '-') + '-' + batch_id
            stem = f'剧集/{episode}/制作成果/video/{entry}-pika-{backend}-{batch_id}'
            job['outputs'] = [stem + '.mp4', stem + '-master.mp4']
            job['parameters'] = {**pika_parameters(backend, seeds[entry]),
                                 'implementation_sha256': implementation_digest(),
                                 'setup_sha256': digest(setup_manifest)}
        filenames = [digest(root / b['path']) + Path(b['path']).suffix.lower() for b in bindings]
        build_graph(job, filenames, 'preflight')
        path = destination.parent / ('input-' + entry + '-' + backends[i] + '.json')
        write_json(path, job, exclusive=True)
        previews.append(production.prepare_job(root, path))
        jobs.append(job['job_id'])
    adapter_config = Path.home().resolve() / '.config/vision-media' / ('drama-adapters-colab-' + batch_id + '.json')
    batch = {'schema_version': 1, 'batch_id': batch_id, 'batch_file': str(destination),
             'project': str(root), 'jobs': jobs, 'gpu': 'G4', 'session': 'h3-' + batch_id,
             'max_seconds': 3600, 'max_ccu': 15, 'max_rate': 15, 'max_attempts_per_job': 1,
             'resource_state': str(destination.with_name('resources.json')),
             'setup_manifest': str(setup_manifest), 'setup_sha256': digest(setup_manifest),
             'implementation_sha256': implementation_digest(), 'adapter_config': str(adapter_config)}
    write_json(adapter_config, {'adapters': {'h3-colab': {
        'command': [sys.executable, str(SELF), 'adapter', '--batch', str(destination)],
        'timeout_seconds': 3300, 'reference_roles': ['reference_image']}}}, exclusive=True)
    write_json(destination, batch, exclusive=True)
    write_json(destination.with_name('previews.json'), previews, exclusive=True)
    preview = destination.with_name('生产预览.md')
    paragraphs = ['# H3 Colab 三镜准确生产预览',
                  '实验预览上限：各段一次推理、G4 一次分配、15 CCU、60 分钟。',
                  '720×1280、24fps；内部736×1280左右各裁8px；124请求帧，实际时长以输出为准。',
                  '复用已有登录；每镜取回，完成立即释放。无音频参考，保留原生声音，不保证跨镜音色。']
    if pika:
        paragraphs = ['# Pika H3 Flex 准确生产预览',
                      f'实验预览上限：{len(jobs)} 条各一次推理，G4 一次分配，15 CCU / 60 分钟。',
                      '固定Pika Ref2VA LoRA 1.0、LCM、六NFE；参考图全部保留，三组对照正文、seed与几何一致。',
                      '母版768×1344；左右各裁6px后缩至720×1280，均保存；24fps、124帧约5.17秒、原生音频。',
                      '逐条取回，全部取回后立即释放；费用按本批实际费率观测。']
    for p in previews:
        paragraphs += ['## ' + p['source_entry'] + ' · ' + p['parameters'].get('attention_backend', 'pytorch'), p['prompt'],
                       '```json\n' + json.dumps({k: p[k] for k in ('reference_bindings', 'parameters', 'outputs')},
                                                ensure_ascii=False, indent=2) + '\n```']
    preview.write_text('\n\n'.join(paragraphs) + '\n')
    return {'state': 'prepared', 'batch': str(destination), 'preview': str(preview), 'jobs': jobs}


def register_batch(batch):
    root = validate_batch(batch)
    production = engine()
    for job_id in batch['jobs']:
        job = production._read_job(root, job_id)
        if not production._inputs_current(root, job) or production._read_run_history(root, job_id):
            raise H3Error('inputs_changed_or_existing_attempt')
        preview = production._preview(job)
        production.confirm_job(root, job_id=job_id, confirmation=preview['confirmation'])
    return {'state': 'registered_under_task_authority', 'jobs': batch['jobs']}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '_cli':
        existing_login_only()
        from colab_cli.cli import app
        app(args=sys.argv[2:]); return
    if len(sys.argv) > 1 and sys.argv[1] == '_control':
        control_main(*sys.argv[2:]); return
    if len(sys.argv) > 1 and sys.argv[1] == '_allocation':
        existing_login_only()
        from colab_cli.common import state
        operation, session, gpu, request_id, deadline, resource = sys.argv[2:]
        result = colab_allocation.request_once(state, operation, session, gpu, request_id, float(deadline), resource)
        print(MARKER + json.dumps(result))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'register', 'run', 'resume', 'adapter', 'guard', 'release', 'preflight'])
    parser.add_argument('--batch', required=True)
    parser.add_argument('--project')
    parser.add_argument('--entry', action='append')
    parser.add_argument('--backend', action='append', choices=PIKA_BACKENDS,
                        help='One backend per --entry, in the same order; Pika comparisons may repeat an entry.')
    parser.add_argument('--setup-manifest')
    parser.add_argument('--offline', action='store_true', help='Local preflight only; no Colab account calls or GPU allocation.')
    args = parser.parse_args()
    if args.command == 'prepare':
        if not args.project or not args.entry or not args.setup_manifest: parser.error('prepare requires project, entries and setup manifest')
        print(json.dumps(prepare_batch(args.project, args.entry, args.batch, args.setup_manifest,
                                       backends=args.backend), ensure_ascii=False))
        return
    batch = read_json(args.batch)
    from drama_factory import RELEASE
    from drama_factory.releases import dispatch_archived, dispatch_legacy
    if batch.get('execution_release') and batch['execution_release'] != RELEASE:
        dispatch_archived(batch['execution_release'], batch.get('execution_release_sha256'),
                          'scripts/colab_h3.py', sys.argv[1:])
    if not batch.get('execution_release'):
        dispatch_legacy(batch, sys.argv[1:])
    if args.command == 'preflight' and args.offline:
        root = validate_batch(batch)
        production = engine()
        for job_id in batch['jobs']:
            job = production._read_job(root, job_id)
            if not production._inputs_current(root, job): raise H3Error('prepared_inputs_changed')
            names = [digest(root / b['path']) + Path(b['path']).suffix.lower() for b in job['reference_bindings']]
            build_graph(job, names, 'preflight')
        print(json.dumps({'state': 'offline_preflight_pass', 'jobs': batch['jobs'], 'gpu_allocated': False}))
        return
    transport = Colab(batch['session'])
    if args.command == 'register': result = register_batch(batch)
    elif args.command in ('run', 'resume'):
        from drama_factory.progress import resource_snapshot, watch
        lock_path = Path(args.batch).with_suffix('.lock')
        reporter = contextlib.nullcontext() if batch.get('task_budget') else watch(
            f"{Path(batch['project']).name} · {batch['batch_id']}", lock_path.parent,
            lambda: [resource_snapshot(batch['resource_state'], len(batch['jobs']))])
        with lock_path.open('a') as f, reporter:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run_batch(batch, transport, engine(), resume=args.command == 'resume')
    elif args.command == 'adapter': result = run_adapter(json.load(sys.stdin.buffer), batch, transport)
    elif args.command == 'guard': guard_batch(batch); return
    elif args.command == 'release': result = release_batch(batch, transport, read_json(batch['resource_state']))
    else:
        validate_batch(batch)
        result = {'state': 'preflight_pass', 'jobs': batch['jobs'], 'gpu_allocated': False,
                  'usage': transport.control('usage')}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        code = str(exc) if isinstance(exc, H3Error) and re.fullmatch('[a-z_]{1,100}', str(exc)) else type(exc).__name__
        print(json.dumps({'error': {'provider': 'h3-colab', 'category': 'server',
                                  'code': code, 'retryable': False}}))
        raise SystemExit(1)
