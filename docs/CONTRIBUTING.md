# Contributing to EFDI

This repo is EFDI's shared surface for partners building against the pod. It is
deliberately **self-contained and open** — everything here is safe to read, fork, and build
against without any internal access.

## The one hard rule: no secrets or internal infra details land here

This repo is visible to external partners. Do **not** commit anything that belongs to EFDI's
internal infrastructure. Concretely, a change must not introduce:

- Links to or paths inside private internal repos, ops playbooks, or infra-provisioning tooling
  that isn't part of this stack.
- Internal hostnames, mesh IPs, cloud account names, bucket names, cloud CLI profiles, Terraform
  paths, or operator-provisioning playbooks that belong to a specific real deployment.
- Secrets of any kind — NetBird setup keys, certs, private keys, tokens, API keys. Certs are never
  committed (see `scripts/gen-certs.sh`); `compose/.env` and `BUNDLE_DIR` are gitignored — do not
  defeat it. Bridges read the partner namespace from the `PARTNER_NAMESPACE` env var, never
  hardcoded.
- Stack-specific values hardcoded into the pod scaffold (see "No over-specification" below).

If you need to reference an internal decision, describe it in plain prose or summarize it into a
self-contained doc under `docs/`.

## No over-specification against any stack

The pod runs against multiple environments (sandbox today; production later). The compose stack,
host scripts, and config templates **hardcode nothing stack-specific**. Every environment value —
the fabric router endpoint, the mTLS identity, the CA roots, the partner namespace — arrives at
runtime from `compose/.env` (populated by hand from `compose/.env.example`, the source of truth
for a given deployment) and the certs `scripts/gen-certs.sh <namespace>` generates.

When you add a feature, ask: *does this bake one specific deployment's assumption into the pod?*
If so, lift it into an env var — see `compose/.env.example` for what already varies per deployment
and where it legitimately lives.

## How we work

- **Branch + PR.** Open a PR against `main`; keep changes scoped and reviewable.
- **Conventional Commits**, signed-off (`git commit -s`) for DCO.
- **Keep it runnable.** `tests/check-images-public.sh` must pass (every image anonymously
  pullable), and the smoke test in `tests/smoke/` should keep describing a real bidirectional
  round-trip.
- **License.** Contributions are under Apache-2.0 (see [`LICENSE`](../LICENSE)).

## Where things live

- `docs/EXPLAINED.md` — the pod's data flow.
- `clients/` — the connect SDKs + worked examples partners build against.
- `compose/.env.example` — the per-deployment config (the only place stack-specific values belong).
- `host/`, `compose/` — the stack-agnostic pod scaffold.
