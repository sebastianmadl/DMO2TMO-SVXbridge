"""DTMF-Ziffern in svxlinks Steuer-PTY schreiben.

Damit laesst sich die TG einer laufenden Instanz umschalten, ohne sie neu
zu starten.

**Das ist eine PTY, keine Datei.** svxlink legt beim Start ein PTY-Paar an
und setzt am konfigurierten Pfad einen Symlink auf das Slave-Device
`/dev/pts/N`. Steht dort eine echte Datei, hat jemand anderes sie angelegt
und svxlink konnte den Symlink nicht setzen - dann ist die Umschaltung
wirkungslos, und das muss auffallen statt still ins Leere zu laufen.

Weitere Eigenheiten, die man nicht raten kann:

* Geschrieben wird ASCII, ein Zeichen je DTMF-Ziffer, **ohne Newline** und
  in *einem* `write`. Gepuffertes `open()` taugt nicht.
* Die Rechte haengen am `/dev/pts/N`, nicht am Symlink - `chmod` auf den
  Symlink ist wirkungslos.
* Laeuft svxlink nicht, zeigt der Symlink ins Leere und der Schreibversuch
  wirft ENOENT oder EIO. Das heisst "svxlink ist unten", nicht "hat nicht
  geklappt".
* Die PTY ist bidirektional: ueber HF empfangene Ziffern kommen dort auch
  wieder heraus.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("dtmf")

# Was svxlinks dtmfCtrlPtyCmdReceived() ueberhaupt annimmt. Der Quelltext
# laesst A-F zu, echtes DTMF kennt nur A-D - wir bleiben beim Strengeren.
ERLAUBT = set("0123456789ABCD*#")

# ReflectorLogic-Kommandos, erreichbar ueber die Kommandonummer des Links:
#   1<tg>   TG waehlen          9*      TG-Status ansagen
#   1       vorherige TG        94<tg>  TG nur mithoeren
TG_TEMPLATE = "91{tg}#"


class PtyError(RuntimeError):
    """Schreiben in die PTY ging schief."""


class PtyDown(PtyError):
    """Die PTY zeigt ins Leere - svxlink laeuft nicht."""


@dataclass
class PtyStatus:
    path: str
    ok: bool
    detail: str
    target: str = ""

    def as_dict(self) -> dict:
        return {"path": self.path, "ok": self.ok, "detail": self.detail,
                "target": self.target}


def status(path: str | Path) -> PtyStatus:
    """Prueft, ob am Pfad wirklich eine brauchbare PTY haengt."""
    p = Path(path)
    if not p.is_symlink():
        if p.exists():
            return PtyStatus(str(p), False,
                             "Da liegt eine echte Datei statt eines Symlinks - "
                             "svxlink konnte die PTY nicht anlegen")
        return PtyStatus(str(p), False, "nicht vorhanden, svxlink laeuft nicht")

    ziel = os.path.realpath(p)
    try:
        modus = os.stat(p).st_mode
    except OSError as exc:
        return PtyStatus(str(p), False, f"Symlink zeigt ins Leere ({exc.strerror})",
                         ziel)
    if not stat.S_ISCHR(modus):
        return PtyStatus(str(p), False, "kein Zeichengeraet", ziel)
    return PtyStatus(str(p), True, "bereit", ziel)


def send(path: str | Path, digits: str) -> None:
    """Schreibt die Ziffern in einem Rutsch. Wirft PtyError bei Problemen."""
    fremd = [c for c in digits if c not in ERLAUBT]
    if fremd:
        raise PtyError(f"Unerlaubte Zeichen im Kommando: {''.join(fremd)!r}")
    if not digits:
        raise PtyError("Leeres Kommando")

    zustand = status(path)
    if not zustand.ok:
        raise PtyDown(f"{path}: {zustand.detail}")

    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.EIO, errno.ENXIO):
            raise PtyDown(f"{path}: svxlink laeuft nicht ({exc.strerror})")
        raise PtyError(f"{path} nicht zu oeffnen: {exc.strerror}")

    try:
        # Ein einziges write, ohne Newline - jedes Zeichen ist eine Ziffer.
        geschrieben = os.write(fd, digits.encode("ascii"))
        if geschrieben != len(digits):
            raise PtyError(
                f"Nur {geschrieben} von {len(digits)} Zeichen geschrieben")
    except OSError as exc:
        if exc.errno in (errno.EIO, errno.ENXIO):
            raise PtyDown(f"{path}: Gegenstelle weg ({exc.strerror})")
        raise PtyError(f"Schreiben auf {path} fehlgeschlagen: {exc.strerror}")
    finally:
        os.close(fd)

    log.info("DTMF %r an %s", digits, path)


def select_tg(path: str | Path, tg: int,
              template: str = TG_TEMPLATE) -> str:
    """Schaltet die TG der laufenden ReflectorLogic um."""
    if tg <= 0:
        raise PtyError(f"Ungueltige TG: {tg}")
    kommando = template.format(tg=tg)
    send(path, kommando)
    return kommando
