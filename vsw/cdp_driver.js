#!/usr/bin/env node
/**
 * vsw-cdp-driver.js — headless Chromium + CDP driver for the Visual Search Workbench.
 *
 * Why this file exists (measured 2026-10-16, see vsw/engines.py):
 *   Every reverse-image-search HTTP endpoint the workbench tried was walled off:
 *     - graph.baidu.com/upload      -> {"status":1,"msg":"Reject"}
 *     - image.baidu.com?objurl=     -> captcha redirect
 *     - cn.bing.com ...&q=imgurl:.. -> silently dropped, lands on a *text* image search
 *     - bing.com/images/searchbyimage/upload -> 404 (endpoint is gone)
 *   The one path that still works is the one a human takes: open the engine's own page in
 *   a real browser and hand the file to its <input type=file> via CDP
 *   DOM.setFileInputFiles. The page then does its own signed upload.
 *
 * The same technique is what the QQAgent `image-search` skill uses (Baidu), so this driver
 * follows that proven shape and extends it to Bing/others.
 *
 * It is deliberately a *single-process, single-job* CLI: Python (the workbench) spawns it,
 * reads one JSON object from stdout, and kills it if it hangs. No long-lived state.
 *
 * Usage:  node vsw-cdp-driver.js --job <job.json>
 * Job:    {
 *          "url": "https://graph.baidu.com/pcpage/index?tpl_from=pc",
 *          "file": "C:\\...\\variant.jpg",        // optional: set into input[type=file]
 *          "selector": "input[type=file]",        // optional, default above
 *          "waitText": ["相关商品", "相似图片"],   // optional: resolve when any appears in body
 *          "waitFailText": ["上传失败"],           // optional: abort when any appears
 *          "waitMs": 30000,                        // total budget
 *          "settleMs": 4000,                       // initial wait after navigate
 *          "extractJs": "(() => ({...}))()",       // optional: evaluated, JSON.parse'd if string
 *          "dumpProbe": false,                     // include page probe (inputs/buttons/images)
 *          "browserPath": "",                      // optional explicit exe
 *          "userAgent": ""
 *        }
 * Output: one JSON object on stdout: {ok, engine, finalUrl, title, text, extract, probe, error}
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('node:child_process');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function loadWebSocket() {
  // Prefer the runtime's global WebSocket (Node >= 21). Fall back to a `ws` checkout, which
  // this machine happens to have next to the QQAgent skill.
  if (typeof globalThis.WebSocket === 'function') return globalThis.WebSocket;
  const candidates = [
    'D:/QQAgent/QQ Agent/resources/app/node_modules/ws',
    path.join(__dirname, 'node_modules', 'ws'),
  ];
  for (const c of candidates) {
    try {
      const m = require(c);
      return m.default || m.WebSocket || m;
    } catch { /* next */ }
  }
  throw new Error('没有可用的 WebSocket 实现（Node 需 >= 21，或提供 ws 包）');
}

function findBrowser(override) {
  const cands = [];
  if (override) cands.push(override);
  const pf = process.env.ProgramFiles || 'C:\\Program Files';
  const pf86 = process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)';
  const local = process.env.LOCALAPPDATA || '';
  if (process.platform === 'win32') {
    cands.push(
      path.join(pf86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
      path.join(pf, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
      path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      path.join(pf86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      local ? path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe') : '',
    );
  } else if (process.platform === 'darwin') {
    cands.push('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge');
  } else {
    cands.push('/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/microsoft-edge');
  }
  for (const p of cands.filter(Boolean)) {
    try { if (fs.existsSync(p) && fs.statSync(p).size > 1024) return p; } catch { /* next */ }
  }
  return '';
}

async function waitCdp(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return await r.json();
    } catch { /* not up yet */ }
    await sleep(300);
  }
  throw new Error('浏览器调试端口没起来');
}

async function cdpSession(wsUrl) {
  const WS = loadWebSocket();
  const sock = new WS(wsUrl);
  const on = (ev, fn) => (typeof sock.on === 'function' ? sock.on(ev, fn) : sock.addEventListener(ev, fn));
  await new Promise((res, rej) => { on('open', res); on('error', rej); });
  let id = 0;
  const pending = new Map();
  const listeners = new Map();
  on('message', (raw) => {
    let m = null;
    try { m = JSON.parse(typeof raw === 'string' ? raw : (raw.data || raw.toString())); } catch { return; }
    if (m && m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m && m.method) {
      const fns = listeners.get(m.method);
      if (fns) for (const fn of fns) { try { fn(m); } catch { /* listener errors must not kill the run */ } }
    }
  });
  const send = (method, params = {}, timeoutMs = 30000) => new Promise((resolve, reject) => {
    const i = ++id;
    pending.set(i, (m) => (m.error ? reject(new Error(`${method}: ${JSON.stringify(m.error)}`)) : resolve(m.result)));
    try { sock.send(JSON.stringify({ id: i, method, params })); } catch (e) { reject(e); return; }
    setTimeout(() => { if (pending.has(i)) { pending.delete(i); reject(new Error(`${method} 超时`)); } }, timeoutMs);
  });
  const subscribe = (method, fn) => {
    if (!listeners.has(method)) listeners.set(method, []);
    listeners.get(method).push(fn);
  };
  return { send, on: subscribe, close: () => { try { sock.close(); } catch { /* ignore */ } } };
}

/** Kill the whole Chromium process tree — killing the top process alone leaves children. */
function killTree(pid) {
  if (!pid) return;
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
    } else {
      process.kill(pid, 'SIGKILL');
    }
  } catch { /* ignore */ }
}

const PROBE_JS = `(() => {
  const abs = (u) => { try { return new URL(u, location.href).toString(); } catch { return ''; } };
  const inputs = [...document.querySelectorAll('input')].map((el) => ({
    type: el.type || '', name: el.name || '', id: el.id || '',
    accept: el.accept || '', cls: String(el.className || '').slice(0, 80),
    hidden: !!el.hidden || getComputedStyle(el).display === 'none',
  }));
  const buttons = [...document.querySelectorAll('button,a,span,div,label')]
    .map((el) => String(el.textContent || '').trim())
    .filter((t) => t && t.length <= 18)
    .slice(0, 60);
  const imgs = [...document.querySelectorAll('img')]
    .map((el) => abs(el.getAttribute('data-src') || el.currentSrc || el.src || ''))
    .filter(Boolean);
  const hosts = {};
  for (const u of imgs) { try { const h = new URL(u).host; hosts[h] = (hosts[h] || 0) + 1; } catch { /* ignore */ } }
  return JSON.stringify({
    href: location.href, title: document.title || '',
    text: (document.body ? document.body.innerText : '').slice(0, 1200),
    inputs, buttons: [...new Set(buttons)].slice(0, 40),
    imgCount: imgs.length, imgHosts: hosts,
    forms: [...document.querySelectorAll('form')].map((f) => ({ action: f.getAttribute('action') || '', method: f.method || '' })),
  });
})()`;

async function run(job) {
  const out = { ok: false, engine: job.engine || '', finalUrl: '', title: '', text: '',
    extract: null, probe: null, error: '' };
  const browser = findBrowser(job.browserPath);
  if (!browser) throw new Error('本机没找到 Edge / Chrome');
  const port = 9400 + Math.floor(Math.random() * 500);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-cdp-'));
  let child = null;
  let cdp = null;
  const close = async () => {
    try { if (cdp) { await cdp.send('Browser.close', {}, 4000).catch(() => {}); cdp.close(); } } catch { /* ignore */ }
    try { if (child && !child.killed) child.kill(); } catch { /* ignore */ }
    killTree(child && child.pid);
    await sleep(400);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
  };
  try {
    child = spawn(browser, [
      '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
      '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--disable-extensions',
      '--disable-background-networking', '--disable-features=Translate',
      '--window-size=1400,1000', 'about:blank',
    ], { stdio: 'ignore', windowsHide: true });

    await waitCdp(port, 25000);
    const tabRes = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })
      .catch(() => fetch(`http://127.0.0.1:${port}/json/new?about:blank`));
    const tab = await tabRes.json();
    cdp = await cdpSession(tab.webSocketDebuggerUrl);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('DOM.enable');
    if (job.userAgent) {
      await cdp.send('Emulation.setUserAgentOverride', { userAgent: job.userAgent }).catch(() => {});
    }
    const evaluate = async (expr, timeoutMs = 20000) => {
      const r = await cdp.send('Runtime.evaluate',
        { expression: expr, returnByValue: true, awaitPromise: true }, timeoutMs);
      return r && r.result ? r.result.value : undefined;
    };

    // Optional XHR/fetch capture. Useful when a result page renders nothing useful in its DOM
    // but clearly fetched structured data — then the API response is the real data source.
    const captured = [];
    const seenUrls = new Set();
    const captureWanted = (url) => {
      if (!job.captureFilter) return true;
      try { return new RegExp(job.captureFilter).test(url); } catch { return true; }
    };
    if (job.captureNetwork) {
      await cdp.send('Network.enable', { maxResourceBufferSize: 8 * 1024 * 1024 });
      const grabBody = async (entry, requestId, tries = 6) => {
        for (let i = 0; i < tries; i += 1) {
          try {
            const body = await cdp.send('Network.getResponseBody', { requestId }, 8000);
            const text = (body && body.body) || '';
            if (text) {
              entry.encoded = !!(body && body.base64Encoded);
              entry.bytes = text.length;
              entry.body = text.slice(0, job.captureBodyLimit || 4000);
              return;
            }
          } catch { /* body not ready / already evicted — retry */ }
          await sleep(500);
        }
      };
      const onMessage = (msg) => {
        if (!msg || msg.method !== 'Network.responseReceived') return;
        const p = msg.params || {};
        const url = (p.response && p.response.url) || '';
        if (!url || seenUrls.has(url) || !captureWanted(url)) return;
        const type = p.type || '';
        if (!/XHR|Fetch|Document/.test(type)) return;
        seenUrls.add(url);
        const entry = { url, type, status: (p.response || {}).status,
                        mime: (p.response || {}).mimeType };
        captured.push(entry);
        if (captured.length > 120) captured.splice(0, captured.length - 120);
        grabBody(entry, p.requestId).catch(() => { /* give up quietly */ });
      };
      cdp.on('Network.responseReceived', onMessage);
    }
    await cdp.send('Page.navigate', { url: job.url });
    await sleep(Math.max(1500, Math.min(job.settleMs || 4000, 20000)));
    // Dismiss the Baidu "知道啦" coach overlay and similar first-run popups.
    await evaluate(`(() => {
      const keys = ['知道啦', '知道了', '我知道了', 'Got it', '关闭'];
      const nodes = [...document.querySelectorAll('button,a,span,div')];
      let hits = 0;
      for (const k of keys) {
        const b = nodes.find((x) => String(x.textContent || '').trim() === k);
        if (b) { try { b.click(); hits += 1; } catch (e) { /* ignore */ } }
      }
      return hits;
    })()`).catch(() => 0);

    if (job.file) {
      const selector = job.selector || 'input[type=file]';
      // Single-page upload widgets mount asynchronously; poll instead of failing once.
      let q = null;
      const deadline = Date.now() + Math.max(3000, job.inputWaitMs || 12000);
      while (Date.now() < deadline) {
        const doc = await cdp.send('DOM.getDocument', { depth: 2, pierce: true }).catch(() => null);
        if (doc) {
          q = await cdp.send('DOM.querySelector', { nodeId: doc.root.nodeId, selector })
            .catch(() => null);
          if (!q || !q.nodeId) {
            const alt = await cdp.send('DOM.querySelectorAll',
              { nodeId: doc.root.nodeId, selector: 'input[type=file]' }).catch(() => null);
            if (alt && alt.nodeIds && alt.nodeIds.length) q = { nodeId: alt.nodeIds[0] };
          }
        }
        if (q && q.nodeId) break;
        await sleep(1000);
      }
      if (!q || !q.nodeId) {
        const probe = await evaluate(PROBE_JS).catch(() => null);
        out.probe = probe ? JSON.parse(probe) : null;
        out.finalUrl = await evaluate('location.href', 8000).catch(() => job.url);
        throw new Error(`页面上没找到上传入口（selector=${selector}）`);
      }
      await cdp.send('DOM.setFileInputFiles', { files: [job.file], nodeId: q.nodeId });
    }

    const waitMs = Math.max(3000, job.waitMs || 30000);
    const waitText = job.waitText || [];
    const failText = job.waitFailText || [];
    const waitJs = job.waitJs || '';            // "() => boolean" — engine-specific readiness
    const waitUrl = job.waitUrl || '';          // substring the final URL must contain
    // Expose the captured JSON/XHR urls to the page so an extraction script can replay the
    // engine's own signed request (Baidu's /ajax/pcsimi carries the source-page field the DOM
    // never renders). Without this the extraction only ever sees rendered thumbnails.
    if (job.captureNetwork) {
      await evaluate(`window.__vswCaptured = ${JSON.stringify(captured.map((c) => c.url))};`)
        .catch(() => {});
    }
    let seen = { text: '', matched: false, failed: '' };
    for (let waited = 0; waited < waitMs; waited += 1200) {
      await sleep(1200);
      const raw = await evaluate(`JSON.stringify({
        text: (document.body ? document.body.innerText : '').slice(0, 4000),
        href: location.href,
        ready: ${waitJs ? `(() => { try { return !!(${waitJs}); } catch (e) { return false; } })()` : 'true'}
      })`).catch(() => null);
      if (!raw) continue;
      try { seen = { ...seen, ...JSON.parse(raw) }; } catch { /* ignore */ }
      const body = seen.text || '';
      const bad = failText.find((t) => body.includes(t));
      if (bad) { seen.failed = bad; break; }
      const urlOk = !waitUrl || String(seen.href || '').includes(waitUrl);
      const textOk = !waitText.length || waitText.some((t) => body.includes(t));
      if (urlOk && textOk && seen.ready) { seen.matched = true; break; }
    }
    // Give late XHRs (the result payload) a moment, then detach the network capture so the
    // extraction script owns the console while it replays the engine's own request.
    if (job.captureNetwork) {
      await sleep(1500);
      await evaluate(`window.__vswCaptured = ${JSON.stringify(captured.map((c) => c.url))};`)
        .catch(() => {});
      if (job.captureThenDetach !== false) {
        try { await cdp.send('Network.disable', {}, 5000); } catch { /* ignore */ }
      }
    }

    out.finalUrl = await evaluate('location.href', 8000).catch(() => job.url);
    out.title = await evaluate('document.title || ""', 8000).catch(() => '');
    out.text = (seen.text || '').slice(0, 4000);
    if (job.captureNetwork) out.capture = captured;
    if (job.extractSettleMs) await sleep(Math.min(job.extractSettleMs, 15000));
    if (job.extractJs) {
      const raw = await evaluate(job.extractJs, 25000).catch((e) => `__ERR__${e.message}`);
      if (typeof raw === 'string' && raw.startsWith('__ERR__')) {
        out.error = raw.slice(7);
      } else if (typeof raw === 'string') {
        try { out.extract = JSON.parse(raw); } catch { out.extract = raw; }
      } else {
        out.extract = raw === undefined ? null : raw;
      }
    }
    if (job.dumpProbe) {
      const probe = await evaluate(PROBE_JS).catch(() => null);
      try { out.probe = probe ? JSON.parse(probe) : null; } catch { out.probe = null; }
    }
    out.ok = !seen.failed && (!waitText.length || seen.matched);
    if (seen.failed) out.error = `引擎报错：${seen.failed}`;
    else if (!seen.matched) out.error = `等 ${Math.round(waitMs / 1000)} 秒没有出现预期结果（${(waitText || []).join('/') || waitUrl || 'waitJs'}）`;
    return out;
  } finally {
    await close();
  }
}

async function main() {
  const argv = process.argv.slice(2);
  const jobIdx = argv.indexOf('--job');
  const outIdx = argv.indexOf('--out');
  let job = {};
  if (jobIdx >= 0 && argv[jobIdx + 1]) {
    job = JSON.parse(fs.readFileSync(argv[jobIdx + 1], 'utf8'));
  } else {
    // also accept a JSON string on argv for quick manual probes
    const rawIdx = argv.findIndex((a) => a.trim().startsWith('{'));
    job = rawIdx >= 0 ? JSON.parse(argv[rawIdx]) : {};
  }
  if (!job.url) throw new Error('缺少 url');
  const result = await run(job);
  const payload = JSON.stringify(result);
  // Writing to a file is the reliable channel on Windows (console redirection there can
  // transcode to UTF-16), stdout stays for interactive use.
  if (outIdx >= 0 && argv[outIdx + 1]) {
    fs.writeFileSync(argv[outIdx + 1], payload, 'utf8');
  }
  process.stdout.write(payload);
}

main().then(() => process.exit(0)).catch((e) => {
  const payload = JSON.stringify({ ok: false, error: String((e && e.message) || e) });
  const outIdx = process.argv.indexOf('--out');
  try {
    if (outIdx >= 0 && process.argv[outIdx + 1]) fs.writeFileSync(process.argv[outIdx + 1], payload, 'utf8');
  } catch { /* ignore */ }
  process.stdout.write(payload);
  process.exit(0);
});
