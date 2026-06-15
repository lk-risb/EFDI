# Workload identity (SPIRE/SPIFFE) — optional overlay, Phase 3+

**Status:** Design note / scaffold seam. Not in the base pod. Partner-gated.

## What it is

An **optional overlay** that gives the pod's own containers (Zenoh router, translation engine, audit sink, operator UI) **attested workload identity** — short-lived X509-SVIDs issued by a SPIRE server + agent running *inside the pod*. It is layered exactly like the local-IdP choice (Kanidm / Dex / Authelia / Zitadel / external): the partner picks their altitude.

## First-class option, not a filter

The base pod meets the seven-requirement partner-receiver contract **without** workload identity, and the easy deploy path must stay an honest default — a non-engineer stands the pod up end-to-end with no SPIRE. This overlay is **fully available** to any partner who wants, or is required to have, attested workload identity. Offering the simple default does **not** mean withholding the capability — that would be gatekeeping. Two first-class paths; neither is second-class. This is progressive disclosure, the same principle as the IdP overlay choice.

## Why the pod is the right home

- **The pod is a controlled bundle**, so its workloads *are* attestable (a SPIRE agent ships in the bundle) — unlike a bare external publisher running on unmanaged hardware.
- **SPIFFE federation is the designed cross-trust-domain mechanism.** The pod runs its own trust domain (`spiffe://<partner>.pod`); it federates **trust bundles** (public keys only) with the goatnet trust domain. The pod's Zenoh router then carries a cryptographically-attested identity the goat-side ingress gateway can verify — an upgrade over trusting the setup key embedded in the signed bundle.
- **Sovereignty-clean.** SPIRE runs inside the pod, on partner hardware. Goat hosts no partner SVIDs. Federation exchanges only public trust bundles — same posture as the partner-net coordination-metadata carve-out.

## Integration nuance (carried from the goat-side analysis)

Eclipse Zenoh's access-control matches on cert **Common Name** only — it has no URI-SAN matcher today. A SPIFFE SVID puts identity in the URI SAN with an empty CN, so an SVID is invisible to a CN-only Zenoh ACL. Two ways the overlay handles this, neither a fork:

- **CN-bearing SVID** — the SPIRE registration entry sets the SVID's Subject CN (via its first DNS name) to the ACL-expected subject, so a router admits it unchanged.
- **Ingress-gateway verification** — for pod→goat peering, the goat-side ingress gateway verifies the federated SVID off the core router, so the core ACL is not the enforcement point.

The native fix is an upstream URI-SAN matcher in Zenoh's access-control (tracked goat-side as `zenoh-uri-san-acl-matcher-upstream`); when it lands, the SVID's `spiffe://` SAN becomes the wire identity directly.

## Scope of this overlay (when built)

A `profiles/<env>-spire/` overlay + a compose overlay adding `spire-server` + `spire-agent` and a `spiffe-helper` sidecar per workload that needs an SVID. Phase 3+ — gated behind a partner that needs it. The goat-side proving-ground for the same pattern (an EFDI sandbox slice) and the full analysis live in the goat substrate (partner-gated; not required to run a pod).
