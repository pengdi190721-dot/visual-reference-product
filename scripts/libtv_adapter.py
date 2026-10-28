#!/usr/bin/env python3
"""LibTV 薄适配层：只用官方命令，不调用私有网络接口。仅依赖 Python 标准库。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid


class AdapterError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise AdapterError(message)


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(dump(value) + "\n", encoding="utf-8")
    os.replace(temp, path)


def parse_output(raw):
    """支持单个 JSON 与逐行 JSON；拒绝混入无法识别的业务输出。"""
    try:
        return [json.loads(raw)]
    except ValueError:
        lines = [line for line in raw.splitlines() if line.strip()]
        require(bool(lines), "命令未返回可解析结果；不要据此重复提交。")
        try:
            return [json.loads(line) for line in lines]
        except ValueError as exc:
            raise AdapterError("返回结构无法识别，请查看本地日志；不要重复提交。") from exc


def one_object(records):
    require(len(records) == 1 and isinstance(records[0], dict), "返回结构变化，需人工核对适配。")
    return records[0]


def node_key(records):
    keys = set()
    for item in records:
        if not isinstance(item, dict):
            continue
        for key in ("nodeKey", "newNodeKey"):
            if isinstance(item.get(key), str) and item[key]:
                keys.add(item[key])
    require(len(keys) == 1, "未取得唯一节点标识；保留现场并查询画布，不能自动重新创建。")
    return keys.pop()


def find_cli(explicit=None):
    candidate = explicit or shutil.which("libtv")
    if not candidate:
        fallback = Path.home() / ".libtv" / ("libtv.exe" if os.name == "nt" else "libtv")
        candidate = str(fallback) if fallback.is_file() else None
    require(bool(candidate), "未找到 LibTV。请按中文使用说明安装官方命令行工具。")
    resolved = shutil.which(str(candidate)) or str(Path(candidate).expanduser().resolve())
    require(Path(resolved).is_file(), "指定的 LibTV 程序不存在。")
    return resolved


class CLI:
    def __init__(self, executable, cwd):
        self.executable = executable
        self.cwd = Path(cwd)

    def call(self, args, log=None, raw=False):
        # DEVNULL 避免 LibTV 将父进程输入误当上游节点。无 shell、无自动重试。
        # --run 由官方 CLI 同步等待，故不另设终止超时或后台轮询。
        result = subprocess.run([self.executable, *args], cwd=self.cwd,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=None, text=True, encoding="utf-8")
        if log:
            Path(log).write_text(result.stdout, encoding="utf-8")
        require(result.returncode == 0, "LibTV 命令失败；查看上方错误及本地记录。状态不明时不要重提生成。")
        return result.stdout.strip() if raw else parse_output(result.stdout)


def load_job(path):
    path = Path(path).resolve()
    job = read_json(path)
    require(isinstance(job, dict) and job.get("version") == 1, "调用计划版本应为 1。")
    try:
        uuid.UUID(job.get("project", ""))
    except (ValueError, TypeError, AttributeError):
        raise AdapterError("project 必须是明确选定的 LibTV 画布 UUID。")
    require(isinstance(job.get("model_name"), str) and job["model_name"].strip(), "请填实际查询得到的模型展示名。")
    require(isinstance(job.get("prompt"), str) and job["prompt"].strip(), "提示词不能为空。")
    require("{{Node" not in job["prompt"], "调用计划只用 [[素材编号]]，节点映射由脚本填写。")
    require(job.get("mode") in ("precise", "creative", "concept"), "mode 必须是 precise、creative 或 concept。")
    assets = job.get("assets")
    require(isinstance(assets, list) and bool(assets), "本适配流程至少需要一张产品或对标素材。")
    seen = set()
    resolved = []
    for asset in assets:
        require(isinstance(asset, dict), "素材项应为对象。")
        key = asset.get("id", "")
        require(isinstance(key, str) and bool(re.fullmatch(r"[RPH][0-9]{2,}", key)) and key not in seen, "素材编号须唯一，例如 P01、R01、H01。")
        seen.add(key)
        require(asset.get("role") in ("product", "style", "person"), "素材角色应为 product、style 或 person。")
        require(key[0] == {"product": "P", "style": "R", "person": "H"}[asset["role"]], "素材编号前缀与角色不一致。")
        require(isinstance(asset.get("path"), str) and asset["path"], "素材文件路径不能为空。")
        source = Path(asset["path"]).expanduser()
        source = (path.parent / source).resolve() if not source.is_absolute() else source.resolve()
        require(source.is_file(), f"素材不存在：{key}")
        require(source.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"), "仅接收 PNG、JPEG、WebP；其他格式先另存副本。")
        require(f"[[{key}]]" in job["prompt"], f"提示词缺少 {key} 的明确角色映射。")
        resolved.append({**asset, "path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    referenced = set(re.findall(r"\[\[([^\[\]]+)\]\]", job["prompt"]))
    require(referenced == seen, "提示词引用了未提供的素材，或有素材未明确映射。")
    if job["mode"] != "concept":
        require(any(a["role"] == "product" for a in assets), "精准/视觉创作模式必须提供产品图片；否则用概念模式。")
    params = job.get("params", {})
    require(isinstance(params, dict), "params 应为对象。")
    require(not ({"prompt", "model", "modeType"} & set(params)), "params 不能覆盖提示词、模型或输入模式。")
    require(isinstance(job.get("generation_authorized"), bool) and isinstance(job.get("summary_shown"), bool), "需记录真实的生成授权与摘要展示状态。")
    job = {**job, "assets": resolved, "params": params}
    return job


def enum_values(spec):
    if isinstance(spec, list):
        return spec
    if isinstance(spec, dict) and isinstance(spec.get("enum"), list):
        return spec["enum"]
    return None


def prompt_for(job, mapping):
    prompt = job["prompt"]
    for key, node in mapping.items():
        prompt = prompt.replace(f"[[{key}]]", "{{Node " + node + "}}")
    return prompt


def validate_schema(job, response, prompt):
    """只支持已知的图片 schema 子集；新结构宁可停止也不猜字段。"""
    schema = response.get("schema")
    require(isinstance(schema, dict) and isinstance(schema.get("properties"), dict), "模型参数结构不可识别。")
    require(response.get("modality") == "image", "所选模型不是已验证的图片模态。")
    props = schema["properties"]
    modes = props.get("modeType", {})
    require(isinstance(modes, dict), "参考图模式结构不可识别。")
    items = modes.get("items", {})
    require(isinstance(items, dict), "参考图模式数量结构不可识别。")
    limits = items.get("image2image")
    require(isinstance(limits, list) and len(limits) == 2 and all(type(x) is int for x in limits), "模型未提供可识别的图片参考数量限制。")
    require(limits[0] <= len(job["assets"]) <= limits[1], f"参考图数量超出模型允许范围 {limits}；优先保留产品/人物，风格可转成文字规则。")
    prompt_spec = props.get("prompt", {})
    require(isinstance(prompt_spec, dict), "提示词规则不可识别。")
    maximum = prompt_spec.get("maxLength")
    if maximum is not None:
        require(type(maximum) is int, "提示词长度规则不可识别。")
        # JS 字符串上限按 UTF-16 单元计，避免 emoji 少计。
        require(len(prompt.encode("utf-16-le")) // 2 <= maximum, f"提示词超过模型上限 {maximum}；请保留产品锁后压缩，不直接截断。")
    require(job["params"].get("count") == 1, "本适配器一次只生成一张；多张请按授权数量拆分独立计划。")
    for key, value in job["params"].items():
        require(key in props, f"当前模型未定义参数：{key}")
        spec = props[key]
        choices = enum_values(spec)
        if choices is not None:
            require(any(type(value) is type(x) and value == x for x in choices), f"参数 {key} 的值不在模型允许列表中。")
        elif isinstance(spec, dict) and spec.get("component") == "switch":
            require(type(value) in (bool, int) and value in (False, True, 0, 1), f"开关参数 {key} 必须是布尔值或 0/1。")
        else:
            raise AdapterError(f"参数 {key} 的规则尚未适配，请由宿主核对官方说明后直接调用 CLI。")
    # 防止模型默认开启组图；只针对 schema 实际存在的字段。
    if "sequential" in props:
        require(job["params"].get("sequential") in (0, False) and "sequential" in job["params"], "当前模型有组图开关，请显式设 sequential=0，避免超出一张授权。")
    for key, spec in props.items():
        if isinstance(spec, dict) and spec.get("required") is True:
            require(key in ("prompt", "modeType") or key in job["params"], f"缺少模型必填参数 {key}。")
    rules = schema.get("rules", [])
    require(isinstance(rules, list), "模型生成规则不可识别。")
    present = {"prompt": bool(prompt.strip()), "image": bool(job["assets"]), "media": bool(job["assets"]), "video": False, "audio": False}
    for rule in rules:
        require(isinstance(rule, dict) and not (set(rule) - {"require", "mode", "forModeTypes"}), "模型有尚未适配的生成规则。")
        scope = rule.get("forModeTypes")
        require(scope is None or isinstance(scope, list), "生成规则模式不可识别。")
        if scope and "image2image" not in scope:
            continue
        needs = rule.get("require")
        require(isinstance(needs, list) and all(x in present for x in needs), "模型输入规则无法识别。")
        mode = rule.get("mode", "all")
        require(mode in ("all", "any"), "模型逻辑规则无法识别。")
        require((any if mode == "any" else all)(present[x] for x in needs), "模型输入条件不满足。")
    return schema


def preflight(cli, job):
    found = one_object(cli.call(["model", "search", "-t", "image"]))
    matches = [x for x in found.get("matches", []) if x.get("modelName") == job["model_name"]]
    require(len(matches) == 1, "模型展示名未唯一命中；请重新查询，不能用内部模型键替代展示名。")
    response = one_object(cli.call(["model", job["model_name"]]))
    # 上传前预留节点标识长度，上传后再用真实标识精确检查。
    estimated = prompt_for(job, {a["id"]: "x" * 64 for a in job["assets"]})
    validate_schema(job, response, estimated)
    return response


def fingerprint(job):
    return hashlib.sha256(json.dumps(job, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def execute(cli, job, folder):
    require(job["generation_authorized"] and job["summary_shown"], "尚未记录生成授权或摘要展示；不能执行。")
    folder = Path(folder)
    state_path = folder / "state.json"
    signature = fingerprint(job)
    state = read_json(state_path) if state_path.exists() else {"fingerprint": signature, "run_id": uuid.uuid4().hex[:12], "uploads": {}, "phase": "new"}
    require(state.get("fingerprint") == signature, "计划或素材已改变，不能复用旧运行目录。先核对旧任务再新建计划。")
    require(not state["phase"].endswith("pending"), "上次操作状态不明；请先查日志和画布节点，禁止自动重提或盲目删除状态文件。")
    if state["phase"] in ("run_returned", "downloaded"):
        return {"message": "该计划已调用生成，不重复提交。请继续查询、下载与视觉验收。", "state": state}
    response = preflight(cli, job)
    save_json(folder / "model-schema.json", response)
    save_json(folder / "job-resolved.json", job)
    save_json(state_path, state)
    for asset in job["assets"]:
        key = asset["id"]
        if key in state["uploads"]:
            continue
        name = "对标生图-" + state["run_id"] + "-" + key
        state.update(phase="upload_pending", pending_name=name)
        save_json(state_path, state)
        records = cli.call(["upload", name, "-p", job["project"], "-t", "image", "-f", asset["path"]], folder / f"upload-{key}.jsonl")
        state["uploads"][key] = node_key(records)
        state.update(phase="assets_ready", pending_name=None)
        save_json(state_path, state)
    if not state.get("node"):
        prompt = prompt_for(job, state["uploads"])
        validate_schema(job, response, prompt)
        name = "对标生图-" + state["run_id"] + "-成图"
        args = ["node", "create", name, "-p", job["project"], "-t", "image", "--prompt", prompt, "-s", "model=" + job["model_name"]]
        for key, value in job["params"].items():
            encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            args += ["-s", key + "=" + encoded]
        for asset in job["assets"]:
            args += ["--left", state["uploads"][asset["id"]]]
        state.update(phase="create_pending", pending_name=name)
        save_json(state_path, state)
        state["node"] = node_key(cli.call(args, folder / "create.jsonl"))
        state.update(phase="node_ready", pending_name=None)
        save_json(state_path, state)
    state["phase"] = "run_pending"
    save_json(state_path, state)
    cli.call(["node", state["node"], "-p", job["project"], "--run"], folder / "run.jsonl")
    # 进程返回不等于视觉成功，业务终态留给宿主读取原始结果核验。
    state["phase"] = "run_returned"
    save_json(state_path, state)
    return {"message": "生成命令已返回。请核对 run.jsonl 的业务终态，下载并实际看图；尚未视觉验收。", "state": state}


def main():
    parser = argparse.ArgumentParser(description="对标产品生图的 LibTV 适配工具")
    parser.add_argument("--libtv", help="官方 LibTV 可执行文件路径")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="只读检查版本与在线图片模型")
    for command in ("plan", "execute", "inspect", "download"):
        sub = subs.add_parser(command)
        sub.add_argument("job", help="调用计划 JSON")
        sub.add_argument("--run-dir", required=True, help="此计划的独立本地运行目录")
    args = parser.parse_args()
    if args.command == "doctor":
        import tempfile
        with tempfile.TemporaryDirectory(prefix="visual-reference-doctor-") as directory:
            cli = CLI(find_cli(args.libtv), directory)
            print(dump({"version": cli.call(["--version"], raw=True), "models": one_object(cli.call(["model", "search", "-t", "image"]))}))
        return
    job = load_job(args.job)
    folder = Path(args.run_dir).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    cwd = folder / "cli-work"
    cwd.mkdir(exist_ok=True)
    require(not (cwd / ".libtv").exists(), "运行目录含隐式 LibTV 绑定，请换一个独立目录，防止默认分组串用。")
    cli = CLI(find_cli(args.libtv), cwd)
    if args.command == "plan":
        response = preflight(cli, job)
        save_json(folder / "model-schema.json", response)
        print(dump({"valid": True, "remote_writes": False, "model": job["model_name"], "assets": [{"id": a["id"], "role": a["role"]} for a in job["assets"]], "params": job["params"], "next": "用户已授权且摘要已展示时执行 execute"}))
        return
    if args.command in ("inspect", "download"):
        state = read_json(folder / "state.json")
        require(state["fingerprint"] == fingerprint(job), "计划或输入发生变化。")
        require(bool(state.get("node")), "尚无生成节点，需按 pending_name 查询画布，不能自动重建。")
        if args.command == "inspect":
            print(dump(cli.call(["node", state["node"], "-p", job["project"]], folder / "inspect.jsonl")))
        else:
            require(state["phase"] == "run_returned", "生成未返回或已下载，请先人工核对原节点状态与现有文件。")
            out = folder / "images"
            out.mkdir(exist_ok=True)
            cli.call(["download", "-n", state["node"], "-p", job["project"], "-o", str(out)], folder / "download.log", raw=True)
            state["phase"] = "downloaded"
            save_json(folder / "state.json", state)
            print(dump({"directory": str(out), "visual_review": "待验收"}))
        return
    lock = folder / "execute.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AdapterError("同一计划已有执行锁；先确认原进程结束及画布状态，不要并行提交。")
    os.close(fd)
    try:
        print(dump(execute(cli, job, folder)))
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (AdapterError, OSError, ValueError, KeyError, TypeError) as exc:
        print("处理停止：" + str(exc), file=sys.stderr)
        sys.exit(2)
