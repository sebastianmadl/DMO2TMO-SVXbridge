"""Konfiguration: laden, validieren, wieder wegschreiben.

Die Struktur ist absichtlich schon vollstaendig - Scheduler, Prioritaeten
und Reflektor-Zuordnung sind hier drin, auch wenn die Etappen dahinter
(Codec, USRP, WebUI) noch nicht gebaut sind. So muss spaeter niemand das
Datenmodell umkrempeln.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

import yaml

DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
DAY_INDEX = {name: i for i, name in enumerate(DAYS)}

ROUTE_FIXED = "fixed"
ROUTE_DYNAMIC = "dynamic"

SHUTDOWN_GRACE = "grace"
SHUTDOWN_HARD = "hard"


class ConfigError(ValueError):
    pass


@dataclass
class BrewConfig:
    url: str = "http://127.0.0.1:9000"
    path: str = "/brew/"
    username: str = ""
    password: str = ""
    issi: int = 0
    mode: str = "Terminal"          # Terminal | Basestation
    user_agent: str = "dmo2tmo-svxbridge/0.1"
    brew_version: int = 1
    verify_tls: bool = True
    reconnect_min_seconds: float = 2.0
    reconnect_max_seconds: float = 60.0
    ping_interval_seconds: float = 20.0


@dataclass
class Window:
    start: str = "00:00"
    end: str = "23:59"

    def validate(self) -> None:
        for value in (self.start, self.end):
            parts = value.split(":")
            if len(parts) != 2 or not all(x.isdigit() for x in parts):
                raise ConfigError(f"Ungueltige Uhrzeit: {value!r} (Format HH:MM)")
            hour, minute = int(parts[0]), int(parts[1])
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ConfigError(f"Uhrzeit ausserhalb des Bereichs: {value!r}")


@dataclass
class Schedule:
    enabled: bool = False
    days: list[str] = field(default_factory=lambda: list(DAYS))
    windows: list[Window] = field(default_factory=list)

    def validate(self) -> None:
        for day in self.days:
            if day.upper() not in DAY_INDEX:
                raise ConfigError(f"Unbekannter Wochentag: {day!r}")
        for window in self.windows:
            window.validate()
        if self.enabled and not self.windows:
            raise ConfigError("Zeitplan aktiv, aber kein Zeitfenster definiert")

    @property
    def day_numbers(self) -> set[int]:
        return {DAY_INDEX[d.upper()] for d in self.days if d.upper() in DAY_INDEX}


@dataclass
class Route:
    gssi: int = 0
    reflector: str = ""
    tg: int = 0
    type: str = ROUTE_FIXED
    priority: int = 0
    enabled: bool = True

    # Nur fuer type=dynamic
    default_tg: int = 0
    dynamic_timeout_minutes: float = 10.0

    # Abschaltverhalten am Fensterende: grace = Nachlaufzeit, hard = sofort
    shutdown_mode: str = SHUTDOWN_GRACE
    grace_minutes: float = 5.0
    max_extend_minutes: float = 120.0     # 0 = keine Obergrenze

    schedule: Schedule = field(default_factory=Schedule)
    name: str = ""

    def validate(self) -> None:
        if self.gssi <= 0:
            raise ConfigError(f"Route ohne gueltige GSSI: {self}")
        if self.type not in (ROUTE_FIXED, ROUTE_DYNAMIC):
            raise ConfigError(f"GSSI {self.gssi}: type muss fixed oder dynamic sein")
        if self.shutdown_mode not in (SHUTDOWN_GRACE, SHUTDOWN_HARD):
            raise ConfigError(
                f"GSSI {self.gssi}: shutdown_mode muss grace oder hard sein"
            )
        if self.type == ROUTE_FIXED and self.tg <= 0:
            raise ConfigError(f"GSSI {self.gssi}: fixe Route braucht eine TG")
        if self.type == ROUTE_DYNAMIC and self.default_tg <= 0:
            raise ConfigError(f"GSSI {self.gssi}: dynamische Route braucht default_tg")
        self.schedule.validate()

    @property
    def label(self) -> str:
        return self.name or f"GSSI {self.gssi}"


@dataclass
class Reflector:
    name: str = ""
    host: str = ""
    port: int = 5300              # SvxReflector
    callsign: str = ""            # Node-Rufzeichen, z.B. OE4SXM-HS
    auth_key: str = ""
    # ReflectorV2 = Anmeldung nur ueber AUTH_KEY (das faehrt OE1SXM).
    # Reflector   = neuere Variante mit Zertifikat und CSR.
    logic_type: str = "ReflectorV2"
    # Aus Sicht von svxlink sind die Ports vertauscht benannt:
    # wir senden auf usrp_port      -> svxlinks USRP_RX_PORT
    # wir lauschen auf usrp_listen  -> svxlinks USRP_TX_PORT
    usrp_port: int = 0
    usrp_listen_port: int = 0
    default_tg: int = 0           # ReflectorLogic DEFAULT_TG
    enabled: bool = True

    # TG-Umschaltung ohne Neustart: svxlink bekommt die Ziffern ueber eine
    # PTY. Die haengt an einer stummen SimplexLogic, weil ReflectorV2Logic
    # und UsrpLogic von LogicBase erben und gar keinen DTMF-Eingang haben.
    # DMO2TMO: keine TG-Umschaltung, die PTY wird nicht mehr gebraucht.
    # Feld bleibt, damit aeltere config.yaml noch laden.
    pty_enabled: bool = False
    dtmf_pty: str = ""            # leer = /data/pty/<name>

    # Node-Info, die svxlink beim Anmelden an den Reflektor schickt
    # (NODE_INFO_FILE -> MsgNodeInfo). Dashboards zeigen daraus z.B. den
    # Standorttext. node_info_extra: weitere Schluessel als JSON-Objekt.
    node_location: str = ""       # -> "nodeLocation"
    sysop: str = ""               # -> "sysop"
    node_info_extra: str = ""

    def node_info(self) -> dict:
        """Inhalt der node_info.json. Leer = keine Datei."""
        import json
        info = {}
        if self.node_info_extra.strip():
            info.update(json.loads(self.node_info_extra))
        if self.node_location:
            info["nodeLocation"] = self.node_location
        if self.sysop:
            info["sysop"] = self.sysop
        return info

    def pty_path(self) -> str:
        return self.dtmf_pty or f"/data/pty/{self.name}"

    def validate(self) -> None:
        if not self.name:
            raise ConfigError("Reflektor ohne Namen")
        if self.node_info_extra.strip():
            import json
            try:
                extra = json.loads(self.node_info_extra)
            except ValueError as exc:
                raise ConfigError(f"Reflektor {self.name}: node_info_extra "
                                  f"ist kein gueltiges JSON ({exc})")
            if not isinstance(extra, dict):
                raise ConfigError(f"Reflektor {self.name}: node_info_extra "
                                  "muss ein JSON-Objekt {...} sein")
        if self.usrp_port and self.usrp_port == self.usrp_listen_port:
            raise ConfigError(
                f"Reflektor {self.name}: usrp_port und usrp_listen_port "
                "muessen verschieden sein"
            )

    def ready_for_svxlink(self) -> list[str]:
        """Was fehlt, damit sich eine svxlink.conf erzeugen laesst."""
        missing = []
        for field_name in ("host", "callsign", "auth_key"):
            if not getattr(self, field_name):
                missing.append(field_name)
        for field_name in ("usrp_port", "usrp_listen_port"):
            if not getattr(self, field_name):
                missing.append(field_name)
        return missing


@dataclass
class LoopbackConfig:
    target_gssi: int = 0
    source_issi: int = 0
    callsign: str = ""


GAIN_MIN_DB = -30.0
GAIN_MAX_DB = 30.0


def db_to_factor(db: float) -> float:
    """dB in einen linearen Faktor. 0 dB = 1.0, -6 dB = halbe Amplitude."""
    return 10.0 ** (db / 20.0)


# Mehr als drei Sekunden Nachlauf blockiert den Reflektor sinnlos.
HANG_MAX_MS = 3000


@dataclass
class AudioConfig:
    backend: str = "loopback"       # loopback | null | usrp
    # Pegel in dB, global fuer alle Reflektoren - nicht je Instanz, weil
    # der Unterschied aus den Codecs kommt und nicht aus dem Reflektor.
    #
    # Es gibt zwei Stellschrauben je Richtung, und die in svxlink ist die
    # bessere: dort wird in Fliesskomma verstaerkt und anschliessend *weich*
    # begrenzt (NET_LIMITER_THRESH / LOCAL_LIMITER_THRESH). Unser apply_gain
    # arbeitet auf 16-Bit-PCM und schneidet hart ab. Grosse Anhebungen also
    # ueber net_preamp_db/preamp_db machen, die Bruecken-Gains nur zum
    # Feintrimmen.
    net_preamp_db: float = 0.0      # in svxlink, Richtung TETRA -> SVX
    preamp_db: float = 0.0          # in svxlink, Richtung SVX -> TETRA
    gain_svx_db: float = 0.0        # in der Bruecke, nach dem ACELP-Decode
    gain_tetra_db: float = 0.0      # in der Bruecke, vor dem ACELP-Encode
    # Nachlauf am Ende einer Aussendung von der SVX-Seite. svxlink schickt
    # sein Abschaltpaket in dem Moment, in dem PTT losgelassen wird - der
    # TETRA-Ruf endet dann hart und das letzte Wort geht verloren. Waehrend
    # des Nachlaufs bleibt der Gruppenruf offen (Stille), und ein sofort
    # folgender Durchgang haengt sich an, statt neu aufzubauen.
    hang_ms: int = 500
    loopback: LoopbackConfig = field(default_factory=LoopbackConfig)

    def validate(self) -> None:
        for name in ("gain_svx_db", "gain_tetra_db",
                     "net_preamp_db", "preamp_db"):
            wert = getattr(self, name)
            if not (GAIN_MIN_DB <= wert <= GAIN_MAX_DB):
                raise ConfigError(
                    f"audio.{name}: {wert} dB liegt ausserhalb "
                    f"{GAIN_MIN_DB:g} bis {GAIN_MAX_DB:g} dB"
                )
        if not (0 <= self.hang_ms <= HANG_MAX_MS):
            raise ConfigError(
                f"audio.hang_ms: {self.hang_ms} liegt ausserhalb "
                f"0 bis {HANG_MAX_MS} ms"
            )

    @property
    def svx_factor(self) -> float:
        return db_to_factor(self.gain_svx_db)

    @property
    def tetra_factor(self) -> float:
        return db_to_factor(self.gain_tetra_db)


@dataclass
class SdsControlConfig:
    enabled: bool = True
    control_issi: int = 0
    reply: bool = True
    coding_scheme: str = "8bit"
    allow_any_tg: bool = True
    allowed_tgs: list[int] = field(default_factory=list)


@dataclass
class TetraConfig:
    """TETRA-Erweiterungen des svxlink-Zweigs (tetra-contrib + UsrpLogic).

    issi_passthrough: die ISSI des sprechenden TETRA-Teilnehmers geht in
        den USRP-Metadaten an svxlink (-> Qso:info am Reflektor), und eine
        ISSI, die svxlink von einem fremden TETRA-Gateway meldet, wird als
        Quell-ISSI des Gruppenrufs gesetzt.
    sds_forward: Text-SDS gehen ueber svxlink an den Reflektor
        (Sds:info / ForwardSds:info) und umgekehrt.
    """
    issi_passthrough: bool = True
    sds_forward: bool = True


# Positionsformat wie im tetra_link_gw.conf: Breite ggmm.mm, Laenge gggmm.mm
# (APRS-Stil, ohne Hemisphaere - der OE-Verbund kennt nur N/E).
_LAT = re.compile(r"^\d{4}\.\d{2}$")
_LON = re.compile(r"^\d{5}\.\d{2}$")
TETRA_FW_PORT = 5220


@dataclass
class TetraFwConfig:
    """Lastheard im Format des TETRA-AUSTRIA-Verbunds (tetra_fw_server).

    UDP-Textzeilen an den Host des Reflektors, Port 5220 - getrennt vom
    svxlink-Audio. Protokoll: docs/tetra_fw_protocol.md.

    gateway_call: Spalte "Gateway" am Dashboard. Leer = Node-Rufzeichen
        des aktiven Reflektors.
    gateway_comment: Spalte "Comment", z.B. "Tetra-OE4SXM/430.0000".
    gateway_lat/lon: eigene Position fuer die Start-Bake (ggmm.mm bzw.
        gggmm.mm). Leer = keine Bake.
    """
    enabled: bool = False
    gateway_call: str = ""
    gateway_comment: str = ""
    gateway_lat: str = ""
    gateway_lon: str = ""
    port: int = TETRA_FW_PORT
    # Rueckweg: ISSI und Rufzeichen eines fremden Sprechers aus dem
    # Lastheard des OE-Dashboards (siehe app/oe_dashboard.py).
    dashboard_issi: bool = False
    dashboard_url: str = ""       # leer = http://<Reflektor-Host>/
    dashboard_max_age: int = 30   # Sekunden; aelter gilt als anderer Durchgang

    def dashboard_url_for(self, host: str) -> str:
        return self.dashboard_url or (f"http://{host}/" if host else "")

    def validate(self) -> None:
        for name in ("gateway_call", "gateway_comment"):
            wert = getattr(self, name)
            # Die Zeile ist komma-getrennt - ein Komma verschiebt am
            # Dashboard alle Spalten.
            if any(z in wert for z in ",\r\n"):
                raise ConfigError(f"tetra_fw.{name}: kein Komma und kein "
                                  "Zeilenumbruch erlaubt")
        if self.gateway_lat and not _LAT.match(self.gateway_lat):
            raise ConfigError("tetra_fw.gateway_lat: Format ggmm.mm, "
                              "z.B. 4816.82")
        if self.gateway_lon and not _LON.match(self.gateway_lon):
            raise ConfigError("tetra_fw.gateway_lon: Format gggmm.mm, "
                              "z.B. 01624.91")
        if bool(self.gateway_lat) != bool(self.gateway_lon):
            raise ConfigError("tetra_fw: Breite und Laenge nur gemeinsam")
        if not 1 <= int(self.port) <= 65535:
            raise ConfigError("tetra_fw.port: 1 bis 65535")
        if self.dashboard_url and not self.dashboard_url.startswith(
                ("http://", "https://")):
            raise ConfigError("tetra_fw.dashboard_url: muss mit http:// oder "
                              "https:// beginnen")
        if not 1 <= int(self.dashboard_max_age) <= 3600:
            raise ConfigError("tetra_fw.dashboard_max_age: 1 bis 3600 s")


@dataclass
class StatusServerConfig:
    enabled: bool = True
    listen: str = "0.0.0.0"
    port: int = 3007


@dataclass
class Config:
    brew: BrewConfig = field(default_factory=BrewConfig)
    timezone: str = "Europe/Vienna"
    log_level: str = "INFO"
    audio: AudioConfig = field(default_factory=AudioConfig)
    sds_control: SdsControlConfig = field(default_factory=SdsControlConfig)
    tetra: TetraConfig = field(default_factory=TetraConfig)
    tetra_fw: TetraFwConfig = field(default_factory=TetraFwConfig)
    status_server: StatusServerConfig = field(default_factory=StatusServerConfig)
    reflectors: list[Reflector] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)

    def validate(self) -> None:
        self.audio.validate()
        self.tetra_fw.validate()
        if not self.brew.issi:
            raise ConfigError("brew.issi fehlt")
        if not self.brew.username:
            raise ConfigError("brew.username fehlt")
        names = set()
        for reflector in self.reflectors:
            reflector.validate()
            if reflector.name in names:
                raise ConfigError(f"Reflektor {reflector.name!r} doppelt")
            names.add(reflector.name)

        # DMO2TMO: genau ein Reflektor, eine feste Sprechgruppe. Es gibt
        # keine dynamische Umschaltung mehr - die Felder bleiben nur, damit
        # eine aeltere config.yaml noch geladen werden kann.
        aktiv = [r for r in self.reflectors if r.enabled]
        if len(aktiv) > 1:
            raise ConfigError(
                "DMO2TMO: nur ein aktiver Reflektor erlaubt, gefunden: "
                + ", ".join(r.name for r in aktiv)
            )
        if len(self.routes) > 1:
            raise ConfigError(
                f"DMO2TMO: genau eine Sprechgruppe, gefunden {len(self.routes)} "
                "Zeilen"
            )

        seen_gssi = set()
        dynamic_per_reflector = {}
        for route in self.routes:
            if route.type == ROUTE_DYNAMIC:
                raise ConfigError(
                    f"GSSI {route.gssi}: dynamische Zeilen gibt es bei DMO2TMO "
                    "nicht - type: fixed und eine feste tg"
                )
            route.validate()
            if route.gssi in seen_gssi:
                raise ConfigError(f"GSSI {route.gssi} kommt mehrfach vor")
            seen_gssi.add(route.gssi)
            if route.reflector and route.reflector not in names:
                raise ConfigError(
                    f"GSSI {route.gssi} zeigt auf unbekannten Reflektor "
                    f"{route.reflector!r}"
                )
            if route.type == ROUTE_DYNAMIC:
                if route.reflector in dynamic_per_reflector:
                    raise ConfigError(
                        f"Reflektor {route.reflector!r} hat mehr als eine "
                        "dynamische Zeile"
                    )
                dynamic_per_reflector[route.reflector] = route.gssi

    def reflector(self, name: str) -> Optional[Reflector]:
        for item in self.reflectors:
            if item.name == name:
                return item
        return None

    def active_reflector(self) -> Optional[Reflector]:
        """DMO2TMO hat hoechstens einen aktiven Reflektor."""
        for item in self.reflectors:
            if item.enabled:
                return item
        return None

    def route_for_gssi(self, gssi: int) -> Optional[Route]:
        for route in self.routes:
            if route.gssi == gssi:
                return route
        return None

    def to_dict(self) -> dict:
        data = asdict(self)
        return data


def _build(cls, data: dict, *, path: str = ""):
    """Baut eine Dataclass aus einem dict und meckert bei Tippfehlern."""
    if data is None:
        return cls()
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(data) - known
    if unknown:
        where = f" in {path}" if path else ""
        raise ConfigError(f"Unbekannte Felder{where}: {', '.join(sorted(unknown))}")
    return cls(**{k: v for k, v in data.items() if k in known})


def load(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return from_dict(raw)


def from_dict(raw: dict) -> Config:
    raw = copy.deepcopy(raw or {})

    cfg = Config()
    cfg.timezone = raw.get("timezone", cfg.timezone)
    cfg.log_level = raw.get("log_level", cfg.log_level)
    cfg.brew = _build(BrewConfig, raw.get("brew"), path="brew")
    cfg.status_server = _build(
        StatusServerConfig, raw.get("status_server"), path="status_server"
    )
    cfg.sds_control = _build(
        SdsControlConfig, raw.get("sds_control"), path="sds_control"
    )
    cfg.tetra = _build(TetraConfig, raw.get("tetra"), path="tetra")
    cfg.tetra_fw = _build(TetraFwConfig, raw.get("tetra_fw"), path="tetra_fw")

    audio_raw = raw.get("audio") or {}
    loopback = _build(
        LoopbackConfig, audio_raw.pop("loopback", None), path="audio.loopback"
    )
    cfg.audio = _build(AudioConfig, audio_raw, path="audio")
    cfg.audio.loopback = loopback

    cfg.reflectors = [
        _build(Reflector, item, path="reflectors[]") for item in raw.get("reflectors") or []
    ]

    routes = []
    for item in raw.get("routes") or []:
        item = dict(item or {})
        schedule_raw = item.pop("schedule", None) or {}
        windows = [
            _build(Window, w, path="routes[].schedule.windows[]")
            for w in schedule_raw.pop("windows", None) or []
        ]
        schedule = _build(Schedule, schedule_raw, path="routes[].schedule")
        schedule.windows = windows
        route = _build(Route, item, path="routes[]")
        route.schedule = schedule
        if route.type == ROUTE_DYNAMIC and not route.tg:
            route.tg = route.default_tg
        routes.append(route)
    cfg.routes = routes

    cfg.validate()
    return cfg


def save(cfg: Config, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(cfg.to_dict(), handle, sort_keys=False, allow_unicode=True)
