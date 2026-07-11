# MATLAB — via the REST bridge and the file-drop bridge

MATLAB can't link a native Zenoh client, but it doesn't need to. It already speaks two protocols a
bridge understands: **HTTP** (`webwrite`/`webread`, built in — no toolbox) and **files**
(`fopen`/`fread`/`dir`). Both go through a bridge running next to the pod on `127.0.0.1`; the bridge
holds the Zenoh mTLS identity. **No toolboxes, no compiled MEX, no internet** from MATLAB's side.

Two paths, pick by what your box allows:

| File | Path | Uses | When |
|---|---|---|---|
| `publish.m` | REST bridge | `webwrite` → `POST /pub` | publish from MATLAB |
| `receive_rest.m` | REST bridge | `webread` → `GET /sub` | receive when HTTP to localhost is allowed |
| `receive_filedrop.m` | file-drop bridge | poll a directory | receive when you can **only** read/write files (most locked-down) |

```
publish.m / receive_rest.m  ──HTTP──▶  REST bridge (127.0.0.1:8080)  ──mTLS──▶  pod
receive_filedrop.m  ◀──reads files──  file-drop bridge (INBOX_DIR)  ──mTLS──▶  pod
```

## Prerequisites

- **A bridge is running** next to the pod, with your namespace in its environment
  (`PARTNER_NAMESPACE=release/acme`):
  - For `publish.m` / `receive_rest.m`: the **REST bridge** —
    [`../../bridges/rest-http/README.md`](../../bridges/rest-http/README.md).
  - For `receive_filedrop.m`: the **file-drop bridge** —
    [`../../bridges/file-drop/README.md`](../../bridges/file-drop/README.md). Note its `INBOX_DIR`;
    you pass the same directory to `receive_filedrop`.
- **MATLAB** (any reasonably recent release; `webwrite`/`webread` have shipped in base MATLAB since
  R2014b). No add-on toolbox required.

Add this folder to your path or `cd` into it, then call the functions.

## Publish (REST) — `publish.m`

`webwrite` does a `POST`; setting `MediaType` to `application/octet-stream` sends your value as the
raw request body, which the bridge publishes verbatim under your namespace.

```matlab
publish('sensors/temp', '{"temp_c":21.5}')                 % -> release/acme/sensors/temp
publish('sensors/temp', '21.5')
publish('sensors/temp', '21.5', 'Count', 10, 'IntervalSec', 0.2)   % 10 samples, 200 ms apart
```

## Receive (REST) — `receive_rest.m`

`webread` does a `GET /sub/<keyexpr>?count=N&timeout=S`. The bridge **blocks** until N samples arrive
(or the timeout), then returns a JSON array that MATLAB auto-decodes into a struct array:

```matlab
s = receive_rest('sensors/temp');                          % wait for 1 sample
s = receive_rest('sensors/temp', 'Count', 5, 'TimeoutSec', 60);
s = receive_rest('release/goat/**', 'Count', 3);    % inbound from goat (full key)
disp(s(1).key)      % 'release/acme/sensors/temp'
disp(s(1).text)     % '21.5'   (or s(1).b64 for non-UTF-8 bytes)
```

> The function sets the HTTP read timeout to `TimeoutSec + 10` so `webread` doesn't give up before
> the bridge's blocking window closes.

## Receive (files) — `receive_filedrop.m`

The most locked-down path: **no network call at all.** The file-drop bridge writes each inbound
sample into `INBOX_DIR` as a `.bin` file named by its key (`release__goat__sensors__temp.<ms>.bin`).
Files appear atomically, so any file you can `dir` is complete. `receive_filedrop` polls the folder,
reverses the name back to a key, and reads the bytes:

```matlab
receive_filedrop('./inbox')                                % poll forever (Ctrl-C to stop)
receive_filedrop('./inbox', 'MaxSamples', 5)               % stop after 5
receive_filedrop('./inbox', 'PollSec', 0.5, 'Callback', @(key,bytes) disp(key))
```

To **publish** the file-drop way, write a file into the bridge's `OUTBOX_DIR` — the path under the
outbox becomes the key. Write to a temp name then `movefile` it in so the bridge never reads a partial
file:

```matlab
outbox = './outbox';
tmp = fullfile(outbox, '.tmp_temp');
fid = fopen(tmp, 'w'); fwrite(fid, unicode2native('21.5','UTF-8')); fclose(fid);
movefile(tmp, fullfile(outbox, 'sensors', 'temp'));        % -> release/acme/sensors/temp
```

(mkdir `fullfile(outbox,'sensors')` first if it doesn't exist.)

## OFFLINE / air-gapped notes

- **MATLAB needs nothing vendored.** `webwrite`/`webread`/`fopen`/`dir` are all base MATLAB. No
  toolbox, no MEX, no download. That's the payoff of going through a bridge.
- **What needs vendoring is the bridge's runtime:** Python 3 + the `eclipse-zenoh` 1.9.0 wheel — build
  a wheelhouse on a connected box and carry it in. See the OFFLINE section in
  [`../README.md`](../README.md).
- **Everything is localhost.** REST calls go to `http://127.0.0.1:8080`; file-drop reads/writes a
  local directory. No DNS, no proxy, no internet from MATLAB. Override the REST base with the
  `BRIDGE_URL` env var or the `'BridgeUrl'` option if you moved the port.
- **Clock skew breaks the bridge, not MATLAB.** MATLAB↔bridge (HTTP, or local files) ignores the
  clock. The bridge↔pod hop is **mTLS** and rejects certs outside their validity window. If `publish`
  succeeds but nothing is received — or the bridge never prints `bridge on http://…` / its inbound
  line — fix the box's clock first (`!date` from MATLAB to check; set it at the OS level). Full
  discussion in [`../README.md`](../README.md).
- **Air-gapped + file-only?** Use `receive_filedrop.m` and the outbox-write snippet above: MATLAB
  never opens a socket, so even a MATLAB install with networking disabled can exchange fabric data.
