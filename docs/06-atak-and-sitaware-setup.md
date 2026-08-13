# 06 — ATAK & SitaWare Setup

## ATAK Setup

### UDP multicast (same-subnet deployments)

1. **Settings → Network → Multicast** — enable multicast receiver
2. Verify `239.2.3.1:6969` appears in the address list
3. Tracks should appear within one poll cycle (≤ 10 s for drone detections, ≤ 60 s for radar keepalive)

### TAK Server

Set `TAK_HOST` and `TAK_PORT` in `.env`, then select `tak-layer` in the launcher.

### SitaWare HQ REST tracking (optional inbound adapter)

Use `sitaware` only when the target deployment documents a compatible JSON unit resource and authentication method. A `/rest/v2/*` servlet mapping does not imply that `/rest/v2/units` exists; that guessed resource returns 404 on the verified HQ 6.22 installation.

Leave `SITAWARE_URL`/`SITAWARE_USER`/`SITAWARE_PASS` unset in `.env` and the launcher prompts for the server address and login (username, then hidden password input) each time you select `sitaware` — or pre-fill them in `.env` to skip the prompt. (A second address can still be set via `SITAWARE_URL_FALLBACK` directly in `.env` for a genuine LAN-vs-mesh split — the interactive prompt only asks for one.)

**`.env` fields:**

```bash
SITAWARE_URL=https://<sitaware-host>
SITAWARE_URL_FALLBACK=https://swhq.efdi.ltu:10006 # optional stable mesh-DNS path
SITAWARE_USER=<username>
SITAWARE_PASS=<password>
SITAWARE_API_PATH=/<documented-resource-path>
SITAWARE_POLL_S=10   # optional — poll interval in seconds (default 10)
```

The bridge reads MIL-STD-2525B SIDC codes from SitaWare and routes each unit to the correct Zenoh topic by affiliation and battle dimension:

| SIDC affiliation | SIDC dimension | Zenoh topic path | ATAK CoT type |
| --- | --- | --- | --- |
| Friendly / Assumed Friendly | Ground (G) | `…/land/sitaware/c2/friendly/unit/…` | `a-f-G-U-C` |
| Hostile | Ground (G) | `…/land/sitaware/c2/hostile/unit/…` | `a-h-G-U-C` |
| Neutral | Ground (G) | `…/land/sitaware/c2/neutral/unit/…` | `a-n-G-U-C` |
| Friendly | Air (A) | `…/air/sitaware/c2/friendly/aircraft/…` | `a-f-A-M-F` |
| Hostile | Air (A) | `…/air/sitaware/c2/hostile/aircraft/…` | `a-h-A-M-F` |
| Friendly | Sea (S) | `…/sea/sitaware/c2/friendly/vessel/…` | `a-f-S-X-L` |
| Hostile | Sea (S) | `…/sea/sitaware/c2/hostile/vessel/…` | `a-h-S-X-L` |
| Friendly / Hostile / Neutral / Unknown | Space (P) | `…/space/sitaware/c2/<affiliation>/satellite/…` | matching `a-<affiliation>-P` |
| Any | Special operations forces (F) | `…/land/sitaware/c2/<affiliation>/unit/…` | matching ground-unit type |

### NATO NFFI friendly-force protocol translator

`nffi` subscribes to complete NFFI XML documents that a partner receiver or detection system has already published under `…/raw/nffi/{source-id}` in Zenoh. It translates every unit to `…/land/nato/c2/friendly/unit/{type}/{id}/sapient`. It owns no TCP client, listener, endpoint, or framing convention. A product-specific connection must live in a separate `_bridge.py` after its endpoint and ICD are known.

NFFI friendly-force interoperability is ADatP-36 / STANAG 5527. STANAG 4677 is the separate dismounted-soldier interoperability family; a 4677 JDSSDM-over-NFFI profile would need a separate, profile-specific implementation.

**`.env` fields:**

```bash
NFFI_INPUT_TOPIC=               # optional; default: …/raw/nffi/*
```

### SitaWare Headquarters (outbound NVG pull feed)

`sitaware-hq-nvg` is the native Python output for an HQ-only deployment. It subscribes to EFDI tracks, keeps a bounded live snapshot, and exposes NVG 2.0.2 over a read-only HTTP(S) endpoint. SitaWare Headquarters polls it through **SitaWare Communication → NVG → NVG Import Subscriptions**. There is no separate NVG-XML ingest bridge — SitaWare ingress goes through the `sitaware` REST service instead.

Create an HQ layer first:

```text
Suggested Layer Key: blank
Name:                EFDI Live Tracks
Path:                /efdi-live
Type:                NVG
Persist tracks:      off
```

Configure the feed in `compose/.env`:

```bash
SITAWARE_HQ_NVG_ENABLE=1
SITAWARE_HQ_NVG_BIND=0.0.0.0   # or the EFDI LAN/Tailscale IP if you prefer a pinned listener
SITAWARE_HQ_NVG_PORT=8088
SITAWARE_HQ_NVG_PATH=/nvg
SITAWARE_HQ_NVG_USER=<dedicated-feed-user>
SITAWARE_HQ_NVG_PASS=<dedicated-random-password>
SITAWARE_HQ_NVG_TLS_CERT=/path/to/server-cert.pem
SITAWARE_HQ_NVG_TLS_KEY=/path/to/server-key.pem
SITAWARE_HQ_NVG_STALE_S=120
SITAWARE_HQ_NVG_MAX_TRACKS=10000
```

Start `sitaware-hq-nvg` from `./start.sh`, or use `./run.sh all`. Test from the HQ Windows host without printing operational data:

```powershell
curl.exe -k -u "<feed-user>:<feed-password>" -sS -o NUL `
  -w "HTTP %{http_code} %{content_type}`n" `
  https://<efdi-linux-ip-or-tailscale-ip>:8088/nvg
```

Use `-k` only for the initial connectivity check. Install the feed certificate's issuing CA in the HQ Windows trust store before normal operation.

Create the HQ import subscription:

```text
Subscription Name:         EFDI Live Tracks
Remote Endpoint:           https://<efdi-linux-ip-or-tailscale-ip>:8088/nvg
Target Layer:              efdi-live / EFDI Live Tracks
Request NVG periodically:  yes
Polling Interval:          10 seconds
Reconnect Delay:           90 seconds
Authentication:            enabled, using the dedicated feed credentials
Pause Subscription:        no
```

The endpoint accepts GET/HEAD only. It requires Basic authentication by default, bounds the cache, removes tracks not refreshed within `SITAWARE_HQ_NVG_STALE_S`, and gives each published NVG object a matching `TimeSpan` expiry. When present in the source, standard NVG modifiers and bounded `ExtendedData` carry callsign, registration/ICAO, aircraft or vessel type, squawk, route, source, vessel IDs, sensor identity, and other safe scalar fields. The Attributes view reuses the CoT/TAK domain formatter, presenting clean sections rather than raw Python field names. Aircraft expose separate barometric and geometric altitude, primary altitude in metres/feet/flight level, climb/descent rate, selected/target altitude, speed/heading, emergency/autopilot state, and ADS-B quality. dronuradaras.lt detections use the HQ-supported generic neutral equipment-sensor symbol; weather observations use the distinct neutral emplaced-sensor symbol because HQ 6.22 renders standards-native METOC symbols as Unknown. Neither is classified as a military-intelligence unit. It refuses cleartext HTTP on a non-loopback address unless `SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP=1` is explicitly set for an isolated lab. Do not use a Keycloak account or password for this feed.

#### One-time cleanup of legacy HQ objects

An NVG 2.0.2 data document has no per-object delete operation. Removing a
track from the EFDI snapshot therefore does not delete a copy that HQ already
imported. Current EFDI objects carry `TimeSpan/end`, but objects imported by an
older feed without that element can remain indefinitely and cannot be repaired
after EFDI has forgotten their URIs.

To remove those legacy objects without mixing them with live tracks:

1. Confirm the EFDI feed returns HTTP 200 and contains current objects.
2. Pause the existing import subscription.
3. Create a fresh NVG layer with **Persist tracks** set to **off**.
4. Retarget or recreate the subscription against that fresh layer and resume
   polling.
5. Confirm current EFDI objects appear and carry recent timestamps, then delete
   the old layer containing the legacy objects.

Do not clear a shared operational layer to work around this limitation.

### Icon reference

| ATAK appearance | CoT type | Source |
| --- | --- | --- |
| Neutral radar sensor (client-native MIL symbol) | `a-n-G-E-S-R` | CAT-34 site marker, including VERA-NG |
| Blue ground unit | `a-f-G-U-C` | SitaWare friendly ground unit |
| Red ground unit | `a-h-G-U-C` | SitaWare hostile ground unit |
| Yellow/green ground unit | `a-n-G-U-C` | SitaWare neutral ground unit |
| Blue aircraft | `a-f-A-M-F` | SitaWare friendly air unit |
| Red aircraft | `a-h-A-M-F` | SitaWare hostile air unit |
| Blue vessel | `a-f-S-X-L` | SitaWare friendly vessel |
| Red vessel | `a-h-S-X-L` | SitaWare hostile vessel |
| Green/yellow/red sensor box (same icon, recolors) | `a-n-G-E-S` / `a-u-G-E-S` / `a-h-G-E-S` | currently-online dronuradaras.lt acoustic sensor — green=idle, yellow=cooling down, red=active detection (last 60s); offline devices are removed |
| White unknown aircraft | `a-u-A-C-F` | Unclassified radar track |

> Position, speed, and course on the radar marker update automatically from the live CAT-34 stream. On a mobile platform, ATAK will show a speed vector and movement trail.

---

