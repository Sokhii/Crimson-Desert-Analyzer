"""Main window. Presentation only - all logic lives in ``cstudio.services`` and below."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QRadioButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from cstudio import APP_DISPLAY_NAME, __version__
from cstudio.ai.catalog import LocalModel
from cstudio.analyzer import queries
from cstudio.analyzer.discovery import default_steam_candidates
from cstudio.services import Studio

from .workers import Worker

STYLE = """
QWidget { background: #17181b; color: #e6e1da; font-size: 10pt; }
QGroupBox { border: 1px solid #3a3136; border-radius: 6px; margin-top: 14px; padding: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #e0645a; font-weight: bold; }
QPushButton { background: #3b2426; border: 1px solid #6b3a3a; border-radius: 4px; padding: 6px 14px; }
QPushButton:hover { background: #55302f; }
QPushButton:disabled { background: #26262a; color: #777; border-color: #333; }
QPushButton#primary { background: #8e2d2a; font-weight: bold; }
QLineEdit, QComboBox, QPlainTextEdit, QTableWidget, QListWidget { background: #101113; border: 1px solid #333; }
QHeaderView::section { background: #2a2224; padding: 4px; border: 0; }
QTabBar::tab { background: #232326; padding: 8px 16px; }
QTabBar::tab:selected { background: #3b2426; }
QProgressBar { border: 1px solid #333; border-radius: 3px; text-align: center; }
QProgressBar::chunk { background: #a8332f; }
"""


def _mono() -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.Monospace)
    return font


def _fill_table(table: QTableWidget, headers: List[str], rows: List[List[Any]], keys: Optional[List[Any]] = None) -> None:
    table.setSortingEnabled(False)
    table.clear()
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            item = QTableWidgetItem()
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                item.setData(Qt.DisplayRole, value)
            else:
                item.setText("" if value is None else str(value))
            if c == 0 and keys is not None:
                item.setData(Qt.UserRole, keys[r])
            table.setItem(r, c, item)
    table.setSortingEnabled(True)
    table.resizeColumnsToContents()
    table.horizontalHeader().setStretchLastSection(True)


def _make_table() -> QTableWidget:
    table = QTableWidget()
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(False)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    return table


class MainWindow(QMainWindow):
    def __init__(self, studio: Studio) -> None:
        super().__init__()
        self.studio = studio
        self.workers: List[Worker] = []
        self.scan_worker: Optional[Worker] = None
        self.ai_worker: Optional[Worker] = None
        self.dl_worker: Optional[Worker] = None
        self.pause_event = threading.Event()
        self.chain_ai_after_scan = False
        self.setWindowTitle(f"{APP_DISPLAY_NAME} {__version__}")
        self.resize(1320, 860)
        self.setStyleSheet(STYLE)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_home(), "Home")
        self.tabs.addTab(self._build_scan(), "Scan")
        self.tabs.addTab(self._build_results(), "Results")
        self.tabs.addTab(self._build_ai(), "AI Investigation")
        self.statusBar().showMessage(f"Data folder: {studio.paths.root}")
        self._load_settings_into_ui()
        self.refresh_results()

    # ================================================================ HOME
    def _build_home(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        box = QGroupBox("Crimson Desert installation")
        grid = QGridLayout(box)
        self.game_edit = QLineEdit()
        self.game_edit.setPlaceholderText("Select the Crimson Desert folder (the one containing the numbered 0000, 0001 ... folders)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_game)
        self.game_info = QLabel("")
        self.game_info.setWordWrap(True)
        grid.addWidget(self.game_edit, 0, 0)
        grid.addWidget(browse, 0, 1)
        grid.addWidget(self.game_info, 1, 0, 1, 2)
        note = QLabel("The installation is only ever read. Nothing inside it is modified, renamed or deleted.")
        note.setStyleSheet("color:#9d948a")
        grid.addWidget(note, 2, 0, 1, 2)
        self.game_edit.editingFinished.connect(self._game_changed)
        layout.addWidget(box)

        ai = QGroupBox("Local AI")
        ai_layout = QVBoxLayout(ai)
        tiers = QHBoxLayout()
        self.tier_group = QButtonGroup(self)
        self.tier_buttons: Dict[str, QRadioButton] = {}
        catalog = self.studio.catalog()
        for key in ("low", "medium", "high", "custom"):
            info = catalog.tiers.get(key)
            label = info.label if info else "Custom"
            text = f"{label}\n{info.recommended_vram} VRAM" if info else "Custom\nany local GGUF"
            button = QRadioButton(text)
            button.setToolTip(info.best_for if info else "Select another compatible local model (GGUF file)")
            self.tier_group.addButton(button)
            self.tier_buttons[key] = button
            tiers.addWidget(button)
            button.toggled.connect(lambda checked, k=key: checked and self._tier_changed(k))
        ai_layout.addLayout(tiers)
        form = QFormLayout()
        self.model_combo = QComboBox()
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        form.addRow("Model:", self.model_combo)
        self.model_info = QLabel("")
        self.model_info.setWordWrap(True)
        form.addRow("", self.model_info)
        self.model_state = QLabel("")
        form.addRow("Status:", self.model_state)
        ai_layout.addLayout(form)
        row = QHBoxLayout()
        self.download_btn = QPushButton("Download Model")
        self.download_btn.clicked.connect(self._download_model)
        self.cancel_dl_btn = QPushButton("Cancel")
        self.cancel_dl_btn.setEnabled(False)
        self.cancel_dl_btn.clicked.connect(lambda: self.dl_worker and self.dl_worker.cancel())
        self.verify_btn = QPushButton("Verify")
        self.verify_btn.clicked.connect(self._verify_model)
        self.test_btn = QPushButton("Load && Test")
        self.test_btn.clicked.connect(self._test_model)
        self.custom_btn = QPushButton("Select GGUF…")
        self.custom_btn.clicked.connect(self._select_custom)
        for w in (self.download_btn, self.cancel_dl_btn, self.verify_btn, self.test_btn, self.custom_btn):
            row.addWidget(w)
        row.addStretch()
        ai_layout.addLayout(row)
        self.dl_progress = QProgressBar()
        self.dl_progress.setVisible(False)
        ai_layout.addWidget(self.dl_progress)
        self.runtime_label = QLabel("")
        self.runtime_label.setStyleSheet("color:#9d948a")
        ai_layout.addWidget(self.runtime_label)
        layout.addWidget(ai)

        actions = QGroupBox("Analyze")
        act = QHBoxLayout(actions)
        self.analyze_btn = QPushButton("Analyze Game")
        self.analyze_btn.setObjectName("primary")
        self.analyze_btn.clicked.connect(lambda: self._start_scan(False))
        self.analyze_ai_btn = QPushButton("Analyze + AI Investigation")
        self.analyze_ai_btn.setObjectName("primary")
        self.analyze_ai_btn.clicked.connect(lambda: self._start_scan(True))
        import_btn = QPushButton("Import community CSV…")
        import_btn.setToolTip("Import a community research table (e.g. a soundbank guide CSV) as separate, labelled evidence")
        import_btn.clicked.connect(self._import_csv)
        reports_btn = QPushButton("Open reports folder")
        reports_btn.clicked.connect(lambda: self._open_folder(self.studio.paths.reports))
        for w in (self.analyze_btn, self.analyze_ai_btn, import_btn, reports_btn):
            act.addWidget(w)
        act.addStretch()
        layout.addWidget(actions)
        layout.addStretch()
        return page

    def _load_settings_into_ui(self) -> None:
        s = self.studio.settings
        if not s.game_path:
            cands = default_steam_candidates()
            if cands:
                s.game_path = str(cands[0])
        self.game_edit.setText(s.game_path)
        self._game_changed()
        tier = s.ai_tier if s.ai_tier in self.tier_buttons else "medium"
        self.tier_buttons[tier].setChecked(True)
        self._tier_changed(tier)
        self._update_runtime_label()

    def _update_runtime_label(self) -> None:
        if self.studio.settings.external_endpoint:
            self.runtime_label.setText(f"Using external endpoint {self.studio.settings.external_endpoint} (advanced setting)")
        elif self.studio.runtime_available():
            self.runtime_label.setText("Bundled llama.cpp runtime found in runtime/.")
        else:
            self.runtime_label.setText("llama.cpp runtime not found in runtime/ - the portable release includes it.")

    def _browse_game(self) -> None:
        start = self.game_edit.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Select Crimson Desert installation folder", start)
        if folder:
            self.game_edit.setText(folder)
            self._game_changed()

    def _game_changed(self) -> None:
        path = self.game_edit.text().strip()
        self.studio.settings.game_path = path
        self.studio.save_settings()
        if not path:
            self.game_info.setText("")
            return
        if not Path(path).is_dir():
            self.game_info.setText("Folder not found.")
            return
        info = self.studio.describe_installation(path)
        if info["valid"]:
            self.game_info.setText(f"Found {len(info['packages'])} archive packages ({', '.join(info['packages'][:12])}"
                                   f"{'…' if len(info['packages']) > 12 else ''}).")
        else:
            self.game_info.setText("This folder does not look like a Crimson Desert installation: " + " ".join(info["notes"]))

    def _tier_changed(self, tier: str) -> None:
        self.studio.settings.ai_tier = tier
        catalog = self.studio.catalog()
        models = catalog.for_tier(tier)
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        default = catalog.default_for_tier(tier)
        for m in models:
            self.model_combo.addItem(m.display_name + ("  (recommended)" if default and m.id == default.id else ""), m.id)
        self.model_combo.blockSignals(False)
        preferred = self.studio.settings.ai_model_id
        index = self.model_combo.findData(preferred)
        if index < 0 and default is not None:
            index = self.model_combo.findData(default.id)
        self.model_combo.setCurrentIndex(max(0, index))
        self.custom_btn.setVisible(tier == "custom")
        self._model_changed()

    def current_model(self) -> Optional[LocalModel]:
        model_id = self.model_combo.currentData()
        return self.studio.catalog().get(model_id) if model_id else None

    def _model_changed(self) -> None:
        model = self.current_model()
        if model is None:
            self.model_info.setText("No model selected. Choose Custom → Select GGUF… to use your own file." if
                                    self.studio.settings.ai_tier == "custom" else "")
            self.model_state.setText("")
            for w in (self.download_btn, self.verify_btn, self.test_btn):
                w.setEnabled(False)
            return
        self.studio.settings.ai_model_id = model.id
        self.studio.save_settings()
        status = self.studio.model_status(model)
        parts = []
        if model.approximate_size_gb:
            parts.append(f"Download ≈ {model.approximate_size_gb:.1f} GB")
        if model.recommended_vram_gb:
            parts.append(f"≈ {model.recommended_vram_gb:.0f} GB VRAM recommended (approximate; CPU offload supported)")
        if model.quantization:
            parts.append(model.quantization)
        if model.license:
            parts.append(f"License: {model.license}")
        if model.repository:
            parts.append(f"Source: huggingface.co/{model.repository}")
        self.model_info.setText(" · ".join(parts))
        label = {"verified": "Downloaded and verified", "available": "Available", "not_downloaded": "Not downloaded",
                 "missing": "Missing (file was removed)", "downloading": "Download incomplete", "load_failed": "Failed to load"}
        text = label.get(str(status["status"]), str(status["status"]))
        if status.get("inference_ok"):
            text += " · inference test passed"
        self.model_state.setText(text)
        self.download_btn.setEnabled(model.source == "huggingface" and not status["present"])
        self.verify_btn.setEnabled(bool(status["present"]))
        self.test_btn.setEnabled(bool(status["present"]))

    def _download_model(self) -> None:
        model = self.current_model()
        if model is None:
            return
        self.dl_progress.setVisible(True)
        self.dl_progress.setValue(0)
        self.download_btn.setEnabled(False)
        self.cancel_dl_btn.setEnabled(True)
        worker = Worker(lambda progress, cancel: self.studio.download(model, progress, cancel))
        worker.progress.connect(self._download_progress)
        worker.finished_ok.connect(lambda r: self._download_done(True, "Download verified (SHA-256 " + r["sha256"][:12] + "…)"))
        worker.failed.connect(lambda e: self._download_done(False, e))
        self.dl_worker = worker
        self._start(worker)

    def _download_progress(self, p: Dict[str, Any]) -> None:
        total = p.get("total") or 0
        done = p.get("downloaded") or 0
        if total:
            self.dl_progress.setMaximum(1000)
            self.dl_progress.setValue(int(done * 1000 / total))
        else:
            self.dl_progress.setMaximum(0)
        speed = p.get("speed_bps") or 0
        self.dl_progress.setFormat(f"{p.get('phase')}: {done / 1e9:.2f} / {total / 1e9:.2f} GB  ({speed / 1e6:.1f} MB/s)")

    def _download_done(self, ok: bool, message: str) -> None:
        self.cancel_dl_btn.setEnabled(False)
        self.dl_progress.setVisible(not ok)
        if not ok:
            self.dl_progress.setFormat("Stopped")
            QMessageBox.warning(self, "Model download", message.split("\n\n")[0])
        else:
            self.statusBar().showMessage(message)
        self._model_changed()

    def _verify_model(self) -> None:
        model = self.current_model()
        if model is None:
            return
        worker = Worker(lambda progress, cancel: self.studio.verify(model))
        worker.finished_ok.connect(lambda r: (QMessageBox.information(
            self, "Verify", f"GGUF file OK.\nSHA-256: {r['sha256']}\nChecked against source hash: {r['hash_checked_against_source']}"),
            self._model_changed()))
        worker.failed.connect(lambda e: QMessageBox.warning(self, "Verify", e.split("\n\n")[0]))
        self._start(worker)

    def _test_model(self) -> None:
        model = self.current_model()
        if model is None:
            return
        self.test_btn.setEnabled(False)
        self.model_state.setText("Loading model… (first load can take a while)")
        worker = Worker(lambda progress, cancel: self.studio.load_and_test(model))
        worker.finished_ok.connect(self._test_done)
        worker.failed.connect(lambda e: (QMessageBox.warning(self, "Load model", e.split("\n\n")[0]), self._model_changed()))
        self._start(worker)

    def _test_done(self, result: Dict[str, Any]) -> None:
        self._model_changed()
        QMessageBox.information(self, "Local AI", f"Inference {'works' if result['ok'] else 'FAILED'} "
                                f"({result['seconds']} s).\nReply: {result['reply']}")

    def _select_custom(self) -> None:
        file, _ = QFileDialog.getOpenFileName(self, "Select a GGUF model", str(self.studio.paths.models), "GGUF models (*.gguf)")
        if not file:
            return
        try:
            model = self.studio.register_custom(file)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Custom model", str(exc))
            return
        if not self.studio.paths.is_inside(Path(file)):
            QMessageBox.information(self, "Custom model", "Tip: copy the model into the models/ folder so it moves with the "
                                    "application folder. It will still work from its current location.")
        self.studio.settings.ai_model_id = model.id
        self._tier_changed("custom")

    def _import_csv(self) -> None:
        file, _ = QFileDialog.getOpenFileName(self, "Import community research CSV", str(Path.home()), "CSV (*.csv)")
        if not file:
            return
        try:
            result = self.studio.import_community_csv(file)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Import", str(exc))
            return
        QMessageBox.information(self, "Import", f"Imported {result.get('imported', 0)} rows as community-provided evidence."
                                " Re-run the analysis to use it.")

    def _open_folder(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ================================================================ SCAN
    def _build_scan(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.scan_phase = QLabel("Idle")
        self.scan_phase.setStyleSheet("font-size:14pt; color:#e0645a")
        layout.addWidget(self.scan_phase)
        self.scan_bar = QProgressBar()
        layout.addWidget(self.scan_bar)
        grid = QGridLayout()
        self.scan_labels: Dict[str, QLabel] = {}
        fields = [("files_discovered", "Files discovered"), ("archive_entries", "Archive entries"),
                  ("candidates_total", "Audio candidates"), ("files_analyzed", "Files analyzed"), ("files_cached", "Files cached"),
                  ("bnks_parsed", "BNKs parsed"), ("hirc_objects", "HIRC objects"), ("wems_discovered", "WEMs discovered"),
                  ("music_candidates", "Music candidates"), ("unknown_structures", "Unknown structures"),
                  ("ai_findings", "AI findings"), ("errors", "Errors"), ("elapsed", "Elapsed (s)")]
        for i, (key, label) in enumerate(fields):
            grid.addWidget(QLabel(label + ":"), i // 3, (i % 3) * 2)
            value = QLabel("0")
            value.setStyleSheet("font-weight:bold")
            self.scan_labels[key] = value
            grid.addWidget(value, i // 3, (i % 3) * 2 + 1)
        layout.addLayout(grid)
        self.scan_current = QLabel("")
        self.scan_current.setStyleSheet("color:#9d948a")
        layout.addWidget(self.scan_current)
        row = QHBoxLayout()
        self.stop_scan_btn = QPushButton("Stop scan")
        self.stop_scan_btn.setEnabled(False)
        self.stop_scan_btn.clicked.connect(lambda: self.scan_worker and self.scan_worker.cancel())
        row.addWidget(self.stop_scan_btn)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(QLabel("Errors / warnings:"))
        self.scan_errors = QPlainTextEdit()
        self.scan_errors.setReadOnly(True)
        self.scan_errors.setFont(_mono())
        layout.addWidget(self.scan_errors)
        return page

    def _start_scan(self, with_ai: bool) -> None:
        path = self.game_edit.text().strip()
        if not path or not Path(path).is_dir():
            QMessageBox.warning(self, "Analyze", "Select the Crimson Desert installation folder first.")
            return
        if with_ai and self.current_model() is None:
            QMessageBox.warning(self, "Analyze + AI", "Select and download a local AI model first.")
            return
        if with_ai and not self.studio.model_status(self.current_model())["present"]:
            QMessageBox.warning(self, "Analyze + AI", "The selected model is not downloaded yet.")
            return
        self.chain_ai_after_scan = with_ai
        self.analyze_btn.setEnabled(False)
        self.analyze_ai_btn.setEnabled(False)
        self.stop_scan_btn.setEnabled(True)
        self.scan_errors.clear()
        self.scan_bar.setMaximum(0)
        self.tabs.setCurrentIndex(1)
        worker = Worker(lambda progress, cancel: self.studio.run_scan(path, progress, cancel))
        worker.progress.connect(self._scan_progress)
        worker.finished_ok.connect(self._scan_done)
        worker.failed.connect(self._scan_failed)
        self.scan_worker = worker
        self._start(worker)

    def _scan_progress(self, stats: Dict[str, Any]) -> None:
        self.scan_phase.setText(str(stats.get("phase", "")).capitalize() + "…")
        for key, label in self.scan_labels.items():
            value = stats.get(key, 0)
            label.setText(f"{value:,}" if isinstance(value, int) else str(value))
        total = stats.get("candidates_total") or 0
        done = (stats.get("files_analyzed") or 0) + (stats.get("files_cached") or 0)
        if stats.get("phase") == "analyzing audio assets" and total:
            self.scan_bar.setMaximum(total)
            self.scan_bar.setValue(min(done, total))
            self.scan_labels["files_analyzed"].setText(f"{stats.get('files_analyzed', 0):,}  ({done:,} / {total:,})")
        elif stats.get("phase") in ("completed", "cancelled", "failed"):
            self.scan_bar.setMaximum(1)
            self.scan_bar.setValue(1)
        else:
            self.scan_bar.setMaximum(0)
        self.scan_current.setText(str(stats.get("current", ""))[:200])
        errors = stats.get("error_messages") or []
        if errors and len(errors) != self.scan_errors.blockCount() - 1:
            self.scan_errors.setPlainText("\n".join(errors))

    def _scan_done(self, result) -> None:
        self.stop_scan_btn.setEnabled(False)
        self.analyze_btn.setEnabled(True)
        self.analyze_ai_btn.setEnabled(True)
        self.scan_phase.setText(f"Scan {result.status}.  Report: {result.report_dir}" if result.report_dir else f"Scan {result.status}.")
        self.refresh_results()
        if result.status == "completed" and self.chain_ai_after_scan:
            self.chain_ai_after_scan = False
            self._start_ai()

    def _scan_failed(self, error: str) -> None:
        self.stop_scan_btn.setEnabled(False)
        self.analyze_btn.setEnabled(True)
        self.analyze_ai_btn.setEnabled(True)
        self.scan_phase.setText("Scan failed")
        self.scan_errors.appendPlainText(error)

    # ============================================================= RESULTS
    def _build_results(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search media id, name, bank, event…")
        self.search_edit.returnPressed.connect(self.refresh_results)
        self.role_combo = QComboBox()
        for label, value in (("Music (all confidence levels)", "music*"), ("Music (high)", "music"), ("Likely music", "likely_music"),
                             ("Possible music", "possible_music"), ("Ambience", "ambience"), ("SFX", "sfx"), ("Voice", "voice"),
                             ("Unknown role", "unknown"), ("All media", "")):
            self.role_combo.addItem(label, value)
        self.role_combo.currentIndexChanged.connect(self.refresh_results)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_results)
        self.overview_label = QLabel("")
        top.addWidget(self.search_edit, 3)
        top.addWidget(self.role_combo, 1)
        top.addWidget(refresh)
        layout.addLayout(top)
        layout.addWidget(self.overview_label)
        self.result_tabs = QTabWidget()
        layout.addWidget(self.result_tabs)

        self.media_table = _make_table()
        self.media_detail = QPlainTextEdit(readOnly=True)
        self.media_detail.setFont(_mono())
        self.media_table.itemSelectionChanged.connect(self._media_selected)
        self.result_tabs.addTab(self._split(self.media_table, self.media_detail), "Media / music assets")

        self.bank_table = _make_table()
        self.bank_objects = _make_table()
        self.object_detail = QPlainTextEdit(readOnly=True)
        self.object_detail.setFont(_mono())
        self.bank_table.itemSelectionChanged.connect(self._bank_selected)
        self.bank_objects.itemSelectionChanged.connect(self._object_selected)
        right = QSplitter(Qt.Vertical)
        right.addWidget(self.bank_objects)
        right.addWidget(self.object_detail)
        self.result_tabs.addTab(self._split(self.bank_table, right), "Soundbanks")

        rel = QWidget()
        rel_layout = QVBoxLayout(rel)
        rel_row = QHBoxLayout()
        self.rel_edit = QLineEdit()
        self.rel_edit.setPlaceholderText("Object, event, bank or media id")
        self.rel_edit.returnPressed.connect(self._show_relationships)
        rel_btn = QPushButton("Inspect")
        rel_btn.clicked.connect(self._show_relationships)
        rel_row.addWidget(self.rel_edit)
        rel_row.addWidget(rel_btn)
        rel_layout.addLayout(rel_row)
        self.rel_view = QPlainTextEdit(readOnly=True)
        self.rel_view.setFont(_mono())
        rel_layout.addWidget(self.rel_view)
        self.result_tabs.addTab(rel, "Relationships")

        self.unknown_table = _make_table()
        self.unknown_detail = QPlainTextEdit(readOnly=True)
        self.unknown_detail.setFont(_mono())
        self.unknown_table.itemSelectionChanged.connect(self._unknown_selected)
        self.result_tabs.addTab(self._split(self.unknown_table, self.unknown_detail), "Unknown structures")

        self.finding_table = _make_table()
        self.finding_detail = QPlainTextEdit(readOnly=True)
        self.finding_detail.setFont(_mono())
        self.finding_table.itemSelectionChanged.connect(self._finding_selected)
        self.result_tabs.addTab(self._split(self.finding_table, self.finding_detail), "Findings")
        return page

    @staticmethod
    def _split(left: QWidget, right: QWidget) -> QSplitter:
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([760, 520])
        return split

    def refresh_results(self) -> None:
        inst = self.studio.current_installation_id()
        if inst is None:
            self.overview_label.setText("No scan yet. Select the installation on the Home tab and press Analyze Game.")
            return
        ov = queries.overview(self.studio.db, inst)
        roles = ov.get("roles", {})
        self.overview_label.setText(
            f"Banks: {ov['banks']:,} · HIRC objects: {ov['hirc_objects']:,} · Media IDs: {ov['media_ids']:,} · "
            f"Music: {roles.get('music', 0):,} / likely {roles.get('likely_music', 0):,} / possible {roles.get('possible_music', 0):,} · "
            f"Open unknowns: {ov['unknown_structures']:,} · Findings: {sum(ov['findings'].values()) if ov['findings'] else 0}")
        rows = queries.media_summary_rows(self.studio.db, inst, self.search_edit.text(), self.role_combo.currentData() or "", 5000)
        _fill_table(self.media_table, ["Media ID", "Name", "Role", "Score", "Confidence", "Duration (s)", "Codec", "Ch", "Owner", "Banks"],
                    [[r["source_id"], r["name"], r["role"], r["score"], r["confidence"], r["duration_s"], r["codec"], r["channels"],
                      r["owner_type"], r["banks"]] for r in rows], [r["source_id"] for r in rows])
        banks = queries.soundbanks(self.studio.db, inst)
        _fill_table(self.bank_table, ["Path", "Name", "Bank ID", "Version", "Objects", "Embedded", "Parse status"],
                    [[b["path"], b["name"], b["bank_id"], b["version"], b["object_count"], b["embedded_media"],
                      ", ".join(f"{k}:{v}" for k, v in b["parse_status"].items())] for b in banks], [b["asset_id"] for b in banks])
        unknown = queries.unknown_structures(self.studio.db, inst)
        _fill_table(self.unknown_table, ["Status", "Category", "Signature", "Count", "Description"],
                    [[u["status"], u["category"], u["signature"], u["occurrences"], u["description"]] for u in unknown],
                    [u["signature"] for u in unknown])
        findings = self.studio.knowledge.list(limit=2000)
        _fill_table(self.finding_table, ["Status", "Title", "Category", "Subject", "By", "Updated"],
                    [[f["status"], f["title"], f["category"], f"{f['subject_type']}:{f['subject_key']}", f["created_by"], f["updated_at"]]
                     for f in findings], [f["uid"] for f in findings])

    def _selected_key(self, table: QTableWidget):
        items = table.selectedItems()
        if not items:
            return None
        return table.item(items[0].row(), 0).data(Qt.UserRole)

    def _media_selected(self) -> None:
        key = self._selected_key(self.media_table)
        inst = self.studio.current_installation_id()
        if key is None or inst is None:
            return
        self.media_detail.setPlainText(json.dumps(queries.media_record(self.studio.db, inst, int(key)), indent=2, ensure_ascii=False, default=str))

    def _bank_selected(self) -> None:
        key = self._selected_key(self.bank_table)
        inst = self.studio.current_installation_id()
        if key is None or inst is None:
            return
        contents = queries.bank_contents(self.studio.db, inst, int(key), limit=5000) or {"objects": []}
        objs = contents["objects"]
        _fill_table(self.bank_objects, ["Object ID", "Type", "Name", "Parse status", "Size", "Error"],
                    [[o["object_id"], o["type_name"], o.get("name"), o["parse_status"], o["size"], o["error"]] for o in objs],
                    [o["object_id"] for o in objs])
        self.object_detail.setPlainText(json.dumps({k: v for k, v in contents.items() if k != "objects"}, indent=2, default=str))

    def _object_selected(self) -> None:
        key = self._selected_key(self.bank_objects)
        inst = self.studio.current_installation_id()
        if key is None or inst is None:
            return
        data = {"object": queries.get_object(self.studio.db, inst, int(key)),
                "referenced_by": queries.find_referencing(self.studio.db, inst, int(key))}
        self.object_detail.setPlainText(json.dumps(data, indent=2, ensure_ascii=False, default=str))

    def _show_relationships(self) -> None:
        inst = self.studio.current_installation_id()
        text = self.rel_edit.text().strip()
        if inst is None or not text.isdigit():
            return
        from cstudio.analyzer.relationships import object_neighbourhood

        value = int(text)
        data = {"anywhere": queries.find_references_anywhere(self.studio.db, inst, value),
                "graph": object_neighbourhood(self.studio.db, inst, value, 2)}
        if data["anywhere"]["as_media"]:
            data["media"] = queries.media_record(self.studio.db, inst, value)
        self.rel_view.setPlainText(json.dumps(data, indent=2, ensure_ascii=False, default=str))

    def _unknown_selected(self) -> None:
        key = self._selected_key(self.unknown_table)
        inst = self.studio.current_installation_id()
        if key is None or inst is None:
            return
        for u in queries.unknown_structures(self.studio.db, inst):
            if u["signature"] == key:
                self.unknown_detail.setPlainText(json.dumps(u, indent=2, ensure_ascii=False, default=str))
                break

    def _finding_selected(self) -> None:
        key = self._selected_key(self.finding_table)
        if key is None:
            return
        self.finding_detail.setPlainText(json.dumps(self.studio.knowledge.get(key), indent=2, ensure_ascii=False, default=str))

    # ================================================================= AI
    def _build_ai(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        box = QGroupBox("AI Investigation")
        form = QGridLayout(box)
        self.ai_labels: Dict[str, QLabel] = {}
        for i, (key, label) in enumerate([("model", "Model"), ("status", "Status"), ("current_task", "Current task"),
                                          ("steps", "Steps completed"), ("verified", "Verified findings"),
                                          ("probable", "Probable findings"), ("hypothesis", "Hypotheses"),
                                          ("unknowns_remaining", "Unknowns remaining")]):
            form.addWidget(QLabel(label + ":"), i, 0)
            value = QLabel("-")
            value.setWordWrap(True)
            value.setStyleSheet("font-weight:bold")
            self.ai_labels[key] = value
            form.addWidget(value, i, 1)
        layout.addWidget(box)
        row = QHBoxLayout()
        self.ai_start_btn = QPushButton("Start investigation")
        self.ai_start_btn.setObjectName("primary")
        self.ai_start_btn.clicked.connect(self._start_ai)
        self.ai_pause_btn = QPushButton("Pause")
        self.ai_pause_btn.setEnabled(False)
        self.ai_pause_btn.clicked.connect(self._toggle_pause)
        self.ai_stop_btn = QPushButton("Stop")
        self.ai_stop_btn.setEnabled(False)
        self.ai_stop_btn.clicked.connect(self._stop_ai)
        for w in (self.ai_start_btn, self.ai_pause_btn, self.ai_stop_btn):
            row.addWidget(w)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(QLabel("Investigation log (every tool call is stored in the database):"))
        self.ai_log = QPlainTextEdit(readOnly=True)
        self.ai_log.setFont(_mono())
        self.ai_log.setMaximumBlockCount(5000)
        layout.addWidget(self.ai_log)
        self._last_logged_step = 0
        return page

    def _start_ai(self) -> None:
        model = self.current_model()
        if model is None or not self.studio.model_status(model)["present"]:
            QMessageBox.warning(self, "AI Investigation", "Select a downloaded model on the Home tab first.")
            return
        if self.studio.current_installation_id() is None:
            QMessageBox.warning(self, "AI Investigation", "Analyze the game first.")
            return
        self.tabs.setCurrentIndex(3)
        self.pause_event.clear()
        self.ai_start_btn.setEnabled(False)
        self.ai_pause_btn.setEnabled(True)
        self.ai_stop_btn.setEnabled(True)
        self.ai_labels["model"].setText(model.display_name)
        self.ai_labels["status"].setText("Loading model…")
        self.ai_log.appendPlainText(f"=== Investigation with {model.display_name} ===")
        self._last_logged_step = 0
        worker = Worker(lambda progress, cancel: self.studio.run_investigation(model, progress, stop=cancel, pause=self.pause_event))
        worker.progress.connect(self._ai_progress)
        worker.finished_ok.connect(self._ai_done)
        worker.failed.connect(self._ai_failed)
        self.ai_worker = worker
        self._start(worker)

    def _ai_progress(self, p: Dict[str, Any]) -> None:
        counts = p.get("counts") or {}
        self.ai_labels["status"].setText(str(p.get("status", "")).capitalize() + (f" — {p['message']}" if p.get("message") else ""))
        self.ai_labels["current_task"].setText(f"[{p.get('task_index')}/{p.get('task_total')}] {p.get('current_task', '')}")
        invalid = p.get("invalid_replies", 0)
        self.ai_labels["steps"].setText(f"{p.get('steps', 0)}" + (f"  ({invalid} unreadable replies)" if invalid else ""))
        for key in ("verified", "probable", "hypothesis"):
            self.ai_labels[key].setText(str(counts.get(key, 0)))
        self.ai_labels["unknowns_remaining"].setText(str(p.get("unknowns_remaining", 0)))
        step = p.get("steps", 0)
        if step and step != self._last_logged_step:
            self._last_logged_step = step
            self.ai_log.appendPlainText(f"[{step}] {p.get('last_thought', '')}\n    → {p.get('last_tool', '')}\n    ← {p.get('last_result', '')[:300]}")

    def _toggle_pause(self) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.ai_pause_btn.setText("Pause")
        else:
            self.pause_event.set()
            self.ai_pause_btn.setText("Resume")

    def _stop_ai(self) -> None:
        if self.ai_worker:
            self.pause_event.clear()
            self.ai_worker.cancel()
            self.ai_labels["status"].setText("Stopping after the current step…")

    def _ai_done(self, state) -> None:
        self._ai_reset_buttons()
        self.ai_labels["status"].setText(f"{state.status.capitalize()} — {state.steps} steps" + (f" — {state.message}" if state.message else ""))
        self.refresh_results()

    def _ai_failed(self, error: str) -> None:
        self._ai_reset_buttons()
        self.ai_labels["status"].setText("Failed")
        self.ai_log.appendPlainText(error)

    def _ai_reset_buttons(self) -> None:
        self.ai_start_btn.setEnabled(True)
        self.ai_pause_btn.setEnabled(False)
        self.ai_pause_btn.setText("Pause")
        self.ai_stop_btn.setEnabled(False)

    # =============================================================== misc
    def _start(self, worker: Worker) -> None:
        self.workers.append(worker)
        worker.finished.connect(lambda w=worker: self.workers.remove(w) if w in self.workers else None)
        worker.start()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        for worker in list(self.workers):
            worker.cancel()
        self.pause_event.clear()
        for worker in list(self.workers):
            worker.wait(15000)
        self.studio.close()
        super().closeEvent(event)
