import os
import subprocess
import sys
from pathlib import Path

from cstudio import app_paths
from cstudio.app_paths import AppPaths, detect_app_root


def test_frozen_root_is_executable_directory(monkeypatch, tmp_path):
    exe = tmp_path / "portable" / "CrimsonSoundtrackStudio.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.delenv(app_paths.ENV_HOME_OVERRIDE, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)  # the working directory must not matter
    assert detect_app_root() == exe.parent.resolve()


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(app_paths.ENV_HOME_OVERRIDE, str(tmp_path / "home"))
    assert detect_app_root() == (tmp_path / "home").resolve()


def test_all_dirs_inside_root(tmp_path):
    paths = AppPaths(tmp_path / "app").ensure()
    for directory in paths.all_dirs():
        assert directory.is_dir()
        assert paths.is_inside(directory)
    assert paths.is_inside(paths.database_file)
    assert paths.is_inside(paths.settings_file)
    assert not paths.is_inside(tmp_path / "other")
    assert paths.check_writable() is None


def test_no_system_data_dirs(tmp_path):
    paths = AppPaths(tmp_path / "app")
    for directory in paths.all_dirs():
        text = str(directory).lower()
        for forbidden in ("appdata", "programdata", "documents"):
            assert forbidden not in text.replace(str(tmp_path).lower(), "")


def test_moving_app_folder_keeps_data(tmp_path):
    from cstudio.config import Settings
    from cstudio.db.database import Database

    first = AppPaths(tmp_path / "a" / "App").ensure()
    settings = Settings(game_path="X:/Games/Crimson Desert", ai_tier="high")
    settings.save(first)
    db = Database(first.database_file)
    db.get_or_create_installation("X:/Games/Crimson Desert")
    db.close()
    moved = tmp_path / "b" / "Moved"
    moved.parent.mkdir()
    os.replace(first.root, moved)
    second = AppPaths(moved)
    assert Settings.load(second).ai_tier == "high"
    db2 = Database(second.database_file)
    assert db2.query_one("SELECT root_path FROM installation")["root_path"] == "X:/Games/Crimson Desert"
    db2.close()


def test_print_paths_from_other_cwd(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, CSTUDIO_HOME=str(tmp_path / "home"), PYTHONPATH=str(root / "src"))
    out = subprocess.run([sys.executable, "-m", "cstudio", "--print-paths"], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert str((tmp_path / "home").resolve()) in out.stdout
