"""Machine-readable JSON output plus a human-readable HTML report.

The JSON files (and the SQLite database) are authoritative. The HTML report is
a convenience view and is never meant to be scraped.

Output per scan: ``data/scans/scan_<id>/`` and a copy in ``data/reports/latest/``.
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from cstudio import __version__
from cstudio.app_paths import AppPaths
from cstudio.db.database import Database, loads, now_iso

from . import queries

SCHEMA_ID = "crimson-soundtrack-studio/analysis"
SCHEMA_VERSION = 1


def _write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


def _envelope(kind: str, inst: dict, scan_id: int, data: Any) -> Dict[str, Any]:
    return {
        "schema": f"{SCHEMA_ID}/{kind}",
        "schema_version": SCHEMA_VERSION,
        "generator": f"CrimsonSoundtrackStudio {__version__}",
        "generated_at": now_iso(),
        "scan_id": scan_id,
        "installation": inst,
        "data": data,
    }


def write_all(db: Database, paths: AppPaths, inst_id: int, scan_id: int, stats: Dict[str, Any], layout) -> Path:
    out = paths.scans / f"scan_{scan_id:05d}"
    out.mkdir(parents=True, exist_ok=True)
    inst_row = db.query_one("SELECT * FROM installation WHERE id=?", (inst_id,))
    inst = {"id": inst_id, "root_path": inst_row["root_path"] if inst_row else None,
            "package_dirs": getattr(layout, "package_dirs", []), "notes": getattr(layout, "notes", [])}
    overview = queries.overview(db, inst_id)
    music = queries.music_assets(db, inst_id)
    banks = queries.soundbanks(db, inst_id)
    unknown = queries.unknown_structures(db, inst_id)
    findings = [dict(f) for f in _findings(db)]
    contexts = []
    for r in db.query("SELECT * FROM media_context WHERE installation_id=? ORDER BY source_id", (inst_id,)):
        contexts.append({
            "source_id": r["source_id"], "owner_object_id": r["owner_object_id"], "owner_type": r["owner_type"],
            "containers": loads(r["container_ids_json"], []), "container_types": loads(r["container_types_json"], []),
            "events": loads(r["event_ids_json"], []), "banks": loads(r["bank_ids_json"], []),
        })
    edges = queries.relationship_edges(db, inst_id)
    scan_payload = {
        "stats": stats,
        "overview": overview,
        "extensions": queries.extension_summary(db, inst_id)[:200],
        "research_sources": [dict(r) for r in db.query("SELECT id, name, kind, url, trust_label, imported_at FROM research_source")],
    }
    _write(out / "scan.json", _envelope("scan", inst, scan_id, scan_payload))
    _write(out / "music_assets.json", _envelope("music_assets", inst, scan_id, music))
    _write(out / "soundbanks.json", _envelope("soundbanks", inst, scan_id, banks))
    _write(out / "relationships.json", _envelope("relationships", inst, scan_id, {
        "edge_format": ["from", "to", "kind", "confidence", "bank_id"],
        "edges": [[e["from"], e["to"], e["kind"], e["confidence"], e["bank_id"]] for e in edges],
        "media_context": contexts,
    }))
    _write(out / "findings.json", _envelope("findings", inst, scan_id, findings))
    _write(out / "unknown_structures.json", _envelope("unknown_structures", inst, scan_id, unknown))
    (out / "report.html").write_text(_html(inst, scan_id, stats, overview, music, banks, unknown, findings), encoding="utf-8")
    latest = paths.reports / "latest"
    if latest.exists():
        shutil.rmtree(latest)
    shutil.copytree(out, latest)
    return out


def _findings(db: Database) -> List[Dict[str, Any]]:
    rows = db.query("SELECT * FROM finding ORDER BY status, updated_at DESC")
    for r in rows:
        yield {
            "uid": r["uid"], "title": r["title"], "statement": r["statement"], "status": r["status"],
            "category": r["category"], "subject_type": r["subject_type"], "subject_key": r["subject_key"],
            "evidence": loads(r["evidence_json"], []), "reasoning": r["reasoning"],
            "verification": loads(r["verification_json"], None), "created_by": r["created_by"], "model_id": r["model_id"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        }


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape('' if c is None else str(c))}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _html(inst, scan_id, stats, overview, music, banks, unknown, findings) -> str:
    music_rows = [[m["source_id"], m["name"], m["role"], m["score"], m["duration_s"], m["codec"], m["channels"],
                   ", ".join(str(b["name"] or b["bank_id"]) for b in m["banks"]),
                   ", ".join(str(e["name"] or e["event_id"]) for e in m["events"][:3])] for m in music[:3000]]
    bank_rows = [[b["path"], b["name"], b["bank_id"], b["version"], b["object_count"], b["embedded_media"],
                  json.dumps(b["parse_status"])] for b in banks]
    unknown_rows = [[u["status"], u["category"], u["signature"], u["occurrences"], u["description"]] for u in unknown]
    finding_rows = [[f["status"], f["title"], f["statement"], f["created_by"]] for f in findings]
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Crimson Desert audio analysis - scan {scan_id}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;background:#141414;color:#e8e2da}}
h1,h2{{color:#e0564b}}table{{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:24px}}
td,th{{border:1px solid #333;padding:3px 6px;text-align:left;vertical-align:top}}th{{background:#2a2020}}
.note{{color:#b9ada0}}</style></head><body>
<h1>Crimson Desert audio analysis</h1>
<p class="note">Installation: {html.escape(str(inst.get('root_path')))} &middot; scan {scan_id} &middot; generated {now_iso()}.
The JSON files next to this report and the SQLite database are the authoritative output.</p>
<h2>Summary</h2>{_table(["metric", "value"], [[k, json.dumps(v) if isinstance(v, dict) else v] for k, v in overview.items()])}
<h2>Scan statistics</h2>{_table(["metric", "value"], [[k, v] for k, v in stats.items() if k != 'error_messages'])}
<h2>Music assets ({len(music)})</h2>{_table(["media id", "name", "role", "score", "duration s", "codec", "ch", "banks", "events"], music_rows)}
<h2>Soundbanks ({len(banks)})</h2>{_table(["path", "name", "bank id", "version", "objects", "embedded", "parse status"], bank_rows)}
<h2>Unknown / unresolved structures ({len(unknown)})</h2>{_table(["status", "category", "signature", "count", "description"], unknown_rows)}
<h2>Findings ({len(findings)})</h2>{_table(["status", "title", "statement", "by"], finding_rows)}
</body></html>"""
