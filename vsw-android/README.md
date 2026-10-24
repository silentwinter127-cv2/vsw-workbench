# 视觉搜索工作台 · 安卓版（APK）

把 [vsw/static/mobile](../vsw/static/mobile) 里的手机端 WebUI 包成一个可以**直接发给别人**的 APK。

## 产物

仓库里**只有源码**（`*.apk` 已加入 `.gitignore`）。自己构建一次即可得到：

| 文件 | 说明 |
| --- | --- |
| `vsw-workbench.apk` | 可直接安装的 release 包（约 602 KB，用你自己的密钥库自签；当前 r16 为 620,589 字节） |
| `vsw-workbench-debug.apk` | 带调试签名的版本（`build_apk.bat` 不带参数时的产物） |

包名 `com.vsw.workbench` · 应用名「视觉搜索工作台」· minSdk 24（Android 7.0+）· targetSdk 34。

> **给别人装的时候要先说一句**：这是自签名包，Android 会提示"未知来源"，需要对方在
> 设置里允许安装未知应用。想上应用商店得注册开发者账号 + 用商店要求的签名方式重新签，
> 而且商店通常要 AAB 而不是 APK。

## 怎么构建

不需要 Gradle、不需要 Android Studio、不需要 npm。用手工链：

```
aapt2 compile → aapt2 link → javac → d8 → 打包 → zipalign → apksigner
```

```bat
cd vsw-android
build_apk.bat            :: debug 包
build_apk.bat release    :: release 包（自动生成密钥库并签名）
```

首次构建前需要一套工具链（本机已经装好，放在 `C:\vsw-build`，不占项目目录）：

| 组件 | 版本 | 大小 | 说明 |
| --- | --- | --- | --- |
| JDK | Temurin 17.0.20.1 | 182 MB | `C:\vsw-build\jdk17`，路径记在 `C:\vsw-build\java-home.txt` |
| Android SDK cmdline-tools | 11076708 | 147 MB | 提供 `apkanalyzer` / `sdkmanager` |
| platform-tools | 34.0.0 | — | `adb` |
| platforms;android-34 | — | — | 编译用的 `android.jar` |
| build-tools;34.0.0 | 2.19 | 394 MB（含平台） | `aapt2` / `d8` / `zipalign` / `apksigner` |
| ZXing core | 3.5.3 | 593 KB | 唯一的第三方依赖，纯 Java 的二维码解码 |

重建整个工具链：见本文件末尾「从头装一遍」。

## 关键取舍（都是被"要发给别人"逼出来的）

1. **不硬编码任何服务器地址。** 应用装完是空的，第一次打开会弹引导页，让用户
   填自己电脑的地址、扫码，或导入一份数据集。写死我的地址，对方装完就是一片空白。
2. **不引 androidx / Gradle。** 手工链少一层依赖，APK 小（354 KB），
   也不会因为 Gradle 插件版本、网络代理、依赖解析失败而卡住。
   代价：`-bootclasspath android.jar` 下不能用 lambda（缺 `LambdaMetafactory`），
   Java 代码里全部用匿名内部类。
3. **不需要 FileProvider。** WebView 的 `onShowFileChooser` 会把相机/相册返回的
   `content URI` 交给页面读取，Java 侧不碰文件，所以省掉 `androidx.core`。
4. **权限按需申请。** 相机权限只在真的点「拍照」或「扫码」时才弹，
   不给定位/通讯录，`READ_EXTERNAL_STORAGE` 限到 API 32 以下。
5. **允许明文 HTTP。** 工作台跑在局域网里只有 HTTP，`usesCleartextTraffic="true"`。
6. **扫码自己写。** 用 ZXing core + `android.hardware.Camera`，没引 Play Services
   （那套的依赖链不透明，而且很多国内设备没有 GMS）。
7. **状态栏手势返回。** `onBackPressed` 先问页面 `window.vswHandleBack()`，
   关掉全屏面板/引导页之后才退出应用。

## 验证（以及验证不到的部分）

这台机器**没有可用的真机调试环境**：没有通过 adb 连接的设备、CPU 虚拟化被 Hyper-V 占着
开不了模拟器、装系统镜像又要额外 1–2 GB 而 C 盘只剩 3 GB。所以本机能做的是下面这四层
自动化验证 + **真机截图反馈**（三轮真机反馈各抓出一个只有设备上才暴露的 bug，见后文）。
人工在设备上点一遍仍然比这些探针更可靠 —— 别把自动化当成真机验证的替代品。

```bash
# 1) 交付面：结构 / 身份 / 权限 / 签名 / 三条读取路径 / 类是否真进了 dex（38 项）
python ../scripts/vsw-verify-apk.py

# 2) 行为面：从 APK 里解出内置页面，用同内核的浏览器跑起来验（13 项）
node ../scripts/vsw-probe-apk-page.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799

# 3) 导入队列行为：真造 3 张图走一遍「逐张导入 + 全部导入」（11 项）
node ../scripts/vsw-probe-import-queue.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799

# 4) 回收站图片：故意删掉本地缩略图，看会不会自动补回（5 项）
node ../scripts/vsw-probe-trash-thumbs.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799

# 5) 队列缩略图：变体图/种子图、键不撞车、缺图自动补（7 项）
node ../scripts/vsw-probe-queue-thumbs.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799

# 6) 队列筛选与删除：自造测试数据，验完删掉（14 项）
node ../scripts/vsw-probe-queue-filter.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799

# 7) 归档查看与归档文件夹 + 批次计数现算（20 项）
node ../scripts/vsw-probe-archive-view.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799

# 8) 合并归档相册：跨批次去重/排序/打包 + 手机与桌面两处入口（40 项）
node ../scripts/vsw-probe-archive-merge.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799

# 9) 访客密钥：上锁、角色分级、失败限速、页面壳子免检（45 项）
python ../scripts/vsw-probe-access.py

# 10) 账号 / 私人工作区 / 协作：建号、邀请码、隔离、会话头、共用语义引擎、本机/远程判定（65 项）
# 11) 公网通道：Funnel 状态解析、XFF 优先、只有主人能开、没门槛时拒绝开（35 项）
# 12) 自动搜图：候选挑选、串行跑批、失败不中断、谁能开跑（24 项）
# 13) 应用自更新：版本比较、下载与 SHA256、匿名可取、GitHub 兜底（43 项）
# 14) 更新卡片：进度/失败/权限/已最新/问不到版本（31 项）
# 15) 手机自己搜：桥接前不显示按钮、把图与提取脚本交给原生、结果面板、没图要提示（20 项）
python ../scripts/vsw-probe-accounts.py

# 0) 语法闸门：内联脚本一处语法错，整页就不能点（比行为探针便宜得多，先跑它）
python ../scripts/vsw-check-syntax.py
```

| 层 | 查什么 | 结果 |
| --- | --- | --- |
| 结构 | 清单/dex/资源表/内置网页/图标/签名文件；dex 555 KB（含 ZXing） | 通过 |
| 身份 | 包名、启动 Activity、minSdk 24、targetSdk 34、4 条权限、无多余权限 | 通过 |
| 签名 | `apksigner verify` 通过，v1+v2 都签；证书 `CN=Visual Search Workbench` | 通过 |
| 代码 | `apkanalyzer` 解析 dex，`MainActivity`/`QrScanActivity`/`GraySource`/`Bridge` + ZXing 解码类都在 | 通过 |
| 行为 | `file://` 页面能 fetch `http://`、XHR 也通、IndexedDB 可读写、9 个对象仓都建好、快照能落盘、首次引导会弹出、无 JS 异常 | 通过 |

其中两个**必须实测**的点，探针给了确定答案（这也是"内置页面能不能直接干活"的前提）：

- `file://` 页面 **可以** fetch/XHR 到 `http://<局域网IP>`（200，读到真实工作台数据）；
- `file://` 源 **可以** 建库并读写 IndexedDB。

所以 APK 不需要 service worker，也不需要把页面挂到 HTTP 上才能联网。

### 真机踩到的坑 1：内置页面读不出来（已修）

第一版装到小米（Android 15 / WebView 131）上打开是 `net::ERR_FILE_NOT_FOUND`。
后来加了启动诊断屏，拿到确切原因：

```
资产 assets/mobile/index.html → 0 字节
失败：读取内置页面失败：FileNotFoundException mobile/index.html
```

**条目明明在 APK 里**（电脑上解包能看到 81,445 字节、而且是 STORED），但
`getAssets().open("mobile/index.html")` 在真机上抛 `FileNotFoundException`。
进一步的证据是 `resources.arsc` 里**查不到任何 assets 路径记录** ——
也就是"条目在包里，但 AssetManager 按名字索引不到它"。

修法是**三级兜底**，三条都失败才显示诊断页，并把每条的错误写在屏幕上：

| 顺序 | 路径 | 说明 |
| --- | --- | --- |
| 1 | `getAssets().open()` | 正常路径 |
| 2 | **ZipFile 直接读 APK** | 把 APK 当 zip，按 `assets/mobile/index.html` 抠出来。不经过 AssetManager 的名字索引，所以"索引有洞"也照样能拿到内容 |
| 3 | `res/raw/page.html` | 资源表索引的资源，aapt2 一定会为它建索引 |

构建脚本现在把页面**同时打进** `assets/` 与 `res/raw/`，启动屏会写明这次实际走的是哪条路
（`来源 assets 索引` / `APK zip 直读` / `res/raw`）。应用名也带上构建号（视觉工作台 r3），
避免"我装了新版"其实还是旧包这种误判。

`scripts/vsw-verify-apk.py` 补了对应断言：兜底资源必须在包里、两份内容必须一致、
资源表里必须查得到条目名 —— 这个坑不能再靠人眼发现（现在 38 项）。

### 真机踩到的坑 2：导入图片后面板被关掉、其余图片不会被导入（已修）

真机反馈：在「导入图片」面板点一张图的「直接导入」，**整个面板被关掉**、人被弹回上一页，
而且选中的其它图片再也不会被导入。两个原因都在 `importOne()` 里：

1. 导入成功后直接 `closeSheet()` —— 把面板整个关掉了；
2. 面板一关，列表里剩下的文件就只是"没人点的按钮"，没有队列驱动它们。

修法：导入做成**队列**（`IMPORT_QUEUE`），每张图有独立状态
（`idle / sending / done / queued / failed`），面板**不主动关闭**：

- 逐张导入：点一张只处理那一张，其余照旧待处理，面板保持打开；
- **全部导入**：一次把剩下的做完（串行，避免并发几十个请求）；
- 每张卡片显示自己的结果（`✓ 已建成种子 #12` / `✗ 原因`），失败可单独重试；
- 离线时逐张进「离线收件箱」，同样标成已处理，不留在待处理里混淆；
- 从导入面板进「先框选再导入」的，框完**回到导入面板**继续处理剩下的。

探针 `scripts/vsw-probe-import-queue.js` 真造 3 张图、真走一遍"逐张导入 → 全部导入"，
断言面板仍打开、每张状态独立、工作台里确实多出这 3 个种子。

### 真机踩到的坑 3：回收站图片全是空白、每张都写「0 源」（已修）

服务端数据是对的（实测 206 条回收站条目**全部**带 `thumb_path` 与 `source_count:2`），
所以是手机端的问题，两个独立原因：

1. **「0 源」**：手机端列表里写的是 `f.sources.length`，而快照为了省体积**默认不带来源明细**
   （只带 `source_count`）—— 于是恒等于 0。改用 `sourceCount()`：有明细数明细，没有就用
   `source_count`。
2. **图片空白**：缩略图是"同步时按需下载"的，而**回收站里的条目往往是之后才被清掉的**
   —— 上一次同步时它们还不是回收站条目，图就没下过。

修法是加 `ensureThumbs()`：数据进内存后（`loadLocal()` —— 启动、同步完成、导入数据集都会走它）
检查本地库里哪些条目**连 Blob 都没有**，缺的当场从电脑补下来，然后只把 `<img>` 补上、
不重画整棵树（避免滚动位置丢失）。原来的实现把补图挂在"切到归档页"这个时机上，太脆。

两个判断都只认本地 Blob，**不看内存里的 URL 映射，也不看 `S.online`** —— 踩过的坑：
`S.online` 是异步检查的结果，可能还没落定（探测到过"快照明明拉下来了但 `S.online` 仍是
false，结果一张图都没补"）。另外回收站缩略图的键原来用 `trash:` 前缀绕了一层，也一并统一了。

探针 `scripts/vsw-probe-trash-thumbs.js` 复现这条路径：拉快照 → **故意把本地 206 张缩略图
全删掉** → 渲染回收站 → 断言自动补回、`<img>` 上是真实图片数据（`blob:` 且
`naturalWidth > 0`）、「N 源」不再是 0。

### 真机踩到的坑 4：队列页缩略图空白 / 显示成别的图（已修）

真机反馈队列页有的条目有图、有的空白，而且"看着有图"的那几张其实不是该条目对应的变体图。

查到三个原因，前两个都是设计缺陷：

1. **变体没有缩略图**：变体表只有 `rel_path`（原尺寸图），没有缩略图列；快照的 `thumbs`
   档又把变体唯一的 `rel_path` 清空了 —— 队列根本拿不到"这一条派发的是哪个变体"的图。
   修法：服务端新增 `GET /api/variant/{id}/thumb`（按需生成 + 磁盘缓存，
   `thumbs/variants/v{id}-{box}.jpg`），快照里给每个变体带上 `thumb` 字段；
   同步时只下载**队列里真正用到**的变体图。
2. **本地图片键撞车**：结果 / 回收站 / 变体三张表 id 各成一套，却有大量重号
   （实测结果↔变体重叠 **39** 个、回收站↔变体重叠 **99** 个）。原来键写作 `thumb:{id}`，
   于是"变体 5 的图"和"结果 5 的图"是同一个键 —— 队列会显示成**别的图**或空白。
   修法：键加命名空间 `fthumb: / ffull: / tthumb: / sthumb: / vthumb:`，
   IndexedDB 升到 v2 并在升级时清空 `assets`（旧键有歧义，可能已存成错图，重下更可靠）。
3. **`tx()` 在 `get` 未命中时返回了 IDBRequest 对象**（最隐蔽的一个）：
   原实现只在 `transaction.oncomplete` 里取值，而 `get` 未命中时 `out.result` 是 `undefined`，
   于是 `resolve(out)` 把 **请求对象本身** 当结果返回 —— 它是 truthy。
   后果是**所有 `if (await local.get(...))` 恒为真**：图片缓存永远认为"已经存过了"，
   一张都不会下载。这也解释了之前回收站探针里"明明清空了却显示已存在"的怪现象。
   修法：单个请求按它自己的 `onsuccess` 取 `result`（未命中就是 `undefined`）。

队列项的取图顺序改为：**变体自己的小图 → 种子缩略图 → 占位**；缺图的在渲染后自动补
（`ensureJobThumbs`，只补当前显示的那些，几百条不必全下）。

探针 `scripts/vsw-probe-queue-thumbs.js`（7 项，随数据增减）验：同步能拉到变体图、绝大多数队列项有图、
图是真实数据（`blob:` 且 `naturalWidth > 0`）、有变体的项优先用变体图、
**同 id 的结果图与变体图能共存且内容不同**（撞车 bug 的回归测试）、
清掉本地图后能自动补回。

### 手机端补齐：队列筛选与删除（真机反馈"操作起来麻烦"）

原来手机端队列只有状态栏和引擎筛选，也没有删除入口 —— 队列几百条时很难用。现在：

- **筛选**：状态 + 种子（下拉，带「已产出/总数」）+ 引擎芯片 + 变体类型芯片 + 关键词 + 产出
  （有产出/无产出）；芯片都带基数，摘要写明叠加了哪些条件、筛出多少条；
- **删除**：卡片上的「删除」（单条）、顶部「删除筛选结果（N）」（当前筛选那批）、
  「选择」进多选后「删除选中」/「全选本页」（也可长按卡片进入多选）；
- 删除只删队列记录 —— 已产出的结果和种子都不动；离线时按钮会明确提示需要连上电脑；
- 删完用 `/api/queue` 重拉队列写回本地库，界面与电脑一致，不留幽灵条目。

服务端新增 `POST /api/queue/delete`（`{job_ids}` 批量删，或 `{statuses}` 按状态清空）
和 `store.delete_jobs()`（分批拼占位符，避开 SQLite 变量数上限）。

#### 顺带修掉的幽灵行 bug

做这个功能时发现：**已归档的对象被移进回收站后会彻底消失** —— `findings(trashed=True)`
和 `count_findings(trashed=True)` 默认又叠了 `archived_at=0` 的过滤，于是
`trashed=1 且 archived_at>0` 的行既不在归档列表、也不在回收站里，看不见、也清理不掉。
归档与回收站本来是正交状态，现在回收站查询不再叠加归档过滤；恢复后对象回到归档视图。

探针 `scripts/vsw-probe-queue-filter.js`（14 项）：为了不碰真实数据，它**先自己造几条测试
队列项**再删掉；验筛选逐项生效且可叠加、删除删的正是选中那批、电脑端与本地库都同步、
删除不影响种子；还会从归档对象里借样本造回收站数据并在结束时还原。

### 手机端导入"没进队列"（已修）

三个原因叠在一起：

1. **只有新种子才入队** —— `import_bytes` 的入队写在 `if created:` 里，导入一张已存在的图
   （像素哈希命中种子池）就静默跳过。现在重复导入也入队，`store.enqueue` 幂等复用已有任务；
   任务被删掉后再导一次也能回到队列。
2. **导入后没刷新队列** —— 手机端不知道新任务已经生成。现在每张导入后静默重拉队列，
   提示里写「入队 N 条」；批量导入结束汇总一次。
3. **快照队列上限太小** —— 快照用同一个 `limit` 截断结果与队列，而队列条数通常是结果的几倍
   （一次导入 × 16 个变体），新任务按 id 升序排最后就被截掉了。现在队列单独用
   `max(1200, limit*4)` 且同状态**最新在前**；手动刷新也在客户端按同样规则排序
   （桌面保持 id 升序的 FIFO 语义）。

顺带：导入面板加了「入队变体」选项 —— 默认**仅全图（每张 1 条）**，需要全套变体时切
「全部变体（每张约 16 条）」，选择会被记住。

### 手机端「按引擎清理」预设

队列页顶部会出现 `清 so360 待处理 84` 这样的预设按钮：一次删掉该引擎的全部待处理任务
（典型场景：某引擎长期被验证码挡着，那批待搜索只是噪声）。只列确实有待处理的引擎并显示条数；
确认框会说明只删队列记录，结果与种子不受影响。

### 手机端归档打不开（已修）+ 归档文件夹

真机反馈：手机上归档只有一个「取消归档」，**根本打不开批次**看里面有什么。

- 归档对象现在也进快照（`archived` 仓，IndexedDB 升到 v3），所以**离线也能翻归档**；
- 批次卡上两个按钮：
  - 「查看（N）」列出该批对象（缩略图 + 标题 + 来源数），点一条看详情；
  - 「归档文件夹」让**电脑**生成并弹出该目录（手机自己打不开电脑目录），并显示路径；
    离线时明确提示需要连上电脑。
- 归档图**不在启动时补**：归档对象可能上百个，启动时全补会把补图队列堵死
  （实测拖到回收站那几张都补不出来）。改成点开某个批次时按需补。

服务端配套：每批落盘 `data/archives/<id>-<名字>/`（`imgs/` 用**硬链接**，同一卷不占空间；
`manifest.json` 是可读清单）。接口 `GET /api/archive/{id}/paths?export=1` 与
`POST /api/reveal {path}` —— 后者只允许数据目录以内的路径。

桌面端同样加了「归档文件夹」按钮。

### 手机上"看不到归档文件夹"（已修）

真机反馈点「归档文件夹」手机上什么都没发生。两个原因：

1. 那个按钮只是让**电脑**弹资源管理器 —— 手机本来就看不到电脑目录；
2. **APK 里没有设 `DownloadListener`** —— WebView 里任何下载链接点下去都是静默无效。

现在归档卡片三个按钮：**查看（N）**（手机本地，离线可看）、**保存到手机**（打包 zip 下载到
手机「下载」）、**在电脑上打开**（让电脑弹窗，路径标注为"电脑上的文件夹"）。

APK 侧：
- `setDownloadListener` + 系统 `DownloadManager`，存到 `Downloads/`，完成时通知；
- `guessFileName()` 优先解析 `filename*=UTF-8''…`（否则中文批次名会变成 `archive-15.zip`），
  并清掉文件系统不允许的字符。

服务端顺带修的：
- 新增 `HEAD`（以前一律 501，"先探大小"的客户端会直接失败）；
- `Access-Control-Expose-Headers` 暴露 `Content-Length` / `Content-Disposition` 给跨源页面；
- zip 文件名用 RFC 5987 编码（HTTP 头只能是 latin-1，直接写中文会让发头时崩掉）。

### 合并归档相册（r11 / r12 修计数）

用户要的是「把几个归档批次合成一个相册看」。这是**视图合并**：不改数据库里的批次归属
（批次记的是"当时为什么留下它"），只是把几批的图放到一起，跨批次按 id 去重、按发散度降序。

- 服务端：`GET /api/archive/merge?ids=1,2,3`、`GET /api/archive/merge/zip?ids=…`、
  `POST /api/archive/merge/export`；`export_collection()` 统一负责落盘（图片硬链接 +
  `manifest.json`），单批次与合并共用；
- 手机端：归档卡片加勾选框，勾两批以上后合并条出现「合并查看（N）」「合并保存 zip（N）」
  「清除勾选」；合并查看出的面板就地按需补缩略图（不阻塞启动）；
- 桌面端：归档表加勾选列与合并条（查看 / 导出文件夹 / 保存 zip），全选与半选状态同步。

顺手修的两个真 bug（都是"看起来好了其实没好"）：

1. **字面路由被通配路由抢走**：`/api/archive/*/zip` 先注册，把 `merge` 当成批次 id 去
   `int()` → 400 `invalid literal for int() with base 10: 'merge'`。
   改成两趟匹配：先跑不带 `*` 的字面路由，再跑通配路由。
2. **导出目录与查路径目录不一致**：导出写到 `archives/24`、`archive_paths()` 读
   `archives/24-<名字>`，于是"文件夹生成了但清单是空的"。改成单批导出也用
   `archive_dir()` 的命名（离线自测因此从 275 项里挑出 1 项失败，就是它）。

`scripts/vsw-probe-archive-merge.js`（40 项）把这条链路整个验一遍：服务端去重/排序/计数自洽、
zip 里能解出 manifest 且记着合并了哪些批次、单批 zip 与 HEAD 回归、手机端勾选→合并查看→
保存 zip 的地址、桌面端合并板条数与服务端一致。

### 归档表的数字改成现算（r12）

真机/桌面截图里，归档表明明「对象 11」，同一行却写着「0 条来源记录」，来源列与平台列也都是 0。

根因是 `archive_runs` 存的是**归档那一刻的快照**，而早期版本算 `preview` 时没带来源
（`store.finding()` 单条查询，不 attach `sources`），于是把 `sources=0, platforms=0` 写进了库。
归档之后又在回收站里搬来搬去，快照里的数只会越来越离谱。

改法（`store.archive_live_counts()` + `service.archives()`）：

- 归档表的 对象/来源/平台 **按当前成员对象重算**，不再显示快照；快照原值保留在
  `findings_snapshot` / `sources_snapshot` / `platforms_snapshot` 里备查；
- 另给 `findings_all` 与 `trashed`，界面上写成「对象 5（+6 回收站）」；
- 批次记录还在、但成员对象已经不在的（被恢复归档或删除）标成**空批次**，
  数字归零而不是继续骗人；早期自动生成的备注文案也按现算重写（用户自己写的备注不动）；
- 手机端快照的 `archives` 走同一个函数，所以手机上的批次卡数字也一起对了。

统计 SQL 有个坑：`LEFT JOIN sources` 会把一行对象乘成 N 行，用 `COUNT(*)` 数出来的是
"对象×来源数"。必须 `COUNT(DISTINCT f.id)`（在线数据里这个错让 #24 从 11 个对象变成 22 个）。

`scripts/vsw-probe-archive-view.js` 加了 2 项回归：批次卡上的来源数必须等于详情里逐条来源之和；
空批次的数字必须归零且卡上有「空批次」字样（现在是 20 项）。

### 手机端登录与访客密钥（r13）

APK 里的页面是 `file://`，浏览器不会替它带 cookie —— 所以身份有两种携带方式：

1. **账号登录**：登录成功后把令牌存本地（`vsw.sid`），之后每个请求带 `X-VSW-Session`；
2. **访客密钥**：`X-VSW-Key`（或链接上的 `?key=`，一个照面就换成 cookie）。

「我的」页最上面是账号卡：

- 没登录时：名字 + 密码 + 登录、用邀请码注册、访客密钥三条路；
- 登录后：显示「谁 · 现在在哪个工作区（什么角色）」、工作区下拉切换、退出登录；
- 退出登录会**清空本地库**（数据属于上一个工作区，留着就是串味）。

服务端这次一并修的：没账号但工作区上了锁时，仍然要密钥（之前会直接当本机主人放过去）。
桌面端同理：右上角「共享 / 账号」里有成员表、邀请码生成、一次性明文密钥；没有账号时那里
会多一个「建第一个账号」的入口 —— 否则锁死自己进不去。

> 踩过的坑：`input[type=password]` 在桌面端和手机端都各自漏了样式，渲染成白底黑字的孤岛；
> 两处都补了显式规则。密码框不吃通用 `input` 选择器这件事，只有真的看一眼截图才发现。

### 「建第一个账号」为什么找不到（r14）

真机反馈：桌面端打开了共享面板，只有地址列表，**没有任何建号入口**。两个原因：

1. `bootAuth()` 只在页面加载时取一次 `/api/auth`，页面若是「服务重开之前」加载的，
   `ME` 是 `null`，`ME.accounts === false` 恒假 → 按钮渲染不出来；
2. 建号权限只在环回地址给（`can_setup`），从局域网地址打开时按钮被静默藏掉、一句话都不解释。

改法：顶栏加一个常驻的「建第一个账号」；打开共享面板时**重新拉一次身份**（并刷新按钮显隐）；
拿不到建号权限时不再装没事 —— 面板里直接写清楚「只能在跑服务的那台电脑上用
`http://127.0.0.1:8799/` 打开，右上角就有」。手机端同理，只说明在哪建、不给人假入口
（谁先建谁是主人，这是刻意的）。

顺带修顶栏：按钮挤在一起时「建第一个账号」被折成两行 —— `.topbar > *{flex:0 0 auto}` +
`white-space:nowrap`。

### 建号判断从「回环」改成「本机」（r15）

r14 的修法还留着一个坑：**在这台机器上用局域网地址打开，仍然建不了号**。
用户是从共享面板里点开 `http://192.168.1.46:8799/` 的，`can_setup` 却是 `false`，
点按钮只跳回"去 127.0.0.1 打开"的说明 —— 那句话本身没错，但对同一台机器是多余的。

`vsw/server.py` 新增 `Handler._is_local()`：回环 **或** 对端 IP 属于本机网卡
（`lan_addresses()`）都算「跑服务的那台电脑」，`/api/auth/setup` 与 `can_setup` 改用它。
安全性没有变化 —— 同一台机器上换个地址打开，不是新的信任边界；真·远程设备仍是 `false`。

实测：`/api/auth` 从 `192.168.1.46/137.2/63.1` 打开都是 `can_setup: true`；
在临时实例上用局域网地址 `POST /api/auth/setup` 直接返回 200 并种下会话（修前是 403）。
`vsw-probe-accounts.py` 加了 J 段 4 项：回环算本机、本机网卡算本机、`8.8.8.8` 不算、
同网段别人的机器不算（61 → 65 项）。

### 手机/平板打开电脑版页面版式错乱（r17）

平板用 2000×1200 打开 `http://<电脑>:8799/` 时，顶栏那排 tab 全竖着，每个字一行，顶栏高 815px。
原因不是平板，是 `.topbar .tabs { flex: 1 1 auto; min-width: 0 }` 配 `.tab` 没有
`white-space: nowrap` —— 容器允许被压，tab 就一路压到 40px 宽，中文逐字换行。

修法：`.tab{white-space:nowrap}`；1100px 以下顶栏允许换行、tab 条自己占一行并横向滚动
（隐藏滚动条）；640px 以下再缩字号与卡片最小宽度。实测 390px 视口顶栏 815 → 130px，
1366px 桌面不变。

同一个病还有一处：队列条目 `grid-template-columns: 46px 1fr auto`，中间那格放的是种子
文件名，而导入的图常常是 `BQACAgUAAyEGAAHS...png` 这种没有空格的 base64 —— `1fr` 的
min-content 被撑到上千像素，右边「自动识图 / 打开识图 / 回填结果 / ×2 / 删」整排被挤出
可视区（用户原话：「版式bug 把搜索按钮挤掉了」）。改成 `minmax(0,1fr)` + `.item > *{min-width:0}`，
文件名那行 `overflow-wrap:anywhere` 并限制两行，700px 以下按钮换到第二行整行铺开。
实测 390px 时条目高 2203 → 155px，按钮右边界 338.9 < 390。

### 「手机上没法选择归档进行合并」（r17）

勾了一个批次，按钮却像死的。两个原因叠在一起：

1. CSS 里只有 `.btn[disabled]{opacity:.42}` —— 属性选择器；JS 里 `el.disabled = true`
   只设 DOM 属性，不写 HTML 特性，选择器匹配不到。于是亮蓝色的「合并查看」在禁用状态
   看起来完全可点。
2. 用户勾的那批是**空批次**（对象已被恢复归档或删除，服务器上批次 5–12 都是这种），
   `mergedAlbumItems()` 返回 0 —— 就算按钮可点，也没有东西可显示，表现为「点了没反应」。

修法：`.btn[disabled], .btn:disabled` 一并变灰并 `cursor:not-allowed`；按钮改成**只按勾选数
禁用**（点了会再去电脑上要一次数据）；勾到空批次时提示「这个批次里的对象已经不在了
（被恢复归档或删除）」；顺带把「已归档」指标从 `S.findings.filter(f=>f.archived_at).length`
（归档对象根本不在 findings 里，永远 0）改成 `S.archived.length`。

回归探针 `scripts/vsw-probe-archive-merge-pick.js`（15 项）：未勾禁用、`:disabled` 真的变灰、
勾空批次按钮可点且提示含「空批次」、三批合并后对象数与标题都对。

### 手机端自动搜图（r17）

手机上按「自动搜图（N）」，干活的是这台电脑：请求进 `POST /api/auto/run`，服务端的
排班线程逐个调 `browser_search()`（无头 Chromium + CDP 上传变体、读回结果），手机每
2.5 秒拉一次 `/api/auto/state` 显示进度，跑完自动刷新队列与快照。

- 只有真能自动识图的引擎进候选（实测 `baidu`、`so360`），其余引擎单独计数成 `skipped`。
- 一次只允许一批（一台机器一次只能开一个识图页），重复按会被挡住并说明原因。
- 单个任务失败不中断整批，失败原因写进 `results[].error` 并在手机上显示。
- 跑完的提示放在队列/快照刷新**之后** —— 否则「同步完成」会把结论盖掉，用户只看得到同步。

回归探针：`scripts/vsw-probe-auto.py`（24 项，服务端逻辑与权限）、
`scripts/vsw-probe-auto-search.js`（17 项，手机界面，支持 `--page` 直接测源码、
`--apk` 测打包后的页面）。

### 不在同一个局域网的人打不开 → 公网通道（r16）

用户反馈「其他用户打不开，因为是局域网」。局域网地址当然只在一个网里能用，所以补了一条
**公网通道**：本机装了 Tailscale，就用它的 Funnel 把 `https://<节点名>.ts.net/` 代理到
`127.0.0.1:8799` —— 不用碰路由器，也不需要公网 IP（家宽多在运营商 NAT 后面，端口转发根本
转发不出去），TLS 由 Tailscale 边缘终止。

新增 `vsw/tunnel.py`（`exe/available/parse_status/status/start/stop/node_name`，`status` 带
20 秒缓存）+ 接口 `GET|POST /api/tunnel`（POST 的 `action` 是 `start`/`stop`，在 `_OWNER_WRITES`
里，所以只有主人能点）+ `--tunnel` / `--no-tunnel` 两个启动开关。页面入口在「共享 / 账号」→
「不在同一个局域网 / 别的城市」，手机端登录后「我的」页也会显示这条地址，可一键复制。

**踩到一个必须记下来的坑：Funnel 回源时对端地址永远写成 `127.0.0.1`。** 真实来源在
`X-Forwarded-For` 里（还带 `Tailscale-User-Login`）。如果 `_is_local()` 只看对端地址，
整个互联网都会被当成"跑服务的那台电脑" —— 谁先打开谁就把主人账号建走了。所以
`Handler._forwarded_ip()` + `_is_local()` 改成 **有 XFF 就只按 XFF 判断**。

另一道闸：没有账号也没有有效密钥时，`POST /api/tunnel {action:"start"}` 一律 400
（「先建第一个账号（或 --protect 上锁）再开公网 —— 现在库里没有任何门槛，开到公网等于谁拿到
地址都能读写」），避免"开得出去、收不回来"。

顺带修了两个界面毛病（都是 flex 项默认可缩导致的）：

* 顶栏「建第一个账号」被挤成 `建第一个 / 账号` 两行 → `.topbar button { white-space: nowrap }`；
* 共享面板里「加入 / 进来」「生成」竖排 → `.sharerow .btn` 等同一条规则。

`scripts/vsw-probe-tunnel.py`（35 项）：把 `vsw.tunnel` 的 `status/start/stop` 换成假实现
（真的 `tailscale funnel off` 是机器级开关，探针真去开一次会把用户正在用的地址关掉），
只验我们自己的逻辑：状态解析、XFF 压过对端、只有主人能开、没门槛时 400、`/api/access` 里
出现 `share.public`、经隧道的外部设备 `can_setup=false` 且读数据 401 但页面壳子 200。

真机上的现状（r16 之前实测过公网地址）：`https://silentwinter.tailf5fb77.ts.net/api/health` → 200，
`/api/auth` → `accounts: true, logged_in: false, can_setup: false`，`/api/seeds` → 401。

### 开了账号之后，取图和下载都得带身份（r16）

同一轮验证里翻出两个**只在"已经有账号"之后才出现**的 bug，都是"能看见的地方全绿、看不见的地方
一片空白"那种：

1. **手机端补图静默失败。** `ensureThumbs()` 里补图用的是裸 `fetch(relToMediaServer(rel))` ——
   没带 `X-VSW-Key` / `X-VSW-Session`。服务端一开账号，`/media/...` 全是 401，代码里又是
   `continue`，于是缩略图一张都不补、控制台一行红都没有。表现就是用户早先报的「列表里一片空白」。
   改成 `netFetch(...)`（它统一加身份头）。实测同一脚本：修前 `assets 0 / _thumbs 209`，
   修后 `assets 228 / _thumbs 228`。
2. **「保存到手机」下载 401。** 归档 zip 走 `<a download href=...>`，而 `<a>` 带不了请求头。
   `withKey(url)` 原来只认访客密钥，登录用户的会话没进去。改成"会话令牌优先、其次密钥"
   （`?sid=` / `?key=`，服务端两种都认）。探针里那三项 `size:156 / status:401 / 没有文件名`
   就是它。

教训和 r14 那次一样：**新加一层身份，就得把所有"绕开请求头"的通道都过一遍** ——
`<img>`、`<a download>`、裸 `fetch`、`Background` 下载，一个漏了就是静默失效。

### ⚠ 还没验证到的（真机才能确认）

1. **拍照 / 相册选择** —— `onShowFileChooser` 与系统相机/相册的交互只能在设备上试；
3. **扫码** —— ZXing 解码逻辑无法在没有摄像头的环境里跑；
4. **系统返回键** —— `onBackPressed` → 页面钩子的往返只在真机上才有意义；
5. **图标在启动器里的实际观感**（圆形/方形裁切、不同厂商 ROM 的适配）。

有设备时一条命令就能补上：

```bat
C:\vsw-build\android-sdk\platform-tools\adb.exe install -r vsw-workbench.apk
C:\vsw-build\android-sdk\platform-tools\adb.exe shell am start -n com.vsw.workbench/.MainActivity
C:\vsw-build\android-sdk\platform-tools\adb.exe logcat -s vsw-web:* AndroidRuntime:E
```

`adb logcat -s vsw-web:*` 能看到页面里 `console.error` 的输出（外壳把
`onConsoleMessage` 转到了 logcat），排错很有用。

## 签名密钥（**请生成你自己的**）

公开仓库里**故意不提供**签名密钥与口令 —— 那是"谁能冒充你的应用更新"的凭据。
首次执行 `build_apk.bat release` 时会自动生成一个密钥库，位置在构建根目录下：

```
%VSW_BUILD_ROOT%\vsw-release.keystore      （默认 C:\vsw-build\vsw-release.keystore）
别名      vsw
口令      构建脚本里的 KS_PASS（请改成你自己的强口令）
有效期    10950 天（30 年）
```

三件必须做的事：

1. **改掉 `build_apk.bat` 里的 `KS_PASS`**，别用仓库里的默认值；
2. **备份密钥库并把口令单独保存**：一旦丢失，已经装了你的包的用户**无法升级**
   （Android 只允许同签名覆盖安装），只能卸载重装；
3. **密钥库与口令都不要提交到版本库**（`.gitignore` 已经拦了 `*.keystore` / `*.jks`，
   但仍然别把它放进仓库目录）。

要发布新版：改 `build_apk.bat` 里的 `--version-code` / `--version-name`，用**同一个**密钥库重新签名。

改版本号：`build_apk.bat` 里的 `--version-code 1 --version-name 1.0`，每次发新版都要往上加。

## 分发给别人的完整流程

给别人用有两条路。**推荐第一条**，因为只有它能搜新图：

### A. 对方自己搭一套工作台（能力完整）

1. 把整个 `vsw` 文件夹 + `start_vsw_lan.bat` 发给对方；
2. 对方电脑装 Python 3.10+ 并 `pip install Pillow numpy requests`（语义层可选）；
3. 对方双击 `start_vsw_lan.bat`，启动日志里会出现局域网地址和二维码；
4. 对方把这个 APK 装到手机，打开 → 在引导页扫码或手填地址 → 同步。

对方需要会一点命令行。电脑和手机必须在同一个 WiFi。

### B. 你导出数据集，对方纯离线看（最省事）

1. 你在手机端「我的 → 导出数据集」导出 JSON（含缩略图）；
2. 把文件发给对方，对方在引导页选「选择数据集文件」导入。

对方能翻结果板、看来源与搜索链回放、做归档与清理，但**不能搜新图**
（无头浏览器只能在电脑上跑 —— 这是物理限制，不是没做）。

## 代码结构

```
vsw-android/
  AndroidManifest.xml              清单：权限、明文流量、启动 Activity
  java/com/vsw/workbench/
    MainActivity.java              WebView 外壳：设置、文件选择、权限、原生桥、返回键
    QrScanActivity.java            扫码页：Camera 预览 + ZXing 解码（自己实现，无 AAR）
  res/
    values/{strings,styles,colors}.xml
    drawable/ic_launcher_foreground.xml   矢量前景（取景框 + 搜索点）
    mipmap-anydpi-v26/                    自适应图标（API 26+）
    mipmap-*/                             位图图标（API 24/25 兜底）
  make_icons.py                   生成上面那些 PNG（可从零复现，不塞二进制图）
  build_apk.bat                   手工构建链（必须保持 ASCII，见文件头注释）
```

内置页面是**从 `vsw/static/mobile/index.html` 复制**进 `assets/mobile/` 的，
单一来源，不维护第二份。改手机端 UI 只需要改那一份文件再重新构建。

## 从头装一遍工具链

```powershell
# JDK 17（Temurin，182 MB）
$api = 'https://api.adoptium.net/v3/assets/latest/17/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse'
$url = (Invoke-RestMethod $api)[0].binary.package.link
Invoke-WebRequest $url -OutFile C:\vsw-build\jdk17.zip
Expand-Archive C:\vsw-build\jdk17.zip C:\vsw-build\jdk17
(Get-ChildItem C:\vsw-build\jdk17 -Directory).FullName | Out-File C:\vsw-build\java-home.txt

# Android cmdline-tools（147 MB）
Invoke-WebRequest 'https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip' -OutFile C:\vsw-build\cmdline.zip
Expand-Archive C:\vsw-build\cmdline.zip C:\vsw-build\android-sdk\cmdline-tools\tmp
Move-Item C:\vsw-build\android-sdk\cmdline-tools\tmp\cmdline-tools C:\vsw-build\android-sdk\cmdline-tools\latest
Remove-Item -Recurse C:\vsw-build\android-sdk\cmdline-tools\tmp

# SDK 组件 + 许可
$env:JAVA_HOME = (Get-Content C:\vsw-build\java-home.txt).Trim()
$sm = 'C:\vsw-build\android-sdk\cmdline-tools\latest\bin\sdkmanager.bat'
('y' * 30) -split '' | & $sm --sdk_root=C:\vsw-build\android-sdk --licenses
& $sm --sdk_root=C:\vsw-build\android-sdk platform-tools platforms;android-34 build-tools;34.0.0

# ZXing core
New-Item -ItemType Directory -Force C:\vsw-build\libs | Out-Null
Invoke-WebRequest 'https://repo1.maven.org/maven2/com/google/zxing/core/3.5.3/core-3.5.3.jar' -OutFile C:\vsw-build\libs\zxing-core-3.5.3.jar
```

总共约 700 MB（不含系统镜像）。装完跑一次 `build_apk.bat release` 应能得到同样的 APK。

## 构建时踩到的坑（写下来免得再踩）

1. **`.bat` 必须全 ASCII。** cmd.exe 按 OEM 代码页逐字节解析批处理，文件里出现 UTF-8
   中文注释会让解析器直接崩（报 `'�' is not recognized`），`chcp 65001` 救不了它自己的解析。
2. **`aapt2 link` 的 `-R` 要单个文件，不是目录。** 传目录报 `failed to open file: 数据无效`；
   传 `res.zip` 又会被当成叠加层，报 `resource ... does not override an existing resource`，
   得配 `--auto-add-overlay`。
3. **别在 `.bat` 里调用 `build-tools\*\d8.bat`。** 它做完自己的 `setlocal`/参数处理后，
   会让**父批处理在 d8 之后静默结束**（退出码 0、没有产物，什么错误都不报）。
   直接 `java -cp d8.jar com.android.tools.r8.D8` 才稳；`apksigner.bat` 同理，用
   `java -cp apksigner.jar com.android.apksigner.ApkSignerTool`。
4. **`-bootclasspath android.jar` 下不能用 lambda / 方法引用**（缺
   `java.lang.invoke.LambdaMetafactory`，报 `找不到符号 metafactory`），全改匿名内部类。
5. **`file://` 下 `location.origin` 是字符串 `"null"`。** 早期版本把它当成"已设地址"，
   结果首次引导被跳过、对方打开只看到空白页。这个 bug 是探针抓出来的，不是看代码看出来的。
6. **`apkanalyzer.bat` 不自己找 JDK**，必须把 `JAVA_HOME` 传进它的环境。

### 应用自我更新（r18）

**要解决的问题**：APK 发出去以后，每次改一行都要重新找数据线、发文件、让人再装一遍。

**做法**：打包即发布 + 手机自己问版本。

* `build_release.bat` 打好包后调用 `scripts/vsw-publish-apk.py`，把 APK 和 `version.json`
  （版本、大小、SHA256、说明）放进 `<root>/dist/`。**打一次包 = 发布一次**，没有第二步。
* 版本号只有一个真相：`MainActivity.java` 里的 `BUILD_TAG`。打包、发布、手机上报当前版本都读它。
* 手机端「我的 → 检查更新」→ `GET /api/app/version?build=r17`，回来 `update_available` 就显示
  「下载并安装」。本地 `dist/` 没有包时才去问 GitHub Releases（`--gh OWNER/REPO`）。
* 这两个接口**不需要登录**：版本号和安装包不是谁的私人数据，手机登录前就得能问。

**为什么要自己写 `ApkProvider`**：Android 7+ 不给别的应用 `file://`，安装器要 `content://`。
没有 AndroidX 就用不了 `FileProvider`，所以写了一个最小只读 ContentProvider
（`com.vsw.workbench.files`，只允许单层文件名、`MODE_READ_ONLY`），装的时候再
`FLAG_GRANT_READ_URI_PERMISSION` 把读权限授给系统安装器。

**必须承认的边界**：Android 不允许应用自己静默安装。所以"自动更新"实际是
**自动检查 + 一键下载 + 弹系统安装框**，最后那一下永远得用户点。下载完的包先校 SHA256，
对不上就删掉重来，不会把半个包递给安装器。

**这一版踩到的两个真 bug**：

1. `release.py` 在 `version.json` 缺失时用 `f"size{stat.st_size}"` 当版本号 —— 界面上会出现
   「有新版本 size1204」，还会拿去比大小。改成留空，界面如实说「说不清是不是最新」。
2. 手机端三个原生回调原来叫 `updateProgress/updateFailed/updateNeedPerm`，而 Java 里推的是
   `window.vswUpdateProgress/vswUpdateFailed/vswUpdateNeedPerm` —— **进度和报错全都静默无效**。
   改名对齐后，探针里专门验了「进度条走到 42%」「校验失败显示原因」「缺权限时指路去设置」。

### 手机搜图的结果图全裂了（r23）

~~百度/360 的结果图有防盗链，只认 Referer。~~ **这个判断是错的，已实测推翻**：`mms*.baidu.com`、`p0.ssl.qhimgs1.com` 带不带 Referer 都回 200。真实情况更可能是**提取这一侧就没抓到图**（`similar` 为空），面板于是显示「没抓到相似图」—— 既没图也没统计行，和用户看到的一模一样。

r24 起先加诊断再谈修复：结果为空时把 `diag`（pcsimi 请求条数、页面图片主机分布）写进面板；标题带版本号；抽图从白名单主机放宽到「≥96px 的图都要」。

修法：`WebSearchActivity` 命中结果后不立刻返回，先起 `vsw-thumb-fetch` 线程把前 12 张抓回来（`HttpURLConnection` 带 `Referer: <引擎搜索页>` + 移动 UA，5 秒超时、单张 6 MB 上限、`inSampleSize` 抽样解码、360px / JPEG 72），转成 `data:image/jpeg;base64,…` 塞进每条 `d` 字段；页面 `x.d || x.u`，并把「其中 N 张已存成本地小图」写在统计行。

踩到的两个细节：

- `Referer` 必须是**引擎搜索页**，不能是图片地址本身，否则照样 403。
- 解码一定要先 `inJustDecodeBounds` 再 `inSampleSize`：结果图里有 4K 截图，直接解会吃掉几十 MB。

验证：`vsw-probe-phone-search.js` 从 20 项加到 **23 项**（内嵌图优先、没内嵌的退回远程、统计文案），并新增 APK 内容检查（dex 里要有 `finishWithThumbs`/`fetchThumb`/`vsw-thumb-fetch`，页面里要有 `x.d || x.u`）。

### 点「扫码」就闪退（r22）

小米 9 / MIUI 12.5 / 安卓 11 上用户实测：点扫码直接闪退。

根因：`QrScanActivity.java` 写好了、也编译进 dex 了，**但 AndroidManifest.xml 里没声明这个
Activity** → `startActivityForResult(new Intent(this, QrScanActivity.class), REQ_SCAN)` 抛
`ActivityNotFoundException`，没人接，进程就没了。

第二个坑紧跟着：扫码页用 `takePicture` 的 JPEG 解码，而 `takePicture` 返回的是**照片**分辨率。
小米 9 默认 4000×3000，解一张要 48 MB 位图 + 48 MB 像素数组；`OutOfMemoryError` 是 Error，
`catch (Exception)` 接不住 —— 就算清单补好了，某些机型照样会死在解码那步。

这一版：

- 清单补 `.QrScanActivity`（`exported=false`，锁竖屏）；
- 新增 `pickPictureSize()`：从 ≤200 万像素里挑最大的，相机不再回原尺寸图；
- `decode()` 先 `inJustDecodeBounds` 再按 `inSampleSize` 缩到 800px 内，进内存的永远在 1M 像素内；
- 解码线程 `catch (Throwable)`，OOM 只记日志、继续扫下一帧；
- `openScanner()` 外面再包一层 try/catch，打不开就提示手填地址。

`scripts/vsw-verify-apk.py` 也因此从 38 项加到 **42 项**：多了「清单里声明了
QrScanActivity / WebSearchActivity / ApkProvider」「扫码页锁竖屏」四条 —— 之前的 38 项只查
dex 里有没有类，**dex 里有类 ≠ 系统能启动它**，这次的 bug 就是从这条缝里过去的。

### 断联电脑也能用（r21）

用户实测：拔掉电脑从相册导入一张图，队列里既没有这张图也没有缩略图，「图不见了」，
必须先连电脑才能搜。根因不在图，在离线那一支：`importOne()` 只把图塞进 IndexedDB 的
`pending` 收件箱，本地既不建种子也不建队列项 —— 界面上当然什么都没有。

现在（`vsw/static/mobile/index.html`）：离线导入就地建本地种子 + 本地队列项 + 320px 缩略图 +
原图，id 一律用负数（`meta.localSeq` 分配），队列页立刻看得见图、标着「手机上」，
「手机搜」用本地原图直接喂 WebView。离线删除也通（连带清掉 pending 那条），回连后
`flushPending()` 推给电脑进正式库并删掉本地影子记录。

一起修的两个「同步一次就没了」：`refreshQueueFromServer()` 与 `syncSnapshot()` 清空本地仓之前，
先把 `local: 1` 的行留出来，写完服务端数据再补回去。

另外修了一个只在「库里有访问密钥」时才出现的启动崩溃：Windows 控制台是 GBK，
`print("[vsw] \U0001f512 已上锁…")` 抛 `UnicodeEncodeError`，服务还没监听就退出。
`main()` 现在先把 stdout/stderr 改成 UTF-8 + `errors="replace"`。

### 手机自己搜（r19）：先纠正我自己说错的一句话

上一版我写过「手机跑不了无头浏览器，所以自动识图只能在电脑上」。这句把两件事混了：

* **WebView 能上传**。`WebChromeClient.onShowFileChooser()` 就是替网页的
  `<input type=file>` 交图的钩子；不实现它，网页的上传按钮才点不动。
* **但不能无人值守**。浏览器规定文件选择器必须由用户手势触发，后台注入的
  `input.click()` 打不开它（没有 user activation），因此「一张接一张自动跑」手机上做不到。

所以 r19 做的是**半自动**，只在「电脑不在手边」时用：

```
队列项「手机搜」
  → MainActivity.Bridge.webSearch(engine, label, url, extractJs, imageBase64)
  → 图落到 getCacheDir()/websearch/，经 ApkProvider 换成 content:// URI
  → WebSearchActivity 打开引擎页面；onShowFileChooser 立刻把这张图还回去
  → shouldInterceptRequest 记下 /ajax/pcsimi（替代电脑端的 capture_network）
  → 每 1.5 秒注入一次提取脚本（就是 vsw/engines.py 里那两份，原样搬进页面）
  → 相似图 ≥ 4 张就回传，最多等 90 秒；结果交回页面渲染成面板
```

几处刻意的取舍：

* **先试真实触摸**：`MotionEvent` + `dispatchTouchEvent` 去点页面上的上传按钮（合成事件
  拿不到 user activation，所以走的是真输入通道）。点不动就明说「请你点一下上传」。
* **没有预选图时返回 `false`**，让系统文件选择器正常弹出来 —— 宁可多一步，也不要卡死。
* 顶栏一直显示当前阶段（找上传按钮 / 图已交出 / 等结果 / 抓到了），失败也写清原因，
  用户截一张图就能定位，不需要 logcat。
* 结果只放进手机侧面板（识别结论 / 相似图 / 来源网页 / 复制链接）。要进库、要去重、
  要算发散度，仍然回到电脑那条流水线 —— 手机上不重写一套去重。

**验证到哪**：页面这一半 20/20（`scripts/vsw-probe-phone-search.js`：桥接前不显示按钮、
点击把图与提取脚本交给原生、没图要提示、结果面板与错误面板）；APK 里确认
`WebSearchActivity` 进了清单与 dex、`onShowFileChooser` 在类里、页面含 14 万字符与两份提取脚本。
**没验到**：真机上 `onShowFileChooser` 何时被调用、触摸能不能拿到 user activation、
百度的上传控件在手机上长什么样 —— 这三件只能你装上去点一次才知道。

### 真机报错：Can't toast on a thread that has not called Looper.prepare()（r20）

r18 在真机上的第一次「下载并安装」就炸了：包下完、SHA256 也过了，然后页面显示

```
更新失败：Can't toast on a thread that has not called Looper.prepare()
```

原因很直白：`installUpdate()` 为了不卡界面开了后台线程，`downloadAndInstall()` 就跑在那条
线程上；而它最后调 `startInstall()` —— 里面既要 `startActivity`（调系统安装器）又要
`Toast`。**Toast 在没有 Looper 的线程上直接抛异常**，而这句 `toast(...)` 恰好排在
「未知来源未授权」的分支里：本来该跳去设置页，结果是先崩了。

修法（r20）：

* `toast()` 自己改成 `runOnUiThread(...)`，所有调用点都不用再操心线程。
* `startInstall()` 只做一件事：`runOnUiThread(doStartInstall(apk))`；真正的
  `startActivity` 挪进 `doStartInstall`，跑在 UI 线程上。
* 顺手补了「授权后自动继续」：未授权时把待装的包记进 `pendingInstall`，跳设置页；
  `onResume()` 里发现权限已给就直接 `doStartInstall`，用户不用回来看半天再点一次。

**已经装在 r18 上的机器怎么办**：r18 的这段代码是坏的，所以要么手动装一次 r20，要么
先在系统设置里把「视觉工作台 → 安装未知应用」打开（授权在手时 r18 走的是不需要 Toast
的那条分支，`startActivity` 从后台线程调用是合法的），再回去点「下载并安装」。

### 手机搜「抓到 8 张无关图」（r27）

面板上那行 `图源：页面里捡的` 是决定性的：它说明 `pcsimi` 一条都没捕到，也就是**图根本没传上去**，
页面还停在百度识图首页 —— 而啤酒瓶、水花、花、人像、雪山正是首页的样图。所以那一刻展示的不是搜索结果。

现在的处理：

* `diag.source === 'dom'` 时直接判失败，面板写「这次没把图交给引擎…回到搜索页点一下「上传图片」」，
  清空图片列表、不给「存进结果板」按钮；
* 同一张图常带多个尺寸地址（`~tplv-…`、`?x-oss-process=…`、`_500x500`），
  `Service._image_key()` 归一后去重，库里已有的直接算「重复」，不再收成好几个对象；
* 顺手把 r23 那句「引擎有防盗链」删了 —— 实测 549 条真实引擎图地址在三种 Referer 下**全部 200**，
  跟防盗链无关。

服务端替用户上传这条路也试过：`POST https://graph.baidu.com/upload?uptime=…` 返回
`{"status":1,"msg":"Reject","data":null}`，所以那次上传只能由 WebView 里的真实手势触发
（`input.click()` 拿不到 user activation）。程序会先自己算坐标点一次，点不动就明说让你手点。

### 手机搜的「点这里」按钮（r31）

`WebSearchActivity.onPageFinished` 注入 `#vsw-cta` 浮层按钮。点它 → 在真实点击事件里
`input.click()` → `onShowFileChooser` 交回预选图（`window.__vswOffered` 回写、按钮变绿）。
自动派发合成触摸这条路在 Xiaomi 15 / HyperOS 上拿不到 user activation，已放弃当作主路径。

### 手动抓图：开始 / 结束（r32）

`WebSearchActivity` 顶栏两个按钮：`startCapture()` 置 `capturing=true`，`pollTask` 里
`accumulate()` 按 `u` 去重累计（`CAPTURE_MAX=160`）；`stopCapture()` 把 `accSimilar`
塞进最新提取结果（并写 `diag.capture`）再 `finishWithThumbs`。`THUMB_MAX` 12 → 40。

### 结束抓图闪退 / 只抓首屏（r33/r34）

结果改走进程内 `staticResult`（Intent extras 过 Binder 会 `TransactionTooLargeException`）；
抓图期间每次轮询滚一屏、到底点「查看更多 / 下一页」，把懒加载的结果顶出来。

### 抓图三处修正（r35）

页面端收进结果板不再只送 12 张（去重最多 60）；`accumulate()` 遍历结果对象所有数组
（related/products/more 也收）；抓图中点「关闭」会先弹确认，别让人手快把这一轮丢掉。

### 「存进结果板（10）」其实数错了（r36）

按钮上的数字原来数的是内嵌小图（受字节预算限制），改成数可存张数；
全部重复时提示改成「这 N 张都已经在库里了，没有新图」，不再标红成失败。

### 结果板「没有图」（r38）

不是图丢了：卡片原来只认本地缓存，而缓存是后台慢慢下的；结果板又按发散度排序，
新存的图在最下面。r38 起在线直接引用电脑上的缩略图，存完立刻可见。

### 「存进结果板」按不动（r39）

面板内容有三千多像素长，操作按钮原来在最后一行 —— 不滚到底点不到。
r39 起按钮固定在面板底栏；预览只画前 12 张。另外 `openSheet()` 会先收掉首次说明
那层全屏遮罩（z-index 70 > 面板 60），否则面板上什么都点不动。

### 手机自己就是个相册（r40）

用户问「为什么一定要连电脑，再同步才能看到结果板，即使我在本地没有 SigLIP，
但是也有图片啊，可以就当作一个相册」——r40 把「有图」和「能算指标」解耦：

- 手机上搜到/导入的图以 `local:1` 进结果板，缩略图与原图都存在 IndexedDB；
- 指标位留空（不显示 0.00），卡片写「手机上 · 本地相册 · 还没送进电脑」；
- 详情说明「发散度/来源/分类要 SigLIP 和电脑」，可删、可在浏览器打开来源；
- 同步快照时自动把这批送进电脑（`/api/mobile/results`）换成正式对象。

顺带修掉 `makeThumb()` 返回 `{blob,size}` 被当成 Blob 存进库的真 bug（重载后结果板空白）。

### 深挖加「上一层」、手机相册自动推回（r41）

**深挖一层挖到空页**：百度对一部分图就是没有结果。现在 `WebSearchActivity` 深挖前把当前地址压进
`ArrayDeque<String> backStack`，顶栏多一个「上一层」按钮；退回时先 `depthPhase = false` 停掉自动换页，
再用 `goingBack` 挡住 4 秒内的判断，否则刚退回去又被换走。栈空时按钮灰着、提示「已经在最上面这一层」。

**离线攒的图自动推回电脑**：`flushLocalFindings()` 把本地 `local:1` 的条目按 60 张一批
`POST /api/mobile/results`；服务端 `add_mobile_results()` 发现没有队列项时用 `_album_job()` 现建
「手机相册（手机推回）」任务。断网时抓到的 `data:` 内嵌图在服务端走 `base64.b64decode`，
不走 `requests`（它没有 `data:` 适配器，会抛 `InvalidSchema`）。

打包：`vsw-workbench.apk` 739,370 字节，SHA256 `7B1919B491E39169DAA1A8E7E2AC1FE0BCCBF0551187F449951571F6E4B76CCB`；
`vsw-verify-apk.py` 42 项、`vsw-probe-local-album.js` 17 项、`vsw-probe-offline.js` 25 项、
`vsw-probe-mobile-results.py` 13 项全绿。

### 「已经在结果板里的图推不进电脑」（r42）

r41 的自动推送发的是 `image_url`（远程地址），让电脑自己去下载；电脑拿不到就 `new_findings = 0`、
`duplicates = 0`，手机端判定失败但没有任何提示。

r42 三件事：

1. `localFindingDataUrl(f)` 在推送前把 IndexedDB 里的图转成 data URL（原图优先，退回缩略图），图跟着数据走；
2. 结果板顶部加「推到电脑（N）」按钮，自动推失败时还能手动点；
3. 失败不再沉默：把服务端返回的 `failures[0]` 说出来。

探针 `scripts/vsw-probe-album-push.js` 13 项，故意用**打不开的域名**当 `image_url`，
只有 data URL 这条路能成。r42 = 747,562 字节，SHA256 `CB10BBC27CB4BE2D628B67911F4A1C17A2E4DC1CBA333816833588F2BB808518`。
