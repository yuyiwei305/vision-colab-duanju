"""Host work orders and evidence: reads do not become invented content acceptance."""
import subprocess
import sys
import time
import uuid

from .core import (LEDGER, ROOT, FactoryError, document_hashes, file_sha, fingerprint,
                   project, project_path, read_json, write_json)
from .planning import stage_orders
from .skill_routes import resolve


def work_order(root, episode, stage):
    root = project(root)
    order = next((r for r in stage_orders(root, episode, 'delivery') if r['stage'] == stage), None)
    if order is None:
        raise FactoryError('unknown_stage')
    value = {**order, **resolve(order['sop_step']), 'episode': episode, 'project': str(root),
             'input_revisions': document_hashes(root, episode),
             'execution': 'current_host_agent', 'quality': 'not_reviewed_by_cli'}
    value['id'] = fingerprint(value)
    write_json(project_path(root, LEDGER / 'work-orders' / (value['id'] + '.json')), value)
    return value


def complete(root, order_id, *, evidence, observations, actor='executor'):
    root = project(root)
    order = read_json(project_path(root, LEDGER / 'work-orders' / (order_id + '.json'), exists=True))
    if not evidence or not observations:
        raise FactoryError('real_stage_evidence_and_observations_required')
    files = [{'path': p, 'sha256': file_sha(project_path(root, p, exists=True))} for p in evidence]
    if order['output'] and order['output'] not in evidence:
        raise FactoryError('formal_stage_output_required')
    for path in order['inputs']:
        if file_sha(project_path(root, path, exists=True)) != order['input_revisions'].get(path):
            raise FactoryError('stage_input_changed_refresh_work_order:' + path)
    check = None
    if order['output'] and order['stage'] != 'edit':
        checked = subprocess.run([sys.executable, str(ROOT / 'ref/drama-skills/skills/short-drama/scripts/creator_markdown_check.py'),
            str(root / '剧集' / order['episode']), '--partial'], capture_output=True, text=True)
        check = {'returncode': checked.returncode, 'output': checked.stdout + checked.stderr}
        if checked.returncode:
            raise FactoryError('formal_document_check_failed:' + check['output'][-3000:])
    if order['stage'] == 'edit':
        from .delivery import edit_parser
        parser = edit_parser()
        cut_list = project_path(root, order['output'], exists=True)
        try:
            _, cuts, unused = parser.parse_cut_list(cut_list)
        except parser.EditError as exc:
            raise FactoryError(str(exc)) from exc
        _, errors = parser.sequence_cut_layout(cut_list.parent, cuts, unused)
        if errors:
            raise FactoryError('formal_document_check_failed:' + '\n'.join(errors))
        check = {'returncode': 0, 'output': '剪辑单及段落来源结构检查'}
    value = {'id': uuid.uuid4().hex, 'order_id': order_id, 'stage': order['stage'], 'episode': order['episode'],
             'evidence': files, 'observations': observations, 'actor': actor, 'structure_check': check,
             'input_revisions': {p: order['input_revisions'].get(p) for p in order['inputs']},
             'asserts_user_approval': False, 'at': time.time()}
    write_json(project_path(root, LEDGER / 'stage-receipts' / (value['id'] + '.json')), value, exclusive=True)
    return value


def status(root, episode):
    root = project(root)
    receipts = []
    for path in (root / LEDGER / 'stage-receipts').glob('*.json'):
        receipt = read_json(path)
        if receipt['episode'] != episode:
            continue
        hashes = {**receipt.get('input_revisions', {}), **{e['path']: e['sha256'] for e in receipt['evidence']}}
        current = all(project_path(root, p).is_file() and file_sha(root / p) == sha for p, sha in hashes.items())
        receipts.append({**receipt, 'current': current})
    findings = [read_json(p) for p in (root / LEDGER / 'findings').glob('*.json')]
    return {'receipts': receipts, 'findings': [f for f in findings if f['episode'] == episode]}


REPAIR_OWNERS = {'story': 'short-drama-write', 'visual': 'short-drama-assets',
                 'staging': 'short-drama-storyboard', 'prompt': 'short-drama-video-prompts',
                 'generation': 'short-drama-produce', 'transport': 'task resume',
                 'subtitle': 'short-drama-edit', 'edit': 'short-drama-edit',
                 'finishing': 'finish resume', 'review': 'short-drama-review'}


def finding(root, episode, *, category, locator, observation):
    if category not in REPAIR_OWNERS or not locator or not observation:
        raise FactoryError('specific_finding_category_locator_and_observation_required')
    value = {'id': uuid.uuid4().hex, 'episode': episode, 'category': category, 'locator': locator,
             'observation': observation, 'owner': REPAIR_OWNERS[category], 'state': 'open', 'at': time.time()}
    write_json(project_path(project(root), LEDGER / 'findings' / (value['id'] + '.json')), value, exclusive=True)
    return value
