"""Native H3 dialogue excerpts from prior, SHA-verified official outputs.

Recipes are frozen before allocation; their audio artifacts are resolved only
after the named source job has been collected. No TTS or source separation.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def parse(section, earlier_jobs):
    match = re.search(r'^- 原生音色参考：(.+)$', section, re.M)
    if not match:
        return []
    result = []
    for value in match[1].split('；'):
        m = re.fullmatch(r'(MOTION-EP\d{3}-\d{3})/([A-Z][A-Z0-9-]{1,30})/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)', value)
        if not m or m[1] not in earlier_jobs:
            raise ValueError('native_voice_source_must_precede_consumer')
        source, speaker, start, duration = earlier_jobs[m[1]], m[2], float(m[3]), float(m[4])
        recipe = {'source_job': source, 'speaker': speaker,
                  'start_seconds': start, 'duration_seconds': duration,
                  'path': f'输入/声音参考/native-first-{speaker.lower()}-{source}.wav'}
        validate(recipe)
        result.append(recipe)
    if len(result) > 3 or len({r['speaker'] for r in result}) != len(result):
        raise ValueError('native_voice_speakers_must_be_unique_and_finite')
    return result


def validate(recipe):
    if set(recipe) != {'source_job', 'speaker', 'start_seconds', 'duration_seconds', 'path'}:
        raise ValueError('invalid_native_voice_recipe')
    job, speaker = recipe['source_job'], recipe['speaker']
    if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', job) or not re.fullmatch(r'[A-Z][A-Z0-9-]{1,30}', speaker):
        raise ValueError('invalid_native_voice_identity')
    start, duration = recipe['start_seconds'], recipe['duration_seconds']
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in (start, duration))
            or start < 0 or not 2 <= duration <= 15):
        raise ValueError('invalid_native_voice_window')
    if recipe['path'] != f'输入/声音参考/native-first-{speaker.lower()}-{job}.wav':
        raise ValueError('invalid_native_voice_path')


def resolve(project, cache, recipes):
    """Return verified WAV metadata; never trust or overwrite an unrelated file."""
    root, cache = Path(project).resolve(strict=True), Path(cache)
    receipts = [json.loads(p.read_text()) for p in cache.glob('*.json')]
    outputs = []
    for recipe in recipes:
        validate(recipe)
        sources = [r for r in receipts if r.get('job_id') == recipe['source_job'] and r.get('files')]
        if len(sources) != 1:
            raise ValueError('native_voice_source_not_collected')
        source_file = sources[0]['files'][0]
        source = (root / source_file['target']).resolve(strict=True)
        if (not source.is_relative_to(root) or not source.is_file()
                or sha(source) != source_file['sha256']):
            raise ValueError('native_voice_source_integrity_mismatch')
        source_duration = float(subprocess.check_output(['ffprobe', '-v', 'error',
            '-show_entries', 'format=duration', '-of', 'csv=p=0', str(source)]))
        if recipe['start_seconds'] + recipe['duration_seconds'] > source_duration + .02:
            raise ValueError('native_voice_window_exceeds_collected_source')
        target = root / recipe['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve(strict=True).is_relative_to(root) or target.is_symlink():
            raise ValueError('invalid_native_voice_path')
        metadata = target.with_suffix('.json')
        basis = {'recipe': recipe, 'source_target': source_file['target'],
                 'source_sha256': source_file['sha256'], 'method': 'native_excerpt_no_tts_no_separation'}
        if target.exists() or metadata.exists():
            if not target.is_file() or not metadata.is_file():
                raise ValueError('native_voice_existing_artifact_requires_reconciliation')
            saved = json.loads(metadata.read_text())
            if any(saved.get(k) != v for k, v in basis.items()) or sha(target) != saved.get('sha256'):
                raise ValueError('native_voice_existing_artifact_integrity_mismatch')
        else:
            with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
                candidate = Path(tmp) / 'sample.wav'
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source),
                    '-ss', str(recipe['start_seconds']), '-t', str(recipe['duration_seconds']),
                    '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '32000', '-c:a', 'pcm_s16le',
                    '-n', str(candidate)], check=True)
                probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error',
                    '-show_streams', '-show_format', '-of', 'json', str(candidate)]))
                actual = float(probe['format']['duration'])
                if abs(actual - recipe['duration_seconds']) > .02:
                    raise ValueError('native_voice_extracted_duration_mismatch')
                saved = {**basis, 'path': recipe['path'], 'sha256': sha(candidate),
                         'bytes': candidate.stat().st_size, 'duration_seconds': actual,
                         'sample_rate': 32000, 'channels': 1,
                         'listening_acceptance': 'unverified; may contain native background sound'}
                # Hard-link publishes exclusively: existing user assets are never overwritten.
                target.hardlink_to(candidate)
                with metadata.open('x') as stream:
                    json.dump(saved, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
        outputs.append(saved)
    return outputs
