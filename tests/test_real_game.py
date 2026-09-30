"""Local integration tests against a real Crimson Desert installation.

Skipped unless CRIMSON_DESERT_DIR points at the game folder. Never run in CI
(the proprietary game files are not in the repository or on the runners).

    set CRIMSON_DESERT_DIR=D:\\SteamLibrary\\steamapps\\common\\Crimson Desert
    python -m pytest tests/test_real_game.py -m game -s

Optionally set CRIMSON_SOUNDBANK_GUIDE to a community CSV to cross-validate.
"""

import os
from pathlib import Path

import pytest

from cstudio.analyzer import community, queries
from cstudio.analyzer.scanner import Scanner

GAME = os.environ.get("CRIMSON_DESERT_DIR")
pytestmark = [pytest.mark.game, pytest.mark.skipif(not GAME, reason="CRIMSON_DESERT_DIR not set")]


def _snapshot(root: Path):
    return sorted((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in root.rglob("*") if p.is_file())


def test_real_scan(db, app_paths):
    root = Path(GAME)
    before = _snapshot(root)
    result = Scanner(db, app_paths).run(root)
    assert result.status == "completed"
    assert _snapshot(root) == before, "the installation must never change"
    inst = result.installation_id
    ov = queries.overview(db, inst)
    print("\noverview:", ov)
    banks = queries.soundbanks(db, inst)
    versions = sorted({b["version"] for b in banks})
    print("bank versions:", versions)
    statuses = {}
    for b in banks:
        for k, v in b["parse_status"].items():
            statuses[k] = statuses.get(k, 0) + v
    print("HIRC parse statuses:", statuses)
    assert ov["banks"] > 0
    csv_path = os.environ.get("CRIMSON_SOUNDBANK_GUIDE")
    if csv_path:
        community.import_csv(db, Path(csv_path))
        print("community cross-validation:", community.cross_validate(db, inst))
    second = Scanner(db, app_paths).run(root)
    assert second.stats.files_analyzed == 0 and second.stats.files_cached > 0
