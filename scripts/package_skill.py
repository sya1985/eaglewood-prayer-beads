#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本 skill 打包成可分发的 zip。

排除规则（防泄漏核心）
--------------------------------------------------------------------------
+-----------------------------+--------------------------------------------+
| 类别                        | 规则                                        |
+=============================+============================================+
| 目录                        | ``__pycache__``、``outputs``、``.workbuddy`` |
+-----------------------------+--------------------------------------------+
| 后缀                        | ``.pyc``                                    |
+-----------------------------+--------------------------------------------+

⚠️ ``outputs`` 是**打包产物自己的落盘目录**（zip 就写在那里）。不排除的话 zip 会把
上一次的自己收进包内，且逐字节回验必然对不上（新旧两代 zip 内容不同）。

⚠️ ``.workbuddy`` 是**工作区目录**（临时脚本、改动前备份）。它在 skill 目录下随时可能
被建出来 —— 一旦被打进包，安装包里会多出本机的中间产物与旧版本文件。

本 skill 无运行时数据文件（价格基准直接维护在 ``references/region-benchmarks.md``
§一 1.1 表内），包内文件即全部状态。

用法
--------------------------------------------------------------------------
::

    python scripts/package_skill.py --out <目标.zip>

不带 ``--out`` 时默认写到当前工作目录下的 ``eaglewood-prayer-beads.zip``。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SKILL_DIR = os.path.dirname(HERE)          # 本脚本位于 <skill>/scripts/
TOP = "eaglewood-prayer-beads"                     # zip 内的顶层目录名

#: 排除的目录名（任意层级命中即整棵剪掉）
EXCLUDE_DIRS = {"__pycache__", "outputs", ".workbuddy"}

#: 排除的文件后缀
EXCLUDE_SUFFIXES = {".pyc"}

#: 排除的**相对路径**（POSIX 风格，以 skill 根为基准）
#: 价格基准已并入 region-benchmarks.md §一 1.1 表，无独立数据文件 → 置空
EXCLUDE_REL: set[str] = set()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _md5(data: bytes) -> str:
    """返回字节串的 md5 十六进制摘要。

    :param data: 待摘要的字节串
    :return: 32 位小写十六进制字符串
    """
    return hashlib.md5(data).hexdigest()


def _rel_posix(path: str, root: str) -> str:
    """把绝对路径转成相对 ``root`` 的 **POSIX 风格** 相对路径。

    ⚠️ 必须先 ``replace(os.sep, "/")`` 再比较，否则 Windows 上的 ``\\``
    会让 :data:`EXCLUDE_REL` 永远匹配不上。

    :param path: 文件绝对路径
    :param root: 基准目录绝对路径
    :return: 形如 ``references/xxx.md`` 的相对路径
    """
    return os.path.relpath(path, root).replace(os.sep, "/")


def collect(skill_dir: str) -> list[tuple[str, str]]:
    """遍历 skill 目录，返回**过滤后**的 ``(rel, abs)`` 列表。

    过滤规则见模块头部的「排除规则」。结果按 ``rel`` 排序，保证打包可复现。

    :param skill_dir: skill 根目录
    :return: 排序后的 ``[(相对路径, 绝对路径), ...]``
    """
    keep: list[tuple[str, str]] = []
    for cur, dirs, files in os.walk(skill_dir):
        # ⚠️ 原地裁剪 dirs 才能阻止 os.walk 继续下探
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in sorted(files):
            abs_path = os.path.join(cur, name)
            rel = _rel_posix(abs_path, skill_dir)

            if rel in EXCLUDE_REL:
                continue
            if os.path.splitext(name)[1].lower() in EXCLUDE_SUFFIXES:
                continue
            keep.append((rel, abs_path))
    keep.sort(key=lambda t: t[0])
    return keep


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def pack(skill_dir: str, out_zip: str) -> int:
    """打包并**逐字节回验**，返回退出码（0 = 成功）。

    回验做两件事：

    1. 包内每个文件与磁盘源文件 **md5 必须一致**（写入成功 ≠ 落盘正确）
    2. :data:`EXCLUDE_REL` 里的文件 **必须不在包内**（排除规则真的生效了）

    :param skill_dir: skill 根目录
    :param out_zip: 目标 zip 路径
    :return: 0 表示全部校验通过；1 表示有任一校验失败
    """
    files = collect(skill_dir)
    if not files:
        print("❌ 未收集到任何文件，skill_dir 是否正确？", file=sys.stderr)
        return 1

    excluded_present = [r for r in sorted(EXCLUDE_REL)]

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, abs_path in files:
            zf.write(abs_path, "%s/%s" % (TOP, rel))

    # ---- 回验 ----
    ok = True
    with zipfile.ZipFile(out_zip) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]

        # 1) 逐字节比对
        bad = []
        for rel, abs_path in files:
            arc = "%s/%s" % (TOP, rel)
            if arc not in names:
                bad.append("%s（包内缺失）" % rel)
                continue
            if _md5(zf.read(arc)) != _md5(open(abs_path, "rb").read()):
                bad.append("%s（内容不一致）" % rel)

        # 2) 排除项确实不在包内
        leaked = [r for r in EXCLUDE_REL if "%s/%s" % (TOP, r) in names]

        print("打包 -> %s" % out_zip)
        print("  内含文件数 : %d" % len(names))
        print("  逐字节不一致: %s" % (bad if bad else "无（%d/%d 全一致）" % (len(files), len(files))))
        print("  排除规则    : %s" % sorted(EXCLUDE_REL))
        print("  排除项泄漏  : %s" % (leaked if leaked else "无（已确认不在包内）"))

        if bad:
            ok = False
        if leaked:
            ok = False

    if ok:
        print("✅ 安装包口径打包完成，%d 文件，%d 字节" % (len(files), os.path.getsize(out_zip)))
        # 提示被排除的内容，避免误当"备份"
        for r in excluded_present:
            src = os.path.join(skill_dir, r.replace("/", os.sep))
            state = "（本机存在，已刻意排除）" if os.path.exists(src) else "（本机不存在）"
            print("   ⚠️ 排除 %s %s" % (r, state))
        return 0

    print("❌ 校验未通过，请检查上面的不一致项", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    :param argv: 参数列表（``None`` 时取 ``sys.argv[1:]``）
    :return: 进程退出码
    """
    ap = argparse.ArgumentParser(
        description="按「安装包」口径打包 eaglewood-prayer-beads skill"
    )
    ap.add_argument("--skill-dir", default=DEFAULT_SKILL_DIR,
                    help="skill 根目录（默认：本脚本的上一级）")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "%s.zip" % TOP),
                    help="目标 zip 路径（默认：当前目录/%s.zip）" % TOP)
    args = ap.parse_args(argv)
    return pack(args.skill_dir, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
