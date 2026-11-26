#!/usr/bin/env python3
"""Run the official ComfyUI RelayVSR 2x graph on consecutive shots, then reuse RIFE."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

from relay_vsr_colab_remote import command, sha, worker as finish_with_rife, write


def fetch_json(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def graph(spec, start, end, prefix):
    fps = spec["source_fps"]
    # Place boundaries between source frames to avoid float rounding onto the wrong PTS.
    begin = (start - 0.25) / fps if start else 0.0
    stop = (end - 0.25) / fps
    link = lambda node, slot=0: [str(node), slot]
    def node(kind, **inputs):
        return {"class_type": kind, "inputs": inputs}
    return {
        "1": node("LoadVideo", file="source.mp4"),
        "2": node("Video Slice", video=link(1), start_time=begin, duration=stop-begin, strict_duration=False),
        "3": node("GetVideoComponents", video=link(2)),
        "4": node("UNETLoader", unet_name=Path(spec["unet"]).name, weight_dtype="default"),
        "5": node("RelayVSRConditioning", model=link(4), images=link(3)),
        "6": node("KSampler", model=link(4), positive=link(5), negative=link(5), latent_image=link(5, 1),
                  seed=42, steps=1, cfg=1.0, sampler_name="euler", scheduler="simple", denoise=1.0),
        "7": node("VAELoader", vae_name=Path(spec["vae"]).name),
        "8": node("VSRDecode", vae=link(7), samples=link(6), images=link(3), detail=1.0),
        "9": node("CreateVideo", images=link(8), fps=link(3, 2), bit_depth="auto",
                  color_space="sRGB", codec="none", chroma_subsampling="auto"),
        "10": node("SaveVideo", video=link(9), filename_prefix=prefix, format="auto"),
    }


def video_stream(path):
    return json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", path,
    ]))["streams"][0]


def check_shot(path, spec, count):
    v = video_stream(path)
    actual = (v["width"], v["height"], v["avg_frame_rate"], int(v["nb_frames"]))
    expected = (spec["output_width"], spec["output_height"], f'{spec["source_fps"]}/1', count)
    if actual != expected:
        raise ValueError(f"shot {path.name}: {actual} != {expected}")


def render_shots(root, spec, comfy):
    start_time = time.monotonic()
    shots = root / "shots"
    shots.mkdir(exist_ok=True)
    base = "http://127.0.0.1:8188"
    log = (root / "comfy.log").open("ab")
    server = subprocess.Popen([
        sys.executable, "-u", "main.py", "--listen", "127.0.0.1", "--port", "8188",
        "--disable-all-custom-nodes", "--disable-metadata", "--cache-classic",
        "--input-directory", str(comfy / "input"), "--output-directory", str(shots),
    ], cwd=comfy, stdout=log, stderr=log)
    try:
        for _ in range(120):
            if server.poll() is not None:
                raise RuntimeError("ComfyUI exited; see comfy.log")
            try:
                fetch_json(base, "/object_info/RelayVSRConditioning")
                break
            except OSError:
                time.sleep(2)
        else:
            raise TimeoutError("ComfyUI startup timeout")
        schema = fetch_json(base, "/object_info/Video%20Slice")
        if "Video Slice" not in schema:
            raise RuntimeError("Video Slice node unavailable")
        bounds = [0, *spec["cuts"], spec["frames"]]
        paths, timings = [], []
        for index, (a, b) in enumerate(zip(bounds, bounds[1:]), 1):
            prefix = f"shot-{index:03d}"
            path = shots / f"{prefix}.mp4"
            if not path.exists():
                write(root / "status.json", {"state": "comfy", "shot": index, "shots": len(bounds)-1,
                                               "frames": a, "total": spec["frames"]})
                queue_time = time.monotonic()
                # Explicit ID allows history polling without replaying an accepted prompt.
                prompt_id = str(uuid.uuid4())
                try:
                    reply = fetch_json(base, "/prompt", {"prompt": graph(spec, a, b, prefix),
                                                         "prompt_id": prompt_id})
                    prompt_id = reply["prompt_id"]
                except (TimeoutError, ConnectionError):
                    pass
                while time.time() < spec["deadline"] - 120:
                    if server.poll() is not None:
                        raise RuntimeError("ComfyUI exited during rendering")
                    record = fetch_json(base, f"/history/{prompt_id}").get(prompt_id)
                    if record:
                        if not record.get("status", {}).get("completed"):
                            raise RuntimeError(f"ComfyUI shot {index} failed: {record.get('status')}")
                        generated = next(shots.glob(f"{prefix}_*.mp4"))
                        check_shot(generated, spec, b-a)
                        generated.rename(path)
                        timings.append({"shot": index, "frames": b-a, "seconds": time.monotonic()-queue_time})
                        break
                    time.sleep(1)
                else:
                    raise TimeoutError("RelayVSR compute deadline")
            check_shot(path, spec, b-a)
            paths.append((path, b-a))
        manifest = root / "concat.txt"
        manifest.write_text("".join(f"file 'shots/{path.name}'\nduration {count/spec['source_fps']:.9f}\n" for path, count in paths))
        master = root / spec["master_filename"]
        command(["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "concat", "-safe", "0", "-i",
                 manifest, "-map", "0:v:0", "-c", "copy", master])
        check_shot(master, spec, spec["frames"])
        write(root / "comfy-receipt.json", {"shots": timings, "frames": spec["frames"],
              "render_seconds": sum(t["seconds"] for t in timings), "elapsed_seconds": time.monotonic()-start_time,
              "comfy_commit": spec["comfy_commit"], "weights_revision": spec["weights_revision"]})
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log.close()
    return master


def setup_comfy(root, spec):
    comfy = root / "ComfyUI"
    if not comfy.exists():
        command(["git", "clone", "--quiet", "--branch", "v0.45.0", "--depth", "1",
                 "https://github.com/Comfy-Org/ComfyUI.git", comfy])
    if subprocess.check_output(["git", "-C", comfy, "rev-parse", "HEAD"], text=True).strip() != spec["comfy_commit"]:
        raise RuntimeError("ComfyUI revision mismatch")
    command([sys.executable, "-m", "pip", "install", "-q", "-r", comfy / "requirements.txt"])
    return comfy


def main(root, spec):
    use_rife = spec.get('target_fps', 60) != spec['source_fps']
    if sha(root / "input.mp4") != spec["input_sha256"] or (use_rife and sha(root / "rife.zip") != spec["rife_zip_sha256"]):
        raise ValueError("source or RIFE hash mismatch")
    master = root / spec['master_filename']
    if spec.get('resume_relay_sha256'):
        if not master.is_file() or sha(master) != spec['resume_relay_sha256']:
            raise ValueError('recovered_master_sha_mismatch')
    final = root / 'final.mp4'
    if final.is_file() and master.is_file():
        target_fps = spec.get('target_fps', 60)
        check_shot(master, spec, spec['frames'])
        check_shot(final, {**spec, 'source_fps': target_fps}, spec['frames'] * target_fps // spec['source_fps'])
        files = {}
        for path in (final, master):
            command(['ffmpeg', '-v', 'error', '-xerror', '-i', path, '-f', 'null', '-'])
            files[path.name] = {'sha256': sha(path), 'bytes': path.stat().st_size}
        write(root / 'receipt.json', {'state': 'succeeded', 'files': files, 'recovered_existing_final': True})
        write(root / 'status.json', {'state': 'succeeded'})
        return
    if not master.exists():
        write(root / "status.json", {"state": "installing_comfy"})
        comfy = setup_comfy(root, spec)
        source = comfy / "input" / "source.mp4"
        if not source.exists():
            # Copy only the video stream. The source AAC is remuxed once at the end.
            command(["ffmpeg", "-v", "error", "-nostdin", "-n", "-i", root / "input.mp4", "-map",
                     "0:v:0", "-c", "copy", source])
        for name, expected in spec["comfy_weights"].items():
            target = comfy / "models" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            write(root / "status.json", {"state": "downloading_weights", "file": target.name})
            if not target.exists():
                partial = target.with_suffix(".partial")
                command(["curl", "-fL", "--retry", "3", "-sS", "--max-time", "900",
                         f'https://huggingface.co/Comfy-Org/RelayVSR/resolve/{spec["weights_revision"]}/{name}',
                         "-o", partial])
                if sha(partial) != expected:
                    raise RuntimeError(f"checkpoint SHA mismatch: {name}")
                partial.rename(target)
            elif sha(target) != expected:
                raise RuntimeError(f"checkpoint SHA mismatch: {name}")
    master = root / spec["master_filename"]
    if not master.exists():
        master = render_shots(root, spec, comfy)
    check_shot(master, spec, spec["frames"])
    if not use_rife:
        final = root / 'final.mp4'
        if not final.exists():
            command(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', master, '-i', root / 'input.mp4',
                     '-map', '0:v:0', '-map', '1:a:0?', '-c', 'copy', '-movflags', '+faststart', final])
        check_shot(final, spec, spec['frames'])
        files = {}
        for path in (final, master):
            command(['ffmpeg', '-v', 'error', '-xerror', '-i', path, '-f', 'null', '-'])
            files[path.name] = {'sha256': sha(path), 'bytes': path.stat().st_size}
        write(root / 'receipt.json', {'state': 'succeeded', 'files': files,
              'source_fps': spec['source_fps'], 'target_fps': spec['source_fps'], 'rife': 'not_required'})
        write(root / 'status.json', {'state': 'succeeded'})
        return
    write(root / "status.json", {"state": "preparing_rife", "frames": spec["frames"], "total": spec["frames"]})
    for name, revision, repo in (
        ("RelayVSR", spec["relay_commit"], "kopperx/RelayVSR"),
        ("Practical-RIFE", spec["rife_commit"], "hzwer/Practical-RIFE"),
    ):
        checkout = root / name
        if not checkout.exists():
            command(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
                     f"https://github.com/{repo}.git", checkout])
            command(["git", "-C", checkout, "checkout", "--quiet", revision])
        if subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip() != revision:
            raise RuntimeError(f"{name} revision mismatch")
    with zipfile.ZipFile(root / "rife.zip") as archive:
        for item in archive.infolist():
            if item.filename.startswith("train_log/") and item.filename.endswith((".py", ".pkl")):
                archive.extract(item, root / "Practical-RIFE")
    # A fresh VM that starts with a verified master skips ComfyUI's installer.
    command([sys.executable, "-m", "pip", "install", "-q", "av==18.1.0",
             "accelerate==1.15.0", "einops==0.8.2", "diffusers==0.37.1"])
    video = video_stream(master)
    srgb = (video.get('color_space'), video.get('color_primaries'), video.get('color_transfer'),
            video.get('color_range')) == ('bt709', 'bt709', 'iec61966-2-1', 'tv')
    finish_with_rife(root, {**spec, "model_revision": spec["weights_revision"],
                            "rife_master_srgb": srgb, "resume_relay_sha256": sha(master)})
    receipt = json.loads((root / "receipt.json").read_text())
    comfy_receipt = root / 'comfy-receipt.json'
    receipt["comfy"] = (json.loads(comfy_receipt.read_text()) if comfy_receipt.exists() else
                        {'recovered_master_sha256': spec['resume_relay_sha256']})
    write(root / "receipt.json", receipt)


if __name__ == "__main__":
    def stop(signum, frame):
        raise TimeoutError("resource guard stopped compute")
    signal.signal(signal.SIGTERM, stop)
    root = Path(sys.argv[1])
    spec = json.loads((root / "runtime.json").read_text())
    try:
        main(root, spec)
    except Exception as exc:
        write(root / "status.json", {"state": "failed", "error": type(exc).__name__, "detail": str(exc)[:1200]})
        raise
