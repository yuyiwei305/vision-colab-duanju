"""Exercise the local review CLI with non-numeric story order and a real fixture receipt."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import colab_h3
from drama_factory.core import ROOT


class MotionReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        self.drama("project", "init", str(self.root), "--title", "Fixture", "--aspect-ratio", "16:9")
        self.episode = self.root / "剧集/EP001"
        self.episode.mkdir(parents=True)
        self.order = [f"MOTION-EP001-{n}" for n in ("001", "004", "002", "003")]
        blocks = ["## SEQUENCE-EP001-001 · 场内\n- 计划时长：16s"]
        for i, entry in enumerate(self.order):
            blocks.append(f"## {entry} · 镜头\n- 段落：SEQUENCE-EP001-001\n"
                          f"- 段落区间：{i*4}s-{(i+1)*4}s\n- 入段：独立\n"
                          f"## SHOT-EP001-{i+1:03d} · 镜头\n- 生成段：{entry}\n- 段内区间：0s-4s\n"
                          "- 起点：门闭合。\n- 唯一动作：抬眼。\n- 终点：侧望门外。\n- 声音：脚步。")
        (self.episode / "分镜.md").write_text("\n".join(blocks), encoding="utf-8")
        self.target = "剧集/EP001/制作成果/video/fixture.mp4"

    def drama(self, *args):
        return json.loads(subprocess.check_output(
            ["bash", str(ROOT / "scripts/drama"), *args], cwd=ROOT, text=True))

    def review(self, *entries):
        args = [arg for entry in entries for arg in ("--motion", entry)]
        return self.drama("project", "review-motion", str(self.root), "--episode", "EP001", *args)

    def test_review_before_production_follows_story_order_and_skips_unreceipted_file(self):
        path = self.root / self.target
        path.parent.mkdir(parents=True)
        path.write_bytes(b"no receipt")
        result = self.review(self.order[1], self.order[0])
        self.assertEqual([row["motion"] for row in result["motions"]], self.order[:3])
        self.assertEqual([row["requested"] for row in result["motions"]], [True, True, False])
        self.assertTrue(all(not row["takes"] for row in result["motions"]))
        self.assertIsNone(result["cut_list"])
        self.assertEqual(result["issues"], [])

    def test_receipt_links_exact_cut_and_rejects_replaced_bytes(self):
        motion = self.order[1]
        source = "剧集/EP001/视频提示词.md"
        (self.root / source).write_text(f"## {motion} · 抬眼\n- 输入参考图：无\n"
                                       "### 可复制提示词\n> Looking up.\n", encoding="utf-8")
        job = self.root / "job.json"
        job.write_text(json.dumps({"job_id": "fixture", "adapter": "fixture", "modality": "video",
                                   "source": source, "source_entry": motion, "prompt": "Looking up.",
                                   "outputs": [self.target], "parameters": {"ratio": "16:9"}}), encoding="utf-8")
        adapter = ROOT / "ref/drama-skills/skills/short-drama-produce/scripts/fixture_adapter.py"
        config = self.root.parent / "adapter.json"
        config.write_text(json.dumps({"adapters": {"fixture": {"command": [sys.executable, str(adapter)]}}}))
        production = colab_h3.engine()
        preview = production.prepare_job(self.root, job)
        production.confirm_job(self.root, job_id="fixture", confirmation=preview["confirmation"])
        production.run_job(self.root, job_id="fixture", adapter_config=config)
        (self.episode / "剪辑单.md").write_text(
            f"## CUT-EP001-001 · 抬眼\n- 来源：{motion} · 制作成果/video/fixture.mp4\n"
            "- 入点：1\n- 出点：3\n- 时长：2\n", encoding="utf-8")
        result = self.review(motion)
        take, = result["motions"][1]["takes"]
        self.assertEqual((take["job"], take["path"]), ("fixture", self.target))
        self.assertEqual(take["cuts"], [{"id": "CUT-EP001-001", "in": 1, "out": 3}])
        media = self.root / self.target
        media.write_bytes(b"x" * media.stat().st_size)
        changed = self.review(motion)
        self.assertEqual(changed["motions"][1]["takes"], [])
        self.assertTrue(any(self.target in issue for issue in changed["issues"]))
