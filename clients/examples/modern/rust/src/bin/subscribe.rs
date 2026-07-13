//! subscribe.rs — receive data from the EFDI fabric (modern Rust).
//!
//! ```text
//! cargo run --bin subscribe                              # your own namespace, follow forever
//! cargo run --bin subscribe -- 'release/<partner>/**'  # inbound data a partner sends you
//! cargo run --bin subscribe -- '<keyexpr>' 5             # exit after 5 samples
//! ```
//!
//! Default key-expr is `<namespace>/**` (everything under your prefix). Use `**` for any
//! depth, `*` for a single segment.

#[tokio::main]
async fn main() -> efdi_connect::Result<()> {
    zenoh::init_log_from_env_or("error");

    let args: Vec<String> = std::env::args().collect();
    let keyexpr = match args.get(1) {
        Some(k) => k.clone(),
        None => format!("{}/**", efdi_connect::namespace()?),
    };
    let limit: usize = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(0); // 0 = forever

    let session = efdi_connect::session().await?;
    let subscriber = session.declare_subscriber(keyexpr.clone()).await?;
    println!("subscribed: {keyexpr} (Ctrl-C to stop)");

    let mut seen: usize = 0;
    loop {
        tokio::select! {
            recv = subscriber.recv_async() => {
                let sample = match recv {
                    Ok(s) => s,
                    Err(_) => break, // subscriber closed
                };
                let raw = sample.payload().to_bytes();
                let shown = match sample.payload().try_to_string() {
                    Ok(s) => s.into_owned(),
                    Err(_) => {
                        let head: Vec<u8> = raw.iter().take(32).copied().collect();
                        format!("<{} bytes> {}", raw.len(), hex(&head))
                    }
                };
                println!(
                    "{}  {}  {}",
                    chrono::Local::now().format("%H:%M:%S"),
                    sample.key_expr().as_str(),
                    shown
                );
                seen += 1;
                if limit != 0 && seen >= limit {
                    break;
                }
            }
            _ = tokio::signal::ctrl_c() => break,
        }
    }

    Ok(())
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
