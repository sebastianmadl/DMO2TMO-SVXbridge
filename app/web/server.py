"""WebUI: Sitzungen, Rechte, API.

Vier Reiter, passend zum Vierer-Raster in der style.css von
svxdmr-multilink:

    Status   Verbindungen, laufende Rufe, Last Heard
    Routen   Mapping-Tabelle, Handbetrieb, dynamische TG
    Log      Live-Log mit Level-Filter
    System   Dienste, svxlink.conf, Benutzer

Alles was konfigurierbar ist, ist hier bedienbar (Grundregel 4). Wo eine
Aenderung erst nach einem Neustart der Bruecke greift, sagt die Antwort
das ausdruecklich - stillschweigend wirkungslose Knoepfe waeren schlimmer
als gar keine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import aiohttp
import aiohttp_jinja2
import jinja2
import yaml
from aiohttp import web

import confgen

from .. import config as cfgmod
from ..brew import sds as sdsmod
from . import logbuffer
from .users import (AREAS, AREA_KEYS, SESSION_HOURS, SessionStore,
                    UserStore)

log = logging.getLogger("web")

COOKIE = "dmo2tmo"
NAME = "DMO2TMO SVXBridge"
BY = "by OE4SXM"
# In der Rechte-Matrix steht der lange Name, in der Reiterleiste muss es
# ein Wort sein - sonst bricht die Zeile um (fuenf Reiter auf einem Handy).
TAB_TITLES = {"status": "Status", "routes": "Routen", "sds": "SDS",
              "log": "Log", "system": "System"}
HERE = Path(__file__).resolve().parent.parent      # app/
STATIC = HERE / "static"
TEMPLATES = HERE / "templates"


def _user(request: web.Request):
    return request.get("user")


def _asset_ver() -> int:
    """Zeitstempel fuer ?v= an CSS und JS.

    Ohne das haelt der Browser eine geaenderte Oberflaeche fuer die alte -
    beim Umbau der Reiterleiste genau so passiert.
    """
    try:
        return int(max((STATIC / "style.css").stat().st_mtime,
                       (STATIC / "app.js").stat().st_mtime))
    except OSError:
        return 0


def needs(area: str):
    """Dekorator: Bereich noetig, sonst 403 bzw. Weiterleitung zum Login."""
    def wrap(handler):
        async def inner(request: web.Request):
            user = _user(request)
            if user is None:
                raise web.HTTPUnauthorized(text="nicht angemeldet")
            if not user.may(area):
                raise web.HTTPForbidden(text=f"Kein Zugriff auf {area}")
            return await handler(request)
        return inner
    return wrap


@web.middleware
async def session_middleware(request: web.Request, handler):
    sessions: SessionStore = request.app["sessions"]
    users: UserStore = request.app["users"]
    token = request.cookies.get(COOKIE)
    name = sessions.user_of(token)
    request["user"] = users.get(name) if name else None
    response = await handler(request)
    if name and sessions.renew(token):
        _set_cookie(response, token)
    return response


def _set_cookie(response, token: str) -> None:
    response.set_cookie(COOKIE, token, httponly=True, samesite="Lax",
                        max_age=int(SESSION_HOURS * 3600))


# ------------------------------------------------------------------ Seiten


async def page_login(request: web.Request):
    if _user(request) is not None:
        raise web.HTTPFound("/")
    return aiohttp_jinja2.render_template(
        "login.html", request,
        {"error": "", "ver": _asset_ver(), "app_name": NAME})


async def do_login(request: web.Request):
    form = await request.post()
    users: UserStore = request.app["users"]
    user = users.check(str(form.get("username", "")).strip(),
                       str(form.get("password", "")))
    if user is None:
        log.warning("Fehlgeschlagene Anmeldung fuer %r",
                    form.get("username", ""))
        return aiohttp_jinja2.render_template(
            "login.html", request,
            {"error": "Benutzer oder Passwort falsch", "ver": _asset_ver(),
             "app_name": NAME},
            status=401)
    token = request.app["sessions"].new(user.name)
    response = web.HTTPFound("/")
    _set_cookie(response, token)
    log.info("Anmeldung: %s", user.name)
    raise response


async def do_logout(request: web.Request):
    request.app["sessions"].drop(request.cookies.get(COOKIE))
    response = web.HTTPFound("/login")
    response.del_cookie(COOKIE)
    raise response


async def page_index(request: web.Request):
    user = _user(request)
    if user is None:
        raise web.HTTPFound("/login")
    tabs = [(key, TAB_TITLES[key]) for key, _ in AREAS
            if key in TAB_TITLES and user.may(key)]
    return aiohttp_jinja2.render_template("index.html", request, {
        "user": user,
        "tabs": tabs,
        "areas": AREAS,
        "may": {key: user.may(key) for key, _ in AREAS},
        "ver": _asset_ver(),
        "app_name": NAME,
        "app_by": BY,
    })


# --------------------------------------------------------------------- API


# Groesser als das ist die Liste nicht - was drueber kommt, ist ein Irrtum
# (falsche URL, Fehlerseite) und muss nicht erst in den Speicher.
IDLIST_MAX_BYTES = 40 * 1024 * 1024


def _namebook(request: web.Request):
    buch = request.app["state"].namebook
    if buch is None:
        raise web.HTTPServiceUnavailable(
            text="Rufzeichenliste steht nicht zur Verfuegung")
    return buch


@needs("system")
async def api_idlist(request: web.Request):
    return web.json_response(_namebook(request).status())


@needs("system")
async def api_idlist_refresh(request: web.Request):
    """Liste herunterladen und einlesen.

    Der Download laeuft ueber aiohttp (blockiert nicht), das Einlesen der
    324000 Zeilen in einem Thread - sonst stuenden Audio und Web fuer ein
    paar Sekunden.
    """
    buch = _namebook(request)
    try:
        data = await request.json()
    except ValueError:
        data = {}
    url = str(data.get("url") or "").strip() or buch.url

    if not url.startswith(("http://", "https://")):
        raise web.HTTPBadRequest(text="URL muss mit http:// oder https:// beginnen")

    erwartet = 0
    puffer = bytearray()
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as sitzung:
            async with sitzung.get(url) as antwort:
                if antwort.status != 200:
                    raise web.HTTPBadGateway(
                        text=f"{url} antwortet mit HTTP {antwort.status}")
                erwartet = antwort.content_length or 0
                # Stueckweise lesen und dabei mitzaehlen. `content.read(n)`
                # waere falsch: das liefert nur, was gerade im Puffer liegt,
                # nicht n Bytes - damit kam beim ersten Versuch ein Bruchteil
                # der Liste an, und niemand haette es gemerkt.
                async for stueck in antwort.content.iter_chunked(64 * 1024):
                    puffer += stueck
                    if len(puffer) > IDLIST_MAX_BYTES:
                        raise web.HTTPBadGateway(
                            text=f"Antwort groesser als "
                                 f"{IDLIST_MAX_BYTES // 1024 // 1024} MB - das "
                                 f"ist wohl nicht die Rufzeichenliste")
    except aiohttp.ClientError as exc:
        raise web.HTTPBadGateway(text=f"Download fehlgeschlagen: {exc}")
    except asyncio.TimeoutError:
        raise web.HTTPGatewayTimeout(text="Download hat zu lange gedauert")

    # Abgebrochene Uebertragung nicht als gueltige Liste einlesen.
    if erwartet and len(puffer) != erwartet:
        raise web.HTTPBadGateway(
            text=f"Download unvollstaendig: {len(puffer)} von {erwartet} Byte")
    roh = bytes(puffer)

    text = roh.decode("utf-8", errors="replace")
    try:
        anzahl = await asyncio.to_thread(buch.update_from_text, text)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    except (OSError, RuntimeError) as exc:
        raise web.HTTPInternalServerError(text=str(exc))

    buch.url = url
    log.info("Rufzeichenliste aktualisiert von %s (%s): %d Eintraege",
             url, _user(request).name, anzahl)
    return web.json_response({"ok": True, "count": anzahl,
                              "status": buch.status()})


@needs("system")
async def api_idlist_manual(request: web.Request):
    buch = _namebook(request)
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="kein gueltiges JSON")
    try:
        buch.set_manual(data.get("issi"), str(data.get("name", "")))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    except (OSError, RuntimeError) as exc:
        raise web.HTTPInternalServerError(text=str(exc))
    return web.json_response({"ok": True, "manual": buch.manual_all()})


@needs("system")
async def api_idlist_manual_delete(request: web.Request):
    buch = _namebook(request)
    try:
        issi = int(request.match_info["issi"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="ISSI muss eine Zahl sein")
    if not buch.delete_manual(issi):
        raise web.HTTPNotFound(text=f"{issi} steht nicht in der Liste")
    return web.json_response({"ok": True, "manual": buch.manual_all()})


@needs("sds")
async def api_sds(request: web.Request):
    """Postfach lesen. `since` liefert nur das Neue, wie beim Log."""
    box = request.app["router"].sdsbox
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        raise web.HTTPBadRequest(text="since muss eine Zahl sein")
    state = request.app["state"]
    nachrichten = []
    for eintrag in (box.since(since) if since else box.all()):
        # Namen erst hier dazu, nicht in der Ablage: die Liste kann sich
        # aendern, und dann soll auch alte Post den neuen Namen zeigen.
        nachrichten.append(dict(eintrag,
                                name=state.callsign(eintrag.get("peer"))))
    return web.json_response({
        "messages": nachrichten,
        "last": box.last_seq,
        "counts": box.counts(),
        "scheme": request.app["cfg"].sds_control.coding_scheme,
        "control_issi": request.app["cfg"].sds_control.control_issi,
        "own_issi": request.app["cfg"].brew.issi,
    })


@needs("sds")
async def api_sds_send(request: web.Request):
    router = request.app["router"]
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="kein gueltiges JSON")
    try:
        ziel = int(data.get("destination") or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Ziel-ISSI muss eine Zahl sein")
    try:
        eintrag = router.send_text_sds(ziel, str(data.get("text", "")),
                                       data.get("scheme") or None)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    except RuntimeError as exc:
        # Nicht der Fehler des Benutzers - die Verbindung fehlt.
        raise web.HTTPServiceUnavailable(text=str(exc))
    log.info("SDS ueber die WebUI an %s gesendet (%s)", ziel,
             _user(request).name)
    return web.json_response({"ok": True, "message": eintrag,
                              "last": router.sdsbox.last_seq})


@needs("sds")
async def api_sds_scheme(request: web.Request):
    """Zeichenkodierung fuer ausgehende SDS.

    Gehoert in die Oberflaeche, weil man sie nur durch Ausprobieren am
    echten Geraet findet: manche Handgeraete lesen alles als UCS-2 und
    zeigen bei 8-Bit Zeichensalat an.
    """
    cfg: cfgmod.Config = request.app["cfg"]
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="kein gueltiges JSON")
    name = str(data.get("scheme", "")).strip().lower()
    if name not in sdsmod.MODES:
        raise web.HTTPBadRequest(
            text="scheme muss eines von " + ", ".join(sdsmod.MODES) + " sein")
    raw = cfg.to_dict()
    raw.setdefault("sds_control", {})["coding_scheme"] = name
    return await _apply(request, raw, "SDS-Kodierung")


@needs("sds")
async def api_sds_clear(request: web.Request):
    request.app["router"].sdsbox.clear()
    log.info("SDS-Postfach geleert (%s)", _user(request).name)
    return web.json_response({"ok": True, "last": 0})


@needs("status")
async def api_status(request: web.Request):
    app = request.app
    cfg, state, router, client = (app["cfg"], app["state"], app["router"],
                                  app["client"])
    return web.json_response({
        "brew": {
            "url": cfg.brew.url, "issi": cfg.brew.issi, "mode": cfg.brew.mode,
            "connected": client.connected, "connects": client.connect_count,
            "last_error": client.last_error,
            "affiliated": sorted(client.affiliated_groups),
        },
        "audio_backend": cfg.audio.backend,
        "gain_svx_db": cfg.audio.gain_svx_db,
        "gain_tetra_db": cfg.audio.gain_tetra_db,
        "net_preamp_db": cfg.audio.net_preamp_db,
        "preamp_db": cfg.audio.preamp_db,
        "timezone": cfg.timezone,
        "reflectors": [
            {"name": r.name, "host": r.host, "callsign": r.callsign,
             "enabled": r.enabled, "tg": r.default_tg,
             "usrp_port": r.usrp_port}
            for r in cfg.reflectors
        ],
        "services": app["manager_status"](),
        "services_dir": app["services_dir"],
        "default_password": app["users"].uses_default_password(),
        **router.status(),
        **state.as_dict(),
    })


@needs("log")
async def api_log(request: web.Request):
    buf = logbuffer.get()
    if buf is None:
        return web.json_response({"lines": [], "last": 0})
    since = int(request.query.get("since", 0) or 0)
    level = request.query.get("level") or None
    return web.json_response({"lines": buf.since(since, level),
                              "last": buf.last_seq})


@needs("routes")
async def api_routes(request: web.Request):
    cfg: cfgmod.Config = request.app["cfg"]
    router = request.app["router"]
    return web.json_response({
        "routes": [asdict(r) for r in cfg.routes],
        "reflectors": [r.name for r in cfg.reflectors],
        "state": router.status()["routes"],
    })


@needs("routes")
async def api_route_manual(request: web.Request):
    router = request.app["router"]
    try:
        gssi = int(request.match_info["gssi"])
    except ValueError:
        raise web.HTTPBadRequest(text="GSSI ungueltig")
    scheduler = router.schedulers.get(gssi)
    if scheduler is None:
        raise web.HTTPNotFound(text=f"Keine Zeile mit GSSI {gssi}")
    value = (request.query.get("value") or "").lower()
    mapping = {"on": True, "1": True, "ein": True,
               "off": False, "0": False, "aus": False,
               "auto": None, "": None}
    if value not in mapping:
        raise web.HTTPBadRequest(text="value muss on, off oder auto sein")
    scheduler.set_manual(mapping[value])
    log.info("Handbetrieb GSSI %s: %s", gssi, value or "auto")
    return web.json_response({"gssi": gssi, "manual": scheduler.manual})


@needs("routes")
async def api_command(request: web.Request):
    text = request.query.get("text", "")
    return web.json_response({"reply": request.app["router"].handle_command(text)})


async def _apply(request: web.Request, raw: dict, was: str) -> web.Response:
    """Config pruefen, schreiben und im laufenden Betrieb uebernehmen."""
    try:
        new_cfg = cfgmod.from_dict(raw)
    except cfgmod.ConfigError as exc:
        raise web.HTTPBadRequest(text=str(exc))

    cfgmod.save(new_cfg, request.app["config_path"])

    # Die svxlink.conf wird sonst nur beim Start des Supervisors erzeugt -
    # ein Neustart danach wuerde die alte Datei laden.
    if new_cfg.audio.backend == "usrp":
        try:
            confgen.write_all(new_cfg, Path(request.app["svxlink_dir"]))
        except (cfgmod.ConfigError, OSError) as exc:
            log.warning("svxlink.conf nicht neu erzeugbar: %s", exc)

    restart = await request.app["router"].reload(new_cfg)
    request.app["cfg"] = new_cfg

    hinweis = f"{was} gespeichert und übernommen."
    if restart:
        hinweis += (" Für " + ", ".join(restart) +
                    " muss die svxlink-Instanz neu starten (Reiter System).")
    return web.json_response({"ok": True, "restart": restart,
                              "hinweis": hinweis})


@needs("routes")
async def api_save_routes(request: web.Request):
    """Mapping-Tabelle zurueckschreiben - inklusive Zeitplan je Zeile."""
    data = await request.json()
    raw = request.app["cfg"].to_dict()
    raw["routes"] = data.get("routes", [])
    return await _apply(request, raw, f"{len(raw['routes'])} Zeile(n)")


@needs("system")
async def api_reflectors(request: web.Request):
    cfg: cfgmod.Config = request.app["cfg"]
    if request.method == "GET":
        return web.json_response({
            "reflectors": [asdict(r) for r in cfg.reflectors],
            "in_use": {r.name: [x.gssi for x in cfg.routes if x.reflector == r.name]
                       for r in cfg.reflectors},
        })
    data = await request.json()
    raw = cfg.to_dict()
    raw["reflectors"] = data.get("reflectors", [])
    return await _apply(request, raw, "Reflektoren")


@needs("system")
async def api_tetra(request: web.Request):
    """ISSI-Durchreichen und SDS-Weiterleitung ein/aus."""
    cfg: cfgmod.Config = request.app["cfg"]
    if request.method == "GET":
        return web.json_response(asdict(cfg.tetra))
    data = await request.json()
    raw = cfg.to_dict()
    for key in ("issi_passthrough", "sds_forward"):
        if key in data:
            raw.setdefault("tetra", {})[key] = bool(data[key])
    antwort = await _apply(request, raw, "TETRA-Einstellungen")
    # SHARE_USERINFO steht in der svxlink.conf und wirkt erst nach einem
    # Neustart der Instanz.
    daten = json.loads(antwort.body)
    neu = await _restart_svxlink(request)
    if neu:
        daten["hinweis"] += " svxlink neu gestartet: " + ", ".join(neu) + "."
    return web.json_response(daten)


@needs("system")
async def api_tetra_fw(request: web.Request):
    """Lastheard an den TETRA-AUSTRIA-Verbund (UDP 5220).

    Wirkt ohne Neustart - der Sender liest die Config bei jedem Durchgang.
    """
    cfg: cfgmod.Config = request.app["cfg"]
    sender = request.app["router"].tetra_fw
    if request.method == "GET":
        reflektor = cfg.active_reflector()
        return web.json_response({
            **asdict(cfg.tetra_fw),
            # Vorbelegung fuer das Feld Gateway-Call
            "default_call": reflektor.callsign if reflektor else "",
            "status": sender.status(),
        })
    data = await request.json()
    raw = cfg.to_dict()
    fw = raw.setdefault("tetra_fw", {})
    if "enabled" in data:
        fw["enabled"] = bool(data["enabled"])
    for key in ("gateway_call", "gateway_comment", "gateway_lat",
                "gateway_lon"):
        if key in data:
            fw[key] = str(data[key] or "").strip()
    if "port" in data:
        try:
            fw["port"] = int(data["port"])
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="port: keine ganze Zahl")
    return await _apply(request, raw, "Lastheard-Einstellungen")


@needs("system")
async def api_audio(request: web.Request):
    """Audiopegel in dB. Global, nicht je Reflektor."""
    cfg: cfgmod.Config = request.app["cfg"]
    if request.method == "GET":
        return web.json_response({
            "net_preamp_db": cfg.audio.net_preamp_db,
            "preamp_db": cfg.audio.preamp_db,
            "gain_svx_db": cfg.audio.gain_svx_db,
            "gain_tetra_db": cfg.audio.gain_tetra_db,
            "backend": cfg.audio.backend,
            "hang_ms": cfg.audio.hang_ms,
            "min_db": cfgmod.GAIN_MIN_DB, "max_db": cfgmod.GAIN_MAX_DB,
            "hang_max_ms": cfgmod.HANG_MAX_MS,
        })
    data = await request.json()
    raw = cfg.to_dict()
    svxlink_neu = False
    for key in ("gain_svx_db", "gain_tetra_db", "net_preamp_db", "preamp_db"):
        if key in data:
            try:
                raw["audio"][key] = float(data[key])
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text=f"{key}: keine Zahl")
            if key in ("net_preamp_db", "preamp_db"):
                svxlink_neu = True

    # Der Nachlauf sitzt in der Bruecke, nicht in svxlink - kein Neustart.
    if "hang_ms" in data:
        try:
            raw["audio"]["hang_ms"] = int(data["hang_ms"])
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="hang_ms: keine ganze Zahl")

    antwort = await _apply(request, raw, "Audiopegel")
    if not svxlink_neu:
        return antwort

    # PREAMP/NET_PREAMP landen ueber confgen in der svxlink.conf und wirken
    # erst nach einem Neustart. Den machen wir gleich selbst - ein Knopf,
    # der die Einstellung sichtbar uebernimmt, statt sie still liegenzulassen.
    daten = json.loads(antwort.body)
    neu = await _restart_svxlink(request)
    if neu:
        daten["restarted"] = neu
        daten["hinweis"] += (" svxlink neu gestartet: " + ", ".join(neu) +
                             " – die Pegel sind in ein paar Sekunden aktiv.")
    else:
        daten["hinweis"] += (" Kein Supervisor gefunden – die svxlink-Pegel "
                             "wirken erst nach einem Neustart der Instanzen.")
    return web.json_response(daten)


async def _restart_svxlink(request: web.Request) -> list[str]:
    """Startet alle svxlink-Instanzen neu. Gibt zurueck, welche."""
    control = request.app["manager_control"]
    if control is None:
        return []
    namen = [s["name"] for s in request.app["manager_status"]()
             if s.get("name", "").startswith("svxlink-")]
    for name in namen:
        try:
            await control(name, "restart")
        except Exception as exc:            # pragma: no cover - defensiv
            log.warning("Neustart von %s fehlgeschlagen: %s", name, exc)
    if namen:
        log.info("svxlink neu gestartet wegen geaenderter Pegel: %s",
                 ", ".join(namen))
    return namen


@needs("config")
async def api_config(request: web.Request):
    """Die config.yaml im Klartext - fuer alles, wofuer es kein Formular gibt."""
    path = Path(request.app["config_path"])
    if request.method == "GET":
        return web.json_response({"text": path.read_text("utf-8")})
    data = await request.json()
    try:
        raw = yaml.safe_load(data.get("text", "")) or {}
    except yaml.YAMLError as exc:
        raise web.HTTPBadRequest(text=f"YAML kaputt: {exc}")
    if not isinstance(raw, dict):
        raise web.HTTPBadRequest(text="Die Datei muss eine Zuordnung enthalten")
    return await _apply(request, raw, "Konfiguration")


@needs("system")
async def api_backup(request: web.Request):
    """config.yaml und users.yaml als eine Datei zum Herunterladen."""
    parts = []
    for label, path in (("config.yaml", request.app["config_path"]),
                        ("users.yaml", request.app["users"].path)):
        path = Path(path)
        if path.is_file():
            parts.append(f"# ---- {label} ----\n{path.read_text('utf-8')}")
    body = "\n".join(parts)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return web.Response(
        body=body.encode("utf-8"),
        headers={"Content-Disposition":
                 f'attachment; filename="dmo2tmo-svxbridge-{stamp}.txt"'},
        content_type="text/plain",
    )


@needs("system")
async def api_restore(request: web.Request):
    """Sicherung wieder einspielen. Nur die config.yaml, Benutzer bleiben."""
    data = await request.json()
    text = data.get("text", "")
    if "# ---- config.yaml ----" in text:
        text = text.split("# ---- config.yaml ----", 1)[1]
        text = text.split("# ---- users.yaml ----", 1)[0]
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise web.HTTPBadRequest(text=f"YAML kaputt: {exc}")
    return await _apply(request, raw, "Sicherung")


@needs("system")
async def api_svxlink_conf(request: web.Request):
    name = request.match_info["name"]
    path = Path(request.app["svxlink_dir"]) / name / "svxlink.conf"
    if request.method == "GET":
        if not path.is_file():
            raise web.HTTPNotFound(text=f"Keine Konfiguration fuer {name}")
        return web.json_response({"name": name, "text": path.read_text("utf-8")})
    data = await request.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data.get("text", ""), encoding="utf-8")
    log.info("svxlink.conf fuer %s geschrieben", name)
    return web.json_response({
        "ok": True,
        "hinweis": "Gespeichert. Wirksam nach Neustart dieser Instanz. "
                   "Achtung: confgen.py ueberschreibt die Datei beim naechsten "
                   "Start der Bruecke.",
    })


@needs("system")
async def api_service(request: web.Request):
    name = request.match_info["name"]
    action = request.match_info["action"]
    if action not in ("start", "stop", "restart"):
        raise web.HTTPBadRequest(text="Aktion muss start, stop oder restart sein")
    control = request.app["manager_control"]
    if control is None:
        raise web.HTTPServiceUnavailable(
            text="Kein Supervisor - die Bruecke laeuft ohne manager.py")

    # Namen pruefen, bevor wir ein Kommando hinterlegen: der Supervisor
    # bekommt es sonst zwar, verwirft es als unbekannt, und die UI haette
    # trotzdem "ok" gemeldet.
    bekannt = [s.get("name") for s in request.app["manager_status"]()]
    if name not in bekannt:
        raise web.HTTPNotFound(
            text=f"Unbekannter Dienst {name}. Bekannt: {', '.join(bekannt)}")
    await control(name, action)
    log.info("Dienst %s: %s", name, action)
    return web.json_response({"ok": True, "service": name, "action": action})


# ---------------------------------------------------------------- Benutzer


@needs("users")
async def api_users(request: web.Request):
    users: UserStore = request.app["users"]
    return web.json_response({
        "users": [{"name": u.name, "admin": u.admin, "areas": u.areas}
                  for u in users.users.values()],
        "areas": [{"key": k, "title": t} for k, t in AREAS],
    })


@needs("users")
async def api_user_save(request: web.Request):
    data = await request.json()
    users: UserStore = request.app["users"]
    name = str(data.get("name", "")).strip()
    areas = [a for a in data.get("areas", []) if a in AREA_KEYS]
    admin = bool(data.get("admin"))
    try:
        if users.get(name) is None:
            password = str(data.get("password") or "")
            if len(password) < 8:
                raise ValueError("Passwort braucht mindestens 8 Zeichen")
            users.add(name, password, admin=admin, areas=areas)
            log.info("Benutzer %s angelegt", name)
        else:
            users.set_areas(name, areas, admin)
            if data.get("password"):
                if len(str(data["password"])) < 8:
                    raise ValueError("Passwort braucht mindestens 8 Zeichen")
                users.set_password(name, str(data["password"]))
                request.app["sessions"].drop_user(name)
            log.info("Benutzer %s geaendert", name)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return web.json_response({"ok": True})


@needs("users")
async def api_user_delete(request: web.Request):
    name = request.match_info["name"]
    users: UserStore = request.app["users"]
    if _user(request).name == name:
        raise web.HTTPBadRequest(text="Sich selbst kann man nicht loeschen")
    try:
        users.remove(name)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    request.app["sessions"].drop_user(name)
    log.info("Benutzer %s geloescht", name)
    return web.json_response({"ok": True})


async def api_password(request: web.Request):
    """Eigenes Passwort aendern - dafuer braucht es kein Sonderrecht."""
    user = _user(request)
    if user is None:
        raise web.HTTPUnauthorized(text="nicht angemeldet")
    data = await request.json()
    users: UserStore = request.app["users"]
    if users.check(user.name, str(data.get("old", ""))) is None:
        raise web.HTTPBadRequest(text="Altes Passwort stimmt nicht")
    new = str(data.get("new", ""))
    if len(new) < 8:
        raise web.HTTPBadRequest(text="Neues Passwort braucht mindestens 8 Zeichen")
    users.set_password(user.name, new)
    # Alle Sitzungen beenden, auch die eigene: nach einem Passwortwechsel
    # darf ein altes Cookie nicht weitergelten.
    request.app["sessions"].drop_user(user.name)
    log.info("Passwort geaendert: %s", user.name)
    return web.json_response({"ok": True,
                              "hinweis": "Geaendert. Bitte neu anmelden."})


# ------------------------------------------------------------------ Aufbau


def build_app(cfg, state, router, client, *, config_path: str,
              users_path: str, svxlink_dir: str, services_dir: str = "",
              manager_status=None, manager_control=None) -> web.Application:
    app = web.Application(middlewares=[session_middleware])

    users = UserStore(users_path)
    if users.ensure_default_admin():
        log.warning("Erststart: Benutzer 'admin' mit Passwort 'admin' angelegt "
                    "- bitte im Reiter System aendern.")

    app["cfg"] = cfg
    app["state"] = state
    app["router"] = router
    app["client"] = client
    app["users"] = users
    app["sessions"] = SessionStore()
    app["config_path"] = config_path
    app["svxlink_dir"] = svxlink_dir
    # Wo der Supervisor seinen Zustand hinterlegt - steht im Status, damit
    # eine leere Dienste-Karte sagen kann, wo vergeblich gesucht wurde.
    app["services_dir"] = services_dir or svxlink_dir
    app["manager_status"] = manager_status or (lambda: [])
    app["manager_control"] = manager_control

    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(str(TEMPLATES)))

    app.router.add_get("/", page_index)
    app.router.add_get("/login", page_login)
    app.router.add_post("/login", do_login)
    app.router.add_post("/logout", do_logout)

    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/log", api_log)
    app.router.add_get("/api/idlist", api_idlist)
    app.router.add_post("/api/idlist/refresh", api_idlist_refresh)
    app.router.add_post("/api/idlist/manual", api_idlist_manual)
    app.router.add_delete("/api/idlist/manual/{issi}",
                          api_idlist_manual_delete)
    app.router.add_get("/api/sds", api_sds)
    app.router.add_post("/api/sds", api_sds_send)
    app.router.add_delete("/api/sds", api_sds_clear)
    app.router.add_post("/api/sds/scheme", api_sds_scheme)
    app.router.add_get("/api/routes", api_routes)
    app.router.add_post("/api/routes", api_save_routes)
    app.router.add_get("/api/reflectors", api_reflectors)
    app.router.add_post("/api/reflectors", api_reflectors)
    app.router.add_get("/api/tetra", api_tetra)
    app.router.add_post("/api/tetra", api_tetra)
    app.router.add_get("/api/tetra_fw", api_tetra_fw)
    app.router.add_post("/api/tetra_fw", api_tetra_fw)
    app.router.add_get("/api/audio", api_audio)
    app.router.add_post("/api/audio", api_audio)
    app.router.add_get("/api/config", api_config)
    app.router.add_post("/api/config", api_config)
    app.router.add_get("/api/backup", api_backup)
    app.router.add_post("/api/restore", api_restore)
    app.router.add_post("/api/route/{gssi}/manual", api_route_manual)
    app.router.add_post("/api/command", api_command)
    app.router.add_get("/api/svxlink/{name}", api_svxlink_conf)
    app.router.add_post("/api/svxlink/{name}", api_svxlink_conf)
    app.router.add_post("/api/service/{name}/{action}", api_service)
    app.router.add_get("/api/users", api_users)
    app.router.add_post("/api/users", api_user_save)
    app.router.add_delete("/api/users/{name}", api_user_delete)
    app.router.add_post("/api/password", api_password)

    app.router.add_get("/healthz", lambda r: web.json_response(
        {"ok": True, "connected": client.connected}))
    app.router.add_static("/static/", STATIC)
    return app


async def start(cfg, state, router, client, **kwargs) -> web.AppRunner:
    app = build_app(cfg, state, router, client, **kwargs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.status_server.listen, cfg.status_server.port)
    await site.start()
    log.info("WebUI auf http://%s:%s/",
             cfg.status_server.listen, cfg.status_server.port)
    return runner
