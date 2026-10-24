/**
 * 探针：验证「导入图片」面板的队列行为。
 *
 * 背景（用户实测反馈的真 bug）：
 *   1. 点「直接导入」成功后整个面板被关掉，人被弹回上一页；
 *   2. 面板关掉后，列表里其它文件再也不会被导入。
 * 这类 bug 只看代码很难确认修好了 —— 所以这里真的把面板跑起来，
 * 用真实的 File 对象走一遍"选 3 张 → 逐张导入 → 全导入"，检查：
 *   - 导入后面板是否还在（sheet 仍打开、列表还在）；
 *   - 每张是否各自更新状态（不会互相干扰、不会漏掉）；
 *   - 「全部导入」是否把剩下的都做完。
 *
 * 用法：node vsw-probe-import-queue.js [file:///…/index.html | --apk <apk>] [工作台地址]
 */
'use strict';
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseArgs() {
  const out = { apk: '', page: '', origin: 'http://127.0.0.1:8799' };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--apk') out.apk = argv[++i];
    else if (argv[i] === '--page') out.page = argv[++i];
    else if (argv[i] === '--origin') out.origin = argv[++i];

    else if (argv[i] === '--sid') out.sid = argv[++i];
    else if (!out.page && !out.apk) out.page = argv[i];
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
  throw new Error('找不到 Chromium 内核浏览器');
}

function extractPageFromApk(apk) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-import-probe-'));
  const target = path.join(dir, 'index.html');
  const script = [
    'import zipfile',
    `z=zipfile.ZipFile(r"${apk}")`,
    'data=z.read("assets/mobile/index.html")',
    `open(r"${target}","wb").write(data)`,
    'print(len(data))',
  ].join(';');
  const out = execFileSync('python', ['-c', script], { encoding: 'utf8' });
  return { html: target, bytes: parseInt(out.trim(), 10), dir };
}

async function main() {
  const args = parseArgs();
  let pageUrl = args.page;
  let extracted = null;
  if (args.apk) {
    extracted = extractPageFromApk(path.resolve(args.apk));
    pageUrl = 'file:///' + extracted.html.replace(/\\/g, '/');
  }
  if (!pageUrl) {
    console.error('给 --apk <apk> 或 --page <file:// …>');
    process.exit(2);
  }

  const exe = findBrowser();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-import-profile-'));
  const port = 9650 + Math.floor(Math.random() * 80);
  const child = spawn(exe, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--allow-file-access-from-files',
  ], { stdio: 'ignore' });

  const results = [];
  const check = (label, ok, detail) => {
    results.push({ label, ok: !!ok });
    console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}${!ok && detail ? ' :: ' + detail : ''}`);
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
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const target = list.find((t) => t.type === 'page');
    const ws = new WebSocket(target.webSocketDebuggerUrl);
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

    console.log(`内核 ${version.Browser} · 页面 ${pageUrl}\n`);
    await send('Page.navigate', { url: pageUrl });
    await sleep(3000);

    // 先把地址填好，避免首次引导挡住导入面板
    await send('Page.navigate', { url: pageUrl });
    await sleep(1200);
    await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)}); ${args.sid ? `localStorage.setItem('vsw.sid', ${JSON.stringify(args.sid)});` : ''}`);
    await send('Page.reload');
    await sleep(2500);

    // 造 3 张真实的小图（png），塞进导入队列并打开面板
    const setup = await evaluate(`(async () => {
      const makeFile = (name, color) => {
        return new Promise((resolve) => {
          const canvas = document.createElement('canvas');
          canvas.width = 160; canvas.height = 120;
          const ctx = canvas.getContext('2d');
          ctx.fillStyle = color; ctx.fillRect(0, 0, 160, 120);
          ctx.fillStyle = '#333'; ctx.font = '20px sans-serif';
          ctx.fillText(name, 8, 60);
          canvas.toBlob((blob) => resolve(new File([blob], name, { type: 'image/png' })), 'image/png');
        });
      };
      const files = await Promise.all([
        makeFile('探针甲.png', '#c33'), makeFile('探针乙.png', '#3c3'), makeFile('探针丙.png', '#33c'),
      ]);
      // 走应用自己的队列函数，而不是绕过它
      const added = filesToQueue(files);
      openImportSheet();
      return { added, cards: document.querySelectorAll('#importList .card').length,
               queue: IMPORT_QUEUE.length };
    })()`);
    check('选 3 张后列表里出现 3 张卡片',
          setup && setup.added === 3 && setup.cards === 3, JSON.stringify(setup));

    // 点第一张的「直接导入」，然后看面板是否还开着
    const afterFirst = await evaluate(`(async () => {
      const btn = document.querySelector('#importList [data-do="send"]');
      if (!btn) return { error: '找不到导入按钮' };
      btn.click();
      await new Promise((r) => setTimeout(r, 4000));
      return {
        sheetOpen: document.querySelector('#sheet').classList.contains('open'),
        title: document.querySelector('#sheetTitle').textContent,
        listStillThere: !!document.querySelector('#importList .card'),
        cards: document.querySelectorAll('#importList .card').length,
        done: IMPORT_QUEUE.filter((e) => e.status === 'done').length,
        statuses: IMPORT_QUEUE.map((e) => e.status),
      };
    })()`);
    check('导入一张后面板仍然开着（不再把人弹回上一页）',
          afterFirst && afterFirst.sheetOpen && afterFirst.listStillThere,
          JSON.stringify(afterFirst));
    check('导入一张后仍然停在「导入图片」面板',
          afterFirst && afterFirst.title === '导入图片', afterFirst && afterFirst.title);
    check('列表里 3 张都还在（没被清掉）',
          afterFirst && afterFirst.cards === 3, String(afterFirst && afterFirst.cards));
    check('只有那一张变成已导入，其余仍是待处理',
          afterFirst && afterFirst.done === 1
          && afterFirst.statuses.filter((s) => s === 'idle').length === 2,
          JSON.stringify(afterFirst && afterFirst.statuses));

    // 用「全部导入」把剩下的做完
    const afterAll = await evaluate(`(async () => {
      const btn = document.querySelector('#importAll');
      if (!btn) return { error: '没有「全部导入」按钮（待处理数不足 2？）' };
      btn.click();
      await new Promise((r) => setTimeout(r, 6000));
      return {
        sheetOpen: document.querySelector('#sheet').classList.contains('open'),
        statuses: IMPORT_QUEUE.map((e) => e.status),
        details: IMPORT_QUEUE.map((e) => e.detail),
        pendingLeft: IMPORT_QUEUE.filter((e) => e.status === 'idle').length,
      };
    })()`);
    check('「全部导入」把剩下的都处理了（没有漏在 idle）',
          afterAll && afterAll.pendingLeft === 0, JSON.stringify(afterAll && afterAll.statuses));
    check('三张都成功建成种子',
          afterAll && afterAll.statuses.every((s) => s === 'done'),
          JSON.stringify(afterAll && afterAll.details));
    check('全部导入后面板依然开着',
          afterAll && afterAll.sheetOpen, JSON.stringify(afterAll && afterAll.sheetOpen));

    // 到工作台确认这三张真的入库了
    // 导入之后队列里必须立刻能看到新任务（这正是用户反馈"没进队列"的那一点）
    const queued = await evaluate(`(async () => {
      const ids = IMPORT_QUEUE.map(e => (e.detail || '').match(/#(\\d+)/)).filter(Boolean).map(m => Number(m[1]));
      return {
        seedIds: ids,
        jobsTotal: S.jobs.length,
        jobsForSeeds: S.jobs.filter(j => ids.includes(j.seed_id)).length,
        pending: S.jobs.filter(j => ['pending','running','review'].includes(j.status)).length,
        topSeeds: S.jobs.slice(0, 5).map(j => j.seed_id),
      };
    })()`);
    check('导入后队列里立刻能看到新任务（不用手动再同步）',
          queued && queued.jobsForSeeds >= queued.seedIds.length && queued.seedIds.length > 0,
          JSON.stringify(queued));
    check('新任务排在队列前面（同状态按最新优先）',
          queued && queued.topSeeds.some(id => queued.seedIds.includes(id)),
          JSON.stringify(queued && queued.topSeeds));

    const seeded = await evaluate(`(async () => {
      // 用页面的 api()：它带 cookie / X-VSW-Session，裸 fetch 在有账号时只有 401。
      const list = (await api('/api/seeds?limit=500')).seeds || [];
      return list.filter((s) => /探针/.test(s.title || '')).map((s) => s.title);
    })()`);
    check('工作台里确实多了这三张（按标题能找到）',
          Array.isArray(seeded) && seeded.length >= 3, JSON.stringify(seeded));

    const failed = results.filter((r) => !r.ok);
    console.log(`\n通过 ${results.length - failed.length} 项，失败 ${failed.length} 项`);
    if (failed.length) process.exitCode = 1;
    ws.close();
  } finally {
    child.kill();
    await sleep(300);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* lock */ }
    if (extracted) { try { fs.rmSync(extracted.dir, { recursive: true, force: true }); } catch (e) { /* lock */ } }
  }
}

main().catch((e) => { console.error('探针失败:', e.message); process.exit(1); });
