# 实测笔记：以图搜图的自动化可行性（2026-10-16）

这份笔记记录的是**这台机器上真实测出来的结果**，不是推测。工作台里引擎的「能不能自动跑」就是按这些结论标注的。
复现工具：`python vsw-probe-cdp.py <引擎>`、`python vsw-verify-browser.py`。

## 一、结论速览

| 引擎 | 自动识图 | 依据 |
| --- | --- | --- |
| **百度识图** | ✅ 可用（主力） | 无头浏览器打开入口页 → 把变体文件交给它的 `input[type=file]`（CDP `DOM.setFileInputFiles`）→ 页面自己完成签名上传 → 跳结果页 `graph.baidu.com/s?...` |
| **360 识图** | ⚠️ 时好时坏 | 镜像 `st.so.com` 上有 `#stUpload` 文件框，能跑通；但 `image.so.com` 会直接弹「访问异常出错 + 验证码」，实测多次后被墙 |
| Bing 中国（图片） | ❌ 已失效 | 见下节 |
| 搜狗识图 | ❌ 无入口 | `pic.sogou.com` 只有关键词框，没有公开上传入口（`fileInputs=0`） |
| Google Lens / TinEye / SauceNAO | ❌ 连不通 | 连接超时 / TLS 握手失败 |

## 二、Bing：从「按图 URL 搜」变成「把图送去 OCR 再搜文字」

用户反馈的现象，实测确认了三条不同的坏法：

1. **深链被静默丢弃**
   `https://cn.bing.com/images/search?view=detailv2&iss=sbi&FORM=SBIIDP&sbisrc=UrlPaste&q=imgurl:<图片URL>`
   → 302 到 `https://cn.bing.com/images/search?q=世界旅游胜地&FORM=HDRSC2`（把参数当普通文字搜索）。
2. **老的上传端点没了**
   `https://www.bing.com/images/searchbyimage/upload` → **404**（这个接口历史上用过，现已移除）。
3. **visualsearch 页面只会做文字提取**
   用无头浏览器把图片文件塞进它的 `#sb_fileinput`，页面把图**当成 OCR 素材**，跳去
   `www.bing.com/search?q=<识别出来的文字>&...&FORM=SBIVSP&qt=txt`。
   实测两次分别识别成「烘焙技巧与配方」「油炸薯条」——那是 OCR 猜出来的关键词，不是视觉检索。
   也就是说：**在必应这边，"以图搜图"这个能力对我们已经不可用了**，不是参数写错。

因此工作台把 `bing_cn` 降级为「仅手动打开」：
`accepts_image_url=False`、不再作为可自动执行的引擎，UI 上也不会再显示「可深链」。

## 三、百度：唯一的全自动路径，而且**真的能拿到来源页**（走 XHR，不是 DOM）

### 3.1 先说结论

百度识图的产出现在是**带来源页的**，可以进「来源追踪」队列。实测一次上传（美食照片）拿到 30 条结果：

```
来源平台: 大众点评/美团 19、小红书 2、去哪儿 2、花瓣 2、
          淘宝/天猫/1688 1、16pic 1、红动中国 1、昵图网 1、百度 1
带真实来源页的比例: 30 / 30
来源潜力最高 0.81（小红书原帖）
```

### 3.2 为什么必须走 XHR

一开始我按读 DOM 的思路做，结论是「百度给不了来源归属」——因为：

- 结果页 30 个相似图卡片在 DOM 里**全部**指向百度自己的看图器
  `graph.baidu.com/pcpage/similar?carousel=503&image=...&shituToken=...`；
- 整页 HTML 里搜不到任何外部域名（`externalInURLs = 0`），`iframe` 数为 0；
- 试着直接开那个 `similar` 链接 → `graph.baidu.com/errpage?pron=carousel`（缺签名，打不开）。

**但那是 DOM 的错觉。** 用 CDP 的 `Network.enable` 抓这个页面的 XHR，会看到它自己发了一条：

```
https://graph.baidu.com/ajax/pcsimi?carousel=503&entrance=GENERAL&inspire=general_pc
   &limit=30&next=2&render_type=card&session_id=...&sign=...&tk=...&tpl_from=pc&page=1
```

响应是 24KB 的 JSON，**每条结果都带 `fromUrl`**：

```json
{"status":0,"data":{"length":30,"list":[
  {"contsign":"2821776438,4222046131","width":1080,"height":1441,
   "thumbUrl":"http://mms1.baidu.com/it/u=3329642034,1281162831&fm=253&app=138&f=JPEG?w=500&h=667",
   "fromUrl":"http://www.xiaohongshu.com/discovery/item/6064539a00000000210399bb",
   "objUrl":"https://graph.baidu.com/pcpage/similar?carousel=503&..."}
]}}
```

也就是说：**来源页一直在数据里，只是渲染时被丢掉了。**

### 3.3 工作台怎么拿

驱动在等结果的同时把页面的 XHR URL 收集起来，注入成页面里的 `window.__vswCaptured`；
提取脚本（`EXTRACT_BAIDU`）从中找出 `pcsimi` 那条，**在页面上下文里用 `fetch(..., {credentials:'include'})`
回放它**（签名、session、tk 都是页面自己生成的，回放才能过），然后把 `fromUrl` 作为来源页写进候选。
回放完就 `Network.disable`，避免抓包和提取抢同一个控制台。

### 3.4 「相似站点」入口

同一套参数换路径去试 `/ajax/pcsites`、`/ajax/pcsame`、`/ajax/pcsource`… 的结果：

| 路径 | 结果 |
| --- | --- |
| `/ajax/pcsimi` | 200，24KB JSON（主路径，带 fromUrl） |
| `/ajax/pcsimilar` | 200，与 pcsimi 同样 24KB（别名） |
| `/ajax/pcsites`、`/ajax/pcsame`、`/ajax/pcsource`、`/ajax/pcfrom`、`/ajax/pcpage`、`/ajax/pcgoods` | **400 Bad Request** |
| 页面 `/pcpage/similar-sites`（无会话） | 400 |

结论：**没有独立的「相似站点」接口可调**（至少这些名字都没有，且 `/pcpage/similar-sites` 缺会话直接 400）。
但这件事已经不再重要了——`pcsimi.fromUrl` 每个结果都给来源页，等于把「相似站点」的效果直接拿到了：
30 条结果里 19 条来自大众点评、2 条来自小红书原帖。

### 3.5 顺带说明

- 百度结果里的 `w`/`h` 是**原图尺寸**（不是缩略图尺寸），元数据价值因此更高；
- 百度只给 http:// 形式的 `fromUrl`，工作台落库前统一升成 https://（小红书、大众点评这些站早已只走 https，
  原样存会让链接点不开）；
- 提取脚本保留 DOM 兜底：万一 `pcsimi` 没抓到（改版/限流），仍然按原来的方式从渲染出的缩略图里取，
  只是那种情况下没有来源页。

## 四、工作台里的实现位置

- `vsw/cdp_driver.js` —— 无头 Chromium + CDP 驱动（单进程单任务 CLI，Python 起它、读一份 JSON、必要时杀掉）。
  做五件事：起浏览器 → 把文件塞进上传框 → 等结果（URL/正文/自定义 JS 条件）→（可选）抓 XHR 并把 URL
  注入 `window.__vswCaptured` → 执行提取脚本并回收 JSON。
- `vsw/engines.py` —— 每个引擎的 `browser` / `browser_entry` / `upload_selector` / `wait_text` /
  `wait_fail_text` / `extract_js` / `candidate_limit` / `contributes_sources` / `source_label`。
  提取脚本放在引擎定义旁边，因为 DOM 知识属于引擎，不属于驱动。
- `vsw/service.py` —— `browser_search()`：跑驱动 → 候选 → **复用同一条三层去重/聚类/打分管线**。
  也就是说自动识图和手动回填的结果，走的是完全一样的下游处理。
- `vsw/server.py` —— `POST /api/job/{id}/browser`（后台线程跑，几十秒不阻塞 HTTP）+ `GET /api/browser/state`。
- UI —— 队列项上的「自动识图」按钮 + 队列头部的进度胶囊，完成后自动刷新结果板。

Node 运行时：优先 `VSW_NODE` 环境变量，其次 PATH，再退到已知安装位置。驱动只用标准库
（`node:child_process` + 全局 `WebSocket`），不依赖 `ws` 包也能跑（Node ≥ 21）；装了 `ws` 则自动回退使用。

## 五、还想更省事的话

1. **给自动路径加轮换**：360 被验证码挡住时，换 UA / 换出口 IP 可能又能用；目前只是如实报错。
2. **接一个真正的视觉检索 API**（如果有付费额度）：工作台的引擎层是插拔式的，加一个 `Engine` 条目 +
   一个 `extract_js`（或者干脆走 HTTP 就够）即可，无需动去重/排序那一层。
3. **百度的「相似站点」再挖一层**：现有名字（`pcsites`/`pcsame`/`pcsource`/`pcfrom`/`pcpage`/`pcgoods`）
   全是 400，而且 `/pcpage/similar-sites` 无会话直接 400。要真正挖它，得在**有会话**的前提下把结果页里
   所有按钮点一遍、同时抓包看它请求了哪个路径——这一轮我没做（收益也不大：`pcsimi.fromUrl` 已经把
   来源页给了）。下面是这一轮的负结果，免得再试一遍。

| 试过的路径 | 结果 |
| --- | --- |
| `/ajax/pcsimi` | 200 · 24KB JSON（带 fromUrl，主路径） |
| `/ajax/pcsimilar` | 200 · 与 pcsimi 同样的 JSON（别名） |
| `/ajax/pcsites` | 400 |
| `/ajax/pcsame` | 400 |
| `/ajax/pcsource` | 400 |
| `/ajax/pcfrom` | 400 |
| `/ajax/pcpage` | 400 |
| `/ajax/pcgoods` | 400 |
| `/pcpage/similar-sites`（无会话） | 400 Bad Request |
