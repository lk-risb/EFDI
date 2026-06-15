"""goat_connect — open an mTLS Zenoh session to a goat-moon-pod from env vars.

The ONLY goat-specific code you need. Everything else is plain Zenoh (eclipse-zenoh 1.x).

    pip install eclipse-zenoh

Env vars (see ../../README.md):
    GOAT_ROUTER     tls/127.0.0.1:7447     pod Zenoh endpoint
    GOAT_CERT       path to your mTLS client cert (PEM)
    GOAT_KEY        path to your mTLS private key (PEM)
    GOAT_CA         path to the CA root that signs the router (PEM)
    GOAT_NAMESPACE  release/<you>          your owned prefix (publish under this)
    GOAT_VERIFY_NAME  "true"/"false"       TLS hostname verification (default false for a
                                           local pod reached at 127.0.0.1; "true" for a
                                           DNS-named remote router)

Usage:
    import goat_connect
    with goat_connect.session() as s:
        s.put(goat_connect.key("sensors/temp"), b"21.5")
"""

import json
import os
import zenoh


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(
            f"{name} is not set. Source the pod env (see clients/README.md), e.g.\n"
            f"  export {name}=..."
        )
    return v


def config() -> "zenoh.Config":
    """Build the Zenoh client config. mTLS TLS block inserted as ONE object — the sub-key
    form silently disables the client-cert send path on Zenoh 1.x (the #1 gotcha)."""
    router = _env("GOAT_ROUTER")
    verify_name = os.environ.get("GOAT_VERIFY_NAME", "false").lower() == "true"
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([router]))
    conf.insert_json5(
        "transport/link/tls",
        json.dumps(
            {
                "root_ca_certificate": _env("GOAT_CA"),
                "connect_certificate": _env("GOAT_CERT"),
                "connect_private_key": _env("GOAT_KEY"),
                "enable_mtls": True,
                "verify_name_on_connect": verify_name,
            }
        ),
    )
    return conf


def session() -> "zenoh.Session":
    """Open and return a Zenoh session. Close it (or use as a context manager)."""
    return zenoh.open(config())


def namespace() -> str:
    """Your owned prefix, e.g. 'release/acme' (trailing slash stripped)."""
    return _env("GOAT_NAMESPACE").rstrip("/")


def key(suffix: str) -> str:
    """Build a fully-qualified key under your namespace: key('sensors/temp') ->
    'release/acme/sensors/temp'. Pass an absolute key (starts with a non-namespace prefix)
    only if you have rights to it (e.g. subscribing to 'release/goat/**')."""
    return f"{namespace()}/{suffix.lstrip('/')}"
