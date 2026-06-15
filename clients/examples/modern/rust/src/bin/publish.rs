//! publish.rs — send data to the goat fabric (modern Rust).
//!
//! ```text
//! export GOAT_ROUTER=tls/127.0.0.1:7447 GOAT_CERT=... GOAT_KEY=... GOAT_CA=... GOAT_NAMESPACE=release/acme
//! cargo run --bin publish              # one JSON sample
//! cargo run --bin publish -- 50 0.2    # 50 samples at 200ms
//! ```
//!
//! Publishes JSON under `<namespace>/sensors/temp`. Real payloads can be anything (bytes,
//! protobuf, CBOR); JSON here for legibility.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::json;

#[tokio::main]
async fn main() -> goat_connect::Result<()> {
    zenoh::init_log_from_env_or("error");

    let args: Vec<String> = std::env::args().collect();
    let n: usize = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(1);
    let interval: f64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(1.0);

    let key = goat_connect::key("sensors/temp")?;

    let session = goat_connect::session().await?;
    let publisher = session.declare_publisher(key.clone()).await?;

    for i in 0..n {
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        let payload = json!({
            "ts": ts,
            "seq": i,
            "temp_c": 21.5 + (i as f64) * 0.1,
        })
        .to_string();

        publisher.put(payload.clone()).await?;
        println!("published -> {key}: {payload}");

        if i + 1 < n {
            tokio::time::sleep(Duration::from_secs_f64(interval)).await;
        }
    }

    Ok(())
}
