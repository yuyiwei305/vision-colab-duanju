#!/usr/bin/env python3
"""Colab-local H3 worker. No OAuth caches, public tunnels or external API keys."""
from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

COMFY = Path('/content/ComfyUI')
API = 'http://127.0.0.1:8188'


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.h3-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=True); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally: Path(tmp).unlink(missing_ok=True)


def read(path):
    return json.loads(Path(path).read_bytes())


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b''): h.update(b)
    return h.hexdigest()


def http(path, body=None):
    req = urllib.request.Request(API + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024: raise RuntimeError('response_too_large')
    return json.loads(raw)


def download(item):
    out = COMFY / 'models' / item['path']
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size == item['bytes'] and sha(out) == item['sha256']: return
    temp = out.with_suffix('.partial')
    h = hashlib.sha256(); count = 0
    with urllib.request.urlopen(item['url'], timeout=90) as r, temp.open('wb') as f:
        for part in iter(lambda: r.read(8 * 1024 * 1024), b''):
            f.write(part); h.update(part); count += len(part)
            if count > item['bytes']: raise RuntimeError('oversized_model')
        f.flush(); os.fsync(f.fileno())
    if count != item['bytes'] or h.hexdigest() != item['sha256']:
        raise RuntimeError('model_digest_mismatch')
    os.replace(temp, out)


def apply_source_patches(directory, patches):
    """Apply only exact, hash-pinned deployment changes; reject upstream drift."""
    root = Path(directory).resolve()
    for patch in patches:
        path = (root / patch['path']).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or sha(path) != patch['before_sha256']:
            raise RuntimeError('plugin_patch_source_mismatch')
        text = path.read_text()
        for replacement in patch['replacements']:
            if text.count(replacement['before']) != 1:
                raise RuntimeError('plugin_patch_context_mismatch')
            text = text.replace(replacement['before'], replacement['after'])
        if hashlib.sha256(text.encode()).hexdigest() != patch['after_sha256']:
            raise RuntimeError('plugin_patch_result_mismatch')
        path.write_text(text)


def bootstrap(directory):
    d = Path(directory); config = read(d / 'setup.json')
    def phase(name, **extra):
        write(d / 'boot.json', {'state': 'loading', 'phase': name, 'time': time.time(), **extra})
    try:
        phase('hardware')
        gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=name,memory.total,driver_version',
                                       '--format=csv,noheader,nounits'], text=True).strip()
        values = gpu.split(',')
        if len(values) != 3 or int(values[1]) < 85000:
            raise RuntimeError('unexpected_gpu_memory')
        pika = config.get('profile') in ('pika-ref-v1', 'pika-ref-episode-v2')
        v4 = config.get('profile') == 'h3-v4-g4-fullframe-v1'
        if (pika or v4) and ('RTX PRO 6000' not in values[0] or 'Blackwell' not in values[0]):
            raise RuntimeError('unexpected_gpu_type')
        if (pika or v4) and sys.version_info[:2] != (3, 13):
            raise RuntimeError('unexpected_python_version')
        if shutil.disk_usage('/content').free < config.get('minimum_free_disk_gib', 100) * 1024**3:
            raise RuntimeError('insufficient_local_disk')
        phase('source')
        url = 'https://codeload.github.com/Comfy-Org/ComfyUI/zip/' + config['comfy_commit']
        archive = d / 'comfy.zip'
        urllib.request.urlretrieve(url, archive)
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                parts = Path(info.filename).parts
                if not parts or '..' in parts or Path(info.filename).is_absolute():
                    raise RuntimeError('unsafe_archive')
            z.extractall(d / 'source')
        source = d / 'source' / ('ComfyUI-' + config['comfy_commit'])
        if COMFY.exists(): raise RuntimeError('unexpected_existing_comfy_directory')
        shutil.move(source, COMFY)
        (COMFY / 'input').mkdir(exist_ok=True)
        (COMFY / 'output').mkdir(exist_ok=True)
        for package in config.get('custom_archives', []):
            archive_path = d / (package['name'] + '.zip')
            urllib.request.urlretrieve(package['url'], archive_path)
            if sha(archive_path) != package['sha256']:
                raise RuntimeError('plugin_digest_mismatch')
            with zipfile.ZipFile(archive_path) as z:
                for info in z.infolist():
                    if '..' in Path(info.filename).parts or Path(info.filename).is_absolute():
                        raise RuntimeError('unsafe_plugin_archive')
                z.extractall(d / 'plugins')
            shutil.move(d / 'plugins' / package['directory'], COMFY / 'custom_nodes' / package['name'])
            apply_source_patches(COMFY / 'custom_nodes' / package['name'], package.get('patches', []))
        phase('models_and_dependencies')
        constraints = []
        if pika or v4:
            if sha(d / 'constraints.txt') != config['constraints']['sha256']:
                raise RuntimeError('constraints_digest_mismatch')
            constraints = ['-c', str(d / 'constraints.txt')]
        if pika:
            if sha(d / 'h3_pika_nodes.py') != config['custom_nodes_sha256']:
                raise RuntimeError('custom_node_digest_mismatch')
            shutil.copyfile(d / 'h3_pika_nodes.py', COMFY / 'custom_nodes/h3_pika_nodes.py')
        if v4:
            if sha(d / 'h3_v4_nodes.py') != config['measurement_nodes']['sha256']:
                raise RuntimeError('custom_node_digest_mismatch')
            shutil.copyfile(d / 'h3_v4_nodes.py', COMFY / 'custom_nodes/h3_v4_nodes.py')
        # Concurrent downloads overlap environment setup; never run inference here.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            downloads = [pool.submit(download, item) for item in config['models']]
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                            'torch==2.10.0', 'torchvision==0.25.0', 'torchaudio==2.10.0',
                            '--index-url', 'https://download.pytorch.org/whl/cu130', *constraints], check=True)
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                            '-r', str(COMFY / 'requirements.txt'), *constraints], check=True)
            if config.get('extra_dependencies'):
                subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                                *config['extra_dependencies'], *constraints], check=True)
            if pika:
                wheel = config['sage3_wheel']; wheel_path = d / wheel['name']
                urllib.request.urlretrieve(wheel['url'], wheel_path)
                if sha(wheel_path) != wheel['sha256']:
                    raise RuntimeError('sage3_wheel_digest_mismatch')
                subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', str(wheel_path)], check=True)
            for future in downloads: future.result()
        freeze = subprocess.check_output([sys.executable, '-m', 'pip', 'freeze'], text=True)
        (d / 'requirements-resolved.txt').write_text(freeze)
        phase('starting_comfy')
        with (d / 'comfy.log').open('wb') as log:
            environment = dict(os.environ, H3_METRICS_DIR=str(d / 'metrics'))
            subprocess.Popen([sys.executable, 'main.py', '--listen', '127.0.0.1', '--port', '8188',
                              '--highvram', '--disable-mmap', '--disable-auto-launch'],
                             cwd=COMFY, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True, env=environment)
        while time.time() < config['inference_deadline']:
            try:
                info = http('/object_info')
                required = set(config['required_nodes'])
                if not required.issubset(info): raise RuntimeError('missing_required_nodes')
                write(d / 'object-info.json', {n: info[n] for n in sorted(required)})
                if pika:
                    kernel = http('/h3/pika-preflight')
                    write(d / 'kernel-preflight.json', kernel)
                    if kernel.get('state') != 'ready': raise RuntimeError('attention_preflight_failed')
                elif v4:
                    kernel = http(config['measurement_nodes']['preflight_route'])
                    write(d / 'kernel-preflight.json', kernel)
                    if kernel.get('state') != 'ready': raise RuntimeError('attention_preflight_failed')
                write(d / 'boot.json', {'state': 'ready', 'phase': 'ready', 'hardware': gpu,
                      'models': [{'path': m['path'], 'sha256': m['sha256']} for m in config['models']],
                      'comfy_commit': config['comfy_commit'], 'python': sys.version,
                      'time': time.time()})
                return
            except (urllib.error.URLError, TimeoutError): time.sleep(3)
        raise RuntimeError('bootstrap_deadline')
    except Exception as exc:
        write(d / 'boot.json', {'state': 'failed', 'phase': 'failed',
                               'error_type': type(exc).__name__, 'time': time.time()})
        raise


def guard(directory):
    d = Path(directory); config = read(d / 'setup.json')
    while time.time() < config['deadline']:
        time.sleep(min(3, max(0, config['deadline'] - time.time())))
        # A managed task keeps completed and in-flight results recoverable
        # during a disconnect, up to its original hard deadline.
        if not config.get('retain_for_collection') and time.time() - (d / 'heartbeat').stat().st_mtime > 300:
            break
    write(d / 'guard.json', {'state': 'release_requested', 'at': time.time()})
    address = os.environ.get('TBE_RUNTIME_ADDR', '')
    if not address or not re.fullmatch(r'[A-Za-z0-9.:-]+', address):
        write(d / 'guard.json', {'state': 'release_unverified', 'reason': 'runtime_manager_unavailable'})
        return
    # Same runtime-management request used by google.colab.runtime.unassign().
    for _ in range(3):
        try:
            req = urllib.request.Request('http://' + address + '/unassign', data=b'', method='POST')
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200: return
        except Exception: time.sleep(2)
    write(d / 'guard.json', {'state': 'release_unverified', 'reason': 'runtime_manager_request_failed'})


def find_output(entry, request_id, node_id, prefix):
    info = entry.get('outputs', {}).get(node_id, {})
    candidates = []
    for items in info.values():
        if not isinstance(items, list): continue
        for obj in items:
            if isinstance(obj, dict) and str(obj.get('filename', '')).endswith('.mp4'):
                p = (COMFY / 'output' / obj.get('subfolder', '') / obj['filename']).resolve()
                if p.is_relative_to((COMFY / 'output').resolve()) and p.is_file(): candidates.append(p)
    if not candidates:
        candidates = list((COMFY / 'output').glob(prefix + request_id + '*.mp4'))
    if len(candidates) != 1: raise RuntimeError('ambiguous_or_missing_output')
    p = candidates[0]
    return {'state': 'succeeded', 'path': str(p), 'bytes': p.stat().st_size, 'sha256': sha(p)}


def find_result(entry, request_id):
    delivery = find_output(entry, request_id, '17', 'h3-')
    files = [delivery]
    if '19' in entry.get('outputs', {}):
        files.append(find_output(entry, request_id, '19', 'master-'))
    result = {**delivery, 'files': files}
    latent = COMFY / 'output' / ('context-' + request_id + '_00001.safetensors')
    if latent.is_file():
        result['latent'] = {'path': latent.name, 'sha256': sha(latent), 'bytes': latent.stat().st_size}
    return result


def submit(directory, data):
    # A second controller must not pass the receipt check before the first POST.
    d = Path(directory)
    with (d / 'submit.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _submit_locked(directory, data)


def _submit_locked(directory, data):
    d = Path(directory); config = read(d / 'setup.json')
    if time.time() >= config['inference_deadline']: raise RuntimeError('submission_deadline')
    request_id = str(uuid.UUID(data['request_id']))
    receipt = d / 'receipts' / (request_id + '.json')
    if receipt.exists(): return read(receipt)  # Never repeat POST, even if unknown.
    if http('/queue').get('queue_running') or http('/queue').get('queue_pending'):
        raise RuntimeError('backend_not_idle')
    allowed = set(config['required_nodes'])
    if any(n.get('class_type') not in allowed for n in data['graph'].values()):
        raise RuntimeError('unapproved_graph_node')
    write(d / 'graphs' / (request_id + '.json'), data['graph'])
    value = {'state': 'submitting', 'request_id': request_id, 'submitted_at': time.time(),
             'graph_sha256': hashlib.sha256(json.dumps(data['graph'], sort_keys=True).encode()).hexdigest()}
    write(receipt, value)
    try:
        result = http('/prompt', {'prompt': data['graph'], 'client_id': request_id,
                                 'extra_data': {'vision_h3_request_id': request_id}})
        if result.get('node_errors') or not result.get('prompt_id'):
            value['state'] = 'failed'
            value['error'] = 'node_validation_failed'
        else:
            value.update(state='running', prompt_id=result['prompt_id'])
    except Exception:
        value['state'] = 'unknown'
    write(receipt, value)
    return value


def status(directory, data):
    d = Path(directory); request_id = str(uuid.UUID(data['request_id']))
    receipt = d / 'receipts' / (request_id + '.json')
    if not receipt.exists(): return {'state': 'not_submitted'}
    value = read(receipt)
    if value['state'] in ('failed', 'succeeded'): return value
    pid = value.get('prompt_id')
    if not pid:
        # Reconcile a lost POST response against server-side client correlation.
        history = http('/history?max_items=200')
        queue = http('/queue')
        records = [v.get('prompt', []) for v in history.values()] + queue.get('queue_running', []) + queue.get('queue_pending', [])
        found = [r[1] for r in records if len(r) > 3 and isinstance(r[3], dict)
                 and (r[3].get('vision_h3_request_id') == request_id or r[3].get('client_id') == request_id)]
        if len(set(found)) != 1: return {'state': 'unknown'}
        pid = value['prompt_id'] = found[0]
        value['state'] = 'running'; write(receipt, value)
    entries = http('/history/' + pid)
    if pid not in entries: return {'state': 'running', 'prompt_id': pid}
    entry = entries[pid]
    if entry.get('status', {}).get('status_str') == 'error':
        value.update(state='failed', error='comfy_execution_error')
    elif entry.get('status', {}).get('completed'):
        value.update(find_result(entry, request_id))
        metric = d / 'metrics' / (request_id + '.json')
        if metric.exists(): value['metrics'] = read(metric)
        value['history_status'] = entry.get('status', {})
        value['completed_at'] = time.time()
    write(receipt, value)
    return value


def dispatch(directory, action, data):
    d = Path(directory)
    if not re.fullmatch('/content/vision-h3-[a-zA-Z0-9-]+', str(d)):
        raise RuntimeError('invalid_batch_directory')
    (d / 'heartbeat').touch()
    if action == 'start':
        if (d / 'setup.json').exists(): raise RuntimeError('bootstrap_already_started')
        if not os.environ.get('TBE_RUNTIME_ADDR'): raise RuntimeError('remote_unassign_unavailable')
        write(d / 'setup.json', data)
        write(d / 'boot.json', {'state': 'loading', 'phase': 'launching'})
        with (d / 'guard.log').open('wb') as log:
            subprocess.Popen([sys.executable, __file__, 'guard', str(d)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        with (d / 'bootstrap.log').open('wb') as log:
            subprocess.Popen([sys.executable, __file__, 'bootstrap', str(d)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        return {'state': 'started', 'remote_guard': True}
    if action == 'boot': return read(d / 'boot.json')
    if action == 'submit': return submit(d, data)
    if action == 'status': return status(d, data)
    if action == 'evidence':
        result = {'receipts': [read(p) for p in (d / 'receipts').glob('*.json')],
                  'metrics': {p.stem: read(p) for p in (d / 'metrics').glob('*.json')},
                  'graphs': {p.stem: read(p) for p in (d / 'graphs').glob('*.json')}}
        for name in ['boot.json', 'guard.json', 'object-info.json', 'kernel-preflight.json']:
            if (d / name).exists(): result[name] = read(d / name)
        for name in ['requirements-resolved.txt', 'bootstrap.log', 'comfy.log']:
            if (d / name).exists(): result[name] = (d / name).read_text(errors='replace')[-18000:]
        return result
    raise RuntimeError('unknown_action')


if __name__ == '__main__':
    {'bootstrap': bootstrap, 'guard': guard}[sys.argv[1]](sys.argv[2])
