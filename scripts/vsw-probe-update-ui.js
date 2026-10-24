/* 手机端「版本与更新」卡片的界面回归（不联网、不开真浏览器）。

   更新这件事只有安卓壳能做（下载 + 调系统安装器），网页只负责：
   问电脑有没有新版、把进度画出来、把结果说清楚。

   所以这里把 window.AndroidBridge 换成假的，验：卡片在壳里才显示、点检查更新会调原生、
   三种结果（有新版本 / 已是最新 / 说不清）分别怎么写、进度和失败怎么显示、
   没填电脑地址时不该乱发请求。

   用法：node scripts/vsw-probe-update-ui.js --page vsw/static/mobile/index.html
         node scripts/vsw-probe-update-ui.js --apk vsw-workbench.apk
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-updui-'));
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-updui-profile-'));
  const port = 10660 + Math.floor(Math.random() * 40);
  const child = spawn(exe, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--hide-scrollbars',
    '--allow-file-access-from-files',
  ], { stdio: 'ignore' });

  let pass = 0;
  let fail = 0;
  const check = (label, ok, detail) => {
    if (ok) { pass++; console.log(`  ok   ${label}`); }
    else { fail++; console.log(`  FAIL ${label}${detail ? ' :: ' + detail : ''}`); }
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

    // ---------------------------------------------------------------- 浏览器里（没有安卓壳）
    const noShell = await evaluate(`(() => {
      renderUpdateCard();
      return {
        hidden: $('#updateCard').classList.contains('hide'),
        hasBridge: !!window.AndroidBridge,
      };
    })()`);
    check('浏览器里没有更新卡片（装不了，就别诱惑人）',
      noShell && noShell.hidden === true && noShell.hasBridge === false, JSON.stringify(noShell));

    // ---------------------------------------------------------------- 装上假的原生桥
    const wired = await evaluate(`(() => {
      window.__CALLS = [];
      window.AndroidBridge = {
        buildTag: function () { return 'r17'; },
        checkUpdate: function (server) { window.__CALLS.push(['check', server]); },
        installUpdate: function (server, sha) { window.__CALLS.push(['install', server, sha]); },
      };
      S.server = 'http://127.0.0.1:9';
      S.online = true;
      switchTab('me');
      renderUpdateCard();
      return {
        hidden: $('#updateCard').classList.contains('hide'),
        build: $('#appBuild').textContent.trim(),
        checkText: $('#checkUpdateBtn').textContent.trim(),
        installHiddenNow: $('#doUpdateBtn').classList.contains('hide'),
      };
    })()`);
    check('安卓壳里卡片可见', wired && wired.hidden === false, JSON.stringify(wired));
    check('显示当前构建号', wired && wired.build === '当前 r17', wired && wired.build);
    check('一开始没有「下载并安装」按钮', wired && wired.installHiddenNow === true,
      wired && String(wired.installHiddenNow));

    // ---------------------------------------------------------------- 点「检查更新」
    const clicked = await evaluate(`(async () => {
      $('#checkUpdateBtn').click();
      await new Promise((r) => setTimeout(r, 120));
      return { calls: window.__CALLS.slice(), hint: $('#updateInfo').textContent.trim() };
    })()`);
    check('点了会问原生要版本', clicked && clicked.calls.length === 1
      && clicked.calls[0][0] === 'check', JSON.stringify(clicked && clicked.calls));
    check('带上了电脑地址', clicked && clicked.calls[0] && clicked.calls[0][1] === 'http://127.0.0.1:9',
      clicked && JSON.stringify(clicked.calls[0]));
    check('等待时说了在干什么', clicked && /正在问电脑/.test(clicked.hint), clicked && clicked.hint);

    // ---------------------------------------------------------------- 有新版本
    const newer = await evaluate(`(() => {
      vswUpdateInfo({ available: true, current: 'r17', build: 'r18', build_code: 18,
        size: 632876, sha256: 'ABC123', notes: '自更新第一版', source: 'local',
        comparable: true, update_available: true });
      return {
        text: $('#updateInfo').textContent.replace(/\\s+/g, ' ').trim(),
        installHidden: $('#doUpdateBtn').classList.contains('hide'),
        installText: $('#doUpdateBtn').textContent.trim(),
        barHidden: $('#updateBar').classList.contains('hide'),
      };
    })()`);
    check('说有新版本 r18', newer && /有新版本：r18/.test(newer.text), newer && newer.text);
    check('说清你现在是哪个版本', newer && /你现在是 r17/.test(newer.text), newer && newer.text);
    check('带上大小', newer && /0\.6 MB|MB/.test(newer.text), newer && newer.text);
    check('带上更新说明', newer && /自更新第一版/.test(newer.text), newer && newer.text);
    check('出现「下载并安装」', newer && newer.installHidden === false, JSON.stringify(newer));
    check('下载前不显示进度条', newer && newer.barHidden === true, JSON.stringify(newer));

    // ---------------------------------------------------------------- 点「下载并安装」
    const install = await evaluate(`(async () => {
      window.__CALLS.length = 0;
      $('#doUpdateBtn').click();
      await new Promise((r) => setTimeout(r, 120));
      return { calls: window.__CALLS.slice(),
               barHidden: $('#updateBar').classList.contains('hide'),
               barText: $('#updateBarText').textContent.trim() };
    })()`);
    check('点了会叫原生去下载', install && install.calls.length === 1
      && install.calls[0][0] === 'install', JSON.stringify(install && install.calls));
    check('把 SHA256 一起交给原生（下载完要校验）',
      install && install.calls[0] && install.calls[0][2] === 'ABC123',
      install && JSON.stringify(install.calls[0]));
    check('立刻显示进度条', install && install.barHidden === false, JSON.stringify(install));

    // ---------------------------------------------------------------- 进度与失败
    const progress = await evaluate(`(() => {
      vswUpdateProgress(42, '42% · 260/632 KB');
      return { width: $('#updateBarFill').style.width, text: $('#updateBarText').textContent.trim(),
               checkDisabled: $('#checkUpdateBtn').disabled };
    })()`);
    check('进度条按百分比走', progress && progress.width === '42%', progress && progress.width);
    check('进度文字来自原生', progress && /42% · 260\/632 KB/.test(progress.text), progress && progress.text);
    check('下载中点不了「检查更新」', progress && progress.checkDisabled === true, JSON.stringify(progress));

    const failed = await evaluate(`(() => {
      vswUpdateFailed('校验不一致：期望 ABC… 实际 DEF…');
      return { text: $('#updateInfo').textContent.replace(/\\s+/g, ' ').trim(),
               barHidden: $('#updateBar').classList.contains('hide'),
               checkDisabled: $('#checkUpdateBtn').disabled };
    })()`);
    check('失败要说清原因', failed && /校验不一致/.test(failed.text), failed && failed.text);
    check('失败后收起进度条', failed && failed.barHidden === true, JSON.stringify(failed));
    check('失败后能重试', failed && failed.checkDisabled === false, JSON.stringify(failed));

    const needPerm = await evaluate(`(() => {
      vswUpdateNeedPerm();
      return $('#updateInfo').textContent.replace(/\\s+/g, ' ').trim();
    })()`);
    check('没给安装权限时指路去设置', needPerm && /安装未知应用/.test(needPerm), needPerm);

    // ---------------------------------------------------------------- 已是最新 / 说不清
    const latest = await evaluate(`(() => {
      vswUpdateInfo({ available: true, current: 'r18', build: 'r18', build_code: 18,
        size: 1, sha256: 'X', notes: '', source: 'local', comparable: true,
        update_available: false });
      return { text: $('#updateInfo').textContent.replace(/\\s+/g, ' ').trim(),
               installHidden: $('#doUpdateBtn').classList.contains('hide') };
    })()`);
    check('已是最新就说已是最新', latest && /已经是最新的/.test(latest.text), latest && latest.text);
    check('已是最新时藏起安装按钮', latest && latest.installHidden === true, JSON.stringify(latest));

    const unclear = await evaluate(`(() => {
      vswUpdateInfo({ available: true, current: 'r17', build: '', build_code: 0,
        size: 1, sha256: 'X', notes: '', source: 'local', comparable: false,
        update_available: false });
      return { text: $('#updateInfo').textContent.replace(/\\s+/g, ' ').trim(),
               installHidden: $('#doUpdateBtn').classList.contains('hide') };
    })()`);
    check('包上没有版本号就说「说不清」', unclear && /说不清/.test(unclear.text), unclear && unclear.text);
    check('说不清的时候不许点安装', unclear && unclear.installHidden === true, JSON.stringify(unclear));

    const broken = await evaluate(`(() => {
      vswUpdateInfo({ error: '电脑回了 HTTP 500' });
      return $('#updateInfo').textContent.replace(/\\s+/g, ' ').trim();
    })()`);
    check('问不到版本时如实说', broken && /问不到版本/.test(broken) && /HTTP 500/.test(broken), broken);

    // ---------------------------------------------------------------- 没填地址
    const noServer = await evaluate(`(() => {
      S.server = '';
      window.__CALLS.length = 0;
      $('#checkUpdateBtn').click();
      return { calls: window.__CALLS.slice(), info: ($('#toast').textContent || '').trim() };
    })()`);
    check('没填电脑地址就不发请求', noServer && noServer.calls.length === 0,
      JSON.stringify(noServer && noServer.calls));
    check('没填地址时提示先填', noServer && /电脑地址/.test(noServer.info), noServer && noServer.info);

    const errors = await evaluate(`(window.__ERRORS = window.__ERRORS || [])`);
    check('没有未捕获异常', !errors || errors.length === 0, JSON.stringify(errors));
  } finally {
    try { child.kill(); } catch (e) { /* ignore */ }
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  }

  console.log(`\n${pass} 通过 · ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
