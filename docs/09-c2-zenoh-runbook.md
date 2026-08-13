# 09 — C2 ↔ Zenoh Bidirectional Runbook

The directions are independent. Complete only the paths exposed and licensed
by the actual deployment, then select their services in `./start.sh`.

## 9.1 Verify the common Zenoh side

Keep every Python adapter pointed at the local router:

```dotenv
ZENOH_LOCAL_ENDPOINT=tcp/127.0.0.1:7448
```

Set `ZENOH_FABRIC_ENDPOINT` only for the `zenoh-router`, or use the
`ZENOH_FABRIC_ENDPOINTS` JSON array for two or more explicitly configured
uplinks. Bridges and layers do not connect directly to changing backbone
addresses. C2-origin records are
published below `{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/...`. Federation ACLs
decide which partner routers can receive that namespace.

## 9.2 Zenoh → TAK Server

Configure the TAK TCP destination and select `tak-layer`:

```dotenv
TAK_HOST=<tak-server>
TAK_PORT=8089
TAK_TLS=1
TAK_TLS_SERVER_NAME=<dns-san-in-tak-server-certificate>
TAK_CERT=/runtime/path/tak-client.pem
TAK_KEY=/runtime/path/tak-client-key.pem
TAK_CA=/runtime/path/tak-ca.pem
```

These must be TAK-issued credentials. The Zenoh certificate is not valid for
TAK Server. `TAK_HOST` is the stable dial hostname; when the installed TAK
server certificate uses a different legacy DNS SAN, set
`TAK_TLS_SERVER_NAME` to that SAN instead of disabling hostname verification.
For lab plaintext TCP use the deployment's configured TCP port and
leave `TAK_TLS=0`. `tak-layer` egress is one-way; enable `tak-bridge` for a
return feed.

On the TAK Server side:

1. Sign in to the TAK Server administration UI with an administrator identity.
2. Open **User Management** and create a dedicated EFDI client identity; do not
   reuse a human operator account.
3. Assign the mission groups EFDI must publish to and the mission groups it
   must observe. For the `efdi-bridge` client identity, give the broadest
   authorized visibility the deployment allows so the same CoT session can both
   publish and receive server-visible markers.
4. Use the deployment's certificate/enrollment workflow to issue a client
   certificate for that identity and export its certificate, private key and
   TAK CA chain. Current TAK Server exposes user/group and certificate-manager
   operations in its [official API](https://docs.tak.gov/api/takserver); exact
   buttons differ between file-user, LDAP and external-identity deployments.
5. Place the PEM files in a runtime-only directory on the EFDI host, enter their
   paths above, select `tak-layer` in `./start.sh`, and confirm the identity appears
   as connected in TAK Server.

## 9.3 TAK Server → Zenoh

Use the same TAK-issued client identity for the reverse CoT feed, typically the
dedicated `efdi-bridge` account/certificate. Select `tak-bridge` and point it
at the TAK Server CoT endpoint:

```dotenv
TAK_HOST=<tak-server>
TAK_PORT=8089
TAK_TLS=1
TAK_TLS_SERVER_NAME=<dns-san-in-tak-server-certificate>
TAK_CERT=/runtime/path/efdi-bridge.pem
TAK_KEY=/runtime/path/efdi-bridge-key.pem
TAK_CA=/runtime/path/tak-ca.pem
```

The bridge uses the same TAK session model as a normal client: if the server
authorizes the identity for both directions, it can publish into TAK and
subscribe to server-visible CoT at the same time. The bridge republishes the
received `<event>...</event>` frames into Zenoh and marks them as TAK ingress so
the outbound CoT layer does not loop them straight back into the server.

## 9.4 Zenoh → SitaWare HQ

Enable `sitaware-hq-nvg`, configure TLS and dedicated feed credentials, then
create an HQ NVG Import Subscription pointing to the resulting
`SITAWARE_HQ_NVG_PATH`:

```dotenv
SITAWARE_HQ_NVG_ENABLE=1
SITAWARE_HQ_NVG_BIND=<efdi-address>
SITAWARE_HQ_NVG_PORT=8088
SITAWARE_HQ_NVG_PATH=/nvg
SITAWARE_HQ_NVG_USER=<dedicated-feed-user>
SITAWARE_HQ_NVG_PASS=<runtime-secret>
SITAWARE_HQ_NVG_TLS_CERT=/runtime/path/feed-cert.pem
SITAWARE_HQ_NVG_TLS_KEY=/runtime/path/feed-key.pem
```

Inside SitaWare HQ, click **SitaWare Communication → NVG → NVG Import
Subscriptions**, create a subscription, and enter:

```text
Subscription Name:         EFDI Live Tracks
Remote Endpoint:           https://<efdi-address-or-tailscale-ip>:8088/nvg
Target Layer:              efdi-live / EFDI Live Tracks
Request NVG periodically:  yes
Polling Interval:          10 seconds
Reconnect Delay:           90 seconds
Authentication:            enabled; use the dedicated feed user/password
Pause Subscription:        no
```

Create the `EFDI Live Tracks` NVG layer first if it is absent. Trust the feed
certificate's issuing CA in Windows; do not leave certificate verification
disabled after the connectivity test.

## 9.5 SitaWare HQ → Zenoh

This requires a real JSON unit resource documented for that HQ deployment; do
not guess `/rest/v2/units`. Configure and select `sitaware`:

```dotenv
SITAWARE_URL=https://<hq-server>
SITAWARE_USER=<runtime-user>
SITAWARE_PASS=<runtime-secret>
SITAWARE_API_PATH=/<documented-resource-path>
SITAWARE_POLL_S=10
SITAWARE_TLS_VERIFY=1
```

The bridge publishes below `…/{domain}/sitaware/rest/{affiliation}/{entity}/
tracks/v1`. Verify with:

```bash
tail -f "${POD_STATE_DIR:-compose/state}/logs/sitaware.log"
```

On the SitaWare HQ side, the administrator must enable the licensed API, create
a read-only integration account, and grant that account access to the exact
unit/track resource intended for export. Copy these four values from the
installed product's API/ICD into the handover: base URL, resource path,
authentication method, and response schema/version. There is no safe generic
sequence of public HQ menu clicks for this operation and no universal units
resource; if the administrator cannot identify that screen/resource, do not
enable `sitaware`. Use the deployment's NFFI or CoT Gateway interface instead.

## 9.6 Share C2-origin data with partners

Do not rewrite the record into another partner's namespace. Confirm that the
origin namespace is permitted by the router/federation policy and that the
receiving partner subscribes to it. Their `cot-*` or `sitaware-hq-nvg` output
layers will translate authorized normalized topics in the same way as locally
generated sensor data.

## 9.7 Operational-persona test exercise

Use four separate identities or clients in a test. These are operational
personas, not replacements for the Zenoh Admin panel's `superadmin`, `admin`,
and `readonly` roles.

| Persona | Test client and action | EFDI services | Expected result |
| --- | --- | --- | --- |
| C2 operator | A TAK/WinTAK/ATAK or SitaWare HQ operator account observes the configured CoT output. | `tak-layer` and/or `sitaware-hq-nvg`. | Normalized EFDI tracks appear in the authorized C2 system. |
| Sensor publisher | A receiver/detection system attached to a local Zenoh router publishes complete frames/documents to that protocol's `…/raw/<protocol>/<source-id>` topic. For a lab publisher, an admin can generate a script in **Publish Script** after entering that publisher's current router endpoint. | The matching protocol translator and desired C2 output layers. | The translator creates normalized EFDI tracks; the C2 systems show derived markers, not the raw frame. |
| Fabric admin | A separate Zenoh Admin panel account manages router/federation configuration only. | Infrastructure/admin UI; no sensor or C2 feed is required. | May perform its assigned panel actions but is not an operational TAK/SitaWare identity. |

For a first exercise, use a dedicated TAK-issued service identity for `tak-layer`
and confirm the authorized C2 system receives normalized EFDI tracks. Keep raw
sensor publication on a distinct sensor identity/topic; it must not impersonate
an operator identity.

The current router ACL is namespace-scoped, not yet persona/certificate-scoped.
The four test clients prove data flow and C2 behaviour; they do **not** prove
least-privilege Zenoh authorization between personas. Enforced persona access
needs a subsequent certificate-subject ACL design with separate client
credentials and topic permissions.

> **ASTERIX editions:** the implemented standard UAPs are CAT-010 1.1,
> CAT-020 1.11, CAT-021 2.7, CAT-034 1.29, CAT-048 1.32, and CAT-062 1.21.
> Confirm the producer edition before connecting it; a different or
> vendor-specific UAP needs an explicit decoder profile.

### Zenoh topic schema

```text
{NAMESPACE}/{DOMAIN}/{SOURCE}/{MODALITY}/{AFFILIATION}/{ENTITY}/{TYPE}/{ID}/{VIEW}
```

| Field | Values |
| --- | --- |
| `DOMAIN` | `air`, `land`, `sea`, `space`, `env` |
| `AFFILIATION` | `friendly`, `hostile`, `neutral`, `unknown`, `civ`, `mil` |
| `TYPE` | `aircraft`, `vessel`, `vehicle`, `unit`, `sensor`, `uav`, `radar` |

---
