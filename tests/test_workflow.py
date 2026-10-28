import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import meitu_adapter as adapter
import check_analysis as audit
import install as installer
import package as packager


class FakeCLI:
    executable = "designkit"

    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def call(self, args):
        self.calls.append(args)
        if args == ["--version"]:
            return 0, "1.0.75\n"
        if args == ["chat", "--help"]:
            return 0, "--image-file --room-id --prompt"
        if args == ["auth", "status", "--check"]:
            return (1, "disconnected") if self.failure == "auth" else (0, "connected\n")
        if args == ["create-room"]:
            if self.failure == "room":
                return 1, "network failure"
            if self.failure == "room_shape":
                return 0, '{"unexpected":"unknown"}'
            return 0, '{"data":{"room_id":"room-1"}}'
        if args[0] == "chat":
            if self.failure == "chat":
                return 1, "uncertain dispatch"
            if self.failure == "missing_task":
                return 0, '{"message":"not a task confirmation"}'
            if self.failure == "wrong_room":
                return 0, '{"room_id":"other-room","task_id":"task-1"}'
            return 0, '{"room_id":"room-1","task_id":"task-1"}'
        raise AssertionError(args)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # 人工文件头与数据，用于附件映射和散列测试，不作为真实图片视觉验证。
        for name, data in (("产品.png", b"product"), ("风格.png", b"style")):
            (self.root / name).write_bytes(b"\x89PNG\r\n\x1a\n" + data)
        self.job = {"version": 2, "provider": "meitu", "operation": "design", "mode": "creative",
                    "generation_authorized": True, "summary_shown": True,
                    "assets": [{"id":"P01","role":"product","path":"产品.png"},
                               {"id":"R01","role":"style","path":"风格.png"}],
                    "prompt":"保留 [[P01]] 产品，借鉴 [[R01]] 构图。"}

    def loaded(self):
        path = self.root / "job.json"
        path.write_text(json.dumps(self.job), encoding="utf-8")
        return adapter.load_job(path)

    def test_relative_paths_and_unicode(self):
        job = self.loaded()
        self.assertEqual(job["assets"][0]["path"], str((self.root / "产品.png").resolve()))
        self.assertNotEqual(job["assets"][0]["sha256"], job["assets"][1]["sha256"])

    def test_old_provider_plan_rejected(self):
        self.job.update(version=1, provider="libtv")
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_no_legacy_fields_or_room_injection(self):
        for key in ("model_name", "params", "project", "room_id"):
            self.job[key] = "unexpected"
            with self.assertRaises(adapter.AdapterError):
                self.loaded()
            del self.job[key]

    def test_missing_product_rejected(self):
        self.job.update(assets=self.job["assets"][1:], prompt="参考 [[R01]]")
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_concept_without_assets(self):
        self.job.update(mode="concept", assets=[], prompt="一张概念图")
        self.assertEqual(self.loaded()["assets"], [])

    def test_cutout_requires_source(self):
        self.job.update(operation="cutout", assets=self.job["assets"][1:], prompt="参考 [[R01]]")
        with self.assertRaises(adapter.AdapterError):
            self.loaded()
        self.job["assets"][0]["role"] = "source"
        self.assertEqual(self.loaded()["operation"], "cutout")

    def test_missing_file(self):
        self.job["assets"][0]["path"] = "不存在.png"
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_bad_image_header(self):
        (self.root / "产品.png").write_text("not an image")
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_duplicate_or_unmapped_reference(self):
        self.job["assets"][1]["id"] = "P01"
        with self.assertRaises(adapter.AdapterError):
            self.loaded()
        self.job["assets"][1]["id"] = "R01"
        self.job["prompt"] += " [[H01]]"
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_non_boolean_authorization_rejected(self):
        self.job["generation_authorized"] = "false"
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_version_numeric_comparison(self):
        self.assertGreater(adapter.parse_version("1.0.100\n"), (1, 0, 24))
        with self.assertRaises(adapter.AdapterError):
            adapter.parse_version("1.0.75-beta")

    def test_unauthorized_no_calls(self):
        for key in ("generation_authorized", "summary_shown"):
            job = self.loaded()
            job[key] = False
            cli = FakeCLI()
            with self.assertRaises(adapter.AdapterError):
                adapter.submit(cli, job, self.root / key)
            self.assertEqual(cli.calls, [])

    def test_doctor_never_mutates(self):
        cli = FakeCLI()
        self.assertEqual(adapter.doctor(cli, True)["auth"], "connected")
        self.assertEqual(cli.calls, [["--version"], ["chat", "--help"], ["auth", "status", "--check"]])

    def test_disconnected_never_uploads(self):
        cli = FakeCLI("auth")
        with self.assertRaises(adapter.AdapterError):
            adapter.submit(cli, self.loaded(), self.root / "run")
        self.assertFalse(any(c[0] in ("create-room", "chat") and "--help" not in c for c in cli.calls))
        self.assertFalse((self.root / "run/state.json").exists())

    def test_once_and_ordered_roles(self):
        job, cli = self.loaded(), FakeCLI()
        folder = self.root / "run"
        result = adapter.submit(cli, job, folder)
        self.assertEqual(result["state"]["phase"], "submitted")
        adapter.submit(cli, job, folder)
        calls = [c for c in cli.calls if c[0] == "chat" and "--help" not in c]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call[call.index("--image-file")+1:], [a["path"] for a in job["assets"]])
        prompt = call[call.index("--prompt")+1]
        self.assertIn("附件1（P01，产品身份）", prompt)
        self.assertIn("附件2（R01，仅参考风格）", prompt)
        self.assertEqual((folder / "prompt.txt").read_text(), prompt)
        self.assertEqual(result["next_argv"], ["designkit", "history-detail", "--room-id", "room-1", "--watch"])

    def test_uncertain_never_retries(self):
        for failure in ("room", "room_shape", "chat", "missing_task", "wrong_room"):
            with self.subTest(failure=failure):
                folder, cli = self.root / failure, FakeCLI(failure)
                with self.assertRaises(adapter.AdapterError):
                    adapter.submit(cli, self.loaded(), folder)
                count = len(cli.calls)
                with self.assertRaises(adapter.AdapterError):
                    adapter.submit(cli, self.loaded(), folder)
                self.assertEqual(len(cli.calls), count)

    def test_changed_asset_blocks_reuse(self):
        cli = FakeCLI()
        adapter.submit(cli, self.loaded(), self.root / "run")
        (self.root / "产品.png").write_bytes(b"\x89PNG\r\n\x1a\nchanged")
        with self.assertRaises(adapter.AdapterError):
            adapter.submit(cli, self.loaded(), self.root / "run")

    def test_concurrent_lock_blocks(self):
        folder = self.root / "run"
        folder.mkdir()
        (folder / ".submit.lock").touch()
        cli = FakeCLI()
        with self.assertRaises(adapter.AdapterError):
            adapter.submit(cli, self.loaded(), folder)
        self.assertEqual(cli.calls, [])

    def test_subprocess_keeps_prompt_as_data(self):
        dangerous = "提示 `echo x` $(touch injected) \" 换行\n内容"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}")) as run:
            adapter.CLI("designkit").call(["chat", "--prompt", dangerous])
            args, kwargs = run.call_args
            self.assertEqual(args[0][-1], dangerous)
            self.assertFalse(kwargs.get("shell", False))
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_unique_return_id_and_ndjson(self):
        data = adapter.parse_output('{"room_id":"one"}\n{"room_id":"two"}')
        with self.assertRaises(adapter.AdapterError):
            adapter.unique_id(data, "room_id")


def evidence():
    claims = [{"id": "E-" + dimension, "image_id": "R01", "dimension": dimension,
               "statement": "测试结论", "evidence": "测试可见依据", "kind": "observation",
               "confidence": "高", "reason": "仅用来测试数据结构", "bbox": [0, 0, 1, 1],
               "bbox_method": "estimated"} for dimension in sorted(audit.DIMENSIONS)]
    return {"version": 1, "images": [{"id": "R01", "role": "style", "path": "测试图.png", "status": "inspected"}],
            "claims": claims, "groups": [{"id": "G01", "members": ["R01"], "representative": "R01",
                                          "rationale": "测试归类", "evidence_ids": [claims[0]["id"]]}],
            "unresolved_images": [], "transfers": [], "product_facts": []}


class EvidenceTests(unittest.TestCase):
    def draft(self):
        data = evidence()
        data["draft_elements"] = [{"id": "D01", "origin": "generated",
            "location": "右下收纳小图", "content": "未见实拍的收纳形态",
            "invented_or_unverified": "折叠外观由 AI 设想", "confirmation_status": "pending",
            "draft_only": True}]
        return data

    def test_disclosed_detail_draft_allowed(self):
        self.assertEqual(audit.validate(self.draft()), [])

    def test_unconfirmed_detail_cannot_be_ready(self):
        data = self.draft()
        data["delivery_status"] = "ready"
        self.assertTrue(audit.validate(data))

    def test_confirmation_needs_evidence_keeps_origin(self):
        data = self.draft()
        data["draft_elements"][0].update(confirmation_status="confirmed", draft_only=False)
        data["delivery_status"] = "ready"
        self.assertTrue(audit.validate(data))
        data["draft_elements"][0]["confirmation_ref"] = "用户消息：收纳形态与实物一致"
        self.assertEqual(audit.validate(data), [])
        self.assertEqual(data["draft_elements"][0]["origin"], "generated")

    def test_crop_needs_real_product_and_region(self):
        data = self.draft()
        item = data["draft_elements"][0]
        item.update(origin="crop", source_ref="R01", bbox=[0, 0, 1, 1])
        self.assertTrue(audit.validate(data))
        data["images"].append({"id": "P01", "role": "product", "path": "原图.png", "status": "inspected"})
        item["source_ref"] = "P01"
        self.assertEqual(audit.validate(data), [])
        item["bbox"] = [0.5, 0.5, 0.2, 1]
        self.assertTrue(audit.validate(data))

    def test_rejected_detail_must_leave_current_delivery(self):
        data = self.draft()
        item = data["draft_elements"][0]
        item.update(confirmation_status="rejected")
        self.assertTrue(audit.validate(data))
        item["included_in_current"] = False
        data["delivery_status"] = "ready"
        self.assertEqual(audit.validate(data), [])

    def test_complete_record(self):
        self.assertEqual(audit.validate(evidence()), [])

    def test_skipped_image_detected(self):
        data = evidence()
        data["images"].append({"id": "R02", "role": "style", "path": "测试二.png", "status": "inspected"})
        self.assertTrue(audit.validate(data))

    def test_bad_region(self):
        data = evidence()
        data["claims"][0]["bbox"] = [0.9, 0, 0.2, 1]
        self.assertTrue(audit.validate(data))

    def test_inference_needs_alternative(self):
        data = evidence()
        data["claims"][0]["kind"] = "inference"
        self.assertTrue(audit.validate(data))

    def test_low_confidence_cannot_be_transfer(self):
        data = evidence()
        data["claims"][0]["confidence"] = "低"
        data["transfers"] = [{"evidence_ids": [data["claims"][0]["id"]], "implementation": "测试迁移"}]
        self.assertTrue(audit.validate(data))

    def test_unknown_not_high(self):
        data = evidence()
        data["claims"][0]["kind"] = "unknown"
        self.assertTrue(audit.validate(data))

    def test_duplicate_group_members(self):
        data = evidence()
        data["groups"].append({**data["groups"][0], "id": "G02"})
        self.assertTrue(audit.validate(data))

    def test_image_cannot_prove_function(self):
        data = evidence()
        data["images"].append({"id": "P01", "role": "product", "path": "产品.png", "status": "inspected"})
        data["product_facts"] = [{"field": "防晒系数", "value": "50+", "source_kind": "image", "source_ref": "P01", "category": "specification"}]
        self.assertTrue(audit.validate(data))

    def test_unknown_value_is_null(self):
        data = evidence()
        data["product_facts"] = [{"field": "尺寸", "value": "53厘米", "source_kind": "unknown", "category": "specification"}]
        self.assertTrue(audit.validate(data))

    def test_creative_is_not_product_fact(self):
        data = evidence()
        data["product_facts"] = [{"field": "材料", "value": "纯棉", "source_kind": "creative", "source_ref": "创意", "category": "specification", "locked_fact": True}]
        self.assertTrue(audit.validate(data))

    def test_unreadable_is_not_inspected(self):
        data = evidence()
        data["images"][0].update(status="unreadable", reason="打不开")
        self.assertTrue(audit.validate(data))


class InstallTests(unittest.TestCase):
    def test_install_verify_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("---\nname: visual-reference-product\ndescription: 测试\n---\n", encoding="utf-8")
            (source / "说明.md").write_text("中文路径测试", encoding="utf-8")
            target, count = installer.install(source, root / "中文安装目录")
            self.assertEqual(count, 2)
            self.assertTrue((target / "说明.md").is_file())
            installer.install(source, root / "中文安装目录", check=True)
            with self.assertRaises(ValueError):
                installer.install(source, root / "中文安装目录")
            (target / "说明.md").write_text("损坏", encoding="utf-8")
            with self.assertRaises(ValueError):
                installer.install(source, root / "中文安装目录", check=True)

    def test_package_excludes_private_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skill"
            root.mkdir()
            for name in packager.ROOT_FILES:
                (root / name).write_text("测试", encoding="utf-8")
            (root / ".env").write_text("PRIVATE_DATA", encoding="utf-8")
            (root / ".gitignore").write_text("private/", encoding="utf-8")
            (root / "LICENSE").write_text("旧版无扩展名文件", encoding="utf-8")
            (root / "private").mkdir()
            (root / "private" / "客户.png").write_bytes(b"private")
            (root / "scripts").mkdir()
            (root / "scripts" / "test.py").write_text("pass", encoding="utf-8")
            target = Path(directory) / "包.zip"
            count = packager.package(root, target)
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), count)
                self.assertFalse(any(".env" in n or "private" in n for n in names))
                self.assertIn("visual-reference-product/SKILL.md", names)
                self.assertNotIn("visual-reference-product/.gitignore", names)
                self.assertNotIn("visual-reference-product/LICENSE", names)
                self.assertEqual(archive.read("visual-reference-product/LICENSE.md"),
                                 (root / "LICENSE.md").read_bytes())
                self.assertTrue(all(Path(n).suffix in {".md", ".yaml", ".json", ".py"}
                                    and not Path(n).name.startswith(".") for n in names))

    def test_package_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skill"
            root.mkdir()
            for name in packager.ROOT_FILES:
                (root / name).write_text("测试", encoding="utf-8")
            a, b = Path(directory) / "a.zip", Path(directory) / "b.zip"
            packager.package(root, a)
            packager.package(root, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())


if __name__ == "__main__":
    unittest.main()
