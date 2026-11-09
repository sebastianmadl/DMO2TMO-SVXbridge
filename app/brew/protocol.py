"""
Brew (TETRA Homebrew Protocol) message encoding/decoding.

Referenz: https://wiki.tetrapack.online/tetra/specifications/brew/

Alle Strukturen sind __attribute__((packed)) und Little-Endian.
"""

from __future__ import annotations

import json
import struct
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------- Konstanten

CLASS_SUBSCRIBER = 0xF0
CLASS_CALL = 0xF1
CLASS_FRAME = 0xF2
CLASS_ERROR = 0xF3
CLASS_SERVICE = 0xF4

# 0xF0 - Subscriber control
SUB_DEREGISTER = 0
SUB_REGISTER = 1
SUB_REREGISTER = 2
SUB_AFFILIATE = 8
SUB_DEAFFILIATE = 9

SUB_NAMES = {
    SUB_DEREGISTER: "DEREGISTER",
    SUB_REGISTER: "REGISTER",
    SUB_REREGISTER: "REREGISTER",
    SUB_AFFILIATE: "AFFILIATE",
    SUB_DEAFFILIATE: "DEAFFILIATE",
}

# 0xF1 - Call control
CALL_GROUP_TX = 2
CALL_GROUP_IDLE = 3
CALL_SETUP_REQUEST = 4
CALL_SETUP_ACCEPT = 5
CALL_SETUP_REJECT = 6
CALL_ALERT = 7
CALL_CONNECT_REQUEST = 8
CALL_CONNECT_CONFIRM = 9
CALL_RELEASE = 10
CALL_SHORT_TRANSFER = 11
CALL_SIMPLEX_GRANTED = 12
CALL_SIMPLEX_IDLE = 13
CALL_PDP_REQUEST = 14
CALL_PDP_ACCEPT = 15
CALL_PDP_REJECT = 16
CALL_PDP_RELEASE = 17

CALL_NAMES = {
    CALL_GROUP_TX: "GROUP_TX",
    CALL_GROUP_IDLE: "GROUP_IDLE",
    CALL_SETUP_REQUEST: "SETUP_REQUEST",
    CALL_SETUP_ACCEPT: "SETUP_ACCEPT",
    CALL_SETUP_REJECT: "SETUP_REJECT",
    CALL_ALERT: "CALL_ALERT",
    CALL_CONNECT_REQUEST: "CONNECT_REQUEST",
    CALL_CONNECT_CONFIRM: "CONNECT_CONFIRM",
    CALL_RELEASE: "CALL_RELEASE",
    CALL_SHORT_TRANSFER: "SHORT_TRANSFER",
    CALL_SIMPLEX_GRANTED: "SIMPLEX_GRANTED",
    CALL_SIMPLEX_IDLE: "SIMPLEX_IDLE",
    CALL_PDP_REQUEST: "PDP_REQUEST",
    CALL_PDP_ACCEPT: "PDP_ACCEPT",
    CALL_PDP_REJECT: "PDP_REJECT",
    CALL_PDP_RELEASE: "PDP_RELEASE",
}

# 0xF2 - Voice and data frames
FRAME_TRAFFIC_CHANNEL = 0
FRAME_SDS_TRANSFER = 1
FRAME_SDS_REPORT = 2
FRAME_DTMF_DATA = 3
FRAME_PACKET_DATA = 4

FRAME_NAMES = {
    FRAME_TRAFFIC_CHANNEL: "TRAFFIC_CHANNEL",
    FRAME_SDS_TRANSFER: "SDS_TRANSFER",
    FRAME_SDS_REPORT: "SDS_REPORT",
    FRAME_DTMF_DATA: "DTMF_DATA",
    FRAME_PACKET_DATA: "PACKET_DATA",
}

# 0xF3 - Errors
ERROR_MALFORMED = 0
ERROR_RESTRICTED = 1

# 0xF4 - Service messages
SERVICE_QUERY_SUBSCRIBERS = 1
SERVICE_SUBSCRIBER_PROFILES = 2

# Ein Traffic-Frame traegt 60 ms Audio = 2 ACELP-Subframes a 137 Bit.
ACELP_SUBFRAME_BITS = 137
TRAFFIC_FRAME_BYTES = 36
TRAFFIC_FRAME_MS = 60

# offsetof(struct BrewCallControlData, data) == 1 + 1 + 16
CALL_HEADER_LEN = 18
# offsetof(struct BrewFrameData, data) == 1 + 1 + 16 + 2
FRAME_HEADER_LEN = 20


class BrewProtocolError(ValueError):
    """Nachricht laesst sich nicht parsen."""


# ---------------------------------------------------------------- Datentypen


@dataclass
class SubscriberMessage:
    type: int
    number: int
    time: int
    fraction: int
    groups: list[int] = field(default_factory=list)

    @property
    def type_name(self) -> str:
        return SUB_NAMES.get(self.type, f"0x{self.type:02x}")


@dataclass
class GroupTransmission:
    source: int
    destination: int
    priority: int
    access: int
    service: int
    mnemonic: bytes = b""

    @property
    def mnemonic_text(self) -> str:
        return decode_mnemonic(self.mnemonic)


@dataclass
class ShortData:
    source: int
    destination: int
    number: str = ""


@dataclass
class CallMessage:
    type: int
    identifier: _uuid.UUID
    cause: Optional[int] = None
    group: Optional[GroupTransmission] = None
    short: Optional[ShortData] = None
    raw: bytes = b""

    @property
    def type_name(self) -> str:
        return CALL_NAMES.get(self.type, f"0x{self.type:02x}")


@dataclass
class FrameMessage:
    type: int
    identifier: _uuid.UUID
    bits: int
    data: bytes

    @property
    def type_name(self) -> str:
        return FRAME_NAMES.get(self.type, f"0x{self.type:02x}")


@dataclass
class ErrorMessage:
    type: int
    data: bytes


@dataclass
class ServiceMessage:
    type: int
    payload: object


# ---------------------------------------------------------------- Hilfsmittel


def _now_parts() -> tuple[int, int]:
    now = time.time()
    seconds = int(now)
    nanos = int((now - seconds) * 1_000_000_000)
    return seconds, nanos


def get_bits(data: bytes, offset: int, count: int) -> int:
    """Liest 'count' Bits ab Bitposition 'offset' (MSB first) als int."""
    value = 0
    for i in range(count):
        bit_index = offset + i
        byte = data[bit_index >> 3]
        bit = (byte >> (7 - (bit_index & 7))) & 1
        value = (value << 1) | bit
    return value


def set_bits(buf: bytearray, offset: int, count: int, value: int) -> None:
    """Schreibt 'count' Bits von 'value' ab Bitposition 'offset' (MSB first)."""
    for i in range(count):
        bit = (value >> (count - 1 - i)) & 1
        bit_index = offset + i
        byte_index = bit_index >> 3
        mask = 1 << (7 - (bit_index & 7))
        if bit:
            buf[byte_index] |= mask
        else:
            buf[byte_index] &= ~mask & 0xFF


# ---------------------------------------------------------------- Mnemonic
#
# ETSI EN 300 392-9, 8.4.2, Tabelle 17:
#   Oktett 0: Bit 7 = 0, Bits 6-0 = Text Coding Scheme
#   Oktett 1: Laenge des folgenden Textes in Bit
#   Oktett 2+: Text
#
# Coding Scheme 0x00 = 7-Bit-Alphabet (gepackt), 0x01 = 8 Bit ISO 8859-1.
# Wir senden per Default 8 Bit, das nehmen die meisten Geraete an.

MNEMONIC_FIELD_LEN = 34
CODING_SCHEME_7BIT = 0x00
CODING_SCHEME_8BIT = 0x01


def encode_mnemonic(text: str, scheme: int = CODING_SCHEME_8BIT) -> bytes:
    """Baut das 34-Byte-Mnemonic-Feld fuer GROUP_TX (SS-TPI)."""
    if not text:
        return b"\x00" * MNEMONIC_FIELD_LEN
    if scheme == CODING_SCHEME_7BIT:
        payload = pack_7bit(text)
        bits = len(text) * 7
    else:
        payload = text.encode("iso-8859-1", errors="replace")
        bits = len(payload) * 8
    payload = payload[: MNEMONIC_FIELD_LEN - 2]
    bits = min(bits, len(payload) * 8)
    out = bytearray(MNEMONIC_FIELD_LEN)
    out[0] = scheme & 0x7F
    out[1] = bits & 0xFF
    out[2 : 2 + len(payload)] = payload
    return bytes(out)


def decode_mnemonic(data: bytes) -> str:
    if not data or len(data) < 2:
        return ""
    scheme = data[0] & 0x7F
    bits = data[1]
    if bits == 0:
        return ""
    payload = data[2:]
    if scheme == CODING_SCHEME_7BIT:
        return unpack_7bit(payload, bits // 7)
    length = min((bits + 7) // 8, len(payload))
    return payload[:length].decode("iso-8859-1", errors="replace")


def pack_7bit(text: str) -> bytes:
    """GSM-7-Bit-Packung (EN 300 392-2, 29.5.4)."""
    buf = bytearray((len(text) * 7 + 7) // 8)
    for i, ch in enumerate(text):
        set_bits(buf, i * 7, 7, ord(ch) & 0x7F)
    return bytes(buf)


def unpack_7bit(data: bytes, count: int) -> str:
    chars = []
    for i in range(count):
        if (i * 7 + 7) > len(data) * 8:
            break
        chars.append(chr(get_bits(data, i * 7, 7)))
    return "".join(chars)


# ---------------------------------------------------------------- Encoder


def build_subscriber(
    msg_type: int, issi: int, groups: Optional[list[int]] = None
) -> bytes:
    seconds, nanos = _now_parts()
    out = struct.pack("<BBIQI", CLASS_SUBSCRIBER, msg_type, issi, seconds, nanos)
    for gssi in groups or []:
        out += struct.pack("<I", gssi)
    return out


def build_group_tx(
    identifier: _uuid.UUID,
    source: int,
    destination: int,
    priority: int = 0,
    access: int = 0,
    service: int = 0,
    mnemonic: bytes = b"",
) -> bytes:
    if not mnemonic:
        mnemonic = b"\x00" * MNEMONIC_FIELD_LEN
    mnemonic = mnemonic[:MNEMONIC_FIELD_LEN].ljust(MNEMONIC_FIELD_LEN, b"\x00")
    body = struct.pack("<IIBBH", source, destination, priority, access, service)
    return (
        struct.pack("<BB", CLASS_CALL, CALL_GROUP_TX)
        + identifier.bytes_le
        + body
        + mnemonic
    )


def build_group_idle(identifier: _uuid.UUID, cause: int = 0) -> bytes:
    return (
        struct.pack("<BB", CLASS_CALL, CALL_GROUP_IDLE)
        + identifier.bytes_le
        + struct.pack("<B", cause)
    )


def build_short_transfer(
    identifier: _uuid.UUID, source: int, destination: int, number: str = ""
) -> bytes:
    num = number.encode("ascii", errors="ignore")[:32].ljust(32, b"\x00")
    return (
        struct.pack("<BB", CLASS_CALL, CALL_SHORT_TRANSFER)
        + identifier.bytes_le
        + struct.pack("<II", source, destination)
        + num
    )


def build_frame(msg_type: int, identifier: _uuid.UUID, data: bytes,
                bits: Optional[int] = None) -> bytes:
    if bits is None:
        bits = len(data) * 8
    return (
        struct.pack("<BB", CLASS_FRAME, msg_type)
        + identifier.bytes_le
        + struct.pack("<H", bits)
        + data
    )


def build_service(msg_type: int, payload: object) -> bytes:
    blob = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\x00"
    return struct.pack("<BB", CLASS_SERVICE, msg_type) + blob


def build_query_subscribers(issis: list[int]) -> bytes:
    return build_service(SERVICE_QUERY_SUBSCRIBERS, list(issis))


# ------------------------------------------------------- Traffic-Frame-Layout
#
# Oktett 0    : Bit 7 = 1, Bits 6-2 = STE-Bits C1-C5, Bits 1-0 = 0
# Oktett 1+   : 137 Bit ACELP-Subframe 1        (Bitoffset 8)
# Oktett 18+  : 137 Bit ACELP-Subframe 2        (Bitoffset 145, "um 1 Bit
#               nach rechts verschoben" ist genau das)
# Oktett 35   : Bits 7-6 = Rest Subframe 2, Bits 5-0 = 0x3f (unbenutzt)

SUBFRAME1_BIT_OFFSET = 8
SUBFRAME2_BIT_OFFSET = SUBFRAME1_BIT_OFFSET + ACELP_SUBFRAME_BITS  # 145


def split_traffic_frame(data: bytes) -> tuple[int, int, int]:
    """Zerlegt einen 36-Byte-Traffic-Frame in (ste, subframe1, subframe2).

    Die Subframes kommen als int mit je 137 Bit zurueck (MSB first).
    """
    if len(data) < TRAFFIC_FRAME_BYTES:
        raise BrewProtocolError(
            f"Traffic-Frame zu kurz: {len(data)} statt {TRAFFIC_FRAME_BYTES} Byte"
        )
    ste = (data[0] >> 2) & 0x1F
    sub1 = get_bits(data, SUBFRAME1_BIT_OFFSET, ACELP_SUBFRAME_BITS)
    sub2 = get_bits(data, SUBFRAME2_BIT_OFFSET, ACELP_SUBFRAME_BITS)
    return ste, sub1, sub2


def build_traffic_frame(sub1: int, sub2: int, ste: int = 0) -> bytes:
    """Baut einen 36-Byte-Traffic-Frame aus zwei 137-Bit-Subframes."""
    buf = bytearray(TRAFFIC_FRAME_BYTES)
    buf[0] = 0x80 | ((ste & 0x1F) << 2)
    set_bits(buf, SUBFRAME1_BIT_OFFSET, ACELP_SUBFRAME_BITS, sub1)
    set_bits(buf, SUBFRAME2_BIT_OFFSET, ACELP_SUBFRAME_BITS, sub2)
    buf[35] = (buf[35] & 0xC0) | 0x3F
    return bytes(buf)


# ---------------------------------------------------------------- Decoder


def parse(msg: bytes):
    """Zerlegt eine Brew-Binaernachricht. Wirft BrewProtocolError."""
    if len(msg) < 2:
        raise BrewProtocolError("Nachricht kuerzer als 2 Byte")
    kind, msg_type = msg[0], msg[1]

    if kind == CLASS_SUBSCRIBER:
        return _parse_subscriber(msg_type, msg)
    if kind == CLASS_CALL:
        return _parse_call(msg_type, msg)
    if kind == CLASS_FRAME:
        return _parse_frame(msg_type, msg)
    if kind == CLASS_ERROR:
        return ErrorMessage(type=msg_type, data=msg[2:])
    if kind == CLASS_SERVICE:
        return _parse_service(msg_type, msg)
    raise BrewProtocolError(f"Unbekannte Message-Klasse 0x{kind:02x}")


def _parse_subscriber(msg_type: int, msg: bytes) -> SubscriberMessage:
    if len(msg) < 18:
        raise BrewProtocolError("Subscriber-Nachricht zu kurz")
    _, _, number, timestamp, fraction = struct.unpack("<BBIQI", msg[:18])
    groups = []
    rest = msg[18:]
    for i in range(0, len(rest) - 3, 4):
        groups.append(struct.unpack("<I", rest[i : i + 4])[0])
    return SubscriberMessage(msg_type, number, timestamp, fraction, groups)


def _parse_call(msg_type: int, msg: bytes) -> CallMessage:
    if len(msg) < CALL_HEADER_LEN:
        raise BrewProtocolError("Call-Control-Nachricht zu kurz")
    identifier = _uuid.UUID(bytes_le=msg[2:18])
    body = msg[CALL_HEADER_LEN:]
    out = CallMessage(type=msg_type, identifier=identifier, raw=body)

    if msg_type == CALL_GROUP_TX:
        if len(body) < 12:
            raise BrewProtocolError("GROUP_TX zu kurz")
        source, destination, priority, access, service = struct.unpack(
            "<IIBBH", body[:12]
        )
        out.group = GroupTransmission(
            source=source,
            destination=destination,
            priority=priority,
            access=access,
            service=service,
            mnemonic=body[12 : 12 + MNEMONIC_FIELD_LEN],
        )
    elif msg_type in (CALL_GROUP_IDLE, CALL_SETUP_REJECT, CALL_RELEASE,
                      CALL_PDP_REJECT, CALL_PDP_RELEASE):
        out.cause = body[0] if body else None
    elif msg_type == CALL_SHORT_TRANSFER:
        if len(body) < 8:
            raise BrewProtocolError("SHORT_TRANSFER zu kurz")
        source, destination = struct.unpack("<II", body[:8])
        number = body[8:40].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        out.short = ShortData(source, destination, number)
    return out


def _parse_frame(msg_type: int, msg: bytes) -> FrameMessage:
    if len(msg) < FRAME_HEADER_LEN:
        raise BrewProtocolError("Frame-Nachricht zu kurz")
    identifier = _uuid.UUID(bytes_le=msg[2:18])
    (bits,) = struct.unpack("<H", msg[18:20])
    return FrameMessage(
        type=msg_type,
        identifier=identifier,
        bits=bits,
        data=msg[FRAME_HEADER_LEN:],
    )


def _parse_service(msg_type: int, msg: bytes) -> ServiceMessage:
    blob = msg[2:]
    if blob.endswith(b"\x00"):
        blob = blob[:-1]
    try:
        payload = json.loads(blob.decode("utf-8"))
    except Exception as exc:  # pragma: no cover - defensiv
        raise BrewProtocolError(f"Service-JSON kaputt: {exc}") from exc
    return ServiceMessage(type=msg_type, payload=payload)
