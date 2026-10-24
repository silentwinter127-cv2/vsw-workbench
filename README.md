# QQ 群相册本地备份（WebUI + SQLite）

把 QQ 群相册**完整备份到本地**：相册、照片的全部可抓元数据（上传者、上传时间、描述、尺寸、批次）、
群动态里的**评论与回复、点赞**、群成员昵称，全部写入 SQLite；同时可把**原图下载**到本地，
并提供一个本地 WebUI 用来浏览、搜索、筛选和分类。

参考项目：[ShunCai/QQ-Group-Albums-Downloader](https://github.com/ShunCai/QQ-Group-Albums-Downloader)、
[ShunCai/QZoneExport](https://github.com/ShunCai/QZoneExport)。

---

## 1. 特性

| 能力 | 说明 |
| --- | --- |
| 元数据全量抓取 | 78 个相册 / 44,930 张照片（本项目实测）一次抓完约 1 分钟 |
| 上传者 | 每张照片的上传者 QQ 号 + 昵称，并解析全部群成员昵称 |
| 上传时间 | 服务器时间戳，同时保存原始 epoch 与本地时间文本 |
| 评论 / 回复 | 动态里的评论与楼中楼回复（含作者、时间、内容） |
| 点赞 | 每条动态的点赞人与点赞数，昵称按 QQ 号批量回填 |
| 原图下载 | 走 CDN 原始尺寸地址，支持并发、断点续传、失败重试、尺寸回退 |
| 视频下载 | 群相册视频的原片（单独一页可播放/另存为）；地址需向机器人引擎现取，见 §3.3 |
| 按封面找视频 | 视频没有独立 AI 索引，但封面照片有：用「只看视频」或视频页搜索框按封面画面/描述/标签找，顺带带出发布者与点赞评论，见 §3.4 |
| 本地浏览 | 相册总览、照片网格、视频、上传者排行、评论流、随机精选、时间线 |
| 搜索 | 描述 / 上传者 / 照片 ID / 批次 ID / **评论内容** 全文搜索 |
| 分类 | 手动标签 +「按上传者自动打标签」，按标签筛选 |
| 导出 | CSV / JSON 整库导出 |
| 增量备份 | 按相册记录游标，二次运行只抓新照片；断点续传可跨多次运行 |
| 无第三方依赖 | 只用 Python 标准库 + Pillow 生成缩略图（可选） |

### AI 语义检索（SigLIP2，本地运行）

| 能力 | 说明 |
| --- | --- |
| 文搜图 | 用中文或英文描述找图，例如「夜晚的城市街道」「一只猫」「电脑截图」 |
| 以图搜图 | 拖入 / 粘贴 / 选择任意本地图片，找相册里最像的图 |
| 相似图片 | 在任意照片详情里点「找出相似图片」 |
| AI 打标 | 每张图自动打上最多 12 个标签（94 个词表），附带相对置信度 |
| 图片描述 | BLIP 为每张图生成一句英文描述（如 “a girl with purple hair and a pink background”） |
| 描述检索 | 中文查询会先用本地翻译模型转英文，再对描述做全文检索（FTS5） |
| 组合筛选 | 语义结果可再叠加相册 / 上传者 / 时间范围 / AI 标签 |
| 无网络依赖 | 模型从 `D:\siglip2-base-patch16-224` 等本地目录加载，离线可用 |

语义检索 + 描述检索是**两路合并**的：每张结果卡片会标出「语义」还是「描述匹配」，
命中描述时直接显示那句话，两者按可配置权重（默认各 50%）混合排序。
描述检索不依赖向量索引，即使还没建索引也能用。

实测（群 684690254）：按相册标题做标注集验证，同一批照片用 4 个查询排序，
每个查询都在自己的类别上取得最高分，**自身查询 top-1 命中率 75%**；
以图搜图时同批次照片相似度 0.7~0.85，不同内容约 0.1，区分度良好。

> 打标速度约 **12 张/秒**（20 核 CPU，14 线程），4.5 万张约 1 小时。
> 描述速度约 **2.5 张/秒**（int8 量化 + batch 12），4.5 万张约 5 小时。
> 索引期间可以正常浏览和搜索，已处理的部分立即可用；侧栏会显示两条进度，
> 支持随时停止、之后再「继续」（已完成的不会重算）。


### 实测结果（群 684690254）

```
相册 78 · 照片 44,932 · 已下载原图/动图 43,304（8.3 GB）· 仅缩略图 1,628 · 失败 0
评论 2,334（含回复）· 动态 5,172 · 点赞记录 2,661 条 · 群成员 559 · 上传者 315
媒体类型：普通照片 34,117 · 动图(GIF) 1,158 · 其它 QZone 类型 9,657
字段完整度：上传者 100% · 上传时间 100% · 原图地址 100% · 与动态关联 44,855/44,932
```

两个正常现象，不是缺陷：

* **仅缩略图（1,628 张，3.6%）**：QQ 服务器上这些照片本来就没有原图，
  CDN 只返回 16×16～32×32 的占位图。程序按**像素尺寸**判定（不是按字节数），
  保留占位图并标记为 `thumb-only`，不会反复重试，也不会误伤正常的小图。
* **点赞明细是分批补全的**：动态列表接口只返回少量点赞人，程序用详情接口补全，
  但该接口大约每 50 次调用后限流（报「请求超时」）。每次运行补一批，多跑几次可逐步补全；
  点赞**数量**在首次抓取时就是准确的（写入 `photos.like_count`）。


## 2. 环境要求

* Windows（本机实测 Windows + Edge）
* Python 3.10+（实测 3.13）
* 本机已安装 Edge 或 Chrome（用于复用登录态）
* 可选：`Pillow`（生成缩略图；缺失时缩略图回退到 QQ CDN）

```powershell
pip install pillow
```

## 3. 快速开始
```powershell
cd C:\Users\Administrator\Documents\deepseek-harness\default-workspace
python -m qqalbum 684690254
```

或双击 `run_backup.bat`（默认群号 684690254，可传参：`run_backup.bat 123456`）。

启动后：

1. 程序自动拉起一个**独立的 Edge 窗口**（用户数据目录 `D:\QQAlbumBackup\edge-profile`），
   首次需要**手机 QQ 扫码登录**一次；登录态会长期保存在该目录。
   > 它使用独立配置目录，因此**不影响你日常使用的 Edge**，也不会读取你原配置里的登录态。
2. 命令行打印 WebUI 地址（默认 <http://127.0.0.1:8787/>），浏览器会自动打开。
3. 在左侧面板点击「**开始抓取元数据**」→ 抓取相册、照片、评论、点赞、成员。
4. 再点击「**下载原图（未完成的）**」→ 把原图下载到 `D:\QQAlbumBackup\photos\<群号>\...`。

### 3.1 开机自动启动（重启后机器人要能连上，就必须配）

**这个服务不会自己开机启动。** 重启电脑后如果不手动打开它，QQ Agent 的
「群相册检索」技能就会报「相册备份服务没启动」—— 服务没跑，端口自然没人监听。

目录里现在有三个启动脚本：

| 脚本 | 用途 |
| --- | --- |
| `start_album.bat` | **手动启动**（前台窗口）。先检查 8787：已在跑就不重复启动；被别人占用会**明确报错**而不是偷偷换端口 |
| `autostart_album.bat` | **开机自启入口**。已在跑就什么都不做；没跑就调 `start_album_hidden.vbs` 隐藏窗口启动，最多重试 3 次，全过程记到 `tools\autostart.log` |
| `stop_album.bat` | 停止服务（只结束占用 8787 的 python 进程） |

已经注册好了一个登录时触发的计划任务（延迟 30 秒，失败自动重启 3 次）：

```powershell
# 查看状态
Get-ScheduledTask -TaskName QQAlbumBackup-WebUI | Select-Object TaskName,State
# 手动触发一次（不用重启就能验证）
Start-ScheduledTask -TaskName QQAlbumBackup-WebUI
# 不再需要自启时删掉
Unregister-ScheduledTask -TaskName QQAlbumBackup-WebUI -Confirm:$false
```

> **为什么脚本要写这么啰嗦**：`python -m qqalbum` 在 8787 被占用时会**静默**改用
> 8788 —— 而机器人技能里配的是 8787，结果就是「服务明明在跑，机器人却连不上」。
> 所以启动脚本宁可直接报错，也不换端口。

### 3.2 健康检查

```powershell
curl http://127.0.0.1:8787/api/health
# {"ok":true,"service":"qqalbum","group_id":"684690254","version":"QQAlbumBackup/1.0"}
```

不碰数据库、不加载模型，可用它判断「服务起没起、是不是这个端口」。

### 3.3 视频（需要 QQ Agent 在运行）

相册的**网页接口不返回视频地址** —— 实测 `qun_list_photo_v2` 只有 18 个字段，
没有 `videourl`/`videodata`，所以早期版本只能把视频的**封面图**当普通照片存下来
（`btype=1`，`is_video` 永远是 0）。真正的播放地址只有机器人引擎
（QQ Agent 里的 **SnowLuma**）会给：

```
POST http://127.0.0.1:3000/get_group_album_media_list
Authorization: Bearer <令牌>
{"group_id":684690254,"album_id":"<albumId>"}
→ mediaList 里视频项为 {"video":{"id":…,"url":"….mp4","cover":{…}}}
```

所以：

- 抓视频元数据时**必须让 QQ Agent 处于运行状态**；它没开时只记一行日志跳过视频，
  **不会**让照片抓取失败。界面上的「视频元数据」复选框控制这一项。
- 令牌自动获取，顺序：`QQALBUM_SNOWLUMA_TOKEN` 环境变量 →
  QQ Agent `/api/onebot-token` → QQ Agent `data/config.json` → SnowLuma `config/onebot_*.json`。
  都拿不到时明确报错，不会静默。
- 视频地址**带签名会过期**（`dis_k`/`dis_t`），所以下载前会现场重取；遇到 403 自动换一条重试。
- 文件落在 `D:\QQAlbumBackup\videos\<群号>\<相册ID>\<video_id>.mp4`，与照片目录并列。
- 视频**不参与 AI 检索**（SigLIP2 是图片模型），但它的封面照片在照片库里，
  可以用「视频」页看、按相册/已下载筛选、在线播放（支持拖动进度条）或「另存为」。
- 同一个视频会挂在多个相册下，入库时按 `video_id` 去重；封面用地址里的 **pickey** 与
  `photos.lloc` 对齐（`video.cover.lloc` 是另一种编码，对不上）。
- 时长字段叫 **`videoTime`**，挂在**外层 media 项**上（毫秒）—— `video` 对象里没有
  `time`/`duration`，按那两个名字取会一直是空的。
- 发布者与上传时间也能从 media 项（`uploader` / `uploadTime`）拿到；
  视频表里没存时用封面照片那份补上。

### 3.4 用封面找视频（视频本身没有 AI 索引，封面有）

视频不需要单独建索引：**封面本身就是一条 `photos` 记录**，而照片那条链路已经给它
做完了 AI 标签、BLIP 描述和 SigLIP 向量。所以「搜封面」就等于「搜视频」，而且顺带
就能拿到发布者、上传时间、评论数与点赞数：

| 想找什么 | 怎么用 |
| --- | --- |
| 按画面描述找视频 | 视频页的搜索框，例如「黑色背景」「树林」「poster」 |
| 在全部照片里只看视频 | AI 语义检索页的「**只看视频**」下拉（`/api/ai/search?media=videos`） |
| 以图搜视频 | 以图搜图也认 `media=videos`（拖入一张图 → 只看视频） |
| 组合筛选 | `media=videos` 可以和相册 / 上传者 / 时间 / AI 标签 / 排序叠加 |

两个必须知道的边界：

- **搜的是封面，不是视频内部**。封面常常是标题卡或纯色底，所以「黑色背景」这种能
  找到一堆，而具体内容（比如视频里的动作）搜不到。
- 视频只有几百个、照片四万多，所以在「全部」结果里视频**几乎露不出来**
  （实测前 30 条里 0 个视频）—— 想找视频**必须**切到「只看视频」。

## 4. 数据存放位置（D 盘）

```
D:\QQAlbumBackup\
├── qqalbum.sqlite3          # 全部元数据（相册/照片/评论/点赞/成员/原始 JSON）
├── photos\
│   ├── 684690254\
│   │   ├── <相册ID>\<上传者QQ>\<照片ID>.jpg     # 原图
│   │   └── _thumbs\                             # 缩略图缓存（自动生成）
│   └── _covers\                                 # 相册封面缓存
└── edge-profile\            # 浏览器登录态（只放登录信息，可随时删）
```

数据库路径、图片路径、端口都可用参数覆盖（见第 6 节）。

## 5. WebUI 功能

* **相册总览**：统计卡片（相册/照片/已下载/评论/点赞/动态/上传者/占用空间）+ 相册封面墙，
  点封面直接进入该相册的照片列表。
* **全部照片**：网格浏览；可按相册、上传者、标签筛选，可切换「有评论 / 有点赞 / 未下载 /
  已存原图 / 仅缩略图」，支持按最新、最早、评论最多、点赞最多排序，分页。
  * 「未下载」= 本地还没有可用文件（真正待办）；
  * 「已存原图」= 已存下完整原图；「仅缩略图」= QQ 端没有原图、只存到占位图。
* **照片详情**（点击任意照片）：原图预览（← → 键翻页）、上传者、上传时间、批次、尺寸、
  本地文件状态、该照片关联的**评论与回复全文**、**点赞名单**、标签管理。
* **上传者**：照片数 / 评论数 / 点赞数排行，点击直接筛出该人的照片。
* **AI 语义检索**：中文/英文描述搜图、以图搜图（拖入或 Ctrl+V 粘贴）、相似图片、
  AI 标签云；结果可叠加相册 / 上传者 / 时间范围 / 多标签筛选，并按相似度显示百分比。
  另有「**只看视频 / 只看图片**」下拉（见 §3.4），以及相关度 / 纯语义 / 时间 / 上传者 / 互动排序。
* **视频**：网格播放（支持拖动进度条）、按相册与已下载筛选、分页、「另存为」；
  顶部搜索框可**按封面描述/标签找视频**，结果带发布者、上传时间、评论数与点赞数。
* **时间轴**：按「年 → 月 → 日」分组浏览全部照片，日期分隔条滚动吸顶；
  右侧年/月索引可**一键跳页**（跳到某年、或某年某月），滚到底自动加载下一页。
  跳页按**月份切片**而不是 offset 翻页 —— offset 翻到几万条会越来越慢，
  按月份切片永远只扫一个月（实测 3 千多张的月份 0.3 秒）。
  接口：`/api/time-axis`（年→月→日 计数）与 `/api/time-photos?year=&month=&page=`。
* **评论流**：最新 200 条评论，可跳回对应照片。
* **随机精选 / 时间线**：随机浏览已下载图片、按月份统计上传量。
* **导出**：一键下载 CSV / JSON 全量清单。

## 6. 命令行参数
```
python -m qqalbum <群号> [选项]

--db PATH         SQLite 路径（默认 D:\QQAlbumBackup\qqalbum.sqlite3）
--root PATH       图片与缓存根目录（默认与数据库同目录）
--profile PATH    浏览器用户数据目录（默认 D:\QQAlbumBackup\edge-profile）
--port N          WebUI 端口（默认 8787，占用时自动 +1 避让）
--browser PATH    指定 Edge/Chrome 可执行文件
--headless        无头模式（已登录后可用）
--workers N       下载并发数（默认 4）
--page-size N     照片分页大小（默认 300）
--no-open         不自动打开浏览器
--no-web          只跑命令行抓取，不起 WebUI
--sync            启动后立即抓取一次
--download        启动后立即下载原图
--full            关闭增量（重新全量抓取）
--status          打印数据库统计后退出
--groups          列出已备份的群
--siglip-dir PATH 本地 SigLIP2 模型目录（默认自动探测 D:\siglip2-base-patch16-224）
--ai-index        启动后立即建立 AI 向量索引与标签
--ai-limit N      本次 AI 索引最多处理多少张（0=全部）
--ai-retag        重新打标（覆盖已有 AI 标签）
--caption-dir P   本地 BLIP 描述模型目录（默认自动探测 D:\blip-image-captioning-base）
--caption         启动后立即生成图片描述（BLIP）
--caption-limit N 本次最多生成多少条描述（0=全部）
--recaption       重新生成描述（覆盖已有）
--videos          抓取时一并抓视频元数据（需 QQ Agent 的 SnowLuma 引擎在运行）
--download-videos 启动后立即下载未完成的视频
--video-limit N   本次最多下载多少个视频（0=全部）
```

示例：

```powershell
# 只抓元数据并直接退出（跑批）
python -m qqalbum 684690254 --no-web --sync

# 无头下载原图
python -m qqalbum 684690254 --no-web --download --workers 8

# 建立 AI 索引（一次性，约 1 小时；也可在 WebUI 里点按钮）
python -m qqalbum 684690254 --no-web --ai-index

# 只先索引 2000 张试试
python -m qqalbum 684690254 --no-web --ai-index --ai-limit 2000

# 查看已备份的群
python -m qqalbum --groups
```

## 6.1 AI 功能的依赖与模型

AI 检索需要三样东西，都已经在本机可用：

| 组件 | 现状 |
| --- | --- |
| PyTorch | `torch 2.14.0+cpu`（CPU 版，无需显卡） |
| Transformers | `transformers 5.17.0`（提供 `Siglip2Model` / `BlipForConditionalGeneration`） |
| SigLIP2 权重 | `D:\siglip2-base-patch16-224`（`model.safetensors` 1.5 GB） |
| BLIP 描述权重 | `D:\blip-image-captioning-base`（`pytorch_model.bin` 944 MB） |
| 中英翻译权重 | `D:\opus-mt-zh-en`（297 MB，可选；缺失时退化为内置关键词映射） |

> 另外还下载了 `microsoft/Florence-2-base`（`D:\Florence-2-base`，887 MB），
> 但它的自定义代码与本机 `transformers 5.17` 不兼容
> （报 `'Florence2LanguageConfig' object has no attribute 'forced_bos_token_id'`），
> 因此改用 BLIP。这个目录可以删掉，不影响任何功能。

> 翻译模型需要 `sentencepiece` 与 `sacremoses` 两个包（已装）。

若换机器需要补装（国内镜像）：

```powershell
pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple torch transformers sentencepiece sacremoses
```

本机镜像 `https://hf-mirror.com` 可用（`huggingface.co` 直连超时），
下载模型用 `tools/fetch_model.py`（已自动关闭 Xet 协议，否则镜像会 401）：

```powershell
python tools/fetch_model.py Salesforce/blip-image-captioning-base D:\blip-image-captioning-base
python tools/fetch_model.py Helsinki-NLP/opus-mt-zh-en D:\opus-mt-zh-en
```

模型目录可用 `--siglip-dir` / `--caption-dir` 或环境变量 `QQALBUM_SIGLIP_DIR`、
`QQALBUM_CAPTION_DIR`、`QQALBUM_TRANSLATE_DIR` 指定；
缺失时 WebUI 会明确提示哪一项不可用，其余功能不受影响。

调优用的环境变量：

* `QQALBUM_TORCH_THREADS=14`：SigLIP 打标线程数（默认 ≤12）。
* `QQALBUM_CAPTION_THREADS=5`：BLIP 描述线程数（默认约半数核心）。
* `QQALBUM_CAPTION_BATCH=12`：BLIP 批大小，越大越快但更吃内存。


## 7. 抓取原理（为什么不需要逆向签名）

QQ 群相册的 H5 页面（`h5.qzone.qq.com/groupphoto/index`）通过同源接口读写数据，
签名参数 `g_tk` 由 `p_skey`/`skey` 现场计算，另有 `qzonetoken`。

本工具用 **CDP（Chrome DevTools Protocol）** 驱动一个已登录的 Edge：

1. 打开群相册页面，注入一段 JS 桥（`qqalbum/js_bridge.py`），
   由它负责 `g_tk` 计算、JSONP 解包、同源 `fetch`；
2. Python 侧通过 `Runtime.evaluate` 调用该桥，拿到解析后的 JSON；
3. 图片走 CDN 直链下载（无需 Cookie）。

用到的接口（全部来自页面自身 JS 中的定义）：

| 用途 | 接口 |
| --- | --- |
| 相册列表 | `u.photo.qzone.qq.com/cgi-bin/upp/qun_list_album_v2`（`cmd=qunGetAlbumList`） |
| 照片列表 | `u.photo.qzone.qq.com/cgi-bin/upp/qun_list_photo_v2`（`cmd=qunGetPhotoList`，`start/num` 分页） |
| 群动态（评论/点赞） | `r.qzone.qq.com/cgi-bin/user/group_feeds_get.cgi`（`cmd=qunGetFeeds`，`attach_info` 游标，单页上限 10 条） |
| 动态详情（完整点赞/评论） | `h5.qzone.qq.com/webapp/json/groupphoto/qunGetDetail`，由页面 `logic.getFeedDetail()` 调用 |
| 成员昵称 | `u.photo.qzone.qq.com/cgi-bin/upp/qun_list_member_info`（一次约 60 个 QQ 号） |
| 单张照片评论 | `u.photo.qzone.qq.com/cgi-bin/upp/qun_list_photocmt_v2` |

工程上的几个关键处理：

* 群动态接口只接受页面自身的 JSONP 包装（外部 `fetch`/`XHR` 会被 500 拒绝），
  因此动态抓取调用页面内的 `GroupZone.GPHOTO.logic.getFeeds()`；
  由于一次 CDP 往返才能取 10 条，程序在页面内用一个循环一次取多页（默认 20 页/次），
  实测比逐页调用快约 4 倍。
* **断点续传**：每 200 条新动态把 `attach_info` 游标写库；相册按相册记录
  `album_cursor:<群>:<相册>`（已抓到的最大上传时间）。中断后重跑只补新内容，
  不会重放已入库的页。
* **自动恢复**：长时间抓取后页面可能卡死（fetch 永久挂起、WebSocket 读超时），
  此时连探测用的 `Runtime.evaluate` 都会超时。程序对超时类错误**直接重启浏览器进程**
  并从游标继续（登录态在配置目录里，不受影响）；相册列表、单个相册、动态抓取
  各自都有这一层保护，单步恢复后会继续完成整个任务。
* **增量语义**：每个相册都会抓第一页再按上传时间游标提前停止，因此
  “相册内新增一张照片”这类改动不会因为外部计数没变而被跳过。
* **限流**：动态详情接口大约 50 次调用后开始超时，程序保留已抓数据、记录进度并优雅退出，
  下次运行继续补。
* **前端缓存**：`/static/*` 以 `no-store` 返回（本地改前端代码后刷新即生效），
  缩略图/封面等媒体仍走 `max-age=86400` 缓存以保证滚动流畅。
* **临时文件**：下载先用 `<文件>.part.<线程>` 写入再原子改名；每轮下载开始时会清理
  历史残留的 `.part` 文件，磁盘上只保留最终图片。


## 8. 数据库结构

| 表 | 内容 |
| --- | --- |
| `groups` | 群号、统计、最后同步时间、服务器时间偏移 |
| `albums` | 相册标题/描述/封面/张数/创建者/创建时间/隐私 |
| `photos` | 照片描述、上传者 QQ+昵称、上传时间、批次、尺寸、原图 URL、本地文件状态、媒体类型（照片/动图/视频）、评论数/点赞数、原始 JSON |
| `feeds` | 每条上传动态：批次号、动态时间、上传者、相册、照片数、评论数、点赞数、`busi_param` |
| `comments` | 评论与回复（`is_reply`、`parent_id`、作者、时间、内容、来源） |
| `likes` | 每条动态的点赞人（QQ + 昵称） |
| `feed_photos` | 动态里的图片（`pic_key`、顺序、原图 URL） |
| `members` | 群成员昵称、群备注、角色 |
| `tags` / `photo_tags` | 分类标签 |
| `downloads` / `sync_log` | 下载与同步审计日志 |
| `photo_fts` | FTS5 全文索引（描述/上传者/相册） |
| `model_tags` | AI 标签词表（94 条，含中文名与英文 prompt、颜色） |
| `photo_ai_tags` | 每张图的 AI 标签：`score`（余弦相似度）、`confidence`（图内相对值）、`rank` |
| `photo_embedding` | 每张图的 768 维归一化向量（float32 BLOB，约 3 KB/张） |
| `photo_caption` | BLIP 生成的一句英文描述（`caption` 原文 / `caption_en` 可检索文本） |
| `caption_fts` | 描述的 FTS5 全文索引（中英关键词检索走这里） |
| `videos` | 群相册视频：`video_id`、相册、封面 pickey、时长/尺寸、播放地址、下载状态与本地路径 |
| `photos.raw_json` 等 | 每张照片、每条动态的**原始 JSON**，保证零信息丢失 |

照片与评论通过 `batch_id`（动态里的 `busi_param["21"]`，与照片的 `batchid` 一致）关联。
`link_photos_to_feeds` 会把评论数/点赞数回填到 `photos`。

> 说明：QQ 服务端返回的上传时间戳本身处于「未来」（本项目实测为 2026 年），
> 本工具如实存储原始 epoch 并按其换算本地时间，不做修正。

## 9. 项目结构

```
qqalbum\
├── __main__.py        # python -m qqalbum 入口
├── cli.py             # 命令行解析与启动
├── cdp.py             # 纯标准库 CDP/WebSocket 客户端（含自动重连）
├── js_bridge.py       # 注入页面执行的 JS 桥 + 字段归一化
├── qzone.py           # 业务 API 封装（相册/照片/动态/成员/动态详情）
├── store.py           # SQLite 表结构、迁移与读写
├── crawler.py         # 抓取编排、增量游标、断点续传、进度状态
├── downloader.py      # 原图/动图并发下载（尺寸回退、占位图识别）
├── siglip.py          # SigLIP2 封装 + 94 条标签词表 + 向量序列化
├── caption.py         # BLIP 描述封装（int8 量化/批处理）+ 中英查询处理
├── ai.py              # AI 索引与描述任务、语义/描述/以图搜图检索
├── web.py             # 标准库 HTTP 服务 + JSON API + 媒体代理
└── static\            # WebUI（index.html / style.css / app.js）

run_backup.bat         # Windows 一键启动（WebUI）
run_tests.py           # 单元测试（42 项，无需浏览器/网络）
run_sync.py            # 命令行抓取脚本（调参/验证用）
run_download.py        # 命令行下载脚本（调参/验证用）
tools\                 # 验证与排查脚本
├── acceptance.py      # 全量验收：D 盘布局 + 数据完整性 + 全部 WebUI 端点
├── test_webui.py      # WebUI 端点 HTTP 冒烟测试
├── e2e_clean.py       # 空库端到端验证
├── diag_*.py          # 数据完整性/错误原因排查
├── shot_ui.py         # 自动截图（CDP）
└── research\          # 最初逆向接口时用的抓包脚本
```

## 9.5 让其他人访问（手机 / 外地朋友）

**先读这一条**：这个界面能看到**群里其他人的**昵称、QQ 号、全部照片和评论原文。
它默认只绑 `127.0.0.1`，所以以前不需要口令；**一旦要让别人访问，就等于把这些内容
交出去** —— 所以放开的**前提是有口令**。

```bat
:: 1) 局域网（同一个 WiFi 下的手机/平板）
python -m qqalbum <群号> --host 0.0.0.0 --password 你的口令
:: 然后手机浏览器打开  http://<本机IP>:8787/
::   本机 IP 启动时会打印（例如 http://192.168.1.46:8787/）

:: 2) 外地的人（Cloudflare 快速隧道，免注册）
share_online.bat <群号> 你的口令
::   会打印一个 https://xxxx.trycloudflare.com 的网址，把它和口令一起发出去
```

口令也可以放在环境变量里，避免出现在命令历史中：`QQALBUM_PASSWORD=...`

### 口令存哪里（不要写死在 .bat 里）

口令放在启动脚本旁边的 **`album_password.txt`**（首次运行自动生成），启动脚本**读取**它：

* 不写死在 `.bat` 里 → 不会进版本库；
* 从文件读再传给 python → 不出现在进程命令行里（`Get-CimInstance Win32_Process` 看不到），
  比直接 `--password xxx` 或用环境变量更干净；
* 文件已在 `.gitignore` 中；
* 万一文件丢了，启动脚本会现场生成一个新的并保存。

**为什么必须这样**：开机自启链路（计划任务 → `autostart_album.bat` →
`start_album_hidden.vbs` → `start_album.bat`）原来没有传口令。在 Funnel 指向本机端口
之后，**一次重启就会把一个没有口令的界面发布到公网**。现在整条链路都会读口令，
拿不到口令时**拒绝启动**，而不是无口令裸奔。

### 安全设计（以及为什么这么设计）

| 措施 | 说明 |
| --- | --- |
| 口令用 PBKDF2 存哈希 | 内存里不留明文；比较用 `hmac.compare_digest`，防时序侧信道 |
| 会话是随机 token + HttpOnly cookie | 12 小时过期，重启服务即失效 |
| **只有确实走 HTTPS 才加 `Secure`** | 否则通过 http 访问时浏览器不回传 cookie，表现为「登录了却一直弹登录框」 |
| 失败限速 | 同一 IP 连续失败 5 次后开始递增延迟（最多 60 秒），防口令暴破 |
| **先验口令、再罚失败** | 反过来的话，别人拿错口令刷几次就能把合法用户一起挡在门外（拒绝服务） |
| `--host 0.0.0.0` 且没给口令时**自动生成** | 不允许「绑对外但没口令」的裸奔状态，生成的口令会打印出来 |
| `/api/health` 免验证 | 自启动脚本和机器人技能靠它探活，它们没有会话 |
| `--no-auth` 需要显式声明 | 而且绑对外时会在启动日志里再警告一次风险 |

未登录时：`/api/*` 返回 `401 {"auth_required":true}`，首页返回登录页，
**静态资源也一起挡住**（`/static/app.js` 也是 401，避免靠猜路径绕过）。

### 固定地址（不用每次换网址）

Cloudflare 快速隧道（`share_online.bat`）的网址**每次启动都会变**，而且官方声明这种
无账号隧道**没有可用性保证**。实测重启一次：

```
旧 https://nashville-...-ideal.trycloudflare.com   → HTTP 530
新 https://significantly-...-justin.trycloudflare.com → 200
```

要固定地址有三条路：

| 方案 | 固定地址 | 对方要装东西吗 | 你这边要装吗 | 花钱 |
| --- | --- | --- | --- | --- |
| 临时隧道 `share_online.bat` | ✗ 每次都变 | 不用 | 不用 | 免费 |
| **Tailscale Funnel** `fixed_online.bat` | ✓ `https://<机器名>.<tailnet>.ts.net` | **不用** | 装一次 Tailscale | **免费** |
| Cloudflare 命名隧道 | ✓ `album.你的域名.com` | 不用 | 不用 | 要买域名 |

**想要「固定地址 + 对方什么都不用装 + 不花钱」，答案就是 Tailscale Funnel。**
它包含在免费 Personal 计划里，只转发 443 / 8443 / 10000，前面挂 HTTPS。

```bat
fixed_online.bat <群号> 你的口令
```

> ⚠️ 新版 tailscale 改了 serve/funnel 的 CLI：旧写法 `tailscale funnel 443 on` 会报
> `the CLI for serve and funnel has changed`。现在的写法是把目标直接交给 funnel，
> 它同时负责 HTTPS 终止与转发（不用再单独跑 `tailscale serve`）：
> ```bat
> tailscale funnel --bg --https=443 http://127.0.0.1:8787
> tailscale funnel status
> tailscale funnel --https=443 off
> ```
> 需要先在 <https://login.tailscale.com/f/funnel> 为本节点启用一次 Funnel（强制）。

首次运行会：下载安装 Tailscale（弹 UAC 点「是」）→ 打开浏览器让你授权一次 →
开启 Funnel → 打印固定地址并记到 `tools\fixed_url.txt`。之后重启电脑也不变。

#### 本机自己打不开 Funnel 地址？（不是 Funnel 的问题）

如果你这台电脑装了 VPN / 代理客户端，很可能浏览器打不开自己的 Funnel 地址，
而**别人完全正常**。实测的根因是代理客户端的 PAC 无条件全走代理：

```js
function FindProxyForURL(url, host) {
  return "SOCKS5 127.0.0.1:7890; DIRECT;";   // 没有任何绕过规则
}
```

Funnel 的域名解析到 Tailscale 地址（`100.64.0.0/10` 里的 `100.x.y.z`）。
浏览器用 PAC 时**不会**回退到 DIRECT，于是把 `100.x.y.z` 交给代理 → 连接被关掉，
表现为 `ERR_CONNECTION_CLOSED`、`ERR_PROXY_CONNECTION_FAILED`。
而 Python / curl 直连 socket 不经 WinINET 代理，所以一直是通的 —— 很容易误判成
"Funnel 没配好"。

三种解法：

1. **只影响本机浏览器**：用 `open_fixed_url.bat`，或桌面上的「群相册备份（固定地址）」
   快捷方式。⚠️ 关键细节：**光加 `--no-proxy-server` 没用** —— Edge/Chrome 是单实例的，
   如果浏览器已经在运行，新开的"窗口"只是复用已有进程，**这个参数会被直接丢掉**，
   窗口仍然走 PAC（实测踩坑：本机有 23 个 msedge.exe 进程，加了参数照样打不开）。
   必须再给一个独立的 `--user-data-dir`，强制它另起一个进程，参数才会生效：
   ```bat
   msedge --user-data-dir="%LOCALAPPDATA%\qqalbum-browser" --no-proxy-server ^
          --new-window https://<你的地址>.ts.net
   ```
   （别用 `^` 续行接在 `start` 后面 —— 参数会被截断，这是另一个踩过的坑。）
2. **给 PAC 加绕过**：让代理客户端对 `100.64.0.0/10` 或 `*.ts.net` 返回 `DIRECT`。
3. **改 Windows 代理绕过**（PAC 与静态代理并存时有效）：
   `设置 → 网络和 Internet → 代理 → 手动设置代理 → 绕过列表` 里加 `100.*;*.ts.net`。

验证方法：浏览器打开 `edge://net-export/` 抓包，或干脆加 `--no-proxy-server`
再试一次 —— 如果能开，就一定是代理的问题，与 Funnel 无关。

常见报错对照：

* `HTTPS is disabled` → 去 <https://login.tailscale.com/admin/dns> 勾选 HTTPS Certificates
* `funnel not available for this node` → 去 <https://login.tailscale.com/admin/acls>
  确认没禁用 funnel 节点属性
* 地址能打开但 502 → 本机相册服务没在跑，先 `start_album.bat`
* 想停掉对外访问 → `tailscale funnel 443 off`

口令必须保留：Funnel 是**公开地址**，任何人拿到网址都会到登录页。

### 两种对外方式怎么选

* **Tailscale（更安全，推荐长期用）**：装完相当于把你的电脑和对方设备放进同一个
  虚拟局域网，**不暴露公网**，只有你们几台设备能连。双方都装一次、登录同一账号即可。
  代价是要装软件。
* **Cloudflare 快速隧道（最省事，适合临时给人看）**：
  `share_online.bat` 已经包好了，网址每次启动都变、关窗口即失效。
  代价是**真的挂在公网上**，口令是唯一防线 —— 请用长口令，看完就关掉。

**不建议**用路由器端口转发直接暴露 8787：这是 Python 标准库的 HTTP 服务，
没有 HTTPS、没有防重放、也没有对抗扫描的加固，直接挂公网风险很高。

## 9.8 让服务扛住崩溃：看门狗（别只靠开机自启）

**开机自启不等于守护。** 一次真实的翻车记录：

某次系统虚拟内存耗尽，相册服务进程被系统杀掉。开机自启任务在那天开机时**确实成功
拉起过服务**（`LastTaskResult=0`），但它是「登录时触发一次」，所以进程被杀之后
**再也没有任何人把它拉起来** —— 公网地址就那么一直死着，直到有人发现。

同一个故障还暴露出第二个问题：隧道任务用的是 `AtLogOn` 触发器，而任务是在当天
**登录之后**才创建的，于是它**从未运行过**（`LastTaskResult=267011`，
`LastRun=1999-11-30`，即"尚未运行"）。

**现在的机制**（三个计划任务）：

| 任务 | 触发 | 作用 |
| --- | --- | --- |
| `QQAlbumBackup-Watchdog` | 开机 + **每 2 分钟** | 检查服务与隧道，挂了就拉起来 |
| `QQAlbumBackup-NgrokTunnel` | 开机 + 登录 | 尽早建立隧道（看门狗兜底） |
| `QQAlbumBackup-WebUI` | 登录 | 启动相册服务（看门狗兜底） |

看门狗每轮都会检查 `/api/health`（本机与公网各一次），任一不通就重启对应进程，
并把结果写进 `tools\watchdog.log`。用 `S4U` 主体注册，所以**不需要有人登录**也会跑。

实测：手动杀掉服务进程后，看门狗下一轮即自动恢复，公网地址与口令鉴权都回到正常。

### 真正的根因：页面文件太小（不是物理内存不够）

日志里 01:33 有一条关键记录：

```
Application popup: Windows - Virtual Memory Minimum Too Low :
系统虚拟内存太低。Windows 正在增加虚拟内存分页文件的大小。
在这个过程中，一些应用程序的内存请求会被拒绝。
```

排查数字：

```
物理内存 31.8 GB
页面文件 D:\pagefile.sys  当前 476 MB  ← 自动管理被关掉了，只剩这么点
提交上限 37.8 GB           ← 只比物理内存高 6 GB
已提交   32.6 GB
提交可用  5.2 GB           ← 一点突发分配就会被拒绝
```

**修法**：把「自动管理页面文件大小」打开（`Win32_ComputerSystem.AutomaticManagedPagefile = True`），
**重启后生效**。改动记录在此以便回滚。

查看当前内存与提交状态：

```bat
python tools\report_commit.py
```

## 10. 常见问题

**Q：提示「浏览器尚未登录 QQ 空间」？**
在自动打开的 Edge 窗口里扫码登录一次即可；登录态保存在 `D:\QQAlbumBackup\edge-profile`，
之后无需重复登录。若窗口已被关闭，重新运行程序会自动拉起。

**Q：抓取动态时报「非该群成员，无法查看照片和进行操作」？**
这是服务端限流。已抓取的数据会保留，等几分钟后再次点击「开始抓取元数据」，
程序会从上次游标继续。

**Q：缩略图、封面显示不出来？**
先安装 Pillow（`pip install pillow`）。未安装时缩略图会回退到 QQ CDN 直链，
离线环境下就会空白；已下载的原图始终可正常显示。

**Q：想换浏览器/端口/数据库位置？**
用 `--browser` / `--port` / `--db` / `--root` 参数，见第 6 节。

**Q：AI 检索结果为什么只有一部分照片？**
索引是逐张建立的，界面侧栏会显示「已索引 x / y」。等进度到 100%，或者
在搜索时按时间/相册缩小范围即可。已索引的照片立刻可搜，不需要等全部完成。

**Q：AI 标签的百分比是什么意思？**
SigLIP2 训练时带一个学习到的偏置（约 -16），所以绝对 sigmoid 值很小且未校准。
界面显示的是**同一张图内部的相对值**（最强标签 = 100%），
数据库里同时保存原始余弦相似度 `score`，排序和筛选都用它。

**Q：视频能下吗？**
能，但它和照片走**两条完全不同的路**：相册网页接口不返回视频地址，视频地址只能问
QQ Agent 的 SnowLuma 引擎要（详见 §3.3）。所以下视频时 **QQ Agent 必须在运行**。
视频页可以按相册/已下载筛选、在线播放、另存为；地址会过期，程序在下载前会自动重取。

**Q：重启电脑后，QQ 机器人说「相册备份服务没启动」？**
服务不会自己开机启动。用 `start_album.bat` 手动开，或者确认计划任务在跑：

```powershell
Start-ScheduledTask -TaskName QQAlbumBackup-WebUI   # 立刻拉起来
Get-Content tools\autostart.log -Tail 10            # 看自启过程有没有报错
curl http://127.0.0.1:8787/api/health               # 确认起来了
```

如果日志里写 `port 8787 is taken by another program`，说明端口被别的程序占了 ——
先 `netstat -ano | findstr :8787` 找出占用者，关掉它或把技能设置里的服务地址改掉。
**不要把服务改到别的端口却不改技能设置**，那正是「服务在跑但机器人连不上」的经典原因。

**Q：AI 检索会把图片传到网上吗？**
不会。模型和向量都在本机，只有你自己上传的那张查询图会被送到本地
`127.0.0.1` 的服务进程做一次编码，不会离开这台机器。

**Q：图片描述（BLIP）和 AI 标签有什么区别？**
标签是从 94 个固定词里挑出的类别（适合筛选、浏览），描述的是一句自由句子
（适合找具体内容，比如「a girl with purple hair」「a bus next to a city」）。
两者互补：搜索时两路结果会合并，卡片上会标明是「语义」还是「描述匹配」。
BLIP 只输出英文，所以中文查询会先用 `D:\opus-mt-zh-en` 翻译成英文再匹配描述；
翻译模型缺失时退化为内置的中英关键词映射（约 150 个常用视觉词）。

**Q：生成 4.5 万条描述要多久？**
约 5 小时（CPU，2.5 张/秒）。这是纯后台任务：可以随时停止、明天继续，
全程 WebUI 都能正常用；也可以只对某个相册生成（挑小相册先试）。

**Q：原图下载占空间大吗？**
本项目实测该群 44,932 个文件共约 8.3 GB；
可按相册单独下载（在 WebUI 里先选中相册，再点下载）。

**Q：为什么有些照片只有很小的图（显示「仅缩略图」）？**
这些照片在 QQ 服务器端只有 16×16～32×32 的占位图，没有可下载的原图，
属于源数据缺失而非下载失败。程序保留占位图并单独计数，不会反复重试。

**Q：会把我的数据传到别处吗？**
不会。程序只与腾讯服务器通信，所有数据落在本机（默认 D 盘），WebUI 只监听 `127.0.0.1`。
