"""Read-only discovery of a Crimson Desert installation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

AUDIO_EXTENSIONS = {".bnk", ".wem", ".wav", ".ogg", ".opus", ".flac", ".mp3"}
AUDIO_METADATA_EXTENSIONS = {".xml", ".txt", ".json", ".csv"}
AUDIO_DIR_TOKENS = ("sound", "audio", "wwise", "bgm", "music")
AUDIO_NAME_TOKENS = ("soundbank", "soundbanksinfo", "wwise", "bgm", "music", "audio")

PACKAGE_DIR_RE = re.compile(r"^\d{4}$")


@dataclass
class DiscoveredFile:
    rel_path: str
    abs_path: str
    size: int
    mtime_ns: int
    kind: str  # pamt | paz | papgt | loose


@dataclass
class InstallationLayout:
    root: Path
    package_root: Path
    game_executable: Optional[Path]
    package_dirs: List[str] = field(default_factory=list)
    has_papgt: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def looks_valid(self) -> bool:
        return bool(self.package_dirs)


def _has_pamt(directory: Path) -> bool:
    try:
        return any(child.suffix.lower() == ".pamt" for child in directory.iterdir() if child.is_file())
    except OSError:
        return False


def detect_layout(root: Path) -> InstallationLayout:
    root = Path(root)
    candidates = [root, root / "game_files"]
    package_root = root
    for candidate in candidates:
        if candidate.is_dir() and any(
            PACKAGE_DIR_RE.match(c.name) and _has_pamt(c) for c in _safe_iterdir(candidate) if c.is_dir()
        ):
            package_root = candidate
            break
    layout = InstallationLayout(root=root, package_root=package_root, game_executable=None)
    for child in sorted(_safe_iterdir(package_root), key=lambda p: p.name):
        if child.is_dir() and PACKAGE_DIR_RE.match(child.name) and _has_pamt(child):
            layout.package_dirs.append(child.name)
    layout.has_papgt = (package_root / "meta" / "0.papgt").is_file()
    for exe in ("bin64/CrimsonDesert.exe", "CrimsonDesert.exe", "bin64/crimsondesert.exe"):
        if (root / exe).is_file():
            layout.game_executable = root / exe
            break
    if not layout.package_dirs:
        layout.notes.append("No numbered package directories with .pamt indexes were found.")
    if layout.game_executable is None:
        layout.notes.append("CrimsonDesert.exe was not found (not required for analysis).")
    return layout


def _safe_iterdir(path: Path) -> List[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def walk_installation(root: Path, skip_dirs: tuple = ("cdmods", ".git")) -> Iterator[DiscoveredFile]:
    """Enumerate every file under ``root`` using read-only directory listing."""

    root = Path(root)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() not in skip_dirs:
                        stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            rel = Path(entry.path).relative_to(root).as_posix()
            name = entry.name.lower()
            if name.endswith(".pamt"):
                kind = "pamt"
            elif name.endswith(".paz"):
                kind = "paz"
            elif name.endswith(".papgt"):
                kind = "papgt"
            else:
                kind = "loose"
            yield DiscoveredFile(rel, entry.path, stat.st_size, stat.st_mtime_ns, kind)


def is_audio_relevant(path: str) -> bool:
    """Decide whether a (virtual or loose) path is worth deep analysis."""

    low = path.lower().replace("\\", "/")
    dot = low.rfind(".")
    ext = low[dot:] if dot >= 0 else ""
    if ext in AUDIO_EXTENSIONS:
        return True
    parts = low.split("/")
    in_audio_dir = any(any(tok in part for tok in AUDIO_DIR_TOKENS) for part in parts[:-1])
    name = parts[-1]
    if ext in AUDIO_METADATA_EXTENSIONS and (in_audio_dir or any(tok in name for tok in AUDIO_NAME_TOKENS)):
        return True
    return in_audio_dir and ext not in {".dds", ".pac", ".pam", ".pab", ".hkx", ".paa"}


def default_steam_candidates() -> List[Path]:
    """Common install locations (only used to pre-fill the folder picker)."""

    out: List[Path] = []
    if os.name != "nt":
        return out
    for drive in "CDEFGH":
        for rel in (
            r"Program Files (x86)\Steam\steamapps\common\Crimson Desert",
            r"SteamLibrary\steamapps\common\Crimson Desert",
            r"Steam\steamapps\common\Crimson Desert",
            r"Games\Crimson Desert",
            r"Epic Games\CrimsonDesert",
        ):
            path = Path(f"{drive}:\\") / rel
            if path.is_dir():
                out.append(path)
    return out
