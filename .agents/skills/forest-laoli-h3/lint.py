"""Advisory checks for one V4 prompt and its actual ordered references."""
import re

FIELDS = ('subject_definitions', 'summary', 'retention_analysis', 'detailed_description',
          'overall_soundscape', 'non_diegetic_music')
HEAD = re.compile(r'^(%s):[ \t]*' % '|'.join(FIELDS), re.M)
LABEL = r'<(?:Subject|Picture|Audio|Video) [1-9]\d*>'
VISUAL_RELATIONS = {'fully_preserved', 'partially_preserved', 'attribute_transfer', 'weak_reference'}
AUDIO_RELATIONS = {'fully_copy', 'partially_copy', 'reference', 'weak_reference'}
TASKS = {'reference generation', 'keyframe completion', 'video editing', 'video continuation',
         'audio reuse', 'audio reference'}
DOOR_SOUND = re.compile(r'\b(?:knocks?|knocking|pounding)\b', re.I)
SPEAKER = re.compile(r'\(S[1-9]\d*\)')


def inspect(prompt, duration, image_count, audio_count, composition=()):
    notes = []
    heads = list(HEAD.finditer(prompt))
    if tuple(m[1] for m in heads) != FIELDS:
        notes.append(('H1', '核对六段字段是否齐全、唯一且顺序正确'))
    sections = {m[1]: prompt[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(prompt)]
                for i, m in enumerate(heads)}
    if not 4 <= duration <= 15:
        notes.append(('H3', '核对请求时长是否在 4–15 秒'))
    for label, count in (('Picture', image_count), ('Audio', audio_count), ('Video', 0)):
        refs = set(re.findall(rf'<{label}\s+(\d+)>', prompt, re.I))
        outside = sorted(int(n) for n in refs if not 1 <= int(n) <= count)
        if outside:
            notes.append(('H36', f'{label} 槽 {outside} 超出本段的 {count} 个实际绑定'))

    definitions = sections.get('subject_definitions', '')
    summary = sections.get('summary', '')
    retention = sections.get('retention_analysis', '')
    body = sections.get('detailed_description', '')
    soundscape = sections.get('overall_soundscape', '')
    music = sections.get('non_diegetic_music', '')
    task = re.match(r'\s*\[([^\]]+)\]', summary)
    if not task or set(part.strip() for part in task[1].split('+')) - TASKS:
        notes.append(('H37', 'summary 应以与真实参考用途一致的方括号任务类型开头'))

    defined = re.findall(rf'^\s*({LABEL})', definitions, re.M)
    # Asset-only pictures belong inside Subject definitions. Composition pictures have their own entry.
    required = ({f'<Picture {n}>' for n in range(1, image_count + 1)}
                | {f'<Audio {n}>' for n in range(1, audio_count + 1)})
    missing = sorted(required - set(re.findall(LABEL, definitions)))
    standalone = [f'<Picture {n}>' for n in composition if f'<Picture {n}>' not in defined]
    unused_composition = [f'<Picture {n}>' for n in composition if f'<Picture {n}>' not in body]
    undefined = sorted(set(re.findall(r'<Subject [1-9]\d*>', summary + retention + body)) - set(defined))
    if missing or standalone or unused_composition or undefined or not defined:
        notes.append(('H38', '核对主体、真实图/音频和构图图的定义与镜内引用'
                      f'（缺绑定或引用：{sorted(set(missing + standalone + unused_composition + undefined))}）'))
    retained = {}
    for line in retention.splitlines():
        match = re.match(rf'\s*({LABEL})(?:\s*\(.*?\))?\s*:\s*(\w+)', line)
        if match:
            retained[match[1]] = match[2]
    unmarked = [label for label in defined if retained.get(label) not in
                (AUDIO_RELATIONS if label.startswith('<Audio ') else VISUAL_RELATIONS)]
    if not retained or unmarked:
        notes.append(('H39', f'retention_analysis 应逐项写适用的保留关系（待核对：{unmarked}）'))

    if '[Shot 1]' not in body:
        notes.append(('H40', 'detailed_description 应标出 [Shot 1]，单镜同样适用'))
    dialogue = list(re.finditer(r'<d>(.*?)</d>', body, re.S))
    if any(not re.match(r'\s*\[[A-Za-z][A-Za-z -]*\]', line[1]) for line in dialogue):
        notes.append(('H4', '有对白未声明口语语言；核对说话人和逐字原文'))
    previous_end = 0
    for line in dialogue:
        if not SPEAKER.search(body[previous_end:line.start()]):
            notes.append(('H41', '有对白缺少同一发声事件的 (Sx)；画外对白同时注明 off-screen'))
            break
        previous_end = line.end()
    if SPEAKER.search(retention) or any('<d>' in part for part in (definitions, summary, retention, soundscape, music)):
        notes.append(('H42', '保留关系不写 (Sx)，逐字对白和歌词放在 detailed_description'))

    visuals = re.sub(r'<d>.*?</d>', ' ', body, flags=re.S)
    if re.search(r"\b(?:do not|don't|avoid|never|must not)\b", visuals, re.I):
        notes.append(('H13', '有否定描述；核对能否改为可见状态，保留必要禁令'))
    if DOOR_SOUND.search(soundscape) and not DOOR_SOUND.search(visuals):
        notes.append(('H31', '若敲门是触发动作的同步事件，在镜内时间线写清画外声源、时点和反应'))
    return notes
