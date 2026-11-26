"""Project-contained storage, recoverable document revisions and episode specs."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
LEDGER = Path('.short-drama/production')
DOCUMENTS = ('剧本.md', '视觉设定.md', '分镜.md', '图片提示词.md', '视频提示词.md', '剪辑单.md')


class FactoryError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def atomic_bytes(path, data, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.factory-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temp, path)
        else:
            os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path, value, *, exclusive=False):
    atomic_bytes(path, canonical(value) + b'\n', exclusive=exclusive)


def project_path(root, relative, *, exists=False):
    root = Path(root).resolve(strict=True)
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise FactoryError('project_relative_path_required')
    path = root / relative
    if not path.resolve(strict=exists).is_relative_to(root) or path.is_symlink():
        raise FactoryError('path_outside_project_or_symlink')
    return path


def project(root):
    root = Path(root).resolve(strict=True)
    if not project_path(root, 'short-drama.json', exists=True).is_file():
        raise FactoryError('project_required')
    return root


def episode_id(value):
    if not re.fullmatch(r'EP\d{3}', value):
        raise FactoryError('invalid_episode')
    return value


@contextlib.contextmanager
def project_lock(root):
    path = project_path(root, LEDGER / 'factory.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def document_hashes(root, episode):
    directory = Path('剧集') / episode_id(episode)
    return {str(directory / name): file_sha(project_path(root, directory / name))
            for name in DOCUMENTS if project_path(root, directory / name).is_file()}


def sections(text):
    """Return stable heading IDs without interpreting creative prose."""
    matches = list(re.finditer(r'^## `?([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)`? ·[^\n]*', text, re.M))
    result = {}
    for index, match in enumerate(matches):
        entry = match[1]
        if entry in result:
            raise FactoryError('duplicate_entry:' + entry)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[entry] = text[match.start():end]
    return result


def field(section, name, *, default=None):
    values = re.findall(r'^- (?:\*\*)?' + re.escape(name) + r'(?:\*\*)?：([^\n]*)$', section, re.M)
    if len(values) > 1:
        raise FactoryError('duplicate_field:' + name)
    return values[0].strip() if values else default


def replace_field(section, name, value):
    pattern = r'^- (?:\*\*)?' + re.escape(name) + r'(?:\*\*)?：[^\n]*$'
    if len(re.findall(pattern, section, re.M)) != 1:
        raise FactoryError('unique_existing_field_required:' + name)
    return re.sub(pattern, lambda _: '- ' + name + '：' + value, section, flags=re.M)


def _revision_path(root, name):
    if not re.fullmatch(r'[a-f0-9]{32}', name):
        raise FactoryError('invalid_revision_id')
    return project_path(root, LEDGER / 'revisions' / name)


def _recover(root, directory, *, rollback=False):
    manifest = read_json(directory / 'revision.json')
    if manifest['state'] != 'committing':
        return manifest
    protect_active_inputs(root, [item['path'] for item in manifest['files']])
    # Never overwrite a third revision, including a concurrent external editor.
    for item in manifest['files']:
        path = project_path(root, item['path'])
        actual = file_sha(path) if path.exists() else None
        if actual not in (item['before'], item['after']):
            raise FactoryError('revision_recovery_conflict:' + item['path'])
    for index, item in enumerate(manifest['files']):
        path = project_path(root, item['path'])
        side = 'before' if rollback else 'after'
        if item[side] is None:
            if path.exists():
                path.unlink()
        else:
            raw = (directory / f'{index}-{side}').read_bytes()
            if fingerprint(raw) != item[side]:
                raise FactoryError('revision_snapshot_corrupt')
            atomic_bytes(path, raw)
    manifest['state'] = 'rolled_back' if rollback else 'committed'
    manifest['finished_at'] = time.time()
    write_json(directory / 'revision.json', manifest)
    return manifest


def recover_revisions(root, *, revision=None, rollback=False):
    root = project(root)
    with project_lock(root):
        directories = [_revision_path(root, revision)] if revision else sorted(
            project_path(root, LEDGER / 'revisions').glob('*'))
        return [_recover(root, directory, rollback=rollback) for directory in directories
                if (directory / 'revision.json').is_file()]


def revise(root, changes, expected, *, reason, validate=None):
    """CAS multi-document commit. On interruption the manifest completes/rolls back."""
    root = project(root)
    if not reason or not changes or set(expected) != set(changes):
        raise FactoryError('reason_and_exact_expected_revisions_required')
    with project_lock(root):
        protect_active_inputs(root, changes)
        for directory in project_path(root, LEDGER / 'revisions').glob('*'):
            if (directory / 'revision.json').is_file():
                _recover(root, directory)
        for name, raw in changes.items():
            if name != 'short-drama.json' and not re.fullmatch(
                    r'剧集/EP\d{3}/(?:' + '|'.join(re.escape(d) for d in DOCUMENTS) + r')', name):
                raise FactoryError('official_document_path_required:' + name)
            path = project_path(root, name)
            if (file_sha(path) if path.exists() else None) != expected[name]:
                raise FactoryError('source_revision_changed:' + name)
            if not isinstance(raw, str) or '\x00' in raw:
                raise FactoryError('utf8_document_required')
            if name.endswith('.md'):
                sections(raw)
            else:
                json.loads(raw)
        if validate:
            validate(changes)
        rid = uuid.uuid4().hex
        directory = _revision_path(root, rid)
        directory.mkdir(parents=True)
        files = []
        for index, (name, raw) in enumerate(changes.items()):
            path = project_path(root, name)
            if path.exists():
                atomic_bytes(directory / f'{index}-before', path.read_bytes(), exclusive=True)
            atomic_bytes(directory / f'{index}-after', raw.encode(), exclusive=True)
            files.append({'path': name, 'before': expected[name], 'after': fingerprint(raw.encode())})
        write_json(directory / 'revision.json', {'id': rid, 'state': 'committing',
                   'reason': reason, 'created_at': time.time(), 'files': files}, exclusive=True)
        return _recover(root, directory)


def protect_active_inputs(root, changed_paths):
    """Keep active production's formal source documents immutable until release."""
    changed_paths = set(changed_paths)
    ledger = project_path(root, LEDGER)
    jobs = None
    for path in (ledger / 'colab').glob('*/batch.json'):
        batch = read_json(path)
        state_path = batch.get('resource_state') or batch.get('state_file')
        if not state_path or not Path(state_path).is_file():
            continue
        state = read_json(state_path)
        if state.get('state') in ('released', 'transferred'):
            continue
        if 'short-drama.json' in changed_paths:
            raise FactoryError('active_production_config_frozen_until_release')
        if jobs is None:
            jobs = {j['job_id']: j for j in (read_json(p) for p in (ledger / 'jobs').glob('*.json'))}
        for job_id in batch.get('jobs', [batch.get('job_id')]):
            source = jobs.get(job_id, {}).get('source', '')
            parent = str(Path(source).parent)
            if any(str(Path(name).parent) == parent for name in changed_paths):
                raise FactoryError('active_production_documents_frozen_until_release:' + parent)


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def validate_spec(spec):
    if set(spec) != {'schema_version', 'episode', 'production', 'delivery'} or spec['schema_version'] != 1:
        raise FactoryError('invalid_episode_spec')
    episode_id(spec['episode'])
    production, delivery = spec['production'], spec['delivery']
    if (set(production) != {'profile', 'native_canvas', 'fps'}
            or production['profile'] != 'h3-v4-g4-fullframe-v1'
            or production['native_canvas'] not in ([1344, 768], [768, 1344])
            or type(production['fps']) is not int or production['fps'] != 24):
        raise FactoryError('unsupported_native_spec')
    if (set(delivery) - {'canvas', 'fps', 'fit_policy', 'editor', 'subtitle_style_ref'}
            or delivery.get('fit_policy') not in ('preserve', 'explicit_crop', 'letterbox')
            or delivery.get('editor') not in ('chatcut', 'openchatcut')
            or type(delivery.get('fps')) is not int or delivery['fps'] not in (24, 25, 30, 50, 60)
            or not isinstance(delivery.get('canvas'), list) or len(delivery['canvas']) != 2
            or any(type(x) is not int or x < 64 or x > 8192 or x % 2 for x in delivery['canvas'])):
        raise FactoryError('unsupported_delivery_spec')
    if delivery['fit_policy'] == 'preserve' and delivery['canvas'] != production['native_canvas']:
        raise FactoryError('canvas_change_requires_explicit_fit_policy')
    canonical(spec)
    return spec


def resolve_spec(root, episode, *, override=None, authorization=None):
    root = project(root)
    episode_id(episode)
    config = read_json(root / 'short-drama.json')
    authority = config.get('creator_authority', {})
    frozen = authority.get('episode_specs', {}).get(episode)
    if frozen:
        if frozen.get('status') != 'accepted':
            raise FactoryError('episode_spec_not_accepted')
        base = copy.deepcopy(frozen['spec'])
        if override:
            if not authorization:
                raise FactoryError('task_override_authorization_required')
            base = merge(base, override)
        if base.get('episode') != episode:
            raise FactoryError('episode_spec_identity_mismatch')
        return validate_spec(base)
    ratio = config.get('format', {}).get('aspect_ratio')
    historical = set()
    for path in project_path(root, LEDGER / 'jobs').glob('*.json'):
        job = read_json(path)
        parameters = job.get('parameters', {})
        if job.get('source') == f'剧集/{episode}/视频提示词.md' and parameters.get('profile') == 'h3-v4-g4-fullframe-v1':
            historical.add((parameters.get('width'), parameters.get('height')))
    if len(historical) > 1 and not (override and override.get('production', {}).get('native_canvas')):
        raise FactoryError('historical_episode_canvases_conflict_freeze_explicit_spec')
    if len(historical) == 1:
        native = next(iter(historical))
        if native not in ((1344, 768), (768, 1344)):
            raise FactoryError('unsupported_historical_canvas')
        ratio = '16:9' if native == (1344, 768) else '9:16'
    if ratio not in ('16:9', '9:16'):
        raise FactoryError('explicit_supported_aspect_ratio_required')
    canvas = [1344, 768] if ratio == '16:9' else [768, 1344]
    base = {'schema_version': 1, 'episode': episode,
            'production': {'profile': 'h3-v4-g4-fullframe-v1', 'native_canvas': canvas, 'fps': 24},
            'delivery': {'canvas': canvas, 'fps': 24, 'fit_policy': 'preserve', 'editor': 'chatcut'}}
    defaults = authority.get('factory_defaults', {})
    if defaults.get('status') == 'accepted':
        base = merge(base, defaults['spec'])
    if len(historical) == 1:
        base['production']['native_canvas'] = list(next(iter(historical)))
        if base['delivery']['fit_policy'] == 'preserve':
            base['delivery']['canvas'] = list(next(iter(historical)))
    # Explicit formal edit specifications may refine delivery without changing the native bucket.
    edit = project_path(root, f'剧集/{episode}/剪辑单.md')
    if edit.is_file():
        text = edit.read_text()
        if re.search(r'^- 工程：OpenChatCut Pro\b', text, re.M):
            base['delivery']['editor'] = 'openchatcut'
        match = re.search(r'^- 画幅与帧率：[^\n]*?(\d+)[×x](\d+)[^\n]*?(\d+(?:\.\d+)?)\s*fps', text, re.M)
        if match:
            canvas = [int(match[1]), int(match[2])]
            fps = float(match[3])
            if not fps.is_integer():
                raise FactoryError('unsupported_formal_delivery_fps')
            base['delivery'].update(canvas=canvas, fps=int(fps))
            if canvas != base['production']['native_canvas']:
                header = text[match.start():].splitlines()[0]
                if any(word in header for word in ('裁切', '铺满')):
                    base['delivery']['fit_policy'] = 'explicit_crop'
                elif '留边' in header:
                    base['delivery']['fit_policy'] = 'letterbox'
                else:
                    raise FactoryError('formal_delivery_fit_policy_required')
    if override:
        if not authorization:
            raise FactoryError('task_override_authorization_required')
        base = merge(base, override)
    if base.get('episode') != episode:
        raise FactoryError('episode_spec_identity_mismatch')
    return validate_spec(base)


def freeze_spec(root, episode, *, override=None, authorization):
    root = project(root)
    path = root / 'short-drama.json'
    before = file_sha(path)
    spec = resolve_spec(root, episode, override=override, authorization=authorization)
    config = read_json(path)
    records = config.setdefault('creator_authority', {}).setdefault('episode_specs', {})
    if records.get(episode, {}).get('spec') == spec:
        return spec
    records[episode] = {'status': 'accepted', 'spec': spec, 'authorization_basis': authorization,
                        'adopted_by': 'executor_under_task_authority', 'at': time.time()}
    revise(root, {'short-drama.json': json.dumps(config, ensure_ascii=False, indent=2) + '\n'},
           {'short-drama.json': before}, reason=authorization)
    return spec
