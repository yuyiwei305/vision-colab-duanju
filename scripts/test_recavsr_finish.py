"""Regression cases for the shared finish command and its media boundary."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from drama_factory import cli, core, finishing
import recavsr_remote as worker


class FinishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        core.write_json(self.root / "short-drama.json", {"format": {"aspect_ratio": "16:9"}})
        episode = self.root / "剧集/EP001"
        episode.mkdir(parents=True)
        (episode / "剪辑单.md").write_text("# 剪辑单\n")
        core.write_json(self.root / "cuts.json", [2])

    def prepare(self, size, *options):
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=blue:s={size}:r=24",
            "-frames:v", "5", "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p",
            str(self.root / "input.mp4"),
        ], check=True)
        args = cli.parser().parse_args([
            "finish", "prepare", str(self.root), "--episode", "EP001", "--id", "finish-test",
            "--source", "input.mp4", "--cuts", str(self.root / "cuts.json"),
            "--cuts-evidence", "synthetic fixture boundary", "--authorization", "offline fixture",
            "--max-ccu", "1", "--max-seconds", "300", *options,
        ])
        profile_path = self.root / "recavsr-profile.json"
        profile = core.read_json(finishing.PROFILE)
        bundle = self.root / "fixture-rife.zip"
        bundle.write_bytes(b"offline archive checksum fixture")
        profile["rife_zip_sha256"] = core.file_sha(bundle)
        core.write_json(profile_path, profile)
        args.rife_zip = bundle
        with patch.object(Path, "home", return_value=self.root), patch.object(finishing, "PROFILE", profile_path):
            prepared = cli.execute(args)
        batch_file = Path(prepared["batch"])
        return core.read_json(batch_file), core.read_json(batch_file.with_name("job.json"))

    def test_default_finish_uses_one_full_video_and_returns_only_the_original_audio_final(self):
        batch, job = self.prepare("1920x1080")
        parameters = job["parameters"]
        self.assertEqual(job["adapter"], "recavsr-comfy-colab")
        self.assertIsNone(batch["max_seconds"])
        self.assertIsNone(core.read_json(batch["adapter_config"])["adapters"][job["adapter"]]["timeout_seconds"])
        self.assertEqual((parameters["output_width"], parameters["output_height"]), (3840, 2160))
        self.assertEqual((parameters["target_fps"], parameters["target_frames"]), (60, 13))
        self.assertEqual(len(job["outputs"]), 1)
        self.assertTrue(job["outputs"][0].endswith("-60fps.mp4"))
        self.assertFalse(Path(batch["state_file"]).exists())
        graph = worker.graph(parameters)
        self.assertEqual([node["class_type"] for node in graph.values()],
                         ["LoadVideo", "ReCaVSRLoader", "ReCaVSRUpscale", "SaveVideo"])
        self.assertEqual(graph["4"]["inputs"]["video"], ["1", 0])

    def test_native_h3_portrait_uses_source_aspect_and_optional_output_spec(self):
        _, job = self.prepare("768x1344", "--short-edge", "1440", "--fps", "24")
        parameters = job["parameters"]
        self.assertEqual((parameters["output_width"], parameters["output_height"]), (1440, 2520))
        self.assertEqual((parameters["target_fps"], parameters["target_frames"]), (24, 5))

    def test_rife_holds_at_cut_and_never_reads_beyond_last_frame(self):
        cuts = {2}
        self.assertEqual(worker.target_frame_source(2, 24, 60, cuts, 5), (0, 48))
        self.assertEqual(worker.target_frame_source(3, 24, 60, cuts, 5), (1, 0))
        self.assertEqual(worker.target_frame_source(5, 24, 60, cuts, 5), (2, 0))
        self.assertEqual(worker.target_frame_source(12, 24, 60, cuts, 5), (4, 0))

    def test_remux_preserves_all_original_audio_streams(self):
        source = self.root / "input.mp4"
        master = self.root / "master.mp4"
        final = self.root / "final.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=160x90:r=24:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-itsoffset", "0.1", "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac", str(source),
        ], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-an", "-c:v", "copy", str(master)],
                       check=True)
        worker.finish(self.root, {
            "input_sha256": core.file_sha(source), "master_filename": master.name,
            "resume_master_sha256": core.file_sha(master), "output_width": 160, "output_height": 90,
            "source_fps": 24, "target_fps": 24, "frames": 24, "target_frames": 24,
        })
        self.assertEqual(finishing.verify_original_audio(source, final), "identical")
        self.assertEqual(core.read_json(self.root / "status.json")["state"], "succeeded")
        self.assertEqual(len([s for s in json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(final),
        ]))["streams"] if s["codec_type"] == "audio"]), 2)
