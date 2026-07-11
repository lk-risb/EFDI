//! efdi_connect — open an mTLS Zenoh session to a goat-moon-pod from env vars.
//!
//! The ONLY goat-specific code you need. Everything else is plain Zenoh (the official
//! `zenoh` crate, 1.x).
//!
//! ```no_run
//! # async fn demo() -> zenoh::Result<()> {
//! let session = efdi_connect::session().await?;
//! session.put(efdi_connect::key("sensors/temp")?, "21.5").await?;
//! # Ok(()) }
//! ```
//!
//! Env vars (see ../../README.md):
//!
//! - `EFDI_ROUTER`      `tls/127.0.0.1:7447`   pod Zenoh endpoint
//! - `EFDI_CERT`        path to your mTLS client cert (PEM)
//! - `EFDI_KEY`         path to your mTLS private key (PEM)
//! - `EFDI_CA`          path to the CA root that signs the router (PEM)
//! - `PARTNER_NAMESPACE`   `release/<you>`        your owned prefix (publish under this)
//! - `EFDI_VERIFY_NAME` `"true"`/`"false"`     TLS hostname verification (default false for a
//!   local pod reached at 127.0.0.1; "true" for a DNS-named remote router)

use std::env;

use serde_json::json;
use zenoh::config::Config;
use zenoh::{Session, Wait};

/// Error type: a simple message string.
pub type Error = Box<dyn std::error::Error + Send + Sync>;
pub type Result<T> = std::result::Result<T, Error>;

/// Read a required env var or return an actionable error.
fn env_var(name: &str) -> Result<String> {
    match env::var(name) {
        Ok(v) if !v.is_empty() => Ok(v),
        _ => Err(format!(
            "{name} is not set. Source the pod env (see clients/README.md), e.g.\n  export {name}=..."
        )
        .into()),
    }
}

/// Build the Zenoh client config from the GOAT_* env vars.
///
/// CRITICAL GOTCHA: the mTLS TLS settings are inserted as ONE json5 object at
/// `transport/link/tls` with `enable_mtls: true`. Inserting the sub-keys individually
/// (`transport/link/tls/connect_certificate`, etc.) silently does NOT enable the client-cert
/// send path on Zenoh 1.x — the session opens but the router rejects you or you connect
/// read-only. So we serialize the whole block and `insert_json5` it in a single call.
pub fn config() -> Result<Config> {
    let router = env_var("EFDI_ROUTER")?;
    let ca = env_var("EFDI_CA")?;
    let cert = env_var("EFDI_CERT")?;
    let key = env_var("EFDI_KEY")?;
    let verify_name = env::var("EFDI_VERIFY_NAME")
        .map(|v| v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);

    let mut conf = Config::default();

    conf.insert_json5("mode", r#""client""#)?;
    conf.insert_json5("connect/endpoints", &json!([router]).to_string())?;

    let tls = json!({
        "root_ca_certificate": ca,
        "connect_certificate": cert,
        "connect_private_key": key,
        "enable_mtls": true,
        "verify_name_on_connect": verify_name,
    });
    conf.insert_json5("transport/link/tls", &tls.to_string())?;

    Ok(conf)
}

/// Open and return a Zenoh session (async; the 1.x API is tokio-based).
pub async fn session() -> Result<Session> {
    Ok(zenoh::open(config()?).await?)
}

/// Open a session synchronously via the `Wait` adapter (handy for `fn main` without an async
/// runtime). Most callers should prefer [`session`].
pub fn session_blocking() -> Result<Session> {
    Ok(zenoh::open(config()?).wait()?)
}

/// Your owned prefix, e.g. "release/acme" (trailing slash stripped).
pub fn namespace() -> Result<String> {
    Ok(env_var("PARTNER_NAMESPACE")?.trim_end_matches('/').to_string())
}

/// Build a fully-qualified key under your namespace: `key("sensors/temp")` ->
/// `"release/acme/sensors/temp"`. Pass an absolute key (e.g. `"release/goat/**"`) only
/// if you have rights to it.
pub fn key(suffix: &str) -> Result<String> {
    Ok(format!("{}/{}", namespace()?, suffix.trim_start_matches('/')))
}
