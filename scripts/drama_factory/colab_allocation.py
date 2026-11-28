"""Bound Colab GPU assignment and persist its notebook ID before the first POST."""
from __future__ import annotations

import contextlib
import fcntl
import math
import os
from pathlib import Path
import time
import uuid

from .core import FactoryError, read_json, write_json

MAX_WAIT = 30 * 60
HTTP_TIMEOUT = 45
RETRYABLE_STATUS = {502, 503, 504}


@contextlib.contextmanager
def journal_lock(resource):
    journal = Path(resource).with_name('allocation-request.json')
    with journal.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield journal


def record(journal, request, operation, reply, started):
    if operation == 'attempt':
        request['attempts'] += 1
    now = time.time()
    request.setdefault('events', []).append(
        {'operation': operation, 'at': now, 'elapsed_seconds': max(0, now - started), **reply})
    if reply['state'] == 'assigned':
        request['endpoint'] = reply['endpoint']
    if reply['state'] == 'rejected':
        request['rejected_status'] = reply['status']
    write_json(journal, request)


def _lookup(transport, resource, journal, request, *, waiting=False):
    started = time.time()
    reply = transport.allocation_request(
        'lookup', request['gpu'], request['request_id'], started + HTTP_TIMEOUT, resource)
    if waiting and reply.get('status') == 412 and reply['state'] == 'rejected':
        reply = {**reply, 'state': 'retry', 'reason': 'capacity_or_assignment_limit'}
    record(journal, request, 'lookup', reply, started)
    if waiting and reply['state'] == 'retry':
        return reply
    if reply['state'] not in ('assigned', 'unassigned'):
        raise FactoryError('colab_allocation_lookup_unverified')
    return reply


def _history(request):
    return [*request.get('previous_requests', []),
            {key: value for key, value in request.items() if key != 'previous_requests'}]


def recover(transport, resource):
    """Find the exact requested runtime, even if the assigning subprocess was lost."""
    with journal_lock(resource) as journal:
        if not journal.is_file():
            local = transport.control('session', transport.session)
            return local['endpoint'] if local else None
        request = read_json(journal)
        if request['session'] != transport.session:
            raise FactoryError('colab_allocation_request_session_changed')
        if request.get('endpoint'):
            return request['endpoint']
        if request.get('request_id'):
            _lookup(transport, resource, journal, request)
        return request.get('endpoint')


def possible_assignment_at(resource):
    """Read the earliest possible paid time for the current request, including an uncertain POST."""
    journal = Path(resource).with_name('allocation-request.json')
    clock = journal.with_name('allocation-clock.json')
    if journal.is_file() and clock.is_file():
        request, placement = read_json(journal), read_json(clock)
        if request.get('request_id') == placement['request_id']:
            return placement['possible_assignment_at']
    return None


def _deadline_for_paid_time(started, state, max_rate):
    return started + state['ccu_remaining_at_reservation'] * 3600 / max_rate - 120


def paid_deadline(resource, state, max_rate):
    """Use the paid clock even if the main controller lost the placement response."""
    if state['deadline'] is not None or not state.get('ccu_only'):
        return state['deadline']
    started = possible_assignment_at(resource)
    if started is not None:
        return _deadline_for_paid_time(started, state, max_rate)
    return None


def claim_paid_lease(resource, state, max_rate):
    started = possible_assignment_at(resource)
    if started is None:
        raise FactoryError('ccu_only_assignment_clock_missing')
    state.update(paid_started_at=started, deadline=_deadline_for_paid_time(started, state, max_rate))


def free_wait_orphaned(state):
    if state.get('ccu_only') and state['deadline'] is None and state['state'] == 'allocating':
        try:
            os.kill(state['controller_pid'], 0)
        except ProcessLookupError:
            return True
    return False


def carry_expired_request(transport, resource, target_resource, *, target_session, gpu):
    """Preserve a verified unassigned request when replanning under a newer release."""
    with journal_lock(resource) as source, journal_lock(target_resource) as target:
        request = read_json(source)
        state = read_json(resource)
        if (state.get('state') != 'released' or state.get('failure') != 'colab_allocation_deadline'
                or state.get('endpoint') or request.get('endpoint') or 'rejected_status' in request
                or time.time() < request['deadline']
                or (request['session'], request['gpu']) != (transport.session, gpu)):
            raise FactoryError('released_unassigned_window_required')
        if target.exists():
            saved = read_json(target)
            history = saved.get('previous_requests', [])
            if (saved.get('session') == target_session and saved.get('gpu') == gpu
                    and not saved.get('request_id') and history and history[-1]['request_id'] == request['request_id']):
                return
            raise FactoryError('allocation_history_conflict')
        if _lookup(transport, resource, source, request)['state'] != 'unassigned':
            raise FactoryError('colab_previous_allocation_still_active')
        write_json(target, {'session': target_session, 'gpu': gpu, 'previous_requests': _history(request)},
                   exclusive=True)


def allocate(transport, resource, gpu, use_by, *, max_wait=MAX_WAIT, previous_endpoint=None,
             check_budget=None):
    waiting = math.isinf(use_by)
    def start_window(journal, history):
        request = {'request_id': str(uuid.uuid4()), 'session': transport.session,
                   'gpu': gpu, 'deadline': min(use_by, time.time() + min(MAX_WAIT, max_wait)),
                   'attempts': 0, 'previous_requests': history}
        write_json(journal, request)

    with journal_lock(resource) as journal:
        request = read_json(journal) if journal.is_file() else {}
        if request and (request['session'], request['gpu']) != (transport.session, gpu):
            raise FactoryError('colab_allocation_request_scope_changed')
        if request and previous_endpoint and request.get('endpoint') == previous_endpoint:
            if previous_endpoint in {item['endpoint'] for item in transport.control('assignments')}:
                raise FactoryError('colab_previous_allocation_still_active')
            start_window(journal, _history(request))
        elif not request.get('request_id'):
            start_window(journal, request.get('previous_requests', []))

    while True:
        with journal_lock(resource) as journal:
            request = read_json(journal)
            if read_json(resource)['state'] != 'allocating':
                raise FactoryError('colab_allocation_stopped')
            if request.get('rejected_status') == 412:
                raise FactoryError('colab_allocation_limit_or_capacity')
            if 'rejected_status' in request:
                raise FactoryError(f"colab_allocation_rejected_http_{request['rejected_status']}")
            deadline = min(use_by, request['deadline'])
            if time.time() >= deadline:
                had_endpoint = bool(request.get('endpoint'))
                reply = _lookup(transport, resource, journal, request, waiting=waiting)
                if read_json(resource)['state'] != 'allocating':
                    raise FactoryError('colab_allocation_stopped')
                if time.time() >= use_by:
                    raise FactoryError('colab_allocation_deadline')
                if check_budget:
                    check_budget()
                if reply['state'] == 'assigned':
                    return reply['endpoint']
                if reply['state'] == 'retry':
                    # Keep the original request until an authenticated lookup is conclusive.
                    deadline = time.time() + 20
                elif had_endpoint:
                    raise FactoryError('colab_original_allocation_unavailable')
                else:
                    start_window(journal, _history(request))
                    continue
            else:
                if check_budget:
                    check_budget()
                operation = 'lookup' if request.get('endpoint') else 'attempt'
                started = time.time()
                reply = transport.allocation_request(operation, gpu, request['request_id'], deadline, resource)
                if waiting and reply.get('status') == 412 and reply['state'] == 'rejected':
                    reply = {**reply, 'state': 'retry', 'reason': 'capacity_or_assignment_limit'}
                record(journal, request, operation, reply, started)
                if reply['state'] == 'assigned':
                    if read_json(resource)['state'] != 'allocating':
                        raise FactoryError('colab_allocation_stopped')
                    if time.time() >= use_by:
                        raise FactoryError('colab_allocation_deadline')
                    return reply['endpoint']
                if reply['state'] == 'unassigned':
                    raise FactoryError('colab_original_allocation_unavailable')
                if reply['state'] == 'rejected':
                    continue
        delay = min(20, 5 * 2 ** min(max(0, request['attempts'] - 1), 2))
        time.sleep(min(delay, max(0, deadline - time.time())))

def request_once(client_state, operation, session, gpu, request_id, deadline, resource):
    """Use the installed Colab CLI's authenticated assignment and session models."""
    # The HTTP subprocess may outlive an interrupted controller. Serialize at
    # the request boundary as well so a resumed controller cannot race its POST.
    with Path(resource).with_name('allocation-http.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _request_once(client_state, operation, session, gpu, request_id, deadline, resource)


def _request_once(client_state, operation, session, gpu, request_id, deadline, resource):
    from colab_cli.client import Assignment, ColabRequestError
    from colab_cli.commands.session import resolve_runtime_options
    from colab_cli.state import SessionState
    from requests.exceptions import Timeout

    variant, accelerator, shape = resolve_runtime_options(gpu)
    args = uuid.UUID(request_id), variant, accelerator, shape
    if time.time() >= deadline:
        return {'state': 'retry', 'reason': 'deadline'}
    client = client_state.client

    # These are the same preflight and placement calls used by Client.assign().
    # The outer factory owns the retry schedule and passes the same UUID every time.
    try:
        _set_http_timeout(deadline)
        result = client._get_assignment(*args)
        if not isinstance(result, Assignment):
            state = read_json(resource)
            clock = Path(resource).with_name('allocation-clock.json')
            if state.get('ccu_only'):
                # A successful GET proves this request has no earlier placement.
                write_json(clock, {'request_id': request_id, 'possible_assignment_at': None})
            if operation == 'lookup':
                return {'state': 'unassigned'}
            if time.time() >= deadline:
                return {'state': 'retry', 'reason': 'deadline'}
            if state['state'] != 'allocating':
                return {'state': 'retry', 'reason': 'stopped'}
            if state.get('ccu_only'):
                # Persist before POST so a lost response or controller can still be bounded.
                write_json(clock, {'request_id': request_id, 'possible_assignment_at': time.time()})
            _set_http_timeout(deadline)
            result = client._post_assignment(args[0], result.token, *args[1:])
    except ColabRequestError as exc:
        status = exc.response.status_code
        return {'state': 'retry' if status in RETRYABLE_STATUS else 'rejected', 'status': status}
    except Timeout:
        return {'state': 'retry', 'reason': 'http_timeout'}
    proxy = result.runtime_proxy_info
    client_state.store.add(SessionState(
        name=session, endpoint=result.endpoint, token=proxy.token, url=proxy.url,
        token_expires_at=proxy.expires_at(), variant=variant.value, accelerator=accelerator.value))
    return {'state': 'assigned', 'endpoint': result.endpoint}


def _set_http_timeout(deadline):
    os.environ['COLAB_CLI_REQUEST_TIMEOUT'] = str(max(0.01, min(HTTP_TIMEOUT, deadline - time.time())))
