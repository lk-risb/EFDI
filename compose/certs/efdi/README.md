# Local EFDI identity

Required filenames:

- `efdi-ca-root.pem` — public trust root
- `<PARTNER_NAMESPACE>-cert.pem` — this router's certificate
- `<PARTNER_NAMESPACE>-key.pem` — matching private key

Set `PARTNER_NAMESPACE` in `compose/.env`, then run `host/first-boot.sh`.
Alternatively, generate a local development identity with
`scripts/gen-certs.sh <PARTNER_NAMESPACE>`.

Private CA keys and serial files are optional provisioning material and must
also remain untracked.
