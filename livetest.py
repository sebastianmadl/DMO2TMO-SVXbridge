#!/usr/bin/env python3
"""Test gegen den echten brew-server. Braucht den Codec, laeuft also im Container.

Drei Betriebsarten:

  rx    auf GSSIs affiliieren, eingehende Gruppenrufe decodieren, .wav schreiben
  tx    eine .wav (oder einen Testton) als ACELP-Frames senden
  loop  beides gleichzeitig mit zwei Verbindungen - der Selbsttest ueber den
        echten Server, ohne Gegenstelle

Beispiele:

  docker run --rm -v "$PWD/out:/data" dmo2tmo-svxbridge python3 livetest.py rx \\
      --url https://brew.oe1sxm.at --user 100000001 --password '<pw-bs1>' \\
      --issi 2320997 --gssi 2321

  docker run --rm -v "$PWD/out:/data" dmo2tmo-svxbridge python3 livetest.py loop \\
      --url https://brew.oe1sxm.at \\
      --user 100000001 --password '<pw-bs1>' --issi 2320997 \\
      --user2 100000002 --password2 '<pw-bs2>' --issi2 2320998 \\
      --gssi 2321 --seconds 3

rx protokolliert das Laengenfeld eingehender Traffic-Frames und die
SS-TPI-Mnemonic - damit beantworten sich die offenen Punkte 1 und 2, sobald
eine fremde Gegenstelle sendet.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
import time
import uuid as _uuid
import wave
from pathlib import Path
from typing import Optional

from app import config as cfgmod
from app.audio import acelp
from app.brew import protocol as p
from app.brew import sds as sdsmod
from app.brew.client import BrewClient, BrewHandler

log = logging.getLogger("livetest")

FRAME_MS = 60
FRAME_BYTES = acelp.FRAME_SAMPLES * 2      # 480 Samples, 16 Bit


# ------------------------------------------------------------------ Hilfsmittel


def make_config(url: str, user: str, password: str, issi: int) -> cfgmod.BrewConfig:
    cfg = cfgmod.BrewConfig()
    cfg.url = url
    cfg.username = user
    cfg.password = password
    cfg.issi = issi
    cfg.reconnect_min_seconds = 2.0
    cfg.reconnect_max_seconds = 10.0
    return cfg


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise SystemExit(
                f"{path}: brauche mono 16 Bit, ist {handle.getnchannels()} Kanal / "
                f"{handle.getsampwidth() * 8} Bit"
            )
        if handle.getframerate() != 8000:
            raise SystemExit(
                f"{path}: brauche 8000 Hz, ist {handle.getframerate()}. "
                f"Umwandeln z.B. mit: sox {path} -r 8000 -c 1 -b 16 out.wav"
            )
        return handle.readframes(handle.getnframes())


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(pcm)


def test_tone(seconds: float) -> bytes:
    """Stimmhaftes Testsignal - ein reiner Sinus ist fuer ACELP unfair."""
    out = bytearray()
    for i in range(int(8000 * seconds)):
        t = i / 8000.0
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 3.0 * t)
        value = (
            0.60 * math.sin(2 * math.pi * 130.0 * t)
            + 0.25 * math.sin(2 * math.pi * 390.0 * t)
            + 0.10 * math.sin(2 * math.pi * 910.0 * t)
            + 0.05 * math.sin(2 * math.pi * 1700.0 * t)
        )
        out += int(max(-1.0, min(1.0, value * env)) * 8000).to_bytes(
            2, "little", signed=True
        )
    return bytes(out)


def pad_to_frame(pcm: bytes) -> bytes:
    rest = len(pcm) % FRAME_BYTES
    return pcm + b"\x00" * (FRAME_BYTES - rest) if rest else pcm


# ---------------------------------------------------------------------- Empfang


class Receiver(BrewHandler):
    def __init__(self, gssis: list[int], outdir: Path,
                 codec: Optional[acelp.AcelpCodec]):
        self.gssis = gssis
        self.outdir = outdir
        self.codec = codec
        self.calls: dict[_uuid.UUID, dict] = {}
        self.finished: list[dict] = []
        self.connected = asyncio.Event()

    async def on_connected(self, client) -> None:
        await client.affiliate(self.gssis)
        self.connected.set()

    async def on_group_tx(self, msg: p.CallMessage) -> None:
        group = msg.group
        if group is None:
            return
        mnemonic = group.mnemonic_text
        log.info("RX GROUP_TX  GSSI %s  von ISSI %s%s",
                 group.destination, group.source,
                 f"  Mnemonic {mnemonic!r}" if mnemonic else "  (keine Mnemonic)")
        if mnemonic:
            # Offener Punkt 2: so schickt es die Gegenstelle wirklich.
            log.info("   Mnemonic roh: %s", group.mnemonic[:12].hex())
        self.calls[msg.identifier] = {
            "gssi": group.destination,
            "source": group.source,
            "mnemonic": mnemonic,
            "frames": [],
            "pcm": bytearray(),
            "started": time.time(),
            "logged_bits": False,
        }

    async def on_traffic(self, frame: p.FrameMessage) -> None:
        call = self.calls.get(frame.identifier)
        if call is None:
            return
        if not call["logged_bits"]:
            # Offener Punkt 1: was traegt eine echte Gegenstelle im Laengenfeld?
            log.info("   erster Frame: bits=%s  len(data)=%s Byte  (unsere Annahme: "
                     "%s)", frame.bits, len(frame.data), len(frame.data) * 8)
            call["logged_bits"] = True
        call["frames"].append(frame.data)
        if self.codec is None or len(frame.data) < p.TRAFFIC_FRAME_BYTES:
            return
        try:
            _, sub1, sub2 = p.split_traffic_frame(frame.data)
            call["pcm"] += self.codec.decode_frame(sub1, sub2)
        except p.BrewProtocolError as exc:
            log.warning("   Frame nicht zerlegbar: %s", exc)

    async def on_group_idle(self, msg: p.CallMessage) -> None:
        call = self.calls.pop(msg.identifier, None)
        if call is None:
            return
        duration = time.time() - call["started"]
        name = (f"rx_{call['gssi']}_{call['source']}_"
                f"{time.strftime('%H%M%S')}.wav")
        path = self.outdir / name
        if call["pcm"]:
            write_wav(path, bytes(call["pcm"]))
            log.info("RX fertig: %d Frames / %.1f s -> %s",
                     len(call["frames"]), duration, path)
        else:
            log.info("RX fertig: %d Frames / %.1f s (kein Audio geschrieben)",
                     len(call["frames"]), duration)
        call["path"] = path if call["pcm"] else None
        self.finished.append(call)

    async def on_error_message(self, msg: p.ErrorMessage) -> None:
        log.error("Server meldet Fehler Typ %s: %s", msg.type, msg.data[:64].hex())


# ----------------------------------------------------------------------- Senden


class Sender(BrewHandler):
    def __init__(self, gssis: list[int]):
        self.gssis = gssis
        self.connected = asyncio.Event()

    async def on_connected(self, client) -> None:
        await client.affiliate(self.gssis)
        self.connected.set()

    async def on_error_message(self, msg: p.ErrorMessage) -> None:
        log.error("Server meldet Fehler Typ %s: %s", msg.type, msg.data[:64].hex())


async def transmit(client: BrewClient, gssi: int, pcm: bytes,
                   codec: acelp.AcelpCodec, callsign: str = "",
                   bits_override: Optional[int] = None) -> int:
    """Sendet PCM als Gruppenruf. Gibt die Anzahl gesendeter Frames zurueck."""
    pcm = pad_to_frame(pcm)
    identifier = client.group_tx(gssi, callsign=callsign)
    log.info("TX GROUP_TX auf GSSI %s (Call %s)%s", gssi, identifier,
             f", Mnemonic {callsign!r}" if callsign else "")
    await asyncio.sleep(0.2)          # dem Server kurz Zeit geben

    count = 0
    start = time.monotonic()
    try:
        count = await _frames_senden(client, identifier, pcm, codec, count,
                                     start, bits_override)
    finally:
        # Auch bei Abbruch (Strg-C, docker stop): ohne GROUP_IDLE bleibt der
        # Ruf auf dem brew-server ewig offen stehen.
        client.group_idle(identifier)
        await client.flush(1.0)
    log.info("TX fertig: %d Frames / %.1f s", count, count * FRAME_MS / 1000.0)
    return count


async def _frames_senden(client, identifier, pcm, codec, count, start,
                         bits_override):
    for offset in range(0, len(pcm), FRAME_BYTES):
        sub1, sub2 = codec.encode_frame(pcm[offset:offset + FRAME_BYTES])
        data = p.build_traffic_frame(sub1, sub2)
        if bits_override is None:
            client.traffic(identifier, data)
        else:
            client.send(p.build_frame(p.FRAME_TRAFFIC_CHANNEL, identifier,
                                      data, bits=bits_override))
        count += 1
        # Ohne Driftausgleich laeuft das ueber laengere Ansagen weg.
        target = start + count * (FRAME_MS / 1000.0)
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    return count


# ------------------------------------------------------------------ Betriebsart


async def run_client(client: BrewClient, ready: asyncio.Event,
                     timeout: float = 30.0) -> asyncio.Task:
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        raise SystemExit(f"Keine Verbindung innerhalb {timeout:.0f} s "
                         f"({client.last_error or 'kein Fehler gemeldet'})")
    return task


def load_codec(required: bool) -> Optional[acelp.AcelpCodec]:
    try:
        return acelp.AcelpCodec()
    except acelp.CodecUnavailable as exc:
        if required:
            raise SystemExit(f"Codec fehlt: {exc}")
        log.warning("Codec fehlt (%s) - es werden nur Frames gezaehlt, kein Audio",
                    exc)
        return None


def source_pcm(args) -> bytes:
    if args.wav:
        pcm = read_wav(Path(args.wav))
        log.info("Sende %s (%.1f s)", args.wav, len(pcm) / 2 / 8000)
        return pcm
    pcm = test_tone(args.seconds)
    log.info("Sende Testton (%.1f s)", args.seconds)
    return pcm


async def mode_rx(args) -> int:
    codec = load_codec(required=False)
    receiver = Receiver(args.gssi, Path(args.out), codec)
    client = BrewClient(make_config(args.url, args.user, args.password, args.issi),
                        receiver)
    task = await run_client(client, receiver.connected)
    log.info("Empfange auf GSSI %s - Abbruch mit Strg-C",
             ", ".join(str(g) for g in args.gssi))
    try:
        await asyncio.sleep(args.listen)
    except asyncio.CancelledError:
        pass
    await client.stop()
    task.cancel()
    log.info("%d Gruppenruf(e) empfangen", len(receiver.finished))
    return 0 if receiver.finished else 1


async def mode_tx(args) -> int:
    codec = load_codec(required=True)
    sender = Sender(args.gssi)
    client = BrewClient(make_config(args.url, args.user, args.password, args.issi),
                        sender)
    task = await run_client(client, sender.connected)
    await transmit(client, args.gssi[0], source_pcm(args), codec,
                   callsign=args.callsign, bits_override=args.bits)
    await asyncio.sleep(0.5)
    await client.stop()
    task.cancel()
    return 0


async def mode_sds(args) -> int:
    """Text-SDS an die Control-ISSI schicken und auf die Antwort warten."""
    class Empfaenger(Sender):
        def __init__(self, gssis):
            super().__init__(gssis)
            self.antwort = None
            self._short = {}

        async def on_call(self, msg):
            if msg.type == p.CALL_SHORT_TRANSFER and msg.short is not None:
                self._short[msg.identifier] = msg.short

        async def on_sds(self, frame):
            if frame.type != p.FRAME_SDS_TRANSFER:
                return
            short = self._short.pop(frame.identifier, None)
            parsed = sdsmod.decode_text(frame.data)
            if parsed is not None:
                self.antwort = parsed.text
                log.info("Antwort von ISSI %s: %r",
                         short.source if short else "?", parsed.text)

    handler = Empfaenger(args.gssi)
    client = BrewClient(make_config(args.url, args.user, args.password, args.issi),
                        handler)
    task = await run_client(client, handler.connected)

    ziel = args.to or args.issi
    log.info("SDS an ISSI %s: %r", ziel, args.text)
    client.send_sds(ziel, sdsmod.encode_text(args.text))

    for _ in range(int(args.wait * 10)):
        if handler.antwort is not None:
            break
        await asyncio.sleep(0.1)

    await client.stop()
    task.cancel()
    if handler.antwort is None:
        print("Keine Antwort erhalten.")
        return 1
    print(f"Antwort: {handler.antwort}")
    return 0


async def mode_loop(args) -> int:
    """Sender und Empfaenger gleichzeitig, zwei Verbindungen, ein Prozess."""
    if not args.user2:
        raise SystemExit("loop braucht --user2/--password2/--issi2")

    codec_tx = load_codec(required=True)
    codec_rx = load_codec(required=True)

    receiver = Receiver(args.gssi, Path(args.out), codec_rx)
    rx_client = BrewClient(
        make_config(args.url, args.user2, args.password2, args.issi2), receiver
    )
    rx_task = await run_client(rx_client, receiver.connected)

    sender = Sender(args.gssi)
    tx_client = BrewClient(
        make_config(args.url, args.user, args.password, args.issi), sender
    )
    tx_task = await run_client(tx_client, sender.connected)

    original = pad_to_frame(source_pcm(args))
    await asyncio.sleep(0.5)          # Affiliation setzen lassen
    sent = await transmit(tx_client, args.gssi[0], original, codec_tx,
                          callsign=args.callsign, bits_override=args.bits)

    await asyncio.sleep(1.5)          # Nachlauf fuer die letzten Frames
    await tx_client.stop()
    await rx_client.stop()
    tx_task.cancel()
    rx_task.cancel()

    print()
    if not receiver.finished:
        print(f"FEHLGESCHLAGEN: {sent} Frames gesendet, nichts empfangen.")
        print("Der Server hat die Frames nicht weitergeleitet. Wenn im Log ein")
        print("Fehler Typ 0 (MALFORMED) steht, mit --bits 282 gegenprobieren.")
        return 1

    call = receiver.finished[0]
    got = len(call["frames"])
    print(f"Gesendet: {sent} Frames   Empfangen: {got} Frames")
    ok = got == sent
    print(f"  {'ok  ' if ok else 'FEHL'}  Frameanzahl stimmt")
    if call["path"]:
        print(f"  ok    Audio geschrieben: {call['path']}")
    print(f"  {'ok  ' if call['mnemonic'] else '--  '}  "
          f"Mnemonic zurueck: {call['mnemonic']!r}")
    return 0 if ok else 1


# --------------------------------------------------------------------- Aufrufe


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="livetest.py",
        description="Test gegen den echten brew-server",
    )
    parser.add_argument("mode", choices=["rx", "tx", "loop", "sds"])
    parser.add_argument("--to", type=int,
                        help="Ziel-ISSI fuer sds (Default: die eigene)")
    parser.add_argument("--text", default="?", help="SDS-Text")
    parser.add_argument("--wait", type=float, default=8.0,
                        help="wie lange auf die SDS-Antwort gewartet wird")
    parser.add_argument("--url", default="https://brew.oe1sxm.at")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--issi", type=int, required=True)
    parser.add_argument("--gssi", type=int, nargs="+", required=True,
                        help="GSSI(s); beim Senden wird die erste benutzt")
    parser.add_argument("--user2", help="zweiter Zugang, nur fuer loop")
    parser.add_argument("--password2")
    parser.add_argument("--issi2", type=int)
    parser.add_argument("--wav", help="zu sendende Datei, 8 kHz mono 16 Bit")
    parser.add_argument("--seconds", type=float, default=3.0,
                        help="Laenge des Testtons, wenn keine --wav angegeben")
    parser.add_argument("--callsign", default="OE4SXM",
                        help="SS-TPI-Mnemonic im GROUP_TX")
    parser.add_argument("--bits", type=int,
                        help="Laengenfeld ueberschreiben (offener Punkt 1: 282)")
    parser.add_argument("--out", default="/data",
                        help="Verzeichnis fuer die .wav-Dateien")
    parser.add_argument("--listen", type=float, default=300.0,
                        help="wie lange rx lauscht, in Sekunden")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )

    runner = {"rx": mode_rx, "tx": mode_tx, "loop": mode_loop,
              "sds": mode_sds}[args.mode]
    try:
        return asyncio.run(runner(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
