# Edge Bus protocol — telemetry frame v1

Layer 3 uses a local binary WebSocket server. Telemetry, typed JSON control and
JSON metadata are different WebSocket messages: control bytes are never packed
into the telemetry frame. Native Flutter uses WebSocket directly;
`SharedArrayBuffer` is a browser API and is not part of this transport.

## Binary telemetry

The server sends one **binary WebSocket message containing exactly 24 bytes**
every 100 ms (10 Hz). All integers are little-endian and serialized byte by
byte. C++ struct layout, floats and host endianness are never part of the wire
format.

| Offset | Size | Field | Encoding |
|-------:|-----:|-------|----------|
| 0 | 1 | magic | ASCII `M`, `0x4D` |
| 1 | 1 | version | `0x01` |
| 2 | 2 | seq | Sequence number, modulo 65536 |
| 4 | 4 | t_ms | Monotonic mission/bus elapsed milliseconds |
| 8 | 2 | pos_x | AOI-local x metres, rounded |
| 10 | 2 | pos_y | AOI-local y metres, rounded, increasing south |
| 12 | 2 | alt_dm | Signed decimetres relative to HELLO `altitude_origin_m` |
| 14 | 1 | o2 | Oxygen fraction in units of 0.5%, valid 0..200 |
| 15 | 1 | batt | Battery fraction in units of 0.5%, valid 0..200 |
| 16 | 2 | rad_cs | Cumulative dose in units of **0.01 mSv** |
| 18 | 2 | ttr_s | Time to return in seconds |
| 20 | 1 | flags | Alert bitfield below |
| 21 | 1 | reserved | Zero |
| 22 | 2 | crc16 | CRC16-CCITT-FALSE over offsets 0..21 |

Unknown unsigned fields use their all-ones sentinel (`0xFF` or `0xFFFF`).
Finite quantization clamps and never wraps. To preserve `0xFFFF` as unknown,
valid u16 quantities saturate at 65534. Oxygen and battery saturate at 200.
Altitude saturates at the i16 limits. The short real-AOI demonstration dose is
below 0.01 mSv and can legitimately quantize to zero; the field is not rescaled
to hide that fact.

CRC16-CCITT-FALSE parameters: polynomial `0x1021`, initial value `0xFFFF`, no
input/output reflection, xorout zero. Known answer: ASCII `123456789` produces
`0x29B1`. Receivers reject wrong length, magic, version, CRC or nonzero reserved
bits.

## Alert bits

| Bit | Name | Producer |
|----:|------|----------|
| 0 | STORM | Layer 3 storm control |
| 1 | RAD_PEAK | Layer 2 dose or layer 3 radiation-peak control |
| 2 | O2_LOW | Layer 2 PLSS |
| 3 | BATT_LOW | Layer 2 PLSS |
| 4 | COMMS_LOST | Layer 3 external event |
| 5 | ROUTE_STALE | Layer 3 while recalculating or after recalculation failure |
| 6 | EVA_ABORT | Layer 2 PLSS/route feasibility |
| 7 | reserved | Always zero |

Clients render flags; they do not derive their own alert decisions.

PLSS fields are explicitly model projections, not measured suit sensors. Layer
3 accumulates consumption between accepted positions; each new layer 2 plan
starts with those remaining resources. Oxygen/battery are projected at the
objective and encoded relative to the capacities at EVA start. Dose combines
the accumulated movement dose with the planned round trip, and TTR uses the
engine's inbound-leg convention. During evacuation the outbound leg
is the route to the refuge; use `outbound.sim.time_s` from the JSON plan as its
ETA. Repeated 10 Hz publication does not itself consume resources.

## JSON messages

Each JSON value is one complete text WebSocket message.

### Server HELLO

Sent immediately after the WebSocket upgrade:

```json
{
  "type": "hello",
  "protocol": "mars-eva-edge/1",
  "frame_bytes": 24,
  "telemetry_hz": 10,
  "telemetry_kind": "planned_projection",
  "altitude_origin_m": -3600.0,
  "engine": {"ok": true, "aoi": {"format": "mars-eva-aoi/1"}}
}
```

`engine` is the unmodified `eva_engine info` result, including grid shape,
cell size and `bbox_lonlat`. Row zero is the north edge; x is
`col * cell_size_m.dx`, y is `row * cell_size_m.dy`. Altitude is relative to the
initial refuge elevation, reported as `altitude_origin_m`.

### Server route update

```json
{"type":"route_update","cause":"initial","revision":1,"resources":{"o2_remaining_kg":1.2,"battery_remaining_wh":250,"cumulative_dose_msv":0,"projected_o2_fraction_of_initial":0.98,"projected_battery_fraction_of_initial":0.97},"result":{"ok":true,"found":true}}
```

`result` is the complete unmodified output of `eva_engine sim ... --path`.
`resources` is layer 3's persistent EVA ledger. Its projected fractions use the
capacities at EVA start, unlike the engine result whose capacity arguments are
the resources remaining when that particular calculation begins.
Causes include `initial`, `position`, `set_goal`, `storm`, `storm_clear`,
`radiation_peak` and `radiation_peak_clear`. Server errors use
`{"type":"error","error":"...","revision":1,"position":{"row":500,"col":500}}`.
Revision increments only when a new engine result is successfully admitted.
Rejected controls leave the mission and revision unchanged.

### Client controls

```json
{"type":"position","row":500,"col":500}
{"type":"set_goal","row":600,"col":650}
{"type":"storm","active":true}
{"type":"radiation_peak","active":true}
{"type":"comms_lost","active":false}
```

Position and goal updates recalculate the current mission. Storm or radiation
peak sets its alert bit, marks the route stale and recalculates an evacuation
from the current position to the initial refuge. Clearing an emergency restores
the selected goal when no other emergency remains. `comms_lost` only changes
its alert bit. Every accepted control receives an `event_ack` message.
New accepted route commands are coalesced without killing the engine process in
flight. Obsolete results are discarded and the newest state is calculated next.
The last good plan remains available while recalculating or after a failure;
stale flags tell the client that its projections may not correspond to the
current position.

## Process boundary

The bus invokes the existing layer 2 CLI directly with argument arrays, not a
shell command. A failed engine execution emits an error and leaves ROUTE_STALE
set. The child has a 5-second timeout and 8 MiB output cap, while the socket
reactor continues broadcasting telemetry. Layer 1 and layer 2 artifacts are not
modified by the bus.

The server binds `127.0.0.1` only and rejects browser Origin headers. Android
native clients can use per-device `adb reverse tcp:8765 tcp:8765`. No TLS,
authentication, external-network exposure or Flutter client is implemented by
this layer. See `../communication/README.md` for the launch commands and limits.
