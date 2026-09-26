"""FastAPI 组装：`/api/*` 路由、`X-Data-Status` 响应头、可选令牌、前端静态资源（SPA 回退）。"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from moneytool import __version__
from moneytool.api.routers import actions, flow, market, review, sectors, status, stocks
from moneytool.params import builtin_params_dir
from moneytool.storage.conn import Database

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PUBLIC_PATHS = ("/api/health",)


def create_app(
    db: Database,
    *,
    ctx: Any = None,
    runner: Any = None,
    token: str = "",
    static_dir: Path | None = STATIC_DIR,
) -> FastAPI:
    app = FastAPI(title="moneytool", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.state.db = db
    app.state.ctx = ctx
    app.state.runner = runner
    app.state.token = token
    app.state.params_dir = ctx.params_dir if ctx is not None else builtin_params_dir()

    @app.middleware("http")
    async def auth_and_status_header(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if (
            token
            and path.startswith("/api/")
            and path not in PUBLIC_PATHS
            and not _token_ok(request, token)
        ):
            return JSONResponse({"detail": "未授权"}, status_code=401)
        response = await call_next(request)
        if not (
            path.startswith("/api/")
            and response.headers.get("content-type", "").startswith("application/json")
        ):
            return response
        # 状态头从响应体 meta 提取：收集分块后重建响应
        chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
        body = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        try:
            meta = json.loads(body).get("meta") or {}
        except (ValueError, AttributeError):
            meta = {}
        if isinstance(meta, dict) and meta.get("status"):
            headers["X-Data-Status"] = str(meta["status"])
            if meta.get("trade_date"):
                headers["X-Trade-Date"] = str(meta["trade_date"])
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    api_prefix = "/api"
    app.include_router(status.router, prefix=api_prefix)
    app.include_router(market.router, prefix=api_prefix)
    app.include_router(sectors.router, prefix=api_prefix)
    app.include_router(stocks.router, prefix=api_prefix)
    app.include_router(actions.router, prefix=api_prefix)
    app.include_router(review.router, prefix=api_prefix)
    app.include_router(flow.router, prefix=api_prefix)

    if static_dir is not None and (static_dir / "index.html").exists():
        assets = static_dir / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")
        index = static_dir / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            candidate = (static_dir / full_path).resolve()
            if full_path and candidate.is_file() and static_dir.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(index)

    else:

        @app.get("/", include_in_schema=False)
        def no_frontend() -> JSONResponse:
            return JSONResponse(
                {
                    "message": "前端未构建：运行 `python scripts/build_frontend.py`，或直接访问 /api/docs",
                    "docs": "/api/docs",
                }
            )

    return app


def _token_ok(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    supplied = (
        header[7:]
        if header.lower().startswith("bearer ")
        else request.query_params.get("token", "")
    )
    return secrets.compare_digest(supplied, token)
