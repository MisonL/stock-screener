"""口令失败限流（`api/auth.py`）。

**为什么需要**：没有限流时外网可以无限次猜口令——实测连打 10 次错误口令全是 401、
没有任何延迟。口令是人起的一串字，被慢慢穷举只是时间问题；一旦猜中，
`/api/chat` 会花 LLM 预算、`/api/hook/*` 能改状态。

**为什么按 IP 分桶而不是全局计数**：全局限流等于给了攻击者一个免费的拒绝服务手段
（打 10 次就能把主人锁在门外）。而且请求经 cloudflared 进来，
`request.client.host` 永远是隧道容器地址——用它当键正好退化成全局桶，
所以必须优先读 `CF-Connecting-IP`。
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from api import auth


def _cred(user: str, pw: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def passwd(monkeypatch):
    monkeypatch.setenv("SS_WEB_USER", "u")
    monkeypatch.setenv("SS_WEB_PASS", "p")


def _get(api, headers: dict[str, str] | None = None, path: str = "/api/status"):
    async def run():
        async with api() as client:
            return await client.get(path, headers=headers or {})

    return asyncio.run(run())


def test_repeated_failures_get_throttled(api, tmp_db, passwd):
    """连续失败到阈值后必须返回 429，而不是继续给 401。"""
    for i in range(auth.FAIL_MAX - 1):
        assert _get(api, _cred("u", f"bad{i}")).status_code == 401
    # 第 FAIL_MAX 次：**这一次就回 429**。原先"第 10 次给 401、第 11 次才 429"
    # 让封锁在统计上永远晚一轮，也让攻击者多拿到一次尝试机会。
    assert _get(api, _cred("u", "bad-last")).status_code == 429
    blocked = _get(api, _cred("u", "bad-after"))
    assert blocked.status_code == 429, f"达到 {auth.FAIL_MAX} 次失败后没有限流"
    assert int(blocked.headers["retry-after"]) > 0


def test_correct_password_also_blocked_while_throttled(api, tmp_db, passwd):
    """封锁期内连**正确**口令也拒——否则限流对已泄露口令毫无意义。"""
    for i in range(auth.FAIL_MAX):
        _get(api, _cred("u", f"bad{i}"))
    assert _get(api, _cred("u", "p")).status_code == 429


def test_success_resets_the_counter(api, tmp_db, passwd):
    """成功登录要清零：偶尔手滑两次不该积累到某天突然被锁。"""
    for i in range(auth.FAIL_MAX - 1):
        _get(api, _cred("u", f"bad{i}"))
    assert _get(api, _cred("u", "p")).status_code == 200
    # 清零后再失败 FAIL_MAX-1 次，仍然不该被锁
    for i in range(auth.FAIL_MAX - 1):
        assert _get(api, _cred("u", f"again{i}")).status_code == 401
    assert _get(api, _cred("u", "p")).status_code == 200


def test_other_client_not_affected(api, tmp_db, passwd):
    """**A 的失败不能连累 B**：按 IP 分桶。

    用 `request.client.host` 当键时所有请求都来自隧道容器，等于全球一个桶——
    攻击者打 10 次就把主人锁在门外。所以必须认 `CF-Connecting-IP`。
    """
    attacker = {**_cred("u", "bad"), "CF-Connecting-IP": "203.0.113.9"}
    for _ in range(auth.FAIL_MAX):
        _get(api, attacker)
    assert _get(api, {**_cred("u", "bad2"), "CF-Connecting-IP": "203.0.113.9"}).status_code == 429

    # 同一台机器，换一个 CF 客户端 IP → 不受影响
    owner = {**_cred("u", "p"), "CF-Connecting-IP": "198.51.100.1"}
    assert _get(api, owner).status_code == 200


def test_window_expiry_clears_the_counter(api, tmp_db, passwd, monkeypatch):
    """窗口过去后计数必须清零，否则「上周输错 9 次」会永远挂在那里。"""
    now = [1_000_000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])

    # 停在阈值之下（FAIL_MAX-1 次），否则会先触发封锁，测的就不是窗口了
    for i in range(auth.FAIL_MAX - 1):
        assert _get(api, _cred("u", f"bad{i}")).status_code == 401
    # 时间跳出窗口：计数应清零，再失败 FAIL_MAX-1 次仍不该被锁
    now[0] += auth.FAIL_WINDOW_S + 1
    for i in range(auth.FAIL_MAX - 1):
        assert _get(api, _cred("u", f"later{i}")).status_code == 401
    assert _get(api, _cred("u", "p")).status_code == 200


def test_block_expires_after_block_seconds(api, tmp_db, passwd, monkeypatch):
    """封锁到期后要放行，不能变成永久封禁。"""
    now = [2_000_000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    for i in range(auth.FAIL_MAX):
        _get(api, _cred("u", f"bad{i}"))
    assert _get(api, _cred("u", "p")).status_code == 429
    now[0] += auth.BLOCK_S + 1
    assert _get(api, _cred("u", "p")).status_code == 200


def test_missing_credentials_count_as_failure(api, tmp_db, passwd):
    """不带凭据也要计数——否则攻击者只要省略 header 就能绕开限流。"""
    for _ in range(auth.FAIL_MAX):
        assert _get(api).status_code == 401
    assert _get(api).status_code == 429


def test_healthz_is_never_throttled(api, tmp_db, passwd):
    """`/healthz` 免鉴权，也不该被限流影响：docker healthcheck 挡了会无限重启。"""
    for i in range(auth.FAIL_MAX + 2):
        _get(api, _cred("u", f"bad{i}"))
    assert _get(api, path="/healthz").status_code == 200


def test_client_key_prefers_cloudflare_header():
    """键的取法：CF-Connecting-IP > X-Forwarded-For 第一跳 > client.host。"""

    class FakeClient:
        host = "192.0.2.5"   # RFC 5737 文档地址，不用真实网段

    class FakeReq:
        def __init__(self, headers, client=FakeClient()):
            self.headers = headers
            self.client = client

    assert auth._client_key(FakeReq({"cf-connecting-ip": "1.2.3.4",
                                     "x-forwarded-for": "5.6.7.8"})) == "1.2.3.4"
    assert auth._client_key(FakeReq({"x-forwarded-for": "5.6.7.8, 9.9.9.9"})) == "5.6.7.8"
    assert auth._client_key(FakeReq({})) == "192.0.2.5"
    assert auth._client_key(FakeReq({}, client=None)) == "unknown"


def test_failures_dict_is_bounded(monkeypatch):
    """`_fails` 必须有上限：只出现一次的 key 也要被清理掉。

    过期清理只发生在 `_blocked_until(key)`——即**该 key 再次被访问**时。
    伪造 `CF-Connecting-IP`（这个头客户端可任意设置）每次换一个 IP、各失败一次，
    失败的 key 就永远留在 dict 里：100 万个 IP ≈ 100MB，而每个只失败一次连封锁
    都不会触发，看起来「什么都没发生」。这是可被外部触发的内存耗尽。
    """
    for i in range(auth.MAX_TRACKED + 500):
        auth._note_failure(f"10.0.{i // 256}.{i % 256}", 1000.0 + i)
    assert len(auth._fails) <= auth.MAX_TRACKED, \
        f"_fails 涨到 {len(auth._fails)} 条，超过上限 {auth.MAX_TRACKED}"


def test_expired_entries_are_swept_even_if_never_revisited(monkeypatch):
    """过期条目要在后续调用中被**批量**清掉，不能只清「又被访问到的那一个」。"""
    for i in range(100):
        auth._note_failure(f"192.0.2.{i}", 1000.0)
    assert len(auth._fails) == 100
    # 时间跳出窗口后再来一次失败（另一个 key），应触发清扫
    auth._note_failure("198.51.100.7", 1000.0 + auth.FAIL_WINDOW_S + 10)
    assert len(auth._fails) <= 2, f"过期条目没被清扫，剩 {len(auth._fails)} 条"


def test_real_attack_still_blocked_after_sweep(api, tmp_db, passwd):
    """清扫不能把**仍在窗口内**的攻击计数一起清掉。"""
    for i in range(auth.FAIL_MAX):
        _get(api, {**_cred("u", f"bad{i}"), "CF-Connecting-IP": "203.0.113.50"})
    # 再触发一次清扫（另一个 key 的第一次失败）
    _get(api, {**_cred("u", "x"), "CF-Connecting-IP": "198.51.100.99"})
    assert _get(api, {**_cred("u", "p"), "CF-Connecting-IP": "203.0.113.50"}).status_code == 429


def test_sweep_never_evicts_a_blocked_entry():
    """**清扫的容量淘汰不能扔掉正在封锁中的条目**——等同于给攻击者一个解锁按钮。

    淘汰按 `window_start` 升序丢最旧，完全不看 `blocked` 是否仍在生效。
    真实攻击路径：先用 IP A 打满 10 次拿到封锁（它的 `window_start` 自然最早），
    再用 `MAX_TRACKED` 个其它 IP 各失败一次把 dict 撑爆 —— A 作为「最旧」被丢，
    封锁立即失效，攻击者换回 A 继续猜。

    直接用 `_note_failure` / `_sweep` 构造，避免受 HTTP 层的真实时钟影响。
    """
    t0 = 1000.0
    victim = "203.0.113.77"
    for _ in range(auth.FAIL_MAX):
        auth._note_failure(victim, t0)          # window_start = t0（最早）
    _ws, _cnt, blocked = auth._fails[victim]
    assert blocked > t0, "前置条件不成立：victim 没有被封锁"

    # 攻击者用海量其它 IP 撑爆 dict（窗口起点都比 victim 晚）
    for i in range(auth.MAX_TRACKED + 500):
        auth._note_failure(f"198.18.{i // 256}.{i % 256}", t0 + 1 + i * 0.001)

    assert victim in auth._fails, (
        "正在封锁中的条目被容量淘汰了；攻击者撑爆 dict 就能解除自己的封锁")
    assert auth._fails[victim][2] > t0, "victim 的封锁被清掉了"
