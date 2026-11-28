"""Read a storyboard alongside locally collected takes, before or after editing."""
import argparse
from html import escape
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

import colab_h3
from drama_factory.core import atomic_bytes, project_path
from drama_factory.delivery import edit_parser


def review_motion(project_tool, path, episode, entries):
    root = project_tool.find_project(path)
    board = project_tool.storyboard_read_through(root, episode, with_sound=True)
    by_motion = {}
    for shot in board["shots"]:
        if shot.get("motion"):
            by_motion.setdefault(shot["motion"], []).append(shot)
    missing = set(entries).difference(by_motion)
    if missing:
        raise ValueError("当前分镜中找不到生成段：" + "、".join(sorted(missing)))

    order = list(by_motion)
    indexes = set()
    for entry in entries:
        i = order.index(entry)
        indexes.update(range(max(0, i - 1), min(len(order), i + 2)))
    rows = {}
    for i in sorted(indexes):
        motion = order[i]
        rows[motion] = {"motion": motion, "requested": motion in entries,
                        "shots": by_motion[motion], "takes": []}
    episode_dir = Path(board["source"]).parent
    issues = list(board["issues"])
    read_takes(root, str(episode_dir / "视频提示词.md"), rows, issues)
    cut_source = attach_cuts(root, episode_dir, rows, issues)
    return {"project": str(root), "episode": episode, "storyboard": board["source"],
            "cut_list": cut_source, "motions": list(rows.values()), "issues": issues}


def read_takes(root, prompt_source, rows, issues):
    production = colab_h3.engine()
    for name in production._metadata_json_names(root, ("jobs",)):
        try:
            job = production._metadata_read_json(root, ("jobs",), name, maximum=production.MAX_JOB_BYTES)
            if not isinstance(job, dict):
                raise ValueError("job 记录格式有误")
            if (job.get("modality") != "video" or job.get("source_entry") not in rows
                    or job.get("source") != prompt_source):
                continue
            production._validate_stored_job(root, job)
            if name != production._job_key(job["job_id"]) + ".json":
                raise ValueError("job 编号与账本不一致")
            completed = [run for run in production._read_run_history(root, job["job_id"])
                         if run["status"] == "succeeded" and run["fingerprint"] == job["fingerprint"]]
            if not completed:
                continue
            run = completed[-1]
            for filename, sha, size in production._validated_succeeded_outputs(job, run):
                # H3 declares the de-duplicated native first and may also retain a context master.
                if job["adapter"] == "h3-colab" and filename != job["outputs"][0]:
                    continue
                if not production.MEDIA_TYPES[Path(filename).suffix.casefold()].startswith("video/"):
                    continue
                relative = production._relative_path(filename, output=True)
                if production._hash_production_output(root, relative) != (sha, size):
                    raise ValueError("本地文件与成功回执不一致：" + relative)
                rows[job["source_entry"]]["takes"].append({
                    "job": job["job_id"], "run": run["run_id"], "finished_at": run["finished_at"],
                    "path": relative, "sha256": sha, "cuts": [],
                })
        except (KeyError, OSError, TypeError, ValueError) as exc:
            issues.append(f"回执 {name} 无法核实：{exc}")
    for row in rows.values():
        row["takes"].sort(key=lambda take: (take["finished_at"], take["job"]))


def attach_cuts(root, episode_dir, rows, issues):
    cut_source = str(episode_dir / "剪辑单.md")
    path = project_path(root, cut_source)
    if not path.is_file():
        return None
    editor = edit_parser()
    try:
        _, cuts, _ = editor.parse_cut_list(path)
    except (editor.EditError, OSError, ValueError) as exc:
        issues.append(f"{cut_source}：{exc}")
        return cut_source
    for cut in cuts:
        if cut.motion not in rows:
            continue
        media = editor._resolve_media(path.parent, root, cut.media)
        matched = False
        for take in rows[cut.motion]["takes"]:
            if root / take["path"] == media:
                take["cuts"].append({"id": cut.cut_id, "in": cut.start, "out": cut.end})
                matched = True
        if not matched:
            issues.append(f"{cut.cut_id} 引用的文件没有可核实的对应回片：{cut.media}")
    return cut_source


PAGE_MARKER = "<!-- drama project review-motion -->"

SEAM_PLAYER = """<script>
const player = document.querySelector('#seam-player');
const statusLine = document.querySelector('#seam-status');
let selections = [], position = -1, frame = 0;
function stopPreview(message) {
  position = -1;
  cancelAnimationFrame(frame);
  player.pause();
  statusLine.textContent = message;
}
function playNext() {
  cancelAnimationFrame(frame);
  player.pause();
  position += 1;
  if (position >= selections.length) return stopPreview('选段已播完');
  const item = selections[position];
  statusLine.textContent = (position + 1) + ' / ' + selections.length + '　' + item.name;
  player.src = item.url;
  player.hidden = false;
}
player.onloadedmetadata = () => {
  if (position < 0) return;
  const item = selections[position];
  if (item.end === null) item.end = player.duration;
  if (!(item.start >= 0 && item.start < item.end && item.end <= player.duration + 0.001)) {
    return stopPreview(item.name + ' 的入出点超出素材，请调整后重播。');
  }
  player.currentTime = item.start;
  player.play().catch(error => {
    if (error.name !== 'AbortError' && position >= 0) stopPreview('请检查播放器权限后重播。');
  });
};
function checkOutPoint() {
  if (position < 0 || player.paused) return;
  if (player.currentTime >= selections[position].end) return playNext();
  frame = requestAnimationFrame(checkOutPoint);
}
player.onplaying = () => { cancelAnimationFrame(frame); checkOutPoint(); };
player.onended = () => { if (position >= 0) playNext(); };
player.onerror = () => { if (position >= 0) stopPreview('原片无法播放，请核对本地路径或浏览器解码能力。'); };
document.querySelector('#play-seam').onclick = () => {
  selections = Array.from(document.querySelectorAll('figure input:checked'), box => {
    const card = box.closest('figure');
    const end = card.querySelector('.out').value;
    return {url:card.querySelector('video').src, name:card.querySelector('a').textContent,
            start:Number(card.querySelector('.in').value), end:end === '' ? null : Number(end)};
  });
  stopPreview('先勾选要连播的原片。');
  if (selections.length) playNext();
};
document.querySelector('#stop-seam').onclick = () => stopPreview('已停止');
document.addEventListener('play', event => {
  if (!(event.target instanceof HTMLVideoElement)) return;
  if (event.target !== player && position >= 0) stopPreview('正在单独查看原片');
  document.querySelectorAll('video').forEach(video => { if (video !== event.target) video.pause(); });
}, true);
</script>"""


def write_page(report, target):
    if target.suffix.lower() != ".html" or target.is_symlink():
        raise ValueError("审看页需使用 .html 文件")
    exists = target.exists()
    if exists:
        with target.open("rb") as file:
            if file.readline().rstrip() != PAGE_MARKER.encode():
                raise ValueError("同名文件已有其他内容，请选一个审看页路径")
    root = Path(report["project"])
    title = escape(f"{root.name} · {report['episode']} 片段审看")
    parts = [PAGE_MARKER, '<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f'<title>{title}</title>', """<style>
body{max-width:80rem;margin:auto;padding:1rem 3vw 3rem;color:#20302c;background:#faf9f6;font:16px/1.55 system-ui,sans-serif}
h1,h2{line-height:1.25}h2{margin-top:2rem;border-top:1px solid #bbb;padding-top:1rem}
.takes{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(20rem,100%),1fr));gap:1rem}
video{width:100%;background:#17211f}figure{margin:0}figcaption{overflow-wrap:anywhere}
details{margin:.6rem 0}summary,button{cursor:pointer}a{color:#126454}
.range{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;margin-top:.4rem}
.range input[type=number]{width:5em}#seam-player{max-width:50rem}button{padding:.3rem .65rem}
</style>""", f"<main><h1>{title}</h1>",
             "<p>按当前分镜排列。可单独看原片，或勾选版本、填写入出秒数后连播。空白表示从片头或播至片尾。剪辑单已引用的区间列在片名下。</p>",
             '<p><button id="play-seam">按选段连播</button> <button id="stop-seam">停止</button> '
             '<span id="seam-status" role="status"></span></p>',
             '<video id="seam-player" controls playsinline hidden></video>']
    for row in report["motions"]:
        prefix = "复核段" if row["requested"] else "相邻段"
        parts.append(f"<section><h2>{prefix} · {escape(row['motion'])}</h2><details><summary>当前分镜与声音</summary>")
        for shot in row["shots"]:
            times = shot.get("interval")
            timing = f" · {times[0]:g}–{times[1]:g} 秒" if times else ""
            parts.append(f"<p><b>{escape(shot['shot'])}{timing}</b><br>")
            for key in ("起点", "唯一动作", "终点", "声音"):
                parts.append(f"{key}　{escape(shot.get(key, ''))}<br>")
            parts.append("</p>")
        parts.append('</details><div class="takes">')
        for take in row["takes"]:
            url = escape(quote(os.path.relpath(root / take["path"], target.parent)))
            parts.append(f'<figure><video controls playsinline preload="metadata" src="{url}"></video>'
                         f'<figcaption><a href="{url}">{escape(Path(take["path"]).name)}</a>')
            for cut in take["cuts"]:
                parts.append(f'<br>剪辑单 {escape(cut["id"])} · '
                             f'<a href="{url}#t={cut["in"]},{cut["out"]}">{cut["in"]:g}–{cut["out"]:g} 秒</a>')
            parts.append('</figcaption><div class="range"><label><input type="checkbox">连播</label>'
                         '<label>入 <input class="in" type="number" min="0" step="any"></label>'
                         '<label>出 <input class="out" type="number" min="0" step="any"></label></div></figure>')
        if not row["takes"]:
            parts.append("<p>暂无可核实的回片。</p>")
        parts.append("</div></section>")
    if report["issues"]:
        parts.append("<h2>待核记录</h2><ul>")
        parts.extend(f"<li>{escape(issue)}</li>" for issue in report["issues"])
        parts.append("</ul>")
    parts.extend(("</main>", SEAM_PLAYER, "</html>"))
    atomic_bytes(target, ("\n".join(parts) + "\n").encode(), exclusive=not exists)


def main(project_tool, argv):
    parser = argparse.ArgumentParser(prog="drama project review-motion",
                                     description="按分镜顺序查看指定段及相邻段的原片和剪辑单引用。")
    parser.add_argument("project", type=Path)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--motion", action="append", required=True, help="可重复指定")
    parser.add_argument("--html", type=Path, help="写本地可播放的审看页，省略则输出核对数据")
    args = parser.parse_args(argv)
    try:
        report = review_motion(project_tool, args.project, args.episode, args.motion)
        if args.html:
            target = args.html.absolute()
            write_page(report, target)
            print(json.dumps({"page": str(target), "issues": report["issues"]}, ensure_ascii=False))
        else:
            print(json.dumps(report, indent=2, ensure_ascii=False))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
