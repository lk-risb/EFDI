FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
WORKDIR /app
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Scripts are the source of truth here; certs come from BUNDLE_DIR at /certs (see compose.yml).
# bridges/ = data ingestion (sensor/API → Zenoh)
# protocols/ = one reusable protocol translator per wire/API contract
# layers/    = TAK and SitaWare connection/output layers
COPY namespace_prefix.py http_json.py bridges/*.py protocols/*.py layers/*.py ./
# No writable mounts (certs come in read-only) — drop root, nothing needs it.
# Fixed UID:GID 10001:10001 — scripts/gen-certs.sh chgrp's each leaf private
# key to GID 10001 so this user can read it despite the /certs mount being :ro.
RUN groupadd -g 10001 bridge && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin bridge
USER bridge
CMD ["python3", "/app/airplaneslive_adsb_bridge.py"]
