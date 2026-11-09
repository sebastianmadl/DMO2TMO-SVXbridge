"""Benutzer, Passwoerter, Rechte.

Liegt bewusst nicht in der config.yaml: die schreibt die WebUI beim
Bearbeiten von Routen neu, und Zugangsdaten haben darin nichts verloren.

Passwoerter werden als PBKDF2-HMAC-SHA256 mit Salt abgelegt - das kann die
Standardbibliothek, wir brauchen dafuer keine zusaetzliche Abhaengigkeit.

Die Rechte sind eine Matrix Benutzer x Bereich, passend zu den Reitern der
Oberflaeche. Wer einen Bereich nicht hat, sieht ihn gar nicht erst.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# Bereiche = Reiter der WebUI. Reihenfolge bestimmt die Anzeige.
# Die Bereiche sind genau das, was es auch als Ansicht gibt. Reflektoren
# stecken im Status, der Zeitplan in den Routen - dafuer eigene Rechte
# anzubieten haette nur eine Auswahl vorgegaukelt, die nichts bewirkt.
AREAS = [
    ("status", "Status"),
    ("routes", "Routen und Zeitplan"),
    ("sds", "SDS lesen und senden"),
    ("log", "Log"),
    ("system", "System, Pegel, Dienste"),
    ("config", "config.yaml bearbeiten"),
    ("users", "Benutzerverwaltung"),
]
AREA_KEYS = [key for key, _ in AREAS]

DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "admin"

PBKDF2_ROUNDS = 240_000
SESSION_HOURS = 12
# Wer die Oberflaeche benutzt, soll nicht mitten in der Arbeit
# hinausfliegen. Die Frist laeuft deshalb mit - hoechstens aber
# einmal je Stunde, sonst schreibt jeder Klick ein neues Cookie.
SESSION_RENEW_AFTER = 3600.0


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
    )
    return f"pbkdf2${PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds)
    )
    return hmac.compare_digest(check.hex(), digest)


@dataclass
class User:
    name: str
    password: str = ""                      # gehasht
    admin: bool = False
    areas: list[str] = field(default_factory=list)

    def may(self, area: str) -> bool:
        return self.admin or area in self.areas

    def as_dict(self) -> dict:
        return {"password": self.password, "admin": self.admin,
                "areas": list(self.areas)}


class UserStore:
    """Benutzerliste in einer eigenen YAML-Datei."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.users: dict[str, User] = {}
        self.load()

    # --------------------------------------------------------------- Datei

    def load(self) -> None:
        if not self.path.is_file():
            self.users = {}
            return
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.users = {
            name: User(
                name=name,
                password=data.get("password", ""),
                admin=bool(data.get("admin")),
                areas=[a for a in data.get("areas", []) if a in AREA_KEYS],
            )
            for name, data in (raw.get("users") or {}).items()
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"users": {n: u.as_dict() for n, u in self.users.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def ensure_default_admin(self) -> Optional[str]:
        """Legt beim ersten Start 'admin' mit dem Passwort 'admin' an.

        Bewusst vorhersagbar, damit man nach dem ersten Start ohne Umweg
        ueber das Log hineinkommt. Gibt das Passwort zurueck, solange es
        noch das voreingestellte ist - die Oberflaeche weist dann darauf
        hin, dass es geaendert gehoert.
        """
        if self.users:
            return None
        self.users["admin"] = User(name="admin",
                                   password=hash_password(DEFAULT_PASSWORD),
                                   admin=True, areas=list(AREA_KEYS))
        self.save()
        return DEFAULT_PASSWORD

    def uses_default_password(self) -> bool:
        """Laeuft hier noch admin/admin? Dann muss die UI meckern."""
        user = self.users.get("admin")
        return bool(user and verify_password(DEFAULT_PASSWORD, user.password))

    # ------------------------------------------------------------ Benutzer

    def get(self, name: str) -> Optional[User]:
        return self.users.get(name)

    def check(self, name: str, password: str) -> Optional[User]:
        user = self.users.get(name)
        if user is None or not user.password:
            # Trotzdem rechnen, damit ein unbekannter Name nicht schneller
            # antwortet als ein falsches Passwort.
            hash_password(password)
            return None
        return user if verify_password(password, user.password) else None

    def add(self, name: str, password: str, admin: bool = False,
            areas: Optional[list[str]] = None) -> User:
        if not name.strip():
            raise ValueError("Benutzername fehlt")
        if name in self.users:
            raise ValueError(f"Benutzer {name!r} gibt es schon")
        user = User(name=name, password=hash_password(password), admin=admin,
                    areas=[a for a in (areas or []) if a in AREA_KEYS])
        self.users[name] = user
        self.save()
        return user

    def set_password(self, name: str, password: str) -> None:
        user = self.users.get(name)
        if user is None:
            raise ValueError(f"Unbekannter Benutzer {name!r}")
        user.password = hash_password(password)
        self.save()

    def set_areas(self, name: str, areas: list[str], admin: bool) -> None:
        user = self.users.get(name)
        if user is None:
            raise ValueError(f"Unbekannter Benutzer {name!r}")
        if user.admin and not admin and self._admin_count() <= 1:
            raise ValueError("Der letzte Administrator kann sich nicht "
                             "selbst die Rechte nehmen")
        user.admin = admin
        user.areas = [a for a in areas if a in AREA_KEYS]
        self.save()

    def remove(self, name: str) -> None:
        user = self.users.get(name)
        if user is None:
            return
        if user.admin and self._admin_count() <= 1:
            raise ValueError("Der letzte Administrator kann nicht "
                             "geloescht werden")
        del self.users[name]
        self.save()

    def _admin_count(self) -> int:
        return sum(1 for u in self.users.values() if u.admin)


class SessionStore:
    """Sitzungen im Speicher. Neustart heisst neu anmelden - das ist ok."""

    def __init__(self, hours: float = SESSION_HOURS):
        self.ttl = hours * 3600
        self._sessions: dict[str, tuple[str, float]] = {}

    def new(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (username, time.time() + self.ttl)
        self._expire()
        return token

    def user_of(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        entry = self._sessions.get(token)
        if entry is None:
            return None
        username, expires = entry
        if time.time() > expires:
            del self._sessions[token]
            return None
        return username

    def renew(self, token: Optional[str]) -> bool:
        """Frist verlaengern, wenn die Sitzung schon eine Weile laeuft.

        Liefert True, wenn dabei auch das Cookie neu gesetzt gehoert -
        sonst wuerde der Browser es wegwerfen, obwohl der Server die
        Sitzung noch kennt.
        """
        entry = self._sessions.get(token) if token else None
        if entry is None:
            return False
        username, expires = entry
        if expires - time.time() > self.ttl - SESSION_RENEW_AFTER:
            return False
        self._sessions[token] = (username, time.time() + self.ttl)
        return True

    def drop(self, token: Optional[str]) -> None:
        if token:
            self._sessions.pop(token, None)

    def drop_user(self, username: str) -> None:
        """Nach Passwortwechsel oder Loeschen alle Sitzungen beenden."""
        for token, (name, _) in list(self._sessions.items()):
            if name == username:
                del self._sessions[token]

    def _expire(self) -> None:
        now = time.time()
        for token, (_, expires) in list(self._sessions.items()):
            if now > expires:
                del self._sessions[token]
