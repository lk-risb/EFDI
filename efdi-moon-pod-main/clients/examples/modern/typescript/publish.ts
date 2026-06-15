// publish.ts — send data to the goat fabric (modern TypeScript).
//
//   npm install && npm run publish              # one JSON sample
//   npm run publish -- 50 0.2                    # 50 samples at 200ms
//
// Publishes JSON under <namespace>/sensors/temp via the pod's remote-api plugin (WebSocket).
// Real payloads can be anything (bytes, protobuf, CBOR); JSON here for legibility.
//
// IMPORTANT: zenoh-ts connects to the zenoh-plugin-remote-api over a WebSocket, NOT directly to
// the router over mTLS. See ./README.md and ../../../connect/typescript/goat_connect.ts.

import { Encoding } from "@eclipse-zenoh/zenoh-ts";
import { session, key } from "../../../connect/typescript/goat_connect.js";

async function main(): Promise<void> {
  const n = process.argv[2] ? parseInt(process.argv[2], 10) : 1;
  const interval = process.argv[3] ? parseFloat(process.argv[3]) : 1.0;
  const k = key("sensors/temp");

  const s = await session();
  try {
    const pub = await s.declarePublisher(k, { encoding: Encoding.APPLICATION_JSON });
    for (let i = 0; i < n; i++) {
      const payload = JSON.stringify({ ts: Date.now(), seq: i, temp_c: 21.5 + i * 0.1 });
      await pub.put(payload);
      console.log(`published -> ${k}: ${payload}`);
      if (i + 1 < n) {
        await sleep(interval * 1000);
      }
    }
    await pub.undeclare();
  } finally {
    await s.close();
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
