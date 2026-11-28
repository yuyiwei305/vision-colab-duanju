"""Task v2: immutable authorization plus append-only waves in the official ledger.

The existing Colab controller owns jobs, GPU lifetimes and provider handles.
This module owns task membership; it never writes invented provider outcomes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import fcntl
import math
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from . import RELEASE
from .core import (LEDGER, ROOT, FactoryError, file_sha, fingerprint, project,
                   project_path, read_json, write_json)
from .planning import validate_plan
from .progress import resource_snapshot, watch


# One ordered list defines accepted upgrades and the next supported target.
EXECUTION_UPGRADES = (
    ('factory-v3', 'execution-upgrade.json', ('factory-v2',)),
    ('factory-v4', 'execution-upgrade-factory-v4.json', ('factory-v2', 'factory-v3')),
    ('factory-v7', 'execution-upgrade-factory-v7.json', ('factory-v6',)),
    ('factory-v8', 'execution-upgrade-factory-v8.json', ('factory-v7',)),
    ('factory-v9', 'execution-upgrade-factory-v9.json', ('factory-v8',)),
    ('factory-v10', 'execution-upgrade-factory-v10.json', ('factory-v9',)),
    ('factory-v12', 'execution-upgrade-factory-v12.json', ('factory-v11',)),
    ('factory-v13', 'execution-upgrade-factory-v13.json', ('factory-v12',)),
    ('factory-v14', 'execution-upgrade-factory-v14.json', ('factory-v13',)),
    ('factory-v15', 'execution-upgrade-factory-v15.json', ('factory-v14',)),
    ('factory-v16', 'execution-upgrade-factory-v16.json', ('factory-v15',)),
    ('factory-v17', 'execution-upgrade-factory-v17.json', ('factory-v16',)),
    ('factory-v18', 'execution-upgrade-factory-v18.json', ('factory-v17',)),
    ('factory-v19', 'execution-upgrade-factory-v19.json', ('factory-v18',)),
    ('factory-v20', 'execution-upgrade-factory-v20.json', ('factory-v19',)),
    ('factory-v21', 'execution-upgrade-factory-v21.json', ('factory-v20',)),
    ('factory-v22', 'execution-upgrade-factory-v22.json', ('factory-v21',)),
    ('factory-v23', 'execution-upgrade-factory-v23.json', ('factory-v22',)),
    ('factory-v24', 'execution-upgrade-factory-v24.json', ('factory-v23',)),
    ('factory-v25', 'execution-upgrade-factory-v25.json', ('factory-v24',)),
)


def task_path(root, task_id):
    if not re.fullmatch(r'[a-z0-9-]{1,24}', task_id):
        raise FactoryError('invalid_task_id')
    return project_path(root, LEDGER / 'colab' / ('task-' + task_id) / 'task.json')


@contextlib.contextmanager
def task_lock(path):
    with Path(path).with_suffix('.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def membership(path):
    """Verify the immutable wave chain; never trust a writable list of batch paths."""
    path = Path(path)
    contract = read_json(path)
    if contract.get('schema_version') != 2:
        raise FactoryError('task_v2_required')
    result, previous = {}, None
    for index, wave_path in enumerate(sorted(path.parent.glob('wave-*.json')), 1):
        wave = read_json(wave_path)
        if (wave_path.name != f'wave-{index:04}.json' or wave['previous'] != previous
                or wave['task_sha256'] != file_sha(path)):
            raise FactoryError('wave_chain_integrity_mismatch')
        for filename, sha in wave['batches'].items():
            if filename in result:
                raise FactoryError('duplicate_wave_batch')
            result[filename] = sha
        previous = file_sha(wave_path)
    return result


def _limits(max_ccu, max_seconds, max_instances):
    if (type(max_ccu) not in (int, float) or not math.isfinite(max_ccu) or not 0 < max_ccu <= 100
            or (max_seconds is not None and (type(max_seconds) is not int or not 0 < max_seconds <= 28800))
            or type(max_instances) is not int or not 1 <= max_instances <= 2):
        raise FactoryError('invalid_task_budget_or_instance_limit')


def create(root, task_id, plan, *, authorization, max_ccu, max_seconds=None, max_instances=None):
    root = project(root)
    validate_plan(root, plan)
    if plan['release'] != RELEASE:
        raise FactoryError('replan_with_current_execution_release')
    if not authorization or not plan['motions']:
        raise FactoryError('current_scope_and_nonempty_plan_required')
    instances = plan['schedule']['instances'] if max_instances is None else max_instances
    _limits(max_ccu, max_seconds, instances)
    if instances < plan['schedule']['instances']:
        raise FactoryError('replan_with_actual_instance_limit')
    path = task_path(root, task_id)
    from .releases import release_path, verify_release
    verify_release(RELEASE)
    contract = {'schema_version': 2, 'release': RELEASE, 'release_sha256': file_sha(release_path(RELEASE)), 'task_id': task_id,
                'project': str(root), 'episode': plan['episode'], 'goal': plan['goal'],
                'authorization_basis': authorization, 'max_ccu': max_ccu,
                'max_seconds': max_seconds, 'max_instances': instances,
                'created_at': time.time(), 'initial_plan': plan['plan_id']}
    write_json(path, contract, exclusive=True)
    return {'task': str(path), 'state': 'created_no_gpu', 'contract': contract}


def _terminal_batch(batch, production):
    root = Path(batch['project'])
    state_path = Path(batch['resource_state'])
    state = read_json(state_path)['state'] if state_path.exists() else None
    if state == 'paused_recoverable':
        receipt = state_path.with_name('retained-for-follow-up.json')
        return (receipt.is_file() and read_json(receipt).get('resource_sha256') == file_sha(state_path)
                and _retained_without_pending_requests(batch, production))
    if state not in (None, 'released', 'transferred'):
        return False
    import colab_h3 as h
    done = set(h.completed_jobs(root, batch, production))
    evidence_path = state_path.with_name('reconciliation.json')
    evidence = read_json(evidence_path).get('jobs', []) if evidence_path.exists() else []
    saved = {row['job_id']: row for row in evidence}
    proven = set()
    for job_id in batch['jobs']:
        history = production._read_run_history(root, job_id)
        row = saved.get(job_id, {})
        if row.get('state') in ('failed', 'not_submitted'):
            proven.add(job_id)
        if len(history) == 1 and state == 'released':
            run = history[-1]
            request = run.get('provider_job_id') or production._read_provider_handle(root, job_id, run['run_id'])
            if _failed_before_graph(state_path, run, request):
                proven.add(job_id)
            if (row.get('state') == 'runtime_released_uncollected'
                    and row.get('request_id') == request
                    and row.get('resource_sha256') == file_sha(state_path)
                    and _released_uncollected(state_path, read_json(state_path), run, request)):
                proven.add(job_id)
        if not history and _never_dispatched(root, job_id, production):
            proven.add(job_id)
    # A local failed subprocess is NOT evidence of provider terminal failure.
    return set(batch['jobs']) <= done | proven


def _never_dispatched(root, job_id, production):
    key = production._job_key(job_id)
    confirmation = root / LEDGER / 'confirmations' / (key + '.json')
    handles = root / LEDGER / 'handles' / key
    return (not any(handles.glob('*.json'))
            and (not confirmation.exists() or not read_json(confirmation).get('consumed_at')))


def _failed_before_graph(state_path, run, request):
    """The H3 adapter commits this graph before its first and only POST."""
    if (run.get('status') != 'failed' or run.get('adapter') != 'h3-colab'
            or run.get('error', {}).get('code') != 'colab_command_failed'
            or not isinstance(request, str)):
        return False
    try:
        if str(uuid.UUID(request)) != request:
            return False
    except ValueError:
        return False
    directory = state_path.parent
    graphs = directory / 'graphs'
    if not graphs.is_dir() or graphs.is_symlink():
        return False
    # A retained local graph or output receipt means a POST may have occurred.
    return not any(p.exists() or p.is_symlink() for p in (
        graphs / (request + '.json'), directory / 'results' / (request + '.json')))


def _released_uncollected(state_path, state, run, request):
    """A removed ephemeral VM cannot still run this request or return its local files."""
    release_at = state.get('finished_at')
    usage = state.get('usage_after', {})
    code = run.get('error', {}).get('code')
    if (state.get('state') != 'released' or not state.get('endpoint')
            or not isinstance(release_at, (int, float)) or not state.get('release_requested_at')
            or type(usage.get('count')) is not int or usage['count'] < 0
            or run.get('status') != 'failed' or run.get('adapter') != 'h3-colab'
            or code not in ('colab_command_failed', 'colab_response_unknown', 'provider_not_submitted')
            or not isinstance(request, str)):
        return False
    try:
        if str(uuid.UUID(request)) != request:
            return False
    except ValueError:
        return False
    directory = state_path.parent
    result = directory / 'results' / (request + '.json')
    evidence = directory / 'remote-evidence.json'
    if evidence.is_file():
        # A receipt for a reportedly unsubmitted request contradicts that result.
        if any(row.get('request_id') == request
               and (row.get('state') == 'succeeded' or code == 'provider_not_submitted')
               for row in read_json(evidence).get('receipts', [])):
            return False
    return ((directory / 'graphs' / (request + '.json')).is_file()
            and not result.exists() and not result.is_symlink())


def _execution_chain(path, contract):
    """Return the last verified append-only adoption; the task contract stays frozen."""
    release = contract['release'], contract['release_sha256']
    previous = None
    for new, filename, allowed in EXECUTION_UPGRADES:
        upgrade = path.with_name(filename)
        if not upgrade.exists():
            continue
        record = read_json(upgrade)
        if (release[0] not in allowed or record.get('task_sha256') != file_sha(path)
                or (record.get('from_release'), record.get('from_sha256')) != release
                or record.get('release') != new
                or record.get('previous') != (file_sha(previous) if previous else None)
                or not re.fullmatch(r'[a-f0-9]{64}', record.get('release_sha256', ''))
                or not record.get('authorization')):
            raise FactoryError('invalid_task_execution_upgrade')
        release = record['release'], record['release_sha256']
        previous = upgrade
    return release, previous


def execution_release(path, contract):
    return _execution_chain(path, contract)[0]


def upgrade_execution(root, task_id, *, authorization):
    from .releases import release_path, verify_release
    import colab_h3 as h
    root = project(root)
    path = task_path(root, task_id)
    contract = read_json(path)
    if not authorization:
        raise FactoryError('explicit_upgrade_authorization_required')
    with path.with_name('runner.lock').open('a') as runner, task_lock(path):
        fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.with_name('stop.json').exists():
            raise FactoryError('task_stopped')
        (prior, prior_sha), previous = _execution_chain(path, contract)
        next_upgrade = next((rule for rule in EXECUTION_UPGRADES[1:] if prior in rule[2]), None)
        if next_upgrade is None:
            if previous and prior in ('factory-v10', RELEASE):
                verify_release(prior, prior_sha)
                return read_json(previous)
            raise FactoryError('supported_prior_factory_release_required')
        upgrade_release, filename, _ = next_upgrade
        current = file_sha(release_path(upgrade_release))
        target = path.with_name(filename)
        verify_release(prior, prior_sha)
        verify_release(upgrade_release, current)
        transport_compatible = upgrade_release in ('factory-v8', 'factory-v9', 'factory-v10')
        production = h.engine()
        for filename, sha in membership(path).items():
            if file_sha(filename) != sha:
                raise FactoryError('task_batch_snapshot_changed')
            batch = read_json(filename)
            if (transport_compatible and batch.get('implementation_sha256') == h.implementation_digest()
                    and _retained_without_pending_requests(batch, production)):
                continue
            state = Path(batch['resource_state'])
            if state.exists() and read_json(state)['state'] not in ('released', 'transferred'):
                raise FactoryError('release_all_instances_before_upgrade')
            if not _terminal_batch(batch, production):
                raise FactoryError('reconcile_existing_requests_before_upgrade')
        record = {'task_sha256': file_sha(path), 'from_release': prior,
                  'from_sha256': prior_sha, 'release': upgrade_release,
                  'release_sha256': current, 'authorization': authorization,
                  'previous': file_sha(previous) if previous else None, 'at': time.time()}
        write_json(target, record, exclusive=True)
        return record


def _retained_without_pending_requests(batch, production):
    import colab_h3 as h
    state = Path(batch['resource_state'])
    if not state.is_file():
        return False
    saved = read_json(state)
    if saved['state'] != 'paused_recoverable' or not saved.get('endpoint'):
        return False
    root = Path(batch['project'])
    done = set(h.completed_jobs(root, batch, production))
    return all(job in done or (not production._read_run_history(root, job)
                               and _never_dispatched(root, job, production)) for job in batch['jobs'])


def append_wave(root, task_id, plan, *, reason, retain_runtime=False, reuse_runtime=False,
                retry_allocation=False):
    import colab_h3 as h
    import h3_episode
    import h3_production
    root = project(root)
    validate_plan(root, plan)
    path = task_path(root, task_id)
    contract = read_json(path)
    from .releases import verify_release
    release, release_sha = execution_release(path, contract)
    verify_release(release, release_sha)
    if release != RELEASE or plan['release'] != RELEASE:
        raise FactoryError('new_wave_requires_current_release_task_and_plan')
    if (contract['episode'] != plan['episode'] or contract['project'] != str(root)
            or contract['max_instances'] < plan['schedule']['instances'] or not reason):
        raise FactoryError('wave_outside_task_scope')
    if path.with_name('stop.json').exists():
        raise FactoryError('task_stopped')
    # One scope-level real review may cover a subset of the same unchanged inputs.
    h3_production.guard(root, plan['episode'], [n['entry'] for n in plan['motions']])
    with path.with_name('runner.lock').open('a') as runner, task_lock(path):
        fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        members = membership(path)
        choice = {'plan_id': plan['plan_id'], 'reason': reason}
        if retain_runtime or reuse_runtime:
            choice.update(retain_runtime=retain_runtime, reuse_runtime=reuse_runtime)
        if retry_allocation:
            choice['retry_allocation'] = True
        wave_key = fingerprint(choice)
        for existing in sorted(path.parent.glob('wave-*.json')):
            saved = read_json(existing)
            if saved.get('wave_key') == wave_key:
                _register_pending(saved['batches'])
                return {'state': 'existing_wave_recovered', 'task': str(path),
                        'wave': int(existing.stem.split('-')[1]), 'batches': list(saved['batches'])}
        old_batches = [read_json(filename) for filename in members]
        production = h.engine()
        parent = None
        if reuse_runtime:
            candidates = [b for b in old_batches if _retained_without_pending_requests(b, production)]
            if plan['schedule']['instances'] != 1 or len(candidates) != 1:
                raise FactoryError('single_retained_runtime_without_pending_requests_required')
            parent = candidates[0]['batch_file']
        if any(b['batch_file'] != parent and not _terminal_batch(b, production) for b in old_batches):
            raise FactoryError('existing_wave_requires_reconciliation_before_revision')
        allocation_source = None
        if retry_allocation:
            latest = max((b['wave'] for b in old_batches), default=0)
            candidates = [b for b in old_batches if b['wave'] == latest]
            if parent or len(candidates) != 1 or len(plan['schedule']['batches']) != 1:
                raise FactoryError('single_unsubmitted_batch_required_for_allocation_retry')
            allocation_source = candidates[0]
            source_plan = read_json(root / LEDGER / 'plans' / allocation_source['factory_plan'] / 'plan.json')
            if (any(production._read_run_history(root, job_id) for job_id in allocation_source['jobs'])
                    or any(plan[key] != source_plan[key] for key in ('motions', 'spec'))):
                raise FactoryError('allocation_retry_requires_same_unsubmitted_scope')
        waves = sorted(path.parent.glob('wave-*.json'))
        index = len(waves) + 1
        intent = path.parent / f'prepare-{index:04}.json'
        allocation_from = allocation_source['batch_file'] if allocation_source else None
        if intent.exists():
            saved = read_json(intent)
            if (saved['plan_id'] != plan['plan_id'] or saved.get('runtime_from') != parent
                    or saved.get('allocation_from') != allocation_from):
                raise FactoryError('incomplete_wave_prepare_recover_original_plan')
        else:
            saved = {'plan_id': plan['plan_id'], 'reason': reason, 'runtime_from': parent}
            if allocation_from:
                saved['allocation_from'] = allocation_from
            write_json(intent, saved, exclusive=True)
        created, previous_by_lane = {}, {0: parent} if parent else {}
        schedule = sorted(plan['schedule']['batches'], key=lambda b: (b['lane'], b['sequence']))
        accounting_path = path.with_name('accounting.json')
        accounting = read_json(accounting_path) if accounting_path.exists() else {}
        max_ccu = min(50, accounting.get('authorized_max_ccu', contract['max_ccu']))
        for serial, part in enumerate(schedule):
            batch_id = f'{task_id}-w{index:02}-{serial:02}'
            filename = root / LEDGER / 'colab' / batch_id / 'batch.json'
            prior = previous_by_lane.get(part['lane'])
            if not filename.exists():
                h3_episode.prepare(root, plan['episode'], batch_id, max_ccu=max_ccu,
                    max_seconds=contract['max_seconds'], pipeline='v4', only=part['entries'],
                    ordered_entries=part['entries'], authorization_basis=contract['authorization_basis'],
                    runtime_from=prior, episode_spec=plan['spec'],
                    ccu_only=accounting.get('deadline', contract['max_seconds']) is None)
            batch = read_json(filename)
            if batch.get('runtime_from') != prior:
                raise FactoryError('prepared_wave_runtime_changed')
            validate_compilation(root, batch, plan)
            if Path(batch['resource_state']).exists():
                raise FactoryError('wave_prepare_has_running_resources')
            if allocation_source:
                from .colab_allocation import carry_expired_request
                carry_expired_request(h.Colab(allocation_source['session']), allocation_source['resource_state'],
                                      batch['resource_state'], target_session=batch['session'], gpu=batch['gpu'])
            batch.update(task_budget=str(path), factory_plan=plan['plan_id'], wave=index,
                         lane=part['lane'], sequence=part['sequence'],
                         keep_runtime=retain_runtime or any(p['lane'] == part['lane'] and p['sequence'] > part['sequence']
                                                            for p in schedule))
            write_json(filename, batch)
            created[str(filename)] = file_sha(filename)
            previous_by_lane[part['lane']] = str(filename)
        wave = {'schema_version': 1, 'task_sha256': file_sha(path), 'plan_id': plan['plan_id'], 'wave_key': wave_key,
                'reason': reason, 'batches': created, 'created_at': time.time(),
                'previous': file_sha(waves[-1]) if waves else None}
        if parent:
            resource = Path(candidates[0]['resource_state'])
            receipt = resource.with_name('retained-for-follow-up.json')
            hold = {'resource_sha256': file_sha(resource), 'next_wave': index, 'plan_id': plan['plan_id']}
            if not receipt.is_file():
                write_json(receipt, hold, exclusive=True)
            elif read_json(receipt) != hold:
                raise FactoryError('retained_runtime_handoff_conflict')
        write_json(path.parent / f'wave-{index:04}.json', wave, exclusive=True)
        _register_pending(created)
        return {'state': 'registered_under_task_authority', 'task': str(path), 'wave': index,
                'batches': list(created), 'plan_id': plan['plan_id']}


def validate_compilation(root, batch, plan):
    """Execution must match the preview's actual prompt, inputs, seed and recipes."""
    import colab_h3 as h
    production = h.engine()
    jobs = [production._read_job(root, job_id) for job_id in batch['jobs']]
    entries = {job['job_id']: job['source_entry'] for job in jobs}
    nodes = {node['entry']: node for node in plan['motions']}
    for job in jobs:
        node = nodes[job['source_entry']]
        if job['prompt'] != node['prompt']:
            raise FactoryError('compiled_prompt_differs_from_plan:' + job['source_entry'])
        for key, expected in node['parameters'].items():
            actual = job['parameters'].get(key)
            if key == 'continue_from_job':
                actual = entries.get(actual, actual)
            if actual != expected:
                raise FactoryError('compiled_parameter_differs_from_plan:' + key)
        recipes = [{**r, 'source_job': entries.get(r['source_job'], r['source_job'])}
                   for r in job['parameters'].get('native_voice_references', [])]
        if recipes != node['native_voice_recipes']:
            raise FactoryError('compiled_voice_recipe_differs_from_plan')
        references = [{k: v for k, v in r.items() if k not in ('sha256', 'role', 'order')}
                      for r in node['references']]
        actual_references = [{k: v for k, v in r.items() if k not in ('role', 'order')}
                             for r in job['reference_bindings']]
        if references != actual_references:
            raise FactoryError('compiled_reference_order_differs_from_plan')


def _register_pending(filenames):
    import colab_h3 as h
    production = h.engine()
    for filename in filenames:
        batch = read_json(filename)
        root = h.validate_batch(batch)
        for job_id in batch['jobs']:
            if production._read_run_history(root, job_id):
                continue
            job = production._read_job(root, job_id)
            if not production._inputs_current(root, job):
                raise FactoryError('prepared_inputs_changed')
            confirmation = root / LEDGER / 'confirmations' / (production._job_key(job_id) + '.json')
            if confirmation.exists() and read_json(confirmation).get('consumed_at'):
                raise FactoryError('consumed_confirmation_without_run_requires_reconciliation')
            production.confirm_job(root, job_id=job_id, confirmation=production._preview(job)['confirmation'])


def job_state(root, batch, job_id, production, transport=None):
    import colab_h3 as h
    history = production._read_run_history(root, job_id)
    if job_id in h.completed_jobs(root, {'jobs': [job_id]}, production):
        return {'job_id': job_id, 'state': 'collected', 'action': 'reuse_verified_output'}
    if history and history[-1].get('status') == 'succeeded':
        return {'job_id': job_id, 'state': 'output_missing_or_changed', 'action': 'review_retention_no_regeneration'}
    if not history:
        return {'job_id': job_id, 'state': 'prepared', 'action': 'submit_once_after_registration'}
    run = history[-1]
    request = run.get('provider_job_id') or production._read_provider_handle(root, job_id, str(run['run_id']))
    state_path = Path(batch['resource_state'])
    state = read_json(state_path) if state_path.exists() else {}
    if not request:
        return {'job_id': job_id, 'state': 'unknown', 'action': 'inspect_official_run_before_retry'}
    receipt = Path(batch['resource_state']).parent / 'results' / (request + '.json')
    if receipt.exists():
        return {'job_id': job_id, 'state': 'completed_uncollected', 'request_id': request, 'action': 'collect'}
    if len(history) == 1 and state.get('state') == 'released' and _failed_before_graph(Path(batch['resource_state']), run, request):
        return {'job_id': job_id, 'state': 'not_submitted', 'request_id': request,
                'action': 'new_attempt_within_same_task', 'evidence': 'local_adapter_failed_before_graph'}
    if transport and state.get('endpoint'):
        live = {a['endpoint'] for a in transport.control('assignments')}
        if (state['endpoint'] not in live and len(history) == 1
                and _released_uncollected(state_path, state, run, request)):
            acceptance = 'not_submitted' if run['error']['code'] == 'provider_not_submitted' else 'unknown'
            return {'job_id': job_id, 'state': 'runtime_released_uncollected', 'request_id': request,
                    'action': 'new_attempt_within_same_task', 'provider_acceptance': acceptance,
                    'resource_sha256': file_sha(state_path), 'observed_at': time.time()}
        if state['endpoint'] in live:
            observed = transport.remote(state['remote_directory'], 'status', {'request_id': request})
            observed_state = observed['state']
            return {'job_id': job_id, 'state': observed_state, 'request_id': request,
                    'action': {'not_submitted': 'resume_same_request', 'succeeded': 'collect',
                               'running': 'collect', 'failed': 'new_attempt_within_same_task'}.get(observed_state, 'reconcile_no_post'),
                    'provider_evidence': observed, 'observed_at': time.time(), 'endpoint': state['endpoint']}
    if (len(history) == 1 and _released_uncollected(state_path, state, run, request)
            and not transport):
        evidence_path = state_path.with_name('reconciliation.json')
        if evidence_path.is_file():
            for row in read_json(evidence_path).get('jobs', []):
                if (row.get('job_id') == job_id and row.get('request_id') == request
                        and row.get('state') == 'runtime_released_uncollected'
                        and row.get('resource_sha256') == file_sha(state_path)):
                    return row
    return {'job_id': job_id, 'state': 'unknown', 'request_id': request, 'action': 'reconcile_no_post'}


def reconcile(root, task_id, *, online=False):
    import colab_h3 as h
    root = project(root)
    path = task_path(root, task_id)
    batches = []
    for filename, sha in membership(path).items():
        if file_sha(filename) != sha:
            raise FactoryError('task_batch_snapshot_changed')
        batch = read_json(filename)
        transport = h.Colab(batch['session']) if online else None
        rows = [job_state(root, batch, job_id, h.engine(), transport) for job_id in batch['jobs']]
        value = {'batch': filename, 'jobs': rows, 'online': online, 'at': time.time()}
        if online:
            write_json(Path(batch['resource_state']).with_name('reconciliation.json'), value)
        batches.append(value)
    accounting = path.with_name('accounting.json')
    contract = read_json(path)
    return {'task': task_id, 'contract': contract, 'batches': batches,
            'execution_release': execution_release(path, contract)[0],
            'accounting': read_json(accounting) if accounting.exists() else None,
            'stopped': path.with_name('stop.json').exists()}


def run(root, task_id, *, resume=False):
    import colab_h3 as h
    root = project(root)
    path = task_path(root, task_id)
    if path.with_name('stop.json').exists():
        raise FactoryError('task_stopped')
    # Distinct command lock: child controllers acquire the task's allocation lock.
    with path.with_name('runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        members = membership(path)
        batches = [read_json(p) for p in members]
        for filename, sha in members.items():
            if file_sha(filename) != sha:
                raise FactoryError('task_batch_snapshot_changed')
        if not batches:
            raise FactoryError('prepare_wave_first')
        latest = max(b['wave'] for b in batches)
        recovered = []
        if resume:
            # Collection is independent of later edits or unregistered siblings.
            # Never POST here: a lost response is reconciled by the controller.
            production = h.engine()
            for batch in batches:
                for job_id in batch['jobs']:
                    history = production._read_run_history(root, job_id)
                    if not history or job_id in h.completed_jobs(root, batch, production):
                        continue
                    if history[-1].get('status') == 'succeeded':
                        raise FactoryError('completed_output_missing_review_retention_before_resume:' + job_id)
                    try:
                        production.collect_job(root, job_id=job_id, adapter_config=Path(batch['adapter_config']))
                        recovered.append(job_id)
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                        pass
        _register_pending([b['batch_file'] for b in batches if b['wave'] == latest])
        lanes = {}
        for batch in batches:
            if batch['wave'] == latest:
                lanes.setdefault(batch['lane'], []).append(batch)
        def execute_lane(values):
            results = []
            for batch in sorted(values, key=lambda b: b['sequence']):
                state = Path(batch['resource_state'])
                if state.with_name('retained-for-follow-up.json').is_file():
                    raise FactoryError('retained_wave_reprepare_follow_up')
                done = h.completed_jobs(root, batch, h.engine())
                if len(done) == len(batch['jobs']):
                    if state.exists() and read_json(state)['state'] not in ('released', 'transferred') and not batch.get('keep_runtime'):
                        h.release_batch(batch, h.Colab(batch['session']), read_json(state))
                    results.append({'batch': batch['batch_file'], 'state': 'already_collected'})
                    continue
                if state.exists() and not resume:
                    raise FactoryError('existing_runtime_use_resume')
                if any(not h.engine()._read_run_history(root, job_id) for job_id in batch['jobs']):
                    frozen_plan = read_json(root / LEDGER / 'plans' / batch['factory_plan'] / 'plan.json')
                    validate_plan(root, frozen_plan)
                command = 'resume' if state.exists() else 'run'
                log_path = Path(batch['batch_file']).with_name('factory-run.log')
                with log_path.open('ab') as log:
                    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/colab_h3.py'),
                        command, '--batch', batch['batch_file']], stdout=log, stderr=subprocess.STDOUT)
                done = h.completed_jobs(root, batch, h.engine())
                results.append({'batch': batch['batch_file'], 'returncode': result.returncode,
                                'completed': len(done), 'log': str(log_path)})
                if result.returncode or len(done) != len(batch['jobs']):
                    break
            return results
        current = [batch for batch in batches if batch['wave'] == latest]
        def snapshots():
            rows = []
            for batch in current:
                row = resource_snapshot(batch['resource_state'], len(batch['jobs']))
                rows.append({'batch': batch['batch_id'], **row})
            return rows
        with (watch(f'{root.name} · {task_id}', path.parent, snapshots),
              ThreadPoolExecutor(max_workers=read_json(path)['max_instances']) as pool):
            results = list(pool.map(execute_lane, lanes.values()))
        return {'task': task_id, 'lanes': results, 'collected_before_revalidation': recovered,
                'status': reconcile(root, task_id)}


def stop(root, task_id, *, reason):
    import colab_h3 as h
    root = project(root)
    path = task_path(root, task_id)
    with task_lock(path):
        write_json(path.with_name('stop.json'), {'reason': reason, 'at': time.time()})
    released = []
    for filename in membership(path):
        batch = read_json(filename)
        if Path(batch['resource_state']).exists():
            state = read_json(batch['resource_state'])
            if state['state'] not in ('released', 'transferred'):
                released.append(h.release_batch(batch, h.Colab(batch['session']), state))
    return {'state': 'stopped', 'released': released}


def extend(root, task_id, *, max_ccu, authorization, additional_seconds=None, from_now=False, ccu_only=False):
    """Only extend after every owned lease has ended; active old guards remain intact."""
    path = task_path(project(root), task_id)
    with path.with_name('runner.lock').open('a') as runner, task_lock(path):
        fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        contract = read_json(path)
        _limits(max_ccu, contract['max_seconds'], contract['max_instances'])
        if (not authorization or (ccu_only and (additional_seconds is not None or from_now))
                or (not ccu_only and (type(additional_seconds) is not int or not 0 <= additional_seconds <= 28800))):
            raise FactoryError('explicit_budget_amendment_required')
        if ccu_only and execution_release(path, contract)[0] != RELEASE:
            raise FactoryError('upgrade_execution_before_ccu_only_amendment')
        for filename in membership(path):
            batch = read_json(filename)
            state = Path(batch['resource_state'])
            if state.exists() and read_json(state)['state'] not in ('released', 'transferred'):
                raise FactoryError('active_lease_extension_unsupported_release_first')
        accounting_path = path.with_name('accounting.json')
        if not accounting_path.exists() and not ccu_only:
            raise FactoryError('unstarted_task_create_correct_contract')
        accounting = read_json(accounting_path) if accounting_path.exists() else {
            'task_sha256': file_sha(path), 'started_at': time.time(), 'deadline': None}
        prior = accounting.get('authorized_max_ccu', contract['max_ccu'])
        if max_ccu < prior:
            raise FactoryError('amendment_cannot_remove_spent_budget')
        now = time.time()
        old_deadline = accounting['deadline']
        if not ccu_only and old_deadline is None:
            raise FactoryError('ccu_only_task_extension_requires_ccu_only')
        if from_now and (now <= old_deadline or additional_seconds == 0):
            raise FactoryError('new_window_requires_expired_deadline_and_positive_duration')
        start = now if from_now else old_deadline
        event = {'authorization_basis': authorization, 'previous_max_ccu': prior, 'max_ccu': max_ccu,
                 'old_deadline': old_deadline, 'deadline': None if ccu_only else start + additional_seconds,
                 'at': now, 'all_leases_released': True}
        if from_now:
            event['window_started_at'] = start
        accounting.setdefault('amendments', []).append(event)
        accounting.update(authorized_max_ccu=max_ccu, deadline=event['deadline'])
        write_json(accounting_path, accounting)
        return event
