"""脚本共用的命令行提示与参数校验。

为什么需要：这些验证脚本在"参数不对"时的默认行为是继续跑，然后在某个
`os.listdir` 或 `names[0]` 上抛出一大段 Python 栈。对 clone 下来第一次用的人
来说，那不是"参数错了"，而是"这东西坏了"。所以统一在这里给出人话提示，
并在目录不存在/为空时直接退出，而不是等它崩在后面。
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple


def wants_help(argv: Optional[List[str]] = None) -> bool:
    args = sys.argv[1:] if argv is None else argv
    return any(a in ("-h", "--help", "help", "/?") for a in args)


def usage(name: str, lines: List[str]) -> int:
    """打印用法并返回 0（供 `sys.exit(usage(...))` 使用）。"""
    print(f"用法：{name}")
    for line in lines:
        print(f"  {line}")
    return 0


def image_dir(path: str, *, label: str = "素材目录") -> Optional[List[str]]:
    """校验图片目录，返回按体积降序的图片文件名；不合法时打印原因并返回 None。"""
    if not path:
        print(f"[错误] 没有给出{label}")
        return None
    if not os.path.isdir(path):
        print(f"[错误] {label}不存在：{path}")
        print("       这应该是一个装着 .jpg/.jpeg/.png 的文件夹。")
        return None
    names = sorted((n for n in os.listdir(path)
                    if os.path.splitext(n)[1].lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp")),
                   key=lambda n: os.path.getsize(os.path.join(path, n)), reverse=True)
    if not names:
        print(f"[错误] {label}里没有图片：{path}")
        return None
    return names


def positional(argv: Optional[List[str]] = None) -> List[str]:
    """取出非选项参数（这些脚本不用 argparse，只区分位置参数和 --xxx）。"""
    args = sys.argv[1:] if argv is None else argv
    return [a for a in args if not a.startswith("-")]


def base_and_dir(argv: Optional[List[str]] = None, *,
                 default_base: str, default_dir: str) -> Tuple[str, str]:
    """常见的两个位置参数：工作台地址、素材目录。"""
    args = positional(argv)
    base = args[0] if args else default_base
    folder = args[1] if len(args) > 1 else default_dir
    return base, folder
