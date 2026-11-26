"""Native-tool receipts and observed asset versions; no parallel creative text."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import time
import uuid

from .core import (LEDGER, FactoryError, episode_id, field, file_sha, fingerprint,
                   project, project_lock, project_path, read_json, replace_field,
                   revise, sections, write_json)


def _directory(root, episode):
    return project_path(root, LEDGER / 'assets' / episode_id(episode))


def _source(root, episode, entry):
    from image_assets import ImageAssets
    import colab_h3
    return ImageAssets(colab_h3.engine(), root, episode).source(entry)


def image_call(root, episode, entry, *, action='prepare', result_path=None, tool_receipt=None,
               failure=None, failure_kind=None, reason=None):
    root = project(root)
    source = _source(root, episode, entry)
    key = fingerprint({'episode': episode, 'entry': entry, 'source': source['来源 SHA256']})
    directory = _directory(root, episode) / 'host-calls'
    path = directory / (key + '.json')
    with project_lock(root):
        pending = [read_json(p) for p in directory.glob('*.json')]
        if any(r['entry'] == entry and r['id'] != key and r['state'] == 'dispatched_unknown' for r in pending):
            raise FactoryError('earlier_native_tool_result_unknown_reconcile_first')
        if path.exists():
            value = read_json(path)
        else:
            if action != 'prepare':
                raise FactoryError('prepare_native_tool_work_order_first')
            value = {'id': key, 'episode': episode, 'entry': entry, 'source_sha256': source['来源 SHA256'],
                     'state': 'prepared', 'tool': 'image_gen', 'attempt': 1, 'created_at': time.time()}
        if action == 'dispatch':
            if value['state'] != 'prepared':
                raise FactoryError('native_tool_already_dispatched_reconcile_result')
            value.update(state='dispatched_unknown', dispatched_at=time.time())
        elif action == 'result':
            if value['state'] not in ('dispatched_unknown', 'completed') or not tool_receipt:
                raise FactoryError('actual_tool_receipt_required')
            actual = project_path(root, result_path, exists=True)
            result = {'path': str(actual.relative_to(root)), 'sha256': file_sha(actual),
                      'tool_receipt': tool_receipt}
            if value['state'] == 'completed' and value['result'] != result:
                raise FactoryError('native_result_conflict')
            value.update(state='completed', result=result)
        elif action == 'failed':
            if not failure or failure_kind not in ('technical', 'policy') or value['state'] != 'dispatched_unknown':
                raise FactoryError('definite_tool_failure_evidence_required')
            value.update(state='failed', failure=failure, failure_kind=failure_kind)
        elif action == 'retry':
            if value['state'] != 'failed' or value.get('failure_kind') != 'technical' or not reason:
                raise FactoryError('only_definite_technical_failure_can_retry')
            if value.get('attempt', 1) >= 3:
                raise FactoryError('native_tool_attempt_limit_reached')
            value.setdefault('prior_attempts', []).append({k: v for k, v in value.items() if k != 'prior_attempts'})
            value.update(state='prepared', attempt=value.get('attempt', 1) + 1, retry_reason=reason)
            for key in ('failure', 'failure_kind', 'dispatched_at'):
                value.pop(key, None)
        elif action != 'prepare':
            raise FactoryError('unknown_host_action')
        write_json(path, value)
    return {**value, 'prompt': source['提示词'], 'references': source['参考'],
            'next': '当前宿主实际调用工具；结果未知时禁止再生成。实际看图后另记观察并选用。'}


def register(root, episode, *, entry, path, purpose, provenance):
    from image_assets import IMAGE_PURPOSES, ImageAssets
    import colab_h3
    root = project(root)
    if purpose not in IMAGE_PURPOSES or not provenance:
        raise FactoryError('asset_purpose_and_provenance_required')
    source = _source(root, episode, entry)
    ImageAssets(colab_h3.engine(), root, episode).fingerprint(path, purpose, entry)
    actual = project_path(root, path, exists=True)
    if actual.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        raise FactoryError('image_asset_required')
    import json
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
        'stream=width,height', '-of', 'json', str(actual)], timeout=30))
    if not probe.get('streams') or not probe['streams'][0].get('width'):
        raise FactoryError('image_dimensions_unreadable')
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(actual),
                    '-frames:v', '1', '-f', 'null', '-'], check=True, capture_output=True, timeout=30)
    identity = {'entry': entry, 'path': str(actual.relative_to(root)), 'sha256': file_sha(actual),
                'purpose': purpose, 'source_sha256': source['来源 SHA256']}
    version = fingerprint(identity)
    record = {**identity, 'version': version, 'provenance': provenance,
              'width': probe['streams'][0]['width'], 'height': probe['streams'][0]['height'],
              'content_review': 'unreviewed', 'created_at': time.time()}
    target = _directory(root, episode) / 'versions' / (version + '.json')
    with project_lock(root):
        if target.exists():
            return read_json(target)
        write_json(target, record, exclusive=True)
    return record


def _version(root, episode, version):
    if not re.fullmatch(r'[a-f0-9]{64}', version):
        raise FactoryError('invalid_asset_version')
    value = read_json(_directory(root, episode) / 'versions' / (version + '.json'))
    if file_sha(project_path(root, value['path'], exists=True)) != value['sha256']:
        raise FactoryError('asset_file_changed')
    if _source(root, episode, value['entry'])['来源 SHA256'] != value['source_sha256']:
        raise FactoryError('asset_visual_dependency_changed')
    return value


def observe(root, episode, version, *, result, method, notes, observer='executor'):
    root = project(root)
    if result not in ('passed', 'failed') or method not in ('actual_image_view', 'user_image_review') or not notes:
        raise FactoryError('actual_visual_observation_required')
    with project_lock(root):
        asset = _version(root, episode, version)
        record = {'id': uuid.uuid4().hex, 'version': version, 'sha256': asset['sha256'],
                  'source_sha256': asset['source_sha256'], 'result': result,
                  'method': method, 'notes': notes, 'observer': observer, 'at': time.time()}
        write_json(_directory(root, episode) / 'observations' / (record['id'] + '.json'), record, exclusive=True)
    return record


def latest_observation(root, episode, version):
    records = [read_json(p) for p in (_directory(root, episode) / 'observations').glob('*.json')]
    records = [r for r in records if r['version'] == version]
    return max(records, key=lambda r: r['at']) if records else None


def select(root, episode, version, *, slots, authorization):
    import colab_h3
    root = project(root)
    if not authorization or not slots or len(slots) != len(set(slots)) or any(
            not re.fullmatch(r'REF-[A-Z0-9]+(?:-[A-Z0-9]+)*', s) for s in slots):
        raise FactoryError('selection_authority_and_unique_ref_slots_required')
    asset = _version(root, episode, version)
    observation = latest_observation(root, episode, version)
    if not observation or observation['result'] != 'passed':
        raise FactoryError('asset_actual_review_required')
    production = colab_h3.engine()
    changes, expected, consumers, seen = {}, {}, [], set()
    for name in ('分镜.md', '视频提示词.md'):
        relative = f'剧集/{episode}/{name}'
        path = project_path(root, relative)
        if not path.exists():
            continue
        text = path.read_text()
        updated = text
        for entry, section in sections(text).items():
            refs = field(section, '输入参考图')
            if refs is None:
                continue
            bindings = production._markdown_reference_bindings(section, field_name='输入参考图')
            new_refs = refs
            for binding in bindings:
                if binding['slot_id'] in slots:
                    seen.add(binding['slot_id'])
                    old = '· ' + binding['path'] + '《'
                    if old not in new_refs:
                        raise FactoryError('reference_binding_syntax_ambiguous')
                    new_refs = new_refs.replace(old, '· ' + asset['path'] + '《', 1)
                    consumers.append({'document': relative, 'entry': entry, 'slot': binding['slot_id']})
            if new_refs != refs:
                updated = updated.replace(section, replace_field(section, '输入参考图', new_refs), 1)
        if updated != text:
            changes[relative] = updated
            expected[relative] = file_sha(path)
    if seen != set(slots):
        raise FactoryError('reference_slot_not_found:' + ','.join(set(slots) - seen))
    # The recoverable revision includes both reference owners in one commit.
    receipt = revise(root, changes, expected, reason=authorization) if changes else None
    record = {'id': uuid.uuid4().hex, 'version': version, 'observation': observation['id'],
              'authorization_basis': authorization, 'selected_by': 'executor_under_task_authority',
              'consumers': consumers, 'revision': receipt['id'] if receipt else None, 'at': time.time()}
    write_json(_directory(root, episode) / 'selections' / (record['id'] + '.json'), record, exclusive=True)
    return record


def has_current_review(root, episode, path, purpose, image_sha):
    root = project(root)
    for candidate in (_directory(root, episode) / 'selections').glob('*.json'):
        selection = read_json(candidate)
        try:
            asset = _version(root, episode, selection['version'])
        except (ValueError, OSError):
            continue
        if (asset['path'], asset['purpose'], asset['sha256']) == (path, purpose, image_sha):
            observation = latest_observation(root, episode, asset['version'])
            if observation and observation['result'] == 'passed':
                return True
    return False
