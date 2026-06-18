FROM python:3.12-slim
RUN pip install --no-cache-dir eclipse-zenoh==1.9.0
WORKDIR /app
# Scripts are the source of truth here; certs come from BUNDLE_DIR at /certs (see compose.yml).
# CMEMS bridge has its own Dockerfile.cmems (needs copernicusmarine, ~800 MB extra).
# bridges/ = data ingestion (sensor/API → Zenoh)
# layers/  = protocol converters and output (Zenoh → TAK/NATO/SitaWare)
COPY bridges/*.py layers/*.py ./
CMD ["python3", "/app/opensky_states_bridge.py"]
