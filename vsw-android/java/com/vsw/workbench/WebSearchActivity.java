package com.vsw.workbench;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.SystemClock;
import android.util.Base64;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.ConsoleMessage;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * 在手机里自己跑一次「识图」。
 *
 * 为什么手机能做，而工作台之前说做不了：我之前的说法是错的。手机确实没有可以像电脑那样
 * 用 CDP 驱动的无头浏览器，但 Android WebView **本来就能给网页喂文件** ——
 * `WebChromeClient.onShowFileChooser()` 就是给网页的 `<input type=file>` 提供 Uri 的钩子，
 * 默认实现是「取消」，所以只要实现了它，网页的上传按钮就能拿到我们预先选好的图片。
 *
 * 但有一个绕不过去的限制：浏览器的文件选择器**必须由用户手势触发**（user activation）。
 * 我们在后台用 `input.click()` 打不开它。所以这里的策略是：
 *   1) 先试一次「真实触摸」—— 找到页面上「上传/选择图片」那一块，用 MotionEvent 打一次真实的
 *      按下+抬起（不是 JS 的合成事件，是输入系统里的事件），有机会触发 user activation；
 *   2) 如果两秒半内网页没来要文件，就把提示写在顶栏上，让用户自己点那一下。
 * 无论哪条路，选图这一步都被省掉了 —— 图是我们替他选好的。
 *
 * 结果页由调用方传进来的 extractJs 解析（和电脑端 cdp_driver 用的是同一份提取脚本），
 * 拿到 similar/sources 后原样回给页面。
 */
public class WebSearchActivity extends Activity {

    static final String EXTRA_ENGINE = "engine";
    static final String EXTRA_LABEL = "label";
    static final String EXTRA_URL = "url";
    static final String EXTRA_EXTRACT = "extract";
    static final String EXTRA_IMAGE = "image";     // base64（不带 data: 前缀）
    static final String EXTRA_RESULT = "result";

    private static final String SUBDIR = "websearch";
    private static final long POLL_MS = 1500;
    private static final long LIMIT_MS = 90000;
    private static final long TAP_GRACE_MS = 2500;
    /** 最多内嵌几张结果图：再多 JSON 就太大了，交付给页面的 intent 有大小上限。 */
    private static final int THUMB_MAX = 12;
    /** 内嵌图的最长边：结果面板里是小缩略图，360px 足够。 */
    private static final int THUMB_SIDE = 360;
    /** 单张下载的超时（毫秒）。引擎图不大，5 秒拿不到就跳过。 */
    private static final int THUMB_TIMEOUT_MS = 5000;
    /** 内嵌图的总字节上限：intent 里塞太多会 TransactionTooLarge。 */
    private static final int THUMB_TOTAL_MAX = 320 * 1024;
    /** 下载结果图时假装自己是引擎页面自己（防盗链要看 Referer）。 */
    private static final String UA_MOBILE =
            "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 (KHTML, like Gecko) "
                    + "Chrome/120.0.0.0 Mobile Safari/537.36";

    private WebView web;
    private TextView hint;
    private String engine = "";
    private String extractJs = "";
    /** 引擎搜索页地址：抓结果图时当 Referer 用（防盗链只认这个）。 */
    private String searchUrl = "";
    private File imageFile;
    private Uri imageUri;

    private final List<String> captured = new ArrayList<String>();
    private final Handler handler = new Handler();
    private boolean finished = false;
    private boolean offered = false;
    private long startAt = 0;
    private String lastExtract = "";
    /** 最近一次取图拿到的 HTTP 状态码（0 = 还没试过），失败时写进提示里。 */
    private int lastThumbStatus = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        engine = safe(getIntent().getStringExtra(EXTRA_ENGINE));
        String label = safe(getIntent().getStringExtra(EXTRA_LABEL));
        String url = safe(getIntent().getStringExtra(EXTRA_URL));
        searchUrl = url;
        extractJs = safe(getIntent().getStringExtra(EXTRA_EXTRACT));
        String image = safe(getIntent().getStringExtra(EXTRA_IMAGE));

        if (url.isEmpty()) {
            finishWithError("没有给引擎地址");
            return;
        }
        imageFile = writeImage(image);
        if (imageFile != null) {
            imageUri = ApkProvider.uriFor(ApkProvider.SHARE, imageFile.getName());
        }

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.parseColor("#0e1116"));

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setBackgroundColor(Color.parseColor("#151b24"));
        bar.setPadding(24, 18, 24, 18);
        bar.setGravity(android.view.Gravity.CENTER_VERTICAL);

        TextView title = new TextView(this);
        title.setText(label.isEmpty() ? "手机内搜图" : label);
        title.setTextColor(Color.parseColor("#e6edf5"));
        title.setTextSize(15f);
        title.setSingleLine(true);

        hint = new TextView(this);
        hint.setText(imageFile == null ? "图没准备好，请手动选择" : "图已经替你选好了");
        hint.setTextColor(Color.parseColor("#8fa3b8"));
        hint.setTextSize(11f);
        hint.setPadding(20, 0, 12, 0);
        hint.setSingleLine(false);
        hint.setMaxLines(2);

        Button upload = new Button(this);
        upload.setText("上传");
        upload.setAllCaps(false);
        upload.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                if (offered) {
                    setHint("图已经交给网页了，等它出结果…");
                    return;
                }
                tryAutoTap(true);
        // 百度首页的上传卡片是懒渲染的：太早点等于点在空气上。
        // 之后两次只认 input[type=file]，找到就点，找不到就等着（不乱点）。
        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                tryAutoTap(false);
            }
        }, 2500);
        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                tryAutoTap(false);
            }
        }, 6000);
            }
        });

        Button close = new Button(this);
        close.setText("关闭");
        close.setAllCaps(false);
        close.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                finishWithError("你关掉了搜索页");
            }
        });

        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        titles.addView(title);
        titles.addView(hint);
        bar.addView(titles, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        bar.addView(upload);
        bar.addView(close);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#ffffff"));

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setTextZoom(100);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                remember(request == null || request.getUrl() == null ? "" : request.getUrl().toString());
                return null;
            }

            @Override
            @SuppressWarnings("deprecation")
            public WebResourceResponse shouldInterceptRequest(WebView view, String url) {
                remember(url);
                return null;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (startAt == 0) {
                    startAt = System.currentTimeMillis();
                    handler.postDelayed(new Runnable() {
                        @Override
                        public void run() {
                            tryAutoTap();
                        }
                    }, 1200);
                }
                handler.postDelayed(pollTask, 1500);
                installTapHelper(view);
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                offered = true;
                if (imageUri == null) {
                    // 没有预选图就交回去，让系统选择器出现（宁可用户自己选，也不要卡死）
                    return false;
                }
                callback.onReceiveValue(new Uri[]{imageUri});
                setHint("图已交给网页，等它的结果…");
                web.evaluateJavascript(TAP_HELPER_OFFERED, null);
                return true;
            }

            @Override
            public boolean onConsoleMessage(ConsoleMessage message) {
                android.util.Log.i("vsw-websearch", message.message() + " @" + message.lineNumber());
                return true;
            }
        });

        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(web, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);

        web.loadUrl(url);
        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (!finished && System.currentTimeMillis() - startAt > LIMIT_MS) {
                    finishWithThumbs(lastExtract, "等太久了，先把手上的结果给你");
                }
            }
        }, LIMIT_MS + 5000);
    }

    /**
     * 页底浮一个「点这里：把图交给识图」的按钮。
     *
     * 为什么非要有这个：Chromium 只认真实用户手势触发的文件选择器。我们从原生
     * dispatchTouchEvent 打进去的合成触摸在 Xiaomi 15 / HyperOS 上拿不到 user activation
     * （真机证据：诊断行一直 `pcsimi 请求 0 条`），所以自动点那一下不可靠。
     * 但**用户真的点一下按钮**时，那次点击就是激活状态 —— 在同一个事件里调
     * input.click() 就能正常拉起选择器，我们的 onShowFileChooser 再把预选好的图交回去，
     * 用户根本看不到系统选图框。这一步把「必须人手一下」压缩成「点一个按钮」。
     */
    private void installTapHelper(WebView view) {
        view.evaluateJavascript(TAP_HELPER_JS + TAP_HELPER_JS2, null);
    }

    private static final String TAP_HELPER_OFFERED =
            "(function(){var b=document.getElementById('vsw-cta');if(b){b.innerHTML="
            + "'✓ 图已交给网页，等结果…（可以关掉这个页面等它返回）';b.style.background='#1f7a4d';}return 1;})()";

    private static final String TAP_HELPER_JS =
            "(function(){"
            + "if(document.getElementById('vsw-cta'))return 'exists';"
            + "var b=document.createElement('button');"
            + "b.id='vsw-cta';"
            + "b.type='button';"
            + "b.textContent='点这里：把这张图交给识图';"
            + "b.style.cssText='position:fixed;left:12px;right:12px;bottom:16px;z-index:2147483647;'"
            + "+'padding:16px 12px;border:0;border-radius:14px;font-size:16px;font-weight:600;'"
            + "+'color:#fff;background:#2f6fed;box-shadow:0 6px 20px rgba(0,0,0,.45);'"
            + "+'font-family:system-ui,sans-serif;'"
            + "if(window.__vswOffered){b.textContent='✓ 图已交给网页，等结果…（没出结果就再点我一次）';"
            + "b.style.background='#1f7a4d';}";
    // 注意：上面字符串故意断在这里，下面补事件绑定（分开拼，便于阅读）
    private static final String TAP_HELPER_JS2 =
            "b.onclick=function(ev){"
            + "ev.stopPropagation();"
            + "var i=document.querySelector('input[type=file]');"
            + "if(!i){"
            + "var re=/上传|选择图片|点击上传|上传图片|拖拽|选图/;"
            + "var els=document.querySelectorAll('button,a,label,div,span');"
            + "for(var k=0;k<els.length;k++){var t=(els[k].innerText||'').trim();"
            + "if(t&&t.length<=24&&re.test(t)){try{els[k].click();}catch(e){}"
            + "i=document.querySelector('input[type=file]');if(i)break;}}"
            + "}"
            + "if(i){"
            + "try{i.style.display='block';i.style.visibility='visible';i.style.opacity='1';}catch(e){}"
            + "try{i.click();}catch(e){}"
            + "b.textContent='已经点了上传，等选图框…（如果没反应就再点一次）';"
            + "}else{"
            + "b.textContent='还没找到上传控件 —— 稍等一下再点我';"
            + "}"
            + "};"
            + "document.body.appendChild(b);"
            + "return 'ok';})()";

    // ------------------------------------------------------------------ 页面交互

    /** 记下页面自己发的请求：百度的结果轮播是 /ajax/pcsimi 喂的，提取脚本要回放它。 */
    private void remember(String url) {
        if (url == null || url.isEmpty()) return;
        if (url.contains("/ajax/pcsimi") || url.contains("/ajax/pcsites")) {
            synchronized (captured) {
                if (!captured.contains(url) && captured.size() < 20) captured.add(url);
            }
        }
    }

    /**
     * 找到「上传/选择图片」那一块，打一次真实触摸。
     *
     * 合成事件（element.click()）拿不到 user activation，文件选择器不会出现；
     * 从输入系统打进去的 MotionEvent 才有机会。找不到就提示用户自己点。
     */
    private void tryAutoTap() {
        tryAutoTap(true);
    }

    /**
     * @param allowText 只允许第一次去猜「上传图片」文字块（点它才会把 input 渲染出来）；
     *                  之后的重试只认 input[type=file] 本身，宁可不点也不乱点 ——
     *                  r28 就是因为放宽了关键词、还连点好几轮，点到了页面上别的东西。
     */
    private void tryAutoTap(final boolean allowText) {
        if (finished || offered) return;
        String js = "(function(){"
                + "var out={input:null,btn:null};"
                + "function rect(e){if(!e)return null;var r=e.getBoundingClientRect();"
                + "if(!r||r.width<16||r.height<10)return null;"
                + "return {x:r.left+r.width/2,y:r.top+r.height/2};}"
                + "function seen(e){if(!e)return;try{e.scrollIntoView({block:'center'});}catch(x){}}"
                // 有 input[type=file] 就点它本身 —— 别去猜文字按钮。
                + "var inp=document.querySelector('input[type=file]');"
                + "if(inp){seen(inp);out.input=rect(inp);}"
                + "if(!out.input&&ALLOW_TEXT){"
                + "var re=/上传|选择图片|点击上传|上传图片|拖拽|选图/;"
                + "var els=document.querySelectorAll('button,a,label,div,span');"
                + "for(var i=0;i<els.length;i++){var e=els[i];var t=(e.innerText||'').trim();"
                + "if(!t||t.length>24||!re.test(t))continue;var r=rect(e);"
                + "if(!r)continue;seen(e);out.btn=rect(e)||r;break;}}"
                + "return JSON.stringify(out);})()";
        js = js.replace("ALLOW_TEXT", allowText ? "true" : "false");
        web.evaluateJavascript(js, new ValueCallback<String>() {
            @Override
            public void onReceiveValue(String raw) {
                JSONObject pick = jsonObject(unquote(raw));
                JSONObject inputPoint = pick == null ? null : pick.optJSONObject("input");
                JSONObject textPoint = pick == null ? null : pick.optJSONObject("btn");
                final boolean viaText = (inputPoint == null && textPoint != null);
                JSONObject point = inputPoint != null ? inputPoint : textPoint;
                if (point == null) {
                    setHint("没找到上传按钮 —— 点页面上「上传图片」那一块，图已经替你选好了");
                    return;
                }
                setHint(viaText ? "点了「上传图片」，等选图框…" : "点了上传框，等选图框…");
                tap(point.optDouble("x", 0), point.optDouble("y", 0));
                handler.postDelayed(new Runnable() {
                    @Override
                    public void run() {
                        if (finished || offered) return;
                        // 只在"点了文字块"的情况下补点一次 input：文字块点完 input 才出现
                        if (viaText) {
                            web.evaluateJavascript(
                                    "(function(){var i=document.querySelector('input[type=file]');"
                                            + "if(!i)return '';try{i.scrollIntoView({block:'center'});}catch(x){}"
                                            + "var r=i.getBoundingClientRect();"
                                            + "return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2});})()",
                                    new ValueCallback<String>() {
                                        @Override
                                        public void onReceiveValue(String again) {
                                            if (finished || offered) return;
                                            JSONObject p2 = jsonObject(unquote(again));
                                            if (p2 != null) tap(p2.optDouble("x", 0), p2.optDouble("y", 0));
                                        }
                                    });
                            return;
                        }
                        setHint("还没打开选图 —— 点顶栏「上传」再试，或直接点页面上「上传图片」");
                    }
                }, TAP_GRACE_MS);
            }
        });
    }

    /** 在 WebView 上派发一次真实的按下 + 抬起。 */
    private void tap(double cssX, double cssY) {
        try {
            float scale = web.getScale();
            if (scale <= 0) scale = 1f;
            float x = (float) (cssX * scale);
            float y = (float) (cssY * scale);
            long now = SystemClock.uptimeMillis();
            web.requestFocusFromTouch();
            MotionEvent down = MotionEvent.obtain(now, now, MotionEvent.ACTION_DOWN, x, y, 0);
            down.setSource(android.view.InputDevice.SOURCE_TOUCHSCREEN);
            MotionEvent up = MotionEvent.obtain(now, now + 60, MotionEvent.ACTION_UP, x, y, 0);
            up.setSource(android.view.InputDevice.SOURCE_TOUCHSCREEN);
            web.dispatchTouchEvent(down);
            web.dispatchTouchEvent(up);
            down.recycle();
            up.recycle();
        } catch (Exception exc) {
            android.util.Log.w("vsw-websearch", "tap failed: " + exc);
        }
    }

    /** 每 1.5 秒跑一次提取脚本，够了就把结果交回去。 */
    private final Runnable pollTask = new Runnable() {
        @Override
        public void run() {
            if (finished) return;
            String capturedJson;
            synchronized (captured) {
                StringBuilder sb = new StringBuilder("[");
                for (int i = 0; i < captured.size(); i++) {
                    if (i > 0) sb.append(',');
                    sb.append(JSONObject.quote(captured.get(i)));
                }
                sb.append(']');
                capturedJson = sb.toString();
            }
            String expr = "(function(){window.__vswCaptured=" + capturedJson + ";"
                    + "try{var p=(" + extractJs + ");"
                    + "if(p&&typeof p.then==='function'){p.then(function(s){window.__vswExtract=s;})"
                    + ".catch(function(e){window.__vswExtract=JSON.stringify({error:String(e)});});}"
                    + "else{window.__vswExtract=p;}}"
                    + "catch(e){window.__vswExtract=JSON.stringify({error:String(e)});}"
                    + "return window.__vswExtract||'';})()";
            web.evaluateJavascript(expr, new ValueCallback<String>() {
                @Override
                public void onReceiveValue(String raw) {
                    String text = unquote(raw);
                    if (text != null && text.length() > 2) lastExtract = text;
                    JSONObject obj = jsonObject(text);
                    int similar = 0;
                    JSONArray list = obj == null ? null : obj.optJSONArray("similar");
                    if (list != null) similar = list.length();
                    if (similar >= 4) {
                        // 命中就先别急着回去：结果图在引擎那边有防盗链，页面直接 <img src> 会 403，
                        // 表现就是「结果图全部消失」。这里用带 Referer 的原生请求抓下来，
                        // 缩成 360px 的 JPEG 内嵌成 data URL，页面就一定能画出来。
                        finishWithThumbs(text, null);
                        return;
                    }
                    if (!finished) handler.postDelayed(pollTask, POLL_MS);
                }
            });
        }
    };

    // ------------------------------------------------------------------ 收尾

    private void setHint(final String text) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (hint != null) hint.setText(text);
            }
        });
    }

    private void finishWith(String json, String note) {
        if (finished) return;
        finished = true;
        handler.removeCallbacksAndMessages(null);
        String payload = json == null || json.trim().isEmpty() ? "{}" : json.trim();
        if (note != null) payload = mergeNote(payload, note);
        Intent data = new Intent();
        data.putExtra(EXTRA_RESULT, payload);
        setResult(RESULT_OK, data);
        finish();
    }

    private void finishWithError(String message) {
        if (finished) finish();
        finished = true;
        handler.removeCallbacksAndMessages(null);
        Intent data = new Intent();
        data.putExtra(EXTRA_RESULT, "{\"error\":" + JSONObject.quote(message) + "}");
        setResult(RESULT_OK, data);
        finish();
    }

    /**
     * 把结果图抓下来内嵌成 data URL，然后再交回页面。
     *
     * 为什么非要在原生做：百度/360 的结果图有防盗链，`<img src="https://t7.baidu.com/...">`
     * 从我们自己的页面（file:// 或局域网地址）发出去没有 Referer，服务端回 403，
     * 于是**结果图一张都画不出来** —— 用户看到的就是「结果图全部消失」。
     * 在 WebView 里用 fetch 也不行（跨域、而且一样缺 Referer）。
     * 所以这里用 HttpURLConnection 带上引擎页面的 Referer 去拿，缩到 360px 后压成 JPEG。
     */
    private void finishWithThumbs(final String json, final String note) {
        if (finished) return;
        final JSONObject obj = jsonObject(json);
        if (obj == null) {
            finishWith(json, note);
            return;
        }
        setHint("拿到了，正在把结果图抓成本地小图…");
        new Thread(new Runnable() {
            @Override
            public void run() {
                int got = 0;
                int want = 0;
                int bytes = 0;
                lastThumbStatus = 0;
                try {
                    JSONArray list = obj.optJSONArray("similar");
                    if (list != null) {
                        for (int i = 0; i < list.length() && got < THUMB_MAX; i++) {
                            JSONObject item = list.optJSONObject(i);
                            if (item == null) continue;
                            String url = safe(item.optString("u", ""));
                            if (url.isEmpty() || url.startsWith("data:")) continue;
                            if (bytes > THUMB_TOTAL_MAX) break;      // 太大的就不带了，intent 会扛不住
                            want += 1;
                            String data = fetchThumb(url);
                            if (data == null) continue;
                            item.put("d", data);
                            got += 1;
                            bytes += data.length();
                        }
                    }
                } catch (Throwable exc) {
                    android.util.Log.w("vsw-websearch", "finishWithThumbs: " + exc);
                }

                String extra = null;
                int similarCount = 0;
                JSONArray simList = obj.optJSONArray("similar");
                if (simList != null) similarCount = simList.length();
                if (similarCount == 0) {
                    // 一张都没抓到：把提取脚本自己的诊断信息带回去，
                    // 否则用户只看到一句「没抓到图」，我们这边也无从下手。
                    extra = "没抓到相似图（诊断：" + diagnose(obj.optJSONObject("diag")) + "）";
                } else if (got > 0) {
                    extra = "结果图 " + got + "/" + want + " 张已抓成本地小图";
                } else if (want > 0) {
                    extra = "结果图 0/" + want + " 张取回来"
                            + (lastThumbStatus > 0 ? "（最后一张回 HTTP " + lastThumbStatus + "）" : "")
                            + "，可以点「复制图片链接」自己在浏览器里看";
                }
                final String enriched = obj.toString();
                final String merged = extra == null ? note : (note == null ? extra : note + " · " + extra);
                handler.post(new Runnable() {
                    @Override
                    public void run() {
                        finishWith(enriched, merged);
                    }
                });
            }
        }, "vsw-thumb-fetch").start();
    }

    /** 把提取脚本的 diag 读成一句人话：捕获到几条 pcsimi、页面里都有哪些图主机。 */
    private static String diagnose(JSONObject diag) {
        if (diag == null) return "提取脚本没给诊断";
        StringBuilder sb = new StringBuilder();
        sb.append("pcsimi 请求 ").append(diag.optInt("pcsimi", 0)).append(" 条");
        if (diag.has("viaPcsimi")) sb.append("、走接口拿到 ").append(diag.optInt("viaPcsimi", 0)).append(" 张");
        JSONObject hosts = diag.optJSONObject("hosts");
        if (hosts != null && hosts.length() > 0) {
            sb.append("、页面图片主机 ");
            int shown = 0;
            java.util.Iterator<String> it = hosts.keys();
            while (it.hasNext() && shown < 3) {
                String host = it.next();
                sb.append(host).append("×").append(hosts.optInt(host, 0)).append(" ");
                shown += 1;
            }
        } else {
            sb.append("、页面里没读到图");
        }
        return sb.toString().trim();
    }

    /** 抓一张结果图，缩到 360px 压成 JPEG，返回 `data:image/jpeg;base64,…`；失败返回 null。 */
    private String fetchThumb(String url) {
        // 防盗链的规则各站不同：先拿引擎页的完整地址试，被拒就退回站点根，
        // 最后兜底 graph.baidu.com（360 的图对 Referer 宽松些，多试一次不花钱）。
        String[] referers = new String[]{searchUrl, originOf(searchUrl), "https://graph.baidu.com/"};
        for (int i = 0; i < referers.length; i++) {
            String referer = referers[i];
            if (referer == null || referer.isEmpty()) continue;
            int status = fetchThumbWith(url, referer);
            if (status == 200) return fetchThumbBytes(url, referer);
            lastThumbStatus = status;
            if (status == 404 || status == 410) return null;   // 图本身没了，换 Referer 也没用
        }
        return null;
    }

    /** 只问状态码，不下载（换 Referer 重试前先确认能通）。 */
    private int fetchThumbWith(String url, String referer) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(THUMB_TIMEOUT_MS);
            conn.setReadTimeout(THUMB_TIMEOUT_MS);
            conn.setInstanceFollowRedirects(true);
            conn.setRequestProperty("User-Agent", UA_MOBILE);
            conn.setRequestProperty("Referer", referer);
            conn.setRequestProperty("Accept", "image/*,*/*;q=0.8");
            return conn.getResponseCode();
        } catch (Throwable exc) {
            return -1;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static String originOf(String url) {
        if (url == null) return "";
        try {
            java.net.URI uri = new java.net.URI(url);
            return uri.getScheme() + "://" + uri.getHost() + "/";
        } catch (Exception exc) {
            return "";
        }
    }

    private String fetchThumbBytes(String url, String referer) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(THUMB_TIMEOUT_MS);
            conn.setReadTimeout(THUMB_TIMEOUT_MS);
            conn.setInstanceFollowRedirects(true);
            conn.setRequestProperty("User-Agent", UA_MOBILE);
            conn.setRequestProperty("Referer", referer);
            conn.setRequestProperty("Accept", "image/*,*/*;q=0.8");
            if (conn.getResponseCode() != 200) return null;

            InputStream in = conn.getInputStream();
            ByteArrayOutputStream buffer = new ByteArrayOutputStream();
            byte[] chunk = new byte[16 * 1024];
            int read;
            int total = 0;
            while ((read = in.read(chunk)) > 0) {
                total += read;
                if (total > 6 * 1024 * 1024) break;      // 单张上限 6 MB，防跑飞
                buffer.write(chunk, 0, read);
            }
            in.close();
            byte[] raw = buffer.toByteArray();

            // 先读尺寸再抽样解码：结果图有些是 4K 截图，直接解会吃内存
            android.graphics.BitmapFactory.Options bounds = new android.graphics.BitmapFactory.Options();
            bounds.inJustDecodeBounds = true;
            android.graphics.BitmapFactory.decodeByteArray(raw, 0, raw.length, bounds);
            int sample = 1;
            int longest = Math.max(bounds.outWidth, bounds.outHeight);
            while (longest / sample > THUMB_SIDE * 2) sample *= 2;

            android.graphics.BitmapFactory.Options opts = new android.graphics.BitmapFactory.Options();
            opts.inSampleSize = sample;
            android.graphics.Bitmap bitmap = android.graphics.BitmapFactory.decodeByteArray(raw, 0, raw.length, opts);
            if (bitmap == null) return null;
            try {
                int w = bitmap.getWidth();
                int h = bitmap.getHeight();
                int side = Math.max(w, h);
                android.graphics.Bitmap out = bitmap;
                if (side > THUMB_SIDE) {
                    float scale = (float) THUMB_SIDE / side;
                    out = android.graphics.Bitmap.createScaledBitmap(bitmap,
                            Math.max(1, Math.round(w * scale)), Math.max(1, Math.round(h * scale)), true);
                }
                ByteArrayOutputStream jpeg = new ByteArrayOutputStream();
                out.compress(android.graphics.Bitmap.CompressFormat.JPEG, 72, jpeg);
                if (out != bitmap) out.recycle();
                byte[] bytes = jpeg.toByteArray();
                return "data:image/jpeg;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP);
            } finally {
                bitmap.recycle();
            }
        } catch (Throwable exc) {
            android.util.Log.w("vsw-websearch", "fetchThumb " + url + ": " + exc);
            return null;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /** 把一句提示塞进结果 JSON，页面能显示「为什么先给了你这些」。 */
    private static String mergeNote(String json, String note) {
        try {
            JSONObject obj = new JSONObject(json);
            obj.put("note", note);
            return obj.toString();
        } catch (Exception exc) {
            return "{\"error\":" + JSONObject.quote(note) + "}";
        }
    }

    @Override
    public void onBackPressed() {
        finishWithError("你返回了，这次搜索没跑完");
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (web != null) {
            try {
                web.stopLoading();
                web.destroy();
            } catch (Exception ignored) {
            }
            web = null;
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------------ 小工具

    /** 把 base64 写成缓存文件（Provider 才能按名字发它）。 */
    private File writeImage(String base64) {
        if (base64 == null || base64.isEmpty()) return null;
        try {
            String body = base64;
            int comma = body.indexOf(',');
            if (comma >= 0) body = body.substring(comma + 1);   // 容忍 data:image/...;base64,
            byte[] bytes = Base64.decode(body, Base64.DEFAULT);
            if (bytes == null || bytes.length < 32) return null;
            File dir = new File(getCacheDir(), SUBDIR);
            if (!dir.isDirectory() && !dir.mkdirs()) return null;
            File file = new File(dir, "search-" + System.currentTimeMillis() + ".jpg");
            FileOutputStream out = new FileOutputStream(file);
            try {
                out.write(bytes);
                out.flush();
            } finally {
                out.close();
            }
            return file;
        } catch (Exception exc) {
            android.util.Log.w("vsw-websearch", "writeImage: " + exc);
            return null;
        }
    }

    private static String safe(String value) {
        return value == null ? "" : value;
    }

    /** evaluateJavascript 回的是 JSON 字面量：字符串会被引号包住，先还原成真字符串。 */
    private static String unquote(String raw) {
        if (raw == null) return "";
        String text = raw.trim();
        if (text.length() >= 2 && text.startsWith("\"") && text.endsWith("\"")) {
            try {
                Object value = new JSONTokener(text).nextValue();
                return value == null ? "" : String.valueOf(value);
            } catch (Exception exc) {
                return text;
            }
        }
        if ("null".equals(text)) return "";
        return text;
    }

    private static JSONObject jsonObject(String text) {
        if (text == null) return null;
        String body = text.trim();
        if (body.isEmpty() || "null".equals(body)) return null;
        try {
            return new JSONObject(body);
        } catch (Exception exc) {
            return null;
        }
    }
}
