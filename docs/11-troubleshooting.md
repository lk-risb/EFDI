# 11 — Troubleshooting

## 11.1 Symptom-first fixes

Symptom-first fixes for the most common deployment problems. For
infrastructure-level lessons learned (DNS, TLS profiles, atomic writes —
things that don't fit a single symptom), see [§11.2 Gotchas](#112-gotchas) below.

### Zenoh connection failure

**Symptom:** `zenoh.ZError: Unable to connect to any of [tls/zenoh.efdi...]`

```bash
# 1. Verify router is healthy
docker compose -f compose/docker-compose.yml ps zenoh-router

# 2. Verify endpoint variable is set
echo $ZENOH_LOCAL_ENDPOINT   # expected: tcp/127.0.0.1:7448

# 3. Verify certificate files exist
ls $EFDI_CERT_DIR/*.pem
```

If `compose/.env` was loaded with bare `source compose/.env`, variables are not exported to child processes. Use `./start.sh` (which handles this), or:

```bash
set -a && source compose/.env && set +a
```

### No tracks visible in ATAK

```bash
# 1. Confirm tak-layer is running
kill -0 $(cat $POD_STATE_DIR/.pids/tak-layer.pid) && echo running

# 2. Confirm the TAK Server connection is established
ss -tn "( dport = :$TAK_PORT )"

# 3. Confirm TAK_HOST/TAK_PORT/TAK_TLS in .env match the TAK Server's actual endpoint
```

### CAT-34 radar marker is missing

The radar has not transmitted CAT-34 I034/120 (3D-Position), so EFDI cannot
place the site safely. Check the CAT-34 log for `has no site position`. Prefer
enabling I034/120 on the radar/gateway; for a single radar only, set fallback
coordinates in `.env`:

```bash
grep CAT34_RADAR compose/.env
```

### Drone detections not publishing

The bridge discards detections older than 300 s. Verify API connectivity and data freshness:

```bash
curl -s -H "Origin: https://dronuradaras.lt" \
  https://radar-api.mainline.inc/api/v1/public/detections \
  | python3 -c "
import sys, json, time
d = json.load(sys.stdin).get('detections', [])
now = time.time()
fresh = [x for x in d if (now - x.get('detected_at', 0)/1000) < 300]
print(f'{len(fresh)} fresh / {len(d)} total detections')
"
```

### SitaWare units not appearing in ATAK

**1. Verify the bridge is running and polling:**

```bash
tail -f $POD_STATE_DIR/logs/sitaware.log
# Expected: "SitaWare poll: N units published" every SITAWARE_POLL_S seconds
```

**2. Verify credentials and endpoint:**

```bash
curl -s -u "$SITAWARE_USER:$SITAWARE_PASS" "$SITAWARE_URL/..." | python3 -m json.tool | head -20
```

**3. SIDC not mapped — unit appears with wrong icon or not at all:**

SitaWare units without a valid 15-character SIDC are routed to `…/land/sitaware/c2/unknown/unit/…` and rendered as unknown ground units (`a-u-G-U-C`). Check the raw SIDC value in the log:

```bash
grep "sidc=" $POD_STATE_DIR/logs/sitaware.log | head -10
```

### EFDI tracks not appearing in SitaWare HQ

```bash
tail -f $POD_STATE_DIR/logs/sitaware-hq-nvg.log
curl -u "$SITAWARE_HQ_NVG_USER:$SITAWARE_HQ_NVG_PASS" \
  -o /dev/null -w '%{http_code} %{content_type}\n' \
  "http://127.0.0.1:${SITAWARE_HQ_NVG_PORT:-8088}${SITAWARE_HQ_NVG_PATH:-/nvg}"
```

Expected status is `200 application/xml`. In the HQ NVG manager, verify the subscription is unpaused, connected, polling the EFDI host address (not the HQ address), and targets `efdi-live / EFDI Live Tracks`. If TLS is configured, omit `-k` after the issuing CA is trusted. A local `200` plus an HQ connection failure indicates routing, Windows firewall, Linux firewall, or certificate trust—not an NVG conversion failure.

The **Latest replication** timestamp must advance. If it remains old and
**Reload** reports an unknown error, test the same URL from PowerShell on the HQ
host. A connection failure is routing/firewall; HTTP 401 is missing or stale
subscription credentials; success only with `-k` means the feed CA is not
trusted by the account/service performing the import. Fix replication before
replacing a legacy layer, otherwise the replacement layer will remain empty.

The authenticated health endpoint provides server-side evidence without
logging credentials or NVG payloads:

```bash
curl -ksS -u "$SITAWARE_HQ_NVG_USER:$SITAWARE_HQ_NVG_PASS" \
  "https://127.0.0.1:${SITAWARE_HQ_NVG_PORT:-8088}/healthz" | python3 -m json.tool
```

- `successful_requests` remains zero: HQ has not reached the feed.
- `unauthorized_requests` increases: HQ reached it with missing/stale Basic
  credentials.
- `successful_requests` increases while HQ remains Pending: investigate NVG
  parsing or the selected target layer rather than routing or authentication.

Feed access logs contain only the outcome, track count, and client address and
are rate-limited to one line per minute for successful and unauthorized pulls.

### Duplicate process instances

Caused by running `start.sh` twice without stopping:

```bash
pkill -f "_bridge\.py\|tak_layer\|track_fusion"
rm -f $POD_STATE_DIR/.pids/*.pid
./start.sh
```

### Radar icon disappearing from ATAK

The `asterix` bridge publishes a keepalive every 60 s regardless of track activity. If the icon disappears, the bridge has stopped:

```bash
tail -20 $POD_STATE_DIR/logs/asterix.log | grep -E "keepalive|startup|error"
```

## 11.2 Gotchas

This is the *operational/infrastructure* companion to
[`../.ai/.claude/CLAUDE.md`](../.ai/.claude/CLAUDE.md)'s ASTERIX bit-level decode gotchas, and to
[§11.1 Troubleshooting's symptom-first fixes](#111-symptom-first-fixes). Everything
here was a real, confirmed issue hit while running this pod — read it before
debugging something that looks like one of these symptoms, so the same
diagnosis doesn't have to be re-earned.

### NetBird split-DNS is invisible inside containers

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

### A TLS/mTLS identity profile must match the endpoint it dials

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

### A bind-mounted single file breaks atomic writes

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

### Identically-named duplicate function definitions silently shadow

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

### A service bundle needs its own status aggregation

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

### A bind-mounted state file is owned by the wrong user, not just badly mounted

**Symptom:** Saving a config from the WebUI fails with `[Errno 13] Permission
denied: '/data-topic-prefix'` (or `/namespace-prefix`, or the TAK/SitaWare
credential upload directories) — a different failure from the `EBUSY`
atomic-write gotcha above; this one is a plain permission error, not a
rename-over-mountpoint error.

**Cause:** `zenoh-admin` always runs as a fixed non-root uid/gid (`10001`).
Several state paths are individually bind-mounted files or directories
(`namespace-prefix`, `data-topic-prefix`, `integrations/tak`,
`$BUNDLE_DIR/efdi`) created on the **host** side by whichever user ran
`install.sh`/`reinstall.sh` — typically root. A file created by root at mode
`644` is owner-writable only by root; uid 10001 has neither the owner bit nor
(unless the group happens to already be 10001) the group-write bit, so every
write from inside the container fails.

**Fix:** `install.sh` and `reinstall.sh` now `chgrp 10001` + `chmod 664`
(files) / `775` (directories) on every one of these paths after creating
them. If you're troubleshooting a box that was set up before this existed,
either re-run `reinstall.sh`, or fix it directly:

```bash
chgrp 10001 "$POD_STATE_DIR/namespace-prefix" "$POD_STATE_DIR/data-topic-prefix"
chmod 664   "$POD_STATE_DIR/namespace-prefix" "$POD_STATE_DIR/data-topic-prefix"
chgrp 10001 "$POD_STATE_DIR/integrations/tak" "$BUNDLE_DIR/efdi"
chmod 775   "$POD_STATE_DIR/integrations/tak" "$BUNDLE_DIR/efdi"
```

`health.sh`'s interactive menu (option 3, "check for missing/misconfigured
state files") now detects and auto-fixes wrong permissions on these paths
too, not just missing files.

### An unhandled exception in an API handler surfaces as a bare, content-free HTTP 500

**Symptom:** The WebUI shows a plain `Unexpected error applying config` or
`Request failed (HTTP 422)` toast with no further detail — no hint at all
about what actually broke, even though the server *did* hit a real,
specific error.

**Cause:** A handler that catches `Exception` only to log it and then
bare-`raise`s loses the original message once FastAPI's default error
handler takes over — the client only ever sees a generic status code, and if
the underlying failure itself returned an empty string as its "detail" (for
example, a subprocess that crashed before writing anything to
stdout/stderr), even a well-behaved re-raise has nothing useful to show.

**Fix:** Convert an unexpected exception into an `HTTPException` carrying the
real message (`raise HTTPException(500, detail=f"...: {exc}") from exc`), and
make any code path that reports a subprocess/preflight failure fall back to a
descriptive placeholder (`"exited N with no output"`) instead of an empty
string, so the *next* occurrence is diagnosable from the response body alone
— no server log access required.

### A form field silently accepts a value shaped completely differently than it needs

**Symptom:** A "bind address" or similarly single-purpose config field is
given a full URL (`http://0.0.0.0:8088/nvg`) or the wrong host's IP instead of
a bare local IP, and the service that reads it crashes with something
unhelpful like `socket.gaierror: [Errno -2] Name or service not known` at
`socket.bind()`.

**Cause:** A bind address is passed straight to `socket.bind((host, port))`
— it is never a URL (no scheme, no port, no path) and it is *this host's own*
listening address, not the address of whatever remote system will connect to
it. Nothing in a plain text input stops a user from typing a full URL or the
wrong machine's address; the failure only appears downstream, inside a
library call, several layers away from the field that caused it.

**Fix:** For `SITAWARE_HQ_NVG_BIND` specifically, the value must be a bare IP
— `0.0.0.0` to listen on every interface (so a *different* machine, like the
SitaWare HQ box, can reach it), or `127.0.0.1` for loopback-only. Port and
path are separate fields; don't fold them in. More generally: when a field
crashes far from where it's set, check its raw stored value first
(`grep KEY .env`) before chasing the crash site.

### A newly-working feed still gets rejected — check auth before assuming routing is broken

**Symptom:** A remote system can now reach a feed (no more connection
refused/timeout), but every request is still rejected, and the feed's own
log says something like `rejected unauthorized request from <ip>`.

**Cause:** Getting past connectivity (right port, right bind address) is a
separate problem from authentication. A feed with Basic Auth configured
(`SITAWARE_HQ_NVG_USER`/`_PASS`) rejects any request that doesn't present
matching credentials — including from a client that's otherwise perfectly
reachable.

**Fix:** Either configure the *remote* side (the SitaWare HQ import
subscription, in this case) with the same username/password as the feed, or
— for a quick isolated-lab test only — set `SITAWARE_HQ_NVG_ALLOW_ANONYMOUS=1`
to skip auth entirely while you confirm data flows.

### Tracks flash / disappear and reappear on a fixed cycle

**Symptom:** Objects on a pull-based feed (e.g. SitaWare's NVG import) blink
in and out of existence at a period that lines up with the polling interval,
even though the underlying source is genuinely still reporting.

**Cause:** A feed cache drops any entity that hasn't been refreshed within
its staleness window (`SITAWARE_HQ_NVG_STALE_S`, or the equivalent on any
other pull-based layer). If the *upstream* bridge only refreshes a given
entity every N seconds (for example, `dronuradaras_bridge.py`'s
`DEVICE_POLL_S = 60` for radar-node positions) and the staleness window is
shorter than that, every entity goes stale and vanishes for part of every
upstream cycle, then reappears once the next refresh lands — a rolling,
staggered flicker rather than a clean, simultaneous one.

**Fix:** Set the feed's staleness threshold comfortably above the slowest
upstream refresh interval that feeds it (at least 2×) — e.g. `120` for a
60-second upstream cycle. Separately, a downstream C2 system's own "layer
expiration"/"track persistence" setting (SitaWare's Layer Details page has
both) can compound or mask this; if raising the staleness threshold alone
doesn't fix it, check that setting too.

### `pip install` fails with "externally-managed-environment"

**Symptom:** Running `pip install -r requirements.txt` directly against the
system `python3` (rather than through `install.sh`/`start.sh`) fails with
`error: externally-managed-environment` / `This environment is externally
managed` (PEP 668, common on modern Debian/Ubuntu).

**Cause:** The system Python is intentionally locked down against
unmanaged `pip install`s. `install.sh` and `start.sh` don't fight this — they
create and use their own virtualenv at `compose/venv`, which every
Python-based host service in this pod actually runs from.

**Fix:** Use that venv directly instead of the system interpreter:

```bash
compose/venv/bin/pip install -r compose/requirements.txt
compose/venv/bin/python3 layers/some_layer.py
```

If `compose/venv` doesn't exist yet, create it the same way `install.sh`
does: `python3 -m venv compose/venv`, then install into it. Never pass
`--break-system-packages` to the system `pip` — every other host-native
service already expects the venv, not the system interpreter.

### Two different "Save" buttons on the same page do different things

**Symptom:** Saving a value in one section of the WebUI's config page (e.g.
an Integration Settings field like a SitaWare or TAK setting) appears to do
nothing, or triggers an unrelated error (a Zenoh router validation failure)
that has nothing to do with what was actually being changed.

**Cause:** The Zenoh Config page hosts two independent save actions: a
top-level **"Save & Restart"** that validates and applies the *Zenoh router*
config (mTLS port, fabric endpoints, namespace), and each Integration
Settings card's *own* Save button, which only writes that card's `.env`
values and never touches the router config. Clicking the wrong one saves
nothing relevant, and — if the router config happens to be in a
not-yet-valid state (e.g. certificates not yet uploaded) — surfaces a
confusing, unrelated 422/500.

**Fix:** Match the button to the section: router-level fields under
"Zenoh Config" (Transport, Fabric endpoints, Namespace) need the top
"Save & Restart"; every field inside an Integration Settings card (TAK,
SitaWare, sensor feeds, etc.) needs that card's own Save button further down
the page.

### A code fix isn't live until the running process restarts

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

---
