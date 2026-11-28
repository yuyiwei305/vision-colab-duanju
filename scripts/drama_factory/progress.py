"""Emit progress even while the production command is blocked on provider I/O."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .core import read_json

INTERVAL_SECONDS = 600


def resource_snapshot(resource, total, *, engine_progress=None):
    resource = Path(resource)
    state = read_json(resource) if resource.is_file() else {}
    phase = state.get('progress_phase', state.get('state', 'preparing'))
    snapshot = {'phase': phase, 'collected': len(state.get('completed_jobs', [])), 'total': total}
    if state.get('state') in ('released', 'release_requested', 'release_unverified', 'transferred', 'paused_recoverable'):
        snapshot['phase'] = state['state']
    elif state.get('state') == 'allocating' and not state.get('endpoint'):
        request_file = resource.with_name('allocation-request.json')
        request = read_json(request_file) if request_file.is_file() else {}
        event = (request.get('events') or [{}])[-1]
        snapshot.update(phase='waiting_for_gpu', wait_seconds=max(0, int(time.time() - state['started_at'])))
        if 'status' in event:
            snapshot['provider_http_status'] = event['status']
    if state.get('progress_job') and snapshot['phase'] != 'waiting_for_gpu':
        snapshot['current_job'] = state['progress_job']
    if engine_progress and Path(engine_progress).is_file() and snapshot['phase'] != 'waiting_for_gpu':
        worker = read_json(engine_progress)
        for key in ('state', 'phase', 'progress', 'percent', 'frames_done', 'frames_total'):
            if key in worker:
                snapshot.setdefault('worker', {})[key] = worker[key]
    return snapshot


def _notify(title, message):
    if sys.platform != 'darwin' or os.environ.get('DRAMA_PROGRESS_DESKTOP') == '0':
        return
    subprocess.run(
        ['/usr/bin/osascript', '-e',
         'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run',
         title, message],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=8, check=False)


def _message(rows):
    phases = {'preparing': '准备中', 'waiting_for_gpu': '等待 GPU', 'bootstrapping': '加载模型',
              'generating': '生成中', 'collecting': '取回中', 'uploading': '上传素材',
              'released': '实例已释放', 'paused_recoverable': '待恢复',
              'release_requested': '正在释放实例', 'release_unverified': '待核实释放',
              'ready': '已就绪', 'transferred': '实例已交接'}
    parts = []
    for row in rows:
        part = f"{row['collected']}/{row['total']} 已取回 · {phases.get(row['phase'], row['phase'])}"
        if row.get('current_job'):
            part += f" · {row['current_job']}"
        if 'wait_seconds' in row:
            part += f" {row['wait_seconds'] // 60} 分钟"
        if row.get('worker', {}).get('phase'):
            part += f" · {row['worker']['phase']}"
        parts.append(part)
    return ' / '.join(parts)


@contextmanager
def watch(title, directory, snapshot, *, interval=INTERVAL_SECONDS):
    """Keep the machine-readable command result on stdout and push reports separately."""
    stop = threading.Event()
    journal = Path(directory) / 'progress-events.jsonl'
    start = time.monotonic()

    def report():
        next_report = start + interval
        while not stop.wait(max(0, next_report - time.monotonic())):
            next_report += interval
            try:
                rows = snapshot()
                event = {'at': time.time(), 'elapsed_seconds': int(time.monotonic() - start),
                         'task': title, 'batches': rows, 'message': _message(rows)}
                line = json.dumps(event, ensure_ascii=False)
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(line + '\n')
                print(line, file=sys.stderr, flush=True)
                _notify(title, event['message'])
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                print(f'{title} 进度报告暂不可用 ({type(error).__name__})', file=sys.stderr, flush=True)

    thread = threading.Thread(target=report, name='factory-progress', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=9)
