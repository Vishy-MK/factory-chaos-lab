import json
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class InferenceTimingMiddleware:

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                elapsed_ms = (time.perf_counter() - start) * 1000
                headers = list(message.get("headers", []))
                headers.append((b"x-inference-time-ms", f"{elapsed_ms:.3f}".encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class MemoryGuardMiddleware:
    """Sheds memory-heavy RAG requests with 503 before the cgroup OOM killer fires."""

    HEAVY_PREFIXES = ("/api/rag/documents/upload", "/api/rag/retrieve", "/api/rag/query")

    def __init__(self, app: ASGIApp, min_available_mb: int = 0) -> None:
        self.app = app
        self.min_available_mb = min_available_mb

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.min_available_mb and scope["type"] == "http" and scope["method"] == "POST" and scope["path"].startswith(self.HEAVY_PREFIXES):
            from app.os_limits import memory_status

            available = memory_status()["available_mb"]
            if available < self.min_available_mb:
                body = json.dumps({"detail": "Server is low on memory; retry later", "available_mb": available}).encode()
                await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json"), (b"retry-after", b"5"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
