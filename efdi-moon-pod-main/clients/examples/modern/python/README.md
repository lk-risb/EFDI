# Python — modern

Idiomatic Python against the pod. Uses the official `eclipse-zenoh` client + the
[`connect/python/goat_connect.py`](../../../connect/python/goat_connect.py) helper.

## Setup

```sh
python3 -m venv venv && . venv/bin/activate     # Windows: venv\Scripts\activate
pip install eclipse-zenoh                         # 1.x, matches the fabric's Zenoh 1.9.0
# export GOAT_* per clients/README.md
```

## Run

```sh
python3 publish.py                 # publish one JSON sample to <namespace>/sensors/temp
python3 publish.py 50 0.2          # 50 samples, 200ms apart
python3 subscribe.py               # receive everything under your namespace
python3 subscribe.py 'release/goat/**'   # inbound data from goat
python3 request_reply.py serve     # answer queries (queryable)
python3 request_reply.py get       # query it
```

Quick round-trip: run `subscribe.py` in one terminal, `publish.py` in another.

## Notes

- **Windows:** use `python` (not `python3`) inside the venv, or you hit the system Python and get
  `ModuleNotFoundError: zenoh`.
- Payloads here are JSON for readability; send any bytes (protobuf/CBOR/raw). The fabric is
  payload-agnostic — the `format` on a registered topic is a hint for tooling, not enforced.
- `async` style: `eclipse-zenoh` callbacks already run off the main thread; for asyncio, wrap
  `session.get`/subscriber handlers with `loop.run_in_executor` or use a `queue.Queue` drain.
