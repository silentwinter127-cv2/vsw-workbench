/**
 * 用假的「热铁盒」存储把打好补丁的 api.node.js 跑起来，验证 /vsw/* 三条路由。
 *
 * 这个测试**不碰 HYW 的真实数据**：Database 是内存 Map，fetch 指向本地临时
 * 起的一个视觉工作台。跑法（工作台已经在 8791 上起来）：
 *
 *     node hyw/test_vsw_link.js http://127.0.0.1:8791
 */
'use strict';

const path = require('path');

const VSW = (process.argv[2] || 'http://127.0.0.1:8791').replace(/\/+$/, '');

// ---- 假的热铁盒 KV：内存 Map ----
class FakeDB {
  constructor(name) {
    this.name = name;
    FakeDB.store = FakeDB.store || new Map();
    if (!FakeDB.store.has(name)) FakeDB.store.set(name, new Map());
    this.map = FakeDB.store.get(name);
  }
  async get(k) { return this.map.has(k) ? this.map.get(k) : null; }
  async set(k, v) { this.map.set(k, String(v)); return true; }
  async delete(k) { this.map.delete(k); return true; }
}
global.Database = FakeDB;

// ---- 假平台：req/res/document.write ----
let written = '';
global.document = { write: (s) => { written += s; } };

const mod = require(path.join(__dirname, 'api.node.js'));
if (typeof mod.run !== 'function') {
  console.error('没能拿到 run()：RTH_MOCK 没生效？');
  process.exit(1);
}

// seed 里这个 session 属于 admin（id 10）
const TOKEN = 'efd3bbb3b8da76a1a0f7e761ce7d96df';

function req(method, a, body) {
  return {
    method: method,
    query: { a: a },
    headers: { authorization: 'Bearer ' + TOKEN },
    body: body ? JSON.stringify(body) : undefined,
  };
}

const results = [];
function check(name, ok, detail) {
  results.push({ name: name, ok: !!ok, detail: detail });
  console.log((ok ? '  ok   ' : '  FAIL ') + name + (detail === undefined ? '' : ' :: ' + JSON.stringify(detail)));
}

(async () => {
  // 0) 未绑定
  let out = await mod.run(req('GET', '/vsw/status'));
  check('未绑定时 status 说没绑', out.status === 200 && out.body.bound === false, out.body);

  // 1) 密码错 → 不能绑
  out = await mod.run(req('POST', '/vsw/bind', { server: VSW, name: 'sso主人', password: 'wrong-pw' }));
  check('密码错时拒绝绑定', out.status === 401, out.body);

  // 2) 地址缺协议头也能绑（自动补 https:// 之外，这里给 http）
  out = await mod.run(req('POST', '/vsw/bind', { server: VSW, name: 'sso主人', password: 'secret123', label: 'HYW' }));
  check('绑定成功', out.status === 200 && out.body.ok === true, out.body);
  check('返回体里不带令牌明文', !JSON.stringify(out.body).includes('VSWL-'), out.body);

  out = await mod.run(req('GET', '/vsw/status'));
  check('绑完 status 有记录', out.body.bound === true && out.body.link.server === VSW, out.body);

  // 3) 拿票 → 浏览器跳过去
  out = await mod.run(req('POST', '/vsw/open', {}));
  const url = (out.body && out.body.url) || '';
  check('换到一次性链接', out.status === 200 && url.indexOf('/sso?ticket=') > 0, out.body);

  let cookie = '';
  let location = '';
  const resp = await fetch(url, { redirect: 'manual' });
  location = resp.headers.get('location') || '';
  cookie = resp.headers.get('set-cookie') || '';
  check('票据换到会话 cookie', resp.status === 302 && /vsw_sid=/.test(cookie), { status: resp.status, location: location });

  const again = await fetch(url, { redirect: 'manual' });
  check('同一张票不能再用', again.status === 302 && /sso=expired/.test(again.headers.get('location') || ''), again.headers.get('location'));

  // 4) 解绑
  out = await mod.run(req('POST', '/vsw/unbind', {}));
  check('解绑成功', out.status === 200 && out.body.bound === false, out.body);
  out = await mod.run(req('GET', '/vsw/status'));
  check('解绑后回到未绑定', out.body.bound === false, out.body);

  // 5) 没绑就点打开 → 明确让人去绑
  out = await mod.run(req('POST', '/vsw/open', {}));
  check('没绑时点打开给出提示', out.status === 400 && /还没绑定/.test(out.body.error || ''), out.body);

  // 6) 令牌被撤销 → 409 并清掉本地记录
  out = await mod.run(req('POST', '/vsw/bind', { server: VSW, name: 'sso主人', password: 'secret123', label: 'HYW' }));
  check('重新绑定成功', out.status === 200, out.body);
  // 直接对着工作台把这条绑定撤销
  const login = await fetch(VSW + '/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'sso主人', password: 'secret123' }),
  }).then((r) => r.json());
  const sid = login.token;
  const links = await fetch(VSW + '/api/sso/links', { headers: { 'X-VSW-Session': sid } }).then((r) => r.json());
  await fetch(VSW + '/api/sso/revoke/' + links.links[0].id, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-VSW-Session': sid }, body: '{}',
  });
  out = await mod.run(req('POST', '/vsw/open', {}));
  check('令牌被撤销后提示重新绑定', out.status === 409 && out.body.needBind === true, out.body);
  out = await mod.run(req('GET', '/vsw/status'));
  check('撤销后本地记录也清掉了', out.body.bound === false, out.body);

  // 7) 未登录访问一律 401
  const anon = await mod.run({ method: 'GET', query: { a: '/vsw/status' }, headers: {} });
  check('未登录访问 status 是 401', anon.status === 401, anon.body);
  const anon2 = await mod.run({ method: 'POST', query: { a: '/vsw/open' }, headers: {}, body: '{}' });
  check('未登录点打开是 401', anon2.status === 401, anon2.body);

  const passed = results.filter((r) => r.ok).length;
  console.log('');
  console.log(passed + ' 通过 / ' + (results.length - passed) + ' 失败');
  process.exit(passed === results.length ? 0 : 1);
})().catch((e) => {
  console.error('崩了：', e && e.stack || e);
  process.exit(1);
});
