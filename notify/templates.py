"""推送模板（§7.2）。所有文案在这里生成，`Dispatcher` 只负责分级与落库。

三条固定口径：

1. **标题只有「谁 + 发生了什么」**：锁屏折叠时也要看懂。
2. **正文首行重复代码与名称**：通知栏折叠只显示两三行。
3. **必须带涨停价 + 距涨停 + 是否可买**：用户看到「突破」的第一反应是能不能买。

`click` 与 `view` 按钮一律断言 `https://`——ntfy 会把相对地址当成 topic 名，点开是 404。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from notify.hmac_sign import day_end_ts, sign


@dataclass(frozen=True)
class NtfyAction:
    """一个通知按钮（`X-Actions` 的一项）。刻意不叫 `Action`：除权事件结构完全不同。"""

    action: Literal["view", "http", "copy", "broadcast"]
    label: str
    url: str
    method: str | None = None
    clear: bool = False


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    priority: int = 3
    tags: list[str] = field(default_factory=list)
    click: str = ""
    actions: list[NtfyAction] = field(default_factory=list)
    markdown: bool = True
    seq_id: str = ""


@dataclass(frozen=True)
class SigRow:
    """一条可渲染的信号（`load_sig` 的产物）。"""

    id: int
    code6: str
    name: str
    industry: str | None
    kind: str
    level: str
    score: float
    price: float
    chg: float
    box_high: float
    box_low: float
    box_vol_multiple: float | None
    days: int
    b4: float | None
    up: float | None
    a_group_discounted: bool
    extended: bool
    panic: bool
    sealed: bool
    trade_date: int
    event_id: int
    signal_id: int | None
    seq: int
    event_trade_date: int
    code: str = ""                     # 原始带市场前缀的代码（sh600519）；URL 用它，展示用 code6


@dataclass(frozen=True)
class BriefRow:
    code6: str
    name: str
    price: float
    score: float
    chg: float


@dataclass(frozen=True)
class CloseStats:
    pushed: int
    suppressed: int
    signals_n: int
    by_kind: dict[str, int]
    by_level: dict[str, int]
    pool_sizes: dict[str, int]


_AUCTION_LABEL = {"high_open": "高开", "low_open": "低开", "auction_vol": "竞价放量"}


def _host(cfg) -> str:
    """配置里的主机名必须是 https 或裸域名（`http://` 配置要在断言里炸掉，不能悄悄补成 https）。"""
    host = str(cfg.notify.public_host).strip()
    if host.startswith("https://"):
        return host
    if "://" in host:
        return host                      # 别的协议原样返回，交给下面的 https:// 断言拦下
    return f"https://{host}"


def _seq_id(sig: SigRow) -> str:
    """推送序列号：`code + 事件日 + kind + seq`。**必须与 `dispatcher._seq_id` 同口径**。

    少了 `kind` 时，同一只票同一天首次突破与首次跌破都是 `seq=1`，两段会撞成同一个
    序列号——ntfy 把第二条当成第一条的**更新**直接吞掉；而且发出去的
    `X-Sequence-ID` 与 `push_log.seq_id` 对不上，事后无法按序列号对账。
    """
    return f"sig-{sig.code6}-{sig.event_trade_date}-{sig.kind}-{sig.seq}"


def _code_url(sig: SigRow) -> str:
    """URL 用带前缀的完整代码（api 侧路由要 `sh600519`，6 位会 404）。"""
    return sig.code or sig.code6


def _session_ticket(secret: bytes | None):
    """造一张网页访问票据，返回 `(token, exp_ts)`；没有密钥时返回 `(None, 0)`。

    有效期取 `notify.hmac_sign.session_days()`——与 api 发 Cookie 用的是**同一个**
    `SS_SESSION_DAYS`。上一版这里写死 30 天，改环境变量只影响 Cookie 不影响链接，
    RUNBOOK 说的「可改」等于没生效。
    """
    if secret is None:
        return None, 0
    import time as _time

    from notify.hmac_sign import session_days, sign

    exp_ts = int(_time.time() + session_days() * 24 * 3600)
    return sign(secret, "session", 0, exp_ts), exp_ts


def _link(cfg, path: str, secret: bytes | None = None) -> str:
    """拼通知里的绝对 https 链接；给了 `secret` 就带上免密票据。

    **票据放进 URL 是为了「从通知点开不用再输口令」**：手机浏览器不总记得
    HTTP Basic 凭据，一次推送弹一次原生登录框。服务端验票后发 Cookie 并 302
    把 `t`/`exp` 从地址栏抹掉，之后这个浏览器就不再问。
    直接敲域名进来没有票据，仍然走 Basic——这是本设计要保留的边界。
    """
    url = f"{_host(cfg)}{path}"
    assert url.startswith("https://"), f"通知里的链接必须是绝对 https 地址：{url}"
    token, exp_ts = _session_ticket(secret)
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}t={token}&exp={exp_ts}"
    return url


def _base_click(cfg, code6: str, secret: bytes | None = None) -> str:
    return _link(cfg, f"/stock/{code6}", secret)


def _limit_line(sig: SigRow) -> str | None:
    """涨停行三条文案（§7.2.2 的「已封 / 注意笼子 / 可买」）。"""
    if sig.up is None or not sig.price:
        return None
    if sig.price >= sig.up * 0.999:
        return f"涨停 {sig.up:.2f} ｜ 已封涨停，无法成交"
    gap = (sig.up - sig.price) / sig.price
    if 0 < gap < 0.01:
        return f"涨停 {sig.up:.2f} ｜ 距涨停 +{gap:.1%}，注意 2% 价格笼子"
    return f"涨停 {sig.up:.2f} ｜ 距涨停 +{gap:.1%}（可买）"


def render_breakout(sig: SigRow, secret: bytes, cfg) -> Message:
    flags = []
    if sig.sealed:
        flags.append("一字封板，无法成交，仅供知悉")
    if sig.extended:
        flags.append("已涨超 7%，勿追")
    title = f"🚨 {sig.name} 放量突破"
    if flags:
        title += "（" + "；".join(flags) + "）"

    lines = [
        f"{sig.code6} {sig.name}  **总分 {sig.score:.0f}**",
        f"现价 **{sig.price:.2f}**（{sig.chg:+.2f}%）",
        f"突破箱体上沿 {sig.box_high:.2f}，横盘放量倍数 **{sig.box_vol_multiple:.2f}×**"
        if sig.box_vol_multiple is not None else f"突破箱体上沿 {sig.box_high:.2f}",
        _summary_line(sig),
    ]
    limit = _limit_line(sig)
    if limit:
        lines.append(limit)
    if sig.sealed:
        lines.append("一字封板，无法成交，仅供知悉")
    if sig.a_group_discounted:
        lines.append("下跌段过短，A 组已折半，满分 85")

    return Message(
        title=title, body="\n".join(lines), priority=5 if sig.level == "P0" else 3,
        tags=["rotating_light"] if sig.level == "P0" else ["chart_with_upwards_trend"],
        click=_base_click(cfg, _code_url(sig), secret), actions=_actions(sig, secret, cfg),
        seq_id=_seq_id(sig),
    )


def render_breakdown(sig: SigRow, secret: bytes, cfg) -> Message:
    flags = []
    if sig.sealed:
        flags.append("一字跌停，无法成交")
    if sig.panic:
        flags.append("跌幅已超 7%，可能跌停")
    title = f"⚠️ {sig.name} 跌破箱体"
    if flags:
        title += "（" + "；".join(flags) + "）"

    lines = [
        f"{sig.code6} {sig.name}  **总分 {sig.score:.0f}**",
        f"现价 **{sig.price:.2f}**（{sig.chg:+.2f}%）",
        f"跌破箱体下沿 {sig.box_low:.2f}，横盘放量倍数 **{sig.box_vol_multiple:.2f}×**"
        if sig.box_vol_multiple is not None else f"跌破箱体下沿 {sig.box_low:.2f}",
        _summary_line(sig),
    ]
    return Message(
        title=title, body="\n".join(lines), priority=5, tags=["warning"],
        click=_base_click(cfg, _code_url(sig), secret), actions=_actions(sig, secret, cfg),
        seq_id=_seq_id(sig),
    )


def render_batch(items: list[SigRow], secret: bytes, cfg) -> Message:
    """P1 合并 / 板块级合并：一条 P2，正文一行一只。"""
    if not items:
        raise ValueError("合并批次不能为空")
    first = items[0]
    head = f"{first.industry or '同板块'} {len(items)} 只{first.kind}"
    # 计数行也进正文：push_log.body 是第一段原文，第二段拼解读要靠它，日志里也要能看出是几条合并
    lines = [f"{len(items)} 只同向异动"]
    lines += [f"{i.code6} {i.name} {i.price:.2f}（{i.chg:+.2f}%）{i.kind}" for i in items]
    seq_id = f"batch-{min(i.event_id for i in items)}"
    # 含 P0 的合并条必须还是 urgent：恒 priority=3 会让板块普跌日的合并条不响铃，
    # 「合并了但没人看」比不合并更糟
    priority = 5 if any(i.level == "P0" for i in items) else 3
    return Message(
        title=f"📋 {head}", body="\n".join(lines), priority=priority, tags=["memo"],
        # 合并条点进去看**列表**：单票页对「3 只同向」没有意义
        click=_link(cfg, "/signals", secret), actions=_actions(first, secret, cfg), seq_id=seq_id,
    )


# 竞价异常在开盘简报里最多列几条：正文（含池子 Top N）要稳在 ntfy 4KB 上限内
_AUCTION_MAX_LINES = 10
# **正文总长硬上限**（字节）：ntfy 超过约 4KB 会直接 400 拒收，而 open_brief 没有重试，
# 一次超长就等于当天整条简报丢失。竞价条数封顶只管住一处来源，Top N 那一块
# 由 `open_brief_top` 决定，调大或名字变长一样会超——所以再加一道总长度闸。
_BODY_MAX_BYTES = 3900


def render_open_brief(rows: list[BriefRow], n: int, secret: bytes, cfg, overflow=None,
                      pool_date=None, baseline_days=None,
                      auction_detections: list | None = None) -> Message:
    """开盘简报：池子 Top N（按总分 序号. 代码 名称  总分）+ 竞价异常（§7.2）。"""
    shown = rows[:n]
    # 调用方可能只传 Top N 行、把余下的放进 `overflow`：标题与「完整 N 只」都要是整池数
    total = len(rows) + int(overflow or 0)
    header = f"Top {n}（按总分）："

    # ---- 先把**尾部固定行**拼出来，量出它们占多少字节 ----
    # 顺序不能反：预算只扣 Top N 的话，尾部（竞价行 + 「另有」「完整」）会追加在
    # 预算之外。Top N 拉满 + 长股票名 + 多条长原因竞价异常时照样超限，被
    # ntfy 400 拒收（open_brief 无重试，当天整条丢失）。
    tail: list[str] = []
    if pool_date is not None:
        tail.append(f"池子日期 {pool_date}")
    if baseline_days is not None:
        # 局部变量！`total` 是整池只数（标题与「完整 N 只见网页」都用它），
        # 在这里覆盖会把 25 只的池子说成 20 只
        base_total = int(cfg.strategy.intraday.auction.baseline_days)
        tail.append(f"竞价基线 {baseline_days}/{base_total} 日"
                    + ("（未满，量能异常暂不判）" if baseline_days < base_total else ""))
    # **竞价异常必须截断**：448 条全塞进正文会让消息超 ntfy 上限被 400 拒收
    # （曾出现过整条开盘简报因超限被拒收且没有重试）。
    # 行情明细本来就该看网页，推送只留头部若干条。
    auction_shown = auction_detections or []
    auction_total = len(auction_shown)
    for det in auction_shown[:_AUCTION_MAX_LINES]:
        raw = det.payload.get("reason") if isinstance(det.payload, dict) else None
        if raw:
            reason = "、".join(_AUCTION_LABEL.get(r, r) for r in str(raw).split("+"))
            price = det.payload.get("chg")
            suffix = f"（{price:+.1%}）" if isinstance(price, (int, float)) else ""
            tail.append(f"竞价 · {det.code} {reason}{suffix}")
    if auction_total > _AUCTION_MAX_LINES:
        tail.append(f"竞价异常另有 {auction_total - _AUCTION_MAX_LINES} 条见网页")
    if overflow:
        tail.append(f"另有 {overflow} 只未列出")
    tail.append(f"完整 {total} 只见网页")

    def _bytes(ls: list[str]) -> int:
        return sum(len(x.encode("utf-8")) + 1 for x in ls)

    # 「只列前 K 条」那句提示也要占位：按最坏长度预留，免得加完又超
    note_reserve = len(f"（Top {n} 只列前 {len(shown)} 条，完整见网页）".encode("utf-8")) + 1
    budget = _BODY_MAX_BYTES - _bytes([header]) - _bytes(tail) - note_reserve

    lines = [header]
    shown_n = 0
    for i, r in enumerate(shown, 1):
        line = f"{i}. {r.code6} {r.name}  {r.score:.0f}"
        need = len(line.encode("utf-8")) + 1
        if need > budget:
            break
        budget -= need
        lines.append(line)
        shown_n += 1
    if shown_n < len(shown):
        lines.append(f"（Top {n} 只列前 {shown_n} 条，完整见网页）")
    lines += tail

    trade_date = pool_date or 0
    return Message(
        title=f"📋 今日观察池 {total} 只", body="\n".join(lines), priority=4, tags=["memo"],
        click=_link(cfg, "/pool", secret), seq_id=f"open-{trade_date}",
    )


def render_close_digest(stats: CloseStats, cfg, trade_date: int = 0,
                        secret: bytes | None = None) -> Message:
    body = [
        f"推送 {stats.pushed} 条 ｜ 压制 {stats.suppressed} 条 ｜ 信号 {stats.signals_n} 条",
        " · ".join(f"{k} {v}" for k, v in sorted(stats.by_kind.items())) or "无",
        "池子：" + ("、".join(f"{k} {v}" for k, v in sorted(stats.pool_sizes.items())) or "无"),
    ]
    # **图标只留一处**：标题不写 emoji，交给 `tags` 渲染。
    # 两边都写时手机端显示两个图标（实机验证：两个夕阳）。
    return Message(title="收盘统计", body="\n".join(body), priority=3, tags=["city_sunset"],
                   click=_link(cfg, "/", secret), seq_id=f"close-{trade_date}")


_ALERT_PRIORITY = {"P0": 5, "P1": 4, "P2": 3}


#: 告警类型 → 人读标题。**不能直接把 kind 当标题**：手机上收到 "agent_churn"
#: 等于没收到——那是给代码看的标识符，不是给人看的。
_ALERT_TITLE = {
    "agent_down": "agent 进程无响应",
    "agent_stalled": "agent 调度卡死",
    "agent_churn": "agent 空转死循环",
    "jobs_down": "盘中监控已停止",
    "llm_down": "LLM 连续不可用",
    "push_channel_down": "推送通道不可用",
    "budget_exhausted": "agent 日预算已用完",
}


def render_alert(kind: str, text: str, cfg, level: str = "P1",
                 secret: bytes | None = None) -> Message:
    """系统告警（§7.2）：P0 → 5 urgent（watchdog 的 agent 停止必须响铃），P1 → 4，P2 → 3。"""
    # 同上：图标交给 `tags`，标题只放文字
    return Message(title=_ALERT_TITLE.get(kind, kind), body=text,
                   priority=_ALERT_PRIORITY[level], tags=["bell"],
                   click=_link(cfg, "/", secret))


def render_confirm_card(turn_id: int, action_desc: str, host: str, secret: bytes, cfg,
                        exp_ts: int) -> Message:
    """确认卡（D21）：两个按钮，票据绑 purpose='confirm' 与绝对过期时刻。"""
    base = host if host.startswith("https://") else f"https://{host}"
    token = sign(secret, "confirm", turn_id, exp_ts)
    confirm_url = f"{base}/api/hook/agent/{turn_id}/confirm?t={token}&exp={exp_ts}"
    ignore_url = f"{base}/agent-log"
    assert confirm_url.startswith("https://") and ignore_url.startswith("https://")
    return Message(
        title="需要确认", body=action_desc, priority=3, tags=["question"],
        click=ignore_url,
        actions=[NtfyAction("http", "确认执行", confirm_url, method="POST"),
                 NtfyAction("view", "忽略", ignore_url)],
        seq_id=f"confirm-{turn_id}",
    )


def _summary_line(sig: SigRow) -> str:
    parts = [f"横盘 {sig.days} 日"]
    if sig.b4 is not None:
        parts.append(f"缩量 {sig.b4:.2f}")
    if sig.box_low:
        parts.append(f"距下沿 {(sig.price / sig.box_low - 1):+.1%}")
    return " ｜ ".join(parts)


def _actions(sig: SigRow, secret: bytes, cfg) -> list[NtfyAction]:
    token = sign(secret, "read", sig.id, day_end_ts(sig.event_trade_date))
    return [
        NtfyAction("view", "看详情", _base_click(cfg, _code_url(sig), secret)),
        NtfyAction("view", "K 线", _link(cfg, f"/stock/{_code_url(sig)}?tab=k", secret)),
        NtfyAction("http", "标记已读", _link(cfg, f"/api/hook/signals/{sig.id}/read?t={token}"),
                   method="POST"),
    ]
