#!/usr/bin/env python3
"""End-to-End-Test gegen einen Mini-Brew-Server im selben Prozess.

Prueft die Kette Digest-Auth -> WebSocket -> REGISTER -> AFFILIATE ->
GROUP_TX empfangen -> Loopback sendet GROUP_TX + Frames zurueck.

Der Mock-Server ist bewusst dumm - er ersetzt keinen echten brew-server,
sondern prueft nur, dass unsere Seite sich korrekt verhaelt.

    python3 smoketest.py
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import uuid

from aiohttp import web, WSMsgType

from app import config as cfgmod
from app.brew import protocol as p
from app.brew.digest import parse_challenge
from app.brew.client import BrewClient
from app.routing import Router
from app.state import AppState

REALM = "brew-server"
USERNAME = "100000001"
PASSWORD = "geheim"
PORT = 19000

received = []          # was der Server von uns bekommen hat
ws_ref = {}


def md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


async def handle_discovery(request: web.Request):
    auth = request.headers.get("Authorization", "")
    if not request.headers.get("User-Agent"):
        return web.Response(status=400, text="User-Agent fehlt")
    if not auth.startswith("Digest "):
        nonce = os.urandom(8).hex()
        return web.Response(
            status=401,
            headers={
                "WWW-Authenticate": (
                    f'Digest realm="{REALM}", nonce="{nonce}", '
                    'qop="auth", algorithm=MD5'
                )
            },
        )
    fields = parse_challenge(auth)
    ha1 = md5(f"{fields.get('username')}:{REALM}:{PASSWORD}")
    ha2 = md5(f"GET:{fields.get('uri')}")
    expected = md5(
        f"{ha1}:{fields.get('nonce')}:{fields.get('nc')}:"
        f"{fields.get('cnonce')}:{fields.get('qop')}:{ha2}"
    )
    if expected != fields.get("response"):
        return web.Response(status=401, text="Digest falsch")
    received.append(("auth-ok", fields.get("username")))
    return web.Response(text="/brew/session/" + uuid.uuid4().hex)


async def handle_ws(request: web.Request):
    ws = web.WebSocketResponse(protocols=("brew",))
    await ws.prepare(request)
    ws_ref["ws"] = ws
    received.append(("ws-open", request.headers.get("X-Brew-Mode")))
    async for msg in ws:
        if msg.type == WSMsgType.BINARY:
            try:
                parsed = p.parse(msg.data)
            except p.BrewProtocolError as exc:
                received.append(("unparsable", str(exc)))
                continue
            received.append((type(parsed).__name__, parsed))
        elif msg.type == WSMsgType.ERROR:
            break
    return ws


async def run_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/brew/", handle_discovery)
    app.router.add_get("/brew/session/{token}", handle_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    return runner


async def inject_call(gssi: int, issi: int, frames: int = 3) -> None:
    """Server spielt einen Gruppenruf Richtung Bruecke ein."""
    ws = ws_ref["ws"]
    ident = uuid.uuid4()
    await ws.send_bytes(
        p.build_group_tx(ident, source=issi, destination=gssi,
                         mnemonic=p.encode_mnemonic("OE1SXM"))
    )
    for i in range(frames):
        payload = p.build_traffic_frame(sub1=i + 1, sub2=i + 100, ste=1)
        await ws.send_bytes(p.build_frame(p.FRAME_TRAFFIC_CHANNEL, ident, payload))
        await asyncio.sleep(0.06)
    await ws.send_bytes(p.build_group_idle(ident))


def build_config() -> cfgmod.Config:
    return cfgmod.from_dict({
        "log_level": "INFO",
        "timezone": "Europe/Vienna",
        "brew": {
            "url": f"http://127.0.0.1:{PORT}",
            "username": USERNAME,
            "password": PASSWORD,
            "issi": 2320997,
            "ping_interval_seconds": 0,
        },
        "status_server": {"enabled": False},
        "sds_control": {"coding_scheme": "8bit"},
        "audio": {
            "backend": "loopback",
            "loopback": {"target_gssi": 2324, "source_issi": 2320998},
        },
        "reflectors": [{"name": "R2", "enabled": True}],
        "routes": [
            {"gssi": 2321, "reflector": "R2", "tg": 2321, "priority": 5},
        ],
    })


async def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)-12s %(message)s")
    runner = await run_server()

    cfg = build_config()
    state = AppState()
    router = Router(cfg, state)
    client = BrewClient(cfg.brew, router)
    router.attach(client)

    task = asyncio.create_task(client.run())
    for _ in range(50):
        if client.connected and "ws" in ws_ref:
            break
        await asyncio.sleep(0.1)

    ok = True

    def check(name, condition, detail=""):
        nonlocal ok
        print(f"  {'ok   ' if condition else 'FEHL '} {name} {detail}")
        ok = ok and bool(condition)

    print("Verbindungsaufbau:")
    check("verbunden", client.connected)
    check("Digest akzeptiert", ("auth-ok", USERNAME) in received)
    check("X-Brew-Mode Terminal",
          any(k == "ws-open" and v == "Terminal" for k, v in received))

    await asyncio.sleep(0.3)
    subs = [m for k, m in received if k == "SubscriberMessage"]
    check("REGISTER gesendet", any(m.type == p.SUB_REGISTER for m in subs))
    affiliate = [m for m in subs if m.type == p.SUB_AFFILIATE]
    check("AFFILIATE gesendet", bool(affiliate))
    if affiliate:
        check("die eine GSSI affiliiert",
              sorted(affiliate[-1].groups) == [2321],
              str(affiliate[-1].groups))

    print("\nGruppenruf TETRA -> Bruecke -> Loopback:")
    received.clear()
    await inject_call(gssi=2321, issi=2325001, frames=3)
    await asyncio.sleep(0.5)

    calls = [m for k, m in received if k == "CallMessage"]
    frames = [m for k, m in received if k == "FrameMessage"]
    tx = [m for m in calls if m.type == p.CALL_GROUP_TX]
    idle = [m for m in calls if m.type == p.CALL_GROUP_IDLE]

    check("Loopback hat GROUP_TX gesendet", bool(tx))
    if tx:
        check("auf die Ziel-GSSI 2324", tx[0].group.destination == 2324,
              str(tx[0].group.destination))
        check("Rufzeichen im Mnemonic", tx[0].group.mnemonic_text == "OE1SXM",
              repr(tx[0].group.mnemonic_text))
    check("3 Frames gespiegelt", len(frames) == 3, f"ist {len(frames)}")
    if frames:
        ste, sub1, sub2 = p.split_traffic_frame(frames[0].data)
        check("Frame unveraendert durchgereicht", sub1 == 1 and sub2 == 100,
              f"sub1={sub1} sub2={sub2}")
    check("GROUP_IDLE gesendet", bool(idle))
    check("Last Heard befuellt", len(state.last_heard) == 1)
    if state.last_heard:
        entry = state.last_heard[0]
        check("Last Heard: richtige ISSI", entry.issi == 2325001)
        check("Last Heard: 3 Frames", entry.frames == 3, str(entry.frames))
    check("kein Call mehr offen", not router.calls)
    check("Floor wieder frei", not router.floors)

    print("\nUnbekannte GSSI:")
    received.clear()
    await inject_call(gssi=9999, issi=2325001, frames=1)
    await asyncio.sleep(0.3)
    check("nichts weitergeleitet",
          not [m for k, m in received if k == "CallMessage"])
    check("Zaehler dropped_no_route", state.counters["dropped_no_route"] >= 1)

    print("\nHerunterfahren mit offenem Ruf:")
    received.clear()
    ws = ws_ref["ws"]
    ident = uuid.uuid4()
    await ws.send_bytes(p.build_group_tx(ident, source=2325003,
                                         destination=2321))
    await ws.send_bytes(p.build_frame(
        p.FRAME_TRAFFIC_CHANNEL, ident, bytes(36)))
    await asyncio.sleep(0.3)
    check("Ruf laeuft", len(router.calls) == 1)

    # Genau das passiert beim Beenden der Bruecke.
    await router.close_calls()
    check("close_calls raeumt ihn weg", not router.calls, str(router.calls))
    check("Floor wieder frei", not router.floors, str(router.floors))

    print("\nVerwaister Ruf (Geraet weg, kein GROUP_IDLE):")
    from app import routing as _routing
    received.clear()
    ident = uuid.uuid4()
    ws = ws_ref["ws"]
    await ws.send_bytes(p.build_group_tx(ident, source=2325002,
                                         destination=2321))
    await ws.send_bytes(p.build_frame(
        p.FRAME_TRAFFIC_CHANNEL, ident, bytes(36)))
    await asyncio.sleep(0.3)
    check("Ruf laeuft und belegt den Reflektor",
          len(router.calls) == 1 and bool(router.floors),
          f"{len(router.calls)} Rufe, Floors {router.floors}")

    # Zeit vorspulen statt fuenf Sekunden zu warten.
    for laufend in router.calls.values():
        laufend.last_frame -= _routing.CALL_TIMEOUT + 1
    await router.tick()
    check("Wachhund raeumt ihn weg", not router.calls, str(router.calls))
    check("und gibt den Reflektor frei", not router.floors, str(router.floors))
    check("Zaehler dropped_stale",
          state.counters.get("dropped_stale") == 1,
          str(state.counters.get("dropped_stale")))

    print("\nSDS an die Bruecken-ISSI (keine Kommandos mehr):")
    received.clear()
    ws = ws_ref["ws"]
    ident = uuid.uuid4()
    await ws.send_bytes(p.build_short_transfer(ident, 2325001, 2320997))
    from app.brew import sds as sdsmod
    await ws.send_bytes(
        p.build_frame(p.FRAME_SDS_TRANSFER, ident, sdsmod.encode_text("2321"))
    )
    await asyncio.sleep(0.3)
    check("liegt als Text im Postfach", any(
        m["text"] == "2321" and m["kind"] == "text"
        for m in router.sdsbox.all()), str(router.sdsbox.all()))
    check("keine Antwort, kein Kommando",
          not [m for k, m in received if k == "FrameMessage"])

    print("\nSDS an eine andere ISSI:")
    received.clear()
    ident = uuid.uuid4()
    await ws.send_bytes(p.build_short_transfer(ident, 2325001, 2329999))
    await ws.send_bytes(p.build_frame(
        p.FRAME_SDS_TRANSFER, ident, sdsmod.encode_text("Servus")))
    await asyncio.sleep(0.3)
    letzte = router.sdsbox.all()[-1]
    check("landet trotzdem im Postfach",
          letzte["text"] == "Servus" and letzte["direction"] == "rx")
    check("und gilt nicht als Kommando", letzte["kind"] == "text")
    check("keine Antwort darauf",
          not [m for k, m in received if k == "FrameMessage"])

    print("\nSDS ohne vorangehenden SHORT_TRANSFER:")
    received.clear()
    vorher = len(router.sdsbox.all())
    ident = uuid.uuid4()
    # Nur den Frame, kein SHORT_TRANSFER davor - so kam eine SDS aus einer
    # fremden App an, und sie ging frueher lautlos verloren.
    await ws.send_bytes(p.build_frame(
        p.FRAME_SDS_TRANSFER, ident, sdsmod.encode_text("Wer bin ich")))
    await asyncio.sleep(0.3)
    check("geht nicht mehr verloren",
          len(router.sdsbox.all()) == vorher + 1,
          f"{vorher} -> {len(router.sdsbox.all())}")
    letzte = router.sdsbox.all()[-1]
    check("Text ist da", letzte["text"] == "Wer bin ich", repr(letzte["text"]))
    check("Absender bleibt offen", letzte["peer"] is None, str(letzte["peer"]))
    check("und es gilt nicht als Kommando", letzte["kind"] == "text")
    check("keine Antwort ins Leere",
          not [m for k, m in received if k == "FrameMessage"])

    print("\nSDS mit unlesbarer Nutzlast:")
    vorher = len(router.sdsbox.all())
    ident = uuid.uuid4()
    await ws.send_bytes(p.build_short_transfer(ident, 2325001, 2320997))
    await ws.send_bytes(p.build_frame(
        p.FRAME_SDS_TRANSFER, ident, bytes([0x7f, 0x01, 0x02, 0x03])))
    await asyncio.sleep(0.3)
    check("auch die wird behalten",
          len(router.sdsbox.all()) == vorher + 1)
    letzte = router.sdsbox.all()[-1]
    check("mit Rohbytes zur Fehlersuche",
          letzte.get("raw", "").startswith("7f "), str(letzte.get("raw")))

    print("\nSDS aus der WebUI verschicken:")
    received.clear()
    eintrag = router.send_text_sds(2325001, "Hallo aus der WebUI")
    await asyncio.sleep(0.3)
    check("Eintrag angelegt", eintrag["direction"] == "tx"
          and eintrag["peer"] == 2325001)
    raus = [m for k, m in received if k == "FrameMessage"]
    check("beim Server angekommen", bool(raus))
    if raus:
        parsed = sdsmod.decode_text(raus[0].data)
        check("Text unverfaelscht",
              parsed is not None and parsed.text == "Hallo aus der WebUI",
              repr(parsed.text if parsed else None))
    for schlecht, warum in ((("", "leerer Text")), (("   ", "nur Leerzeichen"))):
        try:
            router.send_text_sds(2325001, schlecht)
            check(f"{warum} abgelehnt", False)
        except ValueError:
            check(f"{warum} abgelehnt", True)
    try:
        router.send_text_sds(0, "hallo")
        check("Ziel 0 abgelehnt", False)
    except ValueError:
        check("Ziel 0 abgelehnt", True)

    await client.stop()
    task.cancel()
    await runner.cleanup()

    print("\n" + ("Smoketest ok." if ok else "Smoketest FEHLGESCHLAGEN."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
