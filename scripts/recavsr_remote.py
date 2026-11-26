"""Whole-film ReCaVSR, cut-aware RIFE and original audio remux."""
import hashlib
import json
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def command(args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, record):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(path)


def fetch_json(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def check_video(path, spec, fps, count):
    result = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", path,
    ])
    video = json.loads(result)["streams"][0]
    expected = (spec["output_width"], spec["output_height"], f"{fps}/1", count)
    actual = (video["width"], video["height"], video["avg_frame_rate"], int(video["nb_frames"]))
    if actual != expected:
        raise ValueError(f"{path.name}: video geometry/frame clock mismatch {actual} != {expected}")


def checkout(path, repository, revision):
    if not path.exists():
        command(["git", "init", "-q", path])
    if subprocess.run(["git", "-C", path, "cat-file", "-e", revision], capture_output=True).returncode:
        command(["git", "-C", path, "fetch", "-q", "--depth", "1", f"https://github.com/{repository}.git", revision])
    command(["git", "-C", path, "checkout", "-q", "--detach", revision])
    if subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True).strip() != revision:
        raise ValueError("repository revision mismatch")


def fetch_weight(weight, directory):
    path = directory / weight["file"]
    if not path.is_file():
        temporary = path.with_suffix(".partial")
        url = f'https://huggingface.co/{weight["repository"]}/resolve/{weight["revision"]}/{weight["path"]}'
        command(["curl", "-fLsS", "--retry", "3", "--max-time", "900", url, "-o", temporary])
        if sha(temporary) != weight["sha256"]:
            raise ValueError(f"weight SHA mismatch: {path.name}")
        temporary.rename(path)
    elif sha(path) != weight["sha256"]:
        raise ValueError(f"weight SHA mismatch: {path.name}")


def graph(spec):
    def node(kind, **inputs):
        return {"class_type": kind, "inputs": inputs}

    return {
        "1": node("LoadVideo", file="source.mp4"),
        "3": node("ReCaVSRLoader", transformer="transformer_int8_convrot.safetensors"),
        "4": node("ReCaVSRUpscale", model=["3", 0], video=["1", 0], scale=spec["scale"], color_fix=spec["color_fix"],
                  seed=42, decoder="flashdecoder", kernels="fast", kv_offload=False, encoder="h264 nvenc"),
        "5": node("SaveVideo", video=["4", 0], filename_prefix="recavsr-full", format="auto"),
    }


def superscale(root, spec):
    write(root / "status.json", {"state": "installing_recavsr"})
    comfy = root / "ComfyUI"
    checkout(comfy, "Comfy-Org/ComfyUI", spec["comfy_commit"])
    plugin = comfy / "custom_nodes" / "ComfyUI-ReCaVSR"
    checkout(plugin, "PedroMarinhoDev/ComfyUI-ReCaVSR", spec["recavsr_commit"])
    models = comfy / "models" / "recavsr"
    models.mkdir(exist_ok=True)
    with ThreadPoolExecutor(4) as pool:
        dependencies = pool.submit(command, [sys.executable, "-m", "pip", "install", "-q",
            "-r", comfy / "requirements.txt", "-r", plugin / "requirements.txt",
            "av==18.1.0", "accelerate==1.15.0", "diffusers==0.37.1"])
        weights = [pool.submit(fetch_weight, weight, models) for weight in spec["weights"]]
        for task in [dependencies, *weights]:
            task.result()

    source = comfy / "input" / "source.mp4"
    source.parent.mkdir(exist_ok=True)
    if not source.exists():
        command(["ffmpeg", "-v", "error", "-nostdin", "-n", "-i", root / "input.mp4", "-map", "0:v:0", "-c", "copy", source])
    output_dir = root / "comfy-output"
    output_dir.mkdir(exist_ok=True)
    base = "http://127.0.0.1:8188"
    startup = time.monotonic()
    master = root / spec["master_filename"]
    with (root / "comfy.log").open("ab") as log:
        server = subprocess.Popen([
            sys.executable, "-u", "main.py", "--listen", "127.0.0.1", "--port", "8188",
            "--disable-metadata", "--cache-classic", "--output-directory", str(output_dir),
        ], cwd=comfy, stdout=log, stderr=log)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError("ComfyUI exited at startup; see comfy.log")
                try:
                    schema = fetch_json(base, "/object_info/ReCaVSRUpscale")
                except OSError:
                    time.sleep(2)
                    continue
                if "ReCaVSRUpscale" not in schema:
                    raise RuntimeError("ComfyUI did not load ReCaVSR; see comfy.log")
                break
            else:
                raise TimeoutError("ComfyUI startup timeout")

            write(root / "status.json", {"state": "recavsr", "total": spec["frames"]})
            started = time.monotonic()
            ident = str(uuid.uuid4())
            write(root / "comfy-intent.json", {"prompt_id": ident, "whole_source": True})
            try:
                ident = fetch_json(base, "/prompt", {
                    "prompt": graph(spec), "prompt_id": ident,
                })["prompt_id"]
            except urllib.error.HTTPError as error:
                raise RuntimeError(error.read().decode()[:1800]) from error
            except (TimeoutError, ConnectionError, urllib.error.URLError):
                pass
            while time.time() < spec["deadline"]:
                if server.poll() is not None:
                    raise RuntimeError("ComfyUI exited during ReCaVSR")
                record = fetch_json(base, f"/history/{ident}").get(ident)
                if record:
                    if not record.get("status", {}).get("completed"):
                        detail = record.get("status", {}).get("messages", [])[-1:]
                        raise RuntimeError(f"ReCaVSR failed: {str(detail)[:1350]}")
                    output = output_dir / record["outputs"]["5"]["images"][0]["filename"]
                    check_video(output, spec, spec["source_fps"], spec["frames"])
                    output.rename(master)
                    break
                time.sleep(3)
            else:
                raise TimeoutError("ReCaVSR compute deadline")
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()

    write(root / "comfy-receipt.json", {"frames": spec["frames"], "comfy_prompts": 1,
          "whole_source": True, "master_sha256": sha(master), "geometry": [spec["output_width"], spec["output_height"]],
          "render_seconds": time.monotonic()-started, "elapsed_seconds": time.monotonic()-startup,
          "recavsr_commit": spec["recavsr_commit"], "comfy_commit": spec["comfy_commit"], "weights": spec["weights"]})
    return master


def target_frame_source(index, source_fps, target_fps, cuts, source_frames):
    frame, offset = divmod(index * source_fps, target_fps)
    if frame + 1 in cuts or frame + 1 >= source_frames:
        offset = 0
    return frame, offset


def interpolate(root, master, spec):
    rife = root / "Practical-RIFE"
    checkout(rife, "hzwer/Practical-RIFE", spec["rife_commit"])
    with zipfile.ZipFile(root / "rife.zip") as archive:
        for member in archive.namelist():
            if member.startswith("train_log/") and member.endswith((".py", ".pkl")):
                archive.extract(member, rife)
    # The recovery instance can begin with a master and no ComfyUI environment.
    command([sys.executable, "-m", "pip", "install", "-q", "av==18.1.0"])
    import av
    import torch
    import torch.nn.functional as functional

    sys.path.insert(0, str(rife))
    from train_log.IFNet_HDv3 import IFNet

    torch.set_num_threads(8)
    torch.set_grad_enabled(False)
    model = IFNet().cuda().eval()
    weights = torch.load(rife / "train_log/flownet.pkl", map_location="cpu", weights_only=True)
    loaded = model.load_state_dict({key.removeprefix("module."): value for key, value in weights.items()}, strict=False)
    if loaded.missing_keys or any(not key.startswith(("teacher.", "caltime.")) for key in loaded.unexpected_keys):
        raise ValueError("RIFE 4.25 weights do not match the model")
    del weights

    target = root / "rife-silent.mp4"
    width, height = spec["output_width"], spec["output_height"]
    source_fps, target_fps = spec["source_fps"], spec["target_fps"]
    source_frames, total = spec["frames"], spec["target_frames"]
    cuts = set(spec["cuts"])
    padding = (0, (-width) % 256, 0, (-height) % 256)

    def tensor(rgb):
        data = torch.from_numpy(rgb).permute(2, 0, 1)[None].cuda().float() / 255
        return functional.pad(data, padding, mode="replicate")

    with av.open(str(master)) as source, av.open(str(target), "w") as output:
        stream = output.add_stream("libx264", rate=target_fps, options={"crf": "16", "preset": "fast"})
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        context = stream.codec_context
        context.thread_count = 8
        context.colorspace = context.color_primaries = context.color_range = 1
        context.color_trc = 1 if spec["color_transfer"] == "bt709" else 13
        decoded = (frame.to_ndarray(format="rgb24") for frame in source.decode(video=0))
        previous, following = next(decoded), next(decoded, None)
        previous_gpu = following_gpu = None
        position = 0
        first_interpolation = True
        for index in range(total):
            if time.time() >= spec["deadline"]:
                raise TimeoutError("RIFE compute deadline")
            frame, offset = target_frame_source(index, source_fps, target_fps, cuts, source_frames)
            while position < frame:
                if following is None:
                    raise ValueError("ReCaVSR master ended before the source frame clock")
                previous, following = following, next(decoded, None)
                previous_gpu, following_gpu = following_gpu, None
                position += 1
            rgb = previous
            if offset:
                if previous_gpu is None:
                    previous_gpu = tensor(previous)
                if following_gpu is None:
                    following_gpu = tensor(following)
                with torch.autocast("cuda", dtype=torch.float16):
                    prediction = model(torch.cat((previous_gpu, following_gpu), 1),
                                       offset / target_fps, [32, 16, 8, 4, 2])[2][-1]
                    if first_interpolation and not torch.isfinite(prediction).all():
                        raise ValueError("RIFE returned non-finite pixels")
                first_interpolation = False
                rgb = prediction[0, :, :height, :width].clamp(0, 1).mul(255).round().byte().permute(1, 2, 0).cpu().numpy()
            video_frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
            for packet in stream.encode(video_frame.reformat(format="yuv420p", dst_colorspace="ITU709")):
                output.mux(packet)
            if index % 60 == 0:
                write(root / "status.json", {"state": "rife", "frames": index + 1, "total": total})
        for packet in stream.encode():
            output.mux(packet)
    return target


def remux_video(video, source, target):
    command(["ffmpeg", "-v", "error", "-nostdin", "-y", "-copyts", "-i", video, "-i", source,
             "-map", "0:v:0", "-map", "1:a?", "-c", "copy", "-movflags", "+faststart", target])


def finish(root, spec):
    source = root / "input.mp4"
    use_rife = spec["target_fps"] != spec["source_fps"]
    if sha(source) != spec["input_sha256"] or (use_rife and sha(root / "rife.zip") != spec["rife_zip_sha256"]):
        raise ValueError("source or RIFE archive changed")
    master = root / spec["master_filename"]
    if spec.get("resume_master_sha256"):
        if sha(master) != spec["resume_master_sha256"]:
            raise ValueError("confirmed ReCaVSR master changed")
        check_video(master, spec, spec["source_fps"], spec["frames"])
    else:
        master = superscale(root, spec)

    video = interpolate(root, master, spec) if use_rife else master
    pending = root / "final.partial.mp4"
    final = root / "final.mp4"
    remux_video(video, source, pending)
    check_video(pending, spec, spec["target_fps"], spec["target_frames"])
    command(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", pending, "-f", "null", "-"])
    pending.replace(final)
    files = {path.name: {"sha256": sha(path), "bytes": path.stat().st_size} for path in (final, master)}
    write(root / "receipt.json", {"state": "succeeded", "files": files, "whole_source": True,
                                  "source_frames": spec["frames"], "output_frames": spec["target_frames"],
                                  "source_fps": spec["source_fps"], "target_fps": spec["target_fps"]})
    write(root / "status.json", {"state": "succeeded"})


if __name__ == "__main__":
    def stop(signum, frame):
        raise TimeoutError("resource guard stopped compute")

    signal.signal(signal.SIGTERM, stop)
    root = Path(sys.argv[1])
    spec = json.loads((root / "runtime.json").read_text())
    try:
        finish(root, spec)
    except Exception as error:
        write(root / "status.json", {"state": "failed", "error": type(error).__name__, "detail": str(error)[:1800]})
        raise
