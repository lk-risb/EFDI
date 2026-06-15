# REST/HTTP bridge — optional compose sidecar for the pod.
# Build from the REPO ROOT:  docker build -f clients/examples/bridges/rest-http/Dockerfile -t moon-pod-rest-bridge .
# Run with network_mode: host + the GOAT_* env + the cert dir mounted read-only.
FROM python:3.12-slim
RUN pip install --no-cache-dir eclipse-zenoh
WORKDIR /app
# bridge.py finds goat_connect.py next to itself (see its import fallback) — copy both flat.
COPY clients/connect/python/goat_connect.py /app/goat_connect.py
COPY clients/examples/bridges/rest-http/bridge.py /app/bridge.py
ENV BRIDGE_BIND=0.0.0.0 BRIDGE_PORT=8080
EXPOSE 8080
CMD ["python3", "/app/bridge.py"]
