#!/bin/sh
set -e

# /data, /zenoh-config, and /zenoh-config-backbone are host bind mounts (see
# docker-compose.yml), owned by whatever user ran host/first-boot.sh —
# usually root, or plain root:root when Docker auto-creates a source dir
# that's never been staged by any setup script (confirmed live:
# /zenoh-config-backbone, being new, had no pre-creation step at all, so
# api/backbone_bootstrap.py's own write into it failed with a bare
# "Permission denied" once this process was no longer root). Chown them to
# the app user here, as root, before dropping to it, so uvicorn can still
# write to them once it's no longer root. /certs is read-only and already
# group-readable by this user's GID (scripts/gen-certs.sh chgrp's it).
for d in /data /zenoh-config /zenoh-config-backbone; do
  [ -d "$d" ] && chown -R zenohadmin:zenohadmin "$d"
done
[ -f /namespace-prefix ] && chown zenohadmin:zenohadmin /namespace-prefix

# Preserve the original argv exactly. Flattening it into `su -c "$*"` lets
# shell metacharacters and argument boundaries be reinterpreted, and can turn a
# harmless diagnostic such as `sh -c 'set -e; ...'` into an environment dump.
exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
