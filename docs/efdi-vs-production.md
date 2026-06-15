# EFDI (sandbox validation reference) vs production goat-net — deltas

**Principle:** the pod hardcodes **nothing** stack-specific. Everything environment-specific comes
from the **signed join bundle** (the source of truth) + the active **profile**
(`profiles/<env>/`, a fallback for fields a bundle omits). The EFDI profile is concrete (it's the
live sandbox); the production profile is a documented TODO seam — production specifics that aren't
settled are **not invented** here.

This is the central anti-over-specification guarantee: switching from EFDI to production (or to
any other goatnet) is a different bundle + profile, never a code change to the pod.

| Axis | EFDI (sandbox) | Production goat-net | How the pod stays agnostic |
|---|---|---|---|
| Zenoh topology | single router; peers dial it | per-site planes / multi-router + federation — **not fully settled** | router connect/listen endpoints come from the bundle (`router_endpoint`); pod does not assume a single router |
| Zenoh ACL subject identity | `cert_common_names` (CN-keyed) | likely URI-SAN or other — **TBD** | subject-identity axis is a profile field, not baked into config templates |
| Zenoh key-expr namespace | per-uuid **slot** (`<slot_id>/**`); flat community-read | `release/<partner>/**` (egress) + `release/goat/**` (ingress) | the write namespace is `PARTNER_NAMESPACE` and the bilateral inbound is `INBOUND_NAMESPACE` — both profile-driven, neither hardcoded |
| Netbird / mesh mgmt | EFDI sandbox mgmt + sandbox group taxonomy | **partner-net** (separate mgmt instance) + production group taxonomy | mgmt URL + setup key ride in the bundle (`InnerMeshSetup`); profile carries nothing site-internal |
| PKI roots | shared dev sandbox CA | per-site production CA | CA trust is taken from the bundle's `ca_roots` / `--trust-roots`, never compiled in |
| Audit retention | sandbox tier | prod tier (e.g. 7y cold) | retention is a profile value (`AUDIT_RETENTION_DAYS`) |
| Boundary enforcement (receiver) | pod ↔ sandbox router directly (no gateway yet) | **ingress gateway** per ADR 1020: a moon-pod is a Tier-2 (non-goat↔goat) peer, so its egress is received by a goat-operated gateway that authenticates the verified peering identity, admits origin-scoped, and records provenance — never direct federation into a core router. **goat-side, not built yet** | pod connects to whatever `router_endpoint` the bundle gives; the gateway is a goat-side hop the pod is transparent to. The pod publishes its own namespace regardless; the *admitted* shape is the receiver's concern |
| Mesh client mode | `netbird-only` (no silent control plane on the sandbox) | likely `netbird-only` (cleaner partner on-ramp) — **production-profile TODO** | mode is profile-driven (`GOAT_CLIENT_MODE`) |

## How the bundle carries each per-environment value

The pod reads these straight from the signed bundle — it never derives them from a hardcoded
default:

- **Mesh enrollment** — `InnerMeshSetup.ManagementURL` + `.SetupKey` (the mgmt instance to join).
- **Fabric router endpoint** — `goat_cli_extras.router_endpoint` (written into `profile.toml` by
  `goat profile init`; first-boot reads it from there, profile var is fallback only).
- **Operator portal** — `goat_cli_extras.portal_endpoint` + `portal_token`.
- **mTLS identity** — `goat_cli_extras.mtls_cert` / `mtls_key` (the pod's Zenoh identity on the
  fabric — its slot CN on EFDI).
- **CA roots** — `goat_cli_extras.ca_roots` (verification trust; never compiled in).

The only values that legitimately live in the profile are the ones the bundle does *not* carry:
the bilateral inbound prefix (`INBOUND_NAMESPACE`), audit retention, the mesh mode, and the
ACL subject axis — and even those are plain config, swappable per environment.

## Goat-side dependencies the pod is built to plug into (not pod code)

- **partner-net** — a separate mesh mgmt instance for partners (production path). The pod enrolls
  against whatever mgmt the bundle's `InnerMeshSetup` names; on the sandbox that's the EFDI mgmt.
- **ingress gateway** (ADR 1020) — the goat-side receiver for Tier-2 peered ingress, including
  moon-pods. It authenticates the verified peering identity, admits data origin-scoped, and
  records provenance. **goat-side, not built yet**; the sandbox validates pod ↔ router directly.
- **release-bridge / schema-enforcer / conditioning** — the broader boundary-conditioning layer;
  layers on top of the ingress gateway. Not built yet.

All recorded here so the pod's data path is built to accommodate them without rework.
