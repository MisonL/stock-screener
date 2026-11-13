"""Task 6：确认卡回调（`POST /api/hook/agent/{turn_id}/confirm`）。

Step 1 清单里属于本文件的条目：
① 验签失败 / 过期 → 403 **且不改库、不执行写**；
② `sign(secret, "read", …)` 生成的「标记已读」票据打 confirm → 403（purpose 绑定）；
③ `pending_status != 'pending'` 的重复点击 → 403 且不重复执行；
④ 真点一次 → `WestockMcp.set_alert` 被调、`pending_status='confirmed'`、
   `push_log` 多一条 `kind='confirm_result'` 的审计行；
⑤ 第 ④ 步执行失败 → `pending_status` 回滚成 `pending` + 500 + 一条告警；
⑥ `confirm_result` **不是推送**：`/api/status` 的 `notify_ok` 推导必须排除它。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from api.routes import create_app
from core.clock import FixedClock
from notify.hmac_sign import ensure_secret, sign
from tests.helpers import SH

T0 = datetime(2026, 10, 30, 10, 0, tzinfo=SH)
TURN = 7
DAY = 20261030


class FakeMcp:
    """只记录调用；`ok=False` 或 `exc` 用来验证回滚路径。"""

    def __init__(self, ok=True, exc=None):
        self.ok = ok
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    def _rec(self, name, kw):
        self.calls.append((name, kw))
        if self.exc is not None:
            raise self.exc
        return {"ok": self.ok, "data": {"done": True}, "as_of": T0.isoformat(),
                "reason": None if self.ok else "mcp_error:upstream",
                "adjust": None}

    def add_watch(self, **kw):
        return self._rec("add_watch", kw)

    def remove_watch(self, **kw):
        return self._rec("remove_watch", kw)

    def set_alert(self, **kw):
        return self._rec("set_alert", kw)

    def cancel_alert(self, **kw):
        return self._rec("cancel_alert", kw)


class Disp:
    def __init__(self):
        self.alerts: list[tuple[str, str, str]] = []

    def alert(self, kind, text, level="P1"):
        self.alerts.append((kind, text, level))


@pytest.fixture
def mcp():
    return FakeMcp()


@pytest.fixture
def disp():
    return Disp()


@pytest.fixture
def api(tmp_db, CFG, mcp, disp):
    app = create_app(CFG, lambda: tmp_db, FixedClock(T0), dispatcher=disp, mcp=mcp)
    app.state.conn = tmp_db

    def make():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    make.app = app
    make.mcp = mcp
    make.disp = disp
    make.conn = tmp_db
    return make


def seed_turn(conn, turn_id=TURN, tool="mcp_set_alert", args=None, status="pending",
              card=True):
    action = {"tool": tool, "args": args if args is not None else {
        "code": "sh600519", "kind": "up", "threshold": 1200}}
    conn.execute(
        "INSERT INTO agent_turn(id, ts, wake_kind, context_digest, rounds, decision, fallback,"
        " market, pending_action, pending_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (turn_id, T0.isoformat(), "chat", "chat 写操作 " + tool, 1, "speak", 0, "cn_main",
         json.dumps(action, ensure_ascii=False), status))
    if card:
        conn.execute(
            "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, trade_date, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (T0.isoformat(), "sh600519", "confirm_card", "P2", f"confirm-{turn_id}", 1,
             "设置提醒 sh600519", DAY, "accepted"))
    conn.commit()


def ticket(conn, turn_id=TURN, purpose="confirm", exp=None):
    exp = int(T0.timestamp()) + 3600 if exp is None else exp
    token = sign(ensure_secret(conn), purpose, turn_id, exp)
    return f"/api/hook/agent/{turn_id}/confirm?t={token}&exp={exp}"


def post(api, url):
    async def run():
        async with api() as client:
            return await client.post(url)

    return asyncio.run(run())


def pending_status(conn, turn_id=TURN):
    return conn.execute("SELECT pending_status FROM agent_turn WHERE id=?", (turn_id,)).fetchone()[0]


def test_read_ticket_cannot_be_replayed_on_confirm(api, tmp_db, mcp):
    """同一张 `read` 票据改投 confirm 端点必须 403（purpose 绑定，spec §7.2.6）。"""
    seed_turn(tmp_db)
    url = ticket(tmp_db, purpose="read")
    r = post(api, url)
    assert r.status_code == 403
    assert mcp.calls == []
    assert pending_status(tmp_db) == "pending"


def test_expired_ticket_403_and_does_not_write(api, tmp_db, mcp):
    seed_turn(tmp_db)
    url = ticket(tmp_db, exp=int(T0.timestamp()) - 1)      # 签名对、但已过期
    r = post(api, url)
    assert r.status_code == 403
    assert mcp.calls == []
    assert pending_status(tmp_db) == "pending"


def test_tampered_ticket_403(api, tmp_db, mcp):
    seed_turn(tmp_db)
    good = ticket(tmp_db)
    r = post(api, good[:-4] + "0000")
    assert r.status_code == 403
    assert mcp.calls == []


def test_unknown_turn_without_confirm_card_404(api, tmp_db, mcp):
    """没有确认卡就说明不是我们发的卡片：不执行（即使签名对）。"""
    seed_turn(tmp_db, card=False)
    r = post(api, ticket(tmp_db))
    assert r.status_code == 404
    assert mcp.calls == []


def test_confirm_executes_write_and_marks_confirmed(api, tmp_db, mcp):
    seed_turn(tmp_db)
    r = post(api, ticket(tmp_db))
    assert r.status_code == 200, r.text
    assert [name for name, _ in mcp.calls] == ["set_alert"]
    assert mcp.calls[0][1] == {"code": "sh600519", "kind": "up", "threshold": 1200}
    assert pending_status(tmp_db) == "confirmed"


def test_duplicate_click_403_and_executes_once(api, tmp_db, mcp):
    seed_turn(tmp_db)
    url = ticket(tmp_db)
    assert post(api, url).status_code == 200
    r = post(api, url)
    assert r.status_code == 403
    assert len(mcp.calls) == 1
    assert pending_status(tmp_db) == "confirmed"


def test_expired_pending_card_403(api, tmp_db, mcp):
    """主循环把超过 24h 的卡标成 `expired` 后，任何回调都不能再执行。"""
    seed_turn(tmp_db, status="expired")
    r = post(api, ticket(tmp_db))
    assert r.status_code == 403
    assert mcp.calls == []


def test_write_failure_rolls_back_and_alerts(api, tmp_db, disp):
    api.mcp.ok = False
    seed_turn(tmp_db)
    r = post(api, ticket(tmp_db))
    assert r.status_code == 500
    assert pending_status(tmp_db) == "pending"             # 回滚，用户还能再点一次
    assert len(disp.alerts) == 1 and disp.alerts[0][2] in ("P1", "P0")


def test_write_exception_rolls_back_and_alerts(api, tmp_db, disp):
    api.mcp.exc = RuntimeError("socket closed")
    seed_turn(tmp_db)
    r = post(api, ticket(tmp_db))
    assert r.status_code == 500
    assert pending_status(tmp_db) == "pending"
    assert len(disp.alerts) == 1


def test_unknown_tool_in_pending_action_is_rejected(api, tmp_db, mcp, disp):
    """`pending_action` 是库里的数据，不能当任意方法名的入口。"""
    seed_turn(tmp_db, tool="system")
    r = post(api, ticket(tmp_db))
    assert r.status_code == 500
    assert mcp.calls == []
    assert pending_status(tmp_db) == "pending"
    assert len(disp.alerts) == 1


def test_confirm_result_audit_row(api, tmp_db):
    seed_turn(tmp_db)
    assert post(api, ticket(tmp_db)).status_code == 200
    row = tmp_db.execute(
        "SELECT ts, code, kind, level, seq_id, segment, body, trade_date, status"
        " FROM push_log WHERE kind='confirm_result'").fetchone()
    assert row is not None, "确认回调没有写审计行"
    ts, code, kind, level, seq_id, segment, body, trade_date, status = row
    assert level == "P2" and seq_id == f"confirm-result-{TURN}" and segment == 1
    assert trade_date == DAY and status == "accepted"
    assert body and isinstance(body, str)
    assert code == "sh600519"


def test_confirm_result_does_not_flip_notify_ok(api, tmp_db):
    """`confirm_result` 不是推送：ntfy 全挂时点一次确认不能把「推送通道异常」翻回绿。"""
    tmp_db.execute(
        "INSERT INTO push_log(ts, code, kind, level, seq_id, segment, body, trade_date, status)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        # 时间晚于确认卡（T0）：通道故障是**最新**事件，之后只有那条审计行
        ("2026-10-30T23:00:00+08:00", None, "push_channel_down", "P1", "down-1", 1,
         "ntfy 403", DAY, "failed"))
    tmp_db.commit()
    # 确认卡必须存在（端点靠 push_log.seq_id='confirm-{turn_id}' 反查 turn）；
    # 它能成立是因为 `last_ok` 把 kind='confirm_result' 排除了
    seed_turn(tmp_db)
    assert post(api, ticket(tmp_db)).status_code == 200

    async def run():
        async with api() as client:
            return (await client.get("/api/status")).json()

    body = asyncio.run(run())
    assert body["notify_ok"] is False
    assert body["notify_last_error"] == "ntfy 403"
