#!/usr/bin/env python3
"""安装本技能文件；不覆盖已有目录，不执行依赖安装、不上传。"""
import argparse
import hashlib
from pathlib import Path
import shutil
import sys

NAME = "visual-reference-product"
IGNORED = {".git", "__pycache__", ".DS_Store", ".pytest_cache", ".libtv", "dist", "runs"}


def files(root):
    return sorted(p for p in root.rglob("*") if p.is_file() and not any(x in IGNORED for x in p.relative_to(root).parts) and p.suffix != ".pyc")


def install(source, skills_dir, check=False):
    source = Path(source).resolve()
    target = Path(skills_dir).expanduser().resolve() / NAME
    if source == target or source in target.parents:
        raise ValueError("目标不能是源目录或其子目录。")
    content = (source / "SKILL.md").read_text(encoding="utf-8")
    if "name: " + NAME not in content:
        raise ValueError("源技能名称不匹配。")
    selected = files(source)
    if any(p.is_symlink() or any(a.is_symlink() for a in p.parents if a != source) for p in selected):
        raise ValueError("安装源含符号链接，请使用完整发布包。")
    if not check:
        if target.exists():
            raise ValueError("目标目录已存在，未覆盖。升级前请另行备份并选择安装方式。")
        target.mkdir(parents=True)
        for src in selected:
            dest = target / src.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    for src in selected:
        dest = target / src.relative_to(source)
        if not dest.is_file() or hashlib.sha256(src.read_bytes()).digest() != hashlib.sha256(dest.read_bytes()).digest():
            raise ValueError("安装内容不完整或不同：" + str(src.relative_to(source)))
    expected = {str(p.relative_to(source)) for p in selected}
    actual = {str(p.relative_to(target)) for p in files(target)}
    if expected != actual:
        raise ValueError("安装目录含额外文件，请核对版本。")
    return target, len(selected)


def main():
    parser = argparse.ArgumentParser(description="安装到 WorkBuddy，或校验已安装副本")
    parser.add_argument("--skills-dir", default=str(Path.home() / ".workbuddy" / "skills"))
    parser.add_argument("--check", action="store_true", help="只校验已安装文件，不修改")
    args = parser.parse_args()
    target, count = install(Path(__file__).resolve().parents[1], args.skills_dir, args.check)
    print(f"文件校验通过：{count} 个文件\n安装目录：{target}\n请在 WorkBuddy 新开会话检查发现与调用；文件通过不代表界面已启用。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print("安装停止：" + str(exc), file=sys.stderr)
        sys.exit(2)
