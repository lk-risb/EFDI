#!/bin/bash
ORG="1851281db70ccc0409dad4ecfc874cf5"
KEY="vp_1851281db70ccc0409dad4ecfc874cf5_c265b09f3c7319ba4921a408f20f501773bb56ae166955661eb0772cfc2ff385"
BASE="https://portal.efdi.netbird.efdi-backbone.net/api/vendors/ltu-cis-gabrielius/topics"

register() {
  result=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d "{\"key_pattern\":\"$ORG/$1\",\"format\":\"json\",\"description\":\"$1\"}" \
    "$BASE")
  echo "$result $1"
}

# ── AIR ──────────────────────────────────────────────────────────────────────
register "air/civ/tracks/v1"        # OpenSky + FR24 + airplanes.live + APRS aircraft
register "air/mil/tracks/v1"        # airplanes.live military
register "air/quality/v1"           # PurpleAir air quality sensors
register "air/notam/v1"             # ICAO NOTAMs worldwide (single topic, location in payload)

# ── LAND ─────────────────────────────────────────────────────────────────────
register "land/civ/tracks/v1"       # APRS land vehicles
register "land/traffic/v1"          # HERE Traffic Flow road segments
register "land/geo/v1"              # OSM features (aerodromes, ports, military, stations)

# ── SEA ──────────────────────────────────────────────────────────────────────
register "sea/civ/tracks/v1"        # AIS vessel positions + APRS vessels
register "sea/surface/v1"           # CMEMS ocean surface data

# ── SPACE ────────────────────────────────────────────────────────────────────
register "space/tracks/v1"          # N2YO satellite positions

# ── WEATHER (cross-domain) ───────────────────────────────────────────────────
for place in vilnius kaunas klaipeda riga tallinn kaliningrad; do
  register "weather/$place/current/v1"    # Open-Meteo current conditions
  register "weather/$place/forecast/v1"   # meteo.lt + yr.no + Windy forecasts
done
for place in siauliai panevezys; do
  register "weather/$place/forecast/v1"
done

# ── APRS catch-all (unclassified symbols) ────────────────────────────────────
register "aprs/tracks/v1"
