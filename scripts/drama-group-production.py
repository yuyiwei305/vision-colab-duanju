#!/usr/bin/env python3
"""Project-only GROUP support around the pinned production engine.

The upstream files stay untouched. Jobs keep their actual GROUP source ID and
use the upstream preview, confirmation, snapshots, provider handles and ledger.
This prototype deliberately supports image-reference groups only.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import types
from pathlib import Path

import h3_media_retention

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = ROOT / "ref/drama-skills/skills/short-drama-produce/scripts/production_tool.py"


def dependency_paths(root: Path, source: str | None, entry: str | None) -> list[str]:
    if not entry or not entry.startswith("GROUP-") or source is None:
        return []
    return [str(Path(source).with_name(name)) for name in ("分镜.md", "剧本.md", "视觉设定.md")] + ["short-drama.json"]


def field(section: str, name: str) -> str:
    values = re.findall(rf"^- {re.escape(name)}：([^\n]+)$", section, re.MULTILINE)
    if len(values) != 1:
        raise ValueError(f"GROUP: {name} must occur exactly once")
    return values[0].strip()


def seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|秒)", value)
    if not match:
        raise ValueError("GROUP: duration must be seconds")
    return float(match[1])


def validate_group(engine, root: Path, job: dict) -> None:
    entry = job.get("source_entry") or ""
    if not entry.startswith("GROUP-"):
        return
    source = engine._project_file(root, job["source"])
    document = source.read_text(encoding="utf-8")
    group = engine._markdown_section(document, entry)
    profile = json.loads(engine._project_file(root, "short-drama.json").read_text())
    choices = profile["creator_authority"]["production_profile"]["choices"]
    dialect = choices.get("video_prompt_dialect")
    if choices.get("multi_shot_containers") is not True or dialect not in {"seedance-2.0", "seedance-2.5"}:
        raise ValueError("GROUP: the accepted profile must enable a supported Seedance multi-shot dialect")
    if field(group, "任务类型") != "reference":
        raise ValueError("GROUP: this prototype only supports reference generation")
    task_type = job["parameters"].get("omni_reference_task_type")
    if (dialect == "seedance-2.5" and task_type != "reference") or (dialect == "seedance-2.0" and task_type is not None):
        raise ValueError("GROUP: task-type parameter must match the accepted dialect")
    if field(group, "生成方式") != "图生视频" or not job["reference_bindings"]:
        raise ValueError("GROUP: real image references are required")
    if any(item["role"] != "reference_image" for item in job["reference_bindings"]):
        raise ValueError("GROUP: only reference_image roles are supported in this prototype")
    members = re.split(r"[、，,]", field(group, "成员"))
    if len(members) < 2 or len(set(members)) != len(members) or any(not re.fullmatch(r"MOTION-[A-Z0-9-]+", item) for item in members):
        raise ValueError("GROUP: at least two distinct MOTION members are required")
    board_path = engine._project_file(root, str(Path(job["source"]).with_name("分镜.md")))
    board = board_path.read_text(encoding="utf-8")
    shot_order = re.findall(r"^## (SHOT-[A-Z0-9-]+) ·", board, re.MULTILINE)
    shot_ids, durations, scenes, member_paths = [], [], set(), set()
    for member in members:
        motion = engine._markdown_section(document, member)
        shot_id = field(motion, "分镜")
        shot = engine._markdown_section(board, shot_id)
        shot_ids.append(shot_id)
        duration = seconds(field(motion, "时长"))
        if duration <= 0 or duration != seconds(field(shot, "时长")):
            raise ValueError("GROUP: member and SHOT durations must match")
        durations.append(duration)
        scene_ids = re.findall(r"EP\d+-SC\d+", field(shot, "来源"))
        if len(scene_ids) != 1:
            raise ValueError("GROUP: every member must have exactly one scene")
        scenes.update(scene_ids)
        if field(motion, "输入参考图") != field(shot, "输入参考图"):
            raise ValueError("GROUP: member references must match the SHOT")
        declared = engine._markdown_reference_bindings(motion, field_name="输入参考图")
        for binding in declared:
            member_paths.add(binding["path"])
        if re.search(r"^- 参考音频：", motion, re.MULTILINE):
            raise ValueError("GROUP: reference audio is not supported by this prototype")
    positions = [shot_order.index(shot) for shot in shot_ids]
    if len(scenes) != 1 or positions != list(range(positions[0], positions[0] + len(positions))):
        raise ValueError("GROUP: members must be consecutive SHOTs in one scene")
    if member_paths != {item["path"] for item in job["reference_bindings"]}:
        raise ValueError("GROUP: the shared reference list must cover exactly the member images")
    declared_duration = seconds(field(group, "时长"))
    requested = job["parameters"].get("duration")
    maximum = 15 if dialect == "seedance-2.0" else 30
    if isinstance(requested, bool) or not isinstance(requested, int) or not 4 <= requested <= maximum:
        raise ValueError(f"GROUP: duration must be an integer from 4 to {maximum}")
    if declared_duration != requested or sum(durations) != declared_duration:
        raise ValueError("GROUP: requested, declared and summed durations must match")
    if dialect == "seedance-2.0":
        if re.search(r"\d+:\d{2}\s*[–—-]\s*\d+:\d{2}", job["prompt"]):
            raise ValueError("GROUP: Seedance 2.0 uses ordinal cuts, not timestamp control")
        numbers = re.findall(r"镜头\s*(\d+)\s*[：:]", job["prompt"])
        if numbers != [str(n) for n in range(1, len(members) + 1)]:
            raise ValueError("GROUP: Seedance 2.0 needs consecutive ordinal cuts for every member")
        return
    cuts = re.findall(r"镜头\s*(\d+)\s*\[(\d+):(\d{2})[–—-](\d+):(\d{2})\]", job["prompt"])
    if len(cuts) != len(members):
        raise ValueError("GROUP: each member needs one explicit timestamp range")
    cursor = 0
    for index, (number, start_m, start_s, end_m, end_s) in enumerate(cuts):
        start, end = int(start_m) * 60 + int(start_s), int(end_m) * 60 + int(end_s)
        if int(number) != index + 1 or start != cursor or end - start != durations[index]:
            raise ValueError("GROUP: timestamps must follow the accepted member durations without gaps")
        cursor = end
    if cursor != requested:
        raise ValueError("GROUP: the final cut must end at the requested duration")


def load_engine():
    raw = ENGINE_PATH.read_bytes()
    lock = json.loads((ROOT / "drama-skills.lock.json").read_text())
    key = "skills/short-drama-produce/scripts/production_tool.py"
    if hashlib.sha256(raw).hexdigest() != lock["files_sha256"][key]:
        raise RuntimeError("Pinned production engine changed; review GROUP compatibility before use")
    code = raw.decode("utf-8")
    replacements = [
        # The existing project uses EP01; retain the episode/output guard while
        # accepting its two-digit directory names as well as upstream EP001.
        ('re.fullmatch(r"EP\\d{3,}", parts[1], re.IGNORECASE)',
         're.fullmatch(r"EP\\d{2,}", parts[1], re.IGNORECASE)', 1),
        ('"视频提示词.md": ("MOTION-",', '"视频提示词.md": (("MOTION-", "GROUP-"),', 1),
        ('{"image": "IMG-", "video": "MOTION-"}', '{"image": "IMG-", "video": ("MOTION-", "GROUP-")}', 1),
        ('input_paths = ([source] if source is not None else []) + references',
         'input_paths = ([source] if source is not None else []) + references + _group_dependency_paths(root, source, source_entry)', 2),
        # The profile is an internal dependency, never a user-selected source,
        # reference or output. Snapshot it without widening public path access.
        ('relative = _relative_path(relative_value)',
         'relative = PROJECT_FILE if relative_value == PROJECT_FILE else _relative_path(relative_value)', 1),
    ]
    for old, new, count in replacements:
        if code.count(old) != count:
            raise RuntimeError("Unsupported upstream production engine layout")
        code = code.replace(old, new)
    engine = types.ModuleType("drama_group_engine")
    engine.__file__ = str(ENGINE_PATH)
    engine._group_dependency_paths = dependency_paths
    exec(compile(code, str(ENGINE_PATH), "exec"), engine.__dict__)

    # A continuation must snapshot and verify the actual preceding video, too.
    # Keep the pinned upstream verifier for all existing image/audio slots;
    # extend it only when the source explicitly declares a 参考视频 field.
    verify_source = engine._verify_markdown_source

    def verify_continuation(root, *, source, source_entry, prompt, bindings):
        video_bindings = [b for b in bindings if b["role"] == "reference_video"]
        section = engine._markdown_section(engine._project_file(root, source).read_text(encoding="utf-8"), source_entry)
        video_lines = engine._field_values(section, "参考视频") if Path(source).name == "视频提示词.md" else []
        if not video_lines:
            if video_bindings:
                raise ValueError("reference_video requires an explicit 参考视频 source declaration")
            return verify_source(root, source=source, source_entry=source_entry, prompt=prompt, bindings=bindings)
        if len(video_lines) != 1 or not video_bindings:
            raise ValueError("declared 参考视频 must be bound explicitly")
        image_bindings = [b for b in bindings if b["role"] != "reference_video"]
        if bindings != image_bindings + video_bindings:
            raise ValueError("reference videos must follow the other bindings")
        verify_source(root, source=source, source_entry=source_entry, prompt=prompt, bindings=image_bindings)
        video_pattern = re.compile(
            r"(REF-[A-Z0-9][A-Z0-9-]{0,79})（顺序：([1-9]\d*)）· "
            r"([^；\n]+?\.(?:mp4|mov))《([^》\n]+)》"
            r"（用途：[^；）\n]+；控制：([^；）\n]+)；不得控制：([^）\n]+)）",
            re.IGNORECASE,
        )
        value = video_lines[0].strip()
        matches = list(video_pattern.finditer(value))
        cursor = 0
        declared = []
        for index, match in enumerate(matches):
            if value[cursor:match.start()] != ("" if index == 0 else "；"):
                raise ValueError("invalid 参考视频 declaration")
            cursor = match.end()
            declared.append({
                "slot_id": match.group(1), "order": int(match.group(2)),
                "path": engine._relative_path(match.group(3)), "label": match.group(4).strip(),
                "may_control": engine._scope_items(match.group(5)),
                "must_not_control": engine._scope_items(match.group(6)),
            })
        if not matches or value[cursor:] not in {"", "。"}:
            raise ValueError("invalid 参考视频 declaration")
        keys = ("slot_id", "order", "path", "label", "may_control", "must_not_control")
        comparable = [{key: binding[key] for key in keys} for binding in video_bindings]
        if declared != comparable:
            raise ValueError("job reference video bindings do not match the selected source entry")

    engine._verify_markdown_source = verify_continuation
    normalize = engine._normalize_job

    def normalize_group(root, raw):
        job = normalize(root, raw)
        if job["modality"] == "video":
            project = json.loads(engine._project_file(root, "short-drama.json").read_text())
            aspect = project.get("format", {}).get("aspect_ratio")
            if job['parameters'].get('profile') == 'h3-v4-g4-fullframe-v1':
                match = re.fullmatch(r'剧集/(EP\d{3})/视频提示词.md', job.get('source') or '')
                if match:
                    from drama_factory.core import resolve_spec
                    spec = resolve_spec(root, match[1])
                    aspect = '16:9' if spec['production']['native_canvas'] == [1344, 768] else '9:16'
            if job['adapter'] == 'relayvsr-comfy-colab' and job['parameters'].get('finishing_protocol') == 'factory-v1':
                from drama_factory.delivery import probe
                media = probe(engine._project_file(root, job['references'][0]))
                p = job['parameters']
                if (p.get('input_sha256') != media['sha256'] or p.get('output_width') != media['width'] * 2
                        or p.get('output_height') != media['height'] * 2):
                    raise ValueError('Finishing must preserve the verified source geometry at native 2x')
                aspect = '16:9' if media['width'] >= media['height'] else '9:16'
            if (job['adapter'] == 'recavsr-comfy-colab' and
                    job['parameters'].get('finishing_protocol') == 'recavsr-whole-film-v1'):
                from fractions import Fraction
                from drama_factory.delivery import probe
                from drama_factory.finishing import geometry
                media = probe(engine._project_file(root, job['references'][0]))
                p = job['parameters']
                width, height = p['output_width'], p['output_height']
                if (p.get('input_sha256') != media['sha256'] or
                        (width, height, p.get('scale')) != geometry(
                            media['width'], media['height'], min(width, height))):
                    raise ValueError('ReCaVSR finishing must preserve the verified source geometry')
                aspect = str(Fraction(media['width'], media['height'])).replace('/', ':')
            if aspect and job["parameters"].get("ratio") != aspect:
                raise ValueError("Video ratio must explicitly match the accepted project aspect ratio")
        validate_group(engine, root, job)
        return job

    engine._normalize_job = normalize_group
    # Recheck before new generation, while leaving status/audit/collect able to
    # recover an already-paid task even if the creator later edits a document.
    def check_current(root, job_id):
        root = engine.find_project(root)
        job = engine._read_job(root, job_id)
        if (job.get("source_entry") or "").startswith("GROUP-"):
            if not engine._inputs_current(root, job):
                raise engine.ConfirmationRequiredError("GROUP inputs changed; prepare a new preview")
            validate_group(engine, root, job)

    confirm, run = engine.confirm_job, engine.run_job

    def confirm_group(root, *, job_id, confirmation):
        check_current(root, job_id)
        return confirm(root, job_id=job_id, confirmation=confirmation)

    def run_group(root, *, job_id, adapter_config):
        check_current(root, job_id)
        return run(root, job_id=job_id, adapter_config=adapter_config)

    engine.confirm_job, engine.run_job = confirm_group, run_group
    h3_media_retention.install(engine)
    return engine


if __name__ == "__main__":
    try:
        raise SystemExit(load_engine().main())
    except (ValueError, RuntimeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
