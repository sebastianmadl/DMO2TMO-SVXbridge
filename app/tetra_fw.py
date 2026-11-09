"""Lastheard an den TETRA-AUSTRIA-Verbund (tetra_fw_server).

Der OE-Verbund nimmt Lastheard nicht ueber svxlink entgegen, sondern ueber
ein eigenes UDP-Textprotokoll: eine Zeile je Paket an den Reflektor-Host,
Port 5220. Kein Handshake, keine Antwort. Nachgebaut nach dem Gateway
`tetra_link_gw` von OE1KBC (mit seinem OK), Details in
docs/tetra_fw_protocol.md.

Zeilen, die wir schicken:

    VOICE: <gw>,<comment>,<zeit>,<call>,VOICE,#<tg>,<issi>,,,
    POS:   <gw>,<comment>,<zeit>,<gw>,POS,#<tg>,<issi>,,E;<lon>;N;<lat>;000;,

Laenge vor Breite: so steht es im Original (`E;01624.91;N;4816.82`), auch
wenn die Buchstaben davor anderes vermuten lassen.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Callable, Optional

from . import config as cfgmod

log = logging.getLogger("tetra_fw")

NO_USER = "NO USER"           # so meldet das Original einen Unbekannten
BEACON_COUNT = 6              # Start-Bake: 6x im Abstand von 1 s
BEACON_INTERVAL = 1.0
DNS_TTL = 300.0


def _feld(text) -> str:
    """Ein Komma oder Zeilenumbruch wuerde die Spalten verschieben."""
    return (str(text if text is not None else "")
            .replace(",", " ").replace("\r", " ").replace("\n", " ").strip())


def zeitstempel(ts: Optional[float] = None) -> str:
    # Vierstelliges Jahr wie bei den anderen Gateways am OE-Dashboard -
    # dort steht "09.11.2026 12:41:19"; mit "09.11.26" passte unsere Zeile
    # in eine Zeile und tanzte aus der Reihe.
    return time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ts))


def voice_line(gateway: str, comment: str, call: str, tg: int, issi: int,
               ts: Optional[float] = None) -> str:
    return (f"{_feld(gateway)},{_feld(comment)},{zeitstempel(ts)},"
            f"{_feld(call) or NO_USER},VOICE,#{int(tg)},{int(issi)},,,\n")


def pos_line(gateway: str, comment: str, call: str, tg: int, issi: int,
             lat: str, lon: str, ts: Optional[float] = None) -> str:
    return (f"{_feld(gateway)},{_feld(comment)},{zeitstempel(ts)},"
            f"{_feld(call) or NO_USER},POS,#{int(tg)},{int(issi)},,"
            f"E;{lon};N;{lat};000;,\n")


class TetraFwSender:
    """Schickt die Zeilen. Liest die Config bei jedem Aufruf frisch, damit
    eine Aenderung in der WebUI ohne Neustart greift."""

    def __init__(self, get_cfg: Callable[[], cfgmod.Config]):
        self._get_cfg = get_cfg
        self._sock: Optional[socket.socket] = None
        self._dns: dict[str, tuple[float, str]] = {}
        self._beacon_task: Optional[asyncio.Task] = None
        self.sent = 0
        self.errors = 0
        self.last_line = ""
        self.last_error = ""
        self.last_sent: Optional[float] = None

    # -------------------------------------------------------------- Werte

    def target(self) -> tuple[str, int]:
        cfg = self._get_cfg()
        reflector = cfg.active_reflector()
        return (reflector.host if reflector else ""), int(cfg.tetra_fw.port)

    def gateway_call(self) -> str:
        cfg = self._get_cfg()
        if cfg.tetra_fw.gateway_call:
            return cfg.tetra_fw.gateway_call
        reflector = cfg.active_reflector()
        return reflector.callsign if reflector else ""

    def tg(self) -> int:
        cfg = self._get_cfg()
        return cfg.routes[0].tg if cfg.routes else 0

    def enabled(self) -> bool:
        return bool(self._get_cfg().tetra_fw.enabled)

    def status(self) -> dict:
        host, port = self.target()
        return {
            "host": host, "port": port,
            "gateway_call": self.gateway_call(), "tg": self.tg(),
            "sent": self.sent, "errors": self.errors,
            "last_line": self.last_line.rstrip("\n"),
            "last_error": self.last_error, "last_sent": self.last_sent,
        }

    # -------------------------------------------------------------- Senden

    async def _resolve(self, host: str) -> str:
        jetzt = time.monotonic()
        treffer = self._dns.get(host)
        if treffer and jetzt - treffer[0] < DNS_TTL:
            return treffer[1]
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        addr = infos[0][4][0]
        self._dns[host] = (jetzt, addr)
        return addr

    async def send_line(self, line: str) -> bool:
        host, port = self.target()
        if not host:
            self._fehler("kein aktiver Reflektor mit Host")
            return False
        try:
            addr = await self._resolve(host)
            if self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.setblocking(False)
            self._sock.sendto(line.encode("utf-8", "replace"), (addr, port))
        except OSError as exc:
            self._fehler(f"{host}:{port}: {exc}")
            return False
        self.sent += 1
        self.last_line = line
        self.last_sent = time.time()
        self.last_error = ""
        log.debug("-> %s:%s %s", host, port, line.rstrip("\n"))
        return True

    def _fehler(self, text: str) -> None:
        self.errors += 1
        # Nur beim ersten Mal laut, sonst flutet ein toter DNS das Log.
        if text != self.last_error:
            log.warning("Lastheard nicht gesendet: %s", text)
        self.last_error = text

    async def voice(self, issi: Optional[int], callsign: str) -> bool:
        """VOICE-Zeile zu Beginn eines Durchgangs von TETRA."""
        if not self.enabled():
            return False
        if not issi:
            log.debug("Lastheard: Durchgang ohne Quell-ISSI - nichts gemeldet")
            return False
        cfg = self._get_cfg()
        line = voice_line(self.gateway_call(), cfg.tetra_fw.gateway_comment,
                          callsign, self.tg(), issi)
        ok = await self.send_line(line)
        if ok:
            log.info("Lastheard: ISSI %s (%s) gemeldet", issi,
                     callsign or NO_USER)
        return ok

    async def beacon(self) -> int:
        """Start-Bake: eigene Position 6x. Ohne Position passiert nichts."""
        cfg = self._get_cfg()
        fw = cfg.tetra_fw
        if not fw.enabled or not (fw.gateway_lat and fw.gateway_lon):
            return 0
        gesendet = 0
        for i in range(BEACON_COUNT):
            cfg = self._get_cfg()
            fw = cfg.tetra_fw
            if not fw.enabled or not (fw.gateway_lat and fw.gateway_lon):
                break
            line = pos_line(self.gateway_call(), fw.gateway_comment,
                            self.gateway_call(), self.tg(), cfg.brew.issi,
                            fw.gateway_lat, fw.gateway_lon)
            if await self.send_line(line):
                gesendet += 1
            if i < BEACON_COUNT - 1:
                await asyncio.sleep(BEACON_INTERVAL)
        if gesendet:
            log.info("Lastheard: Start-Bake %dx an %s:%s", gesendet,
                     *self.target())
        return gesendet

    def start_beacon(self) -> None:
        """Bake im Hintergrund; eine laufende wird ersetzt."""
        if self._beacon_task is not None and not self._beacon_task.done():
            self._beacon_task.cancel()
        self._beacon_task = asyncio.create_task(self.beacon(),
                                                name="tetra_fw-bake")

    def close(self) -> None:
        if self._beacon_task is not None and not self._beacon_task.done():
            self._beacon_task.cancel()
        if self._sock is not None:
            self._sock.close()
            self._sock = None
