#!/usr/bin/env python3
"""Erzeugt je Reflektor eine svxlink.conf.

Eine Instanz je Reflektor (Grundregel 2), jede mit eigenem Node-Rufzeichen,
eigenem AUTH_KEY und eigenem USRP-Portpaar.

Die Schluesselnamen stammen nicht aus dem Gedaechtnis, sondern aus dem
Quelltext von `ReflectorLogic.cpp` und `UsrpLogic.cpp` im Branch
svxlink-usrp. Zwei Fallen, die dort auffielen:

  * Der Reflektor-Host heisst **HOSTS** (Mehrzahl), nicht HOST.
  * Die USRP-Portnamen sind aus svxlinks Sicht benannt und damit aus
    unserer Sicht vertauscht: `USRP_TX_PORT` ist der Port, auf den svxlink
    *sendet* - dort muessen wir lauschen. `USRP_RX_PORT` ist der, auf dem
    svxlink *hoert* - dorthin senden wir.

    python3 confgen.py config.yaml --out /data/svxlink
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import config as cfgmod

# Pfad der Logic-Module im Image. Steht so in der mitgelieferten
# svxlink.conf der Debian-Bauart.
LOGIC_CORE_PATH = "/usr/lib/x86_64-linux-gnu/svxlink"
EVENT_HANDLER = "/usr/share/svxlink/events.tcl"
# Kommandonummer der ReflectorLogic im Link: "9" + "1<tg>" + "#"
DTMF_LINK_CMD = 9


def usrp_call(reflector: cfgmod.Reflector) -> str:
    """Rufzeichen fuer UsrpLogic.

    UsrpLogic laesst hoechstens 6 Zeichen zu ("Callsign is to long should
    have 6 digits maximum") und laedt sonst gar nicht - dann haengt die
    ReflectorLogic ohne Audiopfad in der Luft. Der Node-Suffix muss also
    weg; fuer ReflectorLogic bleibt das vollstaendige Rufzeichen.
    """
    return reflector.callsign.split("-")[0][:6]


def monitor_tgs(cfg: cfgmod.Config, reflector: cfgmod.Reflector) -> list[int]:
    """Alle TGs, die diese Instanz mithoeren soll.

    Das sind die fixen Zeilen des Reflektors. Die dynamische Zeile kommt
    ueber DEFAULT_TG und wird beim Umschalten neu geschrieben.
    """
    out = []
    for route in cfg.routes:
        if route.reflector != reflector.name or not route.enabled:
            continue
        if route.type == cfgmod.ROUTE_FIXED and route.tg:
            out.append(route.tg)
    return sorted(set(out))


def default_tg(cfg: cfgmod.Config, reflector: cfgmod.Reflector,
               dynamic_tg: int = 0) -> int:
    """Die TG, auf der diese Instanz sendet."""
    if dynamic_tg:
        return dynamic_tg
    if reflector.default_tg:
        return reflector.default_tg
    for route in cfg.routes:
        if route.reflector == reflector.name and route.enabled:
            if route.type == cfgmod.ROUTE_DYNAMIC:
                return route.default_tg
            if route.tg:
                return route.tg
    return 0


def render(cfg: cfgmod.Config, reflector: cfgmod.Reflector,
           dynamic_tg: int = 0) -> str:
    missing = reflector.ready_for_svxlink()
    if missing:
        raise cfgmod.ConfigError(
            f"Reflektor {reflector.name}: es fehlt {', '.join(missing)}"
        )

    tgs = monitor_tgs(cfg, reflector)
    tg = default_tg(cfg, reflector, dynamic_tg)
    # Die Sende-TG muss auch mitgehoert werden, sonst hoeren wir die
    # Gegenstelle auf der eigenen Gruppe nicht.
    if tg and tg not in tgs:
        tgs = sorted(set(tgs) | {tg})

    # Eine stumme SimplexLogic nur wegen der DTMF-PTY: ReflectorV2Logic und
    # UsrpLogic erben von LogicBase und haben gar keinen DTMF-Eingang.
    # DTMF_CTRL_PTY sitzt in Logic, also braucht es eine vollwertige Logic.
    # Die Kommandonummer aus CONNECT_LOGICS (hier 9) waehlt beim Waehlen den
    # Empfaenger: "9" + "1<tg>" + "#" landet ueber
    # LinkManager::sendCmdToLogics in ReflectorV2Logic::remoteCmdReceived.
    # DMO2TMO: eine feste Sprechgruppe, keine Umschaltung - also auch keine
    # DTMF-Logic mehr.
    dtmf = False
    logics = "UsrpLogic,ReflectorLogic" + (",DtmfLogic" if dtmf else "")
    connect = "UsrpLogic,ReflectorLogic" + (
        f",DtmfLogic:{DTMF_LINK_CMD}:dtmf" if dtmf else "")
    dtmf_block = f"""
[DtmfLogic]
TYPE=Simplex
RX=NONE
TX=NONE
CALLSIGN={usrp_call(reflector)}
DTMF_CTRL_PTY={reflector.pty_path()}
EVENT_HANDLER={EVENT_HANDLER}
DEFAULT_LANG=de_DE
""" if dtmf else ""

    return f"""# Erzeugt von confgen.py - Aenderungen werden ueberschrieben.
# Reflektor {reflector.name} / Node {reflector.callsign}

[GLOBAL]
LOGIC_CORE_PATH={LOGIC_CORE_PATH}
LOGICS={logics}
LINKS=UsrpToReflector
TIMESTAMP_FORMAT="%c"
CARD_SAMPLE_RATE=48000

[UsrpToReflector]
CONNECT_LOGICS={connect}
DEFAULT_ACTIVE=1
TIMEOUT=0
{dtmf_block}

[UsrpLogic]
TYPE=Usrp
USRP_HOST=127.0.0.1
# svxlink sendet hierhin, die Bruecke lauscht darauf
USRP_TX_PORT={reflector.usrp_listen_port}
# svxlink hoert hier, die Bruecke sendet dorthin
USRP_RX_PORT={reflector.usrp_port}
CALL={usrp_call(reflector)}
DEFAULT_TG={tg}
JITTER_BUFFER_DELAY=100
# Verstaerkung gehoert hierher und nicht in die Bruecke: svxlink rechnet
# in Fliesskomma und begrenzt danach weich (siehe *_LIMITER_THRESH),
# waehrend apply_gain() auf 16-Bit-PCM hart abschneidet.
# PREAMP     wirkt auf Audio vom Reflektor zu uns  (SVX -> TETRA)
# NET_PREAMP wirkt auf Audio von uns zum Reflektor (TETRA -> SVX)
PREAMP={cfg.audio.preamp_db:g}
NET_PREAMP={cfg.audio.net_preamp_db:g}
EVENT_HANDLER={EVENT_HANDLER}
DEFAULT_LANG=de_DE
# TETRA-Erweiterung (svxlink/patches/01-usrplogic-tetra.patch): ISSI und
# SDS als State-Events (Qso:info, Sds:info, ForwardSds:info) zum Reflektor
SHARE_USERINFO={1 if (cfg.tetra.issi_passthrough or cfg.tetra.sds_forward) else 0}

[ReflectorLogic]
TYPE={reflector.logic_type}
HOSTS={reflector.host}
HOST_PORT={reflector.port}
CALLSIGN={reflector.callsign}
AUTH_KEY="{reflector.auth_key}"
MONITOR_TGS={",".join(str(t) for t in tgs)}
DEFAULT_TG={tg}
TG_SELECT_TIMEOUT=3600
# Ohne diese beiden schaltet svxlink die *erste* Aussendung nach dem
# Aktivieren einer TG stumm. Am Reflektor sieht man dann zwar
# "Selecting TG", aber keinen Sprecher - der erste Ruf ginge verloren.
MUTE_FIRST_TX_LOC=0
MUTE_FIRST_TX_REM=0
AUDIO_CODEC=OPUS
JITTER_BUFFER_DELAY=0
EVENT_HANDLER={EVENT_HANDLER}
DEFAULT_LANG=de_DE
"""


def write_all(cfg: cfgmod.Config, outdir: Path,
              dynamic_tgs: dict[str, int] | None = None) -> list[Path]:
    """Schreibt je aktivem Reflektor eine Datei. Gibt die Pfade zurueck."""
    dynamic_tgs = dynamic_tgs or {}
    written = []
    for reflector in cfg.reflectors:
        if not reflector.enabled:
            continue
        # Jede Instanz in ihr eigenes Verzeichnis: svxlink liest sonst alle
        # .conf im Verzeichnis der Hauptdatei mit ein, und die zweite
        # ueberschreibt dann die Abschnitte der ersten.
        path = conf_path(outdir, reflector.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if dtmf_noetig(reflector):
            # svxlink legt nur den Symlink an, nicht das Verzeichnis darueber.
            Path(reflector.pty_path()).parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render(cfg, reflector, dynamic_tgs.get(reflector.name, 0)),
            encoding="utf-8",
        )
        written.append(path)
    return written


def dtmf_noetig(reflector: cfgmod.Reflector) -> bool:
    """Bei DMO2TMO nie - es gibt keine TG-Umschaltung."""
    return False


def conf_path(outdir: Path, name: str) -> Path:
    return outdir / name / "svxlink.conf"


def main() -> int:
    parser = argparse.ArgumentParser(prog="confgen.py")
    parser.add_argument("config", nargs="?", default="config.yaml")
    parser.add_argument("--out", default="/data/svxlink")
    parser.add_argument("--print", action="store_true",
                        help="nur ausgeben, nichts schreiben")
    args = parser.parse_args()

    try:
        cfg = cfgmod.load(args.config)
    except (cfgmod.ConfigError, FileNotFoundError) as exc:
        print(f"Konfiguration: {exc}", file=sys.stderr)
        return 2

    try:
        if args.print:
            for reflector in cfg.reflectors:
                if reflector.enabled:
                    print(render(cfg, reflector))
            return 0
        for path in write_all(cfg, Path(args.out)):
            print(f"geschrieben: {path}")
    except cfgmod.ConfigError as exc:
        print(f"Konfiguration: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
