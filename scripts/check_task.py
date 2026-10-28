#!/usr/bin/env python3
"""检查需求继承、计划覆盖和交付证据；不代替看图，不验证用户引文真假。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys


class TaskError(ValueError):
    pass


def need(ok, message):
    if not ok:
        raise TaskError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def records(items, label):
    need(isinstance(items, list) and items, label + "不能为空。")
    result = {}
    for item in items:
        need(isinstance(item, dict) and nonempty(item.get("id")), label + "缺少编号。")
        need(item["id"] not in result, label + "编号重复。")
        result[item["id"]] = item
    return result


def source(value):
    return (isinstance(value, dict) and nonempty(value.get("ref"))
            and nonempty(value.get("quote")))


def load_contract(path, seen=None):
    path = Path(path).resolve()
    seen = set() if seen is None else seen
    need(path not in seen and len(seen) < 100, "需求版本循环或过深，请保留历史并整理接续记录。")
    seen.add(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    need(isinstance(data, dict) and data.get("schema") == 1, "不支持的需求清单格式。")
    need(nonempty(data.get("task_id")), "缺少任务标识。")
    revision = data.get("revision")
    need(type(revision) is int and revision >= 1, "版本号须为正整数。")
    requirements = records(data.get("requirements"), "需求")
    outputs = records(data.get("outputs"), "交付物")
    questions = records(data.get("questions"), "问卷状态")
    need("product" in questions, "必须单独记录产品信息问题，不能由风格选择代答。")
    for q in questions.values():
        need(q.get("state") in ("unasked", "waiting", "answered", "skipped", "deferred", "not_applicable"), "问题状态无效。")
        if q["state"] in ("answered", "skipped", "deferred"):
            need(source(q.get("basis")), "已答、跳过或先出草稿必须记录对应用户原话及位置。")
        if q["state"] == "not_applicable":
            need(nonempty(q.get("reason")), "问题不适用须说明原因。")
    active_outputs = set()
    for key, item in outputs.items():
        need(item.get("state") in ("active", "retired") and nonempty(item.get("spec")), "交付物须有状态和具体规格。")
        need(source(item.get("source")), "交付物缺少约定来源。")
        if item["state"] == "active":
            active_outputs.add(key)
    need(active_outputs, "没有当前交付物。")
    covered = set()
    for item in requirements.values():
        need(item.get("state") in ("active", "retired"), "需求状态无效。")
        need(nonempty(item.get("text")) and source(item.get("source")), "需求缺少具体内容或来源。")
        need(item.get("kind") in ("visual", "identity", "copy", "repair"), "需求类型无效。")
        if item["state"] == "active":
            ids = item.get("outputs")
            need(isinstance(ids, list) and ids and len(set(ids)) == len(ids) and set(ids) <= active_outputs,
                 "需求须映射到当前交付物，不能挂在已取消页上。")
            covered.update(ids)
            need(nonempty(item.get("implementation")), "需求缺少具体落实方法。")
            assets = item.get("required_assets")
            need(isinstance(assets, list) and all(nonempty(x) for x in assets), "须列出需求依赖素材，可为空数组。")
    need(covered == active_outputs, "存在没有任何设计需求的交付物。")
    parent = data.get("previous")
    if revision == 1:
        need(parent is None, "首版不能带前版。")
        need(all(x["state"] == "active" for x in [*requirements.values(), *outputs.values()]), "首版不能隐藏已取消需求。")
    else:
        need(isinstance(parent, dict) and nonempty(parent.get("path")), "修改须链接前版，不得重置清单。")
        previous_path = path.parent / parent["path"]
        need(digest(previous_path) == parent.get("sha256"), "前版被改写或散列不符。")
        old = load_contract(previous_path, seen)
        need(old["task_id"] == data["task_id"] and old["revision"] + 1 == revision, "任务或版本接续不一致。")
        # 需求和交付规格不得偷偷删减；新增用户要求以新编号追加。
        changes = data.get("changes", {})
        need(isinstance(changes, dict), "变更依据格式无效。")
        for group, current, fields in (
            ("requirements", requirements, ("text", "kind", "state", "outputs", "required_assets", "source")),
            ("outputs", outputs, ("spec", "state", "source")),
        ):
            for before in old[group]:
                key = before["id"]
                need(key in current, "修改丢失旧编号：" + key + "；取消也须保留记录。")
                if any(before.get(f) != current[key].get(f) for f in fields):
                    need(source(changes.get(key)), "需求或交付范围变化缺少用户变更依据：" + key)
        for before in old["questions"]:
            need(before["id"] in questions, "修改丢失问题状态。")
            if before["state"] in ("answered", "skipped", "deferred") and before != questions[before["id"]]:
                need(source(changes.get("question:" + before["id"])), "不能无依据重置已答问题。")
    return data


def plan_instructions(contract, output_ids, asset_ids):
    outputs = {x["id"]: x for x in contract["outputs"] if x["state"] == "active"}
    need(isinstance(output_ids, list) and output_ids and len(set(output_ids)) == len(output_ids)
         and set(output_ids) <= set(outputs), "本次输出编号无效。")
    product = next(q for q in contract["questions"] if q["id"] == "product")
    need(product["state"] not in ("unasked", "waiting"),
         "产品信息尚未收口：集中询问一次；已有直接生成意图时记录 deferred 和原话，不能假写已跳过。")
    lines = ["本次必须完成的交付物："]
    for key in output_ids:
        lines.append(key + "：" + outputs[key]["spec"])
    lines.append("以下为当前有效需求，修改其他部分时也必须保留：")
    for item in contract["requirements"]:
        if item["state"] == "active" and set(item["outputs"]) & set(output_ids):
            need(set(item["required_assets"]) <= set(asset_ids), "调用丢失需求依赖素材：" + item["id"])
            lines.append(item["id"] + "（" + ",".join(sorted(set(item["outputs"]) & set(output_ids))) + "）："
                         + item["text"] + "；落实：" + item["implementation"])
    return "\n".join(lines)


def check_delivery(contract_path, report_path):
    contract_path, report_path = Path(contract_path).resolve(), Path(report_path).resolve()
    task = load_contract(contract_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    need(report.get("contract_sha256") == digest(contract_path), "验收没有绑定当前需求版本。")
    need(report.get("status") in ("incomplete", "draft", "ready"), "交付状态无效。")
    artifacts = records(report.get("artifacts"), "实际产物")
    expected = {o["id"] for o in task["outputs"] if o["state"] == "active"}
    need(set(artifacts) <= expected, "验收产物不在当前交付约定中。")
    failed = set(artifacts) != expected
    for key, artifact in artifacts.items():
        need(nonempty(artifact.get("path")), "缺少实际成图路径。")
        file = report_path.parent / artifact["path"]
        need(file.is_file() and digest(file) == artifact.get("sha256"), "成图缺失、已修改或验收引用旧图：" + key)
        need(type(artifact.get("viewed")) is bool, "须如实记录是否看过成图。")
        failed |= not artifact["viewed"]
        checks = records(artifact.get("checks"), "逐项验收")
        required = {r["id"] for r in task["requirements"] if r["state"] == "active" and key in r["outputs"]}
        need(set(checks) == required, "逐项验收覆盖不全或仍有过期项：" + key)
        for check in checks.values():
            need(check.get("status") in ("pass", "fail", "unverified") and nonempty(check.get("evidence")), "验收须有结论和具体画面位置证据。")
            failed |= check["status"] != "pass"
    pending = report.get("pending_details")
    need(isinstance(pending, list) and all(nonempty(x) for x in pending), "须列出待核对细节与文案，可为空。")
    need(not failed or report["status"] == "incomplete", "有漏页、未看图或未通过需求，只能标未完成，不能宣布整套完成。")
    need(not pending or report["status"] != "ready", "有待核实细节，只能交视觉草稿，不能称可直接发布。")
    return {"valid": True, "status": report["status"], "delivered": len(artifacts),
            "expected": len(expected), "note": "仅验证记录一致性；美观和真实性仍需实际看图与核对。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract")
    parser.add_argument("--delivery", help="核验交付报告；不提供时只检查需求与版本继承")
    args = parser.parse_args()
    if args.delivery:
        result = check_delivery(args.contract, args.delivery)
    else:
        task = load_contract(args.contract)
        result = {"valid": True, "revision": task["revision"], "visual_quality_verified": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (TaskError, OSError, ValueError, TypeError, KeyError) as exc:
        print("检查停止：" + str(exc), file=sys.stderr)
        sys.exit(2)
