"""Quell-ISSI aus dem Lastheard des OE-Dashboards.

Die Gateways im TETRA-AUSTRIA-Verbund (`tetra_link_gw`) schicken die ISSI
ihres Sprechers nicht ueber svxlink, sondern nur per UDP an den
`tetra_fw_server` (Port 5220). Der zeigt sie im Lastheard des Dashboards,
reicht sie aber an niemanden weiter. Ueber den Reflektor kommt bei uns nur
das Node-Rufzeichen an (z.B. `OE1XCS-T`).

Darum der Umweg: wenn ein Gateway zu sprechen beginnt, liest die Bruecke
das Dashboard, sucht die juengste VOICE-Zeile dieses Gateways und nimmt
deren ISSI als Quell-ISSI des Gruppenrufs, deren Rufzeichen als Mnemonic.

Der Parser verlaesst sich nicht auf Klassen oder Tabellen-Reihenfolge: er
sucht eine Kopfzeile mit den Spalten Gateway / Time / Call / Type / ID und
liest alle Zeilen darunter danach.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional

log = logging.getLogger("oe_dashboard")

CACHE_SECONDS = 2.0          # mehrere Rufbeginne kurz hintereinander: ein Abruf
FETCH_TIMEOUT = 1.5
MAX_BYTES = 2 * 1024 * 1024
ZEITFORMATE = ("%d.%m.%Y %H:%M:%S", "%d.%m.%y %H:%M:%S", "%d.%m.%Y %H:%M")


@dataclass
class Eintrag:
    gateway: str
    call: str
    issi: int
    zeit: Optional[float]      # epoch, lokale Zeit wie am Dashboard
    zeit_text: str


class _Zeilen(HTMLParser):
    """Sammelt jede <tr> als Liste ihrer Zellentexte."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.zeilen: list[list[str]] = []
        self._zeile: Optional[list[str]] = None
        self._zelle: Optional[list[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._zeile_zu()
            self._zeile = []
        elif tag in ("td", "th"):
            self._zelle_zu()
            if self._zeile is None:
                self._zeile = []
            self._zelle = []
        elif tag == "br" and self._zelle is not None:
            self._zelle.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._zelle_zu()
        elif tag in ("tr", "table"):
            self._zeile_zu()

    def handle_data(self, data):
        if self._zelle is not None:
            self._zelle.append(data)

    def _zelle_zu(self):
        if self._zelle is not None and self._zeile is not None:
            self._zeile.append(" ".join("".join(self._zelle).split()))
        self._zelle = None

    def _zeile_zu(self):
        self._zelle_zu()
        if self._zeile:
            self.zeilen.append(self._zeile)
        self._zeile = None

    def close(self):
        super().close()
        self._zeile_zu()


def _spalten(kopf: list[str]) -> Optional[dict[str, int]]:
    """Erkennt die Lastheard-Kopfzeile und liefert die Spaltennummern."""
    namen = [c.strip().lower() for c in kopf]
    def finde(*praefixe):
        for i, n in enumerate(namen):
            if any(n == p or n.startswith(p + " ") for p in praefixe):
                return i
        return None
    idx = {
        "gateway": finde("gateway"),
        "zeit": finde("time", "zeit"),
        "call": finde("call"),
        "type": finde("type", "typ"),
        "id": finde("id", "issi"),
    }
    if None in (idx["gateway"], idx["call"], idx["type"], idx["id"]):
        return None
    return idx


def _zeit(text: str) -> Optional[float]:
    text = " ".join(text.split())
    for fmt in ZEITFORMATE:
        try:
            return time.mktime(datetime.strptime(text, fmt).timetuple())
        except ValueError:
            continue
    return None


def parse(html: str) -> list[Eintrag]:
    """Alle VOICE-Zeilen aus dem Lastheard, juengste zuerst."""
    p = _Zeilen()
    p.feed(html)
    p.close()
    eintraege = []
    idx = None
    for zeile in p.zeilen:
        neu = _spalten(zeile)
        if neu is not None:
            idx = neu
            continue
        if idx is None or len(zeile) <= max(v for v in idx.values() if v is not None):
            continue
        if zeile[idx["type"]].strip().upper() != "VOICE":
            continue
        try:
            issi = int(zeile[idx["id"]].strip())
        except ValueError:
            continue
        if not 0 < issi <= 0xFFFFFF:
            continue
        zt = zeile[idx["zeit"]] if idx["zeit"] is not None else ""
        eintraege.append(Eintrag(
            gateway=zeile[idx["gateway"]].strip(),
            call=zeile[idx["call"]].strip(),
            issi=issi, zeit=_zeit(zt), zeit_text=zt))
    eintraege.sort(key=lambda e: e.zeit or 0, reverse=True)
    return eintraege


def _gleich(a: str, b: str) -> bool:
    return a.strip().upper() == b.strip().upper()


def finde(eintraege: list[Eintrag], gateway: str, max_age: float,
          jetzt: Optional[float] = None) -> Optional[Eintrag]:
    """Juengste VOICE-Zeile dieses Gateways, sofern frisch genug."""
    if not gateway:
        return None
    jetzt = time.time() if jetzt is None else jetzt
    for e in eintraege:
        if not _gleich(e.gateway, gateway):
            continue
        if e.zeit is None or jetzt - e.zeit > max_age:
            return None      # juengste ist zu alt - aeltere erst recht
        return e
    return None


class DashboardLookup:
    def __init__(self):
        self._cache: tuple[float, str, list[Eintrag]] = (0.0, "", [])
        self._lock = asyncio.Lock()
        self.last_error = ""
        self.last_fetch: Optional[float] = None
        self.hits = 0
        self.misses = 0

    async def eintraege(self, url: str, force: bool = False) -> list[Eintrag]:
        async with self._lock:
            zeit, alt_url, daten = self._cache
            if (not force and alt_url == url
                    and time.monotonic() - zeit < CACHE_SECONDS):
                return daten
            import aiohttp
            try:
                timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get(url, headers={
                            "User-Agent": "dmo2tmo-svxbridge"}) as r:
                        if r.status != 200:
                            raise RuntimeError(f"HTTP {r.status}")
                        roh = await r.content.read(MAX_BYTES)
                html = roh.decode(r.charset or "utf-8", errors="replace")
                daten = parse(html)
            except (aiohttp.ClientError, asyncio.TimeoutError,
                    RuntimeError, OSError) as exc:
                text = f"{url}: {exc or type(exc).__name__}"
                if text != self.last_error:
                    log.warning("Dashboard nicht lesbar: %s", text)
                self.last_error = text
                return []
            self.last_error = ""
            self.last_fetch = time.time()
            self._cache = (time.monotonic(), url, daten)
            return daten

    async def issi_fuer(self, url: str, gateway: str,
                        max_age: float) -> Optional[Eintrag]:
        eintrag = finde(await self.eintraege(url), gateway, max_age)
        if eintrag is None:
            self.misses += 1
        else:
            self.hits += 1
        return eintrag
