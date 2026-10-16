"""Development probe: drive the headless-Chromium CDP driver against a real engine page.

Usage:
    python vsw-probe-cdp.py baidu     # 百度识图入口：找上传框、看结果页结构
    python vsw-probe-cdp.py bing      # Bing 视觉搜索：确认入口与选择器
    python vsw-probe-cdp.py <URL> [等待文本1,等待文本2]

Prints the page probe (inputs/forms/image hosts/page text) so selectors are discovered from
the live DOM instead of guessed. Also usable as a smoke test for the driver itself.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from _paths import REPO_ROOT, add_repo_to_path  # noqa: E402

add_repo_to_path()
DRIVER = os.path.join(REPO_ROOT, "vsw", "cdp_driver.js")
# 探针要拿真实图片去试；默认指向本机的素材目录，换机器用参数或环境变量覆盖
SRC = os.environ.get("VSW_PROBE_IMAGES") or r"D:\images\和幸(春熙路群光广场店)"

PAGES = {
    "baidu": ("https://graph.baidu.com/pcpage/index?tpl_from=pc", ["相关商品", "相似图片"], ["上传失败"]),
    "bing": ("https://cn.bing.com/visualsearch", [], []),
    "bing_www": ("https://www.bing.com/visualsearch", [], []),
    "bing_sbi": ("https://www.bing.com/images", [], []),
    "bing_images": ("https://cn.bing.com/images", [], []),
    "yandex": ("https://yandex.com/images/", [], []),
    "sogou": ("https://pic.sogou.com/", [], []),
    "so360": ("https://image.so.com/", [], []),
}

WAIT_JS_360 = ("/\\/r\\?|\\/i\\?/.test(location.href) && "
               "document.querySelectorAll('img').length >= 6")

# Engine-specific readiness probes (discovered from the live DOM, not guessed).
WAIT_JS = {
    "bing": "document.querySelectorAll('a[href*=\"images/search\"] img, .mimg, a.iusc').length >= 3",
    "bing_www": "/\\/images\\/search/.test(location.href) && document.querySelectorAll('a.iusc, .mimg, .imgpt').length >= 3",
    "bing_sbi": "/\\/images\\/search|\\/visualsearch/.test(location.href)",
    "bing_images": "document.querySelectorAll('a.iusc, .mimg').length >= 3",
    "so360": WAIT_JS_360,
}


def probe_image() -> str:
    add_repo_to_path()
    from vsw import imaging

    names = sorted(os.listdir(SRC), key=lambda n: os.path.getsize(os.path.join(SRC, n)), reverse=True)
    img = imaging.load_image(os.path.join(SRC, names[0]))
    path = os.path.join(tempfile.gettempdir(), "vsw-cdp-probe.jpg")
    img.save(path, format="JPEG", quality=92)
    return path


def run_driver(job: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False)
        job_path = fh.name
    out_path = job_path + ".out"
    try:
        proc = subprocess.run(["node", DRIVER, "--job", job_path, "--out", out_path],
                              capture_output=True, timeout=240)
        if os.path.isfile(out_path):
            with open(out_path, encoding="utf-8") as fh:
                return json.load(fh)
        return {"ok": False, "error": proc.stdout.decode("utf-8", "ignore")[:400]
                or proc.stderr.decode("utf-8", "ignore")[:400]}
    finally:
        for path in (job_path, out_path):
            try:
                os.remove(path)
            except OSError:
                pass


# Bing 的「按图片 URL 搜索」输入框：填值 → 点它的提交按钮，看能不能真的进视觉结果页。
BING_SUBMIT_JS = """(() => {
  const input = document.querySelector('#sb_sbi_ipt') || document.querySelector('input[name=imgurl]');
  if (!input) return 'NO_INPUT';
  input.value = '__IMGURL__';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  const form = input.closest('form');
  if (form) { form.submit(); return 'FORM_SUBMITTED'; }
  const btn = document.querySelector('#sb_sbi_gh');
  if (btn) { btn.click(); return 'BUTTON_CLICKED'; }
  return 'NO_SUBMIT';
})()"""


def bing_url_probe(image_url: str) -> int:
    """Can Bing still be driven by an image URL? That would restore a one-click deep link."""
    job = {
        "engine": "bing_imgurl",
        "url": "https://www.bing.com/images",
        "settleMs": 7000,
        "waitMs": 45000,
        "waitUrl": "/images/search",
        "extractJs": BING_SUBMIT_JS.replace("__IMGURL__", image_url),
        "dumpProbe": True,
    }
    result = run_driver(job)
    print(f"ok={result.get('ok')} error={result.get('error')!r} finalUrl={result.get('finalUrl')}")
    print(f"submit={result.get('extract')!r} title={result.get('title')!r}")
    probe = result.get("probe") or {}
    print("imgHosts:", json.dumps(probe.get("imgHosts"), ensure_ascii=False))
    print("text:", (result.get("text") or "")[:300].replace("\n", " | "))
    return 0


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "baidu"
    if target == "bing_imgurl":
        return bing_url_probe(sys.argv[2] if len(sys.argv) > 2 else
                              "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4d/"
                              "Cat_November_2010-1a.jpg/320px-Cat_November_2010-1a.jpg")
    if target in PAGES:
        url, wait_text, fail_text = PAGES[target]
    else:
        url = target
        wait_text = (sys.argv[2].split(",") if len(sys.argv) > 2 else [])
        fail_text = []
    file_path = probe_image() if "--nofile" not in sys.argv else ""
    job = {
        "engine": target, "url": url, "file": file_path, "settleMs": 6000,
        "selector": "input[type=file]",
        "waitJs": WAIT_JS.get(target, ""),
        "waitMs": int(sys.argv[-1]) if sys.argv[-1].isdigit() else 40000,
        "waitText": wait_text, "waitFailText": fail_text, "dumpProbe": True,
    }
    print(f"探针图片：{file_path}")
    print(f"目标页面：{url}")
    result = run_driver(job)
    print(f"ok={result.get('ok')} error={result.get('error')!r}")
    print(f"finalUrl={result.get('finalUrl')}")
    print(f"title={result.get('title')!r}")
    probe = result.get("probe") or {}
    print("inputs:", json.dumps(probe.get("inputs"), ensure_ascii=False))
    print("forms:", json.dumps(probe.get("forms"), ensure_ascii=False))
    print("buttons:", json.dumps(probe.get("buttons"), ensure_ascii=False)[:500])
    print("imgHosts:", json.dumps(probe.get("imgHosts"), ensure_ascii=False))
    print("extract:", json.dumps(result.get("extract"), ensure_ascii=False)[:800])
    print("text:", (result.get("text") or "")[:700].replace("\n", " | "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
