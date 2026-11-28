#!/usr/bin/env python3
"""Bounded RelayVSR finishing experiment via the existing Colab transport and ledger."""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import colab_h3 as h

SELF=Path(__file__).resolve()
WORKER=SELF.with_name('relay_vsr_colab_remote.py')


def engine():
    # Full-length finishing assets exceed the upstream short-shot defaults.
    # Keep bounded copying and the same hashing/snapshot/receipt implementation.
    production=h.engine()
    production.MAX_INPUT_BYTES=200*1024*1024
    production.MAX_TOTAL_INPUT_BYTES=256*1024*1024
    production.MAX_OUTPUT_BYTES=4*1024*1024*1024
    return production


def execute(c,code):return c.execute(code+'\nprint('+repr(h.MARKER)+'+json.dumps(result))\n')


def status(c,remote,name='status.json'):
    return execute(c,'import json\nfrom pathlib import Path\n'+
        f'p=Path({remote!r})/{name!r}\nresult=json.loads(p.read_text()) if p.exists() else {{"state":"pending"}}')


def upload(c, source, target):
    # Jupyter's single JSON request failed on the 102 MB input. Reassemble
    # bounded chunks remotely, then verify the complete original byte stream.
    source=Path(source)
    execute(c,'import json\nfrom pathlib import Path\n'+f'Path({target!r}).write_bytes(b"")\nresult={{}}')
    with source.open('rb') as stream, tempfile.TemporaryDirectory() as temp:
        part=Path(temp)/'part.bin'
        while chunk:=stream.read(16*1024*1024):
            part.write_bytes(chunk)
            for attempt in range(3):
                try:c.upload(part,target+'.part');break
                except Exception:
                    if attempt==2:raise
            # Append only after a successful upload; never retry an unknown append.
            execute(c,'import json\nfrom pathlib import Path\n'+
                f'p=Path({(target+".part")!r})\nwith open({target!r},"ab") as out: out.write(p.read_bytes())\n'
                'p.unlink()\nresult={}')
    check=execute(c,'import json,hashlib\nfrom pathlib import Path\n'+
        f'result={{"sha256":hashlib.sha256(Path({target!r}).read_bytes()).hexdigest()}}')
    if check['sha256']!=h.digest(source):raise ValueError('upload_integrity_failed')


def download(c, source, target, size):
    target=Path(target)
    with target.open('wb') as out, tempfile.TemporaryDirectory() as temp:
        part=Path(temp)/'part.bin'
        for offset in range(0,size,32*1024*1024):
            execute(c,'import json\nfrom pathlib import Path\n'+
                f'with open({source!r},"rb") as inp:\n inp.seek({offset}); data=inp.read(32*1024*1024)\n'
                f'Path({(source+".download")!r}).write_bytes(data)\nresult={{}}')
            for attempt in range(3):
                try:c.download(source+'.download',part);break
                except Exception:
                    if attempt==2:raise
            out.write(part.read_bytes())
    if target.stat().st_size!=size:raise ValueError('download_size_failed')


def verify_media(path, *, frames=None, fps=None, audio=False, width=2560, height=1440):
    result=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,check=True)
    probe=json.loads(result.stdout);v=next(s for s in probe['streams'] if s['codec_type']=='video')
    if (v['width'],v['height'])!=(width,height):raise ValueError('output_geometry_mismatch')
    if fps is not None and v['avg_frame_rate']!=f'{fps}/1':raise ValueError('output_fps_mismatch')
    if frames is not None and int(v['nb_frames'])!=frames:raise ValueError('output_frame_count_mismatch')
    if audio and not any(s['codec_type']=='audio' for s in probe['streams']):raise ValueError('output_audio_missing')
    subprocess.run(['ffmpeg','-v','error','-xerror','-nostdin','-i',str(path),'-f','null','-'],capture_output=True,check=True)
    return {'width':v['width'],'height':v['height'],'fps':v['avg_frame_rate'],'frames':int(v.get('nb_frames',0)),
            'duration':float(v['duration']),'full_decode':'passed','sha256':h.digest(path),'bytes':Path(path).stat().st_size}


def save_artifact(c, remote, local, expected):
    local=Path(local);local.parent.mkdir(parents=True,exist_ok=True)
    if local.exists() and h.digest(local)==expected['sha256']:return
    partial=local.with_suffix(local.suffix+'.partial')
    for attempt in range(3):
        try:
            download(c,remote,partial,expected['bytes'])
            if h.digest(partial)!=expected['sha256']:raise ValueError('download_sha_mismatch')
            partial.replace(local);return
        except Exception:
            if attempt==2:raise


def allocate(transport, batch, *, previous_endpoint=None):
    resource = Path(batch['state_file'])
    state = h.read_json(resource)
    deadline = math.inf
    if not state.get('ccu_only'):
        reserve = 480
        if batch.get('finishing_backend') == 'recavsr-comfy-colab':
            reserve = max(90, min(480, (state['deadline'] - state['started_at']) * 0.15))
        deadline = state.get('compute_deadline', state['deadline'] - reserve)
    return h.colab_allocation.allocate(
        transport, resource, batch.get('gpu', 'G4'), deadline,
        max_wait=h._cli_timeout('COLAB_ALLOC_TIMEOUT', h.colab_allocation.MAX_WAIT),
        previous_endpoint=previous_endpoint)


def salvage(batch):
    """Copy every produced MP4 before releasing, including an incomplete candidate."""
    statepath=Path(batch['state_file']);state=h.read_json(statepath)
    if not state.get('active_directory'):return
    c=h.Colab(batch['session']);remote=state['active_directory']
    files=execute(c,'import json,hashlib\nfrom pathlib import Path\n'+
        f'p=Path({remote!r})\nresult={{}}\n'+
        'for f in p.glob("*.mp4"):\n'
        ' if f.name=="input.mp4":continue\n'
        ' digest=hashlib.sha256()\n'
        ' with f.open("rb") as src:\n'
        '  for chunk in iter(lambda:src.read(4*1024*1024),b""):digest.update(chunk)\n'
        ' result[f.name]={"sha256":digest.hexdigest(),"bytes":f.stat().st_size}\n')
    saved={}
    for name,receipt in files.items():
        path=statepath.parent/'preserved'/state['active_job']/name
        save_artifact(c,remote+'/'+name,path,receipt)
        saved[name]={**receipt,'local':str(path)}
        try:saved[name]['media']=verify_media(path)
        except Exception as exc:saved[name]['media_check_error']=type(exc).__name__
    state=h.read_json(statepath)
    state['preserved']=saved;state['active_artifacts_preserved']=True
    h.write_json(statepath,state)


def release(batch):
    c=h.Colab(batch['session']);p=Path(batch['state_file'])
    if not p.exists():return {'state':'not_allocated'}
    state=h.read_json(p)
    if state.get('worker_started') and not state.get('active_artifacts_preserved'):
        raise RuntimeError('release_blocked_until_files_retrieved_and_verified')
    # Recheck saved bytes at the actual destructive boundary.
    for value in state.get('preserved',{}).values():
        if h.digest(value['local'])!=value['sha256']:raise RuntimeError('preserved_file_changed')
    if not state.get('endpoint'):
        state['state'] = 'release_requested'
        h.write_json(p, state)
        endpoint = h.colab_allocation.recover(c, p)
        if endpoint:
            state['endpoint'] = endpoint
            h.write_json(p, state)
    if state.get('endpoint') in {item['endpoint'] for item in c.control('assignments')}:
        c.control('unassign', state['endpoint'])
    assignments=c.control('assignments')
    if state.get('endpoint') in {a['endpoint'] for a in assignments}:raise RuntimeError('runtime_still_assigned')
    state.update(state='released',released_at=time.time(),assignments_after=assignments,usage_after=c.control('usage'))
    h.write_json(p,state);return state


def guard(batch):
    while time.time()<h.read_json(batch['state_file'])['deadline']:
        if h.read_json(batch['state_file']).get('state')=='released':return
        time.sleep(3)
    # Never erase the only copy. Worker stops before this collection reserve.
    try:
        state=h.read_json(batch['state_file'])
        if state.get('worker_pid') and state.get('worker_started'):
            c=h.Colab(batch['session'])
            execute(c,'import json,os,signal\n'+f'pid={state["worker_pid"]}\n'+
                'try:os.killpg(pid,signal.SIGTERM)\nexcept ProcessLookupError:pass\nresult={}')
            time.sleep(10)
        salvage(batch);release(batch)
    except Exception as exc:
        s=h.read_json(batch['state_file']);s.update(state='collection_blocked_gpu_preserved',guard_error=type(exc).__name__)
        h.write_json(batch['state_file'],s)


def adapter(job,batch):
    if job['job_id'] not in batch['jobs'] or h.digest(WORKER)!=batch['worker_sha256']:
        raise ValueError('snapshot_changed')
    c=h.Colab(batch['session']);statepath=Path(batch['state_file']);root=statepath.parent
    remote='/content/'+batch['session'];jobdir=remote+'/jobs/'+job['job_id']
    if not statepath.exists():
        usage=c.control('usage')
        if c.control('assignments') or usage['count'] or usage['rate']:raise ValueError('account_not_idle')
        if usage['balance']<batch['max_ccu']:raise ValueError('insufficient_existing_units')
        now=time.time()
        state={'state':'allocating','started_at':now,'deadline':now+batch['max_seconds'],
            'compute_deadline':now+batch['max_seconds']-420,'usage_before':usage,'remote_directory':remote,'verified_jobs':[]}
        h.write_json(statepath,state,exclusive=True)
        subprocess.Popen([sys.executable,str(SELF),'guard','--batch',batch['batch_file']],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        state['endpoint'] = allocate(c, batch)
        state['state'] = 'uploading'
        h.write_json(statepath,state)
        execute(c,'import json\nfrom pathlib import Path\n'+f'Path({remote!r}).mkdir(exist_ok=True)\nresult={{}}')
        upload(c,Path(job['project_root'])/job['references'][0],remote+'/input.mp4')
        upload(c,batch['rife_zip'],remote+'/rife.zip')
    state=h.read_json(statepath)
    if state['state']=='released':raise RuntimeError('runtime_released')
    key=job.get('collect_provider_job_id')
    if not key:
        if state.get('active_job')==job['job_id']:raise RuntimeError('existing_attempt_collect_first')
        if (state.get('active_job') and state['active_job'] not in state['verified_jobs']
                and job['parameters'].get('resume_from_job')!=state['active_job']):
            raise RuntimeError('previous_job_not_verified')
        key=batch['session']+'/'+job['job_id']
        h.write_json(job['handle_path'],{'provider_job_id':key},exclusive=True)
        execute(c,'import json,os\nfrom pathlib import Path\n'+f'p=Path({jobdir!r});p.mkdir(parents=True,exist_ok=False)\n'+
            f'os.link({(remote+"/input.mp4")!r},p/"input.mp4");os.link({(remote+"/rife.zip")!r},p/"rife.zip")\nresult={{}}')
        if job['parameters'].get('resume_from_job'):
            parent=job['parameters']['resume_from_job']
            if parent not in batch.get('resume_allowed_jobs',[]):raise ValueError('resume_source_not_allowed')
            master_name=job['parameters'].get('master_filename','relay-1440p24.mp4')
            original=remote+'/jobs/'+parent+'/'+master_name
            check=execute(c,'import json,os,hashlib\nfrom pathlib import Path\n'+
                f'p=Path({original!r})\ndigest=hashlib.sha256(p.read_bytes()).hexdigest()\n'+
                f'assert digest=={job["parameters"]["resume_relay_sha256"]!r}\n'+
                f'os.link(p,Path({jobdir!r})/{master_name!r})\nresult={{"sha256":digest}}')
        c.upload(WORKER,jobdir+'/worker.py')
        runtime={**job['parameters'],'deadline':state['compute_deadline'],'cache_root':remote}
        execute(c,'import json\nfrom pathlib import Path\n'+f'(Path({jobdir!r})/"runtime.json").write_text({json.dumps(runtime)!r})\nresult={{}}')
        state.update(state='running',active_job=job['job_id'],active_directory=jobdir,worker_started=True,
            active_artifacts_preserved=False,preserved={})
        h.write_json(statepath,state)
        launched=execute(c,'import json,subprocess,sys\n'+f'log=open({(jobdir+"/worker.log")!r},"ab")\n'+
            f'p=subprocess.Popen([sys.executable,{(jobdir+"/worker.py")!r},"bootstrap",{jobdir!r}],stdout=log,stderr=log,start_new_session=True)\nresult={{"pid":p.pid}}')
        state['worker_pid']=launched['pid'];h.write_json(statepath,state)
    elif key!=batch['session']+'/'+job['job_id']:raise ValueError('collect_handle_mismatch')
    while time.time()<state['deadline']-120:
        for attempt in range(4):
            try:info=status(c,jobdir);break
            except h.H3Error:
                if attempt==3:raise
                time.sleep(3)
        h.write_json(root/'progress.json',{'job_id':job['job_id'],**info})
        if info['state']=='failed':raise RuntimeError('remote_failed:'+str(info))
        if info['state']=='succeeded':break
        usage=c.control('usage')
        if usage['rate']>batch['max_rate'] or state['usage_before']['balance']-usage['balance']>batch['max_ccu']:
            raise RuntimeError('resource_cap_collect_before_release')
        time.sleep(10)
    else:raise TimeoutError('resource_deadline_collect_before_release')
    receipt=status(c,jobdir,'receipt.json');resultdir=root/'results'/job['job_id'];resultdir.mkdir(parents=True,exist_ok=True)
    h.write_json(resultdir/'receipt.json',receipt)
    outputs=[];verified={};preserved={}
    params=job['parameters'];source_fps=params.get('source_fps',24)
    master_name=params.get('master_filename','relay-1440p24.mp4')
    for target,name,fps in zip(job['outputs'],('final.mp4',master_name),(60,source_fps)):
        durable=resultdir/name;save_artifact(c,jobdir+'/'+name,durable,receipt['files'][name])
        media=verify_media(durable,frames=params['frames']*fps//source_fps,fps=fps,audio=fps==60,
            width=params.get('output_width',2560),height=params.get('output_height',1440))
        verified[name]=media;preserved[name]={**media,'local':str(durable)}
        dest=Path(job['output_root'])/name
        import shutil
        shutil.copyfile(durable,dest)
        if h.digest(dest)!=media['sha256']:raise ValueError('staging_integrity_failed')
        outputs.append({'target':target,'source':str(dest)})
    # Preserve source audio by comparing decoded PCM in the requested interval.
    def audio_digest(path):
        duration=[] if params.get('preserve_full_audio') else ['-t',str(params['frames']/source_fps)]
        return subprocess.check_output(['ffmpeg','-v','error','-i',str(path),*duration,
            '-map','0:a:0','-c:a','pcm_s16le','-f','hash','-hash','sha256','-']).decode().strip()
    source=Path(batch['project'])/job['references'][0]
    if audio_digest(source)!=audio_digest(resultdir/'final.mp4'):raise ValueError('source_audio_mismatch')
    verified['audio_pcm']='identical_to_source_interval'
    h.write_json(resultdir/'verification.json',verified)
    state=h.read_json(statepath);state['verified_jobs'].append(job['job_id'])
    state.update(state='ready',active_artifacts_preserved=True,preserved=preserved);h.write_json(statepath,state)
    c.download(jobdir+'/worker.log',resultdir/'worker.log')
    return {'provider_job_id':key,'outputs':outputs}


def run_batch(batch, *, resume=False):
    production=engine();root=Path(batch['project'])
    for key in batch['jobs']:
        job=production._read_job(root,key)
        history=production._read_run_history(root,key)
        if history and not resume:raise ValueError('prior_attempt_collect_first')
        if not history:production.confirm_job(root,job_id=key,confirmation=production._preview(job)['confirmation'])
    try:
        results=[]
        for key in batch['jobs']:
            history=production._read_run_history(root,key)
            if history and history[-1]['status']=='succeeded':result=history[-1]
            elif history:result=production.collect_job(root,job_id=key,adapter_config=Path(batch['adapter_config']))
            else:result=production.run_job(root,job_id=key,adapter_config=Path(batch['adapter_config']))
            # The durable copy and the published project files must both agree.
            job=production._read_job(root,key);local=Path(batch['state_file']).parent/'results'/key
            for target,name in zip(job['outputs'],('final.mp4',job['parameters'].get('master_filename','relay-1440p24.mp4'))):
                if h.digest(root/target)!=h.digest(local/name):raise ValueError('published_output_sha_mismatch')
            results.append(result)
            print(json.dumps({'phase':'collected_verified_published','job_id':key}),flush=True)
        release(batch);return {'state':'released','jobs':batch['jobs']}
    except Exception:
        # No unconditional finally/unassign: preserve first, then release safely.
        if Path(batch['state_file']).exists():
            try:
                state=h.read_json(batch['state_file'])
                if not state.get('worker_started'):release(batch)
                else:
                    c=h.Colab(batch['session']);info=status(c,state['active_directory'])
                    if info['state'] in ('failed','succeeded'):salvage(batch)
                    state=h.read_json(batch['state_file']);state['state']='needs_recovery_gpu_preserved'
                    h.write_json(batch['state_file'],state)
            except Exception as exc:
                s=h.read_json(batch['state_file']);s.update(state='needs_recovery_gpu_preserved',recovery_error=type(exc).__name__)
                h.write_json(batch['state_file'],s)
        raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['run','resume','adapter','guard','release','status','collect'])
    p.add_argument('--batch',required=True)
    args=p.parse_args();batch=h.read_json(args.batch)
    try:
        if args.command=='adapter':result=adapter(json.load(sys.stdin),batch)
        elif args.command=='release':result=release(batch)
        elif args.command=='guard':guard(batch);result={}
        elif args.command=='status':result=h.read_json(batch['state_file'])
        elif args.command=='collect':
            salvage(batch);result={'state':'preserved_not_released'}
        else:result=run_batch(batch,resume=args.command=='resume')
        print(json.dumps(result))
    except Exception as exc:
        if args.command=='adapter':
            print(json.dumps({'error':{'provider':'relayvsr-colab','category':'server','code':type(exc).__name__,'retryable':False}}))
        else:print(json.dumps({'error':type(exc).__name__,'detail':str(exc)[:700]}))
        sys.exit(1)
