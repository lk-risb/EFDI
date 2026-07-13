# Java 8 — via the REST bridge (no dependencies)

Defense shops pin to **JDK 8** for years. The modern Eclipse `zenoh-java` binding needs **JDK 17+**
(it's a Kotlin/JVM artifact with a Java 17 toolchain — see [`../../modern/java/`](../../modern/java/)),
so on JDK 8 you **cannot** link a native Zenoh client. That's fine: you don't need to.

These two programs talk to the **local REST bridge** over plain HTTP using only
`java.net.HttpURLConnection` from the JDK 8 standard library. **No Maven, no Gradle, no jars, no
internet.** They are not Zenoh clients — the bridge (running next to the pod on `127.0.0.1`) holds
the mTLS identity and does the Zenoh part.

```
Publish.java / Subscribe.java  ──HTTP──▶  REST bridge (127.0.0.1:8080)  ──Zenoh mTLS──▶  pod
        (plain javac/java)                  holds your certs                 tls/127.0.0.1:7447
```

## Prerequisites

1. **The REST bridge is running** next to the pod. Start it once (see
   [`../../bridges/rest-http/README.md`](../../bridges/rest-http/README.md)):
   ```sh
   export EFDI_ROUTER=tls/127.0.0.1:7447 \
          EFDI_CERT=$HOME/efdi-certs/mtls-cert.pem \
          EFDI_KEY=$HOME/efdi-certs/mtls-key.pem \
          EFDI_CA=$HOME/efdi-certs/ca-root.pem \
          PARTNER_NAMESPACE=release/acme
   python3 bridge.py          # serves on http://127.0.0.1:8080
   ```
2. **A JDK 8** (`javac -version` → `1.8.x`). Nothing else.

That's the whole dependency list. Your namespace (e.g. `release/acme`) lives in the *bridge's*
environment, not in this Java code — a bare suffix like `sensors/temp` is auto-scoped to
`release/acme/sensors/temp` by the bridge.

## Build

Plain `javac`, no build tool:

```sh
javac Publish.java Subscribe.java
```

This produces `Publish.class` and `Subscribe.class` in the current directory. (Java 8 compiles these
to bytecode that runs on any JDK 8+ JVM.)

## Run

**Publish** — POSTs a body to `/pub/<suffix>`:

```sh
java Publish sensors/temp '{"temp_c":21.5}'        # one sample -> release/acme/sensors/temp
java Publish sensors/temp '21.5' 10 200            # 10 samples, 200 ms apart
```

**Subscribe** — GET `/sub/<keyexpr>?count=N` (blocks) or `/stream/<keyexpr>` (continuous):

```sh
java Subscribe sensors/temp                        # wait for 1 sample under your namespace
java Subscribe sensors/temp 5 60                   # wait for 5 samples, up to 60 s
java Subscribe 'release/<partner>/**' 3          # inbound data from a partner (full key, passed through)
java Subscribe sensors/temp stream                 # follow continuously (Ctrl-C to stop)
```

Subscribe prints the raw JSON the bridge returns — for `/sub` a JSON array, for `/stream` one JSON
object per line. Each sample looks like `{"key":"release/acme/sensors/temp","ts":...,"text":"21.5"}`
(or `"b64"` instead of `"text"` if the bytes aren't valid UTF-8). We deliberately **don't** pull in a
JSON parser — a JDK 8 shop parses these with whatever it already has (Jackson/Gson if vendored, or a
hand-rolled split), or just logs them.

**Quick round-trip:** in one terminal `java Subscribe sensors/temp stream`, in another
`java Publish sensors/temp '21.5' 5 500`. The samples appear in the subscriber.

> Use full keys with `**` quoted in the shell (`'release/<partner>/**'`) so the glob isn't expanded
> by your shell before Java sees it.

## OFFLINE / air-gapped notes

- **Your app needs nothing vendored.** These programs use only `java.net.*`/`java.io.*` from the JDK
  itself. Carry in the JDK 8 installer once (if not already present) and you're done — there are no
  third-party jars to sneakernet for the Java side. That is exactly why the bridge path beats trying
  to link a native client on JDK 8.
- **What *does* need vendoring is the bridge's runtime:** Python 3 + the `eclipse-zenoh` 1.9.0 wheel.
  Build a wheelhouse on a connected box and carry it across — see the OFFLINE section in
  [`../README.md`](../README.md).
- **Everything is localhost.** `BRIDGE_URL` defaults to `http://127.0.0.1:8080`; override it only if
  you moved the bridge's port/bind. No DNS, no proxy, no internet from the Java side.
- **Clock skew breaks the bridge, not this code.** Your Java↔bridge hop is plain HTTP and ignores the
  clock. But the bridge↔pod hop is **mTLS**, which rejects certs outside their validity window. If
  `Publish`/`Subscribe` connect but nothing flows — or the bridge never prints `bridge on http://…` —
  fix the box's clock first (`date`, then `sudo date -s '…'`). One clock fix covers pod + bridge
  since they share the box. Full discussion in [`../README.md`](../README.md).

## Adapting into your own project

Drop `Publish.java` / `Subscribe.java` into your source tree as-is — they have no package and no
dependencies. The only knobs are the two CLI args and the optional `BRIDGE_URL` env var. If you need
to set custom HTTP headers, timeouts, or parse the JSON, edit the `post(...)` / `batch(...)` methods;
they're plain `HttpURLConnection` and short enough to read in one sitting.
