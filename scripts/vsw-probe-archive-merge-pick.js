/* 归档「合并相册」的手机端勾选回归（用户报过：手机上没法选择归档进行合并）。

   背景：勾到空批次时，代码把「合并查看」设成 disabled，但手机端只有 `.btn[disabled]`
   这条属性选择器 —— 而 JS 的 `el.disabled = true` 只是设属性、不写 HTML 特性，所以
   选择器匹配不到，按钮看着还是亮的蓝按钮，点了没反应。用户结论就是「手机不能合并」。

   这个探针不需要登录、不需要真服务：把 APK 里的内置页面解出来，用 file:// 打开，
   直接塞一份假的 S.archives / S.archived，然后按用户的操作点。

   用法：node scripts/vsw-probe-archive-merge-pick.js --apk vsw-workbench.apk
*/
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const { pathToFileURL } = require('url');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseArgs(argv) {
  const out = { apk: 'vsw-workbench.apk' };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--apk') out.apk = argv[++i];
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
    path.join(pf86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    local && path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe'),
  ].filter(Boolean);
  for (const p of cands) {
    try { if (fs.existsSync(p)) return p; } catch (e) { /* ignore */ }
  }
  throw new Error('找不到 Edge/Chrome');
}

/* 用 python 从 APK 里解出内置页面（Node 没有 zip 库，也不依赖第三方包） */
function extractPage(apk, outDir) {
  const script = `
import json, sys, zipfile
z = zipfile.ZipFile(${JSON.stringify(path.resolve(apk))})
name = 'assets/mobile/index.html'
data = z.read(name)
open(${JSON.stringify(path.join(outDir, 'index.html'))}, 'wb').write(data)
print(json.dumps({'bytes': len(data)}))
`;
  const { execFileSync } = require('child_process');
  const raw = execFileSync('python', ['-c', script], { encoding: 'utf8' });
  return JSON.parse(raw.trim().split('\n').pop());
}

(async () => {
  const args = parseArgs(process.argv);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-mergepick-'));
  const extracted = extractPage(args.apk, dir);
  const pageUrl = pathToFileURL(path.join(dir, 'index.html')).href;

  const exe = findBrowser();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-mergepick-profile-'));
  const port = 10540 + Math.floor(Math.random() * 60);
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
    const target = targets.find((t) => t.type === 'page');
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
    await send('Page.navigate', { url: pageUrl });
    await sleep(2500);

    // 直接喂数据：两批有对象、一批是空的（真实数据里第 5–12 批就是空的）
    const seeded = await evaluate(`(() => {
      S.archives = [
        { id: 101, name: '有对象的一批', findings: 3, trashed: 0, sources: 6, platforms: 2,
          date_from: '2026-10-20', date_to: '2026-10-20', created_at: 1792000000 },
        { id: 102, name: '也有对象', findings: 2, trashed: 0, sources: 4, platforms: 1,
          date_from: '2026-10-21', date_to: '2026-10-21', created_at: 1792100000 },
        { id: 103, name: '空批次', findings: 0, trashed: 0, sources: 0, platforms: 0,
          date_from: '2026-10-16', date_to: '2026-10-16', created_at: 1791500000, empty: true },
      ];
      S.archived = [
        { id: 9001, archive_id: 101, title: '甲', explore_value: 0.8, triage: 'keep', sources: [{}, {}] },
        { id: 9002, archive_id: 101, title: '乙', explore_value: 0.7, triage: 'keep', sources: [{}, {}] },
        { id: 9003, archive_id: 101, title: '丙', explore_value: 0.6, triage: 'review', sources: [{}, {}] },
        { id: 9004, archive_id: 102, title: '丁', explore_value: 0.9, triage: 'keep', sources: [{}, {}] },
        { id: 9005, archive_id: 102, title: '戊', explore_value: 0.5, triage: 'fanout', sources: [{}, {}] },
      ];
      S.albumPick = new Set();
      switchTab('archive');
      return { cards: document.querySelectorAll('#archiveList .card').length };
    })()`);
    check('归档页渲染出 3 张批次卡', seeded && seeded.cards === 3, JSON.stringify(seeded));

    const idle = await evaluate(`(() => {
      const look = document.querySelector('#archiveMergeLook');
      return { text: look.textContent, disabled: look.disabled,
               opacity: Number(getComputedStyle(look).opacity) };
    })()`);
    check('没勾选时合并按钮是禁用的', idle && idle.disabled === true, JSON.stringify(idle));
    check('禁用的按钮看起来就该是灰的（:disabled 生效）',
          idle && idle.opacity < 0.8, JSON.stringify(idle));

    // 1. 勾空批次：按钮必须可点、文案带（N）、提示说清"空批次"
    const empty = await evaluate(`(() => {
      const box = document.querySelector('[data-pick="103"]');
      box.checked = true;
      box.dispatchEvent(new Event('change'));
      const look = document.querySelector('#archiveMergeLook');
      const hint = document.querySelector('#archiveMergeHint');
      return { text: look.textContent, disabled: look.disabled, hint: hint.textContent,
               picked: [...S.albumPick], items: mergedAlbumItems().length,
               cardLit: document.querySelector('[data-batch="103"]').classList.contains('is-picked') };
    })()`);
    check('勾空批次后按钮变成可点（不再"看着亮、点不动"）',
          empty && empty.disabled === false, JSON.stringify(empty));
    check('按钮文案带上勾选数（1）', empty && /（1）/.test(empty.text || ''), empty && empty.text);
    check('提示说清「空批次 / 没有对象」',
          empty && /空批次/.test(empty.hint || '') && /没有对象/.test(empty.hint || ''), empty && empty.hint);
    check('勾中的卡片有高亮', empty && empty.cardLit === true, JSON.stringify(empty && empty.cardLit));

    // 2. 再勾两批有对象的：计数、去重、排序
    const two = await evaluate(`(() => {
      for (const id of [101, 102]) {
        const box = document.querySelector('[data-pick="' + id + '"]');
        box.checked = true;
        box.dispatchEvent(new Event('change'));
      }
      const look = document.querySelector('#archiveMergeLook');
      return { text: look.textContent, disabled: look.disabled,
               picked: [...S.albumPick], items: mergedAlbumItems().length,
               hint: document.querySelector('#archiveMergeHint').textContent };
    })()`);
    check('再勾两批后按钮带（3）', two && /（3）/.test(two.text || ''), two && two.text);
    check('合并对象数等于「有对象的那两批」之和（5）', two && two.items === 5, JSON.stringify(two && two.items));
    check('提示报出对象总数', two && /共 5 个对象/.test(two.hint || ''), two && two.hint);

    // 3. 只勾两批有对象的：点「合并查看」出面板
    const panel = await evaluate(`(async () => {
      S.albumPick = new Set([101, 102]);
      document.querySelectorAll('[data-pick]').forEach((b) => {
        b.checked = S.albumPick.has(Number(b.dataset.pick));
      });
      syncAlbumBar();
      await openMergedAlbum();
      const title = document.querySelector('#sheetTitle').textContent;
      const body = document.querySelector('#sheetBody').textContent;
      return { title, cards: document.querySelectorAll('#archiveBatchList .fcard').length,
               bodyHead: body.slice(0, 80) };
    })()`);
    check('点合并查看弹出面板且标题写明合并了哪几批',
          panel && /合并相册/.test(panel.title) && /#101/.test(panel.title) && /#102/.test(panel.title),
          JSON.stringify(panel && panel.title));
    check('面板里列出 5 个对象', panel && panel.cards === 5, JSON.stringify(panel && panel.cards));
    check('面板统计写清批次/对象数', panel && /批次 2/.test(panel.bodyHead) && /对象 5/.test(panel.bodyHead),
          panel && panel.bodyHead);

    // 4. 只勾空批次时点「合并查看」：给明确提示，不是空面板
    const onlyEmpty = await evaluate(`(async () => {
      S.albumPick = new Set([103]);
      syncAlbumBar();
      await openMergedAlbum();
      const toastEl = document.querySelector('#toast, .toast');
      return { title: document.querySelector('#sheetTitle').textContent,
               body: document.querySelector('#sheetBody').textContent,
               toast: toastEl ? toastEl.textContent : '' };
    })()`);
    check('只勾空批次时面板里也有"没有可显示的对象"这句',
          onlyEmpty && /没有可显示的对象/.test(onlyEmpty.body || ''), JSON.stringify(onlyEmpty && onlyEmpty.body));

    // 5. 指标「已归档」用的是归档库的条数，不是从 S.findings 里过滤（那样永远是 0）
    const metrics = await evaluate(`(() => {
      S.findings = [];
      S.trash = [];
      S.archives = S.archives;
      renderArchive();
      return [...document.querySelectorAll('#archiveMetrics .metric')]
        .map((m) => [m.querySelector('.l').textContent, m.querySelector('.n').textContent]);
    })()`);
    const archivedMetric = (metrics || []).find((m) => m[0] === '已归档');
    check('「已归档」不再是 0（用 S.archived.length）',
          archivedMetric && archivedMetric[1] === '5', JSON.stringify(metrics));

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
