"""Entry point: GUI by default, plus a small headless CLI.

    CrimsonSoundtrackStudio.exe                     start the GUI
    CrimsonSoundtrackStudio.exe --scan "D:\\Games\\Crimson Desert"
    CrimsonSoundtrackStudio.exe --import-csv guide.csv
    CrimsonSoundtrackStudio.exe --selftest          scan a synthetic install (used by CI on the frozen build)
    CrimsonSoundtrackStudio.exe --print-paths
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from . import APP_DISPLAY_NAME, __version__
from .app_paths import AppPaths, get_paths


def _cli_scan(paths: AppPaths, game: str) -> int:
    from .services import Studio

    studio = Studio(paths)
    try:
        def progress(stats):
            print(f"\r{stats.phase:<32} files={stats.files_discovered} analyzed={stats.files_analyzed} "
                  f"cached={stats.files_cached} bnk={stats.bnks_parsed} wem={stats.wems_discovered}", end="", flush=True)

        result = studio.run_scan(game, progress)
        print()
        print(json.dumps({"status": result.status, "report_dir": str(result.report_dir), "stats": result.stats.to_dict()}, indent=2))
        return 0 if result.status == "completed" else 1
    finally:
        studio.close()


def _selftest(paths: AppPaths) -> int:
    """End-to-end check that needs no game files: build a fake install, scan twice, check outputs."""

    from .analyzer import queries
    from .services import Studio
    from .testing.builders import make_fake_install

    work = paths.temp / "selftest"
    if work.exists():
        shutil.rmtree(work)
    game = work / "Crimson Desert"
    expected = make_fake_install(game)
    studio = Studio(paths)
    try:
        first = studio.run_scan(str(game), allow_inside_app=True)
        second = studio.run_scan(str(game), allow_inside_app=True)
        inst = studio.current_installation_id() or first.installation_id
        music = {m["source_id"] for m in queries.music_assets(studio.db, first.installation_id)}
        checks = {
            "scan_completed": first.status == "completed" and second.status == "completed",
            "second_scan_cached": second.stats.files_cached >= 1 and second.stats.files_analyzed == 0,
            "music_found": set(expected["music_wems"]) <= music,
            "reports_written": all((first.report_dir / f).is_file() for f in
                                   ("scan.json", "music_assets.json", "soundbanks.json", "relationships.json",
                                    "findings.json", "unknown_structures.json", "report.html")),
            "data_inside_app_folder": paths.is_inside(paths.database_file),
            "installation_id": inst is not None,
        }
        report = json.dumps({"version": __version__, "root": str(paths.root), "checks": checks}, indent=2)
        (paths.logs / "selftest.json").write_text(report, encoding="utf-8")  # readable even from the windowed exe
        if sys.stdout is not None:
            print(report)
        return 0 if all(checks.values()) else 1
    finally:
        studio.close()
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="CrimsonSoundtrackStudio", description=APP_DISPLAY_NAME)
    parser.add_argument("--scan", metavar="GAME_DIR", help="analyze an installation without the GUI")
    parser.add_argument("--import-csv", metavar="CSV", help="import a community research CSV")
    parser.add_argument("--selftest", action="store_true", help="run the built-in end-to-end self test")
    parser.add_argument("--print-paths", action="store_true", help="show the portable data folders")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    paths = get_paths().ensure()
    problem = paths.check_writable()
    if not problem:
        # the windowed Windows build has no console: keep CLI output in logs/console.log
        if sys.stdout is None:
            sys.stdout = open(paths.logs / "console.log", "a", encoding="utf-8")  # noqa: SIM115
        if sys.stderr is None:
            sys.stderr = sys.stdout
    if args.print_paths:
        print(json.dumps({"root": str(paths.root), "dirs": [str(d) for d in paths.all_dirs()]}, indent=2))
        return 0
    if problem and (args.scan or args.selftest or args.import_csv):
        print(problem, file=sys.stderr)
        return 2
    if args.selftest:
        return _selftest(paths)
    if args.import_csv:
        from .services import Studio

        studio = Studio(paths)
        try:
            print(json.dumps(studio.import_community_csv(args.import_csv), indent=2))
        finally:
            studio.close()
        if not args.scan:
            return 0
    if args.scan:
        return _cli_scan(paths, args.scan)
    return run_gui(paths, problem)


def run_gui(paths: AppPaths, problem=None) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    if problem:
        QMessageBox.critical(None, APP_DISPLAY_NAME, problem)
        return 2
    from .services import Studio
    from .ui.main_window import MainWindow

    studio = Studio(paths)
    window = MainWindow(studio)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
