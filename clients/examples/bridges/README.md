# bridges — talk to the pod in a protocol you already speak

A **bridge** is a tiny process that is itself a Zenoh client (mTLS, your namespace, the works) but
exposes a **different protocol** to *your* application — HTTP, a watched directory, etc. Your app
never links a Zenoh library and contains **no Zenoh code at all**. It speaks the protocol it already
knows; the bridge does the Zenoh part.

This is the great equalizer for legacy and military shops that cannot (or will not) link an
`eclipse-zenoh` client: MATLAB, PLCs, old .NET Framework, Java 8, COBOL-era systems, anything that
can do an HTTP request or write a file.

## Bridge vs native client — when to use which

| Use a **native client** (`connect/` + `examples/modern/`) | Use a **bridge** |
|---|---|
| You can `pip install eclipse-zenoh` (or the Go/Rust/Java/C++ lib) | You can't link a Zenoh client (toolchain, policy, certification) |
| You want lowest latency and full pub/sub/query semantics | You want zero Zenoh code in your app |
| Modern language, you control the build | MATLAB / PLC / old .NET / air-gapped / file-only shops |
| Long-lived in-process subscriptions | "fire an HTTP call" or "drop a file" is all you've got |

A native client is always faster and richer. Reach for a bridge only when linking Zenoh isn't an
option — then your app stays trivial and the bridge carries the complexity.

## The bridges here

| Bridge | Your app speaks | Good for |
|---|---|---|
| [`rest-http/`](rest-http/) | HTTP `POST` / `GET` / Server-Sent-Events | anything with an HTTP client; webhooks |
| [`file-drop/`](file-drop/) | files in a directory | MATLAB, PLC, old .NET, air-gapped, file-only |

## Run them co-located with the pod (localhost)

A bridge is the same kind of Zenoh client as everything else in `clients/`: it reads the same five
env vars (`EFDI_ROUTER`, `EFDI_CERT`, `EFDI_KEY`, `EFDI_CA`, `PARTNER_NAMESPACE`) and reuses
[`connect/python/efdi_connect.py`](../../connect/python/efdi_connect.py) to open its mTLS session.

**Run the bridge on the same machine as the pod**, and have it expose its protocol on
**`127.0.0.1` only**. The bridge holds your mTLS client identity, so its plaintext side (HTTP, the
watched directory) is an unauthenticated door into the fabric — keep that door on localhost. If your
app is on another host, put the bridge next to *that* app and point its `EFDI_ROUTER` at the pod's
mesh IP, but understand you've then moved the trust boundary to the link between bridge and pod
(which is still mTLS) — never expose the plaintext side to a network you don't trust.

## Dependencies

Both bridges are **stdlib + `eclipse-zenoh` only** (no web framework, no file-watcher library), so
they run anywhere Python 3 runs, including air-gapped boxes. Each has an optional `Dockerfile` to
run as a compose sidecar next to the pod. Examples target **Zenoh 1.9.0** (fleet-pinned).
