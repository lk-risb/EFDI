// Publish.java — send data to the goat fabric (modern Java, JDK 17+).
//
//   ./gradlew run -Pmain=Publish                 # one JSON sample
//   ./gradlew run -Pmain=Publish --args="50 0.2" # 50 samples at 200ms
//
// Publishes JSON under <namespace>/sensors/temp. Real payloads can be anything (bytes, protobuf,
// CBOR); JSON here for legibility.

import io.zenoh.Session;
import io.zenoh.bytes.ZBytes;
import io.zenoh.keyexpr.KeyExpr;
import io.zenoh.pubsub.Publisher;

public final class Publish {

    public static void main(String[] args) throws Exception {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 1;
        double interval = args.length > 1 ? Double.parseDouble(args[1]) : 1.0;
        String key = GoatConnect.key("sensors/temp");

        try (Session session = GoatConnect.session();
             KeyExpr keyExpr = KeyExpr.tryFrom(key)) {
            Publisher pub = session.declarePublisher(keyExpr);
            for (int i = 0; i < n; i++) {
                String payload = String.format(
                    "{\"ts\": %d, \"seq\": %d, \"temp_c\": %.1f}",
                    System.currentTimeMillis(), i, 21.5 + i * 0.1);
                pub.put(ZBytes.from(payload));
                System.out.println("published -> " + key + ": " + payload);
                System.out.flush();
                if (i + 1 < n) {
                    Thread.sleep((long) (interval * 1000));
                }
            }
        }
    }
}
