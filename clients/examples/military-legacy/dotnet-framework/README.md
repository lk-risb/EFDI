# .NET Framework 4.x — via the REST bridge (no NuGet)

Lots of defense software is pinned to the **.NET Framework 4.x** (4.5 / 4.6 / 4.7.2 / 4.8) on
Windows — not modern .NET / .NET Core. There is no usable Zenoh binding for Framework, and you
likely can't NuGet-restore on an air-gapped box anyway. So this example doesn't link Zenoh: it speaks
**plain HTTP to the local REST bridge** using only `System.Net.HttpWebRequest` from the Framework BCL.
**No NuGet packages, no modern `HttpClient` needed, no internet.**

The bridge (running next to the pod on `127.0.0.1`) holds the Zenoh mTLS identity and does the Zenoh
part. Your `.exe` just makes HTTP calls to localhost.

```
GoatBridgeClient.exe  ──HTTP (HttpWebRequest)──▶  REST bridge (127.0.0.1:8080)  ──mTLS──▶  pod
   (BCL only, no NuGet)                              holds your certs               tls/127.0.0.1:7447
```

## Prerequisites

1. **The REST bridge is running** next to the pod (see
   [`../../bridges/rest-http/README.md`](../../bridges/rest-http/README.md)). Its environment carries
   your namespace (`GOAT_NAMESPACE=release/acme`), so a bare suffix like `sensors/temp` becomes
   `release/acme/sensors/temp`.
2. **A .NET Framework 4.x compiler** — either `csc.exe` (ships with every Framework install) or
   MSBuild / Visual Studio Build Tools. Nothing else.

## Build

### Option A — `csc.exe` directly (simplest, fully offline)

`csc.exe` ships in the Framework install directory; no project file, no restore. From a Developer
Command Prompt (or with the full path):

```bat
REM the exact path varies by Framework version; v4.x lives under the Microsoft.NET folder:
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /out:GoatBridgeClient.exe Program.cs
```

`Program.cs` references only `System.dll` (auto-referenced by `csc`), so that one line is the whole
build. Produces `GoatBridgeClient.exe` next to the source.

### Option B — the old-style `.csproj` with MSBuild

`GoatBridgeClient.csproj` is a classic (non-SDK) project that references only the BCL — nothing to
restore:

```bat
msbuild GoatBridgeClient.csproj /p:Configuration=Release
REM -> bin\Release\GoatBridgeClient.exe
```

Edit `<TargetFrameworkVersion>` in the `.csproj` to match whatever 4.x your shop pins (it's set to
`v4.7.2`; `v4.5` / `v4.6.1` / `v4.8` all work the same).

## Run

```bat
REM Publish: POST a body to /pub/<suffix>
GoatBridgeClient.exe pub sensors/temp {"temp_c":21.5}
GoatBridgeClient.exe pub sensors/temp 21.5 10 200          REM 10 samples, 200 ms apart

REM Subscribe: block for N samples (GET /sub/<keyexpr>?count=N&timeout=S)
GoatBridgeClient.exe sub sensors/temp                       REM wait for 1 sample
GoatBridgeClient.exe sub sensors/temp 5 60                  REM wait for 5, up to 60s
GoatBridgeClient.exe sub release/goat/** 3           REM inbound from goat (full key)

REM Stream continuously (GET /stream/<keyexpr>, Server-Sent Events)
GoatBridgeClient.exe stream sensors/temp                    REM Ctrl-C to stop
```

`sub` / `stream` print the raw JSON the bridge returns — each sample is
`{"key":"release/acme/sensors/temp","ts":...,"text":"21.5"}` (or `"b64"` for non-UTF-8 bytes). We
deliberately add **no JSON dependency**; parse with whatever your shop already has, or just log the
lines.

**Quick round-trip:** in one window `GoatBridgeClient.exe stream sensors/temp`, in another
`GoatBridgeClient.exe pub sensors/temp 21.5 5 500`.

> Override the bridge location with the `BRIDGE_URL` environment variable
> (`set BRIDGE_URL=http://127.0.0.1:9000`); it defaults to `http://127.0.0.1:8080`.

## Why `HttpWebRequest` and not `HttpClient`?

`HttpWebRequest` is in **every** Framework version back to 2.0 and needs no package. `HttpClient`
exists on 4.5+ too, but on a locked Framework box `HttpWebRequest` is the lowest-common-denominator
that is guaranteed present — which is exactly the audience here. The streaming path
(`ReadWriteTimeout = Infinite`, `AllowReadStreamBuffering = false`) is also more predictable with
`HttpWebRequest` for an open-ended Server-Sent-Events response.

## OFFLINE / air-gapped notes

- **Your app needs nothing restored.** `Program.cs` uses only the BCL (`System.Net`, `System.IO`,
  `System.Text`). There are **no NuGet packages** — neither Option A nor Option B touches the
  network. The Framework + compiler are already on a Windows box; that's the whole toolchain.
- **What needs vendoring is the bridge's runtime:** Python 3 + the `eclipse-zenoh` 1.9.0 wheel. Build
  a wheelhouse on a connected box and sneakernet it in — see the OFFLINE section in
  [`../README.md`](../README.md).
- **Everything is localhost.** The `.exe` talks only to `http://127.0.0.1:8080`. No DNS, no proxy, no
  internet from the .NET side.
- **Clock skew breaks the bridge, not this code.** Your `.exe`↔bridge hop is plain HTTP and ignores
  the clock. The bridge↔pod hop is **mTLS** and rejects certs outside their validity window. If the
  `.exe` connects but nothing flows — or the bridge never prints `bridge on http://…` — fix the box's
  clock first. On Windows: `w32tm /resync`, or set it manually if truly air-gapped. Full discussion in
  [`../README.md`](../README.md).
