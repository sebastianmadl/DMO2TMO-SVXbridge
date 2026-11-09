#!/usr/bin/env python3
"""Selbsttest ohne Netzwerk: Framing, Bitpackung, Zeitsteuerung.

    python3 selftest.py
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.audio import usrp
from app.audio.base import UsrpSink
from app.brew import protocol as p
from app.brew import sds
from app import config as cfgmod
from app.schedule import RouteScheduler, STATE_ACTIVE, STATE_EXTENDED, STATE_INACTIVE

TZ = ZoneInfo("Europe/Vienna")
failures = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FEHL  {name} {detail}")
        failures.append(name)


def test_lengths():
    print("Nachrichtenlaengen (gegen die Brew-Spec):")
    ident = uuid.uuid4()

    msg = p.build_group_tx(ident, source=2320997, destination=2324606)
    # offsetof(data)=18 + sizeof(BrewGroupTransmission)=46
    check("GROUP_TX = 64 Byte", len(msg) == 64, f"ist {len(msg)}")

    msg = p.build_group_idle(ident, cause=0)
    check("GROUP_IDLE = 19 Byte", len(msg) == 19, f"ist {len(msg)}")

    msg = p.build_short_transfer(ident, 1, 2)
    check("SHORT_TRANSFER = 58 Byte", len(msg) == 58, f"ist {len(msg)}")

    msg = p.build_subscriber(p.SUB_REGISTER, 2320997)
    check("REGISTER = 18 Byte", len(msg) == 18, f"ist {len(msg)}")

    msg = p.build_subscriber(p.SUB_AFFILIATE, 2320997, [1, 2, 3])
    check("AFFILIATE mit 3 GSSI = 30 Byte", len(msg) == 30, f"ist {len(msg)}")

    frame = p.build_frame(p.FRAME_TRAFFIC_CHANNEL, ident, b"\x00" * 36)
    check("Traffic-Frame = 20 + 36 Byte", len(frame) == 56, f"ist {len(frame)}")


def test_roundtrip():
    print("Round-Trip Encode -> Parse:")
    ident = uuid.uuid4()

    raw = p.build_group_tx(ident, source=2320997, destination=2324606,
                           priority=7, mnemonic=p.encode_mnemonic("OE4SXM"))
    msg = p.parse(raw)
    check("GROUP_TX UUID", msg.identifier == ident)
    check("GROUP_TX source", msg.group.source == 2320997)
    check("GROUP_TX destination", msg.group.destination == 2324606)
    check("GROUP_TX priority", msg.group.priority == 7)
    check("GROUP_TX mnemonic", msg.group.mnemonic_text == "OE4SXM",
          f"ist {msg.group.mnemonic_text!r}")

    raw = p.build_group_idle(ident, cause=3)
    msg = p.parse(raw)
    check("GROUP_IDLE cause", msg.cause == 3)

    raw = p.build_short_transfer(ident, 2320997, 2320998, "12345")
    msg = p.parse(raw)
    check("SHORT_TRANSFER source", msg.short.source == 2320997)
    check("SHORT_TRANSFER number", msg.short.number == "12345")

    raw = p.build_subscriber(p.SUB_AFFILIATE, 2320997, [2321, 2324, 9])
    msg = p.parse(raw)
    check("AFFILIATE Gruppen", msg.groups == [2321, 2324, 9], f"ist {msg.groups}")

    raw = p.build_query_subscribers([2320997])
    msg = p.parse(raw)
    check("Service-JSON", msg.payload == [2320997])
    check("Service NULL-terminiert", raw.endswith(b"\x00"))


def test_traffic_bits():
    print("ACELP-Bitpackung (60 ms = 2 x 137 Bit):")
    sub1 = (1 << 136) | 0b101          # MSB und LSBs gesetzt
    sub2 = (1 << 135) | 0b11
    frame = p.build_traffic_frame(sub1, sub2, ste=0b10101)
    check("Frame = 36 Byte", len(frame) == p.TRAFFIC_FRAME_BYTES, f"ist {len(frame)}")
    check("Oktett 0 Bit 7 gesetzt", frame[0] & 0x80 == 0x80)
    check("Oktett 0 Bits 1-0 = 0", frame[0] & 0x03 == 0)
    check("Oktett 35 Bits 5-0 = 0x3f", frame[35] & 0x3F == 0x3F)

    ste, out1, out2 = p.split_traffic_frame(frame)
    check("STE zurueck", ste == 0b10101, f"ist {ste:05b}")
    check("Subframe 1 zurueck", out1 == sub1)
    check("Subframe 2 zurueck", out2 == sub2)

    # Subframe 2 beginnt bei Bitoffset 145 = Oktett 18, Bit 1
    check("Subframe-2-Offset = 145", p.SUBFRAME2_BIT_OFFSET == 145)


def test_sds():
    print("SDS Type 4:")
    pdu = sds.encode_text("2324606")
    parsed = sds.decode_text(pdu)
    check("8-Bit Text", parsed is not None and parsed.text == "2324606",
          f"ist {parsed}")
    check("PID = 0x02", pdu[0] == sds.PID_SIMPLE_TEXT)

    pdu = sds.encode_text("R2 2321", sds.SCHEME_7BIT)
    parsed = sds.decode_text(pdu)
    check("7-Bit Text", parsed is not None and parsed.text.startswith("R2 2321"),
          f"ist {parsed.text!r}" if parsed else "None")

    check("Fremde PID ignoriert", sds.decode_text(b"\x0a\x00abc") is None)


def _route(**kwargs) -> cfgmod.Route:
    route = cfgmod.Route(
        gssi=kwargs.pop("gssi", 2324606),
        reflector=kwargs.pop("reflector", "R1"),
        tg=kwargs.pop("tg", 2324606),
        **kwargs,
    )
    return route


def test_schedule_grace():
    print("Zeitsteuerung - Nachlauf (grace):")
    route = _route(shutdown_mode="grace", grace_minutes=5, max_extend_minutes=120)
    route.schedule = cfgmod.Schedule(
        enabled=True, days=["WE"],
        windows=[cfgmod.Window(start="20:00", end="22:00")],
    )
    scheduler = RouteScheduler(route, "Europe/Vienna")

    # Mittwoch, 3.9.2025 war ein Mittwoch -> nehmen wir einen echten:
    wednesday = datetime(2026, 9, 2, 21, 0, tzinfo=TZ)   # Mi 21:00
    check("21:00 aktiv", scheduler.update(wednesday) == STATE_ACTIVE,
          scheduler.state)

    # Runde laeuft noch um 22:00
    scheduler.note_traffic_start(wednesday + timedelta(minutes=59))
    after = datetime(2026, 9, 2, 22, 5, tzinfo=TZ)
    check("22:05 mit laufendem Verkehr = Nachlauf",
          scheduler.update(after) == STATE_EXTENDED, scheduler.state)
    check("22:05 ist aktiv (neue Durchgaenge erlaubt)", scheduler.is_active(after))

    # Runde geht bis 23:20
    late = datetime(2026, 9, 2, 23, 20, tzinfo=TZ)
    scheduler.note_traffic_end(late)
    check("23:22 noch aktiv (2 min Ruhe)",
          scheduler.update(late + timedelta(minutes=2)) == STATE_EXTENDED,
          scheduler.state)
    check("23:26 aus (5 min Ruhe)",
          scheduler.update(late + timedelta(minutes=6)) == STATE_INACTIVE,
          scheduler.state)


def test_schedule_hard():
    print("Zeitsteuerung - hart:")
    route = _route(shutdown_mode="hard")
    route.schedule = cfgmod.Schedule(
        enabled=True, days=["WE"],
        windows=[cfgmod.Window(start="20:00", end="22:00")],
    )
    scheduler = RouteScheduler(route, "Europe/Vienna")
    scheduler.update(datetime(2026, 9, 2, 21, 0, tzinfo=TZ))
    scheduler.note_traffic_start()
    check("22:01 sofort aus trotz Verkehr",
          scheduler.update(datetime(2026, 9, 2, 22, 1, tzinfo=TZ)) == STATE_INACTIVE,
          scheduler.state)


def test_schedule_cap():
    print("Zeitsteuerung - Obergrenze:")
    route = _route(shutdown_mode="grace", grace_minutes=5, max_extend_minutes=60)
    route.schedule = cfgmod.Schedule(
        enabled=True, days=["WE"],
        windows=[cfgmod.Window(start="20:00", end="22:00")],
    )
    scheduler = RouteScheduler(route, "Europe/Vienna")
    scheduler.update(datetime(2026, 9, 2, 21, 0, tzinfo=TZ))
    scheduler.note_traffic_start()
    check("23:05 Obergrenze greift",
          scheduler.update(datetime(2026, 9, 2, 23, 5, tzinfo=TZ)) == STATE_INACTIVE,
          scheduler.state)


def test_schedule_overnight():
    print("Zeitsteuerung - ueber Mitternacht:")
    route = _route()
    route.schedule = cfgmod.Schedule(
        enabled=True, days=["FR"],
        windows=[cfgmod.Window(start="22:00", end="02:00")],
    )
    scheduler = RouteScheduler(route, "Europe/Vienna")
    check("Fr 23:00 aktiv",
          scheduler.update(datetime(2026, 9, 4, 23, 0, tzinfo=TZ)) == STATE_ACTIVE)
    scheduler2 = RouteScheduler(route, "Europe/Vienna")
    check("Sa 01:00 noch aktiv",
          scheduler2.update(datetime(2026, 9, 5, 1, 0, tzinfo=TZ)) == STATE_ACTIVE)
    scheduler3 = RouteScheduler(route, "Europe/Vienna")
    check("Sa 03:00 aus",
          scheduler3.update(datetime(2026, 9, 5, 3, 0, tzinfo=TZ)) == STATE_INACTIVE)


def _example_raw() -> dict:
    import yaml
    with open("config.example.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _abgelehnt(name: str, raw: dict) -> None:
    try:
        cfgmod.from_dict(raw)
        check(name, False)
    except cfgmod.ConfigError:
        check(name, True)


def test_config():
    print("Konfiguration:")
    cfg = cfgmod.load("config.example.yaml")
    check("eine Zeile geladen", len(cfg.routes) == 1, f"ist {len(cfg.routes)}")
    check("ein Reflektor", len(cfg.reflectors) == 1)
    check("Zeile ist fix", cfg.routes[0].type == "fixed")
    check("Reflektor hat Rufzeichen und Schluessel",
          all(r.callsign and r.auth_key for r in cfg.reflectors))
    check("Reflektor ist svxlink-fertig",
          all(not r.ready_for_svxlink() for r in cfg.reflectors),
          str([r.ready_for_svxlink() for r in cfg.reflectors]))
    check("TETRA-Erweiterungen per Default an",
          cfg.tetra.issi_passthrough and cfg.tetra.sds_forward)
    check("keine DTMF-PTY per Default",
          not cfgmod.Reflector(name="X").pty_enabled)
    # Kein echter Schluessel im Beispiel - das Repo ist oeffentlich.
    check("Beispiel-AUTH_KEY ist ein Platzhalter",
          cfg.reflectors[0].auth_key == "change-me")

    raw = _example_raw()
    zwei = dict(raw)
    zwei["reflectors"] = raw["reflectors"] + [
        {**raw["reflectors"][0], "name": "R2", "usrp_port": 41235,
         "usrp_listen_port": 41245}]
    _abgelehnt("zweiter aktiver Reflektor wird abgelehnt", zwei)
    zwei_aus = dict(zwei)
    zwei_aus["reflectors"] = [zwei["reflectors"][0],
                              {**zwei["reflectors"][1], "enabled": False}]
    try:
        cfgmod.from_dict(zwei_aus)
        check("abgeschalteter zweiter Reflektor ist erlaubt", True)
    except cfgmod.ConfigError as exc:
        check("abgeschalteter zweiter Reflektor ist erlaubt", False, str(exc))
    _abgelehnt("zweite Zeile wird abgelehnt", {**raw, "routes": raw["routes"] + [
        {**raw["routes"][0], "gssi": 2322, "tg": 2322}]})
    _abgelehnt("dynamische Zeile wird abgelehnt", {**raw, "routes": [
        {"gssi": 9, "reflector": "R1", "type": "dynamic", "default_tg": 1}]})

    try:
        cfgmod.from_dict({
            "brew": {"issi": 1, "username": "x"},
            "reflectors": [{"name": "R1", "usrp_port": 100,
                            "usrp_listen_port": 100}],
            "routes": [{"gssi": 9, "reflector": "R1", "tg": 1}],
        })
        check("gleicher Sende- und Lauschport wird abgelehnt", False)
    except cfgmod.ConfigError:
        check("gleicher Sende- und Lauschport wird abgelehnt", True)


    try:
        cfgmod.from_dict({
            "brew": {"issi": 1, "username": "x"},
            "routes": [{"gssi": 9, "reflector": "gibtsnicht", "tg": 1}],
        })
        check("unbekannter Reflektor wird abgelehnt", False)
    except cfgmod.ConfigError:
        check("unbekannter Reflektor wird abgelehnt", True)


def test_router_tetra():
    print("TETRA-Erweiterung (ISSI, SDS):")
    import asyncio
    import json
    from app.routing import Router
    from app.state import AppState, LastHeardEntry

    cfg = cfgmod.load("config.example.yaml")
    state = AppState()
    router = Router(cfg, state)

    class FakeSink(UsrpSink):
        def __init__(self):
            super().__init__(cfg.reflectors[0])
            self.texts = []

        def send_text(self, text):
            self.texts.append(text)
            return True

    sink = FakeSink()
    router._sinks[f"usrp:{cfg.routes[0].reflector}"] = sink
    state.add_last_heard(LastHeardEntry(
        timestamp=1.0, direction="tetra->svx", gssi=2321, tg=2321,
        reflector="R1", issi=2320998, callsign="OE4SXM"))

    check("Ziel aus 'to:CALL:Text'",
          router.forward_sds_to_svx(2320998, "to:oe1abc:Servus") and
          json.loads(sink.texts[-1][4:])["dest_call"] == "OE1ABC")
    msg = json.loads(sink.texts[-1][4:])
    check("Text ohne Zielangabe", msg["text"] == "Servus", repr(msg))
    check("Absender mit Rufzeichen", msg["source_call"] == "OE4SXM"
          and msg["source_issi"] == 2320998, repr(msg))
    router.forward_sds_to_svx(2320998, "OE1ABC-T: kurz")
    check("Ziel aus 'CALL:Text' mit Suffix",
          json.loads(sink.texts[-1][4:])["dest_call"] == "OE1ABC-T")
    router.forward_sds_to_svx(2320998, "nur ein Text: ohne Ziel")
    msg = json.loads(sink.texts[-1][4:])
    check("ohne Rufzeichen kein Ziel", msg["dest_call"] == ""
          and msg["text"] == "nur ein Text: ohne Ziel", repr(msg))
    cfg.tetra.sds_forward = False
    check("abgeschaltet geht nichts raus",
          not router.forward_sds_to_svx(2320998, "to:OE1ABC:x"))
    cfg.tetra.sds_forward = True

    check("Rufzeichen -> ISSI", state.issi_for_callsign("oe4sxm") == 2320998)
    check("Rufzeichen mit Suffix -> ISSI",
          state.issi_for_callsign("OE4SXM-T") == 2320998)
    check("unbekanntes Rufzeichen -> 0", state.issi_for_callsign("OE9XYZ") == 0)

    class FakeClient:
        connected = True

        def __init__(self):
            self.sds = []
            self.tx = []

        def send_sds(self, dest, pdu, source=None):
            self.sds.append((dest, pdu))

        def group_tx(self, gssi, source=None, callsign="", priority=0,
                     identifier=None):
            self.tx.append((gssi, source, callsign))
            return identifier

        def group_idle(self, identifier, cause=0):
            pass

    client = FakeClient()
    router.attach(client)
    asyncio.run(router.on_svx_sds("R1", {"dest_call": "OE4SXM",
                                         "source_call": "DL1ABC",
                                         "text": "Hallo"}))
    check("SDS vom Reflektor an die ISSI zugestellt",
          client.sds and client.sds[-1][0] == 2320998, repr(client.sds))
    vorher = len(client.sds)
    asyncio.run(router.on_svx_sds("R1", {"dest_call": "OE9XYZ",
                                         "text": "weg"}))
    check("unbekanntes Ziel wird nicht verschickt", len(client.sds) == vorher)
    check("...aber im Postfach vermerkt",
          any("OE9XYZ" in (m.get("text") or "") for m in router.sdsbox.since(0)))

    class FakeSource:
        talker_issi = 2329001
    router.sources["R1"] = FakeSource()
    asyncio.run(router.on_svx_start("R1", "DL1ABC", 2321))
    check("Quell-ISSI aus svxlink wird durchgereicht",
          client.tx and client.tx[-1][1] == 2329001, repr(client.tx))
    asyncio.run(router.on_svx_stop("R1"))
    cfg.tetra.issi_passthrough = False
    asyncio.run(router.on_svx_start("R1", "DL1ABC", 2321))
    check("abgeschaltet: eigene ISSI", client.tx[-1][1] is None,
          repr(client.tx))
    asyncio.run(router.on_svx_stop("R1"))
    cfg.tetra.issi_passthrough = True
    FakeSource.talker_issi = 0x1000000
    asyncio.run(router.on_svx_start("R1", "DL1ABC", 2321))
    check("zu grosse ISSI wird ignoriert", client.tx[-1][1] is None)
    asyncio.run(router.on_svx_stop("R1"))


def test_usrp():
    print("USRP-Pakete (gegen die Mitschnitte in usrp_protocol.txt):")

    pcm = bytes(range(256)) + bytes(64)          # 320 Byte = 160 Samples
    pkt = usrp.build_voice(seq=11, pcm=pcm, keyup=True)
    check("Sprachpaket = 352 Byte", len(pkt) == usrp.VOICE_PACKET_LEN,
          f"ist {len(pkt)}")
    check("beginnt mit 'USRP'", pkt[0:4] == b"USRP")
    check("Sequenz big endian in Byte 4-7", pkt[4:8] == b"\x00\x00\x00\x0b",
          pkt[4:8].hex())
    check("Keyup big endian in Byte 12-15", pkt[12:16] == b"\x00\x00\x00\x01",
          pkt[12:16].hex())
    check("Typ VOICE = 0", pkt[20:24] == b"\x00\x00\x00\x00", pkt[20:24].hex())

    # Der Typ sitzt im ersten Byte des Feldes - genau das ist die Eigenart.
    text = usrp.build_metadata(seq=1, callsign="OE4SXM", talkgroup=2321)
    check("Typ TEXT steht in Byte 20", text[20] == usrp.TYPE_TEXT, text[20:24].hex())
    check("Rufzeichen in der Nutzlast", b"OE4SXM" in text)
    check("TLV_TAG_SET_INFO gesetzt",
          text[usrp.HEADER_LEN] == usrp.TLV_TAG_SET_INFO)
    # svxlinks UsrpTlvMetaMsg hat ein festes 306-Byte-Feld am Ende. Ist das
    # Paket kuerzer, scheitert dort unpack() lautlos und die Metadaten sind
    # weg - im Log steht dann "Talker stop: unknown TG# 0".
    check("Metadatenpaket = 352 Byte", len(text) == usrp.META_PACKET_LEN,
          f"ist {len(text)}")
    # tlv(1) tlvlen(1) dmrid(3) rptid(4) -> TG beginnt bei +9
    tg_bytes = text[usrp.HEADER_LEN + 9:usrp.HEADER_LEN + 12]
    check("TG steht im TLV", int.from_bytes(tg_bytes, "big") == 2321,
          tg_bytes.hex())

    back = usrp.parse(pkt)
    check("Round-Trip Typ", back.type == usrp.TYPE_VOICE)
    check("Round-Trip Sequenz", back.seq == 11)
    check("Round-Trip Keyup", back.keyup is True)
    check("Round-Trip Nutzlast", back.payload == pcm)

    end = usrp.parse(usrp.build_end(seq=12))
    check("Traegerende hat Keyup=0", end.keyup is False)
    check("Traegerende ist trotzdem VOICE", end.type == usrp.TYPE_VOICE)
    check("Traegerende traegt Stille", end.payload == b"\x00" * usrp.VOICE_BYTES)

    # Toleranz: eine Gegenstelle, die alles big endian schreibt, muss auch gehen.
    other = bytearray(usrp.build_voice(seq=1, pcm=pcm, keyup=False))
    other[12:16] = b"\x01\x00\x00\x00"          # Keyup im ersten Byte
    other[20:24] = b"\x00\x00\x00\x02"          # Typ big endian
    liberal = usrp.parse(bytes(other))
    check("Keyup auch im ersten Byte erkannt", liberal.keyup is True)
    check("Typ auch big endian erkannt", liberal.type == usrp.TYPE_TEXT,
          f"ist {liberal.type}")

    # 60 ms TETRA muessen glatt in drei USRP-Pakete passen.
    chunks = usrp.split_pcm(bytes(960))
    check("60 ms = 3 USRP-Pakete", len(chunks) == 3, f"sind {len(chunks)}")
    check("jedes Stueck 320 Byte", all(len(c) == 320 for c in chunks))
    check("Rest wird aufgefuellt", len(usrp.split_pcm(bytes(100))[0]) == 320)

    try:
        usrp.parse(b"XXXX" + bytes(28))
        check("fremdes Paket wird abgelehnt", False)
    except usrp.UsrpError:
        check("fremdes Paket wird abgelehnt", True)


def test_confgen():
    print("svxlink.conf erzeugen:")
    import confgen

    cfg = cfgmod.load("config.example.yaml")
    r1 = cfg.reflector("R1")
    text = confgen.render(cfg, r1)

    def value(key: str, section: str = "") -> str:
        """Wert lesen, optional nur innerhalb eines Abschnitts.

        Noetig seit der DtmfLogic: die steht vor den anderen und haette
        sonst mit ihrem CALLSIGN die Pruefung der ReflectorLogic verfaelscht.
        """
        drin = not section
        for line in text.splitlines():
            if line.startswith("["):
                drin = (line.strip() == f"[{section}]") if section else True
            elif drin and line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
        return ""

    check("alle Logics eingetragen, keine DtmfLogic",
          value("LOGICS") == "UsrpLogic,ReflectorLogic",
          value("LOGICS"))
    check("Link ist dauerhaft aktiv", value("DEFAULT_ACTIVE") == "1")

    # UsrpLogic laedt nicht, wenn CALL laenger als 6 Zeichen ist - dann
    # haengt ReflectorLogic ohne Audiopfad da.
    check("UsrpLogic-CALL hoechstens 6 Zeichen", len(value("CALL")) <= 6,
          repr(value("CALL")))
    check("UsrpLogic-CALL ohne Node-Suffix", value("CALL") == "OE0XXX",
          repr(value("CALL")))
    check("ReflectorLogic behaelt das volle Rufzeichen",
          value("CALLSIGN", "ReflectorLogic") == r1.callsign,
          value("CALLSIGN", "ReflectorLogic"))
    # DMO2TMO: feste Sprechgruppe, also keine DTMF-PTY mehr.
    check("keine DtmfLogic", "[DtmfLogic]" not in text
          and "DTMF_CTRL_PTY" not in text)
    check("Link nur Usrp und Reflector",
          value("CONNECT_LOGICS") == "UsrpLogic,ReflectorLogic",
          value("CONNECT_LOGICS"))
    # Ohne SHARE_USERINFO schickt die UsrpLogic keine State-Events - dann
    # kommen weder ISSI noch SDS am Reflektor an.
    check("SHARE_USERINFO an", value("SHARE_USERINFO", "UsrpLogic") == "1",
          value("SHARE_USERINFO", "UsrpLogic"))
    # CFG_DIR wuerde svxlink alle .conf im Verzeichnis mitlesen lassen.
    check("kein CFG_DIR gesetzt", "CFG_DIR" not in text)

    # Die Portnamen sind aus svxlinks Sicht benannt und damit gegenlaeufig
    # zu unseren. Genau hier vertut man sich.
    check("USRP_TX_PORT ist unser Lauschport",
          value("USRP_TX_PORT") == str(r1.usrp_listen_port),
          f"{value('USRP_TX_PORT')} vs {r1.usrp_listen_port}")
    check("USRP_RX_PORT ist unser Sendeport",
          value("USRP_RX_PORT") == str(r1.usrp_port),
          f"{value('USRP_RX_PORT')} vs {r1.usrp_port}")
    check("die beiden Ports sind verschieden",
          value("USRP_TX_PORT") != value("USRP_RX_PORT"))

    check("Reflektor-Host heisst HOSTS", value("HOSTS") == r1.host,
          value("HOSTS"))
    check("kein HOST= ohne S", "\nHOST=" not in text)
    check("Rufzeichen gesetzt", r1.callsign in value("CALLSIGN", "ReflectorLogic"))
    check("AUTH_KEY gesetzt", r1.auth_key in value("AUTH_KEY", "ReflectorLogic"))
    check("Reflektor-Logic-Typ aus der Config",
          f"TYPE={r1.logic_type}" in text, r1.logic_type)
    check("TYPE=Usrp vorhanden", "TYPE=Usrp" in text)
    # ReflectorV2 meldet sich nur ueber AUTH_KEY an, ohne Zertifikat.
    check("kein CERT_PKI_DIR", "CERT_PKI_DIR" not in text)
    check("AUDIO_CODEC=OPUS gesetzt", value("AUDIO_CODEC") == "OPUS")
    # Sonst geht der erste Ruf nach einem TG-Wechsel stumm verloren.
    check("erste Aussendung nicht stumm", value("MUTE_FIRST_TX_LOC") == "0")

    check("DEFAULT_TG ist die TG der Zeile", value("DEFAULT_TG") == "2321",
          value("DEFAULT_TG"))
    check("Sende-TG wird auch mitgehoert", "2321" in value("MONITOR_TGS"),
          value("MONITOR_TGS"))

    aus = cfgmod.from_dict({**_example_raw(),
                            "tetra": {"issi_passthrough": False,
                                      "sds_forward": False}})
    check("SHARE_USERINFO aus, wenn beides aus",
          "\nSHARE_USERINFO=0" in confgen.render(aus, aus.reflector("R1")))

    # Unvollstaendiger Reflektor darf nicht stillschweigend durchgehen.
    broken = cfgmod.Reflector(name="X", usrp_port=1, usrp_listen_port=2)
    try:
        confgen.render(cfg, broken)
        check("unvollstaendiger Reflektor fliegt auf", False)
    except cfgmod.ConfigError as exc:
        check("unvollstaendiger Reflektor fliegt auf",
              "host" in str(exc) and "auth_key" in str(exc), str(exc))


def test_gain():
    print("Audiopegel in dB:")
    from app.audio.base import apply_gain
    from app.config import db_to_factor

    check("0 dB = Faktor 1", abs(db_to_factor(0) - 1.0) < 1e-9)
    check("-6 dB halbiert", abs(db_to_factor(-6) - 0.5) < 0.01,
          f"{db_to_factor(-6):.4f}")
    check("+6 dB verdoppelt", abs(db_to_factor(6) - 2.0) < 0.01,
          f"{db_to_factor(6):.4f}")
    check("-20 dB = ein Zehntel", abs(db_to_factor(-20) - 0.1) < 1e-9)

    pcm = b"".join(int(v).to_bytes(2, "little", signed=True)
                   for v in (1000, -1000, 8000, -8000))
    check("0 dB laesst das PCM unangetastet", apply_gain(pcm, 1.0) == pcm)

    leiser = apply_gain(pcm, db_to_factor(-6))
    werte = [int.from_bytes(leiser[i:i+2], "little", signed=True)
             for i in range(0, len(leiser), 2)]
    check("-6 dB halbiert die Samples",
          abs(werte[0] - 501) <= 1 and abs(werte[2] - 4010) <= 1, str(werte))
    check("Vorzeichen bleibt", werte[1] < 0 and werte[3] < 0, str(werte))

    # Begrenzen statt ueberlaufen - sonst klingt lautes Audio wie Kies.
    laut = apply_gain(pcm, db_to_factor(20))
    werte = [int.from_bytes(laut[i:i+2], "little", signed=True)
             for i in range(0, len(laut), 2)]
    check("uebersteuertes Audio wird begrenzt",
          werte[2] == 32767 and werte[3] == -32768, str(werte))

    # Der Konfigurationsrahmen muss unsinnige Werte ablehnen.
    try:
        cfgmod.from_dict({"brew": {"issi": 1, "username": "x"},
                          "audio": {"gain_svx_db": 99}})
        check("99 dB wird abgelehnt", False)
    except cfgmod.ConfigError:
        check("99 dB wird abgelehnt", True)


def test_web():
    print("SDS-Kodierung:")
    from app.brew import sds as _sds

    for name, wert in (("8bit", _sds.SCHEME_8BIT), ("ucs2", _sds.SCHEME_UCS2),
                       ("7bit", _sds.SCHEME_7BIT)):
        pdu = _sds.encode_text("123456 Test", wert)
        check(f"{name} hin und zurueck",
              _sds.decode_text(pdu).text == "123456 Test", pdu.hex(" "))
        check(f"{name} deklariert sich richtig",
              pdu[1] == wert and _sds.scheme_name(wert) == name)

    falsch = bytes([_sds.PID_SIMPLE_TEXT, _sds.SCHEME_8BIT]) + \
        "123456 Test".encode("utf-16-be")
    check("UCS-2 wird erkannt, auch wenn 8-Bit drauf steht",
          _sds.decode_text(falsch).text == "123456 Test")
    check("echtes 8-Bit bleibt 8-Bit",
          _sds.decode_text(_sds.encode_text("Servus")).text == "Servus")
    check("kurze Nutzlast verwirrt die Erkennung nicht",
          _sds.decode_text(_sds.encode_text("ab")).text == "ab")
    check("Umlaute ueberleben 8-Bit",
          _sds.decode_text(_sds.encode_text("Gruess")).text == "Gruess")
    check("eine normale 8-Bit-Nachricht wird NICHT fuer roh gehalten",
          _sds.decode_text(_sds.encode_text("Servus")).mode == "8bit")
    check("und eine 7-Bit-Nachricht auch nicht",
          _sds.decode_text(_sds.encode_text("Servus", _sds.SCHEME_7BIT)).mode
          == "7bit")

    print("SDS ohne Kodierungsbyte (fuer TetraLink):")

    def wie_tetralink(pdu):
        """Nur den PID ueberspringen, Rest als UTF-16BE - so liest es die App."""
        rest = pdu[1:]
        voll = rest[:len(rest) - len(rest) % 2]
        return voll.decode("utf-16-be", errors="replace") + (
            "\ufffd" if len(rest) % 2 else "")

    roh16 = _sds.build_text_pdu("test", "raw16")
    check("raw16 hat kein Kodierungsbyte",
          roh16 == bytes([_sds.PID_SIMPLE_TEXT]) + "test".encode("utf-16-be"),
          roh16.hex(" "))
    check("und ist damit in TetraLink lesbar", wie_tetralink(roh16) == "test",
          repr(wie_tetralink(roh16)))
    check("die ETSI-Varianten dagegen nicht",
          all(wie_tetralink(_sds.build_text_pdu("test", m)) != "test"
              for m in ("7bit", "8bit", "ucs2")))

    rohascii = _sds.build_text_pdu("test", "ascii")
    check("ascii ist PID + blanker Text", rohascii == b"\x02test", rohascii.hex(" "))

    for art in _sds.MODES:
        pdu = _sds.build_text_pdu("test", art)
        zurueck = _sds.decode_text(pdu)
        check(f"{art}: kommt zurueck", zurueck.text == "test", repr(zurueck.text))
        check(f"{art}: wird als {art} erkannt", zurueck.mode == art, zurueck.mode)

    check("Umlaute gehen in raw16",
          _sds.decode_text(_sds.build_text_pdu("Grueezi", "raw16")).text == "Grueezi")
    check("unbekannter Name faellt auf 8bit zurueck",
          _sds.mode_from_name("quatsch") == "8bit"
          and _sds.mode_from_name("") == "8bit")
    check("16bit ist ein Zweitname fuer raw16",
          _sds.mode_from_name("16bit") == "raw16")

    print("SDS-TL, PID 0x82 - das Format von TetraLink:")
    mitschnitt = bytes.fromhex("8204070168616c6c6f")     # echt, 2026-09-08
    d = _sds.decode_text(mitschnitt)
    check("TetraLinks eigene PDU wird gelesen", d.text == "hallo", repr(d.text))
    check("als sdstl gemeldet", d.mode == "sdstl", d.mode)
    check("Message Reference kommt mit", d.message_reference == 7,
          str(d.message_reference))

    eigene = _sds.build_text_pdu("hallo", "sdstl")
    check("wir bauen denselben Aufbau",
          eigene[0] == 0x82 and eigene[1] == 0x04 and eigene[3] == 0x01
          and eigene[4:] == b"hallo", eigene.hex(" "))
    check("und lesen ihn selbst wieder",
          _sds.decode_text(eigene).text == "hallo")
    erste = _sds.build_text_pdu("a", "sdstl")[2]
    zweite = _sds.build_text_pdu("a", "sdstl")[2]
    check("Referenz laeuft hoch", zweite != erste, f"{erste} -> {zweite}")
    check("tetralink meint jetzt sdstl",
          _sds.mode_from_name("tetralink") == "sdstl"
          and _sds.mode_from_name("0x82") == "sdstl")
    check("16bit bleibt raw16", _sds.mode_from_name("16bit") == "raw16")

    print("Nachlauf:")
    from app import config as _cfg2

    audio = _cfg2.AudioConfig()
    check("500 ms voreingestellt", audio.hang_ms == 500)
    audio.hang_ms = 0
    audio.validate()
    check("0 ist erlaubt", True)
    audio.hang_ms = _cfg2.HANG_MAX_MS + 1
    try:
        audio.validate()
        check("zu langer Nachlauf abgelehnt", False)
    except _cfg2.ConfigError:
        check("zu langer Nachlauf abgelehnt", True)
    gebaut = _cfg2.from_dict({"brew": {"issi": 1, "username": "x"},
                              "audio": {"hang_ms": 250}})
    check("aus der YAML gelesen", gebaut.audio.hang_ms == 250)
    check("und wieder hinein", gebaut.to_dict()["audio"]["hang_ms"] == 250)

    print("Nachlauf im Betrieb:")
    import asyncio as _aio
    from app.audio import usrp as _usrp
    from app.audio.source import UsrpSource as _Source

    class _Refl:
        name = "R1"
        usrp_listen_port = 0

    async def _lauf(hang_ms, zweiter_durchgang):
        """Sprechbeginn, Sprechende, dann warten - was passiert wann?"""
        ablauf = []
        quelle = _Source(
            _Refl(),
            on_start=lambda n, c, t: _fertig(ablauf.append(("start", c))),
            on_frame=lambda n, pcm: _fertig(ablauf.append(("frame", len(pcm)))),
            on_stop=lambda n: _fertig(ablauf.append(("stop",))),
            hang_ms=lambda: hang_ms,
        )
        await quelle.handle(_usrp.build_voice(1, b"\x00" * 320, keyup=True))
        await quelle.handle(_usrp.build_end(2))          # PTT losgelassen
        if zweiter_durchgang:
            await _aio.sleep(hang_ms / 2000.0)
            await quelle.handle(
                _usrp.build_voice(3, b"\x00" * 320, keyup=True))
            await quelle.handle(_usrp.build_end(4))      # und wieder los
        await _aio.sleep(hang_ms / 1000.0 + 0.3)
        await quelle.close()
        return ablauf

    def _fertig(_):
        async def nichts():
            return None
        return nichts()

    ohne = _aio.run(_lauf(0, False))
    check("ohne Nachlauf endet der Ruf sofort",
          [x[0] for x in ohne] == ["start", "stop"], str(ohne))

    mit = _aio.run(_lauf(300, False))
    check("mit Nachlauf kommt zuerst Stille",
          [x[0] for x in mit].count("frame") >= 4, str(mit))
    check("und danach das Rufende", mit[-1][0] == "stop", str(mit))

    weiter = _aio.run(_lauf(300, True))
    check("neuer Durchgang im Nachlauf baut nicht neu auf",
          [x[0] for x in weiter].count("start") == 1, str(weiter))
    check("und der Ruf endet trotzdem irgendwann",
          weiter[-1][0] == "stop", str(weiter))

    print("Rufzeichenliste:")
    import tempfile as _tmp2
    from pathlib import Path as _Path2
    from app.idlist import IdList
    from app.state import AppState as _AppState

    buch = IdList(_Path2(_tmp2.mkdtemp()) / "idlist.sqlite")
    check("faengt leer an", buch.count() == 0 and buch.manual_all() == [])
    check("unbekannte ISSI gibt leeren Text", buch.lookup(2320997) == "")

    liste = "\n".join(f"232{i:04d};OE{i % 10}TEST{i};" for i in range(1200))
    check("Liste eingelesen", buch.update_from_text(liste) == 1200)
    check("Rufzeichen gefunden", buch.lookup(2320005) == "OE5TEST5")
    check("Zeitstempel gesetzt", buch.status()["updated"] > 0)

    try:
        buch.update_from_text("2320001;OE1ABC;")
        check("zu kurze Liste abgelehnt", False)
    except ValueError:
        check("zu kurze Liste abgelehnt", True)
    check("alte Liste steht noch", buch.count() == 1200)

    buch.set_manual(2329999, "SVXTetraBridge")
    check("eigener Name gespeichert", buch.manual_name(2329999) == "SVXTetraBridge")
    buch.set_manual(2320005, "Kaffeemaschine")
    check("eigener Name schlaegt die Liste",
          buch.lookup(2320005) == "Kaffeemaschine")
    check("in der Uebersicht", len(buch.status()["manual"]) == 2)
    for schlecht, warum in ((0, "ISSI 0"), ("keine zahl", "ISSI als Text")):
        try:
            buch.set_manual(schlecht, "x")
            check(f"{warum} abgelehnt", False)
        except ValueError:
            check(f"{warum} abgelehnt", True)
    try:
        buch.set_manual(2320007, "   ")
        check("leerer Name abgelehnt", False)
    except ValueError:
        check("leerer Name abgelehnt", True)
    buch.set_manual(2320008, "x" * 200)
    check("langer Name wird gekappt", len(buch.manual_name(2320008)) == 60)
    check("loeschen geht", buch.delete_manual(2329999) is True)
    check("zweimal loeschen sagt nein", buch.delete_manual(2329999) is False)

    zustand = _AppState()
    zustand.namebook = buch
    zustand.remember_subscriber(2320005, callsign="OE5BREW")
    check("eigener Name schlaegt alles",
          zustand.callsign(2320005, "OE5FUNK") == "Kaffeemaschine")
    check("Geraet schlaegt die Auskunft",
          zustand.callsign(2320006, "OE6FUNK") == "OE6FUNK")
    zustand.remember_subscriber(2320009, callsign="OE9BREW")
    check("Auskunft schlaegt die Liste", zustand.callsign(2320009) == "OE9BREW")
    check("sonst die Liste", zustand.callsign(2320004) == "OE4TEST4")
    check("und sonst gar nichts", zustand.callsign(9999999) == "")

    ohne = _AppState()
    check("ohne Liste laeuft es weiter",
          ohne.callsign(2320005) == "" and ohne.callsign(None, "OE1X") == "OE1X")

    print("Bekannte Rufzeichen:")
    from app.state import LastHeardEntry as _LH
    import time as _t

    zustand.add_last_heard(_LH(timestamp=_t.time() - 60, direction="tetra->svx",
                               gssi=2321, tg=2321, reflector="R1",
                               issi=2320997, callsign="OE4SXM"))
    zustand.add_last_heard(_LH(timestamp=_t.time(), direction="tetra->svx",
                               gssi=2323, tg=2323, reflector="R3",
                               issi=2320042, callsign=""))
    liste = zustand.known_stations()
    nach_issi = {k["issi"]: k for k in liste}

    check("wer gehoert wurde, steht drin", 2320997 in nach_issi)
    check("das Gehoerte schlaegt die Liste",
          nach_issi[2320997]["name"] == "OE4SXM",
          nach_issi[2320997]["name"])
    check("ohne Mnemonic greift die Liste",
          nach_issi[2320042]["name"] == "OE2TEST42",
          nach_issi.get(2320042, {}).get("name"))
    # 2320005 steht in der eigenen Liste ("Kaffeemaschine") UND in der
    # Brew-Auskunft ("OE5BREW") - der eigene Eintrag muss gewinnen.
    check("eigener Name ist dabei",
          nach_issi.get(2320005, {}).get("name") == "Kaffeemaschine",
          nach_issi.get(2320005, {}).get("name"))
    check("Quelle wird mitgegeben",
          nach_issi[2320997]["source"] == "gehört"
          and nach_issi[2320005]["source"] == "eigener Name",
          nach_issi.get(2320005, {}).get("source"))
    check("zuletzt Gehoertes steht oben", liste[0]["issi"] == 2320042,
          str([k["issi"] for k in liste]))
    check("kein Hilfsfeld nach aussen",
          all("_gehoert" not in k for k in liste))
    check("ohne ISSI kein Eintrag", None not in nach_issi and 0 not in nach_issi)
    check("landet auch im Status",
          "known_stations" in zustand.as_dict())

    print("SDS-Postfach:")
    import tempfile as _tmp
    from pathlib import Path as _Path
    from app.sdsbox import SdsBox, RX, TX

    ablage = _Path(_tmp.mkdtemp()) / "sds.json"
    box = SdsBox(ablage)
    check("faengt leer an", box.all() == [] and box.last_seq == 0)
    eins = box.add(RX, 2320997, "Servus")
    zwei = box.add(TX, 2320997, "Servus zurueck")
    check("Nummern laufen hoch", eins["seq"] == 1 and zwei["seq"] == 2)
    check("Richtung gemerkt",
          eins["direction"] == "rx" and zwei["direction"] == "tx")
    check("since liefert nur das Neue",
          [m["seq"] for m in box.since(1)] == [2])
    check("Zaehler stimmen",
          box.counts() == {"total": 2, "rx": 1, "tx": 1})
    box.add(RX, None, "x" * 2000)
    check("zu langer Text wird gekappt", len(box.all()[-1]["text"]) == 1000)
    check("unbekannter Absender ist erlaubt", box.all()[-1]["peer"] is None)

    check("ueberlebt Neustart", [m["text"] for m in SdsBox(ablage).all()[:2]]
          == ["Servus", "Servus zurueck"])
    check("Nummern laufen nach dem Neuladen weiter",
          SdsBox(ablage).add(RX, 1, "vier")["seq"] == 4)

    eng = SdsBox(capacity=2)
    eng.add(RX, 1, "a"); eng.add(RX, 1, "b"); eng.add(RX, 1, "c")
    check("Ringpuffer wirft Altes weg",
          [m["text"] for m in eng.all()] == ["b", "c"])

    kaputt = _Path(_tmp.mkdtemp()) / "sds.json"
    kaputt.write_text("{kein json", encoding="utf-8")
    check("kaputte Datei wirft nicht", SdsBox(kaputt).all() == [])

    box.clear()
    check("leeren raeumt auf",
          box.all() == [] and box.counts()["total"] == 0)
    check("und das bleibt auch so", SdsBox(ablage).all() == [])

    print("Benutzer, Rechte, Sitzungen, Log-Puffer:")
    import logging
    import tempfile
    from pathlib import Path
    from app.web import logbuffer
    from app.web.users import (UserStore, SessionStore, SESSION_RENEW_AFTER,
                               hash_password, verify_password)

    h = hash_password("geheim")
    check("richtiges Passwort", verify_password("geheim", h))
    check("falsches Passwort", not verify_password("Geheim", h))
    check("Salt ist zufaellig", hash_password("geheim") != h)
    check("kaputter Hash wird abgelehnt", not verify_password("x", "muell"))

    path = Path(tempfile.mkdtemp()) / "users.yaml"
    store = UserStore(path)
    check("Erststart legt Admin an", bool(store.ensure_default_admin()))
    check("Admin darf alles", store.get("admin").may("system"))
    check("kein zweiter Default-Admin",
          UserStore(path).ensure_default_admin() is None)

    store.add("hans", "start123", areas=["status", "log"])
    check("Recht gesetzt", store.get("hans").may("log"))
    check("Recht begrenzt", not store.get("hans").may("system"))
    check("unbekannter Bereich fliegt raus",
          "quatsch" not in store.add("eva", "x", areas=["status", "quatsch"]).areas)
    check("Anmeldung klappt", store.check("hans", "start123") is not None)
    check("falsches Passwort scheitert", store.check("hans", "falsch") is None)
    check("ueberlebt Neuladen",
          UserStore(path).check("hans", "start123") is not None)
    try:
        store.remove("admin")
        check("letzter Admin ist geschuetzt", False)
    except ValueError:
        check("letzter Admin ist geschuetzt", True)

    sessions = SessionStore()
    token = sessions.new("hans")
    check("Sitzungstoken gilt", sessions.user_of("hans") is None
          and sessions.user_of(token) == "hans")
    check("erfundenes Token gilt nicht", sessions.user_of("xyz") is None)
    check("frische Sitzung braucht kein neues Cookie",
          sessions.renew(token) is False)
    name, frist = sessions._sessions[token]
    sessions._sessions[token] = (name, frist - SESSION_RENEW_AFTER - 1)
    check("nach einer Stunde laeuft die Frist mit", sessions.renew(token) is True)
    check("und danach gleich wieder nicht", sessions.renew(token) is False)
    check("erfundenes Token wird nicht verlaengert",
          sessions.renew("xyz") is False and sessions.renew(None) is False)
    sessions._sessions[token] = ("hans", time.time() - 1)
    check("abgelaufene Sitzung ist nicht wiederbelebbar",
          sessions.user_of(token) is None and sessions.renew(token) is False)

    token = sessions.new("hans")
    sessions.drop_user("hans")
    check("Passwortwechsel beendet Sitzungen", sessions.user_of(token) is None)

    # Der Root-Logger filtert vor den Handlern - ohne das kommt INFO nie an.
    logging.getLogger().setLevel(logging.INFO)
    buf = logbuffer.install()
    start = buf.last_seq
    log = logging.getLogger("probe")
    log.warning("laut")
    log.info("leise")
    rows = buf.since(start)
    check("beide Zeilen gepuffert", len(rows) == 2, f"sind {len(rows)}")
    check("Text erhalten", any("laut" in r["text"] for r in rows))
    check("Level-Filter greift", len(buf.since(start, level="WARNING")) == 1)
    check("since liefert nur Neues", buf.since(buf.last_seq) == [])


def test_nodeinfo():
    print("Node-Info (NODE_INFO_FILE):")
    import json
    import tempfile
    from pathlib import Path
    import confgen
    raw = _example_raw()
    cfg = cfgmod.from_dict(raw)
    with tempfile.TemporaryDirectory() as tmp:
        confgen.write_all(cfg, Path(tmp))
        conf = (Path(tmp) / "R1" / "svxlink.conf").read_text()
        check("ohne Angaben kein NODE_INFO_FILE", "NODE_INFO_FILE" not in conf)
        check("und keine Datei",
              not (Path(tmp) / "R1" / "node_info.json").exists())
        r = dict(raw["reflectors"][0], node_location="Tetra-OE4SXM/430.0000",
                 sysop="OE4SXM", node_info_extra='{"locator": "JN87"}')
        cfg = cfgmod.from_dict({**raw, "reflectors": [r]})
        confgen.write_all(cfg, Path(tmp))
        conf = (Path(tmp) / "R1" / "svxlink.conf").read_text()
        datei = Path(tmp) / "R1" / "node_info.json"
        check("NODE_INFO_FILE in ReflectorLogic",
              f"NODE_INFO_FILE={datei.resolve()}" in conf
              and conf.index("NODE_INFO_FILE") > conf.index("[ReflectorLogic]"))
        info = json.loads(datei.read_text())
        check("Inhalt der node_info.json",
              info == {"locator": "JN87", "nodeLocation":
                       "Tetra-OE4SXM/430.0000", "sysop": "OE4SXM"}, str(info))
        r["node_location"] = r["sysop"] = r["node_info_extra"] = ""
        confgen.write_all(cfgmod.from_dict({**raw, "reflectors": [r]}),
                          Path(tmp))
        check("geleert -> Datei weg", not datei.exists())
    for name, extra in (("kaputtes JSON", "{locator"), ("Liste", "[1]")):
        r = dict(raw["reflectors"][0], node_info_extra=extra)
        _abgelehnt(f"node_info_extra: {name} wird abgelehnt",
                   {**raw, "reflectors": [r]})


def test_tetra_fw():
    print("Lastheard TETRA-AUSTRIA (tetra_fw):")
    import asyncio
    import socket
    from app import tetra_fw as fw

    ts = time.mktime((2026, 11, 9, 12, 34, 56, 0, 0, -1))
    zeile = fw.voice_line("OE4SXM-R", "Tetra-OE4SXM/430.0000", "OE4SXM",
                          232, 2320998, ts)
    check("VOICE-Zeile wie das Original",
          zeile == "OE4SXM-R,Tetra-OE4SXM/430.0000,09.11.2026 12:34:56,"
                   "OE4SXM,VOICE,#232,2320998,,,\n", repr(zeile))
    zeile = fw.voice_line("OE4SXM-R", "", "", 232, 2320998, ts)
    check("unbekannt -> NO USER", ",NO USER,VOICE," in zeile, repr(zeile))
    zeile = fw.voice_line("OE4SXM-R", "a,b", "X\nY", 232, 1, ts)
    check("Komma/Umbruch verschiebt keine Spalten",
          zeile.count(",") == 9 and zeile.count("\n") == 1, repr(zeile))
    zeile = fw.pos_line("OE4SXM-R", "c", "OE4SXM-R", 232, 2320997,
                        "4816.82", "01624.91", ts)
    check("POS: Laenge vor Breite wie im Original",
          zeile.endswith(",POS,#232,2320997,,E;01624.91;N;4816.82;000;,\n"),
          repr(zeile))

    raw = _example_raw()
    for name, fw_raw in (
        ("Komma im Comment", {"gateway_comment": "Wien, 430"}),
        ("Breite falsch", {"gateway_lat": "48.16", "gateway_lon": "01624.91"}),
        ("Laenge falsch", {"gateway_lat": "4816.82", "gateway_lon": "1624.91"}),
        ("nur Breite", {"gateway_lat": "4816.82"}),
        ("Port 0", {"port": 0}),
    ):
        _abgelehnt(f"tetra_fw: {name} wird abgelehnt",
                   {**raw, "tetra_fw": fw_raw})
    cfg = cfgmod.from_dict({**raw, "tetra_fw": {
        "enabled": True, "gateway_lat": "4816.82", "gateway_lon": "01624.91"}})
    check("tetra_fw per Default aus", not cfgmod.from_dict(raw).tetra_fw.enabled)
    check("Port per Default 5220", cfg.tetra_fw.port == 5220)

    # Gegen einen echten UDP-Empfaenger auf localhost
    empf = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    empf.bind(("127.0.0.1", 0))
    empf.settimeout(2)
    cfg.reflectors[0].host = "127.0.0.1"
    cfg.tetra_fw.port = empf.getsockname()[1]
    sender = fw.TetraFwSender(lambda: cfg)
    check("Gateway-Call faellt auf Reflektor-Rufzeichen zurueck",
          sender.gateway_call() == cfg.reflectors[0].callsign)
    old_interval = fw.BEACON_INTERVAL
    fw.BEACON_INTERVAL = 0

    async def lauf():
        ok = await sender.voice(2320998, "OE4SXM")
        bake = await sender.beacon()
        ohne = await sender.voice(None, "")
        return ok, bake, ohne
    try:
        ok, bake, ohne = asyncio.run(lauf())
    finally:
        fw.BEACON_INTERVAL = old_interval
    zeilen = []
    try:
        while True:
            zeilen.append(empf.recv(2048).decode())
            empf.settimeout(0.2)
    except socket.timeout:
        pass
    empf.close()
    sender.close()
    check("VOICE gesendet", ok and zeilen and ",OE4SXM,VOICE,#2321,2320998,"
          in zeilen[0], repr(zeilen[:1]))
    check("Bake 6x", bake == 6 and len(zeilen) == 7, f"{bake} / {len(zeilen)}")
    check("ohne ISSI nichts gemeldet", ohne is False)
    cfg.tetra_fw.enabled = False
    check("abgeschaltet sendet nicht",
          asyncio.run(sender.voice(2320998, "X")) is False)


def main() -> int:
    for test in (
        test_lengths, test_roundtrip, test_traffic_bits, test_sds,
        test_usrp, test_confgen, test_gain, test_web,
        test_schedule_grace, test_schedule_hard, test_schedule_cap,
        test_schedule_overnight, test_config, test_router_tetra,
        test_tetra_fw, test_nodeinfo,
    ):
        test()
        print()
    if failures:
        print(f"{len(failures)} Test(s) fehlgeschlagen: {', '.join(failures)}")
        return 1
    print("Alle Tests ok.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
