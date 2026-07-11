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
EXPOSE 8895
# Loopback-only — not directly reachable. The zenoh-admin-proxy (Caddy) container
# shares this container's network namespace (network_mode: host) and terminates
# real TLS on 8890, the port operators actually visit.
CMD ["uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8895"]
