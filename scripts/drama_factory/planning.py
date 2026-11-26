"""Compile fixed stage contracts and dependency-preserving finite H3 waves."""
from __future__ import annotations

from pathlib import Path
import re

from . import RELEASE
from .core import (DOCUMENTS, LEDGER, ROOT, FactoryError, document_hashes, field,
                   file_sha, fingerprint, project, project_path, resolve_spec,
                   sections, write_json)

from .skill_routes import skill_names

# The host executes these skills; CLI existence checks never claim creative acceptance.
STAGES = (
    ('develop', None, (), 3),
    ('write', '剧本.md', (), 4),
    ('assets', '视觉设定.md', ('剧本.md',), 5),
    ('storyboard', '分镜.md', ('剧本.md', '视觉设定.md'), 6),
    ('image-prompts', '图片提示词.md', ('视觉设定.md', '分镜.md'), 7),
    ('images', None, ('图片提示词.md',), 7),
    ('video-prompts', '视频提示词.md', ('剧本.md', '视觉设定.md', '分镜.md'), 9),
    ('review', None, DOCUMENTS[:5], 10),
    ('clips', None, ('视频提示词.md',), 11),
    ('edit', '剪辑单.md', DOCUMENTS[:5], 13),
    ('delivery', None, ('剪辑单.md',), 18),
)
STAGE_OWNERS = {'images': 'short-drama-produce'}
GOALS = {'script': 'write', 'assets': 'images', 'prompts': 'video-prompts', 'clips': 'clips', 'delivery': 'delivery'}


def stage_orders(root, episode, goal):
    if goal not in GOALS:
        raise FactoryError('unsupported_goal')
    directory = Path('剧集') / episode
    result = []
    for name, output, inputs, step in STAGES:
        skills = skill_names(step)
        paths = [directory / value for value in inputs]
        missing = [str(p) for p in paths if not project_path(root, p).is_file()]
        exists = bool(output and project_path(root, directory / output).is_file())
        result.append({'stage': name, 'state': 'missing_inputs' if missing else
                       ('artifact_exists_review_required' if exists else 'host_work_required'),
                       'owner': STAGE_OWNERS.get(name, skills[2]), 'skills': skills, 'sop_step': step,
                       'inputs': [str(p) for p in paths], 'missing': missing,
                       'output': str(directory / output) if output else None,
                       'checks': '正式文档检查与实际内容观察分开登记；文件存在不代表验收'})
        if name == GOALS[goal]:
            break
    return result


def _topological(nodes, dependencies, order):
    remaining = set(nodes)
    result = []
    while remaining:
        ready = sorted((n for n in remaining if not (set(dependencies[n]) & remaining)), key=order.__getitem__)
        if not ready:
            raise FactoryError('dependency_cycle:' + ','.join(sorted(remaining)))
        result.extend(ready)
        remaining.difference_update(ready)
    return result


def motion_graph(root, episode, spec, only=None):
    import colab_h3 as h
    import h3_workflow_v4 as v4
    production = h.engine()
    source = f'剧集/{episode}/视频提示词.md'
    text = project_path(root, source, exists=True).read_text()
    from h3_shot_contract import validate
    board = project_path(root, f'剧集/{episode}/分镜.md')
    storyboard = board.read_text() if board.is_file() else None
    blocks = sections(text)
    entries = list(blocks)
    if not entries or any(not re.fullmatch(f'MOTION-{episode}-' + r'\d{3}', e) for e in entries):
        raise FactoryError('unique_episode_motion_entries_required')
    order = {entry: index for index, entry in enumerate(entries)}
    chains, dependencies, voices, source_routes = [], {}, {}, []
    for entry, block in blocks.items():
        parent = field(block, '续接来源')
        if parent == '无':
            chains.append([entry])
        elif parent in order and order[parent] == order[entry] - 1:
            chains[-1].append(entry)
        else:
            raise FactoryError('unresolved_or_nonadjacent_continuation:' + entry)
        dependencies[entry] = [] if parent == '无' else [parent]
        recipes = field(block, '原生音色参考')
        voice_sources = []
        if recipes:
            import h3_native_voice
            parsed = h3_native_voice.parse(block, {e: e for e in entries})
            voice_sources = [r['source_job'] for r in parsed]
            dependencies[entry].extend(voice_sources)
        voices[entry] = voice_sources
        source_routes.append((entry, parent, v4.select_scene(block, production._copyable_prompt(block))))
    routes = v4.route_chains(source_routes)
    # Validate cycles before closure and combine voice-connected chains into atomic groups.
    _topological(entries, dependencies, order)
    chain_of = {entry: index for index, chain in enumerate(chains) for entry in chain}
    chain_dependencies = {index: set() for index in range(len(chains))}
    for entry, sources in voices.items():
        for dep in sources:
            if chain_of[entry] == chain_of[dep]:
                if order[dep] >= order[entry]:
                    raise FactoryError('voice_source_must_precede_within_chain:' + entry)
            else:
                chain_dependencies[chain_of[entry]].add(chain_of[dep])
    chain_order = _topological(list(chain_dependencies), chain_dependencies,
                               {i: order[c[0]] for i, c in enumerate(chains)})
    selected = set(entries if only is None else only)
    if not selected or not selected <= set(entries) or (only is not None and len(only) != len(selected)):
        raise FactoryError('invalid_selected_entries')
    requested = set(selected)
    # Never split an AV chain: scene routing and latent lineage belong to the whole chain.
    while True:
        previous = set(selected)
        for entry in list(selected):
            selected.update(dependencies[entry])
            selected.update(chains[chain_of[entry]])
        if previous == selected:
            break
    coverage = validate(storyboard, text, only=[e for e in entries if e in selected])
    components = [{index} for index in chain_order if any(e in selected for e in chains[index])]
    for entry in selected:
        for dep in voices[entry]:
            first = next(c for c in components if chain_of[entry] in c)
            second = next(c for c in components if chain_of[dep] in c)
            if first is not second:
                first.update(second)
                components.remove(second)
    groups = [[e for index in chain_order if index in component for e in chains[index]] for component in components]
    if any(len(group) > 32 for group in groups):
        raise FactoryError('dependency_group_exceeds_32_batch_capacity; use already-reviewed_saved_WAV_for_cross_batch_voice')
    ratio = '16:9' if spec['production']['native_canvas'] == [1344, 768] else '9:16'
    capabilities = v4.reference_capabilities()
    nodes = []
    for group in groups:
        for entry in group:
            block = blocks[entry]
            raw_duration = field(block, '时长', default='')
            if not re.fullmatch(r'\d+s', raw_duration):
                raise FactoryError('integer_duration_required:' + entry)
            parent = field(block, '续接来源')
            seed_text = field(block, 'Seed', default=str(2026110940 + order[entry]))
            if not re.fullmatch(r'\d+', seed_text):
                raise FactoryError('nonnegative_integer_seed_required:' + entry)
            params = v4.parameters(routes[entry]['scene_profile'], int(seed_text),
                                   int(raw_duration[:-1]), ratio, None if parent == '无' else parent,
                                   field(block, 'H3上下文', default='5v24a'))
            images = production._markdown_reference_bindings(block, field_name='输入参考图')
            audio = production._markdown_audio_bindings(block)
            if not v4.reference_counts_fit(capabilities, len(images), len(audio) + len(voices[entry])):
                raise FactoryError('reference_capacity_exceeded:' + entry)
            refs = [{**b, 'sha256': file_sha(project_path(root, b['path'], exists=True))} for b in images + audio]
            nodes.append({'entry': entry, 'source': source, 'source_index': order[entry],
                          'prompt': production._copyable_prompt(block),
                          'content_sha256': fingerprint(block.strip().encode()),
                          'dependencies': dependencies[entry],
                          'voice_sources': voices[entry],
                          'native_voice_recipes': h3_native_voice.parse(block, {e: e for e in entries}) if voices[entry] else [],
                          'parameters': params,
                          'references': refs, 'routing': routes[entry]})
            if entry in coverage:
                nodes[-1]['coverage'] = coverage[entry]
            if not coverage.get(entry, {}).get('sequence'):
                nodes[-1]['shot'] = field(block, '分镜')
    return nodes, groups, sorted(selected - requested)


def partition(groups, *, max_instances=2):
    if type(max_instances) is not int or not 1 <= max_instances <= 2:
        raise FactoryError('supported_instance_limit_is_1_or_2')
    count = sum(map(len, groups))
    requested = 1 if count < 20 else 2 if count < 40 else 4
    actual = min(requested, max_instances, len(groups)) if groups else 0
    # Pack balanced lanes; a lane can emit several <=32-entry sequential batches.
    lanes = [[] for _ in range(actual)]
    for group in sorted(groups, key=len, reverse=True):
        target = min(range(actual), key=lambda i: sum(len(g) for g in lanes[i]))
        lanes[target].append(group)
    batches = []
    for lane, units in enumerate(lanes):
        current = []
        ordinal = 0
        for group in units:
            if len(current) + len(group) > 32:
                batches.append({'lane': lane, 'sequence': ordinal, 'entries': current})
                ordinal += 1
                current = []
            current.extend(group)
        if current:
            batches.append({'lane': lane, 'sequence': ordinal, 'entries': current})
    return {'requested_instances': requested if count else 0, 'instances': actual, 'batches': batches,
            'reason': ('锁定调度器最多2台；并发再受完整依赖组数量与本次上限限制' if actual < requested else '符合段数档位')}


def plan(root, episode, *, goal='clips', only=None, override=None, authorization=None, max_instances=2, persist=True):
    root = project(root)
    spec = resolve_spec(root, episode, override=override, authorization=authorization)
    orders = stage_orders(root, episode, goal)
    sources = document_hashes(root, episode)
    nodes, groups, added = [], [], []
    video_path = project_path(root, f'剧集/{episode}/视频提示词.md')
    if goal in ('clips', 'delivery') and video_path.is_file():
        nodes, groups, added = motion_graph(root, episode, spec, only)
    result = {'schema_version': 1, 'release': RELEASE, 'project': str(root), 'episode': episode,
              'goal': goal, 'spec': spec, 'sources': sources, 'stages': orders, 'motions': nodes,
              'dependency_added_entries': added, 'schedule': partition(groups, max_instances=max_instances)}
    if nodes:
        result['shot_motions'] = {}
        for node in nodes:
            mapped = node.get('coverage', {}).get('shots')
            for shot in ([span['shot'] for span in mapped] if mapped is not None else [node['shot']]):
                result['shot_motions'].setdefault(shot, []).append(node['entry'])
        result['sequences'] = {}
        for node in nodes:
            coverage = node.get('coverage', {})
            if 'sequence' in coverage:
                result['sequences'].setdefault(coverage['sequence'], []).append(node['entry'])
        result['counts'] = {'shot_entries': len(result['shot_motions']), 'generation_segments': len(nodes),
                            'continuation_chains': sum(n['parameters']['continue_from_job'] is None for n in nodes)}
    result['plan_id'] = fingerprint(result)
    if persist:
        directory = project_path(root, LEDGER / 'plans' / result['plan_id'])
        path = directory / 'plan.json'
        if path.exists():
            from .core import read_json
            if read_json(path) != result:
                raise FactoryError('plan_digest_collision_or_tampering')
        else:
            write_json(path, result, exclusive=True)
        # Full document and effective-spec snapshots, not hashes alone.
        for name, sha in sources.items():
            target = directory / 'sources' / name
            raw = project_path(root, name).read_bytes()
            if fingerprint(raw) != sha:
                raise FactoryError('sources_changed_during_plan')
            from .core import atomic_bytes
            if not target.exists():
                atomic_bytes(target, raw, exclusive=True)
        write_json(directory / 'spec.json', spec)
    return result


def validate_plan(root, value):
    if value.get('project') != str(project(root)):
        raise FactoryError('plan_project_mismatch')
    body = {k: v for k, v in value.items() if k != 'plan_id'}
    if fingerprint(body) != value.get('plan_id'):
        raise FactoryError('plan_integrity_mismatch')
    if document_hashes(root, value['episode']) != value['sources']:
        raise FactoryError('plan_sources_changed')
    if resolve_spec(root, value['episode']) != value['spec']:
        raise FactoryError('freeze_task_spec_before_prepare')
    for node in value['motions']:
        for binding in node['references']:
            if file_sha(project_path(root, binding['path'], exists=True)) != binding['sha256']:
                raise FactoryError('plan_reference_changed:' + binding['path'])
    return value


def impact(previous, current):
    """Exact dependency propagation; no inferred visual approval from text heuristics."""
    old = {n['entry']: n for n in previous.get('motions', [])}
    new = {n['entry']: n for n in current.get('motions', [])}
    changed = {entry for entry in old.keys() | new.keys() if old.get(entry) != new.get(entry)}
    while True:
        before = set(changed)
        changed.update(e for e, n in new.items() if set(n['dependencies']) & changed)
        if before == changed:
            break
    source_changes = [p for p in previous['sources'].keys() | current['sources'].keys()
                      if previous['sources'].get(p) != current['sources'].get(p)]
    return {'changed_documents': sorted(source_changes), 'affected_motions': sorted(changed),
            'unchanged_motions': sorted(new.keys() - changed),
            'spec_changed': previous['spec'] != current['spec'],
            'upstream_review_required': any(Path(p).name in ('剧本.md', '视觉设定.md', '分镜.md') for p in source_changes),
            'content_acceptance': 'not_inferred_from_hashes'}
