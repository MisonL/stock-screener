#!/usr/bin/env node
// 腾讯自选股 westock-mcp 的无浏览器授权：微信扫码 → 三件套 cookie → consent → token
//
// ⚠️ 使用边界（请先读这段）
//   本脚本把「用你自己的微信扫一次码」这个人工步骤自动化到命令行，登录态**只代表
//   你本人的账号**。请勿用于批量注册、多账号轮换、账号池或对外提供数据服务。
//   腾讯侧的服务条款你需要在用之前自行确认；本项目不对此作任何担保，也不提供账号。
//   仓库的 LICENSE 只覆盖本仓库代码，不覆盖任何第三方服务。
//
// 设计前提：全部在 VM 上用纯 HTTP 完成，不需要浏览器、不需要 Mac。
// 唯一的真人动作是「用微信扫一下二维码」，二维码由本脚本生成并打印保存路径。
//
// 用法：
//   node wb_auth_headless.mjs --out ~/.config/stock-screener/westock.env
//   node wb_auth_headless.mjs --qr-out /tmp/wb_qr.jpg --out ...   # 同时把二维码存成图片
//   node wb_auth_headless.mjs --wx-code-file /tmp/wb_wx_code.txt --out ...  # 已有 wx_code，跳过扫码
//
// 依赖：Node 18+（用内置 fetch），无第三方包。

import { writeFileSync, readFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { createHash, randomBytes } from 'node:crypto';

// 四个端点默认打腾讯。`WB_AUTH_BASE` / `WB_PROXY_BASE` 只为测试留的本地 stub 口子
// （生产不设这两个变量，行为与以前逐字一致）。
const AUTH_BASE = process.env.WB_AUTH_BASE || 'https://stockbuddy.qq.com/cgi/cgi-bin/openai/mcp/oauth';
const PROXY_BASE = process.env.WB_PROXY_BASE || 'https://proxy.finance.qq.com';
const AUTHZ = `${AUTH_BASE}/authorize`;
const TOKEN = `${AUTH_BASE}/token`;
const REGISTER = `${AUTH_BASE}/register`;
// 写进 westock.env 的 MCP 端点：读取方（mcp/westock.py）只认这个键名
const MCP_URL = process.env.WESTOCK_MCP_URL || 'https://stockbuddy.qq.com/cgi/cgi-bin/openai/mcp/mcp';

const WX_APPID = 'wxff01bb724e435269';
// 微信扫码的回调地址有两条，语义不同（2026-10-30 从 gu.qq.com 登录页 bundle 逐字读出）：
//   • weixin/web   —— 自选股自己的老网页登录，回调只下发 w_r_token/w_openid/w_token/w_uin，
//                     **没有 zxg_***。consent 不认 w_*，这条是死路。
//   • weixin/panel —— workBuddy 登录页真正用的那条（`fr()` 拼出来的 URL）。它把登录页当前的
//                     一整套 OAuth 参数原样带着走，是唯一出现在成功历史里的回调。
// 所以默认用 panel，把完整 OAuth 参数拼进去；web 留作 --redirect-mode=web 的对照。
const WX_PANEL = 'https://weixin.finance.qq.com/stockapp/weixin/panel';
const WX_WEB = `https://weixin.finance.qq.com/stockapp/weixin/web?appid=${WX_APPID}&ref=web`;
// 与 qq/login 对称的微信换码接口（2026-10-30 实测存在）：
//   返回 {code:0, data:{openid,fskey,check,g_openid,nickname,headimgurl,reg_time,status}}
//   字段与 zxg_* 三件套一一对应 → 拿到 wx_code 直接问它，比追 cookie 链更干净。
//   appid 判别清晰：wxff01bb… / wxcbc3… → 走换发；101481127（QQ 的）→ 参数错误(appid error)。
const WX_LOGIN = `${PROXY_BASE}/cgi/cgi-bin/login/weixin/login`;
const WX_QRCONNECT = 'https://open.weixin.qq.com/connect/qrconnect';
const WX_QRCODE = 'https://open.weixin.qq.com/connect/qrcode';
const WX_LONGPOLL = 'https://long.open.weixin.qq.com/connect/l/qrconnect';

// 用一个通用的 Android Chrome 手机 UA。腾讯的登录流程对手机 UA 更宽松，
// 桌面 UA 容易走到另一套风控路径；具体机型无所谓，只要是主流手机 UA 即可。
// 需要时设环境变量 WB_UA 覆盖成你自己设备的 UA。
const UA = process.env.WB_UA
  || 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36';

// ---------- 参数 ----------
const argv = process.argv.slice(2);
const arg = (name, dflt) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
};
const OUT = arg('out', `${process.env.HOME}/.config/stock-screener/westock.env`);
const QR_OUT = arg('qr-out', '');
const CLIENT_CACHE = arg('client-cache', `${process.env.HOME}/.config/stock-screener/wb_client.json`);
const CONNECTOR = arg('connector', 'stock-screener');
const REDIRECT_URI = `workbuddy://workbuddy/mcp/connector:${CONNECTOR}/oauth/callback`;
const RESOURCE = 'https://stockbuddy.qq.com/cgi/cgi-bin/openai/mcp/mcp';
const REDIRECT_MODE = arg('redirect-mode', 'panel');   // panel | web
// panel 模式的回调 URL：复刻登录页 `fr()` = `${Qu}?${hu()}`，即把登录页当前的整套参数
// （client_id/code_challenge/redirect_uri/resource/response_type/state + from=workbuddy）
// 原样带上，再由 pf.init 追加 appid=…&ref=web。扫码成功后微信把 wx_code 送回这里。
function buildPanelRedirect(clientId, challenge, state) {
  const p = new URLSearchParams();
  p.set('client_id', clientId);
  p.set('code_challenge', challenge);
  p.set('code_challenge_method', 'S256');
  p.set('redirect_uri', REDIRECT_URI);
  p.set('resource', RESOURCE);
  p.set('response_type', 'code');
  p.set('state', state);
  p.set('from', 'workbuddy');
  p.set('appid', WX_APPID);
  p.set('ref', 'web');
  return `${WX_PANEL}?${p.toString()}`;
}
// 扫码时交给 qrconnect 的 redirect_uri；实际值在拿到 clientId/challenge/state 后写入。
let WX_REDIRECT = REDIRECT_MODE === 'web' ? WX_WEB : '';
const POLL_LIMIT = Number(arg('poll-limit', '2000'));            // 轮询次数硬上限（防死循环）
const POLL_TIMEOUT_MS = Number(arg('poll-timeout-ms', String(20 * 60 * 1000)));  // 总等待时长，20 分钟

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const mask = (s) => (s ? `${String(s).slice(0, 6)}…(${String(s).length})` : '(空)');

// ---------- 风控闸门 ----------
// 这是保护「用户本人的腾讯账号」不被风控，不是性能优化。三条硬约束：
//   ① 两次运行之间至少间隔 MIN_INTERVAL_MS（默认 5 分钟），失败后不会连打
//   ② 静默续期成功时不动二维码端点，绝不无谓地新建登录会话
//   ③ 连续失败达到上限就停手，把问题交给人，而不是继续重试
const MIN_INTERVAL_MS = Number(arg('min-interval-ms', String(5 * 60 * 1000)));
const STAMP = arg('stamp', `${process.env.HOME}/.config/stock-screener/.wb_auth_lastrun`);

function guardInterval() {
  if (MIN_INTERVAL_MS <= 0) return;
  try {
    const last = Number(readFileSync(STAMP, 'utf8').trim());
    const since = Date.now() - last;
    if (Number.isFinite(last) && since < MIN_INTERVAL_MS) {
      const wait = Math.ceil((MIN_INTERVAL_MS - since) / 1000);
      throw new Error(`距上次运行仅 ${Math.round(since / 1000)}s，风控闸门要求间隔 ≥ ${MIN_INTERVAL_MS / 1000}s（还需等 ${wait}s）。用 --min-interval-ms 0 可跳过（仅调试用）`);
    }
  } catch (e) {
    if (e.message.includes('风控闸门')) throw e;
    // 文件不存在或损坏 → 放行
  }
}
function stampRun() {
  try {
    mkdirSync(dirname(STAMP), { recursive: true });
    writeFileSync(STAMP, String(Date.now()), { mode: 0o600 });
  } catch { /* 打不上时间戳不影响主流程 */ }
}

// ---------- 简易 cookie jar ----------
const jar = new Map();
function absorbCookies(res) {
  const raw = typeof res.headers.getSetCookie === 'function' ? res.headers.getSetCookie() : [];
  const added = [];
  for (const line of raw) {
    const [pair, ...attrs] = line.split(';');
    const eq = pair.indexOf('=');
    if (eq < 0) continue;
    const name = pair.slice(0, eq).trim();
    const value = pair.slice(eq + 1).trim();
    const expired = attrs.some((a) => /^\s*max-age\s*=\s*0\s*$/i.test(a))
      || attrs.some((a) => /^\s*expires\s*=.*1970/i.test(a));
    if (expired) jar.delete(name);
    else { jar.set(name, value); added.push(name); }
  }
  return added;
}
const cookieHeader = () => [...jar.entries()].map(([k, v]) => `${k}=${v}`).join('; ');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function get(url, opts = {}) {
  const headers = { 'User-Agent': UA, ...(opts.headers || {}) };
  const c = cookieHeader();
  if (c) headers.Cookie = c;
  const res = await fetch(url, { ...opts, headers, redirect: opts.redirect ?? 'manual' });
  absorbCookies(res);
  return res;
}

// ---------- PKCE / 客户端注册 ----------
const b64url = (buf) => buf.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

async function ensureClient() {
  if (existsSync(CLIENT_CACHE)) {
    try {
      const c = JSON.parse(readFileSync(CLIENT_CACHE, 'utf8'));
      if (c.client_id && c.redirect_uri === REDIRECT_URI) {
        log(`复用已注册客户端 ${mask(c.client_id)}`);
        return c.client_id;
      }
    } catch { /* 缓存坏了就重新注册 */ }
  }
  const body = {
    client_name: `stock-screener-${Date.now()}`,
    redirect_uris: [REDIRECT_URI],
    grant_types: ['authorization_code'],
    response_types: ['code'],
    token_endpoint_auth_method: 'none',
  };
  const res = await fetch(REGISTER, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'User-Agent': UA },
    body: JSON.stringify(body),
  });
  const json = await res.json().catch(() => null);
  if (!res.ok || !json?.client_id) {
    throw new Error(`动态注册失败 HTTP ${res.status}: ${JSON.stringify(json).slice(0, 300)}`);
  }
  mkdirSync(dirname(CLIENT_CACHE), { recursive: true });
  writeFileSync(CLIENT_CACHE, JSON.stringify({ client_id: json.client_id, redirect_uri: REDIRECT_URI, created_at: Date.now() }, null, 2), { mode: 0o600 });
  log(`新注册客户端 ${mask(json.client_id)}`);
  return json.client_id;
}

// ---------- 微信扫码 ----------
// 风控要点：新建一次 qrconnect = 新建一个登录会话。手机相册扫码可能要好几分钟，
// 所以 uuid 跨进程缓存复用，只有真过期（长轮询返回 404）才重建。
const QR_TTL_MS = Number(arg('qr-ttl-ms', String(4 * 60 * 1000)));
const QR_CACHE = arg('qr-cache', `${process.env.HOME}/.config/stock-screener/.wb_qr.json`);

function loadCachedQr() {
  try {
    const o = JSON.parse(readFileSync(QR_CACHE, 'utf8'));
    if (o.uuid && Date.now() - o.at < QR_TTL_MS) return o.uuid;
  } catch { /* 无缓存或已过期 */ }
  return null;
}
function saveCachedQr(uuid) {
  try {
    mkdirSync(dirname(QR_CACHE), { recursive: true });
    writeFileSync(QR_CACHE, JSON.stringify({ uuid, at: Date.now() }), { mode: 0o600 });
  } catch { /* 缓存失败不影响主流程 */ }
}
function clearCachedQr() {
  try { writeFileSync(QR_CACHE, '{}', { mode: 0o600 }); } catch { /* ignore */ }
}

async function fetchQrImage(uuid) {
  const res = await get(`${WX_QRCODE}/${uuid}`);
  const buf = Buffer.from(await res.arrayBuffer());
  const ct = res.headers.get('content-type') || '';
  if (QR_OUT) {
    mkdirSync(dirname(QR_OUT), { recursive: true });
    writeFileSync(QR_OUT, buf, { mode: 0o600 });
  }
  return { buf, ct };
}

async function newQr() {
  const u = new URL(WX_QRCONNECT);
  u.searchParams.set('appid', WX_APPID);
  u.searchParams.set('redirect_uri', WX_REDIRECT);
  u.searchParams.set('response_type', 'code');
  u.searchParams.set('scope', 'snsapi_login');
  u.searchParams.set('state', '111');
  u.searchParams.set('href', 'https://wzq.gtimg.com/resources/web-zixuangu/wx_style_20260710.css');

  const html = await (await get(u.toString())).text();
  const uuid = (html.match(/uuid=([0-9A-Za-z_-]{10,})/) || [])[1];
  if (!uuid) throw new Error('qrconnect 页面里没找到 uuid');
  saveCachedQr(uuid);
  log(`新二维码 uuid = ${uuid}`);
  const { buf, ct } = await fetchQrImage(uuid);
  log(`二维码图片 ${ct} ${buf.length}B${QR_OUT ? ` → ${QR_OUT}` : ''}`);
  return uuid;
}

function announceQr(uuid) {
  console.log('');
  console.log('════════════════════════════════════════════');
  console.log('  这张二维码必须用微信「扫一扫 → 摄像头」扫，不能存图后从相册识别。');
  console.log('  微信对登录类二维码封禁了「长按识别」和「相册识别」两条路，');
  console.log('  所以屏幕上必须显示着这张图（电脑/平板/另一台手机），用你的手机去扫它。');
  console.log('  扫完手机会弹「登录确认」，点「允许」。');
  if (QR_OUT) console.log(`  图片路径: ${QR_OUT}`);
  console.log(`  也可直接在浏览器打开: ${WX_QRCODE}/${uuid}`);
  console.log('  二维码有效期只有几十秒到几分钟，过期会自动重建。');
  console.log('════════════════════════════════════════════');
  console.log('');
}

// 返回 {code} / {expired:true} / {cancelled:true}
//
// 状态机以官方 qrconnect 页面自身的轮询 JS 为准（2026-10-30 从页面源码逐字核对）：
//   408 = 未扫码            → 继续轮询（页面用 2s 间隔）
//   404 = 已扫码、等用户点「允许」→ 继续轮询（页面用 100ms 间隔），**不是过期**
//   405 = 已确认，wx_code 有值 → 成功
//   402 = 二维码过期         → 返回 expired，由调用方重建
//   403 = 用户取消           → 返回 cancelled
//   500 = 服务端要求重载     → 当作过期处理
// 页面在后续请求上会带 &last=<上一次的 errcode>，这里同样照做。
//
// 循环以「总时长」为主、轮询次数为辅：已扫待确认阶段是 0.5s 一次，
// 若按次数限制会在两分钟内就耗尽，把用户还在手机上点「允许」的时间误判成超时。
async function waitForScan(uuid) {
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  let last = '';
  let sawScanned = false;
  let i = 0;
  for (; i < POLL_LIMIT && Date.now() < deadline; i++) {
    const url = `${WX_LONGPOLL}?uuid=${uuid}${last ? '&last=' + last : ''}&_=${Date.now()}`;
    let text = '';
    try {
      text = await (await get(url)).text();
    } catch (e) {
      log(`轮询异常（继续）: ${e.message}`);
      await sleep(1000);
      continue;
    }
    const errcode = (text.match(/wx_errcode=(\d+)/) || [])[1] || '';
    const code = (text.match(/wx_code='([^']*)'/) || text.match(/wx_code=([^;]*)/) || [])[1] || '';
    if (i === 0 || errcode !== last) log(`轮询 #${i + 1} errcode=${errcode || '(无)'} code=${code ? mask(code) : '(空)'}`);

    if (code) return { code };
    if (errcode === '405') {
      // 官方语义里 405 就该带 code；没带说明这一轮还没写进来，继续等一次
      log('已确认但本轮未带 code，继续轮询');
      await sleep(500);
      continue;
    }
    if (errcode === '404') {
      if (!sawScanned) { sawScanned = true; log('已扫码，等待手机上点「允许」…'); }
      last = errcode;
      await sleep(500);
      continue;
    }
    if (errcode === '408') { last = errcode; await sleep(1500); continue; }
    if (errcode === '402') { log('二维码已过期（402）'); return { expired: true }; }
    if (errcode === '403') { log('用户在手机上取消了（403）'); return { cancelled: true }; }
    if (errcode === '500') { log('服务端要求重载（500），按过期处理'); return { expired: true }; }
    // 未知码：不猜测语义，稍等再看，但不覆盖 last（未知码回传可能干扰服务端状态）
    log(`未知 errcode=${errcode || '(无)'}，稍后重试`);
    await sleep(1500);
  }
  throw new Error(`轮询超时（${i} 次 / ${Math.round(POLL_TIMEOUT_MS / 60000)} 分钟）`);
}

// ---------- 微信 code → 登录态三件套 ----------
//
// 2026-10-30 从 gu.qq.com 登录页 bundle 读出的真实结构：
//   • 登录页 JS **从不写** zxg_*（全文件只有删除，`Be.set` 0 次；`loginByWechatCookie` 只读
//     document.cookie）。所以 zxg_* 必须是某个服务端响应的 Set-Cookie。
//   • 登录页的微信回调地址是 `weixin/panel`（`fr()`），不是 `weixin/web`。历史里唯一一次
//     成功授权用的正是 panel，且 zxg_* 与 panel 的导航发生在同一秒。
//   • 另有一个与 qq/login 对称的换码接口 `login/weixin/login`，返回的 data 字段
//     {openid,fskey,check,...} 与三件套一一对应。
//
// 于是这里按「先接口、再跳转」两段走，任一段拿到 zxg_* 就收工。两条都用同一个 wx_code，
// 扫码只消耗一次（微信的 code 在 5 分钟内可重复使用，轮询到 405 后服务端并未立即消费）。
async function tryWeixinLoginApi(wxCode) {
  const u = new URL(WX_LOGIN);
  u.searchParams.set('app', 'darwin');
  u.searchParams.set('appid', WX_APPID);
  u.searchParams.set('code', wxCode);
  try {
    const res = await get(u.toString());
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* 非 JSON */ }
    if (!json) { log(`  weixin/login 返回非 JSON: ${text.slice(0, 160)}`); return null; }
    const d = json.data || {};
    log(`  weixin/login: code=${json.code} msg=${json.msg || ''} openid=${mask(d.openid)} fskey=${mask(d.fskey)} check=${mask(d.check)}`);
    if (json.code === 0 && d.openid && d.fskey) {
      // 顺手把服务端可能下发的 cookie 收进 jar（有的响应会同时 Set-Cookie）
      return { openid: String(d.openid), fskey: String(d.fskey), check: String(d.check ?? '') };
    }
  } catch (e) {
    log(`  weixin/login 异常: ${e.message}`);
  }
  return null;
}

async function exchangeWxCode(wxCode) {
  // ① 直调换码接口
  const apiSession = await tryWeixinLoginApi(wxCode);
  if (apiSession && apiSession.openid && apiSession.fskey) {
    log(`  走 weixin/login 直换成功，无需追 cookie 链`);
    return apiSession;
  }

  // ② 回退到「扫码回调 → 跟跳转 → 收 Set-Cookie」
  const start = `${WX_REDIRECT}&code=${encodeURIComponent(wxCode)}&state=111`;

  const follow = async (url, tag) => {
    for (let hop = 0; hop < 8; hop++) {
      const res = await get(url);
      const loc = res.headers.get('location');
      log(`  ${tag} 跳转 ${hop + 1}: HTTP ${res.status} cookie=[${[...jar.keys()].join(',') || '无'}] → ${loc ? loc.slice(0, 90) : '(无 Location)'}`);
      if (!loc) return;
      url = new URL(loc, url).toString();
      if (!/qq\.com$/.test(new URL(url).hostname)) return;
    }
  };

  await follow(start, '初次');
  // 立刻把完整 cookie 落盘：万一后面推不出 zxg_*，可以拿这份离线重试，不必再消耗一次扫码。
  const rawJar = Object.fromEntries(jar);
  try {
    const p = `${dirname(SESSION_CACHE)}/wb_cookies.json`;
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, JSON.stringify({ ...rawJar, saved_at: new Date().toISOString() }, null, 2), { mode: 0o600 });
    log(`  完整 cookie 已落盘: ${p}（${Object.keys(rawJar).join(',') || '空'}）`);
  } catch (e) { log(`  落盘 cookie 失败: ${e.message}`); }

  if (!jar.get('zxg_openid') || !jar.get('zxg_fskey')) {
    log(`  未见 zxg_*，当前 cookie=[${[...jar.keys()].join(',')}]，开始逐个探测换发入口`);
    // 模拟 wx_proxy.html 的 reload：带完整 w_* cookie 再请求一次原地址
    await follow(start, '重放');
  }
  if (!jar.get('zxg_openid') || !jar.get('zxg_fskey')) {
    log('  仍未见 zxg_*，改为不带 code 再试一次');
    await follow(WX_REDIRECT, '无码');
  }
  if (!jar.get('zxg_openid') || !jar.get('zxg_fskey')) {
    // 候选端点：登录页 JS 里出现过的、以及 w_* 登录态可能被换取 zxg_* 的地方。
    // 每个都打出发了新 cookie 没有，一次跑完就能定位。
    const probe = async (tag, url) => {
      const before = new Set(jar.keys());
      try {
        const res = await get(url);
        const after = [...jar.keys()].filter((k) => !before.has(k));
        log(`  探针 ${tag}: HTTP ${res.status} 新 cookie=[${after.join(',') || '无'}]`);
      } catch (e) {
        log(`  探针 ${tag}: 异常 ${e.message}`);
      }
    };
    const candidates = [
      ['wx_proxy', 'https://gu.qq.com/login/wx_proxy.html'],
      ['自选股首页', 'https://gu.qq.com/resource/products/portfolio/index_v1.html'],
      ['登录页', 'https://gu.qq.com/resource/workBuddyLoginPage/index.html'],
      ['微信面板', 'https://weixin.finance.qq.com/stockapp/weixin/panel?from=workbuddy'],
      ['自选股域名', 'https://wzq.gtimg.cn/index.html'],
      ['行情页', 'https://stockapp.finance.qq.com/mstats/'],
      ['stockbuddy 首页', 'https://stockbuddy.qq.com/'],
    ];
    for (const [tag, url] of candidates) {
      if (jar.get('zxg_openid') && jar.get('zxg_fskey')) break;
      await probe(tag, url);
    }
  }

  const pick = () => ({
    openid: jar.get('zxg_openid') || jar.get('w_openid') || '',
    fskey: jar.get('zxg_fskey') || jar.get('w_token') || '',
    check: jar.get('check') || (jar.get('w_openid') ? 'wx' : ''),
  });
  const s = pick();
  const gotZxg = !!(jar.get('zxg_openid') && jar.get('zxg_fskey'));
  log(`登录态: openid=${mask(s.openid)} fskey=${mask(s.fskey)} check=${mask(s.check)}${gotZxg ? '（zxg_* 已就绪）' : '（仅 w_*，consent 可能失败）'}`);
  if (!s.openid || !s.fskey) {
    throw new Error(`微信登录没有拿到 openid/fskey（现有 cookie: ${[...jar.keys()].join(',') || '无'}）`);
  }
  return s;
}

// ---------- consent → code → token ----------
async function consent(clientId, challenge, state, session) {
  const u = new URL(AUTHZ);
  u.searchParams.set('response_type', 'code');
  u.searchParams.set('client_id', clientId);
  u.searchParams.set('code_challenge', challenge);
  u.searchParams.set('code_challenge_method', 'S256');
  u.searchParams.set('state', state);
  u.searchParams.set('redirect_uri', REDIRECT_URI);
  u.searchParams.set('scope', 'read write');
  u.searchParams.set('openid', session.openid);
  u.searchParams.set('fskey', session.fskey);
  u.searchParams.set('check', session.check);
  u.searchParams.set('consent', '1');

  const res = await get(u.toString());
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* 非 JSON */ }
  if (!json) throw new Error(`consent 返回非 JSON（HTTP ${res.status}）: ${text.slice(0, 200)}`);
  if (json.error) {
    throw new Error(`consent 失败: ${json.error.error} ${json.error.error_description || ''}`.trim());
  }
  if (!json.code) throw new Error(`consent 没返回 code: ${JSON.stringify(json).slice(0, 200)}`);
  if (json.state && json.state !== state) throw new Error(`state 不匹配（返回 ${json.state}），丢弃`);
  log(`拿到授权码 ${mask(json.code)}`);
  return json.code;
}

async function exchangeToken(clientId, code, verifier) {
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    redirect_uri: REDIRECT_URI,
    client_id: clientId,
    code_verifier: verifier,
  });
  const res = await fetch(TOKEN, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': UA },
    body,
  });
  const json = await res.json().catch(() => null);
  if (!res.ok || !json?.access_token) {
    throw new Error(`换取 token 失败 HTTP ${res.status}: ${JSON.stringify(json).slice(0, 300)}`);
  }
  return json;
}

// ---------- 登录态三件套的缓存 ----------
// 关键：openid/fskey/check 是「腾讯账号登录态」，与 OAuth access_token 相互独立。
// 只要登录态还在，就能反复 consent 出新 code、换新 token，完全不需要再扫码。
// 所以扫码只在登录态过期时才发生，而不是每 4.17 天一次。
// 缓存「完整 cookie jar」而不是只存映射后的三件套（2026-10-30 踩坑后改）。
// 原因：consent 要 zxg_*，但换到 zxg_* 的第二次请求需要 w_r_token / w_openid / w_token / w_uin
// 这一整套 w_* 才能成立。只存三件套等于把重放所需的凭据丢了，登录态也就没法复用。
const SESSION_CACHE = arg('session-cache', `${process.env.HOME}/.config/stock-screener/wb_session.json`);
const SESSION_COOKIE_PREFIX = /^(zxg_|w_|check$)/;

function loadSession() {
  if (!existsSync(SESSION_CACHE)) return null;
  try {
    const s = JSON.parse(readFileSync(SESSION_CACHE, 'utf8'));
    if (s.openid && s.fskey && s.check) return s;
  } catch { /* 缓存坏了就走扫码 */ }
  return null;
}
function saveSession(s) {
  const cookies = {};
  for (const [k, v] of jar) if (SESSION_COOKIE_PREFIX.test(k)) cookies[k] = v;
  mkdirSync(dirname(SESSION_CACHE), { recursive: true });
  writeFileSync(SESSION_CACHE, JSON.stringify({ ...s, cookies, saved_at: new Date().toISOString() }, null, 2), { mode: 0o600 });
  log(`登录态已缓存: ${SESSION_CACHE}（cookie ${Object.keys(cookies).length} 个）`);
}
// 把缓存里的 cookie 灌回 jar，让「静默续期」能原样重放 consent 所需的那套凭据
function hydrateSession(s) {
  if (!s || !s.cookies) return;
  for (const [k, v] of Object.entries(s.cookies)) jar.set(k, v);
  log(`已从缓存恢复 cookie: ${Object.keys(s.cookies).join(',') || '(空)'}`);
}

// ---------- main ----------
// 全部流程包进 `main()`：**失败必须 exit 1**（`jobs/westock_renew.py` 按 returncode
// 判成败；顶层 await 抛错时 Node 的退出码依赖版本，不能靠它）。
async function main() {
guardInterval();
stampRun();

const clientId = await ensureClient();
const verifier = b64url(randomBytes(32));
const challenge = b64url(createHash('sha256').update(verifier).digest());
const state = b64url(randomBytes(16));

if (!WX_REDIRECT) {
  WX_REDIRECT = buildPanelRedirect(clientId, challenge, state);
  log(`扫码回调模式: panel（length=${WX_REDIRECT.length}）`);
}

// 先试静默续期：用缓存的登录态直接 consent。成功则全程无人工。
let session = loadSession();
let silent = false;
if (session) {
  log(`发现缓存登录态 openid=${mask(session.openid)}，尝试静默续期…`);
  try {
    const code = await consent(clientId, challenge, state, session);
    const tok = await exchangeToken(clientId, code, verifier);
    silent = true;
    writeEnv(tok);
    log('静默续期成功，本次没有任何人工动作');
  } catch (e) {
    log(`静默续期失败（${e.message}），降级为扫码`);
    session = null;
  }
}

if (!silent) {
  // 已经用别的方式（比如一个常驻的扫码页）拿到 wx_code 时，直接喂进来，不必再扫一次。
  // 这是风控友好的：同一次扫码的产物可以复用，不重复生成登录会话。
  const CODE_FILE = arg('wx-code-file', '');
  let wxCode = arg('wx-code', '') || (CODE_FILE && existsSync(CODE_FILE) ? readFileSync(CODE_FILE, 'utf8').trim() : '');
  if (wxCode) {
    log(`使用外部提供的 wx_code ${mask(wxCode)}，跳过扫码`);
  } else {
    // 复用缓存里的 uuid，避免手机还没扫完就重建登录会话（风控）
    let uuid = loadCachedQr();
    if (uuid) {
      log(`复用未过期的二维码 uuid = ${uuid}`);
      await fetchQrImage(uuid);
    } else {
      uuid = await newQr();
    }
    announceQr(uuid);

    let scan = await waitForScan(uuid);
    if (scan.expired) {
      log('二维码已过期，重建一次');
      clearCachedQr();
      uuid = await newQr();
      announceQr(uuid);
      scan = await waitForScan(uuid);
      if (scan.expired) throw new Error('二维码连续两次过期，先停下来，稍后重试（风控）');
    }
    if (scan.cancelled) throw new Error('你在手机上取消了授权；脚本按风控规则直接退出，不自动重试');
    clearCachedQr();
    log('已扫码并确认');
    wxCode = scan.code;
  }
  session = await exchangeWxCode(wxCode);
  saveSession(session);

  const code = await consent(clientId, challenge, state, session);
  const tok = await exchangeToken(clientId, code, verifier);
  writeEnv(tok);
}
}

main().catch((e) => {
  console.error(`授权失败: ${e && e.message ? e.message : e}`);
  process.exit(1);
});

function writeEnv(tok) {
  mkdirSync(dirname(OUT), { recursive: true });
  // **epoch 秒字符串**：`app_secret('westock_expires_at')` 与续期判断都用这个口径，
  // 写 ISO 串会在读取方的 `int()` 上炸。
  const expiresAt = Math.floor(Date.now() / 1000) + Number(tok.expires_in || 0);
  const content = [
    `WESTOCK_MCP_URL=${MCP_URL}`,
    `WESTOCK_ACCESS_TOKEN=${tok.access_token}`,
    `WESTOCK_EXPIRES_AT=${expiresAt}`,
  ].join('\n') + '\n';
  writeFileSync(OUT + '.tmp', content, { mode: 0o600 });
  writeFileSync(OUT, content, { mode: 0o600 });
  log(`token 已写入 ${OUT}`);
  log(`  长度 ${tok.access_token.length}  有效期 ${tok.expires_in}s  到期 ${expiresAt}（epoch 秒）`);
  log(`  refresh_token 是否等于 access_token: ${tok.refresh_token === tok.access_token}`);
}
