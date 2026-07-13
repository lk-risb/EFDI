# C99 — military / legacy

Pure **C99** against the pod, for older or constrained toolchains: just libc + `libzenohc`, a
`Makefile` (no CMake required), static-link guidance, and an **offline / air-gapped** build path.
Each example is a single self-contained `.c` (the connect logic is inlined as `efdi_config()` —
no extra header to ship), so you can drop one file plus `libzenohc` onto a target and build it.

Uses [`zenoh-c`](https://github.com/eclipse-zenoh/zenoh-c) **1.9.0** — the C library that the
Go/C++/Python bindings also wrap.

## Dependency: zenoh-c 1.9.0

You need `zenoh.h` + `libzenohc` (the shared `.so`/`.dylib` and/or the static `libzenohc.a`).
zenoh-c is a Rust library exposed as a C ABI, so building from source needs Rust/Cargo + CMake;
an air-gapped host can instead consume a **prebuilt** or **vendored** copy (below).

### A) Build from source (has internet, or a populated Cargo cache)

```sh
git clone https://github.com/eclipse-zenoh/zenoh-c
cd zenoh-c && git checkout 1.9.0
cmake -B build -DCMAKE_BUILD_TYPE=Release \
      -DZENOHC_BUILD_WITH_UNSTABLE_API=ON     # REQUIRED — see note below
cmake --build build --config Release
sudo cmake --install build                    # zenoh.h + libzenohc -> /usr/local
sudo ldconfig                                 # Linux: refresh the linker cache
```

> **`-DZENOHC_BUILD_WITH_UNSTABLE_API=ON` is required.** The json5 config entry point these
> examples use (`zc_config_from_str`) — the only way to inject the one-block mTLS config — is
> gated behind the unstable API. Build without it and the symbol won't be in `libzenohc`, and
> the link will fail with `undefined reference to zc_config_from_str`.

### B) Prebuilt release artifact (download once, carry in)

The zenoh project publishes prebuilt `zenoh-c` packages per release (Debian `.deb`,
`.tar.gz` with `include/` + `lib/`) on the GitHub Releases page for tag `1.9.0`. On a machine
**with** internet, download the artifact for your target arch/OS, verify its checksum, then move
the tarball/`.deb` to the air-gapped host and unpack it (e.g. to `/opt/zenoh-c`). Point the
build at it: `make ZENOHC=/opt/zenoh-c`.

### C) Vendor it (fully offline build from source)

On a connected machine, do a one-time vendoring so the air-gapped host needs no network:

```sh
git clone https://github.com/eclipse-zenoh/zenoh-c && cd zenoh-c && git checkout 1.9.0
cargo vendor ../zenoh-c-vendor > ../cargo-config.toml   # fetches all crate deps locally
```

Copy the whole `zenoh-c/` tree + `zenoh-c-vendor/` to the target, drop `cargo-config.toml` into
`zenoh-c/.cargo/config.toml`, and run the same `cmake` build offline (Cargo reads the vendored
crates instead of crates.io). Then `cmake --install` as in (A).

## Build the examples

```sh
make                        # dynamic link against libzenohc in /usr/local
make ZENOHC=/opt/zenoh-c    # if installed to a non-default prefix
make static                 # static-link libzenohc.a -> self-contained binaries
```

- **Dynamic** (default): smaller binaries; `libzenohc.{so,dylib}` must be present and on the
  loader path at runtime (`LD_LIBRARY_PATH` / `DYLD_LIBRARY_PATH`, or installed system-wide).
- **Static** (`make static`): links `libzenohc.a` into each binary so you can drop a single file
  onto an air-gapped host with no shared lib at runtime. On Linux the Makefile adds
  `-lpthread -ldl -lm`; on **macOS** static-linking a Rust archive also needs
  `-framework Security -framework CoreFoundation` (see the commented recipe in the Makefile).

## Setup (env)

```sh
export EFDI_ROUTER="tls/127.0.0.1:7447"
export EFDI_CERT="$HOME/efdi-certs/mtls-cert.pem"
export EFDI_KEY="$HOME/efdi-certs/mtls-key.pem"
export EFDI_CA="$HOME/efdi-certs/ca-root.pem"
export PARTNER_NAMESPACE="release/acme"
# EFDI_VERIFY_NAME defaults to false (local pod at 127.0.0.1); set true for a DNS-named router.
```

## Run — the round-trip

```sh
./publish               # publish one JSON sample to <namespace>/sensors/temp
./publish 50 200        # 50 samples, 200ms apart (interval is MILLISECONDS in C)
./subscribe             # receive everything under your namespace (<ns>/**)
./subscribe 'release/<partner>/**'   # inbound data from a partner
./subscribe '<keyexpr>' 5              # stop after 5 samples
```

**Quick round-trip:** run `./subscribe` in one terminal, `./publish 10 500` in another. The JSON
samples should arrive in the subscriber.

## Clock sync (TLS fails on a skewed clock)

mTLS validates certificate **validity windows** (`notBefore`/`notAfter`). If the host clock is
off by more than the cert's skew tolerance, the handshake fails with a confusing error and
`z_open` returns non-zero — even when certs and paths are correct. On a legacy/air-gapped box
with no NTP, **set the clock before connecting** (e.g. `sudo date -u -s 'YYYY-MM-DD HH:MM:SS'`,
or sync to the pod host / a local NTS source). This is the single most common "my certs are fine
but it won't connect" cause on offline hardware.

## The one connection gotcha (read this — it bites everyone)

Zenoh's TLS config must be one **whole json5 block** at `transport/link/tls` with
**`enable_mtls: true`**. Setting the sub-keys one at a time silently does **not** turn on the
client-cert send path on Zenoh 1.x — the session opens but the router rejects you, or you connect
read-only. `efdi_config()` in each `.c` builds the **entire** config (mode + connect endpoint +
the complete TLS object: `root_ca_certificate` / `connect_certificate` / `connect_private_key` /
`enable_mtls` / `verify_name_on_connect`) as a single json5 string and parses it once with
`zc_config_from_str()`.

When the router cert's SAN binds an **IP/mesh address** rather than the DNS name you dial, set
`EFDI_VERIFY_NAME=false` (the default). A DNS-named remote router can use `true`.

## Notes / API accuracy

- Symbols used match zenoh-c **1.9.0** (verified against the `1.9.0` tag's `include/zenoh_commons.h`
  and `examples/z_pub.c` / `examples/z_sub.c`): `zc_config_from_str`, `z_open`,
  `z_view_keyexpr_from_str`, `z_declare_publisher`, `z_publisher_put` with `z_owned_bytes_t`
  (`z_bytes_copy_from_str`) + `z_publisher_put_options_default`, `z_declare_subscriber` with a
  `z_owned_closure_sample_t` built by the `z_closure(...)` macro, `z_sample_payload` /
  `z_sample_keyexpr`, `z_bytes_to_string`, `z_keyexpr_as_view_string`, and
  `z_string_data`/`z_string_len`. Ownership uses `z_move`/`z_loan`/`z_drop`. If you pin a
  different minor, re-check against that tag's examples.
- The connect logic is **inlined** in each example (not a shared header) so each `.c` is a
  standalone drop. The canonical contract is the same as every other client here — see
  [`clients/README.md`](../../../README.md) and the helpers under `clients/connect/`.
- PEM **paths** are interpolated into the json5 string assuming they contain no `"` or `\`
  (true for typical Unix paths). If yours can, escape them before building the config string.
- Payloads here are JSON for readability; send any bytes via `z_bytes_copy_from_buf(&b, ptr, len)`.
