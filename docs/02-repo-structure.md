# 02 — Repo Structure

What lives where, and who owns it. If you're hunting for "where does X
happen," start here before grepping blind.

## Top level

| Path | What it is |
| --- | --- |
| `install.sh` | The installer — bare host to running pod, one command. See [03](03-bootstrap-and-install.md). |
| `start.sh` | Interactive service launcher — picks which bridges/layers run, wires ports, starts them. |
| `stop.sh` | Tears down whatever `start.sh`/`run.sh` started. |
| `run.sh` | Starts EFDI bridges as background scripts without Docker (Zenoh router still runs via Docker). |
| `update.sh` | Fast-forward update with cache verification and automatic recovery. |
| `reinstall.sh` | Removes local images/containers, keeps certs and data — a clean rebuild without losing identity. |
| `health.sh` | Self-heals the deployment, then runs every repository test and static check. |
| `dev.sh` | Disposable local MariaDB + env for previewing zenoh-admin panel changes — admin panel only, no fabric. |
| `compose/` | The actual pod: bridges, layers, protocol translators, the admin panel, Docker Compose. |
| `clients/` | Connect SDKs and worked examples partners build against. |
| `examples/` | Standalone runnable snippets (first publisher/subscriber, liveliness, resilient subscriber, etc.) and `first-boot.sh`. |
| `host/` | Host-level Zenoh router config template. |
| `scripts/` | One-off operational scripts — cert generation, protobuf codegen, radar UDP capture/relay, host detection. |
| `tools/` | ASTERIX probe/relay utilities for debugging a live feed. |
| `tests/` | The full test suite — unit tests, smoke tests, security checks, CI-image-public checks. |
| `docs/` | Everything you're reading now — see [00](00-start-here.md) for the map. |

## `compose/` — the pod itself

| Path | What it is |
| --- | --- |
| `docker-compose.yml` | Zenoh router + zenoh-admin containers — the only two containerized components. |
| `.env.example` | Configuration template; the real `.env` is gitignored and holds per-deployment secrets/identity. |
| `certs/` *(gitignored)* | Router mTLS certificates. |
| `state/` *(gitignored)* | Runtime state — logs, PIDs, generated Zenoh router config. |
| `venv/` *(gitignored)* | Python virtual environment `start.sh`/`install.sh` create on first run. |
| `generated/` *(gitignored)* | Compiled protobuf bindings (`*_pb2.py`) — build output of `scripts/generate-protobuf.sh`. |
| `control/` | Shared helpers + host control plane: `supervisor.py` (keeps native processes alive), `admin_control.py` (the admin API's backend logic), `gateway.py`, `namespace_prefix.py`, `zenoh_auth.py`, `presence.py`, `http_json.py`. |
| `bridges/` | Ingress: pulls data from a source (sensor feed, C2 system, generic protocol) onto the fabric. Naming convention below. |
| `layers/` | Egress: pushes fused fabric data out to a C2 system (`tak_layer.py`, `sitaware_layer.py`). |
| `protocols/` | Protocol translators and their `.proto` contracts — decode/encode logic, mostly Zenoh-independent (see [08](08-integrations.md)). |
| `zenoh-admin/` | The FastAPI + React admin panel — `api/` (backend), `ui/` (frontend), its own `Dockerfile`/`Caddyfile`. |

### `compose/bridges/` and `compose/layers/` naming convention

The prefix on a bridge/layer script is direction, not the vendor: a
**`_bridge`** brings an external system *into* the fabric; a **`_layer`**
writes the fabric *out* to a C2 system. Which side opens the network
connection is irrelevant to the name — e.g. SitaWare HQ polling
`sitaware_layer`'s served feed is still egress, so it's a `_layer` even
though HQ is the one initiating the HTTP request.

| Script | Direction |
| --- | --- |
| `tak_bridge.py`, `sitaware_bridge.py` | C2 → Zenoh |
| `tak_layer.py`, `sitaware_layer.py` | Zenoh → C2 |
| `asterix_bridge.py`, `dronuradaras_bridge.py`, `flex335_bridge.py`, `udp_ingress_bridge.py`, `mqtt_bridge.py`, `sensorthings_bridge.py`, `raw_socket_bridge.py`, `meteolt_forecast_bridge.py`, `4586_bridge.py`, `4609_bridge.py`, `5516_bridge.py` | Source → Zenoh |

### `compose/protocols/` layout

| Path | What it is |
| --- | --- |
| `gateway.py` | The only module in this tree that imports `zenoh` directly — every translator gets a Zenoh session through here, keeping the transport swappable in one place. |
| `fusion.py` | Multi-source track correlation (ASTERIX CAT-48/CAT-21). |
| `data_stats.py` | Byte/message counters surfaced in the zenoh-admin dashboard. |
| `track_views.py` | The four-encoding publish helper (`/sapient`, `/json`, `/proto`, `/raw`). |
| `process_bundle.py` | Shared inbound-process bootstrap for translator CLIs. |
| `vendors/asterix/`, `vendors/sapient/`, `vendors/stanag/`, `vendors/sparkplug/` | The ASTERIX (`cat.py`), SAPIENT (`flex335.py`), STANAG (`stanag.py`), and Sparkplug B translators. |
| `random/` | Vendor-neutral translators: CAP 1.2, GeoJSON/OGC Features, mission routes, MQTT-JSON, NFFI, sensor health, RF spectrum observations, OGC SensorThings. |
| `proto/` | The `.proto` contracts, one per translator family. |

## `docs/` — this directory

Numbered docs are the operator manual, read in roughly that order; see
[00-start-here.md](00-start-here.md) for the full map. `references/`
holds source-and-trust-assessment notes for each external spec
(ASTERIX, SAPIENT, STANAG, TAK, SitaWare) this repo implements against.
`superpowers/` is the AI-assisted design/planning archive (specs and
dated plans) — development history, not operator documentation.

## Tests

`tests/` mirrors the pod's shape: one `test_*.py` per bridge/protocol/
translator, plus `smoke/` (end-to-end round-trip), `security/`
(ACL/auth checks), and `check-images-public.sh` /
`check_service_paths.py` (CI-only sanity checks, not unit tests).
