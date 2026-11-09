"""Ringpuffer fuer den Live-Log der WebUI.

Haengt sich als Handler ins normale Logging und haelt die letzten Zeilen
im Speicher. Die Oberflaeche holt sie ueber /api/log ab und fragt mit
`since` nur das Neue nach - damit braucht es keinen WebSocket und der
Autoscroll bekommt trotzdem alles mit.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional

MAX_LINES = 2000


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = MAX_LINES):
        super().__init__()
        self.records: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:            # pragma: no cover - Logging darf nie werfen
            return
        with self._lock:
            self._seq += 1
            self.records.append({
                "seq": self._seq,
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "text": text,
            })

    def since(self, seq: int = 0, level: Optional[str] = None,
              limit: int = 500) -> list[dict]:
        """Alles neuer als `seq`, optional ab einem Mindest-Level."""
        threshold = getattr(logging, (level or "").upper(), 0)
        with self._lock:
            rows = [r for r in self.records if r["seq"] > seq]
        if threshold:
            rows = [r for r in rows
                    if getattr(logging, r["level"], 0) >= threshold]
        return rows[-limit:]

    @property
    def last_seq(self) -> int:
        return self._seq


_buffer: Optional[LogBuffer] = None


def install(level: int = logging.INFO) -> LogBuffer:
    """Haengt den Puffer einmalig ins Root-Logging."""
    global _buffer
    if _buffer is None:
        _buffer = LogBuffer()
        _buffer.setLevel(level)
        logging.getLogger().addHandler(_buffer)
    return _buffer


def get() -> Optional[LogBuffer]:
    return _buffer
