package com.vsw.workbench;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;

/**
 * 安卓外壳：一个 WebView 装载打包在 assets 里的手机端单文件 WebUI。
 *
 * 设计取舍（都是为了"分发给别人也能跑"）：
 *   - 不硬编码任何服务器地址：工作台在哪由用户在应用内填写或扫码，见网页里的引导页。
 *   - 全部资源内置：断网、没有电脑也能打开，浏览已导入的数据集。
 *   - 不引入 androidx / 任何第三方 UI 库：手工编译链少依赖、APK 小、出问题的环节少。
 *   - 文件选择走 WebView 自带通道（相册/相机的 content URI 直接交给页面读），不需要 FileProvider。
 *   - 权限按需申请：只在真要用相机时才弹，避免一装就要一堆权限。
 */
public class MainActivity extends Activity {

    /** 页面在 APK 里的位置。 */
    static final String ASSET_PAGE = "mobile/index.html";
    /**
     * 真正的加载地址。**不直接用 `file:///android_asset/`**：
     * 真机上报 `net::ERR_FILE_NOT_FOUND`（资产默认压缩存储，不同 ROM/WebView 版本
     * 对 android_asset 这个虚拟目录的处理差别很大）。所以启动时先把页面复制到应用私有
     * 目录，再用真实文件路径加载 —— 路径存在与否我们自己在 Java 侧就能确认。
     */
    static final String PAGE_FILE = "mobile/index.html";

    /**
     * 用在应用名和启动屏里的构建标识。每次改 APK 都要改它 —— 这样"我装了新版吗"
     * 这个问题不需要靠猜（真机上真的发生过"装的是旧包"）。
     */
    static final String BUILD_TAG = "r16";

    private int assetBytes = 0;
    private long pageFileLength = 0;
    private String sourceUsed = "";

    private static final int REQ_FILE = 1001;
    private static final int REQ_WEB_CAMERA = 1002;
    private static final int REQ_SCAN = 1003;
    private static final int REQ_SCAN_PERM = 1004;

    private WebView web;
    private android.widget.TextView splash;
    private ValueCallback<Uri[]> fileCallback;
    private PermissionRequest pendingWebPermission;
    private String pageUrl = "";
    private final StringBuilder trail = new StringBuilder();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        FrameLayout root = new FrameLayout(this);
        root.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.setBackgroundColor(Color.parseColor("#0e1116"));

        web = new WebView(this);
        web.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        web.setBackgroundColor(Color.parseColor("#0e1116"));
        root.addView(web);

        // 启动时先把"准备阶段"的事实摊在屏幕上：资产多大、落盘到哪、WebView 版本。
        // 真机出问题时，用户截一张图就能代替 logcat（之前只有一句 ERR_FILE_NOT_FOUND，
        // 完全看不出是哪一步断的）。页面加载成功后 JS 会调 AndroidBridge.hideSplash()。
        splash = new android.widget.TextView(this);
        splash.setBackgroundColor(Color.parseColor("#0e1116"));
        splash.setTextColor(Color.parseColor("#9aa8b8"));
        splash.setTextSize(12f);
        splash.setPadding(28, 90, 28, 28);
        splash.setText("正在准备内置页面…");
        root.addView(splash, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        setContentView(root);

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);          // IndexedDB / localStorage 都要它
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setJavaScriptCanOpenWindowsAutomatically(true);
        settings.setSupportMultipleWindows(false);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(false);           // 页面自带 viewport，别让 WebView 再缩放
        settings.setTextZoom(100);                    // 系统字号放大不该把布局顶坏
        // 页面是 file:// 装载的，要允许它去 http://<局域网IP> 取快照与图片
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return openExternally(request.getUrl());
            }

            @Override
            @SuppressWarnings("deprecation")
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return openExternally(Uri.parse(url));
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileCallback != null) {
                    fileCallback.onReceiveValue(null);
                }
                fileCallback = callback;
                try {
                    Intent intent = params.createIntent();
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    startActivityForResult(intent, REQ_FILE);
                    return true;
                } catch (Exception exc) {
                    fileCallback = null;
                    toast("打不开文件选择器：" + exc.getMessage());
                    return false;
                }
            }

            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                // 页面用 getUserMedia 直接开摄像头时才走这里；我们的拍照导入走系统相机，通常不触发
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        if (hasCameraPermission()) {
                            request.grant(request.getResources());
                        } else {
                            pendingWebPermission = request;
                            requestPermissions(new String[]{Manifest.permission.CAMERA},
                                    REQ_WEB_CAMERA);
                        }
                    }
                });
            }

            @Override
            public boolean onConsoleMessage(ConsoleMessage message) {
                android.util.Log.i("vsw-web", message.message()
                        + " @" + message.sourceId() + ":" + message.lineNumber());
                return true;
            }
        });

        web.addJavascriptInterface(new Bridge(), "AndroidBridge");

        // 下载处理。没有它，页面里任何"下载"链接在 WebView 里都是**静默无效**的 ——
        // 手机上"看不到归档文件夹"有一半是这个原因：点了什么都不发生。
        // 这里交给系统的 DownloadManager：文件会落到「下载」，完成时给通知，
        // 用户可以在任意文件管理器里打开（zip 里是图片 + manifest.json）。
        web.setDownloadListener(new android.webkit.DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                        String mimeType, long contentLength) {
                try {
                    String name = guessFileName(url, contentDisposition, mimeType);
                    android.app.DownloadManager manager = (android.app.DownloadManager)
                            getSystemService(DOWNLOAD_SERVICE);
                    if (manager == null) {
                        toast("系统没有下载服务，无法保存");
                        return;
                    }
                    android.app.DownloadManager.Request request =
                            new android.app.DownloadManager.Request(Uri.parse(url));
                    request.setMimeType(mimeType);
                    request.addRequestHeader("User-Agent", userAgent);
                    request.setNotificationVisibility(
                            android.app.DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    request.setDestinationInExternalPublicDir(
                            android.os.Environment.DIRECTORY_DOWNLOADS, name);
                    manager.enqueue(request);
                    toast("已开始下载：" + name + "\n完成后在系统的「下载」里打开");
                } catch (Exception exc) {
                    toast("下载失败：" + exc.getMessage());
                }
            }
        });

        // 先把页面落到私有目录，再加载真实路径；失败也要说清原因，不要只给一个白屏
        String problem = preparePage();
        updateSplash(problem);
        if (problem == null) {
            android.util.Log.i("vsw-web", "loading " + pageUrl);
            web.loadUrl(pageUrl);
        } else {
            android.util.Log.e("vsw-web", "preparePage failed: " + problem);
            web.loadDataWithBaseURL(null, buildDiagnosticPage(problem), "text/html", "utf-8", null);
        }
    }

    /** 把准备阶段的事实写进启动屏；成功时也保留几秒，方便用户截图核对。 */
    private void updateSplash(String problem) {
        if (splash == null) return;
        StringBuilder sb = new StringBuilder();
        sb.append("构建 ").append(BUILD_TAG).append("\n");
        sb.append("包名 ").append(getPackageName()).append("\n");
        sb.append("Android ").append(android.os.Build.VERSION.RELEASE)
          .append(" (API ").append(android.os.Build.VERSION.SDK_INT).append(")\n");
        sb.append("WebView ")
          .append(android.webkit.WebSettings.getDefaultUserAgent(this).replaceAll(".*Chrome/", "Chrome/"))
          .append("\n");
        sb.append("资产 assets/").append(ASSET_PAGE).append(" → ").append(assetBytes)
          .append(" 字节\n");
        if (problem == null) {
            sb.append("来源 ").append(sourceUsed).append("\n");
            sb.append("落盘 ").append(pageUrl).append("\n");
            sb.append("文件 ").append(pageFileLength).append(" 字节 · 可读=")
              .append(isPageReadable()).append("\n");
            sb.append("\n页面已提交加载。如果这行下面一直是空白或英文报错，请截图。");
        } else {
            sb.append("失败：").append(problem).append("\n");
            sb.append(trail);
        }
        splash.setText(sb.toString());
    }

    private boolean isPageReadable() {
        try {
            java.io.File file = new java.io.File(getFilesDir(), PAGE_FILE);
            java.io.FileInputStream in = new java.io.FileInputStream(file);
            byte[] head = new byte[16];
            int read = in.read(head);
            in.close();
            return read > 0 && new String(head, "UTF-8").toLowerCase().contains("doctype");
        } catch (Exception exc) {
            trail.append("读回失败: ").append(exc.getMessage()).append('\n');
            return false;
        }
    }

    /**
     * 把内置页面复制到 /data/data/<pkg>/files/mobile/index.html，返回可加载的 URL；
     * 出问题则返回一句人话说明（会显示在页面上，方便没有 logcat 的人直接截图反馈）。
     *
     * 为什么要三级兜底：真机（小米 / Android 15）上 `getAssets().open("mobile/index.html")`
     * 抛 FileNotFoundException，而同一个 APK 在电脑上解包**明明有**这个条目，
     * 而且 `resources.arsc` 里查不到任何 assets 路径记录 —— 也就是"条目在包里，
     * 但 AssetManager 按名字索引不到它"。所以不能只依赖 assets：
     *
     *   1. `getAssets().open()`            —— 正常路径；
     *   2. 直接 ZipFile 打开 APK 抠条目     —— assets 索引失效时的兜底（和读 SQLite 一个道理，
     *                                          绕开那层虚拟目录，自己按路径找）；
     *   3. `res/raw/page.html`             —— 资源表索引的资源，aapt2 一定会建索引。
     *
     * 三条都失败才会显示诊断页，并在上面写清每一条的错误。
     */
    private String preparePage() {
        byte[] payload = null;
        String source = "";

        try {
            payload = readAsset(ASSET_PAGE);
            source = "assets 索引";
        } catch (Exception first) {
            trail.append("assets 失败: ").append(brief(first)).append('\n');
            try {
                payload = readFromApkZip(ASSET_PAGE);
                source = "APK zip 直读";
            } catch (Exception second) {
                trail.append("zip 直读失败: ").append(brief(second)).append('\n');
                try {
                    payload = readRawResource();
                    source = "res/raw";
                } catch (Exception third) {
                    trail.append("res/raw 失败: ").append(brief(third)).append('\n');
                    return "三条读取路径都失败（详见下方诊断）";
                }
            }
        }

        assetBytes = payload == null ? 0 : payload.length;
        sourceUsed = source;
        if (payload == null || payload.length < 1024) {
            return "内置页面只有 " + assetBytes + " 字节，打包不完整（来源：" + source + "）";
        }

        java.io.FileOutputStream output = null;
        try {
            java.io.File dir = new java.io.File(getFilesDir(), "mobile");
            if (!dir.isDirectory() && !dir.mkdirs()) {
                return "无法创建目录 " + dir.getAbsolutePath();
            }
            java.io.File target = new java.io.File(dir, "index.html");
            // 内容变了才重写（升级 APK 后能自动刷新，平时不白写磁盘）
            boolean needWrite = !target.isFile() || target.length() != payload.length;
            if (needWrite) {
                output = new java.io.FileOutputStream(target);
                output.write(payload);
                output.flush();
            }
            if (!target.isFile() || target.length() == 0) {
                return "写入页面失败：" + target.getAbsolutePath();
            }
            pageFileLength = target.length();
            pageUrl = "file://" + target.getAbsolutePath();
            return null;
        } catch (Exception exc) {
            return "落盘失败：" + brief(exc);
        } finally {
            try { if (output != null) output.close(); } catch (Exception ignored) { /* 忽略 */ }
        }
    }

    /** 路径 1：正常走 AssetManager。 */
    private byte[] readAsset(String name) throws Exception {
        java.io.InputStream input = getAssets().open(name);
        try {
            return drain(input);
        } finally {
            try { input.close(); } catch (Exception ignored) { /* 忽略 */ }
        }
    }

    /**
     * 路径 2：把 APK 当一个 zip 打开，按条目名直接读。
     *
     * 为什么可行：APK 本身就是 zip，我们自己的进程一定能读到自己的安装包
     * （ApplicationInfo.sourceDir）。这条路不经过 AssetManager 的名字索引，
     * 所以"索引里有洞"也照样能拿到内容。缺点是启动时多一次 zip 解析（几十毫秒）。
     */
    private byte[] readFromApkZip(String name) throws Exception {
        String apkPath = getApplicationInfo().sourceDir;
        java.util.zip.ZipFile zip = new java.util.zip.ZipFile(apkPath);
        try {
            java.util.zip.ZipEntry entry = zip.getEntry("assets/" + name);
            if (entry == null) {
                // 有些打包方式把 assets 放在别的层级，退一步做一次包含匹配
                java.util.Enumeration<? extends java.util.zip.ZipEntry> all = zip.entries();
                while (all.hasMoreElements()) {
                    java.util.zip.ZipEntry candidate = all.nextElement();
                    if (candidate.getName().endsWith("/" + name)) {
                        entry = candidate;
                        break;
                    }
                }
            }
            if (entry == null) {
                throw new java.io.FileNotFoundException("APK 里没有 assets/" + name);
            }
            java.io.InputStream input = zip.getInputStream(entry);
            try {
                return drain(input);
            } finally {
                try { input.close(); } catch (Exception ignored) { /* 忽略 */ }
            }
        } finally {
            try { zip.close(); } catch (Exception ignored) { /* 忽略 */ }
        }
    }

    /** 路径 3：资源表索引的 raw 资源（aapt2 一定会为它建索引）。 */
    private byte[] readRawResource() throws Exception {
        int id = getResources().getIdentifier("page", "raw", getPackageName());
        if (id == 0) {
            throw new java.io.FileNotFoundException("没有 res/raw/page.html");
        }
        java.io.InputStream input = getResources().openRawResource(id);
        try {
            return drain(input);
        } finally {
            try { input.close(); } catch (Exception ignored) { /* 忽略 */ }
        }
    }

    private static byte[] drain(java.io.InputStream input) throws java.io.IOException {
        java.io.ByteArrayOutputStream buffer = new java.io.ByteArrayOutputStream();
        byte[] chunk = new byte[16384];
        int read;
        while ((read = input.read(chunk)) > 0) {
            buffer.write(chunk, 0, read);
        }
        return buffer.toByteArray();
    }

    private static String brief(Exception exc) {
        String message = exc.getMessage();
        return exc.getClass().getSimpleName() + (message == null ? "" : " " + message);
    }

    /** 出错时显示的信息页：把关键事实摊开说，省得只能看到一句 ERR_FILE_NOT_FOUND。 */
    private String buildDiagnosticPage(String problem) {
        StringBuilder sb = new StringBuilder();
        sb.append("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">")
          .append("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
          .append("<style>body{background:#0e1116;color:#e6ecf3;font:15px/1.6 sans-serif;")
          .append("padding:24px}h1{font-size:18px}code{background:#1b222c;padding:2px 6px;")
          .append("border-radius:5px;font-size:12px;word-break:break-all}")
          .append("p{color:#9aa8b8}</style></head><body>")
          .append("<h1>页面没能加载</h1><p>内置资源读取失败，请把这一屏截图发给开发者。</p>")
          .append("<p><code>").append(escapeHtml(problem)).append("</code></p>")
          .append("<p>诊断信息：</p><ul>")
          .append("<li>包名 <code>").append(getPackageName()).append("</code></li>")
          .append("<li>页面资产 <code>assets/").append(ASSET_PAGE).append("</code></li>")
          .append("<li>私有目录 <code>").append(escapeHtml(getFilesDir().getAbsolutePath()))
          .append("</code></li>")
          .append("<li>Android ").append(android.os.Build.VERSION.RELEASE)
          .append(" (API ").append(android.os.Build.VERSION.SDK_INT).append(")</li>")
          .append("<li>设备 ").append(escapeHtml(android.os.Build.MANUFACTURER)).append(" ")
          .append(escapeHtml(android.os.Build.MODEL)).append("</li>")
          .append("</ul></body></html>");
        return sb.toString();
    }

    private static String escapeHtml(String value) {
        if (value == null) return "";
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    /** 暴露给网页的原生能力：window.AndroidBridge.xxx() */
    private class Bridge {
        @JavascriptInterface
        public void scanQR() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    requestScan();
                }
            });
        }

        /** 页面加载完成后自己把启动屏收掉（也能顺带确认 JS 真的跑起来了）。 */
        @JavascriptInterface
        public void hideSplash() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    if (splash != null) {
                        splash.setVisibility(android.view.View.GONE);
                    }
                }
            });
        }

        @JavascriptInterface
        public String platform() {
            return "android";
        }

        @JavascriptInterface
        public String buildTag() {
            return BUILD_TAG;
        }
    }

    /** 扫二维码取工作台地址。相机权限在真要扫的时候才申请。 */
    private void requestScan() {
        if (!hasCameraPermission()) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_SCAN_PERM);
            return;
        }
        startActivityForResult(new Intent(this, QrScanActivity.class), REQ_SCAN);
    }

    private void deliverScanResult(String text) {
        if (web == null) return;
        String payload = text == null ? "" : text.replace("\\", "\\\\").replace("'", "\\'")
                .replace("\n", "").replace("\r", "");
        web.evaluateJavascript("window.vswQrResult && window.vswQrResult('" + payload + "')", null);
    }

    /** 外部链接（来源页、引擎识图页）交给系统浏览器，别在应用内打开。 */
    private boolean openExternally(Uri uri) {
        if (uri == null) return false;
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
        if (scheme.startsWith("http")) {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, uri));
            } catch (Exception exc) {
                toast("没有可打开该链接的应用");
            }
            return true;
        }
        return false;   // file:// / data: 之类让 WebView 自己处理
    }

    private boolean hasCameraPermission() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.M
                || checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
    }

    // ------------------------------------------------------------- 回调
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE) {
            Uri[] result = null;
            if (resultCode == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    int count = data.getClipData().getItemCount();
                    List<Uri> uris = new ArrayList<>();
                    for (int i = 0; i < count; i++) {
                        uris.add(data.getClipData().getItemAt(i).getUri());
                    }
                    result = uris.toArray(new Uri[0]);
                } else if (data.getData() != null) {
                    result = new Uri[]{data.getData()};
                }
            }
            if (fileCallback != null) {
                fileCallback.onReceiveValue(result);
                fileCallback = null;
            }
            return;
        }
        if (requestCode == REQ_SCAN) {
            String text = (resultCode == RESULT_OK && data != null)
                    ? data.getStringExtra(QrScanActivity.EXTRA_RESULT) : null;
            if (text == null || text.isEmpty()) {
                toast("没扫到内容，可以直接手填地址");
                deliverScanResult("");
            } else {
                deliverScanResult(text);
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        if (requestCode == REQ_WEB_CAMERA && pendingWebPermission != null) {
            boolean granted = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
            if (granted) {
                pendingWebPermission.grant(pendingWebPermission.getResources());
            } else {
                pendingWebPermission.deny();
                toast("没有相机权限，可以改用「从相册选」");
            }
            pendingWebPermission = null;
            return;
        }
        if (requestCode == REQ_SCAN_PERM) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                startActivityForResult(new Intent(this, QrScanActivity.class), REQ_SCAN);
            } else {
                toast("没有相机权限，可以手填电脑地址");
                deliverScanResult("");
            }
            return;
        }
        super.onRequestPermissionsResult(requestCode, permissions, results);
    }

    /** 返回键：先让页面处理（关掉全屏面板 / 引导页），页面说没处理再退出应用。 */
    @Override
    public void onBackPressed() {
        if (web == null) {
            super.onBackPressed();
            return;
        }
        web.evaluateJavascript(
                "(function(){try{return window.vswHandleBack?window.vswHandleBack():false}"
                        + "catch(e){return false}})()",
                new ValueCallback<String>() {
                    @Override
                    public void onReceiveValue(String value) {
                        if (!"true".equals(value)) {
                            finish();
                        }
                    }
                });
    }

    /**
     * 给下载的文件起个名字。
     *
     * 服务器按 RFC 5987 发的头是 `filename="archive-15.zip"; filename*=UTF-8''15-%E6%89%8B...zip`，
     * 中文名在 `filename*` 里（URL 编码）。这里优先取它 —— 直接取 `filename=` 会拿到
     * 无意义的 `archive-15.zip`，用户就分不清是哪一批了。
     */
    private String guessFileName(String url, String contentDisposition, String mimeType) {
        String name = "";
        if (contentDisposition != null) {
            int star = contentDisposition.indexOf("filename*=");
            if (star >= 0) {
                String tail = contentDisposition.substring(star + "filename*=".length()).trim();
                int semi = tail.indexOf(';');
                if (semi >= 0) tail = tail.substring(0, semi);
                tail = tail.trim().replace("\"", "");
                int quote = tail.indexOf("''");
                if (quote >= 0) tail = tail.substring(quote + 2);
                try {
                    name = Uri.decode(tail);
                } catch (Exception exc) {
                    name = "";
                }
            }
            if (name.isEmpty()) {
                int at = contentDisposition.indexOf("filename=");
                if (at >= 0) {
                    name = contentDisposition.substring(at + "filename=".length()).trim();
                    int semi = name.indexOf(';');
                    if (semi >= 0) name = name.substring(0, semi);
                    name = name.replace("\"", "").trim();
                }
            }
        }
        if (name.isEmpty() && url != null) {
            int slash = url.lastIndexOf('/');
            if (slash >= 0 && slash + 1 < url.length()) {
                name = Uri.decode(url.substring(slash + 1));
                int q = name.indexOf('?');
                if (q >= 0) name = name.substring(0, q);
            }
        }
        if (name.isEmpty()) name = "vsw-download";
        // 去掉文件名里不能出现在外置存储上的字符
        name = name.replaceAll("[\\\\/:*?\"<>|]", "_");
        if (!name.contains(".")) {
            if ("application/zip".equals(mimeType)) name += ".zip";
            else if ("image/jpeg".equals(mimeType)) name += ".jpg";
            else name += ".bin";
        }
        return name;
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }
}
