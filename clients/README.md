# clients — send & receive data with an EFDI pod

See `docs/INSTALL.md` §7 "Integrations → Client SDKs — connecting to the pod" for the
full guide: the connection model, required env vars, the mTLS one-block
gotcha, and per-language/per-stack build and run instructions for everything
under `connect/` and `examples/` below.

```text
clients/
├── connect/             minimal "cert bundle -> Zenoh session" helper per language
├── examples/
│   ├── modern/          idiomatic pub/sub/request-reply per language
│   ├── military-legacy/ older toolchains, offline/air-gapped, file/HTTP fallbacks
│   └── bridges/         use a protocol you already speak — no Zenoh code in your app
└── README.md            this file
```
