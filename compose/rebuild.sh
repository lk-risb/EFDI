#!/bin/bash
cd "$(dirname "$0")" || exit
docker compose up -d --build \
  zenoh-router zenoh-admin-db docker-socket-proxy zenoh-admin zenoh-admin-proxy
