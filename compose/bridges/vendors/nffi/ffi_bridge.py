#!/usr/bin/env python3
"""FFI (APP-11 Edition D) ingress -> Zenoh: nffi_bridge.py with the FFI_* settings.

A separate bridge from the NFFI one so each can dial its own SitaWare server (an IP1 Classic Server writes one payload, NFFI or FFI),
and so each has its own settings and service. The connection, the 16-byte header and the framing are the same code, and a document is
published under raw/nffi or raw/ffi by what the frame says it is; ffi.py decodes what lands on raw/ffi. Settings: FFI_HOST, FFI_PORT,
FFI_TLS, FFI_CERT, FFI_KEY, FFI_CA, FFI_TLS_SERVER_NAME, FFI_SOURCE_ID, FFI_RECONNECT_S (see nffi_bridge.py for what each is, and for
the contract the SitaWare server needs for this host).
"""

from bridges.vendors.nffi import nffi_bridge


def main(argv=None) -> None:
    nffi_bridge.main(argv, prefix="FFI", description="FFI (APP-11 Edition D) -> Zenoh ingress bridge")


if __name__ == "__main__":
    main()
