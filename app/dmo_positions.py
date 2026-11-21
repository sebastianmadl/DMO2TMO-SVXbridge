"""DMO positions for the brew-server map (Brew extension `dmo-position`).

The DMO gateways of the TETRA-AUSTRIA network report to the
`tetra_fw_server` (UDP 5220, docs/tetra_fw_protocol.md), and its dashboard
shows the result in the Lastheard:

    GPS   a DMO user's position (call, ISSI, gateway that heard it)
    POS   a gateway's own beacon (gateway call, gateway ID, position)
    VOICE a DMO user talking through a gateway

plus a table of the nodes connected to the reflector. The bridge reads that
page every `map.poll_seconds` and forwards it to the brew-server as
`SERVICE_DMO_POSITION` (0xf4 0x26, JSON), but only when the server lists
`dmo-position` in `X-Brew-Extensions` on the 101. TetraPack, BrandMeister and
older brew-servers never offer it, so they never see the message.

Messages (agreed format, handover/map-rework/dmo-positions-for-brew-server.md):

    {"kind": "gateway", "issi": 232102, "lat": .., "lon": .., "name": "..",
     "online": true}
    {"kind": "user", "issi": 2321109, "via": 232102, "lat": .., "lon": ..,
     "at_ms": 1763710000000, "name": "OE1GKS"}

A user without lat/lon means "heard on this gateway, no position": the map
docks it next to the gateway.

Without the extension nothing new is sent. Only if `map.lip_fallback` is on,
a user's position goes out as a plain LIP short location report SDS with the
user's ISSI as source, which every brew-server already decodes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import config as cfgmod
from .brew import protocol as p
from .oe_dashboard import Dashboard, DashboardLookup, Eintrag

log = logging.getLogger("dmo_pos")

GATEWAY_REPEAT = 600.0      # every gateway again every 10 min
NO_USER = "NO USER"


@dataclass
class Gateway:
    call: str
    issi: int
    lat: float
    lon: float
    name: str
    online: bool
    source: str             # "settings", "dashboard" or a mix

    def message(self) -> dict:
        return {"kind": "gateway", "issi": self.issi,
                "lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "name": self.name, "online": self.online}


def _ms(ts: Optional[float]) -> Optional[int]:
    return int(ts * 1000) if ts else None


def user_message(issi: int, via: int, lat: Optional[float] = None,
                 lon: Optional[float] = None, at: Optional[float] = None,
                 name: str = "") -> dict:
    msg: dict = {"kind": "user", "issi": int(issi), "via": int(via)}
    if lat is not None and lon is not None and not (lat == 0 and lon == 0):
        msg["lat"] = round(lat, 6)
        msg["lon"] = round(lon, 6)
    if at:
        msg["at_ms"] = _ms(at)
    name = (name or "").strip()
    if name and name.upper() != NO_USER:
        msg["name"] = name
    return msg


# ------------------------------------------------------------------ LIP

def _put(bits: list[int], value: int, count: int) -> None:
    for i in range(count - 1, -1, -1):
        bits.append((value >> i) & 1)


def lip_short_report(lat: float, lon: float, age: float = 0.0) -> bytes:
    """LIP short location report (TS 100 392-18), PID 0x0A first.

    Layout as decoded by brew-server `position::decode_lip`: PDU type 2,
    time elapsed 2, longitude 25 (signed, 360/2^25), latitude 24 (signed,
    180/2^24), position error 3, horizontal velocity 7, direction of travel
    4, type of additional data 1, additional data 8. The code values for
    time elapsed, position error, velocity "unknown" and the reason for
    sending are not checked against the TS 100 392-18 text (not in the
    project files); brew-server only reads latitude and longitude.
    """
    if age < 5:
        elapsed = 0
    elif age < 300:
        elapsed = 1
    elif age < 1800:
        elapsed = 2
    else:
        elapsed = 3
    lon_raw = round(lon * (1 << 25) / 360.0) & ((1 << 25) - 1)
    lat_raw = round(lat * (1 << 24) / 180.0) & ((1 << 24) - 1)
    bits: list[int] = []
    _put(bits, 0, 2)            # short location report
    _put(bits, elapsed, 2)
    _put(bits, lon_raw, 25)
    _put(bits, lat_raw, 24)
    _put(bits, 2, 3)            # position error (ddmm.mm is ~20 m)
    _put(bits, 127, 7)          # horizontal velocity unknown
    _put(bits, 0, 4)            # direction of travel
    _put(bits, 0, 1)            # type of additional data: reason for sending
    _put(bits, 0, 8)            # reason for sending
    while len(bits) % 8:
        bits.append(0)
    out = bytearray([0x0A])
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out)


# ------------------------------------------------------------- Sender

class DmoPositions:
    """Reads the dashboard, sends gateways and users. Reads the config on
    every poll, so a change in the WebUI applies without a restart."""

    def __init__(self, get_cfg: Callable[[], cfgmod.Config],
                 dashboard: DashboardLookup,
                 own_gateway: Callable[[], str]):
        self._get_cfg = get_cfg
        self.dashboard = dashboard
        self._own_gateway = own_gateway
        self.client = None
        self._task: Optional[asyncio.Task] = None
        self._next_poll = 0.0
        self._warned_no_ext = False
        # Last beacon per gateway; the dashboard drops old rows, the
        # gateway's position does not change.
        self._beacons: dict[str, Eintrag] = {}
        self._online: dict[str, bool] = {}
        self._reset_sent()
        # Status for the WebUI
        self.last_poll: Optional[float] = None
        self.last_error = ""
        self.sent_gateways = 0
        self.sent_users = 0
        self.sent_lip = 0
        self.gateways: dict[str, Gateway] = {}
        self.unresolved: dict[str, str] = {}

    def _reset_sent(self) -> None:
        self._gw_sent: dict[str, tuple] = {}         # call -> state sent
        self._gw_sent_at: dict[str, float] = {}
        self._user_key: dict[int, tuple] = {}         # issi -> fix sent
        self._user_at: dict[int, float] = {}          # issi -> monotonic
        self._user_via: dict[int, int] = {}           # issi -> gateway docked

    # ------------------------------------------------------- lifecycle

    def attach(self, client) -> None:
        self.client = client

    def on_connected(self, client) -> None:
        """New Brew connection: the server may have restarted, send all again."""
        self.attach(client)
        self._reset_sent()
        self._warned_no_ext = False
        self._next_poll = 0.0

    def on_disconnected(self) -> None:
        self._next_poll = 0.0

    def changed(self) -> None:
        """Settings saved: send the gateways again at the next tick."""
        self._gw_sent_at.clear()
        self._next_poll = 0.0

    def tick(self) -> None:
        """Called once a second by the router; starts a poll when due."""
        cfg = self._get_cfg()
        if not cfg.map.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        if time.monotonic() < self._next_poll:
            return
        self._next_poll = time.monotonic() + cfg.map.poll_seconds
        self._task = asyncio.create_task(self._poll_safe(), name="dmo-pos")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _poll_safe(self) -> None:
        try:
            await self.poll()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc) or type(exc).__name__
            log.exception("DMO positions: poll failed")

    # ---------------------------------------------------------- mode

    def mode(self) -> str:
        """"extension", "lip" or "" (nothing goes out)."""
        client = self.client
        if client is None or not getattr(client, "connected", False):
            return ""
        if client.has_extension(p.EXT_DMO_POSITION):
            return "extension"
        if self._get_cfg().map.lip_fallback:
            return "lip"
        return ""

    def _url(self, cfg: cfgmod.Config) -> str:
        reflektor = cfg.active_reflector()
        return cfg.tetra_fw.dashboard_url_for(
            reflektor.host if reflektor else "")

    # ---------------------------------------------------------- poll

    async def poll(self, now: Optional[float] = None) -> None:
        """Reads the dashboard. Without a mode it only works out the
        gateways for the WebUI and sends nothing."""
        cfg = self._get_cfg()
        mode = self.mode()
        if (not mode and self.client is not None
                and getattr(self.client, "connected", False)
                and not self._warned_no_ext):
            log.info("brew-server does not offer %s, DMO positions are "
                     "not sent", p.EXT_DMO_POSITION)
            self._warned_no_ext = True
        url = self._url(cfg)
        if not url:
            self.last_error = "no dashboard address"
            return
        seite = await self.dashboard.seite(url, max_cache=1.0)
        if self.dashboard.last_error:
            self.last_error = self.dashboard.last_error
            return
        self.last_error = ""
        self.last_poll = time.time()
        self.process(seite, mode, now=now)

    def process(self, seite: Dashboard, mode: str,
                now: Optional[float] = None) -> None:
        """Works out gateways and users from one dashboard page and sends."""
        cfg = self._get_cfg()
        now = time.time() if now is None else now
        own = (self._own_gateway() or "").strip().upper()
        max_age = cfg.map.max_age_hours * 3600

        for e in seite.eintraege:
            if e.typ == "POS" and _same(e.call, e.gateway):
                alt = self._beacons.get(e.gateway.upper())
                if alt is None or (e.zeit or 0) >= (alt.zeit or 0):
                    self._beacons[e.gateway.upper()] = e
        if seite.verbunden is not None:
            for call in set(self._beacons) | {g.call.strip().upper()
                                             for g in cfg.map.gateways}:
                self._online[call] = call in seite.verbunden

        def fresh(e: Eintrag) -> bool:
            return not max_age or (e.zeit is not None and now - e.zeit <= max_age)

        # Gateways that carry users, so a missing one shows in the WebUI
        heard = {e.gateway.strip().upper() for e in seite.eintraege
                 if e.typ != "POS" and fresh(e)}
        gateways = self._resolve_gateways(cfg, own, heard)
        if mode == "extension":
            self._send_gateways(gateways)
        self.gateways = gateways
        if not mode:
            return

        # Oldest first, so the newest fix of a user is sent last.
        zeilen = sorted(seite.eintraege, key=lambda e: e.zeit or 0)
        for e in zeilen:
            gw = gateways.get(e.gateway.strip().upper())
            if gw is None or _same(e.gateway, own) or not fresh(e):
                continue
            if e.issi == gw.issi or _same(e.call, e.gateway):
                continue                     # the gateway's own beacon
            if e.typ == "VOICE":
                if mode == "extension":
                    self._dock(e, gw)
            elif e.lat is not None and e.lon is not None:
                self._position(e, gw, mode, cfg, now)

    # ------------------------------------------------------ gateways

    def _resolve_gateways(self, cfg: cfgmod.Config, own: str,
                          heard: frozenset = frozenset()) -> dict[str, Gateway]:
        calls = {g.call.strip().upper() for g in cfg.map.gateways}
        if cfg.map.auto_gateways:
            calls |= set(self._beacons)
        calls.discard(own)
        calls.discard("")
        out: dict[str, Gateway] = {}
        # Reason codes (the WebUI words them): unknown = carries users but has
        # neither a beacon nor an entry; no_issi; no_position;
        # own_issi:<issi>; dup_issi:<issi>:<other call>
        unresolved: dict[str, str] = {}
        issis: dict[int, str] = {}
        for call in sorted(set(heard) - calls - {own, ""}):
            unresolved[call] = "unknown"
        for call in sorted(calls):
            entry = cfg.map.gateway(call)
            beacon = self._beacons.get(call)
            issi = (entry.issi if entry and entry.issi else
                    beacon.issi if beacon else 0)
            if entry and entry.lat is not None:
                lat, lon = float(entry.lat), float(entry.lon)
            elif beacon is not None:
                lat, lon = beacon.lat, beacon.lon
            else:
                lat = lon = None
            if not issi:
                unresolved[call] = "no_issi"
                continue
            if lat is None:
                unresolved[call] = "no_position"
                continue
            if issi == cfg.brew.issi:
                unresolved[call] = f"own_issi:{issi}"
                continue
            if issi in issis:
                unresolved[call] = f"dup_issi:{issi}:{issis[issi]}"
                continue
            issis[issi] = call
            name = ((entry.name if entry else "") or
                    (beacon.comment if beacon else "") or
                    (entry.call.strip() if entry else call))
            source = ("settings" if entry and entry.issi and entry.lat is not None
                      else "dashboard" if entry is None
                      else "settings + dashboard")
            out[call] = Gateway(call=call, issi=issi, lat=lat, lon=lon,
                                name=name, online=self._online.get(call, True),
                                source=source)
        for call, why in unresolved.items():
            if self.unresolved.get(call) != why:
                log.info("DMO gateway %s not on the map: %s", call, why)
        self.unresolved = unresolved
        return out

    def _send_gateways(self, gateways: dict[str, Gateway]) -> None:
        jetzt = time.monotonic()
        for call, gw in gateways.items():
            msg = gw.message()
            state = tuple(sorted(msg.items()))
            if (self._gw_sent.get(call) == state
                    and jetzt - self._gw_sent_at.get(call, 0) < GATEWAY_REPEAT):
                continue
            if self._gw_sent.get(call) != state:
                log.info("DMO gateway %s: ISSI %s %.5f/%.5f %s", call,
                         gw.issi, gw.lat, gw.lon,
                         "online" if gw.online else "offline")
            self._send(msg)
            self._gw_sent[call] = state
            self._gw_sent_at[call] = jetzt
            self.sent_gateways += 1
        # Gone from the settings / no longer resolvable: offline once.
        for call in [c for c in self._gw_sent if c not in gateways]:
            alt = dict(self._gw_sent.pop(call))
            self._gw_sent_at.pop(call, None)
            if alt.get("online"):
                alt["online"] = False
                self._send(alt)
                self.sent_gateways += 1

    # --------------------------------------------------------- users

    def _dock(self, e: Eintrag, gw: Gateway) -> None:
        """First heard on this gateway: announce without a position."""
        if self._user_via.get(e.issi) == gw.issi:
            return
        self._user_via[e.issi] = gw.issi
        self._send(user_message(e.issi, gw.issi, at=e.zeit, name=e.call))
        self.sent_users += 1

    def _position(self, e: Eintrag, gw: Gateway, mode: str,
                  cfg: cfgmod.Config, now: float) -> None:
        key = (e.zeit_text, round(e.lat, 6), round(e.lon, 6), gw.issi)
        if self._user_key.get(e.issi) == key:
            return
        jetzt = time.monotonic()
        letzte = self._user_at.get(e.issi)
        if letzte is not None and jetzt - letzte < cfg.map.user_min_interval:
            return                  # comes at a later poll
        self._user_key[e.issi] = key
        self._user_at[e.issi] = jetzt
        self._user_via[e.issi] = gw.issi
        if mode == "extension":
            self._send(user_message(e.issi, gw.issi, e.lat, e.lon, e.zeit,
                                    e.call))
            self.sent_users += 1
        elif mode == "lip":
            age = max(0.0, now - e.zeit) if e.zeit else 1e9
            ziel = cfg.map.lip_destination or gw.issi
            self.client.send_sds(ziel, lip_short_report(e.lat, e.lon, age),
                                 source=e.issi)
            self.sent_lip += 1

    def _send(self, msg: dict) -> None:
        self.client.send(p.build_dmo_position(msg))

    # -------------------------------------------------------- status

    def status(self) -> dict:
        return {
            "mode": self.mode(),
            "offered": bool(self.client is not None
                            and self.client.has_extension(p.EXT_DMO_POSITION)),
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "sent_gateways": self.sent_gateways,
            "sent_users": self.sent_users,
            "sent_lip": self.sent_lip,
            "gateways": [
                {"call": g.call, "issi": g.issi, "name": g.name,
                 "lat": round(g.lat, 6), "lon": round(g.lon, 6),
                 "online": g.online, "source": g.source}
                for g in self.gateways.values()],
            "unresolved": [{"call": c, "why": w}
                           for c, w in sorted(self.unresolved.items())],
        }


def _same(a: str, b: str) -> bool:
    return (a or "").strip().upper() == (b or "").strip().upper()
