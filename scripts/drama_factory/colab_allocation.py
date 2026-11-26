"""Bound Colab GPU assignment and persist its notebook ID before the first POST."""
from __future__ import annotations

import contextlib
import fcntl
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
        started = time.time()
        reply = transport.allocation_request(
            'lookup', request['gpu'], request['request_id'], started + HTTP_TIMEOUT, resource)
        record(journal, request, 'lookup', reply, started)
        if reply['state'] not in ('assigned', 'unassigned'):
            raise FactoryError('colab_allocation_lookup_unverified')
        return request.get('endpoint')


def allocate(transport, resource, gpu, use_by, *, max_wait=MAX_WAIT, previous_endpoint=None,
             check_budget=None):
    with journal_lock(resource) as journal:
        request = read_json(journal) if journal.is_file() else None
        if request and (request['session'], request['gpu']) != (transport.session, gpu):
            raise FactoryError('colab_allocation_request_scope_changed')
        if request and previous_endpoint and request.get('endpoint') == previous_endpoint:
            if previous_endpoint in {item['endpoint'] for item in transport.control('assignments')}:
                raise FactoryError('colab_previous_allocation_still_active')
            history = [*request.get('previous_requests', []),
                       {key: request[key] for key in ('request_id', 'endpoint', 'deadline', 'events')}]
            request = None
        else:
            history = []
        if request is None:
            now = time.time()
            request = {'request_id': str(uuid.uuid4()), 'session': transport.session,
                       'gpu': gpu, 'deadline': min(use_by, now + min(MAX_WAIT, max_wait)),
                       'attempts': 0, 'previous_requests': history}
            write_json(journal, request)

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
                break
            if check_budget:
                check_budget()
            operation = 'lookup' if request.get('endpoint') else 'attempt'
            started = time.time()
            reply = transport.allocation_request(operation, gpu, request['request_id'], deadline, resource)
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

    endpoint = recover(transport, resource)
    if endpoint and time.time() < use_by and read_json(resource)['state'] == 'allocating':
        return endpoint
    raise FactoryError('colab_allocation_deadline')


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
            if operation == 'lookup':
                return {'state': 'unassigned'}
            if time.time() >= deadline:
                return {'state': 'retry', 'reason': 'deadline'}
            if read_json(resource)['state'] != 'allocating':
                return {'state': 'retry', 'reason': 'stopped'}
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
