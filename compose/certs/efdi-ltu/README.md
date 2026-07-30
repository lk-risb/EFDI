# LTU fabric identity

Required fixed filenames:

- `client.pem` — LTU-issued client certificate, with `serverAuth` as well when
  other routers dial this router
- `client.key` — matching private key; it may be passphrase protected
- `ca.crt` — LTU fabric trust root

For an encrypted participant bundle or a leaf certificate without its issuing
intermediate, connect with:

```bash
EFDI_LTU_PARTNER_NAMESPACE=<ltu-issued-slot-id> ./scripts/connect-ltu.sh
```

The helper verifies the key, certificate chain, and remote endpoint names. It
stages only runtime copies under the ignored `compose/state/` directory.
The slot ID becomes the exact data root (`<slot-id>/**`); do not prepend a
legacy vendor prefix and do not publish to the wildcard itself.

For unattended first boot, also provide `client-chain.pem` containing the leaf
followed by its intermediate, and make `client.key` an unencrypted runtime key.
`host/first-boot.sh` then discovers and stages the profile automatically. Never
put a key passphrase in `.env`.
