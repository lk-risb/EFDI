#!/bin/bash
cd "$(dirname "$0")" || exit
docker compose up -d --build \
  opensky-bridge aisstream-bridge openmeteo-bridge meteo-lt-bridge \
  yr-no-bridge n2yo-bridge osm-bridge purpleair-bridge \
  notam-bridge fr24-bridge windy-bridge airplaneslive-bridge \
  aprs-bridge here-traffic-bridge \
  atak-cot-udp atak-cot-tcp
