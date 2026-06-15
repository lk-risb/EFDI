# goat-client v0.2 — build contract (the mesh-daemon seam)

**Status:** CONFIRMED against the released `goat-client-v0.2.0` tag (2026-06-01,
`prerelease=false`). Reconciled from the tagged source `DesertGOAT/goat-client@goat-client-v0.2.0`:
the `goat-clientd` flag set below matches exactly (`--import-bundle`, `--headless`, `--mode`,
`--bundle`, `--trust-roots`, `--socket`, `--config`), plus one new flag `--auto-connect`
(default `true`). The release ships linux-amd64/arm64 binaries (both `goat-client` + `goat-clientd`
in `goat-client-linux-<arch>.tar.gz`), cosign-signed.

Source of truth: the goat-client repo at tag `goat-client-v0.2.0` (`cmd/goat-clientd/main.go`,
`internal/mode/mode.go`, `internal/ipc/ipc.go`, `internal/bundle/bundle.go`).

## Binaries

- **`goat-clientd`** — the daemon. Runs headless as a host system service (systemd on Linux).
  Holds tunnel-management privilege. **This is what the pod runs on the host.**
- **`goat-client`** — the desktop GUI + CLI client (Fyne systray; also `getmode`/`setmode`
  subcommands). Talks to the daemon over IPC. Not used on a headless pod (the pod uses the
  daemon's one-shot import + IPC status surface directly).

## `goat-clientd` flags (from cmd/goat-clientd/main.go)

```
--bundle <path>         path to persisted CBOR bundle            (daemon.DefaultBundlePath())
--trust-roots <path>    PEM of offline-CA public keys            (daemon.DefaultTrustRootsPath())
--socket <path>         IPC endpoint (Unix socket / Win pipe)    (daemon.DefaultSocketPath())
--config <path>         config.toml (v0.2 mode selector)         (mode.DefaultConfigPath())
--mode <string>         active-mode override: wg-cp0-only|netbird-only|combined
                        (empty = use --config file)              ("")
--headless              explicit headless marker (no-op; daemon never imports a GUI)  (false)
--import-bundle <path>  one-shot: validate + persist bundle, bring up subsystems, exit 0  ("")
--auto-connect          after loading a persisted bundle, auto-bring-up the active mode's
                        subsystems (idempotent). Disable for GUI installs.            (true)
```

Verified against `goat-client-v0.2.0` (`cmd/goat-clientd/main.go`): all of the above match
exactly; `--auto-connect` is the only addition vs the pre-release contract.

Pod host service runs: `goat-clientd --headless --mode ${GOAT_CLIENT_MODE}`.
First-boot imports via: `goat-clientd --import-bundle <bundle.cbor>` (one-shot, no IPC server).

## Operating modes (internal/mode/mode.go)

Exact string values:

```go
WGCP0Only   Mode = "wg-cp0-only"   // v0.1.x default; outer tunnel only
NetbirdOnly Mode = "netbird-only"  // inner mesh only; mgmt via Block 80 public-mTLS crutch
Combined    Mode = "combined"      // both: wg-cp0 outer + inner netbird mesh
const Default = Combined           // v0.2 install default
```

Selection precedence: `--mode` flag > `config.toml` > `Combined` default. (Bundle-content
detection informs *eligibility* via `HasInnerMesh()` / `HasWgCp0()`, below.)

Pod-relevant:
- **`combined`** — most complete single-binary "join goat" shape; design names moon-pods as a consumer.
- **`netbird-only`** — inner mesh only; mgmt/signal/relay over the Block 80 public-mTLS crutch
  (uses bundle `MobileCert`). Likely the cleaner **production** partner on-ramp (no silent-control-
  plane baggage) — a production-profile decision, not a v0 one.

The pod treats mode as **profile-driven** (`profiles/<env>/profile.env` → `GOAT_CLIENT_MODE`).

## Bundle fields consumed (internal/bundle/bundle.go — CBOR EnrollmentBundle)

```go
// wg-cp0 outer tunnel:
CPDevicePubkey  []byte  `cbor:"cp_device_pubkey,omitempty"`
CPDevicePrivkey []byte  `cbor:"cp_device_privkey,omitempty"`
CPDeviceAddress string  `cbor:"cp_device_address,omitempty"`
KnownEndpoints  []KnownEndpoint `cbor:"known_endpoints"`   // kind="cp-relay" for wg-cp0

// inner mesh (v0.2 extension):
InnerMeshSetup InnerMeshSetup `cbor:"inner_mesh_setup,omitempty"`
  // .ManagementURL, .SetupKey, .AdminAccessToken, .PreSharedKey
MobileCert []byte `cbor:"mobile_cert,omitempty"`           // Block 80F per-device mTLS (netbird-only)
```

Eligibility helpers:
- `HasInnerMesh()` → true when `InnerMeshSetup.ManagementURL` **and** `.SetupKey` are non-empty
  (enables `netbird-only` / `combined`).
- `HasWgCp0()` → true when `CPDevicePubkey` + `CPDevicePrivkey` (32 bytes each) + `CPDeviceAddress`
  are present.

Signature: ECDSA-signed over **canonical CBOR** (CTAP2 shortest-int, sorted keys, definite-length);
`omitempty` keeps v0.1.x bundles byte-identical for verification. `goat_cli_extras`
(portal_endpoint/token, router_endpoint, mtls_cert/key, ca_roots) is consumed by **goat-cli**,
not goat-clientd. The pod uses **one combined bundle** carrying both mesh-enrollment + goat_cli_extras.

**CA trust comes from the bundle / `--trust-roots`, not a compiled-in pin** — a partner pod may be
rooted in a production CA, not the dev sandbox CA.

## IPC contract (internal/ipc/ipc.go) — JSON-RPC 2.0, newline-framed, uid-authed

Socket default: `$XDG_RUNTIME_DIR/goat-clientd.sock` (Linux/macOS) / `\\.\pipe\goat-clientd`
(Windows); override with `--socket`. Read-only methods open to any uid; mutating methods require
the trusted (socket-owner) uid.

Methods: `importBundle`, `getStatus`, `connect`, `disconnect`, `getDiagnostics`, `getMode`,
`setMode`; inner-mesh: `getInnerMeshStatus`, `setInnerMeshProfile`, `enableInnerMesh`,
`disableInnerMesh`, `getInnerMeshDiagnostics`; multi-profile: `listProfiles`, `addProfile`,
`removeProfile`, `renameProfile`, `setActiveProfile`, `getActiveProfile`.

## Status / health (StatusReply — the shape goat-doctor reads)

```go
type StatusReply struct {
  Mode                string    `json:"mode,omitempty"`
  State               TunnelState `json:"state"`           // no-bundle|disconnected|connecting|connected|error
  BundleLoaded        bool      `json:"bundleLoaded"`
  DeviceID            string    `json:"deviceID,omitempty"`
  Site                string    `json:"site,omitempty"`
  BundleExpiresAt     time.Time `json:"bundleExpiresAt,omitempty"`
  PeerPubkey          []byte    `json:"peerPubkey,omitempty"`
  LastHandshake       time.Time `json:"lastHandshake,omitempty"`
  BytesIn             uint64    `json:"bytesIn"`
  BytesOut            uint64    `json:"bytesOut"`
  ConfiguredEndpoints []string  `json:"configuredEndpoints,omitempty"`
  ErrorMessage        string    `json:"errorMessage,omitempty"`
  InnerMesh           *InnerMeshSnapshot `json:"innerMesh,omitempty"`  // state,peerCount,bytesIn/Out,lastHandshake
}
```

`getStatus` always returns these fields as JSON; the pod's `goat doctor` correlates this with its
own Zenoh/portal probes. **Pod isolation rule:** interact only through `--import-bundle` (first-boot)
and `getStatus` (health). If v0.2 internals shift, only the pod's thin adapter changes.

## Service install (packaging/deb-headless/systemd/goat-clientd-headless.service)

```ini
[Service]
Type=notify
ExecStart=/usr/bin/goat-clientd --headless --bundle-dir=/var/lib/goat-client \
    --ipc-socket=/run/goat-client/ipc.sock --log-file=/var/log/goat-client/goat-clientd.log \
    --config=/etc/goat-client/config.toml $GOAT_CLIENTD_FLAGS
User=goat-client
Group=goat-client
AmbientCapabilities=CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN
```

Runs as non-root `goat-client` user with **`CAP_NET_ADMIN` only** (userspace wireguard-go; no full
root). The pod's `host/goat-clientd.service.example` mirrors this, parameterized by `GOAT_CLIENT_MODE`.

> Resolved at v0.2.0: the **headless** unit
> (`packaging/deb-headless/systemd/goat-clientd-headless.service`) — the one a pod uses — uses
> `--bundle=` / `--socket=`, matching `main.go`. The `--bundle-dir` / `--ipc-socket` forms appear
> only in the **non-headless** deb/rpm/msi/dmg (GUI) units. So the pod's
> `host/goat-clientd.service.example` is correct with `--headless --mode`.

## Release binary (pin in the pod / EC2 module)

`goat-client-v0.2.0` (non-prerelease, 2026-06-01) ships
`goat-client-linux-{amd64,arm64}.tar.gz` (each contains both `goat-client` + `goat-clientd`),
cosign-signed (`.cosign-bundle` + `.sha256` per asset). Pin the matching-arch tarball URL as the
EC2 module's `goat_clientd_url`. Note: on the sandbox the pod uses **stock netbird** for the mesh (the
minted artifact is a NetBird setup-key); goat-clientd is the **production** mesh path.

## Maturity (confirmed at v0.2.0)

- Three-mode triad implemented + released; `--import-bundle` / `--headless` / `--mode` flags
  confirmed against the tag (this contract's flag table matches `main.go` exactly, plus the new
  `--auto-connect`).
- Remaining pod-side items (not contract gaps — pod integration choices):
  - `getStatus` JSON shape (for goat-doctor correlation) — the pod reads its own `goat doctor`
    today; goat-clientd `getStatus` correlation is a later enhancement.
  - On the sandbox the pod does not exercise goat-clientd (stock netbird); the production-mesh path
    (goat-clientd `netbird-only`/`combined` with an `inner_mesh_setup` bundle) is validated when
    partner-net lands.
