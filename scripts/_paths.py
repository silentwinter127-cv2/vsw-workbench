"""脚本共用的路径助手。

脚本住在 `scripts/` 里，包（`vsw/`）在仓库根。以前这些脚本在根目录，搬家之后
"相对 `__file__` 找东西"的写法就会指错地方，所以统一走这里：

    from _paths import REPO_ROOT, add_repo_to_path, apk_default, build_root
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 从 scripts/ 往上找带 vsw/ 的那一层；找不到就退化成 scripts/ 的父目录
REPO_ROOT = next(
    (candidate for candidate in (os.path.dirname(HERE), HERE)
     if os.path.isdir(os.path.join(candidate, "vsw"))),
    os.path.dirname(HERE),
)


def add_repo_to_path() -> str:
    """把仓库根插进 sys.path，返回它。"""
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    return REPO_ROOT


def apk_default(name: str = "vsw-workbench.apk") -> str:
    """默认 APK 位置：仓库根 → vsw-android/。用 --apk 可以覆盖。"""
    for candidate in (os.path.join(REPO_ROOT, name),
                      os.path.join(REPO_ROOT, "vsw-android", name)):
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(REPO_ROOT, name)


def build_root() -> str:
    """安卓构建链的位置。

    默认 `C:\\vsw-build`（本机安装位置），可以用环境变量 `VSW_BUILD_ROOT` 覆盖 ——
    别人 clone 之后几乎肯定不在同一个路径，所以不能写死。
    """
    return os.environ.get("VSW_BUILD_ROOT") or r"C:\vsw-build"


def android_sdk() -> str:
    return os.path.join(build_root(), "android-sdk")


def build_tools(version: str = "34.0.0") -> str:
    return os.path.join(android_sdk(), "build-tools", version)


def java_home() -> str:
    """JDK 位置：环境变量 → 构建根下的 java-home.txt → 构建根下的 jdk17/*。"""
    env = os.environ.get("JAVA_HOME")
    if env and os.path.isfile(os.path.join(env, "bin", "java.exe")):
        return env
    marker = os.path.join(build_root(), "java-home.txt")
    if os.path.isfile(marker):
        with open(marker, encoding="utf-8") as fh:
            value = fh.read().strip()
            if value and os.path.isfile(os.path.join(value, "bin", "java.exe")):
                return value
    jdk_dir = os.path.join(build_root(), "jdk17")
    if os.path.isdir(jdk_dir):
        for name in sorted(os.listdir(jdk_dir)):
            candidate = os.path.join(jdk_dir, name)
            if os.path.isfile(os.path.join(candidate, "bin", "java.exe")):
                return candidate
    return ""
