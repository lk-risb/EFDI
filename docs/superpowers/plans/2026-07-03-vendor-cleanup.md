# Vendor Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove EFDI/goat vendor tooling that isn't load-bearing for pod operation (portal registration, proprietary audit sink, health-probe timer, partnership/contract docs), without touching the mesh/cert bootstrap or anything in the live CoT data path.

**Architecture:** Straight deletions plus a narrow consistency pass — no new code, no new services. Every step is verified by (a) confirming the deleted thing is actually gone and nothing references it, and (b) confirming the pod's existing data flow (bridges → Zenoh → `cot_layer.py` → TAK Server / ATAK) still works exactly as before.

**Tech Stack:** Bash, YAML (docker-compose), Markdown.

## Global Constraints

- **Hard non-regression requirement** (from the spec, `docs/superpowers/specs/2026-07-02-vendor-cleanup-and-zenoh-admin-gui-design.md`): live CoT delivery to TAK Server/WinTAK/other fabric users must keep working throughout and after every task in this plan.
- **Do not touch:** `host/first-boot.sh`, `host/goat-clientd.service.example`, `host/zenoh-router.json5.tmpl`, `profiles/efdi/profile.env`, `profiles/production/profile.env.TODO`, any bridge/layer Python file, `clients/`, `examples/`, `tools/asterix_relay.py`. These are either the pod's only working mesh/cert bootstrap (kept until a future pass) or not vendor-specific at all.
- **Never commit without being asked.** After each task's verification step, stop and describe what changed — wait for the user to say "commit this" or run `git commit` themselves. Do not run `git commit` unprompted.

---

### Task 1: Delete `register_topics.sh` and its CLAUDE.md references

**Files:**
- Delete: `register_topics.sh`
- Modify: `CLAUDE.md:114`, `CLAUDE.md:116`

**Interfaces:** None — no other file invokes `register_topics.sh` or reads `EFDI_PORTAL_KEY`/`EFDI_VENDOR_SLUG` (verified: these vars are not referenced by any bridge, layer, or script other than this one file).

- [ ] **Step 1: Confirm nothing else references this script or its env vars**

Run:
```bash
grep -rln "register_topics\|EFDI_PORTAL_KEY\|EFDI_VENDOR_SLUG" /home/ndukve/IdeaProjects/efdi-moon-pod --include="*.sh" --include="*.py" --include="*.md" --include="*.yml" 2>/dev/null | grep -v __pycache__
```
Expected output: `register_topics.sh` and `CLAUDE.md` only. If any bridge/layer/compose file shows up, stop and report back before deleting.

- [ ] **Step 2: Delete the script**

```bash
rm /home/ndukve/IdeaProjects/efdi-moon-pod/register_topics.sh
```

- [ ] **Step 3: Remove the stale CLAUDE.md security-constraint lines**

`CLAUDE.md:114` currently reads:
```
- `BUNDLE_DIR`, `register_topics.sh` — gitignored, never commit
```
`BUNDLE_DIR` is still relevant (used by the cert bootstrap, which is kept). Change to:
```
- `BUNDLE_DIR` — gitignored, never commit
```

`CLAUDE.md:116` currently reads:
```
- `EFDI_PORTAL_KEY`, `EFDI_VENDOR_SLUG` — from environment only, never hardcoded
```
Delete this line entirely (both vars only existed for the now-deleted script).

- [ ] **Step 4: Verify — no dangling references**

Run:
```bash
grep -rn "register_topics\|EFDI_PORTAL_KEY\|EFDI_VENDOR_SLUG" /home/ndukve/IdeaProjects/efdi-moon-pod --include="*.sh" --include="*.py" --include="*.md" --include="*.yml" 2>/dev/null | grep -v __pycache__
```
Expected: no output (empty).

- [ ] **Step 5: Stage and stop (do not commit)**

```bash
git add register_topics.sh CLAUDE.md
git status --short
```
Report the staged diff to the user. Do not run `git commit`.

---

### Task 2: Remove the `audit-sink` service from `docker-compose.yml`

**Files:**
- Modify: `compose/docker-compose.yml:1-14` (header comment), `compose/docker-compose.yml:40-66` (service block)

**Interfaces:** None — `audit-sink` only subscribes for observability (Prometheus metrics on `:9303`), no other service or bridge depends on it existing. `zenoh-router` has no `depends_on` relationship pointing *at* `audit-sink` (dependency direction is the reverse).

- [ ] **Step 1: Confirm the exact block boundaries**

Run:
```bash
grep -n "^  [a-z]" /home/ndukve/IdeaProjects/efdi-moon-pod/compose/docker-compose.yml | head -10
```
Confirm `audit-sink:` starts at line 40 and the next top-level service (`opensky-bridge:`) starts at line 67 — i.e. the block to delete is lines 40–66 inclusive (including the trailing blank line before `opensky-bridge:`).

- [ ] **Step 2: Delete the `audit-sink` service block**

Delete lines 40–66 of `compose/docker-compose.yml` (the entire `audit-sink:` block, from the `audit-sink:` line through the trailing blank line right before `opensky-bridge:`).

- [ ] **Step 3: Update the header comment**

`compose/docker-compose.yml` currently opens with:
```yaml
# moon-pod — v0 data-plane bundle.
#
# Runs the containerizable data-plane services. The WireGuard mesh client (goat-clientd)
# runs on the HOST as a systemd service (see ../host/), NOT here — see ../docs/architecture.md.
# The goat-doctor health probe also runs as a HOST systemd timer (goat-cli is no-daemon by
# design, ADR 1019, and has no published container image) — see ../host/goat-doctor.* .
#
# network_mode: host so each service binds the pod's mesh IP directly (the mesh is maintained
# by the host's goat-clientd). No netns sidecar, no NAT gymnastics.
#
# Values come from .env (copied from .env.example, populated by first-boot.sh from the active
# profile + the minted artifacts). Images are pinned by digest (ADR 0859). Both services here
# use ALREADY-PUBLISHED images — no local build context (the earlier ../audit + ../doctor
# build-context stubs were an oversight; corrected to reuse published artifacts).
```
Replace with (drops the now-removed `goat-doctor`/`audit-sink` references, keeps everything else accurate):
```yaml
# moon-pod — v0 data-plane bundle.
#
# Runs the containerizable data-plane services. The WireGuard mesh client (goat-clientd)
# runs on the HOST as a systemd service (see ../host/), NOT here — see ../docs/architecture.md.
#
# network_mode: host so each service binds the pod's mesh IP directly (the mesh is maintained
# by the host's goat-clientd). No netns sidecar, no NAT gymnastics.
#
# Values come from .env (copied from .env.example, populated by first-boot.sh from the active
# profile + the minted artifacts). Images are pinned by digest (ADR 0859). zenoh-router uses an
# ALREADY-PUBLISHED image — no local build context.
```

- [ ] **Step 4: Verify the compose file is still valid YAML and no other service depends on `audit-sink`**

Run:
```bash
cd /home/ndukve/IdeaProjects/efdi-moon-pod/compose && docker compose config --quiet && echo "compose config valid"
grep -rn "audit-sink" /home/ndukve/IdeaProjects/efdi-moon-pod/compose/docker-compose.yml
```
Expected: `compose config valid` printed, and the `grep` returns no output (empty — `audit-sink` no longer appears anywhere in the file).

- [ ] **Step 5: Non-regression check — zenoh-router still starts clean**

```bash
docker compose -f /home/ndukve/IdeaProjects/efdi-moon-pod/compose/docker-compose.yml up -d zenoh-router
sleep 5
docker compose -f /home/ndukve/IdeaProjects/efdi-moon-pod/compose/docker-compose.yml ps zenoh-router --format "{{.Status}}"
```
Expected: status shows `healthy` (or `Up`), same as before this change.

- [ ] **Step 6: Stage and stop (do not commit)**

```bash
git add compose/docker-compose.yml
git status --short
```
Report the staged diff to the user. Do not run `git commit`.

---

### Task 3: Delete `host/goat-doctor.*`

**Files:**
- Delete: `host/goat-doctor.sh`, `host/goat-doctor.service.example`, `host/goat-doctor.timer.example`

**Interfaces:** None — these are only ever invoked by `host/first-boot.sh`'s step [6/6] ("Install the host goat-doctor probe timer"), which is itself a soft/optional step (`if command -v goat >/dev/null 2>&1`). Deleting the referenced files means step 6 will fail to find `goat-doctor.sh` if it's reached; this is addressed in Step 3 below.

- [ ] **Step 1: Confirm no other file references these three files**

Run:
```bash
grep -rln "goat-doctor" /home/ndukve/IdeaProjects/efdi-moon-pod --include="*.sh" --include="*.md" --include="*.yml" 2>/dev/null | grep -v __pycache__
```
Expected: `host/first-boot.sh`, `host/goat-doctor.sh`, `host/goat-doctor.service.example`, `host/goat-doctor.timer.example`, `docs/architecture.md`, `compose/docker-compose.yml` (the last one should already be gone after Task 2 — if it still shows up, Task 2 wasn't fully applied, stop and check).

- [ ] **Step 2: Delete the three files**

```bash
rm /home/ndukve/IdeaProjects/efdi-moon-pod/host/goat-doctor.sh
rm /home/ndukve/IdeaProjects/efdi-moon-pod/host/goat-doctor.service.example
rm /home/ndukve/IdeaProjects/efdi-moon-pod/host/goat-doctor.timer.example
```

- [ ] **Step 3: Remove the now-dead step [6/6] in `host/first-boot.sh`**

Find the block (it starts with `# --- [6/6] Install the host goat-doctor probe timer` and ends right before `echo "==> first-boot complete."`):

```bash
grep -n "6/6\]\|first-boot complete" /home/ndukve/IdeaProjects/efdi-moon-pod/host/first-boot.sh
```

Delete the entire `[6/6]` block (from the `# --- [6/6] ...` comment line through the line right before `echo "==> first-boot complete."`, inclusive of the `fi` that closes its `if command -v goat` check).

Also update the renumbered comment for the now-final step ("Render compose .env and start the data plane") from `[5/6]` to `[5/5]`, and the earlier `[1/6]`...`[4/6]` comments to `[1/5]`...`[4/5]` to match (5 steps total instead of 6). Use:
```bash
sed -i \
  -e 's/\[1\/6\]/[1\/5]/' \
  -e 's/\[2\/6\]/[2\/5]/' \
  -e 's/\[3\/6\]/[3\/5]/' \
  -e 's/\[4\/6\]/[4\/5]/' \
  -e 's/\[5\/6\]/[5\/5]/' \
  /home/ndukve/IdeaProjects/efdi-moon-pod/host/first-boot.sh
```
(Run this *after* deleting the `[6/6]` block, so there's no `[6/6]` left to accidentally renumber.)

- [ ] **Step 4: Verify — no dangling references, script still valid bash**

```bash
bash -n /home/ndukve/IdeaProjects/efdi-moon-pod/host/first-boot.sh && echo "first-boot.sh syntax OK"
grep -n "goat-doctor\|\[6/6\]" /home/ndukve/IdeaProjects/efdi-moon-pod/host/first-boot.sh
```
Expected: `first-boot.sh syntax OK`, and the `grep` returns no output.

- [ ] **Step 5: Stage and stop (do not commit)**

```bash
git add -A host/goat-doctor.sh host/goat-doctor.service.example host/goat-doctor.timer.example host/first-boot.sh
git status --short
```
Report the staged diff to the user. Do not run `git commit`.

---

### Task 4: Delete the EFDI partnership/contract docs

**Files:**
- Delete: `docs/partner-contract.md`, `docs/efdi-vs-production.md`, `docs/goat-client-v02-contract.md`, `docs/workload-identity-spire-overlay.md`, `docs/quality-of-service.md`, `docs/proof-out-tracks.md`, `docs/partner-run-guide.md`

**Interfaces:** None — check for cross-references from docs that are being kept before deleting.

- [ ] **Step 1: Check for cross-references from kept docs**

```bash
grep -rln "partner-contract\|efdi-vs-production\|goat-client-v02-contract\|workload-identity-spire-overlay\|quality-of-service\.md\|proof-out-tracks\|partner-run-guide" /home/ndukve/IdeaProjects/efdi-moon-pod --include="*.md" 2>/dev/null
```
Note every file that shows up besides the 7 being deleted themselves — these need their links removed/updated in Step 3.

- [ ] **Step 2: Delete the 7 docs**

```bash
rm /home/ndukve/IdeaProjects/efdi-moon-pod/docs/partner-contract.md
rm /home/ndukve/IdeaProjects/efdi-moon-pod/docs/efdi-vs-production.md
rm /home/ndukve/IdeaProjects/efdi-moon-pod/docs/goat-client-v02-contract.md
rm /home/ndukve/IdeaProjects/efdi-moon-pod/docs/workload-identity-spire-overlay.md
rm /home/ndukve/IdeaProjects/efdi-moon-pod/docs/quality-of-service.md
rm /home/ndukve/IdeaProjects/efdi-moon-pod/docs/proof-out-tracks.md
rm /home/ndukve/IdeaProjects/efdi-moon-pod/docs/partner-run-guide.md
```

- [ ] **Step 3: Fix dangling links — `CONTRIBUTING.md`**

`CONTRIBUTING.md:22` currently reads:
```
If you need to reference an internal decision, describe it in plain prose or summarize it into a
self-contained doc under `docs/` (as `docs/partner-contract.md` does for the boundary contract).
```
Replace with:
```
If you need to reference an internal decision, describe it in plain prose or summarize it into a
self-contained doc under `docs/`.
```

`CONTRIBUTING.md:34` currently reads:
```
When you add a feature, ask: *does this bake an EFDI / jailbreak / production assumption into the
pod?* If so, lift it into the bundle contract or a profile var. The deltas table in
[`docs/efdi-vs-production.md`](docs/efdi-vs-production.md) is the reference for what varies per
stack and where it legitimately lives.
```
Replace with:
```
When you add a feature, ask: *does this bake an EFDI / jailbreak / production assumption into the
pod?* If so, lift it into the bundle contract or a profile var — see `profiles/<env>/` for what
varies per stack and where it legitimately lives.
```

`CONTRIBUTING.md:48-51` currently reads (a bullet list under "Where things live"):
```
- `docs/partner-contract.md` — the boundary contract a pod meets (self-contained).
- `docs/architecture.md` — the bundle shape + data flow.
- `docs/partner-run-guide.md` — how a partner runs a pod.
- `docs/efdi-vs-production.md` — what varies per stack and how the pod stays agnostic.
```
Replace with:
```
- `docs/architecture.md` — the bundle shape + data flow.
```

- [ ] **Step 4: Fix dangling links — `docs/architecture.md`**

`docs/architecture.md:4` currently reads:
```
The v0 "secure-pipe core": the smallest bundle that moves data **both directions** securely
and stays **partner-custodial**. See [`../README.md`](../README.md) for scope and [`partner-contract.md`](partner-contract.md) for the
boundary contract.
```
Replace with:
```
The v0 "secure-pipe core": the smallest bundle that moves data **both directions** securely
and stays **partner-custodial**. See [`../README.md`](../README.md) for scope.
```

`docs/architecture.md:119` currently reads:
```
The pod's containers can additionally carry **attested workload identity** via a SPIRE/SPIFFE overlay — a **first-class optional overlay**, layered like the local-IdP choice. It is **not** required: the base pod meets the partner-receiver contract without it, and the easy deploy path stays an honest default. A partner who wants (or is required to have) cryptographically-attested workload identity reaches for the overlay; offering the simple default does not withhold the capability. SPIRE runs inside the pod (sovereignty-clean — goat hosts no partner SVIDs); federation exchanges only public trust bundles so the goat-side ingress gateway can verify the pod router's identity. Design + rationale: [`workload-identity-spire-overlay.md`](workload-identity-spire-overlay.md).
```
Replace with (drops just the dead trailing link, keeps the conceptual paragraph):
```
The pod's containers can additionally carry **attested workload identity** via a SPIRE/SPIFFE overlay — a **first-class optional overlay**, layered like the local-IdP choice. It is **not** required: the base pod meets the partner-receiver contract without it, and the easy deploy path stays an honest default. A partner who wants (or is required to have) cryptographically-attested workload identity reaches for the overlay; offering the simple default does not withhold the capability. SPIRE runs inside the pod (sovereignty-clean — goat hosts no partner SVIDs); federation exchanges only public trust bundles so the goat-side ingress gateway can verify the pod router's identity.
```

`docs/architecture.md:127` currently reads:
```
`profiles/<env>/` carries everything that differs between deployments (endpoints, ACL subject
axis, namespace, retention, goat-client mode). `efdi/` is concrete; `production/` is a
documented TODO seam (ADR 0844 unsettled). The compose stack + scripts are identical across
environments; only the profile + the signed bundle change. See
[`efdi-vs-production.md`](efdi-vs-production.md).
```
Replace with:
```
`profiles/<env>/` carries everything that differs between deployments (endpoints, ACL subject
axis, namespace, retention, goat-client mode). `efdi/` is concrete; `production/` is a
documented TODO seam (ADR 0844 unsettled). The compose stack + scripts are identical across
environments; only the profile + the signed bundle change.
```

`docs/architecture.md:144-145` currently reads:
```
The boundary contract (the seven requirements + Tier-2 ingress) is documented self-contained in
[`partner-contract.md`](partner-contract.md).
```
Delete these two lines entirely (including the blank line before them if it leaves two blank lines in a row — check with `cat -A` after editing).

- [ ] **Step 5: Fix dangling link — `examples/README.md`**

`examples/README.md:19` currently reads:
```
The last four are the "resilient / advanced" patterns — reach for them on the
streams that actually need catch-up, must-deliver, or presence. For loss-tolerant
telemetry, plain `put` + Tier-0 reconciliation is the right default; don't pay for
guarantees a stream doesn't need. For *which* reliability each stream needs and how
the fabric protects mission traffic, read [`../docs/quality-of-service.md`](../docs/quality-of-service.md).
```
Replace the last sentence, keeping the paragraph but dropping the dead link:
```
The last four are the "resilient / advanced" patterns — reach for them on the
streams that actually need catch-up, must-deliver, or presence. For loss-tolerant
telemetry, plain `put` + Tier-0 reconciliation is the right default; don't pay for
guarantees a stream doesn't need.
```

- [ ] **Step 6: Fix dangling links — `README.md`**

`README.md:67-71` currently reads (part of a directory-tree listing):
```
├── docs/
│   ├── partner-contract.md       boundary requirements + Tier-2 ingress
│   ├── architecture.md           bundle shape + data flow
│   └── efdi-vs-production.md     sandbox vs production delta table
├── start.sh                      interactive service launcher
├── stop.sh                       service teardown
└── logs/                         per-service log files (runtime)
```
Replace with (also drops the `logs/` line — logs no longer live in the repo, moved to `POD_STATE_DIR` in the prior repo-cleanup work):
```
├── docs/
│   └── architecture.md           bundle shape + data flow
├── start.sh                      interactive service launcher
└── stop.sh                       service teardown
```

`README.md:90` currently reads:
```
Sandbox shape differs from production. See [`docs/efdi-vs-production.md`](docs/efdi-vs-production.md) for the delta table.
```
Delete this line entirely.

- [ ] **Step 7: Fix dangling link — `tests/smoke/README.md`**

`tests/smoke/README.md:22-25` currently reads:
```
### 2. Live EFDI-sandbox validation (slow step, on the validation host)

Run the real `first-boot.sh efdi <bundle.cbor>` against the **live EFDI sandbox fabric**
(sandbox router directly; no partner-net, no release-bridge — see
`../../docs/efdi-vs-production.md`), then:
```
Replace with:
```
### 2. Live EFDI-sandbox validation (slow step, on the validation host)

Run the real `first-boot.sh efdi <bundle.cbor>` against the **live EFDI sandbox fabric**
(sandbox router directly; no partner-net, no release-bridge), then:
```

- [ ] **Step 8: Verify no dangling links remain**

```bash
grep -rln "partner-contract\.md\|efdi-vs-production\.md\|goat-client-v02-contract\.md\|workload-identity-spire-overlay\.md\|quality-of-service\.md\|proof-out-tracks\.md\|partner-run-guide\.md" /home/ndukve/IdeaProjects/efdi-moon-pod --include="*.md" 2>/dev/null
```
Expected: no output (all 7 docs are deleted, so any remaining hit would only be a filename mention in prose, not a real link — re-check by hand if anything shows up).

- [ ] **Step 9: Stage and stop (do not commit)**

```bash
git add -A docs/ CONTRIBUTING.md examples/README.md README.md tests/smoke/README.md
git status --short
```
Report the staged diff to the user. Do not run `git commit`.

---

### Task 5: Fix stale `audit-sink`/`goat-doctor` references in `docs/architecture.md`

**Files:**
- Modify: `docs/architecture.md` (lines 21, 26, 56-57, 64-67, 76, 97 — exact line numbers below assume Tasks 1-4 are already applied; re-run the `grep -n` in Step 1 to get current numbers before editing)

**Interfaces:** None. This task is narrowly scoped to removing/adjusting sentences that assert `audit-sink`/`goat-doctor` are currently-running components. It does **not** touch the broader goat-federation/trust-model content (Tier-1/Tier-2 peering, ADR 1020, receiver-side ingress gateway) — that content describes the fabric's trust model independent of whether this pod runs a local audit-sink, and stays as-is.

- [ ] **Step 1: Find current line numbers for every audit-sink/goat-doctor mention**

```bash
grep -n "audit-sink\|goat-doctor" /home/ndukve/IdeaProjects/efdi-moon-pod/docs/architecture.md
```
Use this output to locate the exact lines for each edit below (numbers may have shifted if this task runs after other edits).

- [ ] **Step 2: Fix the ASCII diagram**

Find the diagram block containing:
```
│    goat-doctor.timer   ← host systemd timer: goat doctor --json → status blob        │
│                          (goat-cli is no-daemon, ADR 1019; no container image)       │
│                                                                                      │
│  docker compose (network_mode: host — binds the pod's mesh IP directly):             │
│    ┌─────────────┐   ┌────────────┐                                                  │
│    │ zenoh-router│──▶│ audit-sink │   (published images, no local build)            │
│    └─────┬───────┘   └─────┬──────┘                                                  │
│          │ mTLS, ACL       │ subscribe <ns>/** over mTLS (observe-only in v0;        │
│          │ <ns>/**         │ durable NDJSON/MinIO is a v0.x follow-up)               │
│          ▼                 ▼                                                          │
│   (over the mesh maintained by goat-clientd)                                          │
```
Replace with:
```
│                                                                                      │
│  docker compose (network_mode: host — binds the pod's mesh IP directly):             │
│    ┌─────────────┐                                                                   │
│    │ zenoh-router│   (published image, no local build)                              │
│    └─────┬───────┘                                                                   │
│          │ mTLS, ACL                                                                 │
│          │ <ns>/**                                                                   │
│          ▼                                                                           │
│   (over the mesh maintained by goat-clientd)                                          │
```
(Keep the box-drawing character alignment consistent with the surrounding diagram lines — pad with spaces to match the existing box width.)

- [ ] **Step 3: Fix "Startup ordering" section**

Find:
```
3. compose: `zenoh-router` (dials the fabric over the mesh) → `audit-sink` (subscribes once the
   router is local). `goat-doctor` runs as a HOST systemd timer (not compose) since goat-cli is
   no-daemon and has no container image.
```
Replace with:
```
3. compose: `zenoh-router` dials the fabric over the mesh.
```

- [ ] **Step 4: Fix "Service images" section**

Find:
```
Both compose services use **already-published images** — no local Dockerfile/build context:
- `zenoh-router`: upstream `eclipse/zenoh:1.9.0` (digest-pinned, ADR 0858).
- `audit-sink`: the published `ghcr.io/desertgoat/goat-audit-subscriber` image (the standalone Rust
  Zenoh client), run in **observe-only** mode in v0 (subscribe + Prometheus metrics, no S3
  endpoint → no durable write). Durable local audit (NDJSON on the LUKS volume, or a co-located
  MinIO) is a documented v0.x follow-up.

The earlier `../audit` and `../doctor` build-context stubs were an oversight (no Dockerfiles →
`docker compose up` would fail); removed in favor of the published image + the host doctor timer.
```
Replace with:
```
The compose service uses an **already-published image** — no local Dockerfile/build context:
- `zenoh-router`: upstream `eclipse/zenoh:1.9.0` (digest-pinned, ADR 0858).
```

- [ ] **Step 5: Fix "Data flow" outbound bullet**

Find:
```
- **Outbound** (partner → fabric): partner publishes under `release/<partner>/**`; the pod's
  Zenoh router carries it over the mesh **to the goat-side ingress gateway** (see "Receiver
  side" below), not directly into a core router. Audited locally by `audit-sink`.
```
Replace with:
```
- **Outbound** (partner → fabric): partner publishes under `release/<partner>/**`; the pod's
  Zenoh router carries it over the mesh **to the goat-side ingress gateway** (see "Receiver
  side" below), not directly into a core router.
```

- [ ] **Step 6: Fix the "independent of local audit-sink" line**

Find:
```
- Every admitted sample carries provenance (verified peering identity, original key,
  timestamps) into the goat audit trail — independent of, and complementary to, the pod's own
  local audit-sink.
```
Replace with:
```
- Every admitted sample carries provenance (verified peering identity, original key,
  timestamps) into the goat audit trail.
```

- [ ] **Step 7: Verify — no dangling references**

```bash
grep -n "audit-sink\|goat-doctor" /home/ndukve/IdeaProjects/efdi-moon-pod/docs/architecture.md
```
Expected: no output.

- [ ] **Step 8: Stage and stop (do not commit)**

```bash
git add docs/architecture.md
git status --short
```
Report the staged diff to the user. Do not run `git commit`.

---

### Task 6: Full non-regression pass

**Files:** None modified — this task only runs verification against everything Tasks 1-5 changed.

**Interfaces:** N/A — this is the acceptance test from the spec's "Non-regression (hard requirement)" section.

- [ ] **Step 1: Baseline — confirm the stack currently delivers CoT end to end**

```bash
cd /home/ndukve/IdeaProjects/efdi-moon-pod
./stop.sh
./start.sh
```
Select at minimum: `zenoh`, `asterix` (if `CAT48_PORT` is set), `cot-udp` and/or `cot-tcp`, `track-fusion`. Confirm in the printed output that each selected service shows `[start]` with a PID (not an error).

- [ ] **Step 2: Confirm tracks are actually flowing**

```bash
tail -20 "${POD_STATE_DIR:-$HOME/goat-moon}/logs/cot-tcp.log" 2>/dev/null || tail -20 "${POD_STATE_DIR:-$HOME/goat-moon}/logs/cot-udp.log"
```
Expected: no `error`/`refused`/`Traceback` lines; if verbose logging was enabled, confirm `CoT ...` send lines are present. If TAK Server/WinTAK is reachable, visually confirm tracks/markers are showing (matches the manual check already done earlier in this project).

- [ ] **Step 3: Confirm no bridge crashed on startup because of a missing file**

```bash
for f in "${POD_STATE_DIR:-$HOME/goat-moon}"/logs/*.log; do
  echo "=== $(basename "$f") ==="
  tail -5 "$f" | grep -iE "error|traceback|no such file" && echo "  ^^ CHECK THIS" || echo "  clean"
done
```
Expected: every service reports "clean" — nothing references a file deleted in Tasks 1-5 (in particular: no bridge should ever have referenced `register_topics.sh`, `goat-doctor.sh`, or any deleted doc, so this should already be clean, but this step exists to catch anything unexpected).

- [ ] **Step 4: Stop cleanly**

```bash
./stop.sh
```

- [ ] **Step 5: Report to the user**

Summarize: what was deleted (Tasks 1-4), what was fixed for consistency (Task 5), and confirm the non-regression pass (Task 6) found no issues. Do not commit anything — all changes from Tasks 1-5 remain staged, individually, for the user to review and commit themselves.
