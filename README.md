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

## 账号、私人工作区与协作

默认和以前一样：本机打开就能用，局域网里的人也能读写。要分人用，就在页面上建第一个账号
（顶栏右上角「建第一个账号」，或「共享 / 账号」面板里的同一个按钮）。

**「跑服务的那台电脑」是按机器判断的，不是按回环判断的。** 在那台机器上打开
`http://127.0.0.1:8799/` 可以建，用**它自己的局域网地址**（`http://192.168.1.46:8799/`）
打开也一样可以 —— 用户常常就是从分享面板里点开局域网地址的，这里挡人没有意义。
判断依据是"对端 IP 是否属于本机网卡"：真·远程设备（手机、别人的电脑）拿到的
`can_setup` 仍是 `false`，页面会直说"你现在是从别的设备打开的"。

### 一个工作区 = 一个数据目录

私人工作区不是"同一张表加个 owner_id 过滤"，而是**物理隔离**：每个工作区有自己的
`workbench.sqlite3`、`media/`、`archives/`，连访客密钥库都是各自的。

```
vsw/data/                     ← 第一个账号（主人）的工作区，老数据原地留下
  workbench.sqlite3
  auth.sqlite3                ← 账号、工作区、成员、会话、邀请码（全局一份）
  ws/2-阿乙的工作区/           ← 后来新建的工作区
    workbench.sqlite3
    media/
```

切工作区只换 `X-VSW-Workspace` / `vsw_ws` cookie，服务端按工作区把请求绑到对应实例，
互不可见。协作 = 被加进同一个工作区，看到的是同一份数据。

**但语义模型是共用的一份。** SigLIP2 是 `VisionEngine.instance()` 的全进程单例，
工作区只各自持有自己的数据（向量存在各自的库里），模型文件只加载一次 ——
开十个工作区不会加载十份 1.5 GB 的权重，也不会多占十份内存。
所以"每人一个私人工作区"的代价只有数据目录，不含模型。

### 三种角色，谁都不能越界

| 角色 | 能做什么 |
| --- | --- |
| 主人 | 全部权限：管成员、发邀请码、改阈值、彻底删除、清空队列 |
| 可操作 | 搜索、导入、归档、移入回收站；不能改全局设置、不能彻底删 |
| 只看 | 只读，连移入回收站都不行 |

### 邀请码：注册不开放

`INV-XXXX-XXXX-XXXX`，一次性、带有效期（默认 3 天）、**不能直接发「主人」**——
先让别人以「可操作」进来，需要的话再在成员列表里升级。这样"邀请"不会变成"交出钥匙"。

### 访客密钥（降级保留）

没有账号、只想给朋友看一会儿的场景仍然可用：`VSW-XXXX-XXXX-XXXX`，分
主人 / 可操作 / 只看三种，库里只存加盐哈希，明文只在生成那一刻显示一次。
密钥是**工作区局部**的，而且访客密钥进不了账号接口（403）。

```bash
python -m vsw.server --root vsw/data --protect     # 上锁并生成第一把主人密钥
python -m vsw.server --root vsw/data --unprotect   # 关掉密钥闸门
```

### 手机与 APK 怎么带身份

手机端页面在 APK 里是 `file://`，浏览器不会替它带 cookie，所以登录后除了 cookie 还会存一份
令牌，走请求头 `X-VSW-Session`（同样认 `Authorization: Bearer`）。访客密钥走
`X-VSW-Key` 或链接上的 `?key=`——`<img>` 带不了请求头，分享链接是必要的退路。

## 不在同一个局域网的人怎么进来

局域网地址（`192.168.x.x`）只在一个网里能用。要让别的城市的人打开，有两种办法，
这个项目内置了第二种：

| 办法 | 要做什么 | 代价 |
| --- | --- | --- |
| 端口转发 | 路由器上把端口开出去 | 家宽大多在运营商 NAT 后面，根本没有公网 IPv4，多半转发不出去 |
| **Tailscale Funnel** | 装 Tailscale 登录，页面里点「打开公网通道」 | 免路由器、免公网 IP、证书由 Tailscale 边缘终止 |

```bat
python -m vsw.server --root vsw/data --host 0.0.0.0 --port 8799 --tunnel
```

启动日志会打印 `🌍 公网地址：https://<节点名>.ts.net/`。也可以在页面右上角
「共享 / 账号」→「不在同一个局域网 / 别的城市」里点一下开关（只有主人能点）。

三条要点：

1. **先有门槛才能开。** 没有账号也没有密钥时，服务端会拒绝开公网（`400` +
   「先建第一个账号（或 --protect 上锁）再开公网」）——否则谁拿到地址都能读写。
2. **经隧道进来的人算「外部设备」。** Funnel 回源时对端地址永远写成 `127.0.0.1`，
   真正的来源在 `X-Forwarded-For` 里，所以 `_is_local()` 先看这个头。否则整个互联网
   都会被当成"跑服务的那台电脑"，谁都能抢先建出主人账号。
3. **关掉它**：重启加 `--no-tunnel`，或在面板里点「关闭公网通道」。

## 快速开始

```bat
:: Windows：双击即可，会自动打开浏览器
start_vsw.bat
```

```bash
# 或手动
python -m vsw.server --root vsw/data --host 127.0.0.1 --port 8799

# 想给人用：绑 0.0.0.0 并上锁（会打印第一把主人密钥）
python -m vsw.server --root vsw/data --host 0.0.0.0 --port 8799 --protect
```

依赖：Python 3.10+、`Pillow`、`numpy`、`requests`；
自动识图另需 Node 18+ 与本机 Edge/Chrome；
语义层需要本机 SigLIP2 模型（没有也能跑，会自动退化成文件层 + 感知哈希层）。

首次运行会创建 `vsw/data/`（SQLite + 图片）。这个目录**不进版本库** —— 它是用出来的，不是写出来的。

手机端：

```bat
start_vsw_lan.bat        :: 绑 0.0.0.0，启动日志会打印局域网地址和二维码
```

## 手机端自动搜图（批量，编队交给电脑）

无人值守那条路必须靠电脑：手机里没有可编程驱动的无头浏览器（`input.click()` 在没有用户手势时
打不开文件选择器，见下一节）。所以批量自动搜图是**派活**，干活的是家里那台电脑：

```
手机队列页「自动搜图（N）」 → POST /api/auto/run → 服务端排班线程逐个 browser_search()
                                        ↓ 每个任务：CDP 开真浏览器 → 上传变体 → 等结果 → 提取 → 入库
手机轮询 GET /api/auto/state（2.5 秒一次）→ 跑完自动刷新队列与快照
```

要点：

* 只有声明了"能自动识图"的引擎会被挑走（实测库里是 `baidu` 和 `so360`），
  其余引擎留在原地、状态不变，面板上会写清楚有几个被跳过。
* 一次一批，服务端串行跑，任务之间停 1 秒；单个任务失败只记原因，不中断整批。
* 手机可以关屏走人 —— 状态在服务端，回来接着看进度。
* 真跑一次百度的实测：28.7 秒、新进 25 张、重复 5 张、失败 0。

## 手机自己搜图（半自动，一句话说清能做到哪）

我一开始说「手机跑不了无头浏览器，所以自动识图只能在电脑上」，这句**说错了一半**，
得分开讲：

* **能**：Android WebView 有 `WebChromeClient.onShowFileChooser()`，网页的
  `<input type=file>` 一被触发，原生就能把**已经选好的图**（`content://`）直接塞回去，
  用户连文件选择器都不用看。
* **不能**：浏览器规定文件选择器必须由**用户手势**触发，后台注入的 `input.click()`
  打不开它。所以「无人值守、一张接一张」这件事手机上做不到 —— 那条路还是电脑的活。

于是手机上做成了**半自动**：APK 里给每个队列项一个「手机搜」按钮 ——

```
点「手机搜」 → 原生把图（base64 → cache 目录 → content://）交给 WebSearchActivity
            → WebView 打开引擎页面 → 用户点一下「上传图片」（真手势）
            → onShowFileChooser 立刻把预选好的图还回去 → 引擎自己上传、自己跳结果页
            → 每 1.5 秒注入一次提取脚本（和电脑端 cdp_driver 用的是同一份）
            → 抓够了就把结果回传页面：识别结论 / 相似图 / 来源网页 / 商品价
```

要点：

* 提取脚本就是 `vsw/engines.py` 里那两份（`EXTRACT_BAIDU` / `EXTRACT_360`），原样搬进
  APK 里的页面，所以手机端不用连电脑也能解析结果；输入框的 URL 缺失时用
  `shouldInterceptRequest` 记 `/ajax/pcsimi`（替代电脑端的 `capture_network`）。
* 会先试着用**真实触摸事件**（`MotionEvent` + `dispatchTouchEvent`）点一下页面上的上传按钮；
  拿不到 user activation 就退化成「请你点一下」，并把这句话写在提示里 —— 不假装全自动。
* 手机这条只为「电脑不在手边」准备：批量、串行、关屏走人仍然是 `POST /api/auto/run`。

## 应用自我更新（电脑上放包，手机点一下）

APK 装好之后不用再找数据线：**电脑上换了新包，手机上「我的 → 检查更新」就能装。**

```
python scripts/vsw-publish-apk.py --notes "这一版改了什么"    # 打包脚本会自动调用它
        ↓  <root>/dist/vsw-workbench.apk + version.json（版本、大小、SHA256、说明）
GET /api/app/version?build=r17   →  { available, available_code, comparable, update_available, ... }
GET /api/app/apk                 →  安装包本身（Content-Type: application/vnd.android.package-archive）
```

* **两个来源**：先看这台电脑的 `data/dist/`，再兜底查 GitHub Releases（`--gh OWNER/REPO`，
  本地没包时才用它）。两边都取"版本号更大"的那个，界面上会写明来源。
* **版本号只有一个真相**：`vsw-android/java/com/vsw/workbench/MainActivity.java` 里的 `BUILD_TAG`，
  打包、发布、手机上报的当前版本都读它，不手写。
* **Android 侧**：`REQUEST_INSTALL_PACKAGES` 权限 + 自带 `ApkProvider`（`com.vsw.workbench.files`，
  只读、只允许单层文件名）。下载写 `.part` 边下边算 SHA-256，和 `version.json` 对不上就删包报错；
  对得上才改名、再弹系统安装框。Android 不允许应用自己静默安装，**那一下必须用户点**。

一条真机教训（r20）：**下载线程里不能 `Toast`**。`startInstall()` 原来就在后台线程里
`Toast.makeText(...)`，真机上直接抛 `Can't toast on a thread that has not called
Looper.prepare()` —— 安装器根本没被调起来，界面还把这句英文当成「更新失败」的原因显示给用户。
现在 `toast()` 自己回 UI 线程、`startInstall()` 整体 `runOnUiThread`，并且
「未授权 → 跳设置页 → 回来」会自动接着装，不用再点一次。
* `/api/app/version` 与 `/api/app/apk` 不需要登录 —— 版本号和二进制不是谁的私人数据，
  手机在登录之前就要能问"有没有新版本"。

## 三个用户报的问题（r17）：每个都追到了具体那行代码

| 现象 | 真正的根因 | 修法 |
| --- | --- | --- |
| 手机/平板打开电脑版页面，顶栏中文竖排、整页错乱 | `.topbar .tabs` 允许被压缩，`.tab` 又没有 `white-space: nowrap`，窄屏下每个 tab 被压到 40px 宽，逐字换行（顶栏高 815px） | tab 不换行；1100px 以下 tab 条独占一行、横向滚动（815 → 130px） |
| 队列条目右侧「自动识图 / 打开识图」整排不见了 | 条目网格中间格是 `1fr`，里面放的 base64 文件名没有空格，min-content 被撑到上千像素 | 改成 `minmax(0,1fr)` + `min-width: 0`，700px 以下按钮换行（条目高 2203 → 155px） |
| 手机上勾了归档批次，「合并查看」点了没反应 | CSS 写的是 `.btn[disabled]`（属性选择器），而 `el.disabled = true` 只设 DOM 属性、不写 HTML 特性 → 选择器匹配不到，按钮看着能点；勾到的又是空批次 | `.btn:disabled` 一起处理；按钮只按勾选数禁用；空批次明确提示「对象已经不在（被恢复归档或删除）」；「已归档」指标改读 `S.archived` |

## 手机上断联电脑也能导入和搜（r21）

用户报的原话：「断联电脑进行测试，手机图片上传之后，队列里的缩略图看不见，而且图片也不见了，
必须同步电脑才可以进行搜索」。这不是图丢了 —— 是**离线那一支压根没建条目**。

改之前：断开电脑时导入一张图，`importOne()` 只把 `data_url` 塞进 IndexedDB 的 `pending`
收件箱（状态写成「已存进离线收件箱」），本地既不建种子也不建队列项。所以队列页当然没有它，
缩略图当然没有，「手机搜」也拿不到图 —— 界面上看起来就是「图不见了」。

改之后（`vsw/static/mobile/index.html`）：

| 动作 | 现在的行为 |
| --- | --- |
| 离线导入一张图 | 就地建一条本地种子 + 一条本地队列项 + 两张本地图（320px 缩略图、原图） |
| id 怎么分 | 一律用**负数**（-1、-2…），存在 `meta.localSeq` 里，永远不会和电脑上的 id 撞 |
| 队列页 | 看得见缩略图，条目上标着「手机上」，`待搜索` 状态照常参与筛选 |
| 「手机搜」 | 直接用本地那张**原图**（不是缩略图）喂给 WebView，完全不需要电脑 |
| 删除 | 本地项离线也能删，连带把「等回连后推给电脑」那条 pending 一起清掉 |
| 回连电脑 | `flushPending()` 把它推给电脑进正式库，推成功后删掉本地影子记录，不会出现两条 |

顺带两个必须一起改的地方，否则「同步一次就没了」：

- `refreshQueueFromServer()` 原来 `local.clear('jobs')` 之后只塞服务端的行 —— 现在先把 `local: 1`
  的行留出来，写完服务端再补回去。
- `syncSnapshot()` 要清七个仓再灌快照 —— 同样把本地种子与队列项留出来重灌。

## 一个只在「库里有密钥」时才炸的启动 bug（r21）

`vsw/server.py` 启动日志里有一句 `print(f"[vsw] 🔒 已上锁：{n} 条有效密钥")`。Windows 控制台默认
GBK，`print` 直接抛：

```
UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f512' in position 6: illegal multibyte sequence
```

服务**还没开始监听就退出**了。它只在「数据目录里已经有访问密钥」时出现 —— 新建的空库不会触发，
所以很容易漏测（我是在做离线探针、复制真实数据起临时服务时才撞上，一撞就起不来）。

修法：`main()` 第一行调 `_relax_console()`，把 `sys.stdout/stderr` 改成 `encoding="utf-8",
errors="replace"`。编码不认识的字变成 `?`，但服务照常起来 —— 启动日志的重要性远低于服务本身。

## 点「扫码」就闪退（r22）

用户报：「扫二维码功能会闪退」，机型小米 9 / MIUI 12.5 / 安卓 11。

根因很直接，也很丢人：`QrScanActivity.java` 写了两百多行、编译进了 dex，**但
`AndroidManifest.xml` 里从来没声明过这个 Activity**。于是

```java
startActivityForResult(new Intent(this, QrScanActivity.class), REQ_SCAN);
```

抛 `ActivityNotFoundException`，没人接，进程直接死 —— 界面上看就是「点扫码，闪退」。

这类 bug 之所以能活到现在，是因为验证脚本只查了「类在不在 dex 里」，没查「清单里有没有声明」。
**dex 里有类 ≠ 系统能启动它。** 现在两件都查（`scripts/vsw-verify-apk.py` 从 38 项加到 42 项）。

顺手修的第二层问题在扫码页本身：它用 `takePicture` 的 JPEG 解码，而 `takePicture` 给的是
**照片**分辨率（不是预览分辨率）。小米 9 默认 4000×3000，解一张就是 48 MB 位图 + 48 MB 像素数组，
加上灰度缓冲，一百多 MB 瞬间要下来 —— MIUI 上 `OutOfMemoryError` 是随时的事，而
`catch (Exception)` **接不住 Error**。

所以这一版：

| 改法 | 作用 |
| --- | --- |
| `pickPictureSize()`：从 ≤200 万像素里挑最大的 | 相机不再回 12MP 的图 |
| `decode()` 先 `inJustDecodeBounds` 再按 `inSampleSize` 缩到 800px 内 | 不管相机给多大，进内存的都在 1M 像素内 |
| 解码线程 `catch (Throwable)` | OOM 不再拉崩应用，记一条日志继续扫下一帧 |
| 清单补 `<activity android:name=".QrScanActivity">`，锁竖屏 | 能启动了；预览旋转只按竖屏算，锁上更稳 |
| `openScanner()` 外面再包一层 try/catch | 万一还打不开，提示手填地址，不闪退 |

## 手机搜图的结果图为什么看不见（r23 / r24 先诊断再猜）

现象：手机上点「手机搜」，百度/360 的页面跑完了，但结果图一张都不显示。

**先说一次自己打脸的过程**：我第一反应是「防盗链 403」，还照着这个结论写了修复。后来拿库里真实的 549 条引擎图地址实测：

| 图片主机 | 不带头 | `Referer: graph.baidu.com` | `Referer: …/pcpage/index` |
| --- | --- | --- | --- |
| `mms0/1/2.baidu.com` | 200 | 200 | 200 |
| `p0.ssl.qhimgs1.com` | 200 | 200 | 200 |

**防盗链这个判断是错的** —— 两家图片 CDN 根本不在乎 Referer。所以「图全部消失」不是被拦，**更可能是我们压根没抓到图**（`similar` 为空），面板落到「没抓到相似图」那一句，于是既没有图、也没有统计行。这与用户反馈的「结果图没出来，统计行没写」完全吻合。

于是 r24 的做法变成：**先让程序自己说清楚断在哪一步**。

1. `WebSearchActivity` 在结果为空时，把提取脚本的 `diag` 读成一句话塞进 `note`：「没抓到相似图（诊断：pcsimi 请求 0 条、页面图片主机 mms0.baidu.com×12 …）」。
2. 面板标题带上当前版本（`手机搜 · 百度识图 · r24 · 队列 #21`），一眼能看出装的是哪版。
3. 抽图规则放宽：原来只认 `t7.baidu.com` / `mms*` 等白名单主机，手机上引擎换域名就变成 0 张；现在改成「够大的图（≥96px）就要」，只按文件名排掉 `logo/icon/avatar/sprite/textocr`。
4. 图真的画不出来时，两秒后数一次并明说「这 5 张图都加载不出来 / 其中 2 张没加载出来」。

内嵌那套机制仍然保留，因为它顺带解决了两件事：跨域加载不再依赖页面自己去请求远程图，以及每张图取没取到、取到几张都能写进统计行。

原来的写法：

```html
<img src="${esc(x.u)}">
```

`x.u` 是引擎的结果图地址（`https://t7.baidu.com/it/u=…`）。这种图只看 `Referer`：从引擎自己的页面发请求给图，从我们的页面（`file://` 或局域网地址）发请求就回 403。于是 `<img>` 全部加载失败，视觉上就是「图全没了」。在 WebView 里用 `fetch` 也不行 —— 跨域，而且同样没有对的 Referer。

修法：**让原生带着引擎页面的 Referer 把图取回来**，缩成 360px 的 JPEG 内嵌成 data URL：

| 位置 | 做法 |
| --- | --- |
| `WebSearchActivity.finishWithThumbs()` | 结果命中后不立刻返回，起 `vsw-thumb-fetch` 线程，取 `similar` 前 12 张 |
| `WebSearchActivity.fetchThumb()` | `HttpURLConnection` + `Referer: <引擎搜索页>` + 移动 UA；先 `inJustDecodeBounds` 再 `inSampleSize` 抽样解码，压到 360px / JPEG 72；单张上限 6 MB |
| 结果 JSON | 每张成功的加一个 `d` 字段（`data:image/jpeg;base64,…`） |
| 手机页 | `<img src="${esc(x.d \|\| x.u)}">`；没取到的那张加 `referrerpolicy="no-referrer"` 再试一次 |
| 面板文案 | 「相似图 5（其中 5 张已存成本地小图）」；一张都没取回来时如实说「结果图没能取回来（引擎拦了请求），可以点『复制图片链接』」 |

为什么放在原生那层做而不是页面自己抓：页面跑在 `file://` 里，跨域读响应体拿不到；`<img>` 也不可能自定义请求头。**能带任意请求头、还能逐张报状态码的只有原生那一层。**

现在的状态说清楚：r24 里这套诊断已经能定位「是没抓到图，还是抓到了但显示不出来」。真机截图一到，就能确定下一步该修哪边 —— 比继续猜强。

代价说清楚：多花几秒（12 张 × 最长 5 秒，实际通常 1–2 秒），结果 JSON 会变大（12 张小图约 200–400 KB），所以上限卡在 12 张、360px。

## 手机搜「抓到 8 张无关图」的两层原因（r25 / r26 / r27）

用户连报三次同一件事，最后一张截图给了铁证：面板上那行小字写着

```
相似图 8（其中 8 张已存成本地小图） · 图源：页面里捡的 · 来源网页 0
```

`图源：页面里捡的` 的意思是 **`pcsimi` 一条都没捕到** —— 也就是那次图片根本没传上去，
页面停在百度识图首页。而那 8 张（啤酒瓶、水花、花、人像、雪山）正好就是首页的样图。
所以我们一直不是在展示「搜索结果」，是在展示首页的推荐位。

三层的修法，一层一层剥：

| 层 | 症状 | 修法 |
| --- | --- | --- |
| 呈现（r25/r26） | 存进结果板后板里是空的 | 原来打的是 `/api/mobile/sync` 的 `imports`（建的是**种子**），结果板列的是 `findings`。新增 `POST /api/mobile/results` → `service.add_mobile_results()` → `ingest_payload(candidates=…)` |
| 判定（r27） | 面板把首页样图当结果，还给了「存进结果板」按钮 | `diag.source === 'dom'` 时直接判失败：写「这次没把图交给引擎…回到搜索页点一下「上传图片」」，清空图片列表、不给保存按钮 |
| 去重（r27） | 结果板里同一张啤酒图 6 条 | 引擎同一张图带多个尺寸地址（`~tplv-…`、`?x-oss-process=…`、`_500x500`）。`Service._image_key()` 归一地址（去查询串 + 去尺寸后缀）后去重，库里已有的地址直接算 `duplicates` |

**顺手纠正一句我自己写错的话。** r23 的文案说「引擎有防盗链，直接引用会全部裂开」。实测不成立：
从库里取 549 条真实引擎图地址（`mms0/1/2.baidu.com`、`p0.ssl.qhimgs1.com`），
用「无 Referer / `https://graph.baidu.com/` / `https://graph.baidu.com/pcpage/index?tpl_from=pc`」
三种方式请求，**全部 200**。所以「图看不见」跟防盗链无关，是压根没搜到东西。

### 为什么不能绕过网页直接调上传接口

试过：把图 multipart POST 到 `https://graph.baidu.com/upload?uptime=…`（带移动 UA、Origin、Referer），
返回

```json
{"status":1,"msg":"Reject","data":null}
```

所以在服务端替用户上传这条路走不通，只能靠 WebView 里真实的用户手势点那次上传
（`input.click()` 拿不到 user activation，这是 Chromium 的硬限制）。
当前实现是：自动算坐标点一次，点不动就明说「点一下页面上「上传图片」那一块，图已经替你选好了」。


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
`vsw-probe-import-queue.js`（11 项）、`-queue-filter.js`（14）、`-queue-thumbs.js`（7）、
`-trash-thumbs.js`（5）、`-archive-view.js`（20）、`-archive-merge.js`（40）、
`-archive-merge-pick.js`（15，勾选与空批次）、`-auto-search.js`（17，自动搜图按钮与进度）、
`-update-ui.js`（31，更新卡片、进度、失败、权限）、`-phone-search.js`（20，手机搜：桥接前不显示、
把图与提取脚本交给原生、结果面板、没图要提示）、`-offline.js`（25，**完全不给服务器地址**时的离线导入：建条目、缩略图看得见、
手机搜拿得到原图、刷新队列冲不掉、离线删除连带清 pending）。
`vsw-verify-apk.py` 现在 42 项（多了 4 项清单声明检查：扫码页 / 手机搜页 / Provider / 竖屏）。
服务端一旦开了账号，这些探针都要带 `--sid <会话令牌>` 跑（否则它们的接口调用全是 401）：

```bash
node scripts/vsw-probe-archive-view.js --apk vsw-workbench.apk      --origin http://127.0.0.1:8799 --sid "$(cat token.txt)"
```

权限与账号是服务端语义，各有独立探针（自建临时数据目录 + 临时密钥库，在随机端口上跑真服务）：

```bash
python scripts/vsw-probe-access.py      # 45 项：密钥、角色分级、限速、页面壳子免检
python scripts/vsw-probe-accounts.py    # 67 项：建号、邀请码、工作区隔离、协作、会话头、共用语义引擎、本机/远程判定
python scripts/vsw-probe-tunnel.py      # 35 项：公网通道解析、XFF 优先、只有主人能开、没门槛时拒绝开
python scripts/vsw-probe-auto.py       # 24 项：自动搜图的候选挑选、串行跑批、失败不中断、谁能开跑
python scripts/vsw-probe-update.py     # 43 项：版本比较、下载与 SHA256、匿名可取、GitHub 兜底、坏 version.json
```

### 加了身份之后，每条"绕开请求头"的通道都要过一遍

账号/密钥是请求头里的东西，但这几类通道**加不了请求头**，第一次上线就踩了两个：

* `<img src>` / 裸 `fetch()` 取图 —— 手机端 `ensureThumbs()` 一开始就是裸 `fetch`，
  开了账号之后 `/media/...` 全 401，代码里 `continue` 掉，于是缩略图一张都不补、
  控制台一行红都没有（用户看到的是「列表一片空白」）。改走统一加头的 `netFetch()`。
* `<a download href>` —— 归档 zip 靠它交给系统下载器，`<a>` 带不了头，同样 401。
  `withKey(url)` 改成"会话令牌优先、其次访客密钥"（`?sid=` / `?key=`，服务端两种都认）。

判断标准很简单：**只要有一处仍然在裸 `fetch` 或裸 `<img>`，开了账号之后它就会静默失效。**
探针里专门留了这几项回归（补图从 0 张变 228 张、zip 从 `status:401` 变合法 PK 头）。

## 仓库结构

```
vsw/                  桌面工作台 + 手机端页面（同一份手机页面被 APK 复用）
  imaging.py          导入解码、SHA-256、pHash/dHash/aHash、主色、16 种变体
  engine.py           本机 SigLIP2（768 维向量、零样本标签、文字检索）+ BLIP 描述
  store.py            SQLite：seeds/variants/jobs/sources/findings/clusters/ledger/settings
  engines.py          外部引擎定义（入口/选择器/提取脚本）、结果页解析、平台与可信度判定
  service.py          闭环：入库 → 变体 → 队列 → 检索 → 三层去重 → 聚类 → 排序 → 回流 → 图谱
  server.py           纯标准库 HTTP 服务：JSON API + 单页 UI + 媒体服务 + 手机端接口 + 身份闸门
  accounts.py         账号 / 工作区 / 成员 / 会话 / 邀请码（auth.sqlite3，PBKDF2 口令哈希）
  tunnel.py           公网通道（Tailscale Funnel）：状态解析、开/关、节点名
  hub.py              工作区句柄表：一个工作区 = 一个数据目录，按请求绑定
  access.py           访客密钥：加盐哈希、角色、失败限速
  release.py          应用自更新：本地 dist/ 与 GitHub Releases 两个源、版本比较、APK 直发
  static/             桌面端 index.html / app.css / app.js（无框架）
  static/mobile/      手机端 PWA（单文件内联全部代码）+ manifest + service worker
  cdp_driver.js       无头 Chromium + CDP 驱动：上传变体 → 等结果 → 提取结构化 JSON
  NOTES-engines.md    实测笔记：各引擎还能不能自动识图、Bing 为何失效、百度给的边界
vsw-android/          安卓 APK 工程（WebView 外壳 + 原生扫码 + 自更新，不需要 Gradle）
  java/.../ApkProvider.java   只读 ContentProvider：把 APK / 待搜图片以 content:// 交出去
  java/.../WebSearchActivity.java  手机自己搜：WebView + onShowFileChooser + 结果提取
scripts/              自测、真实链路验证、引擎探针、APK 验证、发布脚本（vsw-publish-apk.py）
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

## 手机搜到的图为什么只进得去 8 张、还全是无关图（r25）

三件事一起修：

1. **结果没有出口。** 面板只有「复制图片链接」，看完就没了 —— 结果板里当然不会有这些图。
   现在多一个「存进结果板（N）」：联网时走 `POST /api/mobile/sync` 的 `imports` 通道（服务端
   `apply_mobile_sync` → `import_bytes(kind="mobile-import")` 建种子），断网时先写进手机
   待同步队列，联网后自动推。
2. **图源看不出来。** 面板现在永远写一行「图源：引擎接口（pcsimi）」或「图源：页面里捡的
   （接口没捕到）」。前者是能反查来源页的正经结果，后者是 DOM 兜底，命中率低得多 ——
   一眼就能判断这次搜到底走通了没有。
3. **抽错了区域。** 上一版把尺寸门槛放到 96px，于是「猜你喜欢 / 热门推荐 / 页脚」里的
   大图全被当成结果。现在门槛回到 150px，并且往上找祖先容器，`recommend|hot|guess|footer|
   header|nav|sidebar|banner|ad|related` 命中的整块跳过。

`scripts/vsw-probe-phone-search.js` 加到 28 项（新增：按钮文案带张数、点完明确说存了几张、
断网时进待同步队列、待同步里带的是内嵌 data URL、面板写明图源）。

### r26：手机搜的图终于落在「结果板」里

上一次「存进结果板」打的是 `/api/mobile/sync` 的 `imports` —— 服务端那条路走
`import_bytes`，建的是**种子**（我拿去找图的输入）。结果板列的是 `findings`（搜回来的对象），
所以按钮点了、提示也弹了，结果板里还是空的。这是接口选错，不是显示问题。

现在新增 `POST /api/mobile/results`（`service.add_mobile_results`）：

* 入参 `{job_id, items: [{image_url, source_url, title}]}`，`job_id` 是这次搜索的队列项；
* 内部直接走 `ingest_payload(candidates=...)` —— 去重、来源、发散度、状态流转全部照旧；
* 优先用远程原图地址（服务端自己下，拿到原尺寸），取不到才退回手机内嵌的 data URL；
* 返回 `new_findings / duplicates / failures`，手机端据此说「已收进结果板 N 张（重复 M · 失败 K）」
  并直接跳到结果板。

顺手把「图源」从单独一行小字并进了统计行（`相似图 12（其中 12 张已存成本地小图） · 图源：引擎接口 · 来源网页 3`）
—— 上一版那行字太小，用户没看见。

`scripts/vsw-probe-mobile-results.py`（9 项，新增）：第一张被收下、findings 里真能查到、
识别结论当标题、来源页记下来、同一张再收一次不多一条、队列项变「待复查」、缺 job_id/空列表被拒、
坏图只记失败不塞空对象。

## 手机搜：为什么必须先点一下，以及怎么让这一下变容易（r31）

引擎的上传按钮只认**真实用户手势**。Chromium 的规则是：文件选择器必须由 user activation
触发。我们从原生 `dispatchTouchEvent` 打进去的合成触摸在 Xiaomi 15 / HyperOS 上拿不到激活，
真机证据就是诊断行永远写着 `pcsimi 请求 0 条` —— 页面停在首页，抓到的 8 张是首页样图。

所以 r31 不再假装能自动点：

| 做法 | 结果 |
| --- | --- |
| 原生派发合成 MotionEvent 点上传 | r28–r30 三次都失败，真机拿不到 user activation |
| 服务端直接 POST 上传接口 | `{"status":1,"msg":"Reject"}`，签名来自页面 JS，伪造不了 |
| **页面底部浮一个按钮，用户点它** | 这一次点击是真的手势 → 在同一个 click 里调 `input.click()` → `onShowFileChooser` 立刻把预选好的图交回去（用户看不到系统选图框） |

于是 `WebSearchActivity` 在 `onPageFinished` 里注入 `#vsw-cta`：
「点这里：把这张图交给识图」。点完变成「✓ 图已交给网页，等结果…」（`window.__vswOffered`
由 `onShowFileChooser` 回写）。找不到上传控件时就地提示「稍等一下再点我」。

这就是「没有电脑的创作者也能用」的完整代价：**一次点击**。不用选图、不用截图、不用传文件。

## 手机搜改成「手动抓图」（r32）

以前是「抓到 4 张以上就自动返回」，结果首屏那 8 张一出来就收工，往下滚出来的更多结果
根本没机会进仓。现在抓图窗口完全交给用户：

| 按钮 | 作用 |
| --- | --- |
| 顶栏「开始抓图」 | 之后每 1.5 秒把页面上的相似图按 URL 去重累计（最多 160 张） |
| 顶栏「结束抓图」 | 收工，把累计到的全部交回页面；累计 0 张时把诊断一起带回去 |
| 页底「点这里：把这张图交给识图」 | 真实手势上传，图已替你选好（r31） |

两条守卫：① 还没上传图之前不累计（那时页面上只有首页样图）② 20 分钟硬上限才会自动收工。
内嵌小图上限从 12 张提到 40 张，总字节仍卡在 320 KB（intent 会 `TransactionTooLarge`）。

## 结束抓图闪退 + 只能抓到首屏（r33/r34）

**闪退的真因**：结果原来走 `setResult()` 的 Intent extras，而 extras 要过一次 Binder
事务（1 MB 左右）。抓图累计 160 条 + 内嵌小图，parcel 开销一乘就抛
`TransactionTooLargeException` —— 表现正是「点结束抓图就闪退」。
修法：两个 Activity 同一进程，改用静态字段 `WebSearchActivity.takeResult()` 交接，
Intent 里只留一个 `"static"` 标记，`setResult` 外面再包一层 `try/catch` 兜底。
内嵌小图预算也从 40 张/320 KB 收到 24 张/224 KB。

**只能抓首屏**：引擎的结果是一屏一屏懒加载的，不滚页就只有首屏那 8 张。
现在抓图期间每次轮询都往下滚一屏（`VSW_SCROLL_JS`），滚到底就把「查看更多 / 加载更多 /
下一页」点一次；到底又没按钮就回到顶部再来一轮。累计按 URL 去重，上限 160 张。

## 抓图三处修正（r35）

用户实测反馈「少了很多，原本抓到 30 张」「深入更多相关图片的结果没抓到」「担心有人结束抓图后按关闭」：

| 问题 | 真因 | 修法 |
| --- | --- | --- |
| 张数变少 | 页面 `savePhoneResults` 只送 `slice(0, 12)` —— 抓到 30 张也只收 12 张 | 按 `u` 去重后最多送 60 张 |
| 相关图片/更多结果没抓到 | `accumulate()` 只读结果对象里的 `similar` 数组 | 遍历结果对象**所有**数组，元素带 `u` 就收（`related`/`products`/`more` 一并进） |
| 关掉就白抓 | 抓图中「关闭」直接退出 | 抓过图时弹确认「已经抓到 N 张，点关闭这些就交不回去了」，并可「仍然关闭」 |

另外开始抓图的提示里直接写了：⚠️ 别点「关闭」——关掉这一轮就白抓了。

## 「存进结果板（10）」与「重复 30」（r36）

用户抓到 30 张，面板却写「存进结果板（10）」—— 那个数字数的是**内嵌小图**张数
（受 224 KB 预算限制，30 张里只有 10 张带 data URL），不是能存多少张。
按下去之后服务端回「重复 30」：这批图**都已经在库里**（同一张参考图搜了第二遍）。

| 现象 | 真因 | 修法 |
| --- | --- | --- |
| 按钮写（10） | 用了 `shownEmbedded`（内嵌小图数） | 改用 `shownSimilar.length`（能存的张数） |
| 「已收进结果板 0 张（重复 30）」看着像失败 | toast 不区分「重复」和「失败」，0 张还标红 | 全重复时写「这 30 张都已经在库里了，没有新图」，且不标红 |

## 结果板「没有图」：图像在，只是缓存还没下完（r38）

服务端没问题：手机搜存进来的图，`rel_path` / `thumb_path` 都在盘上，`/media/...` 带身份取回来是 200。
问题在手机端的渲染顺序：

* 卡片原来**只用本地缓存**（IndexedDB 里的 `fthumb:<id>`）当 `<img src>`；
* 缓存是同步之后在后台一张张下的，下完之前卡片就只有灰底；
* 而结果板按发散度排序，新存进来的图 E 通常在 0.4 左右 —— 排在最下面，
  滚到那儿时缓存大概率还没轮到它。

r38 改成：**在线时直接引用电脑上的缩略图**（链接带 `?sid=` / `?key=`），本地缓存作为离线兜底；
存完结果板立刻能看见图，后台再把缓存补齐。

## 「存进结果板」点不动 —— 按钮在三千像素底下（r39）

面板（`.sheet`）是 `position:fixed;inset:0` 的全屏层，中间 `.sbody` 才是滚动区。
手机搜一次能抓到三十张，一张卡片按 100px 算，内容就有三千多像素长，
而当时的「存进结果板 / 复制链接 / 关闭」是写在内容**最后**的一行 `<div class="row">` ——
也就是说：不滚到底根本点不到，滚到底时它也贴着屏幕最下沿，很容易被系统手势区吃掉。
用户的原话是「这个按钮按不动」。

r39 把它挪进 `.sheet` 自己的固定底栏（`#sheetFoot`，`flex:0 0 auto`，不参与滚动），
并在关闭面板时清空；预览图只画前 12 张（其余照样全部收进去，只是不铺满屏幕）。
`openSheet()` 还会顺手收掉首次说明那层全屏遮罩 —— 它 z-index 70 高于面板的 60，
开着的时候面板上点任何东西都没反应，是同一个症状的另一个来源。

探针 `scripts/vsw-probe-phone-search.js` 从 34 项加到 39 项：按钮在底栏里、30 张时不滚动也能看见、
按钮中心点命中它自己（用 `document.elementFromPoint` 真的做命中测试）、预览最多 12 张、关面板清空底栏。

## 手机自己就是个相册（r40）

用户的原话：「为什么一定要连电脑，再同步才能看到结果板，即使我在本地没有 SigLIP 识别，
但是也有图片啊，可以不显示它的各项指标，就当作一个相册」。这句话是对的 —— 之前的实现
把「有图」和「能算指标」绑死在一起了：断网时手机搜到的图只进了一个看不见的待同步收件箱，
结果板是空的。

r40 起，手机上搜到/导入的图会以 `local:1` 的条目直接进「结果板」：

| 有 | 没有（也只能在电脑上算） |
| --- | --- |
| 图（缩略图 + 原图都存在手机 IndexedDB 里） | 发散度 E（SigLIP 语义向量 + 三层去重） |
| 标题、尺寸、找到时间、来源页 | 来源聚合、平台、标签、分类 |

卡片上写「手机上 · 本地相册 · 还没送进电脑」，指标位不显示 0.00 这种假数据 ——
算不出来就干脆不显示。点开详情会说明为什么没有指标，底部可以「在浏览器里打开来源」
或「从手机相册删掉」。连上电脑后（同步快照时）自动调 `/api/mobile/results` 把这批图送进去，
换成电脑算好的正式对象；送失败就留着，下次再送。

顺手修掉的一个真 bug：`makeThumb()` 返回的是 `{ blob, size }` 而不是 `Blob`，
把整个对象塞进 IndexedDB 之后，重新加载时取图会抛异常、结果板直接空白（探针里一眼就看出来了）。

探针 `scripts/vsw-probe-local-album.js`（**17 项**）全程不给服务器地址：喂三张 data URL 当手机搜的结果 →
点「存进结果板」→ 断言进了本地库（id 为负）、自动切到结果板、卡片上是 `blob:` 缩略图且能解码、
写着「手机上 · 本地相册」、**没有 E / 来源这些指标**、详情说明「要 SigLIP 和电脑」、
删得掉、重载之后还在。

## 深挖会挖到空页 / 手机关机也能攒图（r41）

用户在真机上提了两件事，都是「在手机上干活」这条线上的洞。

### 1. 深挖一层挖到「百度没有结果」之后退不回去

「深挖一层」会把前 N 张相似图逐个拿去做**按图搜**（`graph.baidu.com/details?...&image=<url>`）。
百度对一部分图就是没有结果，那一下就停在一片空白上，而原来的搜索结果页已经没了 —— 只能整个关掉重来。

修法：深挖时**压栈**。`vsw-android/java/com/vsw/workbench/WebSearchActivity.java` 新增
`private final ArrayDeque<String> backStack` 与顶栏按钮「上一层」（栈空时灰着）：

- `nextDepthImage()` 在 `web.loadUrl(...)` **之前**把当前地址压栈；
- `goBackOneLevel()` 弹出上一层、同时 `depthPhase = false`（先停自动换页，否则刚回去又被换走）、
  `goingBack = true` 挡住这 4 秒内的自动换页判断；
- 栈空时提示「已经在最上面这一层（搜索结果页）了」，不假装能退。

### 2. 手机离线的图，接上电脑后要能自己走过去

上一版（r40）把手机拍的/搜到的图放进「手机相册」，但那些图要在手机上重跑一次「手机搜」才会进电脑。
现在的做法是**谁有图谁说话**：手机相册里攒的图，一旦连上电脑就自动推过去，由电脑补算指标。

- 手机端 `flushLocalFindings()`：本地 `local:1` 的条目按 60 张一批 `POST /api/mobile/results`，
  推成功就把本地那份删掉（图和缩略图一起清）；`checkOnline()` 连上时触发，且 3 分钟内最多试一次。
- 服务端 `service.add_mobile_results()`：`job_id` 缺失（手机相册的条目本来就没有队列项）或那个队列项已经不存在时，
  调 `_album_job()` **现造一条**「手机相册（手机推回）」种子 + 队列项，再把图挂上去。
  第一张图既当种子本身、又作为候选回填一次，所以它会算成 1 条重复 —— 这是对的，
  剩下的图才是「新对象」。
- `data:` 开头的内嵌图（断网时只能拿到内嵌副本）走本地 `base64.b64decode`，不再经过 `requests`
  （它没有 `data:` 的适配器，直接抛 `InvalidSchema`）。

验证：`scripts/vsw-probe-mobile-results.py` 13 项、`scripts/vsw-probe-local-album.js` 17 项、
`scripts/vsw-probe-offline.js` 25 项、`scripts/vsw-verify-apk.py` 42 项全绿。

> 踩坑记录：探针里用**纯色图**做相册用例，结果两张「不同颜色」的图被算成重复 ——
> 纯色图的 pHash 完全一样。相册用例必须画图案（`shape(rgb, kind)`），否则测的是自己写错的假设。
