#!/usr/bin/env node
/**
 * 探针：手机相册里的图能不能真的推回电脑（r41）
 *
 * 背景：用户报「已经在结果版的图推不进电脑」。根因是推送时把 `image_url`
 * 原样发给电脑，让电脑再去百度下载 —— 电脑那边不一定拿得到（缓存/防盗链/
 * 图被删），于是 new_findings 与 duplicates 都是 0，手机上什么提示都没有。
 * 现在推送前先在手机上把图转成 data URL，并给结果板一个明确的「推到电脑（N）」按钮。
 *
 * 这里故意把 image_url 指向一个**打不开的域名**：只有 data URL 那条路能成。
 *
 * 需要一个真服务（要登录）：
 *   node scripts/vsw-probe-album-push.js --page vsw/static/mobile/index.html \
 *     --origin http://127.0.0.1:8795 --sid <session token>
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let PASS = 0;
const FAILS = [];
function check(name, cond, info) {
  if (cond) {
    PASS += 1;
    console.log('  ok    ' + name);
  } else {
    FAILS.push(name);
    console.log('  FAIL  ' + name + (info === undefined ? '' : ' :: ' + info));
  }
}

function parseArgs(argv) {
  const out = { page: 'vsw/static/mobile/index.html', apk: '', origin: 'http://127.0.0.1:8795', sid: '', port: 10711 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--page') out.page = argv[++i];
    else if (a === '--apk') out.apk = argv[++i];
    else if (a === '--origin') out.origin = argv[++i];
    else if (a === '--sid') out.sid = argv[++i];
    else if (a === '--port') out.port = Number(argv[++i]);
  }
  return out;
}

function findBrowser() {
  const cands = [
    path.join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
  ];
  for (const p of cands) {
    try { if (fs.existsSync(p)) return p; } catch (e) { /* ignore */ }
  }
  throw new Error('找不到 Edge / Chrome');
}

/** 从 APK 里抽出手机页面（交给 Python 的 zipfile，别自己解析 ZIP）。 */
function pageFromApk(apkPath) {
  const { execFileSync } = require('child_process');
  const out = path.join(os.tmpdir(), 'vsw-album-push-page.html');
  const code = [
    'import sys, zipfile',
    'src, dst = sys.argv[1], sys.argv[2]',
    'z = zipfile.ZipFile(src)',
    'data = z.read("assets/mobile/index.html")',
    'open(dst, "wb").write(data)',
    'print(len(data))',
  ].join('\n');
  const printed = execFileSync('python', ['-c', code, apkPath, out], { encoding: 'utf8' });
  if (!String(printed).trim()) throw new Error('APK 里没有 assets/mobile/index.html');
  return out;
}

(async () => {
  const args = parseArgs(process.argv.slice(2));
  if (!args.sid) {
    console.error('需要一个已登录的会话：--sid <token>（先 POST /api/auth/setup 或 /login）');
    process.exit(2);
  }
  const pagePath = args.apk ? pageFromApk(args.apk) : path.resolve(args.page);
  const port = args.port;
  const child = spawn(findBrowser(), [
    '--headless=new', `--remote-debugging-port=${port}`,
    `--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-albumpush-'))}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  ], { stdio: 'ignore' });

  try {
    for (let i = 0; i < 80; i++) {
      await sleep(300);
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/version`);
        if (r.ok) break;
      } catch (e) { /* 还没起来 */ }
    }
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const ws = new WebSocket(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
    const pending = new Map();
    let seq = 0;
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    });
    const send = (method, params) => new Promise((res) => {
      const id = ++seq;
      pending.set(id, res);
      ws.send(JSON.stringify({ id, method, params: params || {} }));
    });
    await new Promise((r) => ws.addEventListener('open', r));
    await send('Runtime.enable');
    await send('Page.enable');
    await send('Network.enable');
    const ev = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.result && r.result.exceptionDetails) {
        const d = r.result.exceptionDetails;
        return { __err: (d.exception && d.exception.description) || d.text };
      }
      return r.result && r.result.result ? r.result.result.value : null;
    };
    await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await send('Page.navigate', { url: 'file:///' + pagePath.replace(/\\/g, '/') });
    await sleep(1200);
    await ev(`localStorage.clear();
      localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)});
      localStorage.setItem('vsw.sid', ${JSON.stringify(args.sid)}); 'ok'`);
    await send('Page.reload');
    await sleep(2600);
    await ev(`document.querySelector('#introSkip') && document.querySelector('#introSkip').click(); 'ok'`);

    console.log('A 断网时存进手机相册');
    const made = await ev(`(async () => {
      const mk = (c1, c2, box) => {
        const c = document.createElement('canvas'); c.width = 240; c.height = 240;
        const x = c.getContext('2d');
        x.fillStyle = c1; x.fillRect(0, 0, 240, 240);
        x.fillStyle = c2; x.fillRect(box, box, 130, 130);
        return c.toDataURL('image/jpeg', 0.8);
      };
      S.online = false;
      // 故意给一个打不开的远程地址：只有"手机上把图一起带过去"这条能成
      await addLocalFindings([
        { u: 'https://example.invalid/album-1.jpg', d: mk('#2244aa', '#ffdd00', 20), page: 'https://e.com/a1' },
        { u: 'https://example.invalid/album-2.jpg', d: mk('#aa2244', '#00ffcc', 60), page: 'https://e.com/a2' },
      ], '测试', '手相书', { jobId: 0 });
      switchTab('board');
      renderBoard();
      return { local: S.findings.filter((f) => f && f.local).length };
    })()`);
    check('断网时两张都进了手机相册', made && made.local === 2, JSON.stringify(made));

    console.log('B 结果板上出现「推到电脑（N）」');
    const bar = await ev(`(() => {
      const btn = document.querySelector('#pushLocalBtn');
      if (!btn) return { text: '', visible: false, why: 'no button' };
      const r = btn.getBoundingClientRect();
      const board = document.querySelector('#v-board');
      return { text: btn.textContent.trim(), visible: r.height > 0,
               w: r.width, h: r.height, y: r.y,
               offsetParent: !!btn.offsetParent,
               boardHidden: board ? board.className : '',
               tab: S.tab };
    })()`);
    check('有「推到电脑（2）」按钮', !!(bar && /推到电脑（2）/.test(bar.text)), JSON.stringify(bar));
    check('按钮在可视区里（切到结果板之后）', !!(bar && bar.visible), JSON.stringify(bar));

    console.log('C 点它 → 走 /api/mobile/results，且带的是 data URL');
    const before = await ev(`(async () => {
      window.__PUSH = [];
      const realFetch = window.fetch;
      window.fetch = function (input, init) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (url.indexOf('/api/mobile/results') >= 0 && init && init.body) {
          try { window.__PUSH.push(JSON.parse(init.body)); } catch (e) { window.__PUSH.push({ __bad: String(e) }); }
        }
        return realFetch.apply(this, arguments);
      };
      document.querySelector('#pushLocalBtn').click();
      await new Promise((r) => setTimeout(r, 3500));
      const body = window.__PUSH[0] || null;
      const items = (body && body.items) || [];
      return {
        calls: window.__PUSH.length,
        n: items.length,
        first: items.length ? String(items[0].image_url || '').slice(0, 24) : '',
        jobId: body ? body.job_id : null,
        left: S.findings.filter((f) => f && f.local).length,
        btn: !!document.querySelector('#pushLocalBtn'),
        cards: document.querySelectorAll('#boardList .fcard').length,
        toast: (document.querySelector('#toast') || {}).textContent || '',
      };
    })()`);
    check('发了一次 /api/mobile/results', before && before.calls === 1, JSON.stringify(before && before.calls));
    check('两张都带上了', before && before.n === 2, JSON.stringify(before && before.n));
    check('发的是手机上的 data URL（不是那个打不开的域名）',
      !!(before && /^data:image\//.test(before.first)), JSON.stringify(before && before.first));
    check('没有队列项时 job_id 传 0（让电脑自己建）', before && before.jobId === 0, JSON.stringify(before && before.jobId));
    check('推成功后本地那份清掉了', before && before.left === 0, JSON.stringify(before && before.left));
    check('按钮跟着消失', before && before.btn === false, JSON.stringify(before && before.btn));
    check('提示说送到了电脑', !!(before && /送进电脑/.test(before.toast)), JSON.stringify(before && before.toast));

    console.log('D 电脑那边真的多了对象');
    const after = await ev(`(async () => {
      const snap = await api('/api/mobile/snapshot?images=none&limit=60');
      const rows = (snap.findings || []).filter((f) => String(f.title || '').indexOf('手相书') >= 0);
      return { total: (snap.findings || []).length, mine: rows.length, titles: rows.map((f) => f.title).slice(0, 3) };
    })()`);
    check('电脑的结果里有这两张（标题带识别结论）', !!(after && after.mine >= 1), JSON.stringify(after).slice(0, 220));

    console.log('E 边角');
    const noDup = await ev(`(async () => {
      // 再点一次都没东西可推了
      renderBoard();
      const btn = document.querySelector('#pushLocalBtn');
      return { btn: !!btn, left: S.findings.filter((f) => f && f.local).length };
    })()`);
    check('没东西可推时不显示按钮', noDup && noDup.btn === false, JSON.stringify(noDup));
    const errs = await ev(`(window.__ERRS || []).length`);
    check('没有未捕获异常', !errs, JSON.stringify(errs));
  } finally {
    try { child.kill(); } catch (e) { /* ignore */ }
  }

  console.log('');
  if (FAILS.length) {
    console.log(`${PASS} 通过 / ${FAILS.length} 失败`);
    FAILS.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
  console.log(`${PASS} 通过 / 0 失败`);
})();
