# File-drop bridge

Exchange fabric data as **files in a directory**. Your application never makes a network call, never
links a library — it reads and writes files. This is the most universal path: if a system can write
a file, it can publish to the fabric; if it can read a file, it can receive.

Ideal for **MATLAB, PLCs / SCADA, legacy .NET, shell pipelines, and air-gapped / offline edge**
where you can't (or won't) install a Zenoh client or run an HTTP call.

## Model

```
your app  --writes-->  OUTBOX_DIR/sensors/temp   --bridge publishes-->  release/acme/sensors/temp
                                                  (then moves file to OUTBOX_DIR/.sent/)

release/goat/**  --bridge subscribes-->  INBOX_DIR/release__goat__....bin  --your app reads
```

- **Send:** drop (or `cp`/move) a file into `OUTBOX_DIR`. Its path *under the outbox* becomes the
  key suffix: `OUTBOX_DIR/sensors/temp` → published to `<namespace>/sensors/temp`. The file's bytes
  are the payload (any format). After publishing, the file moves to `OUTBOX_DIR/.sent/`.
- **Receive:** every sample matching `SUB_KEYEXPR` is written into `INBOX_DIR` as a file named by
  its key (slashes → `__`) plus a millisecond timestamp. Files appear atomically (written to a
  `.tmp` then renamed), so your poller never reads a half-written file.

## Run

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
export OUTBOX_DIR=./outbox INBOX_DIR=./inbox SUB_KEYEXPR='release/goat/**'
python3 bridge.py
```

Try it:
```sh
echo '{"temp_c":21.5}' > ./outbox/sensors/temp      # mkdir -p ./outbox/sensors first
# -> bridge publishes to release/acme/sensors/temp and moves the file to ./outbox/.sent/
ls ./inbox/                                          # inbound samples land here as files
```

## Notes

- **Poll-based, stdlib only** (no `watchdog`/inotify dep) — works on any OS, offline, in a
  container, on a constrained host. Tune `POLL_SECONDS` (default 1s).
- Write files **atomically** (write to a temp name, then rename into the outbox) so the bridge
  never publishes a partial file. Files whose name starts with `.` are skipped.
- Set `SUB_KEYEXPR` empty to disable the inbound half (publish-only).
- Runs co-located with the pod and holds your mTLS credentials — keep the directories on a
  trusted local filesystem.
