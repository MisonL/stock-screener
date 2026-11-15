"""模板渲染、HMAC 票据与 ntfy 客户端。"""
import copy
import re
from datetime import datetime
from pathlib import Path

import pytest

from core.config import load_config
from notify.hmac_sign import day_end_ts, ensure_secret, is_expired, sign, verify
from notify.ntfy import NtfyClient, actions_header
from notify.templates import (BriefRow, NtfyAction, SigRow, render_breakdown, render_breakout,
                              render_close_digest, render_confirm_card, render_open_brief)
from tests.helpers import SH

CFG = load_config(Path("config"))
SECRET = b"k" * 32


def sig(**kw):
    base = dict(id=1, code6="600519", name="贵州茅台", industry="白酒", kind="breakout", level="P0",
                score=86.0, price=32.90, chg=3.24, box_high=32.74, box_low=30.21,
                box_vol_multiple=1.82, days=23, b4=0.41, up=33.10, a_group_discounted=False,
                extended=False, panic=False, sealed=False, trade_date=20261029, event_id=1,
                code="sh600519", signal_id=1, seq=1, event_trade_date=20261030)
    if "code" in kw:                                     # 测试习惯写 code="sh600519"
        full = kw.pop("code")
        base["code"] = full
        base["code6"] = full[2:] if len(full) > 6 else full
    base.update(kw)
    return SigRow(**base)


# ---- 突破模板（与 spec §7.2.2 示例逐字一致）-------------------------------

def _path_of(url: str) -> str:
    """取 URL 的 scheme://host/path，丢掉查询串。

    通知里的链接现在会带免密票据 `?t=..&exp=..`（点开不用再登录），
    票据是动态的，断言不该写死它。票据本身另有专门测试。
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def test_breakout_text_exact():
    m = render_breakout(sig(), SECRET, CFG)
    assert m.title == "🚨 贵州茅台 放量突破" and m.priority == 5
    assert m.body == (
        "600519 贵州茅台  **总分 86**\n现价 **32.90**（+3.24%）\n"
        "突破箱体上沿 32.74，横盘放量倍数 **1.82×**\n横盘 23 日 ｜ 缩量 0.41 ｜ 距下沿 +8.9%\n"
        "涨停 33.10 ｜ 距涨停 +0.6%，注意 2% 价格笼子")


def test_breakout_sealed_text():
    m = render_breakout(sig(sealed=True), SECRET, CFG)
    assert "一字封板" in m.title and "无法成交" in m.body


def test_breakout_extended_text():
    m = render_breakout(sig(extended=True, level="P2"), SECRET, CFG)
    assert "已涨超 7%" in m.title and m.priority == 3


def test_discounted_text_in_push():                     # F29
    assert "已折半" in render_breakout(sig(a_group_discounted=True), SECRET, CFG).body


def test_limit_line_three_modes():
    at_limit = render_breakout(sig(price=33.08, up=33.10), SECRET, CFG).body
    assert "已封涨停，无法成交" in at_limit
    near = render_breakout(sig(price=32.90, up=33.10), SECRET, CFG).body
    assert "注意 2% 价格笼子" in near
    far = render_breakout(sig(price=30.00, up=33.10), SECRET, CFG).body
    assert "可买" in far


def test_no_limit_row_when_up_missing():
    assert "涨停" not in render_breakout(sig(up=None), SECRET, CFG).body


def test_click_must_be_absolute():
    from types import SimpleNamespace

    # Config 是只读的（FrozenDict），这里给模板造一个最小的 notify 段
    cfg = SimpleNamespace(notify=SimpleNamespace(public_host="http://stock.example.com"))
    with pytest.raises(AssertionError):
        render_breakout(sig(), SECRET, cfg)


def test_hook_url_signed():
    m = render_breakout(sig(id=7), SECRET, CFG)
    token = sign(SECRET, "read", 7, day_end_ts(20261030))
    assert f"/api/hook/signals/7/read?t={token}" in m.actions[2].url
    assert m.actions[2].method == "POST"


def test_breakdown_flags():
    m = render_breakdown(sig(kind="breakdown", panic=True, price=28.00, chg=-8.0), SECRET, CFG)
    assert "跌破箱体" in m.title and "跌幅已超 7%" in m.title


# ---- 票据 ----------------------------------------------------------------

def test_hmac_purpose_and_expiry():
    exp = day_end_ts(20261030)
    t = sign(SECRET, "read", 7, exp)
    assert verify(SECRET, "read", 7, exp, t)
    assert not verify(SECRET, "confirm", 7, exp, t)          # purpose 不可互换
    assert not verify(SECRET, "read", 7, exp + 86400, t)     # exp 也在签名原文里
    assert not verify(SECRET, "read", 8, exp, t)


def test_expiry_is_separate_check():
    exp = 1000
    assert is_expired(exp, now=1001) and not is_expired(exp, now=999)


def test_secret_persists_across_restart(tmp_db):
    a = ensure_secret(tmp_db)
    b = ensure_secret(tmp_db)
    assert a == b and len(a) == 32


def test_confirm_card_rendered():                          # D21
    exp = 4102444800
    m = render_confirm_card(turn_id=42, action_desc="加入自选 sh600519", host="stock.x.com",
                            secret=SECRET, cfg=CFG, exp_ts=exp)
    assert m.title == "需要确认"
    assert m.actions[0].url.startswith("https://stock.x.com/api/hook/agent/42/confirm?t=")
    assert f"t={sign(SECRET, 'confirm', 42, exp)}" in m.actions[0].url
    assert f"&exp={exp}" in m.actions[0].url


# ---- 简报 / 统计 ----------------------------------------------------------

def test_open_brief_shows_baseline_days():                 # F28
    rows = [BriefRow(f"60051{i}", f"票{i}", 10.0 + i, 90 - i, 1.0) for i in range(15)]
    m = render_open_brief(rows, n=15, secret=SECRET, cfg=CFG, pool_date=20261030, baseline_days=7)
    assert "20261030" in m.body and "7/20" in m.body


def test_open_brief_renders_auction_detections():
    from core.intraday.rules import Detection

    rows = [BriefRow("600519", "茅台", 10.0, 90, 1.0)]
    det = Detection("auction_anomaly", "sh600519", {"reason": "high_open", "chg": 0.03, "baseline_days": 20})
    m = render_open_brief(rows, n=10, secret=SECRET, cfg=CFG, auction_detections=[det])
    assert "高开" in m.body


def test_close_digest_structure():
    from notify.templates import CloseStats

    m = render_close_digest(CloseStats(3, 2, 5, {"breakout": 5}, {"P0": 2}, {"cn_main": 100}), CFG, 20261030)
    assert "推送 3" in m.body and "压制 2" in m.body


# ---- ntfy 客户端 ----------------------------------------------------------

def test_actions_header_separators():
    header = actions_header([NtfyAction("view", "看详情", "https://a/x"),
                             NtfyAction("http", "标记已读", "https://a/y", method="POST")])
    assert header == "action=view,label=看详情,url=https://a/x;action=http,label=标记已读,url=https://a/y,method=POST"


def test_label_commas_are_neutralized():
    header = actions_header([NtfyAction("view", "看,详;情", "https://a/x")])
    assert header.count(";") == 0 and "," in header


def test_publish_uses_headers(monkeypatch):
    import httpx

    from notify.templates import Message

    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "abc"})

    client = NtfyClient("http://ntfy:2586", "stock-alerts", "tok",
                        http=httpx.Client(transport=httpx.MockTransport(handler)))
    msg = Message(title="标题", body="正文", priority=5, tags=["bell"], click="https://a/x",
                  actions=[NtfyAction("view", "看详情", "https://a/x")], seq_id="sig-1")
    assert client.publish(msg) == "abc"
    h = {k.lower(): (v.decode() if isinstance(v, bytes) else v) for k, v in seen["headers"].items()}
    assert h["x-title"] == "标题" and h["x-priority"] == "5"
    assert h["x-sequence-id"] == "sig-1" and h["authorization"] == "Bearer tok"
    assert "x-actions" in h and "x-message-id" not in h
    assert seen["body"] == "正文".encode()


# ---- §7.2 修正：合并条优先级 / 告警映射 / 简报文案 / 链接用完整代码 ----------

def test_batch_priority_is_group_max():
    """合并条 priority 取组内最高：含 P0 → 5，全 P1 → 3（不是恒 3）。"""
    from notify.templates import render_batch

    p1s = [sig(level="P1", event_id=1), sig(level="P1", event_id=2, code6="600000")]
    assert render_batch(p1s, SECRET, CFG).priority == 3
    mixed = [sig(level="P1", event_id=1), sig(level="P0", event_id=2, code6="600000")]
    assert render_batch(mixed, SECRET, CFG).priority == 5


def test_alert_priority_mapping():
    """系统告警：P0 → 5 urgent / P1 → 4 / P2 → 3（watchdog 的 P0 曾发成 4，不响铃）。"""
    from notify.templates import render_alert

    assert render_alert("watchdog", "agent 停止", CFG, level="P0").priority == 5
    assert render_alert("watchdog", "数据源失效", CFG, level="P1").priority == 4
    assert render_alert("watchdog", "池子异常", CFG, level="P2").priority == 3


def test_open_brief_matches_spec():
    """§7.2 开盘简报：priority 4 / click=/pool / 标题「今日观察池」/ Top 15 文案逐字。"""
    rows = [BriefRow(f"6005{i:02d}", f"票{i}", 10.0, 92 - i, 1.0) for i in range(15)]
    m = render_open_brief(rows, n=15, secret=SECRET, cfg=CFG, pool_date=20261030)
    assert m.priority == 4
    assert _path_of(m.click) == f"https://{CFG.notify.public_host}/pool"
    assert m.title == "📋 今日观察池 15 只"
    head, first = m.body.splitlines()[:2]
    assert head == "Top 15（按总分）："
    assert first == "1. 600500 票0  92"


def test_open_brief_footer_counts_whole_pool():
    """「完整 N 只见网页」里的 N 是整个池子，不是列出来的 Top N。"""
    rows = [BriefRow(f"6005{i:02d}", f"票{i}", 10.0, 92 - i, 1.0) for i in range(20)]
    m = render_open_brief(rows, n=15, secret=SECRET, cfg=CFG, pool_date=20261030)
    assert m.title == "📋 今日观察池 20 只"
    assert m.body.splitlines()[-1] == "完整 20 只见网页"
    assert sum(1 for ln in m.body.splitlines() if re.match(r"^\d+\. ", ln)) == 15


def test_click_uses_prefixed_code():
    """`/stock/600519` 在 api 侧 404（要 `sh600519`）；展示文案仍是 6 位。"""
    m = render_breakout(sig(code="sh600519"), SECRET, CFG)
    host = f"https://{CFG.notify.public_host}"
    assert _path_of(m.click) == f"{host}/stock/sh600519"
    assert m.body.splitlines()[0].startswith("600519 贵州茅台")
    assert _path_of(m.actions[0].url) == f"{host}/stock/sh600519"
    # K 线链接的 `tab=k` 是业务参数，不能被票据顶掉：两者必须同时存在
    from urllib.parse import parse_qs, urlsplit

    kline = urlsplit(m.actions[1].url)
    assert f"{kline.scheme}://{kline.netloc}{kline.path}" == f"{host}/stock/sh600519"
    kq = parse_qs(kline.query)
    assert kq.get("tab") == ["k"], "tab=k 丢了（点进去会落到默认页签）"
    assert kq.get("t") and kq.get("exp"), "票据没带上，同一条通知里的 K 线按钮还要再登录"


def test_breakdown_and_batch_click_use_prefixed_code():
    from notify.templates import render_batch

    bd = render_breakdown(sig(kind="breakdown", code="sz000001"), SECRET, CFG)
    assert _path_of(bd.click) == f"https://{CFG.notify.public_host}/stock/sz000001"
    # 合并条是「N 只同向」的列表提醒，点进去应是 /signals 而不是首只票的详情页
    bat = render_batch([sig(code="sh600519"), sig(event_id=2, code="sh600000")], SECRET, CFG)
    assert _path_of(bat.click) == f"https://{CFG.notify.public_host}/signals"


def test_open_brief_total_counts_truncated_rows():
    """jobs 只传 Top N 行、其余放进 overflow：标题与「完整 N 只」必须是整池数。"""
    rows = [BriefRow(f"6005{i:02d}", f"票{i}", 10.0, 92 - i, 1.0) for i in range(15)]
    m = render_open_brief(rows, n=15, secret=SECRET, cfg=CFG, pool_date=20261030, overflow=85)
    assert m.title == "📋 今日观察池 100 只"
    assert m.body.splitlines()[-1] == "完整 100 只见网页"
    assert "另有 85 只未列出" in m.body


# ---- 标题与 tags 不能都带图标（手机上会渲染成两个图标）------------------

# ntfy 客户端把 `tags` 渲染成图标、再原样显示标题。两边都带就是两张图——
# 实机验证：`title="🌇 收盘统计"` + `tags=["city_sunset"]` → 手机显示两个夕阳。


def test_close_digest_does_not_duplicate_icon():
    """收盘统计：标题带 🌇 就不要再给 `city_sunset` tag。"""
    from notify.templates import CloseStats, render_close_digest

    m = render_close_digest(CloseStats(0, 0, 0, {}, {}, {}), CFG, 20261030)
    assert "🌇" not in m.title or not m.tags, (
        f"标题 {m.title!r} 带 emoji，tags={m.tags} 又会渲染一个图标")


def test_alert_does_not_duplicate_icon():
    """告警：同理，标题与 tag 只能有一处出图标。"""
    from notify.templates import render_alert

    m = render_alert("涨停", "600519 涨停", CFG, "P1")
    emoji = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
    assert not (emoji.search(m.title) and m.tags), (
        f"标题 {m.title!r} 带 emoji，tags={m.tags} 又会渲染一个图标")


def test_open_brief_caps_auction_lines():
    """竞价异常要**截断**，不能 448 条全塞进一条消息。

    实况：几百条竞价异常全进正文 → 四百多行 / 一万多字符，
    ntfy 直接 400 拒收，开盘简报**当天整条丢失**（无重试）。行情异常本来就
    该在网页看，推送里只留最有信息量的头部。
    """
    from notify.templates import render_open_brief, BriefRow
    from types import SimpleNamespace

    cfg = CFG
    rows = [BriefRow(code6="600519", name="贵州茅台", price=1700.0, score=87.0, chg=0.01)]
    dets = [SimpleNamespace(code=f"sz{i:06d}", payload={"reason": "low_open", "chg": -0.03})
            for i in range(448)]

    m = render_open_brief(rows, 10, b"k" * 16, cfg, pool_date=20261110,
                          baseline_days=20, auction_detections=dets)
    n_lines = len(m.body.splitlines())
    assert n_lines < 60, f"正文 {n_lines} 行，竞价异常没有截断"
    # 编码后必须能被 ntfy 接受（实测上限约 4KB）
    assert len(m.body.encode("utf-8")) < 4000, \
        f"正文 {len(m.body.encode('utf-8'))} 字节，超过 ntfy 消息上限"
    assert "另有" in m.body, "截断后要说明还有多少条没列"


def test_open_brief_stays_under_ntfy_limit_with_long_pool():
    """Top N 很长时正文也不能超 ntfy 上限——要有**总长度**兜底，不只是竞价条数封顶。

    竞价异常截断到 10 条之后，正文剩下的长度由 `open_brief_top` 决定。
    配置改成 100、股票名很长（含 ST/*ST/科创），10 条竞价 + 100 行池子仍可能超 4KB，
    那时同样会被 ntfy 400 拒收，开盘简报整天丢失且无重试。
    """
    from notify.templates import render_open_brief, BriefRow

    n = 100
    rows = [BriefRow(code6=f"60{i:04d}", name="某某某某某某科技股份有限公司",
                     price=1700.0 + i, score=87.0, chg=0.01) for i in range(n)]
    m = render_open_brief(rows, n, b"k" * 16, CFG, pool_date=20261110, baseline_days=20)
    size = len(m.body.encode("utf-8"))
    assert size < 4000, f"正文 {size} 字节，超过 ntfy 消息上限"


def test_open_brief_total_bytes_covers_tail_lines():
    """字节预算必须覆盖**整条正文**，不只是 Top N 那一段。

    竞价异常截断到 10 条后，那 10 行 + 「另有 N 只」+「完整 N 只见网页」都追加在
    Top N 预算**之外**。实测最坏情况：Top 100 + 超长股票名 + 10 条长原因竞价异常
    → 4150 字节，仍会被 ntfy 400 拒收（而 open_brief 无重试，当天整条丢失）。
    """
    from types import SimpleNamespace
    from notify.templates import render_open_brief, BriefRow

    rows = [BriefRow(code6=f"6{i:05d}", name="某某某某某某某某科技股份有限公司",
                     price=100.0, score=80.0, chg=0.01) for i in range(500)]
    dets = [SimpleNamespace(code=f"sz{i:06d}",
                            payload={"reason": "low_open+vol_spike", "chg": -0.0999})
            for i in range(500)]
    for n in (15, 100, 500):
        m = render_open_brief(rows, n, b"k" * 16, CFG, overflow=178, pool_date=20261110,
                              baseline_days=30, auction_detections=dets)
        size = len(m.body.encode("utf-8"))
        assert size < 4000, f"top_n={n} 正文 {size} 字节，超过 ntfy 上限"


def test_template_seq_id_includes_kind_like_dispatcher():
    """`templates._seq_id` 必须与 `dispatcher._seq_id` **同口径**（含 kind）。

    发出去的 `X-Sequence-ID` 与 `push_log.seq_id` 不一致时，事后按序列号对账对不上；
    更要紧的是：同一只票同一天首次突破与首次跌破都是 `seq=1`，少了 `kind` 两段会
    撞成同一个序列号，ntfy 把第二条当成第一条的**更新**，直接吞掉。
    """
    from dataclasses import replace
    from notify.templates import _seq_id as t_seq
    from notify.dispatcher import _seq_id as d_seq

    one = sig(kind="breakdown", seq=1)
    assert t_seq(one) == d_seq(one), (
        f"两处 seq_id 不一致：templates={t_seq(one)} dispatcher={d_seq(one)}")
    # 同票同序不同 kind 必须区分开
    other = replace(one, kind="breakout")
    assert t_seq(other) != t_seq(one), "同票同 seq 的 breakout/breakdown 撞号了"
