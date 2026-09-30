"""Controlled tools the local AI investigator may call.

Safety model:

* Game data is only ever *read* (``ArchiveReader`` / ``open(..., "rb")``),
  and only for paths that resolve to the scanned installation.
* The only writes are findings/hypotheses through ``KnowledgeStore``.
* ``verified`` status can only be reached through :meth:`mark_verified`,
  which runs a deterministic check (hash match, object/reference/media
  existence, byte pattern) - never on the model's say-so.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cstudio.analyzer import queries
from cstudio.analyzer.relationships import object_neighbourhood
from cstudio.app_paths import AppPaths
from cstudio.db.database import Database, loads
from cstudio.formats import bnk as bnkfmt
from cstudio.formats.hashing import wwise_fnv1_32
from cstudio.formats.paz import ArchiveReader, PamtEntry
from cstudio.formats.wem import parse_wem
from cstudio.knowledge.store import CATEGORY_NAME_MAPPING, KnowledgeStore

MAX_RESULT_CHARS = 3500
RESEARCH_DIRS = [Path(__file__).resolve().parent.parent / "resources" / "research",
                 Path(__file__).resolve().parents[3] / "docs" / "research"]


class ToolError(Exception):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    params: Dict[str, str]
    fn: Callable[..., Any]


def _hexdump(data: bytes, base: int = 0, width: int = 16, limit: int = 512) -> str:
    lines = []
    printable = set(string.printable.encode()) - set(b"\t\n\r\x0b\x0c")
    for off in range(0, min(len(data), limit), width):
        chunk = data[off:off + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if b in printable else "." for b in chunk)
        lines.append(f"{base + off:08x}  {hexpart:<{width * 3}} {asc}")
    return "\n".join(lines)


def _strings(data: bytes, min_len: int = 4, limit: int = 60) -> List[str]:
    out = []
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data):
        out.append(m.group().decode("ascii"))
        if len(out) >= limit:
            break
    return out


def _u32_view(data: bytes, limit: int = 64) -> List[str]:
    out = []
    for off in range(0, min(len(data) - 3, limit * 4), 4):
        out.append(f"+{off:#05x}: {int.from_bytes(data[off:off + 4], 'little')}")
    return out


class InvestigationTools:
    def __init__(self, db: Database, paths: AppPaths, inst_id: int, knowledge: KnowledgeStore,
                 model_id: Optional[str] = None, session_id: Optional[int] = None) -> None:
        self.db = db
        self.paths = paths
        self.inst_id = inst_id
        self.knowledge = knowledge
        self.model_id = model_id
        self.session_id = session_id
        self.reader = ArchiveReader()
        row = db.query_one("SELECT root_path FROM installation WHERE id=?", (inst_id,))
        self.root = Path(row["root_path"]) if row else None
        self.specs: Dict[str, ToolSpec] = {}
        self._register()

    def close(self) -> None:
        self.reader.close()

    # ------------------------------------------------------------ registry
    def _add(self, name: str, description: str, params: Dict[str, str], fn: Callable[..., Any]) -> None:
        self.specs[name] = ToolSpec(name, description, params, fn)

    def _register(self) -> None:
        a = self._add
        a("list_files", "List archive files, optionally filtered by package and extension.",
          {"package": "optional package dir e.g. '0004'", "ext": "optional extension e.g. '.bnk'", "limit": "max rows (default 50)"},
          self.list_files)
        a("search_files", "Search every indexed file path (all packages) with a substring or * wildcard pattern.",
          {"pattern": "e.g. 'bgm' or 'sound/*.xml'", "limit": "max rows"}, self.search_files)
        a("get_file_metadata", "Archive/loose metadata for a file path (sizes, compression, encryption, package).",
          {"path": "'<package>/<path>' or 'loose:<relative path>'"}, self.get_file_metadata)
        a("inspect_file", "Read-only hex dump, strings and u32 view of part of a file.",
          {"path": "file path", "offset": "byte offset (default 0)", "length": "bytes (default 256, max 1024)"}, self.inspect_file)
        a("extract_strings", "Printable strings in a file (useful for game data tables that mention audio names).",
          {"path": "file path", "min_length": "default 4", "limit": "default 80", "contains": "optional filter"}, self.extract_strings)
        a("compare_files", "Compare two files: sizes, hashes, first differing offsets.", {"a": "path", "b": "path"}, self.compare_files)
        a("inspect_bnk", "Parsed summary of a soundbank: chunks, version, object type counts, parse statuses.",
          {"bank": "bank id, asset id, or path fragment"}, self.inspect_bnk)
        a("get_bnk_contents", "Objects (id, type, parse status, name) and embedded media of a bank.",
          {"bank": "bank id / path fragment", "limit": "max objects (default 120)"}, self.get_bnk_contents)
        a("inspect_object_bytes", "Hex dump of a HIRC object's body with how far the parser got. Key tool for unknown structures.",
          {"object_id": "HIRC object id", "bank": "optional bank path fragment", "length": "bytes (default 256)"}, self.inspect_object_bytes)
        a("inspect_wem", "Parse a WEM header from a path or media id (codec, channels, rate, duration, loops).",
          {"target": "media id or file path"}, self.inspect_wem)
        a("get_wem_info", "Everything known about one media id: files, owners, containers, events, banks, classification, evidence.",
          {"source_id": "media (WEM) id"}, self.get_wem_info)
        a("get_object", "Parsed HIRC object(s) with this id, including fields and outgoing references.",
          {"object_id": "id"}, self.get_object)
        a("get_container", "An object plus its surrounding graph (parents, children, media).",
          {"object_id": "id", "depth": "1-3 (default 2)"}, self.get_container)
        a("find_references", "Every place a numeric id appears: as object, media, bank, name, or reference target.",
          {"id": "numeric id"}, self.find_references)
        a("find_referencing_objects", "Objects whose parsed/heuristic references point at this id.",
          {"id": "numeric id"}, self.find_referencing_objects)
        a("search_music_assets", "Search classified media (music/likely/possible) by name, id, bank or event.",
          {"query": "text", "limit": "default 20"}, self.search_music_assets)
        a("get_unknown_structures", "List unresolved structures found by the scanner.",
          {"status": "open|explained|resolved (default open)", "limit": "default 25"}, self.get_unknown_structures)
        a("get_unknown_structure", "Details and examples for one unknown structure signature.", {"signature": "signature"},
          self.get_unknown_structure)
        a("hash_name", "Wwise FNV-1 id of a candidate name, and whether that id exists in the scanned data.",
          {"name": "candidate name"}, self.hash_name)
        a("test_names", "hash_name for up to 40 candidate names at once; returns only the matches plus a count.",
          {"names": "list of candidate names"}, self.test_names)
        a("consult_research", "Search bundled community/Wwise research notes.", {"query": "keywords"}, self.consult_research)
        a("list_findings", "Existing findings/hypotheses (to avoid duplicates and build on earlier work).",
          {"status": "optional status", "limit": "default 20"}, self.list_findings)
        a("record_hypothesis", "Record an untested hypothesis with the evidence gathered so far.",
          {"title": "short title", "statement": "the claim", "subject_type": "media|object|bank|unknown_structure|name|general",
           "subject_key": "id or signature", "evidence": "list of evidence strings", "reasoning": "why"}, self.record_hypothesis)
        a("record_finding", "Record a finding with status probable, hypothesis or unknown (verified requires mark_verified).",
          {"title": "", "statement": "", "status": "probable|hypothesis|unknown", "category": "e.g. structure|name_mapping|role|relationship",
           "subject_type": "", "subject_key": "", "evidence": "list", "reasoning": ""}, self.record_finding)
        a("mark_verified", "Promote a finding to verified by passing a deterministic check.",
          {"uid": "finding uid", "check": "one of {type:name_hash,name,id} {type:object_exists,id} {type:media_exists,id} "
           "{type:reference_exists,from,to} {type:bytes_match,path,offset,hex}"}, self.mark_verified)
        a("reject_finding", "Mark a finding rejected after a failed test (kept so it is not proposed again).",
          {"uid": "finding uid", "reason": "why"}, self.reject_finding)
        a("finish_task", "Finish the current task with a short summary.", {"summary": "what was learned"}, lambda summary="": {"finished": True, "summary": summary})
        a("stop_investigation", "Stop the whole investigation (nothing useful left to do).", {"reason": ""}, lambda reason="": {"stopped": True, "reason": reason})

    def describe(self) -> str:
        lines = []
        for spec in self.specs.values():
            params = ", ".join(f"{k}: {v}" if v else k for k, v in spec.params.items())
            lines.append(f"- {spec.name}({params}): {spec.description}")
        return "\n".join(lines)

    def call(self, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        spec = self.specs.get(name)
        if spec is None:
            return {"error": f"unknown tool '{name}'. Available: {', '.join(self.specs)}"}
        args = args if isinstance(args, dict) else {}
        allowed = set(spec.params)
        clean = {k: v for k, v in args.items() if k in allowed}
        try:
            result = spec.fn(**clean)
        except ToolError as exc:
            return {"error": str(exc)}
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - tool failures are reported to the model, not raised
            return {"error": f"{type(exc).__name__}: {exc}"}
        return result if isinstance(result, dict) else {"result": result}

    @staticmethod
    def truncate(result: Dict[str, Any], limit: int = MAX_RESULT_CHARS) -> str:
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) <= limit:
            return text
        return text[: limit - 60] + f'... [truncated {len(text) - limit + 60} chars]"'

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _int(value: Any, name: str = "id") -> int:
        try:
            if isinstance(value, str) and value.lower().startswith("0x"):
                return int(value, 16)
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"{name} must be an integer, got {value!r}") from exc

    def _resolve(self, path: str):
        """Resolve a path to (kind, entry_or_path). Only installation files are reachable."""

        path = str(path).strip().replace("\\", "/")
        if path.startswith("loose:"):
            rel = path[6:]
            return "loose", self._loose(rel)
        if path.startswith("archive:"):
            path = path[8:]
        package, _, vpath = path.partition("/")
        row = self.db.query_one(
            "SELECT * FROM archive_entry WHERE installation_id=? AND package=? AND vpath=?", (self.inst_id, package, vpath))
        if row is None:
            row = self.db.query_one("SELECT * FROM archive_entry WHERE installation_id=? AND vpath=? LIMIT 1", (self.inst_id, path))
        if row is None:
            if self.root and (self.root / path).is_file():
                return "loose", self._loose(path)
            raise ToolError(f"no file '{path}' in the scanned installation (use search_files)")
        pamt = self.db.query_one("SELECT rel_path FROM source_file WHERE installation_id=? AND kind='pamt' AND rel_path LIKE ?",
                                 (self.inst_id, f"{row['package']}/%.pamt"))
        if pamt is None or self.root is None:
            raise ToolError("archive index for this file is no longer present")
        base = self.root / Path(pamt["rel_path"]).parent
        entry = PamtEntry(row["vpath"], row["package"], str(self.root / pamt["rel_path"]), str(base / f"{row['paz_index']}.paz"),
                          row["paz_index"], row["offset"], row["comp_size"], row["orig_size"], row["flags"])
        return "archive", entry

    def _loose(self, rel: str) -> Path:
        if self.root is None:
            raise ToolError("no installation")
        target = (self.root / rel).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ToolError("path escapes the installation folder") from exc
        if not target.is_file():
            raise ToolError(f"no such file {rel}")
        return target

    def _read(self, path: str, limit: Optional[int] = None) -> bytes:
        kind, ref = self._resolve(path)
        if kind == "archive":
            return self.reader.read(ref, limit=limit)
        with open(ref, "rb") as handle:  # read-only
            return handle.read(limit) if limit else handle.read()

    # --------------------------------------------------------------- tools
    def list_files(self, package: str = "", ext: str = "", limit: int = 50):
        sql = "SELECT package, vpath, orig_size FROM archive_entry WHERE installation_id=?"
        params: List[Any] = [self.inst_id]
        if package:
            sql += " AND package=?"
            params.append(str(package))
        if ext:
            sql += " AND ext=?"
            params.append(ext if str(ext).startswith(".") else f".{ext}")
        sql += " ORDER BY package, vpath LIMIT ?"
        params.append(min(int(limit or 50), 300))
        rows = self.db.query(sql, params)
        return {"files": [f"{r['package']}/{r['vpath']} ({r['orig_size']} B)" for r in rows],
                "extension_summary": queries.extension_summary(self.db, self.inst_id)[:15] if not (package or ext) else None}

    def search_files(self, pattern: str, limit: int = 60):
        return {"matches": queries.search_files(self.db, self.inst_id, str(pattern), min(int(limit or 60), 300))}

    def get_file_metadata(self, path: str):
        kind, ref = self._resolve(path)
        if kind == "loose":
            stat = ref.stat()
            return {"kind": "loose", "size": stat.st_size, "path": str(ref)}
        asset = self.db.query_one("SELECT id, status, content_hash, hash_kind FROM asset WHERE installation_id=? AND locator=?",
                                  (self.inst_id, f"archive:{ref.package}/{ref.path}"))
        return {"kind": "archive", "package": ref.package, "path": ref.path, "paz_index": ref.paz_index, "offset": ref.offset,
                "stored_size": ref.comp_size, "size": ref.orig_size, "compression": ref.compression_type,
                "encryption": ref.encryption_type, "analyzed_asset": dict(asset) if asset else None}

    def inspect_file(self, path: str, offset: int = 0, length: int = 256):
        offset = max(0, int(offset or 0))
        length = max(16, min(int(length or 256), 1024))
        data = self._read(path, limit=offset + length)
        window = data[offset:offset + length]
        return {"size_read": len(data), "offset": offset, "hex": _hexdump(window, offset), "strings": _strings(window, 4, 20),
                "u32_le": _u32_view(window, 32)}

    def extract_strings(self, path: str, min_length: int = 4, limit: int = 80, contains: str = ""):
        data = self._read(path)
        found = _strings(data, max(3, int(min_length or 4)), 5000)
        if contains:
            found = [s for s in found if str(contains).lower() in s.lower()]
        return {"count": len(found), "strings": found[: min(int(limit or 80), 300)]}

    def compare_files(self, a: str, b: str):
        da, db_ = self._read(a), self._read(b)
        n = min(len(da), len(db_))
        ranges: List[List[int]] = []
        differing = 0
        for i in range(n):
            if da[i] != db_[i]:
                differing += 1
                if ranges and i - ranges[-1][1] <= 8:  # merge nearby differences into one range
                    ranges[-1][1] = i
                else:
                    ranges.append([i, i])
        differing += abs(len(da) - len(db_))
        out: Dict[str, Any] = {
            "size_a": len(da), "size_b": len(db_), "sha1_a": hashlib.sha1(da).hexdigest(),
            "sha1_b": hashlib.sha1(db_).hexdigest(), "identical": da == db_,
            "differing_bytes": differing, "differing_ranges": len(ranges),
            "ranges": [{"start": r0, "end": r1, "length": r1 - r0 + 1} for r0, r1 in ranges[:40]],
        }
        if len(ranges) > 40:
            out["ranges_truncated"] = True
        if bnkfmt.is_bank(da) and bnkfmt.is_bank(db_):
            out["bank_comparison"] = self._compare_banks(da, db_)
        return out

    @staticmethod
    def _compare_banks(da: bytes, db_: bytes) -> Dict[str, Any]:
        ba, bb = bnkfmt.parse_bank(da), bnkfmt.parse_bank(db_)
        chunks = []
        ca = {c.tag: c for c in ba.chunks}
        cb = {c.tag: c for c in bb.chunks}
        for tag in sorted(set(ca) | set(cb)):
            x, y = ca.get(tag), cb.get(tag)
            same = bool(x and y and da[x.offset:x.offset + x.size] == db_[y.offset:y.offset + y.size])
            chunks.append({"tag": tag, "size_a": x.size if x else None, "size_b": y.size if y else None, "identical": same})
        oa = {o.object_id: da[o.offset:o.offset + o.size] for o in ba.objects}
        ob = {o.object_id: db_[o.offset:o.offset + o.size] for o in bb.objects}
        types = {o.object_id: o.type_name for o in ba.objects + bb.objects}
        changed = [i for i in set(oa) & set(ob) if oa[i] != ob[i]]
        only_a = sorted(set(oa) - set(ob))
        only_b = sorted(set(ob) - set(oa))
        media_a = {m.source_id: da[m.offset:m.offset + m.size] for m in ba.media}
        media_b = {m.source_id: db_[m.offset:m.offset + m.size] for m in bb.media}

        def by_type(ids):
            counts: Dict[str, int] = {}
            for i in ids:
                counts[types.get(i, "?")] = counts.get(types.get(i, "?"), 0) + 1
            return counts

        return {
            "header": {"bank_id": [ba.bank_id, bb.bank_id], "version": [ba.version, bb.version],
                       "language_id": [ba.language_id, bb.language_id], "project_id": [ba.project_id, bb.project_id],
                       "bank_hash_equal": ba.header_fields.get("bank_hash") == bb.header_fields.get("bank_hash")},
            "chunks": chunks,
            "objects": {"shared_identical": len(set(oa) & set(ob)) - len(changed), "shared_changed": len(changed),
                        "changed_by_type": by_type(changed), "changed_examples": sorted(changed)[:10],
                        "only_in_a": len(only_a), "only_in_a_by_type": by_type(only_a), "only_in_a_examples": only_a[:10],
                        "only_in_b": len(only_b), "only_in_b_by_type": by_type(only_b), "only_in_b_examples": only_b[:10]},
            "embedded_media": {"shared": len(set(media_a) & set(media_b)),
                               "shared_changed": sum(1 for i in set(media_a) & set(media_b) if media_a[i] != media_b[i]),
                               "only_in_a": len(set(media_a) - set(media_b)), "only_in_b": len(set(media_b) - set(media_a))},
        }

    def inspect_bnk(self, bank):
        banks = queries.soundbanks(self.db, self.inst_id)
        key = str(bank).lower()
        for b in banks:
            if key in (str(b["bank_id"]), str(b["asset_id"])) or key in b["path"].lower() or key == str(b["name"]).lower():
                return {k: b[k] for k in ("path", "name", "bank_id", "version", "decoded_version", "object_count", "embedded_media",
                                          "type_counts", "parse_status", "chunks", "errors")}
        raise ToolError(f"no bank matching {bank!r}")

    def get_bnk_contents(self, bank, limit: int = 120):
        result = queries.bank_contents(self.db, self.inst_id, bank, min(int(limit or 120), 400))
        if result is None:
            raise ToolError(f"no bank matching {bank!r}")
        return result

    def inspect_object_bytes(self, object_id, bank: str = "", length: int = 256):
        oid = self._int(object_id, "object_id")
        rows = self.db.query(
            "SELECT o.*, a.vpath, a.locator FROM wwise_object o JOIN asset a ON a.id=o.bank_asset_id WHERE a.installation_id=?"
            " AND o.object_id=?", (self.inst_id, oid))
        if bank:
            rows = [r for r in rows if str(bank).lower() in r["vpath"].lower()] or rows
        if not rows:
            raise ToolError(f"object {oid} not found")
        r = rows[0]
        locator = r["locator"]
        path = locator.split(":", 1)[1]
        data = self._read(path)
        body = data[r["offset"]:r["offset"] + r["size"]]
        length = max(32, min(int(length or 256), 1024))
        consumed = None
        info = bnkfmt.parse_bank(data)
        for obj in info.objects:
            if obj.object_id == oid and obj.offset == r["offset"]:
                consumed = obj.consumed
                break
        return {"bank": r["vpath"], "bank_version": info.version, "type": r["type_name"], "type_code": r["type_code"],
                "size": r["size"], "parse_status": r["parse_status"], "parser_error": r["error"], "parser_consumed_bytes": consumed,
                "note": "offsets are relative to the object body; the first 4 bytes are the object id",
                "hex": _hexdump(body[:length]), "u32_le": _u32_view(body[:length], 48)}

    def inspect_wem(self, target):
        if str(target).isdigit():
            row = self.db.query_one("SELECT a.locator, w.container, w.bank_asset_id FROM wem w JOIN asset a ON a.id=w.asset_id"
                                    " WHERE a.installation_id=? AND w.source_id=? LIMIT 1", (self.inst_id, int(target)))
            if row is None:
                raise ToolError(f"no WEM with media id {target}")
            if row["container"] == "embedded":
                rec = self.db.query_one("SELECT details_json FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.locator=?", (row["locator"],))
                return {"container": "embedded", "header": loads(rec["details_json"], {})}
            path = row["locator"].split(":", 1)[1]
        else:
            path = str(target)
        data = self._read(path, limit=256 * 1024)
        kind, ref = self._resolve(path)
        size = ref.orig_size if kind == "archive" else ref.stat().st_size
        return {"path": path, "header": parse_wem(data, size).to_dict()}

    def get_wem_info(self, source_id):
        return queries.media_record(self.db, self.inst_id, self._int(source_id, "source_id"))

    def get_object(self, object_id):
        found = queries.get_object(self.db, self.inst_id, self._int(object_id, "object_id"))
        if not found:
            return {"found": False, "anywhere": queries.find_references_anywhere(self.db, self.inst_id, self._int(object_id))}
        return {"found": True, "copies": found[:4]}

    def get_container(self, object_id, depth: int = 2):
        oid = self._int(object_id, "object_id")
        graph = object_neighbourhood(self.db, self.inst_id, oid, max(1, min(int(depth or 2), 3)))
        graph["object"] = (queries.get_object(self.db, self.inst_id, oid) or [None])[0]
        return graph

    def find_references(self, id):  # noqa: A002 - tool parameter name
        return queries.find_references_anywhere(self.db, self.inst_id, self._int(id))

    def find_referencing_objects(self, id):  # noqa: A002
        return {"referencing": queries.find_referencing(self.db, self.inst_id, self._int(id))}

    def search_music_assets(self, query: str = "", limit: int = 20):
        rows = queries.media_summary_rows(self.db, self.inst_id, str(query or ""), role="music*", limit=min(int(limit or 20), 100))
        return {"results": rows}

    def get_unknown_structures(self, status: str = "open", limit: int = 25):
        items = queries.unknown_structures(self.db, self.inst_id, status or None)
        return {"unknown_structures": [{"signature": u["signature"], "category": u["category"], "occurrences": u["occurrences"],
                                        "description": u["description"], "status": u["status"]} for u in items[: int(limit or 25)]]}

    def get_unknown_structure(self, signature: str):
        row = self.db.query_one("SELECT * FROM unknown_structure WHERE installation_id=? AND signature=?", (self.inst_id, signature))
        if row is None:
            raise ToolError(f"unknown signature {signature!r}")
        item = dict(row)
        item["details"] = loads(item.pop("details_json"), {})
        return item

    def hash_name(self, name: str):
        value = wwise_fnv1_32(str(name))
        hits = queries.find_references_anywhere(self.db, self.inst_id, value, limit=5)
        exists = any(hits[k] for k in ("as_object", "as_bank", "referenced_by", "as_media")) or any(
            h for h in hits["names"])
        return {"name": name, "id": value, "exists_in_scan": bool(exists), "where": {k: v for k, v in hits.items() if v}}

    def test_names(self, names):
        if isinstance(names, str):
            names = [n.strip() for n in re.split(r"[,\n]", names) if n.strip()]
        matches = []
        for name in list(names)[:40]:
            result = self.hash_name(name)
            if result["exists_in_scan"]:
                matches.append({"name": name, "id": result["id"], "where": list(result["where"].keys())})
        return {"tested": min(len(names), 40), "matches": matches}

    def consult_research(self, query: str):
        words = [w for w in re.findall(r"[a-z0-9_]+", str(query).lower()) if len(w) > 2]
        hits = []
        for directory in RESEARCH_DIRS:
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.md")):
                text = file.read_text(encoding="utf-8", errors="replace")
                for para in re.split(r"\n\s*\n", text):
                    low = para.lower()
                    score = sum(low.count(w) for w in words)
                    if score:
                        hits.append((score, file.name, para.strip()[:700]))
            if hits:
                break
        hits.sort(key=lambda h: -h[0])
        return {"results": [{"source": h[1], "text": h[2]} for h in hits[:5]]}

    def list_findings(self, status: str = "", limit: int = 20):
        items = self.knowledge.list(status or None, limit=min(int(limit or 20), 100))
        return {"findings": [{"uid": f["uid"], "status": f["status"], "title": f["title"], "subject": f"{f['subject_type']}:{f['subject_key']}"}
                             for f in items]}

    def _evidence(self, evidence) -> List[Dict[str, Any]]:
        if isinstance(evidence, str):
            evidence = [evidence]
        out = []
        for item in evidence or []:
            out.append(item if isinstance(item, dict) else {"note": str(item)[:600]})
        return out[:20]

    _STATUS_RANK = {"unknown": 0, "hypothesis": 1, "probable": 2, "verified": 3}

    @staticmethod
    def _norm_title(title: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(title).lower()).strip()

    def _existing_duplicate(self, category: str, subject_type: str, subject_key: str, title: str):
        """An earlier AI finding about the same thing: same category+subject, or same category+title."""

        rows = self.db.query(
            "SELECT uid, status, title, subject_type, subject_key FROM finding WHERE category=? AND status<>'rejected'"
            " AND created_by LIKE 'ai:%'", (category,))
        norm = self._norm_title(title)
        for r in rows:
            if subject_key and r["subject_type"] == subject_type and r["subject_key"] == subject_key:
                return r
        for r in rows:
            if norm and self._norm_title(r["title"]) == norm:
                return r
        return None

    def _store(self, *, title, statement, status, category, subject_type, subject_key, evidence, reasoning):
        dup = self._existing_duplicate(category, subject_type, subject_key, title)
        uid = None
        if dup is not None:
            uid = dup["uid"]
            if self._STATUS_RANK.get(dup["status"], 0) > self._STATUS_RANK.get(status, 0):
                status = dup["status"]  # never downgrade an earlier, stronger finding
            subject_type, subject_key = dup["subject_type"], dup["subject_key"]
        finding = self.knowledge.record(
            title=str(title)[:200], statement=str(statement)[:2000], status=status, category=str(category)[:60],
            subject_type=str(subject_type), subject_key=str(subject_key), evidence=evidence,
            reasoning=str(reasoning)[:2000], created_by=f"ai:{self.model_id or 'model'}", model_id=self.model_id,
            session_id=self.session_id, uid=uid)
        out = {"recorded": finding["uid"], "status": finding["status"]}
        if dup is not None:
            out["merged"] = "an earlier finding about the same subject/title was updated instead of creating a duplicate"
        return out

    def record_hypothesis(self, title: str, statement: str, subject_type: str = "general", subject_key: str = "",
                          evidence=None, reasoning: str = ""):
        return self._store(title=title, statement=statement, status="hypothesis", category="hypothesis",
                           subject_type=str(subject_type), subject_key=str(subject_key),
                           evidence=self._evidence(evidence), reasoning=reasoning)

    def record_finding(self, title: str, statement: str, status: str = "hypothesis", category: str = "general",
                       subject_type: str = "general", subject_key: str = "", evidence=None, reasoning: str = ""):
        status = str(status).lower()
        note = None
        if status == "verified":
            status, note = "probable", "verified requires mark_verified with a passing deterministic check; stored as probable"
        if status not in ("probable", "hypothesis", "unknown"):
            status = "hypothesis"
        evidence = self._evidence(evidence)
        if status == "probable" and len(evidence) < 2:
            status, note = "hypothesis", "probable needs at least two independent evidence items; stored as hypothesis"
        out = self._store(title=title, statement=statement, status=status, category=str(category)[:60],
                          subject_type=str(subject_type), subject_key=str(subject_key), evidence=evidence,
                          reasoning=reasoning)
        if note:
            out["note"] = note
        return out

    def run_check(self, check: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(check, dict):
            raise ToolError("check must be an object")
        kind = check.get("type")
        if kind == "name_hash":
            name, target = str(check.get("name", "")), self._int(check.get("id"), "id")
            value = wwise_fnv1_32(name)
            return {"type": kind, "name": name, "id": target, "computed": value, "passed": value == target}
        if kind == "object_exists":
            oid = self._int(check.get("id"))
            n = self.db.scalar("SELECT COUNT(*) FROM wwise_object o JOIN asset a ON a.id=o.bank_asset_id WHERE a.installation_id=?"
                               " AND o.object_id=?", (self.inst_id, oid), 0)
            return {"type": kind, "id": oid, "count": n, "passed": n > 0}
        if kind == "media_exists":
            mid = self._int(check.get("id"))
            n = self.db.scalar("SELECT COUNT(*) FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=? AND w.source_id=?",
                               (self.inst_id, mid), 0)
            return {"type": kind, "id": mid, "count": n, "passed": n > 0}
        if kind == "reference_exists":
            f, t = self._int(check.get("from"), "from"), self._int(check.get("to"), "to")
            n = self.db.scalar("SELECT COUNT(*) FROM object_ref r JOIN asset a ON a.id=r.bank_asset_id WHERE a.installation_id=?"
                               " AND r.from_object_id=? AND r.to_id=? AND r.confidence='parsed'", (self.inst_id, f, t), 0)
            return {"type": kind, "from": f, "to": t, "count": n, "passed": n > 0}
        if kind == "bytes_match":
            path, offset, hexstr = str(check.get("path")), int(check.get("offset", 0)), str(check.get("hex", "")).replace(" ", "")
            try:
                expected = bytes.fromhex(hexstr)
            except ValueError as exc:
                raise ToolError("hex must be a hex string") from exc
            if not expected:
                raise ToolError("hex must not be empty")
            data = self._read(path, limit=offset + len(expected))
            return {"type": kind, "path": path, "offset": offset, "passed": data[offset:offset + len(expected)] == expected}
        raise ToolError(f"unsupported check type {kind!r}")

    def mark_verified(self, uid: str, check):
        if isinstance(check, str):
            try:
                check = json.loads(check)
            except ValueError as exc:
                raise ToolError("check must be a JSON object") from exc
        finding = self.knowledge.get(str(uid))
        if finding is None:
            raise ToolError(f"no finding {uid}")
        result = self.run_check(check)
        if not result["passed"]:
            return {"verified": False, "check": result, "note": "check failed; finding left unchanged"}
        verification = dict(result)
        verification["checked_by"] = "deterministic_tool"
        self.knowledge.set_status(str(uid), "verified", verification)
        if result["type"] == "name_hash" and finding["subject_key"] in ("", str(result["id"])):
            # a verified name mapping is reused by future scans
            self.knowledge.record(
                title=f"Name for id {result['id']}", statement=f"Wwise id {result['id']} is the hash of '{result['name']}'",
                status="verified", category=CATEGORY_NAME_MAPPING, subject_type="name", subject_key=str(result["id"]),
                evidence=[{"check": "fnv1_32", "source_finding": uid}], verification=verification,
                created_by=f"ai:{self.model_id or 'model'}", model_id=self.model_id, session_id=self.session_id)
        return {"verified": True, "check": result}

    def reject_finding(self, uid: str, reason: str = ""):
        finding = self.knowledge.set_status(str(uid), "rejected", {"reason": str(reason)[:500]})
        if finding is None:
            raise ToolError(f"no finding {uid}")
        return {"rejected": uid}
