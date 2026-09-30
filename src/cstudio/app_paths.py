"""Executable-relative portable directory layout.

Every file the application creates lives below the application root, which is
the directory that contains ``CrimsonSoundtrackStudio.exe`` in a frozen build.
Nothing is written to %APPDATA%, %LOCALAPPDATA%, Documents or ProgramData, so
moving the application folder moves all of its data with it.

The root is derived from the executable's real location, never from the
current working directory, so launching through a shortcut or from another
directory behaves the same.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ENV_HOME_OVERRIDE = "CSTUDIO_HOME"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def detect_app_root() -> Path:
    """Return the portable application root.

    * Frozen (PyInstaller) build: the folder holding the executable.
    * ``CSTUDIO_HOME`` set: that folder (tests and developer overrides).
    * Source checkout: ``<repo>/dev_home`` so development runs never litter
      the repository root.
    """

    override = os.environ.get(ENV_HOME_OVERRIDE)
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "dev_home"


def bundled_resource_root() -> Path:
    """Read-only resources shipped with the application (catalog defaults etc.)."""

    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def database_dir(self) -> Path:
        return self.data / "database"

    @property
    def database_file(self) -> Path:
        return self.database_dir / "studio.sqlite3"

    @property
    def scans(self) -> Path:
        return self.data / "scans"

    @property
    def knowledge(self) -> Path:
        return self.data / "knowledge"

    @property
    def knowledge_verified(self) -> Path:
        return self.knowledge / "verified"

    @property
    def knowledge_hypotheses(self) -> Path:
        return self.knowledge / "hypotheses"

    @property
    def knowledge_research(self) -> Path:
        return self.knowledge / "research"

    @property
    def reports(self) -> Path:
        return self.data / "reports"

    @property
    def metadata(self) -> Path:
        return self.data / "metadata"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def temp(self) -> Path:
        return self.root / "temp"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def settings_file(self) -> Path:
        return self.config / "settings.json"

    @property
    def model_catalog_file(self) -> Path:
        return self.config / "model_catalog.json"

    def all_dirs(self) -> list[Path]:
        return [
            self.runtime,
            self.data,
            self.database_dir,
            self.scans,
            self.knowledge,
            self.knowledge_verified,
            self.knowledge_hypotheses,
            self.knowledge_research,
            self.reports,
            self.metadata,
            self.models,
            self.cache,
            self.logs,
            self.output,
            self.temp,
            self.config,
        ]

    def ensure(self) -> "AppPaths":
        for directory in self.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def is_inside(self, path: Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.root.resolve())
            return True
        except ValueError:
            return False

    def check_writable(self) -> Optional[str]:
        """Return an error message when the portable folder cannot be written."""

        probe = self.temp / ".write_probe"
        try:
            self.temp.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return (
                f"The application folder {self.root} is not writable ({exc}). "
                "Extract Crimson Soundtrack Studio to a folder you own (for example a folder on your "
                "desktop or a games drive) instead of Program Files."
            )
        return None


def redirect_process_temp(paths: AppPaths) -> None:
    """Point the process temp directory (and libraries honouring TMP/TEMP) at ``temp/``."""

    paths.temp.mkdir(parents=True, exist_ok=True)
    temp = str(paths.temp)
    for var in ("TMP", "TEMP", "TMPDIR"):
        os.environ[var] = temp
    tempfile.tempdir = temp


_current: Optional[AppPaths] = None


def get_paths() -> AppPaths:
    global _current
    if _current is None:
        _current = AppPaths(detect_app_root())
    return _current


def set_paths(paths: AppPaths) -> None:
    global _current
    _current = paths
