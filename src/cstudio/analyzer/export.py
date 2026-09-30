"""Read-only lookup and export of single installation files (debugging aid).

A file is addressed as ``<package>/<path>`` (e.g. ``0004/sound/windows/412724365.bnk``), as the bare archive path
(``sound/windows/412724365.bnk``, first match) or as ``loose:<relative path>``. Archive entries are decrypted and
decompressed exactly as the analyzer reads them. Exports are written only below the application's ``output/``
folder; the installation is never written to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple, Union

from cstudio.app_paths import AppPaths
from cstudio.db.database import Database
from cstudio.formats.paz import ArchiveReader, PamtEntry


class FileLookupError(Exception):
    pass


def installation_root(db: Database, inst_id: int) -> Optional[Path]:
    row = db.query_one("SELECT root_path FROM installation WHERE id=?", (inst_id,))
    return Path(row["root_path"]) if row else None


def _loose(root: Optional[Path], rel: str) -> Path:
    if root is None:
        raise FileLookupError("no installation")
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise FileLookupError("path escapes the installation folder") from exc
    if not target.is_file():
        raise FileLookupError(f"no such file {rel}")
    return target


def resolve(db: Database, inst_id: int, path: str) -> Tuple[str, Union[PamtEntry, Path]]:
    """Return ``("archive", PamtEntry)`` or ``("loose", Path)``. Only installation files are reachable."""

    root = installation_root(db, inst_id)
    path = str(path).strip().replace("\\", "/")
    if path.startswith("loose:"):
        return "loose", _loose(root, path[6:])
    if path.startswith("archive:"):
        path = path[8:]
    package, _, vpath = path.partition("/")
    row = db.query_one("SELECT * FROM archive_entry WHERE installation_id=? AND package=? AND vpath=?",
                       (inst_id, package, vpath))
    if row is None:
        row = db.query_one("SELECT * FROM archive_entry WHERE installation_id=? AND vpath=? ORDER BY package LIMIT 1",
                           (inst_id, path))
    if row is None:
        if root and (root / path).is_file():
            return "loose", _loose(root, path)
        raise FileLookupError(f"no file '{path}' in the scanned installation")
    pamt = db.query_one("SELECT rel_path FROM source_file WHERE installation_id=? AND kind='pamt' AND rel_path LIKE ?",
                        (inst_id, f"{row['package']}/%.pamt"))
    if pamt is None or root is None:
        raise FileLookupError("the archive index for this file is no longer present - rescan the game")
    base = root / Path(pamt["rel_path"]).parent
    entry = PamtEntry(row["vpath"], row["package"], str(root / pamt["rel_path"]), str(base / f"{row['paz_index']}.paz"),
                      row["paz_index"], row["offset"], row["comp_size"], row["orig_size"], row["flags"])
    return "archive", entry


def read_file(db: Database, inst_id: int, path: str, limit: Optional[int] = None,
              reader: Optional[ArchiveReader] = None) -> bytes:
    kind, ref = resolve(db, inst_id, path)
    if kind == "archive":
        own = reader is None
        reader = reader or ArchiveReader()
        try:
            return reader.read(ref, limit=limit)
        finally:
            if own:
                reader.close()
    with open(ref, "rb") as handle:  # read-only
        return handle.read(limit) if limit else handle.read()


def export_file(db: Database, paths: AppPaths, inst_id: int, path: str) -> Path:
    """Copy one (decrypted, decompressed) installation file to ``output/exports/``."""

    kind, ref = resolve(db, inst_id, path)
    data = read_file(db, inst_id, path)
    if kind == "archive":
        rel = Path(ref.package) / Path(ref.path)
    else:
        rel = Path("loose") / Path(ref).name
    target = (paths.output / "exports" / rel).resolve()
    if not paths.is_inside(target):
        raise FileLookupError("refusing to write outside the application folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target
