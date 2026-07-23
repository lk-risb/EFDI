# tools/

Standalone operator utilities. Nothing here is imported by the pod, started by
`start.sh`/`run.sh`, or built into any image — these are run by hand, usually
from a laptop or from the PC wired to a radar, while commissioning a feed.

They live outside `compose/` on purpose: `compose/bridges/` and
`compose/protocols/` are the running data plane, this is field tooling.

## `asterix_probe.py` — "is the radar actually sending, and what?"

Passively listens on one UDP port and summarises what arrives: which ASTERIX
categories, which SAC/SIC (Data Source Identifier) values, and at what rate.
Read-only — it never republishes or forwards anything.

Use it before wiring a new radar into the pod, to confirm the feed exists and
to discover which categories you need to enable.

```bash
python3 tools/asterix_probe.py --port 30001
```

Referenced from the setup guides: `INSTALL.md`, `DIEGIMAS.md`, `INTEGRATIONS.md`.

## `asterix_relay.py` — "the radar can't reach the pod directly"

Forwards ASTERIX UDP datagrams unchanged from a local port to a remote
`IP:PORT`. Run it on the machine connected to the radar when that machine is on
the NetBird mesh but the radar itself is not routable from the pod.

Packets are relayed byte-for-byte; nothing is decoded, filtered, or rewritten.

```bash
python3 tools/asterix_relay.py --dest 100.x.y.z:30048
```

## Covered by tests

`tests/test_asterix_raw_pipeline.py` exercises the framing logic these tools
share with the decoders, so changes here are caught by the normal test run.
