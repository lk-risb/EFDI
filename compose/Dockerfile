FROM python:3.13.14-slim@sha256:afe189875f1d2f9b45e287834fb9f2c273a5d59d354ae4050ab9affbf0a6ba06
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
# Shared helpers every bridge/layer imports by bare name.
COPY namespace_prefix.py http_json.py zenoh_auth.py translation_common.py ./
# bridges/ and layers/ are flattened onto /app so CMD can name a script directly.
COPY bridges/*.py layers/*.py ./
# protocols/ keeps its package layout — modules import protocols.<group>.<name>.
COPY protocols ./protocols
# Generate the protobuf bindings exactly as scripts/generate-protobuf.sh does on
# the host: -I . so `import "protocols/..."` resolves, output into generated/,
# which PYTHONPATH points at. Without this every `protocols.*_pb2` import — and
# therefore every publish_dual call — fails at runtime.
RUN mkdir -p generated \
    && python3 -m grpc_tools.protoc -I . --python_out=generated \
       $(find protocols -type f -name '*.proto' | sort)
ENV PYTHONPATH=/app:/app/generated
# No writable mounts (certs come in read-only) — drop root, nothing needs it.
# Fixed UID:GID 10001:10001 — scripts/gen-certs.sh chgrp's each leaf private
# key to GID 10001 so this user can read it despite the /certs mount being :ro.
RUN groupadd -g 10001 bridge && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin bridge
USER bridge
CMD ["python3", "--version"]
