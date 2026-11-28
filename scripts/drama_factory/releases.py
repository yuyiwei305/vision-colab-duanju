"""Separate inference identity from orchestration releases and preserve old runners."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from . import RELEASE
from .core import ROOT, FactoryError, file_sha, read_json, write_json

H3_RUNTIME = 'docs/h3/workflow-v4-g4-runtime.json'
INFERENCE = ('scripts/h3_workflow_v4.py', 'scripts/h3_v4_nodes.py', 'scripts/h3_pika_nodes.py',
             H3_RUNTIME, 'docs/h3/pika-ref-v1-constraints.txt')
PREVIOUS = 'factory-v22'
ARCHIVE_COMPAT = 'scripts/drama_factory/archive_compat/sitecustomize.py'
CONTROLLERS = ('scripts/colab_h3.py', 'scripts/colab_h3_remote.py', 'scripts/h3_episode.py',
               'scripts/h3_production.py', 'scripts/h3_continuity.py', 'scripts/h3_shot_contract.py',
               'scripts/h3_native_voice.py', 'scripts/h3_media_retention.py',
               'scripts/image_assets.py', 'scripts/drama', 'scripts/drama-edit-safe.py',
               'scripts/drama-group-production.py', 'scripts/relay_vsr_comfy_colab.py',
               'scripts/relay_vsr_comfy_remote.py', 'scripts/relay_vsr_colab_remote.py',
               'scripts/relay_vsr_colab.py', 'scripts/breeze_tts_colab.py',
               'scripts/breeze_tts_colab_remote.py',
               'scripts/recavsr_remote.py', 'docs/finishing/recavsr-runtime.json',
               'ref/drama-skills/skills/short-drama/scripts/creator_markdown_check.py',
               'ref/drama-skills/skills/short-drama/scripts/creator_views.py',
               'ref/drama-skills/skills/short-drama-edit/scripts/edit_tool.py',
               'ref/drama-skills/skills/short-drama-produce/scripts/production_tool.py')

ARCHIVED_RELEASES = tuple(f'factory-v{number}' for number in range(1, 23))
ARCHIVE_DIRS = {'factory-v1': 'factory-v1-20261113', 'factory-v11': 'factory-v11-locked-production'}


def release_path(name):
    if name != RELEASE and name not in ARCHIVED_RELEASES:
        raise FactoryError('unsupported_execution_release')
    return ROOT / 'docs/factory/releases' / (name + '.json')


def publish(*, authorization, validation):
    if not authorization or not validation:
        raise FactoryError('release_authorization_and_validation_required')
    previous = verify_release(PREVIOUS)['files_sha256']
    changed = {path for path in INFERENCE if file_sha(ROOT / path) != previous[path]}
    if changed - {H3_RUNTIME}:
        raise FactoryError('inference_changed_outside_orchestration_scope')
    if H3_RUNTIME in changed:
        original = read_json(archived_root(PREVIOUS) / H3_RUNTIME)
        current = read_json(ROOT / H3_RUNTIME)
        # The two authorization notes describe historical work. No runner reads them.
        for runtime in (original, current):
            for key in ('authority', 'gpu_run_authority'):
                runtime.pop(key, None)
        if original != current:
            raise FactoryError('h3_runtime_changed_outside_documentation_scope')
    files = [*CONTROLLERS, *INFERENCE, ARCHIVE_COMPAT, *[str(p.relative_to(ROOT)) for p in
             sorted((ROOT / 'scripts/drama_factory').glob('*.py'))]]
    files += ['scripts/drama-factory.py'] if (ROOT / 'scripts/drama-factory.py').exists() else []
    manifest = {'schema_version': 1, 'release': RELEASE,
                'inference_profile': 'h3-v4-g4-fullframe-v1', 'inference_changed': False,
                'authorization': authorization, 'validation': validation,
                'files_sha256': {path: file_sha(ROOT / path) for path in files}}
    write_json(release_path(RELEASE), manifest, exclusive=True)
    return manifest


def verify_release(name, expected_sha=None):
    if expected_sha is not None and file_sha(release_path(name)) != expected_sha:
        raise FactoryError('execution_release_identity_changed_use_original_release')
    manifest = read_json(release_path(name))
    root = archived_root(name) if name != RELEASE else ROOT
    if any(file_sha(root / path) != sha for path, sha in manifest['files_sha256'].items()):
        raise FactoryError('execution_release_changed_republish_required')
    return manifest


def archived_root(name):
    if name not in ARCHIVED_RELEASES:
        raise FactoryError('unsupported_archived_release')
    root = ROOT / '.local/factory-releases' / ARCHIVE_DIRS.get(name, name)
    archived = root / 'docs/factory/releases' / (name + '.json')
    if not archived.is_file() or file_sha(archived) != file_sha(release_path(name)):
        raise FactoryError('archived_execution_release_required')
    return root


def _exec_archived(root, relative, argv):
    runner = root / relative
    command = [sys.executable, '-B', str(runner), *argv]
    archived_runtime = root / H3_RUNTIME
    live_runtime = ROOT / H3_RUNTIME
    if archived_runtime.is_file() and file_sha(archived_runtime) != file_sha(live_runtime):
        env = os.environ.copy()
        compat_dir = str((ROOT / ARCHIVE_COMPAT).parent)
        paths = [compat_dir, env.get('PYTHONPATH')]
        env['PYTHONPATH'] = os.pathsep.join(path for path in paths if path)
        env['VISION_MEDIA_ARCHIVED_H3_RUNTIME'] = str(archived_runtime)
        env['VISION_MEDIA_LIVE_H3_RUNTIME'] = str(live_runtime)
        os.execve(sys.executable, command, env)
    else:
        os.execv(sys.executable, command)


def dispatch_archived(name, expected_sha, relative, argv):
    verify_release(name, expected_sha)
    root = archived_root(name)
    # The v1 release predates locking the adapter and its engine together.
    lock = read_json(root / 'drama-skills.lock.json')
    key = 'skills/short-drama-produce/scripts/production_tool.py'
    if file_sha(root / 'ref/drama-skills' / key) != lock['files_sha256'][key]:
        raise FactoryError('archived_execution_engine_changed')
    _exec_archived(root, relative, argv)


def legacy_runner(batch):
    """Return a verified shared runner; never change a historical batch fingerprint."""
    import colab_h3 as h
    if batch.get('implementation_sha256') == h.implementation_digest():
        return None
    catalog_path = ROOT / '.local/factory-releases/catalog.json'
    if not catalog_path.exists():
        return None
    item = read_json(catalog_path).get(batch.get('implementation_sha256'))
    if not item:
        return None
    directory = Path(item['root'])
    manifest_path = directory / 'baseline.json'
    if file_sha(manifest_path) != item['manifest_sha256']:
        raise FactoryError('legacy_release_manifest_changed')
    manifest = read_json(manifest_path)
    if any(file_sha(directory / path) != sha for path, sha in manifest['files'].items()):
        raise FactoryError('legacy_release_files_changed')
    return directory / 'scripts/colab_h3.py'


def dispatch_legacy(batch, argv):
    runner = legacy_runner(batch)
    if runner:
        _exec_archived(runner.parents[1], 'scripts/colab_h3.py', argv)
