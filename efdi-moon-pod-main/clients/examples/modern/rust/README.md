# Rust — modern

Idiomatic async Rust against the pod. Uses the official [`zenoh`](https://crates.io/crates/zenoh)
crate (1.x) plus the [`connect/rust`](../../../connect/rust) helper crate (`goat_connect`).

Unlike the Go binding, the Rust crate is **pure Rust** (it *is* the reference implementation —
no C library to install). The 1.x API is **async** (tokio).

## Setup

```sh
# Rust toolchain (stable). https://rustup.rs
# export GOAT_* per clients/README.md
export GOAT_ROUTER="tls/127.0.0.1:7447"
export GOAT_CERT="$HOME/.goat/contexts/default/mtls.cert.pem"
export GOAT_KEY="$HOME/.goat/contexts/default/mtls.key.pem"
export GOAT_CA="$HOME/.goat/contexts/default/ca-roots.pem"
export GOAT_NAMESPACE="release/acme"
# GOAT_VERIFY_NAME defaults to false (local pod at 127.0.0.1); set true for a DNS-named router.

cargo build        # pulls zenoh =1.9.0 + tokio; first build is slow
```

## Run

```sh
cargo run --bin publish                 # publish one JSON sample to <namespace>/sensors/temp
cargo run --bin publish -- 50 0.2       # 50 samples, 200ms apart
cargo run --bin subscribe               # receive everything under your namespace
cargo run --bin subscribe -- 'release/goat/**'   # inbound data from goat
cargo run --bin subscribe -- '<keyexpr>' 5              # stop after 5 samples
```

**Quick round-trip:** run `cargo run --bin subscribe` in one terminal, `cargo run --bin publish
-- 10 0.5` in another. The JSON samples should arrive in the subscriber.

(For release builds add `--release`; the args after `--` go to the program.)

## The one connection gotcha (read this — it bites everyone)

Zenoh's TLS config must be inserted as **one whole json5 block** at `transport/link/tls`, with
**`enable_mtls: true`**. Setting the sub-keys one at a time
(`transport/link/tls/connect_certificate`, etc.) silently does **not** turn on the client-cert
send path on Zenoh 1.x — your session opens but the router rejects you, or you connect
read-only. The `goat_connect` helper does it the working way: it serializes the whole block
(`root_ca_certificate` / `connect_certificate` / `connect_private_key` / `enable_mtls` /
`verify_name_on_connect`) and calls `config.insert_json5("transport/link/tls", &block)` once.

When the router cert's SAN binds an **IP/mesh address** rather than the DNS name you dial, set
`GOAT_VERIFY_NAME=false` (the default). A DNS-named remote router can use `true`.

## Notes

- Payloads here are JSON for readability; `publisher.put(...)` accepts anything that converts to
  `ZBytes` (`&str`, `String`, `Vec<u8>`, `&[u8]`). The fabric is payload-agnostic — a registered
  topic's `format` is a tooling hint, not enforced.
- Read a sample with `sample.payload().try_to_string()` (text) or `.to_bytes()` (raw); the key
  with `sample.key_expr().as_str()`.
- The default subscriber handler is a FIFO channel; the example drives it with
  `subscriber.recv_async().await` inside `tokio::select!` so Ctrl-C cleanly stops the loop.
- `goat_connect` also exposes `session_blocking()` (via zenoh's `Wait` adapter) for callers
  without a tokio runtime, but the examples use the async `session()`.

## Caveats

- TLS is provided by zenoh's rustls-based transport; no system OpenSSL needed.
- Method names here (`Config::default`, `insert_json5`, `zenoh::open`, `declare_publisher`,
  `put`, `declare_subscriber`, `recv_async`, `payload().try_to_string()`, `key_expr().as_str()`,
  `init_log_from_env_or`) match the `zenoh` 1.9.0 docs. If you bump the minor, re-check against
  that version's docs.rs.
