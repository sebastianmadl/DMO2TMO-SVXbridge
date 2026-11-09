# Worklog — DMO2TMO SVXBridge

Chronologie der Arbeiten. Neueste oben.

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

1. **`tetra_fw`-Lastheard-Sender bauen** (UDP → `<reflector>:5220`):
   - VOICE-Zeile bei jedem Durchgang (Format siehe Protokoll-Doc §3).
   - Start-Bake (POS 6×) optional.
   - Rufzeichen aus `AppState.callsign(issi)`.
2. **Config-Abschnitt `tetra_fw`** übernehmen, orientiert an
   `tetra_link_gw.conf` (`GATEWAY_CALL`, `GATEWAY_COMMENT`,
   `GATEWAY_LAT/LON`, `REFLECTOR_DNS`, `REFLECTOR_PORT`) — alles in der
   WebUI editierbar (Grundregel 4).
3. **Gegenprüfen**: Host/Port des Lastheard-Servers (ein Host für Audio +
   Lastheard? siehe Protokoll-Doc §9), Dedup-Verhalten, VOICE-Timing.
4. Später: SDS-Brücke ins `PC:`/`GC:`-Format des OE-Verbunds.
5. Pegel mit echter Sprache nachmessen (preamp_db −3–4 dB, alter TODO 3).

### Offene Fragen an Sebastian

- Lastheard-Host/Port: derselbe wie Audio (195.26.215.3) oder
  `tetra-reflector.dyndns.org`?
- Werte für `GATEWAY_CALL` (z.B. `OE4SXM-R`) und `GATEWAY_COMMENT`
  (z.B. `Tetra-OE4SXM/430.0000`) für die Lastheard-Zeilen.
