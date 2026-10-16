/* 横向视觉搜索工作台 —— 前端逻辑（无框架，纯 DOM）
 *
 * 设计前提（实测得出，不是假设）：
 *   百度识图的 upload 接口从本机返回 {"status":1,"msg":"Reject"}，image.baidu.com 的
 *   objurl 深链直接跳验证码；Google Lens / TinEye / SauceNAO 本机连不通。
 *   所以工作台不假装能抓取引擎：它负责派发（一键打开入口 / 深链）、登记、去重、聚类、
 *   排序和积累，把结果页内容或 URL 回填进来即可。
 */

const S = {
  health: null,
  engines: [],
  variantKinds: [],
  settings: {},
  queue: { jobs: [], counts: {} },
  queueStatus: 'todo',
  board: { findings: [] },
  triage: 'all',
  order: 'value',
  includeDups: false,
  selectedJobs: new Set(),
  selectedFindings: new Set(),
  graph: { nodes: [], edges: [] },
  graphView: { scale: 1, x: 0, y: 0, drag: null },
  lastParse: null,
};

// ---------------------------------------------------------------- tiny helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (v) => `${Math.round((Number(v) || 0) * 100)}%`;
const num = (v, d = 2) => (Number(v) || 0).toFixed(d);
// Stored paths look like "media/thumbs/x.jpg" (relative to the data root) and are served
// under "/media": strip a leading "media/" so we never emit "/media/media/...".
const media = (rel) => (rel ? `/media/${String(rel).replace(/^\/+/, '').replace(/^media\//i, '')}` : '');

function toast(msg, bad = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('bad', !!bad);
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), bad ? 5200 : 2800);
}

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  if (!resp.ok) {
    const msg = (data && (data.error || data.message)) || `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return data;
}

const TRIAGE_LABEL = {
  tracking: '来源追踪',
  diffusion: '主题扩散',
  bridge: '桥接探索',
};
const TRIAGE_NOTE = {
  tracking: '视觉相似度极高：找原图、不同分辨率、裁剪版、转载页',
  diffusion: '中等相似：同期、同场景、同风格，能开出新的搜索分支',
  bridge: '新颖性优先：能通向新平台或新主题簇的桥接图',
};
const STATUS_LABEL = { pending: '待搜索', running: '搜索中', review: '待复查', done: '已完成', failed: '失败' };

// ------------------------------------------------------------------- rendering
function renderTabs() {
  $$('.tab[data-view]').forEach((tab) => {
    tab.onclick = () => {
      $$('.tab[data-view]').forEach((t) => t.classList.toggle('active', t === tab));
      $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${tab.dataset.view}`));
      if (tab.dataset.view === 'graph') drawGraph();
      if (tab.dataset.view === 'metrics') loadReport();
      if (tab.dataset.view === 'sources') loadSources();
    };
  });
}

function renderHealth() {
  const h = S.health;
  if (!h) return;
  const v = h.vision || {};
  $('#enginePill').className = `pill ${v.available ? 'ok' : 'bad'}`;
  $('#enginePill').textContent = v.available ? `SigLIP2 就绪` : '语义层不可用（退化为文件层+感知哈希层）';
  $('#enginePill').title = v.available ? (v.model || '') : (v.reason || '');
  const st = h.stats || {};
  $('#statPill').textContent = `种子 ${st.seeds || 0} · 变体 ${st.variants || 0} · 对象 ${st.findings || 0} · 来源 ${st.sources || 0}`;
  const r = h.reindex || {};
  if (r.running) toast(`语义索引补算中 ${r.done}/${r.total}`);
}

function variantPicker(rootSel, selected = null) {
  const root = $(rootSel);
  const groups = { base: '基准', geometry: '几何', tone: '色调', quality: '画质', custom: '其它' };
  const chosen = selected || new Set(S.variantKinds.map((k) => k.kind));
  root.className = 'chips';
  root.innerHTML = S.variantKinds.map((k) => `
    <span class="chip ${chosen.has(k.kind) ? 'on' : ''}" data-kind="${esc(k.kind)}" title="${esc(k.note)}">
      ${esc(k.label)} <small>${esc(groups[k.group] || k.group)}</small>
    </span>`).join('');
  $$('.chip', root).forEach((chip) => {
    chip.onclick = () => {
      chip.classList.toggle('on');
      root.dataset.changed = '1';
    };
  });
}

function pickedVariants(rootSel) {
  return $$(`${rootSel} .chip.on`).map((c) => c.dataset.kind);
}

function pickedEngines(rootSel) {
  return $$(`${rootSel} .chip.on`).map((c) => c.dataset.engine);
}

function renderEnginePicker(rootSel, selected = null) {
  const root = $(rootSel);
  const chosen = selected || new Set(['baidu', 'so360']);
  root.className = 'chips';
  root.innerHTML = S.engines.map((e) => {
    const dead = !e.browser && !e.accepts_image_url && e.manual_upload === false;
    const tag = dead ? ' <small>已失效</small>'
      : (e.browser ? ' <small>自动</small>' : (e.accepts_image_url ? ' <small>可深链</small>' : ' <small>手动</small>'));
    return `<span class="chip ${chosen.has(e.key) && !dead ? 'on' : ''}${dead ? ' off' : ''}"
      data-engine="${esc(e.key)}" data-dead="${dead ? '1' : ''}"
      style="${dead ? 'opacity:.45;text-decoration:line-through' : ''}"
      title="${esc(e.note)}">${esc(e.name)}${tag}</span>`;
  }).join('');
  $$('.chip', root).forEach((chip) => {
    if (chip.dataset.dead === '1') return;   // 已失效的引擎不允许选中
    chip.onclick = () => chip.classList.toggle('on');
  });
}

// ------------------------------------------------------------------ 种子图片池
async function loadSeeds() {
  const data = await api('/api/seeds?limit=200');
  S.seeds = data.seeds || [];
  const rows = S.seeds.map((s) => `
    <div class="item">
      <img class="thumb" src="${media(s.thumb_path)}" alt="">
      <div>
        <div class="t1">${esc(s.title || `种子 #${s.id}`)}</div>
        <div class="t2">#${s.id} · ${s.width}×${s.height} · 变体 ${s.variant_count} · 队列 ${s.jobs_done}/${s.job_count}
          · ${esc((s.tags || []).map((t) => t.name).join(' / ') || '无标签')}</div>
      </div>
      <div class="row tight">
        <button class="btn primary" data-act="crop" data-id="${s.id}" title="在图上框一块区域，只拿这一块去检索">框选搜索</button>
        <button class="btn" data-act="gene" data-id="${s.id}" title="补齐变体（缺哪补哪）">生成变体</button>
        <button class="btn" data-act="queue" data-id="${s.id}" title="按当前选择的引擎与变体入队">入队</button>
        <button class="btn danger" data-act="del" data-id="${s.id}">删</button>
      </div>
    </div>`).join('');
  $('#seedList').innerHTML = rows || '<div class="empty">还没有种子图。拖拽图片到左侧，或从 URL / 目录导入。</div>';
  $$('#seedList [data-act]').forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      const id = Number(btn.dataset.id);
      const engines = pickedEngines('#importEngines');
      const kinds = pickedVariants('#importVariants');
      try {
        if (btn.dataset.act === 'crop') {
          openCropDrawer(id);
          return;
        }
        if (btn.dataset.act === 'gene') {
          const out = await api(`/api/seed/${id}/variants`, { method: 'POST', body: { kinds: null } });
          toast(`种子 #${id} 现有 ${out.variants.length} 个变体`);
        } else if (btn.dataset.act === 'queue') {
          const out = await api('/api/queue/enqueue', { method: 'POST', body: { seed_ids: [id], engines, kinds } });
          toast(`已入队 ${out.job_ids.length} 项`);
          await loadQueue();
        } else if (btn.dataset.act === 'del') {
          if (!confirm(`删除种子 #${id} 及其变体/结果？`)) return;
          await api(`/api/seed/${id}`, { method: 'DELETE' });
          toast(`已删除种子 #${id}`);
          await loadSeeds();
        }
        if (btn.dataset.act !== 'queue') await loadSeeds();
      } catch (e) { toast(e.message, true); }
    };
  });
}

async function importFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  const engines = pickedEngines('#importEngines');
  const kinds = pickedVariants('#importVariants');
  const uploads = await Promise.all(list.map((f) => new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: f.name, data_url: reader.result });
    reader.readAsDataURL(f);
  })));
  toast(`导入 ${uploads.length} 张，正在生成变体…`);
  try {
    const out = await api('/api/import', {
      method: 'POST',
      body: { kind: 'upload', uploads, variants: kinds, auto_enqueue: $('#autoEnqueue').checked, engines },
    });
    const first = out.imported[0] || {};
    toast(`导入完成：${out.imported.length} 张，变体 ${first.variants ? first.variants.length : 0}，入队 ${(first.job_ids || []).length}`);
    await loadSeeds(); await loadQueue(); await refreshHealth();
  } catch (e) { toast(e.message, true); }
}

function bindImportPanel() {
  const drop = $('#drop');
  drop.onclick = () => $('#fileInput').click();
  $('#fileInput').onchange = (ev) => importFiles(ev.target.files);
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (ev) => {
    ev.preventDefault(); drop.classList.add('hot');
  }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (ev) => {
    ev.preventDefault(); drop.classList.remove('hot');
  }));
  drop.addEventListener('drop', (ev) => importFiles(ev.dataTransfer.files));

  $('#importPathBtn').onclick = async () => {
    const path = $('#importPath').value.trim();
    if (!path) return toast('先填目录或文件路径', true);
    const isDir = !/\.(jpe?g|png|gif|webp|bmp|tiff?)$/i.test(path);
    try {
      toast(isDir ? '扫描目录中…' : '导入中…');
      const out = await api('/api/import', {
        method: 'POST',
        body: isDir
          ? { kind: 'directory', path, limit: 500, variants: pickedVariants('#importVariants'),
              auto_enqueue: $('#autoEnqueue').checked, engines: pickedEngines('#importEngines') }
          : { kind: 'path', paths: [path], variants: pickedVariants('#importVariants'),
              auto_enqueue: $('#autoEnqueue').checked, engines: pickedEngines('#importEngines') },
      });
      toast(isDir ? `扫描 ${out.scanned}，新导入 ${out.imported}` : '导入完成');
      await loadSeeds(); await loadQueue(); await refreshHealth();
    } catch (e) { toast(e.message, true); }
  };

  $('#importUrlBtn').onclick = async () => {
    const urls = $('#importUrls').value.split(/\s+/).map((s) => s.trim()).filter(Boolean);
    if (!urls.length) return toast('先填图片 URL', true);
    try {
      toast(`导入 ${urls.length} 个 URL…`);
      const out = await api('/api/import', {
        method: 'POST',
        body: { kind: 'url', urls, variants: pickedVariants('#importVariants'),
                auto_enqueue: $('#autoEnqueue').checked, engines: pickedEngines('#importEngines') },
      });
      toast(`成功 ${out.imported.length}，失败 ${(out.errors || []).length}`);
      if ((out.errors || []).length) console.warn(out.errors);
      await loadSeeds(); await loadQueue(); await refreshHealth();
    } catch (e) { toast(e.message, true); }
  };
}

// -------------------------------------------------------------------- 批处理队列
const QUEUE_FILTER = { seedId: '', engines: new Set(), kinds: new Set(), text: '', results: '' };
let QUEUE_FACETS = { engines: [], kinds: [], seeds: [], results: { with: 0, without: 0 } };

function queueQuery() {
  const params = new URLSearchParams({ status: S.queueStatus });
  if (QUEUE_FILTER.seedId) params.set('seed_id', QUEUE_FILTER.seedId);
  if (QUEUE_FILTER.engines.size) params.set('engines', Array.from(QUEUE_FILTER.engines).join(','));
  if (QUEUE_FILTER.kinds.size) params.set('kinds', Array.from(QUEUE_FILTER.kinds).join(','));
  if (QUEUE_FILTER.text.trim()) params.set('q', QUEUE_FILTER.text.trim());
  if (QUEUE_FILTER.results) params.set('results', QUEUE_FILTER.results);
  return params.toString();
}

function queueFilterActive() {
  return Boolean(QUEUE_FILTER.seedId || QUEUE_FILTER.engines.size || QUEUE_FILTER.kinds.size
                 || QUEUE_FILTER.text.trim() || QUEUE_FILTER.results);
}

function renderQueueFilterControls() {
  const engines = QUEUE_FACETS.engines || [];
  $('#queueFilterEngines').innerHTML = engines.map((e) => {
    const name = (S.engines.find((x) => x.key === e.key) || {}).name || e.key;
    return `<span class="chip ${QUEUE_FILTER.engines.has(e.key) ? 'on' : ''}" data-engine="${esc(e.key)}"
      title="共 ${e.total} 条 · 已产出 ${e.with} 条 · 未跑 ${e.pending} 条">${esc(name)}
      <span class="n">${e.with}/${e.total}</span></span>`;
  }).join('') || '<span class="small">队列是空的</span>';
  $$('#queueFilterEngines .chip').forEach((chip) => {
    chip.onclick = () => {
      const key = chip.dataset.engine;
      if (QUEUE_FILTER.engines.has(key)) QUEUE_FILTER.engines.delete(key);
      else QUEUE_FILTER.engines.add(key);
      loadQueue();
    };
  });

  const kindLabels = {};
  (S.variantKinds || []).forEach((k) => { kindLabels[k.kind] = k.label; });
  const kinds = QUEUE_FACETS.kinds || [];
  $('#queueFilterKinds').innerHTML = kinds.map((k) => `
    <span class="chip ${QUEUE_FILTER.kinds.has(k.kind) ? 'on' : ''}" data-kind="${esc(k.kind)}"
      title="共 ${k.total} 条 · 已产出 ${k.with} 条 · 未跑 ${k.pending} 条">
      ${esc(kindLabels[k.kind] || k.kind)} <span class="n">${k.with}/${k.total}</span></span>`).join('')
    || '<span class="small">-</span>';
  $$('#queueFilterKinds .chip').forEach((chip) => {
    chip.onclick = () => {
      const key = chip.dataset.kind;
      if (QUEUE_FILTER.kinds.has(key)) QUEUE_FILTER.kinds.delete(key);
      else QUEUE_FILTER.kinds.add(key);
      loadQueue();
    };
  });

  const select = $('#queueSeed');
  const seeds = QUEUE_FACETS.seeds || [];
  const wanted = String(QUEUE_FILTER.seedId || '');
  select.innerHTML = '<option value="">全部种子</option>' + seeds.map((s) => `
    <option value="${s.seed_id}" ${wanted === String(s.seed_id) ? 'selected' : ''}>
      #${s.seed_id} ${(s.title || '').slice(0, 34)} · ${s.pending} 待跑/${s.total}</option>`).join('');
  $('#queueResults').value = QUEUE_FILTER.results;
  $('#queueText').value = QUEUE_FILTER.text;
}

function queueFilterSummary(count) {
  const bits = [];
  if (QUEUE_FILTER.seedId) bits.push(`种子 #${QUEUE_FILTER.seedId}`);
  if (QUEUE_FILTER.engines.size) bits.push(`${QUEUE_FILTER.engines.size} 个引擎`);
  if (QUEUE_FILTER.kinds.size) bits.push(`${QUEUE_FILTER.kinds.size} 种变体`);
  if (QUEUE_FILTER.text.trim()) bits.push(`“${QUEUE_FILTER.text.trim()}”`);
  if (QUEUE_FILTER.results) bits.push(QUEUE_FILTER.results === 'with' ? '有产出' : '无产出');
  const rest = QUEUE_FACETS.results || { with: 0, without: 0 };
  $('#queueFilterHint').textContent = `筛选只影响列表显示，不会改动队列本身。`
    + `（全部 ${QUEUE_FACETS.total || 0} 条中，已产出 ${rest.with} 条、还没产出 ${rest.without} 条）`;
  $('#queueFilterSummary').textContent = queueFilterActive()
    ? `· 已筛选 ${count} 条（${bits.join(' + ')}）`
    : `· 未筛选（共 ${QUEUE_FACETS.total || 0} 条）`;
}

async function loadQueue() {
  const data = await api(`/api/queue?${queueQuery()}`);
  S.queue = data;
  if (data.facets) QUEUE_FACETS = data.facets;
  const counts = data.counts || {};
  $('#queueCounts').innerHTML = ['pending', 'running', 'review', 'done', 'failed']
    .map((k) => `<span class="pill">${STATUS_LABEL[k]} ${counts[k] || 0}</span>`).join('');
  renderQueueFilterControls();
  queueFilterSummary((data.jobs || []).length);
  $('#queueList').innerHTML = (data.jobs || []).map((j) => `
    <div class="item ${S.selectedJobs.has(j.id) ? 'sel' : ''}" data-id="${j.id}">
      <img class="thumb" src="${media(j.variant_path) || media(j.seed_thumb)}" alt="">
      <div>
        <div class="t1">${esc(j.engine_name)} · ${esc(j.variant_label || j.variant_kind)} <span class="badge ${j.status}">${STATUS_LABEL[j.status] || j.status}</span>
          ${j.findings ? `<span class="badge done">产出 ${j.findings}</span>` : ''}</div>
        <div class="t2">#${j.id} 种子「${esc(j.seed_title || '')}」 · 变体 ${esc(j.variant_kind)} · 已回填 ${j.findings} 条${j.note ? ' · ' + esc(j.note) : ''}</div>
      </div>
      <div class="row tight">
        ${j.browser ? `<button class="btn primary" data-act="auto" data-id="${j.id}" title="无头浏览器自动上传变体并读回结果">自动识图</button>` : ''}
        <button class="btn ${j.browser ? '' : 'primary'}" data-act="go" data-id="${j.id}">打开识图</button>
        <button class="btn" data-act="ingest" data-id="${j.id}">回填结果</button>
        <button class="btn ghost" data-act="dup" data-id="${j.id}" title="再开一个同一变体的其它引擎">×2</button>
        <button class="btn danger" data-act="del" data-id="${j.id}">删</button>
      </div>
    </div>`).join('') || '<div class="empty">队列为空。到「种子图片池」选择引擎与变体后入队。</div>';

  $$('#queueList [data-act]').forEach((btn) => {
    btn.onclick = (ev) => { ev.stopPropagation(); queueAction(btn.dataset.act, Number(btn.dataset.id)); };
  });
  $$('#queueList .item').forEach((item) => {
    item.onclick = () => {
      const id = Number(item.dataset.id);
      if (S.selectedJobs.has(id)) S.selectedJobs.delete(id); else S.selectedJobs.add(id);
      item.classList.toggle('sel');
      $('#ingestTarget').textContent = S.selectedJobs.size ? `已选 ${S.selectedJobs.size} 项` : '未选择队列项';
    };
  });
  $('#ingestTarget').textContent = S.selectedJobs.size ? `已选 ${S.selectedJobs.size} 项` : '未选择队列项';
}

async function queueAction(act, id) {
  const job = (S.queue.jobs || []).find((j) => j.id === id);
  try {
    if (act === 'auto') {
      // 无头浏览器自动上传变体 + 读结果页 DOM：这条路是实测唯一还通的（百度 upload 被拒、
      // objurl 深链跳验证码、Bing 已把按图搜索改成文字搜索）。
      await api(`/api/job/${id}/browser`, { method: 'POST', body: { timeout_ms: 90000 } });
      toast(`已启动自动识图（${job ? job.engine_name : ''}）。浏览器要跑十几秒，结果会自动入库。`);
      pollBrowserState();
      return;
    }
    if (act === 'go') {
      const out = await api(`/api/job/${id}/execute`, { method: 'POST', body: {} });
      window.open(out.dispatch_url, '_blank', 'noopener');
      if (out.accepts_image_url) {
        toast('已用图片直链打开；结果页可直接把 URL 回填');
      } else {
        toast(`已打开${out.engine_name}。请上传本地变体文件：${out.local_file}`, false);
        if (navigator.clipboard && out.local_file) {
          navigator.clipboard.writeText(out.local_file).catch(() => {});
        }
      }
      await loadQueue();
    } else if (act === 'ingest') {
      S.selectedJobs.add(id);
      $('#ingestTarget').textContent = `已选 ${S.selectedJobs.size} 项`;
      document.querySelector('.tab[data-view="queue"]').click();
      $('#ingestPayload').focus();
      toast(`目标队列项 #${id}，把结果页内容或 URL 粘到下面`);
    } else if (act === 'dup') {
      const engines = pickedEngines('#queueEngines');
      const out = await api('/api/queue/enqueue', { method: 'POST', body: { variant_ids: [job.variant_id], engines } });
      toast(`同一变体再入队 ${out.job_ids.length} 项`);
      await loadQueue();
    } else if (act === 'del') {
      await api(`/api/job/${id}`, { method: 'DELETE' });
      S.selectedJobs.delete(id);
      await loadQueue();
    }
  } catch (e) { toast(e.message, true); }
}

function pollBrowserState() {
  if (pollBrowserState._timer) return;
  const el = $('#browserState');
  const tick = async () => {
    try {
      const st = await api('/api/browser/state');
      if (st.running) {
        if (el) {
          el.className = 'pill';
          el.textContent = `自动识图：${st.phase || '运行中'}`;
        }
      } else {
        clearInterval(pollBrowserState._timer);
        pollBrowserState._timer = null;
        if (el) {
          el.className = `pill ${st.error ? 'bad' : 'ok'}`;
          el.textContent = st.error ? `自动识图失败：${String(st.error).slice(0, 60)}`
            : `自动识图完成（候选 ${st.found || 0}）`;
        }
        await loadQueue();
        await loadBoard();
        await refreshHealth();
      }
    } catch (e) { /* 服务重启中，下一轮再试 */ }
  };
  pollBrowserState._timer = setInterval(tick, 2500);
  tick();
}

async function submitIngest() {
  if (!S.selectedJobs.size) return toast('先在右上队列里点一项的「回填结果」', true);
  const payload = $('#ingestPayload').value.trim();
  const pageUrl = $('#ingestPageUrl').value.trim();
  if (!payload && !pageUrl) return toast('粘贴结果页内容，或填结果页 URL', true);
  const fetchLimit = Number($('#ingestLimit').value) || 60;
  let ok = 0; let news = 0; let dups = 0; let fails = [];
  for (const id of Array.from(S.selectedJobs)) {
    try {
      toast(`回填队列项 #${id}…`);
      const out = await api(`/api/job/${id}/ingest`, {
        method: 'POST',
        body: { payload, page_url: pageUrl, fetch_limit: fetchLimit, download: true },
      });
      ok += 1; news += out.new_findings; dups += out.duplicates;
      fails = fails.concat(out.failures || []);
    } catch (e) { toast(`#${id} 失败：${e.message}`, true); }
  }
  toast(`回填完成：${ok} 项 · 新增视觉对象 ${news} · 折叠重复转载 ${dups}${fails.length ? ` · ${fails.length} 张取图失败` : ''}`);
  if (fails.length) console.warn('取图失败', fails);
  S.selectedJobs.clear();
  $('#ingestPayload').value = '';
  await loadQueue(); await loadBoard(); await refreshHealth();
}

function bindQueuePanel() {
  $$('.qbar .tab').forEach((tab) => {
    tab.onclick = () => {
      $$('.qbar .tab').forEach((t) => t.classList.toggle('active', t === tab));
      S.queueStatus = tab.dataset.status;
      loadQueue();
    };
  });
  $('#queueSeed').onchange = (ev) => { QUEUE_FILTER.seedId = ev.target.value; loadQueue(); };
  $('#queueResults').onchange = (ev) => { QUEUE_FILTER.results = ev.target.value; loadQueue(); };
  let textTimer = null;
  $('#queueText').oninput = (ev) => {
    QUEUE_FILTER.text = ev.target.value;
    clearTimeout(textTimer);
    textTimer = setTimeout(loadQueue, 300);   // 打字时不要每按一下就请求一次
  };
  $('#queueFilterClear').onclick = () => {
    QUEUE_FILTER.seedId = '';
    QUEUE_FILTER.engines.clear();
    QUEUE_FILTER.kinds.clear();
    QUEUE_FILTER.text = '';
    QUEUE_FILTER.results = '';
    loadQueue();
  };
  $('#queueEnqueueBtn').onclick = async () => {
    const seedIds = S.seeds.map((s) => s.id);
    if (!seedIds.length) return toast('还没有种子图', true);
    try {
      const out = await api('/api/queue/enqueue', {
        method: 'POST',
        body: { seed_ids: seedIds, engines: pickedEngines('#queueEngines'), kinds: null },
      });
      toast(`全部 ${seedIds.length} 个种子已入队：${out.job_ids.length} 项（已有项会复用）`);
      await loadQueue();
    } catch (e) { toast(e.message, true); }
  };
  $('#ingestBtn').onclick = submitIngest;
  $('#parseBtn').onclick = async () => {
    const payload = $('#ingestPayload').value.trim();
    if (!payload) return toast('先粘内容', true);
    try {
      const out = await api('/api/parse', { method: 'POST', body: { payload, engine: 'baidu' } });
      S.lastParse = out;
      $('#parseResult').innerHTML = `<div class="small">解析方式 <b>${esc(out.kind)}</b>：候选 ${out.candidates.length} 条（原始 ${out.raw_count}）</div>` +
        out.candidates.slice(0, 12).map((c) => `<div class="small mono">${esc(c.image_url)}<br><span style="color:var(--fg-mute)">↳ ${esc(c.source_url)} · ${esc(c.platform || '-')}</span></div>`).join('');
      toast(`解析出 ${out.candidates.length} 条候选（未写库）`);
    } catch (e) { toast(e.message, true); }
  };
  $('#queueClearBtn').onclick = async () => {
    if (!confirm('清空所有「待搜索」队列项？')) return;
    const out = await api('/api/queue/clear', { method: 'POST', body: { statuses: ['pending'] } });
    toast(`已清理 ${out.removed} 项`);
    await loadQueue();
  };
}

// -------------------------------------------------------------------- 结果板
async function loadBoard() {
  const q = new URLSearchParams({
    triage: S.triage, order: S.order, limit: '300', dups: S.includeDups ? '1' : '0',
  });
  const data = await api(`/api/findings?${q.toString()}`);
  S.board = data;
  const list = data.findings || [];
  $('#boardCount').textContent = `${list.length} 个视觉对象${S.includeDups ? '（含重复）' : ''}`;
  const triageCounts = { tracking: 0, diffusion: 0, bridge: 0 };
  list.forEach((f) => { if (triageCounts[f.triage] !== undefined) triageCounts[f.triage] += 1; });
  $('#triageCounts').innerHTML = Object.keys(TRIAGE_LABEL)
    .map((k) => `<span class="pill">${TRIAGE_LABEL[k]} ${triageCounts[k]}</span>`).join('');

  $('#board').innerHTML = list.map((f) => {
    const bars = [
      ['相似', f.similarity, ''], ['新颖', f.novelty, 'b2'], ['桥接', f.bridging, 'b3'], ['元数据', f.metadata_score, 'b4'],
    ].map(([label, value, cls]) => `
      <div class="bar ${cls}"><span>${label}</span><i><b style="width:${pct(value)}"></b></i><span class="mono">${num(value)}</span></div>`).join('');
    const chosen = S.selectedFindings.has(f.id);
    return `
    <div class="fcard ${chosen ? 'sel' : ''}" data-id="${f.id}">
      <div class="pic">
        <img loading="lazy" src="${media(f.thumb_path) || media(f.rel_path)}" alt="">
        <span class="val">E ${num(f.explore_value)}</span>
        <span class="dups">${f.sources.length} 源${f.dup_of ? ' · 重复' : ''}</span>
        ${f.status === 'promoted' ? '<span class="promoted" title="已选为下一轮种子">已回流</span>' : ''}
        ${f.archived_at ? '<span class="archived" title="已入库归档">已归档</span>' : ''}
      </div>
      <div class="meta">
        <div class="title">${esc(f.title || f.image_url || `结果 #${f.id}`)}</div>
        <div class="bars">${bars}</div>
        <div class="sources">${esc((f.sources[0] || {}).platform || '-')} · ${esc((f.tags || []).map((t) => t.name).slice(0, 3).join(' / ') || '')}</div>
        <div class="row tight">
          <span class="badge ${f.triage}">${TRIAGE_LABEL[f.triage] || f.triage}</span>
          <span class="badge">${esc(f.status)}</span>
        </div>
      </div>
      <div class="foot">
        <button class="btn" data-act="detail" data-id="${f.id}">详情/回放</button>
        <button class="btn ${chosen ? 'primary' : ''}" data-act="sel" data-id="${f.id}">${chosen ? '已选' : '选为种子'}</button>
        <button class="btn danger" data-act="trash" data-id="${f.id}" title="移进回收站（可恢复）">回收站</button>
      </div>
    </div>`;
  }).join('') || '<div class="empty">还没有结果。到「批处理队列」打开识图，然后把结果页内容回填进来。</div>';

  $$('#board .fcard').forEach((card) => {
    card.onclick = (ev) => {
      const id = Number(card.dataset.id);
      if (ev.target.dataset.act === 'sel' || ev.target.dataset.act === 'trash') return;
      openDetail(id);
    };
  });
  $$('#board [data-act="sel"]').forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      const id = Number(btn.dataset.id);
      const wasSelected = S.selectedFindings.has(id);
      if (wasSelected) S.selectedFindings.delete(id);
      else S.selectedFindings.add(id);
      card_selection_changed();
      const card = btn.closest('.fcard');
      if (card) card.classList.toggle('sel', !wasSelected);
      btn.classList.toggle('primary', !wasSelected);
      btn.textContent = wasSelected ? '选为种子' : '已选';
    };
  });
  $$('#board [data-act="trash"]').forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      const id = Number(btn.dataset.id);
      try {
        await api('/api/findings/trash', { method: 'POST', body: { finding_ids: [id] } });
        S.selectedFindings.delete(id);
        card_selection_changed();
        toast(`结果 #${id} 已移进回收站（可在「来源与聚类」页恢复）`);
        await loadBoard();
        await refreshHealth();
      } catch (e) { toast(e.message, true); }
    };
  });
}

function card_selection_changed() {
  const count = S.selectedFindings.size;
  $('#promoteCount').textContent = `已选 ${count} 个结果`;
  const button = $('#archiveBtn');
  if (button) {
    button.textContent = count ? `归档选中 ${count} 个` : '入库归档';
    button.title = count
      ? `只把选中的 ${count} 个对象入库归档，其余留在结果板`
      : '把当前这批结果入库归档，并从活动结果板上收起来（可恢复）';
  }
  const trashButton = $('#trashSelectedBtn');
  if (trashButton) trashButton.textContent = count ? `选中 ${count} 个 → 回收站` : '选中 → 回收站';
}

function bindBoardPanel() {
  $$('#triageTabs .tab').forEach((tab) => {
    tab.onclick = () => {
      $$('#triageTabs .tab').forEach((t) => t.classList.toggle('active', t === tab));
      S.triage = tab.dataset.triage;
      loadBoard();
    };
  });
  $('#orderSel').onchange = () => { S.order = $('#orderSel').value; loadBoard(); };
  $('#dupsToggle').onchange = () => { S.includeDups = $('#dupsToggle').checked; loadBoard(); };
  $('#promoteBtn').onclick = async () => {
    if (!S.selectedFindings.size) return toast('先选几个结果', true);
    try {
      toast('回流为新种子并入队…');
      const out = await api('/api/findings/promote', {
        method: 'POST',
        body: { finding_ids: Array.from(S.selectedFindings), engines: pickedEngines('#promoteEngines'), kinds: null },
      });
      toast(`已回流 ${out.promoted} 个结果，自动排入下一轮搜索队列`);
      S.selectedFindings.clear();
      await loadBoard(); await loadSeeds(); await loadQueue(); await refreshHealth();
    } catch (e) { toast(e.message, true); }
  };
  $('#rescoreBtn').onclick = async () => {
    const out = await api('/api/rescore', { method: 'POST', body: {} });
    toast(`按当前权重重算 ${out.updated} 个结果的探索价值`);
    await loadBoard();
  };
  $('#trashSelectedBtn').onclick = async () => {
    if (!S.selectedFindings.size) return toast('先点几张卡片的「选为种子」——选中的就是会被清理的', true);
    const ids = Array.from(S.selectedFindings);
    if (!confirm(`把选中的 ${ids.length} 个结果移进回收站？（可恢复，不会删文件）`)) return;
    try {
      const out = await api('/api/findings/trash', {
        method: 'POST', body: { finding_ids: ids, only_unpromoted: true },
      });
      S.selectedFindings.clear();
      card_selection_changed();
      const blocked = (out.skipped || []).filter((s) => s.why.includes('已保护')).length;
      toast(`已移进回收站 ${out.moved.length} 个${blocked ? `（${blocked} 个已回流，受保护跳过）` : ''}`);
      await loadBoard();
      await refreshHealth();
    } catch (e) { toast(e.message, true); }
  };
  $('#trashUnpromotedBtn').onclick = async () => {
    if (!confirm('把所有「还没被选为下一轮种子」的结果移进回收站？\n已回流的成果不会动，回收站里可以随时全部恢复。')) return;
    try {
      toast('清理中…');
      const out = await api('/api/findings/trash', { method: 'POST', body: { sweep: true } });
      toast(`已移进回收站 ${out.moved.length} 个（已回流的成果受保护，未动）`);
      await loadBoard();
      await refreshHealth();
    } catch (e) { toast(e.message, true); }
  };
  $('#archiveBtn').onclick = async () => {
    const ids = Array.from(S.selectedFindings);
    try {
      if (ids.length) {
        // 选了就只归档选中的那几个（其余留在板上继续查）
        const preview = await api(`/api/archives/preview?ids=${ids.join(',')}`);
        if (!preview.findings) return toast('选中的对象没有可归档的内容', true);
        if (!confirm(`只归档选中的 ${preview.findings} 个对象（${preview.sources} 条来源）？\n` +
                     `其余 ${Math.max(0, S.board.findings.length - preview.findings)} 个留在结果板；` +
                     `已归档的可以随时取消归档。`)) return;
        const out = await api('/api/archives', {
          method: 'POST',
          body: {
            finding_ids: ids,
            name: $('#archiveName').value.trim() || `选中 ${preview.findings} 个 · ${new Date().toLocaleString('zh-CN')}`,
            note: $('#archiveNote').value.trim(),
          },
        });
        S.selectedFindings.clear();
        card_selection_changed();
        $('#archiveName').value = '';
        $('#archiveNote').value = '';
        toast(`已归档选中的 ${out.archived} 个对象（批次 #${out.archive.id}）`);
        await loadBoard();
        await loadArchives();
        await refreshHealth();
        return;
      }
      document.querySelector('.tab[data-view="sources"]').click();
      setTimeout(() => $('#archiveNowBtn').focus(), 60);
      toast('没有选中任何图片，将按「整批归档」处理：在「入库归档」里确认后点「归档当前批次」');
    } catch (e) { toast(e.message, true); }
  };
}

// ------------------------------------------------------------------ 框选搜索
/*
 * 框选模块（交互参考 Google Lens）：在种子上拖一个矩形，只拿那一块去检索。
 * 选区存的是**归一化坐标**（0..1），所以前端不需要知道原图尺寸，后端也不必反算。
 * 每次框选都落成一个单独的 crop 变体（带矩形坐标），于是它和自动生成的 16 种变体走同一条
 * 队列 → 去重 → 打分 → 回放链路；「按变体看产出」里就能看出哪一种框法真的有效。
 */
const CROP = {
  seedId: 0,
  img: null,
  sel: null,            // {x, y, w, h} 归一化
  drag: null,
  ratio: 0,             // 0 = 自由
  padding: 0,
  scale: 2,
  vw: 0, vh: 0,         // 图片当前的显示尺寸
};

function openCropDrawer(seedId) {
  CROP.seedId = seedId;
  CROP.sel = null;
  CROP.drag = null;
  CROP.ratio = 0;
  CROP.padding = 0;
  CROP.scale = $('#cropUpscale').checked ? 2 : 1;
  $('#cropScrim').classList.add('open');
  $('#cropDrawer').classList.add('open');
  $('#cropBoxInfo').textContent = '未框选';
  $('#cropState').textContent = '';
  $$('#cropRatios .chip').forEach((c) => c.classList.toggle('on', c.dataset.ratio === 'free'));
  $$('#cropPadding .chip').forEach((c) => c.classList.toggle('on', c.dataset.pad === '0'));
  renderEnginePicker('#cropEngines', new Set(['baidu']));
  loadCropSeed(seedId);
}

function closeCropDrawer() {
  $('#cropScrim').classList.remove('open');
  $('#cropDrawer').classList.remove('open');
}

async function loadCropSeed(seedId) {
  try {
    const info = await api(`/api/seed/${seedId}/image`);
    $('#cropSeedTitle').textContent =
      `种子 #${seedId} · 原图 ${info.width}×${info.height} —— 拖出要检索的范围（默认已给一个居中选区）。`;
    const img = $('#cropImg');
    img.onload = () => {
      CROP.img = img;
      CROP.vw = img.clientWidth;
      CROP.vh = img.clientHeight;
      if (!CROP.sel) CROP.sel = defaultCropBox();
      syncCropOverlay();
    };
    img.src = info.url;
    renderCropHistory(info.variants || [], seedId);
  } catch (e) { toast(e.message, true); }
}

/** Google Lens 那样：进面板就给一个居中、占 60% 的选区，少一步操作。 */
function defaultCropBox() {
  return { x: 0.2, y: 0.2, w: 0.6, h: 0.6 };
}

function syncCropOverlay() {
  const stage = $('#cropStage');
  const img = $('#cropImg');
  const sel = CROP.sel;
  const box = $('#cropSelection');
  if (!sel || !img || !img.clientWidth) {
    box.hidden = true;
    $('#cropHole').setAttribute('width', 0);
    $('#cropHole').setAttribute('height', 0);
    if (!sel) $('#cropBoxInfo').textContent = '未框选';
    return;
  }
  const sx = img.offsetLeft, sy = img.offsetTop;
  const w = CROP.vw || img.clientWidth, h = CROP.vh || img.clientHeight;
  const left = sx + sel.x * w, top = sy + sel.y * h;
  const width = sel.w * w, height = sel.h * h;
  box.hidden = false;
  box.style.left = `${left}px`;
  box.style.top = `${top}px`;
  box.style.width = `${width}px`;
  box.style.height = `${height}px`;
  const hole = $('#cropHole');
  hole.setAttribute('x', left);
  hole.setAttribute('y', top);
  hole.setAttribute('width', width);
  hole.setAttribute('height', height);
  // 外扩预览（虚线矩形）——让用户看到实际会送进引擎的范围
  const padEl = $('#cropPadPreview');
  if (CROP.padding) {
    const px = width * CROP.padding, py = height * CROP.padding;
    if (!padEl) {
      const span = document.createElement('span');
      span.id = 'cropPadPreview';
      span.style.cssText = 'position:absolute;border:1.5px dashed rgba(255,180,84,.95);' +
        'border-radius:3px;pointer-events:none';
      box.appendChild(span);
    }
    const el = $('#cropPadPreview');
    el.style.left = `${-px}px`;
    el.style.top = `${-py}px`;
    el.style.width = `${width + px * 2}px`;
    el.style.height = `${height + py * 2}px`;
  } else if (padEl) {
    padEl.remove();
  }
  const px = Math.round(sel.w * (CROP.img.naturalWidth || w));
  const py = Math.round(sel.h * (CROP.img.naturalHeight || h));
  const area = (sel.w * sel.h * 100).toFixed(1);
  const tag = `${px}×${py} · 占 ${area}%${CROP.padding ? ` · 外扩 ${Math.round(CROP.padding * 100)}%` : ''}`;
  $('#cropSizeTag').textContent = tag;
  $('#cropBoxInfo').textContent = tag;
}

function cropPointFromEvent(ev) {
  const img = $('#cropImg');
  const rect = img.getBoundingClientRect();
  return {
    x: Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width)),
    y: Math.min(1, Math.max(0, (ev.clientY - rect.top) / rect.height)),
  };
}

/** Hit-test against the *rendered* selection: returns n/s/e/w combos, 'move', or ''. */
function cropHandleAt(ev) {
  const sel = CROP.sel;
  if (!sel) return '';
  const img = $('#cropImg');
  const rect = img.getBoundingClientRect();
  const x1 = rect.left + sel.x * rect.width;
  const y1 = rect.top + sel.y * rect.height;
  const x2 = x1 + sel.w * rect.width;
  const y2 = y1 + sel.h * rect.height;
  const tol = 14;
  const nearW = Math.abs(ev.clientX - x1) <= tol;
  const nearE = Math.abs(ev.clientX - x2) <= tol;
  const nearN = Math.abs(ev.clientY - y1) <= tol;
  const nearS = Math.abs(ev.clientY - y2) <= tol;
  const inX = ev.clientX >= x1 - tol && ev.clientX <= x2 + tol;
  const inY = ev.clientY >= y1 - tol && ev.clientY <= y2 + tol;
  if (!inX || !inY) return '';
  let handle = '';
  if (nearN) handle += 'n';
  if (nearS) handle += 's';
  if (nearW) handle += 'w';
  if (nearE) handle += 'e';
  if (handle) return handle;
  return (ev.clientX > x1 && ev.clientX < x2 && ev.clientY > y1 && ev.clientY < y2) ? 'move' : '';
}

function applyCropRatio(sel, anchor) {
  if (!CROP.ratio) return sel;
  const img = $('#cropImg');
  const rect = img.getBoundingClientRect();
  const ratioPx = (rect.width / Math.max(1, rect.height)) / CROP.ratio; // w:h on screen
  let w = sel.w, h = sel.h;
  if (w / h > ratioPx) h = w / ratioPx; else w = h * ratioPx;
  if (w > 1) { w = 1; h = w / ratioPx; }
  if (h > 1) { h = 1; w = h * ratioPx; }
  const out = { x: sel.x, y: sel.y, w, h };
  if (anchor === 'w') out.x = sel.x + (sel.w - w);
  if (anchor === 'n') out.y = sel.y + (sel.h - h);
  out.x = Math.min(1 - out.w, Math.max(0, out.x));
  out.y = Math.min(1 - out.h, Math.max(0, out.y));
  return out;
}

function drawMagnifier(ev) {
  const mag = $('#cropMagnifier');
  const canvas = $('#cropMagnifierCanvas');
  const img = CROP.img;
  if (!img || !canvas) return;
  const ctx = canvas.getContext('2d');
  const stageRect = $('#cropStage').getBoundingClientRect();
  const point = cropPointFromEvent(ev);
  const sw = 44, sh = 44;                       // sampled source size in image pixels
  const sx = point.x * img.naturalWidth - sw / 2;
  const sy = point.y * img.naturalHeight - sh / 2;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
  mag.hidden = false;
  const size = 108;
  let left = ev.clientX - stageRect.left + 26;
  let top = ev.clientY - stageRect.top - size - 18;
  left = Math.min(stageRect.width - size - 6, Math.max(6, left));
  top = Math.min(stageRect.height - size - 6, Math.max(6, top));
  mag.style.left = `${left}px`;
  mag.style.top = `${top}px`;
}

function bindCropPanel() {
  const stage = $('#cropStage');
  stage.classList.add('selecting');
  $('#cropClose').onclick = closeCropDrawer;
  $('#cropScrim').onclick = closeCropDrawer;
  $('#cropClearBtn').onclick = () => { CROP.sel = null; syncCropOverlay(); };
  $('#cropCenterBtn').onclick = () => { CROP.sel = defaultCropBox(); syncCropOverlay(); };

  $$('#cropRatios .chip').forEach((chip) => {
    chip.onclick = () => {
      $$('#cropRatios .chip').forEach((c) => c.classList.toggle('on', c === chip));
      CROP.ratio = chip.dataset.ratio === 'free' ? 0 : Number(chip.dataset.ratio);
      if (CROP.sel) { CROP.sel = applyCropRatio(CROP.sel); syncCropOverlay(); }
    };
  });
  $$('#cropPadding .chip').forEach((chip) => {
    chip.onclick = () => {
      $$('#cropPadding .chip').forEach((c) => c.classList.toggle('on', c === chip));
      CROP.padding = Number(chip.dataset.pad) || 0;
      syncCropOverlay();
    };
  });
  $('#cropUpscale').onchange = (ev) => { CROP.scale = ev.target.checked ? 2 : 1; };

  stage.addEventListener('pointerdown', (ev) => {
    if (!CROP.img || ev.target.closest('.lens-bar')) return;
    const handle = cropHandleAt(ev);
    const point = cropPointFromEvent(ev);
    stage.setPointerCapture(ev.pointerId);
    if (!handle) {
      // 空白处按下 = 重新画一个选区（Lens 也是这个手感）
      CROP.sel = { x: point.x, y: point.y, w: 0.001, h: 0.001 };
      CROP.drag = { mode: 'new', start: point };
    } else {
      CROP.drag = { mode: handle, start: point, origin: { ...CROP.sel },
                    anchor: handle.includes('w') ? 'w' : (handle.includes('n') ? 'n' : '') };
    }
    syncCropOverlay();
    ev.preventDefault();
  });

  stage.addEventListener('pointermove', (ev) => {
    if (!CROP.img) return;
    if (!CROP.drag) {
      const handle = cropHandleAt(ev);
      stage.style.cursor = handle === 'move' ? 'move' : (handle ? `${handle}-resize` : 'crosshair');
      return;
    }
    const drag = CROP.drag;
    const point = cropPointFromEvent(ev);
    if (drag.mode === 'new') {
      CROP.sel = { x: Math.min(drag.start.x, point.x), y: Math.min(drag.start.y, point.y),
                   w: Math.abs(point.x - drag.start.x), h: Math.abs(point.y - drag.start.y) };
    } else if (drag.mode === 'move') {
      const dx = point.x - drag.start.x, dy = point.y - drag.start.y;
      CROP.sel = { x: Math.min(1 - drag.origin.w, Math.max(0, drag.origin.x + dx)),
                   y: Math.min(1 - drag.origin.h, Math.max(0, drag.origin.y + dy)),
                   w: drag.origin.w, h: drag.origin.h };
    } else {
      const o = drag.origin;
      let x1 = o.x, y1 = o.y, x2 = o.x + o.w, y2 = o.y + o.h;
      if (drag.mode.includes('w')) x1 = point.x;
      if (drag.mode.includes('e')) x2 = point.x;
      if (drag.mode.includes('n')) y1 = point.y;
      if (drag.mode.includes('s')) y2 = point.y;
      CROP.sel = { x: Math.min(x1, x2), y: Math.min(y1, y2),
                   w: Math.abs(x2 - x1), h: Math.abs(y2 - y1) };
    }
    CROP.sel = applyCropRatio(CROP.sel, drag.anchor);
    syncCropOverlay();
    if (drag.mode !== 'new' && drag.mode !== 'move') drawMagnifier(ev);
    ev.preventDefault();
  });

  const endDrag = (ev) => {
    if (!CROP.drag) return;
    CROP.drag = null;
    $('#cropMagnifier').hidden = true;
    try { stage.releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
    if (CROP.sel && (CROP.sel.w < 0.02 || CROP.sel.h < 0.02)) {
      CROP.sel = null;
      $('#cropBoxInfo').textContent = '未框选（选区太小已丢弃）';
    }
    syncCropOverlay();
  };
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);
  stage.addEventListener('dblclick', () => { CROP.sel = null; syncCropOverlay(); });
  window.addEventListener('resize', () => {
    if ($('#cropDrawer').classList.contains('open')) {
      CROP.vw = $('#cropImg').clientWidth;
      CROP.vh = $('#cropImg').clientHeight;
      syncCropOverlay();
    }
  });

  $('#cropQueueBtn').onclick = () => runCrop(false);
  $('#cropRunBtn').onclick = () => runCrop(true);
}

async function runCrop(autoRun) {
  if (!CROP.sel || CROP.sel.w <= 0 || CROP.sel.h <= 0) return toast('先在图上框一块区域', true);
  const engines = pickedEngines('#cropEngines');
  if (!engines.length) return toast('先在下面选一个引擎', true);
  if (autoRun && engines.some((e) => !(S.engines.find((x) => x.key === e) || {}).browser)) {
    const names = engines.filter((e) => !(S.engines.find((x) => x.key === e) || {}).browser).join('、');
    return toast(`${names} 不支持自动识图，请用「只加入队列」再手动打开`, true);
  }
  $('#cropState').textContent = '正在生成变体…';
  try {
    const out = await api(`/api/seed/${CROP.seedId}/crop`, {
      method: 'POST',
      body: {
        box: [CROP.sel.x, CROP.sel.y, CROP.sel.w, CROP.sel.h],
        padding: CROP.padding,
        scale: CROP.scale,
        engines,
        auto_run: autoRun,
        label: `框选 ${Math.round(CROP.sel.w * 100)}%×${Math.round(CROP.sel.h * 100)}%`,
      },
    });
    const v = out.variant || {};
    $('#cropState').textContent = `已建变体 #${v.id}（${v.width}×${v.height}）`;
    toast(autoRun ? '已开始框选自动识图，跑完会自动刷新结果板'
                  : `已加入队列：${(out.job_ids || []).length} 项（变体 #${v.id}）`);
    if (out.auto && out.auto.started) pollBrowserState();
    await loadCropSeed(CROP.seedId);
    await loadSeeds();
    await loadQueue();
  } catch (e) {
    $('#cropState').textContent = '';
    toast(e.message, true);
  }
}

function renderCropHistory(variants, seedId) {
  const root = $('#cropHistory');
  if (!root) return;
  root.innerHTML = (variants || []).map((v) => {
    const crop = (v.meta || {}).crop || {};
    return `<div class="item" data-variant="${v.id}">
      <img src="${media(v.rel_path)}" alt="">
      <div class="cap"><b>#${v.id}</b> ${esc(v.label || '框选')}<br>
        ${crop.w ? `${crop.w}×${crop.h} 原图坐标 · ` : ''}${v.width}×${v.height}<br>
        <span style="color:var(--fg-mute)">${esc((v.note || '').slice(0, 40))}</span></div>
    </div>`;
  }).join('') || '<div class="empty">这个种子还没有手工框选</div>';
  $$('#cropHistory .item').forEach((el) => {
    el.onclick = () => {
      const variantId = Number(el.dataset.variant);
      const variant = (variants || []).find((v) => v.id === variantId);
      if (variant && variant.meta && variant.meta.normalised) {
        const n = variant.meta.normalised;
        CROP.sel = { x: n.x, y: n.y, w: n.w, h: n.h };
        CROP.padding = Number(variant.meta.padding) || 0;
        $$('#cropPadding .chip').forEach((c) => c.classList.toggle('on',
          Number(c.dataset.pad) === CROP.padding));
        syncCropOverlay();
        toast(`已载入变体 #${variantId} 的框选，可继续微调`);
      }
    };
  });
}
// ------------------------------------------------------------------ 详情抽屉
async function openDetail(id) {
  const data = await api(`/api/finding/${id}`);
  const f = data.finding;
  const chain = data.chain || { nodes: [], edges: [] };
  const chainHtml = chain.nodes.map((n, i) => `
    ${i ? '<div class="arrow">↓</div>' : ''}
    <div class="node">
      <span class="type">${{ seed: '种子图', variant: '检索变体', finding: '结果图', source: '来源页' }[n.type] || n.type}</span>
      ${n.thumb ? `<img class="thumb" style="width:34px;height:34px" src="${media(n.thumb)}">` : ''}
      <span>${esc(n.title || '')}</span>
      ${n.url ? `<a class="small" href="${esc(n.url)}" target="_blank" rel="noopener">打开</a>` : ''}
      ${n.platform ? `<span class="small">${esc(n.platform)}</span>` : ''}
    </div>`).join('');

  $('#drawerBody').innerHTML = `
    <div class="row" style="align-items:flex-start;gap:14px">
      <img src="${media(f.thumb_path) || media(f.rel_path)}" style="width:220px;border-radius:8px;border:1px solid var(--line)">
      <div style="flex:1">
        <div class="row tight" style="margin-bottom:8px">
          <span class="badge ${f.triage}">${TRIAGE_LABEL[f.triage] || f.triage}</span>
          <span class="badge">${esc(f.status)}</span>
          <span class="pill">E ${num(f.explore_value)}</span>
        </div>
        <div class="small" style="margin-bottom:8px">${esc(TRIAGE_NOTE[f.triage] || '')}</div>
        <dl class="kv">
          <dt>标题</dt><dd>${esc(f.title || '-')}</dd>
          <dt>来源页</dt><dd><a href="${esc(f.source_url)}" target="_blank" rel="noopener">${esc(f.source_url)}</a></dd>
          <dt>图片直链</dt><dd><a href="${esc(f.image_url)}" target="_blank" rel="noopener">${esc(f.image_url)}</a></dd>
          <dt>引擎</dt><dd>${esc(f.engine)} · 队列项 #${f.job_id} · 变体 #${f.variant_id}</dd>
          <dt>尺寸</dt><dd>${f.width}×${f.height} · ${(f.bytes / 1024).toFixed(1)} KB</dd>
          <dt>指纹</dt><dd class="mono">sha ${esc((f.sha256 || '').slice(0, 16))}… · phash ${f.phash}</dd>
          <dt>评分</dt><dd>相似 ${num(f.similarity)} · 新颖 ${num(f.novelty)} · 来源潜力 ${num(f.potential)} · 桥接 ${num(f.bridging)} · 元数据 ${num(f.metadata_score)}</dd>
          <dt>标签</dt><dd>${esc((f.tags || []).map((t) => t.name).join(' / ') || '-')}</dd>
          <dt>主色</dt><dd>${(f.palette || []).map((p) => `<span style="display:inline-block;width:14px;height:14px;border-radius:3px;background:${p.hex};vertical-align:-2px;margin-right:2px"></span>`).join('')}</dd>
        </dl>
        <div class="row" style="margin-top:10px">
          <button class="btn primary" id="drawerPromote">选为下一轮种子</button>
          <button class="btn" id="drawerStatus" data-status="${f.status === 'kept' ? 'new' : 'kept'}">
            ${f.status === 'kept' ? '取消标记' : '标记为有用'}
          </button>
          <button class="btn danger" id="drawerTrash">移进回收站</button>
          <button class="btn danger" id="drawerDelete" title="跳过回收站，直接删库与文件">彻底删除</button>
        </div>
      </div>
    </div>

    <h3>来源聚合（${data.sources.length} 个网页 / 文件）</h3>
    <div class="srclist">
      ${data.sources.map((s) => `
        <div class="src">
          <div class="row tight">
            <span class="trust ${esc(s.trust)}">${esc(s.trust)}</span>
            <span>${esc(s.platform || s.host || '')}</span>
            <span class="small">${esc(s.date_hint || '')}</span>
            <a class="small" href="${esc(s.url)}" target="_blank" rel="noopener">打开</a>
          </div>
          <div>${esc(s.title || '')}</div>
          <div class="u">${esc(s.url)}</div>
        </div>`).join('') || '<div class="empty">没有来源记录</div>'}
    </div>

    <h3>搜索链回放</h3>
    <div class="chain">${chainHtml || '<div class="empty">无链路</div>'}</div>

    <h3>语义邻居（图 → 图）</h3>
    <div class="board">
      ${(data.similar || []).map((s) => `
        <div class="fcard" data-goto="${s.id}">
          <div class="pic"><img loading="lazy" src="${media(s.thumb_path)}"><span class="val">${num(s.query_similarity)}</span></div>
          <div class="meta"><div class="title">${esc(s.title || s.image_url)}</div>
            <div class="sources">${esc((s.sources[0] || {}).platform || '')}</div></div>
        </div>`).join('') || '<div class="empty">还没有语义邻居（先跑一次语义索引补算）</div>'}
    </div>
    ${data.cluster ? `<h3>同簇</h3><div class="small">簇 #${data.cluster.id} · ${esc(data.cluster.label || '')} · 共 ${data.cluster.size} 张 · 平台 ${esc((data.cluster.platforms || []).join(' / ') || '-')}</div>` : ''}
  `;
  $('#drawer').classList.add('open');
  $('#scrim').classList.add('open');
  $$('#drawerBody [data-goto]').forEach((card) => { card.onclick = () => openDetail(Number(card.dataset.goto)); });
  $('#drawerPromote').onclick = async () => {
    try {
      const out = await api('/api/findings/promote', {
        method: 'POST', body: { finding_ids: [id], engines: pickedEngines('#promoteEngines'), kinds: null },
      });
      toast(out.promoted ? '已回流为新种子并入队' : '回流失败（图取不到？）', !out.promoted);
      await loadSeeds(); await loadQueue();
    } catch (e) { toast(e.message, true); }
  };
  $('#drawerStatus').onclick = async (ev) => {
    const next = ev.target.dataset.status;
    await api(`/api/finding/${id}`, { method: 'POST', body: { status: next } });
    toast(next === 'kept' ? '已标记为有用' : '已取消标记');
    await loadBoard();
    openDetail(id);
  };
  $('#drawerTrash').onclick = async () => {
    await api('/api/findings/trash', { method: 'POST', body: { finding_ids: [id] } });
    closeDrawer();
    toast(`结果 #${id} 已移进回收站（可恢复）`);
    await loadBoard(); await refreshHealth();
  };
  $('#drawerDelete').onclick = async () => {
    if (!confirm(`彻底删除结果 #${id}？图片文件也会删掉，不可恢复。\n（只想先收起来的话请用「移进回收站」）`)) return;
    await api(`/api/finding/${id}`, { method: 'DELETE' });
    closeDrawer();
    await loadBoard(); await refreshHealth();
  };
}

function closeDrawer() {
  $('#drawer').classList.remove('open');
  $('#scrim').classList.remove('open');
}

// ------------------------------------------------------------------ 语料检索
function bindSearchPanel() {
  const run = async () => {
    const query = $('#semQuery').value.trim();
    if (!query) return;
    $('#semNote').textContent = 'SigLIP2 文本塔推理中…';
    try {
      const out = await api('/api/search/semantic', { method: 'POST', body: { query, limit: 60 } });
      $('#semNote').textContent = `命中 ${out.results.length} 条${out.note ? ' · ' + out.note : ''}（全程本机，0 成本）`;
      $('#semResults').innerHTML = out.results.map((f) => `
        <div class="fcard" data-goto="${f.id}">
          <div class="pic"><img loading="lazy" src="${media(f.thumb_path) || media(f.rel_path)}">
            <span class="val">${num(f.query_similarity)}</span></div>
          <div class="meta">
            <div class="title">${esc(f.title || f.image_url)}</div>
            <div class="sources">${esc((f.sources[0] || {}).platform || '')} · ${esc((f.tags || []).map((t) => t.name).slice(0, 2).join(' / '))}</div>
          </div>
        </div>`).join('') || '<div class="empty">没有命中</div>';
      $$('#semResults [data-goto]').forEach((c) => { c.onclick = () => openDetail(Number(c.dataset.goto)); });
    } catch (e) {
      $('#semNote').textContent = '';
      toast(e.message, true);
    }
  };
  $('#semBtn').onclick = run;
  $('#semQuery').onkeydown = (ev) => { if (ev.key === 'Enter') run(); };

  $('#reindexBtn').onclick = async () => {
    const out = await api('/api/reindex', { method: 'POST', body: { limit: 500 } });
    toast(`语义索引补算启动：${out.total} 条待处理`);
    const timer = setInterval(async () => {
      const st = await api('/api/reindex');
      $('#reindexNote').textContent = st.running ? `补算中 ${st.done}/${st.total}（${st.note}）` : `空闲（${st.note || '—'}）`;
      if (!st.running) { clearInterval(timer); await loadBoard(); await refreshHealth(); }
    }, 1500);
  };
}

// ---------------------------------------------------------------- 来源 / 图谱
async function loadSources() {
  const clusters = await api('/api/clusters');
  $('#clusters').innerHTML = (clusters.clusters || []).map((c) => `
    <div class="card">
      <div class="row tight"><b>簇 #${c.id}</b><span class="small">${esc(c.label || '')}</span></div>
      <div class="small">${c.size} 张 · 平台 ${esc((c.platforms || []).join(' / ') || '-')}</div>
    </div>`).join('') || '<div class="empty">还没有聚类</div>';
  const report = await api('/api/report');
  $('#platformTable').innerHTML = (report.platforms || []).map((p) => `
    <tr><td>${esc(p.platform)}</td><td class="num">${p.n}</td></tr>`).join('');
  await loadTrash();
  await loadArchives();
}

// -------------------------------------------------------------------- 入库归档
const TRIAGE_SHORT = { tracking: '追踪', diffusion: '扩散', bridge: '桥接' };

function fmtBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n > 1024 * 1024 * 1024) return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n > 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${n} B`;
}

async function loadArchives() {
  const data = await api('/api/archives');
  const selected = Array.from(S.selectedFindings);
  const preview = selected.length
    ? await api(`/api/archives/preview?ids=${selected.join(',')}`)
    : await api('/api/archives/preview');
  const metric = (label, value, note = '') =>
    `<div class="metric"><div class="n">${value}</div><div class="l">${label}</div>${note ? `<div class="small">${note}</div>` : ''}</div>`;
  const scoped = selected.length > 0;
  $('#archivePreview').innerHTML = [
    metric(scoped ? '已选中（将只归档这些）' : '当前活动结果（会全部归档）', preview.findings,
           scoped ? '其余留在结果板上' : ''),
    metric('来源记录', preview.sources),
    metric('平台数', preview.platform_count),
    metric('已选为种子的', preview.promoted, '归档不会动它们的状态'),
    metric('覆盖日期', `${preview.date_from || '-'} ~ ${preview.date_to || '-'}`),
    metric('图片体积', fmtBytes(preview.bytes)),
  ].join('') + [
    metric('已归档批次', data.total),
    metric('归档中的对象', data.current.findings, `体积 ${fmtBytes(data.current.bytes)}`),
  ].join('');
  $('#archiveCurrent').textContent = preview.findings
    ? (scoped ? `已选中 ${preview.findings} 个 · 只归档这些` : `${preview.findings} 个对象待归档（整批）`)
    : (scoped ? '选中的对象没有可归档的内容' : '活动结果板是空的（没有可归档的内容）');

  $('#archiveTable').innerHTML = (data.archives || []).map((a) => {
    const triage = a.triage || {};
    const dist = Object.keys(triage).length
      ? Object.entries(triage).map(([k, v]) => `${TRIAGE_SHORT[k] || k} ${v}`).join(' · ')
      : '-';
    return `<tr>
      <td><b>${esc(a.name)}</b>${a.note ? `<div class="small">${esc(a.note)}</div>` : ''}</td>
      <td class="small">${esc(new Date((a.created_at || 0) * 1000).toLocaleString('zh-CN'))}</td>
      <td class="num">${a.findings}</td>
      <td class="num">${a.sources}</td>
      <td class="num">${a.platforms}</td>
      <td class="small">${esc(dist)}</td>
      <td class="small">${esc(a.date_from || '-')} ~ ${esc(a.date_to || '-')}</td>
      <td class="num">${fmtBytes(a.total_bytes)}</td>
      <td class="nowrap">
        <button class="btn" data-act="look" data-id="${a.id}">查看</button>
        <button class="btn" data-act="undo" data-id="${a.id}">取消归档</button>
      </td>
    </tr>`;
  }).join('') || '<tr><td colspan="9" class="small">还没有归档批次</td></tr>';

  $$('#archiveTable [data-act]').forEach((btn) => {
    btn.onclick = async () => {
      const archiveId = Number(btn.dataset.id);
      try {
        if (btn.dataset.act === 'undo') {
          const out = await api('/api/archives/unarchive', {
            method: 'POST', body: { archive_id: archiveId },
          });
          toast(`已取消归档，${out.restored} 个对象回到结果板`);
          $('#archiveDetailCard').hidden = true;
        } else {
          const detail = await api(`/api/archive/${archiveId}?limit=200`);
          $('#archiveDetailTitle').textContent =
            `归档 #${archiveId} · ${detail.archive.name} · ${detail.count} 个对象`;
          $('#archiveBoard').innerHTML = (detail.findings || []).map((f) => `
            <div class="fcard" data-id="${f.id}">
              <div class="pic">
                <img loading="lazy" src="${media(f.thumb_path) || media(f.rel_path)}" alt="">
                <span class="val">E ${num(f.explore_value)}</span>
                <span class="dups">${f.dup_of ? '重复载体' : `${f.sources.length} 源`}</span>
              </div>
              <div class="meta">
                <div class="title">${esc(f.title || f.image_url || `结果 #${f.id}`)}</div>
                <div class="sources small">${esc((f.sources[0] || {}).platform || '-')} ·
                  ${esc(f.status)}</div>
              </div>
            </div>`).join('') || '<div class="empty">这个批次没有对象</div>';
          $('#archiveDetailCard').hidden = false;
          $$('#archiveBoard .fcard').forEach((card) => { card.onclick = () => openDetail(Number(card.dataset.id)); });
        }
        await loadArchives();
        await loadBoard();
        await refreshHealth();
      } catch (e) { toast(e.message, true); }
    };
  });
}

function bindArchivePanel() {
  $('#archiveDetailClose').onclick = () => { $('#archiveDetailCard').hidden = true; };
  $('#archiveNowBtn').onclick = async () => {
    try {
      const ids = Array.from(S.selectedFindings);
      const scoped = ids.length > 0;
      const preview = scoped
        ? await api(`/api/archives/preview?ids=${ids.join(',')}`)
        : await api('/api/archives/preview');
      if (!preview.findings) {
        return toast(scoped ? '选中的对象没有可归档的内容' : '活动结果板是空的，没有可归档的内容', true);
      }
      const what = scoped
        ? `只归档选中的 ${preview.findings} 个视觉对象（${preview.sources} 条来源）`
        : `把当前全部 ${preview.findings} 个视觉对象（${preview.sources} 条来源）入库归档`;
      if (!confirm(`${what}？\n会留一份快照记录并从活动结果板收起，随时可以取消归档。`)) return;
      const out = await api('/api/archives', {
        method: 'POST',
        body: {
          finding_ids: scoped ? ids : undefined,
          name: $('#archiveName').value.trim(),
          note: $('#archiveNote').value.trim(),
        },
      });
      if (scoped) { S.selectedFindings.clear(); card_selection_changed(); }
      $('#archiveName').value = '';
      $('#archiveNote').value = '';
      toast(`已归档 ${out.archived} 个对象（批次 #${out.archive.id}）`);
      await loadArchives();
      await loadBoard();
      await refreshHealth();
    } catch (e) { toast(e.message, true); }
  };
  $('#archiveRestoreAllBtn').onclick = async () => {
    const data = await api('/api/archives');
    if (!data.current.findings) return toast('当前没有归档中的对象');
    if (!confirm(`取消全部归档？${data.current.findings} 个对象会回到活动结果板。`)) return;
    const out = await api('/api/archives/unarchive', { method: 'POST', body: {} });
    toast(`已恢复 ${out.restored} 个对象`);
    $('#archiveDetailCard').hidden = true;
    await loadArchives();
    await loadBoard();
    await refreshHealth();
  };
  $('#repairLabelsBtn').onclick = async () => {
    try {
      const out = await api('/api/repair/source-labels', { method: 'POST', body: {} });
      toast(out.fixed ? `修好 ${out.fixed} 个对象的历史来源标注` : '没有需要修的历史标注');
      await loadBoard();
    } catch (e) { toast(e.message, true); }
  };
}

// -------------------------------------------------------------------- 回收站
async function loadTrash() {
  const data = await api('/api/findings/trash?limit=200');
  const items = data.findings || [];
  $('#trashCount').textContent = data.total
    ? `${data.total} 个已删除的结果（软删除，可恢复）${data.total > items.length ? `，下面显示最近 ${items.length} 个` : ''}`
    : '';
  $('#trashBoard').innerHTML = items.map((f) => `
    <div class="fcard" data-id="${f.id}">
      <div class="pic">
        <img loading="lazy" src="${media(f.thumb_path) || media(f.rel_path)}" alt="">
        <span class="val">E ${num(f.explore_value)}</span>
        <span class="dups">${f.dup_of ? '重复载体' : `${f.sources.length} 源`}</span>
      </div>
      <div class="meta">
        <div class="title">${esc(f.title || f.image_url || `结果 #${f.id}`)}</div>
        <div class="sources small">删除原因：${esc(f.trash_reason || '-')} ·
          ${esc((f.sources[0] || {}).platform || '-')}</div>
        <div class="row tight">
          <button class="btn" data-act="restore" data-id="${f.id}">恢复</button>
          <button class="btn danger" data-act="purge" data-id="${f.id}"
            title="连图片文件一起彻底删除，不可恢复">彻底删除</button>
        </div>
      </div>
    </div>`).join('') || '<div class="empty">回收站是空的</div>';

  $$('#trashBoard [data-act="restore"]').forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      await api('/api/findings/trash/restore', { method: 'POST', body: { finding_ids: [Number(btn.dataset.id)] } });
      toast('已恢复');
      await loadTrash(); await loadBoard(); await refreshHealth();
    };
  });
  $$('#trashBoard [data-act="purge"]').forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      const id = Number(btn.dataset.id);
      if (!confirm(`彻底删除结果 #${id}？图片文件也会被删掉，不可恢复。`)) return;
      const out = await api('/api/findings/trash/purge', {
        method: 'POST', body: { finding_ids: [id], confirm: true },
      });
      toast(`已彻底删除（清理文件 ${out.files_removed} 个）`);
      await loadTrash(); await refreshHealth();
    };
  });
}

function bindTrashPanel() {
  $('#trashRestoreAllBtn').onclick = async () => {
    if (!confirm('把回收站里的全部结果恢复？')) return;
    const out = await api('/api/findings/trash/restore', { method: 'POST', body: { all: true } });
    toast(`已恢复 ${(out.restored || []).length} 个`);
    await loadTrash(); await loadBoard(); await refreshHealth();
  };
  $('#trashPurgeAllBtn').onclick = async () => {
    const data = await api('/api/findings/trash');
    if (!data.count) return toast('回收站已经是空的');
    if (!confirm(`清空回收站：${data.count} 个结果及其图片文件将被永久删除，不可恢复。继续？`)) return;
    const out = await api('/api/findings/trash/purge', { method: 'POST', body: { all: true, confirm: true } });
    toast(`已清空：删除 ${(out.purged || []).length} 条记录、${out.files_removed} 个文件`);
    await loadTrash(); await refreshHealth();
  };
}

function drawGraph() {
  const canvas = $('#graph');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);

  const nodes = S.graph.nodes || [];
  if (!nodes.length) {
    ctx.fillStyle = '#6b7a8c';
    ctx.font = '13px "Segoe UI", sans-serif';
    ctx.fillText('暂无图谱数据：先导入种子并回填一次结果', 18, 30);
    return;
  }
  // layered layout: seed -> variant -> finding -> source
  const order = { seed: 0, variant: 1, finding: 2, source: 3 };
  const lanes = [[], [], [], []];
  nodes.forEach((n) => { const idx = order[n.type] ?? 3; lanes[idx].push(n); });
  const laneX = [90, 300, 560, 830];
  const positions = new Map();
  lanes.forEach((lane, li) => {
    const step = (rect.height - 60) / Math.max(1, lane.length);
    lane.forEach((n, i) => positions.set(n.id, { x: laneX[li], y: 40 + i * step + step / 2, node: n }));
  });

  const color = { seed: '#4ea1ff', variant: '#ffb454', finding: '#59d9a4', source: '#c58cff' };
  ctx.save();
  ctx.translate(S.graphView.x, S.graphView.y);
  ctx.scale(S.graphView.scale, S.graphView.scale);
  ctx.lineWidth = 0.7;
  (S.graph.edges || []).forEach((e) => {
    const a = positions.get(e.from); const b = positions.get(e.to);
    if (!a || !b) return;
    ctx.strokeStyle = 'rgba(120,140,165,.35)';
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.bezierCurveTo((a.x + b.x) / 2, a.y, (a.x + b.x) / 2, b.y, b.x, b.y);
    ctx.stroke();
  });
  positions.forEach(({ x, y, node }) => {
    ctx.beginPath();
    const r = node.type === 'finding' ? 6 : 4.5;
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = color[node.type] || '#888';
    ctx.fill();
    ctx.fillStyle = '#9aa8b8';
    ctx.font = '9px "Segoe UI", sans-serif';
    const label = String(node.label || '').slice(0, 22);
    ctx.fillText(label, x + 8, y + 3);
  });
  ctx.restore();
  $('#graphNote').textContent = `${nodes.length} 个节点 · ${(S.graph.edges || []).length} 条边（滚动缩放，拖动平移，点击节点打开详情）`;
}

function bindGraphPanel() {
  const canvas = $('#graph');
  $('#graphReload').onclick = async () => {
    S.graph = await api('/api/graph');
    drawGraph();
    toast(`图谱已刷新：${(S.graph.nodes || []).length} 个节点`);
  };
  canvas.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    S.graphView.scale = Math.min(3, Math.max(0.35, S.graphView.scale * (ev.deltaY < 0 ? 1.12 : 0.9)));
    drawGraph();
  }, { passive: false });
  canvas.addEventListener('mousedown', (ev) => {
    const rect = canvas.getBoundingClientRect();
    const hit = hitNode(ev.clientX - rect.left, ev.clientY - rect.top);
    if (hit) { S.graphView.pending = hit; }
    S.graphView.drag = { x: ev.clientX, y: ev.clientY, ox: S.graphView.x, oy: S.graphView.y };
  });
  canvas.addEventListener('mousemove', (ev) => {
    if (!S.graphView.drag) return;
    S.graphView.x = S.graphView.drag.ox + (ev.clientX - S.graphView.drag.x);
    S.graphView.y = S.graphView.drag.oy + (ev.clientY - S.graphView.drag.y);
    drawGraph();
  });
  window.addEventListener('mouseup', () => { S.graphView.drag = null; });
  canvas.addEventListener('click', (ev) => {
    const rect = canvas.getBoundingClientRect();
    const hit = hitNode(ev.clientX - rect.left, ev.clientY - rect.top);
    if (hit && hit.type === 'finding') openDetail(Number(String(hit.id).split(':')[1]));
  });
  canvas.addEventListener('dblclick', () => {
    S.graphView = { scale: 1, x: 0, y: 0, drag: null };
    drawGraph();
  });
  window.addEventListener('resize', () => { if ($('#view-graph').classList.contains('active')) drawGraph(); });
}

function hitNode(px, py) {
  // recompute positions the same way drawGraph does (cheap, small graphs)
  const canvas = $('#graph');
  const rect = canvas.getBoundingClientRect();
  const order = { seed: 0, variant: 1, finding: 2, source: 3 };
  const lanes = [[], [], [], []];
  (S.graph.nodes || []).forEach((n) => lanes[order[n.type] ?? 3].push(n));
  const laneX = [90, 300, 560, 830];
  let best = null;
  lanes.forEach((lane, li) => {
    const step = (rect.height - 60) / Math.max(1, lane.length);
    lane.forEach((n, i) => {
      const x = laneX[li] * S.graphView.scale + S.graphView.x;
      const y = (40 + i * step + step / 2) * S.graphView.scale + S.graphView.y;
      const d = Math.hypot(x - px, y - py);
      if (d < 12 && (!best || d < best.d)) best = { d, node: n };
    });
  });
  return best ? best.node : null;
}

// ------------------------------------------------------------------- 指标报告
async function loadReport() {
  const report = await api('/api/report');
  const st = report.stats || {};
  const metric = (label, value, note = '') =>
    `<div class="metric"><div class="n">${value}</div><div class="l">${label}</div>${note ? `<div class="small">${note}</div>` : ''}</div>`;
  $('#metricCards').innerHTML = [
    metric('每 100 次搜索操作 · 新增不重复图片', st.new_per_100),
    metric('每 100 次操作 · 新增独立来源', st.sources_per_100),
    metric('每 100 次操作 · 新平台', st.platforms_per_100),
    metric('平均多少次操作发现 1 个新来源', st.ops_per_new_source),
    metric('已折叠的重复转载数', st.duplicates, '这些不算新图'),
    metric('视觉对象总数', st.findings),
    metric('来源总数', st.sources),
    metric('平台数', st.platforms),
    metric('视觉簇数', st.clusters),
    metric('搜索操作总数', st.operations),
  ].join('');

  const ratio = st.findings ? (st.duplicates / (st.findings + st.duplicates) * 100).toFixed(1) : '0.0';
  $('#dupRatio').textContent = `重复转载占比 ${ratio}% —— 这些如果不去重，结果板会看起来像「发现了 ${st.findings + st.duplicates} 张新图」，实际只有 ${st.findings} 张。`;

  const rows = (arr, cols) => arr.map((r) => `<tr>${cols.map((c) => `<td class="${c.num ? 'num' : ''}">${esc(c.render ? c.render(r) : r[c.key])}</td>`).join('')}</tr>`).join('');
  $('#engineYield').innerHTML = rows(report.by_engine || [], [
    { key: 'engine' }, { key: 'ops', num: true }, { key: 'findings', num: true },
    { key: 'dups', num: true }, { key: 'sources', num: true },
  ]) || '<tr><td colspan="5" class="small">暂无数据</td></tr>';
  $('#variantYield').innerHTML = rows(report.variant_yield || [], [
    { render: (r) => r.label || r.kind }, { key: 'ops', num: true },
    { key: 'findings', num: true }, { key: 'dups', num: true },
  ]) || '<tr><td colspan="4" class="small">暂无数据</td></tr>';
  $('#dayYield').innerHTML = rows(report.by_day || [], [
    { key: 'day' }, { key: 'ops', num: true }, { key: 'findings', num: true },
    { key: 'dups', num: true }, { key: 'sources', num: true }, { key: 'platforms', num: true },
  ]) || '<tr><td colspan="6" class="small">暂无数据</td></tr>';

  const ledger = await api('/api/ledger?limit=60');
  $('#ledgerTable').innerHTML = (ledger.ledger || []).map((l) => `
    <tr><td>${esc(l.engine)}</td><td class="num">${l.findings_new}</td><td class="num">${l.dup_images}</td>
    <td class="num">${l.new_sources}</td><td class="num">${l.new_platforms}</td><td class="num">${num(l.duration_s, 1)}s</td>
    <td class="small">${esc(l.note || '')}</td></tr>`).join('') || '<tr><td colspan="7" class="small">暂无操作记录</td></tr>';

  const s = S.settings || {};
  $('#weightForm').innerHTML = [
    ['w_novelty', '新颖性 N'], ['w_potential', '来源潜力 P'],
    ['w_bridging', '桥接价值 B'], ['w_metadata', '元数据 M'],
  ].map(([key, label]) => `
    <label class="field"><span>${label}</span>
      <input type="number" step="0.05" min="0" max="1" data-weight="${key}" value="${s[key] ?? 0}"></label>`).join('');
  $('#threshForm').innerHTML = [
    ['phash_dup', 'pHash 判同（≤距离）'], ['phash_near', 'pHash 判近（≤距离）'],
    ['semantic_dup', '语义判同（≥余弦）'], ['cluster_join', '入簇阈值（≥余弦）'],
    ['triage_same_image', '来源追踪阈值（相似度≥）'], ['triage_bridge_value', '桥接阈值（E≥）'],
  ].map(([key, label]) => `
    <label class="field"><span>${label}</span>
      <input type="number" step="0.01" min="0" max="1" data-thresh="${key}" value="${s[key] ?? 0}"></label>`).join('');
  $('#weightSum').textContent = [
    'w_novelty', 'w_potential', 'w_bridging', 'w_metadata',
  ].reduce((acc, k) => acc + (Number(s[k]) || 0), 0).toFixed(2);
}

function bindMetricsPanel() {
  $('#saveWeights').onclick = async () => {
    const patch = {};
    $$('#weightForm [data-weight]').forEach((i) => { patch[i.dataset.weight] = Number(i.value); });
    const sum = ['w_novelty', 'w_potential', 'w_bridging', 'w_metadata']
      .reduce((acc, k) => acc + (patch[k] || 0), 0);
    if (Math.abs(sum - 1) > 0.001 && !confirm(`权重合计为 ${sum.toFixed(2)}，不是 1。仍然保存？`)) return;
    try {
      await api('/api/settings', { method: 'POST', body: { settings: patch } });
      S.settings = (await api('/api/settings')).settings;
      await api('/api/rescore', { method: 'POST', body: {} });
      toast('权重已保存并重算排序');
      await loadReport(); await loadBoard();
    } catch (e) { toast(e.message, true); }
  };
  $('#saveThresh').onclick = async () => {
    const patch = {};
    $$('#threshForm [data-thresh]').forEach((i) => { patch[i.dataset.thresh] = Number(i.value); });
    try {
      await api('/api/settings', { method: 'POST', body: { settings: patch } });
      S.settings = (await api('/api/settings')).settings;
      toast('去重/分队列阈值已保存（下次入库生效）');
    } catch (e) { toast(e.message, true); }
  };
  $('#reindexBtn2').onclick = () => $('#reindexBtn').click();
}

// -------------------------------------------------------------------- 启动
async function refreshHealth() {
  S.health = await api('/api/health');
  renderHealth();
}

async function boot() {
  renderTabs();
  $('#scrim').onclick = closeDrawer;
  $('#drawerClose').onclick = closeDrawer;
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') closeDrawer(); });

  try {
    const meta = await api('/api/engines');
    S.engines = meta.engines || [];
    S.variantKinds = meta.variant_kinds || [];
  } catch (e) { toast('加载引擎清单失败：' + e.message, true); }

  S.settings = (await api('/api/settings')).settings || {};
  renderEnginePicker('#importEngines');
  renderEnginePicker('#queueEngines', new Set(['baidu', 'so360']));
  renderEnginePicker('#promoteEngines', new Set(['baidu']));
  variantPicker('#importVariants');

  bindImportPanel();
  bindQueuePanel();
  bindBoardPanel();
  bindSearchPanel();
  bindGraphPanel();
  bindMetricsPanel();
  bindCropPanel();
  bindTrashPanel();
  bindArchivePanel();
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') { closeDrawer(); closeCropDrawer(); }
  });

  await refreshHealth();
  pollBrowserState();
  card_selection_changed();
  await loadSeeds();
  await loadQueue();
  await loadBoard();
  S.graph = await api('/api/graph');
  drawGraph();
  setInterval(refreshHealth, 20000);
}

window.addEventListener('DOMContentLoaded', boot);
