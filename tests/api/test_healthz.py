import httpx

from api.main import app
from core.version import VERSION


async def test_healthz_returns_ok():
    # httpx 的 ASGITransport 只有异步接口，同步 Client 会在 __enter__ 直接 AttributeError。
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # 版本号必须带出来：排障第一个问题就是「线上是哪一版」。
    # 断言取自 `core/version.py` 的唯一来源；写死字面量会在每次发版时误报。
    assert body["version"] == VERSION
