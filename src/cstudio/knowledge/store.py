"""Persistent knowledge: findings, hypotheses and research notes.

Findings are stored in the database *and* mirrored as JSON files under
``data/knowledge`` so knowledge survives a database reset and can be shared:

* ``knowledge/verified/<uid>.json``   - status ``verified``
* ``knowledge/hypotheses/<uid>.json`` - status ``probable``, ``hypothesis``, ``unknown``, ``rejected``
* ``knowledge/research/``             - imported research sources and AI session notes

Status meaning:

* ``verified``   - confirmed by a deterministic check (hash match, parser
                   evidence, database query) or by a human.
* ``probable``   - supported by multiple independent pieces of evidence, not
                   deterministically proven.
* ``hypothesis`` - a proposed explanation that still needs testing.
* ``unknown``    - an open question worth recording.
* ``rejected``   - tested and found false (kept so it is not re-proposed).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from cstudio.app_paths import AppPaths
from cstudio.db.database import Database, dumps, loads, now_iso

STATUSES = ("verified", "probable", "hypothesis", "unknown", "rejected")

CATEGORY_NAME_MAPPING = "name_mapping"
CATEGORY_UNKNOWN_EXPLANATION = "unknown_structure_explanation"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "finding"


def make_uid(category: str, subject_type: str, subject_key: str, statement: str) -> str:
    digest = hashlib.sha1(f"{category}|{subject_type}|{subject_key}|{statement}".encode("utf-8")).hexdigest()[:12]
    return f"{_slug(category or 'general')}-{digest}"


class KnowledgeStore:
    def __init__(self, db: Database, paths: AppPaths) -> None:
        self.db = db
        self.paths = paths

    # --------------------------------------------------------------- write
    def record(
        self,
        *,
        title: str,
        statement: str,
        status: str,
        category: str = "general",
        subject_type: str = "",
        subject_key: str = "",
        evidence: Optional[List[Dict[str, Any]]] = None,
        reasoning: str = "",
        verification: Optional[Dict[str, Any]] = None,
        created_by: str = "parser",
        model_id: Optional[str] = None,
        session_id: Optional[int] = None,
        uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        if status not in STATUSES:
            raise ValueError(f"invalid status {status!r}")
        uid = uid or make_uid(category, subject_type, str(subject_key), statement)
        now = now_iso()
        existing = self.db.query_one("SELECT * FROM finding WHERE uid = ?", (uid,))
        evidence = evidence or []
        if existing:
            merged = loads(existing["evidence_json"], [])
            for item in evidence:
                if item not in merged:
                    merged.append(item)
            self.db.execute(
                "UPDATE finding SET title=?, statement=?, status=?, evidence_json=?, reasoning=?, verification_json=?,"
                " updated_at=?, model_id=COALESCE(?, model_id), session_id=COALESCE(?, session_id) WHERE uid=?",
                (title, statement, status, dumps(merged), reasoning or existing["reasoning"],
                 dumps(verification) if verification is not None else existing["verification_json"],
                 now, model_id, session_id, uid),
            )
        else:
            self.db.execute(
                "INSERT INTO finding(uid,title,statement,status,category,subject_type,subject_key,evidence_json,reasoning,"
                "verification_json,created_by,model_id,session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, title, statement, status, category, subject_type, str(subject_key), dumps(evidence), reasoning,
                 dumps(verification) if verification is not None else None, created_by, model_id, session_id, now, now),
            )
        self.db.commit()
        finding = self.get(uid)
        assert finding is not None
        self._write_file(finding)
        return finding

    def set_status(self, uid: str, status: str, verification: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if status not in STATUSES:
            raise ValueError(f"invalid status {status!r}")
        finding = self.get(uid)
        if finding is None:
            return None
        self.db.execute(
            "UPDATE finding SET status=?, verification_json=COALESCE(?, verification_json), updated_at=? WHERE uid=?",
            (status, dumps(verification) if verification is not None else None, now_iso(), uid),
        )
        self.db.commit()
        self._remove_files(uid)
        finding = self.get(uid)
        assert finding is not None
        self._write_file(finding)
        return finding

    # ---------------------------------------------------------------- read
    def get(self, uid: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM finding WHERE uid = ?", (uid,))
        return self._row(row) if row else None

    def list(self, status: Optional[str] = None, category: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM finding WHERE 1=1"
        params: List[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [self._row(r) for r in self.db.query(sql, params)]

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for row in self.db.query("SELECT status, COUNT(*) c FROM finding GROUP BY status"):
            out[row["status"]] = row["c"]
        return out

    def verified_names(self) -> Dict[int, str]:
        """Name mappings proven in earlier investigations (reused by every scan)."""

        out: Dict[int, str] = {}
        for row in self.db.query(
            "SELECT subject_key, verification_json FROM finding WHERE status='verified' AND category=?",
            (CATEGORY_NAME_MAPPING,),
        ):
            verification = loads(row["verification_json"], {}) or {}
            name = verification.get("name")
            try:
                out[int(row["subject_key"])] = str(name)
            except (TypeError, ValueError):
                continue
        return out

    def explanations(self) -> Dict[str, Dict[str, Any]]:
        """Unknown-structure explanations keyed by structure signature."""

        out: Dict[str, Dict[str, Any]] = {}
        rank = {"verified": 2, "probable": 1}
        for row in self.db.query(
            "SELECT * FROM finding WHERE category=? AND status IN ('verified','probable')", (CATEGORY_UNKNOWN_EXPLANATION,)
        ):
            prev = out.get(row["subject_key"])
            if prev is None or rank[row["status"]] > rank[prev["status"]]:
                out[row["subject_key"]] = self._row(row)
        return out

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        return {
            "uid": row["uid"],
            "title": row["title"],
            "statement": row["statement"],
            "status": row["status"],
            "category": row["category"],
            "subject_type": row["subject_type"],
            "subject_key": row["subject_key"],
            "evidence": loads(row["evidence_json"], []),
            "reasoning": row["reasoning"],
            "verification": loads(row["verification_json"], None),
            "created_by": row["created_by"],
            "model_id": row["model_id"],
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # --------------------------------------------------------------- files
    def _dir_for(self, status: str) -> Path:
        return self.paths.knowledge_verified if status == "verified" else self.paths.knowledge_hypotheses

    def _remove_files(self, uid: str) -> None:
        for directory in (self.paths.knowledge_verified, self.paths.knowledge_hypotheses):
            candidate = directory / f"{uid}.json"
            if candidate.exists():
                candidate.unlink()

    def _write_file(self, finding: Dict[str, Any]) -> None:
        directory = self._dir_for(finding["status"])
        directory.mkdir(parents=True, exist_ok=True)
        other = self.paths.knowledge_hypotheses if directory == self.paths.knowledge_verified else self.paths.knowledge_verified
        stale = other / f"{finding['uid']}.json"
        if stale.exists():
            stale.unlink()
        (directory / f"{finding['uid']}.json").write_text(json.dumps(finding, indent=2, ensure_ascii=False), encoding="utf-8")

    def sync_from_files(self) -> int:
        """Import knowledge files that are not in the database (e.g. after a DB reset or sharing)."""

        imported = 0
        for directory in (self.paths.knowledge_verified, self.paths.knowledge_hypotheses):
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                uid = data.get("uid")
                if not uid or self.db.query_one("SELECT 1 FROM finding WHERE uid=?", (uid,)):
                    continue
                if data.get("status") not in STATUSES:
                    continue
                now = now_iso()
                self.db.execute(
                    "INSERT INTO finding(uid,title,statement,status,category,subject_type,subject_key,evidence_json,reasoning,"
                    "verification_json,created_by,model_id,session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, data.get("title", ""), data.get("statement", ""), data["status"], data.get("category"),
                     data.get("subject_type"), str(data.get("subject_key", "")), dumps(data.get("evidence", [])),
                     data.get("reasoning"), dumps(data.get("verification")) if data.get("verification") is not None else None,
                     data.get("created_by", "import"), data.get("model_id"), None,
                     data.get("created_at", now), data.get("updated_at", now)),
                )
                imported += 1
        if imported:
            self.db.commit()
        return imported

    def write_research_note(self, name: str, content: str) -> Path:
        self.paths.knowledge_research.mkdir(parents=True, exist_ok=True)
        path = self.paths.knowledge_research / f"{_slug(name)}.md"
        path.write_text(content, encoding="utf-8")
        return path
