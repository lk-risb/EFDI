#!/bin/sh
set -e

# /data and /zenoh-config are host bind mounts (see docker-compose.yml),
# owned by whatever user ran host/first-boot.sh — usually root. Chown them to
# the app user here, as root, before dropping to it, so uvicorn can still
# write to them once it's no longer root. /certs is read-only and already
# group-readable by this user's GID (scripts/gen-certs.sh chgrp's it).
for d in /data /zenoh-config; do
  [ -d "$d" ] && chown -R zenohadmin:zenohadmin "$d"
done

exec su -s /bin/sh zenohadmin -c "$*"
