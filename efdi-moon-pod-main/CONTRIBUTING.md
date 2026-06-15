# Contributing to efdi-moon-pod

This repo is the shared surface where goat and EFDI partners co-develop the moon-pod. It is
deliberately **self-contained and open** — everything here is safe to read, fork, and build
against without goat-internal access.

## The one hard rule: nothing goat-internal lands here

This repo is visible to external partners. Do **not** commit anything that belongs to the goat
internal substrate. Concretely, a change must not introduce:

- Links to or paths inside the private goat monorepo (`github.com/dlf-dds/...`, `docs/design/...`,
  `ops/ansible/...`, ADR/finding *files* — citing an ADR *number* in prose is fine).
- Internal hostnames, mesh IPs, AWS account names, S3 bucket names, `aws-vault` profiles,
  Terraform paths, or operator-provisioning playbooks. The pod is the *partner* side; how goat
  mints bundles stays goat-internal.
- Secrets of any kind — setup-keys, certs, private keys, tokens, bundle CBOR. The `.gitignore`
  blocks the obvious ones; do not defeat it.
- Stack-specific values hardcoded into the pod scaffold (see "No over-specification" below).

If you need to reference an internal decision, describe it in plain prose or summarize it into a
self-contained doc under `docs/` (as `docs/partner-contract.md` does for the boundary contract).

## No over-specification against any stack

The pod runs against multiple goatnets (EFDI sandbox today; production later). The compose stack,
host scripts, and config templates **hardcode nothing stack-specific**. Every environment value —
the fabric router endpoint, the mesh mgmt URL + setup-key, the mTLS identity, the CA roots —
arrives at runtime from the **signed join bundle** (the source of truth) or, for the handful of
values the bundle does not carry, the active **profile** under `profiles/<env>/`.

When you add a feature, ask: *does this bake an EFDI / jailbreak / production assumption into the
pod?* If so, lift it into the bundle contract or a profile var. The deltas table in
[`docs/efdi-vs-production.md`](docs/efdi-vs-production.md) is the reference for what varies per
stack and where it legitimately lives.

## How we work

- **Branch + PR.** Open a PR against `main`; keep changes scoped and reviewable.
- **Conventional Commits**, signed-off (`git commit -s`) for DCO.
- **Keep it runnable.** `tests/check-images-public.sh` must pass (every image anonymously
  pullable), and the smoke test in `tests/smoke/` should keep describing a real bidirectional
  round-trip.
- **License.** Contributions are under Apache-2.0 (see [`LICENSE`](LICENSE)).

## Where things live

- `docs/partner-contract.md` — the boundary contract a pod meets (self-contained).
- `docs/architecture.md` — the bundle shape + data flow.
- `docs/partner-run-guide.md` — how a partner runs a pod.
- `docs/efdi-vs-production.md` — what varies per stack and how the pod stays agnostic.
- `clients/` — the connect SDKs + worked examples partners build against.
- `profiles/` — per-environment config (the only place stack-specific values belong).
- `host/`, `compose/` — the stack-agnostic pod scaffold.
