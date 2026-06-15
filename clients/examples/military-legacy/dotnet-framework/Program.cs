/*
 * Program.cs — publish to / receive from a goat-moon-pod on .NET FRAMEWORK 4.x.
 *
 * This is NOT a Zenoh client. It speaks plain HTTP to the local REST bridge
 * (clients/examples/bridges/rest-http/), which runs next to the pod on 127.0.0.1
 * and holds the Zenoh mTLS identity. We use only System.Net.HttpWebRequest from the
 * .NET Framework BCL — no NuGet packages, no modern HttpClient required, no internet.
 *
 *   POST http://127.0.0.1:8080/pub/<suffix>        body -> publish
 *   GET  http://127.0.0.1:8080/sub/<keyexpr>?count=N   block, return JSON array
 *   GET  http://127.0.0.1:8080/stream/<keyexpr>    Server-Sent Events, continuous
 *
 * Build & run: see README.md (csc.exe or the minimal old-style .csproj).
 *   GoatBridgeClient.exe pub sensors/temp {"temp_c":21.5}
 *   GoatBridgeClient.exe pub sensors/temp 21.5 10 200
 *   GoatBridgeClient.exe sub sensors/temp 5 60
 *   GoatBridgeClient.exe stream sensors/temp
 *
 * The bridge base URL defaults to http://127.0.0.1:8080; override with the BRIDGE_URL env var.
 */
using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;

namespace GoatBridgeClient
{
    internal static class Program
    {
        private static string BridgeBase()
        {
            string env = Environment.GetEnvironmentVariable("BRIDGE_URL");
            return string.IsNullOrEmpty(env) ? "http://127.0.0.1:8080" : env;
        }

        private static int Main(string[] args)
        {
            if (args.Length < 2)
            {
                Console.Error.WriteLine("usage:");
                Console.Error.WriteLine("  GoatBridgeClient pub    <suffix>  <body> [count] [intervalMs]");
                Console.Error.WriteLine("  GoatBridgeClient sub    <keyexpr> [count] [timeoutSec]");
                Console.Error.WriteLine("  GoatBridgeClient stream <keyexpr>");
                return 2;
            }

            string cmd = args[0].ToLowerInvariant();
            try
            {
                switch (cmd)
                {
                    case "pub":
                        return Publish(args);
                    case "sub":
                        return Subscribe(args);
                    case "stream":
                        return Stream(args[1]);
                    default:
                        Console.Error.WriteLine("unknown command '" + cmd + "' (use pub | sub | stream)");
                        return 2;
                }
            }
            catch (WebException wex)
            {
                Console.Error.WriteLine("HTTP error talking to bridge at " + BridgeBase() + " : " + wex.Message);
                Console.Error.WriteLine("  (is the REST bridge running? see ../../bridges/rest-http/README.md)");
                return 1;
            }
        }

        // POST a body to /pub/<suffix>. The bridge returns HTTP 201 on success.
        private static int Publish(string[] args)
        {
            if (args.Length < 3)
            {
                Console.Error.WriteLine("usage: pub <suffix> <body> [count] [intervalMs]");
                return 2;
            }
            string suffix = args[1];
            string body = args[2];
            int count = args.Length >= 4 ? int.Parse(args[3]) : 1;
            int intervalMs = args.Length >= 5 ? int.Parse(args[4]) : 0;

            byte[] payload = Encoding.UTF8.GetBytes(body);
            for (int i = 0; i < count; i++)
            {
                var req = (HttpWebRequest)WebRequest.Create(BridgeBase() + "/pub/" + suffix);
                req.Method = "POST";
                req.ContentType = "application/octet-stream";
                req.ContentLength = payload.Length;
                req.Timeout = 30000;
                using (Stream rs = req.GetRequestStream())
                {
                    rs.Write(payload, 0, payload.Length);
                }
                using (var resp = (HttpWebResponse)req.GetResponse())
                {
                    Console.WriteLine("POST /pub/" + suffix + " -> HTTP " + (int)resp.StatusCode
                                      + " (" + payload.Length + " bytes) " + ReadBody(resp));
                }
                if (i + 1 < count && intervalMs > 0) Thread.Sleep(intervalMs);
            }
            return 0;
        }

        // GET /sub/<keyexpr>?count=N&timeout=S — blocks until N samples or the timeout, then
        // returns a JSON array. We print it raw (no JSON dependency).
        private static int Subscribe(string[] args)
        {
            string keyexpr = args[1];
            int count = args.Length >= 3 ? int.Parse(args[2]) : 1;
            int timeoutSec = args.Length >= 4 ? int.Parse(args[3]) : 30;

            string url = BridgeBase() + "/sub/" + keyexpr + "?count=" + count + "&timeout=" + timeoutSec;
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "GET";
            // The HTTP read must outlast the bridge's blocking window, or the GET dies early.
            req.Timeout = (timeoutSec + 10) * 1000;
            using (var resp = (HttpWebResponse)req.GetResponse())
            {
                Console.WriteLine(ReadBody(resp));
            }
            return 0;
        }

        // GET /stream/<keyexpr> — Server-Sent Events. Print each "data:" line; skip ":" comments.
        private static int Stream(string keyexpr)
        {
            var req = (HttpWebRequest)WebRequest.Create(BridgeBase() + "/stream/" + keyexpr);
            req.Method = "GET";
            req.Accept = "text/event-stream";
            req.Timeout = 10000;          // connect timeout only...
            req.ReadWriteTimeout = Timeout.Infinite;   // ...the stream itself is open-ended
            req.AllowReadStreamBuffering = false;       // deliver lines as they arrive, not buffered
            Console.Error.WriteLine("streaming " + keyexpr + " from " + BridgeBase() + " (Ctrl-C to stop)");
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var reader = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            {
                string line;
                while ((line = reader.ReadLine()) != null)
                {
                    if (line.StartsWith("data:"))
                        Console.WriteLine(line.Substring(5).Trim());
                    // blank lines and ":"-comment keepalive lines are ignored
                }
            }
            return 0;
        }

        private static string ReadBody(HttpWebResponse resp)
        {
            using (var reader = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            {
                return reader.ReadToEnd();
            }
        }
    }
}
