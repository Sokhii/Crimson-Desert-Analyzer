"""Collects structures the deterministic analyzer does not (yet) understand.

Unknowns are aggregated by *signature* (e.g. ``hirc_partial:v150:MusicTrack``)
so a recurring structure is one work item with an occurrence count and
examples, not thousands of rows. Signatures that a verified/probable
knowledge finding explains are marked ``explained`` automatically - this is
how an investigation done once makes later scans easier.
"""

from __future__ import annotations

from typing import Dict

from cstudio.db.database import Database, dumps, loads

STRUCTURAL_REF_KINDS = ("child", "playlist_item", "playlist_segment", "switch_assoc", "transition_segment",
                        "stinger_segment", "action_target", "event_action", "layer_assoc")


def detect(db: Database, inst_id: int, scan_id: int, knowledge=None) -> int:
    found: Dict[str, dict] = {}

    def add(signature: str, category: str, description: str, example: dict, entity_type: str = "", entity_key: str = "") -> None:
        item = found.get(signature)
        if item is None:
            item = found[signature] = {
                "category": category, "description": description, "entity_type": entity_type,
                "entity_key": entity_key, "examples": [], "count": 0,
            }
        item["count"] += 1
        if len(item["examples"]) < 12:
            item["examples"].append(example)

    assets = "SELECT id FROM asset WHERE installation_id=?"
    bank_rows = db.query(
        "SELECT b.*, a.vpath FROM bnk b JOIN asset a ON a.id=b.asset_id WHERE a.installation_id=?", (inst_id,))
    versions = {r["asset_id"]: r["version"] for r in bank_rows}
    for r in bank_rows:
        if not r["decoded_version"]:
            add(f"bnk_version:v{r['version']}", "bank_version",
                f"Bank version {r['version']} is outside the range the HIRC body decoders were written for",
                {"bank": r["vpath"]}, "bank_version", str(r["version"]))
        for err in loads(r["errors_json"], []) or []:
            add(f"bnk_error:{err.split(':')[0][:40]}", "bank_structure", "Bank chunk structure problem",
                {"bank": r["vpath"], "error": err})
    for r in db.query(
        "SELECT o.type_name, o.type_code, o.parse_status, o.error, o.object_id, o.size, o.bank_asset_id, a.vpath"
        " FROM wwise_object o JOIN asset a ON a.id=o.bank_asset_id WHERE a.installation_id=?"
        " AND o.parse_status IN ('partial','failed','header_only')", (inst_id,),
    ):
        v = versions.get(r["bank_asset_id"], 0)
        if r["parse_status"] == "header_only" and r["type_name"].startswith("Unknown"):
            sig = f"hirc_type:v{v}:0x{r['type_code']:02X}"
            desc = f"HIRC object type 0x{r['type_code']:02X} is not a known Wwise type for bank version {v}"
        elif r["parse_status"] == "header_only":
            sig = f"hirc_undecoded:v{v}:{r['type_name']}"
            desc = f"{r['type_name']} bodies are not decoded for bank version {v}"
        else:
            sig = f"hirc_{r['parse_status']}:v{v}:{r['type_name']}"
            desc = f"{r['type_name']} objects (bank v{v}) do not match the expected layout ({r['parse_status']})"
        add(sig, "hirc_object", desc,
            {"bank": r["vpath"], "object_id": r["object_id"], "size": r["size"], "error": r["error"]},
            "hirc_type", r["type_name"])
    for r in db.query("SELECT entity_key, value FROM metadata WHERE entity_type='asset' AND key='unknown_chunk'"
                      " AND entity_key IN (SELECT CAST(id AS TEXT) FROM asset WHERE installation_id=?)", (inst_id,)):
        chunk = loads(r["value"], {}) or {}
        add(f"bnk_chunk:{chunk.get('tag')}", "bank_chunk", f"Unknown bank chunk '{chunk.get('tag')}'",
            {"asset_id": r["entity_key"], **chunk})
    # dangling structural references
    object_ids = {r[0] for r in db.query(f"SELECT DISTINCT object_id FROM wwise_object WHERE bank_asset_id IN ({assets})", (inst_id,))}
    marks = ",".join("?" * len(STRUCTURAL_REF_KINDS))
    for r in db.query(
        "SELECT r.kind, r.from_object_id, r.to_id, a.vpath FROM object_ref r JOIN asset a ON a.id=r.bank_asset_id"
        f" WHERE a.installation_id=? AND r.confidence='parsed' AND r.kind IN ({marks})", (inst_id, *STRUCTURAL_REF_KINDS),
    ):
        if r["to_id"] not in object_ids and r["to_id"] not in (0, 0xFFFFFFFF):
            add(f"dangling_ref:{r['kind']}", "reference", f"'{r['kind']}' references point to objects not found in any scanned bank",
                {"from": r["from_object_id"], "to": r["to_id"], "bank": r["vpath"]})
    # media referenced by objects but not present as a WEM
    have_wem = {r[0] for r in db.query(
        "SELECT DISTINCT w.source_id FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=?", (inst_id,))}
    for r in db.query("SELECT m.source_id, m.owner_type, m.stream_type, a.vpath FROM media_source m JOIN asset a"
                      " ON a.id=m.bank_asset_id WHERE a.installation_id=?", (inst_id,)):
        if r["source_id"] not in have_wem:
            add(f"missing_media:{r['stream_type']}", "media", "Media referenced by a bank has no WEM in the scanned data",
                {"source_id": r["source_id"], "owner": r["owner_type"], "bank": r["vpath"]})
    referenced = {r[0] for r in db.query(
        f"SELECT DISTINCT source_id FROM media_source WHERE bank_asset_id IN ({assets})", (inst_id,))}
    for r in db.query("SELECT w.source_id, w.codec, w.valid, a.vpath FROM wem w JOIN asset a ON a.id=w.asset_id"
                      " WHERE a.installation_id=? AND w.container='loose'", (inst_id,)):
        if r["source_id"] >= 0 and r["source_id"] not in referenced:
            add("orphan_wem", "media", "WEM files not referenced by any scanned bank object",
                {"source_id": r["source_id"], "path": r["vpath"]})
        if r["source_id"] < 0:
            add("wem_unmapped_name", "media", "WEM files whose name is not a numeric media ID and not mapped by SoundbanksInfo",
                {"path": r["vpath"]})
    for r in db.query("SELECT w.codec, w.format_tag, w.valid, a.vpath FROM wem w JOIN asset a ON a.id=w.asset_id"
                      " WHERE a.installation_id=?", (inst_id,)):
        if not r["valid"]:
            add("wem_invalid", "wem_format", "Media that does not parse as RIFF/WAVE", {"path": r["vpath"]})
        elif r["codec"] and r["codec"].startswith("unknown"):
            add(f"wem_codec:0x{(r['format_tag'] or 0):04X}", "wem_format", f"Unknown WEM codec {r['codec']}", {"path": r["vpath"]})
    for r in db.query("SELECT x.*, a.vpath FROM xml_doc x JOIN asset a ON a.id=x.asset_id WHERE a.installation_id=?", (inst_id,)):
        if not r["recognized"]:
            add(f"xml_schema:{r['root_tag']}", "xml", f"XML document with root <{r['root_tag']}> is not a known Wwise schema",
                {"path": r["vpath"], "tags": dict(list((loads(r["tag_counts_json"], {}) or {}).items())[:20])})
        unrec = loads(r["unrecognized_json"], {}) or {}
        if unrec and r["recognized"]:
            add(f"xml_tags:{r['root_tag']}", "xml", "Unrecognised elements inside SoundbanksInfo", {"path": r["vpath"], "tags": unrec})
    for r in db.query("SELECT a.id, a.vpath, a.ext, m.value FROM asset a LEFT JOIN metadata m ON m.entity_type='asset'"
                      " AND m.entity_key=CAST(a.id AS TEXT) AND m.key='magic' WHERE a.installation_id=? AND a.status='unrecognized'",
                      (inst_id,)):
        magic = (r["value"] or "")[:8]
        add(f"file_format:{r['ext']}:{magic}", "file_format", f"Unrecognised audio-area file format '{r['ext']}' (magic {magic})",
            {"path": r["vpath"], "magic": r["value"]})
    for r in db.query("SELECT locator, error FROM asset WHERE installation_id=? AND status='error'", (inst_id,)):
        add("asset_error", "read_error", "Files that could not be read or parsed", {"locator": r["locator"], "error": r["error"]})

    explanations = knowledge.explanations() if knowledge is not None else {}
    with db.transaction() as conn:
        existing = {r["signature"]: r for r in conn.execute(
            "SELECT signature, first_scan_id, status FROM unknown_structure WHERE installation_id=?", (inst_id,)).fetchall()}
        conn.execute("DELETE FROM unknown_structure WHERE installation_id=?", (inst_id,))
        for sig, item in found.items():
            prior = existing.get(sig)
            explained = explanations.get(sig)
            status = "explained" if explained else (prior["status"] if prior and prior["status"] == "resolved" else "open")
            conn.execute(
                "INSERT INTO unknown_structure(installation_id, signature, category, entity_type, entity_key, description,"
                " details_json, occurrences, first_scan_id, last_scan_id, status, knowledge_uid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (inst_id, sig, item["category"], item["entity_type"], item["entity_key"], item["description"],
                 dumps({"examples": item["examples"]}), item["count"], prior["first_scan_id"] if prior else scan_id, scan_id,
                 status, explained["uid"] if explained else None),
            )
    return len(found)
