#!/usr/bin/env python3
"""Finite Breeze TTS 2 voice-design worker; private Colab files, no network service."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO_URL = 'https://github.com/breezeblue-ai/breeze-tts.git'


def write(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=True))
    os.replace(tmp, path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def worker(directory):
    spec = json.loads((directory / 'runtime.json').read_text())
    sys.path.insert(0, str(directory / 'breeze-tts'))
    import numpy as np
    import soundfile as sf
    import torch
    import transformers
    from huggingface_hub import snapshot_download
    from breeze_infer.runtime import (load_runtime, set_all_seeds,
                                      update_generation_config_for_breeze)
    from breeze_infer.templates import get_template, prepare_inputs, select_template_name
    write(directory / 'status.json', {'state': 'loading_model'})
    ckpt = Path(snapshot_download(spec['model'], revision=spec['revision']))
    tokenizer, model, audio_tokenizer = load_runtime(
        ckpt, device='cuda:0', attn_implementation='eager')
    update_generation_config_for_breeze(model)
    sample_rate = int(model.config.codec_config.sampling_rate)
    write(directory / 'environment.json', {
        'model': spec['model'], 'revision': spec['revision'],
        'repo_commit': spec['repo_commit'], 'torch': torch.__version__,
        'transformers': transformers.__version__,
        'gpu': torch.cuda.get_device_name(), 'attention': 'eager',
        'sample_rate': sample_rate})
    write(directory / 'status.json', {'state': 'ready'})
    completed = 0
    while time.time() < spec['deadline'] and completed < spec['max_jobs']:
        pending = sorted((directory / 'requests').glob('*.json'))
        for request in pending:
            key = request.stem
            receipt = directory / 'results' / (key + '.json')
            started = directory / 'results' / (key + '.started')
            if receipt.exists() or started.exists():
                continue
            started.touch(exist_ok=False)
            payload = json.loads(request.read_text())
            try:
                set_all_seeds(payload['seed'])
                req = {'id': key, 'text': payload['text'], 'speaker': 'S0',
                       'instruction': payload['instruction']}
                inputs = prepare_inputs(
                    tokenizer, audio_tokenizer, model, [req],
                    get_template(select_template_name(req)),
                    guidance_scale=4.0, guidance_scale_ref=None,
                    guidance_scale_ins=None)
                out = model.generate(**inputs, output_audio=True,
                    audio_tokenizer=audio_tokenizer, repetition_penalty=1.1,
                    return_dict_in_generate=True)
                audio = out.audio[0].detach().float().cpu().numpy().reshape(-1)
                duration = len(audio) / sample_rate
                if not np.isfinite(audio).all() or not 2 <= duration <= 15:
                    raise ValueError('reference_duration_or_signal_invalid')
                if float(np.sqrt(np.mean(audio ** 2))) < 0.001:
                    raise ValueError('reference_silent')
                output = directory / 'results' / (key + '.wav')
                sf.write(output, audio, sample_rate, subtype='PCM_16')
                write(receipt, {'state': 'succeeded', 'file': str(output),
                    'sha256': sha(output), 'bytes': output.stat().st_size,
                    'sample_rate': sample_rate, 'channels': 1, 'duration': duration,
                    'peak': float(np.abs(audio).max()),
                    'rms': float(np.sqrt(np.mean(audio ** 2))),
                    'clipped_fraction': float(np.mean(np.abs(audio) >= 0.999)),
                    'text': payload['text'], 'instruction': payload['instruction'],
                    'seed': payload['seed'], 'model': spec['model'],
                    'revision': spec['revision'], 'repo_commit': spec['repo_commit']})
            except Exception as exc:
                write(receipt, {'state': 'failed', 'error': type(exc).__name__,
                                'message': str(exc)[:300]})
            completed += 1
        time.sleep(0.3)
    write(directory / 'status.json', {'state': 'finished', 'completed': completed})


def bootstrap(directory):
    write(directory / 'status.json', {'state': 'installing'})
    spec = json.loads((directory / 'runtime.json').read_text())
    repo = directory / 'breeze-tts'
    subprocess.run(['git', 'init', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', REPO_URL], check=True)
    subprocess.run(['git', '-C', str(repo), 'fetch', '--depth', '1', 'origin',
                    spec['repo_commit']], check=True)
    subprocess.run(['git', '-C', str(repo), 'checkout', 'FETCH_HEAD'], check=True)
    env = directory / 'env'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip',
                    '--system-site-packages', str(env)], check=True)
    python = str(env / 'bin/python')
    subprocess.run([sys.executable, '-m', 'pip', '--python', python,
        'install', '--disable-pip-version-check',
        'transformers==4.57.3', 'qwen-tts==0.1.1', 'soundfile',
        'huggingface-hub'], check=True)
    subprocess.run([python, __file__, 'worker', str(directory)], check=True)


if __name__ == '__main__':
    directory = Path(sys.argv[2])
    try:
        (bootstrap if sys.argv[1] == 'bootstrap' else worker)(directory)
    except Exception as exc:
        write(directory / 'status.json', {'state': 'failed',
                                          'error': type(exc).__name__,
                                          'message': str(exc)[:300]})
        raise