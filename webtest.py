"""Kompletter Durchlauf durch die WebUI-API einer laufenden Instanz.

Braucht einen laufenden Stack und die Anmeldung admin/admin:

    docker run -d --name svxstack -p 3007:3007 -v "$PWD/out:/data" \
        dmo2tmo-svxbridge python3 manager.py /data/live.yaml
    python3 webtest.py [http://127.0.0.1:3007]

    python3 webtest.py http://127.0.0.1:3007 admin geheim

Laesst sich gefahrlos gegen eine laufende Anlage fahren: was angelegt
wird (Route 4711, Benutzer "pruefling", ISSI 9999998), wird wieder
weggeraeumt, und echte Daten bleiben unangetastet. Eine echte SDS geht
dabei raus. Setzt am Ende Exit 1, wenn etwas fehlt.
"""
import copy
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import os
import sys

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3007"
# Nach dem ersten Passwortwechsel taugt admin/admin nicht mehr - Zugang
# also als Argument oder ueber die Umgebung.
USER = (sys.argv[2] if len(sys.argv) > 2
        else os.environ.get("SVXTETRA_USER", "admin"))
PASSWORT = (sys.argv[3] if len(sys.argv) > 3
            else os.environ.get("SVXTETRA_PASSWORD", "admin"))
ok = True
fehler = []


def check(name, cond, detail=""):
    global ok
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FEHL  {name} {detail}")
        fehler.append(name)
        ok = False


def sitzung():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def login(op, user, pw):
    return op.open(B + "/login", urllib.parse.urlencode(
        {"username": user, "password": pw}).encode())


def get(op, p):
    return json.load(op.open(B + p))


def post(op, p, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else b""
    r = urllib.request.Request(B + p, data, {"content-type": "application/json"},
                               method=method)
    return json.load(op.open(r))


def code_of(fn):
    try:
        fn()
        return 0
    except urllib.error.HTTPError as e:
        return e.code


# ------------------------------------------------------------------ Anmeldung
print("Anmeldung und Rechte:")
op, jar = sitzung()
check("API ohne Sitzung gesperrt", code_of(lambda: get(op, "/api/status")) == 401)
check("falsches Passwort abgelehnt",
      code_of(lambda: login(op, USER, "garantiert-falsch")) == 401)
try:
    login(op, USER, PASSWORT)
except urllib.error.HTTPError as fehler:
    if fehler.code == 401:
        print(f"Anmeldung als {USER!r} abgelehnt.")
        print("Aufruf: python3 webtest.py [url] [benutzer] [passwort]")
        print("        oder SVXTETRA_USER / SVXTETRA_PASSWORD setzen")
        sys.exit(1)
    raise
check("Anmeldung geht", any(c.name == "dmo2tmo" for c in jar))
d = get(op, "/api/status")
check("Standardpasswort wird gemeldet",
      isinstance(d.get("default_password"), bool),
      str(d.get("default_password")))

# --------------------------------------------------------------------- Status
print("Status:")
for feld in ("brew", "reflectors", "routes", "floors", "active_calls",
             "last_heard", "subscribers", "known_stations", "counters",
             "services", "uptime",
             "net_preamp_db", "preamp_db", "gain_svx_db", "gain_tetra_db"):
    check(f"Feld {feld}", feld in d)
# Ohne brew-Verbindung laesst sich nichts verschicken. Das ist ein
# gueltiger Zustand (Zugang abgeschaltet, Server nicht erreichbar) und darf
# den ganzen Durchlauf nicht abstuerzen lassen.
VERBUNDEN = d["brew"]["connected"] is True
if not VERBUNDEN:
    print(f"  hinweis  keine brew-Verbindung ({d['brew'].get('last_error') or '?'})"
          " - alles, was senden muesste, wird uebersprungen")
# Keine festen Zahlen: die Anlage waechst (R3 kam dazu). Geprueft wird
# der Zusammenhang - fuer jeden aktiven Reflektor muss ein Dienst laufen.
check("Reflektoren gelistet", len(d["reflectors"]) >= 1,
      str([r["name"] for r in d["reflectors"]]))
aktiv = [r["name"] for r in d["reflectors"] if r.get("enabled")]
laeuft = {s["name"] for s in d["services"] if s["running"]}
check("Bruecke laeuft", "bruecke" in laeuft, str(sorted(laeuft)))
check("je aktivem Reflektor eine laufende Instanz",
      all(f"svxlink-{name}" in laeuft for name in aktiv),
      f"aktiv={aktiv}, laeuft={sorted(laeuft)}")
check("keine Instanz fuer abgeschaltete Reflektoren",
      not [r["name"] for r in d["reflectors"] if not r.get("enabled")
           and f"svxlink-{r['name']}" in laeuft])
check("alle gemeldeten Dienste laufen",
      all(s["running"] for s in d["services"]), str(d["services"]))

# --------------------------------------------------------------------- Routen
print("Routen:")
original = get(op, "/api/routes")["routes"]
check("Routen gelistet", len(original) >= 1)
# DMO2TMO: genau eine Zeile - eine zweite muss abgelehnt werden, und zum
# Pruefen wird die vorhandene voruebergehend ersetzt.
check("zweite Zeile abgelehnt", code_of(lambda: post(op, "/api/routes", {
    "routes": original + [{"gssi": 4712, "reflector": "R1", "tg": 2322}]})) == 400)
neu = []
neu.append({"gssi": 4711, "name": "Vollcheck", "reflector": "R1", "tg": 2321,
            "type": "fixed", "priority": 1, "enabled": True, "default_tg": 0,
            "dynamic_timeout_minutes": 10, "shutdown_mode": "grace",
            "grace_minutes": 5, "max_extend_minutes": 120,
            "schedule": {"enabled": True, "days": ["MO", "WE"],
                         "windows": [{"start": "20:00", "end": "22:00"}]}})
r = post(op, "/api/routes", {"routes": neu})
check("Zeile angelegt", r["ok"])
d = get(op, "/api/status")
zeile = [x for x in d["routes"] if x["gssi"] == 4711]
check("sofort aktiv, ohne Neustart", len(zeile) == 1)
check("Zeitplan uebernommen",
      zeile and zeile[0]["schedule"]["days"] == ["MO", "WE"])
if VERBUNDEN:
    check("Affiliation nachgezogen", 4711 in d["brew"]["affiliated"],
          str(d["brew"]["affiliated"]))

check("GSSI 0 abgelehnt", code_of(lambda: post(op, "/api/routes", {
    "routes": [{"gssi": 0, "reflector": "R1", "tg": 1}]})) == 400)
check("unbekannter Reflektor abgelehnt", code_of(lambda: post(op, "/api/routes", {
    "routes": [{"gssi": 5, "reflector": "gibtsnicht", "tg": 1}]})) == 400)
check("fixe Zeile ohne TG abgelehnt", code_of(lambda: post(op, "/api/routes", {
    "routes": [{"gssi": 5, "reflector": "R1", "tg": 0, "type": "fixed"}]})) == 400)

print("Handbetrieb:")
post(op, f"/api/route/4711/manual?value=off")
d = get(op, "/api/status")
zeile = [x for x in d["routes"] if x["gssi"] == 4711][0]
check("manuell aus", zeile["manual"] is False and zeile["active"] is False)
post(op, f"/api/route/4711/manual?value=auto")
check("zurueck auf Automatik",
      [x for x in get(op, "/api/status")["routes"]
       if x["gssi"] == 4711][0]["manual"] is None)
check("unbekannte GSSI -> 404",
      code_of(lambda: post(op, "/api/route/9999/manual?value=off")) == 404)
check("unsinniger Wert -> 400",
      code_of(lambda: post(op, "/api/route/4711/manual?value=quatsch")) == 400)

post(op, "/api/routes", {"routes": original})
check("aufgeraeumt", len(get(op, "/api/routes")["routes"]) == len(original))

# -------------------------------------------------------------------- TETRA
print("TETRA-Einstellungen:")
t = get(op, "/api/tetra")
check("ISSI- und SDS-Schalter gelistet",
      "issi_passthrough" in t and "sds_forward" in t, str(t))

# ---------------------------------------------------------------- Reflektoren
print("Reflektoren:")
rf = get(op, "/api/reflectors")
check("mit allen Feldern", "auth_key" in rf["reflectors"][0]
      and "pty_enabled" in rf["reflectors"][0])
check("Zuordnung zu GSSIs", isinstance(rf["in_use"], dict))

# ---------------------------------------------------------------------- Pegel
print("Audiopegel:")
a = get(op, "/api/audio")
merker = dict(a)
for feld in ("net_preamp_db", "preamp_db", "gain_svx_db", "gain_tetra_db"):
    check(f"Feld {feld}", feld in a)
check("Feld hang_ms", "hang_ms" in a)
r = post(op, "/api/audio", {"hang_ms": 800})
check("Nachlauf ohne Neustart", "restarted" not in r)
check("Nachlauf uebernommen", get(op, "/api/audio")["hang_ms"] == 800)
check("Nachlauf ueber der Grenze abgelehnt", code_of(
    lambda: post(op, "/api/audio", {"hang_ms": 99000})) == 400)
check("Nachlauf als Text abgelehnt", code_of(
    lambda: post(op, "/api/audio", {"hang_ms": "lang"})) == 400)
post(op, "/api/audio", {"hang_ms": merker["hang_ms"]})

r = post(op, "/api/audio", {"gain_svx_db": 1.5})
check("Bruecken-Regler ohne Neustart", "restarted" not in r)
check("uebernommen", get(op, "/api/audio")["gain_svx_db"] == 1.5)
check("99 dB abgelehnt", code_of(
    lambda: post(op, "/api/audio", {"gain_svx_db": 99})) == 400)
check("Text abgelehnt", code_of(
    lambda: post(op, "/api/audio", {"gain_svx_db": "laut"})) == 400)
post(op, "/api/audio", {"gain_svx_db": merker["gain_svx_db"]})
check("zurueckgesetzt",
      get(op, "/api/audio")["gain_svx_db"] == merker["gain_svx_db"])

# --------------------------------------------------------------------- Config
print("config.yaml und Sicherung:")
cfg = get(op, "/api/config")
check("lesbar", "brew:" in cfg["text"])
check("kaputtes YAML abgelehnt", code_of(
    lambda: post(op, "/api/config", {"text": "brew: [unclosed"})) == 400)
check("Liste statt Zuordnung abgelehnt", code_of(
    lambda: post(op, "/api/config", {"text": "- a\n- b"})) == 400)
bak = op.open(B + "/api/backup").read().decode()
check("Sicherung enthaelt beide Dateien",
      "config.yaml" in bak and "users.yaml" in bak)
r = post(op, "/api/restore", {"text": bak})
check("Sicherung einspielbar", r["ok"])

# ------------------------------------------------------------------- Dienste
print("Dienste:")
check("unbekannter Dienst -> 404",
      code_of(lambda: post(op, "/api/service/gibtsnicht/restart")) == 404)
check("unsinnige Aktion -> 400",
      code_of(lambda: post(op, "/api/service/svxlink-R1/tanzen")) == 400)

# ----------------------------------------------------------------------- Log
print("Log:")
lg = get(op, "/api/log?since=0")
check("liefert Zeilen", len(lg["lines"]) > 0)
# Nicht auf "leer" pruefen: die Bruecke loggt weiter, und dann faellt der
# Test zufaellig durch. Entscheidend ist, dass nichts Altes noch mal kommt.
nachgereicht = get(op, f"/api/log?since={lg['last']}")["lines"]
check("since schneidet ab",
      all(x["seq"] > lg["last"] for x in nachgereicht),
      str([x["seq"] for x in nachgereicht][:5]))
warn = get(op, "/api/log?since=0&level=WARNING")["lines"]
check("Level-Filter", all(x["level"] in ("WARNING", "ERROR", "CRITICAL")
                          for x in warn))

# ------------------------------------------------------------ Rufzeichen
print("Rufzeichenliste:")
d = get(op, "/api/idlist")
for feld in ("count", "updated", "url", "manual"):
    check(f"Feld {feld}", feld in d)
check("Quelle voreingestellt", "radioid.net" in d["url"])

# Wegwerf-ISSI: echte Eintraege des Betreibers bleiben unangetastet.
PROBE_ISSI = 9999998
r = post(op, "/api/idlist/manual", {"issi": PROBE_ISSI, "name": "Vollcheck"})
check("eigener Name eingetragen",
      any(m["issi"] == PROBE_ISSI and m["name"] == "Vollcheck"
          for m in r["manual"]))
check("ISSI 0 abgelehnt", code_of(lambda: post(op, "/api/idlist/manual", {
    "issi": 0, "name": "x"})) == 400)
check("leerer Name abgelehnt", code_of(lambda: post(op, "/api/idlist/manual", {
    "issi": 2320001, "name": "  "})) == 400)
check("kaputte URL abgelehnt", code_of(lambda: post(op, "/api/idlist/refresh", {
    "url": "ftp://wo.auch.immer"})) == 400)
check("unbekannte ISSI loeschen -> 404", code_of(
    lambda: post(op, "/api/idlist/manual/1234567", method="DELETE")) == 404)
check("ISSI als Text loeschen -> 400", code_of(
    lambda: post(op, "/api/idlist/manual/hallo", method="DELETE")) == 400)
r = post(op, "/api/idlist/manual/{}".format(PROBE_ISSI), method="DELETE")
check("geloescht", not any(m["issi"] == PROBE_ISSI for m in r["manual"]))

# ---------------------------------------------------------------------- SDS
print("SDS:")
d = get(op, "/api/sds?since=0")
for feld in ("messages", "last", "counts", "control_issi", "own_issi"):
    check(f"Feld {feld}", feld in d)
vorher = d["last"]
check("Kodierung wird gemeldet",
      d["scheme"] in ("7bit", "8bit", "ucs2", "raw16", "ascii", "sdstl"),
      d["scheme"])
merk_scheme = d["scheme"]
check("auf UCS-2 umstellbar",
      post(op, "/api/sds/scheme", {"scheme": "ucs2"})["ok"])
check("greift sofort", get(op, "/api/sds?since=0")["scheme"] == "ucs2")
check("16-Bit roh waehlbar",
      post(op, "/api/sds/scheme", {"scheme": "raw16"})["ok"])
check("ASCII roh waehlbar",
      post(op, "/api/sds/scheme", {"scheme": "ascii"})["ok"])
check("SDS-TL waehlbar",
      post(op, "/api/sds/scheme", {"scheme": "sdstl"})["ok"])
check("Unsinn abgelehnt", code_of(
    lambda: post(op, "/api/sds/scheme", {"scheme": "klingonisch"})) == 400)
post(op, "/api/sds/scheme", {"scheme": merk_scheme})
check("since schneidet ab",
      all(m["seq"] > vorher
          for m in get(op, f"/api/sds?since={vorher}")["messages"]))
check("since muss eine Zahl sein",
      code_of(lambda: get(op, "/api/sds?since=viele")) == 400)
check("leerer Text abgelehnt", code_of(lambda: post(op, "/api/sds", {
    "destination": 2320997, "text": "   "})) == 400)
check("Ziel 0 abgelehnt", code_of(lambda: post(op, "/api/sds", {
    "destination": 0, "text": "hallo"})) == 400)
check("Ziel als Text abgelehnt", code_of(lambda: post(op, "/api/sds", {
    "destination": "keine zahl", "text": "hallo"})) == 400)

if VERBUNDEN:
    post(op, "/api/idlist/manual", {"issi": 2320997, "name": "Pruefstelle"})
    r = post(op, "/api/sds", {"destination": 2320997, "text": "Vollcheck"})
    check("gesendet", r["ok"] and r["message"]["text"] == "Vollcheck")
    neue = get(op, f"/api/sds?since={vorher}")["messages"]
    check("liegt im Postfach", any(
        m["text"] == "Vollcheck" and m["direction"] == "tx" for m in neue))
    check("Rohbytes zur Fehlersuche dabei", any(
        m["text"] == "Vollcheck" and m.get("raw", "")[:2] in ("02", "82")
        for m in neue), str([m.get("raw") for m in neue]))
    check("mit aufgeloestem Namen", any(
        m["peer"] == 2320997 and m["name"] == "Pruefstelle" for m in neue),
        str([m.get("name") for m in neue]))
    post(op, "/api/idlist/manual/2320997", method="DELETE")
else:
    check("ohne Verbindung wird das Senden abgelehnt, nicht verschluckt",
          code_of(lambda: post(op, "/api/sds",
                               {"destination": 2320997, "text": "x"})) == 503)

# ------------------------------------------------------------------ Benutzer
print("Benutzerverwaltung und Rechteschranken:")
post(op, "/api/users", {"name": "pruefling", "password": "geheim123",
                        "areas": ["status", "log"]})
check("angelegt", any(u["name"] == "pruefling" for u in get(op, "/api/users")["users"]))
check("zu kurzes Passwort abgelehnt", code_of(lambda: post(op, "/api/users", {
    "name": "kurz", "password": "abc", "areas": []})) == 400)

op2, _ = sitzung()
login(op2, "pruefling", "geheim123")
check("eingeschraenkter Benutzer sieht Status", "brew" in get(op2, "/api/status"))
check("darf Log", "lines" in get(op2, "/api/log?since=0"))
check("darf KEINE Routen", code_of(lambda: get(op2, "/api/routes")) == 403)
check("darf KEINE SDS lesen", code_of(lambda: get(op2, "/api/sds")) == 403)
check("darf die Kodierung nicht umstellen", code_of(
    lambda: post(op2, "/api/sds/scheme", {"scheme": "ucs2"})) == 403)
check("darf die Rufzeichenliste nicht sehen",
      code_of(lambda: get(op2, "/api/idlist")) == 403)
check("darf sie nicht aktualisieren", code_of(
    lambda: post(op2, "/api/idlist/refresh", {})) == 403)
check("darf KEINE SDS senden", code_of(lambda: post(op2, "/api/sds", {
    "destination": 2320997, "text": "verboten"})) == 403)
check("darf den Verlauf nicht leeren",
      code_of(lambda: post(op2, "/api/sds", method="DELETE")) == 403)
check("darf KEIN System", code_of(lambda: get(op2, "/api/audio")) == 403)
check("darf KEINE Benutzer", code_of(lambda: get(op2, "/api/users")) == 403)
check("darf config.yaml nicht", code_of(lambda: get(op2, "/api/config")) == 403)

seite = op2.open(B + "/").read().decode()
reiter = re.findall(r'data-tab="([a-z]+)"', seite)
check("sieht nur seine Reiter", reiter == ["status", "log"], str(reiter))
check("kein SDS-Reiter", "tab-sds" not in seite)
check("kein System-Reiter", "tab-system" not in seite)
volle_seite = op.open(B + "/").read().decode()
check("Zeitplan-Dialog ist da",
      all(x in volle_seite for x in
          ('id="plan-dlg"', 'id="plan-body"', 'id="plan-close"')))
voll = re.findall(r'data-tab="([a-z]+)"', op.open(B + "/").read().decode())
check("Administrator sieht alle fuenf",
      voll == ["status", "routes", "sds", "log", "system"], str(voll))
check("Name in der Kopfzeile", "DMO2TMO SVXBridge" in seite and "by OE4SXM" in seite)
check("CSS und JS mit Versionsstempel",
      'style.css?v=' in seite and 'app.js?v=' in seite)

r = post(op2, "/api/password", {"old": "geheim123", "new": "nochgeheimer"})
check("eigenes Passwort aenderbar", r["ok"])
check("alte Sitzung beendet", code_of(lambda: get(op2, "/api/status")) == 401)

check("sich selbst loeschen verboten", code_of(
    lambda: post(op, f"/api/users/{USER}", method="DELETE")) == 400)
post(op, "/api/users/pruefling", method="DELETE")
check("geloescht", not any(u["name"] == "pruefling"
                           for u in get(op, "/api/users")["users"]))

print()
print("ALLES OK." if ok else f"{len(fehler)} FEHLER: {', '.join(fehler)}")
sys.exit(0 if ok else 1)
