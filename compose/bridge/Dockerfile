FROM python:3.12-slim
RUN pip install --no-cache-dir eclipse-zenoh==1.9.0 defusedxml==0.7.1
WORKDIR /app
# Scripts are the source of truth here; certs come from BUNDLE_DIR at /certs (see compose.yml).
# bridges/ = data ingestion (sensor/API → Zenoh)
# layers/  = protocol converters and output (Zenoh → TAK/NATO/SitaWare)
COPY namespace_prefix.py bridges/*.py layers/*.py ./
# No writable mounts (certs come in read-only) — drop root, nothing needs it.
# Fixed UID:GID 10001:10001 — scripts/gen-certs.sh chgrp's each leaf private
# key to GID 10001 so this user can read it despite the /certs mount being :ro.
RUN groupadd -g 10001 bridge && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin bridge
USER bridge
CMD ["python3", "/app/airplaneslive_adsb_bridge.py"]
