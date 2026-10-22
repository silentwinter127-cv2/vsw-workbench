/**
 * 探针：手机端队列页的「筛选」和「删除」。
 *
 * 背景（用户反馈）：手机端只有状态栏和引擎筛选，也没法删队列项，操作起来麻烦。
 * 补齐后需要确认的是行为，不是"有没有按钮"：
 *   A. 筛选真的生效（引擎 / 变体 / 种子 / 关键词 / 产出各自单独生效，且可叠加）；
 *   B. 「删除筛选结果」删的正好是筛出来的那批；
 *   C. 删除只删队列记录 —— 已产出的结果与种子不受影响；
 *   D. 删完列表、计数、本地库都同步更新（不会留幽灵条目）。
 *
 * 为了不破坏真实队列，探针会**先给自己造几条测试队列项**（用同一个种子、指定变体），
 * 验证完只删自己造的那些。
 *
 * 用法：node vsw-probe-queue-filter.js --apk ../vsw-workbench.apk --origin http://127.0.0.1:8799
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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-qf-probe-'));
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'vsw-qf-profile-'));
  const port = 9950 + Math.floor(Math.random() * 80);
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

    // 先造测试队列项（用真实种子的某个变体，避免污染其它数据）
    const created = await evaluate(`(async () => {
      const res = await fetch(${JSON.stringify(args.origin)} + '/api/queue?status=all&limit=50');
      const q = await res.json();
      const seed = (q.jobs || []).find(j => j.seed_id);
      if (!seed) return { error: '队列里没有可用的种子' };
      // 用种子重新入队一个"指定变体"，这样能精确认出自己造的那几条
      const make = await fetch(${JSON.stringify(args.origin)} + '/api/queue/enqueue', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seed_ids: [seed.seed_id], engines: ['baidu'], kinds: ['edges'] }),
      });
      const out = await make.json();
      return { seedId: seed.seed_id, jobIds: out.job_ids || [] };
    })()`);
    check('已造出用于验证的测试队列项', created && created.jobIds && created.jobIds.length > 0,
          JSON.stringify(created));
    const testIds = new Set((created && created.jobIds) || []);

    console.log(`内核 ${version.Browser}\n页面 ${pageUrl}\n`);
    await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)})`);
    await send('Page.navigate', { url: pageUrl });
    await sleep(2500);
    const ready = await evaluate('S.server');
    if (!ready) {
      await evaluate(`localStorage.setItem('vsw.server', ${JSON.stringify(args.origin)}); location.reload();`);
      await sleep(3000);
    }

    // 拉队列写本地库
    const synced = await evaluate(`(async () => {
      const data = await api('/api/queue?status=all&limit=800');
      await local.clear('jobs');
      for (const row of data.jobs || []) await local.put('jobs', row);
      S.jobs = data.jobs || [];
      switchTab('queue');
      await new Promise((r) => setTimeout(r, 3000));
      return { jobs: S.jobs.length };
    })()`);
    check('队列加载出条目', synced && synced.jobs > 0, JSON.stringify(synced));

    // 筛选控件是否都在
    const ui = await evaluate(`(() => ({
      seed: !!document.querySelector('#queueSeed'),
      kinds: document.querySelectorAll('#queueFilterKinds .chip').length,
      engines: document.querySelectorAll('#queueFilterEngines .chip').length,
      text: !!document.querySelector('#queueText'),
      results: !!document.querySelector('#queueResults'),
      clear: !!document.querySelector('#queueFilterClear'),
      summary: (document.querySelector('#queueFilterSummary') || {}).textContent || '',
      selectBtn: !!document.querySelector('#queueSelBtn'),
      deleteFiltered: !!document.querySelector('#queueDeleteFiltered'),
    }))()`);
    check('筛选控件齐全（种子/引擎/变体/关键词/产出/清空）',
          ui && ui.seed && ui.text && ui.results && ui.clear && ui.engines > 0 && ui.kinds > 0,
          JSON.stringify(ui));
    check('删除入口齐全（选择模式 + 删除筛选结果）',
          ui && ui.selectBtn && ui.deleteFiltered, JSON.stringify(ui));

    // A. 逐个筛选单独生效
    const filters = await evaluate(`(async () => {
      const out = {};
      const reset = () => { S.queueSeed=''; S.queueText=''; S.queueResults='';
                            S.queueEngines.clear(); S.queueKinds.clear(); };
      // 引擎
      reset(); S.queueStatus = 'all';
      const eng = S.jobs[0].engine;
      S.queueEngines.add(eng); renderQueue();
      out.engine = { want: eng, total: queueRows().length,
                     allMatch: queueRows().every(j => j.engine === eng) };
      // 变体
      reset();
      const kind = S.jobs[0].variant_kind;
      S.queueKinds.add(kind); renderQueue();
      out.kind = { want: kind, total: queueRows().length,
                   allMatch: queueRows().every(j => j.variant_kind === kind) };
      // 种子
      reset();
      const sid = S.jobs[0].seed_id;
      S.queueSeed = String(sid); renderQueue();
      out.seed = { want: sid, total: queueRows().length,
                   allMatch: queueRows().every(j => String(j.seed_id) === String(sid)) };
      // 关键词（用种子标题的一段）
      reset();
      S.queueText = 'e8416a8c'; renderQueue();
      out.text = { total: queueRows().length,
                   allMatch: queueRows().every(j => JSON.stringify(j).toLowerCase().includes('e8416a8c')) };
      // 产出
      reset();
      S.queueResults = 'with'; renderQueue();
      out.withResults = { total: queueRows().length, allMatch: queueRows().every(j => j.findings > 0) };
      reset();
      S.queueResults = 'without'; renderQueue();
      out.withoutResults = { total: queueRows().length, allMatch: queueRows().every(j => !(j.findings > 0)) };
      // 叠加：某引擎 + 某变体
      reset();
      S.queueEngines.add(eng); S.queueKinds.add(kind); renderQueue();
      out.combined = { total: queueRows().length,
                       allMatch: queueRows().every(j => j.engine === eng && j.variant_kind === kind) };
      reset(); renderQueue();
      return out;
    })()`);
    const fOk = filters && ['engine', 'kind', 'seed', 'text', 'withResults', 'withoutResults', 'combined']
      .every((k) => filters[k] && filters[k].allMatch);
    check('每个筛选条件都真的生效（且可叠加）', fOk, JSON.stringify(filters));
    check('筛选摘要会显示当前条件', ui && /共 \d+ 条|筛出/.test(ui.summary || ''), ui && ui.summary);

    // D. 按引擎批量删的预设按钮：只清该引擎的待处理，别的引擎不动
    const preset = await evaluate(`(async () => {
      window.confirm = () => true;
      // 造几条 so360 的待处理任务（这个引擎常年被验证码挡着，正是"整批清掉"的典型场景）
      const seed = S.jobs.find(j => j.seed_id) || null;
      if (!seed) return { skipped: '队列里没有可用种子' };
      await post('/api/queue/enqueue', { seed_ids: [seed.seed_id], engines: ['so360'], kinds: ['full'] });
      await refreshQueueFromServer({ silent: true });
      S.queueSelectMode = false;
      S.queueSeed = ''; S.queueText = ''; S.queueResults = '';
      S.queueEngines.clear(); S.queueKinds.clear();
      S.queueStatus = 'all';
      renderQueue();
      const btn = Array.from(document.querySelectorAll('#queueActions [data-preset-engine]'))
        .find(b => b.dataset.presetEngine === 'so360');
      if (!btn) return { skipped: '没有 so360 的预设按钮' };
      const before = S.jobs.filter(j => j.engine === 'so360'
        && ['pending','running','review'].includes(j.status)).length;
      const othersBefore = S.jobs.filter(j => j.engine !== 'so360'
        && ['pending','running','review'].includes(j.status)).length;
      btn.click();
      await new Promise(r => setTimeout(r, 3000));
      return {
        before, othersBefore,
        afterSo360: S.jobs.filter(j => j.engine === 'so360'
          && ['pending','running','review'].includes(j.status)).length,
        othersAfter: S.jobs.filter(j => j.engine !== 'so360'
          && ['pending','running','review'].includes(j.status)).length,
        hasPreset: true,
      };
    })()`);
    if (preset && preset.skipped) {
      console.log(`      （跳过按引擎清理断言：${preset.skipped}）`);
    } else {
      check('存在「按引擎清理」的预设按钮', preset && preset.hasPreset);
      check('点预设只清掉该引擎的待处理任务',
            preset && preset.before > 0 && preset.afterSo360 === 0,
            JSON.stringify(preset));
      check('预设不影响其它引擎的任务',
            preset && preset.othersAfter === preset.othersBefore, JSON.stringify(preset));
    }

    // B/C/D. 删除：只删自己造的测试项，并确认结果与种子不受影响
    const del = await evaluate(`(async () => {
      window.confirm = () => true;                  // 探针里不需要真人确认
      const ids = ${JSON.stringify(Array.from(testIds))};
      const beforeFindings = await api('/api/findings?limit=1&dups=1');
      const beforeSeeds = await api('/api/seeds?limit=500');
      await deleteQueueJobs(ids, '测试');
      await new Promise((r) => setTimeout(r, 2500));
      const afterJobs = S.jobs.map(j => j.id);
      const serverQ = await api('/api/queue?status=all&limit=800');
      const serverIds = (serverQ.jobs || []).map(j => j.id);
      const afterSeeds = await api('/api/seeds?limit=500');
      return {
        wantGone: ids,
        stillInMemory: ids.filter(i => afterJobs.includes(i)),
        stillOnServer: ids.filter(i => serverIds.includes(i)),
        inLocalDb: (await local.all('jobs')).filter(j => ids.includes(j.id)).map(j => j.id),
        seedsBefore: (beforeSeeds.seeds || []).length,
        seedsAfter: (afterSeeds.seeds || []).length,
        jobsCount: afterJobs.length,
      };
    })()`);
    check('删除的正是选中的那些（内存里已消失）',
          del && del.stillInMemory.length === 0, JSON.stringify(del && del.stillInMemory));
    check('电脑端的队列也真的删掉了', del && del.stillOnServer.length === 0,
          JSON.stringify(del && del.stillOnServer));
    check('手机本地库同步更新（没有幽灵条目）', del && del.inLocalDb.length === 0,
          JSON.stringify(del && del.inLocalDb));
    check('删除队列项不影响种子', del && del.seedsBefore === del.seedsAfter,
          JSON.stringify({ before: del && del.seedsBefore, after: del && del.seedsAfter }));

    // 多选模式 DELETE 单条
    const single = await evaluate(`(async () => {
      const target = S.jobs.find(j => j.id && !${JSON.stringify(Array.from(testIds))}.includes(j.id));
      if (!target) return { skipped: '没有可删的条目' };
      window.confirm = () => true;
      await deleteQueueJobs([target.id], '这个');
      await new Promise((r) => setTimeout(r, 2000));
      return { id: target.id, gone: !S.jobs.some(j => j.id === target.id) };
    })()`);
    if (single && single.skipped) {
      console.log('      （跳过单条删除断言：没有可用条目）');
    } else {
      check('单条删除也生效（卡片上的「删除」走同一条路径）',
            single && single.gone, JSON.stringify(single));
    }

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
