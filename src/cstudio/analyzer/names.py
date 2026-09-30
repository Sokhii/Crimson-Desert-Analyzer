"""Collects names for Wwise IDs from every available source, with provenance.

Wwise derives the IDs of named objects (banks, events, states, switches,
game parameters, busses) from the FNV-1 hash of the lowercased name, so such a
name can be *verified*: ``hash_verified = 1`` means the name provably belongs
to the ID. Media IDs and object IDs of sounds/containers are not name hashes;
names for those are provenance-only.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple

from cstudio.db.database import Database, loads
from cstudio.formats.hashing import wwise_fnv1_32

HASHED_KINDS = {
    "bank", "event", "dialogueevent", "stategroup", "state", "switchgroup", "switch", "gameparameter", "trigger",
    "bus", "auxbus", "argument", "argumentvalue", "audiodevice", "plugin",
}


def _verified(name: str, value: int) -> int:
    return int(bool(name) and wwise_fnv1_32(name) == (value & 0xFFFFFFFF))


def rebuild(db: Database, inst_id: int, knowledge=None) -> int:
    rows: List[Tuple[int, str, str, str, int]] = []

    def add(value: Optional[int], name: Optional[str], kind: str, source: str, hashed: Optional[bool] = None) -> None:
        if value is None or not name:
            return
        name = name.strip()
        if not name:
            return
        is_hashed = kind.lower() in HASHED_KINDS if hashed is None else hashed
        rows.append((int(value), name, kind, source, _verified(name, int(value)) if is_hashed else 0))

    assets = "SELECT id FROM asset WHERE installation_id=?"
    for r in db.query("SELECT entity_key, value FROM metadata WHERE entity_type='asset' AND key='stid_name' AND entity_key IN (SELECT CAST(id AS TEXT) FROM asset WHERE installation_id=?)", (inst_id,)):
        data = loads(r["value"], {}) or {}
        add(data.get("bank_id"), data.get("name"), "bank", "bnk_stid")
    for r in db.query("SELECT b.bank_id, a.vpath FROM bnk b JOIN asset a ON a.id=b.asset_id WHERE a.installation_id=?", (inst_id,)):
        stem = PurePosixPath(r["vpath"].replace("\\", "/")).stem
        if wwise_fnv1_32(stem) == r["bank_id"]:
            add(r["bank_id"], stem, "bank", "file_name")
    for r in db.query(f"SELECT bank_id, name, path FROM xml_bank WHERE asset_id IN ({assets})", (inst_id,)):
        add(r["bank_id"], r["name"], "bank", "soundbanksinfo")
    for r in db.query(f"SELECT event_id, name FROM xml_event WHERE asset_id IN ({assets})", (inst_id,)):
        add(r["event_id"], r["name"], "event", "soundbanksinfo")
    for r in db.query(f"SELECT object_id, name, kind FROM xml_object WHERE asset_id IN ({assets})", (inst_id,)):
        add(r["object_id"], r["name"], r["kind"].lower(), "soundbanksinfo")
    for r in db.query(f"SELECT media_id, short_name, path, cache_path FROM xml_media WHERE asset_id IN ({assets})", (inst_id,)):
        add(r["media_id"], r["short_name"], "media", "soundbanksinfo", hashed=False)
    for r in db.query("SELECT media_id, original_name, bank_name FROM community_media"):
        add(r["media_id"], r["original_name"], "media", "community", hashed=False)
        if r["bank_name"]:
            add(wwise_fnv1_32(r["bank_name"]), r["bank_name"], "bank", "community")
    if knowledge is not None:
        for value, name in knowledge.verified_names().items():
            add(value, name, "knowledge", "knowledge", hashed=True)
    with db.transaction() as conn:
        conn.execute("DELETE FROM name")
        conn.executemany(
            "INSERT OR IGNORE INTO name(id_value, name, kind, source, hash_verified) VALUES (?,?,?,?,?)", rows
        )
    return len(rows)


def best_names(db: Database, ids: List[int]) -> Dict[int, str]:
    """Best display name per ID: hash-verified first, then soundbanksinfo, then others."""

    out: Dict[int, Tuple[int, str]] = {}
    priority = {"bnk_stid": 5, "soundbanksinfo": 4, "file_name": 4, "knowledge": 3, "community": 2}
    chunk = 500
    for i in range(0, len(ids), chunk):
        part = ids[i : i + chunk]
        marks = ",".join("?" * len(part))
        for r in db.query(f"SELECT id_value, name, source, hash_verified FROM name WHERE id_value IN ({marks})", part):
            score = r["hash_verified"] * 10 + priority.get(r["source"], 1)
            prev = out.get(r["id_value"])
            if prev is None or score > prev[0]:
                out[r["id_value"]] = (score, r["name"])
    return {k: v[1] for k, v in out.items()}
