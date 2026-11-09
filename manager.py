#!/usr/bin/env python3
"""Supervisor: Bruecke und die svxlink-Instanzen in einem Container.

Ein Prozess je Reflektor plus die Bruecke selbst. Jede Instanz bekommt ihre
eigene svxlink.conf aus `confgen.py` - eigenes Node-Rufzeichen, eigener
AUTH_KEY, eigenes USRP-Portpaar. Damit bleibt Grundregel 2 gewahrt: getrennt
sind die Instanzen durch den Prozess, nicht durch den Container.

Stirbt ein Kind, wird es mit Backoff neu gestartet. Beim Beenden bekommen
alle SIGTERM, und wer dann nicht geht, SIGKILL.

    python3 manager.py [config.yaml]
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

import confgen
from app import config as cfgmod

log = logging.getLogger("manager")

SVXLINK_BIN = os.environ.get("SVXLINK_BIN", "svxlink")
RESTART_MIN = 2.0
RESTART_MAX = 60.0


# Meldungen, die svxlink zwar schreibt, die hier aber nichts bedeuten.
# Unsere Logics haben RX=NONE/TX=NONE - eine Ansage koennte gar nirgends
# hinausgehen, und Sprachdateien sind im Image bewusst nicht dabei. Beim
# TG-Wechsel kaeme sonst je Ziffer eine Warnung. In svxlink.log stehen sie
# weiterhin, nur die Konsole bleibt lesbar.
LEISE = ("Could not find audio clip",)


class Child:
    """Ein ueberwachter Prozess."""

    def __init__(self, name: str, argv: list[str],
                 logfile: Path | None = None):
        self.name = name
        self.argv = argv
        # Mitschreiben, damit die Bruecke svxlinks Ausgabe lesen kann - sie
        # braucht sie, um eine TG-Umschaltung per DTMF zu bestaetigen.
        self.logfile = logfile
        self.process: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self._stop = False

    async def _tee(self) -> None:
        """Ausgabe in die Datei *und* nach stdout, damit beides da ist."""
        if self.process is None or self.process.stdout is None:
            return
        try:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            with open(self.logfile, "a", encoding="utf-8", errors="replace") as fh:
                async for zeile in self.process.stdout:
                    text = zeile.decode("utf-8", "replace").rstrip()
                    fh.write(text + "\n")
                    fh.flush()
                    if not any(muster in text for muster in LEISE):
                        print(f"[{self.name}] {text}", flush=True)
        except (OSError, ValueError) as exc:
            log.warning("[%s] Log nicht schreibbar: %s", self.name, exc)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = RESTART_MIN
        while not stop.is_set() and not self._stop:
            log.info("[%s] starte: %s", self.name, " ".join(self.argv))
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *self.argv,
                    stdout=asyncio.subprocess.PIPE if self.logfile else None,
                    stderr=asyncio.subprocess.STDOUT if self.logfile else None,
                )
                if self.logfile:
                    asyncio.create_task(self._tee())
            except FileNotFoundError:
                log.error("[%s] nicht gefunden: %s - Kind wird aufgegeben",
                          self.name, self.argv[0])
                return
            except Exception as exc:
                log.error("[%s] Start fehlgeschlagen: %s", self.name, exc)
                return

            code = await self.process.wait()
            self.process = None
            if stop.is_set() or self._stop:
                log.info("[%s] beendet (%s)", self.name, code)
                return

            self.restarts += 1
            log.warning("[%s] endete mit %s - Neustart in %.0f s",
                        self.name, code, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, RESTART_MAX)

    async def restart(self) -> None:
        """Prozess beenden, ohne die Aufsicht abzuschalten.

        Nicht ueber terminate() gehen: das setzt `_stop`, und die
        run()-Schleife sieht das, sobald der Prozess weg ist - sie gibt das
        Kind dann endgueltig auf, statt es neu zu starten. Hier bleibt
        `_stop` unberuehrt, damit die Schleife von selbst wieder anlaeuft.
        """
        process = self.process
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("[%s] reagiert nicht auf SIGTERM - SIGKILL", self.name)
            try:
                process.kill()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    async def terminate(self) -> None:
        self._stop = True
        process = self.process
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("[%s] reagiert nicht auf SIGTERM - SIGKILL", self.name)
            try:
                process.kill()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass


def build_children(cfg: cfgmod.Config, config_path: str,
                   confdir: Path) -> list[Child]:
    children = [
        Child("bruecke", [sys.executable, "-m", "app.main", config_path])
    ]
    if cfg.audio.backend != "usrp":
        log.info("Audio-Backend ist '%s' - svxlink wird nicht gestartet",
                 cfg.audio.backend)
        return children

    for reflector in cfg.reflectors:
        if not reflector.enabled:
            continue
        missing = reflector.ready_for_svxlink()
        if missing:
            log.warning("[%s] wird uebersprungen, es fehlt: %s",
                        reflector.name, ", ".join(missing))
            continue
        conf = confgen.conf_path(confdir, reflector.name)
        children.append(Child(
            f"svxlink-{reflector.name}",
            # Kein --logfile: damit schreibt svxlink still in die Datei statt
            # nach stdout, und im Container-Log steht dann gar nichts mehr.
            # Wir fangen stdout selbst ab, siehe Child._tee().
            [SVXLINK_BIN, f"--config={conf}"],
            logfile=conf.parent / "svxlink.log",
        ))
    return children


STATUS_FILE = "services.json"
COMMAND_FILE = "services.cmd"


async def _bridge(children: list[Child], confdir: Path,
                  stop: asyncio.Event) -> None:
    """Verbindung zur WebUI ueber zwei Dateien.

    Die Oberflaeche laeuft in der Bruecke, die svxlink-Prozesse haengen am
    Supervisor - der eine kann den anderen nicht direkt steuern. Statt IPC
    aufzuziehen schreibt der Supervisor einmal je Sekunde seinen Zustand
    und liest dabei eine Kommandodatei ein, die die UI hinterlegt.
    """
    status = confdir / STATUS_FILE
    command = confdir / COMMAND_FILE
    by_name = {child.name: child for child in children}
    confdir.mkdir(parents=True, exist_ok=True)

    while not stop.is_set():
        try:
            status.write_text(json.dumps([
                {"name": c.name,
                 "running": c.process is not None and c.process.returncode is None,
                 "restarts": c.restarts}
                for c in children
            ]), encoding="utf-8")
        except OSError as exc:
            log.debug("Statusdatei nicht schreibbar: %s", exc)

        try:
            if command.is_file():
                # Erst wegbenennen, dann lesen: was die UI in der Zwischenzeit
                # schreibt, landet in einer frischen Datei und geht nicht
                # verloren. Rename im selben Verzeichnis ist atomar.
                work = command.with_suffix(".cmd.work")
                os.replace(command, work)
                zeilen = work.read_text(encoding="utf-8").splitlines()
                work.unlink(missing_ok=True)

                for line in zeilen:
                    name, _, action = line.strip().partition(" ")
                    if not name:
                        continue
                    child = by_name.get(name)
                    if child is None:
                        log.warning("Kommando fuer unbekannten Dienst %r", name)
                    elif action == "restart":
                        log.info("WebUI: Neustart %s", name)
                        await child.restart()
                    elif action == "stop":
                        log.info("WebUI: Stop %s", name)
                        await child.terminate()
                    elif action == "start":
                        if child.process is None:
                            child._stop = False
        except OSError as exc:
            log.debug("Kommandodatei nicht lesbar: %s", exc)

        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def amain(config_path: str, confdir: Path) -> int:
    cfg = cfgmod.load(config_path)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if cfg.audio.backend == "usrp":
        try:
            for path in confgen.write_all(cfg, confdir):
                log.info("svxlink-Konfiguration: %s", path)
        except cfgmod.ConfigError as exc:
            log.error("svxlink-Konfiguration nicht erzeugbar: %s", exc)

    # Der Bruecke sagen, wo der Supervisor seinen Zustand hinschreibt.
    # Sonst rechnet sie sich den Pfad selbst aus dem Config-Pfad aus, und
    # sobald der woanders liegt (relativer Pfad, Config ausserhalb von
    # /data, eigenes --svxlink-conf-dir), suchen beide an verschiedenen
    # Stellen: die Dienste-Karte bleibt leer und sagt nicht warum.
    os.environ["SVXTETRA_SVXLINK_DIR"] = str(confdir)

    children = build_children(cfg, config_path, confdir)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # Windows
            pass

    tasks = [asyncio.create_task(child.run(stop), name=child.name)
             for child in children]
    tasks.append(asyncio.create_task(_bridge(children, confdir, stop),
                                     name="bruecke-ipc"))
    log.info("%d Prozess(e) unter Aufsicht", len(children))

    await stop.wait()
    log.info("Beende alle Kinder ...")
    await asyncio.gather(*(child.terminate() for child in children),
                         return_exceptions=True)
    await asyncio.gather(*tasks, return_exceptions=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="manager.py")
    parser.add_argument(
        "config", nargs="?",
        default=os.environ.get("SVXTETRA_CONFIG", "config.yaml"),
    )
    parser.add_argument(
        "--svxlink-conf-dir",
        default=os.environ.get("SVXTETRA_SVXLINK_DIR", "/data/svxlink"),
    )
    args = parser.parse_args()
    try:
        return asyncio.run(amain(args.config, Path(args.svxlink_conf_dir)))
    except cfgmod.ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"Datei nicht gefunden: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
