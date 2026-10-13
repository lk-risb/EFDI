#!/usr/bin/env python3
"""ffi_layer.py - TAK users as an FFI (APP-11 Edition D) feed for SitaWare force tracking.

The same feed as nffi_layer.py (TAK users from tak_bridge.py, sent to a SitaWare NFFI and FFI Manager "IP1 Client" that this layer connects
out to, or served on a port), but written as the APP-11(D) FriendlyForceInformation message instead of NFFI. SitaWare's manager has both
payloads (its servers take "Payload Message": NFFI or FFI), and FFI is the newer one: its UnitSymbol comes with a SymbolStandard, so it can
carry the numeric APP-6(D) dismounted individual where the NFFI unitSymbol takes only 15 letters or digits. It is a separate layer so the NFFI
route that works on SitaWare 6.21 is untouched while this one is tried; run one of the two against a client, not both.

The message structure is copied from what a live SitaWare 6.21 IP1 Classic Server writes as FFI (docs/references/nffi/NFFI.md): the
namespace urn:nato:mtf:app-11(d):1:ffi, MessageIdentifier, GeodeticDatum WGE, one TrackInformation per message with TrackSource, the
security label, TrackPositionalData (time, position in degrees and minutes) and TrackIdentificationData (UnitSymbol, SymbolStandard,
UnitShortName), each behind the 16-byte header with the first byte 0x14 (0x04 on NFFI). Whether the IP1 Client accepts it is what this
layer is for finding out; the client form has no payload setting.

Env (at least one of FFI_LAYER_CONNECT and FFI_LAYER_PORT is required; the layer does not start without one):
  FFI_LAYER_CONNECT          host:port of SitaWare's IP1 client to connect out to (comma-separated for several)
  FFI_LAYER_RECONNECT_S      seconds between connection attempts (default 10)
  FFI_LAYER_PORT             TCP port to listen on, if SitaWare dials in instead
  FFI_LAYER_BIND             address to listen on (default 127.0.0.1: set the one address SitaWare reaches, e.g. the VPN address)
  FFI_LAYER_ALLOW            comma-separated source addresses that may connect (default: any that can reach the port)
  FFI_LAYER_SYMBOL_STANDARD  APP-6(B) (default: the 15-character letter code) or APP-6(D) (the 20-digit dismounted individual)
  FFI_LAYER_STALE_S          drop a track this long after its last report (default 600)
  FFI_LAYER_REFRESH_S        re-send every track this often (default 30)
The country, system and subsystem are those of nffi_layer.py (NFFI_COUNTRY, EFDI, TAK).
"""

import os
import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

from layers.vendors.tak.systematic import nffi_layer as nffi
from protocols.vendors.random import ground_unit

SYMBOL_STANDARD = os.environ.get("FFI_LAYER_SYMBOL_STANDARD", "APP-6(B)").strip()


def _ffi_time(when: float) -> str:
    t = datetime.fromtimestamp(float(when), tz=timezone.utc)
    return ("<Year4Digit>{:04d}</Year4Digit><MonthNumeric>{:02d}</MonthNumeric><Day>{:02d}</Day><TimeDelimiter>T</TimeDelimiter>"
            "<HourTime>{:02d}</HourTime><MinuteTime>{:02d}</MinuteTime><SecondTime>{:02d}</SecondTime><TimeZoneZulu>Z</TimeZoneZulu>"
            ).format(t.year, t.month, t.day, t.hour, t.minute, t.second)


def _ffi_location(lat: float, lon: float) -> str:
    def part(value: float, width: int):
        degrees, minutes = divmod(abs(value) * 60.0, 60.0)
        return "{:0{}d}".format(int(degrees), width), "{:06.3f}".format(minutes)
    lat_d, lat_m = part(lat, 2)
    lon_d, lon_m = part(lon, 3)
    return ("<Location><LatitudeDegrees>{}</LatitudeDegrees><LatitudeMinutes04DecimalPlaces>{}</LatitudeMinutes04DecimalPlaces>"
            "<LatitudinalHemisphere>{}</LatitudinalHemisphere><Hyphen>-</Hyphen><LongitudeDegrees>{}</LongitudeDegrees>"
            "<LongitudeMinutes04DecimalPlaces>{}</LongitudeMinutes04DecimalPlaces><LongitudinalHemisphere>{}</LongitudinalHemisphere></Location>"
            ).format(lat_d, lat_m, "N" if lat >= 0 else "S", lon_d, lon_m, "E" if lon >= 0 else "W")


def track_message(track: dict, now: float | None = None) -> bytes | None:
    """One APP-11(D) FriendlyForceInformation message with one track for a TAK client's friendly unit track, in the structure a SitaWare
    6.21 IP1 server writes as FFI; None when it has no usable position or is not a TAK client. With FFI_LAYER_SYMBOL_STANDARD=APP-6(D)
    the unit symbol is the 20-digit dismounted individual instead of the 15-character letter code."""
    try:
        lat, lon = float(track["lat_deg"]), float(track["lon_deg"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 < lon <= 180) or (lat == 0.0 and lon == 0.0):
        return None
    if not (track.get("tak_platform") or track.get("tak_device")):
        return None
    uid = nffi._clean(track.get("uid"), r"A-Za-z0-9.,:*\-", 60)
    if not uid:
        return None
    when = track.get("cot_time_epoch_s") or track.get("_ts") or now or time.time()
    esc = lambda text: xml_escape(str(text))
    label = '<SecurityClassificationExtended>UNCLASSIFIED</SecurityClassificationExtended>'
    numeric = SYMBOL_STANDARD.upper().startswith("APP-6(D")
    symbol = (ground_unit.tak_individual_sidc(track.get("cot_type"), track.get("tak_role")) if numeric else nffi.unit_symbol(track))
    name = nffi._clean(track.get("callsign") or track.get("uid"), r"A-Za-z0-9.,:*\- ", 21).strip()
    body = (
        '<mtf:FriendlyForceInformation xmlns:mtf="urn:nato:mtf:app-11(d):1:ffi" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" mtfid="FFI">'
        "<MessageIdentifier><MessageTextFormatIdentifier>FFI</MessageTextFormatIdentifier><Standard>APP-11(D)</Standard><Version>1</Version>"
        "<Originator>{country}</Originator><ReferenceTimeOfPublication><DateTimeIso>{published}</DateTimeIso></ReferenceTimeOfPublication>"
        "<MessageSecurityPolicy>0</MessageSecurityPolicy>"
        "<MessageClassification>{label}</MessageClassification><MessageSecurityCategory>0</MessageSecurityCategory></MessageIdentifier>"
        "<GeodeticDatum><GeodeticDatum>WGE</GeodeticDatum></GeodeticDatum>"
        "<TrackInformation><TrackSource><TransponderId>{uid}</TransponderId><System>{system}</System><Subsystem>{subsystem}</Subsystem></TrackSource>"
        "<TrackSecurityLabel><TrackSecurityPolicy>0</TrackSecurityPolicy><TrackSecurityClassification>{label}</TrackSecurityClassification>"
        "<TrackSecurityCategory>0</TrackSecurityCategory><BlueDotSecurityClassification>{label}</BlueDotSecurityClassification>"
        "<BlueDotSecurityCategory>0</BlueDotSecurityCategory><DetailsSecurityClassification>{label}</DetailsSecurityClassification>"
        "<DetailsSecurityCategory>0</DetailsSecurityCategory></TrackSecurityLabel>"
        "<TrackPositionalData>{time}{location}</TrackPositionalData>"
        "<TrackIdentificationData><UnitSymbol>{symbol}</UnitSymbol><SymbolStandard>{standard}</SymbolStandard>{short}</TrackIdentificationData>"
        "</TrackInformation></mtf:FriendlyForceInformation>"
    ).format(country=nffi.COUNTRY, published=_ffi_time(time.time()),
             label=label, uid=esc(uid), system=nffi.SYSTEM, subsystem=nffi.SUBSYSTEM, time="<Time>{}</Time>".format(_ffi_time(when)), location=_ffi_location(lat, lon),
             symbol=esc(symbol), standard=esc(SYMBOL_STANDARD), short="<UnitShortName>{}</UnitShortName>".format(esc(name)) if name else "")
    document = b'<?xml version="1.0" encoding="UTF-8"?>\n' + body.encode("utf-8") + b"\n"
    return nffi.ip1_header(0x14, len(document), when) + document


def main() -> None:
    nffi.run("FFI_LAYER", track_message, "FFI", "TAK users -> FFI (APP-11 Edition D) feed for SitaWare force tracking")


if __name__ == "__main__":
    main()
