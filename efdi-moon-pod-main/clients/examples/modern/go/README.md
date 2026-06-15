# Go — modern

Idiomatic Go against the pod. Uses the **official** Zenoh Go binding
[`github.com/eclipse-zenoh/zenoh-go`](https://github.com/eclipse-zenoh/zenoh-go) (the `zenoh`
package) plus the [`connect/go/goatconnect.go`](../../../connect/go/goatconnect.go) helper.

## Read this first: the binding is a cgo wrapper over zenoh-c

The official Go binding landed with **Zenoh 1.9.x "Longwang" (April 2026)**. It is **not pure
Go** — it wraps [`zenoh-c`](https://github.com/eclipse-zenoh/zenoh-c) through cgo. That means:

- You must install **zenoh-c 1.9.0** (built with the unstable API:
  `-DZENOHC_BUILD_WITH_UNSTABLE_API=ON`) before anything will compile or link.
- `CGO_ENABLED=1` and a C toolchain are required. Cross-compiling is awkward (it's cgo).
- The import path is `github.com/eclipse-zenoh/zenoh-go/zenoh` — note the trailing `/zenoh`
  package. (The old `github.com/eclipse-zenoh/zenoh-go` top-level / `zenoh-net` API is the
  abandoned 0.4.x binding — do **not** use it; last real commit was 2020.)

If you need a pure-Go, no-cgo path, there isn't an official one today; use the HTTP/MQTT/Kafka
bridge in `examples/bridges/` from a pure-Go app instead.

## Install zenoh-c (the C library this binding links)

```sh
git clone https://github.com/eclipse-zenoh/zenoh-c
cd zenoh-c && git checkout 1.9.0
cmake -B build -DCMAKE_BUILD_TYPE=Release -DZENOHC_BUILD_WITH_UNSTABLE_API=ON
cmake --build build --config Release
sudo cmake --install build       # installs headers + libzenohc to /usr/local
sudo ldconfig                    # Linux: refresh the linker cache
```

If you install somewhere other than `/usr/local`, point cgo at it:

```sh
export CGO_CFLAGS="-I/path/to/zenoh-c/include"
export CGO_LDFLAGS="-L/path/to/zenoh-c/lib -lzenohc"
export LD_LIBRARY_PATH="/path/to/zenoh-c/lib:$LD_LIBRARY_PATH"   # macOS: DYLD_LIBRARY_PATH
```

## Setup

```sh
go mod download
# export GOAT_* per clients/README.md
export GOAT_ROUTER="tls/127.0.0.1:7447"
export GOAT_CERT="$HOME/.goat/contexts/default/mtls.cert.pem"
export GOAT_KEY="$HOME/.goat/contexts/default/mtls.key.pem"
export GOAT_CA="$HOME/.goat/contexts/default/ca-roots.pem"
export GOAT_NAMESPACE="release/acme"
# GOAT_VERIFY_NAME defaults to false (local pod at 127.0.0.1); set true for a DNS-named router.
```

## Run

`publish.go` and `subscribe.go` are each a standalone `main` (tagged `//go:build ignore`) so
they live in one directory without two `main()`s colliding. Run them with `go run <file>`:

```sh
go run publish.go                 # publish one JSON sample to <namespace>/sensors/temp
go run publish.go 50 0.2          # 50 samples, 200ms apart
go run subscribe.go               # receive everything under your namespace
go run subscribe.go 'release/goat/**'   # inbound data from goat
go run subscribe.go '<keyexpr>' 5              # stop after 5 samples
```

**Quick round-trip:** run `go run subscribe.go` in one terminal, `go run publish.go 10 0.5` in
another. You should see the JSON samples arrive in the subscriber.

## The one connection gotcha (read this — it bites everyone)

Zenoh's TLS config must be inserted as **one whole json5 block** at `transport/link/tls`, with
**`enable_mtls: true`**. Setting the sub-keys one at a time
(`transport/link/tls/connect_certificate`, etc.) silently does **not** turn on the client-cert
send path on Zenoh 1.x — your session opens but the router rejects you, or you connect
read-only. The helper does it the working way: it marshals the whole block
(`root_ca_certificate` / `connect_certificate` / `connect_private_key` / `enable_mtls` /
`verify_name_on_connect`) and calls `Config.InsertJson5("transport/link/tls", <block>)` once.

When the router cert's SAN binds an **IP/mesh address** rather than the DNS name you dial, set
`GOAT_VERIFY_NAME=false` (the default). A DNS-named remote router can use `true`.

## Notes

- Payloads here are JSON for readability; send any bytes via `zenoh.NewZBytes([]byte{...})`.
  The fabric is payload-agnostic — a registered topic's `format` is a tooling hint, not enforced.
- Read a sample's bytes with `sample.Payload().Bytes()` (or `.String()` for text); the key with
  `sample.KeyExpr().String()`.
- Subscriber delivery is a callback (`zenoh.Closure[zenoh.Sample]{Call: ...}`) that runs on a
  zenoh worker thread — guard shared state (the example uses `sync/atomic`).
- Always `defer session.Drop()` / `pub.Drop()` / `sub.Drop()`; the binding owns C resources and
  relies on explicit drops (finalizers are a backstop, not a guarantee of timely release).

## Caveats / binding maturity

- The binding is new (1.9.x) and cgo-only. Build friction is mostly "is zenoh-c findable by the
  linker." If `go build` fails with `zenoh.h: No such file` or `cannot find -lzenohc`, fix
  `CGO_CFLAGS`/`CGO_LDFLAGS`/`LD_LIBRARY_PATH` above — it is not an API problem.
- Method names here (`NewConfigDefault`, `InsertJson5`, `Open`, `DeclarePublisher`, `Put`,
  `DeclareSubscriber`, `NewKeyExpr`, `NewZBytes`, `Payload().Bytes()`, `Drop`) match the
  `zenoh-go` v1.9.0 source. If you pin a different minor, re-check against that tag's
  `examples/z_pub` and `examples/z_sub`.
