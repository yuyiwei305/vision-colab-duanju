"""Offline tests of GROUP safety boundaries; never call a paid provider."""
import importlib.util
import contextlib
import io
import json
import hashlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("group_bridge", Path(__file__).with_name("drama-group-production.py"))
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


class GroupProductionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.episode = self.root / "剧集/EP01"
        self.episode.mkdir(parents=True)
        self.engine = BRIDGE.load_engine()
        self.profile = {"creator_authority": {"production_profile": {"choices": {
            "multi_shot_containers": True, "video_prompt_dialect": "seedance-2.5"}}}}
        (self.root / "short-drama.json").write_text(json.dumps(self.profile))
        self.reference = "REF-ACTOR（顺序：1）· actor.png《角色板》（用途：身份；控制：身份；不得控制：动作）"
        (self.root / "actor.png").write_bytes(b"offline-test-image")
        (self.episode / "剧本.md").write_text("## EP001-SC001\n一场戏。\n")
        (self.episode / "视觉设定.md").write_text("角色设定。\n")
        board, motions = [], []
        for n, duration in [(1, 5), (2, 7)]:
            board.append(f"## SHOT-EP001-{n:03d} · 镜头\n- 来源：EP001-SC001\n- 时长：{duration}s\n- 输入参考图：{self.reference}\n")
            motions.append(f"## MOTION-EP001-{n:03d} · 镜头\n- 分镜：SHOT-EP001-{n:03d}\n- 时长：{duration}s\n- 输入参考图：{self.reference}\n\n### 可复制提示词\n> 动作。\n")
        (self.episode / "分镜.md").write_text("\n".join(board))
        self.prompt = "镜头 1 [00:00–00:05]：动作。\n镜头 2 [00:05–00:12]：反应。"
        group = f"## GROUP-EP001-001 · 双镜\n- 成员：MOTION-EP001-001、MOTION-EP001-002\n- 时长：12s\n- 生成方式：图生视频\n- 任务类型：reference\n- 输入参考图：{self.reference}\n\n### 可复制提示词\n" + "\n".join("> " + line for line in self.prompt.splitlines()) + "\n"
        self.video = self.episode / "视频提示词.md"
        self.video.write_text("\n".join(motions) + "\n" + group)
        self.raw = {"job_id": "group-test", "modality": "video", "adapter": "offline", "source": "剧集/EP01/视频提示词.md", "source_entry": "GROUP-EP001-001", "prompt": self.prompt,
                    "reference_bindings": [{"slot_id": "REF-ACTOR", "order": 1, "path": "actor.png", "label": "角色板", "role": "reference_image", "may_control": ["身份"], "must_not_control": ["动作"]}],
                    "outputs": ["剧集/EP01/制作成果/video/test.mp4"], "parameters": {"duration": 12, "omni_reference_task_type": "reference"}, "overwrite": False}
        self.job_file = self.root / "job.json"
        self.adapter = self.root.parent / (self.root.name + "-adapters.json")
        self.addCleanup(lambda: self.adapter.unlink(missing_ok=True))
        fixture = BRIDGE.ROOT / "ref/drama-skills/skills/short-drama-produce/scripts/fixture_adapter.py"
        self.adapter.write_text(json.dumps({"adapters": {"offline": {"command": [sys.executable, str(fixture)], "timeout_seconds": 10, "reference_roles": ["reference_image"]}}}))

    def prepare(self):
        self.job_file.write_text(json.dumps(self.raw))
        return self.engine.prepare_job(self.root, self.job_file)

    def test_real_group_id_and_dependencies_survive_storage(self):
        self.prepare()
        stored = self.engine._read_job(self.root, "group-test")
        self.assertEqual(stored["source_entry"], "GROUP-EP001-001")
        self.assertEqual(len(stored["inputs"]), 6)
        self.assertIn("short-drama.json", stored["inputs"])

    def test_no_generation_without_confirmation(self):
        self.prepare()
        with self.assertRaises(self.engine.ConfirmationRequiredError):
            self.engine.run_job(self.root, job_id="group-test", adapter_config=self.adapter)

    def test_confirmation_is_consumed_and_cannot_run_twice(self):
        preview = self.prepare()
        self.engine.confirm_job(self.root, job_id="group-test", confirmation=preview["confirmation"])
        self.engine.run_job(self.root, job_id="group-test", adapter_config=self.adapter)
        with self.assertRaises(self.engine.ConfirmationRequiredError):
            self.engine.run_job(self.root, job_id="group-test", adapter_config=self.adapter)

    def test_changed_script_invalidates_confirmation(self):
        preview = self.prepare()
        (self.episode / "剧本.md").write_text("changed")
        with self.assertRaises(self.engine.ConfirmationRequiredError):
            self.engine.confirm_job(self.root, job_id="group-test", confirmation=preview["confirmation"])

    def test_collect_recovers_same_task_after_documents_change(self):
        config = json.loads(self.adapter.read_text())
        config["adapters"]["offline"]["command"].append("--submit-then-fail")
        self.adapter.write_text(json.dumps(config))
        preview = self.prepare()
        self.engine.confirm_job(self.root, job_id="group-test", confirmation=preview["confirmation"])
        with self.assertRaises(self.engine.AdapterError):
            self.engine.run_job(self.root, job_id="group-test", adapter_config=self.adapter)
        (self.episode / "剧本.md").write_text("changed after submission")
        recovered = self.engine.collect_job(self.root, job_id="group-test", adapter_config=self.adapter)
        self.assertEqual(recovered["provider_job_id"], "fixture-task-1")
        self.assertTrue(recovered["collected"])

    def test_bad_duration_rejected(self):
        for duration in [4, 30, 31, True]:
            with self.subTest(duration=duration):
                self.raw["parameters"]["duration"] = duration
                with self.assertRaises(ValueError): self.prepare()

    def use_fast(self):
        self.profile["creator_authority"]["production_profile"]["choices"]["video_prompt_dialect"] = "seedance-2.0"
        (self.root / "short-drama.json").write_text(json.dumps(self.profile))
        ordinal = "镜头1：动作。\n镜头2：反应。"
        text = self.video.read_text()
        self.video.write_text(text.replace(self.prompt, ordinal).replace("> 镜头 1 [00:00–00:05]：动作。\n> 镜头 2 [00:05–00:12]：反应。", "> 镜头1：动作。\n> 镜头2：反应。"))
        self.raw["prompt"] = ordinal
        self.raw["parameters"].pop("omni_reference_task_type")

    def test_fast_ordinals_and_profile_are_accepted(self):
        self.use_fast()
        self.prepare()

    def test_fast_rejects_oversize_container(self):
        self.use_fast()
        self.raw["parameters"]["duration"] = 16
        with self.assertRaisesRegex(ValueError, "4 to 15"):
            self.prepare()

    def test_fast_rejects_25_task_type(self):
        self.use_fast()
        self.raw["parameters"]["omni_reference_task_type"] = "reference"
        with self.assertRaisesRegex(ValueError, "task-type"):
            self.prepare()

    def test_portrait_project_rejects_missing_or_landscape_ratio(self):
        self.profile["format"] = {"aspect_ratio": "9:16"}
        (self.root / "short-drama.json").write_text(json.dumps(self.profile))
        for ratio in (None, "16:9"):
            with self.subTest(ratio=ratio):
                if ratio:
                    self.raw["parameters"]["ratio"] = ratio
                with self.assertRaisesRegex(ValueError, "aspect ratio"):
                    self.prepare()
        self.raw["parameters"]["ratio"] = "9:16"
        self.prepare()

    @unittest.skipUnless((BRIDGE.ROOT / "scripts/drama-credit-adapter.py").is_file(), "local adapter")
    def test_seedance_20_rejects_mixed_input_modes(self):
        spec = importlib.util.spec_from_file_location("credit_modes_test", BRIDGE.ROOT / "scripts/drama-credit-adapter.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        fake = types.SimpleNamespace(compile_seedance_payload=lambda *a, **kw: {}, _http_failure=lambda *args: None)
        adapter.adapt_seedance(fake)
        for roles in (["first_frame", "reference_image"], ["last_frame"], ["reference_image"] * 10):
            with self.subTest(roles=roles), self.assertRaises(ValueError):
                fake.compile_seedance_payload({"parameters": {}}, model="doubao-seedance-2-0-260128", reference_roles=roles)
        fake.compile_seedance_payload({"parameters": {}}, model="doubao-seedance-2-0-260128", reference_roles=["first_frame", "last_frame"])

    def test_project_aspect_extension_keeps_other_format_fields_closed(self):
        spec = importlib.util.spec_from_file_location("project_aspect_test", BRIDGE.ROOT / "scripts/drama-project.py")
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        engine = bridge.load_engine()
        project = {"format": {"aspect_ratio": "16:9"}}
        tokens = engine._authority_tokens("/format/aspect_ratio")
        engine._write_authority_value(project, tokens, "9:16")
        self.assertEqual(project["format"]["aspect_ratio"], "9:16")
        with self.assertRaises(ValueError):
            engine._authority_tokens("/format/unknown")
        with self.assertRaises(ValueError):
            engine._write_authority_value(project, tokens, ["9:16"])

    def test_prompt_drift_rejected(self):
        self.raw["prompt"] += "changed"
        with self.assertRaises(ValueError): self.prepare()

    def test_reordered_members_rejected(self):
        self.video.write_text(self.video.read_text().replace("MOTION-EP001-001、MOTION-EP001-002", "MOTION-EP001-002、MOTION-EP001-001"))
        with self.assertRaises(ValueError): self.prepare()

    def test_cross_scene_rejected(self):
        path = self.episode / "分镜.md"
        text = path.read_text(); pos = text.rfind("EP001-SC001")
        path.write_text(text[:pos] + text[pos:].replace("EP001-SC001", "EP001-SC002", 1))
        with self.assertRaises(ValueError): self.prepare()

    def test_reference_role_cannot_silently_change(self):
        self.raw["reference_bindings"][0]["role"] = "first_frame"
        with self.assertRaises(ValueError): self.prepare()

    def test_unsafe_output_rejected(self):
        self.raw["outputs"] = ["输入/test.mp4"]
        with self.assertRaises(ValueError): self.prepare()

    def test_existing_motion_path_unchanged(self):
        self.raw["source_entry"] = "MOTION-EP001-001"
        self.raw["prompt"] = "动作。"
        self.raw["parameters"] = {"duration": 5}
        self.prepare()
        stored = self.engine._read_job(self.root, "group-test")
        self.assertEqual(len(stored["inputs"]), 2)

    @unittest.skipUnless((BRIDGE.ROOT / "scripts/drama-credit-adapter.py").is_file(), "local adapter")
    def test_gateway_headers_and_profile_error_are_preserved(self):
        spec = importlib.util.spec_from_file_location("credit_adapter_test", BRIDGE.ROOT / "scripts/drama-credit-adapter.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        fake = self.root / "offline_provider.py"
        fake.write_text('import json\ndef _request_json(*args, **kwargs):\n return {}, {}\ndef main():\n print(json.dumps({"error":{"provider":"seedance","category":"permission","code":"http_403","http_status":403,"retryable":False}}))\n return 1\n')
        adapter.ADAPTER = fake
        captured = io.StringIO()
        with patch.object(adapter, "configure", return_value={"key_configured": True}), patch.object(sys, "argv", ["adapter", "seedance", "--key", "1"]), patch.object(adapter.urllib.request, "install_opener") as install, contextlib.redirect_stdout(captured):
            result = adapter.main()
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(captured.getvalue())["error"]["provider"], "credit-seedance-key1")
        self.assertIn(("User-Agent", "credit-media-skill/1.0"), install.call_args.args[0].addheaders)

    @unittest.skipUnless((BRIDGE.ROOT / "scripts/drama-credit-adapter.py").is_file(), "local adapter")
    def test_explicit_resolution_is_forwarded_without_mutating_job(self):
        spec = importlib.util.spec_from_file_location("credit_resolution_test", BRIDGE.ROOT / "scripts/drama-credit-adapter.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        fake = types.SimpleNamespace(compile_seedance_payload=lambda job, **kw: dict(job["parameters"]), _http_failure=lambda *args: None)
        adapter.adapt_seedance(fake)
        job = {"parameters": {"duration": 16, "resolution": "720p"}}
        self.assertEqual(fake.compile_seedance_payload(job)["resolution"], "720p")
        self.assertEqual(job["parameters"]["resolution"], "720p")
        with self.assertRaises(ValueError):
            fake.compile_seedance_payload({"parameters": {"resolution": "4k"}})

    @unittest.skipUnless((BRIDGE.ROOT / "scripts/drama-credit-adapter.py").is_file(), "local adapter")
    def test_provider_diagnostic_redacts_credentials(self):
        spec = importlib.util.spec_from_file_location("credit_diagnostic_test", BRIDGE.ROOT / "scripts/drama-credit-adapter.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        adapter.ROOT = self.root
        fake = types.SimpleNamespace(compile_seedance_payload=lambda *a, **kw: {}, _http_failure=lambda *args: None, MAX_ERROR_BODY_BYTES=65536)
        adapter.adapt_seedance(fake)
        raw = json.dumps({"error": {"message": "rejected SECRET-FOR-OFFLINE-TEST"}}).encode()
        error = adapter.urllib.error.HTTPError("https://example.test/tasks", 400, "bad", {}, io.BytesIO(raw))
        with patch.dict(adapter.os.environ, {"ARK_API_KEY": "SECRET-FOR-OFFLINE-TEST"}):
            fake._http_failure("seedance", error)
        diagnostic = (self.root / ".short-drama/diagnostics/provider-latest-error.json").read_text()
        self.assertNotIn("SECRET-FOR-OFFLINE-TEST", diagnostic)
        self.assertIn("[REDACTED]", diagnostic)

    @unittest.skipUnless((BRIDGE.ROOT / "scripts/drama-credit-adapter.py").is_file(), "local adapter")
    def test_hosted_reference_must_match_local_bytes(self):
        spec = importlib.util.spec_from_file_location("credit_hosted_test", BRIDGE.ROOT / "scripts/drama-credit-adapter.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        adapter.ROOT = self.root
        path = self.root / "actor.png"
        mapping = self.root / "references.json"
        mapping.write_text(json.dumps({"actor.png": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "url": "https://example.test/reference.png"}}))
        fake = types.SimpleNamespace(compile_seedance_payload=lambda *a, **kw: {}, _http_failure=lambda *args: None, _inline_reference_urls=lambda *args, **kw: ["data:offline"])
        adapter.adapt_seedance(fake, mapping)
        self.assertEqual(fake._inline_reference_urls([path], ["reference_image"]), ["https://example.test/reference.png"])
        staged = self.root / "staging/actor.png"
        staged.parent.mkdir()
        staged.write_bytes(path.read_bytes())
        self.assertEqual(fake._inline_reference_urls([staged], ["reference_image"]), ["https://example.test/reference.png"])
        path.write_bytes(b"changed reference")
        with self.assertRaisesRegex(ValueError, "does not match"):
            fake._inline_reference_urls([path], ["reference_image"])

    @unittest.skipUnless((BRIDGE.ROOT / "scripts/drama-credit-adapter.py").is_file(), "local adapter")
    def test_curl_transport_keeps_credentials_off_argv_and_submits_once(self):
        spec = importlib.util.spec_from_file_location("credit_curl_test", BRIDGE.ROOT / "scripts/drama-credit-adapter.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        fake = types.SimpleNamespace(MAX_JSON_RESPONSE=10000)
        adapter.use_curl_json_transport(fake)

        def curl(argv, **kwargs):
            self.assertEqual(argv, ["curl", "--config", "-"])
            lines = kwargs["input"].splitlines()
            output = next(line.split(" = ", 1)[1] for line in lines if line.startswith("output = "))
            Path(json.loads(output)).write_text('{"id":"offline-task"}')
            self.assertIn('header = "Authorization: Bearer offline-secret"', lines)
            self.assertFalse(any("retry" in line for line in lines))
            return types.SimpleNamespace(returncode=0, stdout="200")

        with patch.object(adapter.subprocess, "run", side_effect=curl) as run:
            response, _ = fake._request_json("https://example.test/tasks", provider="seedance", body={"prompt": "offline"}, token="offline-secret")
        self.assertEqual(response["id"], "offline-task")
        self.assertEqual(run.call_count, 1)

    @unittest.skipUnless((BRIDGE.ROOT / "scripts/drama-credit-adapter.py").is_file(), "local adapter")
    def test_actual_gateway_state_is_recorded_without_guessing(self):
        spec = importlib.util.spec_from_file_location("credit_status_test", BRIDGE.ROOT / "scripts/drama-credit-adapter.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        adapter.ROOT = self.root
        raw = {"id": "existing-task", "status": "submitted"}
        request = types.SimpleNamespace(_request_json=lambda *args, **kwargs: (raw, {}))
        adapter.capture_seedance_status(request)
        result, _ = request._request_json("https://example.test/contents/generations/tasks/existing-task", method="GET")
        self.assertEqual(result["status"], "submitted")
        recorded = json.loads((self.root / ".short-drama/diagnostics/task-status/existing-task.json").read_text())
        self.assertEqual(recorded, raw)
        created, _ = request._request_json("https://example.test/contents/generations/tasks", method="POST")
        self.assertEqual(created["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
