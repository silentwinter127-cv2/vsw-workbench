"""External search engines: dispatch links, result-page parsing, source aggregation.

Design decision (measured, not assumed): every engine was probed from this machine.

* ``graph.baidu.com/upload`` answers ``{"status":1,"msg":"Reject"}`` — the JSON API is
  walled off, and ``image.baidu.com`` GET-by-URL redirects straight to a captcha.
* Google Lens / TinEye / SauceNAO are unreachable from here.

So the workbench never pretends to scrape an engine. It builds **one-click dispatch
links** (some engines accept an image URL, all of them accept a manual upload of the
variant file) and then **parses whatever result page you bring back** — either by
pasting the page URL or by pasting the page/JSON you copied out of the browser. That
keeps the discovery layer external and replaceable while the workbench owns scheduling,
dedup, ranking and accumulation, which is exactly the split the design calls for.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Engine:
    key: str
    name: str
    note: str
    url_template: str          # {url} = public image URL, {page} = local file page
    accepts_image_url: bool    # whether a deep link can carry the image itself
    manual_upload: bool = True  # whether the engine's site accepts a hand upload
    home: str = ""
    # ---- real-browser automation (see vsw/cdp_driver.js) -------------------------
    browser: bool = False          # drivable headless: hand it the file, read the DOM
    browser_entry: str = ""        # page that hosts the upload widget
    upload_selector: str = "input[type=file]"
    wait_text: Tuple[str, ...] = ()      # any of these appearing means results are up
    wait_fail_text: Tuple[str, ...] = ()  # any of these means the engine rejected the image
    wait_ready_js: str = ""        # extra readiness predicate evaluated in the page
    extract_js: str = ""           # returns JSON: {similar, sources, goods, guess}
    capture_network: bool = False  # expose the page's own XHR urls to extract_js
    # How much of an engine's output to feed into the queue/board, and what its provenance
    # really is. Baidu's carousel carries **no** source-page attribution (measured: every
    # result links back to graph.baidu.com/pcpage/similar with only the image and a session
    # token), so its "N similar images" are neighbours, not转载源 — they must be valued as
    # divergence material rather than as source tracking.
    candidate_limit: int = 24
    source_label: str = ""         # shown as the title of engine-only provenance records
    trust_label: str = "engine"
    contributes_sources: bool = False


# Extraction scripts run *inside* the engine's result page. They are kept here (rather than
# in the driver) so each engine's DOM knowledge lives next to the engine definition.
#
# 百度这条为什么走 XHR 而不是读 DOM（实测 2026-10-16）：
#   结果页的 30 个相似图卡片在 DOM 里只指向百度自己的看图器（graph.baidu.com/pcpage/similar
#   ?image=...&shituToken=...），整页 HTML 里搜不到任何外部域名。但那个轮播是由
#   `/ajax/pcsimi` 喂的，它的 JSON 每条都带 `fromUrl` —— **真实来源网页**（实测第一条就是
#   www.xiaohongshu.com/discovery/item/6064539a...）。所以提取脚本回放页面自己发过的那条
#   带签名的请求，把 fromUrl 拿回来，百度的产出就从「没有归属的邻域图」变成「可进来源追踪队列」。
EXTRACT_BAIDU = r"""(() => {
  const abs = (u) => { try { return new URL(u, location.href).toString(); } catch (e) { return ''; } };
  const out = { similar: [], sources: [], goods: [], guess: '', sizes: [], diag: { hosts: {} } };
  const text = document.body ? document.body.innerText : '';
  const g = /图中可能是\s*([^\n]{1,40})/.exec(text);
  if (g) out.guess = g[1].trim();

  const captured = Array.isArray(window.__vswCaptured) ? window.__vswCaptured : [];
  const pcsimi = captured.find((u) => /\/ajax\/pcsimi/.test(u)) || '';

  const push = (item) => {
    if (!item || !item.u || out.similar.length >= 60) return;
    out.similar.push(item);
  };

  const fromDom = () => {
    const seen = new Set();
    for (const img of document.querySelectorAll('img')) {
      const u = abs(img.getAttribute('data-src') || img.getAttribute('data-imgurl') || img.currentSrc || img.src || '');
      if (!u || u.startsWith('data:')) continue;
      let host = ''; try { host = new URL(u).host; } catch (e) { }
      out.diag.hosts[host] = (out.diag.hosts[host] || 0) + 1;
      const isResult = /\/\/(t\d+|mms\d*|mms-graph|shitu-query[a-z-]*)\.(baidu|bcebos)\.(com|net)\//.test(u);
      const w = img.naturalWidth || 0, h = img.naturalHeight || 0;
      if (!isResult || /textocr|pc-icon/.test(u) || w < 80 || h < 80) continue;
      if (seen.has(u)) continue;
      seen.add(u);
      push({ u: u, w: w, h: h, page: '', alt: (img.getAttribute('alt') || '').slice(0, 80) });
    }
  };

  const goods = () => {
    const priceRe = /¥\s*([\d.]+)/;
    const seenGoods = new Set();
    for (const el of document.querySelectorAll('div,li,a')) {
      const t = (el.innerText || '').trim();
      if (!t || t.length > 220) continue;
      const m = priceRe.exec(t);
      if (!m) continue;
      const platform = (t.match(/淘宝|京东|天猫|拼多多|闲鱼/) || [''])[0];
      const lines = t.split('\n').map((x) => x.trim()).filter(Boolean);
      const title = lines.find((l) => !priceRe.test(l) && !/^(淘宝|京东|天猫|拼多多|闲鱼)$/.test(l)) || '';
      const key = m[1] + '|' + title.slice(0, 40);
      if (seenGoods.has(key)) continue;
      seenGoods.add(key);
      out.goods.push({ price: m[1], title: title.slice(0, 80), platform: platform });
      if (out.goods.length >= 8) break;
    }
  };

  const externalSources = () => {
    const seenUrl = new Set();
    for (const a of document.querySelectorAll('a[href]')) {
      const h = abs(a.getAttribute('href'));
      if (!/^https?:\/\//.test(h) || /baidu\.com|baidu\.cn/.test(h)) continue;
      const t = (a.innerText || '').trim();
      if (!t || t.length > 90 || seenUrl.has(h)) continue;
      seenUrl.add(h);
      out.sources.push({ url: h, title: t });
      if (out.sources.length >= 12) break;
    }
  };

  const finish = () => {
    goods();
    if (!out.similar.length) fromDom();
    externalSources();
    out.diag.viaPcsimi = out.diag.viaPcsimi || 0;
    return JSON.stringify(out);
  };

  // 「相似站点」（同一张图出现在哪些网页）只在部分结果上开放；有就顺带捞一把，
  // 没有也不影响主路径（pcsimi 的 fromUrl 已经给了每个相似图的来源页）。
  const fetchSites = () => {
    if (!pcsimi) return Promise.resolve();
    const u = pcsimi.replace('/ajax/pcsimi', '/ajax/pcsites');
    return fetch(u, { credentials: 'include' })
      .then((r) => (r.ok ? r.json() : null))
      .then((json) => {
        const list = (json && json.data && (json.data.list || json.data.sites)) || [];
        out.diag.sites = list.length;
        for (const s of list) {
          const url = s.fromUrl || s.url || s.siteUrl || '';
          if (url) out.sources.push({ url: url, title: (s.title || s.name || '').slice(0, 80) });
        }
      })
      .catch(() => { /* 没有这个入口就算了 */ });
  };

  const fetchPcsimi = () => {
    if (!pcsimi) { fromDom(); return Promise.resolve(); }
    return fetch(pcsimi, { credentials: 'include' })
      .then((r) => r.json())
      .then((json) => {
        const list = (json && json.data && json.data.list) || [];
        out.diag.pcsimi = list.length;
        for (const item of list) {
          const url = item.thumbUrl || item.hoverUrl || '';
          if (!url) continue;
          out.similar.push({
            u: url,
            w: item.width || 0,
            h: item.height || 0,
            page: item.fromUrl || '',
            alt: item.title || '',
          });
          out.diag.viaPcsimi += 1;
        }
      })
      .catch(() => { fromDom(); });
  };

  return Promise.all([fetchPcsimi(), fetchSites()]).then(finish);
})()"""

EXTRACT_360 = r"""(() => {
  const abs = (u) => { try { return new URL(u, location.href).toString(); } catch (e) { return ''; } };
  const out = { similar: [], sources: [], goods: [], guess: '', sizes: [], diag: { hosts: {} } };
  const text = document.body ? document.body.innerText : '';
  const sz = /图片尺寸：(\d+)x(\d+)/.exec(text);
  if (sz) out.sizes.push({ w: +sz[1], h: +sz[2] });
  const seen = new Set();
  for (const img of document.querySelectorAll('img')) {
    const u = abs(img.getAttribute('data-src') || img.getAttribute('data-original') || img.currentSrc || img.src || '');
    if (!u || u.startsWith('data:')) continue;
    let host = ''; try { host = new URL(u).host; } catch (e) { }
    out.diag.hosts[host] = (out.diag.hosts[host] || 0) + 1;
    const isResult = /qhimg|360tres/.test(host);
    const w = img.naturalWidth || 0, h = img.naturalHeight || 0;
    if (!isResult && (w < 180 || h < 180)) continue;
    if (seen.has(u)) continue;
    seen.add(u);
    const a = img.closest('a');
    out.similar.push({ u: u, w: w, h: h, page: a ? abs(a.getAttribute('href')) : '', alt: (img.getAttribute('alt') || '').slice(0, 80) });
    if (out.similar.length >= 60) break;
  }
  const seenUrl = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const h = abs(a.getAttribute('href'));
    if (!/^https?:\/\//.test(h)) continue;
    if (/so\.com|360\.cn|360kan|360safe|qhimg|360tres|beian/.test(h)) continue;
    const t = (a.innerText || '').trim();
    if (!t || t.length > 90 || seenUrl.has(h)) continue;
    seenUrl.add(h);
    out.sources.push({ url: h, title: t });
    if (out.sources.length >= 12) break;
  }
  return JSON.stringify(out);
})()"""

ENGINES: Tuple[Engine, ...] = (
    Engine("baidu", "百度识图（自动·真浏览器）",
           "主力同图检索。upload 接口一律 Reject、objurl 深链跳验证码，所以工作台用无头浏览器"
           "把变体文件塞进它自己的上传框（CDP DOM.setFileInputFiles），再读结果页 DOM："
           "识别结论 + 相关商品 + 图片来源网页 + 相似图片。实测可用，全程自动。",
           "https://graph.baidu.com/pcpage/index?tpl_from=pc",
           accepts_image_url=False, home="https://graph.baidu.com/",
           browser=True, browser_entry="https://graph.baidu.com/pcpage/index?tpl_from=pc",
           wait_text=("相似图片",),
           wait_fail_text=("图片上传失败", "上传失败"),
           wait_ready_js="document.querySelectorAll('img').length >= 6",
           extract_js=EXTRACT_BAIDU,
           capture_network=True,
           candidate_limit=30,
           source_label="百度识图结果页",
           contributes_sources=True),
    Engine("baidu_image", "百度图片（按图 URL 检索）",
           "直接把变体的公开 URL 塞进检索串。对已发布的旧网图有效；本机图需要先经公网可达地址。"
           "⚠️ 实测常被跳验证码，能自动化的那条是上面的「真浏览器」。",
           "https://image.baidu.com/search/index?tn=baiduimage&objurl={url}",
           accepts_image_url=True, manual_upload=False, home="https://image.baidu.com/"),
    Engine("bing_cn", "Bing 中国（图片）",
           "⚠️ 实测已失效，不再作为识图源：q=imgurl: 深链被当成普通文字搜索；searchbyimage/upload 返回 404；"
           "visualsearch 页会把图送去 OCR 再跳文字搜索。保留首页链接仅供手动打开核对外，不要再入队。",
           "https://cn.bing.com/images/search?view=detailv2&iss=sbi&FORM=SBIIDP&sbisrc=UrlPaste&q=imgurl:{url}",
           accepts_image_url=False, manual_upload=False, home="https://cn.bing.com/images/"),
    Engine("so360", "360 识图（自动·真浏览器）",
           "第二个国内索引，索引与百度不完全重合。用无头浏览器上传，读「相似图片 / 其他尺寸搜索 / "
           "图片尺寸」和来源页。⚠️ 实测它会对本机 IP 弹验证码（image.so.com 报「访问异常出错」），"
           "被挡时会如实报错；镜像 st.so.com 通常还能用。",
           "https://st.so.com/",
           accepts_image_url=False, home="https://st.so.com/",
           browser=True, browser_entry="https://st.so.com/",
           upload_selector="input[type=file]",
           wait_text=("相似图片",),
           wait_fail_text=("暂未找到相关内容", "访问异常", "验证码"),
           wait_ready_js="document.querySelectorAll('img').length >= 6",
           extract_js=EXTRACT_360,
           candidate_limit=20,
           source_label="360识图结果页",
           contributes_sources=True),
    Engine("sogou", "搜狗识图",
           "⚠️ 实测：搜狗图片站没有公开的上传入口（页面只有关键词框，fileInputs=0），无法作为识图源，"
           "保留首页链接仅供手动打开。",
           "https://pic.sogou.com/",
           accepts_image_url=False, manual_upload=False, home="https://pic.sogou.com/"),
    Engine("yandex", "Yandex Images",
           "国外索引，连接不稳定（本机实测域名可达但结果页 1.7KB 空壳），偶尔能命中国内引擎找不到的境外转载。",
           "https://yandex.com/images/search?rpt=imageview&url={url}",
           accepts_image_url=True, home="https://yandex.com/images/"),
    Engine("tineye", "TinEye",
           "以「找到最早出现的那一份」见长，是来源追踪队列的首选；本机实测连接超时。",
           "https://tineye.com/search?url={url}",
           accepts_image_url=True, home="https://tineye.com/"),
)

ENGINE_MAP: Dict[str, Engine] = {e.key: e for e in ENGINES}

# --------------------------------------------------------------------- platforms
PLATFORM_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"(^|\.)image\.baidu\.com$|(^|\.)graph\.baidu\.com$|(^|\.)baidu\.com$", "百度"),
    (r"(^|\.)bing\.com$|(^|\.)bing\.net$", "Bing"),
    (r"(^|\.)sogou\.com$|(^|\.)sogoucdn\.com$", "搜狗"),
    (r"(^|\.)so\.com$|(^|\.)360\.cn$|(^|\.)qhimg\.com$|(^|\.)qhimgs\d?\.com$", "360"),
    (r"(^|\.)yandex\.(com|ru)$|(^|\.)yandex\.net$", "Yandex"),
    (r"(^|\.)tineye\.com$", "TinEye"),
    (r"(^|\.)pinterest\.[a-z.]+$|(^|\.)pinimg\.com$", "Pinterest"),
    (r"(^|\.)dianping\.com$|(^|\.)meituan\.(com|net)$", "大众点评/美团"),
    (r"(^|\.)meishichina\.com$", "美食天下"),
    (r"(^|\.)xiachufang\.com$", "下厨房"),
    (r"(^|\.)douban\.com$|(^|\.)doubanio\.com$", "豆瓣"),
    (r"(^|\.)zhihu\.com$|(^|\.)zhimg\.com$", "知乎"),
    (r"(^|\.)weibo\.(com|cn)$|(^|\.)sinaimg\.(cn|com)$|(^|\.)sina\.com\.cn$", "微博/新浪"),
    (r"(^|\.)qzone\.qq\.com$|(^|\.)qq\.com$|(^|\.)qpic\.cn$|(^|\.)gtimg\.(com|cn)$", "QQ空间/腾讯"),
    (r"(^|\.)bilibili\.com$|(^|\.)hdslb\.com$", "哔哩哔哩"),
    (r"(^|\.)tieba\.baidu\.com$", "百度贴吧"),
    (r"(^|\.)taobao\.com$|(^|\.)tmall\.com$|(^|\.)alicdn\.com$|(^|\.)1688\.com$", "淘宝/天猫/1688"),
    (r"(^|\.)jd\.com$|(^|\.)360buyimg\.com$", "京东"),
    (r"(^|\.)xiaohongshu\.com$|(^|\.)xhscdn\.com$", "小红书"),
    (r"(^|\.)toutiao\.com$|(^|\.)bytedance\.com$|(^|\.)byteimg\.com$", "今日头条"),
    (r"(^|\.)sohu\.com$", "搜狐"),
    (r"(^|\.)163\.com$|(^|\.)126\.net$|(^|\.)127\.net$", "网易"),
    (r"(^|\.)ifeng\.com$", "凤凰网"),
    (r"(^|\.)qq\.com$", "腾讯"),
    (r"(^|\.)wordpress\.com$|(^|\.)blogspot\.[a-z.]+$|(^|\.)typepad\.com$", "博客平台"),
    (r"(^|\.)flickr\.com$|(^|\.)staticflickr\.com$", "Flickr"),
    (r"(^|\.)archive\.org$|(^|\.)web\.archive\.org$", "Internet Archive"),
    (r"(^|\.)tumblr\.com$|(^|\.)tumblr\.co$", "Tumblr"),
    (r"(^|\.)imgur\.com$", "Imgur"),
    (r"(^|\.)photobucket\.com$", "Photobucket"),
    (r"(^|\.)geocities\.", "GeoCities 遗存"),
    (r"(^|\.)angelfire\.com$|(^|\.)tripod\.com$|(^|\.)50megs\.com$", "老牌免费空间"),
    (r"(^|\.)blogspot\.", "Blogspot"),
    (r"(^|\.)livejournal\.com$", "LiveJournal"),
    (r"(^|\.)forum\.|(^|\.)bbs\.|(^|\.)tieba\.|club\.", "论坛/BBS"),
    (r"(^|\.)smugmug\.com$|(^|\.)500px\.com$|(^|\.)deviantart\.com$|(^|\.)artstation\.com$", "图库/作品集"),
    (r"(^|\.)wikimedia\.org$|(^|\.)wikipedia\.org$", "维基"),
    (r"(^|\.)gov\.cn$|(^|\.)edu\.cn$", "机构站点"),
)

DATE_HINTS: Tuple[Tuple[str, str], ...] = (
    (r"(19[5-9]\d|20[0-2]\d)[-/年.](\d{1,2})[-/月.](\d{1,2})", "full-date"),
    (r"(19[5-9]\d|20[0-2]\d)\s*年", "year-cn"),
    (r"(19[5-9]\d|20[0-2]\d)[-/.](\d{1,2})\b", "year-month"),
    (r"\b(19[5-9]\d|20[0-2]\d)\b", "year"),
)


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def platform_of(url: str) -> str:
    """Coarse platform bucket for a source page (used by bridging + platform stats)."""
    host = host_of(url)
    for pattern, name in PLATFORM_PATTERNS:
        if re.search(pattern, host):
            return name
    if not host:
        return ""
    # A bare IP literal has no meaningful "platform": the last two labels of 127.0.0.1 are
    # "0.1", which used to pollute the platform table with nonsense buckets.
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host) or re.fullmatch(r"\[?[0-9a-f:]+\]?", host):
        return ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def date_hint_of(text: str) -> str:
    for pattern, label in DATE_HINTS:
        match = re.search(pattern, text or "")
        if match:
            return f"{label}:{match.group(0)}"
    return ""


def dispatch_url(engine: str, *, image_url: str = "") -> str:
    """Build the link to open for one queue item (may be a bare entry page)."""
    spec = ENGINE_MAP.get(engine)
    if spec is None:
        return image_url or "about:blank"
    if spec.accepts_image_url and image_url:
        return spec.url_template.format(url=urllib.parse.quote(image_url, safe=""))
    return spec.url_template.format(url=urllib.parse.quote(image_url, safe="")) if image_url else spec.url_template


def engines() -> List[Dict[str, Any]]:
    return [{"key": e.key, "name": e.name, "note": e.note, "accepts_image_url": e.accepts_image_url,
             "manual_upload": e.manual_upload, "home": e.home, "entry": e.url_template,
             "browser": e.browser, "browser_entry": e.browser_entry,
             "upload_selector": e.upload_selector,
             "candidate_limit": e.candidate_limit,
             "contributes_sources": e.contributes_sources,
             "source_label": e.source_label} for e in ENGINES]


# --------------------------------------------------------------- result parsing
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".jfif", ".avif")

# Engine-specific "where are the result images" keys, most specific first.
_JSON_IMAGE_KEYS = ("objurl", "objURL", "thumbURL", "middleURL", "hoverURL", "replaceUrl",
                    "murl", "turl", "imgurl", "imageUrl", "imgUrl", "picUrl", "pic_url",
                    "originUrl", "origUrl", "originalUrl", "srcUrl", "sourceUrl",
                    "thumbUrl", "image_url", "url", "src")
_JSON_PAGE_KEYS = ("fromURL", "fromUrl", "referer", "refer", "pageUrl", "page_url", "hostUrl",
                   "siteUrl", "site_url", "sourcePage", "docUrl", "detailUrl", "link")
_JSON_TITLE_KEYS = ("fromPageTitleEnc", "fromPageTitle", "title", "name", "alt", "desc", "caption",
                    "snippet", "text")


def _clean_url(raw: str) -> str:
    if not raw:
        return ""
    url = html.unescape(raw.strip().strip('",\''))
    url = url.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    if url.startswith("//"):
        url = "https:" + url
    lower = url.lower()
    # Engines embed inline pictures as data: URIs surprisingly often (and the workbench can
    # ingest them), so they must survive URL cleaning instead of being dropped.
    if lower.startswith("data:image/"):
        return url
    if not lower.startswith(("http://", "https://")):
        return ""
    return url


def _looks_like_image(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith(IMG_EXT):
        return True
    return False


def _walk_json(node: Any, out: List[Dict[str, Any]], depth: int = 0) -> None:
    """Collect {image, page, title} triples from arbitrary engine JSON."""
    if depth > 12:
        return
    if isinstance(node, dict):
        lowered = {k.lower(): v for k, v in node.items() if isinstance(k, str)}
        image = ""
        for key in _JSON_IMAGE_KEYS:
            value = lowered.get(key.lower())
            if isinstance(value, str):
                candidate = _clean_url(value)
                if candidate and (key.lower() in ("url", "src") and not _looks_like_image(candidate)):
                    continue
                if candidate:
                    image = candidate
                    break
            elif isinstance(value, list) and value and isinstance(value[0], str):
                candidate = _clean_url(value[0])
                if candidate:
                    image = candidate
                    break
        page = ""
        for key in _JSON_PAGE_KEYS:
            value = lowered.get(key.lower())
            if isinstance(value, str):
                candidate = _clean_url(value)
                if candidate and candidate != image:
                    page = candidate
                    break
        title = ""
        for key in _JSON_TITLE_KEYS:
            value = lowered.get(key.lower())
            if isinstance(value, str) and value.strip():
                title = html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
                break
        if image:
            width = lowered.get("width") or lowered.get("w") or 0
            height = lowered.get("height") or lowered.get("h") or 0
            out.append({"image_url": image, "source_url": page or image, "title": title,
                        "width": int(width) if str(width).isdigit() else 0,
                        "height": int(height) if str(height).isdigit() else 0,
                        "provider": "json"})
        for value in node.values():
            _walk_json(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, out, depth + 1)


def _parse_html(html_text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # 1) <img ...> tags with lazy-load attributes and data-* payloads.
    tag_re = re.compile(r"<img\b[^>]*>", re.I)
    for match in tag_re.finditer(html_text):
        tag = match.group(0)
        src = ""
        for attr in ("data-src", "data-original", "data-imgurl", "data-url", "data-thumb",
                     "data-lazy-src", "data-objurl", "data-murl", "src", "data-srcset", "srcset"):
            found = re.search(rf'{attr}\s*=\s*["\']([^"\']+)["\']', tag, flags=re.I)
            if found:
                value = found.group(1).split(",")[0].strip().split(" ")[0]
                candidate = _clean_url(value)
                if candidate:
                    src = candidate
                    break
        if not src:
            continue
        page = ""
        found = re.search(r'(?:data-fromurl|data-referer|data-pageurl)\s*=\s*["\']([^"\']+)["\']',
                          tag, flags=re.I)
        if found:
            page = _clean_url(found.group(1))
        # nearest enclosing <a href> *before* this img (the card link)
        anchor = html_text.rfind("<a ", 0, match.start())
        if not page and anchor != -1:
            chunk = html_text[anchor:match.start()]
            if "</a>" not in chunk:
                href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', chunk, flags=re.I)
                if href:
                    page = _clean_url(href.group(1))
        title_attr = re.search(r'title\s*=\s*["\']([^"\']*)["\']', tag, flags=re.I)
        alt_attr = re.search(r'alt\s*=\s*["\']([^"\']*)["\']', tag, flags=re.I)
        chosen = title_attr or alt_attr
        out.append({"image_url": src, "source_url": page or src,
                    "title": html.unescape(chosen.group(1)).strip() if chosen else "",
                    "width": 0, "height": 0, "provider": "img"})
    # 2) engine script blobs: JSON inside assignments.
    for blob in re.findall(r"(?:objURL|objurl|murl|thumbURL|imgUrl|picUrl)\s*[:=]\s*[\"']([^\"']+)", html_text):
        candidate = _clean_url(blob)
        if candidate:
            out.append({"image_url": candidate, "source_url": candidate, "title": "",
                        "width": 0, "height": 0, "provider": "script"})
    # 3) bare URLs in the text (catches pasted plain lists, not just HTML).
    for raw in re.findall(r"https?://[^\s\"'<>)\]]+", html_text):
        candidate = _clean_url(raw)
        if candidate and _looks_like_image(candidate):
            out.append({"image_url": candidate, "source_url": candidate, "title": "",
                        "width": 0, "height": 0, "provider": "text"})
    return out


def _parse_plain_text(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[\s|,;\t]+", line)
        image = ""
        page = ""
        for part in parts:
            candidate = _clean_url(part)
            if not candidate:
                continue
            if not image:
                image = candidate
            elif not page and candidate != image:
                page = candidate
                break
        if image:
            out.append({"image_url": image, "source_url": page or image, "title": "",
                        "width": 0, "height": 0, "provider": "text"})
    return out


def parse_results(payload: str, *, content_type: str = "", engine: str = "") -> Dict[str, Any]:
    """Parse an engine result page / JSON / pasted list into candidate records.

    Returns ``{"candidates": [...], "kind": "html|text|json", "raw_count": n}``.
    Duplicates are collapsed by (image_url, source_url) before returning.
    """
    text = payload or ""
    kind = "text"
    candidates: List[Dict[str, Any]] = []
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            data = json.loads(stripped)
            _walk_json(data, candidates)
            kind = "json"
        except json.JSONDecodeError:
            kind = "text"
    if not candidates and re.search(r"<(?:html|body|div|img|a)\b", text, flags=re.I):
        candidates = _parse_html(text)
        kind = "html"
    if not candidates:
        candidates = _parse_plain_text(text)
        kind = "text"
    if not candidates:
        # last resort for single-line HTML blobs without tags we know
        candidates = [c for c in _parse_html(text)] or candidates

    # Collapse by image URL: the same picture often appears both as an <img> and as a bare
    # URL in the page text, and the structured record (real source page / title) must win.
    provider_rank = {"json": 3, "img": 2, "script": 1, "text": 0}
    order: List[str] = []
    best: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        image = item.get("image_url", "")
        if not image:
            continue
        if image not in best:
            order.append(image)
            best[image] = item
            continue
        if provider_rank.get(item.get("provider", ""), 0) > provider_rank.get(
                best[image].get("provider", ""), 0):
            best[image] = item
    unique: List[Dict[str, Any]] = []
    for image in order:
        item = dict(best[image])
        if engine and not item.get("engine"):
            item["engine"] = engine
        item["platform"] = platform_of(item.get("source_url") or item.get("image_url") or "")
        item["host"] = host_of(item.get("source_url") or item.get("image_url") or "")
        item["date_hint"] = date_hint_of(f"{item.get('title','')} {item.get('source_url','')}")
        item["trust"] = classify_trust(item.get("source_url") or "")
        unique.append(item)
    return {"candidates": unique, "kind": kind, "raw_count": len(candidates)}


TRUST_BY_PLATFORM = {
    "Internet Archive": "archive",
    "GeoCities 遗存": "archive",
    "老牌免费空间": "archive",
    "维基": "reference",
    "机构站点": "reference",
    "论坛/BBS": "ugc",
    "QQ空间/腾讯": "ugc",
    "微博/新浪": "ugc",
    "小红书": "ugc",
    "哔哩哔哩": "ugc",
    "豆瓣": "ugc",
    "知乎": "ugc",
    "Pinterest": "curation",
    "大众点评/美团": "curation",
    "美食天下": "curation",
    "淘宝/天猫/1688": "commerce",
    "京东": "commerce",
    "百度": "engine",
    "Bing": "engine",
    "搜狗": "engine",
    "360": "engine",
    "Yandex": "engine",
    "TinEye": "engine",
}


def classify_trust(url: str) -> str:
    """Coarse credibility label used by the source-potential score."""
    platform = platform_of(url)
    return TRUST_BY_PLATFORM.get(platform, "unknown")
