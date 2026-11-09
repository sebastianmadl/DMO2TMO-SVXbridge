"""Rufzeichenliste: ISSI -> Rufzeichen bzw. eigener Name.

Zwei Quellen, klare Rangfolge:

1. **Eigene Eintraege** aus der WebUI. Damit bekommt die Bruecke selbst
   (2329999) einen Namen, den keine Datenbank kennt. Schlaegt alles andere.
2. **Heruntergeladene Liste** von radioid.net (`dmrid.dat`), Format
   `ID;RUFZEICHEN;`. Rund 324000 Zeilen, gut 5 MB.

Warum SQLite und nicht ein dict: 324000 Eintraege im Speicher kosten auf
einem kleinen VPS spuerbar RAM, gebraucht wird aber pro Ruf genau eine
Abfrage. Mit Index ist das eine Mikrosekunde, und der Speicher bleibt frei.
sqlite3 ist Standardbibliothek, es kommt also keine Abhaengigkeit dazu.

Die Datei liegt neben der Config und ueberlebt Neustarts. Fehlt sie, laeuft
alles weiter, nur ohne Namen.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("idlist")

DEFAULT_URL = "https://radioid.net/static/dmrid.dat"
# Weniger Zeilen heisst: der Download ist schiefgegangen (Fehlerseite,
# abgebrochene Verbindung). Dann lieber die alte Liste behalten.
MIN_ZEILEN = 1000
NAME_MAX = 60


class IdList:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self._setup()

    # ------------------------------------------------------------- Ablage

    def _conn(self) -> Optional[sqlite3.Connection]:
        if self.path is None:
            return None
        try:
            conn = sqlite3.connect(str(self.path), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        except sqlite3.Error as exc:
            log.warning("Rufzeichenliste %s nicht nutzbar: %s", self.path, exc)
            return None

    def _setup(self) -> None:
        conn = self._conn()
        if conn is None:
            return
        try:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS ids ("
                             "id INTEGER PRIMARY KEY, callsign TEXT)")
                conn.execute("CREATE TABLE IF NOT EXISTS manual ("
                             "id INTEGER PRIMARY KEY, name TEXT)")
                conn.execute("CREATE TABLE IF NOT EXISTS meta ("
                             "key TEXT PRIMARY KEY, value TEXT)")
        except sqlite3.Error as exc:
            log.warning("Rufzeichenliste nicht anlegbar: %s", exc)
        finally:
            conn.close()

    def _meta(self, key: str, default: str = "") -> str:
        conn = self._conn()
        if conn is None:
            return default
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?",
                               (key,)).fetchone()
            return row[0] if row else default
        except sqlite3.Error:
            return default
        finally:
            conn.close()

    def _set_meta(self, key: str, value: str) -> None:
        conn = self._conn()
        if conn is None:
            return
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                             (key, value))
        except sqlite3.Error as exc:
            log.warning("Rufzeichenliste: %s nicht speicherbar (%s)", key, exc)
        finally:
            conn.close()

    # ------------------------------------------------------------- Abfrage

    def lookup(self, issi: Optional[int]) -> str:
        """Eigener Name, sonst Rufzeichen aus dem Download."""
        if not issi:
            return ""
        conn = self._conn()
        if conn is None:
            return ""
        try:
            row = conn.execute("SELECT name FROM manual WHERE id=?",
                               (int(issi),)).fetchone()
            if row and row[0]:
                return row[0]
            row = conn.execute("SELECT callsign FROM ids WHERE id=?",
                               (int(issi),)).fetchone()
            return row[0] if row else ""
        except (sqlite3.Error, ValueError):
            return ""
        finally:
            conn.close()

    def manual_name(self, issi: Optional[int]) -> str:
        """Nur die eigenen Eintraege - die duerfen alles ueberstimmen."""
        if not issi:
            return ""
        conn = self._conn()
        if conn is None:
            return ""
        try:
            row = conn.execute("SELECT name FROM manual WHERE id=?",
                               (int(issi),)).fetchone()
            return row[0] if row else ""
        except (sqlite3.Error, ValueError):
            return ""
        finally:
            conn.close()

    # -------------------------------------------------------- eigene Liste

    def set_manual(self, issi: int, name: str) -> None:
        try:
            issi = int(issi)
        except (TypeError, ValueError):
            raise ValueError("ISSI muss eine Zahl sein")
        name = (name or "").strip()[:NAME_MAX]
        if issi <= 0:
            raise ValueError("ISSI fehlt")
        if not name:
            raise ValueError("Name ist leer")
        conn = self._conn()
        if conn is None:
            raise RuntimeError("Rufzeichenliste nicht beschreibbar")
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO manual VALUES (?, ?)",
                             (issi, name))
        finally:
            conn.close()

    def delete_manual(self, issi: int) -> bool:
        conn = self._conn()
        if conn is None:
            return False
        try:
            with conn:
                cur = conn.execute("DELETE FROM manual WHERE id=?",
                                   (int(issi),))
            return cur.rowcount > 0
        except (sqlite3.Error, ValueError):
            return False
        finally:
            conn.close()

    def manual_all(self) -> list[dict]:
        conn = self._conn()
        if conn is None:
            return []
        try:
            return [{"issi": row[0], "name": row[1]} for row in
                    conn.execute("SELECT id, name FROM manual ORDER BY id")]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ------------------------------------------------------- Aktualisieren

    def update_from_text(self, text: str) -> int:
        """`ID;RUFZEICHEN;` zeilenweise einlesen, Anzahl zurueckgeben.

        Erst zaehlen, dann tauschen: sieht der Download nach Fehlerseite
        aus, bleibt die alte Liste stehen.
        """
        zeilen = []
        for zeile in text.splitlines():
            teile = zeile.strip().split(";")
            if len(teile) < 2 or not teile[0].isdigit():
                continue
            call = teile[1].strip().upper()
            if call:
                zeilen.append((int(teile[0]), call))

        if len(zeilen) < MIN_ZEILEN:
            raise ValueError(
                f"nur {len(zeilen)} brauchbare Zeilen - das sieht nicht nach "
                f"der Rufzeichenliste aus, die alte bleibt stehen")

        conn = self._conn()
        if conn is None:
            raise RuntimeError("Rufzeichenliste nicht beschreibbar")
        try:
            with conn:
                conn.execute("DELETE FROM ids")
                conn.executemany("INSERT OR REPLACE INTO ids VALUES (?, ?)",
                                 zeilen)
        finally:
            conn.close()
        self._set_meta("updated", str(int(time.time())))
        log.info("Rufzeichenliste aktualisiert: %d Eintraege", len(zeilen))
        return len(zeilen)

    # ---------------------------------------------------------------- Info

    @property
    def url(self) -> str:
        return self._meta("url", DEFAULT_URL) or DEFAULT_URL

    @url.setter
    def url(self, wert: str) -> None:
        self._set_meta("url", (wert or "").strip() or DEFAULT_URL)

    def count(self) -> int:
        conn = self._conn()
        if conn is None:
            return 0
        try:
            return conn.execute("SELECT COUNT(*) FROM ids").fetchone()[0]
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def status(self) -> dict:
        updated = self._meta("updated", "")
        return {
            "count": self.count(),
            "updated": int(updated) if updated.isdigit() else 0,
            "url": self.url,
            "manual": self.manual_all(),
        }
