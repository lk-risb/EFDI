// ws-polyfill.ts — make `WebSocket` global under Node.js.
//
// zenoh-ts talks to the remote-api plugin over a WebSocket and uses the browser-global
// `WebSocket`. Node has no global WebSocket on older LTS lines, so we install the `ws` package
// implementation onto `globalThis` BEFORE zenoh-ts loads. Imported via tsx's `--import` flag (see
// package.json) so it runs first. Node 22+ ships a native global WebSocket and this is a no-op.
//
// Not needed in the browser or Deno (both provide a native WebSocket).

import WebSocket from "ws";

const g = globalThis as unknown as { WebSocket?: unknown };
if (!g.WebSocket) {
  g.WebSocket = WebSocket;
}
