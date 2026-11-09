# DMO2TMO SVXBridge

Bruecke TETRA (Brew-Protokoll) <-> SVXLink fuer **eine** Sprechgruppe:
ein Reflektor, eine feste GSSI <-> TG, keine Umschaltung. Dazu reicht sie
die **ISSI** des Sprechers und **SDS** ueber den Reflektor durch - im Format
von DL1HRCs `tetra-contrib`, das TETRA OE DMO verwendet.

- Brew-Client: HTTP-Digest-Auth, WebSocket-Upgrade, Reconnect mit Backoff
- ACELP-Codec (ETSI-Referenzcode, zur Buildzeit geholt) per ctypes
- svxlink mit UsrpLogic **auf Basis von `tetra-contrib`**, gepatcht
  (`svxlink/patches/`), damit ISSI und SDS als State-Events zum Reflektor
  gehen
- Zeitsteuerung der Sprechgruppe inklusive Nachlaufzeit
- WebUI auf Port 3007: Status, Sprechgruppe, SDS, Log, System

Details, Messwerte und Geschichte stehen in `CLAUDE.md`.

## ISSI und SDS

| Richtung | Was passiert |
|---|---|
| TETRA -> SVX | ISSI im DMR-ID-Feld der USRP-Metadaten -> UsrpLogic -> `Qso:info` (plus `TetraUsers:info`/`Register:info`) am Reflektor |
| SVX -> TETRA | `Qso:info` eines anderen TETRA-Gateways -> UsrpLogic -> Metadaten -> Quell-ISSI des Gruppenrufs; Rufzeichen des Sprechers als Mnemonic |
| SDS TETRA -> SVX | `to:OE1ABC:Text` oder `OE1ABC:Text` an die Bruecken-ISSI -> `Sds:info` + `ForwardSds:info` |
| SDS SVX -> TETRA | `ForwardSds:info` fuer ein Rufzeichen -> ISSI aus eigenen Namen / Gehoertem -> SDS `Absender: Text` |

Beides ist in der WebUI (System -> TETRA ↔ Reflektor) abschaltbar.

**Was der Reflektor dafuer braucht:** `tetra-contrib` von DL1HRC. Damit eine
SDS beim *Gateway des Empfaengers* ankommt und nicht beim Absender zurueck,
zusaetzlich `svxlink/patches/02-reflector-forwardsds.patch`. Ein normaler
SvxReflector ignoriert die State-Events - Audio laeuft trotzdem.

Gegen einen lokalen `svxreflector` mit zwei svxlink-Knoten durchgetestet:
ISSI kommt am Reflektor als `Qso:info` an und beim zweiten Knoten als
Quell-ISSI raus, SDS wird ueber den Reflektor zum richtigen Gateway
weitergereicht.

## Schnellstart

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml     # anpassen!
python3 -m app.main config.yaml
```

Oder mit Docker:

```bash
mkdir -p data && cp config.example.yaml data/config.yaml
docker compose up --build
```

Tests ohne Netzwerk und ohne Server:

```bash
python3 selftest.py     # Framing, Bitpackung, Zeitsteuerung
python3 smoketest.py    # kompletter Ablauf gegen einen Mini-Brew-Server
```

Beide laufen durch, bevor du irgendwas deployst. Der Smoketest baut sich
seinen eigenen Brew-Server im selben Prozess und prueft Digest-Auth,
Registrierung, Affiliation, Gruppenruf, Loopback und SDS.

## Auf den VPS bringen

### Was hinaufgehoert

Der Quellbaum, **ohne** Daten- und Bauartefakte. Das `.dockerignore` haelt
sie ohnehin aus dem Image, aber hinaufkopieren muss man sie gar nicht:

Am einfachsten per `git clone` nach `/opt/dmo2tmo-svxbridge`. Von Hand:

```
app/  codec/  svxlink/
Dockerfile  docker-compose.yml  .dockerignore  requirements.txt
confgen.py  manager.py
config.example.yaml
livetest.py  selftest.py  smoketest.py  codectest.py  webtest.py
README.md  CLAUDE.md
```

**Nicht** hinauf: `out/` bzw. `data/` (Laufzeitdaten mit Zugangsdaten,
Rufzeichenliste, Aufnahmen), `__pycache__/`, `codec/*.so`,
`codec/etsi_tetra_codec.zip`.

```bash
rsync -av --delete \
  --exclude out/ --exclude data/ --exclude __pycache__/ \
  --exclude 'codec/*.so' --exclude 'codec/build/' \
  --exclude 'codec/etsi_tetra_codec.zip' \
  ./ user@ms-photo:/opt/dmo2tmo-svxbridge/
```

### Was auf dem VPS entsteht

Ein Verzeichnis `data/` neben dem Compose-File. Da hinein gehoert genau
**eine** Datei von Hand — alles andere legt das Programm selbst an:

```
/opt/dmo2tmo-svxbridge/data/
  config.yaml        <- von Hand, aus config.example.yaml oder der eigenen
  users.yaml         automatisch beim ersten Start (admin/admin)
  sds.json           automatisch, das SDS-Postfach
  idlist.sqlite      automatisch, sobald die Rufzeichenliste geholt wird
  svxlink/R1/…       automatisch, je Instanz eine svxlink.conf + Zertifikat
  svxlink.log        automatisch
```

Das `data/`-Verzeichnis ist **das einzige, was gesichert werden muss**.

### Ausfuehren

Der Container haengt im vorhandenen Netz **`proxy_bridge`**, legt also
kein eigenes an. Das Netz muss es geben, sonst startet Compose nicht:

```bash
docker network ls | grep proxy_bridge     # da? sonst:
docker network create proxy_bridge
```

```bash
cd /opt/dmo2tmo-svxbridge
mkdir -p data
cp config.example.yaml data/config.yaml
$EDITOR data/config.yaml          # brew-Zugang, Reflektoren, Routen

docker compose build              # beim ersten Mal 20-60 min (svxlink)
docker compose up -d
docker compose logs -f
```

Dann `http://127.0.0.1:3007`, Anmeldung **admin / admin**. Port 3007,
weil die alte svxtetra-Bruecke (SVXLink Austria) auf 3006 weiterlaeuft.

Der Reverse-Proxy erreicht die Oberflaeche im selben Netz unter
**`http://dmo2tmo-svxbridge:3007`** — kein veroeffentlichter Port noetig. Die
zusaetzliche Bindung auf `127.0.0.1:3007` im Compose-File ist nur fuer
`curl` und `webtest.py` am VPS selbst gedacht und kann weg.

> Scheitert ein `docker compose up` (etwa an einem belegten Port), erst
> `docker compose down` und dann neu starten. Ein Container aus einem
> abgebrochenen Start haengt sonst in **gar keinem** Netz und ist weder
> ueber den Proxy noch lokal erreichbar — sein Healthcheck meldet trotzdem
> `healthy`, weil der im Container selbst laeuft.

### Danach, in dieser Reihenfolge

1. **Passwort aendern.** System-Reiter. Solange `admin/admin` steht, warnt
   die Oberflaeche sichtbar.
2. **Reverse-Proxy davor**, wenn die Oberflaeche von aussen erreichbar sein
   soll. Das Compose-File veroeffentlicht bewusst nur auf `127.0.0.1` — die
   WebUI spricht kein HTTPS und kennt nur ein Passwort.
3. **Rufzeichenliste holen** (System-Reiter, „Aktualisieren"), sonst bleibt
   die Namensaufloesung auf das beschraenkt, was Geraete selbst mitschicken.

### Betrieb

```bash
docker compose ps                 # Zustand, inkl. Healthcheck
docker compose logs -f            # mitlesen
docker compose restart            # neu starten
docker compose down               # anhalten (SIGTERM, 30 s Frist)
docker compose up -d --build      # neue Version einspielen
```

**Nie `docker kill` oder `docker rm -f`.** Beim Beenden schliesst die
Bruecke offene Gruppenrufe und leert die Sendequeue; wird sie getoetet,
bleiben die Rufe am brew-server als Geister stehen. Dafuer steht
`stop_grace_period: 30s` im Compose-File.

### Was der Build braucht

- **Internet zur Buildzeit**: der ETSI-Codec wird von der ETSI-Seite geholt
  (Grundregel 1, er darf nicht ins Repo) und svxlink von DL1HRC
  (`tetra-contrib` + UsrpLogic aus `svxlink-usrp`, beide auf Commits
  festgenagelt) geholt, gepatcht und uebersetzt.
- **Zeit und RAM**: der svxlink-Build dauert 20-60 Minuten und will rund
  2 GB. Er landet in einer eigenen Docker-Stufe und wird danach gecacht -
  spaetere Builds dauern Sekunden.
- **Ausgehend offen**: brew-Server (WSS 443) und SvxReflector (TCP 5300).
  Eingehend muss nichts offen sein, der USRP-Verkehr laeuft im Container.

## Der Loopback-Test

Der brew-server routet nur *zwischen* verbundenen Clients. Haengt nur eine
Bruecke dran, passiert gar nichts - kein Fehler, einfach Stille. Zum Testen
brauchst du zwei Endpunkte:

1. `brew-server` starten, zwei Digest-User anlegen:

   ```toml
   [auth.users]
   "100000001" = "bridge-a"
   "100000002" = "bridge-b"
   ```

2. Instanz A: ISSI 2320997, Route auf GSSI 2321, `loopback.target_gssi: 2324`
3. Instanz B: ISSI 2320998, Route auf GSSI 2324, `loopback.target_gssi: 2321`

Was du auf 2321 reinschickst, muss auf 2324 wieder rauskommen und
umgekehrt. Damit sind Auth, Registrierung, Affiliation, GROUP_TX-Routing,
Framing und beide Senderichtungen geprueft - ohne Codec, ohne svxlink,
ohne TETRA-Hardware.

Im brew-server-Dashboard (`http://<server>:9000/`) musst du beide als
Subscriber sehen. Steht dort `subscriber registered`, aber kein
`subscriber affiliated ... gssi=`, dann greift das GSSI-Routing nicht -
setz uebergangsweise `route_without_affiliations = true`.

## Die Sprechgruppe

Genau eine Zeile: `gssi` auf TETRA-Seite, `tg` am Reflektor, `type: fixed`.
Eine zweite Zeile, ein zweiter aktiver Reflektor oder `type: dynamic`
werden von der Config-Pruefung abgelehnt. Felder fuer Zeitplan und
Nachlauf (`shutdown_mode`, `grace_minutes`, `schedule`) gelten weiter.

## Zeitsteuerung

Drei Zustaende pro Zeile:

- **active** - Fenster offen
- **extended** - Fenster zu, aber es war zuletzt Verkehr. Es laeuft ganz
  normal weiter, auch neue Durchgaenge. Geht die Runde von 20-22 Uhr bis
  23:20, schaltet die Zeile bei 5 Minuten Nachlauf um 23:25 ab.
- **inactive** - aus

`shutdown_mode: hard` ueberspringt `extended` und schaltet um Punkt
Fensterende ab, auch mitten im Gespraech.

Gezaehlt wird der Verkehr aus beiden Richtungen. Der Nachlauf-Timer
startet bei jedem Talker-Ende neu und wird bei jedem neuen Talker
verworfen. `max_extend_minutes` ist die Notbremse, falls irgendwo ein Node
haengt und alle paar Minuten einen Traeger raushaut.

Manuelles Schalten schlaegt den Zeitplan und gilt bis zum naechsten
Fensterwechsel:

```bash
curl -X POST "http://localhost:3007/api/route/2321/manual?value=off"
curl -X POST "http://localhost:3007/api/route/2321/manual?value=auto"
```

Zeitzone kommt aus der Config, nicht aus dem Container-Default - sonst
sitzt du im Winter eine Stunde daneben.

## SDS

Es gibt keine SDS-Kommandos mehr. Jede Text-SDS an die Bruecke landet im
Postfach (Reiter SDS) und geht - wenn eingeschaltet - ueber den Reflektor
weiter, siehe oben. `sds_control.coding_scheme` bestimmt nur noch, wie die
Bruecke selbst SDS kodiert.

## Rufzeichen

Die Bruecke loest ISSI -> Rufzeichen ueber die Mnemonic des Geraets, eigene
Namen (System-Reiter) und die radioid.net-Liste auf. Fuer SDS vom Reflektor
geht es umgekehrt (Rufzeichen -> ISSI), dafuer zaehlen nur eigene Namen und
Gehoertes - die radioid.net-Liste vergibt DMR-IDs.

## Was noch gegen einen echten Server geprueft werden muss

Drei Stellen habe ich nach der Spec gebaut, aber ohne echten Mitschnitt.
Wenn etwas nicht tut, zuerst hier schauen:

1. **Laengenfeld im Traffic-Frame.** Die Spec sagt "Laenge der folgenden
   Daten in Bit". Wir senden `len(data) * 8` = 288 bei einem 36-Byte-Frame.
   Denkbar waere auch 282 (8 + 137 + 137 ohne Padding). Falls der Server
   meckert: `build_frame(..., bits=282)`.
2. **Mnemonic Coding Scheme.** Default ist 8 Bit ISO 8859-1 (Scheme 0x01).
   Falls das Geraet Zeichensalat anzeigt, auf 7 Bit umstellen
   (`p.encode_mnemonic(text, p.CODING_SCHEME_7BIT)`).
3. **Reihenfolge REGISTER -> AFFILIATE.** Wir schicken beides direkt nach
   dem Upgrade. Falls der Server eine Bestaetigung dazwischen erwartet,
   muss `on_connected` warten.

Die Frame-Laengen selbst (GROUP_TX 64, GROUP_IDLE 19, SHORT_TRANSFER 58,
REGISTER 18) sind im `selftest.py` gegen die Spec verankert.

## Aufbau

```
app/
  main.py            Einstiegspunkt, Ticker
  config.py          Konfiguration, Validierung
  routing.py         Sprechgruppe, Floor-Lock, ISSI- und SDS-Weiterleitung
  schedule.py        Zeitfenster und Nachlauf-Zustandsmaschine
  state.py           Last Heard, Zaehler, Subscriber-Cache
  sdsbox.py          SDS-Postfach
  idlist.py          ISSI -> Rufzeichen
  brew/              Protokoll, WebSocket-Client, Digest, SDS
  audio/             ACELP, USRP-Pakete, Senke/Quelle Richtung svxlink
  web/               WebUI (aiohttp + Jinja), Benutzer, Log-Puffer
confgen.py           erzeugt je Reflektor eine svxlink.conf
manager.py           Supervisor fuer Bruecke + svxlink-Instanzen
codec/               Build-Script, C-Wrapper, osmocom-Patches (kein ETSI-Code)
svxlink/patches/     UsrpLogic auf tetra-contrib, ISSI/SDS; Reflektor-Fix
```

## Etappen

- [x] 1 - WS-Client, Digest, Register/Affiliate
      (gegen `brew.oe1sxm.at` geprueft: Auth, REGISTER, AFFILIATE laufen)
- [x] 2 - Frame-Layer, Traffic-Frames roh durchschleifen
- [x] 2b - SDS-Control fuer die TG-Umschaltung
- [x] 3 - ACELP: Codec gebaut und ueber den echten brew-server abgenommen
      (50 Frames raus / 50 rein, Korrelation 0.984, 5 ms Laufzeit).
      Werkzeug dafuer ist `livetest.py`. Offen bleibt die Gegenprobe mit
      einer fremden Gegenstelle - siehe CLAUDE.md.
- [x] 4 - USRP-Anbindung an svxlink, Jitterbuffer 60 ms -> 20 ms
- [x] 5 - WebUI im Multilink-Stil
- [x] 6 - Zeitplan in der WebUI editierbar
- [x] 7 - ISSI und SDS durch den SvxReflector (`tetra-contrib` + Patches)

Der Umstieg von deinem eigenen Server auf tetrapack.online ist danach nur
noch ein anderer Hostname plus deren Credentials.
