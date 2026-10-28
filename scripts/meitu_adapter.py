#!/usr/bin/env python3
"""美图官方 CLI 的计划校验与单次提交；不伪造独立抠图接口或自动答复业务卡片。"""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


class AdapterError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise AdapterError(message)


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(dump(value), encoding="utf-8")
    temporary.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class CLI:
    def __init__(self, executable):
        self.executable = executable

    def call(self, args):
        result = subprocess.run([self.executable, *args], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8")
        # 不把未知错误输出、凭据或完整命令行回显到异常。
        return result.returncode, result.stdout


def parse_version(text):
    match = re.search(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)(?![\d.\w+-])", text)
    require(match is not None, "无法识别稳定版 CLI 版本。")
    return tuple(int(x) for x in match.groups())


def doctor(cli, auth=False):
    code, text = cli.call(["--version"])
    require(code == 0, "美图 CLI 版本查询失败，请检查官方连接器。")
    version = parse_version(text)
    require(version >= (1, 0, 24), "美图 CLI 版本过旧，请按连接器规范更新。")
    code, help_text = cli.call(["chat", "--help"])
    require(code == 0 and all(x in help_text for x in ("--image-file", "--room-id", "--prompt")),
            "当前 CLI 命令与适配方式不符，请重新核对官方帮助。")
    result = {"version": ".".join(map(str, version)), "cli": cli.executable,
              "limits": "本适配器保守上限：22 张 JPG/PNG，每张不超过 20 MB；执行前仍核对当前帮助。",
              "image_quality_verified": False}
    if auth:
        code, status = cli.call(["auth", "status", "--check"])
        result["auth"] = "connected" if code == 0 and status.strip() == "connected" else "unverified_or_disconnected"
    return result


def load_job(path):
    path = Path(path).resolve()
    job = read(path)
    require(isinstance(job, dict) and job.get("version") == 2 and job.get("provider") == "meitu",
            "只接受本版美图计划；旧版画布/模型计划不能直接执行。")
    require(job.get("operation") in ("design", "cutout"), "operation 必须为 design 或 cutout。")
    require(job.get("mode") in ("precise", "creative", "concept"), "未知创作模式。")
    for key in ("generation_authorized", "summary_shown"):
        require(type(job.get(key)) is bool, key + " 必须是布尔值，不能用文字冒充授权。")
    require(isinstance(job.get("prompt"), str) and job["prompt"].strip(), "提示词不能为空。")
    require(not any(key in job for key in ("project", "model_name", "params", "room_id")),
            "不要混入旧通道参数或指定已有房间；后续操作使用运行记录中的原房间。")
    assets = job.get("assets")
    require(isinstance(assets, list) and len(assets) <= 22, "本适配器一次最多接收 22 张图片，不能静默丢图。")
    seen = set()
    for asset in assets:
        require(isinstance(asset, dict), "素材记录必须为对象。")
        key = asset.get("id")
        require(isinstance(key, str) and re.fullmatch(r"[RPHSA][0-9]{2,}", key), "素材编号格式不正确。")
        require(key not in seen, "素材编号重复。")
        seen.add(key)
        require(asset.get("role") in ("product", "person", "style", "source"), "素材角色不正确。")
        require(isinstance(asset.get("path"), str) and asset["path"], "缺少图片路径。")
        p = (path.parent / asset["path"]).resolve()
        require(p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"), "素材必须是存在的 JPG/PNG 文件。")
        require(0 < p.stat().st_size <= 20_000_000, "图片为空或超过适配器 20 MB 上限。")
        data = p.read_bytes()
        is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
        is_jpeg = data.startswith(b"\xff\xd8\xff")
        require(is_png if p.suffix.lower() == ".png" else is_jpeg, "文件头与图片扩展名不匹配。")
        require("[[" + key + "]]" in job["prompt"], "提示词未明确引用素材 " + key)
        asset.update(path=str(p), sha256=hashlib.sha256(data).hexdigest())
    refs = set(re.findall(r"\[\[([^\[\]]+)\]\]", job["prompt"]))
    require(refs == seen, "提示词引用了未上传素材，或素材缺少角色说明。")
    if job["operation"] == "cutout":
        require(any(a["role"] in ("product", "person", "source") for a in assets), "抠图需要原始主体图片。")
    elif job["mode"] != "concept":
        require(any(a["role"] == "product" for a in assets), "非概念设计需要真实产品图。")
    return job


def rendered_prompt(job):
    labels = {"product": "产品身份", "person": "指定人物", "style": "仅参考风格", "source": "待处理原始主体"}
    prompt = job["prompt"]
    mapping = []
    for index, asset in enumerate(job["assets"], 1):
        name = f"附件{index}（{asset['id']}，{labels[asset['role']]}）"
        prompt = prompt.replace("[[" + asset["id"] + "]]", name)
        mapping.append(name)
    return "素材按实际附件顺序如下：" + ("；".join(mapping) or "无，文字概念图") + "。\n" + prompt


def parse_output(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        records = []
        for line in text.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        require(records, "未取得可解析的业务返回；保留现场，不重复提交。")
        return records


def unique_id(data, name, required=True):
    values = set()
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == name and isinstance(item, str) and item:
                    values.add(item)
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(data)
    if not values and not required:
        return None
    require(len(values) == 1, "缺少或冲突的 " + name + "；不要猜测或重复创建。")
    value = values.pop()
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}", value), "业务编号格式不可识别。")
    return value


@contextmanager
def locked(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".submit.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise AdapterError("同目录仍有提交或遗留锁；先核对原进程，不并发提交。")
    try:
        handle.close()
        yield
    finally:
        lock.unlink(missing_ok=True)


def submit(cli, job, folder):
    require(job["generation_authorized"] is True and job["summary_shown"] is True,
            "没有实际生成意图或未展示摘要，不能上传/提交。")
    folder = Path(folder)
    with locked(folder):
        fingerprint = hashlib.sha256(dump(job).encode()).hexdigest()
        state_path = folder / "state.json"
        if state_path.exists():
            state = read(state_path)
            require(state.get("fingerprint") == fingerprint, "计划或素材已改变；先核对原任务，不能覆盖状态。")
            require(state.get("phase") == "submitted", "上次状态不明；核对原房间，禁止重提。")
            return {"submitted_again": False, "state": state}
        check = doctor(cli, auth=True)
        require(check.get("auth") == "connected", "当前终端未验证登录；按美图连接器登录后再继续，不索取密钥。")
        save(folder / "job-resolved.json", job)
        (folder / "prompt.txt").write_text(rendered_prompt(job), encoding="utf-8")
        save(folder / "preflight.json", check)
        state = {"fingerprint": fingerprint, "phase": "room_pending"}
        save(state_path, state)
        code, output = cli.call(["create-room"])
        require(code == 0, "创建房间未确认成功；保留运行目录，先核对历史，不重复创建。")
        state["room_id"] = unique_id(parse_output(output), "room_id")
        state["phase"] = "chat_pending"
        save(state_path, state)
        args = ["chat", "--room-id", state["room_id"], "--prompt", rendered_prompt(job)]
        if job["assets"]:
            args += ["--image-file"] + [a["path"] for a in job["assets"]]
        code, output = cli.call(args)
        require(code == 0, "任务提交未确认成功；可能已上传或扣费，请查询同一房间，不重新发送。")
        response = parse_output(output)
        state["task_id"] = unique_id(response, "task_id")
        returned_room = unique_id(response, "room_id", required=False)
        require(returned_room is None or returned_room == state["room_id"], "返回房间与提交房间不一致。")
        state["phase"] = "submitted"
        save(state_path, state)
        return {"submitted_again": False, "state": state,
                "next_argv": [cli.executable, "history-detail", "--room-id", state["room_id"], "--watch"],
                "note": "仅已提交，尚未完成或验收。由宿主持续等待、处理官方交互并逐项展示产物。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--designkit", help="官方 CLI 的实际可执行入口")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("doctor")
    check.add_argument("--check-auth", action="store_true")
    plan = sub.add_parser("plan")
    plan.add_argument("job")
    execute = sub.add_parser("submit")
    execute.add_argument("job")
    execute.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    if args.command == "plan":
        job = load_job(args.job)
        print(dump({"valid": True, "upload_order": [a["id"] for a in job["assets"]],
                    "prompt": rendered_prompt(job), "submitted": False}))
        return
    executable = args.designkit or shutil.which("designkit")
    require(executable, "找不到 designkit，请连接美图设计室 AI设计 CLI，按官方说明安装。")
    cli = CLI(str(Path(executable).resolve()))
    result = doctor(cli, args.check_auth) if args.command == "doctor" else submit(cli, load_job(args.job), args.run_dir)
    print(dump(result))


if __name__ == "__main__":
    try:
        main()
    except (AdapterError, OSError, json.JSONDecodeError) as exc:
        print("执行停止：" + str(exc), file=sys.stderr)
        sys.exit(2)
