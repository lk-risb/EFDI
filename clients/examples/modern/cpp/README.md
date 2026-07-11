# C++ — modern

Idiomatic C++ against the pod, using the **official** Zenoh C++ binding
[`zenoh-cpp`](https://github.com/eclipse-zenoh/zenoh-cpp) **1.9.0** plus the header-only
[`connect/cpp/efdi_connect.hpp`](../../../connect/cpp/efdi_connect.hpp) helper.

## Read this first: zenoh-cpp is a header-only wrapper over zenoh-c

`zenoh-cpp` is **header-only C++** (RAII wrappers, lambdas-as-closures) but it does **not** ship
its own engine — it wraps a backend. The default/portable backend is
[`zenoh-c`](https://github.com/eclipse-zenoh/zenoh-c) (the `ZENOHCXX_ZENOHC` build of the C++
binding; the imported target is `zenohcxx::zenohc`). So you install **two** things, in order:

1. **zenoh-c 1.9.0** — the actual library (`libzenohc`), built with the unstable API.
2. **zenoh-cpp 1.9.0** — the header-only wrappers + the CMake package config (`zenohcxx`).

## Install the dependencies

A C toolchain, a C++17 compiler, CMake ≥ 3.16, and Rust/Cargo (to build zenoh-c) are needed.

```sh
# 1) zenoh-c 1.9.0  (builds libzenohc; UNSTABLE API is required — the json5 config
#    insert/from-str path the connect helper relies on is behind it)
git clone https://github.com/eclipse-zenoh/zenoh-c
cd zenoh-c && git checkout 1.9.0
cmake -B build -DCMAKE_BUILD_TYPE=Release -DZENOHC_BUILD_WITH_UNSTABLE_API=ON
cmake --build build --config Release
sudo cmake --install build          # headers + libzenohc -> /usr/local
sudo ldconfig                        # Linux: refresh the linker cache
cd ..

# 2) zenoh-cpp 1.9.0  (header-only wrappers + the zenohcxx CMake package)
git clone https://github.com/eclipse-zenoh/zenoh-cpp
cd zenoh-cpp && git checkout 1.9.0
cmake -B build -DCMAKE_BUILD_TYPE=Release    # finds the zenoh-c you just installed
sudo cmake --install build
cd ..
```

Installing to a non-default prefix? Point CMake at it when you build the examples:
`cmake -B build -DCMAKE_PREFIX_PATH=/path/to/zenoh-install`.

## Build the examples

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
# binaries: ./build/publish  ./build/subscribe
```

The header-only connect helper is pulled in from `clients/connect/cpp/` via the include path
(set by `CMakeLists.txt`); there is nothing extra to build for it.

## Setup (env)

```sh
export EFDI_ROUTER="tls/127.0.0.1:7447"
export EFDI_CERT="$HOME/.goat/contexts/default/mtls.cert.pem"
export EFDI_KEY="$HOME/.goat/contexts/default/mtls.key.pem"
export EFDI_CA="$HOME/.goat/contexts/default/ca-roots.pem"
export PARTNER_NAMESPACE="release/acme"
# EFDI_VERIFY_NAME defaults to false (local pod at 127.0.0.1); set true for a DNS-named router.
```

## Run — the round-trip

```sh
./build/publish              # publish one JSON sample to <namespace>/sensors/temp
./build/publish 50 0.2       # 50 samples, 200ms apart
./build/subscribe            # receive everything under your namespace (<ns>/**)
./build/subscribe 'release/goat/**'   # inbound data from goat
./build/subscribe '<keyexpr>' 5              # stop after 5 samples
```

**Quick round-trip:** run `./build/subscribe` in one terminal, `./build/publish 10 0.5` in
another. The JSON samples should arrive in the subscriber.

## The one connection gotcha (read this — it bites everyone)

Zenoh's TLS config must be inserted as **one whole json5 block** at `transport/link/tls`, with
**`enable_mtls: true`**. Setting the sub-keys one at a time
(`transport/link/tls/connect_certificate`, etc.) silently does **not** turn on the client-cert
send path on Zenoh 1.x — your session opens but the router rejects you, or you connect
read-only. The helper does it the working way: it builds the entire config (mode + connect
endpoint + the complete TLS object with `root_ca_certificate` / `connect_certificate` /
`connect_private_key` / `enable_mtls` / `verify_name_on_connect`) as a single json5 document and
parses it once with `zenoh::Config::from_str(...)`.

When the router cert's SAN binds an **IP/mesh address** rather than the DNS name you dial, set
`EFDI_VERIFY_NAME=false` (the default). A DNS-named remote router can use `true`.

## Notes

- Payloads here are JSON for readability; send any bytes — `pub.put(zenoh::Bytes(buf))` or
  `pub.put(std::string(...))`. The fabric is payload-agnostic.
- Read a sample with `sample.get_payload().as_string()` (text) and
  `sample.get_keyexpr().as_string_view()` (key).
- The subscriber handler is a lambda passed to `declare_subscriber(ke, handler, closures::none)`;
  it runs on a zenoh worker thread, so guard shared state (the example uses `std::atomic`).
- RAII owns the C resources: `Session`, `Publisher`, `Subscriber` drop on scope exit. Errors
  surface as `zenoh::ZException` (caught in `main`).

## Caveats / API accuracy

- API symbols used here match zenoh-cpp **1.9.0**: `Config::from_str`, `Session::open`,
  `KeyExpr`, `declare_publisher`, `Publisher::put`, `declare_subscriber` (lambda + `closures::none`),
  `Sample::get_payload().as_string()`, `Sample::get_keyexpr().as_string_view()`. If you pin a
  different minor, re-check against that tag's `examples/universal/z_pub.cxx` and `z_sub.cxx`.
- `Config::from_str` / json5 config live behind the **unstable** zenoh-c API — build zenoh-c with
  `-DZENOHC_BUILD_WITH_UNSTABLE_API=ON` (step 1) or `Config::from_str` won't be available and the
  helper won't compile/link.
- If `find_package(zenohcxx)` fails, your zenoh-cpp install isn't on `CMAKE_PREFIX_PATH`; if it
  finds the package but linking fails on `libzenohc`, zenoh-c isn't installed/findable — fix the
  prefix, it's not an API problem.
