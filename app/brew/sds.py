"""SDS Type 4 - Textnachrichten kodieren/dekodieren.

Brew transportiert im FRAME_TYPE_SDS_TRANSFER die komplette SDS-Type-4-PDU,
das erste Oktett ist also der Protocol Identifier (ETSI TS 100 392-2,
Tabelle 29.21).

Uns interessieren zwei PIDs:

  0x02  Simple Text Messaging
        Oktett 0: PID
        Oktett 1: Bit 7 = Timestamp-Flag, Bits 6-0 = Text Coding Scheme
        Oktett 2+: Text

  0x09  Text Messaging (mit SDS-TL)
        Oktett 0: PID
        Oktett 1: Message Type / Delivery-Report-Request / Flags
        Oktett 2: Message Reference
        Oktett 3: Bit 7 = Timestamp-Flag, Bits 6-0 = Text Coding Scheme
        Oktett 4+: Text

Zum Senden nehmen wir Simple Text - das versteht jedes Geraet und wir
brauchen keine Delivery Reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .protocol import (
    CODING_SCHEME_7BIT,
    CODING_SCHEME_8BIT,
    pack_7bit,
    unpack_7bit,
)

PID_SIMPLE_TEXT = 0x02
PID_TEXT_MESSAGING = 0x09
# Das ist, was TetraLink schickt (am 2026-09-08 mitgeschnitten):
#   82 04 07 01 68 61 6c 6c 6f   ->  PID, Typ/Flags, Referenz, Kodierung, "hallo"
# Aufbau identisch zu PID 0x09, nur die Kennung ist eine andere. Echte
# Funkgeraete verwenden ueblicherweise ebenfalls 0x82.
PID_SDS_TL_TEXT = 0x82
# Typ/Flags wie in TetraLinks eigener PDU. Bits: Nachrichtentyp
# SDS-TRANSFER, Zustellbericht angefordert.
SDS_TL_FLAGS = 0x04

# Coding Schemes laut EN 300 392-2, 29.5.4
SCHEME_7BIT = 0x00
SCHEME_8BIT = 0x01
SCHEME_UCS2 = 0x1A


# Fuenf Betriebsarten. Die ersten drei sind ETSI: PID, dann ein Oktett mit
# Timestamp-Flag und Kodierung, dann der Text. Die letzten beiden lassen das
# Kodierungsbyte **weg** - TetraLink ueberspringt beim Lesen genau ein Byte
# (den PID) und liest alles Weitere als UTF-16BE, verschluckt unser
# Kodierungsbyte also als Textzeichen. Am 2026-09-08 zeichengenau
# nachgewiesen (siehe CLAUDE.md).
MODE_7BIT = "7bit"
MODE_8BIT = "8bit"
MODE_UCS2 = "ucs2"
MODE_RAW16 = "raw16"          # 02 + UTF-16BE, ohne Kodierungsbyte
MODE_ASCII = "ascii"          # 02 + ASCII, ohne Kodierungsbyte
MODE_SDSTL = "sdstl"          # 82 + SDS-TL-Kopf, so wie TetraLink selbst
MODES = (MODE_7BIT, MODE_8BIT, MODE_UCS2, MODE_RAW16, MODE_ASCII, MODE_SDSTL)


@dataclass
class SdsText:
    text: str
    pid: int
    coding_scheme: int
    message_reference: Optional[int] = None
    mode: str = ""


def decode_text(pdu: bytes) -> Optional[SdsText]:
    """Holt den Klartext aus einer SDS-Type-4-PDU. None wenn kein Text."""
    if len(pdu) < 2:
        return None
    pid = pdu[0]

    if pid == PID_SIMPLE_TEXT:
        # Erst pruefen, ob ueberhaupt ein Kodierungsbyte da ist. Fehlt es,
        # waere alles um ein Byte verschoben und der Text unlesbar.
        roh = _als_rohtext(pdu[1:])
        if roh is not None:
            text, art = roh
            return SdsText(text=text, pid=pid, coding_scheme=-1, mode=art)
        header = pdu[1]
        payload = pdu[2:]
        reference = None
    elif pid in (PID_TEXT_MESSAGING, PID_SDS_TL_TEXT):
        if len(pdu) < 4:
            return None
        reference = pdu[2]
        header = pdu[3]
        payload = pdu[4:]
    else:
        return None

    scheme = header & 0x7F
    timestamped = bool(header & 0x80)
    if timestamped:
        # Zeitstempel sind 3 Oktett, danach geht der Text weiter.
        payload = payload[3:]

    text = _decode_payload(payload, scheme)
    art = MODE_SDSTL if pid == PID_SDS_TL_TEXT else scheme_name(scheme)
    return SdsText(text=text, pid=pid, coding_scheme=scheme,
                   message_reference=reference, mode=art)


def sieht_nach_ucs2_aus(payload: bytes) -> bool:
    """Jedes zweite Byte 0x00 und lesbarer Text dazwischen?

    Manche Geraete schreiben UCS-2 hinein und deklarieren trotzdem 8 Bit.
    Ohne diese Pruefung stuende in der Oberflaeche Zeichensalat, obwohl der
    Text klar erkennbar ist.
    """
    if len(payload) < 4 or len(payload) % 2:
        return False
    if any(payload[i] != 0 for i in range(0, len(payload), 2)):
        return False
    return all(0x20 <= payload[i] < 0x7F or payload[i] in (0x0a, 0x0d)
               for i in range(1, len(payload), 2))


def _druckbar(b: int) -> bool:
    return 0x20 <= b < 0x7F or b in (0x0a, 0x0d, 0x09)


def _als_rohtext(rest: bytes) -> Optional[tuple[str, str]]:
    """Steckt hinter dem PID direkt Text, ohne Kodierungsbyte?

    Nur bei eindeutigen Faellen - sonst wuerde eine normale Nachricht hier
    landen. Unsere eigenen ETSI-Formate haben an dieser Stelle 0x00, 0x01
    oder 0x1a und fallen damit durch beide Pruefungen.
    """
    # 02 + UTF-16BE: gerade Laenge, jedes erste Byte 0x00, Rest druckbar.
    if len(rest) >= 4 and not len(rest) % 2:
        if (all(rest[i] == 0 for i in range(0, len(rest), 2))
                and all(_druckbar(rest[i]) for i in range(1, len(rest), 2))):
            return rest.decode("utf-16-be", errors="replace"), MODE_RAW16

    # 02 + ASCII: alles druckbar, und das erste Byte kann gar kein
    # Kodierungsbyte sein (die gehen nur bis 0x1a).
    if len(rest) >= 2 and rest[0] >= 0x20 and all(_druckbar(b) for b in rest):
        return rest.decode("ascii", errors="replace"), MODE_ASCII

    return None


def _decode_payload(payload: bytes, scheme: int) -> str:
    if scheme == SCHEME_7BIT:
        return unpack_7bit(payload, (len(payload) * 8) // 7).rstrip("\x00")
    if scheme == SCHEME_UCS2 or sieht_nach_ucs2_aus(payload):
        try:
            return payload.decode("utf-16-be", errors="replace").rstrip("\x00")
        except Exception:
            return ""
    return payload.decode("iso-8859-1", errors="replace").rstrip("\x00")


def encode_text(text: str, scheme: int = SCHEME_8BIT) -> bytes:
    """Baut eine Simple-Text-PDU (PID 0x02) ohne Zeitstempel."""
    if scheme == SCHEME_7BIT:
        payload = pack_7bit(text)
    elif scheme == SCHEME_UCS2:
        payload = text.encode("utf-16-be")
    else:
        scheme = SCHEME_8BIT
        payload = text.encode("iso-8859-1", errors="replace")
    return bytes([PID_SIMPLE_TEXT, scheme & 0x7F]) + payload


def build_text_pdu(text: str, mode: str = MODE_8BIT) -> bytes:
    """Text-PDU in einer der fuenf Betriebsarten.

    `raw16` und `ascii` lassen das Kodierungsbyte weg. Das ist bewusst
    nicht ETSI-konform - es ist das Format, das TetraLink liest.
    """
    mode = mode_from_name(mode)
    if mode == MODE_SDSTL:
        return build_sdstl_pdu(text)
    if mode == MODE_RAW16:
        return bytes([PID_SIMPLE_TEXT]) + text.encode("utf-16-be")
    if mode == MODE_ASCII:
        return bytes([PID_SIMPLE_TEXT]) + text.encode("ascii", errors="replace")
    return encode_text(text, scheme_from_name(mode))


_referenz = 0


def build_sdstl_pdu(text: str, scheme: int = SCHEME_8BIT) -> bytes:
    """PID 0x82 mit SDS-TL-Kopf - Byte fuer Byte wie TetraLink es schickt.

    Die Message Reference laeuft mit, damit ein Empfaenger Wiederholungen
    auseinanderhalten kann; TetraLink macht das genauso.
    """
    global _referenz
    _referenz = (_referenz % 255) + 1
    if scheme == SCHEME_UCS2:
        payload = text.encode("utf-16-be")
    elif scheme == SCHEME_7BIT:
        payload = pack_7bit(text)
    else:
        scheme = SCHEME_8BIT
        payload = text.encode("iso-8859-1", errors="replace")
    return bytes([PID_SDS_TL_TEXT, SDS_TL_FLAGS, _referenz,
                  scheme & 0x7F]) + payload


def mode_from_name(name: str) -> str:
    key = (name or "").strip().lower()
    if key in ("sdstl", "sds-tl", "0x82", "82", "tetralink"):
        return MODE_SDSTL
    if key in ("raw16", "16bit", "16-bit", "utf16-roh"):
        return MODE_RAW16
    if key in ("ascii", "ascii-roh", "8bit-roh"):
        return MODE_ASCII
    if key in ("7bit", "7-bit", "gsm7"):
        return MODE_7BIT
    if key in ("ucs2", "utf16", "utf-16"):
        return MODE_UCS2
    return MODE_8BIT


SCHEME_NAMES = {SCHEME_7BIT: "7bit", SCHEME_8BIT: "8bit", SCHEME_UCS2: "ucs2"}


def scheme_name(scheme: int) -> str:
    return SCHEME_NAMES.get(scheme, f"0x{scheme:02x}")


def scheme_from_name(name: str) -> int:
    key = (name or "").strip().lower()
    if key in ("7bit", "7-bit", "gsm7"):
        return SCHEME_7BIT
    if key in ("ucs2", "utf16", "utf-16"):
        return SCHEME_UCS2
    return SCHEME_8BIT


__all__ = [
    "SdsText",
    "decode_text",
    "encode_text",
    "scheme_from_name",
    "SCHEME_7BIT",
    "SCHEME_8BIT",
    "SCHEME_UCS2",
    "PID_SIMPLE_TEXT",
    "PID_TEXT_MESSAGING",
    "CODING_SCHEME_7BIT",
    "CODING_SCHEME_8BIT",
]
