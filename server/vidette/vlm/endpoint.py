"""Which VLM endpoint a judgment goes to, and what the person should know about it."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from vidette.core.config import VidetteConfig, VlmBackend, VlmPrices

BACKEND_META_KEY = "vlm.backend"  # the System page's choice, stored in the meta table


class VlmUnavailableError(Exception):
    """The chosen backend is not configured; the message says what to add."""


@dataclass(frozen=True)
class VlmEndpoint:
    backend: VlmBackend  # local | cloud
    base_url: str
    model: str
    api_key: str | None
    vendor: str  # who receives the frames
    token_param: str
    timeout_s: float
    max_calls_per_day: int
    max_calls_per_minute: int
    max_output_tokens: int
    max_image_side: int
    prices: VlmPrices

    @property
    def host(self) -> str:
        parts = urlsplit(self.base_url)
        return parts.netloc or self.base_url

    @property
    def is_loopback(self) -> bool:
        host = urlsplit(self.base_url).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1", "host.docker.internal")

    @property
    def privacy(self) -> str:
        """One honest line: where the frames go."""
        if self.backend is VlmBackend.cloud:
            return f"Frames from your cameras are sent to {self.vendor} ({self.host})."
        if self.is_loopback:
            return f"Frames stay on this machine ({self.host})."
        return f"Frames go to your own server at {self.host}; nothing goes to a cloud vendor."

    def __repr__(self) -> str:  # never print the key
        return f"VlmEndpoint({self.backend.value}, {self.model} @ {self.host})"


def endpoint_for(config: VidetteConfig, backend: VlmBackend) -> VlmEndpoint:
    vlm = config.understanding.vlm
    if backend is VlmBackend.local:
        local = vlm.local
        return VlmEndpoint(
            backend=backend,
            base_url=local.base_url,
            model=local.model,
            api_key=local.api_key,
            vendor="your own server",
            token_param="max_tokens",
            timeout_s=local.timeout_s,
            max_calls_per_day=local.max_calls_per_day,
            max_calls_per_minute=local.max_calls_per_minute,
            max_output_tokens=local.max_output_tokens,
            max_image_side=local.max_image_side,
            prices=local.prices,
        )
    if backend is VlmBackend.cloud:
        cloud = vlm.cloud
        if cloud is None:
            raise VlmUnavailableError(
                "the cloud backend is not configured: add understanding.vlm.cloud with a "
                "preset, model and api_key (docs/vlm.md)"
            )
        return VlmEndpoint(
            backend=backend,
            base_url=cloud.resolved_base_url,
            model=cloud.resolved_model,
            api_key=cloud.api_key,
            vendor=cloud.vendor,
            token_param=cloud.token_param,
            timeout_s=cloud.timeout_s,
            max_calls_per_day=cloud.max_calls_per_day,
            max_calls_per_minute=cloud.max_calls_per_minute,
            max_output_tokens=cloud.max_output_tokens,
            max_image_side=cloud.max_image_side,
            prices=cloud.prices,
        )
    raise VlmUnavailableError("no VLM backend selected (understanding.vlm.backend: none)")


def available_backends(config: VidetteConfig) -> dict[VlmBackend, str | None]:
    """Each backend → None when it can be selected, else why not."""
    return {
        VlmBackend.none: None,
        VlmBackend.local: None,
        VlmBackend.cloud: (
            None
            if config.understanding.vlm.cloud is not None
            else "add understanding.vlm.cloud (preset, model, api_key) to the config"
        ),
    }


def parse_choice(value: str | None) -> VlmBackend | None:
    try:
        return None if value is None else VlmBackend(value)
    except ValueError:
        return None


def stored_choice(database: Path) -> VlmBackend | None:
    """The System page's choice from the server's database, read-only, for CLI tools."""
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (BACKEND_META_KEY,))
            found = row.fetchone()
    except sqlite3.Error:
        return None
    return parse_choice(None if found is None else str(found[0]))


def effective_backend(config: VidetteConfig, stored: VlmBackend | None) -> VlmBackend:
    """The stored UI choice wins over the config, unless it is no longer available."""
    if stored is not None and available_backends(config)[stored] is None:
        return stored
    return config.understanding.vlm.backend
