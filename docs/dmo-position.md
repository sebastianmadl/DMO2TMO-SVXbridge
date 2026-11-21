# DMO positions for the brew-server map (`dmo-position`)

The bridge forwards the DMO users and DMO gateways of the TETRA-AUSTRIA
network to the brew-server map. Code: `app/dmo_positions.py`, config section
`map`, WebUI System -> "Karte (brew-server)", API `GET/POST /api/map`.

## Source: the OE dashboard

The DMO gateways (`tetra_link_gw`) report to the `tetra_fw_server`
(docs/tetra_fw_protocol.md). Its dashboard (`tetra_fw.dashboard_url`, default
`http://<reflector host>/`, the bridge follows `create_html.php`) shows:

| Dashboard | Meaning for the map |
| --- | --- |
| Lastheard `GPS` row | a DMO user's position: Call, ID = user ISSI, Gateway = the gateway that heard it, POS = `E;gggmm.mm;N;ggmm.mm;...` |
| Lastheard `POS` row with Call = Gateway | the gateway's own beacon: ID = gateway ID, POS = its position, Comment = its name |
| Lastheard `VOICE` row | a DMO user talking through a gateway (no position) |
| table "Gateway / Comment / Connected since" | the nodes connected to the reflector: online/offline |

The bridge reads the page every `map.poll_seconds` (default 30 s). It
remembers the last beacon of every gateway, because the dashboard drops old
rows. Rows of the bridge's own gateway (`tetra_fw` gateway call) are never
sent: those are TETRA users, not DMO.

## Gateways ("map settings")

Per gateway, keyed by its dashboard call (e.g. `OE1XCS-T`):

- `issi`: gateway ISSI on the map (24 bit). Empty/0 = ID from its POS beacon.
- `name`: empty = Comment from the dashboard, else the call.
- `lat`/`lon`: decimal degrees. Empty = position from its POS beacon.

With `auto_gateways` (default on) every gateway with a POS beacon is sent even
without an entry. A gateway that carries users but has neither a beacon nor an
entry is listed in the WebUI ("Eintragen"); its users are not sent, since
`via` is required. A gateway ISSI must not be the bridge's own Brew ISSI and
must not be used twice (checked).

## Transport

Agreed with the brew-server map rework
(`handover/map-rework/dmo-positions-for-brew-server.md`):

```
0xf4  0x26  <UTF-8 JSON object>  0x00      SERVICE_DMO_POSITION
```

Sent only when the server lists `dmo-position` in `X-Brew-Extensions` on the
101 of the WebSocket upgrade (`BrewClient.extensions`). TetraPack,
BrandMeister and older brew-servers never send the header, so nothing changes
for them.

```json
{"kind": "gateway", "issi": 232102, "lat": 48.1665, "lon": 16.388667,
 "name": "Tetra-Laaerberg", "online": true}
{"kind": "user", "issi": 2321109, "via": 232102, "lat": 48.148, "lon": 16.320833,
 "at_ms": 1794253200000, "name": "OE1GKS"}
{"kind": "user", "issi": 2321109, "via": 232102, "at_ms": 1794253800000, "name": "OE1GKS"}
```

When the bridge sends what:

- **gateway**: on every (re)connect, on a change (position, name, ISSI,
  online), after saving the settings, and every 10 min. A gateway removed from
  the settings is sent once with `online: false`.
- **user with lat/lon**: for every new GPS row (time, position or gateway
  changed), at most once per `user_min_interval` (default 30 s) per ISSI; a
  newer fix that has to wait goes out at a later poll. `at_ms` = time of the
  row (dashboard local time).
- **user without lat/lon**: when a user is first heard (VOICE) on a gateway
  that differs from the last one sent for that ISSI. `at_ms` here is the time
  it was heard.
- Rows older than `max_age_hours` (default 24, 0 = all) are not sent.
- After a reconnect everything is sent again (the server may have restarted),
  with the original `at_ms`.
- `name` is left out for `NO USER`.

## Without the extension

Nothing new is sent. Only with `map.lip_fallback` (default off) a user's
position goes out as a LIP short location report SDS (PID 0x0A), source = user
ISSI, destination = gateway ISSI; every brew-server decodes those and shows
the user as a normal radio. The field layout follows brew-server
`position::decode_lip`; the code values for time elapsed, position error and
reason for sending are not checked against the TS 100 392-18 text.
