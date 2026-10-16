# 视觉搜索工作台 · 安卓版（APK）

把 [vsw/static/mobile](../vsw/static/mobile) 里的手机端 WebUI 包成一个可以**直接发给别人**的 APK。

## 产物

仓库里**只有源码**（`*.apk` 已加入 `.gitignore`）。自己构建一次即可得到：

| 文件 | 说明 |
| --- | --- |
| `vsw-workbench.apk` | 可直接安装的 release 包（约 354 KB，用你自己的密钥库自签） |
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

没有真机可用：这台机器没有通过 adb 连接的设备，CPU 虚拟化被 Hyper-V 占着、
开不了模拟器，装系统镜像也要额外 1–2 GB 而 C 盘只剩 3 GB。所以做了四层验证：

```bash
# 1) 交付面：结构 / 身份 / 权限 / 签名 / 类是否真进了 dex（32 项）
python ../scripts/vsw-verify-apk.py

# 2) 行为面：从 APK 里解出内置页面，用同内核的浏览器跑起来验（13 项）
node ../scripts/vsw-probe-apk-page.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799
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

### 真机踩到的坑：内置页面读不出来（已修）

第一版装到小米（Android 15 / WebView 131）上打开是 `net::ERR_FILE_NOT_FOUND`。
后来做了启动诊断屏，拿到确切原因：

```
资产 assets/mobile/index.html → 0 字节
失败：读取内置页面失败：FileNotFoundException mobile/index.html
```

**条目明明在 APK 里**（电脑上解包能看到 81,445 字节，且是 STORED），但
`getAssets().open("mobile/index.html")` 在真机上抛 `FileNotFoundException`。
进一步的证据是 `resources.arsc` 里**查不到任何 assets 路径记录** ——
也就是"条目在包里，但 AssetManager 按名字索引不到它"。

修法是**三级兜底**，三条都失败才显示诊断页，并把每条的错误写在屏幕上：

| 顺序 | 路径 | 说明 |
| --- | --- | --- |
| 1 | `getAssets().open()` | 正常路径 |
| 2 | **ZipFile 直接读 APK** | 把 APK 当 zip，按 `assets/mobile/index.html` 抠出来。不经过 AssetManager 的名字索引，所以"索引有洞"也照样能拿到内容 |
| 3 | `res/raw/page.html` | 资源表索引的资源，aapt2 一定会为它建索引 |

构建脚本现在把页面**同时打进** `assets/` 与 `res/raw/`（两份都是未压缩，共约 81 KB × 2），
启动屏会写明这一次实际走的是哪条路（`来源 assets 索引` / `APK zip 直读` / `res/raw`）。

`scripts/vsw-verify-apk.py` 也补了对应断言：兜底资源必须在包里、两份内容必须一致、
资源表里必须查得到条目名 —— 这个坑不能再靠人眼发现。

### ⚠ 还没验证到的（真机才能确认）

1. **装完点开能不能正常显示** —— 没真机调试环境，只能确认同一 Blink 内核下页面正常启动；
   （上面那个 assets 问题就是真机反馈才发现的，所以这一条以真机为准）
2. **拍照 / 相册选择** —— `onShowFileChooser` 与系统相机/相册的交互只能在设备上试；
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
