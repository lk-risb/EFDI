# Partner Certificate Enrollment Portal — Design

## Goal

Replace goat-client's onboarding stack (CBOR bundle, offline CA signing, cross-platform GUI app, cosign/OIDC-signed releases) with a minimal, web-based self-service certificate enrollment portal built into the existing zenoh-admin GUI, backed by the team's existing step-ca root CA on node-1. NetBird mesh enrollment stays separate, handled by NetBird's own existing self-hosted dashboard — not duplicated.

## Why

This session surfaced two facts that motivate this design:

1. goat-client's onboarding stack solves a real problem — enrolling untrusted, non-technical, external partner organizations at arm's length, with tamper-proof enrollment and no terminal use. It does this with a signed CBOR bundle format, a cross-platform daemon+GUI+mobile client, and cosign/OIDC-signed releases.
2. This team's actual situation doesn't need most of that complexity: the team already runs its own step-ca root CA (node-1) and already self-hosts NetBird for the same team. Today's cross-sandbox connectivity failure (the `zenoh2` cert `UnknownIssuer` error) was directly caused by two independent, ad-hoc enrollment paths issuing certs from different CAs — exactly the class of problem a shared, simple internal enrollment flow prevents going forward.

## Non-goals

- Not replacing NetBird mesh enrollment — NetBird's own dashboard already has a non-technical, web-based peer-invite/setup-key flow. This design does not wrap or duplicate it.
- Not building any custom client software, GUI app, daemon, or mobile app.
- Not implementing offline/air-gapped CA signing — step-ca is assumed always reachable to this portal at enrollment time.

## Architecture

A new feature added to the existing zenoh-admin app (FastAPI backend + React frontend + Postgres, all already built this session). No new service, no new deployment surface.

## Components

**Database**: new `invites` table — `id`, `token_hash`, `partner_name`, `namespace`, `created_by` (references `admin_users.id`), `created_at`, `expires_at`, `used_at` (nullable).

**Backend** (`compose/zenoh-admin/api/enrollment.py`):

- `POST /api/enrollment/invites` (superadmin) — body: `partner_name`, `namespace`, `expires_in_hours` (default 48). Generates a random token, stores its hash, returns the invite link with the token in cleartext (shown once, not retrievable again).
- `GET /api/enrollment/invites` (superadmin) — lists all invites with status (pending / used / expired) for visibility.
- `GET /api/enroll/{token}` (public, no auth) — validates the token (exists, not expired, not used); returns `partner_name` + status for the frontend to render.
- `POST /api/enroll/{token}` (public, no auth) — the enrollment action:
  1. Re-validate the token.
  2. Generate an EC keypair + CSR in memory (CN = namespace).
  3. Call step-ca to sign the CSR (exact provisioner/auth mechanism is an open question below).
  4. Render a `config.json5` snippet, reusing the template-substitution approach already built in `compose/zenoh-admin/api/config.py`, with the new namespace filled in.
  5. Mark the invite used (`used_at = now`). If step-ca signing fails, do **not** mark it used — the invite stays retryable.
  6. Return a single bundle (zip): `cert.pem`, `key.pem`, `ca-root.pem`, `config.json5`. This is the only point the private key exists outside memory — never persisted server-side, included once in the response body, then discarded.

**Frontend**:

- `/admin/enrollment` (superadmin-only, new sidebar entry) — form to create an invite; table listing existing invites with a copy-link action.
- `/enroll/:token` (public route, no `Layout`/sidebar, no auth guard) — minimal page: partner name, one "Enroll" button, then a download link once complete.

## Data flow

1. Superadmin creates an invite for a partner + namespace → link generated.
2. Superadmin shares the link out-of-band (email, chat — however they already do it today).
3. Partner opens the link, sees their name, clicks Enroll. Zero terminal use.
4. Backend generates a keypair+CSR, gets it signed by step-ca, builds the config, marks the invite used, returns a downloadable bundle.
5. Partner downloads the bundle and drops cert/key/config into their own zenoh-router setup — same manual placement step as today, just replacing "someone hand-copied a cert" with self-service download.
6. NetBird mesh enrollment is handled separately, via NetBird's own dashboard — not part of this flow.

## Security

- Token is single-use, time-limited, and delivered out-of-band — never logged in cleartext after creation.
- Private key is generated in memory only, included once in the response body, never written to disk or the database.
- Enrollment endpoints run over the same TLS the rest of zenoh-admin uses.
- An audit log entry is written on invite creation and on successful enrollment, reusing the existing `write_audit` helper.

## Error handling

- Expired or already-used token → `410 Gone` with a clear message on the partner-facing page.
- step-ca unreachable or signing failure → `502`, invite remains usable for retry.
- Malformed/missing namespace at invite-creation time → `400` (superadmin-facing, not partner-facing).

## Testing

- Unit: invite creation, expiry logic, single-use enforcement.
- Integration: full enroll flow against a real (or test) step-ca instance, confirming the returned cert validates against the CA root.
- Manual: create an invite, open the link in a browser, confirm the whole flow end-to-end, confirm the resulting cert/config actually works with a real zenoh-router.

## Open questions for implementation-plan time

- Exact step-ca provisioner type node-1 uses, and how zenoh-admin authenticates to it (JWK provisioner password? admin API token? a per-invite one-time-token minted at invite-creation time?).
- Whether the `/enroll/:token` route needs any additional brute-force protection beyond token length/randomness (rate limiting, etc.).
