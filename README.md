# 横向视觉搜索工作台 · Visual Search Workbench

> 把一次次零散的搜索，变成一个可管理、可比较、可迭代的批量工作流。
> **搜索引擎负责发现，工作台负责调度、去重、排序和积累知识。**

不是又一个图片收藏站。核心不是"看更多图"，而是**让搜索行为本身可积累**：
每一次检索都留下来源、留下指纹、留下"哪一步带来了新平台"的证据。

```
   一图多变体  ──▶  批量队列  ──▶  真实检索  ──▶  三层去重
   (16 种处理)      (待搜索…)      (自动/手动)     (文件/感知/语义)
                                        │
        新种子回流  ◀──  发散度排序  ◀──  来源聚合  ◀──  视觉聚类
        (扩充搜索面)      E(i) 三队列      (转载合并)      (同物归一)
```

## 三个版本，同一套核心

| 版本 | 位置 | 说明 |
| --- | --- | --- |
| **桌面 WebUI** | [`vsw/`](vsw/README.md) | 完整工作台：七个工作区、无头浏览器自动识图、本机 SigLIP2 语义层 |
| **手机 PWA** | [`vsw/static/mobile/`](vsw/static/mobile/index.html) | 手机浏览器打开 `http://<电脑IP>:8799/m/`，可添加到主屏幕；**有自己的本地库，能离线用** |
| **安卓 APK** | [`vsw-android/`](vsw-android/README.md) | 同一份手机端页面打成的安装包（约 414 KB），可发给别人 |

手机端只负责"随时翻看与收集"。**自动识图必须由电脑执行** —— 无头浏览器在手机上跑不起来，
这是物理限制而不是没做，所以手机端有独立的 IndexedDB 本地库：支持快照同步、离线浏览、
拍照导入、手机上框选；离线期间攒下的操作，回到同一 WiFi 再一键推回电脑。

## 六个核心功能 → 实现位置

| # | 功能 | 在哪 |
| --- | --- | --- |
| 1 | **一图多变体生成器**：每个变体独立 ID + 父子关系，记录"哪种处理方式有效" | [`vsw/imaging.py`](vsw/imaging.py)（16 种变体）、指标页按变体出报表 |
| 2 | **多查询批处理队列**：待搜索/搜索中/已完成/失败/待复查 + 多维筛选 | [`vsw/store.py`](vsw/store.py) `jobs()` / `job_facets()` |
| 3 | **三层去重与聚类**：文件层 SHA-256(像素) / 感知层 pHash / 语义层向量 | [`vsw/service.py`](vsw/service.py) `_ingest_one()` |
| 4 | **来源聚合器**：转载合并成同一个视觉对象，保留全部发现记录 | `findings` / `sources` 表，详情页可展开 |
| 5 | **发散度排序器** `E(i)=0.30N+0.25P+0.25B+0.20M`，三队列 A/B/C | [`vsw/service.py`](vsw/service.py) `rescore_all()`，权重界面上可调 |
| 6 | **搜索链回放**：结果 → 来源 → 变体 → 种子，一路可追 | 详情抽屉的搜索链、搜索图谱页 |

成功指标（指标页实时算）：每 100 次操作带来的**新增不重复图片 / 独立来源 / 新平台**，
以及**每个新来源需要多少次操作**。

## 归档与合并相册

归档 = 把一批结果从"还在挑"变成"这批东西决定了"，落成一条带名字、带日期的批次记录。
批次不会把对象从结果板里挪走，只是打上归属；**归档对象进回收站以后也还能在批次里看到**
（灰显 + 「回收站」标记），因为"归档过什么"是历史，不该因为后来的清理而消失。

**合并相册**解决的是"一次看几批"：在归档表里勾选任意几个批次 →

| 动作 | 结果 |
| --- | --- |
| 合并查看 | 一个跨批次的相册：按对象 id 去重、按发散度降序，标出每条来自哪个批次 |
| 合并导出文件夹 | `vsw/data/archives/merge-<批次>/`，图片硬链接 + 可读 `manifest.json` |
| 合并保存 zip | 同一份内容打成 zip，手机端点一下存到「下载」 |

这是**视图合并**，不改数据库里的批次归属 —— 批次是"当时为什么留下它"的记录，
合并只是"这次我想一起看"。三处入口都用同一套接口：

```bash
GET  /api/archive/merge?ids=24,23,22           # 合并视图（去重 + 排序 + 计数）
GET  /api/archive/merge/zip?ids=24,23,22       # 合并打包
POST /api/archive/merge/export {"ids":[24,23]} # 合并导出文件夹
```

### 批次表上的数字是现算的

`archive_runs` 里存的是**归档那一刻**的快照。早期版本算快照时没带来源，于是把
`sources=0, platforms=0` 写进了库 —— 归档表上「对象 11」旁边写着「0 条来源记录」，
来源列、平台列也都是 0。归档之后对象又在回收站里搬来搬去，快照只会越来越离谱。

现在归档表按**当前成员对象**重算：

- 对象 / 来源 / 平台都按成员现算；快照原值留在 `findings_snapshot` 等字段里备查；
- 对象列写成「5（+6 回收站）」，因为归档与回收站是正交状态；
- 批次记录还在、成员对象已经不在了的，标 **空批次**、数字归零（不再显示存档时的旧数字）；
- 早期自动生成的备注文案按现算重写，用户自己敲的备注不动。

> 统计 SQL 的坑：`LEFT JOIN sources` 会把一个对象乘成 N 行，`COUNT(*)` 数出来的是
> "对象 × 来源数"。必须用 `COUNT(DISTINCT f.id)` —— 在线数据里这个错让批次 #24
> 从 11 个对象变成 22 个。

## 快速开始

```bat
:: Windows：双击即可，会自动打开浏览器
start_vsw.bat
```

```bash
# 或手动
python -m vsw.server --root vsw/data --host 127.0.0.1 --port 8799
```

依赖：Python 3.10+、`Pillow`、`numpy`、`requests`；
自动识图另需 Node 18+ 与本机 Edge/Chrome；
语义层需要本机 SigLIP2 模型（没有也能跑，会自动退化成文件层 + 感知哈希层）。

首次运行会创建 `vsw/data/`（SQLite + 图片）。这个目录**不进版本库** —— 它是用出来的，不是写出来的。

手机端：

```bat
start_vsw_lan.bat        :: 绑 0.0.0.0，启动日志会打印局域网地址和二维码
```

## 自测

```bash
python scripts/selftest_vsw.py            # 276 项，不联网、不跑浏览器
python scripts/selftest_vsw.py --vision   # 285 项，额外覆盖语义层
python scripts/selftest_vsw.py crop       # 只跑某一段
```

自测自带夹具 HTTP 服务器，走真实下载、真实去重、真实回流，不是 mock。
另有针对运行中实例的真实链路验证与 APK 验证：

```bash
python scripts/vsw-verify-live.py     http://127.0.0.1:8799 "D:\images" 12   # 手动回填路径
python scripts/vsw-verify-browser.py  http://127.0.0.1:8799 "D:\images\<素材>" baidu
python scripts/vsw-verify-crop.py     http://127.0.0.1:8799 "D:\images\<素材>"
python scripts/vsw-verify-apk.py                                              # APK 交付验证
node   scripts/vsw-probe-apk-page.js  --apk vsw-workbench.apk --origin http://127.0.0.1:8799
python scripts/vsw-check-syntax.py                                            # 语法闸门（内联脚本一处语法错，整页失效）
```

手机端每个真实修过的坑都有一个行为探针（从 APK 里解出内置页面，用同内核浏览器跑）：
`vsw-probe-import-queue.js`、`-queue-filter.js`、`-queue-thumbs.js`、`-trash-thumbs.js`、
`-archive-view.js`、`-archive-merge.js`。

## 仓库结构

```
vsw/                  桌面工作台 + 手机端页面（同一份手机页面被 APK 复用）
  imaging.py          导入解码、SHA-256、pHash/dHash/aHash、主色、16 种变体
  engine.py           本机 SigLIP2（768 维向量、零样本标签、文字检索）+ BLIP 描述
  store.py            SQLite：seeds/variants/jobs/sources/findings/clusters/ledger/settings
  engines.py          外部引擎定义（入口/选择器/提取脚本）、结果页解析、平台与可信度判定
  service.py          闭环：入库 → 变体 → 队列 → 检索 → 三层去重 → 聚类 → 排序 → 回流 → 图谱
  server.py           纯标准库 HTTP 服务：JSON API + 单页 UI + 媒体服务 + 手机端接口
  static/             桌面端 index.html / app.css / app.js（无框架）
  static/mobile/      手机端 PWA（单文件内联全部代码）+ manifest + service worker
  cdp_driver.js       无头 Chromium + CDP 驱动：上传变体 → 等结果 → 提取结构化 JSON
  NOTES-engines.md    实测笔记：各引擎还能不能自动识图、Bing 为何失效、百度给的边界
vsw-android/          安卓 APK 工程（WebView 外壳 + 原生扫码，不需要 Gradle）
scripts/              自测、真实链路验证、引擎探针、APK 验证
```

## 两个刻意的设计选择

**1. 工作台从不抓取引擎的搜索结果页。**
只做两件事：把查询**派发**到引擎（深链，或用真实浏览器 + CDP 操作引擎自己的上传控件），
以及把结果**导入**回来。理由不是"技术上做不到"，而是抓取会随页面改版不断失效 ——
实测记录见 [`vsw/NOTES-engines.md`](vsw/NOTES-engines.md)：Bing 识图接口已死、
百度 HTTP 接口已封（返回 `Reject`）、360 图片搜索会 IP 拦截。

**2. 宁可如实标注，也不冒充。**
拿不到"原网页归属"时，工作台把可信度标成 `engine`（引擎结果页），而不是当成真实来源页。
同理，抓不到就报抓不到，不填假数据。

## 许可

仓库未附带开源许可文件 —— 即默认保留所有权利。要允许别人自由使用 / 修改 / 分发，
请自行添加 `LICENSE`（例如 MIT）。
