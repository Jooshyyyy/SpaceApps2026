# Run the Mars EVA Edge Bus

`eva_bus` connects the layer 2 engine to native tablet/watch clients using a
local WebSocket: JSON metadata and controls, plus 24-byte binary telemetry at
10 Hz. It is C++20/POSIX (Linux), with no third-party libraries or threads.

## Quick start

From the project root:

```sh
make -C core_engine
make -C communication
./communication/eva_bus --dir data/processed --engine ./core_engine/eva_engine \
  --start 500,500 --goal 600,650 --port 8765
```

For a self-running accelerated demonstration, add `--demo-step-ms 250`. The
bus then advances one route cell every 250 ms. Omitting the option (or using
zero) keeps production behavior: positions must come from the client.

Startup prints a JSON `listening` event on stdout. Connect a **native** client to
`ws://127.0.0.1:8765/`. For an Android device connected by USB:

```sh
adb -s DEVICE_SERIAL reverse tcp:8765 tcp:8765
```

Use that device's loopback URL above. Forward each device separately. Actual
Android/Flutter hardware integration is not implemented or claimed tested here.
The bus binds only loopback and rejects browser `Origin` headers; there is no
network authentication or TLS. It must not be exposed as a public service.
There is no SharedArrayBuffer requirement on native Flutter.

## What clients receive

1. Text `hello`: AOI metadata from `eva_engine info`, cell size/bbox, altitude
   datum, protocol version and `telemetry_kind: "planned_projection"`. After
   the environment ETL it also carries the RAD baseline, temperature, pressure,
   mean thermal inertia and terrain-traction classification.
2. Text `route_update`: the unmodified `eva_engine sim --path` result, the
   control cause, and a successful-plan revision counter (initially `1`).
3. Binary frames at 10 Hz, exactly as specified in
   [`../docs/telemetry-frame.md`](../docs/telemetry-frame.md).

**Simulation, not live suit sensors:** oxygen, battery, dose and TTR are model
outputs rather than suit measurements. Layer 3 accumulates the resources spent
between accepted positions and passes what remains into every new layer 2
projection. Oxygen/battery are projected remaining at the objective, expressed
against the capacities at EVA start; dose combines accumulated travel dose and
the current round-trip projection. TTR is the engine's inbound-leg estimate.
Positions normally come from explicit controls; only `--demo-step-ms` enables
synthetic route following. Repeated 10 Hz publications do not consume tanks.
High-precision dose remains present in `route_update` JSON even when a short
walk rounds to zero in the frozen binary field's 0.01 mSv units.
When an emergency targets the refuge, the outbound path is the evacuation path;
its `outbound.sim.time_s` is the evacuation ETA. The frame's TTR retains the
engine's inbound-leg convention, and is not relabeled as a live evacuation ETA.

The altitude datum is the initial refuge elevation because this AOI's absolute
elevations do not fit an i16 decimetre field. `rad_cs` retains the frozen unit
of 0.01 mSv; very small projected doses legitimately quantize to zero.

## Send events and position updates

Send each JSON object as a single, complete, masked text WebSocket message:

```json
{"type":"position","row":510,"col":520}
{"type":"set_goal","row":600,"col":650}
{"type":"storm","active":true}
{"type":"radiation_peak","active":true}
{"type":"comms_lost","active":false}
```

- Position/goal use integer AOI row/col, not latitude/longitude or frame metres.
- Storm/radiation peak switches the outbound goal to the initial refuge.
  Clearing both restores the selected goal. Events are explicit simulations,
  not weather/radiation measurements inferred from absent data.
- `comms_lost` is an externally supplied status bit. One client disconnecting
  does not imply the entire mission link is lost.
- A newer accepted route command does not terminate the calculation in flight.
  Its result is discarded if stale, then only the newest requested state is
  calculated. This prevents continuous position updates from starving routing.
- Accepted controls get `event_ack`. All connected clients get successful
  `route_update` messages. Unknown/out-of-range/malformed controls return
  `error` without changing mission state, stored grids or the plan revision.
- While the engine is busy or failed, `ROUTE_STALE` is set; the most recent good
  projections remain available. A failure emits `error` and never clears stale.
  Reconnecting clients still get the last good plan; its revision identifies
  which projection it belongs to. Position/flags in current frames may be newer.

## Limits and failure behavior

| Limit | Value / behavior |
|-------|------------------|
| Clients | 8 maximum |
| Handshake | 8 KiB headers, 3-second deadline |
| Client message | 4096 bytes, no fragments/extensions/binary controls |
| JSON nesting | 16 maximum |
| Client rate | 32 WebSocket messages per second |
| Input buffer | 16 KiB per client |
| Output queue | 16 MiB per client; overflow disconnects slow client |
| Engine output | 8 MiB maximum, 5-second timeout |
| Engine process | Direct argv execution, no shell; close-on-exec descriptors |
| Shutdown | SIGINT/SIGTERM; clients close and pending engine is reaped |

WebSocket supports the RFC 6455 upgrade, server text/binary output, masked text
input, ping/pong and close. Unsupported frames cause disconnection. The SHA-1
implementation is only for the standard handshake, not for authentication.
Startup/argument failures print JSON to stderr and exit `2`. `--help` lists all
options; `--port 0` chooses an available port for isolated tests.

## Verification

```sh
make -C communication tests
```

C++ checks pin the CRC known answer, golden 24-byte vector, quantization,
mission events and WebSocket parser. Python stdlib E2E tests use a synthetic AOI
and the real `eva_engine` executable. A wrapper adds deterministic delay/hang
cases to test continuous telemetry and the engine timeout. Test scratch data is
isolated in temporary directories; the real processed AOI is not modified.

Review order: `docs/telemetry-frame.md`, `include/comms/frame.hpp`,
`src/mission.cpp`, `src/engine_adapter.cpp`, `src/websocket.cpp`, `src/main.cpp`,
then tests. No layer 1/2 behavior is changed and no git operations are required.
