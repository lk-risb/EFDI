# Proof-out tracks — focus areas for the EFDI moon-pod

This is a **menu of candidate tracks** for proving out the moon-pod: each is a self-contained
engineering campaign with a clear thesis to demonstrate. They are *options* — pick one to focus a
collaboration sprint, or sequence several. EFDI is the sandbox where each is proven; the value of
each track is the arc it carries from **a coalition partner exchanging data in a sandbox** to **a
real operational system** running on the same shape.

## How to read this

Each track has two parts:

- **Why it matters** — the operational stake, and why proving it on the pod (rather than only on
  the goat side) is what makes it real for a coalition.
- **What to prove it out** — concrete work, laid out **crawl → walk → run**: a first demonstrable
  result, a hardening pass, then the operational-context milestone. The crawl step is deliberately
  small so a partner can get a green light fast.

Everything here builds on what the pod already ships — the secure-pipe core (mesh client, mTLS
Zenoh router, ACL-scoped slot, observe-only audit-sink, `goat-cli` operator surface), the
[client SDKs](../clients/README.md), the [producer best-practice examples](../examples/README.md),
the [boundary contract](partner-contract.md), and the [QoS model](quality-of-service.md).

**Honest framing.** Some of what these tracks lean on is built and already live on the EFDI
fabric; some is designed but not yet built (the receiver-side ingress gateway, releasability
graduation, QoS enforcement, the internal time-sync engine). Each track notes where its
foundation sits so a sprint starts with eyes open. A track is "done" when its thesis is
demonstrated end-to-end on a live pod ↔ EFDI exchange — not when code merges.

## Dependencies between tracks

Two tracks are **foundational** — most others assume them:

- **Identity & access control** establishes *who* a pod is and *what* it may do on both sides of
  the boundary. Every other track's trust claims rest on it.
- **Sovereign secure data sharing** establishes the *clean two-way pipe* under partner custody.
  It is the substrate the operational, QoS, and releasability tracks ride on.

The other four (QoS, time hygiene, releasability/graduated-trust, operational data usage) can be
picked up in parallel once a basic pipe is up, and each sharpens a different operational quality
of that pipe.

---

## Track 1 — Sovereign secure data sharing from the partner side

**Slug:** `sovereign-data-sharing`

### Why it matters

This is the headline promise of the whole pattern: a coalition partner shares data **into** the
fabric (and receives data back) from **their own hardware, in their own facility, under their own
custody** — without surrendering their data to someone else's cloud and without a months-long
custom integration. For a coalition, sovereignty is not a nicety; it is frequently the
precondition for any sharing at all. A partner nation or allied unit that can read every line of
what runs on its network, log every byte that crosses, and walk away with the bundle on any given
day is a partner that can say *yes* to sharing. Proving the pod genuinely preserves that custody —
that data lands on partner storage, partner operators authenticate against a partner-administered
identity store, partner audit logs stay partner-held, and nothing is a black box — is what turns
"trust us" into "verify us." The operational endpoint is a partner running a sovereign node that
exchanges real mission data continuously, that they could fork and self-host any day, and never
need to.

### What to prove it out

- **Crawl — bidirectional smoke on a live pod.** Stand up the pod against the EFDI sandbox, mint a
  slot, and demonstrate a real round trip: publish first-party data under your namespace, confirm
  it lands on a live fabric subscriber, and subscribe to an inbound `release/goat/**` topic.
  Capture the local audit-sink entries for both directions. (The repo's
  [bidirectional smoke test](../tests) is the starting harness.)
- **Crawl — custody walkthrough.** Produce a one-page custody attestation a partner can verify
  themselves: data at rest on the partner's LUKS-encrypted volume, `journalctl`/`docker logs`
  transparency on every container, the bundle/source open for inspection, and the "fork-and-self-host"
  exit path named. Validate it against the [seven requirements](partner-contract.md).
- **Walk — durable, sovereign audit.** Move the audit-sink from observe-only to durable NDJSON on
  the encrypted volume (and optionally a co-located object store with write-once retention), so the
  partner holds an independent, tamper-evident record of everything that crossed — not dependent on
  the goat side.
- **Walk — custody hardening.** Exercise key/credential rotation, WG re-keying, and bundle
  re-mint without losing the partner's namespace or identity; prove the "reversibility" requirement
  by actually standing the coordination plane up partner-side in a lab.
- **Run — operational sovereign node.** A partner runs the pod continuously against EFDI carrying a
  real data product (a sensor feed, an intel layer, a fused track), through a power-cycle and a
  network-change, with the partner's own operators administering it end to end. This is the
  reference a production partnership is sold against.

---

## Track 2 — QoS and data-path quality engineering

**Slug:** `qos-data-path-quality`

### Why it matters

A coalition link is the worst link in the system: it is often a relay-fallback path over
commercial internet, congested, and shared across many flows of wildly different importance. When
the pipe gets thin, *something* has to give — and the entire value of the platform is that the
thing that gives is the bulk file transfer, never the time-critical track. The fabric is
**brokerless**: there is no central traffic cop to enforce that, so quality has to be engineered
into identity, namespace, and the points where the path is actually controlled, plus an **honest
gauge** every participant can read so degradation is visible rather than silent. For a partner,
this is the difference between "we shared a feed" and "we shared a feed that stayed usable when it
mattered." Proving the pod can declare what its traffic *should* look like, see where it actually
sits, and have mission traffic protected across a degrading coalition link is what makes the
platform credible for an operational tempo. (Foundation: the reveal pillar — fabric/mesh/time
health and per-producer footprints — is built and live on EFDI; per-actor throughput envelopes are
built; enforcement is designed and sequenced, not yet built.)

### What to prove it out

- **Crawl — read your own footprint.** From the pod, read the live health gauge and the pod's own
  producer footprint (rate, share, traffic class) off the fabric. Confirm the pod's traffic is
  classified and visible, and that the honest gauge never shows a false green. Start from
  [the QoS guide](quality-of-service.md) §4.
- **Crawl — class your traffic deliberately.** Tag the pod's flows into the four lanes (control /
  mission / situational / bulk) by namespace + intent, and show the class composition in the gauge.
- **Walk — declare a throughput envelope.** Give the pod's producer an expected-rate band (floor /
  typical / elevated / ceiling) and demonstrate the reveal verdict (within range / below floor /
  elevated / over ceiling) tracking real traffic — including the below-floor case as a stall
  signal, not just floods.
- **Walk — degrade the link on purpose.** Use the resilient-producer patterns
  ([catch-up, must-deliver, presence](../examples/README.md)) and measure recovery: induce loss /
  a reconnect on the coalition path and show mission-class data catches up and presence honestly
  flips while bulk traffic is the thing that suffers.
- **Run — protected mission slice across a real coalition link.** Demonstrate, end to end, that
  under contention on a constrained pod link, the reserved mission lane stays serviceable while
  bulk backs off — the operational claim the whole QoS model exists to make. (The enforcement leg
  is goat-side and currently designed; this run milestone is the forcing function that prioritizes
  building it.)

---

## Track 3 — Time hygiene engineering

**Slug:** `time-hygiene`

### Why it matters

Every fused track, every "who saw what first," every last-writer-wins merge, and every
certificate lifetime depends on time the participants agree on. A coalition partner at the edge
frequently has **no reliable external clock** — GPS is jammed or spoofed, NTP is unreachable
across a denied link. The platform's answer is **internal-synchronization-first**: participants
mutually sync to a shared ensemble clock with no external reference required, while GPS/NTP are
treated as *opportunistic anchors* that pull the ensemble toward absolute time when reachable —
not as the foundation. That yields two distinct health questions a coalition has to answer
separately: *are we internally coherent* (good enough to order and fuse events — holds even at the
denied edge) versus *do we know real-world time* (needed for certificate validity and
cross-cluster correlation). For a partner, proving the pod participates honestly in fabric time —
and that its clock claims are **attestation-gated** so a compromised or drifting partner can't
poison everyone's sense of time — is what makes shared situational awareness trustworthy under
exactly the conditions a coalition operates in. (Foundation: time health is on the bus and
live-fired on EFDI; the internal-sync engine itself is the larger designed-not-built piece.)

### What to prove it out

- **Crawl — surface the pod's two time axes.** Read the pod host's time-health on the fabric: its
  internal-coherence state and its UTC-confidence state, each with an honest "unknown" rather than
  a fabricated lock. Confirm a coherent-but-UTC-unknown reading is reported as *healthy for
  ordering*, not as a fault.
- **Crawl — timestamp discipline on the wire.** Show the pod's published samples carry asserted
  high-resolution timestamps so ordering survives across the boundary, using the
  [self-describing / resilient producer patterns](../examples/README.md).
- **Walk — attested clock input.** Have the pod emit a signed time attestation and demonstrate the
  Byzantine gate: a well-behaved pod's clock input is admitted; a deliberately-skewed or
  unsigned one is refused rather than allowed to drag the ensemble.
- **Walk — denied-edge drill.** Cut the pod's external time anchor (no GPS/NTP) and show internal
  coherence holding for ordering while UTC-confidence honestly decays — and recovers when an anchor
  returns.
- **Run — ordered fusion under denial.** Run an operational scenario where pod and fabric events
  must be correctly ordered/fused while the pod is at a denied edge, and show the result is correct
  because internal coherence held — the operational payoff of the whole time model.

---

## Track 4 — Data releasability & graduated trust on ingestion (both directions)

**Slug:** `releasability-graduated-trust`

### Why it matters

Coalition data sharing lives or dies on two governance questions: **what am I allowed to release,
and to whom** (outbound), and **how much do I trust what just arrived** (inbound). Neither is
binary. Outbound, releasability is per-relationship — the same fact may be releasable to one
partner set and not another — so the platform marks data for a *releasability set* rather than
shipping everything to everyone. Inbound, a brand-new partner's first-party data, a third party
*relayed* through that partner, and a long-trusted internal feed are **not** equally trustworthy,
and treating them as if they were is how bad or hostile data contaminates a common picture.
Graduated trust means new and relayed inbound data lands **origin-attributed and quarantined** —
visible and usable, but fenced and labeled — and is *promoted* as confidence is established;
crucially, this applies even **among internal feeds**, so provenance and trust level travel with
the data rather than being assumed from the network it came in on. For a coalition, proving the
pod participates correctly in both directions — releasing only what's marked, and having its
inbound treated as graduated-trust by the receiver — is what lets sharing scale past a single
trusted bilateral pair without poisoning the well. (Foundation: the Tier-2 ingress model and
release-set governance are **designed**; the receiver-side ingress gateway and the graduation
machinery are the build this track forces.)

### What to prove it out

- **Crawl — releasability marking on egress.** Publish pod data marked for a named releasability
  set (not a flat "everyone") and confirm the marking is carried and visible at the boundary.
  Anchor on the egress half of the [boundary contract](partner-contract.md).
- **Crawl — provenance on every sample.** Show the pod's outbound samples carry verifiable origin
  (identity, original key, timestamps) so the receiver can attribute them — the precondition for
  any graduated-trust decision.
- **Walk — origin-scoped admission (receiver side).** Stand up / exercise the Tier-2 ingress
  gateway so the pod's first-party data is admitted **origin-scoped** (the pod can never squat
  another namespace), and *relayed third-party* data is rewritten into an attributed quarantine
  namespace. Settle the open reconciliation noted in [architecture.md](architecture.md) (admit
  first-party directly vs. quarantine-rewrite everything).
- **Walk — quarantine, then promote.** Demonstrate inbound data entering quarantine, being
  consumable-but-labeled, and being **promoted** to trusted as confidence is established —
  including a promotion path for an *internal* feed, proving trust level rides with the data, not
  with the link.
- **Run — multi-party releasability picture.** Run a scenario with more than one partner where
  outbound respects per-set releasability and inbound is graduated by origin, and show an operator
  can see, for any datum, *where it came from and how far it's trusted* — the operational common
  picture a coalition actually needs.

---

## Track 5 — Operational proof-of-principle: using the data

**Slug:** `operational-data-usage-poc`

### Why it matters

It is easy to demonstrate that bytes move; it is the operational *use* of those bytes that proves
the platform. A coalition partner does not care that a sample traversed a mesh — they care that a
sensor reading became a track on a screen, that a cue triggered an action, that two units saw the
same picture and coordinated off it. This track exists to push past plumbing and stand up a real
operational loop on top of everything the platform already provides — pub/sub, schema-described
self-decoding data, the inspection surfaces, health, identity — so the value is shown in
mission terms, not protocol terms. For a coalition, a working proof-of-principle that they can see,
touch, and recognize as *their* mission is the single most persuasive artifact for moving from a
sandbox to a funded operational system. The point is not a new capability; it is composing the
existing ones into something operationally legible.

### What to prove it out

- **Crawl — schema-described round trip.** Publish self-describing (schema-tagged) operational
  data from the pod and consume + correctly decode it on the other side without out-of-band schema
  exchange, using the [encoding example](../examples/README.md) and a [client SDK](../clients/README.md).
- **Crawl — see it on a surface.** Render the pod's live data on an inspection surface (and/or the
  single-pane fabric dashboard) so a non-engineer can watch it arrive — operational legibility from
  day one.
- **Walk — a real producer/consumer pair.** Stand up a representative mission flow end to end (e.g.
  a sensor-style producer on the pod feeding a consumer that acts on it), with resilient delivery
  (catch-up + must-deliver) so it survives a flaky link.
- **Walk — bidirectional operational loop.** Close the loop: pod-published observations drive a
  fabric-side response that comes back to the partner under `release/goat/**`, demonstrating
  two-way operational value, not just one-way telemetry.
- **Run — a recognizable mission vignette.** Run a scripted, partner-recognizable scenario (a
  shared track picture, a cue-to-action, a coordinated two-unit view) end to end on pod ↔ EFDI,
  capturing it as the demonstrable artifact that anchors the operational-system conversation.

---

## Track 6 — Identity & access control on both sides of the pod

**Slug:** `identity-access-control`

### Why it matters

Every trust claim in every other track rests on this one: *who is this pod, who is the operator
behind it, what workload is publishing, and what is each allowed to do* — answered correctly on
**both** sides of the boundary. The platform uses distinct identity domains for distinct subjects,
and a coalition deployment touches all of them at once: **human/service** operators authenticate
through an identity provider the **partner administers** (sovereignty floor — their credential
store, on their hardware); **machines** are admitted to the mesh by WireGuard peer identity with
ACLs governing who may reach whom; **the data plane** is gated by **x509 mTLS** where the
certificate is the write anchor — a pod publishes only within the namespace its cert authorizes,
and a write outside it is denied; and **workloads** can carry **attested SPIFFE/SPIRE identity**,
which the pod is uniquely well-placed to provide because it is a controlled bundle whose containers
are attestable (unlike a bare external publisher). Getting this right is what lets a coalition
extend trust to a partner *precisely* — not "they're on the network so they can do anything" but
"this attested workload, run by this authenticated operator, may write exactly this namespace." For
a coalition moving to operational use, crisp, layered, sovereignty-respecting identity is the
control plane that makes everything else safe to scale. (Foundation: mesh identity, slot-scoped
mTLS ACLs, and the SPIRE pattern are built and proven on EFDI goat-side; the SPIRE pod overlay and
federation are designed as a first-class optional overlay.)

### What to prove it out

- **Crawl — prove the data-plane boundary.** Demonstrate the x509 write-anchor: the pod publishes
  inside its slot namespace and is **silently denied** outside it; show how an operator detects and
  diagnoses that denial rather than guessing. Confirm CA trust comes from the bundle, never
  compiled in.
- **Crawl — mesh identity + reachability.** Show the pod admitted to the mesh as a WireGuard peer
  and that peer-to-peer reachability is governed by ACLs (it can reach what it should, and not what
  it shouldn't).
- **Walk — partner-administered operator identity.** Wire an identity provider on the partner side
  (the local-IdP piece) so the pod's operators authenticate against a **partner-held** credential
  store, and show the sovereignty floor holds (goat hosts no partner credentials).
- **Walk — attested workload identity (the SPIRE overlay).** Stand up the
  [SPIRE/SPIFFE overlay](workload-identity-spire-overlay.md) so the pod's containers carry
  short-lived attested SVIDs, and federate trust bundles so the goat-side ingress can verify the
  pod router's *attested* identity rather than trusting a bundle-embedded key.
- **Walk — identity grammar discipline.** Carry the role / affiliation / echelon / node-class /
  site dimensions cleanly so capability is never named after affiliation, and a partner's
  attribution is distinct from its authorization.
- **Run — precise, layered, end-to-end authorization.** Demonstrate the full stack on a live
  exchange: an authenticated operator, an attested workload, a mesh-admitted peer, and an
  mTLS-anchored namespace — composing into "*this* workload run by *this* operator may write
  *exactly* this," with every layer independently verifiable. This is the control plane an
  operational coalition deployment is governed by.

---

## Picking a track

- Want the fastest credible demo? **Sovereign secure data sharing** (Track 1) — the round-trip
  smoke + custody walkthrough is a green light in a day.
- Want to de-risk the hardest operational claim? **QoS** (Track 2) or **Time hygiene** (Track 3) —
  both target the coalition-link conditions that break naive systems.
- Want to unlock multi-party scale? **Releasability & graduated trust** (Track 4) — the governance
  that lets sharing grow past one bilateral pair.
- Want the most persuasive artifact for a funding/operational conversation? **Operational
  proof-of-principle** (Track 5) — a recognizable mission vignette.
- Want the foundation everything else rests on? **Identity & access control** (Track 6).

Each track is written to stand alone, but **Identity** and **Sovereign data sharing** are the two
most others assume — a sprint that starts there compounds.
