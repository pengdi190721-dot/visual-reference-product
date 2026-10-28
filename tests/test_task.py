import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_task as gate
import meitu_adapter as adapter


def example_task():
    return {"schema": 1, "task_id": "test-task", "revision": 1, "previous": None,
            "questions": [{"id": "product", "state": "deferred", "basis": {"ref": "用户消息2", "quote": "不知道，先生成"}}],
            "outputs": [{"id": "O01", "state": "active", "spec": "3:4完整中文图文页",
                         "source": {"ref": "用户消息1", "quote": "做一张热闹的"}}],
            "requirements": [{"id": "D01", "state": "active", "kind": "visual", "text": "保留可爱手绘装饰和主次层级",
                              "source": {"ref": "用户消息1", "quote": "做一张热闹的"}, "outputs": ["O01"],
                              "required_assets": [], "implementation": "标题旁放爱心，右侧气泡与主体呼应"}], "changes": {}}


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = example_task()
        self.path = self.root / "task-v001.json"
        self.write(self.path, self.data)

    def write(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def next_revision(self):
        newer = copy.deepcopy(self.data)
        newer.update(revision=2, previous={"path": self.path.name, "sha256": gate.digest(self.path)})
        return newer, self.root / "task-v002.json"

    def test_unchanged_requirements_survive_new_layout(self):
        data, path = self.next_revision()
        data["requirements"][0]["implementation"] = "右侧装饰移至标题附近，气泡继续保留"
        self.write(path, data)
        self.assertEqual(gate.load_contract(path)["revision"], 2)

    def test_deleted_requirement_blocked(self):
        data, path = self.next_revision()
        data["requirements"][0]["id"] = "D02"
        self.write(path, data)
        with self.assertRaises(gate.TaskError):
            gate.load_contract(path)

    def test_silent_visual_simplification_blocked(self):
        data, path = self.next_revision()
        data["requirements"][0]["text"] = "删掉可爱装饰，极简"
        self.write(path, data)
        with self.assertRaises(gate.TaskError):
            gate.load_contract(path)
        data["changes"]["D01"] = {"ref": "用户消息3", "quote": "改成极简"}
        self.write(path, data)
        self.assertEqual(gate.load_contract(path)["revision"], 2)

    def test_reference_removal_requires_change_basis(self):
        self.data["requirements"][0]["required_assets"] = ["R01"]
        self.write(self.path, self.data)
        data, path = self.next_revision()
        data["requirements"][0]["required_assets"] = []
        self.write(path, data)
        with self.assertRaises(gate.TaskError):
            gate.load_contract(path)

    def test_prior_file_cannot_be_rewritten(self):
        data, path = self.next_revision()
        self.write(path, data)
        self.data["requirements"][0]["text"] = "改写旧文件"
        self.write(self.path, self.data)
        with self.assertRaises(gate.TaskError):
            gate.load_contract(path)

    def test_answered_product_cannot_return_to_unknown(self):
        self.data["questions"][0].update(state="answered", basis={"ref": "用户消息2", "quote": "黑胶，晴雨两用"})
        self.write(self.path, self.data)
        data, path = self.next_revision()
        data["questions"][0] = {"id": "product", "state": "waiting"}
        self.write(path, data)
        with self.assertRaises(gate.TaskError):
            gate.load_contract(path)

    def test_unanswered_is_not_skip(self):
        self.data["questions"][0] = {"id": "product", "state": "skipped"}
        self.write(self.path, self.data)
        with self.assertRaises(gate.TaskError):
            gate.load_contract(self.path)

    def test_style_answer_does_not_close_product_question(self):
        self.data["questions"] = [{"id": "product", "state": "waiting"},
                                  {"id": "style", "state": "answered", "basis": {"ref": "用户消息3", "quote": "彩色"}}]
        self.write(self.path, self.data)
        task = gate.load_contract(self.path)
        with self.assertRaises(gate.TaskError):
            gate.plan_instructions(task, ["O01"], [])

    def test_explicit_draft_intent_allows_unknown_parameters(self):
        self.assertIn("可爱手绘", gate.plan_instructions(gate.load_contract(self.path), ["O01"], []))

    def test_unplanned_page_rejected(self):
        self.data["outputs"].append(dict(self.data["outputs"][0], id="O02"))
        self.write(self.path, self.data)
        with self.assertRaises(gate.TaskError):
            gate.load_contract(self.path)

    def test_job_cannot_omit_required_style_asset(self):
        self.data["requirements"][0]["required_assets"] = ["R01"]
        self.write(self.path, self.data)
        with self.assertRaises(gate.TaskError):
            gate.plan_instructions(gate.load_contract(self.path), ["O01"], ["P01"])

    def report(self, status="ready"):
        image = self.root / "result.png"
        image.write_bytes(b"fixture-for-hash-not-visual-test")
        report = {"contract_sha256": gate.digest(self.path), "status": status,
                  "artifacts": [{"id": "O01", "path": image.name, "sha256": gate.digest(image), "viewed": True,
                                 "checks": [{"id": "D01", "status": "pass", "evidence": "标题旁爱心和右侧气泡可见（模拟验收）"}]}],
                  "pending_details": []}
        return report, self.root / "delivery.json"

    def test_failed_aesthetic_cannot_be_called_ready(self):
        report, path = self.report()
        report["artifacts"][0]["checks"][0]["status"] = "fail"
        self.write(path, report)
        with self.assertRaises(gate.TaskError):
            gate.check_delivery(self.path, path)
        report["status"] = "incomplete"
        self.write(path, report)
        self.assertEqual(gate.check_delivery(self.path, path)["status"], "incomplete")

    def test_complete_report_accepts_ready_without_claiming_visual_proof(self):
        report, path = self.report()
        self.write(path, report)
        result = gate.check_delivery(self.path, path)
        self.assertEqual((result["status"], result["delivered"], result["expected"]), ("ready", 1, 1))

    def test_missing_two_of_three_pages_cannot_be_complete(self):
        for key in ("O02", "O03"):
            self.data["outputs"].append(dict(self.data["outputs"][0], id=key))
            self.data["requirements"][0]["outputs"].append(key)
        self.write(self.path, self.data)
        report, path = self.report("draft")
        self.write(path, report)
        with self.assertRaises(gate.TaskError):
            gate.check_delivery(self.path, path)

    def test_changed_image_invalidates_previous_review(self):
        report, path = self.report()
        self.write(path, report)
        (self.root / "result.png").write_bytes(b"new-image")
        with self.assertRaises(gate.TaskError):
            gate.check_delivery(self.path, path)

    def test_unviewed_image_cannot_pass(self):
        report, path = self.report()
        report["artifacts"][0]["viewed"] = False
        self.write(path, report)
        with self.assertRaises(gate.TaskError):
            gate.check_delivery(self.path, path)

    def test_generated_detail_can_be_delivered_as_disclosed_draft(self):
        report, path = self.report("draft")
        report["pending_details"] = ["右下折叠示意图由 AI 补出，待核对实物"]
        self.write(path, report)
        self.assertEqual(gate.check_delivery(self.path, path)["status"], "draft")
        report["status"] = "ready"
        self.write(path, report)
        with self.assertRaises(gate.TaskError):
            gate.check_delivery(self.path, path)

    def test_new_requirements_require_new_review(self):
        report, path = self.report()
        self.write(path, report)
        data, new_path = self.next_revision()
        data["requirements"].append(dict(data["requirements"][0], id="D02", text="保持中文语义断行"))
        self.write(new_path, data)
        with self.assertRaises(gate.TaskError):
            gate.check_delivery(new_path, path)

    def test_coverage_cannot_omit_failed_item(self):
        report, path = self.report()
        report["artifacts"][0]["checks"][0]["id"] = "D02"
        self.write(path, report)
        with self.assertRaises(gate.TaskError):
            gate.check_delivery(self.path, path)

    def test_submit_stops_changed_contract_before_any_remote_call(self):
        class NoCalls:
            def call(self, args):
                raise AssertionError("不得调用远端")
        task = gate.load_contract(self.path)
        job = {"generation_authorized": True, "summary_shown": True, "contract": str(self.path),
               "contract_sha256": gate.digest(self.path), "task_snapshot": task,
               "required_instructions": "被删改", "output_ids": ["O01"], "assets": []}
        with self.assertRaises(adapter.AdapterError):
            adapter.submit(NoCalls(), job, self.root / "run")
        self.assertFalse((self.root / "run").exists())


if __name__ == "__main__":
    unittest.main()
