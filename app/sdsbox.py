"""Postfach fuer SDS-Textnachrichten.

Die Brücke sieht ohnehin jede SDS, die ueber ihre ISSI laeuft - bisher hat
sie davon nur die Kommandos an die Control-ISSI beachtet und den Rest
weggeworfen. Hier landet jetzt alles, in beide Richtungen, damit die WebUI
einen Reiter dafuer hat.

Bewusst klein gehalten: ein Ringpuffer wie beim Log, dazu eine Datei
daneben, damit ein Neustart das Postfach nicht leert. Kein Suchindex,
keine Threads - bei ein paar hundert Kurznachrichten waere das Unfug.

`since` arbeitet wie beim Log: die Oberflaeche merkt sich die letzte
Nummer und holt nur das Neue nach.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

log = logging.getLogger("sds")

MAX_MESSAGES = 300
TEXT_MAX = 1000          # laenger nimmt keine SDS, und ins Postfach auch nicht
RAW_MAX = 64             # so viel Rohbytes reichen zur Fehlersuche

RX = "rx"
TX = "tx"


class SdsBox:
    """Die letzten Kurznachrichten, empfangen wie gesendet."""

    def __init__(self, path: Optional[Path] = None,
                 capacity: int = MAX_MESSAGES):
        self.path = Path(path) if path else None
        self.messages: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._load()

    # ------------------------------------------------------------- Ablage

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Postfach %s nicht lesbar (%s) - faengt leer an",
                        self.path, exc)
            return
        for eintrag in raw.get("messages", []):
            if isinstance(eintrag, dict) and "text" in eintrag:
                self.messages.append(eintrag)
        self._seq = max([int(raw.get("last", 0))]
                        + [int(m.get("seq", 0)) for m in self.messages])

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".neu")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(
                {"last": self._seq, "messages": list(self.messages)},
                ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Postfach nicht schreibbar: %s", exc)

    # ------------------------------------------------------------ Eintrag

    def add(self, direction: str, peer: Optional[int], text: str,
            kind: str = "text", ok: bool = True,
            error: str = "", scheme: str = "", raw: bytes = b"") -> dict:
        """Eine Nachricht ablegen. `peer` ist die ISSI der Gegenstelle.

        `scheme` und `raw` sind die Diagnose: welche Kodierung das Geraet
        deklariert hat und wie die PDU roh aussah. Ohne das laesst sich
        Zeichensalat nicht aufklaeren.
        """
        with self._lock:
            self._seq += 1
            eintrag = {
                "seq": self._seq,
                "time": time.time(),
                "direction": RX if direction == RX else TX,
                "peer": int(peer) if peer else None,
                "text": text[:TEXT_MAX],
                "kind": kind,
                "ok": bool(ok),
            }
            if error:
                eintrag["error"] = error
            if scheme:
                eintrag["scheme"] = scheme
            if raw:
                eintrag["raw"] = raw[:RAW_MAX].hex(" ")
            self.messages.append(eintrag)
            self._save()
        return eintrag

    # ------------------------------------------------------------ Abfrage

    def since(self, seq: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            return [m for m in self.messages if m["seq"] > seq][-limit:]

    def all(self, limit: int = MAX_MESSAGES) -> list[dict]:
        with self._lock:
            return list(self.messages)[-limit:]

    def clear(self) -> None:
        with self._lock:
            self.messages.clear()
            self._save()

    @property
    def last_seq(self) -> int:
        return self._seq

    def counts(self) -> dict:
        with self._lock:
            return {
                "total": len(self.messages),
                "rx": sum(1 for m in self.messages if m["direction"] == RX),
                "tx": sum(1 for m in self.messages if m["direction"] == TX),
            }
