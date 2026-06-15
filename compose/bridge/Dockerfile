FROM python:3.12-slim
RUN pip install --no-cache-dir eclipse-zenoh==1.9.0
WORKDIR /app
# Scripts are the source of truth here; certs come from BUNDLE_DIR at /certs (see compose.yml).
# CMEMS bridge has its own Dockerfile.cmems (needs copernicusmarine, ~800 MB extra).
COPY opensky_bridge.py ais_bridge.py aprs_bridge.py cat62_bridge.py \
     openmeteo_bridge.py meteo_lt_bridge.py yr_no_bridge.py \
     n2yo_bridge.py osm_bridge.py purpleair_bridge.py \
     notam_bridge.py fr24_bridge.py windy_bridge.py \
     airplaneslive_bridge.py ./
CMD ["python3", "/app/opensky_bridge.py"]
