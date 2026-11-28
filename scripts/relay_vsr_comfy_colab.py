#!/usr/bin/env python3
"""Shared full-film finishing controller and historical RelayVSR entry point."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import colab_h3 as h
import relay_vsr_colab as relay
from drama_factory import finishing
from drama_factory.colab_allocation import claim_paid_lease, free_wait_orphaned, paid_deadline
from drama_factory.progress import resource_snapshot, watch

SELF = Path(__file__).resolve()
WORKER = SELF.with_name("relay_vsr_comfy_remote.py")
MAX_RESUME_OUTPUT_BYTES = 4 * 1024**3


def engine():
    production = relay.engine()
    production.MAX_INPUT_BYTES = 2 * 1024**3
    production.MAX_TOTAL_INPUT_BYTES = 3 * 1024**3
    production.MAX_TIMEOUT_SECONDS = 28920
    return production


def stage_copy(source, destination):
    if sys.platform == 'darwin':
        result = subprocess.run(['cp', '-c', str(source), str(destination)], capture_output=True)
        if result.returncode == 0:
            return
    shutil.copyfile(source, destination)


def ssh_options(batch):
    directory = resource(batch).parent / "transport-dir"
    if not directory.is_file():
        return None
    key_root = Path(directory.read_text().strip())
    identity = key_root / "key"
    known_hosts = key_root / "known_hosts"
    if not identity.is_file() or not known_hosts.is_file():
        return None
    proxy = shlex.join([str(Path.home() / ".local/bin/colab"), "ssh", "--proxy-mode",
                        "-s", batch["session"], "-i", str(identity)])
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", f"ProxyCommand={proxy}", "-i", str(identity)]


def prepare_ssh(c, batch, remote):
    pointer = resource(batch).parent / "transport-dir"
    if ssh_options(batch) is None:
        key_root = Path(tempfile.mkdtemp(prefix="vision-finish-ssh-"))
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key_root / "key")],
                       check=True, timeout=20)
    else:
        key_root = Path(pointer.read_text().strip())
    keys = relay.execute(c, """
import json
from pathlib import Path
result = ['localhost ' + ' '.join(p.read_text().split()[:2])
          for p in Path('/etc/ssh').glob('ssh_host_*_key.pub')]
""")
    if not keys:
        raise RuntimeError('Colab SSH host key unavailable')
    (key_root / 'known_hosts').write_text('\n'.join(keys) + '\n')
    pointer.write_text(str(key_root) + '\n')
    command = f'test -d {shlex.quote(remote)} && command -v rsync'
    for _ in range(3):
        try:
            result = subprocess.run([*ssh_options(batch), 'root@localhost', command],
                                    capture_output=True, timeout=35)
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
    pointer.unlink(missing_ok=True)
    raise RuntimeError('Colab SSH transport unavailable for large finishing inputs')


def remote_status(c, batch, remote, name="status.json"):
    options = ssh_options(batch)
    if options is None:
        return relay.status(c, remote, name)
    command = "cat " + shlex.quote(remote + "/" + name)
    try:
        result = subprocess.run([*options, "root@localhost", command], capture_output=True, timeout=40)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        pass
    return relay.status(c, remote, name)


def collect_artifact(c, batch, remote, local, item):
    local = Path(local)
    options = ssh_options(batch)
    if options is None:
        return relay.save_artifact(c, remote, local, item)
    if local.exists() and h.digest(local) == item["sha256"]:
        return
    local.parent.mkdir(parents=True, exist_ok=True)
    partial = local.with_suffix(local.suffix + ".partial")
    for _ in range(3):
        try:
            result = subprocess.run(["rsync", "-a", "--partial", "--inplace", "--timeout=120",
                                     "-e", shlex.join(options), f"root@localhost:{remote}", str(partial)],
                                    capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            continue
        if (result.returncode == 0 and partial.is_file() and partial.stat().st_size == item["bytes"]
                and h.digest(partial) == item["sha256"]):
            partial.replace(local)
            return
    raise RuntimeError("verified SSH retrieval failed")

def resource(batch):
    return Path(batch["state_file"])


def remote_launch(c, remote):
    return relay.execute(c, f"""
import fcntl, json, os, subprocess, sys
from pathlib import Path
p = Path({remote!r})
with (p/'launch.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    pid_file = p/'worker.pid'
    if pid_file.exists():
        pid = int(pid_file.read_text())
        try:
            os.kill(pid, 0)
            if str(p/'worker.py').encode() not in Path(f'/proc/{{pid}}/cmdline').read_bytes():
                raise RuntimeError('worker_pid_reused_reconcile_before_launch')
        except ProcessLookupError:
            status = json.loads((p/'status.json').read_text()) if (p/'status.json').exists() else {{}}
            if status.get('state') not in ('succeeded','failed'):
                raise RuntimeError('worker_gone_without_result_salvage_before_retry')
        result = {{'pid':pid,'reused':True}}
    else:
        intent = p/'launch-intent.json'
        if intent.exists():
            raise RuntimeError('worker_launch_unknown_reconcile_before_retry')
        intent.write_text(json.dumps({{'worker':str(p/'worker.py')}}))
        with (p/'worker.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, '-u', str(p/'worker.py'), str(p)], stdout=log, stderr=log,
                                     start_new_session=True)
            pid_file.write_text(str(child.pid))
        result = {{'pid': child.pid,'reused':False}}
""")


def file_info(c, path):
    return relay.execute(c, f"""
import hashlib, json
from pathlib import Path
p = Path({path!r})
result = {{"bytes": 0, "sha256": None}}
if p.exists():
    with p.open('rb') as src:
        result = {{"bytes": p.stat().st_size, "sha256": hashlib.file_digest(src, 'sha256').hexdigest()}}
""")


def upload_resumable(c, source, target):
    info = file_info(c, target)
    offset = info["bytes"]
    with source.open("rb") as src, tempfile.TemporaryDirectory() as work:
        prefix = hashlib.sha256()
        left = offset
        while left:
            data = src.read(min(left, 4 * 1024 * 1024))
            if not data:
                raise ValueError("remote source is longer than the snapshot")
            prefix.update(data)
            left -= len(data)
        if offset and prefix.hexdigest() != info["sha256"]:
            raise ValueError("remote source prefix differs from the snapshot")
        part = Path(work) / "chunk.bin"
        while data := src.read(16 * 1024 * 1024):
            part.write_bytes(data)
            expected = offset + len(data)
            for attempt in range(4):
                try:
                    c.upload(part, target + ".part")
                    relay.execute(c, f"""
import json
from pathlib import Path
p, chunk = Path({target!r}), Path({target+'.part'!r})
size = p.stat().st_size if p.exists() else 0
if size == {offset}:
    data = chunk.read_bytes()
    assert len(data) == {len(data)}
    with p.open('ab') as out:
        out.write(data)
elif size != {expected}:
    raise ValueError('unexpected remote byte count')
chunk.unlink(missing_ok=True)
result = {{'bytes': p.stat().st_size}}
""")
                    break
                except h.H3Error:
                    if attempt == 3:
                        raise
                    time.sleep(4)
            offset = expected
    if file_info(c, target)["sha256"] != h.digest(source):
        raise ValueError("uploaded source SHA mismatch")


def stage_input(c, batch, source, target):
    options = ssh_options(batch)
    if options is None:
        upload_resumable(c, source, target)
        return
    expected = h.digest(source)
    for _ in range(3):
        try:
            result = subprocess.run(['rsync', '-a', '--partial', '--inplace', '--timeout=120',
                                     '-e', shlex.join(options), str(source), f'root@localhost:{target}'],
                                    capture_output=True, timeout=600)
            if result.returncode == 0 and file_info(c, target)['sha256'] == expected:
                return
        except (h.H3Error, subprocess.TimeoutExpired):
            pass
    raise RuntimeError('verified SSH upload failed')


def stage_and_start(c, remote, source, batch, spec, state_file, state, rife_worker, master=None):
    recavsr = batch.get('finishing_backend') == finishing.ADAPTER
    relay.execute(c, f"import json\nfrom pathlib import Path\nPath({remote!r}).mkdir(exist_ok=True)\nresult={{}}")
    if recavsr or master is not None:
        prepare_ssh(c, batch, remote)
    stage_input(c, batch, source, remote + "/input.mp4")
    if spec.get('target_fps', 60) != spec['source_fps']:
        stage_input(c, batch, Path(batch["rife_zip"]), remote + "/rife.zip")
    master_key = 'resume_master_sha256' if recavsr else 'resume_relay_sha256'
    resume_master = Path(batch['durable_root']) / spec['master_filename']
    if master is not None:
        stage_input(c, batch, master, remote + '/' + spec['master_filename'])
    elif state.get('resume_master_sha256'):
        if h.digest(resume_master) != state['resume_master_sha256']:
            raise ValueError('recovered master changed')
        stage_input(c, batch, resume_master, remote + '/' + spec['master_filename'])
        spec = {**spec, master_key: state['resume_master_sha256']}
    stage_input(c, batch, finishing.WORKER if recavsr else WORKER, remote + "/worker.py")
    if rife_worker:
        c.upload(rife_worker, remote + "/relay_vsr_colab_remote.py")
    paid_start = state.get('paid_started_at', state['started_at'])
    reserve = min(480, (state['deadline'] - paid_start) * 0.15) if recavsr else 480
    runtime = json.dumps({**spec, "deadline": state["deadline"] - reserve})
    relay.execute(c, f"import json\nfrom pathlib import Path\nPath({remote+'/runtime.json'!r}).write_text({runtime!r})\nresult={{}}")
    state.update(state="running", worker_started=True, active_artifacts_preserved=False)
    h.write_json(state_file, state)
    state["worker_pid"] = remote_launch(c, remote)["pid"]
    h.write_json(state_file, state)


def start_guard(batch):
    subprocess.Popen([sys.executable, str(SELF), 'guard', '--batch', batch['batch_file']],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def allocate_and_record(c, batch, state):
    endpoint = relay.allocate(c, batch)
    file = resource(batch)
    if h.read_json(file)['state'] != 'allocating':
        raise RuntimeError('colab_allocation_stopped')
    if state.get('ccu_only'):
        claim_paid_lease(file, state, batch['max_rate'])
    state.update(state='uploading', endpoint=endpoint)
    h.write_json(file, state)


def adapter(job, batch):
    if batch.get('execution_release') and not job.get('collect_provider_job_id'):
        from drama_factory.releases import verify_release
        verify_release(batch['execution_release'], batch.get('execution_release_sha256'))
    recavsr = batch.get('finishing_backend') == finishing.ADAPTER
    worker = finishing.WORKER if recavsr else WORKER
    rife_worker = None if recavsr else Path(batch["rife_worker"])
    if (job["job_id"] != batch["job_id"] or h.digest(worker) != batch["worker_sha256"]
            or (rife_worker and h.digest(rife_worker) != batch["rife_worker_sha256"])):
        raise ValueError("job or worker snapshot changed")
    spec = job["parameters"]
    if recavsr and (job['adapter'] != finishing.ADAPTER or spec['finishing_protocol'] != 'recavsr-whole-film-v1'):
        raise ValueError("finishing backend does not match the snapshot")
    if job.get('collect_provider_job_id') and batch.get('execution_release'):
        from drama_factory.finishing import recover_local_outputs
        recovered = recover_local_outputs(job, batch)
        if recovered:
            return recovered
    c = h.Colab(batch["session"])
    state_file = resource(batch)
    directory = state_file.parent
    remote = f'/content/{batch["session"]}'
    key = batch["session"] + "/" + job["job_id"]
    source = Path(job.get("project_root", batch["project"])) / job["references"][0]
    master = None
    master_sha = spec.get('resume_master_sha256' if recavsr else 'resume_relay_sha256')
    if master_sha:
        master = Path(job.get("project_root", batch["project"])) / job["references"][1]
        if h.digest(master) != master_sha:
            raise ValueError('existing master differs from the confirmed snapshot')
    if h.digest(source) != spec["input_sha256"] or (spec.get('target_fps', 60) != spec['source_fps']
            and h.digest(batch["rife_zip"]) != spec["rife_zip_sha256"]):
        raise ValueError("source snapshot or RIFE weights changed")
    if not job.get("collect_provider_job_id"):
        assignments, usage = c.control("assignments"), c.control("usage")
        if len(assignments) >= 2 or usage["count"] >= 2:
            raise RuntimeError("two runtimes already assigned")
        if usage["balance"] < batch["max_ccu"]:
            raise RuntimeError("insufficient Colab balance")
        start = time.time()
        ccu_only = batch['max_seconds'] is None
        if ccu_only and batch['max_ccu'] * 3600 / batch['max_rate'] <= 240:
            raise RuntimeError('finishing_budget_below_closeout_reserve')
        deadline = None if ccu_only else start + min(
            batch["max_seconds"], batch["max_ccu"] / batch["max_rate"] * 3600)
        state = {"state": "allocating", "deadline": deadline, "started_at": start,
                 "active_directory": remote, "active_job": job["job_id"], "usage_before": usage,
                 "prior_assignments": len(assignments)}
        if ccu_only:
            state.update(ccu_only=True, ccu_remaining_at_reservation=batch['max_ccu'], controller_pid=os.getpid())
        h.write_json(state_file, state, exclusive=True)
        h.write_json(job["handle_path"], {"provider_job_id": key}, exclusive=True)
        start_guard(batch)
        allocate_and_record(c, batch, state)
        stage_and_start(c, remote, source, batch, spec, state_file, state, rife_worker, master)
    else:
        if job["collect_provider_job_id"] != key:
            raise ValueError("provider handle mismatch")
        state = h.read_json(state_file)
        if state['state'] == 'release_requested' and not state.get('worker_started'):
            relay.release(batch)
            raise RuntimeError('finishing_allocation_reconciled_no_submission')
        if state['state'] == 'allocating':
            if state.get('ccu_only'):
                state['controller_pid'] = os.getpid()
                h.write_json(state_file, state)
                start_guard(batch)
            allocate_and_record(c, batch, state)
        if state['state'] == 'released' and batch.get('execution_release'):
            from drama_factory.finishing import resume_allocation
            state = resume_allocation(batch, state, c, spec, existing_master=master, start_guard=start_guard)
        if state["state"] == "uploading" and not state.get("worker_started"):
            stage_and_start(c, remote, source, batch, spec, state_file, state, rife_worker, master)
        elif state.get('worker_started') and not state.get('worker_pid'):
            state['worker_pid'] = remote_launch(c, remote)['pid']
            h.write_json(state_file, state)

    while time.time() < state["deadline"] - 90:
        try:
            info = remote_status(c, batch, remote)
        except (h.H3Error, subprocess.TimeoutExpired):
            time.sleep(6)
            continue
        h.write_json(directory / "progress.json", info)
        if info["state"] == "succeeded":
            break
        if info["state"] == "failed":
            raise RuntimeError(info.get("detail", "remote worker failed"))
        time.sleep(8)
    else:
        raise TimeoutError("remote compute did not finish before collection deadline")

    state.update(progress_phase='collecting')
    h.write_json(state_file, state)
    receipt = remote_status(c, batch, remote, "receipt.json")
    h.write_json(directory / "receipt.json", receipt)
    durable_root = Path(batch["durable_root"])
    outputs, preserved, checks = [], {}, {}
    for target, name, fps in zip(job["outputs"], ("final.mp4", spec["master_filename"]),
                                 (spec.get('target_fps', 60), spec["source_fps"])):
        durable = durable_root / name
        collect_artifact(c, batch, remote + "/" + name, durable, receipt["files"][name])
        count = finishing.output_frames(spec, name) if recavsr else spec["frames"] * fps // spec["source_fps"]
        media = relay.verify_media(durable, frames=count,
                                   fps=fps, width=spec["output_width"], height=spec["output_height"],
                                   audio=name == 'final.mp4' and spec.get('has_audio', True))
        preserved[name] = {"local": str(durable), "sha256": media["sha256"]}
        checks[name] = media
        dest = Path(job["output_root"]) / name
        stage_copy(durable, dest)
        if h.digest(dest) != media["sha256"]:
            raise ValueError("staging SHA mismatch")
        outputs.append({"target": target, "source": str(dest)})

    if master is not None and receipt["files"][spec["master_filename"]]["sha256"] != master_sha:
        raise ValueError("remote master differs from the confirmed master")
    def pcm(path):
        return subprocess.check_output(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0",
                                        "-c:a", "pcm_s16le", "-f", "hash", "-hash", "sha256", "-"])
    if recavsr:
        checks['original_audio_packets'] = finishing.verify_original_audio(source, durable_root / 'final.mp4')
    else:
        if spec.get('has_audio', True) and pcm(source) != pcm(durable_root / "final.mp4"):
            raise ValueError("decoded source audio mismatch")
        checks['audio_pcm'] = "identical" if spec.get('has_audio', True) else 'no_source_audio'
    h.write_json(directory / "verification.json", checks)
    state = h.read_json(state_file)
    state.update(state="verified", preserved=preserved, active_artifacts_preserved=True,
                 completed_jobs=[job['job_id']])
    h.write_json(state_file, state)
    for name in ("worker.log", "comfy.log"):
        try:
            options = ssh_options(batch)
            if options:
                cmd = "cat " + shlex.quote(remote + "/" + name)
                result = subprocess.run([*options, "root@localhost", cmd], capture_output=True, timeout=40)
                if result.returncode == 0:
                    (directory / name).write_bytes(result.stdout)
            else:
                c.download(remote + "/" + name, directory / name)
        except (h.H3Error, subprocess.TimeoutExpired):
            pass
    return {"outputs": outputs, "provider_job_id": key}


def recover_and_release(batch):
    state = h.read_json(resource(batch))
    if state.get("state") == "released":
        return state
    if not state.get("worker_started") or state.get("active_artifacts_preserved"):
        return relay.release(batch)
    c = h.Colab(batch["session"])
    remote = state["active_directory"]
    info = remote_status(c, batch, remote)
    if info["state"] not in {"succeeded", "failed"}:
        return state
    spec = None
    receipt = None
    if batch.get('finishing_backend') == finishing.ADAPTER:
        spec = h.read_json(resource(batch).with_name('job.json'))['parameters']
        if info['state'] == 'succeeded':
            receipt = remote_status(c, batch, remote, 'receipt.json')
            h.write_json(resource(batch).with_name('receipt.json'), receipt)
            files = {'final.mp4': receipt['files']['final.mp4']}
        else:
            master = spec['master_filename']
            item = file_info(c, remote + '/' + master)
            files = {master: item} if item['sha256'] else {}
    else:
        # Historical jobs can also recover completed shots.
        files = relay.execute(c, f"""
import hashlib, json
from pathlib import Path
p = Path({remote!r})
files = [*p.glob('*.mp4'), *(p/'shots').glob('shot-???.mp4')]
result = {{}}
for f in files:
    if f.name not in ('input.mp4', 'relay-rife-1440p60-silent.mp4'):
        with f.open('rb') as source:
            result[str(f.relative_to(p))] = {{'bytes': f.stat().st_size,
                                             'sha256': hashlib.file_digest(source, 'sha256').hexdigest()}}
""")
    for name, item in files.items():
        destination = Path(batch["durable_root"]) / name
        collect_artifact(c, batch, remote + "/" + name, destination, item)
        if spec:
            fps = spec['target_fps'] if name == 'final.mp4' else spec['source_fps']
            relay.verify_media(destination, frames=finishing.output_frames(spec, name), fps=fps,
                               width=spec['output_width'], height=spec['output_height'])
        state.setdefault("preserved", {})[name] = {"local": str(destination), "sha256": item["sha256"]}
    state["active_artifacts_preserved"] = True
    h.write_json(resource(batch), state)
    return relay.release(batch)


def guard(batch):
    while True:
        state = h.read_json(resource(batch))
        if state["state"] == "released":
            return state
        deadline = paid_deadline(resource(batch), state, batch.get('max_rate'))
        if free_wait_orphaned(state) or (deadline is not None and time.time() >= deadline):
            break
        time.sleep(3)
    error = None
    try:
        if state.get("worker_started"):
            c = h.Colab(batch["session"])
            remote = state["active_directory"]
            relay.execute(c, f"""
import json, os, signal
from pathlib import Path
p = Path({remote!r})/'worker.pid'
try:
    if p.exists():
        os.killpg(int(p.read_text()), signal.SIGTERM)
except ProcessLookupError:
    pass
result = {{}}
""")
            time.sleep(5)
        result = recover_and_release(batch)
        if result.get('state') == 'released':
            return result
    except Exception as exc:
        error = type(exc).__name__
    if batch.get('execution_release'):
        # The finite deadline includes collection reserve. Failed salvage must
        # not leave a charged GPU running indefinitely beyond that contract.
        state = h.read_json(resource(batch))
        state.update(deadline_release=True, salvage_error=error or 'worker_not_terminal',
                     artifacts_may_be_missing=not state.get('active_artifacts_preserved', False))
        h.write_json(resource(batch), state)
        c = h.Colab(batch['session'])
        if not state.get('endpoint'):
            state['state'] = 'release_requested'
            h.write_json(resource(batch), state)
            endpoint = h.colab_allocation.recover(c, resource(batch))
            if endpoint:
                state['endpoint'] = endpoint
        if state.get('endpoint') in {item['endpoint'] for item in c.control('assignments')}:
            c.control('unassign', state['endpoint'])
        assignments = c.control('assignments')
        if state.get('endpoint') in {v['endpoint'] for v in assignments}:
            raise RuntimeError('deadline_release_not_confirmed')
        state.update(state='released', released_at=time.time(), assignments_after=assignments)
        h.write_json(resource(batch), state)
        return state
    if error:
        raise RuntimeError('historical_finishing_collection_failed:' + error)
    return state


def run(batch, resume=False):
    production = engine()
    root = Path(batch["project"])
    key = batch["job_id"]
    try:
        history = production._read_run_history(root, key) if batch.get('finishing_backend') == finishing.ADAPTER else []
        if history and history[-1]['status'] == 'succeeded':
            result = {'state': 'succeeded', 'job_id': key, 'outputs': history[-1]['outputs']}
        elif resume:
            result = production.collect_job(root, job_id=key, adapter_config=Path(batch["adapter_config"]))
        else:
            if batch.get('finishing_backend') == finishing.ADAPTER and resource(batch).exists():
                raise ValueError("existing finishing allocation, use finish resume")
            job = production._read_job(root, key)
            production.confirm_job(root, job_id=key, confirmation=production._preview(job)["confirmation"])
            result = production.run_job(root, job_id=key, adapter_config=Path(batch["adapter_config"]))
    except BaseException:
        if resource(batch).exists():
            try: recover_and_release(batch)
            except Exception: pass  # Durable deadline guard remains responsible.
        raise
    for output in result["outputs"]:
        path = root / output["path"]
        if h.digest(path) != output["sha256"]:
            raise ValueError("published SHA mismatch")
    if resource(batch).is_file():
        state = h.read_json(resource(batch))
        cached = Path(batch['durable_root']) / 'final.mp4'
        if batch.get('finishing_backend') == finishing.ADAPTER:
            final = result['outputs'][0]
            recorded = state.get('preserved', {}).get('final.mp4')
            if recorded and recorded['sha256'] == final['sha256'] and recorded['local'] == str(cached):
                recorded['local'] = str(root / final['path'])
                h.write_json(resource(batch), state)
        if state.get('state') != 'released':
            relay.release(batch)
        if batch.get('finishing_backend') == finishing.ADAPTER and cached.is_file() and (
                h.digest(cached) == result['outputs'][0]['sha256']):
            cached.unlink()
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["adapter", "run", "resume", "guard", "release", "status"])
    p.add_argument("--batch", required=True)
    args = p.parse_args()
    batch = h.read_json(args.batch)
    from drama_factory import RELEASE
    from drama_factory.releases import dispatch_archived
    if batch.get('execution_release') and batch['execution_release'] != RELEASE:
        dispatch_archived(batch['execution_release'], batch.get('execution_release_sha256'),
                          'scripts/relay_vsr_comfy_colab.py', sys.argv[1:])
    if not batch.get('execution_release'):
        archive = h.ROOT / '.local/factory-releases/pre-factory-20261113/scripts'
        if archive.exists() and h.digest(archive / 'relay_vsr_comfy_remote.py') == batch.get('worker_sha256'):
            import os
            os.execv(sys.executable, [sys.executable, str(archive / SELF.name), *sys.argv[1:]])
    try:
        if args.command == "adapter":
            result = adapter(json.load(sys.stdin), batch)
        elif args.command in {"run", "resume"}:
            file = resource(batch)
            with watch(f"{Path(batch['project']).name} · {batch['job_id']}", file.parent,
                       lambda: [resource_snapshot(file, 1, engine_progress=file.with_name('progress.json'))]):
                result = run(batch, resume=args.command == "resume")
        elif args.command == "release":
            result = recover_and_release(batch)
        elif args.command == "guard":
            result = guard(batch)
        else:
            result = h.read_json(resource(batch)) if resource(batch).is_file() else {'state': 'prepared_no_gpu'}
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        name = "adapter-error.json" if args.command == "adapter" else "last-error.json"
        h.write_json(resource(batch).parent / name, {"type": type(exc).__name__, "detail": str(exc)})
        raise


if __name__ == "__main__":
    main()
