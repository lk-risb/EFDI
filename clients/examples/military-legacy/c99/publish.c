/* publish.c — send data to the goat fabric (C99 / zenoh-c 1.9.0).
 *
 *   export GOAT_ROUTER=tls/127.0.0.1:7447 GOAT_CERT=... GOAT_KEY=... GOAT_CA=... \
 *          GOAT_NAMESPACE=release/acme
 *   ./publish              # one JSON sample
 *   ./publish 50 200       # 50 samples, 200ms apart
 *
 * Plain C99, minimal deps (libc + libzenohc). Publishes JSON under <namespace>/sensors/temp.
 * Real payloads can be any bytes; JSON here for legibility — the fabric is payload-agnostic.
 *
 * THE mTLS GOTCHA: the whole Zenoh config — including the transport/link/tls object — is built
 * as ONE json5 string and parsed with zc_config_from_str(). The TLS settings MUST stay in a
 * single object with enable_mtls:true; splitting them across separate inserts silently disables
 * the client-cert send path on Zenoh 1.x. See goat_config() below.
 */

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

/* goat_config — build the mTLS client config from the GOAT_* env vars (the one gotcha).
 * Returns 0 on success; fills `cfg` with an owned config the caller must z_drop(). */
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

    /* One json5 document. transport/link/tls is a single object — do NOT split it. PEM paths
     * are assumed not to contain '"' or '\' (typical on Unix); escape if yours can. */
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

int main(int argc, char **argv) {
    size_t n = (argc > 1) ? (size_t)strtoul(argv[1], NULL, 10) : 1;
    if (n == 0) n = 1;
    size_t interval_ms = (argc > 2) ? (size_t)strtoul(argv[2], NULL, 10) : 1000;

    /* key = <namespace>/sensors/temp */
    char ns[1024];
    goat_namespace(ns, sizeof(ns));
    char key[1280];
    snprintf(key, sizeof(key), "%s/sensors/temp", ns);

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
    if (z_view_keyexpr_from_str(&ke, key) < 0) {
        fprintf(stderr, "invalid key expression: %s\n", key);
        z_drop(z_move(session));
        return 1;
    }

    z_owned_publisher_t pub;
    if (z_declare_publisher(z_loan(session), &pub, z_loan(ke), NULL) < 0) {
        fprintf(stderr, "failed to declare publisher\n");
        z_drop(z_move(session));
        return 1;
    }

    for (size_t i = 0; i < n; i++) {
        char body[256];
        double temp_c = 21.5 + (double)i * 0.1;
        snprintf(body, sizeof(body), "{\"seq\":%zu,\"temp_c\":%.1f}", i, temp_c);

        z_owned_bytes_t payload;
        z_bytes_copy_from_str(&payload, body);

        z_publisher_put_options_t options;
        z_publisher_put_options_default(&options);

        if (z_publisher_put(z_loan(pub), z_move(payload), &options) < 0) {
            fprintf(stderr, "put failed at seq %zu\n", i);
        } else {
            printf("published -> %s: %s\n", key, body);
        }

        if (i + 1 < n) {
            z_sleep_ms(interval_ms);
        }
    }

    z_drop(z_move(pub));
    z_drop(z_move(session));
    return 0;
}
