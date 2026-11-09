"""USRP-Protokoll - das, was svxlink mit `UsrpLogic` per UDP spricht.

Ein Paket ist 32 Byte Header plus Nutzlast. Sprachpakete tragen 160 Samples
(20 ms bei 8 kHz) als 16 Bit little endian, also 320 Byte -> 352 Byte gesamt.
Ein TETRA-Frame sind 60 ms, das sind exakt drei USRP-Pakete.

Header (Byteoffsets):

    0-3    "USRP"
    4-7    Sequenznummer
    8-11   memory, ungenutzt, 0
    12-15  Keyup/PTT, 1 = Traeger an
    16-19  Talkgroup, ungenutzt, 0
    20-23  Frame-Typ
    24-27  mpxid, ungenutzt, 0
    28-31  reserved, ungenutzt, 0

**Zur Endianness, weil sie hier unsauber ist.** In den echten Mitschnitten
in `usrp_protocol.txt` (DL1HRC) stehen Sequenznummer und Keyup big endian -
der Wert sitzt im *letzten* Byte des Feldes. Der Frame-Typ sitzt dagegen im
*ersten* Byte, ist also little endian. Das stammt aus `chan_usrp.h`, wo ein
Teil der Felder durch `htonl()` geht und ein Teil nicht.

`UsrpMsg.h` in svxlink hilft da nicht weiter: die Getter benutzen `ntohl`,
die Setter `htole32`, und der Konstruktor setzt `m_keyup` und `m_type` auf 0,
nachdem er sie gerade aus den Argumenten initialisiert hat. Darauf laesst
sich nichts aufbauen.

Deshalb: **gesendet** wird exakt das Layout aus den Mitschnitten, **gelesen**
wird tolerant - Keyup gilt als gesetzt, wenn irgendeines der vier Bytes
ungleich null ist, und der Typ wird aus beiden Leserichtungen bestimmt. Damit
ist es egal, welche Variante die Gegenstelle schickt. Sobald svxlink wirklich
laeuft, gegen das echte Verhalten gegenpruefen.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

MAGIC = b"USRP"
HEADER_LEN = 32

VOICE_SAMPLES = 160              # 20 ms bei 8 kHz
VOICE_BYTES = VOICE_SAMPLES * 2
VOICE_PACKET_LEN = HEADER_LEN + VOICE_BYTES
VOICE_MS = 20

# Metadatenpaket: svxlinks UsrpTlvMetaMsg endet auf einem festen
# std::array<uint8_t, 306>, das Paket ist also immer gleich lang.
# 32 Header + 1 tlv + 1 tlvlen + 3 dmrid + 4 rptid + 3 tg + 1 ts + 1 cc + 306
META_LEN = 306
META_PACKET_LEN = HEADER_LEN + 14 + META_LEN     # = 352

TYPE_VOICE = 0
TYPE_DTMF = 1
TYPE_TEXT = 2
TYPE_PING = 3
TYPE_TLV = 4

TYPE_NAMES = {
    TYPE_VOICE: "VOICE",
    TYPE_DTMF: "DTMF",
    TYPE_TEXT: "TEXT",
    TYPE_PING: "PING",
    TYPE_TLV: "TLV",
}

TLV_TAG_SET_INFO = 8


class UsrpError(ValueError):
    """Paket laesst sich nicht als USRP lesen."""


@dataclass
class UsrpPacket:
    type: int
    seq: int
    keyup: bool
    talkgroup: int
    payload: bytes

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"0x{self.type:02x}")

    @property
    def is_voice(self) -> bool:
        return self.type == TYPE_VOICE


def build(msg_type: int, seq: int, payload: bytes = b"",
          keyup: bool = False, talkgroup: int = 0) -> bytes:
    """Baut ein USRP-Paket im Layout der Referenz-Mitschnitte."""
    header = bytearray(HEADER_LEN)
    header[0:4] = MAGIC
    struct.pack_into(">I", header, 4, seq & 0xFFFFFFFF)
    struct.pack_into(">I", header, 12, 1 if keyup else 0)
    struct.pack_into(">I", header, 16, talkgroup & 0xFFFFFFFF)
    # Typ steht im ersten Byte des Feldes - siehe Modulkopf.
    struct.pack_into("<I", header, 20, msg_type & 0xFFFFFFFF)
    return bytes(header) + payload


def build_voice(seq: int, pcm: bytes, keyup: bool = True,
                talkgroup: int = 0) -> bytes:
    """Ein Sprachpaket aus genau 160 Samples (320 Byte)."""
    if len(pcm) != VOICE_BYTES:
        raise UsrpError(
            f"Sprachpaket braucht {VOICE_BYTES} Byte, bekommen {len(pcm)}"
        )
    return build(TYPE_VOICE, seq, pcm, keyup=keyup, talkgroup=talkgroup)


def build_end(seq: int, talkgroup: int = 0) -> bytes:
    """Traegerende: Sprachpaket mit Keyup=0 und Stille.

    Die Gegenstelle erkennt das Ende am Keyup-Bit, nicht am Ausbleiben von
    Paketen - deshalb muss dieses eine Paket wirklich raus.
    """
    return build(TYPE_VOICE, seq, b"\x00" * VOICE_BYTES,
                 keyup=False, talkgroup=talkgroup)


def build_metadata(seq: int, callsign: str, talkgroup: int = 0,
                   dmr_id: int = 0) -> bytes:
    """TLV_TAG_SET_INFO mit Rufzeichen - damit svxlink weiss, wer spricht.

    **Feste Laenge, das ist die Falle.** `UsrpTlvMetaMsg` in svxlinks
    UsrpMsg.h endet auf `std::array<uint8_t, 306> m_meta`, das Feld wird
    also nicht nach dem Rufzeichen abgeschnitten. Ein kuerzeres Paket
    laesst svxlinks `unpack()` scheitern - lautlos, es verwirft die
    Metadaten einfach. Im Log steht dann `Talker stop: unknown TG# 0`,
    obwohl das Audio ankommt. Das Paket muss auf 352 Byte aufgefuellt
    werden, genau so lang wie ein Sprachpaket.
    """
    # Beides ist in svxlink nur 3 Byte breit. Eine groessere Zahl liess das
    # Paket frueher mit OverflowError platzen - bevor ueberhaupt Audio
    # floss. Jetzt faellt nur die zu grosse Angabe weg.
    if not 0 <= dmr_id <= MAX_24BIT:
        dmr_id = 0
    if not 0 <= talkgroup <= MAX_24BIT:
        talkgroup = 0
    call = callsign.encode("ascii", errors="ignore")[:META_LEN - 1]
    meta = call.ljust(META_LEN, b"\x00")
    payload = (
        bytes([TLV_TAG_SET_INFO, (len(call) + 13) & 0xFF])
        + dmr_id.to_bytes(3, "big")         # m_dmrid
        + b"\x00\x00\x00\x00"               # m_rptid
        + talkgroup.to_bytes(3, "big")      # m_tg
        + b"\x00"                           # m_ts
        + b"\x01"                           # m_cc, Default in svxlink ist 1
        + meta
    )
    return build(TYPE_TEXT, seq, payload, keyup=False, talkgroup=talkgroup)


# 24 Bit - so breit sind m_dmrid und m_tg in svxlinks UsrpTlvMetaMsg.
MAX_24BIT = 0xFFFFFF

# Textnachrichten zwischen Bruecke und UsrpLogic (TETRA-Erweiterung,
# svxlink/patches/01-usrplogic-tetra.patch): "SDS:" + JSON-Objekt.
SDS_PREFIX = b"SDS:"


@dataclass
class Metadata:
    callsign: str = ""
    issi: int = 0           # steht im DMR-ID-Feld
    talkgroup: int = 0


def build_text(seq: int, text: bytes) -> bytes:
    """Freie Textnachricht (USRP_TYPE_TEXT), etwa SDS fuer die UsrpLogic."""
    return build(TYPE_TEXT, seq, text, keyup=False)


def parse_metadata(payload: bytes) -> Optional[Metadata]:
    """Liest TLV_TAG_SET_INFO: Rufzeichen, DMR-ID (= ISSI) und TG."""
    if len(payload) < 14 or payload[0] != TLV_TAG_SET_INFO:
        return None
    call = payload[14:].split(b"\x00", 1)[0]
    return Metadata(
        callsign=call.decode("ascii", errors="replace").strip(),
        issi=int.from_bytes(payload[2:5], "big"),
        talkgroup=int.from_bytes(payload[9:12], "big"),
    )


def parse(data: bytes) -> UsrpPacket:
    """Liest ein USRP-Paket. Bewusst tolerant, siehe Modulkopf."""
    if len(data) < HEADER_LEN:
        raise UsrpError(f"Paket zu kurz: {len(data)} Byte")
    if data[0:4] != MAGIC:
        raise UsrpError(f"kein USRP-Paket: {data[0:4]!r}")

    seq = struct.unpack_from(">I", data, 4)[0]

    # Keyup: egal ob der Wert im ersten oder letzten Byte steht.
    keyup = any(data[12:16])

    talkgroup = struct.unpack_from(">I", data, 16)[0]

    # Typ: erst little endian (Byte 20), sonst big endian (Byte 23).
    type_field = data[20:24]
    msg_type = type_field[0] or int.from_bytes(type_field, "big")

    return UsrpPacket(
        type=msg_type,
        seq=seq,
        keyup=keyup,
        talkgroup=talkgroup,
        payload=data[HEADER_LEN:],
    )


def split_pcm(pcm: bytes) -> list[bytes]:
    """Teilt PCM in 160-Sample-Haeppchen. Der Rest wird mit Stille aufgefuellt."""
    out = []
    for offset in range(0, len(pcm), VOICE_BYTES):
        chunk = pcm[offset:offset + VOICE_BYTES]
        if len(chunk) < VOICE_BYTES:
            chunk = chunk + b"\x00" * (VOICE_BYTES - len(chunk))
        out.append(chunk)
    return out
