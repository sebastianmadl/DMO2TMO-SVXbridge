#!/usr/bin/env python3
"""Codec-Test. Braucht libtetraacelp.so, laeuft also nur im Container.

    docker build -t dmo2tmo-svxbridge .
    docker run --rm dmo2tmo-svxbridge python3 codectest.py

Ohne .so beendet sich das Script mit Hinweis und Exit 0 - selftest.py und
smoketest.py bleiben davon unberuehrt und laufen auch ohne Codec.
"""

from __future__ import annotations

import asyncio
import math
import socket
import sys

from app.audio import acelp
from app.audio import base
from app.audio import usrp
from app.brew import protocol as p

failures = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FEHL  {name} {detail}")
        failures.append(name)


def speech_like(samples: int, seed: int = 1) -> bytes:
    """Stimmhaftes Testsignal: Grundton mit Obertoenen und Huellkurve.

    Ein reiner Sinus ist fuer einen Sprachcodec ein unfairer Test - ACELP
    modelliert Anregung plus Formanten, nicht Einzeltoene.
    """
    out = bytearray()
    for i in range(samples):
        t = i / 8000.0
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 3.0 * t)
        value = (
            0.60 * math.sin(2 * math.pi * 130.0 * t)
            + 0.25 * math.sin(2 * math.pi * 390.0 * t)
            + 0.10 * math.sin(2 * math.pi * 910.0 * t)
            + 0.05 * math.sin(2 * math.pi * 1700.0 * t)
        )
        sample = int(max(-1.0, min(1.0, value * env)) * 8000)
        out += int(sample).to_bytes(2, "little", signed=True)
    return bytes(out)


def to_ints(pcm: bytes) -> list[int]:
    return [
        int.from_bytes(pcm[i:i + 2], "little", signed=True)
        for i in range(0, len(pcm), 2)
    ]


def correlation(a: list[int], b: list[int]) -> float:
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    den_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    den_b = math.sqrt(sum((y - mean_b) ** 2 for y in b))
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


def main() -> int:
    found = acelp.find_libraries()
    if found is None:
        print("libtetraacelp_enc.so / _dec.so nicht gefunden - Test uebersprungen.")
        print("Bauen mit: codec/build.sh   (oder im Docker-Image)")
        return 0

    print(f"Codec: {found[0].parent}")
    codec = acelp.AcelpCodec()

    print("Grunddaten:")
    check("240 Samples je Subframe", acelp.SUBFRAME_SAMPLES == 240)
    check("137 Bit je Subframe", acelp.SUBFRAME_BITS == 137)

    print("Encode:")
    pcm = speech_like(acelp.SUBFRAME_SAMPLES)
    bits = codec.encode(pcm)
    check("Ergebnis passt in 137 Bit", 0 <= bits < (1 << 137), f"ist {bits.bit_length()} Bit")
    check("nicht alles null", bits != 0)

    print("Bitpackung Codec <-> Brew-Frame (das ist die heikle Stelle):")
    codec.reset()
    pcm2 = speech_like(acelp.FRAME_SAMPLES)
    sub1, sub2 = codec.encode_frame(pcm2)
    frame = p.build_traffic_frame(sub1, sub2)
    check("Frame = 36 Byte", len(frame) == p.TRAFFIC_FRAME_BYTES, f"ist {len(frame)}")
    _, back1, back2 = p.split_traffic_frame(frame)
    check("Subframe 1 bitgenau zurueck", back1 == sub1)
    check("Subframe 2 bitgenau zurueck", back2 == sub2)

    print("Decode:")
    codec.reset()
    out = codec.decode_frame(sub1, sub2)
    check("480 Samples zurueck", len(out) == acelp.FRAME_SAMPLES * 2, f"ist {len(out) // 2}")
    samples = to_ints(out)
    energy = sum(abs(s) for s in samples) / len(samples)
    check("Ausgabe ist nicht still", energy > 50, f"mittlerer Betrag {energy:.1f}")

    print("Qualitaet (encode -> decode gegen das Original):")
    codec.reset()
    original = speech_like(acelp.FRAME_SAMPLES * 8)
    encoded = []
    step = acelp.SUBFRAME_SAMPLES * 2
    for i in range(0, len(original), step):
        encoded.append(codec.encode(original[i:i + step]))
    codec.reset()
    decoded = b"".join(codec.decode(v) for v in encoded)

    # Der Decoder gibt verzoegert aus (Analysefenster + Vorlauf). Ohne
    # Laufzeitausgleich vergleicht man Phasen statt Wellenformen und bekommt
    # bei einem periodischen Signal beliebige Vorzeichen heraus.
    orig = to_ints(original)
    dec = to_ints(decoded)
    skip = acelp.SUBFRAME_SAMPLES * 2      # Einschwingen weg
    window = 2000
    best_corr, best_lag = -1.0, 0
    for lag in range(0, 501):
        if skip + lag + window > len(dec):
            break
        value = correlation(orig[skip:skip + window],
                            dec[skip + lag:skip + lag + window])
        if value > best_corr:
            best_corr, best_lag = value, lag
    print(f"        (Korrelation {best_corr:.3f} bei {best_lag} Samples "
          f"= {best_lag / 8.0:.1f} ms Laufzeit)")
    check("Korrelation mit dem Original > 0.5", best_corr > 0.5,
          f"ist {best_corr:.3f}")

    print("Wiederholbarkeit:")
    codec.reset()
    again = codec.encode(pcm)
    check("gleiches PCM -> gleiche Bits", again == bits)

    # Genau dafuer sind Encoder und Decoder zwei getrennte .so-Dateien:
    # last_ener_pit/last_ener_cod sind in scod_tet.c und sdec_tet.c beide
    # definiert. Lagen sie in einer gemeinsamen Bibliothek, wuerden sich die
    # beiden Richtungen den Zustand der Gain-Quantisierung ueberschreiben und
    # dieser Test ginge schief.
    print("Encoder und Decoder stoeren einander nicht:")
    chunks = [original[i:i + step] for i in range(0, len(original), step)]

    codec.reset()
    ref_bits = [codec.encode(c) for c in chunks]
    codec.reset()
    ref_pcm = [codec.decode(v) for v in ref_bits]

    codec.reset()
    mixed_bits, mixed_pcm = [], []
    for chunk, value in zip(chunks, ref_bits):
        mixed_bits.append(codec.encode(chunk))     # beide Richtungen
        mixed_pcm.append(codec.decode(value))      # dicht verschraenkt
    check("Encode bleibt vom Decode unberuehrt", mixed_bits == ref_bits)
    check("Decode bleibt vom Encode unberuehrt", mixed_pcm == ref_pcm)

    print("Zwei Stroeme gleichzeitig (zwei Reflektoren):")
    zweites = speech_like(len(original) // 2, seed=7)
    chunks2 = [zweites[i:i + step] for i in range(0, len(zweites), step)]
    n = min(len(chunks), len(chunks2))

    a, b = acelp.AcelpCodec(), acelp.AcelpCodec()
    solo_bits = [a.encode(c) for c in chunks[:n]]
    solo_pcm = [b.decode(v) for v in solo_bits]

    # Genau so laeuft es im Betrieb: zwei Reflektoren, Frame um Frame
    # abwechselnd durch den Codec.
    a1, a2 = acelp.AcelpCodec(), acelp.AcelpCodec()
    d1, d2 = acelp.AcelpCodec(), acelp.AcelpCodec()
    misch_bits, misch_pcm = [], []
    for eins, zwei, wert in zip(chunks[:n], chunks2[:n], solo_bits):
        misch_bits.append(a1.encode(eins))
        a2.encode(zwei)
        misch_pcm.append(d1.decode(wert))
        d2.decode(a2.encode(zwei))
    check("zweiter Encoder stoert den ersten nicht", misch_bits == solo_bits)
    check("zweiter Decoder stoert den ersten nicht", misch_pcm == solo_pcm)

    # Gegenprobe: mit geteilter Bibliothek muss es kaputtgehen - sonst
    # prueft der Test oben nichts.
    g1, g2 = (acelp.AcelpCodec(private=False), acelp.AcelpCodec(private=False))
    geteilt = []
    for eins, wert in zip(chunks2[:n], solo_bits):
        geteilt.append(g1.decode(wert))
        g2.encode(eins)
        g2.decode(g2.encode(eins))
    check("und mit geteilter Bibliothek geht es schief", geteilt != solo_pcm)

    asyncio.run(test_usrp_sink(codec))
    asyncio.run(test_svx_to_tetra(codec))

    print()
    if failures:
        print(f"{len(failures)} Test(s) fehlgeschlagen: {', '.join(failures)}")
        return 1
    print("Codec-Test ok.")
    return 0


async def test_usrp_sink(codec) -> None:
    """TETRA -> svxlink komplett, gegen einen simulierten svxlink-Empfaenger."""
    print("UsrpSink (TETRA -> svxlink):")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    port = sock.getsockname()[1]

    class FakeReflector:
        name = "R1"
        usrp_port = port

    sink = base.UsrpSink(FakeReflector())

    class FakeCall:
        identifier = "test"
        gssi = 2321
        source = 2325001
        callsign = "OE1SXM"
        frames = 0
        sink_state: dict = {}

    call = FakeCall()

    # Zwei TETRA-Frames = 120 ms = 6 USRP-Pakete
    pcm = speech_like(acelp.FRAME_SAMPLES * 2)
    codec.reset()
    half = acelp.FRAME_SAMPLES * 2
    frames = []
    for chunk in (pcm[:half], pcm[half:]):
        sub1, sub2 = codec.encode_frame(chunk)
        frames.append(p.build_traffic_frame(sub1, sub2))

    await sink.start(call)
    for data in frames:
        await sink.frame(call, data)
    await sink.stop(call)
    await asyncio.sleep(0.45)          # den Puffer leerlaufen lassen

    packets = []
    while True:
        try:
            packets.append(usrp.parse(sock.recv(4096)))
        except BlockingIOError:
            break
        except usrp.UsrpError as exc:
            check("alle Pakete lesbar", False, str(exc))
            break
    sock.close()
    await sink.close()

    check("Pakete angekommen", bool(packets), f"sind {len(packets)}")
    if not packets:
        return

    meta = [x for x in packets if x.type == usrp.TYPE_TEXT]
    voice = [x for x in packets if x.type == usrp.TYPE_VOICE]
    check("Metadaten vorweg", bool(meta) and packets[0].type == usrp.TYPE_TEXT)
    if meta:
        check("Rufzeichen in den Metadaten", b"OE1SXM" in meta[0].payload,
              meta[0].payload[:32].hex())

    # Das Traegerende ist laut Protokoll ebenfalls ein VOICE-Paket, nur mit
    # Keyup=0 - also 6 mit Audio plus eines zum Abschalten.
    audio = [x for x in voice if x.keyup]
    check("6 Sprachpakete (2 Frames a 60 ms)", len(audio) == 6,
          f"sind {len(audio)}")
    check("genau ein Abschaltpaket", len(voice) - len(audio) == 1,
          f"sind {len(voice) - len(audio)}")
    check("alle Sprachpakete 320 Byte",
          all(len(x.payload) == usrp.VOICE_BYTES for x in voice))
    if voice:
        check("letztes Paket schaltet ab (Keyup=0)", voice[-1].keyup is False)
        check("Sequenznummern steigen",
              all(b.seq > a.seq for a, b in zip(packets, packets[1:])))

    # Das Audio muss den Weg ueberstanden haben: 6 Pakete = 960 Samples.
    got = b"".join(x.payload for x in audio)
    if got:
        orig, out = to_ints(pcm), to_ints(got)
        skip, window = 120, 600
        best = -1.0
        for lag in range(0, 201):
            if skip + lag + window > len(out):
                break
            best = max(best, correlation(orig[skip:skip + window],
                                         out[skip + lag:skip + lag + window]))
        print(f"        (Korrelation durch die ganze Kette: {best:.3f})")
        check("Audio kommt bei svxlink an", best > 0.5, f"ist {best:.3f}")


class FakeClient:
    """Statt zum brew-server zu senden, wird hier nur mitgeschrieben."""

    def __init__(self):
        self.calls = []
        self.frames = []
        self.idles = []

    def group_tx(self, gssi, source=None, callsign="", priority=0,
                 identifier=None):
        import uuid as _uuid
        identifier = identifier or _uuid.uuid4()
        self.calls.append({"gssi": gssi, "callsign": callsign,
                           "identifier": identifier})
        return identifier

    def traffic(self, identifier, data):
        self.frames.append(data)

    def group_idle(self, identifier, cause=0):
        self.idles.append(identifier)

    def send_sds(self, *a, **kw):
        pass

    def query_subscribers(self, *a, **kw):
        pass


async def test_svx_to_tetra(codec) -> None:
    """svxlink -> Bruecke -> TETRA, komplett durch den Router."""
    print("SVX -> TETRA (die neue Richtung):")

    from app import config as cfgmod
    from app.routing import Router
    from app.state import AppState

    # Freien Port besorgen und gleich wieder hergeben.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    listen_port = probe.getsockname()[1]
    probe.close()

    cfg = cfgmod.from_dict({
        "brew": {"issi": 2320997, "username": "test"},
        # Nachlauf hier aus, damit die Frame-Zahl exakt aufgeht - er wird
        # weiter unten eigens geprueft.
        "audio": {"backend": "usrp", "gain_svx_db": 0.0,
                  "gain_tetra_db": 0.0, "hang_ms": 0},
        "reflectors": [{
            "name": "R1", "host": "reflector.example", "callsign": "OE4SXM-HS",
            "auth_key": "x", "usrp_port": listen_port + 1,
            "usrp_listen_port": listen_port, "default_tg": 2321,
        }],
        "routes": [{"gssi": 2321, "reflector": "R1", "tg": 2321,
                    "type": "fixed"}],
    })

    router = Router(cfg, AppState())
    client = FakeClient()
    router.attach(client)
    await router.start_sources()
    check("Empfaenger gestartet", "R1" in router.sources)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = ("127.0.0.1", listen_port)

    # So spricht svxlink: erst Metadaten, dann Sprachpakete, dann abschalten.
    pcm = speech_like(usrp.VOICE_SAMPLES * 12)      # 12 Pakete = 240 ms
    sock.sendto(usrp.build_metadata(1, "OE1ABC"), target)
    await asyncio.sleep(0.05)
    for i, chunk in enumerate(usrp.split_pcm(pcm)):
        sock.sendto(usrp.build_voice(i + 2, chunk, keyup=True), target)
        await asyncio.sleep(0.01)
    laufend = router.status()["active_calls"]
    check("laufender Ruf nennt Reflektor und TG",
          len(laufend) == 1 and laufend[0]["reflector"] == "R1"
          and laufend[0]["tg"] == 2321, str(laufend))

    sock.sendto(usrp.build_end(99), target)
    await asyncio.sleep(0.3)
    sock.close()

    check("Gruppenruf gestartet", len(client.calls) == 1,
          f"sind {len(client.calls)}")

    if client.calls:
        check("auf die richtige GSSI", client.calls[0]["gssi"] == 2321,
              str(client.calls[0]["gssi"]))
        check("Rufzeichen aus den Metadaten uebernommen",
              client.calls[0]["callsign"] == "OE1ABC",
              repr(client.calls[0]["callsign"]))

    # 12 USRP-Pakete a 20 ms = 240 ms = 4 TETRA-Frames
    check("4 TETRA-Frames erzeugt", len(client.frames) == 4,
          f"sind {len(client.frames)}")
    erste_frames = list(client.frames)
    check("jeder Frame 36 Byte",
          all(len(f) == p.TRAFFIC_FRAME_BYTES for f in client.frames))
    check("Gruppenruf beendet", len(client.idles) == 1)
    check("Zaehler calls_tx", router.state.counters["calls_tx"] == 1)
    check("Zaehler frames_tx", router.state.counters["frames_tx"] == 4,
          str(router.state.counters["frames_tx"]))
    check("Floor wieder frei", not router.floors, str(router.floors))
    check("Last Heard als svx->tetra",
          bool(router.state.last_heard)
          and router.state.last_heard[0].direction == "svx->tetra")

    # Nachlauf: derselbe Router, nur mit gesetzter Nachlaufzeit. Dass das
    # ohne Neustart wirkt, ist Absicht - die Quelle fragt den Wert bei
    # jedem Rufende neu ab.
    print("Nachlauf durch den ganzen Router:")
    cfg.audio.hang_ms = 240
    client.calls.clear()
    client.frames.clear()
    client.idles.clear()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i, chunk in enumerate(usrp.split_pcm(
            speech_like(usrp.VOICE_SAMPLES * 3))):
        sock.sendto(usrp.build_voice(i + 200, chunk, keyup=True), target)
        await asyncio.sleep(0.01)
    sock.sendto(usrp.build_end(299), target)

    await asyncio.sleep(0.12)
    check("Ruf laeuft im Nachlauf weiter", len(client.idles) == 0,
          f"schon {len(client.idles)} beendet")
    nach_ptt = len(client.frames)
    await asyncio.sleep(0.35)
    sock.close()
    check("Stille wird nachgeschoben", len(client.frames) > nach_ptt,
          f"{nach_ptt} -> {len(client.frames)}")
    check("und dann ist Schluss", len(client.idles) == 1,
          str(len(client.idles)))
    check("Floor auch danach frei", not router.floors, str(router.floors))

    # Und das Audio muss den Weg ueberstanden haben.
    if erste_frames:
        codec.reset()
        out = b""
        for data in erste_frames:
            _, sub1, sub2 = p.split_traffic_frame(data)
            out += codec.decode_frame(sub1, sub2)
        orig, got = to_ints(pcm), to_ints(out)
        skip, window = 120, 900
        best = -1.0
        for lag in range(0, 201):
            if skip + lag + window > len(got):
                break
            best = max(best, correlation(orig[skip:skip + window],
                                         got[skip + lag:skip + lag + window]))
        print(f"        (Korrelation svxlink -> TETRA: {best:.3f})")
        check("Audio kommt auf der TETRA-Seite an", best > 0.5, f"ist {best:.3f}")

    await router.stop_sources()


if __name__ == "__main__":
    sys.exit(main())
