# REST / HTTP bridge

Talk to the pod with **plain HTTP** — no Zenoh library in your application. The bridge is a small
Python process (itself a Zenoh mTLS client) that runs **next to the pod on localhost** and turns
HTTP requests into fabric publishes/subscribes. This is the path for any language or tool that can
make an HTTP request: `curl`, MATLAB (`webwrite`), old .NET (`HttpClient`), Java 8
(`HttpURLConnection`), shell scripts, a PLC's HTTP block.

## Run the bridge

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
python3 bridge.py                 # serves on http://127.0.0.1:8080
```

## Use it

**Publish** (POST a body to a suffix under your namespace):
```sh
curl -X POST http://127.0.0.1:8080/pub/sensors/temp -d '21.5'
# -> publishes "21.5" to release/acme/sensors/temp
```

**Receive N samples** (blocks until they arrive or `?timeout=` seconds):
```sh
curl 'http://127.0.0.1:8080/sub/sensors/temp?count=3'
# -> [{"key":"release/acme/sensors/temp","ts":...,"text":"21.5"}, ...]
curl 'http://127.0.0.1:8080/sub/release/<partner>/**?count=1'   # inbound from a partner
```

**Stream continuously** (Server-Sent Events — one `data:` line per sample):
```sh
curl -N http://127.0.0.1:8080/stream/sensors/temp
```

**Outbound webhook** (push every matching sample into your own system, no client at all):
```sh
WEBHOOK_URL=https://my-system.local/ingest WEBHOOK_KEYEXPR='release/<partner>/**' \
  python3 bridge.py
```

## Keys

A bare path like `sensors/temp` is scoped under your namespace (`release/acme/sensors/temp`). A
full fabric key you have read rights to (e.g. `release/<partner>/...`) is passed through as-is.
`*` = one segment, `**` = any depth.

## Security

- Binds **127.0.0.1 only** by default (`BRIDGE_BIND`/`BRIDGE_PORT` to change). It is an
  **unauthenticated local proxy that holds your fabric mTLS credentials** — keep it on loopback,
  or put your own auth/reverse-proxy in front before exposing it.
- Runs co-located with the pod; it is not meant to be reachable off-box.

## Run as a compose sidecar (optional)

Build the image with the provided `Dockerfile` and add it to the pod's compose with
`network_mode: host` and the `EFDI_*` env, mounting the cert dir read-only. (The bridge needs the
same mTLS material the pod's other clients use.)
