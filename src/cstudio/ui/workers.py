"""Background workers so the GUI stays responsive."""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    """Runs ``fn(progress=..., cancel=...)`` on a thread and relays progress as dicts."""

    progress = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[..., Any], *, with_cancel: bool = True, parent=None, **kwargs) -> None:
        super().__init__(parent)
        self.fn = fn
        self.kwargs = kwargs
        self.cancel_event = threading.Event()
        self.with_cancel = with_cancel

    def _relay(self, value: Any) -> None:
        payload = value.to_dict() if hasattr(value, "to_dict") else (value.__dict__.copy() if hasattr(value, "__dict__") else value)
        self.progress.emit(payload)

    def run(self) -> None:  # noqa: D401 - Qt override
        try:
            kwargs = dict(self.kwargs)
            kwargs.setdefault("progress", self._relay)
            if self.with_cancel:
                kwargs.setdefault("cancel", self.cancel_event)
            result = self.fn(**kwargs)
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=6)}")

    def cancel(self) -> None:
        self.cancel_event.set()
