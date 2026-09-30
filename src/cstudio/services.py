"""Application services: the only layer the GUI talks to.

Each long-running operation takes its own ``Database`` connection so it can
run on a worker thread while the GUI keeps reading.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from .ai.agent import AgentProgress, Investigator, build_tasks
from .ai.catalog import LocalModel, ModelCatalog, model_status, register_custom_model, set_model_state
from .ai.downloader import DownloadProgress, download_model, fetch_expected, verify_file
from .ai.runtime import InferenceBackend, LlamaServerBackend, OpenAICompatibleBackend, find_llama_server, run_inference_check
from .analyzer import community
from .analyzer.discovery import detect_layout
from .analyzer.scanner import ScanResult, Scanner, ScanStats
from .app_paths import AppPaths, redirect_process_temp
from .config import Settings, ensure_default_config
from .db.database import Database, now_iso
from .knowledge.store import KnowledgeStore

log = logging.getLogger("cstudio")


def setup_logging(paths: AppPaths) -> None:
    paths.logs.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(paths.logs / "studio.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.INFO)


class Studio:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths.ensure()
        redirect_process_temp(paths)
        setup_logging(paths)
        ensure_default_config(paths)
        self.settings = Settings.load(paths)
        self.db = Database(paths.database_file)
        self.knowledge = KnowledgeStore(self.db, paths)
        self.knowledge.sync_from_files()
        self._backend: Optional[InferenceBackend] = None
        self._backend_model: Optional[str] = None

    # ------------------------------------------------------------ settings
    def save_settings(self) -> None:
        self.settings.save(self.paths)

    def catalog(self) -> ModelCatalog:
        return ModelCatalog.load(self.paths, self.db)

    def current_installation_id(self) -> Optional[int]:
        if self.settings.game_path:
            row = self.db.query_one("SELECT id FROM installation WHERE root_path=?", (str(Path(self.settings.game_path).resolve()),))
            if row:
                return int(row["id"])
        row = self.db.latest_installation()
        return int(row["id"]) if row else None

    def describe_installation(self, path: str) -> Dict[str, object]:
        layout = detect_layout(Path(path))
        return {"valid": layout.looks_valid, "packages": layout.package_dirs, "notes": layout.notes,
                "executable": str(layout.game_executable) if layout.game_executable else None}

    # ---------------------------------------------------------------- scan
    def run_scan(self, game_path: str, progress: Optional[Callable[[ScanStats], None]] = None,
                 cancel: Optional[threading.Event] = None, allow_inside_app: bool = False) -> ScanResult:
        db = Database(self.paths.database_file)
        try:
            scanner = Scanner(db, self.paths, progress=progress, cancel=cancel, full_hash=self.settings.full_wem_hash)
            return scanner.run(Path(game_path), allow_inside_app=allow_inside_app)
        finally:
            db.close()

    def import_community_csv(self, csv_path: str) -> Dict[str, object]:
        return community.import_csv(self.db, Path(csv_path))

    # -------------------------------------------------------------- models
    def model_status(self, model: LocalModel) -> Dict[str, object]:
        return model_status(self.db, self.paths, model)

    def runtime_available(self) -> bool:
        return find_llama_server(self.paths, self.settings.llama_server_path) is not None

    def download(self, model: LocalModel, progress: Optional[Callable[[DownloadProgress], None]] = None,
                 cancel: Optional[threading.Event] = None) -> Dict[str, object]:
        db = Database(self.paths.database_file)
        try:
            set_model_state(db, model, status="downloading")
            try:
                result = download_model(model, self.paths, progress=progress, cancel=cancel)
            except Exception:
                set_model_state(db, model, status="not_downloaded")
                raise
            set_model_state(db, model, status="verified", sha256=result["sha256"], size=result["size"], verified_at=now_iso())
            return result
        finally:
            db.close()

    def verify(self, model: LocalModel) -> Dict[str, object]:
        path = model.install_path(self.paths)
        expected = fetch_expected(model) if model.source == "huggingface" else {}
        result = verify_file(path, expected)
        set_model_state(self.db, model, status="verified", sha256=result["sha256"], size=result["size"], verified_at=now_iso())
        return result

    def register_custom(self, gguf_path: str) -> LocalModel:
        return register_custom_model(self.db, self.paths, Path(gguf_path))

    def backend_for(self, model: LocalModel) -> InferenceBackend:
        if self._backend is not None and self._backend_model == model.id:
            return self._backend
        self.stop_backend()
        if self.settings.external_endpoint:
            backend: InferenceBackend = OpenAICompatibleBackend(self.settings.external_endpoint, model.id)
        else:
            backend = LlamaServerBackend(
                self.paths, model, server_path=find_llama_server(self.paths, self.settings.llama_server_path),
                gpu_layers=self.settings.gpu_layers, context_size=self.settings.context_size, threads=self.settings.threads,
                log=log.info)
        self._backend, self._backend_model = backend, model.id
        return backend

    def load_and_test(self, model: LocalModel) -> Dict[str, object]:
        backend = self.backend_for(model)
        backend.start()
        result = run_inference_check(backend)
        db = Database(self.paths.database_file)
        try:
            set_model_state(db, model, status="verified" if result["ok"] else "load_failed", inference_ok=int(result["ok"]))
        finally:
            db.close()
        result["backend"] = backend.describe()
        return result

    def stop_backend(self) -> None:
        if self._backend is not None:
            self._backend.stop()
        self._backend = None
        self._backend_model = None

    # ---------------------------------------------------------- investigate
    def run_investigation(self, model: LocalModel, progress: Optional[Callable[[AgentProgress], None]] = None,
                          stop: Optional[threading.Event] = None, pause: Optional[threading.Event] = None) -> AgentProgress:
        inst_id = self.current_installation_id()
        if inst_id is None:
            raise RuntimeError("scan the game before starting an AI investigation")
        backend = self.backend_for(model)
        backend.start()
        db = Database(self.paths.database_file)
        try:
            agent = Investigator(db, self.paths, inst_id, backend, model.id, progress=progress, stop=stop, pause=pause,
                                 max_steps_per_task=self.settings.max_steps_per_task,
                                 max_total_steps=self.settings.max_total_steps)
            return agent.run(build_tasks(db, inst_id))
        finally:
            db.close()

    def close(self) -> None:
        self.stop_backend()
        self.db.close()
