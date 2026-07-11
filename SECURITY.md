# Security Policy

## Supported Versions

This repo doesn't tag releases — `main` is the only supported branch. Security fixes land there; there is nothing older to backport to.

<!-- Once releases are tagged, replace the paragraph above with:

| Version | Supported |
|---------|-----------|
| latest (`vX.Y.Z`) | ✅ |
| < vX.Y.Z | ❌ |

-->

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Use [GitHub Security Advisories](https://github.com/risblicencijos/EFDI/security/advisories/new) to report privately — this keeps the report confidential until a fix ships.

Include, where possible:
- Affected component (a specific bridge/layer, the Zenoh router config, the zenoh-admin API/UI, `start.sh`/deployment scripts)
- Steps to reproduce
- Impact (what an attacker could actually do — e.g. cross-namespace publish, credential exposure, auth bypass)

You should receive an acknowledgement within a few days. There's no fixed SLA — this is a small, partner-operated project — but reports are taken seriously and fixes are prioritized by severity.

## Scope

This repo covers the sensor bridges/layers (`compose/bridge/`), the Zenoh router deployment (`compose/docker-compose.yml`), the zenoh-admin panel (`compose/zenoh-admin/api`, `compose/zenoh-admin/ui`), and the `start.sh`/`stop.sh` launcher scripts. It does not cover vulnerabilities in upstream dependencies (Zenoh itself, ATAK/TAK Server, SitaWare) — report those to their respective maintainers.

## Notes for Reviewers

- Transport is mutual TLS between pod and fabric; each pod is ACL-scoped to write only within its assigned namespace (`<slot_id>/**`) — publishes outside it are silently denied by the router.
- Certificates are never committed (`compose/certs/`, `compose/.env` are gitignored) and are mounted read-only into containers that need them.
- The zenoh-admin panel is JWT-authenticated with role-based access (`superadmin`/`admin`/`readonly`), bcrypt password hashing, lockout after repeated failed logins, and an audit log for admin actions.
- Logo/file uploads in the admin panel are extension-allowlisted and size-capped.
