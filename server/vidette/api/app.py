"""FastAPI application factory.

Honesty rule (docs/project/principles.md): designed-but-unimplemented routes are mounted and
return `501 {"status": "designed", "milestone": ...}` — the API self-documents the roadmap
instead of 404-ing or, worse, faking.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from vidette import __version__
from vidette.core.config import validate_config_text

_ROADMAP_URL = "https://github.com/baadev/vidette/blob/main/ROADMAP.md"

# (path, methods, milestone) — remove entries as milestones ship; tests pin this behavior.
DESIGNED_ROUTES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("/api/v1/cameras", ("GET",), "M1"),
    ("/api/v1/streams/{camera}/live", ("GET",), "M1"),
    ("/api/v1/recordings", ("GET",), "M1"),
    ("/api/v1/export", ("POST",), "M1"),
    ("/api/v1/events", ("GET",), "M2"),
    ("/api/v1/policies", ("GET", "PUT"), "M4"),
)

_FALLBACK_PAGE = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Vidette</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ background:#0b0e14; color:#e6e9f0; font: 16px/1.6 system-ui, sans-serif;
         display:grid; place-items:center; min-height:100vh; margin:0 }}
  main {{ max-width: 40rem; padding: 2rem }}
  h1 {{ letter-spacing:.35em; font-weight:600 }} h1 b {{ color:#f0a63c }}
  a {{ color:#f0a63c }} code {{ background:#11151f; padding:.15em .4em; border-radius:4px }}
</style></head><body><main>
<h1>VIDE<b>TT</b>E</h1>
<p>Self-hosted video security that understands intent — not just motion.</p>
<p>Status: <strong>M0 design preview</strong> (v{__version__}). The web app is served here
once built; the API is live at <a href="/api/docs">/api/docs</a>.</p>
<p>Try: <code>GET /healthz</code> · <code>GET /api/v1/system</code> ·
<code>POST /api/v1/config/validate</code></p>
<p><a href="https://github.com/baadev/vidette">github.com/baadev/vidette</a></p>
</main></body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vidette",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/system")
    async def system() -> dict[str, object]:
        return {
            "name": "vidette",
            "version": __version__,
            "milestone": "M0",
            "python": platform.python_version(),
            "designed_routes": [
                {"path": path, "milestone": milestone}
                for path, _methods, milestone in DESIGNED_ROUTES
            ],
            "docs": _ROADMAP_URL,
        }

    @app.post("/api/v1/config/validate")
    async def config_validate(request: Request) -> Response:
        body = await request.body()
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return JSONResponse(
                status_code=400,
                content={"valid": False, "errors": ["body must be UTF-8 YAML"], "warnings": []},
            )
        report = validate_config_text(text)
        return JSONResponse(status_code=200 if report.valid else 422,
                            content=report.model_dump())

    def _register_designed(path: str, methods: tuple[str, ...], milestone: str) -> None:
        async def designed() -> JSONResponse:
            return JSONResponse(
                status_code=501,
                content={"status": "designed", "milestone": milestone, "docs": _ROADMAP_URL},
            )

        app.add_api_route(path, designed, methods=list(methods), include_in_schema=True,
                          name=f"designed:{path}", tags=["designed"])

    for route_path, route_methods, route_milestone in DESIGNED_ROUTES:
        _register_designed(route_path, route_methods, route_milestone)

    web_dist = Path(os.environ.get("VIDETTE_WEB_DIST", "/app/web-dist"))
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:
        @app.get("/", include_in_schema=False)
        async def index() -> HTMLResponse:
            return HTMLResponse(_FALLBACK_PAGE)

    return app
