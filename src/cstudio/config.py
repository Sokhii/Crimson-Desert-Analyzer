"""Portable JSON settings stored in ``config/settings.json`` beside the executable."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

from .app_paths import AppPaths

RESOURCES = Path(__file__).resolve().parent / "resources"


@dataclass
class Settings:
    game_path: str = ""
    ai_tier: str = "medium"
    ai_model_id: str = ""
    gpu_layers: str = "auto"          # "auto", "0" (CPU only) or a layer count
    context_size: int = 0             # 0 = model catalog default
    threads: int = 0                  # 0 = llama.cpp default
    llama_server_path: str = ""       # empty = bundled runtime/
    external_endpoint: str = ""       # advanced: use an already-running OpenAI-compatible server
    max_steps_per_task: int = 14
    max_total_steps: int = 400
    full_wem_hash: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, paths: AppPaths) -> "Settings":
        file = paths.settings_file
        if not file.is_file():
            return cls()
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, paths: AppPaths) -> None:
        paths.config.mkdir(parents=True, exist_ok=True)
        tmp = paths.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(paths.settings_file)


def ensure_default_config(paths: AppPaths) -> None:
    """Copy editable defaults (model catalog) into ``config/`` on first run."""

    target = paths.model_catalog_file
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((RESOURCES / "model_catalog.json").read_text(encoding="utf-8"), encoding="utf-8")


def resource_path(name: str) -> Optional[Path]:
    candidate = RESOURCES / name
    return candidate if candidate.exists() else None
