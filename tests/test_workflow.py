import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import libtv_adapter as adapter
import check_analysis as audit
import install as installer
import package as packager


def schema():
    # 人工构造的接口测试数据，不代表线上模型承诺。
    return {"modality": "image", "schema": {"properties": {
        "prompt": {"maxLength": 800}, "count": [1, 2, 4],
        "ratio": {"enum": ["3:4", "1:1"]},
        "sequential": {"component": "switch", "default": 1},
        "modeType": {"items": {"image2image": [1, 3]}}
    }, "rules": [{"require": ["prompt", "image"], "mode": "all"}]}}


class FakeCLI:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def call(self, args, log=None, raw=False):
        self.calls.append(args)
        if self.fail == "run" and "--run" in args:
            raise adapter.AdapterError("模拟提交后断线")
        if self.fail == "upload" and args[0] == "upload":
            raise adapter.AdapterError("模拟上传后断线")
        if self.fail == "create" and args[:2] == ["node", "create"]:
            return [{"unexpected": "节点可能已创建，但字段变化"}]
        if args[:2] == ["model", "search"]:
            return [{"matches": [{"modelName": "测试模型"}]}]
        if args[0] == "model":
            return [schema()]
        if args[0] == "upload":
            return [{"nodeKey": "asset-" + args[1].split("-")[-1]}]
        if args[:2] == ["node", "create"]:
            return [{"newNodeKey": "generated-node"}]
        return [{"status": "completed"}]


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # 仅路径和内容散列测试；不伪称这些字节是真实图像。
        (self.root / "产品.png").write_bytes(b"synthetic-test-bytes")
        (self.root / "风格.png").write_bytes(b"other-synthetic-test-bytes")
        self.job = {"version": 1, "project": "11111111-2222-3333-4444-555555555555",
                    "model_name": "测试模型", "mode": "creative",
                    "generation_authorized": True, "summary_shown": True,
                    "assets": [{"id": "P01", "role": "product", "path": "产品.png"},
                               {"id": "R01", "role": "style", "path": "风格.png"}],
                    "params": {"count": 1, "ratio": "3:4", "sequential": 0},
                    "prompt": "保留 [[P01]] 产品，借鉴 [[R01]] 构图。"}

    def loaded(self):
        path = self.root / "job.json"
        path.write_text(json.dumps(self.job), encoding="utf-8")
        return adapter.load_job(path)

    def test_relative_paths_and_unicode(self):
        self.assertEqual(self.loaded()["assets"][0]["path"], str((self.root / "产品.png").resolve()))

    def test_missing_product_rejected(self):
        self.job["assets"] = self.job["assets"][1:]
        self.job["prompt"] = "参考 [[R01]]"
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_concept_without_product(self):
        self.job.update(mode="concept", assets=self.job["assets"][1:], prompt="参考 [[R01]] 做概念图")
        self.assertEqual(self.loaded()["mode"], "concept")

    def test_missing_file(self):
        self.job["assets"][0]["path"] = "不存在.png"
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_duplicate_asset_id(self):
        self.job["assets"][1]["id"] = "P01"
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_unmapped_reference(self):
        self.job["prompt"] += " [[H01]]"
        with self.assertRaises(adapter.AdapterError):
            self.loaded()

    def test_internal_model_key_not_accepted(self):
        job = self.loaded()
        job["model_name"] = "internal-test-key"
        with self.assertRaises(adapter.AdapterError):
            adapter.preflight(FakeCLI(), job)

    def test_invalid_model_params(self):
        for params in ({"count": 1, "ratio": "9:99", "sequential": 0},
                       {"count": 1}, {"count": 4, "sequential": 0},
                       {"count": 1, "sequential": 0, "invented": 1}):
            with self.subTest(params=params):
                job = self.loaded()
                job["params"] = params
                with self.assertRaises(adapter.AdapterError):
                    adapter.validate_schema(job, schema(), job["prompt"])

    def test_reference_limit(self):
        job = self.loaded()
        job["assets"] *= 2
        with self.assertRaises(adapter.AdapterError):
            adapter.validate_schema(job, schema(), job["prompt"])

    def test_prompt_limit_counts_unicode(self):
        with self.assertRaises(adapter.AdapterError):
            adapter.validate_schema(self.loaded(), schema(), "🌂" * 401)

    def test_unknown_schema_fails_closed(self):
        response = schema()
        response["schema"]["rules"][0]["newRule"] = True
        with self.assertRaises(adapter.AdapterError):
            adapter.validate_schema(self.loaded(), response, "提示词")

    def test_preflight_never_mutates(self):
        cli = FakeCLI()
        adapter.preflight(cli, self.loaded())
        self.assertTrue(all(c[0] == "model" for c in cli.calls))

    def test_unauthorized_never_calls_cli(self):
        for field in ("generation_authorized", "summary_shown"):
            job = self.loaded()
            job[field] = False
            cli = FakeCLI()
            with self.assertRaises(adapter.AdapterError):
                adapter.execute(cli, job, self.root)
            self.assertEqual(cli.calls, [])

    def test_exactly_one_run_and_correct_roles(self):
        job = self.loaded()
        cli = FakeCLI()
        adapter.execute(cli, job, self.root)
        adapter.execute(cli, job, self.root)
        self.assertEqual(sum("--run" in c for c in cli.calls), 1)
        creation = next(c for c in cli.calls if c[:2] == ["node", "create"])
        prompt = creation[creation.index("--prompt") + 1]
        self.assertIn("{{Node asset-P01}}", prompt)
        self.assertIn("{{Node asset-R01}}", prompt)
        self.assertEqual(creation.count("--left"), 2)
        self.assertTrue(all("-p" in c for c in cli.calls if c[0] in ("upload", "node")))

    def test_unknown_remote_state_never_retried(self):
        for failure in ("upload", "create", "run"):
            with self.subTest(failure=failure):
                directory = self.root / failure
                directory.mkdir()
                cli = FakeCLI(failure)
                with self.assertRaises(adapter.AdapterError):
                    adapter.execute(cli, self.loaded(), directory)
                count = len(cli.calls)
                with self.assertRaises(adapter.AdapterError):
                    adapter.execute(cli, self.loaded(), directory)
                self.assertEqual(len(cli.calls), count)

    def test_changed_material_blocks_resume(self):
        cli = FakeCLI()
        adapter.execute(cli, self.loaded(), self.root)
        (self.root / "产品.png").write_bytes(b"changed")
        with self.assertRaises(adapter.AdapterError):
            adapter.execute(cli, self.loaded(), self.root)

    def test_subprocess_treats_prompt_as_data(self):
        dangerous = "提示 `echo x` $(touch injected) \" 换行\n内容"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"ok":true}')) as run:
            adapter.CLI("libtv", self.root).call(["node", "abc", "--prompt", dangerous])
            args, kwargs = run.call_args
            self.assertEqual(args[0][-1], dangerous)
            self.assertFalse(kwargs.get("shell", False))
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_ndjson_and_unique_node(self):
        self.assertEqual(len(adapter.parse_output('{"a":1}\n{"b":2}\n')), 2)
        with self.assertRaises(adapter.AdapterError):
            adapter.node_key([{"nodeKey": "one"}, {"nodeKey": "two"}])


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
