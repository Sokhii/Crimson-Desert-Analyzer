"""Read-side API over the analysis database.

The GUI, the report writer and the AI investigation tools all read through
these functions, so they always see the same facts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cstudio.db.database import Database, loads

from .names import best_names


def _names_for(db: Database, ids) -> Dict[int, str]:
    return best_names(db, sorted({int(i) for i in ids if i is not None}))


def media_record(db: Database, inst_id: int, source_id: int, include_evidence: bool = True) -> Dict[str, Any]:
    wems = [dict(r) for r in db.query(
        "SELECT w.*, a.vpath, a.origin, a.locator, a.content_hash, a.hash_kind, a.size AS file_size FROM wem w"
        " JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=? AND w.source_id=?", (inst_id, source_id))]
    ctx = db.query_one("SELECT * FROM media_context WHERE installation_id=? AND source_id=?", (inst_id, source_id))
    cls = db.query_one("SELECT * FROM classification WHERE installation_id=? AND entity_type='media' AND entity_key=?",
                       (inst_id, source_id))
    xml = [dict(r) for r in db.query(
        "SELECT media_id, short_name, path, cache_path, language, streaming, location, bank_id, relation FROM xml_media"
        " WHERE media_id=? AND asset_id IN (SELECT id FROM asset WHERE installation_id=?)", (source_id, inst_id))]
    sources = [dict(r) for r in db.query(
        "SELECT m.object_id, m.owner_type, m.stream_type, m.in_memory_size, m.details_json, a.vpath AS bank_path"
        " FROM media_source m JOIN asset a ON a.id=m.bank_asset_id WHERE a.installation_id=? AND m.source_id=?",
        (inst_id, source_id))]
    community = [dict(r) for r in db.query(
        "SELECT description, context, category, original_name, bank_name FROM community_media WHERE media_id=?", (source_id,))]
    containers = loads(ctx["container_ids_json"], []) if ctx else []
    container_types = loads(ctx["container_types_json"], []) if ctx else []
    events = loads(ctx["event_ids_json"], []) if ctx else []
    banks = loads(ctx["bank_ids_json"], []) if ctx else []
    name_map = _names_for(db, [source_id] + containers + events + banks)
    primary = wems[0] if wems else None
    clip = None
    for s in sources:
        detail = loads(s.pop("details_json"), {}) or {}
        s["stream_type"] = s.get("stream_type")
        if detail.get("clip"):
            clip = detail["clip"]
            s["clip"] = clip
    record: Dict[str, Any] = {
        "source_id": source_id,
        "name": name_map.get(source_id) or (xml[0]["short_name"] if xml else None),
        "files": [
            {"path": w["vpath"], "origin": w["origin"], "container": w["container"], "hash": w["content_hash"],
             "hash_kind": w["hash_kind"], "size": w["file_size"]}
            for w in wems
        ],
        "codec": primary["codec"] if primary else None,
        "channels": primary["channels"] if primary else None,
        "sample_rate": primary["sample_rate"] if primary else None,
        "duration_s": primary["duration_s"] if primary else None,
        "duration_method": primary["duration_method"] if primary else None,
        "wwise_source_duration_ms": clip.get("source_duration_ms") if clip else None,
        "loops": loads(primary["loops_json"], []) if primary else [],
        "streaming": sorted({s["stream_type"] for s in sources if s.get("stream_type")}),
        "owners": [{"object_id": s["object_id"], "type": s["owner_type"], "bank": s["bank_path"]} for s in sources],
        "containers": [{"object_id": c, "type": t, "name": name_map.get(c)} for c, t in zip(containers, container_types)],
        "events": [{"event_id": e, "name": name_map.get(e)} for e in events],
        "banks": [{"bank_id": b, "name": name_map.get(b)} for b in banks],
        "soundbanksinfo": xml,
        "community": community,
        "role": cls["role"] if cls else "unknown",
        "score": cls["score"] if cls else 0.0,
        "confidence": cls["confidence"] if cls else "low",
    }
    if include_evidence and cls:
        record["evidence"] = (loads(cls["evidence_json"], {}) or {}).get("evidence", [])
    findings = [dict(r) for r in db.query(
        "SELECT uid, title, status FROM finding WHERE subject_type='media' AND subject_key=?", (str(source_id),))]
    if findings:
        record["findings"] = findings
    return record


def music_assets(db: Database, inst_id: int, roles=("music", "likely_music", "possible_music"), query: str = "",
                 limit: int = 100000) -> List[Dict[str, Any]]:
    marks = ",".join("?" * len(roles))
    rows = db.query(
        f"SELECT entity_key FROM classification WHERE installation_id=? AND entity_type='media' AND role IN ({marks})"
        f" ORDER BY score DESC, entity_key LIMIT ?", (inst_id, *roles, limit))
    out = []
    q = query.lower().strip()
    for r in rows:
        rec = media_record(db, inst_id, r["entity_key"], include_evidence=True)
        if q:
            hay = " ".join(str(x) for x in [rec["source_id"], rec["name"], [f["path"] for f in rec["files"]],
                                              [b["name"] for b in rec["banks"]], [e["name"] for e in rec["events"]]]).lower()
            if q not in hay:
                continue
        out.append(rec)
    return out


def media_summary_rows(db: Database, inst_id: int, query: str = "", role: str = "", limit: int = 5000) -> List[Dict[str, Any]]:
    """Lightweight rows for tables (no per-row evidence expansion)."""

    sql = ("SELECT c.entity_key AS source_id, c.role, c.score, c.confidence, mc.owner_type, mc.bank_ids_json,"
           " (SELECT duration_s FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=c.installation_id"
           "   AND w.source_id=c.entity_key LIMIT 1) AS duration_s,"
           " (SELECT codec FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=c.installation_id"
           "   AND w.source_id=c.entity_key LIMIT 1) AS codec,"
           " (SELECT channels FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=c.installation_id"
           "   AND w.source_id=c.entity_key LIMIT 1) AS channels"
           " FROM classification c LEFT JOIN media_context mc ON mc.installation_id=c.installation_id AND mc.source_id=c.entity_key"
           " WHERE c.installation_id=? AND c.entity_type='media'")
    params: List[Any] = [inst_id]
    if role:
        if role == "music*":
            sql += " AND c.role IN ('music','likely_music','possible_music')"
        else:
            sql += " AND c.role=?"
            params.append(role)
    sql += " ORDER BY c.score DESC, c.entity_key LIMIT ?"
    params.append(limit * (4 if query else 1))
    rows = [dict(r) for r in db.query(sql, params)]
    ids = [r["source_id"] for r in rows]
    bank_ids = set()
    for r in rows:
        r["bank_ids"] = loads(r.pop("bank_ids_json"), []) or []
        bank_ids.update(r["bank_ids"])
    names = best_names(db, ids + sorted(bank_ids))
    q = query.lower().strip()
    out = []
    for r in rows:
        r["name"] = names.get(r["source_id"], "")
        r["banks"] = ", ".join(names.get(b, str(b)) for b in r.pop("bank_ids"))
        if q and q not in f"{r['source_id']} {r['name']} {r['banks']}".lower():
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def soundbanks(db: Database, inst_id: int) -> List[Dict[str, Any]]:
    rows = db.query(
        "SELECT b.*, a.vpath, a.content_hash, a.size, a.origin FROM bnk b JOIN asset a ON a.id=b.asset_id"
        " WHERE a.installation_id=? ORDER BY a.vpath", (inst_id,))
    names = best_names(db, [r["bank_id"] for r in rows])
    out = []
    for r in rows:
        status_counts = {s["parse_status"]: s["c"] for s in db.query(
            "SELECT parse_status, COUNT(*) c FROM wwise_object WHERE bank_asset_id=? GROUP BY parse_status", (r["asset_id"],))}
        out.append({
            "asset_id": r["asset_id"],
            "path": r["vpath"],
            "bank_id": r["bank_id"],
            "name": names.get(r["bank_id"]),
            "version": r["version"],
            "decoded_version": bool(r["decoded_version"]),
            "language_id": r["language_id"],
            "project_id": r["project_id"],
            "object_count": r["object_count"],
            "embedded_media": r["media_count"],
            "size": r["size"],
            "hash": r["content_hash"],
            "type_counts": loads(r["type_counts_json"], {}),
            "parse_status": status_counts,
            "chunks": loads(r["chunks_json"], []),
            "errors": loads(r["errors_json"], []),
        })
    return out


def bank_contents(db: Database, inst_id: int, bank: str | int, limit: int = 400) -> Optional[Dict[str, Any]]:
    """``bank`` may be a bank ID, an asset ID or a path/name fragment."""

    row = None
    if isinstance(bank, int) or str(bank).isdigit():
        value = int(bank)
        row = db.query_one("SELECT b.*, a.vpath FROM bnk b JOIN asset a ON a.id=b.asset_id WHERE a.installation_id=?"
                           " AND (b.bank_id=? OR b.asset_id=?)", (inst_id, value, value))
    if row is None:
        row = db.query_one("SELECT b.*, a.vpath FROM bnk b JOIN asset a ON a.id=b.asset_id WHERE a.installation_id=?"
                           " AND lower(a.vpath) LIKE ? ORDER BY length(a.vpath) LIMIT 1", (inst_id, f"%{str(bank).lower()}%"))
    if row is None:
        return None
    objects = [dict(r) for r in db.query(
        "SELECT object_id, type_name, parse_status, size, error FROM wwise_object WHERE bank_asset_id=? ORDER BY type_code, object_id LIMIT ?",
        (row["asset_id"], limit))]
    names = best_names(db, [o["object_id"] for o in objects] + [row["bank_id"]])
    for o in objects:
        if o["object_id"] in names:
            o["name"] = names[o["object_id"]]
    media = [dict(r) for r in db.query(
        "SELECT w.source_id, w.codec, w.duration_s, w.channels FROM wem w WHERE w.bank_asset_id=? LIMIT ?", (row["asset_id"], limit))]
    return {
        "path": row["vpath"], "bank_id": row["bank_id"], "name": names.get(row["bank_id"]), "version": row["version"],
        "type_counts": loads(row["type_counts_json"], {}), "objects": objects, "embedded_media": media,
        "truncated": row["object_count"] > limit,
    }


def get_object(db: Database, inst_id: int, object_id: int) -> List[Dict[str, Any]]:
    rows = db.query(
        "SELECT o.*, a.vpath FROM wwise_object o JOIN asset a ON a.id=o.bank_asset_id WHERE a.installation_id=? AND o.object_id=?",
        (inst_id, object_id))
    names = best_names(db, [object_id])
    out = []
    for r in rows:
        refs = [dict(x) for x in db.query(
            "SELECT kind, to_id, confidence, rel_offset FROM object_ref WHERE bank_asset_id=? AND from_object_id=? LIMIT 500",
            (r["bank_asset_id"], object_id))]
        out.append({
            "object_id": object_id, "name": names.get(object_id), "type": r["type_name"], "type_code": r["type_code"],
            "bank": r["vpath"], "offset": r["offset"], "size": r["size"], "parse_status": r["parse_status"],
            "error": r["error"], "fields": loads(r["fields_json"], {}), "references": refs,
        })
    return out


def find_referencing(db: Database, inst_id: int, target_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    rows = db.query(
        "SELECT r.from_object_id, r.kind, r.confidence, o.type_name, a.vpath FROM object_ref r JOIN asset a ON a.id=r.bank_asset_id"
        " LEFT JOIN wwise_object o ON o.bank_asset_id=r.bank_asset_id AND o.object_id=r.from_object_id"
        " WHERE a.installation_id=? AND r.to_id=? LIMIT ?", (inst_id, target_id, limit))
    names = best_names(db, [r["from_object_id"] for r in rows])
    return [{"from_object_id": r["from_object_id"], "from_type": r["type_name"], "name": names.get(r["from_object_id"]),
             "kind": r["kind"], "confidence": r["confidence"], "bank": r["vpath"]} for r in rows]


def find_references_anywhere(db: Database, inst_id: int, value: int, limit: int = 100) -> Dict[str, Any]:
    """Every place a numeric ID appears in the structured data."""

    return {
        "as_object": [{"type": r["type_name"], "bank": r["vpath"]} for r in db.query(
            "SELECT o.type_name, a.vpath FROM wwise_object o JOIN asset a ON a.id=o.bank_asset_id WHERE a.installation_id=? AND o.object_id=? LIMIT ?",
            (inst_id, value, limit))],
        "referenced_by": find_referencing(db, inst_id, value, limit),
        "as_media": [{"container": r["container"], "path": r["vpath"]} for r in db.query(
            "SELECT w.container, a.vpath FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=? AND w.source_id=? LIMIT ?",
            (inst_id, value, limit))],
        "as_bank": [{"path": r["vpath"]} for r in db.query(
            "SELECT a.vpath FROM bnk b JOIN asset a ON a.id=b.asset_id WHERE a.installation_id=? AND b.bank_id=?", (inst_id, value))],
        "names": [dict(r) for r in db.query("SELECT name, kind, source, hash_verified FROM name WHERE id_value=?", (value,))],
        "xml": [dict(r) for r in db.query(
            "SELECT 'media' AS kind, short_name AS name FROM xml_media WHERE media_id=? UNION ALL "
            "SELECT 'event', name FROM xml_event WHERE event_id=? UNION ALL SELECT kind, name FROM xml_object WHERE object_id=?",
            (value, value, value))][:limit],
    }


def unknown_structures(db: Database, inst_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM unknown_structure WHERE installation_id=?"
    params: List[Any] = [inst_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY status, occurrences DESC"
    out = []
    for r in db.query(sql, params):
        item = dict(r)
        item["details"] = loads(item.pop("details_json"), {})
        out.append(item)
    return out


def search_files(db: Database, inst_id: int, pattern: str, limit: int = 200) -> List[Dict[str, Any]]:
    like = pattern.replace("*", "%").replace("?", "_")
    if "%" not in like and "_" not in like:
        like = f"%{like}%"
    rows = db.query(
        "SELECT package, vpath, ext, comp_size, orig_size, flags FROM archive_entry WHERE installation_id=? AND lower(vpath) LIKE ?"
        " ORDER BY vpath LIMIT ?", (inst_id, like.lower(), limit))
    out = [{"path": f"{r['package']}/{r['vpath']}", "ext": r["ext"], "size": r["orig_size"], "stored_size": r["comp_size"],
            "compression": r["flags"] & 0xF, "encryption": (r["flags"] >> 4) & 0xF} for r in rows]
    if len(out) < limit:
        for r in db.query("SELECT rel_path, size FROM source_file WHERE installation_id=? AND kind='loose' AND lower(rel_path) LIKE ?"
                          " LIMIT ?", (inst_id, like.lower(), limit - len(out))):
            out.append({"path": f"loose:{r['rel_path']}", "size": r["size"]})
    return out


def extension_summary(db: Database, inst_id: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT ext, COUNT(*) AS files, SUM(orig_size) AS bytes FROM archive_entry WHERE installation_id=? GROUP BY ext"
        " ORDER BY files DESC", (inst_id,))]


def relationship_edges(db: Database, inst_id: int, limit: int = 2000000) -> List[Dict[str, Any]]:
    rows = db.query(
        "SELECT r.from_object_id, r.to_id, r.kind, r.confidence, b.bank_id FROM object_ref r JOIN bnk b ON b.asset_id=r.bank_asset_id"
        " JOIN asset a ON a.id=r.bank_asset_id WHERE a.installation_id=? LIMIT ?", (inst_id, limit))
    return [{"from": r["from_object_id"], "to": r["to_id"], "kind": r["kind"], "confidence": r["confidence"],
             "bank_id": r["bank_id"]} for r in rows]


def overview(db: Database, inst_id: int) -> Dict[str, Any]:
    def count(sql, params=(inst_id,)):
        return int(db.scalar(sql, params, 0) or 0)

    roles = {r["role"]: r["c"] for r in db.query(
        "SELECT role, COUNT(*) c FROM classification WHERE installation_id=? GROUP BY role", (inst_id,))}
    return {
        "source_files": count("SELECT COUNT(*) FROM source_file WHERE installation_id=?"),
        "archive_entries": count("SELECT COUNT(*) FROM archive_entry WHERE installation_id=?"),
        "assets": count("SELECT COUNT(*) FROM asset WHERE installation_id=?"),
        "banks": count("SELECT COUNT(*) FROM bnk b JOIN asset a ON a.id=b.asset_id WHERE a.installation_id=?"),
        "hirc_objects": count("SELECT COUNT(*) FROM wwise_object o JOIN asset a ON a.id=o.bank_asset_id WHERE a.installation_id=?"),
        "media_ids": count("SELECT COUNT(DISTINCT w.source_id) FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=?"),
        "roles": roles,
        "unknown_structures": count("SELECT COUNT(*) FROM unknown_structure WHERE installation_id=? AND status='open'"),
        "findings": {r["status"]: r["c"] for r in db.query("SELECT status, COUNT(*) c FROM finding GROUP BY status")},
    }
