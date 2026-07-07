"""Problem-detail helpers for the HTTP API.

Every hand-raised API error goes through `problem()` so the payload always carries the
RFC 7807-style shape the web client is built against:

    {"detail": {"type": "about:blank", "title": "...", "detail": "..."}}

(the outer "detail" envelope is FastAPI's standard HTTPException rendering). House rule:
the `detail` string must tell the user what to do next — point at the endpoint, config key
or doc that resolves the situation, never just restate the failure.
"""

from __future__ import annotations

from fastapi import HTTPException


def problem(status: int, title: str, detail: str) -> HTTPException:
    """Build an HTTPException whose detail is a problem-json-shaped dict.

    Usage: `raise problem(404, "Camera not found", "… — list ids via GET /api/v1/cameras")`.
    """
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "detail": detail},
    )
