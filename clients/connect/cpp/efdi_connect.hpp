// efdi_connect.hpp — open an mTLS Zenoh session to an EFDI pod from env vars.
//
// Header-only. The ONLY EFDI-specific code you need; everything else is plain Zenoh
// (the official zenoh-cpp 1.x binding over zenoh-c).
//
//     #include "efdi_connect.hpp"
//     auto session = efdi::session();                    // opens an mTLS client session
//     auto ke = zenoh::KeyExpr(efdi::key("sensors/temp"));
//     auto pub = session.declare_publisher(ke);
//     pub.put("21.5");
//
// Env vars (see ../../README.md):
//
//     EFDI_ROUTER       tls/127.0.0.1:7447     pod Zenoh endpoint
//     EFDI_CERT         path to your mTLS client cert (PEM)
//     EFDI_KEY          path to your mTLS private key (PEM)
//     EFDI_CA           path to the CA root that signs the router (PEM)
//     PARTNER_NAMESPACE    release/<you>          your owned prefix (publish under this)
//     EFDI_VERIFY_NAME  "true"/"false"         TLS hostname verification (default false for a
//                                              local pod reached at 127.0.0.1; "true" for a
//                                              DNS-named remote router)
//
// Requires zenoh-cpp 1.9.0 built over zenoh-c (the ZENOHCXX_ZENOHC backend — the default for
// the C++ binding) with the unstable API enabled. See ../../examples/modern/cpp/README.md.

#ifndef EFDI_CONNECT_HPP
#define EFDI_CONNECT_HPP

#include <cstdlib>
#include <stdexcept>
#include <string>

#include "zenoh.hxx"

namespace efdi {

// env() returns the value of `name` or throws with an actionable message if unset/empty.
inline std::string env(const char* name) {
    const char* v = std::getenv(name);
    if (v == nullptr || v[0] == '\0') {
        throw std::runtime_error(
            std::string(name) +
            " is not set. Source the pod env (see clients/README.md), e.g.\n  export " +
            name + "=...");
    }
    return std::string(v);
}

// json_escape() escapes a string for embedding inside a json5 double-quoted value. PEM paths
// can contain characters (notably backslashes on Windows) that must be escaped so the json5
// parser sees a clean string literal.
inline std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '"':  out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:   out += c; break;
        }
    }
    return out;
}

// config() builds the Zenoh client config from the EFDI_* env vars.
//
// CRITICAL GOTCHA: the mTLS TLS settings live in ONE json5 object at "transport/link/tls" with
// enable_mtls=true. Inserting the sub-keys individually
// (transport/link/tls/connect_certificate, etc.) silently does NOT enable the client-cert send
// path on Zenoh 1.x — the session opens but the router rejects you or you connect read-only. We
// build the whole config (mode + connect endpoint + the complete TLS block) as a single json5
// document and parse it in one Config::from_str call.
inline zenoh::Config config() {
    const std::string router = env("EFDI_ROUTER");
    const std::string ca     = env("EFDI_CA");
    const std::string cert   = env("EFDI_CERT");
    const std::string key    = env("EFDI_KEY");

    const char* vn = std::getenv("EFDI_VERIFY_NAME");
    std::string verify_name = "false";
    if (vn != nullptr) {
        std::string lower(vn);
        for (char& c : lower) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        if (lower == "true") verify_name = "true";
    }

    // One json5 document. transport/link/tls is a single object — see note above.
    const std::string json5 =
        "{\n"
        "  mode: \"client\",\n"
        "  connect: { endpoints: [\"" + json_escape(router) + "\"] },\n"
        "  transport: { link: { tls: {\n"
        "    root_ca_certificate: \"" + json_escape(ca) + "\",\n"
        "    connect_certificate: \"" + json_escape(cert) + "\",\n"
        "    connect_private_key: \"" + json_escape(key) + "\",\n"
        "    enable_mtls: true,\n"
        "    verify_name_on_connect: " + verify_name + "\n"
        "  } } }\n"
        "}\n";

    return zenoh::Config::from_str(json5);
}

// session() opens and returns a Zenoh client session over mTLS.
inline zenoh::Session session() {
    return zenoh::Session::open(config());
}

// namespace_prefix() returns your owned prefix, e.g. "release/acme" (trailing slash stripped).
// (Named with a trailing underscore-style suffix because `namespace` is a C++ keyword.)
inline std::string namespace_prefix() {
    std::string ns = env("PARTNER_NAMESPACE");
    while (!ns.empty() && ns.back() == '/') ns.pop_back();
    return ns;
}

// key() builds a fully-qualified key under your namespace: key("sensors/temp") ->
// "release/acme/sensors/temp". Pass an absolute key (e.g. "release/<partner>/**") only if you
// have rights to it.
inline std::string key(const std::string& suffix) {
    std::string s = suffix;
    size_t start = 0;
    while (start < s.size() && s[start] == '/') ++start;
    return namespace_prefix() + "/" + s.substr(start);
}

}  // namespace efdi

#endif  // EFDI_CONNECT_HPP
