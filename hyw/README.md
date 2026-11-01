# 在 HYW 上嫁接视觉工作台（HYW 账号 ↔ 工作台账号）

目标：HYW 的用户**拥有 HYW 账号就等于能进视觉工作台**——不必再登一次。
做法是让 HYW 的后端替用户保管一把「绑定令牌」，用户点「打开工作台」时换一张
一次性票据，浏览器带着票据跳过去，工作台认票据、种 cookie。

这个目录里三样东西：

| 文件 | 作用 |
| --- | --- |
| `patch_vsw_link.py` | 给 HYW 的 `api.node.js` 打补丁，加四条 `/vsw/*` 接口（可重复执行） |
| `api.node.js` | 打完补丁的结果（161 KB，`node --check` 通过） |
| `vsw-link-ui.js` | HYW 页面上的「视觉工作台」卡片（绑定表单 / 打开按钮 / 解绑） |
| `test_vsw_link.js` | 本地自测：假 Database + 假 document，16 项断言 |

## 时序

```
HYW 后端                      视觉工作台
   │                              │
   │ ① POST /api/sso/bind         │  用户填一次工作台账号密码
   │    {name, password}          │  （密码只用于这一次校验，不落地）
   │ ───────────────────────────► │
   │ ◄───────── {key: VSWL-…}     │  令牌只存哈希，随时可撤销
   │                              │
   │ 令牌存进 HYW 自己的库          │
   │ （all.accounts.vswLinks[uid]）│
   │                              │
   │ ② 用户点「打开工作台」          │
   │ POST /api/sso/ticket {key}   │
   │ ───────────────────────────► │
   │ ◄──── {url: …/sso?ticket=…}  │  票据 60 秒、用一次就作废
   │                              │
   │ ③ 浏览器跳到那个 url           │
   │ ─────────────────────────────────────────►  /sso?ticket=…
   │                              │  换票 → 种 vsw_sid → 302 到 /
```

令牌**永远不下发到浏览器**：HYW 前端只调自己的 `/vsw/*`，令牌留在 HYW 服务端。

## 一、工作台这一侧（已经做好）

工作台新增了这些接口（`vsw/sso.py` + `vsw/server.py`）：

| 方法 | 路径 | 谁能调 | 干什么 |
| --- | --- | --- | --- |
| POST | `/api/sso/bind` | 任何人（要账号密码） | 密码换绑定令牌，返回 `{ok, key, id, user, server}` |
| POST | `/api/sso/ticket` | 拿着绑定令牌的人 | 换一次性票据，返回 `{ok, url, ticket, expires_in: 60, user}` |
| GET | `/api/sso/links` | 登录用户（主人看全部） | 列出自己有哪些绑定 |
| POST | `/api/sso/revoke/<id>` | 同上 | 撤销一条绑定 |
| POST | `/api/sso/unbind-all` | 同上 | 一次全撤（比如密码怀疑泄露） |
| GET | `/sso?ticket=…` | 浏览器 | 换票种 cookie，302 到主页面 |

安全边界（探针 `scripts/vsw-probe-sso.py` 39 项全部覆盖）：

* 库里**只有哈希**，任何人都拿不到明文令牌；令牌不能当会话令牌用；
* 撤销立刻生效——撤销后拿旧令牌换票直接 401；
* 票据 60 秒、取出即删，历史记录里那条 URL 再点一次只会看到「票过期」；
* 换票接口在 `_guard` **之前**，所以工作台开了账号、上了密钥也照常能换（第一版插在后面，被 401 挡掉过）。

## 二、HYW 这一侧：三步

### 1. 打补丁

```powershell
python hyw\patch_vsw_link.py "C:\path\to\api.node.js"
node --check "C:\path\to\api.node.js"
```

补丁插在 `return err(404, '接口不存在');` 之前，用 `// ===== 视觉工作台绑定…` 包裹，
**可以重复执行**（已经打过就跳过）。四条新接口：

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| GET | `/vsw/status` | 这个 HYW 账号绑没绑、绑的是哪个地址哪个账号 |
| POST | `/vsw/bind` | `{server, name, password, label}` → 调工作台 `/api/sso/bind`，令牌留在服务端 |
| POST | `/vsw/open` | 调工作台 `/api/sso/ticket`，把 `{url}` 交回前端去跳 |
| POST | `/vsw/unbind` | 解绑并清掉服务端那条记录 |

都走 HYW 自己的 `needAuth(...)`，未登录一律 401 `{error:'请先登录'}`。

### 2. 部署 + 记住那条硬限制

**HYW 是公网服务，工作台地址必须是公网能连上的地址。**

填 `http://192.168.1.46:8799` 一定失败——HYW 的服务器在公网，它连不到你家局域网。
要么把工作台开公网通道（Tailscale Funnel）：

```
start_vsw.bat --tunnel
```

启动日志里会出现 `[vsw] 🌍 公网地址：https://xxx.ts.net/`，把它填进 HYW 的绑定表单
（`https://xxx.ts.net`，别带结尾斜杠，带了也会被规范掉）。

也可以用自己的域名 + 反向代理，只要 HYW 服务器能 `fetch` 到、且是 HTTPS
（HYW 在 HTTPS 页面上，绑 `http://` 地址虽然能用，但表单密码是明文过公网，不推荐）。

### 3. 前端挂卡片

```html
<div id="vswCard"></div>
<script src="/vsw-link-ui.js"></script>
<script>
  HywVsw.mount('#vswCard', {
    token: localStorage.getItem('hyw_token'),   // HYW 自己存的登录令牌
    api: (p) => '/api.node.js?a=' + encodeURIComponent(p),
  });
</script>
```

卡片两种状态：没绑 → 地址/账号/密码 + 「绑定」；已绑 → 「打开工作台」+「解绑」。
打开方式是新标签页（`window.open`，被拦就整页跳）。

## 三、自测

```powershell
# 起一个临时工作台（另开一个窗口）
python -m vsw.server --root "$env:TEMP\vsw-hyw-test" --host 127.0.0.1 --port 8791 --no-browser
# 建个账号，然后把地址喂给自测脚本
node hyw\test_vsw_link.js http://127.0.0.1:8791
```

16 项断言：未绑定、错密码 401、绑定成功且响应体不含 `VSWL-` 明文、换票、跳转、
同一张票重放失败、解绑、没绑时点打开给提示、令牌被撤销后提示重新绑定、
未登录 401。

## 四、已知取舍

* 绑定关系存在 `all.accounts.vswLinks[uid]`，跟着 HYW 主库一起写回（`dirty: true`）。
  好处是不用改 HYW 的存储层；代价是它跟账号数据同一个 JSON，账号多了会变大。
* 工作台重启后**没兑换的票据**失效——第三方再要一张就行，用户不会看到失败。
* 一个 HYW 账号只能绑一个工作台账号；要换绑就先解绑（或直接重新绑定，会覆盖）。
* 用户在工作台那边改了密码，绑定令牌**依然有效**（令牌是独立凭证，不是密码派生）。
  要让它失效，得在工作台「共享 / 账号 → 第三方应用绑定」里撤销，或者 `/vsw/unbind`。
