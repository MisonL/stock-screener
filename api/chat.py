"""`POST /api/chat`（计划 Task 14）：**单例** `ChatSession` 注入路由。

每次请求新建 ChatSession 会重建 `RateLimiter` 与 `httpx.Client`，两个并发对话各自
从零计时，westock 的 ≥300ms 间隔形同虚设（D18）。所以会话对象在 app 启动时构造
一次，路由只借用它。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()


class ChatRequest(BaseModel):
    session_id: int | None = None
    text: str = Field(..., min_length=1, max_length=2000)
    market: str | None = None


def register(app, session, public_host: str = "") -> None:
    """注册对话端点。

    **必须做同源校验**：`/api/chat` 每次调用都花 LLM 预算，`/api/watch` 早就
    有这道校验，这里原先没有。`SameSite=Lax` 已经挡住跨站 POST，但那依赖
    浏览器行为；服务端自己判一次 Origin 才不依赖别人。空 `public_host`
    时跳过（测试/本地未配置），与 `_same_origin` 的取舍一致。
    """
    # **`Request` 必须在模块顶层导入**：本文件有 `from __future__ import
    # annotations`，所有注解都变成字符串，FastAPI 用模块全局去解析。函数内导入
    # 时解析不到 `Request`，它会被当成**必填查询参数** `?request=`——
    # 结果是每个请求都 422，而同源校验永远不执行。
    def _same_origin(request: Request) -> None:
        if not public_host:
            return
        origin = request.headers.get("origin")
        if not origin or origin != f"https://{public_host}":
            raise HTTPException(status_code=400, detail="同源校验失败")

    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request):
        _same_origin(request)
        answer = await session.ask(req.session_id, req.text)
        return {"session_id": answer.session_id, "text": answer.text,
                "as_of": answer.as_of, "market": answer.market,
                "fallback": answer.fallback,
                "grounded": {"ungrounded": answer.grounded.ungrounded,
                             "failed": answer.grounded.failed}}
