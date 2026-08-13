# 11 — Troubleshooting

### 11.1 Symptom-first fixes

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

### 11.2 Gotchas

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

