"""HTTP Digest Access Authentication (RFC 2617/2831, MD5 + qop=auth).

Der brew-server verlangt Digest MD5 mit qop=auth. aiohttp bringt das
nicht mit, ist aber schnell selbst gebaut.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Dict

_TOKEN_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^,]*))')


def parse_challenge(header: str) -> Dict[str, str]:
    """Zerlegt einen WWW-Authenticate-Header in seine Felder."""
    if not header:
        return {}
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "digest":
        return {}
    out: Dict[str, str] = {}
    for match in _TOKEN_RE.finditer(rest):
        key = match.group(1).lower()
        out[key] = match.group(2) if match.group(2) is not None else (match.group(3) or "").strip()
    return out


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def build_authorization(
    username: str,
    password: str,
    method: str,
    uri: str,
    challenge: Dict[str, str],
    nonce_count: int = 1,
) -> str:
    """Baut den Authorization-Header fuer eine Digest-Antwort."""
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "")
    opaque = challenge.get("opaque")
    algorithm = challenge.get("algorithm", "MD5")

    cnonce = os.urandom(8).hex()
    nc = f"{nonce_count:08x}"

    ha1 = _md5(f"{username}:{realm}:{password}")
    if algorithm.upper() == "MD5-SESS":
        ha1 = _md5(f"{ha1}:{nonce}:{cnonce}")
    ha2 = _md5(f"{method}:{uri}")

    if qop:
        # Server darf mehrere anbieten, wir koennen nur auth.
        chosen = "auth" if "auth" in [q.strip() for q in qop.split(",")] else qop
        response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{chosen}:{ha2}")
    else:
        chosen = ""
        response = _md5(f"{ha1}:{nonce}:{ha2}")

    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        f"algorithm={algorithm}",
    ]
    if chosen:
        parts += [f"qop={chosen}", f"nc={nc}", f'cnonce="{cnonce}"']
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)
