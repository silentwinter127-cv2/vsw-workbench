/**
 * 探针：回收站/归档列表的图片是否会显示。
 *
 * 背景（用户实测反馈）：回收站里图片一片空白（占位灰块），而且每张都写「0 源」。
 * 查到的事实是：**服务端快照是对的**（206 条回收站条目全部带 thumb_path 与 source_count:2），
 * 所以问题在手机端 —— 图片是"同步时按需下载"的，而回收站里的条目往往是**之后才被清掉的**，
 * 上一次同步时它们还不是回收站条目，缩略图就没下过。
 *
 * 这个探针复现那条路径：
 *   1. 打开归档页 → 回收站应该渲染出卡片（不是空列表）；
 *   2. 故意把本地 assets 里回收站那批缩略图删掉，模拟"同步时没下到"；
 *   3. 重新渲染回收站 → 应该自动把缺的图补回来并填进 <img>；
 *   4. 检查 DOM 里每张卡片的 img 是否真的有 src（不是占位灰块）；
 *   5. 顺带检查「N 源」显示的是真实来源数，而不是恒为 0。
 *
 * 用法：node vsw-probe-trash-thumbs.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799
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

function extractPage(apk) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-trash-probe-'));
  const target = path.join(dir, 'index.html');
  execFileSync('python', ['-c', [
    'import zipfile',
    `z=zipfile.ZipFile(r"${apk}")`,
    'open(r"' + target + '","wb").write(z.read("assets/mobile/index.html"))',
  ].join(';')], { encoding: 'utf8' });
  return { html: target, dir };
}

async function main() {
  const args = parseArgs();
  let pageUrl = args.page;
  let extracted = null;
  if (args.apk) {
    extracted = extractPage(path.resolve(args.apk));
    pageUrl = 'file:///' + extracted.html.replace(/\\/g, '/');
  }
  if (!pageUrl) { console.error('给 --apk 或 --page'); process.exit(2); }

  const exe = findBrowser();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-trash-profile-'));
  const port = 9750 + Math.floor(Math.random() * 80);
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

    console.log(`内核 ${version.Browser}\n页面 ${pageUrl}\n`);
    // 先写地址再**重新加载**：boot() 只在页面加载时读一次 localStorage，
    // 写完不刷新的话应用内存里 S.server 还是空的，媒体请求就没有主机名
    // （第一版探针就栽在这里，误判成应用 bug）。
    await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)})`);
    await send('Page.navigate', { url: pageUrl });
    await sleep(2500);
    const ready = await evaluate(`({ server: S.server, online: S.online,
      stored: localStorage.getItem('vsw.server'), origin: location.origin, proto: location.protocol })`);
    console.log(`      应用地址: ${ready && ready.server} · online=${ready && ready.online}`
      + ` · localStorage=${JSON.stringify(ready && ready.stored)} · ${ready && ready.proto}`);
    if (!ready || !ready.server) {
      // 地址丢了就没法验证图片补齐 —— 直接把它补上并重新加载一次，别让探针假失败
      await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)}); location.reload();`);
      await sleep(3000);
      const again = await evaluate(`({ server: S.server, online: S.online })`);
      console.log(`      二次尝试后: ${again && again.server} · online=${again && again.online}`);
    }

    // 拉一次快照并写入本地库（模拟手机同步过）
    const synced = await evaluate(`(async () => {
      const res = await fetch(${JSON.stringify(args.origin)} + '/api/mobile/snapshot?images=thumbs&limit=300&trash=1&archives=1');
      const snap = await res.json();
      for (const store of ['findings','trash','archives','jobs','seeds','variants']) {
        const rows = store === 'findings' ? snap.findings : store === 'trash' ? snap.trash
                   : store === 'archives' ? snap.archives : store === 'jobs' ? snap.jobs
                   : store === 'seeds' ? snap.seeds : snap.variants;
        for (const row of rows || []) await local.put(store, row);
      }
      await local.put('meta', { key: 'snapshot', value: { at: Date.now(), counts: snap.counts } });
      await loadLocal();          // 必须调：数据进内存、补图都在这里发生
      return { trash: S.trash.length, findings: (snap.findings || []).length };
    })()`);
    check('快照能拉下来并写进本地库', synced && synced.trash > 0, JSON.stringify(synced));

    // 故意把回收站那批的缩略图从本地删掉，模拟"同步时没下过"
    const wiped = await evaluate(`(async () => {
      let removed = 0;
      for (const f of S.trash) {
        const key = 'thumb:' + f.id;
        if (await local.get('assets', key)) { await local.del('assets', key); removed += 1; }
      }
      S._thumbs = {};                 // 连内存里的 objectURL 也清掉
      return { removed, trash: S.trash.length };
    })()`);
    console.log(`      （清掉了 ${wiped && wiped.removed} 张本地缩略图，回收站共 ${wiped && wiped.trash} 条）`);

    // 渲染回收站：应当自动补齐
    const rendered = await evaluate(`(async () => {
      switchTab('archive');
      await new Promise((r) => setTimeout(r, 9000));   // 给补图留时间
      const cards = Array.from(document.querySelectorAll('#trashList .fcard'));
      const imgs = cards.map((c) => c.querySelector('img'));
      const withSrc = imgs.filter((i) => i && i.getAttribute('src')).length;
      const srcSample = imgs.find((i) => i && i.getAttribute('src'));
      const sourceTexts = cards.map((c) => (c.querySelector('.ln') || {}).textContent || '');
      // 诊断：为什么没补上
      const first = S.trash[0] || {};
      const stored = first.id ? await local.get('assets', 'thumb:' + first.id) : null;
      let probeUrl = '';
      let probeOk = null;
      let probeErr = '';
      if (first.thumb_path) {
        probeUrl = relToMediaServer(first.thumb_path);
        try {
          const r = await fetch(probeUrl, { cache: 'force-cache' });
          probeOk = r.ok;
        } catch (e) { probeErr = String(e); }
      }
      return {
        cards: cards.length,
        withSrc,
        blobSrc: srcSample ? srcSample.getAttribute('src').slice(0, 12) : '',
        loadedSample: srcSample ? (srcSample.naturalWidth || 0) : 0,
        sourceTexts: sourceTexts.slice(0, 3),
        blank: sourceTexts.filter((t) => /·\\s*0 源/.test(t)).length,
        diag: {
          server: S.server, online: S.online,
          firstId: first.id, firstThumb: first.thumb_path,
          mapVal: S._thumbs ? String(S._thumbs[first.id]).slice(0, 16) : '(no map)',
          storedAsset: stored ? 'yes' : 'no',
          probeUrl, probeOk, probeErr,
          hasEnsure: typeof ensureThumbs,
        },
      };
    })()`);
    console.log('      诊断:', JSON.stringify(rendered && rendered.diag));
    check('回收站渲染出卡片（不是空列表）',
          rendered && rendered.cards > 0, JSON.stringify(rendered));
    check('本地图被删掉后会自动补回来（绝大多数字段都有 src）',
          rendered && rendered.cards > 0 && rendered.withSrc >= Math.ceil(rendered.cards * 0.9),
          `${rendered && rendered.withSrc}/${rendered && rendered.cards}`);
    check('补回来的是真实图片数据（objectURL 且 naturalWidth > 0）',
          rendered && rendered.blobSrc.startsWith('blob:') && rendered.loadedSample > 0,
          JSON.stringify({ src: rendered && rendered.blobSrc, w: rendered && rendered.loadedSample }));
    check('「N 源」显示真实来源数（不再恒为 0 源）',
          rendered && rendered.blank === 0, JSON.stringify(rendered && rendered.sourceTexts));

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
