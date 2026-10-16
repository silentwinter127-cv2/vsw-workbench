"""Probe reverse-image-search endpoints for the Visual Search Workbench.

Answers one question per engine: can the workbench build a one-click deep link,
or must the user upload the file by hand and paste the result page URL back?
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

SAMPLE = r"D:\images\三只耳冷锅鱼火锅(玉林店)\03479df4a4ae_00009_03479df4a4ae.jpg"
PUBLIC = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4d/Cat_November_2010-1a.jpg/320px-Cat_November_2010-1a.jpg"

out: dict = {}


def note(key, value):
    out[key] = value
    print(f"[{key}] {json.dumps(value, ensure_ascii=False)[:400]}")


def get(url, **kw):
    kw.setdefault("timeout", 25)
    kw.setdefault("headers", H)
    kw.setdefault("allow_redirects", True)
    return requests.get(url, **kw)


# ---------------------------------------------------------------- baidu graph
def baidu_upload_by_file(path):
    """graph.baidu.com/upload accepts multipart image; returns JSON with url."""
    try:
        with open(path, "rb") as fh:
            r = requests.post(
                "https://graph.baidu.com/upload",
                files={"image": ("a.jpg", fh, "image/jpeg")},
                data={"tn": "pc", "from": "pc", "image_source": "PC_UPLOAD_SEARCH_FILE",
                      "range": '{"page_from": "searchIndex"}'},
                headers={**H, "Referer": "https://graph.baidu.com/"},
                timeout=45,
            )
        j = r.json()
        note("baidu_upload", {"status": r.status_code, "body": j})
        return j
    except Exception as exc:  # noqa: BLE001
        note("baidu_upload", {"error": str(exc)})
        return None


def baidu_upload_by_url(url):
    try:
        r = get("https://graph.baidu.com/upload", params={"image": url, "tn": "pc", "from": "pc"})
        note("baidu_upload_url", {"status": r.status_code, "final": r.url,
                                  "len": len(r.content), "head": r.text[:300]})
    except Exception as exc:  # noqa: BLE001
        note("baidu_upload_url", {"error": str(exc)})


def baidu_similar_page(page_url):
    """Fetch a graph.baidu.com/s result page and count parseable image blocks."""
    try:
        r = get(page_url)
        text = r.text
        cards = re.findall(r'"objURL":"(.*?)"', text)
        note("baidu_similar_parse", {"status": r.status_code, "len": len(text),
                                     "objURL_count": len(cards),
                                     "sample": [c[:80] for c in cards[:3]]})
    except Exception as exc:  # noqa: BLE001
        note("baidu_similar_parse", {"error": str(exc)})


# --------------------------------------------------------------------- yandex
def yandex_by_url(url):
    target = ("https://yandex.com/images/search?rpt=imageview&url="
              + urllib.parse.quote(url, safe=""))
    try:
        r = get(target)
        text = r.text
        note("yandex_url", {"status": r.status_code, "len": len(text),
                            "has_cbir": "CbirSites" in text or "cbir" in text,
                            "img_urls": len(re.findall(r'"origUrl":"(.*?)"', text)),
                            "head": text[:200]})
    except Exception as exc:  # noqa: BLE001
        note("yandex_url", {"error": str(exc)})


# ----------------------------------------------------------------------- bing
def bing_by_url(url):
    target = ("https://www.bing.com/images/searchbyimage/upload")
    try:
        r = requests.post(target,
                          files={"imgurl": (None, url), "cbir": (None, "sbi"),
                                 "imageBin": ("", b"")},
                          headers={**H, "Referer": "https://www.bing.com/images/"},
                          timeout=30)
        note("bing_upload", {"status": r.status_code, "final": r.url, "len": len(r.content)})
    except Exception as exc:  # noqa: BLE001
        note("bing_upload", {"error": str(exc)})


def bing_deep_link(url):
    target = ("https://www.bing.com/images/search?view=detailv2&iss=sbi&FORM=SBIHMP"
              "&sbisrc=UrlPaste&q=imgurl:" + urllib.parse.quote(url, safe=""))
    try:
        r = get(target)
        note("bing_deeplink", {"status": r.status_code, "final": r.url, "len": len(r.content),
                               "has_murl": len(re.findall(r'"murl":"(.*?)"', r.text))})
    except Exception as exc:  # noqa: BLE001
        note("bing_deeplink", {"error": str(exc)})


# --------------------------------------------------------------------- google
def google_lens(url):
    target = "https://lens.google.com/uploadbyurl?url=" + urllib.parse.quote(url, safe="")
    try:
        r = get(target)
        note("google_lens", {"status": r.status_code, "final": r.url[:120], "len": len(r.content)})
    except Exception as exc:  # noqa: BLE001
        note("google_lens", {"error": str(exc)})


# --------------------------------------------------------------------- tineye
def tineye(url):
    target = "https://tineye.com/search?url=" + urllib.parse.quote(url, safe="")
    try:
        r = get(target)
        note("tineye", {"status": r.status_code, "len": len(r.content),
                        "matches": len(re.findall(r"match", r.text))})
    except Exception as exc:  # noqa: BLE001
        note("tineye", {"error": str(exc)})


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "baidu"):
        j = baidu_upload_by_file(SAMPLE)
        if j and isinstance(j, dict):
            data = j.get("data") or {}
            page = data.get("url") or data.get("rawUrl")
            if page:
                baidu_similar_page(page)
        baidu_upload_by_url(PUBLIC)
    if which in ("all", "yandex"):
        yandex_by_url(PUBLIC)
    if which in ("all", "bing"):
        bing_deep_link(PUBLIC)
        bing_by_url(PUBLIC)
    if which in ("all", "google"):
        google_lens(PUBLIC)
    if which in ("all", "tineye"):
        tineye(PUBLIC)
    with open("vsw-probe-engines.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("wrote vsw-probe-engines.json")
