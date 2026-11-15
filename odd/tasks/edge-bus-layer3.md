# Layer 3 — Edge Bus (WebSocket + telemetry + recalculation)

Route: inline C++20/POSIX under `communication/`. No git operations and no new
third-party dependencies. The server uses one nonblocking `poll()` loop rather
than worker threads; layer 1 and layer 2 behavior remain unchanged.

## Specs

S1. User: "requiero que implementes ahora la tercera capa". Layer 3 connects
the layer 2 JSON engine to native clients through the Edge Bus.

S2. Transport selected by the user: "WebSocket binario (recomendado)". Binary
telemetry is compatible with Flutter WebSocket clients; control and HELLO are
separate typed JSON WebSocket messages.

S3. Telemetry frame v1 is exactly 24 bytes, little-endian: magic `M`, version
1, seq u16, mission t_ms u32, pos_x/pos_y u16 metres, alt_dm i16, oxygen and
battery u8 in 0.5%, radiation u16 in 0.01 mSv, TTR u16 seconds, flags u8,
reserved u8 and CRC16-CCITT-FALSE over bytes 0..21. No floats or packed C++
structs are serialized. Quantization clamps; unknown fields use their all-ones
sentinel.

S4. HELLO carries the layer 1 AOI georeferencing and the altitude datum. The
default publication cadence is 10 Hz. Storm and radiation-peak controls set
their alert bits and force an evacuation route back to the refuge; position and
goal controls also recalculate through the public `eva_engine sim` CLI.
ROUTE_STALE is set while recalculation is in progress and cleared on success.
COMMS_LOST is an external event bit. Layer 2 remains the source of PLSS alerts.

S5. No fabricated data. The bus executes the existing engine and reports its
actual JSON result. Failed recalculations are surfaced as errors and leave
ROUTE_STALE set. Radiation remains unavailable when layer 1 has no radiation
grid. No WebSocket shared-memory browser API is claimed for native Flutter.

## Tasks

| ID | Task | Specs | Route | Status |
|----|------|-------|-------|--------|
| T1 | Freeze protocol docs and implement the 24-byte codec | S3 | inline | done |
| T2 | Engine adapter and mission/event state with recalculation | S1, S4, S5 | inline | done |
| T3 | Nonblocking WebSocket server, HELLO, 10 Hz broadcast and CLI | S1, S2, S4 | inline | done |
| T4 | Codec unit tests and end-to-end WebSocket/event tests | S2-S5 | inline | blocked: independent verification failed/timeout; human decision required |
| T5 | Run against the real AOI and record observed behavior | S5 | inline | deferred at user handoff; not run |

## Log

L1. "requiero que implementes ahora la tercera capa"

L2. User selected "WebSocket binario (recomendado)" for the local Edge Bus.

L3. "continua"

L4. "continua"

L5. Build and live transport: existing engine JSON parser compiled locally in
communication/build (no core_engine edits); asynchronous bounded engine child,
loopback-only socket with native Android access via adb reverse. Maximum eight
clients, bounded messages/queues and handshake timeout. No browser Origin.

L6. Checks: 37 codec + 33 mission + 19 WebSocket checks and 8 E2E tests pass.
First combined run timed out at 30s because the test confused the reserved-byte
index with the flags index. No orphan processes were left. Corrected the test
and added bounded waits. No dropped or skipped failures are claimed as passes.

L7. PLSS values are explicitly planned projections from eva_engine sim, not
live suit measurements or cumulative mission consumption. HELLO labels this as
telemetry_kind=planned_projection. Positions come from accepted controls. Dose
scale stays 0.01 mSv (small demonstration doses can quantize to zero).

L8. Independent verifier preparation was rejected because its risk declaration
was not in the required machine-readable form. Zero verifier tests ran and no
child was created. No automatic retry was performed. Inline checks pass 40
codec + 33 mission + 19 WebSocket assertions and 8 E2E tests.

L9. User explicitly selected "Sí, verificar y cerrar la capa 3": one NEW
bounded independent verification attempt, at most one correction round limited
to communication/ and its docs, then checks and real-AOI demonstration. No git
or layer 1/2 behavior changes. Child inventory confirms only the completed
explorer exists; no child is active or uncertain before the new attempt.

Risk: item 2 (untrusted WebSocket input and local control/process boundaries).

L10. New user-authorized verifier task mwh43jgm-2-zhvt failed with
"assistant reported an error (timeout)" after 21 turns / 30 tool calls. No
verdict or reliable returned test evidence was received. Task inventory marks
it failed, with no active child. Do NOT automatically retry or reinterpret this
as a pass. The one authorized correction round has not been used. Real-AOI bus
demonstration is not run; layer 3 is implemented but NOT verification-closed.

L11. User requested handoff: "dame un resumen rapido para pasarlo a otra IA".
Stop implementation/checks and hand off the current state and proof limits.
