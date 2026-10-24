"""把刚打出来的 APK 发布到工作台的 `dist/` 目录 —— 打完包就等于发布完。

手机端的「检查更新」问的是它连着的这台电脑（`/api/app/version`），服务端就在 `dist/` 里
找最新的包。所以这里只做三件事：读出版本号、算 SHA256、把包和 `version.json` 摆好。

用法：

    python scripts/vsw-publish-apk.py                       # 用默认路径
    python scripts/vsw-publish-apk.py --apk vsw-workbench.apk --root vsw/data
    python scripts/vsw-publish-apk.py --notes "修了窄屏版式；手机端加了自动搜图"

版本号从 `vsw-android/java/com/vsw/workbench/MainActivity.java` 的 `BUILD_TAG` 读，
不手写 —— 手写就会和包里真实编译进去的标签不一致。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
JAVA = os.path.join(REPO, "vsw-android", "java", "com", "vsw", "workbench", "MainActivity.java")
APK_NAME = "vsw-workbench.apk"
META_NAME = "version.json"


def read_build_tag(java_path: str) -> str:
    with io.open(java_path, encoding="utf-8") as fh:
        text = fh.read()
    found = re.search(r'BUILD_TAG\s*=\s*"([^"]+)"', text)
    if not found:
        raise SystemExit(f"读不出 BUILD_TAG：{java_path}")
    return found.group(1)


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="发布 APK 到工作台的 dist/")
    parser.add_argument("--apk", default=os.path.join(REPO, APK_NAME))
    parser.add_argument("--root", default=os.path.join(REPO, "vsw", "data"))
    parser.add_argument("--notes", default="", help="这次改了什么，手机上会显示")
    parser.add_argument(
        "--notes-file",
        default="",
        help="从 UTF-8 文件读说明。中文从命令行传会被 Windows 的代码页弄成乱码，优先用这个",
    )
    parser.add_argument("--channel", default="stable")
    parser.add_argument("--java", default=JAVA)
    args = parser.parse_args(argv)

    if not os.path.isfile(args.apk):
        print(f"[发布] 找不到 APK：{args.apk}", file=sys.stderr)
        return 2
    tag = read_build_tag(args.java)
    notes = args.notes
    if args.notes_file:
        with io.open(args.notes_file, encoding="utf-8") as fh:
            notes = fh.read().strip()
    if not notes:
        # 什么都不传时给一句能看的说明 —— 以前会发布出一条空的 release 说明
        notes = "release " + tag
    dist = os.path.join(os.path.abspath(args.root), "dist")
    os.makedirs(dist, exist_ok=True)
    target = os.path.join(dist, APK_NAME)
    # 已经是最新的同一个文件就不重复拷（省一次 600KB 写盘，也让 mtime 保持稳定）
    same = os.path.isfile(target) and os.path.getsize(target) == os.path.getsize(args.apk)
    if same:
        with open(target, "rb") as a, open(args.apk, "rb") as b:
            same = a.read() == b.read()
    if not same:
        shutil.copyfile(args.apk, target)

    size = os.path.getsize(target)
    digest = sha256_of(target)
    meta = {
        "build": tag,
        "notes": notes,
        "channel": args.channel,
        "size": size,
        "sha256": digest,
        "published_at": int(time.time()),
    }
    with io.open(os.path.join(dist, META_NAME), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"[发布] {tag} → {target}")
    print(f"[发布] {size:,} 字节 · SHA256 {digest}")
    print(f"[发布] 手机端打开「我的」→ 检查更新，即可看到 {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
