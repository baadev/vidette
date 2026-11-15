"""Building VLM judges for the command-line tools (`vidette eval`, `vidette label`).

The tools read the same config file as the server, so a backend configured once works
everywhere: `--config PATH`, else `$VIDETTE_CONFIG`, else `~/.config/vidette/vidette.yaml`,
else built-in defaults (local Ollama on localhost, no cloud). Secrets in that file are
`${ENV}` references as always.
"""

from __future__ import annotations

import os
from pathlib import Path

from vidette.core.config import ConfigError, VidetteConfig, VlmBackend, load_config
from vidette.verdicts.vlm import VlmJudge
from vidette.vlm.budget import FileLedger, UsageLedger, default_ledger_path
from vidette.vlm.client import VlmClient
from vidette.vlm.endpoint import effective_backend, endpoint_for, stored_choice

USER_CONFIG = Path("~/.config/vidette/vidette.yaml")
JUDGE_NAMES = {f"vlm-{backend.value}": backend for backend in (VlmBackend.local, VlmBackend.cloud)}


def cli_config(path: Path | None) -> tuple[VidetteConfig, Path | None]:
    """The config the tools use, and where it came from (None: built-in defaults)."""
    candidates = [path] if path is not None else []
    if path is None and os.environ.get("VIDETTE_CONFIG"):
        candidates.append(Path(os.environ["VIDETTE_CONFIG"]))
    if path is None:
        candidates.append(USER_CONFIG.expanduser())
    for candidate in candidates:
        if candidate.is_file():
            config, _warnings = load_config(candidate)
            return config, candidate
        if path is not None:
            raise ConfigError(f"config file {candidate} does not exist")
    return VidetteConfig(), None


def chosen_backend(config: VidetteConfig, flag: str | None) -> VlmBackend:
    """`--backend`, else the System page's stored choice, else the config, else local."""
    if flag is not None:
        return VlmBackend(flag)
    backend = effective_backend(config, stored_choice(config.storage.database))
    return VlmBackend.local if backend is VlmBackend.none else backend


def make_judge(
    config: VidetteConfig,
    backend: VlmBackend,
    *,
    ledger: UsageLedger | None = None,
    cache_dir: Path | None = None,
) -> VlmJudge:
    endpoint = endpoint_for(config, backend)
    client = VlmClient(endpoint, ledger=ledger or FileLedger(default_ledger_path()))
    return VlmJudge(
        client=client,
        frames_per_call=config.understanding.vlm.frames_per_call,
        cache_dir=cache_dir,
        language=config.understanding.vlm.language,
    )
