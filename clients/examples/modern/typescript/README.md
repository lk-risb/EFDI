# TypeScript / Node — modern

Idiomatic TypeScript against the pod. Uses the **official** Zenoh TS/JS binding
[`@eclipse-zenoh/zenoh-ts`](https://github.com/eclipse-zenoh/zenoh-ts) plus the
[`connect/typescript/efdi_connect.ts`](../../../connect/typescript/efdi_connect.ts) helper.

## READ THIS FIRST — zenoh-ts connects through a plugin, not directly to the router

This is the big difference from every other language in this tree, and it changes the connection
model and the mTLS story:

- The native bindings (Python/Go/Rust/Java) open a **direct Zenoh client session to the router over
  mTLS**, and `EFDI_CERT`/`EFDI_KEY`/`EFDI_CA` are the client's mesh credentials. **The
  "one TLS block" gotcha lives there.**
- `@eclipse-zenoh/zenoh-ts` does **not** speak the native Zenoh transport. It talks to the
  [`zenoh-plugin-remote-api`](https://github.com/eclipse-zenoh/zenoh-ts/tree/main/zenoh-plugin-remote-api)
  loaded inside `zenohd`, over a **WebSocket** (`ws://` / `wss://`). Its `Config` takes **only a
  locator string** (e.g. `new Config("ws/127.0.0.1:10000")`) — there is no client-cert / TLS
  block to set on this side at all.

**Consequence for mTLS:** the mesh-side mTLS (the cert/key/CA + the `enable_mtls: true` one-block
config) is configured in the **pod's `zenohd`**, on the links the router itself makes — not in your
TS app. The remote-api plugin is the trust boundary between "the WebSocket client" and "the mesh."
So this binding is the natural fit when:

- the pod runs the remote-api plugin **on loopback** and you trust the local box, **or**
- the plugin is fronted with `wss://` (TLS terminated by the plugin or a reverse proxy) and your
  WebSocket client trusts that server cert.

> **For Node/browser consumers, the REST/WebSocket bridge is often the better path.** If you don't
> specifically need the zenoh-ts object API, the HTTP/WebSocket bridge in
> [`../../bridges/`](../../bridges/) (the Zenoh REST plugin / remote-api) lets you publish and
> subscribe with plain `fetch`/`WebSocket` and no Zenoh-specific client at all. We still ship the
> zenoh-ts example below because it's the idiomatic typed API.

### Pod-side prerequisite (the plugin must be enabled)

The pod operator must enable `zenoh-plugin-remote-api` in `zenohd` (it is **not** on by default).
Minimal `zenohd` config:

```json5
{
  plugins: {
    remote_api: {
      websocket_port: "10000"   // ws/<host>:10000 — what EFDI_WS points at
    }
  }
}
```

If you reach this over anything but loopback, front it with `wss://` — the plugin port is a raw
WebSocket otherwise. Confirm with your operator which locator (and `ws` vs `wss`) to use.

## Env vars

This binding consumes a **different** set than the native ones:

| Var | Used by zenoh-ts? | Meaning |
|---|---|---|
| `EFDI_WS` | yes (preferred) | remote-api plugin WebSocket locator, e.g. `ws/127.0.0.1:10000` |
| `EFDI_ROUTER` | yes (fallback) | native router endpoint; if `EFDI_WS` is unset, the host is reused with port `10000` |
| `PARTNER_NAMESPACE` | yes | your owned prefix (publish under this) |
| `EFDI_CERT` / `EFDI_KEY` / `EFDI_CA` / `EFDI_VERIFY_NAME` | **no** | present for parity with the other bundles; the plugin owns mesh-side mTLS. Only relevant here if you front the plugin with `wss://` and need a custom CA to trust it (see below). |

```sh
export EFDI_WS="ws/127.0.0.1:10000"        # or leave unset to derive from EFDI_ROUTER's host
export EFDI_ROUTER="tls/127.0.0.1:7447"
export PARTNER_NAMESPACE="release/acme"
```

## Setup & run (Node)

zenoh-ts was authored for the **browser / Deno** first. Under **Node** it needs a global
`WebSocket` (we shim it with the `ws` package) and a WASM-capable runtime. This project wires that
up: `npm run *` loads [`ws-polyfill.ts`](./ws-polyfill.ts) before the example via `tsx`.

```sh
npm install
npm run subscribe                              # everything under your namespace
npm run publish                                # one JSON sample to <ns>/sensors/temp
npm run publish -- 50 0.2                       # 50 samples, 200ms apart
npm run subscribe -- 'release/goat/**'   # inbound data from goat
npm run subscribe -- '<keyexpr>' 5              # stop after 5 samples
```

- Node **22+** ships a native global `WebSocket`, so the `ws` polyfill is a harmless no-op there.
  On Node **18/20** the polyfill is required.
- **Quick round-trip:** `npm run subscribe` in one terminal, `npm run publish -- 10 0.5` in another.

### Deno (the upstream-blessed runtime)

If the Node shimming is fragile in your environment, Deno needs no polyfill:

```sh
deno run --allow-net --allow-env --allow-read subscribe.ts
deno run --allow-net --allow-env --allow-read publish.ts 10 0.5
```

(`npm run deno:subscribe` / `deno:publish` wrap these.)

### Browser

The same `efdi_connect.ts` + example logic runs in the browser unchanged (browsers provide native
`WebSocket`); bundle with Vite/esbuild and point `EFDI_WS` at a `wss://` plugin endpoint the page
is allowed to reach. There are no Node globals (`process.env`) in the browser — feed the locator
and namespace in from your app config instead of `process.env`.

## wss + a custom CA (the only place EFDI_CA matters here)

If the plugin is fronted by `wss://` with a cert signed by a **private** CA, the WebSocket client
(not zenoh-ts) must trust it:

- **Node:** `export NODE_EXTRA_CA_CERTS="$EFDI_CA"` before running — Node's TLS stack (and the `ws`
  package) will then accept the server cert.
- **Browser:** the CA must be in the OS/browser trust store; you can't inject it per-connection.

This is server-cert trust for the WebSocket leg only. It is **not** client mTLS — zenoh-ts does not
present a client cert to the plugin.

## The mTLS one-block gotcha (for context — it lives in the POD, not here)

For completeness, since every other README calls it out: Zenoh's `transport/link/tls` block must be
inserted as **one whole object** with **`enable_mtls: true`** (sub-key-at-a-time silently fails to
send the client cert on Zenoh 1.x). That rule applies to the **pod's `zenohd` config** and to the
native client bindings — **not** to this TS app, which has no TLS block at all (the plugin
terminates mesh TLS). If you switch to a native binding later, that's where the gotcha bites.

## Notes & API accuracy

- API used here matches `@eclipse-zenoh/zenoh-ts` 1.9.0: `new Config(locator)`,
  `Session.open(config)`, `session.declarePublisher(keyExpr, { encoding })`, `publisher.put(...)`,
  `session.declareSubscriber(keyExpr, { handler: new RingChannel(n) })`, iterate
  `sub.receiver()` as `ChannelReceiver<Sample>`, `sample.keyexpr()`, `sample.payload().toString()`.
  Subscriber delivery here uses the **RingChannel async-iterator** pattern (the upstream `z_sub`
  example's shape); a callback-handler variant also exists if you prefer push delivery.
- The binding parses key expressions via a **WASM** module; ensure your bundler/runtime can load
  `.wasm`. tsx/Deno handle this out of the box.
- Payloads here are JSON strings; the binding also accepts `Uint8Array` for raw bytes. The fabric
  is payload-agnostic — a registered topic's `format` is a tooling hint, not enforced.
- **Flagged uncertainty:** the exact `Encoding` constant name (`Encoding.APPLICATION_JSON`) and
  whether `payload()` returns a `ZBytes`-like wrapper with `.toString()` vs a raw value can drift
  across 1.x minors and the camelCase API migration. If a symbol doesn't resolve, check the
  [zenoh-ts API docs](https://eclipse-zenoh.github.io/zenoh-ts/) for your pinned version — the
  connection model (Config-locator + WebSocket-to-plugin) is the stable part.
```
