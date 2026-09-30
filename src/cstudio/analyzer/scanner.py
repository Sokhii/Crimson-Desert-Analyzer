"""Deterministic, cached, read-only scan of a Crimson Desert installation.

Pipeline::

    discover files -> index PAMT archives -> select audio-relevant entries
    -> analyze (BNK / WEM / XML / unknown) with caching
    -> names -> relationships -> music candidates -> unknown structures -> reports

The scan never writes inside the installation. Every open() on game data is
read-only (``ArchiveReader`` and ``_read_loose``).
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from cstudio.app_paths import AppPaths
from cstudio.db.database import Database, dumps, now_iso
from cstudio.formats import bnk as bnkfmt
from cstudio.formats.paz import ArchiveError, ArchiveReader, PamtEntry, parse_pamt, parse_papgt
from cstudio.formats.soundbanksinfo import parse_soundbanksinfo
from cstudio.formats.wem import parse_wem
from cstudio.knowledge.store import KnowledgeStore

from . import music, names, relationships, reports, unknowns
from .discovery import (AUDIO_DIR_TOKENS, AUDIO_EXTENSIONS, AUDIO_METADATA_EXTENSIONS, AUDIO_NAME_TOKENS, DiscoveredFile,
                        detect_layout, is_audio_relevant, walk_installation)

PARSER_VERSION = 1
WEM_HEADER_BYTES = 256 * 1024


class ScanCancelled(Exception):
    pass


@dataclass
class ScanStats:
    phase: str = "starting"
    current: str = ""
    files_discovered: int = 0
    archive_entries: int = 0
    candidates_total: int = 0
    files_analyzed: int = 0
    files_cached: int = 0
    bnks_parsed: int = 0
    wems_discovered: int = 0
    hirc_objects: int = 0
    music_candidates: int = 0
    unknown_structures: int = 0
    ai_findings: int = 0
    errors: int = 0
    elapsed: float = 0.0
    error_messages: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["error_messages"] = self.error_messages[-50:]
        return data


@dataclass
class ScanResult:
    scan_id: int
    installation_id: int
    stats: ScanStats
    report_dir: Optional[Path]
    status: str


ProgressCallback = Callable[[ScanStats], None]


class Scanner:
    def __init__(
        self,
        db: Database,
        paths: AppPaths,
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[threading.Event] = None,
        full_hash: bool = False,
    ) -> None:
        self.db = db
        self.paths = paths
        self.progress = progress
        self.cancel = cancel or threading.Event()
        self.full_hash = full_hash
        self.stats = ScanStats()
        self._last_emit = 0.0
        self._started = 0.0
        self.knowledge = KnowledgeStore(db, paths)

    # ---------------------------------------------------------------- utils
    def _emit(self, force: bool = False) -> None:
        now = time.monotonic()
        self.stats.elapsed = round(now - self._started, 2)
        if self.progress and (force or now - self._last_emit > 0.15):
            self._last_emit = now
            self.progress(self.stats)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise ScanCancelled()

    def _error(self, message: str) -> None:
        self.stats.errors += 1
        self.stats.error_messages.append(message)

    # ------------------------------------------------------------------ run
    def run(self, root: Path, mode: str = "analyze", allow_inside_app: bool = False) -> ScanResult:
        self._started = time.monotonic()
        root = Path(root).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"installation folder not found: {root}")
        if self.paths.is_inside(root) and not allow_inside_app:
            raise ValueError("the installation folder must not be inside the application folder")
        layout = detect_layout(root)
        inst_id = self.db.get_or_create_installation(str(root), label=root.name)
        cur = self.db.execute(
            "INSERT INTO scan(installation_id, started_at, status, mode, parser_version) VALUES (?,?,?,?,?)",
            (inst_id, now_iso(), "running", mode, PARSER_VERSION),
        )
        scan_id = int(cur.lastrowid)
        self.db.commit()
        self.knowledge.sync_from_files()
        status = "completed"
        report_dir: Optional[Path] = None
        try:
            with ArchiveReader() as reader:
                self._reader = reader
                files = self._discover(root, inst_id, scan_id)
                mount_order = self._mount_order(root, layout.package_root)
                self._index_archives(root, inst_id, scan_id, files)
                candidates = self._select_candidates(inst_id, files, mount_order)
                self._analyze_all(root, inst_id, scan_id, candidates, files)
                self._finish_assets(inst_id, scan_id)
                self._post_process(inst_id, scan_id)
                report_dir = reports.write_all(self.db, self.paths, inst_id, scan_id, self.stats.to_dict(), layout)
        except ScanCancelled:
            status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - record and re-raise after bookkeeping
            status = "failed"
            self._error(f"scan failed: {exc!r}")
            self._finalize(scan_id, inst_id, status, str(exc))
            raise
        self._finalize(scan_id, inst_id, status, None)
        return ScanResult(scan_id, inst_id, self.stats, report_dir, status)

    def _finalize(self, scan_id: int, inst_id: int, status: str, error: Optional[str]) -> None:
        self.stats.phase = status
        self._emit(force=True)
        self.db.execute(
            "UPDATE scan SET finished_at=?, status=?, stats_json=?, error=? WHERE id=?",
            (now_iso(), status, dumps(self.stats.to_dict()), error, scan_id),
        )
        self.db.execute("UPDATE installation SET last_scanned=? WHERE id=?", (now_iso(), inst_id))
        self.db.commit()

    # ------------------------------------------------------------ discovery
    def _discover(self, root: Path, inst_id: int, scan_id: int) -> Dict[str, DiscoveredFile]:
        self.stats.phase = "discovering files"
        files: Dict[str, DiscoveredFile] = {}
        for item in walk_installation(root):
            files[item.rel_path] = item
            self.stats.files_discovered += 1
            if self.stats.files_discovered % 200 == 0:
                self._check_cancel()
                self.stats.current = item.rel_path
                self._emit()
        self._previous_files = {
            r["rel_path"]: (r["size"], r["mtime_ns"])
            for r in self.db.query("SELECT rel_path, size, mtime_ns FROM source_file WHERE installation_id=?", (inst_id,))
        }
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT INTO source_file(installation_id, rel_path, kind, size, mtime_ns, last_seen_scan) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(installation_id, rel_path) DO UPDATE SET kind=excluded.kind, size=excluded.size,"
                " mtime_ns=excluded.mtime_ns, last_seen_scan=excluded.last_seen_scan",
                [(inst_id, f.rel_path, f.kind, f.size, f.mtime_ns, scan_id) for f in files.values()],
            )
            conn.execute("DELETE FROM source_file WHERE installation_id=? AND last_seen_scan<>?", (inst_id, scan_id))
        self._emit(force=True)
        return files

    def _mount_order(self, root: Path, package_root: Path) -> List[str]:
        papgt = package_root / "meta" / "0.papgt"
        if not papgt.is_file():
            return []
        try:
            with open(papgt, "rb") as handle:
                return [name for name, _flags, _crc in parse_papgt(handle.read())]
        except (OSError, ArchiveError) as exc:
            self._error(f"meta/0.papgt could not be parsed: {exc}")
            return []

    # --------------------------------------------------------------- index
    def _index_archives(self, root: Path, inst_id: int, scan_id: int, files: Dict[str, DiscoveredFile]) -> None:
        self.stats.phase = "indexing archives"
        pamts = [f for f in files.values() if f.kind == "pamt"]
        known_packages = {
            r["package"] for r in self.db.query("SELECT DISTINCT package FROM archive_entry WHERE installation_id=?", (inst_id,))
        }
        live_packages = set()
        for pamt in pamts:
            self._check_cancel()
            package = self._package_for(pamt)
            live_packages.add(package)
            self.stats.current = pamt.rel_path
            previous = self._previous_files.get(pamt.rel_path)
            if previous == (pamt.size, pamt.mtime_ns) and package in known_packages:
                count = self.db.scalar(
                    "SELECT COUNT(*) FROM archive_entry WHERE installation_id=? AND package=?", (inst_id, package), 0
                )
                self.stats.archive_entries += int(count)
                self._emit()
                continue
            try:
                index = parse_pamt(Path(pamt.abs_path))
            except (OSError, ArchiveError) as exc:
                self._error(f"{pamt.rel_path}: {exc}")
                continue
            for warning in index.warnings:
                self._error(f"{pamt.rel_path}: {warning}")
            rows = {}
            for e in index.entries:
                rows[e.path] = (inst_id, package, e.path, e.extension, e.paz_index, e.offset, e.comp_size, e.orig_size, e.flags)
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM archive_entry WHERE installation_id=? AND package=?", (inst_id, package))
                conn.executemany(
                    "INSERT INTO archive_entry(installation_id, package, vpath, ext, paz_index, offset, comp_size, orig_size, flags)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    rows.values(),
                )
                conn.execute("DELETE FROM metadata WHERE entity_type='package' AND entity_key=? AND key='pamt_layout'", (package,))
                conn.execute(
                    "INSERT INTO metadata(entity_type, entity_key, key, value, source) VALUES ('package', ?, 'pamt_layout', ?, 'parser')",
                    (package, index.layout),
                )
            self.stats.archive_entries += len(rows)
            self._emit()
        stale = known_packages - live_packages
        if stale:
            with self.db.transaction() as conn:
                for package in stale:
                    conn.execute("DELETE FROM archive_entry WHERE installation_id=? AND package=?", (inst_id, package))

    @staticmethod
    def _package_for(pamt: DiscoveredFile) -> str:
        rel = Path(pamt.rel_path)
        parent = rel.parent.as_posix()
        return parent if rel.stem == "0" else f"{parent}/{rel.stem}"

    # ---------------------------------------------------------- candidates
    def _select_candidates(self, inst_id: int, files: Dict[str, DiscoveredFile], mount_order: List[str]) -> List[dict]:
        self.stats.phase = "selecting audio assets"
        rank = {name: i for i, name in enumerate(mount_order)}
        candidates: List[dict] = []
        pamt_by_package = {self._package_for(f): f for f in files.values() if f.kind == "pamt"}
        paz_stats = {f.rel_path: (f.size, f.mtime_ns) for f in files.values() if f.kind == "paz"}
        seen_vpaths: Dict[str, Tuple[int, str]] = {}
        exts = sorted(AUDIO_EXTENSIONS | AUDIO_METADATA_EXTENSIONS)
        tokens = sorted(set(AUDIO_DIR_TOKENS) | set(AUDIO_NAME_TOKENS))
        where = " OR ".join(["ext IN (%s)" % ",".join("?" * len(exts))] + ["lower(vpath) LIKE ?"] * len(tokens))
        rows = self.db.query(f"SELECT * FROM archive_entry WHERE installation_id=? AND ({where})",
                             (inst_id, *exts, *[f"%{t}%" for t in tokens]))
        for row in rows:
            if not is_audio_relevant(row["vpath"]):
                continue
            package = row["package"]
            pamt = pamt_by_package.get(package)
            if pamt is None:
                continue
            base = Path(pamt.abs_path).parent
            paz_rel = (Path(pamt.rel_path).parent / f"{row['paz_index']}.paz").as_posix()
            entry = PamtEntry(
                path=row["vpath"], package=package, pamt_path=pamt.abs_path,
                paz_path=str(base / f"{row['paz_index']}.paz"), paz_index=row["paz_index"], offset=row["offset"],
                comp_size=row["comp_size"], orig_size=row["orig_size"], flags=row["flags"],
            )
            fingerprint = hashlib.sha1(
                dumps([entry.identity(), paz_stats.get(paz_rel), PARSER_VERSION]).encode()
            ).hexdigest()
            candidates.append({
                "origin": "archive", "locator": f"archive:{package}/{row['vpath']}", "vpath": row["vpath"],
                "ext": row["ext"], "size": row["orig_size"], "entry": entry, "archive_entry_id": row["id"],
                "fingerprint": fingerprint, "package": package,
            })
            order = rank.get(package.split("/")[0], len(rank) + 1)
            prior = seen_vpaths.get(row["vpath"])
            if prior is None or order < prior[0]:
                seen_vpaths[row["vpath"]] = (order, package)
        # shadowing information (the game reads the first mounted package holding a path)
        for cand in candidates:
            winner = seen_vpaths.get(cand["vpath"])
            cand["shadowed"] = bool(winner and winner[1] != cand["package"])
        for f in files.values():
            if f.kind != "loose" or not is_audio_relevant(f.rel_path):
                continue
            ext = Path(f.rel_path).suffix.lower()
            fingerprint = hashlib.sha1(dumps([f.rel_path, f.size, f.mtime_ns, PARSER_VERSION]).encode()).hexdigest()
            candidates.append({
                "origin": "loose", "locator": f"loose:{f.rel_path}", "vpath": f.rel_path, "ext": ext, "size": f.size,
                "abs_path": f.abs_path, "fingerprint": fingerprint, "shadowed": False,
            })
        self.stats.candidates_total = len(candidates)
        self._emit(force=True)
        return candidates

    # ------------------------------------------------------------- analyze
    def _analyze_all(self, root: Path, inst_id: int, scan_id: int, candidates: List[dict], files) -> None:
        self.stats.phase = "analyzing audio assets"
        existing = {
            r["locator"]: r
            for r in self.db.query(
                "SELECT id, locator, fingerprint, parser_version, status FROM asset WHERE installation_id=? AND origin<>'embedded'",
                (inst_id,),
            )
        }
        pending_touch: List[Tuple[int, int]] = []
        order = sorted(candidates, key=lambda c: ({".xml": 0, ".bnk": 1}.get(c["ext"], 2), c["locator"]))
        for i, cand in enumerate(order):
            if i % 25 == 0:
                self._check_cancel()
            self.stats.current = cand["vpath"]
            old = existing.get(cand["locator"])
            if old and old["fingerprint"] == cand["fingerprint"] and old["parser_version"] == PARSER_VERSION and old["status"] != "error":
                pending_touch.append((scan_id, old["id"]))
                self.stats.files_cached += 1
                if cand["ext"] == ".bnk":
                    self.stats.bnks_parsed += 1
                self._emit()
                continue
            try:
                with self.db.transaction() as conn:
                    if old:
                        self._delete_asset(conn, old["id"])
                    self._analyze_one(conn, inst_id, scan_id, cand)
                self.stats.files_analyzed += 1
            except ScanCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the scan
                self._error(f"{cand['locator']}: {exc!r}")
                with self.db.transaction() as conn:
                    for row in conn.execute("SELECT id FROM asset WHERE installation_id=? AND locator=?",
                                            (inst_id, cand["locator"])).fetchall():
                        self._delete_asset(conn, row[0])
                    self._insert_asset(conn, inst_id, scan_id, cand, None, None, "error", repr(exc))
            self._emit()
        with self.db.transaction() as conn:
            conn.executemany("UPDATE asset SET last_seen_scan=? WHERE id=?", pending_touch)
            conn.execute(
                "UPDATE asset SET last_seen_scan=? WHERE parent_asset_id IN (SELECT id FROM asset WHERE last_seen_scan=?)",
                (scan_id, scan_id),
            )
        self.stats.wems_discovered = int(self.db.scalar(
            "SELECT COUNT(DISTINCT w.source_id) FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=?",
            (inst_id,), 0))
        self._emit(force=True)

    @staticmethod
    def _delete_asset(conn, asset_id: int) -> None:
        ids = [asset_id] + [r[0] for r in conn.execute("SELECT id FROM asset WHERE parent_asset_id=?", (asset_id,))]
        conn.executemany("DELETE FROM metadata WHERE entity_type='asset' AND entity_key=?", [(str(i),) for i in ids])
        conn.execute("DELETE FROM asset WHERE id=?", (asset_id,))

    def _read(self, cand: dict, limit: Optional[int] = None) -> bytes:
        if cand["origin"] == "archive":
            return self._reader.read(cand["entry"], limit=limit)
        with open(cand["abs_path"], "rb") as handle:  # read-only
            return handle.read(limit) if limit else handle.read()

    def _insert_asset(self, conn, inst_id, scan_id, cand, content_hash, hash_kind, status, error=None,
                      parent_id=None) -> int:
        cur = conn.execute(
            "INSERT INTO asset(installation_id, origin, locator, vpath, ext, size, archive_entry_id, parent_asset_id,"
            " fingerprint, content_hash, hash_kind, parser_version, analyzed_scan_id, last_seen_scan, status, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (inst_id, cand["origin"], cand["locator"], cand["vpath"], cand["ext"], cand["size"],
             cand.get("archive_entry_id"), parent_id, cand["fingerprint"], content_hash, hash_kind,
             PARSER_VERSION, scan_id, scan_id, status, error),
        )
        return int(cur.lastrowid)

    def _analyze_one(self, conn, inst_id: int, scan_id: int, cand: dict) -> None:
        ext = cand["ext"]
        if ext == ".wem":
            data = self._read(cand, None if self.full_hash else WEM_HEADER_BYTES)
            full = len(data) >= cand["size"]
            content_hash = hashlib.sha1(data).hexdigest()
            hash_kind = "sha1" if full else f"sha1_prefix_{len(data)}"
            asset_id = self._insert_asset(conn, inst_id, scan_id, cand, content_hash, hash_kind, "ok")
            info = parse_wem(data, cand["size"])
            stem = Path(cand["vpath"]).stem
            source_id = int(stem) if stem.isdigit() else -1
            self._insert_wem(conn, asset_id, source_id, "loose", None, info, {"shadowed": cand.get("shadowed", False)})
            return
        data = self._read(cand)
        content_hash = hashlib.sha1(data).hexdigest()
        if ext == ".bnk" or bnkfmt.is_bank(data):
            asset_id = self._insert_asset(conn, inst_id, scan_id, cand, content_hash, "sha1", "ok")
            self._store_bank(conn, inst_id, scan_id, asset_id, cand, data)
            self.stats.bnks_parsed += 1
            return
        head = data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
        if head.startswith(b"<"):
            asset_id = self._insert_asset(conn, inst_id, scan_id, cand, content_hash, "sha1", "ok")
            self._store_xml(conn, asset_id, data)
            return
        if data[:4] in (b"RIFF", b"RIFX"):
            asset_id = self._insert_asset(conn, inst_id, scan_id, cand, content_hash, "sha1", "ok")
            info = parse_wem(data, len(data))
            self._insert_wem(conn, asset_id, -1, "loose", None, info, {"note": "RIFF audio with non-.wem extension"})
            return
        asset_id = self._insert_asset(conn, inst_id, scan_id, cand, content_hash, "sha1", "unrecognized")
        conn.execute(
            "INSERT INTO metadata(entity_type, entity_key, key, value, source) VALUES ('asset', ?, 'magic', ?, 'parser')",
            (str(asset_id), data[:16].hex()),
        )

    def _insert_wem(self, conn, asset_id, source_id, container, bank_asset_id, info, extra) -> None:
        details = info.to_dict()
        details.update(extra or {})
        conn.execute(
            "INSERT INTO wem(asset_id, source_id, container, bank_asset_id, valid, codec, format_tag, channels, sample_rate,"
            " duration_s, duration_method, sample_count, data_size, loops_json, cues_json, details_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (asset_id, source_id, container, bank_asset_id, int(info.valid), info.codec, info.format_tag, info.channels,
             info.sample_rate, info.duration_seconds, info.duration_method, info.sample_count, info.data_size,
             dumps(info.loops), dumps({"cues": info.cue_points, "labels": info.labels}), dumps(details)),
        )

    def _store_bank(self, conn, inst_id: int, scan_id: int, asset_id: int, cand: dict, data: bytes) -> None:
        bank = bnkfmt.parse_bank(data)
        conn.execute(
            "INSERT INTO bnk(asset_id, bank_id, version, language_id, project_id, bank_type, object_count, media_count,"
            " decoded_version, chunks_json, type_counts_json, errors_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (asset_id, bank.bank_id, bank.version, bank.language_id, bank.project_id, bank.bank_type, len(bank.objects),
             len(bank.media), int(bank.decoded_version),
             dumps([{"tag": c.tag, "offset": c.offset, "size": c.size} for c in bank.chunks]),
             dumps(bank.type_counts()), dumps(bank.errors)),
        )
        for bank_id, name in bank.names.items():
            conn.execute(
                "INSERT INTO metadata(entity_type, entity_key, key, value, source) VALUES ('asset', ?, 'stid_name', ?, 'bnk_stid')",
                (str(asset_id), dumps({"bank_id": bank_id, "name": name})),
            )
        known_ids = {o.object_id for o in bank.objects} | {m.source_id for m in bank.media}
        obj_rows = []
        ref_rows = []
        src_rows = []
        for obj in bank.objects:
            obj_rows.append((asset_id, obj.object_id, obj.type_code, obj.type_name, obj.offset, obj.size,
                             obj.parse_status, obj.error, dumps(obj.fields) if obj.fields else None))
            for kind, target in obj.refs:
                ref_rows.append((asset_id, obj.object_id, target, kind, "parsed", None))
            if obj.parse_status in ("partial", "failed", "header_only") and obj.leftover:
                for rel, target in bnkfmt.heuristic_id_scan(data, obj, known_ids)[:64]:
                    ref_rows.append((asset_id, obj.object_id, target, "heuristic_id", "heuristic", rel))
            if obj.type_code == 0x02 and "source" in obj.fields:
                src = obj.fields["source"]
                src_rows.append((asset_id, obj.object_id, "Sound", src["source_id"], src["stream_type"],
                                 src["in_memory_size"], src["plugin_id"], dumps(src)))
            if obj.type_code == 0x0B:
                playlist = {p["source_id"]: p for p in obj.fields.get("playlist", [])}
                for src in obj.fields.get("sources", []):
                    detail = dict(src)
                    clip = playlist.get(src["source_id"])
                    if clip:
                        detail["clip"] = clip
                    src_rows.append((asset_id, obj.object_id, "MusicTrack", src["source_id"], src["stream_type"],
                                     src["in_memory_size"], src["plugin_id"], dumps(detail)))
        conn.executemany(
            "INSERT INTO wwise_object(bank_asset_id, object_id, type_code, type_name, offset, size, parse_status, error, fields_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)", obj_rows)
        conn.executemany(
            "INSERT INTO object_ref(bank_asset_id, from_object_id, to_id, kind, confidence, rel_offset) VALUES (?,?,?,?,?,?)",
            ref_rows)
        conn.executemany(
            "INSERT INTO media_source(bank_asset_id, object_id, owner_type, source_id, stream_type, in_memory_size, plugin_id,"
            " details_json) VALUES (?,?,?,?,?,?,?,?)", src_rows)
        self.stats.hirc_objects += len(obj_rows)
        for chunk in bank.unknown_chunks:
            conn.execute(
                "INSERT INTO metadata(entity_type, entity_key, key, value, source) VALUES ('asset', ?, 'unknown_chunk', ?, 'parser')",
                (str(asset_id), dumps({"tag": chunk.tag, "size": chunk.size, "head": data[chunk.offset:chunk.offset + 32].hex()})),
            )
        for media in bank.media:
            end = media.offset + media.size
            if end > len(data):
                self._error(f"{cand['locator']}: embedded media {media.source_id} exceeds bank size")
                continue
            blob = data[media.offset:end]
            child = {
                "origin": "embedded", "locator": f"embedded:{cand['locator']}#{media.source_id}@{media.offset}",
                "vpath": f"{cand['vpath']}#{media.source_id}.wem", "ext": ".wem", "size": media.size,
                "fingerprint": cand["fingerprint"],
            }
            child_id = self._insert_asset(conn, inst_id, scan_id, child, hashlib.sha1(blob).hexdigest(), "sha1", "ok",
                                          parent_id=asset_id)
            info = parse_wem(blob, media.size)
            self._insert_wem(conn, child_id, media.source_id, "embedded", asset_id, info,
                             {"didx_index": media.index, "bank_offset": media.offset})

    def _store_xml(self, conn, asset_id: int, data: bytes) -> None:
        info = parse_soundbanksinfo(data)
        conn.execute(
            "INSERT INTO xml_doc(asset_id, root_tag, schema_version, soundbank_version, recognized, tag_counts_json,"
            " unrecognized_json, errors_json) VALUES (?,?,?,?,?,?,?,?)",
            (asset_id, info.root_tag, info.schema_version, info.soundbank_version, int(info.recognized_schema),
             dumps(info.tag_counts), dumps(info.unrecognized_tags), dumps(info.errors)),
        )
        conn.executemany(
            "INSERT INTO xml_bank(asset_id, bank_id, name, path, object_path, language, attrs_json) VALUES (?,?,?,?,?,?,?)",
            [(asset_id, b.bank_id, b.name, b.path, b.object_path, b.language, dumps(b.attrs)) for b in info.banks])
        conn.executemany(
            "INSERT INTO xml_event(asset_id, event_id, name, object_path, bank_id, attrs_json) VALUES (?,?,?,?,?,?)",
            [(asset_id, e.event_id, e.name, e.object_path, e.bank_id, dumps(e.attrs)) for e in info.events])
        conn.executemany(
            "INSERT INTO xml_media(asset_id, media_id, short_name, path, cache_path, language, streaming, location, bank_id,"
            " relation, prefetch_size) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(asset_id, m.media_id, m.short_name, m.path, m.cache_path, m.language,
              None if m.streaming is None else int(m.streaming), m.location, m.bank_id, m.relation, m.prefetch_size)
             for m in info.media])
        conn.executemany(
            "INSERT INTO xml_object(asset_id, kind, object_id, name, parent_id, object_path) VALUES (?,?,?,?,?,?)",
            [(asset_id, o.kind, o.object_id, o.name, o.parent_id, o.object_path) for o in info.named_objects])

    def _finish_assets(self, inst_id: int, scan_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM asset WHERE installation_id=? AND (last_seen_scan IS NULL OR last_seen_scan<>?)",
                         (inst_id, scan_id))
            conn.execute("DELETE FROM metadata WHERE entity_type='asset' AND CAST(entity_key AS INTEGER) NOT IN (SELECT id FROM asset)")

    # --------------------------------------------------------- post-process
    def _post_process(self, inst_id: int, scan_id: int) -> None:
        self._check_cancel()
        self.stats.phase = "resolving names"
        self._emit(force=True)
        names.rebuild(self.db, inst_id, self.knowledge)
        self._check_cancel()
        self.stats.phase = "resolving relationships"
        self._emit(force=True)
        relationships.resolve(self.db, inst_id)
        self._check_cancel()
        self.stats.phase = "identifying music"
        self._emit(force=True)
        self.stats.music_candidates = music.classify(self.db, inst_id, scan_id)
        self._check_cancel()
        self.stats.phase = "collecting unknown structures"
        self._emit(force=True)
        self.stats.unknown_structures = unknowns.detect(self.db, inst_id, scan_id, self.knowledge)
        self.stats.ai_findings = int(self.db.scalar("SELECT COUNT(*) FROM finding WHERE created_by LIKE 'ai%'", (), 0))
        self.stats.phase = "writing reports"
        self._emit(force=True)
