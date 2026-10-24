#!/usr/bin/env node
/**
 * 手机自己搜：(队列项「手机搜」按钮 + 原生命中 + 结果面板)
 *
 * 背景：WebView 能替网页的 <input type=file> 交图（onShowFileChooser），但浏览器规定
 * 文件选择器必须由用户手势触发 —— 后台 input.click() 打不开它。所以手机这条是半自动：
 * 选图由原生代劳，用户只需要在引擎页面里点一下上传。这个探针验的就是"页面这一半"：
 *
 *   - 没有 AndroidBridge（浏览器里打开）时不该出现「手机搜」按钮
 *   - 有桥时，支持的引擎才出现按钮
 *   - 点它会把我方的图（base64）、引擎地址、提取脚本一起交给原生
 *   - 原生回来的结果能渲染成面板，出错也能说清
 *
 * 用法：
 *   node scripts/vsw-probe-phone-search.js --page vsw/static/mobile/index.html
 *   node scripts/vsw-probe-phone-search.js --apk vsw-workbench.apk
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const { pathToFileURL } = require('url');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseArgs(argv) {
  const out = { page: '', apk: '', keep: false };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--page') out.page = argv[++i];
    else if (argv[i] === '--apk') out.apk = argv[++i];
    else if (argv[i] === '--keep') out.keep = true;
  }
  if (!out.page && !out.apk) out.apk = 'vsw-workbench.apk';
  return out;
}

function findBrowser() {
  const roots = [process.env['ProgramFiles(x86)'], process.env.ProgramFiles, process.env.LOCALAPPDATA]
    .filter(Boolean);
  const cands = [];
  for (const root of roots) {
    cands.push(path.join(root, 'Microsoft', 'Edge', 'Application', 'msedge.exe'));
    cands.push(path.join(root, 'Google', 'Chrome', 'Application', 'chrome.exe'));
  }
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-phonesearch-'));
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-phonesearch-profile-'));
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

    // 基础数据：三个队列项（百度 / 360 / 不支持自动识图的引擎），本地只给百度那项一张图
    const setup = await evaluate(`(async () => {
      window.__CALLS = [];
      window.__SEARCH = null;
      window.__ERRORS = [];
      window.addEventListener('error', (e) => window.__ERRORS.push(String(e.message)));
      S.server = '';
      S.online = false;
      S.jobs = [
        { id: 21, engine: 'baidu', status: 'pending', seed_title: '甲', variant_id: 5, seed_id: 3 },
        { id: 22, engine: 'so360', status: 'pending', seed_title: '乙', variant_id: 6, seed_id: 3 },
        { id: 23, engine: 'bing_cn', status: 'pending', seed_title: '丙', variant_id: 7, seed_id: 3 },
        { id: 24, engine: 'baidu', status: 'pending', seed_title: '丁', variant_id: 8, seed_id: 9 },
      ];
      window.api = (p) => {
        window.__CALLS.push(['GET', p]);
        if (p.indexOf('/api/queue') === 0) return Promise.resolve({ jobs: S.jobs, counts: {}, facets: {} });
        return Promise.resolve({ counts: {}, findings: [], seeds: [], variants: [], jobs: S.jobs,
          trash: [], archived: [], archives: [], settings: {}, server_time: Date.now() / 1000 });
      };
      window.post = (p, body) => { window.__CALLS.push(['POST', p, body]); return Promise.resolve({}); };
      // 只给 21（百度）和 22（360）一张本地图；24 故意没有
      const png = new Blob([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 1, 2, 3, 4, 5, 6, 7, 8])],
        { type: 'image/png' });
      await storeBlob(KEY.variantThumb(5), png, {});
      await storeBlob(KEY.variantThumb(6), png, {});
      switchTab('queue');
      renderQueue();
      return { buttons: $$('#queueList [data-act="phone"]').length };
    })()`);
    check('还没接上原生时，页面上不该有「手机搜」', (setup && setup.buttons) === 0, JSON.stringify(setup));

    // 装上假的 AndroidBridge（等价于跑在 APK 里）
    const withBridge = await evaluate(`(() => {
      window.AndroidBridge = {
        webSearchAvailable() { return '1'; },
        webSearch(engine, label, url, extract, image) {
          window.__SEARCH = { engine: engine, label: label, url: url, extract: extract,
            imageLen: image ? image.length : 0, imageHead: image ? image.slice(0, 12) : '' };
        },
      };
      renderQueue();
      return {
        total: $$('#queueList [data-act="phone"]').length,
        ids: $$('#queueList [data-act="phone"]').map((b) => Number(b.dataset.id)),
        text: ($$('#queueList [data-act="phone"]')[0] || {}).textContent,
      };
    })()`);
    check('接上原生之后出现「手机搜」', withBridge && withBridge.total === 3, JSON.stringify(withBridge));
    check('百度与 360 那两项都有，不支持的引擎没有',
      withBridge && withBridge.ids.indexOf(21) >= 0 && withBridge.ids.indexOf(22) >= 0
        && withBridge.ids.indexOf(24) >= 0 && withBridge.ids.indexOf(23) < 0,
      JSON.stringify(withBridge && withBridge.ids));
    check('按钮文案是「手机搜」', withBridge && String(withBridge.text || '').indexOf('手机搜') >= 0,
      JSON.stringify(withBridge && withBridge.text));

    // 点百度那项：应该把图、地址、提取脚本一起交给原生
    const clicked = await evaluate(`(async () => {
      const btn = $$('#queueList [data-act="phone"]').find((b) => Number(b.dataset.id) === 21);
      btn.click();
      for (let i = 0; i < 40 && !window.__SEARCH; i++) await new Promise((r) => setTimeout(r, 100));
      return { sent: window.__SEARCH, toast: ($('#toast') || {}).textContent || '',
        ctx: S.phoneSearch || null };
    })()`);
    check('点「手机搜」把请求交给了原生', !!(clicked && clicked.sent), JSON.stringify(clicked));
    check('交给原生的是百度识图', clicked && clicked.sent && clicked.sent.engine === 'baidu',
      JSON.stringify(clicked && clicked.sent && clicked.sent.engine));
    check('引擎地址是 graph.baidu.com',
      clicked && clicked.sent && /graph\.baidu\.com/.test(clicked.sent.url),
      JSON.stringify(clicked && clicked.sent && clicked.sent.url));
    check('提取脚本一起带过去了（认得 pcsimi）',
      clicked && clicked.sent && /pcsimi/.test(clicked.sent.extract || ''),
      clicked && clicked.sent ? String(clicked.sent.extract || '').slice(0, 40) : '');
    check('图片是 base64（不是 data: 前缀）',
      clicked && clicked.sent && clicked.sent.imageLen > 20 && !/^data:/.test(clicked.sent.imageHead),
      JSON.stringify(clicked && clicked.sent && { len: clicked.sent.imageLen, head: clicked.sent.imageHead }));
    check('提到要在页面里点一下上传',
      clicked && /上传/.test(clicked.toast || ''), JSON.stringify(clicked && clicked.toast));
    check('记下了这次是哪个队列项', clicked && clicked.ctx && Number(clicked.ctx.jobId) === 21,
      JSON.stringify(clicked && clicked.ctx));

    // 本地没有图的那项：要说清楚，而不是静默什么都不做
    const noImage = await evaluate(`(async () => {
      window.__SEARCH = null;
      const btn = $$('#queueList [data-act="phone"]').find((b) => Number(b.dataset.id) === 24);
      btn.click();
      await new Promise((r) => setTimeout(r, 600));
      return { sent: window.__SEARCH, toast: ($('#toast') || {}).textContent || '' };
    })()`);
    check('本地没图时不发请求', !noImage || noImage.sent === null, JSON.stringify(noImage));
    check('并且提示先同步', !!(noImage && /同步/.test(noImage.toast || '')), JSON.stringify(noImage && noImage.toast));

    // 原生回来的结果 → 面板
    const sheet = await evaluate(`(async () => {
      window.vswWebSearchResult(JSON.stringify({
        guess: '橘猫', similar: [
          { u: 'https://t7.baidu.com/a.jpg', w: 800, h: 600, page: 'https://example.com/a' },
          { u: 'https://t8.baidu.com/b.jpg', w: 400, h: 300, page: '' },
        ], sources: [{ url: 'https://example.com/a', title: '某网页' }], goods: [], note: '',
      }));
      await new Promise((r) => setTimeout(r, 200));
      const body = $('#sheetBody') ? $('#sheetBody').innerText : '';
      return { title: $('#sheetTitle').textContent, body: body.slice(0, 400),
        open: !($('#sheet') || {}).classList.contains('hide'),
        imgs: $$('#sheetBody img').length,
        copyImg: !!$('#copyImgLinks'), copyPage: !!$('#copyPageLinks') };
    })()`);
    check('结果面板打开了', !!(sheet && sheet.title), JSON.stringify(sheet && sheet.title));
    check('标题写明是手机搜、哪个队列项',
      sheet && /手机搜/.test(sheet.title) && /#21/.test(sheet.title), JSON.stringify(sheet && sheet.title));
    check('显示识别结论', sheet && /识别结论/.test(sheet.body) && /橘猫/.test(sheet.body),
      JSON.stringify(sheet && sheet.body.slice(0, 80)));
    check('相似图真的画出来了', sheet && sheet.imgs === 2, JSON.stringify(sheet && sheet.imgs));
    check('有「复制图片链接」「复制来源链接」',
      sheet && sheet.copyImg && sheet.copyPage, JSON.stringify(sheet && [sheet.copyImg, sheet.copyPage]));

    // 出错时也要说话
    const errSheet = await evaluate(`(async () => {
      window.vswWebSearchResult({ error: '搜索页没有返回结果' });
      await new Promise((r) => setTimeout(r, 200));
      return { body: $('#sheetBody').innerText.slice(0, 200) };
    })()`);
    check('出错时把原因写在面板上',
      errSheet && /没有返回结果/.test(errSheet.body || ''), JSON.stringify(errSheet && errSheet.body));

    // 防盗链：原生抓回来内嵌成 data URL 的那张（d）要优先用，没抓到的才退回远程 u
    const embedded = await evaluate(`(async () => {
      const px = 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL' +
        'DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB' +
        'AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==';
      window.vswWebSearchResult(JSON.stringify({
        guess: '橘猫', similar: [
          { u: 'https://t7.baidu.com/a.jpg', d: px, page: 'https://example.com/a' },
          { u: 'https://t8.baidu.com/b.jpg', page: '' },
        ], sources: [], goods: [], note: '',
      }));
      await new Promise((r) => setTimeout(r, 200));
      const imgs = $$('#sheetBody img');
      return { srcs: imgs.map((i) => i.getAttribute('src') || ''),
        body: $('#sheetBody').innerText.slice(0, 300) };
    })()`);
    check('有内嵌图时优先用内嵌的（iVBOR/JFIF data URL），不再直接引用远程地址',
      embedded && embedded.srcs.length === 2
        && embedded.srcs[0].indexOf('data:image/jpeg;base64,') === 0,
      JSON.stringify(embedded && embedded.srcs.map((x) => x.slice(0, 32))));
    check('没内嵌的那张退回远程地址', embedded && embedded.srcs[1] === 'https://t8.baidu.com/b.jpg',
      JSON.stringify(embedded && embedded.srcs[1]));
    check('统计里说清有几张存成了本地小图', embedded && /1 张已存成本地小图/.test(embedded.body || ''),
      JSON.stringify(embedded && embedded.body));

    const errors = await evaluate('(() => (window.__ERRORS || []).length)()');
    check('全程没有未捕获异常', errors === 0, JSON.stringify(errors));
  } finally {
    try { child.kill(); } catch (e) { /* ignore */ }
    if (!args.keep) {
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* ignore */ }
      try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ }
    }
  }

  console.log(`\n结果：${pass}/${pass + fail} 通过`);
  process.exit(fail ? 1 : 0);
})();
