/* 开屏检测新版本（用户要求：「做成开屏检测最新版本更新」）。

   钉住的行为：
     1. 浏览器里（没有原生桥）不问、不弹；
     2. 装了应用但还没填电脑地址 → 不问；
     3. 装了应用 + 有地址 → 开屏约 1.5s 后自动问一次，问的就是填过的地址；
     4. 原生回「有新版本」→ 弹层盖上来，标题/现在版本/体积/说明都在；
     5. 点「以后再说」→ 关掉并记下这一版（localStorage `vsw.updateSkip`）；
     6. 同一版再开屏不弹；下一版（r20）照样弹 —— 跳过是按版本记的；
     7. 点「立即更新」→ 把电脑地址交给原生 installUpdate，安装期间按钮禁用；
     8. 进度/失败回调都画在弹层里，失败后按钮恢复；
     9. 已是最新时不弹。

   用法：node scripts/vsw-probe-update-gate.js --page vsw/static/mobile/index.html
         node scripts/vsw-probe-update-gate.js --apk vsw-workbench.apk
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
    local ? path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe') : '',
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-gate-'));
  let pageUrl;
  if (args.page) pageUrl = pathToFileURL(path.resolve(args.page)).href;
  else { extractPage(args.apk, dir); pageUrl = pathToFileURL(path.join(dir, 'index.html')).href; }

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-gate-profile-'));
  const port = 10680 + Math.floor(Math.random() * 40);
  const child = spawn(findBrowser(), [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--hide-scrollbars',
  ], { stdio: 'ignore' });

  let pass = 0;
  let fail = 0;
  const check = (label, ok, detail) => {
    if (ok) { pass++; console.log(`  ok   ${label}`); }
    else { fail++; console.log(`  FAIL ${label}${detail ? ' :: ' + String(detail).slice(0, 200) : ''}`); }
  };
  const SERVER = 'http://127.0.0.1:8799';

  try {
    let version = null;
    for (let i = 0; i < 60 && !version; i++) {
      await sleep(300);
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/version`);
        if (r.ok) version = await r.json();
      } catch (e) { /* not up yet */ }
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
      if (res.result && res.result.exceptionDetails) {
        const d = res.result.exceptionDetails;
        return { __error: (d.exception && d.exception.description) || d.text };
      }
      return res.result && res.result.result ? res.result.result.value : null;
    };

    const setStorage = (kv) => evaluate(`(() => {
      localStorage.clear();
      const kv = ${JSON.stringify(kv)};
      for (const k of Object.keys(kv)) localStorage.setItem(k, kv[k]);
      return Object.keys(localStorage).length;
    })()`);

    /* 假原生桥：只记账，不真下载。装完不能再 reload，否则桥就没了 */
    const installBridge = (buildTag) => evaluate(`(() => {
      window.__UP = { check: [], install: [] };
      window.AndroidBridge = {
        buildTag: () => ${JSON.stringify(buildTag)},
        platform: () => 'android',
        hideSplash: () => {},
        scanQR: () => {},
        webSearchAvailable: () => '1',
        webSearch: () => {},
        checkUpdate: (server) => { window.__UP.check.push(String(server)); },
        installUpdate: (server, sha) => { window.__UP.install.push([String(server), String(sha || '')]); },
      };
      return true;
    })()`);

    const waitReady = async (tries) => {
      for (let i = 0; i < (tries || 40); i++) {
        const ok = await evaluate("!!document.querySelector('#updGate') && typeof vswUpdateInfo === 'function'");
        if (ok === true) return true;
        await sleep(150);
      }
      return false;
    };

    /* 打开应用：写本地存储 → reload → 等页面脚本就绪 → （可选）装假桥 → 等开屏那 1.5s 触发 */
    const openApp = async (storage, buildTag) => {
      await setStorage(storage || {});
      await send('Page.reload', {});
      await sleep(400);
      const ready = await waitReady();
      if (buildTag) await installBridge(buildTag);
      await sleep(2700);
      return ready;
    };

    const gateState = () => evaluate(`({
      hidden: document.querySelector('#updGate').classList.contains('hide'),
      title: document.querySelector('#updTitle').textContent,
      meta: document.querySelector('#updMeta').textContent,
      notes: document.querySelector('#updNotes').textContent,
      nowBtn: document.querySelector('#updNow').textContent,
      laterBtn: document.querySelector('#updLater').textContent,
      nowDisabled: document.querySelector('#updNow').disabled,
      checkCalls: (window.__UP || {}).check || [],
      installCalls: (window.__UP || {}).install || [],
      skip: localStorage.getItem('vsw.updateSkip'),
      hasBridge: !!window.AndroidBridge,
    })`);

    /* 模拟「原生把 /api/app/version 的结果丢回来」—— 走的是开屏自动问的那条路径 */
    const respond = (info) => evaluate(`(() => {
      GATE.auto = true;
      window.vswUpdateInfo(${JSON.stringify(info)});
      return true;
    })()`).then(() => gateState());

    console.log(`内核 ${version.Browser}\n页面 ${pageUrl}\n`);

    const info = (build, code) => ({
      available: true, update_available: true, build, build_code: code,
      current: 'r17', current_code: 17, size: 751659,
      notes: '开屏检测新版本；归档页折叠', source: 'local', comparable: true,
    });

    /* ---------- 1. 浏览器里：不问、不弹 ---------- */
    await send('Page.navigate', { url: pageUrl });
    await sleep(2800);
    await openApp({ 'vsw.server': SERVER }, '');
    const browserMode = await gateState();
    check('浏览器里没有原生桥', browserMode && browserMode.hasBridge === false, JSON.stringify(browserMode));
    check('浏览器里不自动问版本', browserMode && browserMode.checkCalls.length === 0,
      JSON.stringify(browserMode && browserMode.checkCalls));
    check('浏览器里不弹更新层', browserMode && browserMode.hidden === true, JSON.stringify(browserMode));

    /* ---------- 2. 装了应用、没填地址：不问 ---------- */
    await openApp({}, 'r17');
    const noServer = await gateState();
    check('装了应用但没填地址时不去问', noServer && noServer.checkCalls.length === 0,
      JSON.stringify(noServer && noServer.checkCalls));
    check('没地址时也不弹层', noServer && noServer.hidden === true, JSON.stringify(noServer));

    /* ---------- 3. 装了应用 + 有地址：开屏问一次 ---------- */
    await openApp({ 'vsw.server': SERVER }, 'r17');
    const auto = await gateState();
    check('开屏自动问了一次版本', auto && auto.checkCalls.length === 1, JSON.stringify(auto && auto.checkCalls));
    check('问的是填过的电脑地址', auto && auto.checkCalls[0] === SERVER, JSON.stringify(auto && auto.checkCalls));
    check('还没回话时先不弹层', auto && auto.hidden === true, JSON.stringify(auto));

    /* ---------- 4. 回「有新版本」→ 弹层盖上来 ---------- */
    const popped = await respond(info('r19', 19));
    check('有新版本时开屏弹层出现', popped && popped.hidden === false, JSON.stringify(popped));
    check('标题写清新版本号', popped && /r19/.test(popped.title || ''), popped && popped.title);
    check('副标题写清「你现在是」与体积',
      popped && /你现在是 r17/.test(popped.meta || '') && /MB/.test(popped.meta || ''), popped && popped.meta);
    check('说明文字来自电脑', popped && /开屏检测新版本/.test(popped.notes || ''), popped && popped.notes);
    check('两个按钮文案对',
      popped && popped.nowBtn === '立即更新' && popped.laterBtn === '以后再说', JSON.stringify(popped));

    /* ---------- 5. 「以后再说」→ 关掉并记住 ---------- */
    const later = await evaluate(`(() => {
      document.querySelector('#updLater').click();
      return {
        hidden: document.querySelector('#updGate').classList.contains('hide'),
        skip: localStorage.getItem('vsw.updateSkip'),
        toast: (document.querySelector('#toast') || {}).textContent || '',
      };
    })()`);
    check('点「以后再说」把弹层关掉', later && later.hidden === true, JSON.stringify(later));
    check('记下了跳过的版本', later && later.skip === 'r19', JSON.stringify(later && later.skip));
    check('顺带提示去哪儿手动更新', later && /检查更新/.test(later.toast || ''), later && later.toast);

    /* ---------- 6. 同一版不弹、下一版还弹 ---------- */
    const again = await respond(info('r19', 19));
    check('跳过的那一版不再弹', again && again.hidden === true, JSON.stringify(again));
    const newer = await respond(info('r20', 20));
    check('换到更新的一版仍然会弹（跳过是按版本记的）',
      newer && newer.hidden === false && /r20/.test(newer.title || ''), JSON.stringify(newer));

    /* ---------- 7. 「立即更新」交给原生 ---------- */
    const install = await evaluate(`(() => {
      document.querySelector('#updNow').click();
      return { install: (window.__UP || {}).install || [],
               nowDisabled: document.querySelector('#updNow').disabled };
    })()`);
    check('点「立即更新」把电脑地址交给原生',
      install && install.install.length === 1 && install.install[0][0] === SERVER,
      JSON.stringify(install && install.install));
    check('安装期间按钮暂时不能点', install && install.nowDisabled === true, JSON.stringify(install));

    /* ---------- 8. 进度与失败 ---------- */
    const prog = await evaluate(`(() => {
      window.vswUpdateProgress(46, '46% · 292/633 KB');
      return { shown: !document.querySelector('#updBar').classList.contains('hide'),
               width: document.querySelector('#updBarFill').style.width,
               text: document.querySelector('#updBarText').textContent };
    })()`);
    check('进度回调画在弹层里',
      prog && prog.shown === true && prog.width === '46%' && /46%/.test(prog.text || ''), JSON.stringify(prog));

    const failed = await evaluate(`(() => {
      window.vswUpdateFailed('校验不一致：下载到的包和电脑上的不是同一个');
      return { barHidden: document.querySelector('#updBar').classList.contains('hide'),
               notes: document.querySelector('#updNotes').textContent,
               nowDisabled: document.querySelector('#updNow').disabled };
    })()`);
    check('失败原因写回弹层', failed && /校验不一致/.test(failed.notes || ''), JSON.stringify(failed));
    check('失败后按钮恢复可点',
      failed && failed.nowDisabled === false && failed.barHidden === true, JSON.stringify(failed));

    /* ---------- 9. 已是最新：不弹 ---------- */
    const latest = await respond({ available: true, update_available: false, build: 'r17',
      build_code: 17, current: 'r17', comparable: true });
    check('已是最新时不弹层', latest && latest.hidden === true, JSON.stringify(latest));
  } finally {
    try { child.kill(); } catch (e) { /* ignore */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* ignore */ }
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ }
    console.log(`\n${pass} 通过 / ${fail} 失败`);
    process.exitCode = fail ? 1 : 0;
  }
})();
