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

# ── ENV (environmental conditions) ───────────────────────────────────────────
register "env/air_quality/v1"       # PurpleAir ground-based PM2.5/PM10 sensors
# current + forecast (Open-Meteo, yr.no, Windy): all operational cities
for place in \
    vilnius kaunas klaipeda riga tallinn kaliningrad \
    helsinki stockholm oslo \
    warsaw kyiv minsk moscow \
    istanbul beirut tel_aviv baghdad tehran; do
  register "env/weather/current/v1/$place"
  register "env/weather/forecast/v1/$place"
done
# Lithuania-only forecast (meteo.lt)
for place in siauliai panevezys; do
  register "env/weather/forecast/v1/$place"
done

# ── APRS catch-all (unclassified symbols) ────────────────────────────────────
register "aprs/tracks/v1"
