import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cstudio.app_paths import AppPaths  # noqa: E402
from cstudio.db.database import Database  # noqa: E402
from cstudio.testing.builders import make_fake_install  # noqa: E402


@pytest.fixture
def app_paths(tmp_path) -> AppPaths:
    return AppPaths(tmp_path / "CrimsonSoundtrackStudio").ensure()


@pytest.fixture
def db(app_paths) -> Database:
    database = Database(app_paths.database_file)
    yield database
    database.close()


@pytest.fixture
def fake_game(tmp_path):
    root = tmp_path / "Crimson Desert"
    info = make_fake_install(root)
    return root, info


@pytest.fixture
def scanned(db, app_paths, fake_game):
    from cstudio.analyzer.scanner import Scanner

    root, info = fake_game
    result = Scanner(db, app_paths).run(root)
    assert result.status == "completed"
    return result, info, root
