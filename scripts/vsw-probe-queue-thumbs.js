/**
 * 探针：队列页的缩略图。
 *
 * 背景（用户实测反馈）：队列里有的条目有图、有的是空白，而且"看着有图"的那几张
 * 其实不是这条队列项对应的变体图。
 *
 * 查到的两个真 bug：
 *   1. 变体表没有缩略图列，快照的 thumbs 档又把变体唯一的 rel_path 清空了 ——
 *      于是队列根本拿不到"这一条派发的是哪个变体"的图；
 *   2. 结果/回收站/变体三张表 id 大量重号（实测结果↔变体重叠 39 个、
 *      回收站↔变体重叠 99 个），而本地图片键写作 `thumb:{id}`，
 *      于是"变体 5 的图"和"结果 5 的图"是同一个键 —— 会显示成**别的图**或空白。
 *
 * 修法：服务端加 /api/variant/{id}/thumb 按需生成；本地键加命名空间
 * （fthumb/tthumb/sthumb/vthumb），DB 升到 v2 并清掉旧的有歧义的键。
 *
 * 这个探针验：
 *   A. 同步后队列卡片绝大多数有图（不是灰块）；
 *   B. 有变体的队列项，取的是 **变体自己的图**（vthumb 键存在），不是种子的；
 *   C. id 撞车时两种图能共存且内容不同（这条是那个 bug 的回归测试）；
 *   D. 把本地缩略图全删掉后，队列页能自动补回来。
 *
 * 用法：node vsw-probe-queue-thumbs.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-queue-probe-'));
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-queue-profile-'));
  const port = 9850 + Math.floor(Math.random() * 80);
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
    // 地址没落下就再刷一次（boot 只在加载时读 localStorage）
    const ready = await evaluate(`({ server: S.server })`);
    if (!ready || !ready.server) {
      await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)}); location.reload();`);
      await sleep(3000);
    }
    console.log(`      应用地址: ${(await evaluate('S.server')) || '(空)'}`);

    // 走应用自己的同步流程（含变体缩略图下载）
    const synced = await evaluate(`(async () => {
      const snap = await api('/api/mobile/snapshot?images=thumbs&limit=300&trash=0&archives=1');
      for (const st of ['findings','trash','archives','jobs','seeds','variants']) {
        await local.clear(st);
        for (const row of snap[st] || []) await local.put(st, row);
      }
      const used = new Set((snap.jobs || []).map(j => j.variant_id).filter(Boolean));
      let got = 0;
      for (const v of snap.variants || []) {
        if (!used.has(v.id)) continue;
        if (await fetchInto(KEY.variantThumb(v.id), v.thumb || '')) got += 1;
      }
      for (const sd of snap.seeds || []) await fetchInto(KEY.seedThumb(sd.id), sd.thumb_path || sd.rel_path);
      for (const f of snap.findings || []) await fetchInto(KEY.findingThumb(f.id), f.thumb_path || f.rel_path);
      await local.put('meta', { key: 'snapshot', value: { at: Date.now(), counts: snap.counts } });
      await loadLocal();
      return { jobs: (snap.jobs||[]).length, variantsUsed: used.size, variantThumbs: got,
               seeds: (snap.seeds||[]).length, findings: (snap.findings||[]).length };
    })()`);
    check('同步拉到变体缩略图（队列靠它显示"派发的是哪个变体"）',
          synced && synced.variantThumbs > 0, JSON.stringify(synced));

    // A + B：切到队列页，等补图
    const queue = await evaluate(`(async () => {
      switchTab('queue');
      await new Promise((r) => setTimeout(r, 12000));
      const cards = Array.from(document.querySelectorAll('#queueList .job'));
      const imgs = cards.map(c => c.querySelector('img'));
      const withSrc = imgs.filter(i => i && i.getAttribute('src')).length;
      const loaded = imgs.filter(i => i && (i.naturalWidth || 0) > 0).length;
      // 当前显示的队列项（与 DOM 同序）
      const rows = Array.from(document.querySelectorAll('#queueList .job')).map(c => Number(c.dataset.id));
      return { cards: cards.length, withSrc, loaded,
               sampleSrc: (imgs.find(i => i && i.getAttribute('src')) || {}).src || '' };
    })()`);
    check('队列卡片都渲染出来了', queue && queue.cards > 0, JSON.stringify(queue));
    check('绝大多数队列项有缩略图（不是灰块）',
          queue && queue.withSrc >= Math.ceil(queue.cards * 0.9),
          `${queue && queue.withSrc}/${queue && queue.cards}`);
    check('缩略图是真实图片数据（naturalWidth > 0）',
          queue && queue.loaded >= Math.ceil(queue.cards * 0.9),
          `${queue && queue.loaded}/${queue && queue.cards}`);
    check('用的是 blob: 地址（来自本地库，不是直连电脑）',
          queue && queue.sampleSrc.startsWith('blob:'), queue && queue.sampleSrc.slice(0, 24));

    // B：有变体的队列项优先取变体图
    const variantUse = await evaluate(`(async () => {
      const rows = S.jobs.filter(j => j.variant_id);
      let withVariantThumb = 0, usedVariant = 0;
      for (const j of rows.slice(0, 40)) {
        const stored = await local.get('assets', KEY.variantThumb(j.variant_id));
        if (stored && stored.blob) withVariantThumb += 1;
        const resolved = await resolveJobThumb(j);
        if (resolved.key === KEY.variantThumb(j.variant_id)) usedVariant += 1;
      }
      return { checked: Math.min(40, rows.length), withVariantThumb, usedVariant };
    })()`);
    check('有变体的队列项优先显示变体自己的图（而不是种子的图）',
          variantUse && variantUse.usedVariant > 0
          && variantUse.usedVariant === variantUse.withVariantThumb,
          JSON.stringify(variantUse));

    // C：id 撞车时两种图必须能共存且内容不同（回归测试）
    const collision = await evaluate(`(async () => {
      const overlap = S.findings.map(f => f.id).filter(id => S.variants.some(v => v.id === id));
      if (!overlap.length) return { overlap: 0, note: '本地库里没有撞号，跳过' };
      const id = overlap[0];
      const fRec = await local.get('assets', KEY.findingThumb(id));
      const vRec = await local.get('assets', KEY.variantThumb(id));
      const sameKey = KEY.findingThumb(id) === KEY.variantThumb(id);
      const sizes = [fRec && fRec.blob ? fRec.blob.size : 0, vRec && vRec.blob ? vRec.blob.size : 0];
      return { overlap: overlap.length, id, sameKey, sizes,
               coexist: !!(fRec && vRec), differ: sizes[0] !== sizes[1] };
    })()`);
    if (!collision || !collision.overlap) {
      console.log('      （本地库里结果与变体没有重号 id，跳过撞号断言）');
    } else {
      check('结果图与变体图的键不再相同（同 id 也不会互相覆盖）',
            collision.sameKey === false, JSON.stringify(collision));
    }
    if (collision && collision.coexist) {
      check('同 id 的结果图与变体图能共存，且内容不同（这正是之前显示错图的原因）',
            collision.differ, JSON.stringify(collision));
    } else {
      console.log('      （本机数据里没有同时具备两张图的撞号 id，跳过共存断言）');
    }

    // D：删掉本地队列缩略图，看是否自动补回
    const wiped = await evaluate(`(async () => {
      const all = await local.all('assets');
      let n = 0;
      for (const a of all) {
        if (/^(vthumb|sthumb):/.test(a.key)) { await local.del('assets', a.key); n++; }
      }
      S._thumbs = {};
      return { cleared: n };
    })()`);
    console.log(`      （清掉了 ${wiped && wiped.cleared} 张本地队列缩略图）`);
    const repaired = await evaluate(`(async () => {
      renderQueue();
      await new Promise((r) => setTimeout(r, 14000));
      const imgs = Array.from(document.querySelectorAll('#queueList .job img'));
      return { cards: imgs.length, withSrc: imgs.filter(i => i.getAttribute('src')).length };
    })()`);
    check('本地队列缩略图被清掉后会自动补回来',
          repaired && repaired.withSrc >= Math.ceil(repaired.cards * 0.9),
          `${repaired && repaired.withSrc}/${repaired && repaired.cards}`);

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
