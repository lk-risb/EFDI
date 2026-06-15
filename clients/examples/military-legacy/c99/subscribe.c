/* subscribe.c — receive data from the goat fabric (C99 / zenoh-c 1.9.0).
 *
 *   ./subscribe                              # your own namespace (<ns>/**), follow forever
 *   ./subscribe 'release/goat/**'     # inbound data goat sends you
 *   ./subscribe '<keyexpr>' 5                # exit after 5 samples
 *
 * Plain C99, minimal deps (libc + libzenohc). Default key-expr is <namespace>/** (everything
 * under your prefix). Use ** for any depth, * for a single segment.
 *
 * THE mTLS GOTCHA: the whole Zenoh config — including the transport/link/tls object — is built
 * as ONE json5 string and parsed with zc_config_from_str(). The TLS settings MUST stay in a
 * single object with enable_mtls:true; splitting them silently disables the client-cert send
 * path on Zenoh 1.x. See goat_config() below.
 */

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "zenoh.h"

/* env_or_die — return the env var or print an actionable message and exit. */
static const char *env_or_die(const char *name) {
    const char *v = getenv(name);
    if (v == NULL || v[0] == '\0') {
        fprintf(stderr,
                "%s is not set. Source the pod env (see clients/README.md), e.g.\n"
                "  export %s=...\n",
                name, name);
        exit(2);
    }
    return v;
}

/* goat_namespace — GOAT_NAMESPACE with any trailing '/' stripped, copied into `out`. */
static void goat_namespace(char *out, size_t out_sz) {
    const char *ns = env_or_die("GOAT_NAMESPACE");
    size_t n = strlen(ns);
    while (n > 0 && ns[n - 1] == '/') n--;
    if (n >= out_sz) n = out_sz - 1;
    memcpy(out, ns, n);
    out[n] = '\0';
}

/* goat_config — build the mTLS client config from the GOAT_* env vars (the one gotcha). */
static z_result_t goat_config(z_owned_config_t *cfg) {
    const char *router = env_or_die("GOAT_ROUTER");
    const char *ca     = env_or_die("GOAT_CA");
    const char *cert   = env_or_die("GOAT_CERT");
    const char *key    = env_or_die("GOAT_KEY");

    const char *vn = getenv("GOAT_VERIFY_NAME");
    const char *verify_name =
        (vn != NULL && (strcmp(vn, "true") == 0 || strcmp(vn, "TRUE") == 0 ||
                        strcmp(vn, "True") == 0))
            ? "true"
            : "false";

    char json5[2048];
    int written = snprintf(
        json5, sizeof(json5),
        "{ mode: \"client\","
        " connect: { endpoints: [\"%s\"] },"
        " transport: { link: { tls: {"
        " root_ca_certificate: \"%s\","
        " connect_certificate: \"%s\","
        " connect_private_key: \"%s\","
        " enable_mtls: true,"
        " verify_name_on_connect: %s"
        " } } } }",
        router, ca, cert, key, verify_name);
    if (written < 0 || (size_t)written >= sizeof(json5)) {
        fprintf(stderr, "config json5 too long (cert/key/ca paths exceed buffer)\n");
        return -1;
    }
    return zc_config_from_str(cfg, json5);
}

/* Shared state between the zenoh worker thread (data_handler) and main. */
struct sub_ctx {
    size_t seen;
    size_t limit; /* 0 = forever */
};

/* data_handler — runs on a zenoh worker thread for each received sample. */
static void data_handler(z_loaned_sample_t *sample, void *arg) {
    struct sub_ctx *ctx = (struct sub_ctx *)arg;

    /* key expression -> non-owned view string */
    z_view_string_t keystr;
    z_keyexpr_as_view_string(z_sample_keyexpr(sample), &keystr);

    /* payload bytes -> owned, NUL-terminated string for printing */
    z_owned_string_t payload;
    z_bytes_to_string(z_sample_payload(sample), &payload);

    printf("%.*s  %.*s\n",
           (int)z_string_len(z_loan(keystr)), z_string_data(z_loan(keystr)),
           (int)z_string_len(z_loan(payload)), z_string_data(z_loan(payload)));
    fflush(stdout);

    z_drop(z_move(payload));

    ctx->seen++;
    /* Note: when limit is reached we stop printing in main's loop; we don't drop the session
     * from this callback (it would race the worker). main polls ctx->seen and exits. */
}

static volatile sig_atomic_t g_stop = 0;
static void on_sigint(int signo) {
    (void)signo;
    g_stop = 1;
}

int main(int argc, char **argv) {
    char ns[1024];
    char default_ke[1280];
    const char *keyexpr;
    if (argc > 1) {
        keyexpr = argv[1];
    } else {
        goat_namespace(ns, sizeof(ns));
        snprintf(default_ke, sizeof(default_ke), "%s/**", ns);
        keyexpr = default_ke;
    }

    struct sub_ctx ctx;
    ctx.seen = 0;
    ctx.limit = (argc > 2) ? (size_t)strtoul(argv[2], NULL, 10) : 0;

    z_owned_config_t config;
    if (goat_config(&config) < 0) {
        fprintf(stderr, "failed to build config\n");
        return 1;
    }

    z_owned_session_t session;
    if (z_open(&session, z_move(config), NULL) < 0) {
        fprintf(stderr, "failed to open session (check certs, router, and clock skew)\n");
        return 1;
    }

    z_view_keyexpr_t ke;
    if (z_view_keyexpr_from_str(&ke, keyexpr) < 0) {
        fprintf(stderr, "invalid key expression: %s\n", keyexpr);
        z_drop(z_move(session));
        return 1;
    }

    z_owned_closure_sample_t callback;
    z_closure(&callback, data_handler, NULL, &ctx);

    z_owned_subscriber_t sub;
    if (z_declare_subscriber(z_loan(session), &sub, z_loan(ke), z_move(callback), NULL) < 0) {
        fprintf(stderr, "failed to declare subscriber\n");
        z_drop(z_move(session));
        return 1;
    }

    signal(SIGINT, on_sigint);
    printf("subscribed: %s (Ctrl-C to stop)\n", keyexpr);
    fflush(stdout);

    /* Poll for stop conditions; samples arrive on the worker thread via data_handler. */
    while (!g_stop) {
        if (ctx.limit != 0 && ctx.seen >= ctx.limit) break;
        z_sleep_ms(100);
    }

    z_drop(z_move(sub));
    z_drop(z_move(session));
    return 0;
}
