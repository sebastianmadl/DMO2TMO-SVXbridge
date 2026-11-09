"""Der Router - hier faellt die Entscheidung, was wohin darf.

Mapping-Tabelle: GSSI -> (Reflektor, TG). Fixe Zeilen gewinnen immer,
die dynamische Zeile greift nur auf ihrer eigenen GSSI und zeigt auf die
zuletzt per SDS gewaehlte TG.

Der Floor-Lock haengt am Reflektor, nicht global: R1 und R2 stoeren
einander nicht, weil sie getrennte svxlink-Instanzen sind.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import config as cfgmod
from .audio import acelp
from .audio.base import (AudioSink, LoopbackSink, NullSink, UsrpSink,
                         apply_gain)
from .audio.source import UsrpSource
from .brew import protocol as p
from .brew import sds as sdsmod
from .sdsbox import RX, TX, SdsBox
from .brew.client import BrewHandler
from .schedule import RouteScheduler
from .state import AppState, LastHeardEntry

log = logging.getLogger("routing")

SHORT_TRANSFER_TTL = 60.0     # laut Spec verfallen SDS-Transaktionen nach 60 s
# Kommt so lange kein Frame mehr, gilt ein Gruppenruf als verwaist. Das
# passiert, wenn ein Geraet aus der Versorgung faellt oder ein Client
# abgeschossen wird: das GROUP_IDLE kommt dann nie, und ohne diesen
# Wachhund bliebe der Floor des Reflektors dauerhaft gesperrt - der
# Reflektor waere bis zum Neustart tot. TETRA liefert alle 60 ms einen
# Frame, fuenf Sekunden Stille sind also eindeutig.
CALL_TIMEOUT = 5.0

# Weiterleitung nach dem Muster von TetraLogic: "to:OE1ABC:Text" oder
# kurz "OE1ABC:Text". Alles andere geht nur als Sds:info an den Reflektor.
SDS_ZIEL = re.compile(
    r"^\s*(?:to:)?([A-Za-z0-9]{1,3}[0-9][A-Za-z0-9]{1,4}(?:-[A-Za-z0-9]{1,4})?)"
    r"\s*:\s*(.+)$", re.S)

# 24 Bit: mehr passt weder ins DMR-ID-Feld der USRP-Metadaten noch in eine
# ISSI.
MAX_ISSI = 0xFFFFFF


@dataclass
class InboundCall:
    identifier: _uuid.UUID
    gssi: int
    route: cfgmod.Route
    source: Optional[int]
    callsign: str = ""
    # Ziel-TG am Reflektor. Muss mit in den USRP-Header, sonst meldet
    # svxlink "Talker stop: unknown TG# 0" und verwirft den Sprecher.
    tg: int = 0
    started: float = field(default_factory=time.time)
    # Wann kam der letzte Frame? Ohne das bleibt ein Ruf ewig offen, wenn
    # das GROUP_IDLE nie kommt - siehe CALL_TIMEOUT.
    last_frame: float = field(default_factory=time.time)
    frames: int = 0
    sink: Optional[AudioSink] = None
    sink_state: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return time.time() - self.started


@dataclass
class OutboundCall:
    """Ein Sprechvorgang von der SVX-Seite Richtung TETRA."""
    identifier: _uuid.UUID
    route: cfgmod.Route
    reflector: str
    callsign: str = ""
    started: float = field(default_factory=time.time)
    frames: int = 0
    issi: Optional[int] = None      # durchgereichte Quell-ISSI, falls bekannt

    @property
    def duration(self) -> float:
        return time.time() - self.started


@dataclass
class Floor:
    gssi: int
    priority: int
    identifier: _uuid.UUID
    since: float = field(default_factory=time.time)


class Router(BrewHandler):
    def __init__(self, cfg: cfgmod.Config, state: AppState):
        self.cfg = cfg
        self.state = state
        self.client = None
        # Postfach fuer die WebUI. main.py haengt eines mit Datei ein; ohne
        # das laeuft der Router genauso, nur ohne Gedaechtnis.
        self.sdsbox = SdsBox()
        self.schedulers: dict[int, RouteScheduler] = {
            route.gssi: RouteScheduler(route, cfg.timezone) for route in cfg.routes
        }
        # dynamische Zeile: aktuell gewaehlte TG je GSSI
        self.dynamic_tg: dict[int, int] = {
            route.gssi: route.default_tg
            for route in cfg.routes
            if route.type == cfgmod.ROUTE_DYNAMIC
        }
        self.dynamic_last_used: dict[int, float] = {}
        self.floors: dict[str, Floor] = {}
        self.calls: dict[_uuid.UUID, InboundCall] = {}
        self._short_transfers: dict[_uuid.UUID, tuple[float, p.ShortData]] = {}
        self._sinks: dict[str, AudioSink] = {}
        self._pending_lookups: set[int] = set()
        # Richtung SVX -> TETRA
        self.sources: dict[str, UsrpSource] = {}
        self.outbound: dict[str, OutboundCall] = {}
        # Ein Encoder je Reflektor. Einer fuer alle ginge nicht: der
        # ETSI-Code haelt seinen Zustand global, zwei gleichzeitige
        # Aussendungen wuerden sich gegenseitig verfaelschen.
        self._tx_codecs: dict[str, object] = {}
        self._tx_codec_failed = False
        # Wird gerufen, wenn eine dynamische Zeile auf eine andere TG zeigt.
        # svxlink erfaehrt das sonst nie: es sendet auf seinem DEFAULT_TG aus
        # der svxlink.conf und hoert nur MONITOR_TGS mit. Die Umschaltung
        # muss also die Konfiguration neu schreiben und die Instanz neu
        # starten - main.py haengt sich hier ein.
        self.on_dynamic_change = None
        self._dynamic_pending: set[str] = set()

    def _mark_dynamic(self, route: cfgmod.Route) -> None:
        if route.reflector:
            self._dynamic_pending.add(route.reflector)

    def dynamic_tgs_by_reflector(self) -> dict[str, int]:
        """Aktuell gewaehlte TG je Reflektor - Futter fuer confgen."""
        out: dict[str, int] = {}
        for route in self.cfg.routes:
            if route.type == cfgmod.ROUTE_DYNAMIC and route.enabled:
                out[route.reflector] = self.dynamic_tg.get(
                    route.gssi, route.default_tg)
        return out

    def attach(self, client) -> None:
        self.client = client

    async def reload(self, cfg: cfgmod.Config) -> list[str]:
        """Uebernimmt eine geaenderte Konfiguration im laufenden Betrieb.

        Routen lassen sich live tauschen: Zeitplaene werden neu aufgebaut,
        der Handbetrieb bleibt erhalten wo die GSSI gleich geblieben ist,
        und die Affiliation wird nachgezogen.

        Was **nicht** live geht, gibt die Funktion als Liste zurueck - das
        sind Aenderungen an Reflektoren und Ports, dafuer muessen die
        svxlink-Instanzen neu starten.
        """
        needs_restart = []
        alt = {r.name: r for r in self.cfg.reflectors}
        for reflector in cfg.reflectors:
            vorher = alt.get(reflector.name)
            if vorher is None or asdict(vorher) != asdict(reflector):
                needs_restart.append(reflector.name)
        for name in alt:
            if not any(r.name == name for r in cfg.reflectors):
                needs_restart.append(name)

        # Laufende Rufe auf Zeilen, die es nicht mehr gibt, sauber beenden.
        bleibt = {route.gssi for route in cfg.routes}
        for call in list(self.calls.values()):
            if call.gssi not in bleibt:
                await self._close_call(call, reason="Zeile entfernt")
        for name, call in list(self.outbound.items()):
            if call.route.gssi not in bleibt:
                await self.on_svx_stop(name)

        merker = {gssi: (s.manual, s._manual_until)
                  for gssi, s in self.schedulers.items()}
        self.cfg = cfg
        self.schedulers = {}
        for route in cfg.routes:
            scheduler = RouteScheduler(route, cfg.timezone)
            manual, until = merker.get(route.gssi, (None, None))
            scheduler.manual = manual
            scheduler._manual_until = until
            self.schedulers[route.gssi] = scheduler

        vorher_dyn = dict(self.dynamic_tg)
        self.dynamic_tg = {
            route.gssi: vorher_dyn.get(route.gssi, route.default_tg)
            for route in cfg.routes if route.type == cfgmod.ROUTE_DYNAMIC
        }
        self._sinks.clear()          # Senken haengen an den Reflektordaten

        if self.client is not None:
            await self.client.affiliate(self.active_gssis())
        log.info("Konfiguration neu geladen: %d Zeilen%s", len(cfg.routes),
                 f", Neustart noetig fuer {', '.join(needs_restart)}"
                 if needs_restart else "")
        return sorted(set(needs_restart))

    # ------------------------------------------------------------- Tabelle

    def route_for(self, gssi: int) -> Optional[cfgmod.Route]:
        return self.cfg.route_for_gssi(gssi)

    def target_tg(self, route: cfgmod.Route) -> int:
        if route.type == cfgmod.ROUTE_DYNAMIC:
            return self.dynamic_tg.get(route.gssi, route.default_tg)
        return route.tg

    def active_gssis(self) -> list[int]:
        """Alle GSSIs, auf die wir affiliieren wollen."""
        out = []
        for route in self.cfg.routes:
            if not route.enabled:
                continue
            reflector = self.cfg.reflector(route.reflector)
            if reflector is not None and not reflector.enabled:
                continue
            out.append(route.gssi)
        return out

    def _sink_for(self, route: cfgmod.Route) -> AudioSink:
        backend = self.cfg.audio.backend
        key = f"{backend}:{route.reflector}"
        if key in self._sinks:
            return self._sinks[key]

        if backend == "loopback":
            lb = self.cfg.audio.loopback
            sink: AudioSink = LoopbackSink(
                self.client,
                target_gssi=lb.target_gssi,
                source_issi=lb.source_issi or None,
                callsign=lb.callsign,
            )
        elif backend == "usrp":
            sink = UsrpSink(self.cfg.reflector(route.reflector),
                            gain=self.cfg.audio.svx_factor,
                            issi_passthrough=self.cfg.tetra.issi_passthrough)
        else:
            sink = NullSink()
        self._sinks[key] = sink
        return sink

    # ---------------------------------------------------------- Verbindung

    async def on_connected(self, client) -> None:
        self.attach(client)
        await client.affiliate(self.active_gssis())

    async def on_disconnected(self, client) -> None:
        for call in list(self.calls.values()):
            await self._close_call(call, reason="Verbindung weg")
        self.floors.clear()

    # -------------------------------------------------------- Gruppenrufe

    async def on_group_tx(self, msg: p.CallMessage) -> None:
        group = msg.group
        if group is None:
            return
        gssi = group.destination
        route = self.route_for(gssi)
        if route is None:
            self.state.bump("dropped_no_route")
            log.debug("GROUP_TX auf unbekannter GSSI %s - ignoriert", gssi)
            return

        scheduler = self.schedulers[gssi]
        if not route.enabled or not scheduler.is_active():
            self.state.bump("dropped_inactive")
            log.info("GSSI %s: Ruf verworfen (%s)", gssi, scheduler._reason)
            return

        reflector = self.cfg.reflector(route.reflector)
        if reflector is not None and not reflector.enabled:
            self.state.bump("dropped_inactive")
            log.info("GSSI %s: Reflektor %s ist aus", gssi, route.reflector)
            return

        if not self._claim_floor(route, msg.identifier):
            self.state.bump("dropped_floor")
            return

        callsign = self.state.callsign(group.source, group.mnemonic_text)
        if not callsign and group.source:
            self._lookup_subscriber(group.source)

        call = InboundCall(
            identifier=msg.identifier,
            gssi=gssi,
            route=route,
            source=group.source,
            callsign=callsign,
            tg=self.target_tg(route),
            sink=self._sink_for(route),
        )
        self.calls[msg.identifier] = call
        self.state.bump("calls_rx")
        scheduler.note_traffic_start()
        if route.type == cfgmod.ROUTE_DYNAMIC:
            self.dynamic_last_used[gssi] = time.time()

        log.info(
            "RX GSSI %s -> %s/TG %s von ISSI %s%s",
            gssi, route.reflector, self.target_tg(route), group.source,
            f" ({callsign})" if callsign else "",
        )
        if call.sink is not None:
            await call.sink.start(call)

    async def on_traffic(self, frame: p.FrameMessage) -> None:
        call = self.calls.get(frame.identifier)
        if call is None:
            return
        call.frames += 1
        call.last_frame = time.time()
        self.state.bump("frames_rx")
        if call.sink is not None:
            await call.sink.frame(call, frame.data)

    async def on_group_idle(self, msg: p.CallMessage) -> None:
        call = self.calls.get(msg.identifier)
        if call is None:
            self._release_floor(msg.identifier)
            return
        await self._close_call(call, reason="GROUP_IDLE")

    async def _close_call(self, call: InboundCall, reason: str = "") -> None:
        self.calls.pop(call.identifier, None)
        self._release_floor(call.identifier)
        scheduler = self.schedulers.get(call.gssi)
        if scheduler is not None:
            scheduler.note_traffic_end()
        if call.sink is not None:
            await call.sink.stop(call)
        self.state.add_last_heard(
            LastHeardEntry(
                timestamp=call.started,
                direction="tetra->svx",
                gssi=call.gssi,
                tg=self.target_tg(call.route),
                reflector=call.route.reflector,
                issi=call.source,
                callsign=call.callsign or self.state.callsign(call.source),
                duration=call.duration,
                frames=call.frames,
            )
        )
        log.info("Call %s zu Ende (%s), %d Frames / %.1f s",
                 call.identifier, reason, call.frames, call.duration)

    # ----------------------------------------------------------- Floor-Lock

    def _claim_floor(self, route: cfgmod.Route, identifier: _uuid.UUID) -> bool:
        key = route.reflector or "_"
        current = self.floors.get(key)
        if current is None:
            self.floors[key] = Floor(route.gssi, route.priority, identifier)
            return True
        if current.identifier == identifier:
            return True
        if route.priority > current.priority:
            log.info("Reflektor %s: GSSI %s (Prio %s) verdraengt GSSI %s (Prio %s)",
                     key, route.gssi, route.priority,
                     current.gssi, current.priority)
            self.floors[key] = Floor(route.gssi, route.priority, identifier)
            return True
        log.info("Reflektor %s belegt von GSSI %s - GSSI %s wartet nicht, faellt raus",
                 key, current.gssi, route.gssi)
        return False

    def _release_floor(self, identifier: _uuid.UUID) -> None:
        for key, floor in list(self.floors.items()):
            if floor.identifier == identifier:
                del self.floors[key]

    # ------------------------------------------------------------------ SDS

    async def on_call(self, msg: p.CallMessage) -> None:
        if msg.type == p.CALL_SHORT_TRANSFER and msg.short is not None:
            self._short_transfers[msg.identifier] = (time.time(), msg.short)
            self._expire_short_transfers()

    async def on_sds(self, frame: p.FrameMessage) -> None:
        if frame.type != p.FRAME_SDS_TRANSFER:
            log.info("SDS-Report empfangen (%d Byte) - nichts zu tun",
                     len(frame.data))
            return
        self.state.bump("sds_rx")

        # Der SHORT_TRANSFER davor nennt Absender und Ziel. Fehlt er, war die
        # Nachricht frueher einfach weg - auf DEBUG protokolliert, also im
        # Betrieb unsichtbar. Jetzt wird sie trotzdem behalten, nur ohne
        # Absender: eine SDS darf nicht spurlos verschwinden.
        entry = self._short_transfers.pop(frame.identifier, None)
        short = entry[1] if entry else None
        if short is None:
            log.warning("SDS ohne vorangehenden SHORT_TRANSFER (%s), %d Byte: "
                        "%s - Absender unbekannt, Nachricht wird behalten",
                        frame.identifier, len(frame.data),
                        frame.data[:32].hex(" "))

        quelle = short.source if short else None
        parsed = sdsmod.decode_text(frame.data)
        if parsed is None or not parsed.text.strip():
            log.warning("SDS von %s nicht lesbar (PID 0x%02x, %d Byte): %s",
                        quelle if quelle else "?",
                        frame.data[0] if frame.data else 0,
                        len(frame.data), frame.data[:32].hex(" "))
            self.sdsbox.add(RX, quelle, "(nicht lesbar)", kind="text",
                            scheme="?", raw=frame.data)
            return

        # DMO2TMO: keine TG-Kommandos mehr. Jede Text-SDS landet im
        # Postfach und geht - wenn eingeschaltet - ueber svxlink an den
        # Reflektor.
        self.sdsbox.add(RX, quelle, parsed.text, kind="text",
                        scheme=parsed.mode, raw=frame.data)
        log.info("SDS von ISSI %s (%s): %r", quelle if quelle else "?",
                 parsed.mode, parsed.text)
        self.forward_sds_to_svx(quelle, parsed.text)

    def forward_sds_to_svx(self, source: Optional[int], text: str) -> bool:
        """Text-SDS an die UsrpLogic: Sds:info bzw. ForwardSds:info.

        "to:OE1ABC:Hallo" oder "OE1ABC:Hallo" bekommt ein Ziel und wird am
        Reflektor an das Gateway von OE1ABC weitergereicht. Ohne Ziel
        landet die Nachricht nur in der Uebersicht des Reflektors.
        """
        if not self.cfg.tetra.sds_forward or self.cfg.audio.backend != "usrp":
            return False
        route = self.cfg.routes[0] if self.cfg.routes else None
        if route is None:
            return False
        sink = self._sink_for(route)
        if not isinstance(sink, UsrpSink):
            return False
        ziel = ""
        inhalt = text.strip()
        treffer = SDS_ZIEL.match(inhalt)
        if treffer:
            ziel, inhalt = treffer.group(1).upper(), treffer.group(2).strip()
        nachricht = {
            "source_issi": source or 0,
            "source_call": self.state.callsign(source) if source else "",
            "dest_call": ziel,
            "text": inhalt,
        }
        ok = sink.send_text(b"SDS:" + json.dumps(
            nachricht, ensure_ascii=False).encode("utf-8"))
        if ok:
            log.info("SDS an svxlink%s: %r",
                     f" fuer {ziel}" if ziel else " (ohne Ziel)", inhalt)
        return ok

    async def on_svx_sds(self, reflector_name: str, data: dict) -> None:
        """SDS vom Reflektor (ForwardSds:info) an einen TETRA-Teilnehmer."""
        if not self.cfg.tetra.sds_forward:
            log.info("SDS von svxlink verworfen (sds_forward aus): %r", data)
            return
        ziel = str(data.get("dest_call") or "").strip()
        text = str(data.get("text") or "").strip()
        absender = str(data.get("source_call") or "").strip()
        if not ziel or not text:
            log.warning("SDS von svxlink ohne Ziel oder Text: %r", data)
            return
        issi = self.state.issi_for_callsign(ziel)
        if not issi:
            # Nicht verschlucken: ins Postfach, damit es in der WebUI steht.
            log.warning("SDS von %s fuer %s: ISSI unbekannt - nicht zugestellt "
                        "(eigenen Namen fuer %s anlegen)", absender or "?",
                        ziel, ziel)
            self.sdsbox.add(TX, None, f"{absender}>{ziel}: {text}", ok=False,
                            error=f"ISSI fuer {ziel} unbekannt")
            return
        inhalt = f"{absender}: {text}" if absender else text
        try:
            self.send_text_sds(issi, inhalt)
        except (ValueError, RuntimeError) as exc:
            log.warning("SDS fuer %s (ISSI %s) nicht zugestellt: %s",
                        ziel, issi, exc)

    def send_text_sds(self, destination: int, text: str,
                      scheme: Optional[str] = None) -> dict:
        """Kurznachricht an eine ISSI schicken und ins Postfach legen.

        Wirft ValueError, wenn Ziel oder Text nicht taugen, und RuntimeError,
        wenn gerade keine Verbindung zum brew-server steht - beides landet in
        der WebUI als lesbare Meldung.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("Text ist leer")
        if not destination or destination <= 0:
            raise ValueError("Ziel-ISSI fehlt")
        if self.client is None or not self.client.connected:
            self.sdsbox.add(TX, destination, text, ok=False,
                            error="keine Verbindung zum brew-server")
            raise RuntimeError("keine Verbindung zum brew-server")

        art = sdsmod.mode_from_name(scheme or self.cfg.sds_control.coding_scheme)
        pdu = sdsmod.build_text_pdu(text, art)
        self.client.send_sds(destination, pdu)
        self.state.bump("sds_tx")
        log.info("SDS an ISSI %s (%s): %r", destination, art, text)
        return self.sdsbox.add(TX, destination, text, scheme=art, raw=pdu)

    def handle_command(self, text: str, source: Optional[int] = None) -> str:
        """TG-Umschaltung per SDS. Formate:

            2324606          TG setzen (einzige dynamische Zeile)
            R2 2324606       TG auf einem bestimmten Reflektor setzen
            0 / off          zurueck auf die Default-TG
            ? / status       aktuellen Stand abfragen
        """
        parts = text.strip().replace(":", " ").split()
        if not parts:
            return ""

        head = parts[0].lower()
        if head in ("?", "status", "info"):
            return self._status_text()

        reflector_name = None
        value = parts[0]
        if len(parts) >= 2:
            reflector_name = parts[0]
            value = parts[1]

        route = self._dynamic_route(reflector_name)
        if route is None:
            return ("Keine dynamische Zeile"
                    + (f" fuer {reflector_name}" if reflector_name else ""))

        if value.lower() in ("0", "off", "aus", "default"):
            self.dynamic_tg[route.gssi] = route.default_tg
            self.dynamic_last_used[route.gssi] = time.time()
            self._mark_dynamic(route)
            return f"GSSI {route.gssi}: zurueck auf TG {route.default_tg}"

        if not value.isdigit():
            return f"TG {value!r} nicht verstanden"

        tg = int(value)
        control = self.cfg.sds_control
        if not control.allow_any_tg and tg not in control.allowed_tgs:
            return f"TG {tg} nicht freigegeben"

        self.dynamic_tg[route.gssi] = tg
        self.dynamic_last_used[route.gssi] = time.time()
        self._mark_dynamic(route)
        log.info("Dynamische GSSI %s zeigt jetzt auf TG %s", route.gssi, tg)
        return f"GSSI {route.gssi} -> TG {tg} aktiv"

    def _dynamic_route(self, reflector_name: Optional[str]) -> Optional[cfgmod.Route]:
        candidates = [r for r in self.cfg.routes if r.type == cfgmod.ROUTE_DYNAMIC]
        if reflector_name:
            lowered = reflector_name.lower()
            candidates = [r for r in candidates if r.reflector.lower() == lowered]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _status_text(self) -> str:
        bits = []
        for route in self.cfg.routes:
            scheduler = self.schedulers[route.gssi]
            mark = "+" if scheduler.is_active() else "-"
            bits.append(f"{mark}{route.gssi}>{self.target_tg(route)}")
        return " ".join(bits)[:120] or "keine Zeilen"

    def _expire_short_transfers(self) -> None:
        now = time.time()
        for key, (stamp, _) in list(self._short_transfers.items()):
            if now - stamp > SHORT_TRANSFER_TTL:
                del self._short_transfers[key]

    # ------------------------------------------------- SVX -> TETRA

    async def start_sources(self) -> None:
        """Startet je Reflektor einen USRP-Empfaenger."""
        if self.cfg.audio.backend != "usrp":
            return
        for reflector in self.cfg.reflectors:
            if not reflector.enabled or not reflector.usrp_listen_port:
                continue
            source = UsrpSource(
                reflector,
                on_start=self.on_svx_start,
                on_frame=self.on_svx_frame,
                on_stop=self.on_svx_stop,
                # als Funktion, damit eine Aenderung in der WebUI sofort
                # greift - die Quelle wird beim Reload nicht neu gebaut
                hang_ms=lambda: self.cfg.audio.hang_ms,
                on_sds=self.on_svx_sds,
            )
            await source.start()
            self.sources[reflector.name] = source

    async def close_calls(self, grund: str = "Bruecke faehrt herunter") -> None:
        """Alles beenden, was offen ist - sonst bleiben Geister zurueck.

        Der brew-server raeumt einen Gruppenruf nicht von selbst weg: ohne
        GROUP_IDLE steht er dort tagelang als aktiv. Am 2026-09-08 waren so
        13 Geister aufgelaufen, die meisten von Neustarts der Bruecke.
        """
        for name in list(self.outbound):
            await self.on_svx_stop(name)
        for call in list(self.calls.values()):
            await self._close_call(call, reason=grund)

    async def stop_sources(self) -> None:
        for source in self.sources.values():
            await source.close()
        self.sources.clear()

    def _tx_codec_or_none(self, reflector_name: str):
        codec = self._tx_codecs.get(reflector_name)
        if codec is not None or self._tx_codec_failed:
            return codec
        try:
            codec = acelp.AcelpCodec()
        except acelp.CodecUnavailable as exc:
            self._tx_codec_failed = True
            log.error("Kein Codec fuer SVX->TETRA (%s)", exc)
            return None
        self._tx_codecs[reflector_name] = codec
        return codec

    def route_for_svx(self, reflector_name: str,
                      tg: int = 0) -> Optional[cfgmod.Route]:
        """Welche GSSI bekommt Audio von diesem Reflektor?

        Wenn svxlink eine TG mitschickt, entscheidet die. Sonst bleibt nur
        die Prioritaet - siehe CLAUDE.md, das ist die schwache Stelle des
        Aufbaus mit mehreren TGs an einer Instanz.
        """
        candidates = [
            route for route in self.cfg.routes
            if route.reflector == reflector_name and route.enabled
            and self.schedulers[route.gssi].is_active()
        ]
        if not candidates:
            return None
        if tg:
            for route in candidates:
                if self.target_tg(route) == tg:
                    return route
        if len(candidates) == 1:
            return candidates[0]
        return max(candidates, key=lambda r: r.priority)

    async def on_svx_start(self, reflector_name: str, callsign: str,
                           tg: int) -> None:
        if self.client is None:
            return
        route = self.route_for_svx(reflector_name, tg)
        if route is None:
            self.state.bump("dropped_inactive")
            log.info("SVX %s: keine aktive Zeile - Ruf verworfen", reflector_name)
            return

        identifier = _uuid.uuid4()
        if not self._claim_floor(route, identifier):
            self.state.bump("dropped_floor")
            return

        quelle = None
        if self.cfg.tetra.issi_passthrough:
            src = self.sources.get(reflector_name)
            issi = getattr(src, "talker_issi", 0) or 0
            if 0 < issi <= MAX_ISSI:
                quelle = issi
        self.client.group_tx(route.gssi, callsign=callsign,
                             identifier=identifier, source=quelle)
        self.outbound[reflector_name] = OutboundCall(
            identifier=identifier,
            route=route,
            reflector=reflector_name,
            callsign=callsign,
        )
        self.state.bump("calls_tx")
        self.schedulers[route.gssi].note_traffic_start()
        if route.type == cfgmod.ROUTE_DYNAMIC:
            self.dynamic_last_used[route.gssi] = time.time()
        self.outbound[reflector_name].issi = quelle
        log.info("TX %s/TG %s -> GSSI %s%s%s", reflector_name,
                 self.target_tg(route), route.gssi,
                 f" ({callsign})" if callsign else "",
                 f" von ISSI {quelle}" if quelle else "")

    async def on_svx_frame(self, reflector_name: str, pcm: bytes) -> None:
        call = self.outbound.get(reflector_name)
        if call is None or self.client is None:
            return
        codec = self._tx_codec_or_none(reflector_name)
        if codec is None:
            return
        sub1, sub2 = codec.encode_frame(apply_gain(pcm, self.cfg.audio.tetra_factor))
        self.client.traffic(call.identifier, p.build_traffic_frame(sub1, sub2))
        call.frames += 1
        self.state.bump("frames_tx")

    async def on_svx_stop(self, reflector_name: str) -> None:
        call = self.outbound.pop(reflector_name, None)
        if call is None:
            return
        if self.client is not None:
            self.client.group_idle(call.identifier)
        self._release_floor(call.identifier)
        self.schedulers[call.route.gssi].note_traffic_end()
        self.state.add_last_heard(
            LastHeardEntry(
                timestamp=call.started,
                direction="svx->tetra",
                gssi=call.route.gssi,
                tg=self.target_tg(call.route),
                reflector=call.reflector,
                issi=call.issi,
                callsign=call.callsign,
                duration=call.duration,
                frames=call.frames,
            )
        )
        log.info("TX %s zu Ende, %d Frames / %.1f s",
                 reflector_name, call.frames, call.duration)

    # --------------------------------------------------------- Subscriber

    def _lookup_subscriber(self, issi: int) -> None:
        if issi in self._pending_lookups or issi in self.state.subscribers:
            return
        if self.client is None:
            return
        self._pending_lookups.add(issi)
        self.client.query_subscribers([issi])

    async def on_service(self, msg: p.ServiceMessage) -> None:
        if msg.type != p.SERVICE_SUBSCRIBER_PROFILES:
            return
        payload = msg.payload
        if not isinstance(payload, dict):
            return
        for key, info in payload.items():
            try:
                issi = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(info, dict):
                continue
            self.state.remember_subscriber(
                issi, info.get("call", ""), info.get("text", "")
            )
            self._pending_lookups.discard(issi)
            log.debug("Subscriber %s = %s", issi, info.get("call", ""))

    async def on_subscriber(self, msg: p.SubscriberMessage) -> None:
        log.debug("Subscriber-Update: %s ISSI %s Gruppen %s",
                  msg.type_name, msg.number, msg.groups)

    # ------------------------------------------------------------- Ticker

    async def tick(self) -> None:
        """Einmal pro Sekunde: Zeitplan pruefen, Timeouts abarbeiten."""
        now = time.time()

        for route in self.cfg.routes:
            scheduler = self.schedulers[route.gssi]
            was_active = scheduler.state
            active = scheduler.is_active()
            if was_active != scheduler.state:
                log.info("GSSI %s: %s -> %s (%s)", route.gssi, was_active,
                         scheduler.state, scheduler._reason)
            if not active:
                for call in list(self.calls.values()):
                    if call.gssi == route.gssi:
                        await self._close_call(call, reason="Zeitplan")

            if route.type == cfgmod.ROUTE_DYNAMIC and route.dynamic_timeout_minutes:
                current = self.dynamic_tg.get(route.gssi, route.default_tg)
                last = self.dynamic_last_used.get(route.gssi)
                if (current != route.default_tg and last is not None
                        and now - last > route.dynamic_timeout_minutes * 60):
                    self.dynamic_tg[route.gssi] = route.default_tg
                    self.dynamic_last_used[route.gssi] = now
                    self._mark_dynamic(route)
                    log.info("GSSI %s: Timeout, zurueck auf TG %s",
                             route.gssi, route.default_tg)

        for call in list(self.calls.values()):
            if now - call.last_frame > CALL_TIMEOUT:
                log.warning("GSSI %s: %.0f s kein Frame - verwaister Ruf "
                            "beendet, Reflektor %s wird freigegeben",
                            call.gssi, now - call.last_frame,
                            call.route.reflector or "?")
                self.state.bump("dropped_stale")
                await self._close_call(call, reason="verwaist")

        self._expire_short_transfers()

        # Gesammelt abarbeiten: mehrere Wechsel kurz hintereinander sollen
        # nicht mehrere Neustarts ausloesen.
        if self._dynamic_pending and self.on_dynamic_change is not None:
            namen = sorted(self._dynamic_pending)
            self._dynamic_pending.clear()
            for name in namen:
                try:
                    await self.on_dynamic_change(name)
                except Exception:
                    log.exception("Umschalten von %s fehlgeschlagen", name)

    # ------------------------------------------------------------- Status

    def status(self) -> dict:
        rows = []
        now = time.time()
        for route in self.cfg.routes:
            status = self.schedulers[route.gssi].status()
            reflector = self.cfg.reflector(route.reflector)
            # Wann faellt die dynamische Zeile auf die Default-TG zurueck?
            rueckfall = None
            if route.type == cfgmod.ROUTE_DYNAMIC and route.dynamic_timeout_minutes:
                aktuell = self.dynamic_tg.get(route.gssi, route.default_tg)
                zuletzt = self.dynamic_last_used.get(route.gssi)
                if aktuell != route.default_tg and zuletzt is not None:
                    rest = route.dynamic_timeout_minutes * 60 - (now - zuletzt)
                    rueckfall = max(0, round(rest))
            rows.append({
                "gssi": route.gssi,
                "name": route.name,
                "default_tg": route.default_tg,
                "dynamic_timeout_minutes": route.dynamic_timeout_minutes,
                "dynamic_fallback_in": rueckfall,
                "max_extend_minutes": route.max_extend_minutes,
                "schedule": asdict(route.schedule),
                "reflector": route.reflector,
                "reflector_enabled": reflector.enabled if reflector else None,
                "tg": self.target_tg(route),
                "type": route.type,
                "priority": route.priority,
                "enabled": route.enabled,
                "state": status.state,
                "reason": status.reason,
                "active": status.active,
                "manual": status.manual,
                "talkers": status.talkers,
                "shutdown_mode": route.shutdown_mode,
                "grace_minutes": route.grace_minutes,
                "window_end": status.window_end.isoformat() if status.window_end else None,
                "grace_until": status.grace_until.isoformat() if status.grace_until else None,
            })
        return {
            "routes": rows,
            "floors": [
                {"reflector": key, "gssi": floor.gssi,
                 "priority": floor.priority,
                 "seconds": round(now - floor.since, 1)}
                for key, floor in self.floors.items()
            ],
            # Reflektor und TG gehoeren in beide Richtungen mit: sobald
            # mehrere Instanzen gleichzeitig Verkehr haben, ist die GSSI
            # allein nicht mehr aussagekraeftig.
            "active_calls": [
                {"identifier": str(call.identifier), "gssi": call.gssi,
                 "issi": call.source, "callsign": call.callsign,
                 "direction": "tetra->svx",
                 "reflector": call.route.reflector,
                 "tg": call.tg or self.target_tg(call.route),
                 "frames": call.frames, "duration": round(call.duration, 1)}
                for call in self.calls.values()
            ] + [
                {"identifier": str(call.identifier), "gssi": call.route.gssi,
                 "issi": None, "callsign": call.callsign,
                 "direction": "svx->tetra", "reflector": call.reflector,
                 "tg": self.target_tg(call.route),
                 "frames": call.frames, "duration": round(call.duration, 1)}
                for call in self.outbound.values()
            ],
        }
