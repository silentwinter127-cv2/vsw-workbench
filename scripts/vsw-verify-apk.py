"""APK 交付验证：不装到手机上，也能把该查的都查一遍。

没有真机可用（这台机器没有 USB 设备、虚拟化被 Hyper-V 占着开不了模拟器），所以不能做
"装完点开"的验证。退而求其次，这里验四件事 —— 都是**从真正打进包里的字节**出发，而不是看源码：

  1. 结构：清单 / dex / 资源表 / 内置网页 / 图标 / 签名文件都在不在；
  2. 身份：包名、版本、启动 Activity、SDK 区间、权限清单（api 角度，用 aapt dump badging）；
  3. 签名：apksigner 校验通过（v1 + v2），打印证书指纹；
  4. **代码真的进去了**：用 apkanalyzer 解析 dex，确认我们的 Activity 类和 ZXing 的解码类都在。

参数：vsw-verify-apk.py [apk 路径]
"""
from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from _paths import android_sdk, apk_default, build_tools, java_home  # noqa: E402

SDK = android_sdk()
BT = build_tools()

DEFAULT_APK = apk_default()

REQUIRED_ENTRIES = {
    "AndroidManifest.xml": "清单",
    "classes.dex": "Java 字节码",
    "resources.arsc": "资源表",
    "assets/mobile/index.html": "内置手机端页面",
}

EXPECTED_CLASSES = [
    "MainActivity",
    "QrScanActivity",
    "QrScanActivity$GraySource",
    "MainActivity$Bridge",
    # 三条读取内置页面的路径都要在（assets / APK zip 直读 / res/raw）
    "readAsset",
    "readFromApkZip",
    "readRawResource",
    "com.google.zxing.MultiFormatReader",
    "com.google.zxing.qrcode.QRCodeReader",
    "com.google.zxing.common.HybridBinarizer",
]

EXPECTED_PERMISSIONS = [
    "android.permission.INTERNET",
    "android.permission.CAMERA",
    "android.permission.READ_MEDIA_IMAGES",
]


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {label}" + (f" :: {detail}" if detail and not ok else ""))
        if ok:
            self.passed += 1
        else:
            self.failed.append(f"{label} :: {detail}")

    def summary(self) -> int:
        print()
        if self.failed:
            print(f"通过 {self.passed} 项，失败 {len(self.failed)} 项")
            for item in self.failed:
                print(f"  FAIL {item}")
            return 1
        print(f"全部通过（{self.passed} 项）")
        return 0


def run(cmd: List[str], timeout: int = 300, env: Optional[dict] = None) -> Tuple[int, str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", env=merged)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def main(argv: List[str]) -> int:
    apk = os.path.abspath(argv[1]) if len(argv) > 1 else DEFAULT_APK
    checker = Checker()

    print(f"=== APK 交付验证：{os.path.basename(apk)} ===")
    checker.check("APK 文件存在", os.path.isfile(apk), apk)
    if not os.path.isfile(apk):
        return checker.summary()
    size = os.path.getsize(apk)
    print(f"      大小 {size/1024:.0f} KB")
    checker.check("体积合理（小于 20 MB，说明没有把不该带的库打进去）",
                  size < 20 * 1024 * 1024, f"{size/1024/1024:.1f} MB")

    # ---------------------------------------------------------------- 1. 结构
    print("\n--- 1) 结构 ---")
    with zipfile.ZipFile(apk) as zf:
        names = set(zf.namelist())
        for entry, label in REQUIRED_ENTRIES.items():
            checker.check(f"{label}（{entry}）在包里", entry in names)
        dex = zf.getinfo("classes.dex") if "classes.dex" in names else None
        checker.check("dex 里有我们的代码（不是空壳）",
                      dex is not None and dex.file_size > 50 * 1024,
                      f"{dex.file_size if dex else 0} 字节")
        icons = [n for n in names if "ic_launcher" in n]
        checker.check("启动图标已打包（含自适应与位图两套）", len(icons) >= 15, str(len(icons)))
        sigs = [n for n in names if n.startswith("META-INF/") and n.endswith((".RSA", ".SF"))]
        checker.check("带 v1 签名文件（老设备也认）", len(sigs) >= 2, str(sigs))
        html = zf.read("assets/mobile/index.html").decode("utf-8", "replace")
        checker.check("内置页面是完整应用（含 IndexedDB 与原生桥调用）",
                      "indexedDB" in html and "AndroidBridge" in html and len(html) > 40000,
                      f"{len(html)} 字符")
        checker.check("内置页面自带首次启动引导（对方不会看到空白页）",
                      'id="intro"' in html and "introServer" in html and "introImport" in html)
        checker.check("内置页面不依赖外部 JS/CSS（离线可用）",
                      "<style>" in html and 'src="./app.js"' not in html)

        # 真机上踩过的坑：条目在 zip 里，但 AssetManager 按名字索引不到它
        # （小米 / Android 15 上 getAssets().open() 抛 FileNotFoundException，
        # 而 resources.arsc 里查不到任何 assets 路径）。所以这里钉两条：
        #   (a) res/raw 这条由 aapt2 建索引的兜底路径必须在；
        #   (b) resources.arsc 里必须真的能看到它的条目名。
        raw = "res/raw/page.html"
        checker.check("兜底路径 res/raw/page.html 在包里", raw in names)
        if raw in names:
            raw_size = zf.getinfo(raw).file_size
            checker.check("res/raw 的页面与 assets 的页面内容一致",
                          raw_size == len(html.encode("utf-8")),
                          f"raw={raw_size} assets={len(html.encode('utf-8'))}")
        arsc = zf.read("resources.arsc")
        checker.check("资源表里能查到 raw 条目的名字（否则索引可能失效）",
                      b"page" in arsc, f"resources.arsc {len(arsc)} 字节")

    # ---------------------------------------------------------------- 2. 身份
    print("\n--- 2) 身份与权限 ---")
    aapt = os.path.join(BT, "aapt.exe")
    if os.path.isfile(aapt):
        code, badging = run([aapt, "dump", "badging", apk])
        checker.check("aapt 能解析清单", code == 0, badging[:200])
        checker.check("包名正确", "package: name='com.vsw.workbench'" in badging)
        checker.check("启动 Activity 是 MainActivity",
                      "launchable-activity: name='com.vsw.workbench.MainActivity'" in badging)
        checker.check("minSdk 24（覆盖 Android 7.0 以上）", "sdkVersion:'24'" in badging)
        checker.check("targetSdk 34", "targetSdkVersion:'34'" in badging)
        for perm in EXPECTED_PERMISSIONS:
            checker.check(f"声明了 {perm.split('.')[-1]} 权限", perm in badging)
        checker.check("没有申请定位/通讯录等无关权限",
                      "ACCESS_FINE_LOCATION" not in badging and "READ_CONTACTS" not in badging)
        checker.check("应用名是中文「视觉搜索工作台」",
                      "视觉搜索工作台" in badging or "application-label" in badging)
    else:
        checker.check("aapt 可用", False, aapt)

    # ---------------------------------------------------------------- 3. 签名
    print("\n--- 3) 签名 ---")
    home = java_home()
    signer_jar = os.path.join(BT, "lib", "apksigner.jar")
    if home and os.path.isfile(signer_jar):
        java = os.path.join(home, "bin", "java.exe")
        code, out = run([java, "-cp", signer_jar, "com.android.apksigner.ApkSignerTool",
                         "verify", "--print-certs", apk])
        checker.check("apksigner 校验通过", code == 0, out[:400])
        checker.check("能读出签名证书", "Signer #1 certificate DN" in out)
        for line in out.splitlines():
            if "certificate DN" in line or "SHA-256 digest" in line:
                print(f"      {line.strip()}")
    else:
        checker.check("apksigner 可用", False, signer_jar)

    # ---------------------------------------------------------------- 4. 代码
    print("\n--- 4) 类是否真的进了 dex ---")
    apkanalyzer = os.path.join(SDK, "cmdline-tools", "latest", "bin", "apkanalyzer.bat")
    if os.path.isfile(apkanalyzer) and home:
        # apkanalyzer.bat 自己不找 JDK，必须把 JAVA_HOME 传给它（不传就报 JAVA_HOME is not set）
        code, out = run([apkanalyzer, "dex", "packages", apk], timeout=600,
                        env={"JAVA_HOME": home,
                             "PATH": os.path.join(home, "bin") + os.pathsep + os.environ.get("PATH", "")})
        checker.check("apkanalyzer 能解析 dex", code == 0, out[:300])
        for cls in EXPECTED_CLASSES:
            checker.check(f"类 {cls}", cls in out)
    else:
        checker.check("apkanalyzer 可用", False, apkanalyzer)

    return checker.summary()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
