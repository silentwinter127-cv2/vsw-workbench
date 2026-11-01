"""给 HYW 的 `api.node.js` 打一个「绑定视觉工作台」补丁。

用法：

    python hyw/patch_vsw_link.py "C:\\path\\to\\api.node (1).js" hyw/api.node.js

做两件事：在 `routeHandler` 里、`return err(404, '接口不存在');` 之前插入一组
`/vsw/*` 路由，并在文件顶部加一段注释说明它们从哪来。可重复执行（已打过就跳过）。

为什么是补丁脚本而不是直接改：那份文件是 HYW 自己还在迭代的服务端，改动要能
在对方更新之后重新贴上去。补丁块前后都有明确锚点，锚点找不到就报错退出，
绝不会悄悄插到奇怪的位置。
"""

from __future__ import annotations

import io
import os
import sys

MARK_BEGIN = "// ===== 视觉工作台绑定（HYW 账号 ↔ 工作台账号）开始 ====="
MARK_END = "// ===== 视觉工作台绑定结束 ====="

ANCHOR = "  return err(404, '接口不存在');"

BLOCK = MARK_BEGIN + r"""
// 这是「一眼看懂」的局部说明：HYW 上已经登录的人，不该再登一次工作台。
// 绑定只做一次（输入工作台地址 + 账号名 + 密码），换回一把 VSWL-… 令牌存进
// 这个账号的记录里；之后每次点「打开工作台」都拿令牌换一张 60 秒、用一次就
// 作废的票据，浏览器跳到 工作台/sso?ticket=… 就已经是登录状态。
// 令牌是凭据：只留在服务端数据里，绝不下发给浏览器。
function vswNormServer(u) {
  let s = String(u || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
  s = s.replace(/\/+$/, '');
  return s;
}

async function vswCall(server, apiPath, payload) {
  const url = server + apiPath;
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), 15000) : null;
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {}),
      signal: ctrl ? ctrl.signal : undefined,
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data: data || {} };
  } catch (e) {
    return { ok: false, status: 0, data: { error: '连不上工作台：' + String((e && e.message) || e) } };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function vswLinks(all) {
  if (!all.accounts.vswLinks) all.accounts.vswLinks = {};
  return all.accounts.vswLinks;
}

if (method === 'GET' && pathname === '/vsw/status') {
  return needAuth(() => {
    const l = vswLinks(all)[String(me.id)] || null;
    return {
      status: 200,
      body: {
        bound: !!l,
        link: l ? { server: l.server, user: l.user, label: l.label || '', at: l.at || 0 } : null,
        // 前端要拿它拼表单默认值：本机没有别的办法知道工作台在哪
        hint: '在工作台「共享 / 账号」里能看到自己的地址和账号名',
      },
    };
  });
}

if (method === 'POST' && pathname === '/vsw/bind') {
  return needAuth(async () => {
    const server = vswNormServer(body.server || body.url);
    const name = String(body.name || '').trim();
    const password = String(body.password || '');
    const label = String(body.label || 'HYW').slice(0, 40);
    if (!server) return err(400, '先填工作台地址，例如 http://192.168.1.46:8799');
    if (!name || !password) return err(400, '填工作台的账号名和密码');
    const r = await vswCall(server, '/api/sso/bind', { name: name, password: password, label: label });
    if (!r.ok) {
      const msg = (r.data && r.data.error) || ('绑定失败（HTTP ' + r.status + '）');
      return err(r.status === 401 ? 401 : 400, msg);
    }
    const key = r.data && r.data.key;
    if (!key) return err(400, '工作台没有返回绑定令牌（版本太旧？先升级工作台）');
    vswLinks(all)[String(me.id)] = {
      server: server,
      key: key,
      user: (r.data.user && r.data.user.name) || name,
      label: label,
      at: Date.now(),
      lastUse: 0,
    };
    return {
      status: 200,
      body: { ok: true, bound: true, server: server, user: (r.data.user && r.data.user.name) || name },
      dirty: true,
    };
  });
}

if (method === 'POST' && pathname === '/vsw/open') {
  return needAuth(async () => {
    const l = vswLinks(all)[String(me.id)] || null;
    if (!l) return err(400, '还没绑定工作台：先在「工作台」卡片里填一次账号密码');
    const next = String(body.next || '/');
    const r = await vswCall(l.server, '/api/sso/ticket', { key: l.key, label: 'HYW', next: next });
    if (!r.ok) {
      const msg = (r.data && r.data.error) || ('换票失败（HTTP ' + r.status + '）');
      if (r.status === 401) {
        // 令牌被撤销/工作台重装了：留着这条记录只会一直报错，清掉让用户重绑
        delete vswLinks(all)[String(me.id)];
        return { status: 409, body: { error: msg, needBind: true }, dirty: true };
      }
      return err(400, msg);
    }
    l.lastUse = Date.now();
    return { status: 200, body: { ok: true, url: r.data.url, expiresIn: r.data.expires_in || 60 }, dirty: true };
  });
}

if (method === 'POST' && pathname === '/vsw/unbind') {
  return needAuth(() => {
    if (vswLinks(all)[String(me.id)]) delete vswLinks(all)[String(me.id)];
    return { status: 200, body: { ok: true, bound: false }, dirty: true };
  });
}
""" + MARK_END + "\n\n"


def patch(text: str) -> str:
    if MARK_BEGIN in text:
        return text
    if ANCHOR not in text:
        raise SystemExit("找不到锚点 `return err(404, '接口不存在');` —— "
                         "这份 api.node.js 的版本和补丁不匹配，先把文件发我重新对锚点")
    if "async function vswCall" in text:
        raise SystemExit("已经有一份 vsw 绑定代码了（但没有标记注释），不敢重复插入")
    return text.replace(ANCHOR, BLOCK + ANCHOR, 1)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = argv[1]
    dst = argv[2] if len(argv) > 2 else os.path.splitext(src)[0] + ".vsw.js"
    with io.open(src, encoding="utf-8") as fh:
        text = fh.read()
    out = patch(text)
    with io.open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    added = len(out) - len(text)
    print(f"打好补丁：{dst}（+{added} 字符）")
    if added == 0:
        print("（原文件已经打过，内容没变）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
