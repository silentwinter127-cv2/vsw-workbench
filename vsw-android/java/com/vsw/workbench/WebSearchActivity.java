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

import java.io.File;
import java.io.FileOutputStream;
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

    private WebView web;
    private TextView hint;
    private String engine = "";
    private String extractJs = "";
    private File imageFile;
    private Uri imageUri;

    private final List<String> captured = new ArrayList<String>();
    private final Handler handler = new Handler();
    private boolean finished = false;
    private boolean offered = false;
    private long startAt = 0;
    private String lastExtract = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        engine = safe(getIntent().getStringExtra(EXTRA_ENGINE));
        String label = safe(getIntent().getStringExtra(EXTRA_LABEL));
        String url = safe(getIntent().getStringExtra(EXTRA_URL));
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
                    finishWith(lastExtract, "等太久了，先把手上的结果给你");
                }
            }
        }, LIMIT_MS + 5000);
    }

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
        if (finished || offered) return;
        String js = "(function(){"
                + "var out={input:null,btn:null};"
                + "function rect(e){if(!e)return null;var r=e.getBoundingClientRect();"
                + "if(!r||r.width<16||r.height<10)return null;"
                + "return {x:r.left+r.width/2,y:r.top+r.height/2};}"
                + "var inp=document.querySelector('input[type=file]');"
                + "out.input=rect(inp);"
                + "var re=/上传|选择图片|点击上传|上传图片|拖拽|选图/;"
                + "var els=document.querySelectorAll('button,a,label,div,span');"
                + "for(var i=0;i<els.length;i++){var e=els[i];var t=(e.innerText||'').trim();"
                + "if(!t||t.length>24||!re.test(t))continue;var r=rect(e);"
                + "if(!r)continue;out.btn=r;break;}"
                + "return JSON.stringify(out);})()";
        web.evaluateJavascript(js, new ValueCallback<String>() {
            @Override
            public void onReceiveValue(String raw) {
                JSONObject pick = jsonObject(unquote(raw));
                JSONObject point = pick == null ? null : pick.optJSONObject("btn");
                if (point == null && pick != null) point = pick.optJSONObject("input");
                if (point == null) {
                    setHint("没找到上传按钮 —— 点页面上「上传图片」那一块，图已经替你选好了");
                    return;
                }
                tap(point.optDouble("x", 0), point.optDouble("y", 0));
                handler.postDelayed(new Runnable() {
                    @Override
                    public void run() {
                        if (!finished && !offered) {
                            setHint("点一下页面上「上传图片」那一块，图已经替你选好了");
                        }
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
            MotionEvent down = MotionEvent.obtain(now, now, MotionEvent.ACTION_DOWN, x, y, 0);
            MotionEvent up = MotionEvent.obtain(now, now + 40, MotionEvent.ACTION_UP, x, y, 0);
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
                        finishWith(text, null);
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
