// goat_connect — open a Zenoh session to a goat-moon-pod from env vars (TypeScript).
//
// READ THIS, IT'S DIFFERENT FROM THE OTHER LANGUAGES.
//
// The official TypeScript binding `@eclipse-zenoh/zenoh-ts` does NOT speak the native Zenoh
// transport and does NOT open a direct mTLS connection to the router. Instead it talks to the
// `zenoh-plugin-remote-api` plugin loaded inside `zenohd` over a **WebSocket** (`ws://` /
// `wss://`). Its `Config` takes a single locator string and nothing else — there is no
// client-cert / TLS block to configure on this side. See the README for the full caveat.
//
// So the goat-specific job here is:
//   1. Figure out the WebSocket locator for the pod's remote-api plugin (GOAT_WS, or derived
//      from GOAT_ROUTER's host).
//   2. Provide key()/namespace() so your keys land under your owned prefix — identical semantics
//      to the other languages.
//
// The mesh-side mTLS (GOAT_CERT/GOAT_KEY/GOAT_CA, the "one TLS block" gotcha) lives in the POD's
// zenohd config, on the connect/listen links the router itself makes — NOT here. The browser/Node
// client trusts the local pod over loopback, or over `wss://` if the plugin is fronted by TLS.
//
// Env vars (see ../../README.md):
//
//     GOAT_ROUTER       tls/127.0.0.1:7447   pod's NATIVE Zenoh endpoint. Used here only to
//                                            derive the plugin host when GOAT_WS is unset.
//     GOAT_WS           ws/127.0.0.1:10000   remote-api plugin WebSocket locator (preferred).
//                                            If unset, defaults to ws/<GOAT_ROUTER host>:10000.
//     GOAT_NAMESPACE    release/<you>        your owned prefix (publish under this)
//
//     GOAT_CERT / GOAT_KEY / GOAT_CA / GOAT_VERIFY_NAME — present for parity with the other
//       bundles, but NOT consumed by zenoh-ts (the plugin owns mesh-side TLS). They are only
//       relevant here if you front the remote-api plugin with `wss://` and your runtime needs a
//       custom CA to trust it — see the README "wss + custom CA" note.

import { Config, Session, KeyExpr } from "@eclipse-zenoh/zenoh-ts";

/** Return the value of `name` or throw with an actionable message if unset/empty. */
function env(name: string): string {
  const v = process.env[name];
  if (!v) {
    throw new Error(
      `${name} is not set. Source the pod env (see clients/README.md), e.g.\n` +
        `  export ${name}=...`,
    );
  }
  return v;
}

/**
 * Resolve the WebSocket locator for the pod's remote-api plugin.
 *
 * Prefers GOAT_WS (e.g. "ws/127.0.0.1:10000"). If unset, derives it from GOAT_ROUTER's host on
 * the default plugin port 10000 — e.g. GOAT_ROUTER="tls/127.0.0.1:7447" -> "ws/127.0.0.1:10000".
 * Accepts either the Zenoh locator form ("ws/host:port") or a plain URL ("ws://host:port"); the
 * Config constructor wants the locator form, so we normalize to that.
 */
export function wsLocator(): string {
  const raw = process.env.GOAT_WS ?? deriveWsFromRouter(env("GOAT_ROUTER"));
  return normalizeLocator(raw);
}

function deriveWsFromRouter(router: string): string {
  // router is "<proto>/<host>:<port>" e.g. "tls/127.0.0.1:7447".
  const afterProto = router.includes("/") ? router.slice(router.indexOf("/") + 1) : router;
  const host = afterProto.includes(":") ? afterProto.slice(0, afterProto.lastIndexOf(":")) : afterProto;
  return `ws/${host}:10000`;
}

/** Normalize "ws://host:port" or "ws/host:port" -> "ws/host:port" (Config locator form). */
function normalizeLocator(s: string): string {
  return s.replace(/^(wss?):\/\//, "$1/");
}

/**
 * Open and return a Zenoh session via the remote-api plugin WebSocket.
 *
 * NOTE: `Config` only carries the WebSocket locator. There is no mTLS client-cert block here —
 * unlike the native bindings — because zenoh-ts connects to the plugin, not the router. Close the
 * session with `await session.close()` (or rely on process exit).
 */
export async function session(): Promise<Session> {
  return Session.open(new Config(wsLocator()));
}

/** Your owned prefix, e.g. "release/acme" (trailing slash stripped). */
export function namespace(): string {
  return env("GOAT_NAMESPACE").replace(/\/+$/, "");
}

/**
 * Build a fully-qualified key under your namespace: key("sensors/temp") ->
 * "release/acme/sensors/temp". Pass an absolute key (e.g. "release/goat/**") only if you
 * have rights to it.
 */
export function key(suffix: string): string {
  return `${namespace()}/${suffix.replace(/^\/+/, "")}`;
}

/** Convenience: a KeyExpr for key(suffix). */
export function keyExpr(suffix: string): KeyExpr {
  return new KeyExpr(key(suffix));
}
