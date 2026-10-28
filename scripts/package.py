#!/usr/bin/env python3
"""生成仅包含发布文件的安装压缩包和内容散列清单。"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT_FILES = {"SKILL.md", "README.md", "LICENSE", ".gitignore", "VALIDATION.md"}
DIRECTORIES = {"agents": {".yaml"}, "references": {".md"}, "templates": {".md", ".json"},
               "scripts": {".py"}, "tests": {".py", ".md"}}


def package(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    paths = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        allowed = (len(rel.parts) == 1 and rel.name in ROOT_FILES) or (
            len(rel.parts) == 2 and path.suffix in DIRECTORIES.get(rel.parts[0], set()))
        if allowed and path.is_file():
            if path.is_symlink():
                raise ValueError("发布文件不能是符号链接：" + str(rel))
            paths.append(path)
    if not ROOT_FILES <= {str(p.relative_to(root)) for p in paths}:
        raise ValueError("发布文档不完整。")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            name = "visual-reference-product/" + path.relative_to(root).as_posix()
            data = path.read_bytes()
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 28, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
            manifest[name] = hashlib.sha256(data).hexdigest()
    manifest_path = output.with_suffix(".sha256.json")
    manifest_path.write_text(json.dumps({"archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                                        "files": manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(paths)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="打包技能，不上传或提交到远端")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    count = package(Path(__file__).resolve().parents[1], args.output)
    print(f"已打包 {count} 个发布文件：{Path(args.output).resolve()}")
