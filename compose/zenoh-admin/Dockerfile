FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS ui-builder
WORKDIR /ui
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm
COPY ui/package.json ./
RUN pnpm install
COPY ui/ .
RUN pnpm build

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS runtime
WORKDIR /app
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/ ./api/
COPY --from=ui-builder /ui/dist ./static
# Fixed UID:GID 10001:10001 — scripts/gen-certs.sh chgrp's each pod's leaf
# private key to GID 10001 so this user can read it despite /certs being :ro.
RUN groupadd -g 10001 zenohadmin && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin zenohadmin
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
EXPOSE 8895
# Loopback-only — not directly reachable. The zenoh-admin-proxy (Caddy) container
# shares this container's network namespace (network_mode: host) and terminates
# real TLS on 8890, the port operators actually visit.
# Entrypoint starts as root only long enough to chown the writable bind mounts
# (/data, /zenoh-config), then drops to the non-root zenohadmin user — see
# docker-entrypoint.sh.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8895"]

FROM runtime AS postgres-migration
COPY requirements-migration.txt .
RUN pip install --no-cache-dir -r requirements-migration.txt

# Keep a plain `docker build compose/zenoh-admin` on the MariaDB-only runtime.
FROM runtime AS production
