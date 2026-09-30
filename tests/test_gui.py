import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_main_window_scan_flow(qapp, app_paths, fake_game):
    from cstudio.services import Studio
    from cstudio.ui.main_window import MainWindow

    root, info = fake_game
    studio = Studio(app_paths)
    window = MainWindow(studio)
    window.show()
    window.game_edit.setText(str(root))
    window._game_changed()
    assert "archive packages" in window.game_info.text()
    for tier in ("low", "medium", "high", "custom"):
        window.tier_buttons[tier].setChecked(True)
    window.tier_buttons["medium"].setChecked(True)
    assert window.model_combo.count() >= 1
    window._start_scan(False)
    deadline = time.time() + 30
    while window.workers and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    assert "completed" in window.scan_phase.text()
    assert window.media_table.rowCount() == len(info["music_wems"])
    window.media_table.selectRow(0)
    assert "source_id" in window.media_detail.toPlainText()
    window.close()
