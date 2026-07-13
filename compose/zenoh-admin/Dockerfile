FROM node:20-slim AS ui-builder
WORKDIR /ui
RUN npm install -g pnpm
COPY ui/package.json ./
RUN pnpm install
COPY ui/ .
RUN pnpm build

FROM python:3.11-slim
WORKDIR /app
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
