/**
 * HYW 页面上的「视觉工作台」卡片（可以直接贴进 HYW 的前端）。
 *
 * 用法：
 *
 *   <div id="vswCard"></div>
 *   <script src="/vsw-link-ui.js"></script>
 *   <script>
 *     HywVsw.mount('#vswCard', {
 *       token: localStorage.getItem('hyw_token'),   // HYW 自己的登录令牌
 *       api: (p) => '/api.node.js?a=' + encodeURIComponent(p),
 *     });
 *   </script>
 *
 * 卡片只做两件事：绑定（填一次工作台地址 + 账号密码）、打开（换一张一次性票
 * 跳过去）。令牌永远不出服务端 —— 这个文件里拿到的只有「绑没绑」和「跳哪个地址」。
 */
(function () {
  'use strict';

  const state = { token: '', api: null, bound: false, server: '', user: '', label: '', busy: false };
  let root = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
  }

  async function call(path, body, method) {
    const url = state.api(path);
    const res = await fetch(url, {
      method: method || (body ? 'POST' : 'GET'),
      headers: {
        'Content-Type': 'application/json',
        ...(state.token ? { Authorization: 'Bearer ' + state.token } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
    return data;
  }

  function render() {
    if (!root) return;
    if (state.bound) {
      root.innerHTML = `
        <div class="vsw-card">
          <div class="vsw-title">视觉工作台</div>
          <div class="vsw-meta">已绑定：${esc(state.user)} @ ${esc(state.server)}</div>
          <div class="vsw-acts">
            <button class="vsw-btn primary" data-act="open"${state.busy ? ' disabled' : ''}>
              ${state.busy ? '正在打开…' : '打开工作台'}
            </button>
            <button class="vsw-btn" data-act="unbind"${state.busy ? ' disabled' : ''}>解绑</button>
          </div>
          <div class="vsw-hint">点开之后是新标签页，已经登录好了 —— 票据 60 秒内有效、用一次就作废。</div>
        </div>`;
    } else {
      root.innerHTML = `
        <div class="vsw-card">
          <div class="vsw-title">绑定视觉工作台</div>
          <div class="vsw-hint">绑定一次，以后在 HYW 里点一下就进工作台，不用再登一次。</div>
          <input class="vsw-input" data-f="server" placeholder="工作台地址，例如 https://xxx.ts.net" value="${esc(state.server)}">
          <input class="vsw-input" data-f="name" placeholder="工作台的账号名">
          <input class="vsw-input" data-f="password" type="password" placeholder="工作台的密码">
          <div class="vsw-acts">
            <button class="vsw-btn primary" data-act="bind"${state.busy ? ' disabled' : ''}>
              ${state.busy ? '绑定中…' : '绑定'}
            </button>
          </div>
          <div class="vsw-hint">密码只在校验这一次用到，工作台只回一把可以随时撤销的令牌。</div>
        </div>`;
    }
    root.querySelectorAll('[data-act]').forEach((btn) => {
      btn.onclick = () => act(btn.dataset.act);
    });
  }

  async function act(what) {
    if (state.busy) return;
    const q = (f) => (root.querySelector('[data-f="' + f + '"]') || {}).value || '';
    state.busy = true;
    render();
    try {
      if (what === 'bind') {
        const data = await call('/vsw/bind', {
          server: q('server').trim(), name: q('name').trim(), password: q('password'), label: 'HYW',
        });
        state.bound = true; state.server = data.server || q('server'); state.user = data.user || q('name');
        state.busy = false; render(); return;
      }
      if (what === 'unbind') {
        await call('/vsw/unbind', {});
        state.bound = false; state.busy = false; render(); return;
      }
      if (what === 'open') {
        const data = await call('/vsw/open', { next: '/' });
        state.busy = false; render();
        const win = window.open(data.url, '_blank');
        if (!win) window.location.href = data.url;   // 弹窗被拦就整页跳
        return;
      }
    } catch (err) {
      state.busy = false;
      if (err && err.needBind) state.bound = false;
      render();
      const msg = (err && err.message) || String(err);
      if (root) {
        const tip = document.createElement('div');
        tip.className = 'vsw-err';
        tip.textContent = msg;
        root.appendChild(tip);
      }
    }
  }

  async function refresh() {
    try {
      const data = await call('/vsw/status');
      state.bound = !!data.bound;
      state.server = (data.link && data.link.server) || '';
      state.user = (data.link && data.link.user) || '';
    } catch (err) {
      state.bound = false;
    }
    render();
  }

  window.HywVsw = {
    mount: function (selector, opts) {
      root = typeof selector === 'string' ? document.querySelector(selector) : selector;
      if (!root) return;
      state.token = (opts && opts.token) || '';
      state.api = (opts && opts.api) || ((p) => '/api.node.js?a=' + encodeURIComponent(p));
      refresh();
    },
  };
})();
