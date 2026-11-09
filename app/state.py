"""Laufzeitzustand fuer Logging, /api/status und spaeter die WebUI."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


@dataclass
class LastHeardEntry:
    timestamp: float
    direction: str            # "tetra->svx" oder "svx->tetra"
    gssi: int
    tg: int
    reflector: str
    issi: Optional[int] = None
    callsign: str = ""
    duration: float = 0.0
    frames: int = 0

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(self.timestamp)),
            "direction": self.direction,
            "gssi": self.gssi,
            "tg": self.tg,
            "reflector": self.reflector,
            "issi": self.issi,
            "callsign": self.callsign,
            "duration": round(self.duration, 1),
            "frames": self.frames,
        }


@dataclass
class SubscriberInfo:
    issi: int
    callsign: str = ""
    text: str = ""
    updated: float = field(default_factory=time.time)


class AppState:
    def __init__(self, max_last_heard: int = 50):
        self.started = time.time()
        self.last_heard: Deque[LastHeardEntry] = deque(maxlen=max_last_heard)
        self.subscribers: dict[int, SubscriberInfo] = {}
        # Rufzeichenliste (app/idlist.py). main.py haengt sie ein; ohne sie
        # bleibt alles beim Brew-Auskunftsdienst.
        self.namebook = None
        self.counters = {
            "calls_rx": 0,
            "calls_tx": 0,
            "frames_rx": 0,
            "frames_tx": 0,
            "sds_rx": 0,
            "sds_tx": 0,
            "dropped_no_route": 0,
            "dropped_inactive": 0,
            "dropped_floor": 0,
        }

    def bump(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    def add_last_heard(self, entry: LastHeardEntry) -> None:
        self.last_heard.appendleft(entry)

    def callsign(self, issi: Optional[int], mnemonic: str = "") -> str:
        """Wer ist das?

        Rangfolge, und die ist bewusst so herum:

        1. eigener Eintrag aus der WebUI - der Betreiber weiss es besser als
           jede Datenbank, sonst haette er ihn nicht angelegt
        2. was das Geraet selbst mitgeschickt hat (Mnemonic)
        3. Brew-Auskunft ueber den Teilnehmer, live aus diesem Netz
        4. heruntergeladene Rufzeichenliste, weltweit aber moeglicherweise alt
        """
        if issi is None:
            return mnemonic
        if self.namebook is not None:
            eigen = self.namebook.manual_name(issi)
            if eigen:
                return eigen
        if mnemonic:
            return mnemonic
        info = self.subscribers.get(issi)
        if info and info.callsign:
            return info.callsign
        if self.namebook is not None:
            name = self.namebook.lookup(issi)
            if name:
                return name
        # Zuletzt: was dieses Geraet frueher selbst gemeldet hat.
        for eintrag in self.last_heard:
            if eintrag.issi == issi and eintrag.callsign:
                return eintrag.callsign
        return ""

    def issi_for_callsign(self, callsign: str) -> int:
        """Umgekehrt: welche ISSI hat dieses Rufzeichen?

        Fuer SDS, die vom Reflektor an ein Rufzeichen adressiert kommen.
        Gesucht wird nur in dem, was hier verlaesslich ist - eigene Namen,
        Brew-Auskunft und Gehoertes. Die radioid.net-Liste bleibt aussen
        vor: sie vergibt DMR-IDs, eine SDS ginge an eine fremde ISSI.
        Erst exakt, dann ohne Suffix (OE1ABC-T == OE1ABC).
        """
        gesucht = (callsign or "").strip().upper()
        if not gesucht:
            return 0
        stationen = self.known_stations(limit=1000)
        for station in stationen:
            if (station.get("name") or "").strip().upper() == gesucht:
                return int(station["issi"])
        basis = gesucht.split("-", 1)[0]
        for station in stationen:
            name = (station.get("name") or "").strip().upper()
            if name and name.split("-", 1)[0] == basis:
                return int(station["issi"])
        return 0

    def remember_subscriber(self, issi: int, callsign: str = "",
                            text: str = "") -> None:
        self.subscribers[issi] = SubscriberInfo(
            issi=issi, callsign=callsign, text=text
        )

    def known_stations(self, limit: int = 30) -> list[dict]:
        """Wer ist uns bisher begegnet - Rufzeichen zuerst, ISSI dahinter.

        Der Brew-Auskunftsdienst antwortet an dieser Anlage nie, `subscribers`
        bleibt also leer. Wer nur den auswertet, sieht eine leere Karte,
        obwohl im Last Heard laengst Rufzeichen stehen. Darum aus allen
        Quellen zusammengesetzt.
        """
        gefunden: dict[int, dict] = {}

        def merken(issi, quelle, zuletzt=None, gehoert=""):
            if not issi:
                return
            issi = int(issi)
            eintrag = gefunden.setdefault(
                issi, {"issi": issi, "name": "", "source": quelle,
                       "last_seen": None, "_gehoert": ""})
            if zuletzt and (eintrag["last_seen"] is None
                            or zuletzt > eintrag["last_seen"]):
                eintrag["last_seen"] = zuletzt
                # Was das Geraet selbst gemeldet hat, schlaegt die
                # heruntergeladene Liste - die vergibt DMR-IDs und trifft
                # eine TETRA-ISSI nur zufaellig.
                if gehoert:
                    eintrag["_gehoert"] = gehoert

        # Eigene Eintraege zuerst - die sind die verlaesslichste Quelle.
        if self.namebook is not None:
            for eigen in self.namebook.manual_all():
                merken(eigen.get("issi"), "eigener Name")
        for issi in self.subscribers:
            merken(issi, "Auskunft")
        for eintrag in self.last_heard:
            merken(eintrag.issi, "gehört", eintrag.timestamp,
                   eintrag.callsign)

        for issi, eintrag in gefunden.items():
            eintrag["name"] = self.callsign(issi, eintrag.pop("_gehoert", ""))

        # Zuletzt Gehoertes nach oben, Namenloses ans Ende.
        return sorted(gefunden.values(),
                      key=lambda e: (e["last_seen"] or 0), reverse=True)[:limit]

    def uptime(self) -> float:
        return time.time() - self.started

    def as_dict(self) -> dict:
        return {
            "uptime": round(self.uptime()),
            "counters": dict(self.counters),
            "last_heard": [entry.as_dict() for entry in self.last_heard],
            "subscribers": {
                str(issi): {"callsign": info.callsign, "text": info.text}
                for issi, info in self.subscribers.items()
            },
            "known_stations": self.known_stations(),
        }
