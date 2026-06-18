#!/bin/bash
ORG="1851281db70ccc0409dad4ecfc874cf5"
KEY="vp_1851281db70ccc0409dad4ecfc874cf5_c265b09f3c7319ba4921a408f20f501773bb56ae166955661eb0772cfc2ff385"
BASE="https://portal.efdi.netbird.efdi-backbone.net/api/vendors/ltu-cis-gabrielius/topics"

register() {
  result=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d "{\"key_pattern\":\"$ORG/$1\",\"format\":\"json\",\"description\":\"$2\"}" \
    "$BASE")
  echo "$result $1"
}

# ── AIR — open-source/API bridges ────────────────────────────────────────────
register "air/opensky/adsb/civ/aircraft/tracks/v1"           "OpenSky ADS-B civilian aircraft"
register "air/airplaneslive/adsb/civ/aircraft/tracks/v1"     "airplanes.live civilian aircraft"
register "air/airplaneslive/adsb/mil/aircraft/tracks/v1"     "airplanes.live military aircraft"
register "air/fr24/adsb/civ/aircraft/tracks/v1"              "FlightRadar24 civilian aircraft"
register "air/aprs-is/aprs/civ/aircraft/tracks/v1"           "APRS-IS aircraft"
register "air/mavlink/uav/civ/aircraft/tracks/v1"            "MAVLink UAV positions"
register "air/stanag4586/uav/civ/aircraft/tracks/v1"         "STANAG 4586 UAS tracks"
register "air/icao/rest/neutral/notam/notams/v1"             "ICAO NOTAMs"

# ── AIR — ASTERIX (direct sensor feeds) ──────────────────────────────────────
register "air/asterix/cat20/civ/aircraft/tracks/v1"          "ASTERIX CAT-20 MLAT targets"
register "air/asterix/cat21/civ/aircraft/tracks/v1"          "ASTERIX CAT-21 ADS-B (ground station)"
register "air/asterix/cat48/unknown/aircraft/tracks/v1"      "ASTERIX CAT-48 primary radar plots"
register "air/asterix/cat62/unknown/aircraft/tracks/v1"      "ASTERIX CAT-62 system tracks"

# ── AIR — track fusion (correlated multi-source) ─────────────────────────────
# topic: air/fused/<sensor-id>/aircraft/tracks/v1  (sensor-id set by track_fusion_layer)
register "air/fused/+/aircraft/tracks/v1"                    "Multi-source fused air tracks"

# ── AIR — CoT inbound re-published (cot_receiver_bridge) ─────────────────────
register "air/radar/cot/unknown/aircraft/tracks/v1"          "CoT inbound — unknown affiliation"
register "air/radar/cot/friendly/aircraft/tracks/v1"         "CoT inbound — friendly"
register "air/radar/cot/hostile/aircraft/tracks/v1"          "CoT inbound — hostile"

# ── AIR — Link-16 JREAP-C ────────────────────────────────────────────────────
register "air/link16/jreap/friendly/aircraft/tracks/v1"      "Link-16 friendly air"
register "air/link16/jreap/hostile/aircraft/tracks/v1"       "Link-16 hostile air"
register "air/link16/jreap/neutral/aircraft/tracks/v1"       "Link-16 neutral air"
register "air/link16/jreap/unknown/tracks/v1"                "Link-16 unknown air"

# ── AIR — SitaWare + VMF friendly forces ─────────────────────────────────────
register "air/sitaware/rest/friendly/aircraft/tracks/v1"     "SitaWare friendly aircraft"
register "air/vmf/+/+/tracks/v1"                             "VMF air tracks (any affiliation)"

# ── LAND — tracks ─────────────────────────────────────────────────────────────
register "land/aprs-is/aprs/civ/vehicle/tracks/v1"           "APRS-IS land vehicles"
register "land/aprs-is/aprs/neutral/station/tracks/v1"       "APRS-IS fixed stations / wx"
register "land/here/rest/civ/vehicle/tracks/v1"              "HERE Traffic Flow road segments"
register "land/sitaware/rest/friendly/unit/tracks/v1"        "SitaWare friendly ground units"
register "land/sitaware/rest/unknown/unit/tracks/v1"         "SitaWare unknown ground units"
register "land/link16/jreap/friendly/unit/tracks/v1"         "Link-16 friendly ground"
register "land/link16/jreap/hostile/unit/tracks/v1"          "Link-16 hostile ground"
register "land/link16/jreap/neutral/unit/tracks/v1"          "Link-16 neutral ground"
register "land/link16/jreap/unknown/unit/tracks/v1"          "Link-16 unknown ground"
register "land/vmf/+/+/tracks/v1"                            "VMF land tracks (any affiliation)"

# ── LAND — geo / sensor ───────────────────────────────────────────────────────
register "land/osm/overpass/neutral/geo/features/v1"         "OSM Overpass features (aerodromes, ports, military…)"
register "land/asterix/cat34/neutral/radar/status/v1"        "ASTERIX CAT-34 radar site status + position"

# ── SEA ───────────────────────────────────────────────────────────────────────
register "sea/aisstream/ais/civ/vessel/tracks/v1"            "AIS vessel positions (AISStream)"
register "sea/aprs-is/aprs/civ/vessel/tracks/v1"             "APRS-IS vessels"
register "sea/sitaware/rest/friendly/vessel/tracks/v1"       "SitaWare friendly vessels"
register "sea/link16/jreap/friendly/vessel/tracks/v1"        "Link-16 friendly sea"
register "sea/link16/jreap/hostile/vessel/tracks/v1"         "Link-16 hostile sea"
register "sea/link16/jreap/neutral/vessel/tracks/v1"         "Link-16 neutral sea"
register "sea/link16/jreap/unknown/vessel/tracks/v1"         "Link-16 unknown sea"
register "sea/surface/+/v1"                                  "CMEMS ocean surface data (per dataset)"

# ── SPACE ─────────────────────────────────────────────────────────────────────
register "space/n2yo/satpos/civ/satellite/tracks/v1"         "N2YO satellite positions"

# ── ENV — air quality ─────────────────────────────────────────────────────────
register "env/air_quality/station/purpleair/sensors"         "PurpleAir PM2.5/PM10 ground sensors"

# ── ENV — weather (OpenMeteo current, yr.no + meteo.lt forecast) ──────────────
for place in \
    vilnius kaunas klaipeda siauliai panevezys \
    riga tallinn kaliningrad \
    helsinki stockholm oslo \
    warsaw kyiv minsk moscow \
    istanbul beirut tel_aviv baghdad tehran; do
  register "env/weather/station/openmeteo/current/$place"    "OpenMeteo current weather — $place"
  register "env/weather/station/yrno/forecast/$place"        "yr.no forecast — $place"
done
# meteo.lt covers only Lithuanian stations
for place in vilnius kaunas klaipeda siauliai panevezys; do
  register "env/weather/station/meteolt/forecast/$place"     "meteo.lt forecast — $place"
done
