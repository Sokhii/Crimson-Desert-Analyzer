"""Evidence-based identification of music assets.

This deliberately does *not* build a gameplay taxonomy (combat / town / boss
...). It only decides the broad audio role of each media item - ``music``,
``ambience``, ``sfx``, ``voice`` or ``unknown`` - and keeps every piece of
evidence with its weight and source so the decision can be audited, and so the
future matching layer can reason about it.

Evidence sources, strongest first:

1. Wwise structure: the media is a source of a ``MusicTrack`` inside the
   interactive-music hierarchy (parser-verified).
2. Bank identity: the bank's (hash-verified) name, e.g. ``bgm``.
3. Names: SoundbanksInfo short names / event names containing music tokens.
4. Community research (imported CSVs).
5. Audio properties: duration, channel count, loop points, tempo/meter.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Tuple

from cstudio.db.database import Database, dumps, loads

MUSIC_TOKENS = re.compile(r"(^|[^a-z])(bgm|music|mus|theme|ost|score|soundtrack|stinger|combat_music|mx)([^a-z]|$)")
AMB_TOKENS = re.compile(r"(^|[^a-z])(env|amb|ambience|ambient|walla|wind|rain|thunder|insect|bird|ocean|water|lp)([^a-z]|$)")
SFX_TOKENS = re.compile(r"(^|[^a-z])(sfx|fx|ui|foley|impact|hit|swing|step|footstep|skill|expl|cannon|weapon)([^a-z]|$)")
VOICE_TOKENS = re.compile(r"(^|[^a-z])(vo|vox|voice|dialog|dialogue|vce|questdialog|aidialog|nhm|nhw)([^a-z]|$)")


def _tokens(text: str) -> str:
    return re.sub(r"[\s\-./\\]+", "_", text.lower())


def classify(db: Database, inst_id: int, scan_id: int) -> int:
    contexts = {r["source_id"]: r for r in db.query("SELECT * FROM media_context WHERE installation_id=?", (inst_id,))}
    wems: Dict[int, dict] = {}
    for r in db.query(
        "SELECT w.source_id, w.duration_s, w.channels, w.codec, w.loops_json, w.container FROM wem w JOIN asset a ON a.id=w.asset_id"
        " WHERE a.installation_id=? AND w.source_id>=0",
        (inst_id,),
    ):
        wems.setdefault(r["source_id"], dict(r))
    media_ids = set(contexts) | set(wems)
    xml_names: Dict[int, List[str]] = defaultdict(list)
    for r in db.query(
        "SELECT media_id, short_name FROM xml_media WHERE asset_id IN (SELECT id FROM asset WHERE installation_id=?)", (inst_id,)
    ):
        if r["short_name"]:
            xml_names[r["media_id"]].append(r["short_name"])
        media_ids.add(r["media_id"])
    community: Dict[int, List[dict]] = defaultdict(list)
    for r in db.query("SELECT * FROM community_media"):
        community[r["media_id"]].append(dict(r))
    names = {r["id_value"]: r["name"] for r in db.query("SELECT id_value, name FROM name WHERE hash_verified=1")}
    unverified = {r["id_value"]: r["name"] for r in db.query("SELECT id_value, name FROM name WHERE hash_verified=0")}
    segment_meter = _segment_meters(db, inst_id)

    rows = []
    music_count = 0
    for media in sorted(media_ids):
        evidence: List[dict] = []
        scores = {"music": 0.0, "ambience": 0.0, "sfx": 0.0, "voice": 0.0}

        def add(role: str, weight: float, signal: str, detail: str, source: str) -> None:
            scores[role] += weight
            evidence.append({"role": role, "weight": round(weight, 3), "signal": signal, "detail": detail, "source": source})

        ctx = contexts.get(media)
        if ctx is not None:
            owner_type = ctx["owner_type"]
            container_types = loads(ctx["container_types_json"], []) or []
            if owner_type == "MusicTrack":
                add("music", 0.55, "wwise_music_track", "media is a source of a Wwise MusicTrack (interactive music hierarchy)", "parser")
            elif owner_type == "Sound":
                add("sfx", 0.05, "wwise_sound", "media is a source of a plain Wwise Sound object", "parser")
            music_containers = [t for t in container_types if t.startswith("Music")]
            if music_containers and owner_type != "MusicTrack":
                add("music", 0.2, "music_container_ancestor", f"ancestors include {sorted(set(music_containers))}", "parser")
            meter = None
            for cid in loads(ctx["container_ids_json"], []) or []:
                if cid in segment_meter:
                    meter = segment_meter[cid]
                    break
            if meter:
                add("music", 0.05, "musical_meter", f"segment tempo {meter[0]:.1f} BPM, {meter[1]}", "parser")
            for bank_id in loads(ctx["bank_ids_json"], []) or []:
                bank_name = names.get(bank_id) or unverified.get(bank_id)
                if not bank_name:
                    continue
                verified = bank_id in names
                t = _tokens(bank_name)
                weight = 0.25 if verified else 0.15
                label = "hash-verified" if verified else "unverified"
                if MUSIC_TOKENS.search(t):
                    add("music", weight, "bank_name", f"bank '{bank_name}' ({label})", "names")
                elif AMB_TOKENS.search(t):
                    add("ambience", weight, "bank_name", f"bank '{bank_name}' ({label})", "names")
                elif VOICE_TOKENS.search(t):
                    add("voice", weight, "bank_name", f"bank '{bank_name}' ({label})", "names")
                elif SFX_TOKENS.search(t):
                    add("sfx", weight * 0.8, "bank_name", f"bank '{bank_name}' ({label})", "names")
            event_names = [names.get(e) for e in loads(ctx["event_ids_json"], []) or [] if names.get(e)]
            for ev in event_names[:5]:
                t = _tokens(ev)
                if MUSIC_TOKENS.search(t):
                    add("music", 0.1, "event_name", f"reachable from event '{ev}'", "names")
                    break
        for short in xml_names.get(media, [])[:3]:
            t = _tokens(short)
            if MUSIC_TOKENS.search(t) or t.startswith("cd_"):
                add("music", 0.15, "media_name", f"SoundbanksInfo name '{short}'", "soundbanksinfo")
            elif AMB_TOKENS.search(t):
                add("ambience", 0.2, "media_name", f"SoundbanksInfo name '{short}'", "soundbanksinfo")
            elif VOICE_TOKENS.search(t):
                add("voice", 0.2, "media_name", f"SoundbanksInfo name '{short}'", "soundbanksinfo")
            elif SFX_TOKENS.search(t):
                add("sfx", 0.15, "media_name", f"SoundbanksInfo name '{short}'", "soundbanksinfo")
        for item in community.get(media, [])[:2]:
            category = (item.get("category") or "").lower()
            detail = f"community research: {item.get('category')} / {item.get('context')} ('{item.get('original_name')}')"
            if category == "music":
                add("music", 0.2, "community_category", detail, "community")
            elif category in ("ambience", "ambient"):
                add("ambience", 0.2, "community_category", detail, "community")
            elif category in ("voice", "dialogue"):
                add("voice", 0.2, "community_category", detail, "community")
            elif category:
                add("sfx", 0.1, "community_category", detail, "community")
        w = wems.get(media)
        if w:
            duration = w["duration_s"]
            if duration is not None:
                if duration >= 60:
                    add("music", 0.1, "duration", f"{duration:.1f}s long", "wem_header")
                elif duration >= 20:
                    add("music", 0.04, "duration", f"{duration:.1f}s long", "wem_header")
                elif duration < 3:
                    add("sfx", 0.08, "duration", f"only {duration:.2f}s long", "wem_header")
            if (w["channels"] or 0) >= 2:
                add("music", 0.03, "channels", f"{w['channels']} channels", "wem_header")
            elif w["channels"] == 1:
                add("sfx", 0.03, "channels", "mono", "wem_header")
            if loads(w["loops_json"], []):
                evidence.append({"role": "info", "weight": 0, "signal": "loop_points", "detail": "WEM carries smpl loop points", "source": "wem_header"})
        for role in scores:
            scores[role] = min(1.0, scores[role])
        role, score = max(scores.items(), key=lambda kv: kv[1])
        if score < 0.15:
            role = "unknown"
        if role == "music":
            if score >= 0.6:
                label = "music"
            elif score >= 0.4:
                label = "likely_music"
            else:
                label = "possible_music"
        else:
            label = role
        confidence = "high" if score >= 0.7 else "medium" if score >= 0.4 else "low"
        if label in ("music", "likely_music"):
            music_count += 1
        rows.append((inst_id, "media", media, label, round(score, 3), confidence,
                     dumps({"scores": {k: round(v, 3) for k, v in scores.items()}, "evidence": evidence}), scan_id))
    with db.transaction() as conn:
        conn.execute("DELETE FROM classification WHERE installation_id=?", (inst_id,))
        conn.executemany(
            "INSERT INTO classification(installation_id, entity_type, entity_key, role, score, confidence, evidence_json, scan_id)"
            " VALUES (?,?,?,?,?,?,?,?)", rows)
    return music_count


def _segment_meters(db: Database, inst_id: int) -> Dict[int, Tuple[float, str]]:
    out: Dict[int, Tuple[float, str]] = {}
    for r in db.query(
        "SELECT object_id, fields_json FROM wwise_object WHERE type_code IN (10,12,13) AND bank_asset_id IN"
        " (SELECT id FROM asset WHERE installation_id=?)", (inst_id,)
    ):
        fields = loads(r["fields_json"], {}) or {}
        meter = fields.get("meter") or {}
        tempo = meter.get("tempo_bpm")
        if tempo and 20 <= tempo <= 400 and meter.get("override_parent"):
            out[r["object_id"]] = (float(tempo), str(meter.get("time_signature")))
    return out
