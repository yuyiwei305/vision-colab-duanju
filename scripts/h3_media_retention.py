"""One retained artifact per output; explicit user deletions stay in the ledger."""
from pathlib import Path
import hashlib
import json
import os
import re
import tempfile


def project_path(root, relative):
    root = Path(root).resolve()
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('unsafe_retention_path')
    target = root / path
    if any(p.is_symlink() for p in (target, *target.parents)) or not target.resolve().is_relative_to(root):
        raise ValueError('unsafe_retention_path')
    return target


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.retention-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def deleted_files(root):
    result = {}
    directory = Path(root) / '.short-drama/production/retention'
    for file in sorted(directory.glob('*/manifest.json')):
        if file.is_symlink() or file.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('unsafe_retention_manifest')
        manifest = json.loads(file.read_text())
        if manifest.get('state') != 'completed':
            continue
        if manifest.get('schema_version') != 1 or manifest.get('user_authorized_deletion') is not True:
            raise ValueError('invalid_retention_manifest')
        for item in manifest['deleted_files']:
            project_path(root, item['path'])
            if not re.fullmatch('[0-9a-f]{64}', item['sha256']) or type(item['bytes']) is not int:
                raise ValueError('invalid_retention_record')
            result[item['path']] = {**item, 'manifest': str(file.relative_to(root))}
    return result


def removed_outputs(engine, root, job_id, records=None):
    records = deleted_files(root) if records is None else records
    removed = []
    for run in engine._read_run_history(Path(root), job_id):
        if run.get('status') != 'succeeded':
            continue
        for output in run.get('outputs', []):
            item = records.get(output['path'])
            if item and (item['job_id'], item['run_id'], item['sha256'], item['bytes']) == (
                    job_id, run['run_id'], output['sha256'], output['bytes']):
                removed.append(item)
    return removed


def compact_cache(engine, root, job_id):
    """Drop recovery copies only after every canonical output is committed and verified."""
    root = Path(root)
    history = engine._read_run_history(root, job_id)
    if not history or history[-1].get('status') != 'succeeded':
        return
    run = history[-1]
    if run.get('adapter') != 'h3-colab' or not run.get('provider_job_id'):
        return
    matches = list((root / '.short-drama/production/colab').glob(
        '*/results/' + run['provider_job_id'] + '.json'))
    if len(matches) != 1:
        return
    receipt_path = project_path(root, str(matches[0].relative_to(root)))
    if receipt_path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('unsafe_cache_receipt')
    receipt = json.loads(receipt_path.read_text())
    claimed = {v['path']: v for v in run['outputs']}
    if receipt.get('job_id') != job_id or {v['target'] for v in receipt['files']} != set(claimed):
        raise ValueError('cache_receipt_output_mismatch')
    copies = []
    for value in receipt['files']:
        output = claimed[value['target']]
        canonical = project_path(root, value['target'])
        if (canonical.stat().st_size != output['bytes'] or sha256(canonical) != output['sha256']
                or value['sha256'] != output['sha256'] or value['bytes'] != output['bytes']):
            raise ValueError('canonical_output_unverified_keep_recovery_copy')
        name = value.get('cache_name')
        if name:
            if Path(name).name != name:
                raise ValueError('unsafe_cache_name')
            cache = receipt_path.parent / name
            if cache.exists():
                if cache.is_symlink() or cache.stat().st_size != value['bytes'] or sha256(cache) != value['sha256']:
                    raise ValueError('cache_integrity_mismatch')
                copies.append(cache)
        value['retained_path'] = value['target']
    # A crash after this receipt update can recover from either verified copy.
    write_json(receipt_path, receipt)
    for cache in copies:
        cache.unlink()


def install(engine):
    """Adapt pinned status/audit/recovery without changing historical run records."""
    audit, status, collect, run = engine.audit_project, engine.job_status, engine.collect_job, engine.run_job

    def audit_retention(root):
        root = engine.find_project(root)
        report = audit(root)
        records = deleted_files(root)
        acknowledged, pending, removed_by_job = [], [], {}
        for problem in report['problems']:
            if problem['code'] != 'output_missing_or_unsafe':
                pending.append(problem)
                continue
            job_id = problem.get('job_id')
            if job_id not in removed_by_job:
                removed_by_job[job_id] = removed_outputs(engine, root, job_id, records) if job_id else []
            known = next((v for v in removed_by_job[job_id] if v['path'] == problem.get('path')
                          and v['run_id'] == problem.get('run_id')), None)
            if (problem['code'] == 'output_missing_or_unsafe' and known
                    and not (root / known['path']).exists() and not (root / known['path']).is_symlink()):
                acknowledged.append({'path': known['path'], 'job_id': job_id, 'manifest': known['manifest']})
            else:
                pending.append(problem)
        report['problems'] = pending
        report['outputs']['missing'] -= len(acknowledged)
        report['outputs']['intentionally_deleted'] = len(acknowledged)
        report['retention'] = acknowledged
        report['status'] = 'attention' if pending else 'pass'
        return report

    def status_retention(root, *, job_id):
        root = engine.find_project(root)
        result = status(root, job_id=job_id)
        removed = removed_outputs(engine, root, job_id)
        if removed:
            result['retention'] = {'intentionally_deleted': [v['path'] for v in removed],
                                   'action': 'do_not_restore_or_regenerate'}
        return result

    def reject_retired(root, job_id):
        if removed_outputs(engine, engine.find_project(root), job_id):
            raise ValueError('outputs_intentionally_deleted_by_user_do_not_restore_or_regenerate')

    def collect_retention(root, *, job_id, adapter_config):
        reject_retired(root, job_id)
        result = collect(root, job_id=job_id, adapter_config=adapter_config)
        compact_cache(engine, engine.find_project(root), job_id)
        return result

    def run_retention(root, *, job_id, adapter_config):
        reject_retired(root, job_id)
        result = run(root, job_id=job_id, adapter_config=adapter_config)
        compact_cache(engine, engine.find_project(root), job_id)
        return result

    engine.audit_project, engine.job_status = audit_retention, status_retention
    engine.collect_job, engine.run_job = collect_retention, run_retention
