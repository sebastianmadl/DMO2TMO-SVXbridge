"""Audio-Senken.

Etappe 2 kennt noch keinen Codec: die ACELP-Frames werden unveraendert
weitergereicht. Damit laesst sich der komplette Weg (Auth, Registrierung,
Affiliation, GROUP_TX-Routing, Framing) testen, bevor der ETSI-Codec
dazukommt.

Die UsrpSink weiter unten macht daraus Ernst: ACELP -> PCM 8k -> UDP an
svxlink. Die Schnittstelle ist bei allen Senken dieselbe.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Optional

from ..brew import protocol as p
from . import acelp
from . import usrp

log = logging.getLogger("audio")


# Ab hier gilt Audio als uebersteuert. Ein bisschen Anschlag kommt bei
# Sprache vor, aber alles darueber hoert man als Verzerrung.
CLIP_WARN = 0.1          # Prozent begrenzter Samples
CLIP_GRENZE = 32700      # praktisch am Anschlag


def mean_level(pcm: bytes) -> float:
    """Mittlerer Betrag der Samples - grobes Mass fuer den Pegel."""
    return level_info(pcm)[0]


def level_info(pcm: bytes) -> tuple[float, float]:
    """(mittlerer Betrag, Anteil begrenzter Samples in Prozent).

    Der zweite Wert ist der wichtigere: zu hoher Pegel macht Audio nicht
    lauter, sondern kaputt - und man hoert es erst, wenn jemand klagt.
    """
    if not pcm:
        return 0.0, 0.0
    gesamt = 0
    anschlag = 0
    n = len(pcm) // 2
    for i in range(0, len(pcm) - 1, 2):
        wert = abs(int.from_bytes(pcm[i:i + 2], "little", signed=True))
        gesamt += wert
        if wert >= CLIP_GRENZE:
            anschlag += 1
    return gesamt / n, 100.0 * anschlag / n


def apply_gain(pcm: bytes, factor: float) -> bytes:
    """Pegel anpassen, mit Begrenzung statt Ueberlauf.

    Der Faktor kommt aus einem dB-Wert in der Konfiguration; 1.0 laesst das
    Signal unveraendert und wird hier gar nicht erst angefasst.
    """
    if factor == 1.0:
        return pcm
    out = bytearray(len(pcm))
    for i in range(0, len(pcm) - 1, 2):
        value = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        value = max(-32768, min(32767, int(value * factor)))
        out[i:i + 2] = value.to_bytes(2, "little", signed=True)
    return bytes(out)


class AudioSink:
    name = "base"

    async def start(self, call) -> None:
        """Ein Gruppenruf beginnt."""

    async def frame(self, call, data: bytes) -> None:
        """Ein 60-ms-Traffic-Frame (36 Byte ACELP)."""

    async def stop(self, call) -> None:
        """Der Gruppenruf ist zu Ende."""

    async def close(self) -> None:
        """Aufraeumen beim Beenden."""


class NullSink(AudioSink):
    """Verwirft alles, zaehlt nur mit. Gut zum Protokoll-Debuggen."""

    name = "null"

    async def start(self, call) -> None:
        log.info("[null] Call %s auf GSSI %s von ISSI %s",
                 call.identifier, call.gssi, call.source)

    async def frame(self, call, data: bytes) -> None:
        pass

    async def stop(self, call) -> None:
        log.info("[null] Call %s beendet nach %d Frames (%.1f s)",
                 call.identifier, call.frames, call.frames * 0.06)


class LoopbackSink(AudioSink):
    """Schleift die empfangenen Frames unveraendert auf eine andere GSSI.

    Damit testest du beide Richtungen ohne Codec und ohne svxlink: zwei
    Instanzen, zwei ISSIs, jede auf einer eigenen GSSI - was auf der einen
    reingeht, muss auf der anderen rauskommen.
    """

    name = "loopback"

    def __init__(self, client, target_gssi: int,
                 source_issi: Optional[int] = None, callsign: str = ""):
        self.client = client
        self.target_gssi = target_gssi
        self.source_issi = source_issi
        self.callsign = callsign

    async def start(self, call) -> None:
        if not self.target_gssi:
            log.warning("[loopback] keine target_gssi konfiguriert")
            return
        tx_id = self.client.group_tx(
            self.target_gssi,
            source=self.source_issi or call.source,
            callsign=self.callsign or call.callsign,
        )
        call.sink_state["tx"] = tx_id
        log.info("[loopback] GSSI %s -> %s (Call %s)",
                 call.gssi, self.target_gssi, tx_id)

    async def frame(self, call, data: bytes) -> None:
        tx_id = call.sink_state.get("tx")
        if tx_id is not None:
            self.client.traffic(tx_id, data)

    async def stop(self, call) -> None:
        tx_id = call.sink_state.pop("tx", None)
        if tx_id is not None:
            self.client.group_idle(tx_id)
            log.info("[loopback] Call %s beendet, %d Frames gespiegelt",
                     tx_id, call.frames)


class UsrpSink(AudioSink):
    """ACELP -> PCM -> USRP/UDP an svxlink. Richtung TETRA -> SVX.

    svxlink laeuft im selben Container (siehe CLAUDE.md, Etappe 4), darum
    geht alles auf localhost raus.

    Die Umtaktung 60 -> 20 ms passiert hier: ein Brew-Frame sind 480
    Samples, svxlink will 160 pro Paket. Statt drei Pakete im Schwall
    rauszuhauen, sobald ein TETRA-Frame ankommt, laufen sie ueber eine
    Warteschlange, die alle 20 ms genau eines abgibt - das ist der
    Jitterbuffer. Ohne den kaeme svxlink alle 60 ms ein Dreierpack, was es
    zwar wegsteckt, aber unnoetig holprig.
    """

    name = "usrp"

    QUEUE_MAX = 150          # 3 s Audio; laeuft es voller, stimmt was nicht

    def __init__(self, reflector, host: str = "127.0.0.1",
                 gain: float = 1.0, issi_passthrough: bool = True):
        self.reflector = reflector
        self.issi_passthrough = issi_passthrough
        self.host = host
        self.gain = gain
        self.port = getattr(reflector, "usrp_port", 0) or 0
        self.label = getattr(reflector, "name", "?")
        self._codec = None
        self._codec_failed = False
        self._sock: Optional[socket.socket] = None
        self._queue: Optional[asyncio.Queue] = None
        self._pump: Optional[asyncio.Task] = None
        self._seq = 0
        self._tg = 0
        self._pegel = 0.0
        self._pegel_n = 0
        self._clip = 0.0

    # ------------------------------------------------------------- intern

    def _codec_or_none(self):
        if self._codec is not None or self._codec_failed:
            return self._codec
        try:
            self._codec = acelp.AcelpCodec()
        except acelp.CodecUnavailable as exc:
            self._codec_failed = True
            log.error("[usrp] Reflektor %s: kein Codec (%s) - Audio wird "
                      "verworfen", self.label, exc)
        return self._codec

    def _ensure_running(self) -> None:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._pump_loop())

    def _send(self, packet: bytes) -> None:
        if self._sock is None or not self.port:
            return
        try:
            self._sock.sendto(packet, (self.host, self.port))
        except OSError as exc:
            log.warning("[usrp] Senden an %s:%s fehlgeschlagen: %s",
                        self.host, self.port, exc)

    async def _pump_loop(self) -> None:
        """Gibt alle 20 ms ein Paket ab, mit Driftausgleich."""
        next_at = time.monotonic()
        while True:
            item = await self._queue.get()
            if item is None:                       # Traegerende
                self._seq += 1
                self._send(usrp.build_end(self._seq, talkgroup=self._tg))
                next_at = time.monotonic()
                continue

            self._seq += 1
            self._send(usrp.build_voice(self._seq, item, keyup=True,
                                        talkgroup=self._tg))

            next_at += usrp.VOICE_MS / 1000.0
            delay = next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Wir hinken hinterher - Takt neu setzen statt aufzuholen,
                # sonst sendet der Puffer im Schnelldurchlauf leer.
                next_at = time.monotonic()

    # -------------------------------------------------------------- Sink

    async def start(self, call) -> None:
        if not self.port:
            log.warning("[usrp] Reflektor %s hat keinen usrp_port", self.label)
            return
        self._ensure_running()
        # Ohne TG im Header meldet svxlink "Talker stop: unknown TG# 0" und
        # verwirft den Sprecher, obwohl das Audio ankommt.
        self._tg = getattr(call, "tg", 0) or 0
        self._seq += 1
        callsign = call.callsign or (str(call.source) if call.source else "")
        # Die ISSI des Sprechers geht im DMR-ID-Feld mit - die gepatchte
        # UsrpLogic macht daraus ein Qso:info fuer den Reflektor.
        issi = (getattr(call, "source", 0) or 0) if self.issi_passthrough else 0
        self._send(usrp.build_metadata(self._seq, callsign, talkgroup=self._tg,
                                       dmr_id=issi))
        log.info("[usrp] %s: Call von %s%s auf TG %s -> %s:%s",
                 self.label, callsign or "?",
                 f" (ISSI {issi})" if issi else "", self._tg,
                 self.host, self.port)

    def send_text(self, text: bytes) -> bool:
        """Textnachricht an die UsrpLogic, z.B. eine SDS."""
        if not self.port:
            return False
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
        self._seq += 1
        self._send(usrp.build_text(self._seq, text))
        return True

    async def frame(self, call, data: bytes) -> None:
        codec = self._codec_or_none()
        if codec is None or self._queue is None:
            return
        try:
            _, sub1, sub2 = p.split_traffic_frame(data)
        except p.BrewProtocolError as exc:
            log.debug("[usrp] Frame unbrauchbar: %s", exc)
            return
        pcm = apply_gain(codec.decode_frame(sub1, sub2), self.gain)
        pegel, clip = level_info(pcm)
        self._pegel += pegel
        self._clip += clip
        self._pegel_n += 1
        for chunk in usrp.split_pcm(pcm):
            try:
                self._queue.put_nowait(chunk)
            except asyncio.QueueFull:
                log.warning("[usrp] %s: Puffer voll, Audio faellt raus",
                            self.label)
                return

    async def stop(self, call) -> None:
        if self._pegel_n:
            clip = self._clip / self._pegel_n
            log.info("[usrp] %s: Pegel Richtung svxlink %.0f (mittlerer Betrag)",
                     self.label, self._pegel / self._pegel_n)
            if clip > CLIP_WARN:
                log.warning("[usrp] %s: %.2f%% der Samples am Anschlag - "
                            "Pegel Richtung svxlink zu hoch, gain_svx_db "
                            "oder NET_PREAMP senken", self.label, clip)
            self._pegel = self._clip = 0.0
            self._pegel_n = 0
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            self._seq += 1
            self._send(usrp.build_end(self._seq))

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
