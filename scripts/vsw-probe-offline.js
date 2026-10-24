/* 断联电脑时的导入：手机上能不能自己建条目、看得见图、能不能直接「手机搜」。
 *
 * 用户实测反馈：拔掉电脑从相册导入一张图 —— 队列里既没有这张图也没有缩略图，
 * 「图不见了」，必须先同步电脑才能搜。根因是离线那一支只把图塞进 pending 收件箱，
 * 本地既不建种子也不建队列项。
 *
 * 这个探针完全不开电脑：页面用 file:// 打开，不给任何服务器地址，网络层不装假 api
 * （真离线），只塞一个 File 进 IMPORT_QUEUE，然后走真实的 importOne → importOffline。
 *
 * 用法：node scripts/vsw-probe-offline.js --page vsw/static/mobile/index.html
 *       node scripts/vsw-probe-offline.js --apk vsw-workbench.apk
 */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const { pathToFileURL } = require('url');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseArgs(argv) {
  const out = { apk: 'vsw-workbench.apk', page: '' };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--apk') out.apk = argv[++i];
    else if (argv[i] === '--page') out.page = argv[++i];
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-offline-'));
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-offline-profile-'));
  const port = 10720 + Math.floor(Math.random() * 40);
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
    await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
    const evaluate = async (expression) => {
      const res = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
      if (res.result && res.result.exceptionDetails) {
        return { __error: res.result.exceptionDetails.text, desc: res.result.exceptionDetails.exception && res.result.exceptionDetails.exception.description };
      }
      return res.result && res.result.result ? res.result.result.value : null;
    };

    await send('Page.navigate', { url: pageUrl });
    await sleep(2600);

    // 真离线：没有服务器地址、不装假 api/post。先清掉本地存的服务器地址，再重载页面。
    await evaluate(`(() => {
      localStorage.removeItem('vsw.server');
      localStorage.removeItem('vsw.sid');
      localStorage.removeItem('vsw.key');
      return 1;
    })()`);
    await send('Page.reload');
    await sleep(2600);
    // 安卓桥必须在重载之后装（重载会清掉 window 上的东西）
    await evaluate(`(() => {
      window.__SEARCH = null;
      window.AndroidBridge = {
        buildTag: () => 'probe',
        webSearchAvailable: () => '1',
        webSearch: (...a) => { window.__SEARCH = a; },
        checkUpdate: () => {},
        installUpdate: () => {},
      };
      return 1;
    })()`);

    const state0 = await evaluate(`(() => ({
      server: S.server, online: S.online, jobs: S.jobs.length,
      bridge: typeof window.AndroidBridge !== 'undefined',
      ready: phoneSearchReady(),
    }))()`);
    check('完全离线启动（没有服务器地址）', state0 && state0.server === '' && state0.online === false, JSON.stringify(state0));
    check('离线时「手机搜」可用（原生桥在）', !!(state0 && state0.ready), JSON.stringify(state0));

    // 造一张真图，塞进导入队列，走真实的 importOne
    const imported = await evaluate(`(async () => {
      const file = await new Promise((resolve) => {
        const c = document.createElement('canvas');
        c.width = 240; c.height = 180;
        const ctx = c.getContext('2d');
        ctx.fillStyle = '#2d6a4f'; ctx.fillRect(0, 0, 240, 180);
        ctx.fillStyle = '#fff'; ctx.font = '28px sans-serif'; ctx.fillText('VSW', 24, 100);
        c.toBlob((b) => resolve(new File([b], 'offline-test.png', { type: 'image/png' })), 'image/png');
      });
      filesToQueue([file]);
      const entry = IMPORT_QUEUE[IMPORT_QUEUE.length - 1];
      await importOne(entry);
      const job = S.jobs.find((j) => j.local) || null;
      const seed = job ? S.seeds.find((s) => s.id === job.seed_id) : null;
      const thumbRec = job ? await local.get('assets', KEY.seedThumb(job.seed_id)) : null;
      const fullRec = job ? await local.get('assets', KEY.localFull(job.seed_id)) : null;
      const pendingRows = await local.all('pending');
      const b64 = job ? await jobImageBase64(job) : '';
      return {
        status: entry.status, detail: entry.detail,
        job: job && { id: job.id, local: job.local, seed_id: job.seed_id, status: job.status, seed_title: job.seed_title },
        seed: seed && { id: seed.id, local: seed.local, title: seed.title, w: seed.w, h: seed.h },
        thumbBytes: thumbRec && thumbRec.blob ? thumbRec.blob.size : 0,
        fullBytes: fullRec && fullRec.blob ? fullRec.blob.size : 0,
        pending: pendingRows.length,
        pendingLocalJob: pendingRows.length ? pendingRows[0].payload.local_job : null,
        b64Len: b64.length, b64Head: b64.slice(0, 6),
      };
    })()`);
    if (imported && imported.__error) console.log('  （导入求值出错）', imported.desc || imported.__error);
    check('离线导入成功', !!(imported && imported.status === 'queued'), JSON.stringify(imported && imported.status));
    check('提示说的是「已存进手机」而不是「收件箱」', !!(imported && /已存进手机/.test(imported.detail || '')), imported && imported.detail);
    check('本地建了一条队列项（id 为负数）', !!(imported && imported.job && imported.job.local === 1 && imported.job.id < 0), JSON.stringify(imported && imported.job));
    check('本地建了一条种子', !!(imported && imported.seed && imported.seed.local === 1 && imported.seed.title === 'offline-test.png'), JSON.stringify(imported && imported.seed));
    check('种子记下了尺寸', !!(imported && imported.seed && imported.seed.w === 240 && imported.seed.h === 180), JSON.stringify(imported && imported.seed));
    check('缩略图存进了本地库', !!(imported && imported.thumbBytes > 0), `thumbBytes=${imported && imported.thumbBytes}`);
    check('原图也留着（手机搜要用原图，不是缩略图）', !!(imported && imported.fullBytes > 1000), `fullBytes=${imported && imported.fullBytes}`);
    check('离线收件箱也记了一条（回连后推给电脑）', !!(imported && imported.pending === 1 && imported.pendingLocalJob < 0), JSON.stringify(imported && [imported.pending, imported.pendingLocalJob]));
    check('队列项能取到要搜的图（base64 非空）', !!(imported && imported.b64Len > 1000), `len=${imported && imported.b64Len} head=${imported && imported.b64Head}`);

    // 队列页真渲染：缩略图应该是 blob: 地址，并且标着「手机上」
    await evaluate(`(() => { switchTab('queue'); renderQueue(); return 1; })()`);
    await sleep(900);
    const view = await evaluate(`(() => {
      const cards = $$('#queueList .job');
      const card = cards[0] || null;
      const img = card ? card.querySelector('img') : null;
      return {
        cards: cards.length,
        src: img ? String(img.src).slice(0, 22) : '',
        naturalOk: !!(img && img.naturalWidth > 0),
        text: card ? card.innerText.replace(/\\s+/g, ' ') : '',
        hasPhoneBtn: !!(card && card.querySelector('[data-act="phone"]')),
      };
    })()`);
    check('队列页出现了这条（离线也有）', !!(view && view.cards >= 1), JSON.stringify(view && view.cards));
    check('队列缩略图显示出来了（blob:）', !!(view && /^blob:/.test(view.src)), view && view.src);
    check('缩略图真的解码出像素', !!(view && view.naturalOk), JSON.stringify(view && view.naturalOk));
    check('条目上标着「手机上」', !!(view && /手机上/.test(view.text)), view && view.text);
    check('离线时这一条有「手机搜」按钮', !!(view && view.hasPhoneBtn), JSON.stringify(view && view.hasPhoneBtn));

    // 点「手机搜」：图应该交给原生（这一步完全不需要电脑）
    const tapped = await evaluate(`(() => {
      const btn = document.querySelector('#queueList .job [data-act="phone"]');
      if (!btn) return { error: '没有手机搜按钮' };
      btn.click();
      return { clicked: true };
    })()`);
    await sleep(500);
    const nativeCall = await evaluate(`(() => {
      const a = window.__SEARCH;
      if (!a) return { called: false };
      return { called: true, engine: a[0], label: a[1], url: a[2],
               extractHasPcsimi: String(a[3] || '').indexOf('pcsimi') >= 0,
               imageLen: String(a[4] || '').length, imageHead: String(a[4] || '').slice(0, 6) };
    })()`);
    check('点「手机搜」把任务交给了原生', !!(nativeCall && nativeCall.called), JSON.stringify(tapped));
    check('交给原生的是本地那张原图', !!(nativeCall && nativeCall.imageLen > 1000), JSON.stringify(nativeCall && nativeCall.imageLen));
    check('引擎地址与提取脚本都对', !!(nativeCall && nativeCall.engine === 'baidu' && nativeCall.extractHasPcsimi), JSON.stringify(nativeCall));

    // 连不上电脑时刷新队列，不能把本地这条冲掉
    await evaluate(`(() => { refreshQueueFromServer({ silent: true }); return 1; })()`);
    await sleep(800);
    const afterRefresh = await evaluate(`(() => ({
      jobs: S.jobs.length,
      local: S.jobs.filter((j) => j.local).length,
      stored: null,
    }))()`);
    const stored = await evaluate(`(async () => {
      const rows = await local.all('jobs');
      return { rows: rows.length, local: rows.filter((j) => j.local).length };
    })()`);
    check('连不上电脑时刷新队列，本地这条还在', !!(afterRefresh && afterRefresh.local === 1), JSON.stringify(afterRefresh));
    check('本地库里的这条也没被清掉', !!(stored && stored.local === 1), JSON.stringify(stored));

    // 离线删除：不需要联网，连带把待同步那条也去掉
    const deleted = await evaluate(`(async () => {
      window.confirm = () => true;
      const job = S.jobs.find((j) => j.local);
      await deleteQueueJobs([job.id], '这个');
      const rows = await local.all('jobs');
      const pendingRows = await local.all('pending');
      const thumb = await local.get('assets', KEY.seedThumb(job.seed_id));
      const full = await local.get('assets', KEY.localFull(job.seed_id));
      return { jobs: rows.length, localLeft: rows.filter((j) => j.local).length,
               pending: pendingRows.length, thumbGone: !thumb, fullGone: !full };
    })()`);
    check('离线也能删掉本地队列项', !!(deleted && deleted.localLeft === 0), JSON.stringify(deleted));
    check('删掉后待同步那条也一并去掉', !!(deleted && deleted.pending === 0), JSON.stringify(deleted && deleted.pending));
    check('删掉后本地图片也清了（不留垃圾）', !!(deleted && deleted.thumbGone && deleted.fullGone), JSON.stringify(deleted));

    const errs = await evaluate(`window.__ERRS || []`);
    check('没有未捕获异常', !errs || !errs.length, JSON.stringify(errs));
  } catch (e) {
    fail++;
    console.log('  FAIL 探针自身出错 :: ' + (e && e.message ? e.message : e));
  } finally {
    try { child.kill(); } catch (e) { /* ignore */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* ignore */ }
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  }

  console.log(`\n${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
