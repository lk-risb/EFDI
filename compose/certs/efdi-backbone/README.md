# Backbone fabric identity

Required fixed filenames:

- `cert.pem` — Backbone-issued client certificate
- `key.pem` — matching private key
- `ca-roots.pem` — Backbone trust chain

`host/first-boot.sh` validates that the bundle is complete and stages protected
runtime copies. The Zenoh WebUI `Backbone` preset then selects those copies and
the matching Backbone endpoint atomically.
