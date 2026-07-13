# military / legacy / less-common stacks

For developers on **older toolchains** — pinned JDK 8, .NET Framework 4.x, MATLAB, C89/C99 — and
for **air-gapped / offline** shops where you can't reach the internet, can't `pip install`, and
often can't link a native Zenoh client at all. The goal of this directory is one thing: **let you
publish to and receive from the pod with the tools you already have**, no modern build chain, no
package manager, no network egress.

If you have a recent toolchain and can link a Zenoh library, use [`../modern/`](../modern/) instead —
it's faster and richer. This directory is for when you can't.

## Decision guide — pick your path

Work top to bottom; stop at the first row that's true for you.

| If… | Use | Why |
|---|---|---|
| A native Zenoh binding builds and links on your box (you have `zenoh-c`, a C compiler, and policy allows it) | **native** → [`c99/`](c99/) | lowest latency, full pub/sub/query, no extra process |
| You can make an **HTTP request** (any language: `HttpURLConnection`, `HttpWebRequest`, `webwrite`, `curl`, a PLC's HTTP block) | **REST bridge** → [`../bridges/rest-http/`](../bridges/rest-http/) + the `java8/`, `dotnet-framework/`, `matlab/` examples here | zero Zenoh code; works on any stack with an HTTP client |
| You can only **read/write files** (truly air-gapped app, MATLAB on a locked box, SCADA/PLC, shell pipeline) | **file-drop bridge** → [`../bridges/file-drop/`](../bridges/file-drop/) + the `matlab/receive_filedrop.m` example | the most universal path — if it can write a file, it can publish |

The native client is a direct Zenoh client. **The bridges are not** — a bridge is a small Python
process running *next to the pod* that holds the mTLS identity and exposes a plain protocol (HTTP, or
a watched directory) on **`127.0.0.1` only**. Your legacy app talks to that localhost door, never to
Zenoh and never to the network.

```
your legacy app  ──HTTP / files──▶  bridge (on localhost, holds mTLS)  ──Zenoh mTLS──▶  pod router
   (no Zenoh code, no certs)            127.0.0.1:8080 / a directory          tls/127.0.0.1:7447
```

## Examples in this directory

| Dir | Stack | Talks to | Transport |
|---|---|---|---|
| [`c99/`](c99/) | C89/C99 native | the pod router directly | native `zenoh-c` (mTLS) |
| [`java8/`](java8/) | **JDK 8** (modern `zenoh-java` needs JDK 17+) | the REST bridge | `HttpURLConnection`, stdlib only |
| [`dotnet-framework/`](dotnet-framework/) | **.NET Framework 4.x** (not modern .NET) | the REST bridge | `HttpWebRequest` / `HttpClient` |
| [`matlab/`](matlab/) | MATLAB | the REST bridge **and** the file-drop bridge | `webwrite`/`webread` + file polling |

The Java, .NET, and MATLAB examples here are **not** Zenoh clients. They are ~80-line programs using
nothing but their language's standard library against the local bridge. Drop them into your shop and
they compile with the stone-age tools you already have (`javac`, `csc.exe`, MATLAB's editor) — no
Maven, no NuGet restore, no Gradle, no internet.

## OFFLINE / air-gapped first

Assume no internet. Plan for it from the start.

**1. Get the pieces in over sneakernet.** You need, on the air-gapped box:
- **The pod itself** (its container images / compose bundle) — handed to you by your pod operator.
- **Your mTLS cert bundle** — `mtls.cert.pem`, `mtls.key.pem`, `ca-roots.pem`, and your namespace
  (`release/<you>`). The operator produces this out-of-band; see [`../../README.md`](../../README.md).
- **For a bridge:** Python 3 + the `eclipse-zenoh` 1.9.0 wheel, vendored. Build a wheelhouse on a
  connected box and carry it in:
  ```sh
  # on a CONNECTED machine, matching the air-gapped box's OS/arch/python:
  pip download eclipse-zenoh==1.9.0 -d zenoh-wheelhouse/
  # carry zenoh-wheelhouse/ across, then on the AIR-GAPPED box:
  pip install --no-index --find-links zenoh-wheelhouse/ eclipse-zenoh==1.9.0
  ```
- **For your app:** nothing extra. The Java/.NET/MATLAB examples here use only their stdlib —
  whatever's already installed (`javac`, `csc.exe`, MATLAB) is enough. **That is the whole point of
  going through the bridge: your app has no dependencies to vendor.**

**2. Everything is localhost.** The pod, the bridge, and your app all run on the same box. Your app
talks to `http://127.0.0.1:8080` (REST bridge) or a local directory (file-drop bridge). No DNS, no
proxy, no internet — by design. The only network hop is bridge→pod, which is `tls/127.0.0.1:7447`
(also loopback when the pod is on your box). See [`../bridges/README.md`](../bridges/README.md) for the
trust-boundary discussion.

**3. Clock sync — the silent killer.** The pod router speaks **mTLS**, and TLS rejects certificates
whose validity window doesn't contain *now*. An air-gapped box with a dead RTC battery or a clock
that never NTP-synced will drift, and then the **bridge's** session to the pod fails handshake with a
confusing "certificate not yet valid / expired" error — even though your app→bridge HTTP call looks
fine. Symptoms: the bridge logs a TLS error on startup and never prints `bridge on http://…`.
Before you debug anything else:
```sh
date          # is it even close to correct?
# set it (air-gapped, so manually or from a trusted local time source):
sudo date -s '2026-06-02 14:30:00'      # Linux
# the pod and the bridge share the box, so one clock fix covers both.
```
Your app→bridge hop is plain HTTP and does **not** care about the clock — so if `curl
http://127.0.0.1:8080/...` works but no data flows, suspect the bridge→pod TLS clock first.

## A note on payloads and namespaces

- You publish to keys **under your namespace** (`release/<you>/...`). With the REST bridge a bare
  suffix like `sensors/temp` is automatically scoped to `release/<you>/sensors/temp`. A full key you
  have read rights to (e.g. `release/<partner>/**`) is passed through as-is.
- The fabric is **payload-agnostic** — bytes are bytes. These examples send JSON text because it's
  readable, but you can send anything. The REST bridge returns received text as `"text"` in its JSON,
  or base64 as `"b64"` if the bytes aren't valid UTF-8.

Read the two bridge READMEs before you start — they are the contract these examples target:
[`../bridges/rest-http/README.md`](../bridges/rest-http/README.md) and
[`../bridges/file-drop/README.md`](../bridges/file-drop/README.md).
