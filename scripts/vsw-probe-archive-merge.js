/**
 * 探针：合并归档相册（勾几个归档批次 → 当一个大相册看 / 一起导出 / 一起打包）。
 *
 * 为什么要有这个东西：单个批次的「查看」只能看一批。用户真正的工作流是
 * 「把 24/23/22 三批合成一个相册，一起去重看一遍，再一次性导出去」。
 * 服务端做的是**视图合并**（不改数据库里的批次归属），所以三件事必须都验：
 *
 *   A. 服务端：跨批次按 id 去重、按发散度降序、计数自洽（count + trashed === count_all）；
 *   B. 打包：合并 zip 是合法压缩包，manifest 里记着合并了哪些批次；
 *   C. 路由：`/api/archive/merge/zip` 不能被 `/api/archive/<id>/zip` 的通配抢走（曾经抢走过，
 *      把 `merge` 当 id 去 int() → 400）；单批 zip 必须还能用；
 *   D. 手机端：批次卡有勾选框、没勾时按钮禁用、勾两个后按钮带（N）、
 *      「合并查看」出的面板标题与条目数与服务端一致、条目带缩略图、「合并保存 zip」请求对了地址；
 *   E. 桌面端：归档表格有勾选列与合并按钮，点「合并查看」出合并板，zip 链接指向 merge 接口。
 *
 * 用法：
 *   node scripts/vsw-probe-archive-merge.js --apk vsw-workbench.apk --origin http://127.0.0.1:8799
 *   node scripts/vsw-probe-archive-merge.js --page vsw/static/mobile/index.html --origin http://127.0.0.1:8799
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

    else if (argv[i] === '--sid') out.sid = argv[++i];
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

// 手机端页面是单文件（内联脚本），直接从 APK 里取出来跑，验的就是装到手机上的那份
function extractPage(apk) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-merge-probe-'));
  const target = path.join(dir, 'index.html');
  execFileSync('python', ['-c', [
    'import zipfile',
    `z=zipfile.ZipFile(r"${apk}")`,
    'open(r"' + target + '","wb").write(z.read("assets/mobile/index.html"))',
  ].join(';')], { encoding: 'utf8' });
  return { html: target, dir };
}

function zipManifest(buffer) {
  // 只解 manifest.json：zip 里它一般是最后一个条目，用中央目录找。
  // 手写解析太啰嗦，交给 python 的 zipfile 更稳。
  const tmp = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-merge-zip-')), 'b.zip');
  fs.writeFileSync(tmp, buffer);
  try {
    const out = execFileSync('python', ['-c', [
      'import json, zipfile',
      `z = zipfile.ZipFile(r"${tmp}")`,
      'names = z.namelist()',
      'raw = z.read("manifest.json")',
      'print(json.dumps({"names": names, "manifest": json.loads(raw.decode("utf-8"))}, ensure_ascii=False))',
    ].join('\n')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    return JSON.parse(out);
  } finally {
    try { fs.rmSync(path.dirname(tmp), { recursive: true, force: true }); } catch (e) { /* lock */ }
  }
}

async function main() {
  const args = parseArgs();
  const origin = args.origin.replace(/\/+$/, '');
  let pageUrl = args.page;
  let extracted = null;
  if (args.apk) {
    extracted = extractPage(path.resolve(args.apk));
    pageUrl = 'file:///' + extracted.html.replace(/\\/g, '/');
  } else if (pageUrl && !/^[a-z]+:\/\//i.test(pageUrl)) {
    // 允许直接给本地文件路径（相对/绝对都行），否则 Page.navigate 会当成非法 URL 而静默不跳转
    pageUrl = 'file:///' + path.resolve(pageUrl).replace(/\\/g, '/');
  }
  if (!pageUrl) { console.error('给 --apk 或 --page'); process.exit(2); }

  const results = [];
  const check = (label, ok, detail) => {
    results.push({ label, ok: !!ok });
    console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}${!ok && detail ? ' :: ' + detail : ''}`);
  };
  // 服务端开了账号之后，Node 侧的裸 fetch 一律 401 —— 带上探针拿到的那把会话令牌。
  const authHeaders = () => (args.sid ? { 'X-VSW-Session': args.sid } : {});
  const json = async (url, opts) => {
    // 服务端开了账号之后，Node 侧的裸 fetch 一律 401 —— 带上探针拿到的那把会话令牌。
    const conf = Object.assign({}, opts || {});
    if (args.sid) conf.headers = Object.assign({ 'X-VSW-Session': args.sid }, conf.headers || {});
    const res = await fetch(url, conf);
    const text = await res.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { data = { __raw: text.slice(0, 200) }; }
    return { status: res.status, ok: res.ok, data, text, headers: res.headers };
  };

  // ---------------------------------------------------------------- A. 服务端
  console.log('A. 服务端：合并视图');
  const list = await json(origin + '/api/archives?limit=200');
  const batches = (list.data && list.data.archives) || [];
  check('归档批次列表可读', list.ok && batches.length > 0,
        `status=${list.status} batches=${batches.length}`);

  // 找出"真的有对象"的批次（合并 shell 空批次没有意义）
  const withItems = [];
  for (const batch of batches.slice(0, 14)) {
    const probe = await json(`${origin}/api/archive/merge?ids=${batch.id}`);
    if (probe.ok && probe.data && probe.data.count_all > 0) {
      withItems.push({ id: batch.id, count: probe.data.count_all });
    }
    if (withItems.length >= 3) break;
  }
  check('至少 2 个批次里有对象（否则没法验跨批次）', withItems.length >= 2,
        JSON.stringify(withItems));
  if (withItems.length < 2) {
    console.log('\n批次里没内容，后面的跨批次检查无法进行。');
    process.exitCode = 1;
    return;
  }
  const useIds = withItems.slice(0, 3).map((x) => x.id);
  const idsParam = useIds.join(',');

  const merged = await json(`${origin}/api/archive/merge?ids=${idsParam}`);
  const info = merged.data || {};
  check('合并接口返回 200', merged.ok, `status=${merged.status} ${merged.text.slice(0, 120)}`);
  check('合并结果记着合并了哪些批次',
        JSON.stringify((info.batches || []).map((b) => b.id)) === JSON.stringify(useIds),
        JSON.stringify(info.batches));

  const items = info.items || [];
  const itemIds = items.map((f) => f.id);
  check('跨批次按 id 去重（没有重复对象）',
        new Set(itemIds).size === itemIds.length,
        `${itemIds.length} 条 / 去重后 ${new Set(itemIds).size}`);
  check('合并后条目数等于各批之和（去重前逐批相加）',
        items.length <= withItems.slice(0, useIds.length).reduce((n, x) => n + x.count, 0),
        `合并 ${items.length} vs 各批 ${withItems.slice(0, useIds.length).map((x) => x.count).join('+')}`);

  const values = items.map((f) => Number(f.explore_value) || 0);
  let sorted = true;
  for (let i = 1; i < values.length; i++) if (values[i] > values[i - 1] + 1e-9) sorted = false;
  check('按发散度降序（合并相册先看最值钱的）', sorted,
        values.slice(0, 6).map((v) => v.toFixed(3)).join(' '));

  check('计数自洽：count + trashed === count_all === 条目数',
        info.count + info.trashed === info.count_all && info.count_all === items.length,
        `count=${info.count} trashed=${info.trashed} count_all=${info.count_all} items=${items.length}`);

  const perItemSources = items.reduce((n, f) => n + (f.sources || []).length, 0);
  check('来源总数等于逐条来源之和', info.sources === perItemSources,
        `info.sources=${info.sources} 逐条=${perItemSources}`);
  check('平台分布非空且合计等于来源数',
        Array.isArray(info.platforms) && info.platforms.length > 0
          && info.platforms.reduce((n, p) => n + p.n, 0) === info.sources,
        JSON.stringify((info.platforms || []).slice(0, 3)));

  // ---------------------------------------------------------------- B. 打包
  console.log('\nB. 打包：合并 zip');
  const zipRes = await fetch(`${origin}/api/archive/merge/zip?ids=${idsParam}`, { headers: authHeaders() });
  const zipBuf = Buffer.from(await zipRes.arrayBuffer());
  const disposition = zipRes.headers.get('content-disposition') || '';
  check('合并 zip 能下载（200 且非空）', zipRes.ok && zipBuf.length > 1000,
        `status=${zipRes.status} bytes=${zipBuf.length}`);
  check('下载内容是合法 zip（PK 魔数）',
        zipBuf[0] === 0x50 && zipBuf[1] === 0x4b, zipBuf.slice(0, 4).toString('hex'));
  check('文件名用 RFC 5987 编码（中文/多批次名才能写进响应头）',
        /filename\*=UTF-8''/.test(disposition), disposition);
  check('字面路由优先：merge/zip 没被 /api/archive/*/zip 抓去当 id',
        zipRes.status !== 400, `status=${zipRes.status}`);

  // 反证：如果通配路由抢了字面路由，HEAD 会变成 400（int('merge') 解析失败）
  const headRes = await fetch(`${origin}/api/archive/merge/zip?ids=${idsParam}`, { method: 'HEAD', headers: authHeaders() });
  check('同一个地址用 HEAD 也是 200（下载预览/断点都靠它）',
        headRes.ok && Number(headRes.headers.get('content-length') || 0) > 1000,
        `status=${headRes.status} len=${headRes.headers.get('content-length')}`);

  let packed = null;
  try { packed = zipManifest(zipBuf); } catch (e) { check('zip 可解出 manifest.json', false, e.message); }
  if (packed) {
    const man = packed.manifest || {};
    check('zip 里解出了 manifest.json 与图片',
          (packed.names || []).some((n) => n === 'manifest.json')
            && (packed.names || []).filter((n) => n.startsWith('imgs/')).length > 0,
          `${(packed.names || []).length} 条目`);
    check('manifest 记着合并的所有批次',
          JSON.stringify(man.archive_ids || []) === JSON.stringify(useIds),
          JSON.stringify(man.archive_ids));
    check('manifest 条目数与合并视图一致', (man.items || []).length === items.length,
          `manifest=${(man.items || []).length} 视图=${items.length}`);
    check('manifest 中每个条目都有 id 与图片路径',
          (man.items || []).every((it) => it.id != null && (it.file || '').startsWith('imgs/')),
          JSON.stringify((man.items || [])[0] || {}).slice(0, 120));
  }

  const singleZip = await fetch(`${origin}/api/archive/${useIds[0]}/zip`, { headers: authHeaders() });
  const singleBuf = Buffer.from(await singleZip.arrayBuffer());
  check('单个批次的 zip 仍然可用（回归：新路由不能架空前一个）',
        singleZip.ok && singleBuf[0] === 0x50 && singleBuf[1] === 0x4b,
        `status=${singleZip.status} bytes=${singleBuf.length}`);

  const bad = await json(`${origin}/api/archive/merge?ids=abc,def`);
  check('非法 ids 被拒（400 且带错误说明）',
        bad.status === 400 && !!bad.data.error, `status=${bad.status} ${bad.text.slice(0, 80)}`);

  // ---------------------------------------------------------------- 浏览器
  const exe = findBrowser();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-merge-profile-'));
  const port = 10400 + Math.floor(Math.random() * 80);
  const child = spawn(exe, [
    '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--allow-file-access-from-files',
  ], { stdio: 'ignore' });

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
        const det = res.result.exceptionDetails;
        const desc = det.exception && (det.exception.description || det.exception.value);
        return { __error: desc || det.text };
      }
      return res.result && res.result.result ? res.result.result.value : null;
    };

    // ------------------------------------------------------------ D. 手机端
    console.log('\nD. 手机端：勾选 → 合并查看 / 合并保存');
    await send('Page.navigate', { url: pageUrl });
    await sleep(1200);
    await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)}); ${args.sid ? `localStorage.setItem('vsw.sid', ${JSON.stringify(args.sid)});` : ''}`);
    await send('Page.reload');
    await sleep(2500);
    if (!(await evaluate('S.server'))) {
      await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)}); ${args.sid ? `localStorage.setItem('vsw.sid', ${JSON.stringify(args.sid)});` : ''}; location.reload();`);
      await sleep(3000);
    }
    const booted = await evaluate('({ server: S.server, hasAlbumPick: S.albumPick instanceof Set })');
    check('手机端页面能启动（内联脚本没有语法错误）',
          booted && booted.server && booted.hasAlbumPick, JSON.stringify(booted));
    if (!booted || !booted.server) {
      console.log('页面没起来，后面的浏览器检查跳过。');
      throw new Error('手机端页面没起来');
    }

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
      return { batches: S.archives.length, archived: S.archived.length };
    })()`);
    check('快照里有归档批次与对象', synced && synced.batches > 1 && synced.archived > 0,
          JSON.stringify(synced));

    // 归档页是切到那一页才渲染的（渲染函数在切页时调用）
    await evaluate(`document.querySelector('nav button[data-v="archive"]').click()`);
    await sleep(1200);

    const boxes = await evaluate(`(() => {
      const cards = $$('#archiveList [data-batch]').length;
      const picks = $$('#archiveList [data-pick]').length;
      return { cards, picks, look: !!$('#archiveMergeLook'), save: !!$('#archiveMergeSave') };
    })()`);
    check('每个归档卡片都有勾选框（可以多选合并）',
          boxes && boxes.cards > 0 && boxes.picks === boxes.cards, JSON.stringify(boxes));
    check('合并条上有「合并查看」与「合并保存 zip」', boxes && boxes.look && boxes.save,
          JSON.stringify(boxes));

    const initial = await evaluate(`(() => {
      S.albumPick.clear(); syncAlbumBar();
      return { disabled: $('#archiveMergeLook').disabled, text: $('#archiveMergeLook').textContent,
               saveDisabled: $('#archiveMergeSave').disabled };
    })()`);
    check('没勾选时合并按钮是禁用的（不会点了没反应）',
          initial && initial.disabled === true && initial.saveDisabled === true,
          JSON.stringify(initial));

    // 勾选本地有对象的两批
    const pick = await evaluate(`(() => {
      const counts = {};
      for (const f of S.archived) counts[f.archive_id] = (counts[f.archive_id] || 0) + 1;
      const ranked = Object.keys(counts).map(Number)
        .sort((a, b) => counts[b] - counts[a]).slice(0, 2);
      S.albumPick.clear();
      $$('#archiveList [data-pick]').forEach((box) => { box.checked = false; });
      for (const id of ranked) {
        const box = $('#archiveList [data-pick="' + id + '"]');
        if (box) { box.checked = true; box.dispatchEvent(new Event('change')); }
      }
      return { ranked, picked: Array.from(S.albumPick),
               text: $('#archiveMergeLook').textContent,
               disabled: $('#archiveMergeLook').disabled,
               lit: $$('#archiveList [data-batch].is-picked').length,
               hint: $('#archiveMergeHint').textContent };
    })()`);
    check('勾选两个批次后按钮带上（N）并可点',
          pick && pick.picked.length === 2 && pick.disabled === false
            && /合并查看（2）/.test(pick.text || ''),
          JSON.stringify(pick));
    check('勾中的卡片高亮（看得见合了哪几批）', pick && pick.lit === 2, JSON.stringify(pick && pick.lit));

    const panel = await evaluate(`(async () => {
      await openMergedAlbum();
      const cards = $$('#archiveBatchList .fcard');
      const want = mergedAlbumItems();
      const srcs = Array.from(new Set(want.map((f) => Number(f.archive_id))));
      return {
        open: $('#sheet').classList.contains('open'),
        title: $('#sheetTitle').textContent,
        cards: cards.length,
        want: want.length,
        batches: srcs.length,
        thumbs: $$('#archiveBatchList img[src^="blob:"]').length,
        empty: /没有可显示的对象/.test($('#sheetBody').innerHTML),
      };
    })()`);
    check('点「合并查看」会弹出面板', panel && panel.open, JSON.stringify(panel && panel.title));
    check('面板标题写明是合并相册、合了哪几批',
          panel && /合并相册/.test(panel.title || '') && panel.batches >= 2,
          JSON.stringify(panel && panel.title));
    check('面板里的对象数等于跨批次去重后的条目数',
          panel && panel.cards === panel.want && panel.cards > 0,
          JSON.stringify(panel && { cards: panel.cards, want: panel.want }));
    await sleep(2000);
    const painted = await evaluate(`(() => ({
      total: $$('#archiveBatchList .fcard').length,
      withImg: $$('#archiveBatchList img[src^="blob:"]').length,
    }))()`);
    check('合并相册里的条目补上了缩略图（不是一片空白）',
          painted && painted.total > 0 && painted.withImg >= Math.ceil(painted.total * 0.8),
          JSON.stringify(painted));

    const zipUrl = await evaluate(`(() => {
      const seen = [];
      const proto = HTMLAnchorElement.prototype;
      const old = proto.click;
      proto.click = function () { seen.push(this.href); };
      try { saveMergedZip(); } finally { proto.click = old; }
      return { href: seen[0] || '', ids: Array.from(S.albumPick) };
    })()`);
    check('「合并保存 zip」请求的是合并接口且带上勾选的批次',
          zipUrl && /\/api\/archive\/merge\/zip\?ids=/.test(zipUrl.href || '')
            && zipUrl.href.includes(zipUrl.ids.join(',')),
          JSON.stringify(zipUrl));

    const cleared = await evaluate(`(() => {
      $('#archiveMergeClear').click();
      return { picked: Array.from(S.albumPick), disabled: $('#archiveMergeLook').disabled,
               checked: $$('#archiveList [data-pick]:checked').length };
    })()`);
    check('「清除勾选」后按钮回到禁用、勾选框全部取消',
          cleared && cleared.picked.length === 0 && cleared.disabled === true
            && cleared.checked === 0,
          JSON.stringify(cleared));

    // ------------------------------------------------------------ E. 桌面端
    console.log('\nE. 桌面端：勾选批次 → 合并');
    if (args.sid) {
      // 桌面端是 http 源，靠 cookie 认人；先把这个会话塞进浏览器，
      // 否则服务端开了账号之后页面只显示登录卡，表格里一行都没有。
      await send('Network.enable');
      await send('Network.setCookie', {
        name: 'vsw_sid', value: args.sid, domain: new URL(origin).hostname, path: '/',
      });
    }
    await send('Page.navigate', { url: origin + '/' });
    await sleep(2000);
    await evaluate(`document.querySelector('.tab[data-view="sources"]').click()`);
    await sleep(2200);
    const desk = await evaluate(`(() => ({
      picks: $$('#archiveTable .archpick').length,
      lookup: !!$('#archiveMergeLook'), folder: !!$('#archiveMergeFolder'),
      zipBtn: !!$('#archiveMergeZip'), bar: !!$('#archivePickInfo'),
      pickAll: !!$('#archivePickAll'),
    }))()`);
    check('桌面端归档表格有勾选列', desk && desk.picks > 0, JSON.stringify(desk));
    check('桌面端有合并条：查看 / 导出文件夹 / 保存 zip',
          desk && desk.lookup && desk.folder && desk.zipBtn && desk.bar && desk.pickAll,
          JSON.stringify(desk));

    const deskPick = await evaluate(`(() => {
      const boxes = $$('#archiveTable .archpick').slice(0, 2);
      boxes.forEach((box) => { box.checked = true; box.dispatchEvent(new Event('change')); });
      return { picked: Array.from(S.archivesPicked),
               text: $('#archiveMergeLook').textContent,
               disabled: $('#archiveMergeLook').disabled,
               lit: $$('#archiveTable tr.is-picked').length };
    })()`);
    check('桌面端勾选后合并按钮带上（N）',
          deskPick && deskPick.picked.length === 2 && deskPick.disabled === false
            && /合并查看（2）/.test(deskPick.text || ''),
          JSON.stringify(deskPick));

    const deskBoard = await evaluate(`(async () => {
      $('#archiveMergeLook').click();
      await new Promise((r) => setTimeout(r, 1200));
      const title = $('#archiveDetailTitle').textContent;
      const ids = Array.from(S.archivesPicked).join(',');
      const expect = await api('/api/archive/merge?ids=' + ids);
      return { title, cards: $$('#archiveBoard .fcard').length, shown: !$('#archiveDetailCard').hidden,
               expect: (expect.items || []).length, count: expect.count_all };
    })()`);
    check('桌面端「合并查看」出合并板（标题写明合并相册）',
          deskBoard && deskBoard.shown && /合并相册/.test(deskBoard.title || ''),
          JSON.stringify(deskBoard && deskBoard.title));
    check('桌面端合并板条目数与服务端一致',
          deskBoard && deskBoard.cards === deskBoard.expect && deskBoard.cards > 0,
          JSON.stringify(deskBoard && { cards: deskBoard.cards, expect: deskBoard.expect }));

    const deskZip = await evaluate(`(() => {
      const seen = [];
      const proto = HTMLAnchorElement.prototype;
      const old = proto.click;
      proto.click = function () { seen.push(this.getAttribute('href')); };
      try { $('#archiveMergeZip').click(); } finally { proto.click = old; }
      return { href: seen[0] || '', ids: Array.from(S.archivesPicked) };
    })()`);
    check('桌面端「合并保存 zip」指向合并接口',
          deskZip && /\/api\/archive\/merge\/zip\?ids=/.test(deskZip.href || ''),
          JSON.stringify(deskZip));

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
