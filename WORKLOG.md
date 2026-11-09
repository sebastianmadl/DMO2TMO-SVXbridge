# Worklog — DMO2TMO SVXBridge

Chronologie der Arbeiten. Neueste oben.

---

## 2026-11-09 (Nachmittag) — tetra_fw-Lastheard-Sender

- `app/tetra_fw.py`: VOICE-Zeile bei jedem GROUP_TX von TETRA an
  `<Reflektor-Host>:5220`, POS-Bake 6x beim Start und nach Aenderung.
  Unbekanntes Rufzeichen -> `NO USER`. DNS im Hintergrund, 5 min Cache.
- Config `tetra_fw` (enabled, gateway_call, gateway_comment, gateway_lat,
  gateway_lon, port), Pruefung auf Kommas und Positionsformat.
- WebUI System -> „Lastheard (TETRA-AUSTRIA)“, `/api/tetra_fw`, Call
  vorbefuellt mit dem Reflektor-Rufzeichen, Status mit Zaehler und
  letzter Zeile. Wirkt ohne Neustart.
- TG im VOICE-Feld aus der Sprechgruppe statt fix `#232`.
- Protokoll-Doku korrigiert: bei POS/GPS steht die Laenge vor der Breite.
- Tests: selftest (Zeilenformat, Pruefung, UDP-Empfaenger), smoketest
  (ganzer Ruf -> VOICE-Zeile), webtest (API).
- **Live bestaetigt (12:51):** VOICE-Zeile erscheint im OE-MASTER-Lastheard
  (OE4SXM, #232, 2320997).
- Node-Info fuer den oberen Dashboard-Teil: Reflektor-Felder
  `node_location` (-> `nodeLocation`), `sysop`, `node_info_extra` (JSON);
  confgen schreibt `node_info.json` + `NODE_INFO_FILE`. WebUI System ->
  „Node-Info am Reflektor“, `/api/nodeinfo`, startet svxlink neu.
  Ob das OE-Dashboard die Spalte „Comment“ wirklich aus `nodeLocation`
  liest, ist noch zu pruefen. Das Tetra-Wiki sagt zu node_info nichts.
- Node-Info als ausklappbares Formular (Allgemein, Standort, Sender,
  Empfaenger, JSON) - Formular und JSON zeigen dasselbe Objekt.
- Lastheard-Zeit mit vierstelligem Jahr (`09.11.2026 12:51:22`) wie bei
  den anderen Gateways am OE-Dashboard.
- **Rueckweg Quell-ISSI aus dem OE-Dashboard** (`app/oe_dashboard.py`,
  Wunsch Sebastian): bei Sprechbeginn eines fremden Gateways Lastheard des
  Dashboards lesen, juengste VOICE-Zeile dieses Gateways (hoechstens
  `dashboard_max_age` s alt) -> ISSI als Quell-ISSI, Rufzeichen als
  Mnemonic. GROUP_TX wartet bis 1,5 s, Audio wird so lange gepuffert.
  Parser sucht die Kopfzeile Gateway/Time/Call/Type/ID statt fester
  Klassen. Default aus. WebUI: Schalter, Adresse (leer = Reflektor-Host),
  Hoechstalter, Knopf „Dashboard jetzt lesen“.
  **Gegen das echte Dashboard nicht getestet** - von hier nicht erreichbar,
  Parser nach dem Screenshot gebaut. robots.txt des Dashboards sperrt
  Crawler; Abruf nur einmal je Sprechbeginn (2 s Cache).
- **Offen, nur live pruefbar:** ob der Server dedupliziert, ob VOICE zu
  Sprechbeginn das ist, was das Dashboard erwartet.

---

## 2026-11-09 — Umbau svxtetra → DMO2TMO SVXBridge

### Erledigt

- **Projekt umgebaut** von svxtetra (mehrere Reflektoren, dynamische TG)
  auf DMO2TMO: ein Reflektor, eine feste Sprechgruppe.
  - Config-Prüfung lässt nur noch einen aktiven Reflektor + genau eine
    `fixed`-Zeile zu; dynamische Zeilen/DtmfLogic entfernt (Felder bleiben
    ladbar, tun nichts).
  - Umbenannt: Repo `DMO2TMO-SVXbridge`, Pfad `/opt/dmo2tmo-svxbridge`,
    Container/Dienst `dmo2tmo-svxbridge`, Cookie `dmo2tmo`, Name in WebUI.
  - **Port durchgehend 3007** (Container/Proxy/Host). Alte svxtetra bleibt
    auf 3006 für SVXLink Austria — beide Brücken laufen parallel.

- **ISSI-Durchreichung gebaut** (svxlink-Zweige zusammengeführt):
  - Basis `tetra-contrib` (0d52873) + UsrpLogic aus `svxlink-usrp`
    (a1e3427), verbunden über `svxlink/patches/01-usrplogic-tetra.patch`.
  - ISSI des Sprechers im DMR-ID-Feld der USRP-Metadaten → UsrpLogic macht
    `Qso:info` daraus; Gegenrichtung setzt sie als GROUP_TX-Quell-ISSI.
  - `02-reflector-forwardsds.patch`: Reflektor liefert ForwardSds ans
    Gateway des Empfängers statt zurück an den Absender.
  - Lokal gegen echten svxlink + svxreflector getestet (zwei Knoten):
    ISSI kommt als `Qso:info` an, beim zweiten Knoten als Quell-ISSI raus.
  - `build_metadata()` begrenzt TG/ISSI auf 24 Bit (alter OverflowError).

- **SDS**: keine Kommandos mehr; jede Text-SDS → Postfach + optional über
  svxlink als `Sds:info`/`ForwardSds:info`. Rückweg per
  `AppState.issi_for_callsign()` (eigene Namen + Gehörtes).

- **WebUI**: neuer Abschnitt „TETRA ↔ Reflektor" (System), `/api/tetra`,
  Schalter `issi_passthrough` / `sds_forward`.

- **Repo-Hygiene**: echter Reflektor-AUTH_KEY war in `config.example.yaml`
  gelandet → entfernt, History neu geschrieben, Platzhalter `change-me`.
  Später gesamte History auf einen Commit zusammengefasst (auf Wunsch).
  Autor durchgehend Sebastian Madl, keine AI-Attribution.

- **Live am VPS gebracht**: Container baut, svxlink meldet sich an.

### Befund vor Ort (am VPS, 09:00–10:00)

- Brew-Verbindung: erst Platzhalter `127.0.0.1:9000`, dann
  `brew.oe1sxm.at` mit eigenem Digest-User → **läuft**.
- Reflektor `195.26.215.3:5210`: nach Freischaltung durch Sysop
  **Audio läuft**, Node `OE4SXM-R` / TG 232 am OE-MASTER-Dashboard
  sichtbar.
- **Problem: ISSI/Lastheard erscheint nicht im Dashboard.** Unsere Seite
  schickt die ISSI nachweislich raus (`tetra_qso start 2320997`), aber das
  OE-Dashboard zeigt sie nicht.

### Analyse (Reverse Engineering `tetra_link_gw`)

- Der OE-Verbund nutzt **nicht** tetra-contrib. Lastheard läuft über ein
  **eigenes UDP-Textprotokoll auf Port 5220** (`tetra_fw_server` von
  OE1KBC), getrennt vom svxlink-Audio (5210).
- Protokoll vollständig aus dem ARM-Binary extrahiert und dokumentiert →
  **`docs/tetra_fw_protocol.md`**.
- OK von OE1KBC: nachbauen erlaubt, Funktionen selbst herausfinden.

### Als nächstes (TODO)

1. **`tetra_fw`-Lastheard-Sender bauen** (UDP → SvxReflector:5220):
   - Ziel = **dieselbe Adresse wie der svxlink-Reflektor** (Host aus dem
     Reflektor-Eintrag der Brücke), Port 5220. Kein eigener Host nötig.
   - VOICE-Zeile bei jedem Durchgang (Format siehe Protokoll-Doc §3).
   - Start-Bake (POS 6×) optional.
   - `call`-Feld = Rufzeichen des Sprechers aus `AppState.callsign(issi)`.
2. **Config-Abschnitt `tetra_fw`** (alles in der WebUI editierbar,
   Grundregel 4):
   - `gateway_call` — **frei wählbar**, in der UI **vorbefüllt mit dem
     Login-Callsign** des Reflektor-Eintrags (`reflector.callsign`).
   - `gateway_comment` — frei wählbar.
   - `gateway_lat` / `gateway_lon` — eigene Position (für Start-Bake).
   - `enabled` — Schalter.
   - Host/Port **nicht** als eigene Felder: Host wird vom Reflektor-Eintrag
     übernommen, Port fix 5220 (bzw. ein Feld mit Default 5220).
3. **Gegenprüfen**: Dedup-Verhalten des Servers, VOICE-Timing
   (Sprechbeginn vs. -ende).
4. Später: SDS-Brücke ins `PC:`/`GC:`-Format des OE-Verbunds.
5. Pegel mit echter Sprache nachmessen (preamp_db −3–4 dB, alter TODO 3).

### Entscheidungen (geklärt mit Sebastian)

- **Lastheard-Ziel:** dieselbe Adresse wie der SvxReflector (ein Host für
  Audio 5210 + Lastheard 5220).
- **Gateway-Call & Comment:** frei in der UI wählbar; Call dabei mit dem
  Login-Callsign vorbefüllt.
