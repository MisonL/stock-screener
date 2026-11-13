"""`POST /api/chat`（计划 Task 14）：**单例** `ChatSession` 注入路由。

每次请求新建 ChatSession 会重建 `RateLimiter` 与 `httpx.Client`，两个并发对话各自
从零计时，westock 的 ≥300ms 间隔形同虚设（D18）。所以会话对象在 app 启动时构造
一次，路由只借用它。
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


class ChatRequest(BaseModel):
    session_id: int | None = None
    text: str = Field(..., min_length=1, max_length=2000)
    market: str | None = None


def register(app, session) -> None:
    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        answer = await session.ask(req.session_id, req.text)
        return {"session_id": answer.session_id, "text": answer.text,
                "as_of": answer.as_of, "market": answer.market,
                "fallback": answer.fallback,
                "grounded": {"ungrounded": answer.grounded.ungrounded,
                             "failed": answer.grounded.failed}}
