# Partner run guide — stand up the pod you were handed

Turnkey, idempotent path to bring a moon-pod up against the EFDI fabric and verify it. You run
this on **your own host**. Everything environment-specific (which fabric, your identity, your
namespace) is carried in the **signed join bundle** you were given at onboarding — you do not
configure any of it by hand.

## What you receive at onboarding

1. A **signed join bundle** (`<your-handle>.cbor`) — carries your mesh setup-key, your fabric
   router endpoint, your mTLS identity (your EFDI slot), and the CA roots. It is the single
   source of truth; the pod reads everything from it.
2. The **`goat` CLI binary** for your platform (linux-amd64 / linux-arm64).

> Treat the bundle as a secret in transit. It activates on first use and expires; if it lapses,
> ask for a fresh one.

## Prerequisites on your host

- A Linux host you control, with data on a **LUKS-encrypted volume** (the pod's state dir must
  live there — sovereignty requirement 6a).
- **Docker** + the compose plugin.
- A **mesh client**: stock NetBird is the EFDI on-ramp (`netbird up` with the bundle's setup-key);
  the production path uses `goat-clientd` and first-boot drives it for you.
- The **`goat`** binary on `PATH`, and `envsubst` (from `gettext`).

## Bring-up

```
# 1. Join the mesh with the setup-key from your bundle (EFDI sandbox path):
sudo netbird up --management-url <mgmt-url-from-onboarding> --setup-key <setup-key-from-onboarding>

# 2. Run first-boot with the EFDI profile + your bundle. This verifies the bundle, extracts your
#    mTLS identity, renders the Zenoh router config from the BUNDLE's router endpoint, and starts
#    the data plane. Re-runnable.
sudo ./host/first-boot.sh efdi /path/to/<your-handle>.cbor
```

first-boot does, in order: verify the bundle + extract certs (`goat profile init`) → lay the
Zenoh mTLS material → render the router config (router endpoint **from the bundle**) → start
`zenoh-router` + `audit-sink` via compose → install the `goat-doctor` health timer.

## Verify

```
# Health — expect the cert/router checks green:
sudo GOAT_PROFILE_DIR=/var/lib/goat-moon/goat-cli goat doctor --all

# Containers up:
docker ps    # goat-moon-zenoh-router + goat-moon-audit-sink

# Round-trip publish/subscribe on your own namespace (your slot):
echo hi > /tmp/ping
sudo GOAT_PROFILE_DIR=/var/lib/goat-moon/goat-cli goat pub '<your-slot>/test/ping' --payload-file /tmp/ping
sudo GOAT_PROFILE_DIR=/var/lib/goat-moon/goat-cli goat sub '<your-slot>/**' --count 1
```

Your slot is the namespace your cert is bound to (the `PARTNER_NAMESPACE` first-boot reported). A
publish **outside** your slot is silently denied — that is expected; publish only within it.

## Sending and receiving real data

- **From code:** pick your language under [`../clients/`](../clients/), copy the `publish` /
  `subscribe` example, point it at your slot. SDKs ship for C/C++, Go, Java, Python, Rust,
  TypeScript, plus legacy (Java 8, .NET Framework, C99, MATLAB) and file-drop / REST bridges.
- **Do it well:** [`../examples/`](../examples/README.md) has the producer best-practice patterns —
  set a self-describing encoding, recover after a partition, must-deliver a critical stream, and
  use liveliness for presence. Read [`quality-of-service.md`](quality-of-service.md) for which
  reliability each of your streams actually needs (don't over-buy guarantees).
- **Inbound (bilateral):** when a bilateral inbound channel is granted, subscribe to the
  `INBOUND_NAMESPACE` prefix from your profile (`release/goat/**` by convention).

## Idempotency + teardown

- `first-boot.sh` is re-runnable: it rotates the profile if one exists and `docker compose up -d`
  is convergent.
- Tear the data plane down with `docker compose -f compose/docker-compose.yml down`; your audit
  log and state remain on your LUKS volume until you remove them.

## Trouble

- **`goat doctor` router check fails:** confirm the mesh is up (`netbird status` → Connected) and
  that the bundle hasn't expired (`goat profile show`). The router endpoint the pod uses comes
  from the bundle, so a stale bundle points at the wrong place — get a fresh one.
- **A container can't pull its image:** every pod image is public; run `tests/check-images-public.sh`
  to confirm reachability from your host.
- **A publish "succeeds" but nothing arrives:** check you published *inside* your slot. Denials
  are silent by design.
