#!/usr/bin/env python3
"""Bounded Breeze TTS 2 voice bootstrap using the official drama ledger and Colab login."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import wave
import colab_h3 as h
from drama_factory.colab_allocation import claim_paid_lease, free_wait_orphaned, paid_deadline
from drama_factory.progress import resource_snapshot, watch

SELF = Path(__file__).resolve()
REMOTE = SELF.with_name('breeze_tts_colab_remote.py')


def execute(transport, code):
    return transport.execute(code + '\nprint(' + repr(h.MARKER) + '+json.dumps(result))\n')


def remote_status(transport, remote, name='status.json'):
    code = 'import json\nfrom pathlib import Path\n' \
        f'p=Path({remote!r})/{name!r}\nresult=json.loads(p.read_text()) if p.exists() else {{"state":"pending"}}'
    for attempt in range(3):
        try:
            return execute(transport, code)
        except h.H3Error:
            if attempt == 2:
                raise
            time.sleep(5)


def release(batch, transport):
    state = h.read_json(batch['resource_state'])
    if not state.get('endpoint') and state['state'] in ('allocating', 'release_requested'):
        state['state'] = 'release_requested'
        h.write_json(batch['resource_state'], state)
        endpoint = h.colab_allocation.recover(transport, batch['resource_state'])
        if endpoint:
            state['endpoint'] = endpoint
            h.write_json(batch['resource_state'], state)
    if state.get('endpoint') in {item['endpoint'] for item in transport.control('assignments')}:
        transport.control('unassign', state['endpoint'])
    assignments = transport.control('assignments')
    if state.get('endpoint') in {a['endpoint'] for a in assignments}:
        raise h.H3Error('voice_runtime_still_assigned')
    state.update(state='released', assignments_after=assignments,
        usage_after=transport.control('usage'), released_at=time.time())
    h.write_json(batch['resource_state'], state)
    return state


def guard(batch, transport):
    while True:
        state = h.read_json(batch['resource_state'])
        if state['state'] == 'released':
            return
        deadline = paid_deadline(batch['resource_state'], state, batch['max_rate'])
        if free_wait_orphaned(state) or (deadline is not None and time.time() >= deadline):
            break
        time.sleep(3)
    release(batch, transport)


def adapter(job, batch, transport):
    if job['modality'] != 'tts' or job['job_id'] not in batch['jobs']:
        raise ValueError('voice_job_not_in_batch')
    state = h.read_json(batch['resource_state'])
    if state['state'] != 'ready':
        raise ValueError('voice_runtime_not_ready')
    remote = state['remote_directory']
    key = job.get('collect_provider_job_id')
    if not key:
        key = uuid.uuid4().hex
        h.write_json(job['handle_path'], {'provider_job_id': key}, exclusive=True)
        payload = {'text': job['prompt'], 'instruction': job['parameters']['instruction'],
            'seed': job['parameters']['seed']}
        execute(transport, 'import json,os\nfrom pathlib import Path\n'
            f'p=Path({remote!r})/"requests"/{(key + ".json")!r}\n'
            f'v=json.loads({json.dumps(payload,ensure_ascii=True)!r})\n'
            'p.write_text(json.dumps(v));result={"queued":True}')
    if not re.fullmatch('[0-9a-f]{32}', key):
        raise ValueError('invalid_voice_request')
    while time.time() < state['deadline'] - 45:
        receipt = remote_status(transport, remote, 'results/' + key + '.json')
        if receipt['state'] == 'failed':
            h.write_json(Path(batch['resource_state']).parent / ('receipt-failed-' + job['job_id'] + '.json'), receipt)
            raise ValueError('voice_generation_failed: ' + receipt.get('message', '')[:200])
        if receipt['state'] == 'succeeded':
            target = Path(job['output_root']) / 'voice.wav'
            transport.download(receipt['file'], target)
            if h.digest(target) != receipt['sha256']:
                raise ValueError('voice_download_integrity_failed')
            with wave.open(str(target)) as audio:
                if audio.getnchannels() != 1 or not 2 <= audio.getnframes()/audio.getframerate() <= 15:
                    raise ValueError('voice_outside_h3_reference_contract')
            h.write_json(Path(batch['resource_state']).parent / ('receipt-' + job['job_id'] + '.json'), receipt)
            return {'provider_job_id': key, 'outputs': [{'target': job['outputs'][0], 'source': str(target)}]}
        time.sleep(3)
    raise ValueError('voice_deadline_collect_before_retry')


def run(batch, transport):
    root = Path(batch['project'])
    production = h.engine()
    if h.digest(REMOTE) != batch['worker_sha256']:
        raise ValueError('voice_worker_changed')
    for key in batch['jobs']:
        job = production._read_job(root, key)
        if not production._inputs_current(root, job) or production._read_run_history(root, key):
            raise ValueError('voice_input_changed_or_existing_attempt')
        production.confirm_job(root, job_id=key, confirmation=production._preview(job)['confirmation'])
    usage = transport.control('usage')
    if transport.control('assignments') or usage['rate'] or usage['count']:
        raise ValueError('voice_bootstrap_requires_idle_account')
    if usage['balance'] < batch['max_ccu']:
        raise ValueError('voice_budget_unavailable')
    if (not math.isfinite(batch['max_rate']) or batch['max_rate'] <= 0
            or not math.isfinite(batch['max_ccu']) or batch['max_ccu'] * 3600 / batch['max_rate'] <= 240):
        raise ValueError('voice_budget_below_setup_and_release_reserve')
    state = {'state':'allocating','deadline':None,'started_at':time.time(),
        'ccu_only':True, 'ccu_remaining_at_reservation':batch['max_ccu'], 'controller_pid':os.getpid(),
        'usage_before':usage,'remote_directory':'/content/' + batch['session']}
    h.write_json(batch['resource_state'],state,exclusive=True)
    supervisor = subprocess.Popen([sys.executable,str(SELF),'guard','--batch',batch['batch_file']],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        state['endpoint'] = h.colab_allocation.allocate(
            transport, batch['resource_state'], 'L4', math.inf,
            max_wait=h._cli_timeout('COLAB_ALLOC_TIMEOUT', h.colab_allocation.MAX_WAIT))
        if h.read_json(batch['resource_state'])['state'] != 'allocating':
            raise h.H3Error('colab_allocation_stopped')
        claim_paid_lease(batch['resource_state'], state, batch['max_rate'])
        deadline = state['deadline']
        state['progress_phase'] = 'bootstrapping'
        h.write_json(batch['resource_state'],state)
        remote=state['remote_directory']
        # The CLI client intermittently hangs on the first exec after allocation
        # (kernel still booting). Probe readiness with short bounded retries
        # before the idempotent setup; a killed probe never reaches the kernel.
        probe = 'import json\nresult={"ping":True}\nprint(' + repr(h.MARKER) + '+json.dumps(result))\n'
        for attempt in range(6):
            try:
                transport.execute(probe, timeout=60)
                break
            except h.H3Error:
                if attempt == 5:
                    raise
                time.sleep(10)
        runtime={k:batch[k] for k in ('model','revision','repo_commit')}
        runtime.update(deadline=deadline,max_jobs=len(batch['jobs']))
        execute(transport,'import json\nfrom pathlib import Path\n'
            f'p=Path({remote!r});p.mkdir(exist_ok=True)\n'
            '(p/"requests").mkdir(exist_ok=True);(p/"results").mkdir(exist_ok=True)\n'
            f'(p/"runtime.json").write_text({json.dumps(runtime)!r})\nresult={{}}')
        transport.upload(REMOTE,remote+'/worker.py')
        execute(transport,'import json,subprocess,sys\n'
            f'log=open({(remote + "/bootstrap.log")!r},"ab")\n'
            f'p=subprocess.Popen([sys.executable,{(remote + "/worker.py")!r},"bootstrap",{remote!r}],stdout=log,stderr=log,start_new_session=True)\n'
            'result={"started":True}')
        while time.time()<deadline-120:
            status=remote_status(transport,remote)
            if status['state']=='failed': raise ValueError('voice_bootstrap_failed: ' + status.get('message', '')[:200])
            print(json.dumps({'phase':status['state']}),flush=True)
            if status['state']=='ready':break
            usage=transport.control('usage')
            if usage['rate']>batch['max_rate']:raise ValueError('voice_rate_exceeds_cap')
            time.sleep(8)
        else:raise ValueError('voice_bootstrap_deadline')
        state['state']='ready';h.write_json(batch['resource_state'],state)
        for key in batch['jobs']:
            if time.time() + 45 >= deadline:
                raise ValueError('voice_ccu_reserve_reached')
            state.update(progress_phase='generating', progress_job=key)
            h.write_json(batch['resource_state'], state)
            print(json.dumps({'phase':'voice_generating','job_id':key}),flush=True)
            try:
                result=production.run_job(root,job_id=key,adapter_config=Path(batch['adapter_config']))
            except Exception:
                # Transient CLI/download failures: recover the original request
                # by collect (no resubmit); only a real failure stops the batch.
                history = production._read_run_history(root, key)
                if not (history and history[-1].get('provider_job_id')):
                    raise
                result=production.collect_job(root,job_id=key,adapter_config=Path(batch['adapter_config']))
                print(json.dumps({'phase':'voice_recovered_by_collect','job_id':key}),flush=True)
            print(json.dumps({'phase':'voice_collected','job_id':result['job_id']}),flush=True)
            state.setdefault('completed_jobs', []).append(key)
            state.pop('progress_job', None)
            h.write_json(batch['resource_state'], state)
        environment=remote_status(transport,remote,'environment.json')
        h.write_json(Path(batch['resource_state']).parent/'environment.json',environment)
    finally:
        if state.get('state') == 'ready':
            for key in batch['jobs']:
                history = production._read_run_history(root, key)
                if history and history[-1].get('provider_job_id') and history[-1].get('status') != 'succeeded':
                    try:
                        production.collect_job(root,job_id=key,adapter_config=Path(batch['adapter_config']))
                    except Exception:
                        pass
        try:
            remote=state['remote_directory']
            transport.download(remote+'/bootstrap.log',Path(batch['resource_state']).parent/'bootstrap.log')
        except Exception:pass
        release(batch,transport)
        supervisor.terminate()
    return {'state':'released','jobs':batch['jobs']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['run','adapter','guard','release'])
    parser.add_argument('--batch',required=True)
    args=parser.parse_args()
    try:
        batch=h.read_json(args.batch);transport=h.Colab(batch['session'])
        if args.command=='adapter':result=adapter(json.load(sys.stdin),batch,transport)
        elif args.command=='run':
            resource = Path(batch['resource_state'])
            with watch(f"{Path(batch['project']).name} · {batch['session']}", resource.parent,
                       lambda: [resource_snapshot(resource, len(batch['jobs']))]):
                result=run(batch,transport)
        elif args.command=='guard':guard(batch,transport);result={}
        else:result=release(batch,transport)
        print(json.dumps(result,ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'error':{'provider':'breeze-tts2-colab','category':'server',
            'code':type(exc).__name__,'retryable':False,'message':str(exc)[:300]}}))
        raise SystemExit(1)
