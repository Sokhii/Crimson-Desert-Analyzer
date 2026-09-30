"""Local model catalog and resource tiers.

The catalog is data, not code: ``config/model_catalog.json`` (copied from the
bundled default on first run) can be edited to add or swap models without a
new build. Custom models (any local GGUF file) are registered in the database.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from cstudio.app_paths import AppPaths
from cstudio.config import RESOURCES
from cstudio.db.database import Database, dumps, loads

TIERS = ("low", "medium", "high")


@dataclass
class LocalModel:
    id: str
    display_name: str
    tier: str
    backend: str = "llama.cpp"
    source: str = "huggingface"
    repository: str = ""
    revision: str = "main"
    filename: str = ""
    quantization: str = ""
    download_url: str = ""
    approximate_size_gb: float = 0.0
    recommended_vram_gb: float = 0.0
    context_size: int = 8192
    json_mode: str = "schema"
    capabilities: List[str] = field(default_factory=list)
    license: str = ""
    license_url: str = ""
    alternate_repositories: List[str] = field(default_factory=list)  # tried in order if the main repo lacks the file
    local_path: str = ""  # set for custom models / after download

    def resolved_url(self) -> str:
        if self.download_url:
            return self.download_url
        if self.source == "huggingface" and self.repository and self.filename:
            return f"https://huggingface.co/{self.repository}/resolve/{self.revision or 'main'}/{self.filename}"
        return ""

    def install_dir(self, paths: AppPaths) -> Path:
        return paths.models / self.id

    def install_path(self, paths: AppPaths) -> Path:
        if self.local_path:
            candidate = Path(self.local_path)
            return candidate if candidate.is_absolute() else paths.root / candidate
        return self.install_dir(paths) / self.filename

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class TierInfo:
    key: str
    label: str
    best_for: str
    recommended_vram: str
    default_model: str


class ModelCatalog:
    def __init__(self, models: List[LocalModel], tiers: Dict[str, TierInfo]) -> None:
        self.models = {m.id: m for m in models}
        self.tiers = tiers

    @classmethod
    def load(cls, paths: AppPaths, db: Optional[Database] = None) -> "ModelCatalog":
        file = paths.model_catalog_file if paths.model_catalog_file.is_file() else RESOURCES / "model_catalog.json"
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = json.loads((RESOURCES / "model_catalog.json").read_text(encoding="utf-8"))
        known = set(LocalModel.__dataclass_fields__)
        models = [LocalModel(**{k: v for k, v in m.items() if k in known}) for m in data.get("models", [])]
        tiers = {
            key: TierInfo(key, t.get("label", key.title()), t.get("best_for", ""), t.get("recommended_vram", ""),
                          t.get("default_model", ""))
            for key, t in data.get("tiers", {}).items()
        }
        catalog = cls(models, tiers)
        if db is not None:
            for row in db.query("SELECT * FROM model WHERE tier='custom'"):
                details = loads(row["details_json"], {}) or {}
                catalog.models[row["id"]] = LocalModel(
                    id=row["id"], display_name=row["display_name"] or row["id"], tier="custom", source="local",
                    filename=Path(row["file_path"]).name, local_path=row["file_path"],
                    context_size=int(details.get("context_size", 8192)), json_mode=details.get("json_mode", "schema"),
                    capabilities=["chat"], license=details.get("license", "user supplied"),
                )
        return catalog

    def for_tier(self, tier: str) -> List[LocalModel]:
        return [m for m in self.models.values() if m.tier == tier]

    def default_for_tier(self, tier: str) -> Optional[LocalModel]:
        info = self.tiers.get(tier)
        if info and info.default_model in self.models:
            return self.models[info.default_model]
        options = self.for_tier(tier)
        return options[0] if options else None

    def get(self, model_id: str) -> Optional[LocalModel]:
        return self.models.get(model_id)


def register_custom_model(db: Database, paths: AppPaths, gguf_path: Path, display_name: str = "",
                          context_size: int = 8192) -> LocalModel:
    gguf_path = Path(gguf_path)
    with open(gguf_path, "rb") as handle:
        if handle.read(4) != b"GGUF":
            raise ValueError(f"{gguf_path.name} is not a GGUF model file")
    model_id = "custom-" + gguf_path.stem.lower().replace(" ", "-")[:60]
    try:
        stored = gguf_path.resolve().relative_to(paths.root.resolve()).as_posix()
    except ValueError:
        stored = str(gguf_path.resolve())
    db.execute(
        "INSERT INTO model(id, display_name, tier, file_path, size, status, details_json) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET file_path=excluded.file_path, size=excluded.size",
        (model_id, display_name or gguf_path.stem, "custom", stored, gguf_path.stat().st_size, "available",
         dumps({"context_size": context_size, "json_mode": "schema"})),
    )
    db.commit()
    return LocalModel(id=model_id, display_name=display_name or gguf_path.stem, tier="custom", source="local",
                      filename=gguf_path.name, local_path=stored, context_size=context_size)


def model_status(db: Database, paths: AppPaths, model: LocalModel) -> Dict[str, object]:
    row = db.query_one("SELECT * FROM model WHERE id=?", (model.id,))
    path = model.install_path(paths)
    present = path.is_file()
    status = row["status"] if row else ("available" if present else "not_downloaded")
    if not present and status in ("available", "verified"):
        status = "missing"
    return {
        "id": model.id,
        "present": present,
        "path": str(path),
        "status": status,
        "sha256": row["sha256"] if row else None,
        "inference_ok": bool(row["inference_ok"]) if row and row["inference_ok"] is not None else None,
        "verified_at": row["verified_at"] if row else None,
    }


def set_model_state(db: Database, model: LocalModel, **values) -> None:
    row = db.query_one("SELECT id FROM model WHERE id=?", (model.id,))
    if row is None:
        db.execute("INSERT INTO model(id, display_name, tier, file_path, status) VALUES (?,?,?,?,?)",
                   (model.id, model.display_name, model.tier, model.local_path or f"models/{model.id}/{model.filename}",
                    values.get("status", "unknown")))
    for key, value in values.items():
        if key not in ("status", "sha256", "size", "verified_at", "inference_ok", "file_path"):
            continue
        db.execute(f"UPDATE model SET {key}=? WHERE id=?", (value, model.id))
    db.commit()
