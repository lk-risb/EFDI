// GoatConnect — open an mTLS Zenoh session to a goat-moon-pod from env vars.
//
// The ONLY goat-specific code you need. Everything else is plain Zenoh (the official
// zenoh-java 1.x binding, io.zenoh, which bundles the native zenoh library as a resource).
//
//     import io.zenoh.Session;
//     try (Session s = GoatConnect.session()) {
//         var pub = s.declarePublisher(KeyExpr.tryFrom(GoatConnect.key("sensors/temp")));
//         pub.put(ZBytes.from("21.5"));
//     }
//
// Env vars (see ../../README.md):
//
//     GOAT_ROUTER       tls/127.0.0.1:7447     pod Zenoh endpoint
//     GOAT_CERT         path to your mTLS client cert (PEM)
//     GOAT_KEY          path to your mTLS private key (PEM)
//     GOAT_CA           path to the CA root that signs the router (PEM)
//     GOAT_NAMESPACE    release/<you>          your owned prefix (publish under this)
//     GOAT_VERIFY_NAME  "true"/"false"         TLS hostname verification (default false for a
//                                              local pod reached at 127.0.0.1; "true" for a
//                                              DNS-named remote router)
//
// Requires JDK 17+. Depends on org.eclipse.zenoh:zenoh-java-jvm:1.9.0 (see
// ../../examples/modern/java/README.md for the build file).

import io.zenoh.Config;
import io.zenoh.Session;
import io.zenoh.Zenoh;
import io.zenoh.exceptions.ZError;

/** Bundle -> Zenoh session helper. All static; no instances. */
public final class GoatConnect {

    private GoatConnect() {}

    /** Return the value of {@code name} or throw with an actionable message if unset/empty. */
    static String env(String name) {
        String v = System.getenv(name);
        if (v == null || v.isEmpty()) {
            throw new IllegalStateException(
                name + " is not set. Source the pod env (see clients/README.md), e.g.\n"
                + "  export " + name + "=...");
        }
        return v;
    }

    /**
     * Build the Zenoh client config from the GOAT_* env vars.
     *
     * <p>CRITICAL GOTCHA: the mTLS settings are emitted as ONE json5 object under
     * {@code transport/link/tls} with {@code enable_mtls: true}, and the whole config is loaded in
     * a single {@link Config#fromJson5(String)} call. Setting the TLS sub-keys one at a time
     * silently does NOT enable the client-cert send path on Zenoh 1.x — the session opens but the
     * router rejects you, or you connect read-only. So we render the entire block and load it once.
     */
    public static Config config() throws ZError {
        String router = env("GOAT_ROUTER");
        String ca = env("GOAT_CA");
        String cert = env("GOAT_CERT");
        String key = env("GOAT_KEY");
        boolean verifyName = "true".equalsIgnoreCase(System.getenv("GOAT_VERIFY_NAME"));

        // Hand-rolled json5/JSON — these values are file paths + a URL, no escaping concerns beyond
        // quoting. Keep the TLS object whole (the gotcha above).
        String json = "{"
            + "\"mode\": \"client\","
            + "\"connect\": { \"endpoints\": [" + quote(router) + "] },"
            + "\"transport\": { \"link\": { \"tls\": {"
            + "\"root_ca_certificate\": " + quote(ca) + ","
            + "\"connect_certificate\": " + quote(cert) + ","
            + "\"connect_private_key\": " + quote(key) + ","
            + "\"enable_mtls\": true,"
            + "\"verify_name_on_connect\": " + verifyName
            + "} } }"
            + "}";

        return Config.fromJson5(json);
    }

    /** Open and return a Zenoh session. Close it (try-with-resources). */
    public static Session session() throws ZError {
        return Zenoh.open(config());
    }

    /** Your owned prefix, e.g. {@code release/acme} (trailing slash stripped). */
    public static String namespace() {
        String ns = env("GOAT_NAMESPACE");
        int end = ns.length();
        while (end > 0 && ns.charAt(end - 1) == '/') {
            end--;
        }
        return ns.substring(0, end);
    }

    /**
     * Build a fully-qualified key under your namespace: {@code key("sensors/temp")} ->
     * {@code release/acme/sensors/temp}. Pass an absolute key (e.g. {@code release/goat/**})
     * only if you have rights to it.
     */
    public static String key(String suffix) {
        int start = 0;
        while (start < suffix.length() && suffix.charAt(start) == '/') {
            start++;
        }
        return namespace() + "/" + suffix.substring(start);
    }

    /** Minimal JSON string quoting (backslash + double-quote). */
    private static String quote(String s) {
        return "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }
}
