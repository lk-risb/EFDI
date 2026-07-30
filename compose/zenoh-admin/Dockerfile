FROM node:24.18.0-slim@sha256:d45d78e7929b46875bbd4e29bea672d5bc48186c6c3588306521c815e78352d6 AS ui-builder
WORKDIR /ui
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@11.9.0
COPY ui/package.json ui/pnpm-lock.yaml ui/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/ .
RUN pnpm build

FROM python:3.13.14-slim@sha256:afe189875f1d2f9b45e287834fb9f2c273a5d59d354ae4050ab9affbf0a6ba06 AS runtime
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.revision="${GIT_COMMIT}"
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
