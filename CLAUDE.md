# CLAUDE.md — DMO2TMO SVXBridge

Arbeitsanweisung für die Weiterentwicklung. Diese Datei zuerst lesen.

# 🔀 DMO2TMO-Umbau (2026-11-09)

Aus svxtetra (mehrere Reflektoren, dynamische TG) wird **DMO2TMO
SVXBridge**: ein Reflektor, eine feste Sprechgruppe, dafuer ISSI und SDS
durch den Reflektor - im Format von DL1HRCs `tetra-contrib`, das TETRA OE
DMO verwendet.

**Deployment:** `/opt/dmo2tmo-svxbridge`, Container und Compose-Dienst
`dmo2tmo-svxbridge`, Port **3007** durchgehend (Container, Proxy-Ziel
`dmo2tmo-svxbridge:3007`, `127.0.0.1:3007`). Cookie heisst `dmo2tmo`.

**Zwei Bruecken bleiben parallel:** diese fuer **TETRA DMO OE**, die alte
svxtetra (Port 3006) fuer **SVXLink Austria**. Verschiedene Reflektoren,
also keine Kollision bei Node-Rufzeichen.

**Was sich geaendert hat:**

- Config-Pruefung: hoechstens ein aktiver Reflektor, genau eine Zeile,
  `type: fixed`. Die alten Felder (`pty_enabled`, `sds_control.*`,
  dynamische Felder) bleiben ladbar, tun aber nichts mehr. Datenmodell
  nicht umkrempeln - die Pfade fuer dynamisch sind nur tot, nicht weg.
- Keine DtmfLogic mehr in der svxlink.conf, dafuer `SHARE_USERINFO`.
- SDS: keine Kommandos mehr. Jede Text-SDS geht ins Postfach und ueber
  `forward_sds_to_svx()` an die UsrpLogic (`SDS:{json}` als USRP-TEXT).
  Rueckweg: `UsrpSource` erkennt `SDS:` und ruft `Router.on_svx_sds()`,
  Ziel-ISSI ueber `AppState.issi_for_callsign()` (eigene Namen + Gehoertes,
  **nicht** radioid.net).
- ISSI: `UsrpSink` schreibt die Quell-ISSI ins DMR-ID-Feld der
  TLV-Metadaten, `UsrpSource.talker_issi` liest sie zurueck,
  `on_svx_start` setzt sie als `source` im GROUP_TX.
- `build_metadata()` faengt Werte ueber 24 Bit ab (frueher TODO 1: ein
  OverflowError legte die Instanz lahm).
- Neuer Abschnitt `tetra:` (`issi_passthrough`, `sds_forward`), in der
  WebUI unter System -> „TETRA ↔ Reflektor“, `/api/tetra`.

**svxlink:** `Dockerfile` baut `tetra-contrib` (0d52873) + UsrpLogic aus
`svxlink-usrp` (a1e3427) + `svxlink/patches/`:

- `01-usrplogic-tetra.patch` — UsrpLogic: `Qso:info` bei Sprechbeginn/
  -ende aus der ISSI im DMR-ID-Feld, `TetraUsers:info`/`Register:info`
  einmal je ISSI, SDS als `Sds:info`/`ForwardSds:info`;
  `remoteReceivedPublishStateEvent` nimmt `Qso:info`,
  `Reflector:talker_start` und `ForwardSds:info` an. ReflectorV2Logic
  lernt `MsgStateEvent` empfangen und meldet `Reflector:talker_start`
  (konnte nur ReflectorLogic). Beide reichen `TetraUsers:info` weiter.
  `dv_users.json` liegt jetzt bei UsrpLogic, sonst scheitert `make
  install` auf tetra-contrib.
- `02-reflector-forwardsds.patch` — **fuer den Reflektor**: upstream
  schickt `ForwardSds:info` an das Gateway aus der Nachricht, also zurueck
  zum Absender. Jetzt an das Gateway, bei dem der Empfaenger registriert
  ist.

**Gegen echtes svxlink getestet** (lokal gebaut, svxreflector + zwei
Knoten mit UsrpLogic, Python als USRP-Gegenstelle): `Qso:info` mit
`active_issi` am Reflektor, beim zweiten Knoten Metadaten mit ISSI
2320998 und Rufzeichen, SDS per ForwardSds beim richtigen Gateway.
Docker-Build selbst nicht gelaufen (kein Docker hier) - der Patch-Schritt
aber exakt so nachgestellt.

**Bekannte Grenzen von upstream tetra-contrib (nicht unser Code):**
TetraLogic wertet empfangene State-Events nicht aus - sie ueberschreibt
`remoteReceivedPublishStateEvent` nicht, und `Qso:info` von fern ist dort
ein `toDo`. Eine SDS von uns kommt bei einer TetraLogic-Station also erst
an, wenn das upstream behoben ist; zwischen zwei UsrpLogic-Knoten geht es.

**Lastheard im OE-Verbund (2026-11-09):** der OE-Verbund nutzt kein
tetra-contrib, Lastheard laeuft ueber UDP 5220 (`tetra_fw_server`). Gebaut
in `app/tetra_fw.py`, Config `tetra_fw`, WebUI System -> „Lastheard
(TETRA-AUSTRIA)“. Protokoll in `docs/tetra_fw_protocol.md`, Stand in
`WORKLOG.md`.

---

# 📋 TODO (svxtetra, Stand 2026-09-08)

Historisch. 1 ist mit dem Umbau abgesichert, 2/4 sind mit nur einer
Sprechgruppe hinfaellig, 5 ist ueber die ISSI/Rufzeichen-Durchreichung
geloest. 3 und 6 gelten weiter.

## 1. TG-Obergrenze fehlt — ein Tippfehler legt eine Instanz lahm

`m_tg` in den USRP-Metadaten ist **3 Byte** breit, also hoechstens
16777215. Eine groessere TG laesst `build_metadata()` mit `OverflowError`
aussteigen, und zwar in `sink.start()` — also **bevor** Audio fliesst. Auf
dem betroffenen Reflektor geht danach **kein einziger Ruf** mehr durch, im
Log steht nur ein Stacktrace. Am 2026-09-08 live passiert.

Zwei Eingaenge lassen das durch:

- `Router.handle_command()` nimmt bei `allow_any_tg: true` jede
  Ziffernfolge ohne Obergrenze (`app/routing.py`, bei `tg = int(value)`)
- die Config-Pruefung begrenzt `tg` und `default_tg` nicht
  (`app/config.py`, `Route.validate()`)

Zu tun: beide auf 1…16777215 begrenzen, per SDS eine lesbare Antwort statt
eines Absturzes, und `build_metadata()` zusaetzlich absichern, damit eine
durchgerutschte Zahl hoechstens **einen** Ruf kostet. Mit Tests.

## 2. Zwei fixe Routen auf R3 — fremder Verkehr auf der falschen GSSI

Am VPS zeigen `2323` und `2326406` beide auf R3. Redet jemand auf 2326406
(z.B. OE1XUU), schaltet svxlink zwar dorthin um, schreibt in die
USRP-Metadaten aber seine **Sende-TG 2323** — die Bruecke ordnet es der
einzigen dazu passenden Route zu und sendet es auf **GSSI 2323** aus.
Belegt durch sein Log vom 2026-09-08 22:29.

Zu tun: eigene Instanz **R4** fuer 2326406 (eigenes Node-Rufzeichen,
eigener AUTH_KEY, eigene Ports) — oder die Route wieder entfernen. Eine
sendende TG je Instanz, das ist keine Stilfrage.

## 3. Uebersteuerung mit echter Sprache

Am VPS bei jedem Durchgang 0,67–0,99 % der Samples am Anschlag. Die 8/8 dB
waren mit **Testton** eingemessen; echte Sprache hat einen hoeheren
Scheitelfaktor. Zu tun: `preamp_db` (SVX→TETRA) um 3–4 dB zurueck, dann mit
den Warnungen im Log gegenpruefen.

## 4. R2 laeuft am VPS nicht

Im Start-Log fehlt GSSI 2321 in der Affiliation und es startet keine
`usrp-rx`-Quelle fuer R2. Also deaktiviert oder ihm fehlt Rufzeichen bzw.
AUTH_KEY. Klaeren, ob gewollt.

## 5. Talker-Alias — wer spricht da?

`UsrpLogic` schreibt sein **eigenes `CALL`** in die Metadaten, nicht das
Rufzeichen der sendenden Station (am 2026-09-08 mit umbenanntem R1
nachgewiesen). Offen sind zwei Wege, davor ein billiger Test:

- **davor:** in TetraLink schauen, ob waehrend eines Gruppenrufs ueberhaupt
  ein Alias angezeigt wird. Wenn nein, ist der Patch sinnlos.
- **Weg A:** TG als Quell-ISSI senden (funktioniert, gemessen: der
  brew-server reicht eine fremde Quell-ISSI durch). Zeigt *welche TG*,
  nicht *wer*.
- **Weg B:** `UsrpLogic` patchen. svxlink wird ohnehin selbst gebaut, das
  Feld ist im Paket vorhanden.

## 6. Zeitzone am VPS nachziehen

`TZ: Europe/Vienna` steht seit 2026-09-15 im `docker-compose.yml`, wirkt aber
erst, wenn der Container **neu erzeugt** wird (`docker compose up -d` am VPS,
ein `restart` reicht nicht - Umgebungsvariablen stehen beim Anlegen fest).
Bis dahin ist die Logzeit dort weiter UTC.

## 7. Kleineres, unentschieden

- **Zeitplan je Reflektor** — Sebastian hat gefragt, keine Variante
  gewaehlt. Solange ein Reflektor genau eine Zeile hat, deckungsgleich mit
  dem Zeitplan der Zeile.
- **Bekannte Rufzeichen sind nicht dauerhaft** — Last Heard liegt im
  Speicher, nach einem Neustart bleibt nur die eigene Namensliste. Koennte
  man beim Aufloesen mitschreiben.
- **Pegel nur mit Testton eingemessen**, nicht mit Sprache (siehe 3).
- **Etappe 7**, ISSI-Durchreichung durch den SVXReflector: braucht
  `tetra-contrib`, das kein UsrpLogic hat — die Branches muessten
  zusammengefuehrt werden.

---

## Was das Projekt ist

Brücke zwischen TETRA (Brew-Protokoll) und SVXLink. Gleiche Bauart wie
`svxdmr-multilink`, nur mit ACELP statt AMBE — dadurch kein md380-emu und
kein qemu, der Codec läuft nativ auf der CPU.

Betreiber: OE4SXM (Sebastian), läuft am eigenen VPS (Host `ms-photo`)
gegen einen selbst betriebenen `brew-server`. Umstieg auf
tetrapack.online ist später nur ein anderer Hostname plus deren
Credentials.

## Testserver-Zugangsdaten

Echter brew-server (nicht der Dummy aus `config.example.yaml`):

- URL: `wss://brew.oe1sxm.at/brew` (in `config.yaml`: `brew.url:
  "https://brew.oe1sxm.at"`, `brew.path: "/brew/"` — `BrewClient`
  wandelt https→wss selbst um)

Eingetragene Digest-User (nur Test-User):

| ISSI/User | Passwort |
|---|---|
| `100000001` | `<pw-bs1>` |
| `100000002` | `<pw-bs2>` |
| `100000009` | `<pw-bridge>` |

Welcher User/welche ISSI für welche Instanz gedacht ist, ist noch offen.

## Signalweg (Zielbild)

```
TETRA-Gerät → brew-server → [ dmo2tmo-svxbridge ] → svxlink (UsrpLogic) → SvxReflector
                              WS-Client + ACELP        eine Instanz je Reflektor
```

## Referenzen

- Brew-Spec: https://wiki.tetrapack.online/tetra/specifications/brew/
- brew-server (Rust, Serverseite): https://github.com/ysamouhos/brew-server
- flowstation (Rust, echter Brew-*Client* zum Abschauen):
  https://github.com/razvanzeces/flowstation
- ACELP-Referenzcode: ETSI EN 300 395-2, Anhang (bit-exaktes ANSI C)
- Patches dafür: `etsi_codec-patches` in osmo-tetra, inkl.
  `download_and_patch.sh`
- svxlink mit ISSI-Durchreichung: `dl1hrc/svxlink`, Branch `tetra-contrib`

## Grundregeln

1. **Der ETSI-Codec darf nicht ins Repo.** Lizenz. Das Build-Script zieht
   ihn zur Buildzeit von der ETSI-Seite, genau wie osmocom das macht.
2. **Ein Reflektor = eine svxlink-Instanz.** Niemals zwei ReflectorLogics
   über `LINKS` zusammenhängen — dann schlägt Audio von R1 auf R2 durch
   und die Brücke kann die Herkunft nicht mehr unterscheiden.
3. **Pro Reflektor genau eine dynamische Zeile.** Mehr wäre nicht
   eindeutig; die Config-Prüfung lehnt es ab.
4. **Alles was konfigurierbar ist, gehört in die WebUI.** Keine Einstellung
   nur in der YAML. Das ist bei diesen Tools Pflicht, kein Nice-to-have.
5. `selftest.py` und `smoketest.py` müssen nach jeder Änderung grün sein,
   `webtest.py` zusätzlich, wenn die WebUI angefasst wurde.

## Aufbau

```
app/
  main.py            Einstiegspunkt, Ticker
  config.py          Dataclasses, YAML, Validierung
  routing.py         Mapping-Tabelle, Floor-Lock, SDS-Kommandos
  schedule.py        Zeitfenster + Nachlauf-Zustandsmaschine
  state.py           Last Heard, Zähler, Subscriber-Cache
  status_server.py   /api/status, /healthz, manuelles Schalten
  brew/
    protocol.py      Nachrichten kodieren/dekodieren, ACELP-Bitpackung
    client.py        WebSocket, Digest-Auth, Reconnect
    digest.py        HTTP Digest MD5 qop=auth
    sds.py           SDS Type 4
  audio/base.py      NullSink, LoopbackSink, UsrpSink
  audio/acelp.py     ACELP per ctypes, 240 Samples <-> 137 Bit
  audio/usrp.py      USRP-Pakete für svxlink, 160 Samples je Paket
  audio/source.py    SVX -> TETRA: UDP empfangen, Keyup, encodieren
  sdsbox.py          Postfach fuer Kurznachrichten, Ringpuffer + sds.json
  idlist.py          ISSI -> Rufzeichen (radioid.net) + eigene Namen, SQLite
confgen.py           erzeugt je Reflektor eine svxlink.conf
manager.py           Supervisor für Brücke + svxlink-Instanzen
codec/
  build.sh           ETSI-Code holen, patchen, zwei .so bauen
  tetra_wrapper.c    C-Wrapper, zweimal übersetzt (Encoder/Decoder)
  patches/           die fünf osmocom-Patches (GPL), eingecheckt
```

Die Datenstruktur ist bereits vollständig — Scheduler, Prioritäten,
Reflektorzuordnung sind drin, auch wo die Etappe dahinter noch fehlt.
Beim Weiterbauen also **nicht das Datenmodell umkrempeln**, sondern die
leeren Stellen füllen.

---

# Fahrplan

## Etappe 1 — Verbindung ✅ fertig

WS-Client, HTTP-Digest, REGISTER, AFFILIATE auf mehrere GSSIs,
Reconnect mit Backoff.

**Fertig wenn:** beide Instanzen im brew-server-Dashboard als affiliierte
Subscriber stehen.

## Etappe 2 — Frame-Layer ✅ fertig

GROUP_TX / GROUP_IDLE / Traffic-Frames / SDS. Mapping-Tabelle mit
Floor-Lock je Reflektor. Loopback-Senke spiegelt ACELP-Frames unverändert
auf eine andere GSSI — damit ist alles außer dem Codec testbar.

**Fertig wenn:** zwei Instanzen mit gekreuzten GSSIs sich gegenseitig
hören (roh, ohne Codec).

## Etappe 2b — SDS-Control ✅ fertig

TG-Umschaltung per Text-SDS an eine Control-ISSI, Antwort zurück.
Kommandos: `2324606`, `R2 2321`, `0`/`off`, `?`.

---

## Etappe 3 — ACELP-Codec ✅ fertig (2026-09-04)

**Abnahme bestanden.** Über `brew.oe1sxm.at` gefahren, zwei Verbindungen,
Testton als ACELP-Frames gesendet und auf der Gegenseite decodiert:
50 Frames raus, 50 rein, 24000 Samples raus, 24000 rein, Korrelation
**0.984** bei 40 Samples Versatz — exakt die Codec-Laufzeit, die isoliert
gemessen wurde. Werkzeug dafür ist `livetest.py`.

**Was steht:**

- `codec/build.sh` — zieht das ETSI-ZIP, prüft MD5, wendet die fünf
  osmocom-Patches an, baut zwei `.so`. Kein ETSI-Code im Repo,
  `.gitignore` sichert das ab.
- `codec/tetra_wrapper.c` — ctypes-Schnittstelle, 240 Samples ↔ 137 Bit.
- `app/audio/acelp.py` — Anbindung. Fehlt die `.so`, gibt's einen sauberen
  Fehler statt Crash, damit die Tests auf Windows grün bleiben.
- `codectest.py` — läuft nur mit `.so`, überspringt sonst mit Exit 0.
- `Dockerfile` ist multi-stage, der ETSI-Code bleibt in der Build-Stufe.

**Zwei Bibliotheken, nicht eine.** `scod_tet.c` und `sdec_tet.c`
definieren beide `last_ener_pit`/`last_ener_cod`, und `sub_sc_d.c` greift
per `extern` darauf zu — im Encoder- wie im Decoder-Pfad. ETSI baut daraus
zwei getrennte Programme. In einer gemeinsamen `.so` teilen sich beide
Richtungen den Prädiktorzustand der Gain-Quantisierung; die Gegenprobe hat
gezeigt, dass dann *beide* Richtungen verfälscht werden, sobald sie
gleichzeitig laufen. Nicht wieder zusammenlegen. `codectest.py` hat einen
Test dagegen, der bei einer gemeinsamen `.so` fehlschlägt.

**Bit-Fummelei war harmlos.** Der ETSI-Code will 137 Wörter mit je 0 oder 1,
MSB zuerst (`int2bin` in `fbas_tet.c`). `split_traffic_frame()` liefert
MSB-first — die Abbildung ist 1:1, **ohne Umsortieren**.

**Messwerte:** Korrelation Original↔Decodiert **0.985**, Laufzeit des
Decoders **40 Samples = 5 ms** (die Zahl gehört in die Jitterbuffer-Rechnung
von Etappe 4). ETSI-Link lebt, MD5 `a8115fe68ef8f8cc466f4192572a1e3e`
stimmt, alle fünf Patches greifen sauber.

Nur Source-Codec, **kein Channel-Coding** — das macht die Basisstation.

**Was noch offen bleibt:** Der Test war selbstkonsistent — wir haben mit
unserem Encoder gesendet und mit unserem Decoder empfangen. Wäre unsere
Bit-Reihenfolge verdreht, fiele das dabei nicht auf. Die unabhängige
Gegenprobe steht noch aus, siehe unten.

## Dienste-Karte leer: zwei Seiten, zwei Pfade (2026-09-08)

Am VPS stand im System-Reiter „Kein Supervisor - die Bruecke laeuft ohne
manager.py", obwohl `manager.py` lief (seine Logzeilen tragen ja
`[svxlink-R3]`-Praefixe).

**Ursache:** Supervisor und Bruecke haben sich den Pfad **unabhaengig
voneinander** ausgerechnet.

- `manager.py` schreibt `services.json` nach `--svxlink-conf-dir`
  (Vorgabe `/data/svxlink`).
- `app/main.py` bildete `dirname(abspath(config)) + "/svxlink"`.

Solange die Config in `/data` liegt, passt das zufaellig. Ein relativer
Pfad in `SVXTETRA_CONFIG`, eine Config ausserhalb von `/data` oder ein
eigenes `--svxlink-conf-dir` - und beide suchen woanders.

**Das war nicht nur kosmetisch:** derselbe Pfad wird in `_apply()` zum
Schreiben der `svxlink.conf` benutzt. Bei einer Abweichung haette die
WebUI die Konfiguration in ein Verzeichnis geschrieben, das der Supervisor
nie liest - jede Aenderung waere beim naechsten Neustart der Instanz weg
gewesen, lautlos.

**Fix:** der Supervisor setzt `SVXTETRA_SVXLINK_DIR` auf sein eigenes
Verzeichnis, bevor er die Kinder startet; die Bruecke liest das ohnehin
schon aus. Damit koennen die beiden nicht mehr auseinanderlaufen.

**Dazu:** `/api/status` fuehrt jetzt `services_dir`, und die leere Karte
sagt, **wo** vergeblich gesucht wurde - statt nur, dass nichts da ist.

Gegenprobe gefahren: `manager.py --svxlink-conf-dir /data/anderswo`, und
die Bruecke meldet `services_dir: /data/anderswo` und findet den Dienst.
Vorher haette sie in `/data/svxlink` gesucht, das es gar nicht gibt.

## Deployment auf den VPS (2026-09-08)

**Port ist jetzt 3006** — 3003 ist am VPS belegt. Geaendert in `config.py`
(Vorgabe), `config.example.yaml`, `Dockerfile` (`EXPOSE`), README,
`webtest.py` und in `out/live.yaml`.

**`docker-compose.yml` war auf Etappe-2-Stand** (zwei Instanzen fuer den
Loopback-Test, `network_mode: host`). Neu:

- **kein `network_mode: host` mehr.** Der USRP-Verkehr laeuft seit dem
  Ein-Container-Modell im Container selbst; veroeffentlicht wird nur
  `127.0.0.1:3006`. Auf einem VPS ist das der Unterschied zwischen „hinter
  dem Reverse-Proxy" und „mit admin/admin im Internet".
- **`stop_grace_period: 30s`** — die Bruecke schliesst beim Beenden offene
  Gruppenrufe und leert die Sendequeue. Ohne Frist gaebe es wieder Geister.
- **Healthcheck** auf `/healthz`.

**Netz: `proxy_bridge`, extern** (Sebastians vorhandenes Proxy-Netz, liegt
seit Juni auf dem Host). Kein eigenes Compose-Netz. Der Reverse-Proxy
erreicht die Oberflaeche darin unter `svxtetra:3006`; die Bindung auf
`127.0.0.1:3006` bleibt zusaetzlich fuer `curl` und `webtest.py` am VPS.
Durchgetestet: Container haengt nur in `proxy_bridge`, aus einem zweiten
Container in diesem Netz antwortet `http://svxtetra:3006/healthz`.

**Falle beim Testen:** scheitert ein `compose up` (bei mir am belegten
Port), haengt der zurueckgebliebene Container in **gar keinem** Netz - und
sein Healthcheck meldet trotzdem `healthy`, weil der im Container selbst
gegen 127.0.0.1 laeuft. Also `compose down`, dann neu.

**Zeitzone: `TZ: Europe/Vienna` im Compose (2026-09-15).** Ohne das laeuft
der Container auf UTC. Betroffen sind alle Zeitstempel, die aus
`time.localtime()` kommen - **das Log** (`%(asctime)s`, sowohl in `main.py`
als auch im Ringpuffer der WebUI) und **das Last Heard** (`app/state.py`).
Der **Zeitplan** rechnet dagegen mit `timezone` aus der `config.yaml`, also
Wien. Beides auseinander heisst im Sommer zwei Stunden Versatz: ein Fenster
20-22 Uhr schaltet ausweislich des Logs um 18:00, und man sucht den Fehler
im Scheduler statt in der Uhr.

Die beiden Werte muessen **zusammenpassen** - wer `timezone` in der
`config.yaml` aendert, aendert `TZ` im Compose mit. Bei `docker run` statt
Compose gehoert `-e TZ=Europe/Vienna` dazu. tzdata liegt im Image, geprueft:

```
$ docker run --rm -e TZ=Europe/Vienna svxtetra python3 -c "import time;     print(time.strftime('%H:%M %Z'), time.strftime('%H:%M', time.gmtime()))"
10:26 CEST 08:26
```

**`.dockerignore` neu.** Vorher wanderte der komplette Baum in den
Build-Kontext, inklusive `out/` mit `live.yaml` (Zugangsdaten!), der 5-MB-
Rufzeichenliste und aufgenommenen `.wav`. Jetzt sind es **3,4 kB**.

**Der Weg ist durchgetestet:** aus einem leeren Verzeichnis heraus mit
`docker compose up -d` — Container startet, Healthcheck meldet `healthy`,
`/healthz` antwortet, Erststart legt `admin/admin` an. Anleitung im README
unter „Auf den VPS bringen".

## Bekannte Rufzeichen: Karte war immer leer (2026-09-08)

Die Karte hing allein am **Brew-Auskunftsdienst** (`state.subscribers`), und
der antwortet an dieser Anlage nie — `query_subscribers()` geht raus,
`SERVICE_SUBSCRIBER_PROFILES` kommt nicht zurueck, im Log null Treffer. Also
stand dort dauerhaft „Noch keine ISSI aufgeloest", obwohl im Last Heard
laengst Rufzeichen standen.

`AppState.known_stations()` setzt sie jetzt aus **allen** Quellen zusammen:
eigene Eintraege, Brew-Auskunft, und alles, was gehoert wurde. Links das
Rufzeichen mit der Herkunft darunter, rechts die ISSI samt Kuvert-Knopf:

```
OE4SXM                    2320998 [✉]
zuletzt gehört 23:40

SVXTetraBridge            2329999 [✉]
eigener Name
```

Der Knopf springt in den SDS-Reiter und setzt die ISSI ein; er erscheint
nur mit dem Recht `sds`. Kein `<table>`, sondern ein Grid mit zwei Spalten:
die linke darf schrumpfen und kuerzt lange Rufzeichen mit Auslassungs-
punkten, die ISSI bricht nie um.

**Wichtig dabei die Rangfolge:** was das Geraet selbst gemeldet hat
(Mnemonic aus dem Last Heard), schlaegt die heruntergeladene Liste. Sonst
stuende dort das falsche Rufzeichen — radioid.net vergibt DMR-IDs, und eine
TETRA-ISSI trifft die nur zufaellig. Beim ersten Bauen genau in diese Falle
getappt: 2320997 wurde als `OE7TEST997` angezeigt, obwohl sich das Geraet
als `OE4SXM` gemeldet hatte.

Live: `OE4SXM · 2320998 · gehoert · zuletzt 23:32` und
`SVXTetraBridge · 2329999 · eigener Name`.

**Nicht dauerhaft:** Last Heard liegt im Speicher, nach einem Neustart ist
die Karte wieder auf die eigenen Eintraege reduziert, bis wieder jemand
spricht.

## ⚠️ Geister-Rufe am brew-server (2026-09-08)

Sebastians Dashboard zeigte **13 aktive Rufe mit rund 173000 Sekunden
Dauer** — zwei Tage. Die Bruecke selbst meldete null offene Rufe. Es waren
also Leichen auf dem Server, und die meisten kamen **von uns** (Absender
2329999).

**Ursache: ein GROUP_TX ohne GROUP_IDLE bleibt beim brew-server ewig
stehen.** Er raeumt nichts von selbst weg. Drei Wege dorthin, alle
abgestellt:

1. **Die Bruecke schloss beim Beenden keine Rufe.** `main()` rief
   `stop_sources()` und `client.stop()` — ein laufender Gruppenruf Richtung
   TETRA blieb offen. Jetzt gibt es `Router.close_calls()`: GROUP_IDLE fuer
   jeden ausgehenden Ruf, sauberes Ende fuer jeden eingehenden.
2. **`UsrpSource.close()` brach den Nachlauf ab**, statt den Ruf zu
   beenden — `_stop_hang()` cancelte die Aufgabe, `_end()` lief nie.
   Jetzt wird ein laufender Ruf erst abgeschlossen.
3. **`client.stop()` schoss den Sender sofort ab.** Ein gerade eingereihtes
   GROUP_IDLE waere nie hinausgegangen. Neu: `client.flush()` wartet, bis
   die Sendequeue leer ist, und `main()` warnt, wenn das nicht klappt.

`livetest.py` schickt das GROUP_IDLE jetzt in einem `finally` — auch bei
Strg-C oder `docker stop`.

**Live nachgewiesen:** zwei Rufe offen (einer je Richtung), dann `docker
stop` mitten hinein:

```
main       Beende...
audio.src  [usrp-rx] R2: laufender Ruf wird beim Beenden abgeschlossen
routing    TX R2 zu Ende, 187 Frames / 11.2 s
routing    Call 26bbeea8 zu Ende (Bruecke faehrt herunter), 192 Frames
```

### Und darum nicht mehr `docker rm -f` fuer den Stack

`docker rm -f` ist **SIGKILL** — damit laeuft kein einziger der obigen
Handgriffe. Die alte Regel in dieser Datei ("immer mit `docker rm -f`
beenden") stammt aus der Zeit, als Testcontainer als Zombies liegenblieben,
und ist fuer den Stack falsch:

```bash
docker stop -t 15 svxstack && docker rm svxstack     # richtig, SIGTERM
docker rm -f svxstack                                # SIGKILL, hinterlaesst Geister
```

Fuer `livetest`-Container gilt dasselbe.

**Die 13 vorhandenen Geister lassen sich von aussen nicht schliessen** — ein
GROUP_IDLE braucht die volle UUID des Rufs, und die kennt nur noch der
Server (das Dashboard zeigt sie gekuerzt). Sie verschwinden erst, wenn der
brew-server neu startet. **Sinnvoll waere serverseitig ein Timeout:** ein
Gruppenruf ohne Traffic-Frames sollte nach ein paar Sekunden von selbst
enden, so wie es die Bruecke seit heute mit `CALL_TIMEOUT` auch macht.

## Zeitplan im Dialog, Reste weg (2026-09-08)

**Der Zeitplan sitzt jetzt in einem echten `<dialog>`.** In der Routenzeile
steht nur noch eine Zusammenfassung und ein Knopf:

```
täglich · 06:00–23:00 · hart aus            [Bearbeiten]
Mo, Mi · 20:00–22:00 · 5 min Nachlauf       [Bearbeiten]
immer aktiv                                  [Bearbeiten]
```

Die Bedienung selbst ist unveraendert, sie war nie kaputt — sie steckte nur
aufgeklappt in der Zeile, wo sie niemand gefunden hat.

- `zeitplanFormular(r, i)` ist aus der Zeile herausgeloest und wird an
  beiden Stellen verwendet; `zeitplanText(r)` baut die Zusammenfassung.
- `bindRouteInputs(root)` nimmt jetzt einen Wurzelknoten, weil dieselben
  Felder in der Liste **und** im Dialog stecken. `neuZeichnen(el)` schaut,
  wo das Feld haengt, und zeichnet nur das neu.
- Ein `<dialog>` statt eigenem Overlay: Esc schliesst, der Hintergrund
  kommt vom Browser, kein Fokus-Gefummel.

`out/svxlink/t1`, `t2`, `t3` sind geloescht (alte Testconfigs mit
`OE4SXM-HS`, keine Schluessel darin). **Nicht automatisch aufraeumen** —
ein Verzeichnis gehoert zu einem Reflektor, der auch nur voruebergehend
abgeschaltet sein kann, und darin liegt sein Zertifikat.

## ✅ TetraLinks eigenes Format: PID 0x82 (2026-09-08)

Eine SDS aus TetraLink kam bei der Bruecke **gar nicht an** — sie wurde
still verworfen. Mitgeschnitten, nachdem der Weg sichtbar gemacht wurde:

```
82 04 07 01 68 61 6c 6c 6f      ->  "hallo"
PID Typ Ref Kod  Text
```

- `82` = **SDS-TL Text Messaging**, nicht `02` (Simple Text). Unser Decoder
  kannte nur `02` und `09` und gab None zurueck.
- Der Aufbau danach ist **identisch zum vorhandenen 0x09-Zweig**:
  Typ/Flags, Message Reference, Kodierungsbyte, Text. Es reichte, `0x82`
  in denselben Zweig zu nehmen.
- Kodierung ist schlicht **8 Bit** — TetraLink verlangt also nichts
  Exotisches, wir haben nur die falsche PDU-Art geschickt.

**Neuer Sendemodus `sdstl`** baut genau diese PDU, Byte fuer Byte wie die
App (nur die Message Reference laeuft mit). Damit reden beide Seiten
dasselbe, ETSI-konforme Format — die rohen Modi `raw16`/`ascii` bleiben als
Notnagel, sind aber nicht mehr die erste Wahl.

### Warum es so lange unsichtbar war

Drei Stellen haben stillschweigend verworfen. Alle drei sind jetzt laut:

1. `on_sds()` verlangte einen vorangehenden `SHORT_TRANSFER` und verwarf die
   SDS sonst mit `log.debug` — im Betrieb also unsichtbar. Jetzt: Warnung
   mit Rohbytes, und die Nachricht landet **trotzdem** im Postfach, nur
   ohne Absender.
2. Eine PDU, die `decode_text()` nicht versteht, verschwand ebenfalls
   wortlos. Jetzt: Warnung mit PID und Rohbytes, Eintrag `(nicht lesbar)`
   im Postfach — genau daran liess sich PID 0x82 ablesen.
3. `BrewClient._dispatch()` ignorierte unbekannte Frame-Typen und
   Nachrichtenklassen komplett und meldete Parse-Fehler nur auf DEBUG.
   Jetzt Warnungen, entprellt auf eine je zehn Sekunden, damit eine
   Stoerung im Audiotakt das Log nicht flutet.

**Lehre fuers Weiterbauen:** kein Pfad darf eine Nachricht ohne Logzeile
fallen lassen. Der Fehler war zwei Zeilen gross, die Suche kostete einen
Abend.

## ✅ SDS-Zeichensalat aufgeklaert (2026-09-08)

**TetraLink ueberspringt genau ein Byte — den PID — und liest alles Weitere
als UTF-16BE. Unser Kodierungsbyte frisst es als Textzeichen mit.**

Nachgewiesen, nicht vermutet: dreimal `test` an TetraLink geschickt, jedes
Mal mit anderer Kodierung, und die drei Eintraege in seiner Inbox
zeichengenau nachgerechnet.

| gesendet | PDU | TetraLink zeigt | nachgerechnet |
|---|---|---|---|
| 8bit | `02 01 74 65 73 74` | `Ŵ斁?` | U+0174 U+6573 + Restbyte |
| 7bit | `02 00 e9 97 9f 40` | `é鞟?` | U+00E9 U+979F + Restbyte |
| ucs2 | `02 1a 00 74 00 65 00 73 00 74` | `〃琀攀猀?` | U+1A00 U+7400 U+6500 U+7300 |

`Ŵ`, `é鞟` und `琀攀猀` stimmen exakt mit dem ueberein, was herauskommt,
wenn man ab Byte 1 als UTF-16BE liest. Das Restbyte am Ende erklaert das
Ersatzzeichen.

**Konsequenz: keine ETSI-Kodierung kann das reparieren**, weil das
Deklarationsbyte selbst im Text landet und alles um eine Bytegrenze
verschiebt.

### Zwei rohe Betriebsarten dazugebaut

`app/brew/sds.py` kennt jetzt fuenf Betriebsarten statt drei. Die ersten
drei sind ETSI (PID, Kodierungsbyte, Text), die beiden neuen lassen das
Kodierungsbyte **weg**:

| Modus | PDU fuer `test` | was TetraLink daraus macht |
|---|---|---|
| 7bit | `02 00 e9 97 9f 40` | `é鞟?` |
| 8bit | `02 01 74 65 73 74` | `Ŵ敳?` |
| ucs2 | `02 1a 00 74 00 65 00 73 00 74` | `ᨀ琀攀猀?` |
| **raw16** | `02 00 74 00 65 00 73 00 74` | **`test`** |
| ascii | `02 74 65 73 74` | `瑥獴` |

- `build_text_pdu(text, mode)` baut alle fuenf, `mode_from_name()` nimmt
  auch `16bit` und `tetralink` als Zweitnamen fuer `raw16`.
- **Empfangen erkennt sie von selbst** (`_als_rohtext`): fehlt das
  Kodierungsbyte, wird das an der Bytefolge erkannt und richtig gelesen.
  Falsch-positiv geht nicht — unsere eigenen ETSI-Formate haben an der
  Stelle 0x00, 0x01 oder 0x1a und fallen durch beide Pruefungen. Im
  `selftest.py` festgenagelt, in beide Richtungen.
- `SdsText.mode` sagt jetzt, **wie** gelesen wurde; das steht auch im
  Postfach unter jeder Nachricht.

**Nicht ETSI-konform, und das ist Absicht.** `raw16` ist das Format, das
TetraLink liest — ein echtes Funkgeraet wird es vermutlich anders sehen.
Darum umschaltbar und nicht als Vorgabe.

**Am 2026-09-08 von Sebastian bestaetigt: `raw16 test` kam lesbar an.**
`sds_control.coding_scheme` steht seither auf `raw16`, damit auch die
Antworten der TG-Steuerung ankommen — geprueft: Kommando `?` rein, Antwort
`+2321>2321 +9>23211 +2323>2323` als `02 00 2b 00 32 ...` raus. Umstellen
ist ein Klick im SDS-Reiter, sobald ein echtes Geraet dazukommt.

**Offen bleibt:** wie **TetraLink selbst** sendet. Eine SDS aus der App an
2329999 zeigt es — die Rohbytes stehen dann im Verlauf. Erst danach ist
klar, ob `raw16` wirklich ihr Format ist oder nur zufaellig lesbar.

## 👉 HIER GEHT ES WEITER (Stand 2026-09-06, Abend)

> **Getestet wird mit der TetraLink-App, nicht mit einem Funkgeraet.**
> Sebastian hat weiterhin keine Hardware und sagt Bescheid, sobald eine da
> ist. Das ist kein Nachteil: TetraLink ist eine *fremde* Implementierung
> und damit genau die Gegenprobe, die den eigenen Tests bisher gefehlt hat.


**Mapping-Tabelle, wie sie jetzt steht** (die Zeilen heissen wie ihr
Reflektor):

| Name | GSSI | Reflektor | Node | TG | Typ |
|---|---|---|---|---|---|
| R1 | 9 | R1 | OE1SXM-PL | dynamisch, Rueckfall 23211 | dynamic |
| R2 | 2321 | R2 | OE1SXM-P60 | 2321 | fixed |
| R3 | 2323 | R3 | OE1SXM-R | 2323 | fixed |

### Der offene Punkt: wer spricht da eigentlich?

Sebastian will am TETRA-Display sehen, **wer** gerade redet (bzw. aus
welcher TG). Zwei Hebel wurden untersucht, beide **gemessen**, nichts
davon gebaut:

**1. Quell-ISSI faelschen — geht.** Der brew-server reicht eine fremde
Quell-ISSI unveraendert durch: Sender registriert als 2320998, gesendet
mit `source=2321`, der Empfaenger sah `GSSI 2324 von ISSI 2321`.
`client.group_tx()` hat das Feld schon, `routing.py` setzt es nur nicht.
Damit stuende am Geraet die **TG** statt der Bruecken-ISSI 2329999.

**2. Talker-Alias — Sackgasse, so wie es ist.** Das Mnemonic-Feld
(SS-TPI, 34 Byte) traegt heute **nicht** das Rufzeichen der sendenden
Station, sondern das **eigene `CALL` der empfangenden Instanz**.

Der Beweis, und er war nicht billig: R1 testweise auf `OE4SXM-PL`
umbenannt (`CALL` wurde damit `OE4SXM`), R3 (`OE1SXM-R`) sendete auf
TG 2323, R1 hoerte mit.

```
R1s svxlink-Log:   Talker start on TG #2323: OE1SXM-R    <- svxlink WEISS es
bei der Bruecke:   TX R1/TG 2323 -> GSSI 9 (OE4SXM)      <- R1s eigenes CALL
```

svxlink kennt den Sprecher also, gibt ihn ueber die USRP-Schnittstelle
aber nicht weiter. **R1 wurde danach auf `OE1SXM-PL` zurueckgestellt.**
Wichtig fuer eine Wiederholung: alle Nodes heissen `OE1SXM-*` und werden
auf sechs Zeichen gekuerzt — ohne ein Rufzeichen mit anderem Praefix ist
der Test nicht aussagekraeftig.

### Was als naechstes ansteht — in dieser Reihenfolge

1. **Sebastian schaut in TetraLink**, ob ueberhaupt ein Alias angezeigt
   wird. Zeigt die App nur die ISSI, ist Weg 2 sinnlos und nur Weg 1
   bleibt — das spart den ganzen svxlink-Patch.
2. **Quelltext lesen** (`UsrpLogic.cpp`, `ReflectorLogic`), ob das
   Sprecher-Rufzeichen an der Stelle erreichbar ist. Zehn Minuten, kein
   Build. Er hat dazu noch nicht ja gesagt.
3. Danach entscheiden: Quell-ISSI = TG (Schalter in der WebUI, SDS davon
   ausnehmen, Namensaufloesung fuer solche Rufe abschalten) oder
   `UsrpLogic` patchen — svxlink wird ohnehin selbst gebaut, und das
   Metadatenpaket hat das Feld bereits, es wird nur falsch befuellt.

### Ebenfalls offen, kleiner

- **SDS-Zeichensalat ist aufgeklaert** (eigener Abschnitt oben): TetraLink
  ueberspringt ein Byte. `raw16` ist gebaut und sollte lesbar sein — von
  Sebastian noch zu bestaetigen. **Weiterhin offen: eine SDS aus TetraLink
  an die Bruecke schicken**, deren Rohbytes zeigen, was die App selbst
  sendet.
- Ob der **Nachlauf von 500 ms** sich beim Sprechen richtig anfuehlt.
- ~~Zeitplan als Popup~~ und ~~Reste in `out/svxlink`~~ — beides am
  2026-09-08 erledigt, siehe eigener Abschnitt.

## Rollentausch R1/R2 und ein gefaehrlicher Fund (2026-09-06, spaet)

### Neue Aufteilung

| GSSI | Reflektor | TG | Typ |
|---|---|---|---|
| 9 | **R1** (OE1SXM-PL) | dynamisch | Rueckfall 23211 |
| 2321 | R2 (OE1SXM-P60) | 2321 | statisch |
| 2323 | R3 (OE1SXM-R) | 2323 | statisch |

Die dynamische Zeile liegt jetzt auf R1, die beiden anderen sind statisch
mit je genau einer TG.

### Sprechen auf der dynamischen GSSI - was passiert

Gemessen, nicht hergeleitet. Dynamische Zeile (GSSI 9) auf TG 2321,
Aussendung von ISSI 2320998 auf GSSI 9. Ein dritter Zugang hoerte auf
GSSI 2321 mit:

```
RX GROUP_TX  GSSI 9     von ISSI 2320998  'OE4SXM'    <- das Original
RX GROUP_TX  GSSI 2321  von ISSI 2329999  'OE1SXM'    <- die Bruecke gibt es
                                                          auf 2321 wieder aus
```

Der Weg: GSSI 9 -> R1 sendet auf TG 2321 -> R2 hoert TG 2321 mit -> die
Bruecke macht daraus einen Gruppenruf auf GSSI 2321. **Wer auf der
dynamischen Zeile spricht, ist also auf der statischen GSSI derselben TG
zu hoeren.** Das ist gewollt, muss man aber wissen: die beiden GSSIs sind
dann effektiv zusammengeschaltet.

### Der Fund: ein abgebrochener Ruf sperrte den Reflektor dauerhaft

Beim Nachstellen fiel `dropped_floor: 1` auf - ein Durchgang wurde
verworfen, obwohl nichts lief. Ursache: ein frueherer Testsender war
**mitten in der Aussendung** abgeschossen worden. Das GROUP_IDLE kam nie,
der Ruf blieb offen, und `_claim_floor` hielt den Reflektor **fuer immer**
belegt. Im Betrieb reicht dafuer ein Geraet, das aus der Versorgung
faellt: der Reflektor waere bis zum Neustart der Bruecke tot.

Fuer die Gegenrichtung gab es diesen Wachhund laengst
(`SILENCE_TIMEOUT` in `UsrpSource`), fuer die TETRA-Seite nicht.

Jetzt: `InboundCall.last_frame` wird bei jedem Frame nachgezogen, und der
Ticker beendet Rufe, die laenger als `CALL_TIMEOUT` (5 s) still sind -
mit Warnung im Log und Zaehler `dropped_stale`. TETRA liefert alle 60 ms
einen Frame, fuenf Sekunden Stille sind also eindeutig. Im `smoketest.py`
festgenagelt (Ruf ohne GROUP_IDLE -> Wachhund raeumt ihn weg -> Floor
frei).

### Wieviele TGs pro Instanz?

**Senden geht immer nur auf der gerade ausgewaehlten TG** - das ist der
Grund, warum es die DTMF-Umschaltung ueberhaupt braucht. Eine Instanz mit
mehreren Routen kann also nicht auf mehreren TGs gleichzeitig senden; ein
Durchgang auf einer GSSI, deren TG gerade nicht ausgewaehlt ist, ginge auf
der falschen TG hinaus. Dazu kommt der Floor: eine Instanz traegt zu jedem
Zeitpunkt genau einen Durchgang, egal wieviele TGs sie mithoert.

Darum: **eine TG je Instanz**, und die dynamische Zeile bekommt eine
eigene Instanz. Mehr mitgehoerte TGs auf einer Instanz bringen nur beim
reinen Zuhoeren etwas - und ob svxlink dabei die Auswahl selbsttaetig
umschaltet (und damit der Bruecke die Sende-TG unter den Fuessen
wegzieht), ist **nicht geprueft**.

### Nebenbei

`livetest.py rx` schneidet immer nur **einen** Ruf gleichzeitig mit. Bei
zwei parallelen Gruppenrufen steht der zweite im Log, aber nicht in einer
`.wav`. Fuer die Beweisfuehrung reicht das Log; wenn es stoert, muesste
`rx` je Identifier eine eigene Datei fuehren.

## ✅ Drei Instanzen, drei gleichzeitige QSOs (2026-09-06)

R3 scharf geschaltet: `OE1SXM-R`, TG 2323 statisch, Route GSSI 2323,
Ports 41236/41246. `Authentication OK` beim ersten Versuch, der Reflektor
fuehrt ihn mit `monitoredTGs [2323]`.

**Drei gleichzeitige Rufe, aus der laufenden Anlage abgefragt:**

```
tetra->svx  R1  GSSI 2321  TG 2321   OE4SXM
tetra->svx  R3  GSSI 2323  TG 2323   OE1SXM
svx->tetra  R2  GSSI 9     TG 2321   OE1SXM
```

Der Trick beim Testen: es braucht nur **zwei** sendende Zugaenge. Zwei
schicken von TETRA herein (R1, R3), und R2 haengt am Reflektor auf TG 2321
und spielt den Durchgang von R1 nach TETRA zurueck - damit ist die dritte
Instanz in der Gegenrichtung belegt.

**`active_calls` nennt jetzt Reflektor und TG.** Vorher stand dort nur die
GSSI; die Richtung `svx->tetra` hatte gar keine TG und die Richtung
`tetra->svx` keinen Reflektor. Mit einer Instanz war das zu verschmerzen,
mit dreien ist es die entscheidende Information. Faellt in `codectest.py`
auf, wenn es wieder verschwindet.

**`webtest.py` haengt nicht mehr an festen Zahlen.** "zwei Reflektoren",
"drei Dienste" ist genau das, was beim Ausbau kaputtgeht. Geprueft wird
jetzt der Zusammenhang: je aktivem Reflektor eine laufende Instanz, und
fuer abgeschaltete keine.

### Achtung: ueberlappende TGs

Der Reflektor fuehrt gerade:

| Node | Instanz | monitoredTGs |
|---|---|---|
| OE1SXM-PL | R1 | 2321, 23211 |
| OE1SXM-P60 | R2 | dynamisch, Vorgabe **23211** |
| OE1SXM-R | R3 | 2323 |

**R1 und R2 sitzen beide auf 23211**, sobald die dynamische Zeile auf ihre
Vorgabe zurueckfaellt (nach `dynamic_timeout_minutes`, aktuell 10). Ein
Durchgang von TETRA auf GSSI 23211 geht dann ueber R1 hinaus, R2 hoert ihn
mit und spielt ihn als GSSI 9 nach TETRA zurueck - ein Echo. Sauber waere
eine Vorgabe-TG fuer R2, die sonst niemand mithoert.

## Nachlauf, SDS-Kodierung, R3 (2026-09-06)

### Nachlauf am Rufende (`audio.hang_ms`, Standard 500 ms)

svxlink schickt sein Abschaltpaket in dem Moment, in dem PTT losgelassen
wird - der TETRA-Ruf endete bis jetzt hart, das letzte Wort fiel weg.
`UsrpSource` haelt den Ruf jetzt noch `hang_ms` offen und schiebt Stille
nach. Kommt in dieser Zeit wieder Audio, haengt sich der Durchgang an den
laufenden Ruf an, statt neu aufzubauen - kein zweiter Rufaufbau, keine
Luecke am Anfang.

- Einstellbar im System-Reiter (0 bis 3000 ms), wirkt **ohne Neustart**:
  die Quelle fragt den Wert bei jedem Rufende neu ab
  (`hang_ms=lambda: self.cfg.audio.hang_ms`), sonst haette ein Reload
  nichts geaendert.
- Der Wachhund (`SILENCE_TIMEOUT`) haelt sich waehrend des Nachlaufs
  zurueck, sonst wuerde der Ruf zweimal beendet.
- Geprueft in `selftest.py` (ohne Nachlauf endet sofort / mit Nachlauf
  kommt Stille / zweiter Durchgang baut nicht neu auf) und in
  `codectest.py` durch den ganzen Router.

### SDS kam in TetraLink als Zeichensalat an

Gesendet `123456 Test`, in **TetraLink** standen sechs CJK-Zeichen. Erklaerung
steht in den Bytes: wir senden `02 01 31 32 33 34 35 36 20 54 65 73 74`
(PID 0x02, Kodierung 0x01 = 8 Bit), das Geraet liest die Nutzlast aber
**paarweise als UCS-2** - aus elf ASCII-Zeichen werden sechs CJK-Glyphen,
genau die Anzahl am Bild.

Darum:

- **Kodierung ist in der WebUI umstellbar** (SDS-Reiter: 8-Bit / UCS-2 /
  7-Bit, `POST /api/sds/scheme` schreibt `sds_control.coding_scheme`).
  Sie gilt auch fuer die Antworten der TG-Steuerung.
- **Empfangen wird robust:** deklariert ein Geraet 8 Bit, sieht die
  Nutzlast aber nach UCS-2 aus (jedes zweite Byte 0x00, dazwischen
  lesbares ASCII), wird sie als UCS-2 gelesen - `sieht_nach_ucs2_aus()`.
- **Jede Nachricht traegt ihre Diagnose:** deklarierte Kodierung und die
  ersten 64 Rohbytes stehen im Postfach und unter der Nachricht in der
  Oberflaeche. Ohne das laesst sich so etwas nicht aufklaeren.

Welche Kodierung TetraLink wirklich will, zeigt seine **Antwort** - die
Rohbytes dazu stehen dann im Verlauf.

### R3 angelegt

Dritter Reflektor in `out/live.yaml`: Ports 41236/41246. Zuerst
deaktiviert angelegt, am selben Abend mit `OE1SXM-R` scharf geschaltet
(siehe oben). `confgen` und `manager` ueberspringen
deaktivierte Instanzen, es wird also nichts erzeugt und nichts gestartet.
Drei gleichzeitige Gespraeche sind auch die Obergrenze der Luftschnitt-
stelle (drei Verkehrs-Zeitschlitze je Traeger).

### `webtest.py` war nicht harmlos

Zwei Dinge, die auf einer echten Anlage wehtun und jetzt behoben sind:
der Test hat **das SDS-Postfach geleert** (echte Post weg) und beim
Pruefen der eigenen Namen ausgerechnet **2329999** angelegt und wieder
geloescht - also den Eintrag des Betreibers. Jetzt: Wegwerf-ISSI 9999998,
und das Leeren wird im `selftest.py` auf einer Wegwerfdatei geprueft. Der
Test darf gegen die laufende Anlage laufen; nur eine echte SDS geht raus.

## ✅ Zwei gleichzeitige QSOs - und der Fehler, der das verhindert haette (2026-09-06)

**Zwei parallele Gespraeche gehen, eines je Reflektor-Instanz.** Live
belegt: zwei Aussendungen gleichzeitig, R1 von OE4SXM auf TG 2321, R2 von
OE1SXM auf TG 23211, beide acht Sekunden, gleicher Pegel (1594), kein
Uebersprechen.

Der Floor-Lock sitzt **je Reflektor** und gilt fuer **beide Richtungen** -
ein Reflektor traegt also zu jedem Zeitpunkt genau eine Aussendung. Zwei
Zeilen am selben Reflektor (2321 und 23211 auf R1) koennen darum *nicht*
gleichzeitig; da entscheidet die Prioritaet, die andere faellt raus.

**Der Codec haette das kaputtgemacht.** Der ETSI-Code haelt seinen Zustand
in globalen Variablen, und `dlopen` gibt fuer denselben Pfad denselben
Handle zurueck - zwei `AcelpCodec`-Objekte teilten sich also den Zustand.
Gemessen mit zwei verschraenkten Stroemen: **Korrelation 0.366 statt
1.000**, beide Richtungen verzerrt.

Zwei Aenderungen:

1. `AcelpCodec` kopiert die `.so` beim Laden in ein temporaeres
   Verzeichnis und laedt die Kopie (andere Inode -> eigener Ladevorgang ->
   eigene globale Variablen). Die Kopie wird sofort nach dem Laden
   geloescht, die Abbildung im Speicher bleibt. `private=False` gibt es
   noch fuer die Gegenprobe im Test.
2. `Router._tx_codec` war **einer fuer alle Reflektoren** - jetzt einer je
   Reflektor (`_tx_codecs`). `UsrpSource` laedt gar keinen mehr, es prueft
   nur noch `acelp.available()`; encodiert wird im Router.

Danach: Korrelation **1.000**, Encoder bit-identisch. `codectest.py` haelt
das fest, samt Gegenprobe mit geteilter Bibliothek - sonst wuerde der Test
nichts pruefen.

**Fuer mehr als zwei** braucht es nur einen weiteren Eintrag unter
`reflectors` (eigenes Node-Rufzeichen am Reflektor, eigener AUTH_KEY,
eigene `usrp_port`/`usrp_listen_port`) - im Code ist nichts auf zwei
festgenagelt. Kosten je Instanz: rund 19 MB RSS fuer svxlink, dazu 2x56 KB
Codec-Kopie. Der ganze Stack liegt im Leerlauf bei 66 MB und 0,4 % CPU.
**Aber:** die Instanzen duerfen sich nicht dieselben TGs anschauen, sonst
spielt R1 dem R2 den eigenen Verkehr zurueck (genau das war beim
DTMF-Test zu sehen). Eine sendende TG je Instanz, disjunkt.

## ✅ Rufzeichenliste (2026-09-06)

ISSI -> Rufzeichen, mit **Aktualisieren**-Knopf im System-Reiter, dazu
eine Liste eigener Namen.

- `app/idlist.py` - **SQLite**, nicht dict: die Liste von radioid.net hat
  324610 Eintraege. Auf Platte 5 MB, im Speicher nichts, Abfrage per Index.
  sqlite3 ist Standardbibliothek.
- Rangfolge in `AppState.callsign(issi, mnemonic)`: **eigener Eintrag** ->
  **Mnemonic des Geraets** -> **Brew-Auskunft** -> **heruntergeladene
  Liste**. Der eigene Eintrag schlaegt bewusst alles: wer ihn anlegt, weiss
  es besser als jede Datenbank.
- API: `GET /api/idlist`, `POST /api/idlist/refresh`,
  `POST /api/idlist/manual`, `DELETE /api/idlist/manual/{issi}` - alles im
  Bereich `system`.
- Der SDS-Verlauf loest die Namen **beim Abruf** auf, nicht beim Ablegen:
  aendert sich die Liste, zeigt auch alte Post den neuen Namen.

**Beim ersten Live-Versuch kamen 1037 statt 324610 Eintraege.** Grund:
`content.read(n)` liefert, was gerade im Puffer liegt, **nicht** n Bytes.
Jetzt `iter_chunked` plus Abgleich mit `Content-Length` - eine abgebrochene
Uebertragung wird abgelehnt statt eingelesen. Der Download dauert 4,3 s.

**Vorsicht bei den Nummern:** radioid.net vergibt **DMR**-IDs. Wo eine
TETRA-ISSI nicht derselben Nummer entspricht, steht dann ein falsches
Rufzeichen da (2320998 zeigt z.B. OE7CPI, obwohl unser Testsender OE4SXM
heisst). Deshalb steht die Mnemonic des Geraets in der Rangfolge **vor**
der Liste, und fuer alles andere gibt es die eigenen Eintraege.

## Oberflaeche: Name, Kopfzeile, Reiterleiste (2026-09-06)

- Heisst jetzt **SVXTetraBridge**, Kopfzeile darunter **by OE4SXM**
  (`NAME`/`BY` in `app/web/server.py`, Templates ziehen es von dort).
  Der angemeldete Benutzer steht nicht mehr im Kopf.
- **Reiterleiste bleibt einzeilig.** Die Beschriftungen kamen bisher aus
  `AREAS` - das sind aber die Texte der Rechte-Matrix
  ("Routen und Zeitplan", "System, Pegel, Dienste"), viel zu lang fuer
  fuenf Reiter. Dafuer gibt es jetzt `TAB_TITLES` (Status, Routen, SDS,
  Log, System); in der Rechte-Matrix stehen weiterhin die langen Namen.
  Dazu `text-overflow: ellipsis` auf dem Text, damit auf schmalen Geraeten
  gekuerzt statt umgebrochen wird.
- Reiter ohne Recht werden nicht angezeigt - das galt schon, ist jetzt aber
  in `webtest.py` festgenagelt (eingeschraenkter Benutzer sieht genau
  `status` und `log`, kein `tab-sds`, kein `tab-system`).

**`?v=` an CSS und JS** (`_asset_ver()`, Zeitstempel der Dateien). Ohne das
haelt der Browser eine geaenderte Oberflaeche fuer die alte: das
Vierer-Raster der Reiterleiste war serverseitig laengst weg und im Browser
trotzdem noch da. Bei jeder Aenderung an `style.css`/`app.js` aendert sich
die Nummer von selbst.

## ✅ SDS-Reiter in der WebUI (2026-09-06)

Fuenfter Reiter **SDS**: Verlauf lesen, Kurznachricht schicken, Verlauf
leeren. Eigener Rechtebereich `sds`, also getrennt vergebbar.

**Warum es das vorher nicht gab, obwohl SDS laengst lief:** `on_sds()` hat
alles verworfen, was nicht an die Control-ISSI ging. Jetzt wandert **jede**
lesbare Text-SDS zuerst ins Postfach und wird erst danach als Kommando
geprueft (`kind`: `text` / `command` / `reply`). Am Kommandoweg aendert das
nichts, der Smoketest deckt beide Faelle ab.

- `app/sdsbox.py` — Ringpuffer (300) plus `sds.json` neben der Config, damit
  ein Neustart das Postfach nicht leert. `since` wie beim Log, die
  Oberflaeche holt nur das Neue nach.
- `Router.send_text_sds()` — prueft Ziel und Text, wirft `ValueError`
  (400) bzw. `RuntimeError` (503, keine Verbindung). Ein Fehlversuch ohne
  Verbindung wird als `ok: false` mitprotokolliert, sonst waere die
  Nachricht spurlos weg.
- API: `GET /api/sds?since=`, `POST /api/sds`, `DELETE /api/sds`.

**SDS geht nur an eine ISSI, nicht an eine Gruppe.** Brews
`SHORT_TRANSFER` traegt nur `source`/`destination` ohne Gruppenkennung -
Group-SDS ist im Protokoll nicht vorgesehen. Das Ziel-Feld heisst deshalb
ausdruecklich Ziel-ISSI.

**In dieser Installation ist `control_issi` = eigene ISSI (2329999)**,
also gilt *jede* eingehende SDS als Kommando und bekommt eine Antwort. Wer
reinen Textverkehr will, gibt der Control-Funktion eine eigene ISSI.

**Live geprueft:** Kommando `2321` von ISSI 2320998 rein, Antwort
`GSSI 9 -> TG 2321 aktiv` beim Absender angekommen, beides im Postfach;
drei Nachrichten aus der WebUI raus; nach `docker restart` alle fuenf noch
da.

**Nebenbei ein Anzeigefehler behoben:** `.tabs` stand auf
`repeat(4, 1fr)`. Mit fuenf Reitern waere der fuenfte umgebrochen - und wer
nur Teilrechte hatte, sah seine zwei Reiter in der linken Haelfte kleben.
Jetzt `grid-auto-flow: column`, damit passt jede Anzahl.

## ✅ Kompletter Durchgang geprüft (2026-09-06)

62 Prüfungen gegen die laufende Instanz (`scratchpad/vollcheck.py`):
Anmeldung, Rechteschranken, alle Statusfelder, Routen anlegen/ändern/löschen
samt Zeitplan und Eingabeprüfung, Handbetrieb, Reflektoren, Audiopegel mit
Grenzwerten, `config.yaml` inkl. kaputtem YAML, Backup und Restore, Dienste,
Log-Filter, Benutzerverwaltung mit echter Rechteprüfung. **Alles ok.**

**Zwei Fehler dabei gefunden und behoben:**

1. **Ein unbekannter Dienstname wurde stillschweigend angenommen.**
   `/api/service/gibtsnicht/restart` schrieb das Kommando in `services.cmd`,
   der Supervisor fand nichts und tat nichts — die UI meldete Erfolg. Jetzt
   wird gegen `manager_status()` geprüft und mit 404 plus der Liste der
   bekannten Namen abgelehnt.
2. **Die eigene Sitzung überlebte die eigene Passwortänderung.** Wer sein
   Passwort änderte, blieb mit dem alten Cookie angemeldet — und ein
   mitgelesenes Cookie wäre weiter gültig gewesen. `/api/password` ruft jetzt
   `sessions.drop_user()`, die UI schickt einen auf `/login`.

### Übersteuerung wird jetzt erkannt

`level_info()` in `app/audio/base.py` liefert neben dem mittleren Betrag den
**Anteil begrenzter Samples**. Über 0,1 % schreiben `UsrpSink` und
`UsrpSource` am Ende jedes Rufs eine Warnung ins Log, mit der Stellschraube
dazu (`gain_svx_db`/`NET_PREAMP` bzw. `gain_tetra_db`/`PREAMP`). Der Grund:
zu hoher Pegel macht Audio nicht lauter, sondern kaputt, und ohne Messung
merkt man es erst, wenn jemand klagt.

### Pegel neu eingemessen: NET_PREAMP 8 / PREAMP 8

Der vorherige Stand (`net_preamp_db: 24`, `preamp_db: 10`) war live nachweisbar
übersteuert:

| | 24 / 10 dB | **8 / 8 dB** |
|---|---|---|
| Kreis | +4,1 dB | **+1,3 dB** |
| Spitze hin / retour | 100 % / 100 % | **49 % / 51 %** |
| Anschlag in der Brücke | **5,32 %** | **0,00 %** |
| Pegel von svxlink (Mittel) | 13415 | 3789 |

Bei 34 dB Gesamtanhebung lief die Kette in beide Richtungen an den Anschlag;
ACELP hat das danach wieder weichgezeichnet, weshalb es in der `.wav` nur mit
0,21 % auffiel — verzerrt war es trotzdem schon. Mit 8/8 kommt ein Signal
praktisch pegelgleich zurück und hat rund 6 dB Reserve für Sprachspitzen.
Gemessen mit Testton; wenn es mit echter Sprache dumpf klingt, lieber an
`PREAMP` drehen als an den Brücken-Reglern (Limiter, siehe Tabelle oben).

### Sitzungen laufen mit

Angemeldet bleibt man 12 Stunden, und die Frist **verlaengert sich beim
Benutzen** (`SessionStore.renew()`, hoechstens einmal je Stunde, damit
nicht jeder Klick ein Cookie schreibt). Vorher war es eine harte Frist ab
der Anmeldung - man flog mitten in der Arbeit raus. Cookie und
Server-Sitzung werden gemeinsam verlaengert, sonst wirft der Browser das
Cookie weg, waehrend der Server die Sitzung noch kennt.

Die Sitzungen liegen weiterhin **im Speicher**: ein Neustart der Bruecke
heisst neu anmelden. Das Speichern der Pegel loest das *nicht* aus, da
starten nur die svxlink-Instanzen neu.

### Kleinkram

`manager.py` reicht `Could not find audio clip` nicht mehr an die Konsole
durch (Konstante `LEISE`). svxlink schreibt das bei jedem TG-Wechsel je
Ziffer; unsere Logics haben `RX=NONE`/`TX=NONE`, eine Ansage könnte gar
nirgends hinaus. In `svxlink.log` steht es weiterhin.

## 👉 Stand 2026-09-04, Abend

Etappen 1 bis 5 laufen, Etappe 6 ist inhaltlich mit erledigt (Zeitplan ist
in der WebUI bedienbar). Alle Container gestoppt, am Reflektor hängen nur
noch fremde Nodes.

**Zum Weitermachen:**

```bash
docker build -t svxtetra .
docker run -d --name svxstack -p 3006:3006 -v "<pfad>/out:/data" \
    svxtetra python3 manager.py /data/live.yaml
```

Dann `http://localhost:3006`, Anmeldung **admin / admin**.
`out/users.yaml` löschen setzt darauf zurück.

**Container mit `docker stop` beenden, nie mit `timeout` und nicht mit
`docker rm -f`.** `timeout` beendet nur den Client, der Container laeuft
weiter und haelt die Node-Sitzung am Reflektor. `docker rm -f` ist SIGKILL
und hinterlaesst offene Gruppenrufe am brew-server (siehe Abschnitt
„Geister-Rufe"). Richtig: `docker stop -t 15 svxstack && docker rm svxstack`.
Vor jedem Test prüfen:

```bash
docker ps
curl -s http://reflector.oe1sxm.at:8888/status | python3 -m json.tool
```

**Offene Punkte:**

- ~~Update-Funktion im System-Reiter~~ - am 2026-09-08 gestrichen
- Die Optik der WebUI ist nie mit Augen geprüft worden, nur das DOM
- Pegel sind mit synthetischem Testton eingemessen, nicht mit Sprache
- Etappe 7 (ISSI-Durchreichung) braucht `tetra-contrib`, und das enthält
  kein UsrpLogic — die Branches müssten zusammengeführt werden

## 👉 Hier geht's weiter

## ✅ Etappe 4 läuft live (2026-09-04)

Kompletter Kreis über den echten Reflektor nachgewiesen:

```
RX GSSI 2321 -> R1/TG 2321 von ISSI 2320998 (OE4SXM)   TETRA rein
[usrp] R1: Call von OE4SXM auf TG 2321                  -> svxlink -> Reflektor
TX R2/TG 2321 -> GSSI 9 (OE1SXM)                        Reflektor -> zurück nach TETRA
TX R2 zu Ende, 130 Frames / 7.8 s
```

Am Reflektor stand der Node dabei 8 Sekunden als `isTalker`, exakt die
Dauer der Aussendung. Umlaufzeit des ganzen Kreises: **368 ms**.

**Vier Dinge mussten dafür stimmen, alle nur live auffindbar:**

1. **`logic_type: ReflectorV2`** — Sebastians Reflektor spricht V2.
2. **Metadatenpaket muss 352 Byte lang sein.** `UsrpTlvMetaMsg` endet auf
   `std::array<uint8_t, 306>`; ein kürzeres Paket lässt svxlinks `unpack()`
   lautlos scheitern. Im Log steht dann `Talker stop: unknown TG# 0`,
   obwohl das Audio ankommt.
3. **TG gehört in die TLV-Metadaten**, nicht nur in den USRP-Header.
   `InboundCall.tg` trägt sie bis in die `UsrpSink`.
4. **`MUTE_FIRST_TX_LOC=0`** — sonst schaltet svxlink die *erste*
   Aussendung nach einem TG-Wechsel stumm, und der erste Ruf geht verloren.

**Offen, und mit dem Ohr zu entscheiden:** Der Rückweg kommt rund 12 dB
leiser an (mittlerer Betrag 1768 → 461). An `PREAMP`/`NET_PREAMP` liegt es
nicht, die stehen auf 0 und ändern nichts. Wahrscheinlich die Tandem-
Codierung ACELP → OPUS → ACELP. Ein synthetischer Testton ist dafür ein
schlechter Maßstab — das gehört mit echter Sprache beurteilt und dann über
`audio.gain` nachgezogen.

---

**Historie: warum das so lange gedauert hat.**

Zweimal am 2026-09-04 hat sich eine Instanz sauber angemeldet — einmal
`OE4SXM-R`, später `OE1SXM-PL`, beide mit `TYPE=Reflector` (Protokoll 3).
Beide standen danach mit der richtigen `monitoredTGs` in der Nodeliste des
Reflektors. **Die von `confgen.py` erzeugte Konfiguration ist also
richtig.** Dieselbe Konfiguration liefert bei Wiederholung aber
`Heartbeat timeout` — der Reflektor nimmt die Verbindung an und antwortet
dann nicht mehr.

`TYPE=ReflectorV2` von außen: der Reflektor antwortet nie, auch nicht mit
Sebastians erprobter Multilink-Vorlage (die läuft dort allerdings mit
`HOSTS=127.0.0.1`, also auf dem Reflektor selbst). Andere Nodes wie
`OE1XUU-ST` fahren V2 mit demselben svxlink 1.10.1 erfolgreich — von innen.

**Offen und am Server zu klären:** warum identische Anmeldeversuche mal
durchgehen und mal ignoriert werden. Der nächste sinnvolle Schritt ist,
das Reflektor-Log *während* eines Versuchs mitzulesen
(`docker exec svxreflector tail -f /var/log/svxlink/svxreflector.log`) —
dort steht je Verbindung `Unknown user`, `Received valid auth key`,
`Already connected` oder nichts davon.

**Beleg, wie ein geglückter Lauf aussieht:**

```
Encrypted connection established
Authentication OK
Connected nodes: OE4SXM-R, OE1XUU-ST, OE1SXM-PL, OE1XUU, OE4SXM-HS
```

Damit steht fest: `logic_type: "Reflector"` (Protokoll 3), AUTH_KEY in
Anführungszeichen, Rufzeichen mit Suffix in `CALLSIGN`, ohne Suffix in
`CALL` — die von `confgen.py` erzeugte Config stimmt so, wie sie ist.

**Zwei Fallen, die stundenlang wie ein Konfigurationsfehler ausgesehen
haben, es aber nicht waren:**

1. **`docker run` mit `timeout` beendet den Container nicht.** Es beendet
   nur den Client. Es liefen unbemerkt 18 svxlink-Container weiter, die
   die Node-Sitzungen offen hielten. Immer mit `docker run -d --name X`
   arbeiten und mit `docker stop X` beenden, nie mit `timeout`.
2. **Der Reflektor gibt Node-Sitzungen nicht frei.** `OE4SXM-HS` und
   `OE4SXM-R` standen über eine Stunde nach dem letzten Verbindungsende
   weiter als verbunden in der Statusabfrage. Ein zweiter Anmeldeversuch
   mit demselben Rufzeichen läuft dann in `Already connected` und bekommt
   **gar keine Antwort** — im Client sieht das aus wie `Heartbeat timeout`.
   Praktisch heißt das: **ein Rufzeichen ist nach einem Test verbraucht,**
   bis der Reflektor neu gestartet wird.

**Nützlich zum Debuggen:** Der Reflektor hat eine Statusabfrage auf Port
8888, die keine Zugangsdaten braucht und sofort zeigt, wer verbunden ist:

```bash
curl -s http://reflector.oe1sxm.at:8888/status | python3 -m json.tool
```

**Der alte Stand von früher am Tag, zur Einordnung:**

Das Log von `reflector.oe1sxm.at` (2026-09-04, in
`/var/log/svxlink/svxreflector.log` im Container) sagt es eindeutig:

```
OE4SXM-R: Received valid auth key
OE4SXM-R: Already connected
*** WARNING: Unknown user "OE4SXM-HS"
*** WARNING: Unknown user "OE4SXM"
```

1. **Der AUTH_KEY stimmt** — „Received valid auth key". Das war nie das
   Problem, und mit Zertifikaten hat es ebenfalls nichts zu tun.
2. **`OE4SXM-HS` ist am Reflektor nicht angelegt**, das nackte `OE4SXM`
   auch nicht. Nur `OE4SXM-R` existiert. → Node anlegen.
3. **`OE4SXM-R` hängt als „Already connected".** Die Sitzung vom ersten,
   erfolgreichen Lauf wurde nie freigegeben. → Reflektor neu starten,
   danach nicht wieder hart abschießen (svxlink SIGTERM geben, nicht
   SIGKILL), sonst bleibt die Sitzung wieder stehen.

**`logic_type` muss `Reflector` sein, nicht `ReflectorV2`.** Der Server
fordert zwar einen CSR an, findet kein Zertifikat und fällt dann sauber auf
die Schlüssel-Abfrage zurück — das funktioniert. Mit `ReflectorV2`
antwortet er dagegen überhaupt nicht (im Log nur „Client connected" direkt
gefolgt von „disconnected"), der Client läuft in `Heartbeat timeout`.

**Danach:** eine Runde live testen — sprechen auf TG 2321 muss auf der
TETRA-Seite ankommen und umgekehrt. Dann Etappe 5.

**1. Gegenprobe mit der Tetra-Link-App (schnell, hoher Erkenntniswert).**

Sebastian kann sich mit der Tetra-Link-App auf `brew.oe1sxm.at` verbinden.
Das ist eine *fremde* Implementierung und damit der einzige verfügbare Weg,
die Selbstkonsistenz unseres Tests aufzubrechen — ohne Funkgerät.

```bash
# empfangen, was die App sendet
docker run --rm -v "$PWD/out:/data" svxtetra python3 livetest.py rx \
    --url https://brew.oe1sxm.at --user 100000001 --password '<pw-bs1>' \
    --issi 2320997 --gssi <GSSI der App>

# senden, damit die App es hört
docker run --rm svxtetra python3 livetest.py tx \
    --url https://brew.oe1sxm.at --user 100000001 --password '<pw-bs1>' \
    --issi 2320997 --gssi <GSSI der App> --seconds 3
```

`rx` protokolliert von sich aus das Längenfeld eingehender Frames und die
Mnemonic im Klartext plus roh. Damit klären sich in einem Durchgang:

- **Bit-Reihenfolge**: klingt die Stimme aus der App im `.wav` richtig,
  passt unsere Konvention zu einer fremden Gegenstelle.
- **Offener Punkt 1**: was trägt ein echter Client im Längenfeld — 288
  oder 282?
- **Offener Punkt 2**: zeigt die App unsere Mnemonic an, und schickt sie
  selbst eine (8 Bit oder 7 Bit)?

Noch zu klären: ISSI und GSSI der App, und ob sie überhaupt Sprache kann
oder nur SDS.

**2. Dann Etappe 4.**

## Etappe 4 — svxlink anbinden

`UsrpSink` ausbauen: ACELP → PCM 8 kHz → UDP an svxlink, und retour.

**Stand 2026-09-04 — Richtung TETRA → SVX ist fertig und getestet.**

- `app/audio/usrp.py` — Paketbau und -zerlegung. Format nach den echten
  Mitschnitten aus `usrp_protocol.txt` (DL1HRC). **Achtung Endianness:**
  Sequenz und Keyup stehen big endian (Wert im letzten Byte), der Typ aber
  im *ersten* Byte des Feldes. Kommt aus `chan_usrp.h`. `UsrpMsg.h` aus
  svxlink taugt als Referenz nicht — Getter `ntohl`, Setter `htole32`, und
  der Konstruktor nullt `m_keyup`/`m_type` gleich nach dem Setzen. Unser
  Parser liest deshalb bewusst tolerant und nimmt beide Schreibweisen an.
- `UsrpSink` in `app/audio/base.py` — decodiert und schickt. Die Umtaktung
  60 → 20 ms läuft über eine Warteschlange, die alle 20 ms genau ein Paket
  abgibt, mit Driftausgleich. Sonst bekäme svxlich alle 60 ms ein Dreierpack.
- Getestet in `selftest.py` (Paketformat, 21 Prüfungen) und `codectest.py`
  (ganze Kette gegen einen simulierten svxlink-Empfänger, Korrelation
  **0.978** von PCM bis UDP).

**Gegenrichtung SVX → TETRA ist ebenfalls fertig** (`app/audio/source.py`):
UDP-Empfänger je Reflektor, Sprechbeginn/-ende über das Keyup-Bit,
Rufzeichen aus den TLV-Metadaten, eigene Ruf-Zustandsmaschine mit
Floor-Lock, Gain vor dem Encode. Getestet in `codectest.py` komplett durch
den Router, Korrelation **0.982**.

Die Nutzlast des Abschaltpakets wird **nicht** mit verarbeitet — sie ist
laut Mitschnitt Stille, und sie anzuhängen erzeugte am Ende jeder
Aussendung einen überflüssigen Frame.

**Fallen, die beim ersten Lauf gegen den echten Reflektor auffielen:**

1. **`CALL` in UsrpLogic darf höchstens 6 Zeichen haben.** `OE4SXM-HS`
   wird abgelehnt („Callsign is to long"), UsrpLogic lädt dann gar nicht,
   und ReflectorLogic hängt ohne Audiopfad da. `confgen.py` kürzt deshalb
   auf den Teil vor dem Bindestrich. ReflectorLogic bekommt weiterhin das
   volle Rufzeichen.
2. **Kein `CFG_DIR` setzen.** Mit leerem `CFG_DIR` liest svxlink alle
   `.conf` im Verzeichnis der Hauptdatei mit — bei zwei Instanzen
   überschreibt die zweite die Abschnitte der ersten. Jede Instanz bekommt
   jetzt ihr eigenes Unterverzeichnis.
3. **`CERT_PKI_DIR` muss aufs Datenvolume.** svxlink 1.10 meldet sich mit
   einem Zertifikat an. Liegt der Schlüssel im Container, ist er nach dem
   Neustart weg, der Reflektor sieht einen neuen Schlüssel fürs selbe
   Rufzeichen und antwortet `Access denied`. svxlink legt das Verzeichnis
   außerdem nur eine Ebene tief an, `confgen.py` erzeugt den Elternpfad.
4. **Basis-Image auf Bookworm festgenagelt.** `python:3.12-slim` ist
   inzwischen Debian 13; die svxlink-Binaries aus der Bookworm-Stufe
   fänden dort ihre Bibliotheken nicht (`libjsoncpp25` vs `26`).

**svxlink muss selbst gebaut werden** (Notiz von Sebastian aus
svxdmr-multilink): `UsrpLogic` gibt es **nicht** im svxlink-Master, nur im
Branch `svxlink-usrp` von DL1HRC. Der Build dauert je nach VPS 20–60
Minuten, einmalig. Im Docker-Image also eine eigene Build-Stufe, die
gecached wird — dann kostet es nur beim ersten Mal.

**Branch-Frage geklärt (2026-09-04):** `tetra-contrib` enthält **kein**
UsrpLogic — geprüft über die GitHub-API, das Verzeichnis
`src/svxlink/svxlink/contrib/UsrpLogic/` fehlt dort komplett. Beides in
einem Build ist also nicht zu haben. Für uns zählt `svxlink-usrp`
(neuester Stand, Juli 2026). Es gibt noch `tetra-usrp`, das hat UsrpLogic,
ist aber seit Juni 2025 unverändert und bringt zusätzlich TetraLogic mit —
das brauchen wir nicht, TetraLogic redet über PEI mit einem Funkgerät,
wir gehen über Brew. Etappe 7 wird damit später ein eigenes Thema
(Branches zusammenführen), kein Nebenprodukt dieses Builds.

**Betriebsmodell: alles in einem Container** (entschieden 2026-09-04).
Brücke und die svxlink-Instanzen laufen gemeinsam in einem Image, jede
Instanz als eigener Prozess mit eigener `svxlink.conf`, eigenem USRP-Port
und eigenem Node-Rufzeichen — Grundregel 2 bleibt gewahrt, die trennt
Prozesse, nicht Container. Supervisor ist `manager.py` aus Etappe 5, der
wird dafür ohnehin gebraucht. Grund gegen getrennte Container: die WebUI
soll Dienste starten/stoppen/neustarten, und dafür müsste man sonst den
Docker-Socket hereinreichen. Nebeneffekt: der USRP-Verkehr bleibt auf
localhost, nichts muss nach außen offen sein. Ein Audio-Device braucht
svxlink hier nicht, Reflektor- und USRP-Seite sind beide reines Netz.

- Jitterbuffer 60 ms → 20 ms (TETRA-Frame ist 60 ms, svxlink will 20;
  passt glatt auf: 60 ms = 480 Samples = genau 3 USRP-Pakete à 160)
- Gain vor dem Encode, Startwert 0.5 — ACELP mag keine heiße Modulation
- svxlink.conf-Vorlagen je Reflektor: eigener USRP-Port, eigenes
  Node-Rufzeichen, eigener AUTH_KEY
- Fixe TGs über `MONITOR_TGS`, dynamische Zeile über `DEFAULT_TG`

## TG-Umschaltung per DTMF — gelöst (2026-09-06)

**Ohne Neustart, ohne Audiolücke.** Nachgewiesen: SDS `2323` → Brücke →
DTMF `912323#` → `ReflectorLogic: Selecting TG #2323`, Neustartzähler
bleiben auf 0, und die anschließende Aussendung erscheint am Reflektor mit
`tg=2323`.

**Warum es eine dritte, stumme Logic braucht.** `DTMF_CTRL_PTY` sitzt in
`Logic`. `ReflectorV2Logic` und `UsrpLogic` erben aber von `LogicBase` und
haben überhaupt keinen DTMF-*Eingang* — ein `DTMF_CTRL_PTY=` in deren
Abschnitt würde svxlink kommentarlos ignorieren. Also kommt je Instanz
eine `SimplexLogic` mit `RX=NONE`/`TX=NONE` dazu, die nur die PTY trägt.
Verifiziert: die lädt sauber und legt die PTY an.

**Wie das Kommando ankommt:** `CONNECT_LOGICS=…,DtmfLogic:9:dtmf` — die
Zahl ist eine Kommandonummer. Wählt man sie auf der DtmfLogic, reicht
`LinkManager::sendCmdToLogics` den Rest an alle anderen Logics im Link
weiter, wo `ReflectorV2Logic::remoteCmdReceived` `1<tg>` als TG-Auswahl
versteht. Daher das Template `91{tg}#`.

**Das ist eine PTY, keine Datei** (`app/dtmf.py`). svxlink legt einen
Symlink auf `/dev/pts/N`. Liegt dort eine echte Datei, konnte svxlink den
Symlink nicht setzen — das wird als Fehler gemeldet, nicht stillschweigend
hineingeschrieben. Geschrieben wird mit `os.open(O_WRONLY|O_NOCTTY|
O_NONBLOCK)` in *einem* `write`, ohne Newline, Zeichen nur aus
`[0-9A-D*#]`. ENOENT/EIO heißt „svxlink läuft nicht".

**Verifikation:** `manager.py` fängt svxlinks Ausgabe je Instanz in
`svxlink.log` ab; die Brücke wartet dort bis zu 5 s auf
`Selecting TG #<tg>`. Bleibt sie aus, greift der Notnagel.

**Notnagel** ist der alte Weg — Config neu schreiben und Instanz neu
starten. Er läuft nur noch, wenn `pty_enabled: false` steht oder die PTY
nachweislich fehlt, und protokolliert das deutlich.

**Kosmetik, nicht behoben:** ReflectorLogic will die neue TG per
Sprachansage vorlesen und meldet `Could not find audio clip` — im Image
liegen keine Audioschnipsel. Harmlos, aber es steht bei jeder Umschaltung
im Log.

---

**Historisch, der Stand davor:**

**Zur TG-Umschaltung, und das ist eine Einschränkung, keine Wunschlösung:**
Der Fahrplan wollte umschalten, ohne die Config neu zu schreiben. Das geht
mit diesem svxlink nicht. `UsrpLogic` verarbeitet eingehend nur VOICE und
TEXT — DTMF wird beim Empfang ignoriert, und `m_selected_tg` kommt
ausschließlich aus `DEFAULT_TG`. Die PTY-Kommandoschnittstelle
(`DTMF_CTRL_PTY`, `COMMAND_PTY`) sitzt in `Logic`, aber sowohl
`ReflectorLogic` als auch `UsrpLogic` erben von `LogicBase` — dort gibt es
sie nicht.

Bleiben drei Wege:

1. **Config neu schreiben und die betroffene Instanz neu starten.** Dauert
   ein bis zwei Sekunden, `manager.py` kann das. Für eine Zeile, die per
   SDS selten umgeschaltet wird, vertretbar. Aktuell nicht verdrahtet.
2. **Eine dritte Logic** als vollwertige `Logic` mit `RX=NONE`/`TX=NONE`
   dazunehmen, die `DTMF_CTRL_PTY` mitbringt, und darüber das Kommando
   `1<TG>` an ReflectorLogic schicken (siehe
   `ReflectorLogic::remoteCmdReceived`). Sauberer, aber mehr Config.
3. **UsrpLogic patchen**, damit sie eine TG-Umschaltung annimmt. Wir bauen
   svxlink ohnehin selbst, das Patch-Muster gibt es beim Codec schon.

**Und noch eine Konsequenz daraus:** eine Instanz sendet immer auf ihrer
selektierten TG, und bei mehreren mitgehörten TGs sagt uns svxlink nicht
zuverlässig, aus welcher ein Durchgang kam. Darum in `config.example.yaml`
bewusst **eine sendende TG je Instanz**. Mehrere TGs an einer Instanz
gingen zu empfangen, aber die Zuordnung zur GSSI wäre nicht mehr eindeutig.

**Fertig wenn:** Audio auf GSSI 2321 kommt am Reflektor raus und umgekehrt.

**Ohne TETRA-Hardware testbar.** Es gibt (Stand 2026-09-03) weder
Funkgerät noch BlueStation noch flowstation — das Werkzeug entsteht
bewusst vorher. Macht nichts: der eigene Encoder aus `acelp.py` spielt die
TETRA-Seite, schickt Frames über den echten brew-server, und am Reflektor
wird nachgehört. Dafür reicht der Reflektor, der ohnehin läuft.

Was ohne echtes Gerät **nicht** geprüft werden kann und bis zum Kauf offen
bleibt:

- ob unsere Bit-Reihenfolge zu der eines echten Geräts passt (unser Test
  ist in dem Punkt selbstkonsistent — wir senden und empfangen selbst)
- ob das Gerät die SS-TPI-Mnemonic anzeigt (offener Punkt 2)

## Etappe 5 — WebUI (Pflicht)

Optik **exakt wie svxdmr-multilink**. Design-Tokens:

| | |
|---|---|
| Font | Inter (400/500/600/700/800) |
| Primärblau | `#2F6FE4` |
| Karten | weiß, `border-radius: 16px`, dezenter Schatten |
| Dark | `--color-dark: #0F172A`, Toggle im Header, `localStorage` |
| Icons | Lucide |
| Navigation | Bottom-Tabs |
| Stack | Vanilla JS + Jinja, kein Framework |

> **Vor dem Bauen von Sebastian anfordern:** die `style.css` und ein
> Base-Template aus svxdmr-multilink. Die Tokens oben sind aus früheren
> Chats rekonstruiert, die Datei selbst liegt nicht vor. Ohne sie wird's
> ähnlich statt gleich. **Am 2026-09-04 geliefert und unverändert
> übernommen als `app/static/style.css`.**

Die echten Tokens weichen von der Rekonstruktion oben ab: Akzent ist
`#2563eb`, Radius `14px`, Dark ist `#0e1013`/`#17191e`. Und die Bottom-Tabs
sind ein **Vierer**-Raster (`repeat(4, 1fr)`), nicht sechs. Deshalb vier
Reiter, die Inhalte aus der Liste oben sind darin gruppiert:

| Reiter | Inhalt |
|---|---|
| Status | brew-Verbindung, Reflektoren, laufende Rufe, Last Heard, Zähler |
| Routen | Mapping-Tabelle, Handbetrieb je Zeile, dynamische TG schalten |
| SDS | Verlauf empfangen/gesendet, Kurznachricht schicken |
| Log | Live-Log mit Level-Filter und Autoscroll |
| System | Pegel, Dienste, Rufzeichenliste, eigene Namen, `svxlink.conf`, Passwort, Benutzer |

**Was schon steht (optikunabhängig):**

- `app/web/users.py` — Benutzer in eigener `users.yaml` (nicht in der
  config.yaml, die schreibt die UI beim Bearbeiten von Routen neu).
  Passwörter als PBKDF2-HMAC-SHA256 mit Salt, nur Standardbibliothek.
  Rechte als Matrix Benutzer × Bereich, die Bereiche sind die Reiter.
  Beim ersten Start entsteht `admin` mit dem Passwort `admin`. Der letzte
  Administrator kann sich weder löschen noch entrechten.
- `app/web/logbuffer.py` — Ringpuffer als Logging-Handler für den
  Live-Log. Die UI holt mit `since=<seq>` nur das Neue nach, damit
  braucht es keinen WebSocket.
- Beides in `selftest.py` abgedeckt (21 Prüfungen).
- `app/web/server.py` — aiohttp mit Jinja, Session-Cookie, Rechteprüfung
  je Endpunkt. Ersetzt `app/status_server.py`, das ist gelöscht.
- `app/templates/`, `app/static/app.js` — Vanilla JS, keine Frameworks,
  Lucide-Icons inline als SVG. Theme-Umschalter über `data-theme` und
  `localStorage`, wie in der CSS vorgesehen.

**Dienste steuern über zwei Dateien.** Die WebUI läuft in der Brücke, die
svxlink-Prozesse hängen am Supervisor — der eine kann den anderen nicht
direkt steuern. `manager.py` schreibt deshalb einmal je Sekunde
`services.json` und liest dabei `services.cmd`, die die UI hinterlegt.
Kein IPC, keine zusätzliche Abhängigkeit, eine Sekunde Verzögerung.

**Beim ersten Start** legt die UI `admin` mit dem Passwort `admin` an und
schreibt das einmal ins Log. Solange dieses Passwort steht, blendet der
System-Reiter eine Warnung ein und `/api/status` fuehrt
`default_password: true` - damit es niemand uebersieht. Danach steht das
Passwort nur noch gehasht in `users.yaml` neben der `config.yaml`.

**Was die Reiter können (Stand 2026-09-04, zweiter Durchgang):**

- **Status**: brew-Verbindung mit Fehlertext, Reflektoren mit
  Master-Toggle und svxlink-Zustand, **Floor-Lock je Reflektor**,
  laufende Rufe, Last Heard, **ISSI→Rufzeichen aus dem Subscriber-Cache**,
  Zähler, Laufzeit.
- **Routen**: Zeilen anlegen, bearbeiten, löschen — alle Felder inklusive
  **Zeitplan** (Wochentage als Chips, mehrere Zeitfenster, Nachlauf mit
  Minutenfeld oder hart, Obergrenze). Handbetrieb je Zeile mit Anzeige
  bis wann. Dynamische TG per Kommandofeld, mit Rückfall-Restzeit.
  Damit ist **Etappe 6 inhaltlich miterledigt**.
- **Log**: Live mit Level-Filter und Autoscroll.
- **System**: **Audiopegel in dB** (beide Richtungen getrennt, global für
  alle Reflektoren), Dienste starten/stoppen/neustarten, `config.yaml` und
  `svxlink.conf` im Browser editierbar, Backup herunterladen und
  einspielen, eigenes Passwort, Benutzerverwaltung mit Rechte-Matrix.

**Verstärkung gehört nach svxlink, nicht in die Brücke.** Es gibt je
Richtung zwei Stellschrauben, und die in svxlink ist die bessere:

| Richtung | in svxlink | in der Brücke |
|---|---|---|
| TETRA → SVX | `net_preamp_db` → `NET_PREAMP` | `gain_svx_db` |
| SVX → TETRA | `preamp_db` → `PREAMP` | `gain_tetra_db` |

`UsrpLogic.cpp` hängt hinter beide Verstärker einen **Limiter**
(`NET_LIMITER_THRESH`, `LOCAL_LIMITER_THRESH`) und rechnet in Fließkomma;
unser `apply_gain()` arbeitet auf 16-Bit-PCM und **schneidet hart ab**.
Der Unterschied ist messbar — dieselbe Anhebung, einmal hier und einmal
dort:

| | Brücke +7/+8,5 | svxlink +7/+8,5 |
|---|---|---|
| Kreis | −0,6 dB | −1,0 dB |
| Korrelation | 0,229 | 0,228 |
| **Spitzenpegel** | **84 %** | **44 %** |

Gleicher Pegel, gleiche Qualität, halbe Aussteuerung. Die Brücken-Regler
sind deshalb auf 0 und nur zum Feintrimmen um wenige dB gedacht. Ein
Gegenversuch mit `gain_svx_db: 30` lieferte einen Mittelwert von 72 %
Vollaussteuerung und eine Korrelation von **0,146** — das Signal war
praktisch ein Rechteck.

**Speichern der svxlink-Pegel startet die Instanzen automatisch neu**, weil
sie sonst wirkungslos blieben. Die Brücken-Regler greifen sofort und lösen
keinen Neustart aus. Beides ist in der WebUI-Antwort vermerkt.

Beim Bauen dieser Automatik kamen zwei Fehler ans Licht, die im Betrieb
teuer geworden wären:

1. **Die `svxlink.conf` wurde nur beim Start des Supervisors erzeugt.**
   Jede Änderung über die WebUI — auch an Routen — wäre nach einem
   Neustart der Instanz auf die alte Datei zurückgefallen. `_apply()` in
   `app/web/server.py` ruft jetzt `confgen.write_all()` mit auf.
2. **Der Neustart-Knopf tötete den Dienst endgültig.** `Child.terminate()`
   setzt `_stop = True`, und die Aufsichtsschleife sah das, sobald der
   Prozess weg war — sie gab das Kind auf, statt es neu zu starten. Dafür
   gibt es jetzt `Child.restart()`, das `_stop` nicht anfasst.
   `terminate()` bleibt fürs Herunterfahren.

Außerdem: `services.cmd` wird **angehängt statt überschrieben** und vom
Supervisor per `os.replace` weggenommen, bevor er sie liest. Sonst
überschreibt beim Neustart mehrerer Instanzen das zweite Kommando das
erste, bevor es gelesen wurde.

**Frühere Werte, als noch in der Brücke verstärkt wurde:
`gain_svx_db: 7.0` / `gain_tetra_db: 8.5` — eingemessen
auf 0 dB Verlust.** Ein Signal kommt nach dem kompletten Kreis mit
**−0,6 dB** zurück, Spitzen bei 84 % der Aussteuerung, kein einziges
begrenztes Sample. In vier Durchläufen eingemessen (−11,7 → −6,2 → −2,4 →
−0,6 dB), nicht gerechnet: das Modell war jedes Mal etwas zu optimistisch,
weil ACELP bei höherem Pegel anders arbeitet.

Der Messkreis enthält **genau eine** Strecke je Richtung — „Kreis = 0 dB"
heißt also „beide Richtungen = 0 dB".

**Nicht weiter aufdrehen.** Die letzten 0,6 dB wären zu holen, würden die
Spitzen aber auf ~89 % treiben. Echte Sprache hat einen höheren
Scheitelfaktor als der Testton; die Reserve ist mehr wert als die
Nachkommastelle.

**Die Aufteilung dahinter:**
Der Verlust sitzt fast vollstaendig in svxlink, nicht im Codec. Gemessen am
2026-09-04 mit den Pegelzeilen, die `UsrpSink` und `UsrpSource` je Ruf ins
Log schreiben (mittlerer Betrag der Samples):

| Messpunkt | Pegel |
|---|---|
| TETRA rein | 1768 |
| Bruecke -> svxlink (nach ACELP-Decode) | 1578 |
| Bruecke <- svxlink (nach **zwei** Instanzen) | 596 |
| TETRA raus | 461 |

1578 -> 596 sind -8,5 dB fuer zwei svxlink-Instanzen, also rund **-4 dB je
Instanz**. Im Echtbetrieb liegt pro Richtung nur eine im Weg, darum +4 dB
als Ausgleich. Damit landen beide Richtungen rechnerisch bei -1 bis -2 dB.
ACELP selbst kostet fast nichts (-1 dB rein, -2 dB raus).

**Vorbehalt:** gemessen mit synthetischem Testton und unter der Annahme,
dass sich die -8,5 dB gleichmaessig auf beide Instanzen verteilen. Mit
echter Sprache nachjustieren.

**Audiopegel** stehen in `audio.gain_svx_db` (Richtung TETRA → svxlink,
wird nach dem ACELP-Decode angewendet) und `audio.gain_tetra_db`
(Richtung svxlink → TETRA, vor dem Encode). Bewusst **global und nicht je
Reflektor**: der Pegelunterschied stammt aus der Tandem-Codierung, nicht
aus dem einzelnen Reflektor. Erlaubt sind -30 bis +30 dB, `apply_gain()`
in `app/audio/base.py` begrenzt statt überzulaufen. Das frühere lineare
Feld `audio.gain` gibt es nicht mehr.

**Änderungen greifen sofort.** `Router.reload()` tauscht die Konfiguration
im laufenden Betrieb: Zeitpläne werden neu aufgebaut, der Handbetrieb
bleibt erhalten wo die GSSI gleich blieb, laufende Rufe auf entfernten
Zeilen werden sauber beendet, und die Affiliation wird nachgezogen. Nur
Änderungen an Reflektoren und Ports brauchen einen Neustart der
betroffenen svxlink-Instanz — die Antwort sagt das dann ausdrücklich.

**Update-Funktion: gestrichen** (Entscheidung Sebastian, 2026-09-08).
Der Code steckt im Image, der Container kann sich nicht selbst
ersetzen. Die Alternativen waeren gewesen: Code aufs Datenvolume
legen (Umbau am Dockerfile) oder den Docker-Socket hereinreichen
(genau das haben wir bei den Diensten bewusst vermieden). Er baut neu,
wenn er eine neue Version bekommt - dafuer braucht es keinen Knopf.
Backup/Restore bleibt.

Inhalt:

- **Reflektoren**: Liste mit Master-Toggle, Verbindungsstatus
- **Routen**: Tabelle GSSI / Reflektor / TG / Typ / Priorität / aktiv,
  Zeilen anlegen und löschen
- **Status**: aktive Calls, Floor je Reflektor, Last Heard
- **Log**: Live-Stream mit Level-Filter und Autoscroll
- **Config**: svxlink.conf im Browser editierbar
- **System**: Service start/stop/restart, Backup/Restore/Update

Struktur wie bei den Geschwistertools: `config/config.yaml` +
`config.example.yaml`, `confgen.py` erzeugt die svxlink.conf,
`manager.py` als Supervisor. Session-Login mit änderbarem Passwort,
Benutzerverwaltung mit Rechte-Matrix wie beim Rundspruch-Tool.

`/api/status` liefert bereits genau die Struktur, die die UI rendert.

## Etappe 6 — Zeitplan in der WebUI

Die Logik steht komplett und ist getestet (`schedule.py`), das ist nur
noch Oberfläche:

- Wochentage als Chips, mehrere Zeitfenster pro Zeile
- Zwei Hackerl: **Nachlauf** (mit Minutenfeld) oder **hart**
- Obergrenze für den Nachlauf
- Manuelles Übersteuern mit Anzeige „manuell aktiv bis 20:00"

**Verhalten, das nicht verhandelbar ist:** Fenster 20–22 Uhr, Runde geht
bis 23:20 → abgeschaltet wird um 23:25 (bei 5 min Nachlauf). Während des
Nachlaufs sind **neue Durchgänge normal erlaubt** — eine Runde darf nicht
mitten drin zerbrechen. Nur `hard` schaltet um Punkt Fensterende ab.
Verkehr aus beiden Richtungen setzt den Timer zurück.

---

## Etappe 7 — optional, später

Reflektor am VPS auf `dl1hrc/svxlink` tetra-contrib umbauen, dann laufen
Talker-ISSIs durch den SVXReflector durch (Patches an SVXReflector *und*
ReflectorLogic, beide Seiten). Erst angehen wenn der Rest steht — davor
tut's die ISSI→Rufzeichen-Auflösung über die Brew-Subscriber-Abfrage im
Log und in der UI.

---

# Offene Punkte

1. **Längenfeld im Traffic-Frame — halb geklärt.** Wir senden
   `len(data) * 8` = 288 Bit bei 36 Byte. Der brew-server nimmt das an und
   reicht es unverändert weiter (2026-09-04 geprüft), es kommt kein
   `MALFORMED`. Was ein *fremder* Client hineinschreibt, wissen wir noch
   nicht — das zeigt der `rx`-Lauf gegen die Tetra-Link-App. Falls nötig
   umstellen: `livetest.py --bits 282` bzw. `build_frame(..., bits=282)`.
2. **Mnemonic Coding Scheme — halb geklärt.** Unsere 8-Bit-Kodierung
   (Scheme 0x01) überlebt den Server unverändert: gesendet `OE4SXM`,
   zurückgekommen `OE4SXM`, roh `01 30 4f 45 34 53 58 4d`. Ob ein echtes
   Gerät bzw. die App das auch *anzeigt*, ist damit nicht gesagt — bei
   Zeichensalat auf 7 Bit (`CODING_SCHEME_7BIT`).
3. ~~**Reihenfolge REGISTER → AFFILIATE.**~~ **Erledigt 2026-09-03.**
   Gegen `brew.oe1sxm.at` geprüft: beides direkt nacheinander nach dem
   Upgrade wird angenommen, kein `0xf3`, Verbindung stand knapp eine
   Stunde stabil.

Ebenfalls geprüft: die bekannte brew-server-Falle (`subscriber registered`
ohne `subscriber affiliated ... gssi=`) **greift bei uns nicht** — das
Dashboard zeigte nach dem AFFILIATE auf drei GSSIs sauber `Groups: 3`.
`route_without_affiliations` wird also nicht gebraucht.

# Testen

```bash
python3 selftest.py     # Framing, Bitpackung, Zeitsteuerung
python3 smoketest.py    # kompletter Ablauf gegen einen Mini-Brew-Server
python3 webtest.py      # 62 Pruefungen durch die WebUI-API, braucht laufenden Stack
```

Der Smoketest baut sich seinen eigenen Brew-Server im selben Prozess.
Kein Netzwerk, keine Hardware, keine Credentials nötig.

Der Codec-Test braucht die `.so` und läuft daher im Container:

```bash
docker build -t svxtetra .
docker run --rm svxtetra python3 codectest.py
```

`livetest.py` fährt gegen den **echten** Server (`rx` / `tx` / `loop`).
`loop` macht Senden und Empfangen in einem Prozess mit zwei Zugängen und
ist der Selbsttest über den echten Server:

```bash
docker run --rm -v "$PWD/out:/data" svxtetra python3 livetest.py loop \
    --url https://brew.oe1sxm.at \
    --user 100000001 --password '<pw-bs1>' --issi 2320997 \
    --user2 100000002 --password2 '<pw-bs2>' --issi2 2320998 \
    --gssi 2321 --seconds 3
```

Ohne `.so` überspringt `codectest.py` sich selbst mit Exit 0 — auf Windows
bleiben `selftest.py` und `smoketest.py` also normal grün. Auf dieser
Maschine ist Python nicht im PATH; der Interpreter liegt unter
`~/AppData/Local/Programs/Python/Python313/python.exe` und braucht
`pyyaml` plus `tzdata` (Windows bringt keine Zeitzonendatenbank mit).

# Stil

Antworten kurz und direkt, Austrian German. Größere Sachen erst planen
und besprechen, dann Code. Gegenwind ist erwünscht — wenn ein Vorschlag
Blödsinn ist, sag's.
