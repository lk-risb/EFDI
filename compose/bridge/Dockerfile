FROM python:3.12-slim
RUN pip install --no-cache-dir eclipse-zenoh==1.9.0
WORKDIR /app
# Scripts are the source of truth here; certs come from BUNDLE_DIR at /certs (see compose.yml).
# CMEMS bridge has its own Dockerfile.cmems (needs copernicusmarine, ~800 MB extra).
COPY opensky_states_bridge.py aisstream_ws_bridge.py aprsis_bridge.py \
     openmeteo_forecast_bridge.py meteolt_forecast_bridge.py yrno_forecast_bridge.py \
     n2yo_satpos_bridge.py osm_overpass_bridge.py purpleair_sensor_bridge.py \
     icao_notam_bridge.py fr24_live_bridge.py windy_forecast_bridge.py \
     airplaneslive_adsb_bridge.py here_traffic_bridge.py cmems_marine_bridge.py \
     nato_nvg_layer.py \
     cot_layer.py cat62_layer.py sapient_layer.py nato_nffi_layer.py ./
CMD ["python3", "/app/opensky_states_bridge.py"]
