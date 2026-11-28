from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from . import RELEASE, assets, core, delivery, finishing, planning, releases, runtime, skill_routes, stages


def _scope(parser, *, episode=False):
    parser.add_argument('project', type=Path)
    if episode:
        parser.add_argument('--episode', required=True)


def parser():
    root = argparse.ArgumentParser(description='短剧工厂：单集规格、阶段工单、排产、恢复与交付')
    groups = root.add_subparsers(dest='group', required=True)
    skills = groups.add_parser('skills', help='读取指定制作步骤的技能和必读资料')
    skill_query = skills.add_mutually_exclusive_group(required=True)
    skill_query.add_argument('--step', type=int, choices=skill_routes.STEPS)
    skill_query.add_argument('--check', action='store_true', help='核对全部 18 步的资料路径')
    episode = groups.add_parser('episode').add_subparsers(dest='command', required=True)
    for name in ('plan', 'status', 'spec', 'work', 'complete', 'repair', 'impact'):
        p = episode.add_parser(name); _scope(p, episode=True)
        if name in ('plan', 'status'):
            p.add_argument('--goal', choices=planning.GOALS, default='clips')
            p.add_argument('--entry', action='append'); p.add_argument('--max-instances', type=int, default=2)
        if name == 'spec':
            p.add_argument('--override', type=Path); p.add_argument('--authorization')
            p.add_argument('--freeze', action='store_true')
        if name == 'work': p.add_argument('--stage', required=True, choices=[s[0] for s in planning.STAGES])
        if name == 'complete':
            p.add_argument('--order', required=True); p.add_argument('--evidence', action='append', required=True)
            p.add_argument('--observations', required=True)
        if name == 'repair':
            p.add_argument('--category', required=True, choices=stages.REPAIR_OWNERS)
            p.add_argument('--locator', required=True); p.add_argument('--observation', required=True)
        if name == 'impact':
            p.add_argument('--before', required=True, type=Path); p.add_argument('--after', required=True, type=Path)
    task = groups.add_parser('task').add_subparsers(dest='command', required=True)
    for name in ('create', 'prepare', 'run', 'resume', 'status', 'reconcile', 'stop', 'extend', 'upgrade-execution'):
        p = task.add_parser(name); _scope(p); p.add_argument('--id', required=True)
        if name in ('create', 'prepare'): p.add_argument('--plan', type=Path, required=True)
        if name == 'prepare':
            p.add_argument('--retain-runtime', action='store_true')
            reuse = p.add_mutually_exclusive_group()
            reuse.add_argument('--reuse-runtime', action='store_true')
            reuse.add_argument('--retry-allocation', action='store_true')
        if name == 'create':
            p.add_argument('--authorization', required=True); p.add_argument('--max-ccu', required=True, type=float)
            p.add_argument('--max-seconds', type=int, help=argparse.SUPPRESS)
        if name in ('prepare', 'stop'): p.add_argument('--reason', required=True)
        if name == 'extend':
            p.add_argument('--authorization', required=True); p.add_argument('--max-ccu', required=True, type=float)
            amendment = p.add_mutually_exclusive_group()
            amendment.add_argument('--additional-seconds', type=int)
            amendment.add_argument('--ccu-only', action='store_true')
            p.add_argument('--from-now', action='store_true', help='Start the approved interval now after an expired deadline.')
        if name == 'upgrade-execution': p.add_argument('--authorization', required=True)
    document = groups.add_parser('document').add_subparsers(dest='command', required=True)
    for name in ('inspect', 'revise', 'recover'):
        p = document.add_parser(name); _scope(p)
        if name == 'inspect': p.add_argument('--path', required=True)
        if name == 'revise': p.add_argument('--request', type=Path, required=True)
        if name == 'recover': p.add_argument('--revision'); p.add_argument('--rollback', action='store_true')
    asset = groups.add_parser('asset').add_subparsers(dest='command', required=True)
    for name in ('call', 'register', 'observe', 'select'):
        p = asset.add_parser(name); _scope(p, episode=True)
        if name in ('call', 'register'): p.add_argument('--entry', required=True)
        if name == 'call':
            p.add_argument('--action', choices=('prepare', 'dispatch', 'result', 'failed', 'retry'), default='prepare')
            p.add_argument('--result-path'); p.add_argument('--receipt'); p.add_argument('--failure')
            p.add_argument('--failure-kind', choices=('technical', 'policy')); p.add_argument('--reason')
        if name == 'register':
            p.add_argument('--path', required=True); p.add_argument('--purpose', required=True)
            p.add_argument('--provenance', required=True)
        if name in ('observe', 'select'): p.add_argument('--version', required=True)
        if name == 'observe':
            p.add_argument('--result', required=True, choices=('passed', 'failed'))
            p.add_argument('--method', required=True, choices=('actual_image_view', 'user_image_review'))
            p.add_argument('--notes', required=True)
        if name == 'select': p.add_argument('--slot', action='append', required=True); p.add_argument('--authorization', required=True)
    timeline = groups.add_parser('timeline').add_subparsers(dest='command', required=True)
    for name in ('prepare', 'readback', 'diff', 'actions', 'action', 'verify'):
        p = timeline.add_parser(name); _scope(p, episode=True)
        if name == 'readback':
            p.add_argument('--settings', type=Path, required=True); p.add_argument('--snapshot', type=Path, required=True)
            p.add_argument('--details', type=Path, required=True)
            p.add_argument('--media', type=Path); p.add_argument('--scene-source', type=Path)
        if name in ('diff', 'actions'):
            p.add_argument('--timeline', type=Path, required=True); p.add_argument('--readback', type=Path, required=True)
            p.add_argument('--bindings', type=Path, required=True)
        if name == 'actions': p.add_argument('--expected-readback-sha', required=True)
        if name == 'action':
            p.add_argument('--packet', required=True); p.add_argument('--action-id', required=True)
            p.add_argument('--state', choices=('status', 'dispatch', 'completed', 'failed'), default='status')
            p.add_argument('--receipt', type=Path); p.add_argument('--readback', type=Path)
        if name == 'verify': p.add_argument('--path', required=True); p.add_argument('--source-audio')
    finish = groups.add_parser('finish').add_subparsers(dest='command', required=True)
    p = finish.add_parser('prepare'); _scope(p, episode=True)
    p.add_argument('--id', required=True); p.add_argument('--source', required=True)
    p.add_argument('--short-edge', default=2160, type=int, help='Target short edge in pixels, default 2160')
    p.add_argument('--fps', default=60, type=int); p.add_argument('--cuts', type=Path, required=True)
    p.add_argument('--cuts-evidence', required=True); p.add_argument('--authorization', required=True)
    p.add_argument('--max-ccu', required=True, type=float); p.add_argument('--max-seconds', type=int, help=argparse.SUPPRESS)
    p.add_argument('--rife-zip', type=Path)
    p.add_argument('--master', help='Reuse a verified ReCaVSR master with matching source and frame clock')
    for name in ('run', 'resume', 'status', 'release'):
        p = finish.add_parser(name); p.add_argument('--batch', required=True, type=Path)
    release = groups.add_parser('release').add_subparsers(dest='command', required=True)
    release.add_parser('verify')
    return root


def execute(a):
    group = a.group
    if group == 'skills':
        if a.check:
            return {'bundled_drama_skills': skill_routes.verify_bundle(),
                    'steps': [skill_routes.resolve(step) for step in skill_routes.STEPS]}
        return skill_routes.resolve(a.step, with_text=True)
    command = a.command
    if group == 'task' and command in ('run', 'resume', 'stop'):
        path = runtime.task_path(core.project(a.project), a.id)
        release, sha = runtime.execution_release(path, core.read_json(path))
        if release != RELEASE:
            releases.dispatch_archived(release, sha,
                                       'scripts/drama-factory.py', sys.argv[1:])
    if group == 'episode':
        if command in ('plan', 'status'):
            planning_error = None
            try:
                value = planning.plan(a.project, a.episode, goal=a.goal, only=a.entry,
                                      max_instances=a.max_instances, persist=command == 'plan')
            except (OSError, ValueError) as exc:
                if command != 'status':
                    raise
                planning_error = str(exc)
                value = {'spec': None, 'stages': planning.stage_orders(core.project(a.project), a.episode, a.goal)}
            if command == 'plan':
                return {'plan': str(core.project(a.project) / core.LEDGER / 'plans' / value['plan_id'] / 'plan.json'),
                        'id': value['plan_id'], 'motions': len(value['motions']),
                        'counts': value.get('counts', {}),
                        'schedule': value['schedule'], 'stages': value['stages']}
            tasks = [runtime.reconcile(a.project, core.read_json(p)['task_id'])
                     for p in (core.project(a.project) / core.LEDGER / 'colab').glob('task-*/task.json')
                     if core.read_json(p).get('schema_version') == 2 and core.read_json(p)['episode'] == a.episode]
            return {'episode': a.episode, 'spec': value['spec'], 'stages': value['stages'], 'tasks': tasks,
                    'stage_evidence': stages.status(a.project, a.episode),
                    'planning_error': planning_error,
                    'quality': '查看实际阶段回执；计划与文件存在不代表视听验收'}
        if command == 'spec':
            override = core.read_json(a.override) if a.override else None
            if a.freeze:
                if not a.authorization: raise core.FactoryError('freeze_requires_current_authorization')
                return core.freeze_spec(a.project, a.episode, override=override, authorization=a.authorization)
            return core.resolve_spec(a.project, a.episode, override=override, authorization=a.authorization)
        if command == 'work': return stages.work_order(a.project, a.episode, a.stage)
        if command == 'complete': return stages.complete(a.project, a.order, evidence=a.evidence, observations=a.observations)
        if command == 'repair': return stages.finding(a.project, a.episode, category=a.category, locator=a.locator, observation=a.observation)
        if command == 'impact': return planning.impact(core.read_json(a.before), core.read_json(a.after))
    if group == 'task':
        if command == 'create': return runtime.create(a.project, a.id, core.read_json(a.plan), authorization=a.authorization,
                                                       max_ccu=a.max_ccu)
        if command == 'prepare':
            return runtime.append_wave(a.project, a.id, core.read_json(a.plan), reason=a.reason,
                                       retain_runtime=a.retain_runtime, reuse_runtime=a.reuse_runtime,
                                       retry_allocation=a.retry_allocation)
        if command in ('run', 'resume'): return runtime.run(a.project, a.id, resume=command == 'resume')
        if command in ('status', 'reconcile'): return runtime.reconcile(a.project, a.id, online=command == 'reconcile')
        if command == 'stop': return runtime.stop(a.project, a.id, reason=a.reason)
        if command == 'extend': return runtime.extend(a.project, a.id, max_ccu=a.max_ccu,
            additional_seconds=a.additional_seconds, authorization=a.authorization, from_now=a.from_now,
            ccu_only=a.ccu_only or a.additional_seconds is None)
        if command == 'upgrade-execution':
            return runtime.upgrade_execution(a.project, a.id, authorization=a.authorization)
    if group == 'document':
        if command == 'inspect':
            p = core.project_path(a.project, a.path, exists=True)
            return {'path': a.path, 'sha256': core.file_sha(p), 'entries': core.sections(p.read_text())}
        if command == 'revise':
            request = core.read_json(a.request)
            return core.revise(a.project, request['changes'], request['expected'], reason=request['reason'])
        return core.recover_revisions(a.project, revision=a.revision, rollback=a.rollback)
    if group == 'asset':
        if command == 'call': return assets.image_call(a.project, a.episode, a.entry, action=a.action,
            result_path=a.result_path, tool_receipt=a.receipt, failure=a.failure,
            failure_kind=a.failure_kind, reason=a.reason)
        if command == 'register': return assets.register(a.project, a.episode, entry=a.entry, path=a.path,
            purpose=a.purpose, provenance=a.provenance)
        if command == 'observe': return assets.observe(a.project, a.episode, a.version, result=a.result, method=a.method, notes=a.notes)
        return assets.select(a.project, a.episode, a.version, slots=a.slot, authorization=a.authorization)
    if group == 'timeline':
        if command == 'prepare': return delivery.compile_timeline(a.project, a.episode)
        if command == 'readback': return delivery.openchatcut_readback(core.read_json(a.settings), core.read_json(a.snapshot),
            core.read_json(a.details), media=core.read_json(a.media) if a.media else None,
            scene_source=a.scene_source.read_text() if a.scene_source else None)
        if command == 'diff': return delivery.compare(core.read_json(a.timeline), core.read_json(a.readback), core.read_json(a.bindings))
        if command == 'actions': return delivery.prepare_exchange(a.project, a.episode, core.read_json(a.timeline),
            core.read_json(a.readback), core.read_json(a.bindings), expected_readback_sha=a.expected_readback_sha)
        if command == 'action': return delivery.exchange_action(a.project, a.packet, a.action_id, state=a.state,
            receipt=a.receipt, readback=core.read_json(a.readback) if a.readback else None)
        return delivery.verify_file(a.project, a.episode, a.path, source_audio=a.source_audio)
    if group == 'finish':
        if command == 'prepare': return finishing.prepare(a.project, a.episode, a.id, source=a.source, target_fps=a.fps,
            cuts=core.read_json(a.cuts), authorization=a.authorization, max_ccu=a.max_ccu,
            cuts_evidence=a.cuts_evidence, short_edge=a.short_edge, rife_zip=a.rife_zip, master=a.master)
        batch = core.read_json(a.batch)
        release = batch.get('execution_release')
        if release and release != RELEASE:
            releases.dispatch_archived(release, batch.get('execution_release_sha256'),
                                       'scripts/relay_vsr_comfy_colab.py', [command, '--batch', str(a.batch.resolve())])
        runner = core.ROOT / 'scripts/relay_vsr_comfy_colab.py'
        if not release and (a.batch.parent / 'job.json').is_file():
            job = core.read_json(a.batch.parent / 'job.json')
            if job.get('adapter') == 'recavsr-comfy-colab':
                runs = (core.ROOT / '.local' / name for name in ('recavsr-full', 'recavsr-test'))
                saved = next((path for path in runs if (path / 'recavsr_remote.py').is_file() and
                              core.file_sha(path / 'recavsr_remote.py') == batch['worker_sha256']), None)
                if saved is None:
                    raise core.FactoryError('original_recavsr_worker_required_for_historical_batch')
                runner = saved / 'control.py'
                if command == 'status':
                    state = Path(batch['state_file'])
                    return core.read_json(state) if state.is_file() else {'state': 'prepared_no_gpu'}
        result = subprocess.run([sys.executable, str(runner), command, '--batch', str(a.batch.resolve())],
                                stdout=subprocess.PIPE, text=True)
        if result.returncode: raise core.FactoryError('finishing_command_failed_see_original_ledger')
        return json.loads(result.stdout)
    if group == 'release': return releases.verify_release(RELEASE)
    raise core.FactoryError('unknown_command')


def main():
    p = parser()
    try:
        result = execute(p.parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        p.exit(1, '工厂入口：' + str(exc) + '\n')
