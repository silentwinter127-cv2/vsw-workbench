# 视觉搜索工作台 · 安卓版（APK）

把 [vsw/static/mobile](../vsw/static/mobile) 里的手机端 WebUI 包成一个可以**直接发给别人**的 APK。

## 产物

仓库里**只有源码**（`*.apk` 已加入 `.gitignore`）。自己构建一次即可得到：

| 文件 | 说明 |
| --- | --- |
| `vsw-workbench.apk` | 可直接安装的 release 包（约 602 KB，用你自己的密钥库自签；当前 r15 为 616,493 字节） |
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
