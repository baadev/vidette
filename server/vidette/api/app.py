"""FastAPI application factory.

Honesty rule (docs/project/principles.md): designed-but-unimplemented routes are mounted and
return `501 {"status": "designed", "milestone": ...}` — the API self-documents the roadmap
instead of 404-ing or, worse, faking. On the v0.2 line (ADR-0009) auth, events, push,
system, metrics and settings are real; the policies API remains an honest 501 (policies
live in the config file for now).
"""

from __future__ import annotations

import os
import platform
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from vidette import __version__
from vidette.auth.deps import current_principal, require_scope
from vidette.auth.service import Principal
from vidette.core.config import validate_config_text
from vidette.runtime import AppRuntime

_ROADMAP_URL = "https://github.com/baadev/vidette/blob/main/ROADMAP.md"

# Module-level on purpose: with `from __future__ import annotations`, FastAPI resolves the
# stringified `Annotated[..., Depends(_REQUIRE_READ_CONFIG)]` against module globals —
# a closure-local here silently degrades the dependency into a query parameter.
_REQUIRE_READ_CONFIG = require_scope("read:config")

# (path, methods, milestone) — remove entries as milestones ship; tests pin this behavior.
DESIGNED_ROUTES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("/api/v1/policies", ("GET", "PUT"), "v0.2"),
)

# Where the v0.2 line stands; shown by /api/v1/system and the System page.
STAGE = "v0.2-dev: intent layer over Frigate and clip folders (ADR-0009, ADR-0010)"

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
<p>The web app is served here once built (set <code>VIDETTE_WEB_DIST</code>); the API is
live at <a href="/api/docs">/api/docs</a> (v{__version__}).</p>
<p><a href="https://github.com/baadev/vidette">github.com/baadev/vidette</a></p>
</main></body></html>"""


def _frigate_status(rt: AppRuntime) -> dict[str, object] | None:
    """The incident source at a glance; None when no `frigate:` section is configured."""
    if rt.frigate_source is None or rt.config.frigate is None:
        return None
    status = rt.frigate_source.status()
    return {
        "url": rt.config.frigate.url,
        "broker": f"{rt.config.frigate.mqtt.host}:{rt.config.frigate.mqtt.port}",
        "topics": rt.frigate_source.topics,
        "connected": status.connected,
        "available": status.frigate_available,
        "messages_total": status.messages_total,
        "invalid_total": status.invalid_total,
        "incidents_total": status.incidents_total,
        "last_message_at": status.last_message_at,
    }


def _clip_folder_status(rt: AppRuntime) -> list[dict[str, object]]:
    if rt.clip_folders is None:
        return []
    return [
        {
            "path": status.path,
            "camera": status.camera,
            "exists": status.exists,
            "clips_total": status.clips_total,
            "errors_total": status.errors_total,
            "last_clip_at": status.last_clip_at,
            "last_scan_at": status.last_scan_at,
        }
        for status in rt.clip_folders.status()
    ]


def create_app(runtime: AppRuntime | None = None, *, workers: bool = True) -> FastAPI:
    """`runtime=None` builds from the environment at startup (production path); tests pass
    a prepared runtime and usually `workers=False`."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = runtime if runtime is not None else AppRuntime.from_environment()
        app.state.runtime = rt
        await rt.start(workers=workers)
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(
        title="Vidette",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/system")
    async def system(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> dict[str, object]:
        rt: AppRuntime = request.app.state.runtime
        return {
            "name": "vidette",
            "version": __version__,
            "milestone": STAGE,
            "python": platform.python_version(),
            "config_warnings": rt.config_warnings,
            "auth_mode": rt.config.server.auth.mode.value,
            "frigate": _frigate_status(rt),
            "clip_folders": _clip_folder_status(rt),
            "designed_routes": [
                {"path": path, "milestone": milestone}
                for path, _methods, milestone in DESIGNED_ROUTES
            ],
            "docs": _ROADMAP_URL,
        }

    @app.post("/api/v1/config/validate")
    async def config_validate(
        request: Request, principal: Annotated[Principal, Depends(_REQUIRE_READ_CONFIG)]
    ) -> Response:
        body = await request.body()
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return JSONResponse(
                status_code=400,
                content={"valid": False, "errors": ["body must be UTF-8 YAML"], "warnings": []},
            )
        report = validate_config_text(text)
        return JSONResponse(
            status_code=200 if report.valid else 422, content=report.model_dump()
        )

    from vidette.api.routers.auth import router as auth_router
    from vidette.api.routers.events import router as events_router
    from vidette.api.routers.identities import router as identities_router
    from vidette.api.routers.mcp import router as mcp_router
    from vidette.api.routers.metrics import router as metrics_router
    from vidette.api.routers.people import router as people_router
    from vidette.api.routers.push import router as push_router
    from vidette.api.routers.settings import router as settings_router
    from vidette.api.routers.system import router as system_router
    from vidette.api.routers.ws import router as ws_router

    app.include_router(auth_router)
    app.include_router(events_router)
    app.include_router(identities_router)
    app.include_router(mcp_router)
    app.include_router(metrics_router)
    app.include_router(people_router)
    app.include_router(push_router)
    app.include_router(settings_router)
    app.include_router(system_router)
    app.include_router(ws_router)

    def _register_designed(path: str, methods: tuple[str, ...], milestone: str) -> None:
        async def designed() -> JSONResponse:
            return JSONResponse(
                status_code=501,
                content={"status": "designed", "milestone": milestone, "docs": _ROADMAP_URL},
            )

        app.add_api_route(
            path,
            designed,
            methods=list(methods),
            include_in_schema=True,
            name=f"designed:{path}",
            tags=["designed"],
        )

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
