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
      if (res.result && res.result.exceptionDetails) {
        const d = res.result.exceptionDetails;
        const desc = String((d.exception && d.exception.description) || d.text || '');
        console.log('  [evaluate 出错]', desc.slice(0, 400));
        return { __error: d.text, desc: desc.slice(0, 300) };
      }
      if (res.result && res.result.exceptionDetails) {
        const exc = res.result.exceptionDetails.exception || {};
        console.log('  [evaluate 出错]', String(exc.description || res.result.exceptionDetails.text || '').slice(0, 400));
      }
      return res.result && res.result.result ? res.result.result.value : null;
    };

    await send('Page.navigate', { url: pageUrl });
    await sleep(2500);

    // 基础数据：三个队列项（百度 / 360 / 不支持自动识图的引擎），本地只给百度那项一张图
    const setup = await evaluate(`(async () => {
      window.__SAVE_CALLS = [];
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

    // 「存进结果板」：手机搜到的图要能收进工作台，否则结果板里永远没有这些图
    const saved = await evaluate(`(async () => {
      S.server = 'http://probe.local';
      S.online = true;
      window.__CALLS = [];
      window.fetch = async (url, opts) => {
        let body = {};
        try { body = JSON.parse((opts && opts.body) || '{}'); } catch (e) { body = {}; }
        window.__SAVE_CALLS.push({ url: String(url), body: body });
        const u = String(url);
        if (u.indexOf('/api/health') >= 0) {
          return { ok: true, status: 200, json: async () => ({ ok: true, vision: { available: true } }) };
        }
        const snap = u.indexOf('/api/mobile/snapshot') >= 0;
        return { ok: true, status: 200, json: async () => (snap
          ? { findings: [], seeds: [], variants: [], jobs: [], trash: [], archives: [], archived: [], jobs_total: 0 }
          : { imported: [{ id: 1 }, { id: 2 }], new_findings: 2, duplicates: 1, failures: [] }) };
      };
      window.vswWebSearchResult(JSON.stringify({
        guess: '橘猫',
        similar: [{ u: 'https://t7.baidu.com/a.jpg', d: 'data:image/jpeg;base64,AAAA' }],
        sources: [], goods: [], note: '',
        diag: { pcsimi: 3, viaPcsimi: 1, source: 'pcsimi', hosts: {} }
      }));
      await new Promise((r) => setTimeout(r, 250));
      const btn = $('#saveResults');
      const label = btn ? btn.textContent : '';
      const bodyText = ($('#sheetBody') || {}).innerText || '';
      const srcText = bodyText.indexOf('图源：引擎接口') >= 0 ? '图源：引擎接口' : '';
      // 直接调真函数（按钮存在性由上面那项断言；这里验它到底发了什么请求）。
      // S.online 会被 checkOnline 的定时器改，所以紧贴着调用前再置一次。
      S.online = true;
      await savePhoneResults([{ u: 'https://t7.baidu.com/a.jpg', d: 'data:image/jpeg;base64,AAAA' }], { label: '百度识图' });
      await new Promise((r) => setTimeout(r, 350));
      const all = window.__SAVE_CALLS || [];
      const syncCall = all.filter((c) => c && String(c.url || '').indexOf('/api/mobile/sync') >= 0)[0] || null;
      const toastText = (($$('#toast') && $$('#toast')[0]) || $('#toast') || {}).textContent || '';
      return { label: label, saved: syncCall, src: srcText, toastText: toastText,
        dbg2: { calls: all.length, online: S.online, server: S.server, urls: all.map((c) => String(c && c.url)) } };
    })()`);
    check('结果能一键存进结果板，按钮上写着张数', saved && /存进结果板（1）/.test(saved.label || ''),
      JSON.stringify(saved && (saved.dbg || saved)));
    check('在线时点它，给出明确结果（收进结果板 / 失败原因）',
      saved && /结果板/.test(saved.toastText || ''), JSON.stringify(saved && saved.toastText));


    // 离线时不能丢图：先存进手机待同步，联网后再推给电脑
    const offline = await evaluate(`(async () => {
      S.online = false;
      const before = (await local.all('pending')).length;
      await savePhoneResults([{ u: 'https://t7.baidu.com/off.jpg', d: 'data:image/jpeg;base64,BBBB' }], { label: '百度识图' });
      await new Promise((r) => setTimeout(r, 250));
      const rows = await local.all('pending');
      return { added: rows.length - before,
        url: rows.length ? String((rows[rows.length - 1].payload || {}).data_url || '') : '' };
    })()`);
    check('离线时先存进手机待同步，不丢图', offline && offline.added >= 1, JSON.stringify(offline && offline.added));
    check('待同步里带的也是内嵌的 data URL', offline && String(offline.url).indexOf('data:image/jpeg;base64,') === 0,
      JSON.stringify(offline && String(offline.url).slice(0, 32)));

    check('面板写明图是从引擎接口来的', saved && /图源：引擎接口/.test(saved.src || ''), JSON.stringify(saved && saved.src));

    // domOnly：一张图都没交上去时，页面上能捡到的只有百度识图首页的样图。
    // 上一版把 8 张样图当成「搜索结果」显示并存进结果板，用户看到的是一堆无关照片。
    const domOnly = await evaluate(`(() => {
      document.querySelectorAll('#sheetBody').forEach((el) => { el.innerHTML = ''; });
      window.vswWebSearchResult({ similar: [
        { u: 'https://mms-graph.cdn.bcebos.com/home-beer.jpg', w: 882, h: 693 },
        { u: 'https://mms-graph.cdn.bcebos.com/home-splash.jpg?x-oss-process=image/resize,w_500', w: 1024, h: 1024 },
        { u: 'https://mms-graph.cdn.bcebos.com/home-splash.jpg', w: 1024, h: 1024 },
      ], sources: [], goods: [], guess: '', diag: { pcsimi: 0, source: 'dom' } });
      const body = document.body.innerText || '';
      const html = document.querySelector('#sheetBody').innerHTML;
      return { hasWarn: body.indexOf('这次没把图交给引擎') >= 0,
        noSave: body.indexOf('存进结果板') < 0,
        imgs: (html.match(/<img/g) || []).length,
        hasHint: body.indexOf('点一下「上传图片」') >= 0 };
    })()`);
    check('没交上图时面板直接说清（不把首页样图当结果）', domOnly && domOnly.hasWarn, JSON.stringify(domOnly));
    check('这种时候不给「存进结果板」按钮', domOnly && domOnly.noSave, JSON.stringify(domOnly && domOnly.noSave));
    check('这种时候不渲染那些样图', domOnly && domOnly.imgs === 0, JSON.stringify(domOnly && domOnly.imgs));
    check('并且告诉用户回搜索页点「上传图片」', domOnly && domOnly.hasHint, JSON.stringify(domOnly && domOnly.hasHint));

    // ---- 面板底部固定操作栏（r39）：以前按钮在 3000px 长的滚动区最底下，等于点不到
    // 首次说明是全屏遮罩（z-index 70），探针里先点掉它，否则任何点击都会命中它
    await evaluate(`(() => {
      const skip = document.querySelector('#introSkip');
      if (skip) skip.click();
      const intro = document.querySelector('#intro');
      if (intro) intro.classList.add('hide');
      return 1;
    })()`);
    await sleep(200);
    await evaluate(`(() => {
      const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
      const similar = Array.from({ length: 30 }, (_, i) => ({ u: 'https://mms0.baidu.com/it/u=' + i, d: png, w: 300, h: 200, page: '' }));
      window.vswWebSearchResult(JSON.stringify({ similar, sources: [], goods: [], guess: '测试', diag: { pcsimi: 30, viaPcsimi: 30, source: 'pcsimi', hosts: {} } }));
      return 1;
    })()`);
    await sleep(600);
    const foot = await evaluate(`(() => {
      const b = document.querySelector('#saveResults');
      if (!b) return { none: true };
      const r = b.getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return { inFoot: !!b.closest('#sheetFoot'),
               footVisible: !document.querySelector('#sheetFoot').classList.contains('hide'),
               visible: r.y > 0 && r.bottom <= window.innerHeight,
               onTop: !!(hit && (hit === b || b.contains(hit))),
               hit: hit && (hit.id || hit.tagName),
               cards: document.querySelectorAll('#sheetBody .fcard').length };
    })()`);
    if (foot && foot.none) console.log('  [诊断] 面板里没有 #saveResults');
    check('「存进结果板」在固定底栏里，不随内容滚走', foot && foot.inFoot === true, JSON.stringify(foot));
    check('30 张结果时不滚动也能看见按钮', foot && foot.visible === true, JSON.stringify(foot && foot.visible));
    check('按钮中心点命中它自己（没被别的元素盖住）', foot && foot.onTop === true, JSON.stringify(foot && foot.onTop));
    check('预览最多画 12 张（其余只给数字）', foot && foot.cards <= 12, JSON.stringify(foot && foot.cards));
    check('关掉面板会把底栏清空', await evaluate(`(() => { closeSheet(); return !document.querySelector('#sheetFoot').innerHTML; })()`) === true, '');


    // 本地没有那张图（队列项是电脑上新加的）时，要现去电脑要一张，而不是弹
    // 「本地没有这张图：先同步一次快照再搜」。线上这个 bug 用户截图里出现过。
    const onDemand = await evaluate(`(async () => {
      try {
      await local.del('assets', 'vthumb:4321');
      S.online = true;
      let asked = '';
      const realFetch = window.fetch;
      window.fetch = function (url, opts) {
        const u = String(url || '');
        if (u.indexOf('/api/variant/4321/thumb') >= 0) {
          asked = u;
          return Promise.resolve(new Response(new Blob([new Uint8Array(64)], { type: 'image/jpeg' }), { status: 200 }));
        }
        return realFetch.apply(this, arguments);
      };
      if (window.__SEARCH) window.__SEARCH.length = 0;
      const job = { id: 4321, engine: 'baidu', seed_id: 9, variant_id: 4321, variant_kind: 'full', status: 'pending' };
      await startPhoneSearch(4321);
      if (window.__SEARCH) window.__SEARCH.length = 0;
      const b64 = await jobImageBase64(job);
      window.fetch = realFetch;
      return { asked: asked, len: String(b64 || '').length };
      } catch (e) { return { error: String((e && e.stack) || e) }; }
    })()`);
    if (onDemand && onDemand.error) console.log('  [诊断]', onDemand.error);
    check('本地缺图时会现去电脑取那张变体图', onDemand && String(onDemand.asked || '').indexOf('/api/variant/4321/thumb') >= 0,
      JSON.stringify(onDemand));
    check('取回来能转成 base64 交给原生', onDemand && onDemand.len > 40, JSON.stringify(onDemand && onDemand.len));

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
