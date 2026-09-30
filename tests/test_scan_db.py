import json
import sqlite3

import pytest

from cstudio.analyzer import community, queries
from cstudio.analyzer.scanner import Scanner
from cstudio.db.database import Database, MigrationError
from cstudio.db.schema import LATEST_VERSION


def _tree_snapshot(root):
    return sorted((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*"))


def test_migrations_fresh_and_idempotent(app_paths):
    db = Database(app_paths.database_file)
    assert db.schema_version() == LATEST_VERSION
    assert db.migrate() == []
    db.close()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "x.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 5}")
    conn.close()
    with pytest.raises(MigrationError):
        Database(path)


def test_scan_is_read_only(db, app_paths, fake_game):
    root, _ = fake_game
    before = _tree_snapshot(root)
    Scanner(db, app_paths).run(root)
    assert _tree_snapshot(root) == before


def test_scan_relationships_and_music(scanned, db):
    result, info, _root = scanned
    inst = result.installation_id
    music = {m["source_id"]: m for m in queries.music_assets(db, inst)}
    for media in info["music_wems"]:
        assert media in music, media
        assert music[media]["role"] == "music"
    desert = music[433831842]
    assert desert["owners"][0]["type"] == "MusicTrack"
    assert "MusicSegment" in [c["type"] for c in desert["containers"]]
    assert "MusicSwitchContainer" in [c["type"] for c in desert["containers"]]
    assert [e["name"] for e in desert["events"]] == ["Play_BGM_World"]
    assert [b["name"] for b in desert["banks"]] == ["bgm"]
    assert desert["duration_s"] == 180.0 and desert["wwise_source_duration_ms"] == 180000.0
    assert any(ev["signal"] == "wwise_music_track" for ev in desert["evidence"])
    wind = queries.media_record(db, inst, info["ambience_wems"][0])
    assert wind["role"] == "ambience"
    stinger = music[558103]
    assert stinger["files"][0]["container"] == "embedded"


def test_names_hash_verified(scanned, db):
    rows = db.query("SELECT name, source, hash_verified FROM name WHERE name='bgm'")
    assert any(r["hash_verified"] for r in rows)
    ev = db.query_one("SELECT hash_verified FROM name WHERE name='Play_BGM_World'")
    assert ev["hash_verified"] == 1


def test_unknowns_detected(scanned, db):
    result, _info, _root = scanned
    sigs = {u["signature"] for u in queries.unknown_structures(db, result.installation_id)}
    assert "hirc_type:v150:0x2A" in sigs
    assert any(s.startswith("file_format:.pasnd") for s in sigs)


def test_cache_second_scan(scanned, db, app_paths):
    result, _info, root = scanned
    again = Scanner(db, app_paths).run(root)
    assert again.stats.files_analyzed == 0
    assert again.stats.files_cached == result.stats.files_analyzed
    before = queries.overview(db, result.installation_id)
    assert before["banks"] == 2


def test_cache_invalidation_on_change(scanned, db, app_paths):
    result, _info, root = scanned
    from cstudio.testing.builders import make_wem, write_package

    write_package(root / "0005", {"sound/77.wem": make_wem(seconds=61.0)})
    again = Scanner(db, app_paths).run(root)
    assert again.stats.files_analyzed == 1
    assert queries.media_record(db, result.installation_id, 77)["duration_s"] == 61.0


def test_reproducible_scan(tmp_path, fake_game):
    from cstudio.app_paths import AppPaths

    root, _ = fake_game
    outputs = []
    for name in ("a", "b"):
        paths = AppPaths(tmp_path / name).ensure()
        d = Database(paths.database_file)
        res = Scanner(d, paths).run(root)
        data = json.loads((res.report_dir / "music_assets.json").read_text())["data"]
        banks = json.loads((res.report_dir / "soundbanks.json").read_text())["data"]
        rel = json.loads((res.report_dir / "relationships.json").read_text())["data"]
        outputs.append((data, [{k: v for k, v in b.items() if k != "asset_id"} for b in banks], sorted(map(tuple, rel["edges"]))))
        d.close()
    assert outputs[0] == outputs[1]


def test_report_files(scanned):
    result, _info, _root = scanned
    for name in ("scan.json", "music_assets.json", "soundbanks.json", "relationships.json", "findings.json",
                 "unknown_structures.json", "report.html"):
        assert (result.report_dir / name).is_file()
    envelope = json.loads((result.report_dir / "music_assets.json").read_text())
    assert envelope["schema"].endswith("/music_assets") and envelope["schema_version"] == 1


def test_community_import_and_cross_validation(scanned, db, tmp_path, app_paths):
    result, _info, root = scanned
    csv_path = tmp_path / "guide.csv"
    csv_path.write_text(
        "Audio ID,English Description,Location / Context,Category,Original File Name,SoundBank Source\n"
        "433831842,Region big desert,Desert Areas,Music,bgm_cd_region.wav,bgm\n"
        "16721128,Env desert,Desert Areas / Environmental Ambience,Ambience,env.wav,env_region_desert\n"
        "480286974,Wrong bank claim,Desert,Music,x.wav,not_the_bank\n"
        "notanumber,bad,,,,\n", encoding="utf-8")
    out = community.import_csv(db, csv_path)
    assert out["imported"] == 3 and out["invalid_rows"] == 1
    assert community.import_csv(db, csv_path)["imported"] == 0  # idempotent
    Scanner(db, app_paths).run(root)
    report = community.cross_validate(db, result.installation_id)
    assert report["bank_claims_confirmed"] == 2 and report["bank_claims_contradicted"] == 1
    rec = queries.media_record(db, result.installation_id, 433831842)
    assert any(e["source"] == "community" for e in rec["evidence"])


def test_malformed_files_do_not_stop_scan(tmp_path, db, app_paths):
    from cstudio.testing.builders import write_package

    root = tmp_path / "Game"
    write_package(root / "0004", {"sound/broken.bnk": b"BKHD\xff\xff\xff\xff", "sound/1.wem": b"RIFF",
                                  "sound/soundbanksinfo.xml": b"<SoundBanksInfo><broken"})
    (root / "0009").mkdir()
    (root / "0009" / "0.pamt").write_bytes(b"\x00\x01")
    res = Scanner(db, app_paths).run(root)
    assert res.status == "completed"
    assert res.stats.errors >= 1  # the corrupt pamt
    sigs = {u["signature"] for u in queries.unknown_structures(db, res.installation_id)}
    assert "wem_invalid" in sigs


def test_cancel(db, app_paths, fake_game):
    import threading

    root, _ = fake_game
    cancel = threading.Event()
    cancel.set()
    res = Scanner(db, app_paths, cancel=cancel).run(root)
    assert res.status == "cancelled"


def test_media_banks_ignore_banks_that_only_share_ancestors(tmp_path, db, app_paths, fake_game):
    """A parent container duplicated into another bank must not attach that bank to the media."""
    from cstudio.testing.builders import BankBuilder, write_package

    root, _ = fake_game
    other = BankBuilder("unrelated_bank")
    other.actor_mixer(4001, [])  # copy of the world music switch id, as Wwise duplicates parents across banks
    other.actor_mixer(3001, [])
    write_package(root / "0006", {"sound/unrelated_bank.bnk": other.build()})
    res = Scanner(db, app_paths).run(root)
    rec = queries.media_record(db, res.installation_id, 433831842)
    assert [b["name"] for b in rec["banks"]] == ["bgm"]
    stinger = queries.media_record(db, res.installation_id, 558103)
    assert [b["name"] for b in stinger["banks"]] == ["bgm"]  # embedded media keeps its bank
    rel = (res.report_dir / "relationships.json").read_text(encoding="utf-8")
    assert "\n" not in rel.strip()  # compact output


def test_export_file_is_read_only_and_inside_output(scanned, db, app_paths):
    from cstudio.analyzer.export import FileLookupError, export_file

    result, _info, root = scanned
    before = _tree_snapshot(root)
    target = export_file(db, app_paths, result.installation_id, "sound/bgm.bnk")
    assert target.read_bytes()[:4] == b"BKHD"  # decrypted/decompressed bank
    assert app_paths.is_inside(target) and target.parts[-3:] == ("0004", "sound", "bgm.bnk")
    xml = export_file(db, app_paths, result.installation_id, "0004/sound/soundbanksinfo.xml")
    assert b"SoundBanksInfo" in xml.read_bytes()  # encrypted + LZ4 in the archive
    assert _tree_snapshot(root) == before
    with pytest.raises(FileLookupError):
        export_file(db, app_paths, result.installation_id, "loose:../../outside.txt")
    with pytest.raises(FileLookupError):
        export_file(db, app_paths, result.installation_id, "sound/nope.bnk")


def test_cli_export(tmp_path, fake_game):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root, _ = fake_game
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, CSTUDIO_HOME=str(tmp_path / "home"), PYTHONPATH=str(repo / "src"))
    scan = subprocess.run([sys.executable, "-m", "cstudio", "--scan", str(root)], env=env, capture_output=True, text=True, timeout=120)
    assert scan.returncode == 0, scan.stderr
    out = subprocess.run([sys.executable, "-m", "cstudio", "--export", "sound/bgm.bnk"], env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "home" / "output" / "exports" / "0004" / "sound" / "bgm.bnk").is_file()
