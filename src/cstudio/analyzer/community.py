"""Import of community research tables (e.g. the Nexus "Ultimate Soundbank Guide").

Community data is kept separate from parser facts: it is stored in
``community_media`` with a ``research_source`` row describing where it came
from, and it only ever contributes *evidence* (never overrides parsed data).
After a scan, :func:`cross_validate` checks each community claim against what
the parser found, so the report can say which claims were experimentally
confirmed.
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Dict, Optional

from cstudio.db.database import Database, dumps, now_iso
from cstudio.formats.hashing import wwise_fnv1_32

COLUMN_ALIASES = {
    "media_id": ("audio id", "media id", "id", "wem id", "source id"),
    "description": ("english description", "description", "name"),
    "context": ("location / context", "context", "location"),
    "category": ("category", "type"),
    "original_name": ("original file name", "file name", "filename", "shortname"),
    "bank_name": ("soundbank source", "soundbank", "bank", "bank name"),
}


def _map_columns(fieldnames) -> Dict[str, Optional[str]]:
    lower = {f.lower().strip(): f for f in fieldnames or []}
    out: Dict[str, Optional[str]] = {}
    for key, aliases in COLUMN_ALIASES.items():
        out[key] = next((lower[a] for a in aliases if a in lower), None)
    return out


def import_csv(db: Database, path: Path, name: Optional[str] = None, url: str = "",
               license_note: str = "User-supplied community file; not redistributed with the application.") -> Dict[str, object]:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = _map_columns(reader.fieldnames)
    if not cols["media_id"]:
        raise ValueError(f"{path.name}: no media ID column found (columns: {reader.fieldnames})")
    sha1 = hashlib.sha1(raw).hexdigest()
    existing = db.query_one("SELECT id FROM research_source WHERE file_sha1=?", (sha1,))
    if existing:
        return {"source_id": existing["id"], "imported": 0, "skipped": "already imported"}
    cur = db.execute(
        "INSERT INTO research_source(name, kind, url, trust_label, license_note, file_sha1, imported_at, details_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (name or path.stem, "community_csv", url, "community-provided", license_note, sha1, now_iso(),
         dumps({"columns": cols, "file_name": path.name})),
    )
    source_id = int(cur.lastrowid)
    rows = []
    bad = 0
    for row in reader:
        try:
            media_id = int(str(row.get(cols["media_id"], "")).strip())
        except ValueError:
            bad += 1
            continue
        rows.append((source_id, media_id, *(row.get(cols[k]) if cols[k] else None
                                             for k in ("description", "context", "category", "original_name", "bank_name"))))
    db.executemany(
        "INSERT INTO community_media(research_source_id, media_id, description, context, category, original_name, bank_name)"
        " VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    return {"source_id": source_id, "imported": len(rows), "invalid_rows": bad}


def cross_validate(db: Database, inst_id: int) -> Dict[str, object]:
    """Compare community claims with parser facts for one installation."""

    total = found = bank_confirmed = bank_contradicted = 0
    examples_contradicted = []
    wem_ids = {r[0] for r in db.query(
        "SELECT DISTINCT w.source_id FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=?", (inst_id,))}
    media_banks: Dict[int, set] = {}
    for r in db.query("SELECT m.source_id, b.bank_id FROM media_source m JOIN bnk b ON b.asset_id=m.bank_asset_id"
                      " JOIN asset a ON a.id=m.bank_asset_id WHERE a.installation_id=?", (inst_id,)):
        media_banks.setdefault(r["source_id"], set()).add(r["bank_id"])
    for r in db.query("SELECT media_id, bank_name FROM community_media"):
        total += 1
        if r["media_id"] in wem_ids or r["media_id"] in media_banks:
            found += 1
        banks = media_banks.get(r["media_id"])
        if banks and r["bank_name"]:
            if wwise_fnv1_32(r["bank_name"]) in banks:
                bank_confirmed += 1
            else:
                bank_contradicted += 1
                if len(examples_contradicted) < 20:
                    examples_contradicted.append({"media_id": r["media_id"], "claimed_bank": r["bank_name"],
                                                  "parsed_bank_ids": sorted(banks)})
    return {
        "community_rows": total,
        "media_found_in_scan": found,
        "bank_claims_confirmed": bank_confirmed,
        "bank_claims_contradicted": bank_contradicted,
        "contradiction_examples": examples_contradicted,
    }
