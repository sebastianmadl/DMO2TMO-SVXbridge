# TETRA-AUSTRIA Lastheard-/FW-Protokoll (tetra_fw_server)

Analyse des OE-Verbund-Protokolls, mit dem die Gateways (`tetra_link_gw`,
C++ von OE1KBC) ihre Lastheard-, Positions- und SDS-Daten an den
`tetra_fw_server` am SVXReflector melden.

Quelle: Reverse Engineering des ARM-Binaries `tetra_link_gw` (B.01.18,
Raspbian 8.3.0, nicht gestript, mit debug_info) plus das offizielle
`tetra_link_gw.conf` und das OEVSV-TETRA-Wiki
(`srv08.oevsv.at/tetrawiki`, Stand 2024-10-13). Kein Quellcode vorhanden.

Legende: **[sicher]** aus Binary/Config belegt, **[angenommen]** aus
Kontext erschlossen, noch gegenzuprüfen.

---

## 1. Das Grundprinzip: zwei getrennte Kanäle

Der OE-Verbund trennt **Audio** und **Lastheard** sauber:

| Kanal | Protokoll | Port | Wer |
|---|---|---|---|
| Audio | svxlink SvxReflector (TCP, verschlüsselt) | 5210 | svxlink/UsrpLogic |
| Lastheard/Pos/SDS | **eigenes UDP-Textprotokoll** | **5220** | `tetra_link_gw` → `tetra_fw_server` |

**Das ist der Grund, warum unsere ISSI am Dashboard nicht auftaucht.**
Die Brücke hängt nur am Audio-Kanal (5210) und schickt `Qso:info` im
tetra-contrib-Format — das versteht der OE-Server nicht. Die Lastheard-
Anzeige kommt ausschließlich über den UDP-Kanal auf 5220, und dort meldet
sich die Brücke bisher gar nicht.

`tetra-contrib` wird im OE-Verbund **nicht** verwendet. Der Server fährt
normales svxlink + den `tetra_fw_server` von OE1KBC. **[sicher]**

---

## 2. Transport des Lastheard-Kanals **[sicher]**

- **UDP**, `AF_INET`, Ziel `REFLECTOR_IP:REFLECTOR_PORT`.
- `REFLECTOR_IP` = aufgelöst aus `REFLECTOR_DNS` (per `gethostbyname`).
- `REFLECTOR_PORT` aus der Config (Vorgabe/Beispiel **5220**).
- Nutzlast: **eine ASCII-Textzeile je UDP-Paket**, Felder
  komma-getrennt, abgeschlossen mit `\n`.
- Keine Authentifizierung, kein Handshake, kein ACK. Fire-and-forget.
- `sockaddr_in`: `sin_family=2`, `sin_port=htons(PORT)`,
  `sin_addr=inet_addr(REFLECTOR_IP)`, Länge 16.

Beim Start sendet das Gateway seine **eigene Position 6×** im Abstand von
1 s als Registrierungs-/Anmeldebake (POS-Zeile, siehe unten). **[sicher]**

---

## 3. Zeilenformate (Lastheard) **[sicher]**

Die vier führenden Felder sind bei allen Typen gleich und entsprechen 1:1
den Dashboard-Spalten `Gateway | Comment | Time | Call`. Danach folgen
Typ, TG, ID und die Positionsdaten.

```
VOICE:  <gateway>,<comment>,<zeit>,<call>,VOICE,#232,<issi>,,,
POS:    <gateway>,<comment>,<zeit>,<call>,POS,#232,<issi>,,E;<lon>;N;<lat>;000;,
GPS:    <gateway>,<comment>,<zeit>,<call>,GPS,#232,<issi>,,E;<lon8>;N;<lat7>;<kurs>;,
GC:     <gateway>,<comment>,<zeit>,<call>,GC,#0,<text>,,<text>,
```

Rohe Formatstrings aus dem Binary:
```
%s,%s,%s,%s,VOICE,#232,%s,,,
%s,%s,%s,%s,POS,#232,%s,,E;%s;N;%s;000;,
%s,%s,%s,%s,GPS,#232,%s,,E;%8.8s;N;%7.7s;%s;,
%s,%s,%s,%s,GC,#0,%s,,%s,
```

### Feldbelegung

| Feld | Dashboard-Spalte | Inhalt |
|---|---|---|
| 1 `gateway`   | Gateway | Node-/Gateway-Rufzeichen, z.B. `OE4SXM-R` (`GATEWAY_CALL`) |
| 2 `comment`   | Comment | Standorttext, z.B. `Tetra-OE4SXM/430.0000` (`GATEWAY_COMMENT`) |
| 3 `zeit`      | Time    | Zeitstempel `%d.%m.%y %H:%M:%S` (lokale Zeit des Gateways) |
| 4 `call`      | Call    | Rufzeichen des **Sprechers** (nicht des Gateways) |
| —             | Type    | literal `VOICE` / `POS` / `GPS` / `GC` |
| —             | TG      | literal `#232` (bzw. `#0` bei GC) |
| 5 `issi`      | ID      | **ISSI des Sprechers** (bei GC: der Text) |
| 6/7           | POS     | **Laenge, dann Breite** bei POS/GPS (siehe unten) |

**Wichtig:** `call` und `issi` sind **getrennte** Felder. Der Server löst
also nicht ISSI→Call auf — das Gateway liefert beides. Hat das Gateway
kein Rufzeichen zur ISSI, zeigt das Dashboard `NO USER` (im Binary als
String `NO USER` belegt). **[sicher]**

Die TG `#232` ist im Binary **fest einkompiliert** in den
Lastheard-Zeilen. Für eine andere TG müsste man das Feld variabel machen
(bei uns kein Problem, wir bauen die Zeile selbst). **[sicher]**

### VOICE-Ablauf **[angenommen]**
Bei PTT-Beginn (`+CTCR`/`+GIU` vom Funkgerät, bzw. bei uns: Durchgang von
TETRA) wird die VOICE-Zeile gesendet. Zusätzlich schreibt das Gateway
lokal `PTT ON:`/`PTT OFF:` nach `/tmp/ptt`, um svxlink zu tasten — das ist
die PEI-/Funkgerät-Seite und für unsere Brücke irrelevant.

### Positionsdaten
- **Reihenfolge: zuerst die Laenge (nach `E;`), dann die Breite (nach
  `N;`).** Belegt durch das Beispiel `E;01624.91;N;4816.82` und die
  Formatbreiten `%8.8s`/`%7.7s`: `01624.91` (= `GATEWAY_LON`) hat 8
  Zeichen, `4816.82` (= `GATEWAY_LAT`) 7. Eine fruehere Fassung dieses
  Dokuments hatte das vertauscht.
- POS: Laenge/Breite als Strings, Kurs fix `000`.
- GPS: Laenge 8-stellig, Breite 7-stellig, plus Kurs. Format passt zu APRS
  (`E;01624.91;N;4816.82;000;`) und speist die OE-TETRA-MAP über aprs.fi.

---

## 4. Config-Datei `tetra_link_gw.conf` **[sicher]**

Alle Schlüssel, die das Binary liest (`read_config`):

```ini
[GATEWAY]
GATEWAY_CALL=YY1ZZZ-RL          ; Spalte 1 (Gateway)
GATEWAY_COMMENT=City/Frequenzy  ; Spalte 2 (Comment)
GATEWAY_ID=MCC199               ; eigene Gateway-ISSI (Doku: 2321990)
GATEWAY_LAT=4816.82             ; eigene Position (Bake)
GATEWAY_LON=01624.91
# GATEWAY      =NO
# DMO-REPEATER=YES
DMO_REPEATER=YES

[SVXREFLECTOR]
REFLECTOR_DNS=tetra-reflector.dyndns.org   ; I-NET, wird per DNS aufgelöst
#REFLECTOR_DNS=44.143.8.74                 ; alternativ HAMNET
REFLECTOR_PORT=5220                         ; LASTHEARD PORT (UDP)

[WEATHER]
CITY=City
ZIP=9999
COUNTRY=AT

[SYSTEM]
SYS_HARDWARE=MTM5400     ; CM5000 CM5000A MTM5400 MTM800e MTM800FuG MTP850S MTP850FuG SRG3900
SYS_SERPORT=/dev/ttyS0   ; serielle Leitung zum Funkgerät (PEI)
SYS_SERBAUD=9600
SYS_CTOM_WAIT=3
SYS_DEBUG=0
```

Für **unsere** Brücke relevant: `GATEWAY_CALL`, `GATEWAY_COMMENT`,
`GATEWAY_LAT/LON`, `REFLECTOR_DNS`, `REFLECTOR_PORT`. Der ganze
`[SYSTEM]`-Block (serielles Funkgerät) und `[WEATHER]` sind Funkgerät-
bzw. Zusatzkram, den wir nicht brauchen.

---

## 5. SDS-Nachrichtendienst **[sicher aus Binary + Wiki]**

SDS laufen über denselben UDP-Kanal bzw. als Text an den Server. Befehle
per Präfix, Groß-/Kleinschreibung im Befehlsteil egal:

| Präfix | Zweck | Ziel |
|---|---|---|
| `PC:` | Private Call (Textnachricht an einen Teilnehmer) | ISSI oder Call |
| `GC:` | Group Call (wird auch am Dashboard gezeigt) | nur Text |
| `DN:` | DAPNET-Nachricht → hampager.de | Call |
| `DG:` | DAPNET-Rubrik-Nachricht | Rubrik-Code |
| `HM:` | HAMNET-Messenger | Call |
| `RG:` | Registrieren (mehrfach erlaubt, überschreibt RIC) | eigener Call [+ DAPNET-RIC] |
| `DR:` | DAPNET-Rubrik registrieren (braucht vorher `RG:`) | Rubriknummer + Name |
| `DX:` | DAPNET-Rubrik abmelden | Rubrikname/-nummer |

Beispiele: `PC:partner-issi:Nachrichtentext`, `RG:own-call:own-ric`.

Interne Server-Textformate aus dem Binary (für PC/Routing):
```
::%s;%s;RO            ; Roaming/Registrierung  (ROAMING->%s)
::%s;%s;%s;           ; allgemein
::%s;%s;PC:%s;        ; Private Call Zustellung
%s:PC:                ; PC-Weiterleitung
```

SDS **Empfang** vom Funkgerät über AT `+CTSDSR:` / `+CTICN:`. Für unsere
Brücke kommt SDS stattdessen über Brew rein — wir müssten sie nur ins
`PC:`/`GC:`-Format übersetzen.

---

## 6. Status-/DTMF-Steuerung **[sicher]**

Das Gateway übersetzt TETRA-Status-Codes in svxlink-TG-Umschaltungen und
schreibt sie lokal nach `/tmp/dtmf_ctrl` (svxlinks `DTMF_CTRL_PTY`):

| Status vom Gerät | Wirkung |
|---|---|
| `910#`   | lokal (TG 9) |
| `919#`   | — |
| `9120#`  | TG 20 (D-A-CH) |
| `9199#`  | Status-Abfrage (`9199#1#`) |
| `912321#`| TG 2321 |
| `91232#` | TG 232 (OE-weit) |
| `912323#`| TG 2323 |
| `912326#`| TG 2326 |
| `91262#` | TG 262 (DL-weit) |

Das ist die **DMO-Repeater-Seite** (Gerät wählt per Status die TG). Für
unsere feste Ein-TG-Brücke nicht nötig.

---

## 7. Funkgerät-Schnittstelle (PEI, AT-Kommandos) **[sicher]**

Nur zur Vollständigkeit — das ist die serielle Seite zum Motorola-Gerät,
die unsere Brücke durch Brew ersetzt:

```
ATE1
AT+CTOM=6 / AT+CTOM=1      ; Betriebsmodus (DMO/TMO)
AT+CTSP=2,0,0
AT+CTSP=1,2
AT+CTSP=1,3,2|3|10|130|131|138
AT+CNUMF?                  ; eigene Nummer abfragen
AT+CTSDS=12,0              ; SDS-Service
AT+CMGS=<ziel>,<len>       ; SDS senden (8200=GC, 8204/820C=PC)
```
Empfang: `+CTICN:` (eingehender Ruf), `+CTSDSR:` (SDS),
`+CTCR:`/`+CDTXC:`/`+CTXG:`/`+GIU:` (PTT/Talker-Status),
`+CTGS:` (Gruppen-Status).

---

## 8. ISSI-/Kennungsschema (aus dem Wiki) **[sicher]**

ITSI nach ETSI ETS 300 392-1, 48 Bit:
- **MCC** 10 Bit, Amateurwert **901**
- **MNC** 14 Bit, Amateurwert **16383**
- **ISSI** 24 Bit, Amateurformat 7-stellig im DMR-ID-Stil, z.B.
  `2621234` oder `2321234`
- Beispiel gültige Kennung: `901 16383 2321001` (OE1KBC)
- **Gateways** einheitlich `901 16383 2321990` empfohlen
- **GSSI** für DMO-Repeater-Kompatibilität: `1`

Sprechgruppen am OE-SVXReflector:
`9` lokal · `20` D-A-CH · `23` 23cm-SVXLINK · `88` OE-LINK-FM ·
`232` OE-weit (Default OE-Server) · `262` DL-weit (Default DL-Server) ·
`2321–2329` Bundesländer · `9999` Echo.

Reflektoren:
- OE I-NET: `tetra-reflector.dyndns.org`
- OE HAMNET: `44.143.8.74` (`/svxrdb/`)
- D-A-CH: `leipzig2000.dyndns.org:4047`
- YO: `tetra-reflector.ddns.net:8888` (`/svxrdb/`)

---

## 9. Was das für DMO2TMO SVXBridge heißt

Um im OE-Dashboard aufzutauchen, braucht die Brücke einen **zweiten,
kleinen Ausgang**: einen UDP-Sender, der bei jedem Durchgang eine
VOICE-Zeile nach Abschnitt 3 an `<reflector>:5220` schickt — parallel zum
bestehenden svxlink-Audio. Rein additiv, kein svxlink-Patch nötig.

**Umgesetzt** in `app/tetra_fw.py` (Config `tetra_fw`, WebUI System ->
„Lastheard (TETRA-AUSTRIA)“, `/api/tetra_fw`). Abweichungen vom Original:
TG kommt aus der Sprechgruppe statt fix `#232`; die POS-Bake traegt als
Call den Gateway-Call und als ID die eigene Brew-ISSI.

Ursprünglicher Plan (Entscheidungen mit Sebastian geklärt):
1. Config-Abschnitt `tetra_fw` in der WebUI editierbar (Grundregel 4):
   - `gateway_call` — frei wählbar, **vorbefüllt mit dem Login-Callsign**
     des Reflektor-Eintrags (`reflector.callsign`).
   - `gateway_comment` — frei wählbar.
   - `gateway_lat` / `gateway_lon` — eigene Position (Start-Bake).
   - `enabled` — Schalter; Port-Feld mit Default 5220.
   - **Host wird NICHT separat gesetzt** → Lastheard geht an **dieselbe
     Adresse wie der SvxReflector** (ein Host für Audio + Lastheard).
2. UDP-Sender-Modul, das VOICE bei Sprechbeginn und (optional) eine
   Start-Bake (POS 6×) sendet. `call`-Feld = `AppState.callsign(issi)`.
3. Später optional: SDS per `PC:`/`GC:` in denselben Kanal.

Offen / gegenzuprüfen:
- **[angenommen]** Ob der Server pro Gateway-Call dedupliziert oder jede
  VOICE-Zeile einzeln listet.
- Exakte Reihenfolge/Timing von VOICE bei Sprechbeginn vs. -ende.
