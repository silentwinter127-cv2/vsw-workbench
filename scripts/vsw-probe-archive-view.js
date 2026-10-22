/**
 * 探针：手机端能不能真正"打开"归档，以及归档文件夹（直通文件路径）是否可用。
 *
 * 背景（用户反馈）：
 *   1. 手机端归档只有「取消归档」，**没有任何打开入口** —— 看不到一个批次里有什么；
 *   2. 想要一个"直通归档文件路径"的按钮：归档原来只打状态标记，文件散在 media/results 里，
 *      没有"这一批的文件在哪"这回事。
 *
 * 修法：
 *   - 服务端给每个批次落盘一个文件夹 `data/archives/<id>-<名字>/`（imgs/ 用**硬链接**，
 *     同一卷不占额外空间；manifest.json 是可读清单）；
 *   - 新增 `GET /api/archive/{id}/paths?export=1` 与 `POST /api/reveal`（只在电脑上打开，
 *     且限制在数据目录以内）；
 *   - 手机端：批次卡有「查看（N）」列出该批对象（归档对象也进了快照，离线可看）、
 *     「归档文件夹」让电脑生成并弹出该目录（手机自己打不开电脑目录）。
 *
 * 这个探针验：
 *   A. 归档对象确实进了快照与本地库（否则线上也看不到内容）；
 *   B. 点「查看」能列出该批次的对象，且卡片带缩略图；
 *   C. 批次卡上有「查看」与「归档文件夹」两个按钮；
 *   D. 归档文件夹能被生成：目录存在、imgs/ 里有文件、manifest.json 可解析；
 *   E. 越界路径会被拒绝（防止 reveal 变成任意程序启动器）。
 *
 * 注意：为了让测试不打扰人，探针**不会真的点**「归档文件夹」（那会在电脑上弹出资源管理器），
 * 而是直接调用生成接口验证产物，并断言按钮存在。
 *
 * 用法：node vsw-probe-archive-view.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-arch-probe-'));
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-arch-profile-'));
  const port = 10050 + Math.floor(Math.random() * 80);
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
    await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)})`);
    await send('Page.navigate', { url: pageUrl });
    await sleep(2500);
    const ready = await evaluate('S.server');
    if (!ready) {
      await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)}); location.reload();`);
      await sleep(3000);
    }
    console.log(`      应用地址: ${(await evaluate('S.server')) || '(空)'}`);

    // 同步（含归档对象与缩略图）
    const synced = await evaluate(`(async () => {
      const snap = await api('/api/mobile/snapshot?images=thumbs&limit=300&trash=0&archives=1');
      for (const st of ['findings','trash','archived','archives','jobs','seeds','variants']) {
        await local.clear(st);
        for (const row of snap[st] || []) await local.put(st, row);
      }
      for (const row of snap.archived || []) {
        await fetchInto(KEY.archivedThumb(row.id), row.thumb_path || row.rel_path);
      }
      await local.put('meta', { key: 'snapshot', value: { at: Date.now(), counts: snap.counts } });
      await loadLocal();
      return { counts: snap.counts, archivedLocal: S.archived.length, batches: S.archives.length };
    })()`);
    check('快照里带上了归档对象（所以手机上才有内容可看）',
          synced && synced.counts.archived > 0, JSON.stringify(synced));
    check('归档对象写进了手机本地库', synced && synced.archivedLocal > 0,
          JSON.stringify(synced && { archivedLocal: synced.archivedLocal }));
    check('归档批次列表非空', synced && synced.batches > 0, String(synced && synced.batches));

    // 挑一个**确实有内容**的批次来验（库里可能有些批次的对象后来被恢复/清理了）
    const picked = await evaluate(`(async () => {
      const counts = {};
      for (const f of S.archived) counts[f.archive_id] = (counts[f.archive_id] || 0) + 1;
      const ranked = S.archives
        .map(a => ({ id: a.id, name: a.name, items: counts[a.id] || 0 }))
        .sort((a, b) => b.items - a.items);
      return { best: ranked[0] || null, ranked: ranked.slice(0, 3) };
    })()`);
    console.log(`      （候选批次：${JSON.stringify(picked && picked.ranked)}）`);
    check('存在一个含对象的批次可用于验证', picked && picked.best && picked.best.items > 0,
          JSON.stringify(picked && picked.best));
    const probeBatch = (picked && picked.best && picked.best.id) || 0;

    // 切到归档页，检查按钮
    const ui = await evaluate(`(async () => {
      switchTab('archive');
      await new Promise((r) => setTimeout(r, 1200));
      const cards = Array.from(document.querySelectorAll('#archiveList .card'));
      const first = cards[0] || null;
      return {
        cards: cards.length,
        openButtons: document.querySelectorAll('#archiveList [data-open]').length,
        folderButtons: document.querySelectorAll('#archiveList [data-folder]').length,
        firstLabel: first ? (first.querySelector('[data-open]') || {}).textContent : '',
      };
    })()`);
    check('每个批次都有「查看」按钮', ui && ui.openButtons === ui.cards && ui.cards > 0,
          JSON.stringify(ui));
    check('每个批次都有「归档文件夹」按钮', ui && ui.folderButtons === ui.cards && ui.cards > 0,
          JSON.stringify(ui));

    // 点「查看」：应打开面板并列出对象（带缩略图）
    await evaluate("window.__probeBatch = " + JSON.stringify(String(probeBatch)));
    const opened = await evaluate(`(async () => {
      const wanted = String(window.__probeBatch || '');
      const btn = document.querySelector('#archiveList [data-open="' + wanted + '"]')
        || document.querySelector('#archiveList [data-open]');
      if (!btn) return { error: '没有查看按钮' };
      btn.click();
      await new Promise((r) => setTimeout(r, 3000));
      const cards = Array.from(document.querySelectorAll('#archiveBatchList .fcard'));
      const imgs = cards.map((c) => c.querySelector('img'));
      return {
        sheetOpen: document.querySelector('#sheet').classList.contains('open'),
        title: document.querySelector('#sheetTitle').textContent,
        items: cards.length,
        withThumb: imgs.filter((i) => i && i.getAttribute('src')).length,
      };
    })()`);
    check('点「查看」能打开批次面板', opened && opened.sheetOpen
          && /归档/.test(opened.title || ''), JSON.stringify(opened));
    check('面板里列出了该批次的对象', opened && opened.items > 0, JSON.stringify(opened));
    check('对象带缩略图（不是灰块）', opened && opened.withThumb > 0
          && opened.withThumb >= Math.ceil(opened.items * 0.8), JSON.stringify(opened));

    // 归档文件夹：直接调接口验证产物（不点按钮，免得在你电脑上弹资源管理器）
    const folder = await evaluate(`(async () => {
      const batch = { id: ${probeBatch} };   // 由 Node 侧传入，避免依赖页面里的临时变量
      const info = await api('/api/archive/' + batch.id + '/paths?export=1');
      return { dir: info.dir, exists: info.exists, count: info.count,
               exported: info.exported, sample: (info.files || []).slice(0, 2) };
    })()`);
    check('归档文件夹被生成（目录存在且里面有文件）',
          folder && folder.exists && folder.count > 0, JSON.stringify(folder));
    check('导出明细说明用了硬链接 / 复用了已有文件（不占额外空间）',
          folder && folder.exported && folder.exported.images
          && (folder.exported.images.linked + folder.exported.images.copied
              + (folder.exported.images.reused || 0)) === folder.count,
          JSON.stringify(folder && folder.exported));
    check('文件是真实的归档图片路径',
          folder && folder.sample && folder.sample.every((p) => /imgs[\\/].+\.(jpg|jpeg|png|webp|gif)$/i.test(p)),
          JSON.stringify(folder && folder.sample));

    // manifest 可解析（在页面里取不到磁盘，用 node 侧校验 —— 目录路径由上面的接口给出）
    if (folder && folder.dir) {
      const manifestPath = path.join(folder.dir, 'manifest.json');
      let ok = false;
      let detail = '';
      try {
        const raw = fs.readFileSync(manifestPath, 'utf8');
        const data = JSON.parse(raw);
        ok = Array.isArray(data.items) && data.items.length > 0
          && data.items.every((it) => 'id' in it && 'title' in it);
        detail = `items=${(data.items || []).length}`;
      } catch (e) {
        detail = String(e.message || e);
      }
      check('文件夹里有可解析的 manifest.json（清单）', ok, detail);
    } else {
      check('文件夹里有可解析的 manifest.json（清单）', false, '没有目录');
    }

    // 越界路径必须被拒
    const guard = await evaluate(`(async () => {
      // 应用的 post() 不抛异常（只解析 JSON），所以要看返回体里有没有 error
      try {
        const res = await post('/api/reveal', { path: 'C:\\\\Windows' });
        return { rejected: !!(res && res.error), error: (res && res.error) || '' };
      } catch (e) { return { rejected: true, error: String(e.message || e).slice(0, 60) }; }
    })()`);
    check('reveal 只允许数据目录以内的路径（越界被拒）',
          guard && guard.rejected, JSON.stringify(guard));

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
