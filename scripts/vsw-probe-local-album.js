/* 手机相册：不连电脑也能在「结果板」里看见自己在手机上搜到的图。
 *
 * 用户原话（m05939）：「为什么一定要连电脑，再同步才能看到结果板，即使我在本地
 * 没有 SigLIP 识别，但是也有图片啊，可以不显示它的各项指标，就当作一个相册」。
 *
 * 所以这个探针完全不给服务器地址（真离线），只走真实的「手机搜 → 存进结果板」，
 * 检查：图进本地库、结果板里能看见、没有 E/来源这些算不出来的指标、详情说得清
 * 为什么没有指标、能删、重载之后还在。
 *
 * 用法：node scripts/vsw-probe-local-album.js --page vsw/static/mobile/index.html
 *       node scripts/vsw-probe-local-album.js --apk vsw-workbench.apk
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-album-'));
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-album-profile-'));
  const port = 10780 + Math.floor(Math.random() * 40);
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
        return {
          __error: res.result.exceptionDetails.text,
          desc: res.result.exceptionDetails.exception && res.result.exceptionDetails.exception.description,
        };
      }
      return res.result && res.result.result ? res.result.result.value : null;
    };

    await send('Page.navigate', { url: pageUrl });
    await sleep(2600);
    // 真离线：清掉服务器地址与身份，再重载
    await evaluate(`(() => {
      localStorage.removeItem('vsw.server');
      localStorage.removeItem('vsw.sid');
      localStorage.removeItem('vsw.key');
      window.__ERRS = [];
      window.addEventListener('error', (e) => window.__ERRS.push(String(e.message)));
      window.addEventListener('unhandledrejection', (e) => window.__ERRS.push(String(e.reason)));
      return 1;
    })()`);
    await send('Page.reload');
    await sleep(2800);
    await evaluate(`(() => {
      window.__ERRS = [];
      window.addEventListener('error', (e) => window.__ERRS.push(String(e.message)));
      window.addEventListener('unhandledrejection', (e) => window.__ERRS.push(String(e.reason)));
      window.AndroidBridge = { buildTag: () => 'probe', webSearchAvailable: () => '1', webSearch: () => {},
        checkUpdate: () => {}, installUpdate: () => {} };
      const skip = document.querySelector('#introSkip');
      if (skip) skip.click();
      const intro = document.querySelector('#intro');
      if (intro) intro.classList.add('hide');
      return 1;
    })()`);
    await sleep(300);

    const offline = await evaluate(`(() => ({ server: S.server, online: S.online }))()`);
    check('完全离线启动（没有电脑地址）', !!(offline && offline.server === '' && offline.online === false), JSON.stringify(offline));

    // 造三张真图（data URL），走真实的「手机搜结果回传 → 存进结果板」
    const saved = await evaluate(`(async () => {
      const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
      const similar = [1, 2, 3].map((i) => ({
        u: 'https://mms0.baidu.com/it/u=probe' + i, d: png, w: 300 + i, h: 200 + i, page: 'https://example.com/p' + i,
      }));
      S.phoneSearch = { jobId: -1, engine: 'baidu', label: '百度识图（手机内）' };
      window.vswWebSearchResult(JSON.stringify({ similar, sources: [], goods: [], guess: '探针图',
        diag: { pcsimi: 3, viaPcsimi: 3, source: 'pcsimi', hosts: {} } }));
      return { opened: !!document.querySelector('#saveResults') };
    })()`);
    check('手机搜结果面板打开了', !!(saved && saved.opened), JSON.stringify(saved));

    const clicked = await evaluate(`(() => {
      const btn = document.querySelector('#saveResults');
      if (!btn) return { none: true };
      btn.click();
      return { text: btn.textContent.trim() };
    })()`);
    await sleep(1500);
    const afterSave = await evaluate(`(async () => {
      const rows = await local.all('findings');
      const localRows = rows.filter((f) => f.local === 1);
      const cards = $$('#boardList .fcard[data-local="1"]');
      const first = cards[0] || null;
      const img = first ? first.querySelector('img') : null;
      return {
        tab: S.tab, localRows: localRows.length, ids: localRows.map((f) => f.id),
        cards: cards.length,
        src: img ? String(img.src).slice(0, 12) : '',
        naturalOk: !!(img && img.naturalWidth > 0),
        text: first ? first.innerText.replace(/\\s+/g, ' ') : '',
        toast: (document.querySelector('#toast') || {}).textContent || '',
      };
    })()`);
    if (afterSave && afterSave.__error) console.log('  （存进相册求值出错）', afterSave.desc || afterSave.__error);
    check('「存进结果板」在离线时也能收下（按钮可点）', !!(clicked && clicked.text), JSON.stringify(clicked));
    check('三张图进了手机本地库（id 为负、local:1）',
      !!(afterSave && afterSave.localRows === 3 && afterSave.ids.every((i) => i < 0)), JSON.stringify(afterSave && afterSave.ids));
    check('自动切到了结果板', !!(afterSave && afterSave.tab === 'board'), JSON.stringify(afterSave && afterSave.tab));
    check('结果板里出现三张（不连电脑也看得见）', !!(afterSave && afterSave.cards === 3), JSON.stringify(afterSave && afterSave.cards));
    check('卡片上是本地缩略图（blob:）', !!(afterSave && String(afterSave.src).indexOf('blob:') === 0), JSON.stringify(afterSave && afterSave.src));
    check('图真的解码出像素', !!(afterSave && afterSave.naturalOk), JSON.stringify(afterSave && afterSave.naturalOk));
    check('卡片标着「手机上」「本地相册」', !!(afterSave && /手机上/.test(afterSave.text) && /本地相册/.test(afterSave.text)), afterSave && afterSave.text);
    check('卡片上不显示算不出来的指标（没有 E / 源）', !!(afterSave && !/E \\d/.test(afterSave.text) && !/\\d+ 源/.test(afterSave.text)), afterSave && afterSave.text);

    // 详情：说清楚为什么没有指标，并从底栏能删
    const detail = await evaluate(`(async () => {
      const card = document.querySelector('#boardList .fcard[data-local="1"]');
      card.click();
      await new Promise((r) => setTimeout(r, 700));
      const body = document.querySelector('#sheetBody').innerText.replace(/\\s+/g, ' ');
      const foot = document.querySelector('#sheetFoot');
      const del = document.querySelector('#localDel');
      const r = del ? del.getBoundingClientRect() : null;
      const hit = r ? document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) : null;
      return { body, footVisible: !!(foot && !foot.classList.contains('hide')),
               hasDel: !!del, delOnTop: !!(hit && (hit === del || del.contains(hit))),
               heroOk: (() => { const i = document.querySelector('#sheetBody img.hero'); return !!(i && i.naturalWidth > 0); })() };
    })()`);
    if (detail && detail.__error) console.log('  （打开详情求值出错）', detail.desc || detail.__error);
    check('详情里说明了「指标要电脑算」', !!(detail && /SigLIP/.test(detail.body) && /电脑/.test(detail.body)), (detail && detail.body || '').slice(0, 90));
    check('详情有大图', !!(detail && detail.heroOk), JSON.stringify(detail && detail.heroOk));
    check('删掉按钮在固定底栏、并且能点中', !!(detail && detail.footVisible && detail.hasDel && detail.delOnTop), JSON.stringify(detail && { f: detail.footVisible, d: detail.hasDel, t: detail.delOnTop }));

    const removed = await evaluate(`(async () => {
      document.querySelector('#localDel').click();
      await new Promise((r) => setTimeout(r, 900));
      const rows = await local.all('findings');
      return { rows: rows.filter((f) => f.local === 1).length,
               cards: $$('#boardList .fcard[data-local="1"]').length };
    })()`);
    check('删掉之后本地库与板子都少一张', !!(removed && removed.rows === 2 && removed.cards === 2), JSON.stringify(removed));

    // 重载：还在（真的落盘了，不是内存里的一闪）
    await send('Page.reload');
    await sleep(3000);
    const afterReload = await evaluate(`(async () => {
      switchTab('board');
      await new Promise((r) => setTimeout(r, 400));
      const rows = await local.all('findings');
      const cards = $$('#boardList .fcard[data-local="1"]');
      const img = cards[0] ? cards[0].querySelector('img') : null;
      return { rows: rows.filter((f) => f.local === 1).length, cards: cards.length,
               ok: !!(img && img.naturalWidth > 0) };
    })()`);
    check('重载之后相册还在（不靠内存）', !!(afterReload && afterReload.cards === 2), JSON.stringify(afterReload));
    check('重载之后图还能显示', !!(afterReload && afterReload.ok), JSON.stringify(afterReload && afterReload.ok));

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
