"""DMO2TMO SVXBridge - Bruecke TETRA (Brew) <-> SVXLink.

Verbindung, Frame-Layer, ACELP-Codec, USRP-Anbindung an svxlink,
Mapping-Tabelle mit Zeitsteuerung, SDS-Control und WebUI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from . import config as cfgmod
from . import dtmf
from .brew.client import BrewClient
from .routing import Router
from .state import AppState
from .web import logbuffer
from .idlist import IdList
from .sdsbox import SdsBox
from .web import server as web_server

log = logging.getLogger("main")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    # Ringpuffer fuer den Live-Log der WebUI
    logbuffer.install(getattr(logging, level.upper(), logging.INFO))


def _services(svxlink_dir: str) -> list:
    """Zustand der Kinder, den manager.py als JSON hinterlegt."""
    path = os.path.join(svxlink_dir, "services.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return []          # kein Supervisor - die UI zeigt das an


async def _service_cmd(svxlink_dir: str, name: str, action: str) -> None:
    """Kommando fuer manager.py hinterlegen; der holt es binnen einer Sekunde.

    Angehaengt, nicht ueberschrieben: beim Neustart mehrerer Instanzen
    kaemen sonst zwei Kommandos so dicht hintereinander, dass das zweite
    das erste ueberschreibt, bevor der Supervisor gelesen hat.
    """
    os.makedirs(svxlink_dir, exist_ok=True)
    path = os.path.join(svxlink_dir, "services.cmd")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name} {action}\n")


async def _tg_bestaetigt(logfile: Path, tg: int, seit: int,
                         timeout: float = 5.0) -> bool:
    """Wartet, bis svxlink die TG-Umschaltung ins Log schreibt."""
    marke = f"Selecting TG #{tg}"
    ende = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < ende:
        try:
            with open(logfile, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(seit)
                if marke in fh.read():
                    return True
        except OSError:
            pass
        await asyncio.sleep(0.25)
    return False


async def _dynamic_changed(cfg, router: Router, svxlink_dir: str,
                           reflector: str) -> None:
    """Dynamische Zeile zeigt auf eine andere TG.

    Bevorzugt per DTMF in die laufende Instanz - das geht ohne Neustart und
    damit ohne Audiolueck. Nur wenn das abgeschaltet ist oder die PTY
    nachweislich fehlt, wird die Konfiguration neu geschrieben und die
    Instanz neu gestartet.
    """
    if cfg.audio.backend != "usrp":
        return
    reflektor = cfg.reflector(reflector)
    tg = router.dynamic_tgs_by_reflector().get(reflector)
    if reflektor is None or not tg:
        return

    logfile = Path(svxlink_dir) / reflector / "svxlink.log"
    if reflektor.pty_enabled:
        try:
            groesse = logfile.stat().st_size if logfile.exists() else 0
            kommando = dtmf.select_tg(reflektor.pty_path(), tg)
            if await _tg_bestaetigt(logfile, tg, groesse):
                log.info("Reflektor %s per DTMF (%s) auf TG %s umgeschaltet",
                         reflector, kommando, tg)
                return
            log.warning("Reflektor %s: DTMF %s gesendet, aber svxlink hat die "
                        "Umschaltung auf TG %s nicht bestaetigt", reflector,
                        kommando, tg)
        except dtmf.PtyDown as exc:
            log.warning("Reflektor %s: %s", reflector, exc)
        except dtmf.PtyError as exc:
            log.error("Reflektor %s: DTMF fehlgeschlagen: %s", reflector, exc)

    # Notnagel: Konfiguration neu schreiben und die Instanz neu starten.
    log.warning("Reflektor %s: schalte ueber Neustart um - das kostet ein "
                "paar Sekunden Audio", reflector)
    import confgen
    try:
        confgen.write_all(cfg, Path(svxlink_dir),
                          router.dynamic_tgs_by_reflector())
    except Exception as exc:
        log.error("svxlink.conf fuer %s nicht erzeugbar: %s", reflector, exc)
        return
    await _service_cmd(svxlink_dir, f"svxlink-{reflector}", "restart")


async def ticker(router: Router, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await router.tick()
        except Exception:
            log.exception("Ticker-Fehler")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def amain(path: str) -> int:
    cfg = cfgmod.load(path)
    setup_logging(cfg.log_level)

    log.info("DMO2TMO SVXBridge startet")
    log.info("Brew: %s  ISSI %s  Mode %s", cfg.brew.url, cfg.brew.issi, cfg.brew.mode)
    log.info("Audio-Backend: %s", cfg.audio.backend)
    for route in cfg.routes:
        log.info(
            "  GSSI %-8s -> %-4s TG %-8s %-7s Prio %-3s %s",
            route.gssi, route.reflector or "-",
            route.default_tg if route.type == cfgmod.ROUTE_DYNAMIC else route.tg,
            route.type, route.priority,
            "aus" if not route.enabled else (
                route.shutdown_mode if route.schedule.enabled else "immer an"
            ),
        )

    state = AppState()
    router = Router(cfg, state)
    client = BrewClient(cfg.brew, router)
    router.attach(client)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    await router.start_sources()

    data_dir = os.path.dirname(os.path.abspath(path)) or "."
    # Postfach neben die Config legen, damit ein Neustart es nicht leert.
    router.sdsbox = SdsBox(Path(data_dir) / "sds.json")
    state.namebook = IdList(Path(data_dir) / "idlist.sqlite")
    svxlink_dir = os.environ.get("SVXTETRA_SVXLINK_DIR",
                                 os.path.join(data_dir, "svxlink"))
    router.on_dynamic_change = lambda name: _dynamic_changed(
        cfg, router, svxlink_dir, name)

    runner = None
    if cfg.status_server.enabled:
        runner = await web_server.start(
            cfg, state, router, client,
            config_path=path,
            users_path=os.path.join(data_dir, "users.yaml"),
            svxlink_dir=svxlink_dir,
            services_dir=svxlink_dir,
            manager_status=lambda: _services(svxlink_dir),
            manager_control=lambda name, action: _service_cmd(
                svxlink_dir, name, action),
        )

    tasks = [
        asyncio.create_task(client.run(), name="brew"),
        asyncio.create_task(ticker(router, stop), name="ticker"),
    ]

    router.tetra_fw.start_beacon()

    await stop.wait()
    log.info("Beende...")
    # Reihenfolge zaehlt: erst die Quellen (die schliessen laufende Rufe
    # sauber ab), dann was sonst noch offen ist, dann die Sendequeue leeren -
    # und erst danach die Verbindung zumachen.
    await router.stop_sources()
    await router.close_calls()
    if not await client.flush():
        log.warning("Sendequeue beim Beenden nicht leer - moeglicherweise "
                    "bleibt ein Gruppenruf auf dem Server offen")
    await client.stop()
    router.tetra_fw.close()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if runner is not None:
        await runner.cleanup()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="dmo2tmo-svxbridge")
    parser.add_argument(
        "config",
        nargs="?",
        default=os.environ.get("SVXTETRA_CONFIG", "config.yaml"),
        help="Pfad zur config.yaml",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(amain(args.config))
    except cfgmod.ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"Datei nicht gefunden: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
