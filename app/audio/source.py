"""Richtung SVX -> TETRA: USRP von svxlink empfangen, ACELP daraus machen.

Gegenstueck zur `UsrpSink`. svxlink schickt alle 20 ms ein Paket mit 160
Samples; drei davon ergeben einen 60-ms-Frame fuer TETRA. Das Takten
uebernimmt hier also die Gegenstelle - wir sammeln nur auf und geben ab,
sobald 480 Samples beisammen sind.

Sprechbeginn und -ende stehen im Keyup-Bit, nicht im Ausbleiben von
Paketen. Trotzdem gibt es einen Wachhund: bricht svxlink weg, ohne das
Abschaltpaket zu schicken, wuerde der Gruppenruf sonst ewig offen bleiben.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

from . import acelp
from . import usrp
from .base import CLIP_WARN, level_info

log = logging.getLogger("audio.src")

FRAME_BYTES = acelp.FRAME_SAMPLES * 2      # 480 Samples = 60 ms
SILENCE_TIMEOUT = 2.0                      # ohne Pakete gilt der Ruf als aus


class UsrpSource:
    """Lauscht auf UDP und meldet einen Sprechvorgang von der SVX-Seite.

    Die drei Rueckrufe bekommen den Reflektornamen mit, damit der Router
    weiss, um welche Instanz es geht.
    """

    def __init__(self, reflector,
                 on_start: Callable[[str, str, int], Awaitable[None]],
                 on_frame: Callable[[str, bytes], Awaitable[None]],
                 on_stop: Callable[[str], Awaitable[None]],
                 listen_host: str = "127.0.0.1",
                 hang_ms: Optional[Callable[[], int]] = None,
                 on_sds: Optional[Callable[[str, dict], Awaitable[None]]] = None):
        self.reflector = reflector
        self.name = getattr(reflector, "name", "?")
        self.port = getattr(reflector, "usrp_listen_port", 0) or 0
        self.listen_host = listen_host
        self.on_start = on_start
        self.on_frame = on_frame
        self.on_stop = on_stop
        self.on_sds = on_sds
        # ISSI des Sprechers, wenn svxlink sie meldet (TETRA-Gateway am
        # Reflektor, siehe svxlink/patches). 0 = unbekannt.
        self.talker_issi = 0

        self._transport = None
        self._codec = None
        self._codec_failed = False
        self._talking = False
        self._buffer = bytearray()
        self._callsign = ""
        self._tg = 0
        self._last_packet = 0.0
        self._pegel = 0.0
        self._pegel_n = 0
        self._clip = 0.0
        self._watchdog: Optional[asyncio.Task] = None
        # Wird bei jedem Rufende neu gefragt, damit eine Aenderung in der
        # WebUI sofort greift und nicht erst beim naechsten Neustart.
        self._hang_ms = hang_ms or (lambda: 0)
        self._hang: Optional[asyncio.Task] = None

    # ------------------------------------------------------------- Aufbau

    async def start(self) -> None:
        if not self.port:
            log.warning("[usrp-rx] Reflektor %s hat keinen usrp_listen_port",
                        self.name)
            return
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self),
            local_addr=(self.listen_host, self.port),
        )
        self._watchdog = asyncio.create_task(self._watch())
        log.info("[usrp-rx] %s lauscht auf %s:%s",
                 self.name, self.listen_host, self.port)

    async def close(self) -> None:
        self._stop_hang()
        if self._talking:
            # Sonst bleibt der Gruppenruf auf der TETRA-Seite offen stehen -
            # der brew-server raeumt ihn nie weg.
            log.info("[usrp-rx] %s: laufender Ruf wird beim Beenden "
                     "abgeschlossen", self.name)
            await self._end()
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _codec_or_none(self):
        """Nur pruefen, ob es ueberhaupt einen Codec gibt.

        Encodiert wird im Router (je Reflektor eine eigene Instanz) - hier
        eine zweite zu laden waere eine Kopie der Bibliothek fuer nichts.
        """
        if self._codec_failed:
            return None
        if self._codec is None:
            if not acelp.available():
                self._codec_failed = True
                log.error("[usrp-rx] %s: kein Codec gefunden - Audio wird "
                          "verworfen", self.name)
                return None
            self._codec = True
        return self._codec

    # ------------------------------------------------------------ Empfang

    async def handle(self, data: bytes) -> None:
        try:
            packet = usrp.parse(data)
        except usrp.UsrpError as exc:
            log.debug("[usrp-rx] %s: %s", self.name, exc)
            return

        self._last_packet = time.monotonic()

        if packet.type == usrp.TYPE_TEXT:
            if packet.payload.startswith(usrp.SDS_PREFIX):
                await self._handle_sds(packet.payload)
                return
            meta = usrp.parse_metadata(packet.payload)
            if meta is not None:
                if meta.callsign:
                    self._callsign = meta.callsign
                if not self._talking:
                    self.talker_issi = meta.issi
                elif meta.issi and meta.issi != self.talker_issi:
                    # Die Quell-ISSI eines laufenden Gruppenrufs laesst sich
                    # nicht mehr aendern - gilt dann ab dem naechsten.
                    log.info("[usrp-rx] %s: ISSI %s kam erst waehrend des "
                             "Rufs", self.name, meta.issi)
            if packet.talkgroup:
                self._tg = packet.talkgroup
            return

        if not packet.is_voice:
            return

        if packet.keyup:
            # Kommt waehrend des Nachlaufs schon wieder Audio, haengt sich
            # der Durchgang an den laufenden Ruf an - kein neuer Aufbau,
            # keine Luecke.
            self._stop_hang()
            if not self._talking:
                await self._begin()
            self._buffer += packet.payload
            await self._drain()
        elif self._talking and self._hang is None:
            # Die Nutzlast des Abschaltpakets wird nicht mehr angehaengt: sie
            # ist laut Mitschnitt Stille, und sie anzuhaengen erzeugt am Ende
            # jeder Aussendung einen ueberfluessigen Frame.
            await self._drain(flush=True)
            ms = max(0, int(self._hang_ms()))
            if ms:
                self._hang = asyncio.create_task(self._nachlauf(ms))
            else:
                await self._end()

    async def _handle_sds(self, payload: bytes) -> None:
        text = payload[len(usrp.SDS_PREFIX):].split(b"\x00", 1)[0]
        try:
            data = json.loads(text.decode("utf-8", errors="replace"))
        except ValueError as exc:
            log.warning("[usrp-rx] %s: SDS von svxlink ist kein JSON (%s): %r",
                        self.name, exc, text[:80])
            return
        if not isinstance(data, dict):
            log.warning("[usrp-rx] %s: SDS von svxlink unbrauchbar: %r",
                        self.name, data)
            return
        if self.on_sds is None:
            log.info("[usrp-rx] %s: SDS von svxlink, aber keiner nimmt sie: %r",
                     self.name, data)
            return
        await self.on_sds(self.name, data)

    async def _begin(self) -> None:
        self._talking = True
        self._buffer.clear()
        await self.on_start(self.name, self._callsign, self._tg)

    def _stop_hang(self) -> None:
        if self._hang is not None:
            self._hang.cancel()
            self._hang = None

    async def _nachlauf(self, ms: int) -> None:
        """Ruf noch kurz offen halten und Stille schicken.

        Ohne das endet der TETRA-Ruf in dem Moment, in dem PTT losgelassen
        wird. Das Gegenueber hoert ein hartes Ende, und der Anfang eines
        gleich folgenden Durchgangs geht im Rufaufbau unter.
        """
        stille = b"\x00" * FRAME_BYTES
        try:
            for _ in range(max(1, round(ms / 60))):
                await asyncio.sleep(0.06)
                await self.on_frame(self.name, stille)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[usrp-rx] %s: Nachlauf gescheitert", self.name)
        self._hang = None
        await self._end()

    async def _end(self) -> None:
        if self._pegel_n:
            clip = self._clip / self._pegel_n
            log.info("[usrp-rx] %s: Pegel von svxlink %.0f (mittlerer Betrag)",
                     self.name, self._pegel / self._pegel_n)
            if clip > CLIP_WARN:
                log.warning("[usrp-rx] %s: %.2f%% der Samples am Anschlag - "
                            "Pegel Richtung TETRA zu hoch, gain_tetra_db "
                            "oder PREAMP senken", self.name, clip)
            self._pegel = self._clip = 0.0
            self._pegel_n = 0
        self._talking = False
        self._buffer.clear()
        self._callsign = ""
        self.talker_issi = 0
        await self.on_stop(self.name)

    async def _drain(self, flush: bool = False) -> None:
        codec = self._codec_or_none()
        while len(self._buffer) >= FRAME_BYTES:
            chunk = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]
            if codec is not None:
                pegel, clip = level_info(chunk)
                self._pegel += pegel
                self._clip += clip
                self._pegel_n += 1
                await self.on_frame(self.name, chunk)

        if flush and self._buffer and codec is not None:
            # Rest mit Stille auffuellen, sonst geht das letzte Stueck verloren.
            chunk = bytes(self._buffer) + b"\x00" * (FRAME_BYTES - len(self._buffer))
            self._buffer.clear()
            await self.on_frame(self.name, chunk)

    async def _watch(self) -> None:
        """Beendet den Ruf, wenn svxlink verstummt ohne abzuschalten."""
        while True:
            await asyncio.sleep(0.5)
            if not self._talking:
                continue
            if self._hang is not None:
                continue           # der Nachlauf beendet den Ruf selbst
            if time.monotonic() - self._last_packet > SILENCE_TIMEOUT:
                log.warning("[usrp-rx] %s: %.0f s keine Pakete - Ruf beendet",
                            self.name, SILENCE_TIMEOUT)
                await self._end()


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, source: UsrpSource):
        self.source = source

    def datagram_received(self, data: bytes, addr) -> None:
        asyncio.create_task(self._handle(data))

    async def _handle(self, data: bytes) -> None:
        try:
            await self.source.handle(data)
        except Exception:
            log.exception("[usrp-rx] Fehler beim Verarbeiten")

    def error_received(self, exc) -> None:
        log.warning("[usrp-rx] UDP-Fehler: %s", exc)


def _callsign_from_metadata(payload: bytes) -> str:
    """Holt das Rufzeichen aus einer TLV_TAG_SET_INFO-Nutzlast."""
    if len(payload) < 2 or payload[0] != usrp.TLV_TAG_SET_INFO:
        return ""
    body = payload[2:]
    if len(body) <= 12:
        return ""
    call = body[12:].split(b"\x00", 1)[0]
    return call.decode("ascii", errors="replace").strip()
