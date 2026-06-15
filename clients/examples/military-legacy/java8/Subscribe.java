/*
 * Subscribe.java — receive from a goat-moon-pod on JDK 8, with ZERO dependencies.
 *
 * This is NOT a Zenoh client. It does an HTTP GET against the local REST bridge
 * (clients/examples/bridges/rest-http/), which holds the Zenoh mTLS identity and
 * subscribes on our behalf. Only java.net.HttpURLConnection from the JDK 8 stdlib.
 *
 * Two modes:
 *   batch  (default) GET /sub/<keyexpr>?count=N&timeout=S  -> blocks, returns a JSON array
 *   stream           GET /stream/<keyexpr>                 -> Server-Sent Events, runs forever
 *
 * Build & run (see README.md):
 *   javac Subscribe.java
 *   java Subscribe sensors/temp            # wait for 1 sample under your namespace
 *   java Subscribe sensors/temp 5 60       # wait for 5 samples, up to 60s
 *   java Subscribe release/goat/** 3   # inbound data from goat
 *   java Subscribe sensors/temp stream     # follow continuously (Ctrl-C to stop)
 *
 * The bridge returns each sample as JSON: {"key":"...","ts":...,"text":"..."} (or "b64" for
 * non-UTF-8 bytes). We print the raw JSON lines rather than pulling in a JSON parser dependency
 * — a JDK-8 shop can parse these with whatever it already has, or just log them.
 */
import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.ByteArrayOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

public class Subscribe {

    static String bridgeBase() {
        String env = System.getenv("BRIDGE_URL");
        return (env != null && !env.isEmpty()) ? env : "http://127.0.0.1:8080";
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("usage: java Subscribe <keyexpr> [count [timeoutSec] | stream]");
            System.err.println("  e.g. java Subscribe sensors/temp");
            System.err.println("       java Subscribe sensors/temp 5 60");
            System.err.println("       java Subscribe sensors/temp stream");
            System.exit(2);
        }
        String keyexpr = args[0];
        boolean stream = args.length >= 2 && args[1].equalsIgnoreCase("stream");
        if (stream) {
            stream(keyexpr);
        } else {
            int count = (args.length >= 2) ? Integer.parseInt(args[1]) : 1;
            int timeoutSec = (args.length >= 3) ? Integer.parseInt(args[2]) : 30;
            batch(keyexpr, count, timeoutSec);
        }
    }

    /** GET /sub/<keyexpr>?count=N&timeout=S — blocks until N samples arrive or the timeout. */
    static void batch(String keyexpr, int count, int timeoutSec) throws Exception {
        // The keyexpr is a path; only the query values need encoding. '*'/'**' must stay literal,
        // so we put the keyexpr straight into the path (it is already a valid URL path).
        String path = "/sub/" + keyexpr + "?count=" + count + "&timeout=" + timeoutSec;
        URL url = new URL(bridgeBase() + path);
        HttpURLConnection con = (HttpURLConnection) url.openConnection();
        try {
            con.setRequestMethod("GET");
            con.setConnectTimeout(5000);
            // Read timeout must exceed the bridge's blocking window, or the GET dies early.
            con.setReadTimeout((timeoutSec + 10) * 1000);
            int status = con.getResponseCode();
            String resp = drain(status >= 400 ? con.getErrorStream() : con.getInputStream());
            if (status != 200) {
                System.err.println("HTTP " + status + " from " + url + " : " + resp
                        + "  (is the bridge running on " + bridgeBase() + " ?)");
                System.exit(1);
            }
            // resp is a JSON array of sample objects; print it as-is.
            System.out.println(resp);
        } finally {
            con.disconnect();
        }
    }

    /** GET /stream/<keyexpr> — Server-Sent Events; print each "data:" line, ignore comments. */
    static void stream(String keyexpr) throws Exception {
        URL url = new URL(bridgeBase() + "/stream/" + keyexpr);
        HttpURLConnection con = (HttpURLConnection) url.openConnection();
        con.setRequestMethod("GET");
        con.setConnectTimeout(5000);
        con.setReadTimeout(0);                 // 0 = no read timeout; the stream is open-ended
        con.setRequestProperty("Accept", "text/event-stream");
        int status = con.getResponseCode();
        if (status != 200) {
            System.err.println("HTTP " + status + " (is the bridge running on " + bridgeBase() + " ?)");
            System.exit(1);
        }
        System.err.println("streaming " + keyexpr + " from " + bridgeBase() + " (Ctrl-C to stop)");
        try (BufferedReader br = new BufferedReader(new InputStreamReader(con.getInputStream(), "UTF-8"))) {
            String line;
            while ((line = br.readLine()) != null) {
                // SSE: real samples arrive as "data: {json}"; ": keepalive" lines are heartbeats.
                if (line.startsWith("data:")) {
                    System.out.println(line.substring(5).trim());
                }
                // blank lines and ":"-comment lines are ignored.
            }
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
