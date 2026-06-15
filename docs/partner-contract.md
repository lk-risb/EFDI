# Partner-receiver contract — the seven requirements

Self-contained partner-facing summary of the contract an EFDI moon-pod meets at the boundary.
This is the reference vendored into this repo so the pod is documented end-to-end without
reaching into goat's internal design tree. The authoritative contract is maintained on the goat
side; this summary is kept faithful to it.

A conformant moon-pod meets all seven at the boundary. *How* each is met is the pod's (or a
vendor pod's) choice — the contract is the boundary behaviour, not a specific implementation.

## The "moon" metaphor

A moon-pod is a **sovereign satellite**: it orbits the goat fabric and exchanges data with it,
but it runs on the partner's own hardware, in the partner's facility, under the partner's
custody. The partner can read it, log it, fork it, and leave at any time. Goat provides the
"easy button"; it does not put a black box on the partner's network or pull partner data into a
goat-operated cloud.

## The seven requirements

1. **Identity assurance + IdP standard.** Every operator who can read or publish the pod's
   namespace carries an identity from an IdP that emits standards-compliant assertions (OIDC
   discovery, or SAML 2.0 with an integration note). Floor: NIST SP 800-63 IAL2 / AAL2 (verified
   proofing + phishing-resistant MFA).

2. **Network / transport sovereignty.** Bytes between the partner edge and the goat edge are
   end-to-end encrypted with keys neither side delegates to a third party, and the partner-side
   handshake is silent at the public boundary. WireGuard preferred; IPsec acceptable; mTLS over
   public HTTPS acceptable as a documented fallback.

3. **Landing interface contract.** Data flows in and out at well-known Zenoh key-expressions
   under the pod's namespace, or at a documented adapter the partner runs that maps cleanly to
   those keys (REST / gRPC / MQTT / Kafka / file-drop / webhook / syslog / TAK CoT /
   MISP-STIX-TAXII / S3-object-drop). A native Zenoh peer is the preferred zero-translation
   shape.

4. **Audit retention.** The partner retains 90+ days of access logs covering reads and writes to
   the pod's namespace, queryable on demand ("who accessed topic X at time T as identity Y?").
   Format flexible. Goat does not require partners to ship logs to it.

5. **Schema declaration discipline.** Any topic the partner publishes carries a schema
   declaration on the `attest/schema/<topic>` convention. Undeclared publishes are rejected at
   the goat-side boundary by the schema-enforcer. No "declare it later" exemptions.

6. **Sovereignty floor** — three nested invariants, all of which hold:
   - **(a) Data** lands at rest only on partner-controlled hardware/infrastructure.
   - **(b) Identity** — operator credentials live in the partner's controlled IdP. Goat may ship
     the IdP binary; once it runs on the partner's host the credential database is theirs.
   - **(c) Audit** logs live in partner storage. Goat does not aggregate partner audit by default.

   If any of the three flips, it is not sovereign.

7. **Reversibility / migration cleanliness.** A partner running a moon-pod can transition to
   operating their own coordination plane and router without re-onboarding under a different
   namespace, re-issuing identity, or re-issuing per-topic access. No architectural lock-in: the
   easy button is not a trap.

## Sovereignty-floor edge cases (why goat-operated pieces don't break the floor)

- **The goat-hosted coordination plane** (the partner-net mesh mgmt instance) holds only
  connection metadata — WireGuard public keys, setup keys, peer addressing, last-seen — never
  partner data, credentials, or audit. Connection metadata is not in the data path and is
  explicitly excluded from requirement 6.
- **The goat-operated boundary** processes partner data *in flight* (schema-enforcement,
  allowlist, conditioning, audit emission). Requirement 6 is about data *at rest*, not data in
  motion.
- **Goat-shipped binaries running on the partner host** (the pod bundle, IdP, translators):
  sovereignty is about administrative custody, not code provenance. Sovereignty begins at the
  partner's first-boot completion, after every shipped default key and credential is rotated.

## How the goat side receives a pod's data (Tier-2 ingress)

A moon-pod is a **Tier-2 peer** under goat's federated-peering trust model: partner-custodial,
possibly rooted in a different CA, so it is *not* the canonical goat↔goat (Tier-1) federation.
The pod's router does **not** federate directly into a goat core router. It connects to a
**goat-operated ingress gateway** that authenticates the pod's verified peering identity,
assigns it a stable origin scope, admits its data origin-scoped (so a pod can never write
another namespace or impersonate the fabric), and records provenance into the goat audit trail —
independent of, and complementary to, the pod's own local audit sink.

The pod is built to publish its own first-party namespace regardless; the admitted shape is the
receiver's concern. On the EFDI sandbox the pod connects to whatever router endpoint the bundle
provides.
