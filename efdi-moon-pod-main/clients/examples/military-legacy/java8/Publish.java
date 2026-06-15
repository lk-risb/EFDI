/*
 * Publish.java — publish to a goat-moon-pod from JDK 8, with ZERO dependencies.
 *
 * This is NOT a Zenoh client. It POSTs an HTTP body to the local REST bridge
 * (clients/examples/bridges/rest-http/), which runs next to the pod on 127.0.0.1
 * and does the Zenoh mTLS publish for us. We use only java.net.HttpURLConnection
 * from the JDK 8 standard library — no Maven, no Gradle, no jars.
 *
 *   POST http://127.0.0.1:8080/pub/<suffix>   body -> published to <namespace>/<suffix>
 *
 * Build & run (see README.md):
 *   javac Publish.java
 *   java Publish sensors/temp '{"temp_c":21.5}'
 *   java Publish sensors/temp '21.5' 10 200      # 10 samples, 200ms apart
 */
import java.io.OutputStream;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.Charset;

public class Publish {

    // The bridge's base URL. Override with BRIDGE_URL if you moved it off the default port.
    static String bridgeBase() {
        String env = System.getenv("BRIDGE_URL");
        return (env != null && !env.isEmpty()) ? env : "http://127.0.0.1:8080";
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("usage: java Publish <suffix> <body> [count] [intervalMs]");
            System.err.println("  e.g. java Publish sensors/temp '{\"temp_c\":21.5}'");
            System.err.println("       java Publish sensors/temp '21.5' 10 200");
            System.exit(2);
        }
        String suffix = args[0];
        String body = args[1];
        int count = (args.length >= 3) ? Integer.parseInt(args[2]) : 1;
        long intervalMs = (args.length >= 4) ? Long.parseLong(args[3]) : 0L;

        for (int i = 0; i < count; i++) {
            int status = post("/pub/" + suffix, body);
            // The bridge returns 201 on a successful publish.
            System.out.println("POST /pub/" + suffix + " -> HTTP " + status
                    + " (" + body.getBytes("UTF-8").length + " bytes)");
            if (status != 201) {
                System.err.println("unexpected status " + status + " (is the bridge running on "
                        + bridgeBase() + " ?)");
                System.exit(1);
            }
            if (i + 1 < count && intervalMs > 0) Thread.sleep(intervalMs);
        }
    }

    /** POST a UTF-8 body to a path on the bridge; return the HTTP status code. */
    static int post(String path, String body) throws Exception {
        byte[] payload = body.getBytes(Charset.forName("UTF-8"));
        // new URL(String) is the correct, non-deprecated API on JDK 8 (the target here). It is
        // only flagged as deprecated on JDK 20+; this code is meant for JDK 8.
        URL url = new URL(bridgeBase() + path);
        HttpURLConnection con = (HttpURLConnection) url.openConnection();
        try {
            con.setRequestMethod("POST");
            con.setDoOutput(true);                          // we are sending a body
            con.setFixedLengthStreamingMode(payload.length);
            con.setRequestProperty("Content-Type", "application/octet-stream");
            con.setConnectTimeout(5000);
            con.setReadTimeout(30000);
            try (OutputStream os = con.getOutputStream()) {
                os.write(payload);
            }
            int status = con.getResponseCode();
            // Drain the response so the connection can be reused / closed cleanly.
            InputStream is = (status >= 400) ? con.getErrorStream() : con.getInputStream();
            String resp = drain(is);
            if (resp != null && !resp.isEmpty() && status != 201) System.err.println("  bridge said: " + resp);
            return status;
        } finally {
            con.disconnect();
        }
    }

    static String drain(InputStream is) throws Exception {
        if (is == null) return "";
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        byte[] tmp = new byte[4096];
        int n;
        while ((n = is.read(tmp)) != -1) buf.write(tmp, 0, n);
        is.close();
        return new String(buf.toByteArray(), "UTF-8");
    }
}
