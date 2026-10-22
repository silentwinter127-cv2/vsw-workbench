/**
 * APK 内置页面的行为验证（没有真机时能做到的最接近"装完点开"的检查）。
 *
 * 安卓外壳用 file:///android_asset/... 装载网页，于是有两个**必须先证实**的问题：
 *   (a) file:// 页面能不能 fetch http://<局域网IP>   —— 不能就连不上工作台
 *   (b) file:// 源能不能用 IndexedDB               —— 不能就存不下离线库
 * 另外还要确认：页面真的跑起来了、首次引导会弹出、快照字段能被写入本地库。
 *
 * 桌面 Chromium 与 Android WebView 同用 Blink，行为足够接近；`--allow-file-access-from-files`
 * 对齐 WebView 对 file:// 的宽松策略。所以这里得到的结论对 APK 有效 —— 但**不等于**真机验证，
 * 报告里会明确标出来。
 *
 * 用法：
 *   node vsw-probe-apk-page.js --apk ../vsw-workbench.apk --origin http://192.168.1.46:8799
 *   node vsw-probe-apk-page.js --page file:///C:/path/index.html
 */
'use strict';
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseArgs() {
  const out = { apk: '', page: '', origin: 'http://127.0.0.1:8799', keep: false };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (key === '--apk') out.apk = argv[++i];
    else if (key === '--page') out.page = argv[++i];
    else if (key === '--origin') out.origin = argv[++i];
    else if (key === '--keep') out.keep = true;
  }
  return out;
}

function findBrowser() {
  const pf = process.env.ProgramFiles || 'C:\\Program Files';
  const pf86 = process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)';
  const local = process.env.LOCALAPPDATA || '';
  for (const p of [
    path.join(pf86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    path.join(pf, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    local ? path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe') : '',
  ].filter(Boolean)) {
    try { if (fs.existsSync(p) && fs.statSync(p).size > 1024) return p; } catch (e) { /* next */ }
  }
  throw new Error('找不到 Chromium 内核浏览器（Edge 或 Chrome）');
}

/** 从 APK 里取出内置页面 —— 验的是真正打进包的字节，而不是源码目录里的那份。 */
function extractPageFromApk(apk) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-apk-page-'));
  const target = path.join(dir, 'index.html');
  const script = [
    'import zipfile,sys',
    `z=zipfile.ZipFile(r"${apk}")`,
    'data=z.read("assets/mobile/index.html")',
    `open(r"${target}","wb").write(data)`,
    'print(len(data))',
  ].join(';');
  const out = execFileSync('python', ['-c', script], { encoding: 'utf8' });
  return { html: target, bytes: parseInt(out.trim(), 10), dir };
}

class Cdp {
  constructor(ws) { this.ws = ws; this.seq = 0; this.pending = new Map(); this.errors = []; this.logs = []; }

  static async attach(port) {
    let version = null;
    for (let i = 0; i < 60 && !version; i++) {
      await sleep(300);
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/version`);
        if (r.ok) version = await r.json();
      } catch (e) { /* not up yet */ }
    }
    if (!version) throw new Error('调试端口没起来');
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const target = list.find((t) => t.type === 'page');
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    const cdp = new Cdp(ws);
    cdp.browser = version.Browser;
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && cdp.pending.has(msg.id)) { cdp.pending.get(msg.id)(msg); cdp.pending.delete(msg.id); return; }
      if (msg.method === 'Runtime.exceptionThrown') {
        const d = msg.params.exceptionDetails;
        cdp.errors.push(d.exception ? String(d.exception.description || d.exception.value) : d.text);
      }
      if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
        cdp.logs.push(msg.params.args.map((a) => a.value || a.description || '').join(' '));
      }
    });
    await new Promise((r) => ws.addEventListener('open', r));
    await cdp.send('Runtime.enable');
    await cdp.send('Page.enable');
    return cdp;
  }

  send(method, params) {
    return new Promise((resolve) => {
      const id = ++this.seq;
      this.pending.set(id, resolve);
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
    });
  }

  async evaluate(expression) {
    const res = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (res.result && res.result.exceptionDetails) return { __error: res.result.exceptionDetails.text };
    return res.result && res.result.result ? res.result.result.value : null;
  }
}

async function main() {
  const args = parseArgs();
  if (!args.apk && !args.page) {
    console.error('要么给 --apk <路径>，要么给 --page <file:// 路径>');
    process.exit(2);
  }

  let pageUrl = args.page;
  let extracted = null;
  if (args.apk) {
    extracted = extractPageFromApk(path.resolve(args.apk));
    pageUrl = 'file:///' + extracted.html.replace(/\\/g, '/');
  }

  const exe = findBrowser();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-apk-probe-'));
  const port = 9550 + Math.floor(Math.random() * 90);
  const child = spawn(exe, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--allow-file-access-from-files',
  ], { stdio: 'ignore' });

  const results = [];
  const check = (label, ok, detail) => {
    results.push({ label, ok: !!ok, detail: detail || '' });
    console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}${!ok && detail ? ' :: ' + detail : ''}`);
  };

  try {
    const cdp = await Cdp.attach(port);
    console.log(`内核 ${cdp.browser} · 页面 ${pageUrl}`);
    console.log(`内置页面 ${extracted ? extracted.bytes : '?'} 字节（从 APK 中解出）\n`);

    await cdp.send('Page.navigate', { url: pageUrl });
    await sleep(3000);

    const boot = await cdp.evaluate(`(() => {
      const q = (s) => document.querySelector(s);
      return {
        title: document.title,
        booted: !!(window.vswHandleBack && window.vswQrResult),
        introVisible: !!(q('#intro') && !q('#intro').classList.contains('hide')),
        introServer: !!q('#introServer'), introImport: !!q('#introImport'),
        navButtons: document.querySelectorAll('nav button').length,
        netPill: q('#netPill') ? q('#netPill').textContent : '',
        swPill: q('#swPill') ? q('#swPill').textContent : '',
        protocol: location.protocol,
      };
    })()`);
    check('页面加载且脚本跑起来（boot 完成）', boot && boot.booted, JSON.stringify(boot));
    check('首次启动引导会弹出（否则对方看到空白页）', boot && boot.introVisible);
    check('引导页有「填地址」和「导入数据集」两条路',
          boot && boot.introServer && boot.introImport);
    check('五个标签页都在', boot && boot.navButtons === 5, String(boot && boot.navButtons));
    check('file:// 下不依赖 service worker（如实标注）',
          boot && /内置|不可用/.test(boot.swPill || ''), boot && boot.swPill);
    check('无 JS 异常', cdp.errors.length === 0, cdp.errors.slice(0, 2).join(' | '));

    const net = await cdp.evaluate(`(async () => {
      const out = {};
      try {
        const res = await fetch(${JSON.stringify(args.origin)} + '/api/health', { cache: 'no-store' });
        const health = await res.json();
        out.health = { ok: res.ok, findings: health.stats && health.stats.findings };
      } catch (e) { out.health = { ok: false, error: String(e) }; }
      out.xhr = await new Promise((resolve) => {
        try {
          const x = new XMLHttpRequest();
          x.open('GET', ${JSON.stringify(args.origin)} + '/api/health', true);
          x.onload = () => resolve({ ok: true, status: x.status });
          x.onerror = () => resolve({ ok: false, error: 'xhr error' });
          x.send();
        } catch (e) { resolve({ ok: false, error: String(e) }); }
      });
      try {
        const res = await fetch(${JSON.stringify(args.origin)} + '/api/mobile/snapshot?images=none&limit=3&trash=0', { cache: 'no-store' });
        const snap = await res.json();
        out.snapshot = { ok: res.ok, counts: snap.counts };
        const db = await new Promise((resolve, reject) => {
          const req = indexedDB.open('vsw-mobile');   // 不写死版本：应用会自己迁移到最新版
          req.onsuccess = () => resolve(req.result);
          req.onerror = () => reject(req.error);
        });
        out.stores = Array.from(db.objectStoreNames);
        await new Promise((resolve) => {
          const tx = db.transaction('findings', 'readwrite');
          (snap.findings || []).forEach((row) => tx.objectStore('findings').put(row));
          tx.oncomplete = resolve; tx.onerror = resolve;
        });
        out.written = await new Promise((resolve) => {
          const req = db.transaction('findings').objectStore('findings').count();
          req.onsuccess = () => resolve(req.result);
          req.onerror = () => resolve(-1);
        });
        db.close();
      } catch (e) { out.snapshot = { ok: false, error: String(e) }; }
      return out;
    })()`);

    const sameOrigin = /^https?:/i.test(new URL(args.origin).protocol);
    check('file:// 页面能 fetch http://（否则连不上工作台）',
          net && net.health && net.health.ok, JSON.stringify(net && net.health));
    check('XHR 通道也能用（有些 WebView 只放行一种）',
          net && net.xhr && net.xhr.ok, JSON.stringify(net && net.xhr));
    check('file:// 源能写 IndexedDB（否则存不下离线库）',
          net && Array.isArray(net.stores) && net.stores.length >= 8, JSON.stringify(net && net.stores));
    check('整库结构已建好（findings/assets/pending…）',
          net && net.stores && net.stores.includes('assets') && net.stores.includes('pending'));
    check('快照接口可用且字段完整',
          net && net.snapshot && net.snapshot.ok && net.snapshot.counts, JSON.stringify(net && net.snapshot));
    check('快照能写进本地库并读回',
          net && net.written >= 1, String(net && net.written));
    check('无控制台错误', cdp.logs.length === 0, cdp.logs.slice(0, 2).join(' | '));

    const failed = results.filter((r) => !r.ok);
    console.log(`\n通过 ${results.length - failed.length} 项，失败 ${failed.length} 项`);
    console.log('说明：这是桌面 Chromium（同 Blink 内核）的结果，**不能替代真机安装验证**。');
    cdp.ws.close();
    if (failed.length) process.exitCode = 1;
  } finally {
    child.kill();
    await sleep(300);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* windows lock */ }
    if (extracted && !args.keep) { try { fs.rmSync(extracted.dir, { recursive: true, force: true }); } catch (e) { /* lock */ } }
  }
}

main().catch((e) => { console.error('探针失败:', e.message); process.exit(1); });
