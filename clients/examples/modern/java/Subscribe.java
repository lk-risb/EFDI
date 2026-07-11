// Subscribe.java — receive data from the goat fabric (modern Java, JDK 17+).
//
//   ./gradlew run -Pmain=Subscribe                                   # your namespace, forever
//   ./gradlew run -Pmain=Subscribe --args="release/goat/**"   # inbound data from goat
//   ./gradlew run -Pmain=Subscribe --args="<keyexpr> 5"              # exit after 5 samples
//
// Default key-expr is '<namespace>/**' (everything under your prefix). Use ** for any depth,
// * for a single segment.

import java.nio.charset.StandardCharsets;
import java.time.LocalTime;
import java.time.format.DateTimeFormatter;
import java.util.concurrent.atomic.AtomicInteger;

import io.zenoh.Session;
import io.zenoh.bytes.ZBytes;
import io.zenoh.keyexpr.KeyExpr;
import io.zenoh.pubsub.Subscriber;
import io.zenoh.sample.Sample;

public final class Subscribe {

    private static final DateTimeFormatter TIME = DateTimeFormatter.ofPattern("HH:mm:ss");

    public static void main(String[] args) throws Exception {
        String keyexpr = args.length > 0 ? args[0] : EfdiConnect.namespace() + "/**";
        int limit = args.length > 1 ? Integer.parseInt(args[1]) : 0; // 0 = follow forever
        AtomicInteger seen = new AtomicInteger(0);

        try (Session session = EfdiConnect.session();
             KeyExpr keyExpr = KeyExpr.tryFrom(keyexpr)) {
            Subscriber subscriber = session.declareSubscriber(keyExpr, sample -> onSample(sample, seen));
            System.out.println("subscribed: " + keyexpr + " (Ctrl-C to stop)");
            System.out.flush();
            while (limit == 0 || seen.get() < limit) {
                Thread.sleep(200);
            }
        }
    }

    private static void onSample(Sample sample, AtomicInteger seen) {
        ZBytes payload = sample.getPayload();
        byte[] raw = payload.toBytes();
        String shown;
        if (isUtf8(raw)) {
            shown = new String(raw, StandardCharsets.UTF_8);
        } else {
            shown = "<" + raw.length + " bytes> " + hexPrefix(raw, 32);
        }
        System.out.println(LocalTime.now().format(TIME) + "  " + sample.getKeyExpr() + "  " + shown);
        System.out.flush();
        seen.incrementAndGet();
    }

    /** Strict UTF-8 check: decode and compare round-trip length, falling back to hex on garbage. */
    private static boolean isUtf8(byte[] b) {
        try {
            StandardCharsets.UTF_8.newDecoder()
                .decode(java.nio.ByteBuffer.wrap(b));
            return true;
        } catch (java.nio.charset.CharacterCodingException e) {
            return false;
        }
    }

    private static String hexPrefix(byte[] b, int max) {
        int len = Math.min(b.length, max);
        StringBuilder sb = new StringBuilder(len * 2);
        for (int i = 0; i < len; i++) {
            sb.append(String.format("%02x", b[i]));
        }
        return sb.toString();
    }
}
