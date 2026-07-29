# Operational gotchas — lessons already paid for

This is the *operational/infrastructure* companion to
[`../CLAUDE.md`](../CLAUDE.md)'s ASTERIX bit-level decode gotchas, and to
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)'s symptom-first fixes. Everything
here was a real, confirmed issue hit while running this pod — read it before
debugging something that looks like one of these symptoms, so the same
diagnosis doesn't have to be re-earned.

## NetBird split-DNS is invisible inside containers

**Symptom:** A router config points at a mesh hostname (e.g.
`zenoh2.efdi.ltu`); the container never even attempts a connection — no
socket, no TLS error, just silence.

**Cause:** `network_mode: host` shares the network *namespace*, not
`/etc/resolv.conf`. NetBird's split-DNS resolver for its mesh domain runs on
the **host** only; a container still gets Docker's own generated resolver
(typically your LAN's DNS), which has never heard of the mesh domain. The
hostname resolves on the host (`getent hosts` works fine there) and silently
fails to resolve in the container — which looks identical to "nothing is
trying to connect."

**Fix:** Add explicit `extra_hosts` entries mapping each mesh hostname to its
current NetBird IP in the container's compose service. Domain names stay in
the application config; only the container's local hosts-resolution needs
the mapping. Re-add/update these if NetBird ever reassigns the IPs.

## A TLS/mTLS identity profile must match the endpoint it dials

**Symptom:** A fabric connection attempt produces no error and no link —
looks identical to the DNS issue above, or to a firewall block.

**Cause:** Each remote fabric (backbone, a partner's sandbox, this pod's own
local mesh) is signed by a **different CA**. Pointing the right endpoint at
the wrong certificate identity fails the mTLS handshake, and depending on the
failure mode this can look like nothing happened at all rather than a clear
rejection.

**Fix:** Endpoint and TLS identity profile are one atomic choice, never
adjusted independently. If your tooling offers presets, bundle the endpoint
and the matching certificate profile into a single preset rather than two
separate fields a person can mismatch.

## A bind-mounted single file breaks atomic writes

**Symptom:** A config-apply endpoint that writes a small state file (e.g. a
namespace-prefix file) fails with `OSError: [Errno 16] Device or resource
busy`, even though writing the main config file right next to it works fine.

**Cause:** The standard "atomic write" pattern is write-to-temp-file then
`os.replace(temp, target)` — the rename is what guarantees a reader never
sees a half-written file. That rename fails when `target` is itself a
single-file Docker bind mount (`-v host/file:/container/file`): the path *is*
a mount point, and you cannot rename over a mount point. A directory-mounted
file doesn't have this problem, because the rename happens inside the
mounted directory, not over the mount itself.

**Fix:** Fall back to an in-place rewrite (open-write-fsync, no rename) when
`os.replace` fails with `EBUSY`. It's not atomic, but it's the only option
for a bind-mounted single file, and it beats failing the whole apply for an
unrelated file.

## Identically-named duplicate function definitions silently shadow

**Symptom:** A decoder/handler looks obviously wrong when you read it (wrong
field width, wrong scale, a bug that should be very visible in output) — but
production data coming out the other end looks fine.

**Cause:** Python allows redefining a function at module scope with no
warning. If a file has `def handler(...)` twice, the **second** definition
silently wins — the first becomes 100% dead code that still *looks* live
(same indentation, no guard, often even both correctly documented). No
linter in this repo's toolchain flags this by default. This is exactly how a
genuinely broken code path can sit in a file for a long time without ever
affecting anything, and it can cost real debugging time when the "obviously
buggy" copy is the one a human reads first.

**Fix:** Before trusting that a function you're reading is the one that
actually runs, confirm at runtime: `inspect.getsourcelines(module.the_func)`
tells you which definition's line number is actually bound. If a repo has
grown organically (categories/variants added over time, each with "their
own" copy of similar logic), grep for the function name across the whole
file — not just the one you found first — whenever something doesn't add up.

## A service bundle needs its own status aggregation

**Symptom:** A WebUI or status endpoint shows a multi-process bundle
(several children under one logical "service") as permanently stopped, even
though every child process is actually running.

**Cause:** Generic per-service status logic that checks for one pidfile named
after the service will never find it if the bundle launcher writes one
pidfile *per child* instead (e.g. `asterix-cat10.pid`, `asterix-cat48.pid`,
...). The bundle itself has no pidfile, so it always reads "stopped."

**Fix:** A bundle service needs bespoke status logic that enumerates and
aggregates its children's pidfiles, reporting running/degraded/stopped based
on how many are alive — not a naive single-pidfile check.

## A code fix isn't live until the running process restarts

**Symptom:** You fix a bug (in a decoder, in an admin API, anywhere), confirm
the file changed on disk, and the running system's behavior doesn't change —
or a WebUI keeps listing services/data that were just removed from the code.

**Cause:** Editing a `.py` file has zero effect on an already-running
interpreter holding the old bytecode in memory. This sounds obvious stated
plainly, but it's an easy thing to forget mid-investigation when several
files are being edited in sequence and it's not obvious *which* running
process is stale.

**Fix:** After any fix to a long-running service's code, restart that
specific process (not just recompile/test it) before concluding the fix
didn't work, and before reporting a symptom as still-unresolved.
