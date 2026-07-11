# Java — modern (JDK 17+)

Idiomatic Java against the pod. Uses the **official** Eclipse Zenoh Java binding
[`io.zenoh` / `zenoh-java`](https://github.com/eclipse-zenoh/zenoh-java) plus the
[`connect/java/EfdiConnect.java`](../../../connect/java/EfdiConnect.java) helper.

## Read this first: the binding is a JNI wrapper, but native libs are bundled

`zenoh-java` is a Kotlin/JVM binding over the native Zenoh library via JNI. Unlike the Go binding
(which makes you install `zenoh-c` yourself), the published `zenoh-java-jvm` artifact **bundles the
native library as a JAR resource** and loads it at runtime — so a normal Gradle/Maven dependency is
all you need. No separate `zenoh-c` install, no `LD_LIBRARY_PATH`.

- **Coordinates:** `org.eclipse.zenoh:zenoh-java-jvm:1.9.0` (matches the fabric's Zenoh 1.9.0).
- **JDK:** 17+ (this project sets a Java 17 toolchain).
- The bundled native lib targets common desktop/server platforms (Linux/macOS x86-64 + aarch64).
  On an unusual platform you may need to build the binding from source — see the upstream repo.

## Setup

```sh
# export GOAT_* per clients/README.md
export EFDI_ROUTER="tls/127.0.0.1:7447"
export EFDI_CERT="$HOME/.goat/contexts/default/mtls.cert.pem"
export EFDI_KEY="$HOME/.goat/contexts/default/mtls.key.pem"
export EFDI_CA="$HOME/.goat/contexts/default/ca-roots.pem"
export PARTNER_NAMESPACE="release/acme"
# EFDI_VERIFY_NAME defaults to false (local pod at 127.0.0.1); set true for a DNS-named router.
```

You need a Gradle wrapper (`./gradlew`) or a system Gradle 8+. To generate the wrapper once:

```sh
gradle wrapper        # writes gradlew + gradle/wrapper/ (one-time, needs Gradle installed)
```

## Run

`Publish` and `Subscribe` are separate `main` classes in one project; pick one with `-Pmain=`:

```sh
./gradlew run -Pmain=Publish                                  # one JSON sample to <ns>/sensors/temp
./gradlew run -Pmain=Publish --args="50 0.2"                  # 50 samples, 200ms apart
./gradlew run -Pmain=Subscribe                                # everything under your namespace
./gradlew run -Pmain=Subscribe --args="release/goat/**"  # inbound data from goat
./gradlew run -Pmain=Subscribe --args="<keyexpr> 5"           # stop after 5 samples
```

**Quick round-trip:** run `./gradlew run -Pmain=Subscribe` in one terminal,
`./gradlew run -Pmain=Publish --args="10 0.5"` in another. The JSON samples should arrive in the
subscriber.

> The Gradle build adds `../../../connect/java` to the source set so `EfdiConnect.java` compiles
> alongside the examples (the same "examples reach back into `connect/<lang>/`" pattern the other
> languages use). If you vendor these into your own project, just drop `EfdiConnect.java` next to
> your sources.

## The one connection gotcha (read this — it bites everyone)

Zenoh's TLS config must be loaded as **one whole json5 block** at `transport/link/tls`, with
**`enable_mtls: true`**. Setting the sub-keys one at a time silently does **not** turn on the
client-cert send path on Zenoh 1.x — your session opens but the router rejects you, or you connect
read-only. `EfdiConnect.config()` does it the working way: it renders the entire config (including
the whole TLS object) and loads it with a single `Config.fromJson5(json)` call.

When the router cert's SAN binds an **IP/mesh address** rather than the DNS name you dial, set
`EFDI_VERIFY_NAME=false` (the default). A DNS-named remote router can use `true`.

## Notes

- Payloads here are JSON for readability; send any bytes via `ZBytes.from(byte[])`. The fabric is
  payload-agnostic — a registered topic's `format` is a tooling hint, not enforced.
- Read a sample's bytes with `sample.getPayload().toBytes()` (or `.toString()` for text); the key
  with `sample.getKeyExpr()`.
- `declareSubscriber(keyExpr, callback)` delivers samples on a Zenoh worker thread — guard shared
  state (the example uses `AtomicInteger`).
- Use **try-with-resources** for `Session`, `KeyExpr`, `Publisher`, and `Subscriber`; the binding
  owns native handles and relies on `close()`/auto-undeclare to release them promptly.

## Caveats / binding maturity

- The Java binding trailed the others and its surface was still being aligned during the 1.x line.
  Method names used here — `Config.fromJson5`, `Zenoh.open`, `session.declarePublisher`,
  `publisher.put(ZBytes.from(...))`, `session.declareSubscriber(keyExpr, sample -> ...)`,
  `KeyExpr.tryFrom`, `ZBytes.from` / `.toBytes` / `.toString`, `sample.getPayload` /
  `sample.getKeyExpr` — match the `zenoh-java` 1.9.0 examples (`ZPub` / `ZSub`). If you pin a
  different minor, re-check against that tag's `examples/`.
- Import paths can shift between minors. These use `io.zenoh.{Config,Session,Zenoh}`,
  `io.zenoh.bytes.ZBytes`, `io.zenoh.keyexpr.KeyExpr`, `io.zenoh.pubsub.{Publisher,Subscriber}`,
  `io.zenoh.sample.Sample`, `io.zenoh.exceptions.ZError`. If a class doesn't resolve, check that
  tag's javadoc — the package layout is the most likely thing to have moved.
```
