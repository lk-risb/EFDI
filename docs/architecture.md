# moon-pod — v0 architecture

The v0 "secure-pipe core": the smallest bundle that moves data **both directions** securely
and stays **partner-custodial**. See [`../README.md`](../README.md) for scope.

## Component placement: host vs compose

The single genuinely host-shaped component (the WireGuard mesh client) runs on the **host**;
the data-plane services run in a small **docker-compose** bundle on host networking.

```
┌─ Partner host (their hardware, their facility, LUKS-encrypted data volume) ──────────┐
│                                                                                      │
│  systemd:                                                                            │
│    goat-clientd --headless --mode ${GOAT_CLIENT_MODE}   ← WireGuard mesh peer        │
│    (kernel WireGuard; boot-persistent; auto-reconnect)                               │
│                                                                                      │
│  on PATH:                                                                            │
│    goat-cli            ← operator surface: doctor, topic ls, pub, sub (transparency) │
│                                                                                      │
│  docker compose (network_mode: host — binds the pod's mesh IP directly):             │
│    ┌─────────────┐                                                                   │
│    │ zenoh-router│   (published image, no local build)                              │
│    └─────┬───────┘                                                                   │
│          │ mTLS, ACL                                                                 │
│          │ <ns>/**                                                                   │
│          ▼                                                                           │
│   (over the mesh maintained by goat-clientd)                                          │
└──────────────────────────────────────────────────────────────────────────────────────┘
            │ WireGuard mesh
            ▼
   goat fabric (the sandbox router directly;
   later: partner-net + release-bridge boundary)
```

### Why the mesh client is on the host, not in a container

- Kernel WireGuard via `wg-quick` (container path is usually forced to slower userspace `wireguard-go`).
- systemd gives boot-persistence + auto-reconnect-on-network-change for free.
- No privileged container (`NET_ADMIN` + `/dev/net/tun`); no isolation thrown away.
- No netns/restart coupling (a sidecar's restart would drop every dependent container's network).
- Matches goat-client's own design (systemd/launchd/Windows-Service daemon) and is the shape
  the goat-client design doc already names for moon-pods.

Data-plane containers use `network_mode: host` so they bind the pod's mesh IP directly — the
same clean routing a netns sidecar would give, but coupled to the stable host.

## Startup ordering

1. Host: LUKS unlock → Docker up → `goat-clientd` (systemd) brings the mesh up.
2. `first-boot.sh` (once): verify bundle → lay down WG + Zenoh mTLS certs → render `.env` →
   `goat-clientd --import-bundle` → `docker compose up -d`.
3. compose: `zenoh-router` dials the fabric over the mesh.

### Service images (corrected from the v0 scaffold)

The compose service uses an **already-published image** — no local Dockerfile/build context:
- `zenoh-router`: upstream `eclipse/zenoh:1.9.0` (digest-pinned, ADR 0858).

The earlier `../audit` and `../doctor` build-context stubs were an oversight (no Dockerfiles →
`docker compose up` would fail); removed in favor of the published image + the host doctor timer.

## Data flow (bidirectional)

- **Outbound** (partner → fabric): partner publishes under `release/<partner>/**`; the pod's
  Zenoh router carries it over the mesh **to the goat-side ingress gateway** (see "Receiver
  side" below), not directly into a core router.
- **Inbound** (fabric → partner): the partner subscribes to `release/goat/**` (when
  bilateral); v0 consumption is **raw Zenoh via `goat-cli sub`** (translator is post-v0).

## Receiver side (goat-side — how we ingest what the pod sends)

A moon-pod is a **Tier-2 peer** under the federated-peering trust model
(ADR 1020 •
`zenoh-federated-peering-trust.md`):
it is partner-custodial and may be rooted in a different CA, so it is **not** the canonical
goat↔goat (Tier-1) federation. That has a concrete consequence for the data path:

- The pod's router does **not** federate directly into a goat core router. It connects to a
  **goat-operated ingress gateway** that holds goat's own identity.
- The gateway authenticates the pod's *verified* peering identity (its cert), assigns a stable
  `<peering-id>`, and admits the pod's data **origin-scoped** — the gateway is the only writer
  the core router's ACL sees, so a pod can never squat another namespace or impersonate the
  fabric. The required ACL change (ingress/egress `put` split, default-deny on un-scoped
  ingress) is in ADR 1020.
- Every admitted sample carries provenance (verified peering identity, original key,
  timestamps) into the goat audit trail.

**Open reconciliation (a decision for the receiver build, not invented here):** an onboarded
moon-pod publishing *its own first-party* data under `release/<partner>/**` is authorized and
origin-scoped, unlike the un-onboarded **relay** case ADR 1020's quarantine namespace
(`<entity>/<peering-id>/<original-key>`) was written for. The receiver must decide whether the
moon-pod's first-party `release/<partner>/**` is admitted **directly** (origin-scoped, since it
*is* onboarded end-to-end) while only *relayed third-party* traffic is quarantine-rewritten —
or whether all pod ingress flows through the same rewrite. This is tracked as receiver-side
work on this track; the pod is built to publish `release/<partner>/**` regardless, and the
admitted shape is the receiver's concern.

## Trust + custody

- CA trust comes from the **bundle** (`ca_roots` / `--trust-roots`), never compiled in — a
  partner pod may be rooted in a production CA, not the dev sandbox CA.
- Audit logs are append-only NDJSON on the partner's **LUKS-encrypted** volume. Goat does not
  aggregate them. (MinIO + Object-Lock is a later hardening upgrade.)
- The pod is one mesh peer; the partner can `systemctl`/`journalctl`/`docker logs` everything.

### Workload identity (optional overlay, Phase 3+)

The pod's containers can additionally carry **attested workload identity** via a SPIRE/SPIFFE overlay — a **first-class optional overlay**, layered like the local-IdP choice. It is **not** required: the base pod meets the partner-receiver contract without it, and the easy deploy path stays an honest default. A partner who wants (or is required to have) cryptographically-attested workload identity reaches for the overlay; offering the simple default does not withhold the capability. SPIRE runs inside the pod (sovereignty-clean — goat hosts no partner SVIDs); federation exchanges only public trust bundles so the goat-side ingress gateway can verify the pod router's identity.

## Profiles (environment shape)

`profiles/<env>/` carries everything that differs between deployments (endpoints, ACL subject
axis, namespace, retention, goat-client mode). `efdi/` is concrete; `production/` is a
documented TODO seam (ADR 0844 unsettled). The compose stack + scripts are identical across
environments; only the profile + the signed bundle change.

## Provenance + self-containment

This is the EFDI moon-pod, derived from the goat moon-pod reference implementation. Design
principles that keep it cleanly standalone:

- **Self-contained:** no runtime reach into goat-internal paths. Reused upstream artifacts (Zenoh
  image, audit-subscriber, goat-cli, the mesh daemon) are consumed as **released binaries /
  pinned images**, not source-relative imports.
- **Apache-2.0** licensed.
- **Stack-agnostic pod, env-specific bundle.** The compose stack, host scripts, and config
  templates hardcode **nothing** stack-specific (no EFDI / jailbreak / production values). Every
  environment value — router endpoint, namespaces, ACL subject axis, CA roots, mesh enrollment —
  arrives at first-boot from the **signed join bundle** plus the active **profile**
  (`profiles/<env>/`). Switching stacks is a different bundle + profile, never a code change.

