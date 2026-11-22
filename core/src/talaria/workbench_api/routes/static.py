"""独立看盘 SPA 与构建产物托管。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
)

router = APIRouter()


_MISSING_DIST_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Talaria 前端尚未构建</title>
<style>body{font-family:system-ui,sans-serif;max-width:44rem;margin:4rem auto;padding:0 1rem;line-height:1.7;color:#1f2328}code{background:#f3f4f6;padding:.15rem .35rem;border-radius:.25rem}</style>
</head><body>
<h1>前端构建产物缺失</h1>
<p>请先在仓库根目录执行 <code>pnpm -C dsh install &amp;&amp; pnpm -C dsh build</code>，然后刷新本页。</p>
<p>后端 API 仍可通过 <code>/api/health</code> 检查。</p>
</body></html>"""


def _desk_response(index_path: Path, token: str | None = None) -> HTMLResponse:
    if not index_path.is_file():
        response = HTMLResponse(_MISSING_DIST_HTML, status_code=200)
    else:
        response = HTMLResponse(index_path.read_text(encoding="utf-8"))
    if token is not None:
        # HttpOnly 关闭：同源 JS 需要读取后放进 Authorization，cookie 本身不参与鉴权。
        response.set_cookie(
            "talaria_token",
            token,
            httponly=False,
            samesite="strict",
            path="/",
        )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/desk/link", response_class=HTMLResponse, include_in_schema=False)
def desk_link(request: Request) -> HTMLResponse:
    return _desk_response(request.app.state.settings.dist_dir / "index.html")


@router.get("/desk", response_class=HTMLResponse)
@router.get("/desk/{path:path}", response_class=HTMLResponse)
def desk(request: Request, path: str = "") -> HTMLResponse:
    index_path = request.app.state.settings.dist_dir / "index.html"
    return _desk_response(index_path, request.app.state.token)


@router.get("/assets/{asset_path:path}")
def assets(asset_path: str, request: Request):
    base = (request.app.state.settings.dist_dir / "assets").resolve()
    candidate = (base / asset_path).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file():
        return HTMLResponse("资源不存在", status_code=404)
    return FileResponse(candidate)
