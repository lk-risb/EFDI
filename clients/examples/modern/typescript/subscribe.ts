// subscribe.ts — receive data from the goat fabric (modern TypeScript).
//
//   npm run subscribe                                  # your namespace, follow forever
//   npm run subscribe -- 'release/goat/**'      # inbound data goat sends you
//   npm run subscribe -- '<keyexpr>' 5                 # exit after 5 samples
//
// Default key-expr is '<namespace>/**' (everything under your prefix). Use ** for any depth,
// * for a single segment.
//
// IMPORTANT: zenoh-ts connects to the zenoh-plugin-remote-api over a WebSocket, NOT directly to
// the router over mTLS. See ./README.md and ../../../connect/typescript/goat_connect.ts.

import { RingChannel, Sample, ChannelReceiver } from "@eclipse-zenoh/zenoh-ts";
import { session, namespace } from "../../../connect/typescript/goat_connect.js";

async function main(): Promise<void> {
  const keyexpr = process.argv[2] ?? `${namespace()}/**`;
  const limit = process.argv[3] ? parseInt(process.argv[3], 10) : 0; // 0 = follow forever
  let seen = 0;

  const s = await session();
  try {
    const sub = await s.declareSubscriber(keyexpr, { handler: new RingChannel(256) });
    console.log(`subscribed: ${keyexpr} (Ctrl-C to stop)`);

    const receiver = sub.receiver() as ChannelReceiver<Sample>;
    for await (const sample of receiver) {
      const t = new Date().toTimeString().slice(0, 8);
      console.log(`${t}  ${sample.keyexpr()}  ${sample.payload().toString()}`);
      seen++;
      if (limit !== 0 && seen >= limit) {
        break;
      }
    }
    await sub.undeclare();
  } finally {
    await s.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
