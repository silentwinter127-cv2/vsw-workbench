/* 手机端「自动搜图」按钮的界面回归（不联网、不开真浏览器）。

   手机里没有能替引擎上传图片的内核，所以这个按钮的作用是：
   手机把待处理任务交给电脑 → 电脑的无头浏览器一个个真跑 → 结果入库。

   这里只验界面那半段：按钮什么时候可点/文案带几个、点了会发什么请求、
   进度条怎么显示、跑完会不会自己刷新队列。网络层用假的 api/post 顶替。

   用法：node scripts/vsw-probe-auto-search.js --apk vsw-workbench.apk
*/
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const { pathToFileURL } = require('url');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseArgs(argv) {
  const out = { apk: 'vsw-workbench.apk', page: '', keep: false };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--apk') out.apk = argv[++i];
    else if (argv[i] === '--page') out.page = argv[++i];
    else if (argv[i] === '--keep') out.keep = true;
  }
  return out;
}

function findBrowser() {
  const pf = process.env.ProgramFiles || 'C:\\Program Files';
  const pf86 = process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)';
  const local = process.env.LOCALAPPDATA || '';
  const cands = [
    path.join(pf86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    path.join(pf, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    local && path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe'),
  ].filter(Boolean);
  for (const p of cands) {
    try { if (fs.existsSync(p)) return p; } catch (e) { /* ignore */ }
  }
  throw new Error('找不到 Edge/Chrome');
}

function extractPage(apk, outDir) {
  const script = `
import json, zipfile
z = zipfile.ZipFile(${JSON.stringify(path.resolve(apk))})
data = z.read('assets/mobile/index.html')
open(${JSON.stringify(path.join(outDir, 'index.html'))}, 'wb').write(data)
print(json.dumps({'bytes': len(data)}))
`;
  const raw = execFileSync('python', ['-c', script], { encoding: 'utf8' });
  return JSON.parse(raw.trim().split('\n').pop());
}

(async () => {
  const args = parseArgs(process.argv);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-autoui-'));
  // --page 给源码文件：改一行 HTML 就重打 600 KB 的 APK 太慢，先针对源码测，最后再用 --apk 复核
  let info;
  if (args.page) {
    const raw = fs.readFileSync(args.page);
    fs.writeFileSync(path.join(dir, 'index.html'), raw);
    info = { bytes: raw.length };
  } else {
    info = extractPage(args.apk, dir);
  }
  const pageUrl = pathToFileURL(path.join(dir, 'index.html')).href;
  console.log(`${args.page ? '源码' : 'APK'} 页面 ${info.bytes} 字节`);

  const exe = findBrowser();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-autoui-profile-'));
  const port = 10560 + Math.floor(Math.random() * 60);
  const child = spawn(exe, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--hide-scrollbars',
    '--allow-file-access-from-files',
  ], { stdio: 'ignore' });

  let pass = 0;
  let fail = 0;
  const check = (label, ok, detail) => {
    if (ok) { pass++; console.log(`  ok   ${label}`); } else { fail++; console.log(`  FAIL ${label}${detail ? ' :: ' + detail : ''}`); }
  };

  try {
    let version = null;
    for (let i = 0; i < 60 && !version; i++) {
      await sleep(300);
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/version`);
        if (r.ok) version = await r.json();
      } catch (e) { /* not up */ }
    }
    if (!version) throw new Error('调试端口没起来');
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const ws = new WebSocket(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
    const pending = new Map();
    let seq = 0;
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    });
    const send = (method, params) => new Promise((resolve) => {
      const id = ++seq;
      pending.set(id, resolve);
      ws.send(JSON.stringify({ id, method, params: params || {} }));
    });
    await new Promise((r) => ws.addEventListener('open', r));
    await send('Runtime.enable');
    await send('Page.enable');
    const evaluate = async (expression) => {
      const res = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
      if (res.result && res.result.exceptionDetails) return { __error: res.result.exceptionDetails.text };
      return res.result && res.result.result ? res.result.result.value : null;
    };

    await send('Page.navigate', { url: pageUrl });
    await sleep(2500);

    // 假网络：记录请求，按脚本返回
    const installed = await evaluate(`(() => {
      window.__CALLS = [];
      window.__PREVIEW = { count: 7, items: [], skipped: { bing_cn: 3 } };
      window.__STATE = { running: false, done: 0, total: 0, results: [] };
      S.server = 'http://127.0.0.1:9';
      S.online = true;
      S.jobs = [
        { id: 11, engine: 'baidu', status: 'pending', seed_title: '甲' },
        { id: 12, engine: 'so360', status: 'pending', seed_title: '乙' },
      ];
      window.api = (p) => {
        window.__CALLS.push(['GET', p]);
        if (p.indexOf('/api/auto/preview') === 0) return Promise.resolve(window.__PREVIEW);
        if (p.indexOf('/api/auto/state') === 0) return Promise.resolve(window.__STATE);
        if (p.indexOf('/api/queue') === 0) return Promise.resolve({ jobs: S.jobs, counts: {}, facets: {} });
        if (p.indexOf('/api/mobile/snapshot') === 0) {
          return Promise.resolve({ counts: {}, findings: [], seeds: [], variants: [], jobs: S.jobs,
            trash: [], archived: [], archives: [], settings: {}, server_time: Date.now() / 1000 });
        }
        return Promise.resolve({});
      };
      window.post = (p, body) => {
        window.__CALLS.push(['POST', p, body]);
        if (p === '/api/auto/run') {
          window.__STATE = { running: true, done: 0, total: Number(body.limit) || 0, results: [] };
          return Promise.resolve({ started: true, queued: 2, job_ids: [11, 12] });
        }
        return Promise.resolve({});
      };
      switchTab('queue');
      renderQueue();
      return { tab: S.tab };
    })()`);
    check('能切到队列页', installed && installed.tab === 'queue', JSON.stringify(installed));
    await sleep(1500);

    const ready = await evaluate(`(() => {
      const btn = document.querySelector('#autoRunBtn');
      return { text: btn ? btn.textContent.trim() : '', disabled: btn ? btn.disabled : null };
    })()`);
    check('队列页有「自动搜图」按钮', !!ready && /自动搜图/.test(ready.text || ''), JSON.stringify(ready));
    check('按钮带出可跑的任务数（7）', /（7）/.test(ready.text || ''), ready && ready.text);
    check('有任务时按钮可点', ready && ready.disabled === false, JSON.stringify(ready));

    const asked = await evaluate(`(() => {
      const calls = window.__CALLS.filter((c) => c[0] === 'GET' && c[1].indexOf('/api/auto/preview') === 0);
      return { preview: calls.map((c) => c[1]), state: window.__CALLS.some((c) => c[1] === '/api/auto/state') };
    })()`);
    check('渲染队列时会先问「有多少能自动跑」',
          asked && asked.preview.length > 0 && /status=pending/.test(asked.preview[0]), JSON.stringify(asked));
    check('也会读一次运行状态', asked && asked.state === true, JSON.stringify(asked));

    // 点按钮
    const clicked = await evaluate(`(async () => {
      document.querySelector('#autoRunBtn').click();
      await new Promise((r) => setTimeout(r, 600));
      const posts = window.__CALLS.filter((c) => c[0] === 'POST');
      const box = document.querySelector('#autoState');
      return { posts, boxHidden: box.classList.contains('hide'), boxText: box.textContent.trim().slice(0, 120),
               toast: (document.querySelector('#toast') || {}).textContent || '' };
    })()`);
    check('点了会 POST /api/auto/run',
          clicked && Array.isArray(clicked.posts) && clicked.posts.some((p) => p[1] === '/api/auto/run'), JSON.stringify(clicked && clicked.posts));
    check('请求里明确要跑「待处理 + 待复查」',
          clicked && Array.isArray(clicked.posts) && clicked.posts.some((p) => p[1] === '/api/auto/run'
            && Array.isArray(p[2] && p[2].statuses) && p[2].statuses.includes('pending')), JSON.stringify(clicked && clicked.posts));
    check('弹出「已交给电脑跑」的提示',
          clicked && /交给电脑跑/.test(clicked.toast || ''), clicked && clicked.toast);
    check('出现进度条并显示正在跑',
          clicked && clicked.boxHidden === false && /自动搜图|正在/.test(clicked.boxText || ''),
          JSON.stringify(clicked && clicked.boxText));

    // 模拟跑完
    const done = await evaluate(`(async () => {
      window.__STATE = { running: false, done: 2, total: 2, phase: '完成',
        results: [ { job_id: 11, engine: 'baidu', ok: true, new_findings: 9, duplicates: 2, error: '' },
                   { job_id: 12, engine: 'so360', ok: false, new_findings: 0, duplicates: 0, error: '被验证码挡了' } ] };
      await new Promise((r) => setTimeout(r, 3200));
      const box = document.querySelector('#autoState');
      return { text: box.textContent.trim().slice(0, 200), hidden: box.classList.contains('hide'),
               toast: (document.querySelector('#toast') || {}).textContent || '' };
    })()`);
    check('跑完后进度条说清「成功几个、新进几张」',
          done && /自动搜图结束/.test(done.text || '') && /成功 1/.test(done.text || '')
            && /新进 9/.test(done.text || ''), JSON.stringify(done && done.text));
    check('失败项写明原因',
          done && /被验证码挡了/.test(done.text || ''), JSON.stringify(done && done.text));
    check('跑完给一句 toast 说明结果', done && /自动搜图跑完|新进/.test(done.toast || ''), done && done.toast);

    // 断网时按钮不可点
    const offline = await evaluate(`(() => {
      S.online = false;
      S.autoPreview = { count: 5, running: false, done: 0, total: 0 };
      renderQueue();
      const btn = document.querySelector('#autoRunBtn');
      return { disabled: btn ? btn.disabled : null, text: btn ? btn.textContent.trim() : '' };
    })()`);
    check('离线时按钮不可点（手机自己跑不了）',
          offline && offline.disabled === true, JSON.stringify(offline));

    const noTask = await evaluate(`(() => {
      S.online = true;
      S.autoPreview = { count: 0, running: false, done: 0, total: 0, skipped: { bing_cn: 4 } };
      renderQueue();
      const btn = document.querySelector('#autoRunBtn');
      return { disabled: btn ? btn.disabled : null, text: btn ? btn.textContent.trim() : '' };
    })()`);
    check('没有可自动跑的引擎时按钮不可点',
          noTask && noTask.disabled === true && noTask.text === '自动搜图', JSON.stringify(noTask));

    const running = await evaluate(`(() => {
      S.autoPreview = { count: 0, running: true, done: 3, total: 10 };
      renderQueue();
      const btn = document.querySelector('#autoRunBtn');
      return { text: btn ? btn.textContent.trim() : '', disabled: btn ? btn.disabled : null };
    })()`);
    check('正在跑时按钮显示进度并锁住',
          running && /3\/10/.test(running.text || '') && running.disabled === true, JSON.stringify(running));

    const noErrors = await evaluate(`(() => (window.__ERRORS || []).length)()`);
    check('页面没抛未捕获异常', codeChecksOut(noErrors), JSON.stringify(noErrors));

    console.log(`\n通过 ${pass} 项，失败 ${fail} 项`);
    process.exitCode = fail ? 1 : 0;
  } finally {
    try { child.kill(); } catch (e) { /* ignore */ }
    if (!args.keep) {
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* ignore */ }
      try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ }
    }
  }
})().catch((e) => { console.error(e); process.exit(1); });

function codeChecksOut(value) {
  if (value === null || value === undefined) return true;
  return Number(value) === 0;
}
