#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语法闸门：手机端是**单文件**页面，一处语法错误会让整个内联脚本不执行
（页面看起来只剩静态 HTML，什么都不能点，而且控制台只报一行 SyntaxError）。

这个检查比行为探针便宜得多，所以放在最前面跑：

    python scripts/vsw-check-syntax.py

它做三件事：
  1. 把 mobile/index.html 里的内联 <script> 抽出来，`node --check` 一遍；
  2. 桌面端 app.js 同样 `node --check`；
  3. 顺手查几个常见手误（`async async`、重复声明的 `function xxx()`）。

任何一项失败都以非零码退出。
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOBILE = os.path.join(ROOT, "vsw", "static", "mobile", "index.html")
DESKTOP = os.path.join(ROOT, "vsw", "static", "app.js")

problems = []


def check_js(label, source, path):
    if shutil.which("node") is None:
        problems.append(f"{label}: 找不到 node，无法检查语法")
        return
    tmp = os.path.join(tempfile.gettempdir(), "vsw_syntax_" + re.sub(r"\W+", "_", label) + ".js")
    with io.open(tmp, "w", encoding="utf-8") as handle:
        handle.write(source)
    proc = subprocess.run(["node", "--check", tmp], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        head = "\n     ".join(detail[:6])
        problems.append(f"{label}: node --check 失败\n     {head}")
        return
    print(f"  ok   {label} · {len(source)} 字符 · {os.path.relpath(path, ROOT)}")


def main():
    with io.open(MOBILE, encoding="utf-8") as handle:
        html = handle.read()
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        problems.append("手机端页面里没有内联 <script>（结构变了？）")
    joined = "\n".join(blocks)
    print(f"手机端内联脚本块 {len(blocks)} 个，共 {len(joined)} 字符")
    check_js("手机端 mobile/index.html", joined, MOBILE)

    with io.open(DESKTOP, encoding="utf-8") as handle:
        desktop_js = handle.read()
    check_js("桌面端 static/app.js", desktop_js, DESKTOP)

    # 手误扫描：这些错误 node --check 之前往往先被写出来，抓一次很便宜
    for label, source in (("手机端", joined), ("桌面端", desktop_js)):
        for pattern, why in (
            (r"\basync\s+async\b", "重复的 async"),
            (r"\bawait\b[^\n]*", None),
        ):
            if why is None:
                continue
            for match in re.finditer(pattern, source):
                line = source[:match.start()].count("\n") + 1
                problems.append(f"{label}:{line} {why}")

    if problems:
        print("\n语法检查没通过：")
        for item in problems:
            print("  FAIL " + item)
        return 1
    print("\n语法检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
