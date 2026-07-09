"""FastAPI application factory.

Honesty rule (docs/project/principles.md): designed-but-unimplemented routes are mounted and
return `501 {"status": "designed", "milestone": ...}` — the API self-documents the roadmap
instead of 404-ing or, worse, faking. As of M2, cameras/recordings/streams/export/events are
real; policies (M4) remains an honest 501.
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
<p>The web app is served here once built (set <code>VIDETTE_WEB_DIST</code>); the API is
live at <a href="/api/docs">/api/docs</a> (v{__version__}).</p>
<p><a href="https://github.com/baadev/vidette">github.com/baadev/vidette</a></p>
</main></body></html>"""


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
        gateway = await rt.go2rtc.health()
        return {
            "name": "vidette",
            "version": __version__,
            "milestone": "M2",
            "python": platform.python_version(),
            "config_warnings": rt.config_warnings,
            "auth_mode": rt.config.server.auth.mode.value,
            "gateway": {
                "reachable": gateway.reachable,
                "version": gateway.version,
                "streams": sorted(gateway.streams),
                "detail": gateway.detail,
            },
            "recorders": {
                camera: {
                    "state": status.state,
                    "last_segment_at": status.last_segment_at,
                    "last_error": status.last_error,
                    "restarts": status.restarts,
                }
                for camera, status in rt.recorder.status().items()
            },
            "keepwarm": {
                camera: {
                    "state": status.state,
                    "bytes_received": status.bytes_received,
                    "last_data_at": status.last_data_at,
                }
                for camera, status in rt.keepwarm.status().items()
            },
            "detector": rt.detector_state,
            "pipelines": {
                camera: {
                    "state": status.state,
                    "frames_total": status.frames_total,
                    "motion_frames": status.motion_frames,
                    "detect_calls": status.detect_calls,
                    "last_frame_at": status.last_frame_at,
                    "last_error": status.last_error,
                }
                for camera, status in rt.pipeline.status().items()
            },
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
    from vidette.api.routers.cameras import router as cameras_router
    from vidette.api.routers.config_cameras import router as config_cameras_router
    from vidette.api.routers.events import router as events_router
    from vidette.api.routers.metrics import router as metrics_router
    from vidette.api.routers.push import router as push_router
    from vidette.api.routers.recordings import router as recordings_router
    from vidette.api.routers.streams import router as streams_router
    from vidette.api.routers.streams import ws_router as streams_ws_router
    from vidette.api.routers.system import router as system_router
    from vidette.api.routers.ws import router as ws_router

    app.include_router(auth_router)
    app.include_router(cameras_router)
    app.include_router(config_cameras_router)
    app.include_router(events_router)
    app.include_router(metrics_router)
    app.include_router(push_router)
    app.include_router(recordings_router)
    app.include_router(streams_router)
    app.include_router(streams_ws_router)
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
