# efdi-moon-pod

The **EFDI moon-pod** — the minimal, partner-custodial software bundle that lets an EFDI
mission partner share data **bidirectionally and securely** with the goat fabric, running on
**their own hardware, in their facility, under their custody and transparency**.

This repo is the EFDI-partner collaboration surface: it is where we and EFDI partners co-develop
the pod openly. It is self-contained and carries **no goat-internal infrastructure, links, or
credentials** — everything here is safe to develop against in the open.

## In plain language — what this is and what it's for

A moon-pod is a **small, self-contained box of software you run** so you can securely exchange
data with the goat fabric — without joining the fabric wholesale and without a months-long
custom integration.

Think of it as a **sealed, two-way mailbox** both sides understand:

- **You run it on your own hardware, in your own facility, on your own power.** You have full
  custody and transparency — it's open source, so you can read it, log it, and even fork it.
  That's the sovereignty promise: nobody puts a black box on your network or pulls your data into
  someone else's cloud.
- **It speaks goat's language.** Even though it lives on your side, it is built to the fabric's
  security rules (identity, encryption, access control), so when data crosses, both sides trust
  the pipe.
- **Data flows both ways.** You publish under your own namespace; the fabric sends you data under
  `release/goat/**` when the relationship is bilateral.
- **Everything is audited** on your side — a local log of what went in and out.

**What it's for:** an EFDI partner or unit has data the fabric wants (sensor feeds, intel, a data
product) — or needs data the fabric has. The moon-pod makes that **turnkey**: stand up the pod,
get granted a topic, and data flows securely in hours instead of a procurement cycle.

The contract the pod meets at the boundary — the seven requirements, the sovereignty floor, and
how the goat side receives a pod's data — is documented self-contained in
[`docs/partner-contract.md`](docs/partner-contract.md).

## EFDI is a sandbox

EFDI is the sandbox goatnet for rapid onboarding of internal and external (allied / partner-nation)
participants. The pod here is calibrated against the live EFDI fabric:

- **Enrollment** is a NetBird setup-key minted for you at onboarding (no silent control plane on
  the sandbox).
- **Zenoh identity** is a per-uuid **slot** the portal assigns — your cert CN, your portal
  prefix, and your write namespace are all that slot anchor (a UUID, not a human-readable name).
- **Write namespace** is your slot (`<slot_id>/**`). A publish outside your slot is silently
  denied — declare and publish only within it.

None of the sandbox shape is the production shape; production deltas come from a production
profile + the signed bundle. See [`docs/efdi-vs-production.md`](docs/efdi-vs-production.md).

## What this bundle is (the "secure-pipe core")

The full moon-pod spec defines six pieces (network client, Zenoh router, translation engine,
local IdP, audit sink, operator web UI). This bundle ships the **minimal core that moves data
both ways securely and stays partner-custodial**, and defers the productization layers.

| Piece | Here | Deferred |
|---|---|---|
| Network client (mesh peer) | ✅ stock NetBird on the sandbox / `goat-clientd` host daemon for the production path — see [contract](docs/goat-client-v02-contract.md) | |
| Zenoh router | ✅ `eclipse/zenoh:1.9.0` (digest-pinned), mTLS, ACL-scoped | |
| Audit sink | ✅ `goat-audit-subscriber` in **observe-only** mode (subscribe + metrics) | durable NDJSON on LUKS / MinIO |
| Operator surface | ✅ `goat-cli` on host PATH (`doctor`, `pub`, `sub`) + `clients/` SDKs | |
| Translation engine | — | format adapters |
| Local IdP | — | Kanidm / Dex / Authelia / Zitadel overlays |
| Operator web UI | — | SPA |

Partner data egress today is **raw Zenoh via `goat-cli sub`** or one of the `clients/` SDKs; the
translator is a later productization layer.

## Shape

The one genuinely host-shaped thing (the WireGuard mesh client) runs on the **host** as a
service; the data-plane services run in a small **docker-compose** bundle on host networking.

```
Host (systemd)
  mesh client (stock netbird on the sandbox, or goat-clientd for the production path)
  goat-cli                                  # operator surface (on PATH)

docker-compose (network_mode: host)
  zenoh-router   eclipse/zenoh:1.9.0        data plane; mTLS; ACL <namespace>/**
  audit-sink     (goat-audit-subscriber)    observe-only (subscribe + metrics)

host systemd timer
  goat-doctor    goat-cli                    periodic `goat doctor --json` → status blob
```

The mesh client is on the host (not containerized) for kernel WireGuard, boot-persistence +
auto-reconnect, no privileged container, and no netns/restart coupling. Data-plane containers
use `network_mode: host` so they bind the pod's mesh IP directly.

## Layout

```
efdi-moon-pod/
├── README.md                       this file
├── LICENSE                         Apache-2.0
├── CONTRIBUTING.md                 how we co-develop this with EFDI partners (+ the no-internal-leak rule)
├── docs/
│   ├── partner-contract.md         the seven requirements + Tier-2 ingress (self-contained)
│   ├── architecture.md             bundle shape + data flow
│   ├── partner-run-guide.md        run the pod you were handed; enroll; smoke-test
│   ├── goat-client-v02-contract.md mesh-daemon contract (production mesh path)
│   ├── efdi-vs-production.md       sandbox-vs-production deltas table
│   └── workload-identity-spire-overlay.md  optional attested-workload-identity overlay (Phase 3+)
├── clients/                        connect SDKs (5 langs) + worked pub/sub/bridge examples
├── compose/                        zenoh-router + audit-sink (host networking) + .env.example
├── profiles/
│   ├── efdi/                       concrete EFDI sandbox profile
│   └── production/                 documented TODO seam
├── host/                           first-boot.sh, service units, zenoh-router template
├── examples/                       producer best-practice patterns (encoding, resilient sub, must-deliver, presence, reconcile)
└── tests/                          image-public guard + bidirectional smoke test
```

## Getting started

- **Running a pod you were handed:** [`docs/partner-run-guide.md`](docs/partner-run-guide.md).
- **Writing a publisher / subscriber:** [`clients/README.md`](clients/README.md) — pick your
  language, copy the example, point it at your slot.
- **Producer best practices (encoding, catch-up, must-deliver, presence):**
  [`examples/README.md`](examples/README.md) — runnable patterns for resilient, self-describing data.
- **How Quality of Service protects what matters:** [`docs/quality-of-service.md`](docs/quality-of-service.md) —
  the lanes, the honest health gauge, and what you do as a producer to fit the model.
- **Understanding the boundary contract:** [`docs/partner-contract.md`](docs/partner-contract.md).
- **Choosing what to prove out (focus areas):** [`docs/proof-out-tracks.md`](docs/proof-out-tracks.md) —
  candidate tracks (sovereign sharing, QoS, time hygiene, releasability/graduated trust,
  operational use, identity/access), each with why it matters + crawl→walk→run work.
- **Co-developing this repo:** [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

Apache-2.0. See [`LICENSE`](LICENSE).
