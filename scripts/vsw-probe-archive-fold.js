/* 归档页「倒序 + 折叠」的手机端回归（用户要求：便于查找）。

   为什么要有这个探针：归档批次会越来越多（真实库里已经 22 批），
   从最老的往下翻找不动。所以要：
     · 最近归档的排最上面（按 created_at 倒序，同时间按 id 倒序）
     · 批次卡默认折叠，只留标题行（名字 / 对象数 / 日期 / 箭头）
     · 点标题行展开、再点收起；搜索框按名字/备注/编号过滤；全展开/全收起

   不登录、不需要真服务：把 APK 里的内置页面解出来，file:// 打开，塞假的 S.archives。
   用法：node scripts/vsw-probe-archive-fold.js --apk vsw-workbench.apk
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
    path.join(pf86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-fold-'));
  let pageUrl;
  if (args.page) {
    pageUrl = pathToFileURL(path.resolve(args.page)).href;
  } else {
    extractPage(args.apk, dir);
    pageUrl = pathToFileURL(path.join(dir, 'index.html')).href;
  }

  const exe = findBrowser();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-fold-profile-'));
  const port = 10610 + Math.floor(Math.random() * 60);
  const child = spawn(exe, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--hide-scrollbars',
    '--allow-file-access-from-files',
  ], { stdio: 'ignore' });

  let pass = 0;
  let fail = 0;
  const check = (label, ok, detail) => {
    if (ok) { pass++; console.log(`  ok   ${label}`); }
    else { fail++; console.log(`  FAIL ${label}${detail ? ' :: ' + String(detail).slice(0, 200) : ''}`); }
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
      if (res.result && res.result.exceptionDetails) {
        const d = res.result.exceptionDetails;
        return { __error: (d.exception && d.exception.description) || d.text };
      }
      return res.result && res.result.result ? res.result.result.value : null;
    };

    console.log(`内核 ${version.Browser}\n页面 ${pageUrl}\n`);
    await send('Page.navigate', { url: pageUrl });
    await sleep(2500);

    // 三批：created_at 故意乱序，看渲染有没有排好
    const seeded = await evaluate(`(() => {
      S.archives = [
        { id: 201, name: '最老的一批', findings: 2, trashed: 0, shownSources: 4, platforms: 1,
          date_from: '2026-10-01', date_to: '2026-10-01', created_at: 1790000000,
          note: '归档 2 个视觉对象 / 4 条来源记录' },
        { id: 203, name: '最新的一批', findings: 5, trashed: 1, shownSources: 9, platforms: 3,
          date_from: '2026-10-24', date_to: '2026-10-24', created_at: 1793000000 },
        { id: 202, name: '中间的一批', findings: 0, trashed: 0, shownSources: 0, platforms: 0,
          date_from: '2026-10-10', date_to: '2026-10-10', created_at: 1791500000, empty: true },
      ];
      S.archived = [
        { id: 9201, archive_id: 201, title: '甲', explore_value: 0.7, sources: [{}, {}] },
        { id: 9202, archive_id: 201, title: '乙', explore_value: 0.6, sources: [{}, {}] },
        { id: 9203, archive_id: 203, title: '丙', explore_value: 0.9, sources: [{}, {}] },
      ];
      S.archiveOpen = new Set();
      S.archiveQuery = '';
      S.albumPick = new Set();
      switchTab('archive');
      const ids = [...document.querySelectorAll('#archiveList [data-batch]')].map((c) => Number(c.dataset.batch));
      return { ids, cards: ids.length };
    })()`);
    check('归档页渲染出 3 张批次卡', seeded && seeded.cards === 3, JSON.stringify(seeded));
    check('最近归档的排最上面（倒序）',
      seeded && JSON.stringify(seeded.ids) === JSON.stringify([203, 202, 201]), JSON.stringify(seeded && seeded.ids));

    const folded = await evaluate(`(() => {
      const cards = [...document.querySelectorAll('#archiveList [data-batch]')];
      const bodyShown = cards.map((c) => {
        const b = c.querySelector('.cardbody');
        return b ? getComputedStyle(b).display !== 'none' : null;
      });
      const head = cards[0].querySelector('.foldhead');
      return { bodyShown, chev: head.querySelector('.chev').textContent,
               headText: head.textContent.replace(/\\s+/g, ' ').trim(),
               hasOpen: cards.map((c) => c.classList.contains('open')) };
    })()`);
    check('默认全部折叠（正文都不显示）',
      folded && folded.bodyShown.every((v) => v === false), JSON.stringify(folded && folded.bodyShown));
    check('折叠时箭头是 ▸', folded && folded.chev === '▸', folded && folded.chev);
    check('标题行就带对象数与日期',
      folded && /最新的一批/.test(folded.headText) && /5\+1 个/.test(folded.headText)
        && /2026/.test(folded.headText), folded && folded.headText);

    const expanded = await evaluate(`(() => {
      const card = document.querySelector('[data-batch="203"]');
      card.querySelector('.foldhead').click();
      const body = card.querySelector('.cardbody');
      return { open: card.classList.contains('open'), display: getComputedStyle(body).display,
               chev: card.querySelector('.chev').textContent,
               bodyText: body.textContent.replace(/\\s+/g, ' ').trim() };
    })()`);
    check('点标题行能展开', expanded && expanded.open === true, JSON.stringify(expanded && expanded.open));
    check('展开后正文可见', expanded && expanded.display !== 'none', expanded && expanded.display);
    check('展开后箭头变 ▾', expanded && expanded.chev === '▾', expanded && expanded.chev);
    check('正文里有对象/来源/日期与按钮',
      expanded && /对象 5/.test(expanded.bodyText) && /来源 9/.test(expanded.bodyText)
        && /查看（1）/.test(expanded.bodyText), expanded && expanded.bodyText);

    const refolded = await evaluate(`(() => {
      const card = document.querySelector('[data-batch="203"]');
      card.querySelector('.foldhead').click();
      const body = card.querySelector('.cardbody');
      return { open: card.classList.contains('open'), display: getComputedStyle(body).display };
    })()`);
    check('再点一次收起', refolded && refolded.open === false && refolded.display === 'none',
      JSON.stringify(refolded));

    const search = await evaluate(`(() => {
      const input = document.querySelector('#archiveQuery');
      input.value = '中间';
      input.dispatchEvent(new Event('input'));
      return new Promise((resolve) => setTimeout(() => {
        const ids = [...document.querySelectorAll('#archiveList [data-batch]')].map((c) => Number(c.dataset.batch));
        resolve({ ids, empty: document.querySelector('#archiveList .empty') ? true : false });
      }, 400));
    })()`);
    check('搜索「中间」只剩那一条',
      search && JSON.stringify(search.ids) === JSON.stringify([202]), JSON.stringify(search && search.ids));

    const nohit = await evaluate(`(() => {
      const input = document.querySelector('#archiveQuery');
      input.value = 'zzz-不存在';
      input.dispatchEvent(new Event('input'));
      return new Promise((resolve) => setTimeout(() => {
        resolve({ cards: document.querySelectorAll('#archiveList [data-batch]').length,
                  emptyText: (document.querySelector('#archiveList .empty') || {}).textContent || '' });
      }, 400));
    })()`);
    check('搜不到时给「没有匹配的批次」', nohit && nohit.cards === 0 && /没有匹配/.test(nohit.emptyText || ''),
      JSON.stringify(nohit));

    const bulk = await evaluate(`(() => {
      // 直接改状态再重画，别等输入防抖（等的话会是空列表，后面的勾选就找不到元素）
      S.archiveQuery = '';
      renderArchive();
      document.querySelector('#archiveExpandAll').click();
      const allOpen = [...document.querySelectorAll('#archiveList [data-batch]')].every((c) => c.classList.contains('open'));
      document.querySelector('#archiveFoldAll').click();
      const allFolded = [...document.querySelectorAll('#archiveList [data-batch]')].every((c) => !c.classList.contains('open'));
      return { allOpen, allFolded, openCount: S.archiveOpen.size };
    })()`);
    check('「全展开」把可见批次都打开', bulk && bulk.allOpen === true, JSON.stringify(bulk && bulk.allOpen));
    check('「全收起」把都折起来并清空记录', bulk && bulk.allFolded === true && bulk.openCount === 0,
      JSON.stringify(bulk));

    const pick = await evaluate(`(() => {
      const box = document.querySelector('[data-pick="203"]');
      box.checked = true;
      box.dispatchEvent(new Event('change'));
      const card = document.querySelector('[data-batch="203"]');
      const stillFolded = !card.classList.contains('open');
      return { picked: [...S.albumPick], stillFolded, lit: card.classList.contains('is-picked') };
    })()`);
    check('勾选不会顺带展开（点的是复选框）',
      pick && pick.stillFolded === true && pick.picked.includes(203), JSON.stringify(pick));
    check('勾选照样高亮卡片', pick && pick.lit === true, JSON.stringify(pick && pick.lit));

    const err = await evaluate('window.__lastErr || ""');
    check('没有未捕获异常', !err, String(err));
  } finally {
    try { child.kill(); } catch (e) { /* ignore */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* ignore */ }
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ }
    console.log(`\n${pass} 通过 / ${fail} 失败`);
    process.exitCode = fail ? 1 : 0;
  }
})();
