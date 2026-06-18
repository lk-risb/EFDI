# ASTERIX Full Decode — Design Spec
**Date:** 2026-06-18  
**Scope:** Decode every remaining skipped/stub field in all five ASTERIX categories
and display all new fields in cot_layer.py AIR/SEA/LAND stat card remarks.

---

## Goal

Every `pos += N` / `break  # compound` stub in `asterix_bridge.py` becomes a real decoder.
Every new JSON field flows through to the cot_layer.py remarks panel in the right section.
No changes to topics, transports, CoT types, or Zenoh session structure.

---

## 1. CAT-048 (`decode_cat048_record`)

### FRN 11 — I048/042 Cartesian Position (4 bytes, 2 × s16, 1/128 NM per LSB)
Convert to backup WGS-84 lat/lon via radar origin using `_polar_to_wgs84` equivalent (x/y offset).
Only store if `lat_deg` not already set from I048/040.
Fields: `cart_x_nm`, `cart_y_nm`

### FRN 14 — I048/210 Track Quality (4 unsigned bytes)
- Byte 0: σX (1/128 NM) → `track_sigma_x_nm`
- Byte 1: σY (1/128 NM) → `track_sigma_y_nm`
- Byte 2: σH (25 ft) → `track_sigma_h_ft`
- Byte 3: σV (1/128 NM per sec, speed accuracy per component) → `track_sigma_v_kt`

### FRN 15 — I048/030 Warning/Error Conditions (FX variable)
Bit-per-byte FX chain. First byte bits 8..2:
- Bit 8: `spi` (Special Position Identification — pilot ident pulse)
- Bit 7: `pai` (Potential Angle Inaccuracy)
- Bit 6: `stc` (Suspicious Target Code)
- Bit 5: `apw` (Area Proximity Warning)
- Bit 4: reserved
- Bit 3: `msaw` (Minimum Safe Altitude Warning)
- Bit 2: `cst` (Coasted track)
Store each as bool; skip further FX bytes (save raw hex as `we_conditions_hex`).

### FRN 16 — I048/080 Mode-3/A Code Confidence Indicator (2 bytes)
Word bit layout (u16):
- Bit 15: QXI (0=transponder-derived, 1=not from transponder) → `squawk_not_transponder`
- Bit 14: G (garbled) → `squawk_garbled`
- Bit 13: L (0=transponder, 1=smoothed) → `squawk_smoothed`
- Bits 11..0: validity bits for each code digit (skip, too granular)

### FRN 17 — I048/100 Mode-C Code and Confidence (4 bytes)
- Bytes 0-1 (u16, lower 12 bits): Mode-C Gillham code → decode to `mode_c_alt_ft`
  (Gillham decode: D1=0, A1-C4 bits → standard 100-ft FL encoding)
- Bytes 2-3: confidence indicator bits (skip)

### FRN 19 — I048/120 Radial Doppler Speed (compound)
Primary sub-field (byte 0 bit 8 = CAL present):
- CAL sub-field: 2 bytes, s16, LSB = 1/256 NM/s → `doppler_kt` (multiply by 3600/1.852... no: × 3600 to get NM/hr = kt)
  Actually: NM/s × 3600 = NM/hr = kt. So `doppler_kt = _s16(b) / 256.0 * 3600`
RDS sub-field (bit 7): REP × 6 bytes — store first measurement's speed as `doppler_raw_kt`.

### FRN 20 — I048/230 Communications/ACAS Capability (2 bytes)
Byte 0:
- Bits 8-6 (3 bits): COM capability → `com_capability` (0=none,1=ELM,2=SDS+ELM,3=ELM+MODE-S, etc.)
- Bits 5-3 (3 bits): flight status → cross-check on_ground
- Bit 2: SI code family → `si_code`
Byte 1:
- Bit 8: MSSC (Mode-S specific service capability) → `mssc`
- Bit 7: ARC (25 ft alt resolution) → `altitude_25ft`
- Bit 6: AIC (aircraft ID capability) → `aic`
- Bits 5-1: BDS 1,0 bits

### FRN 21 — I048/260 ACAS Resolution Advisory (7 bytes = BDS 3,0)
56-bit register, bit 1 = MSB:
- Bits 1-4: reserved (should be 0000)
- Bit 5: ARA bit 1 — corrective RA active → `acas_ra_corrective`
- Bit 6: ARA bit 2 — downward sense → `acas_ra_downward` (if True: descend; False: climb)
- Bits 7-10: ARA bits 3-6 — increase VR, sense reversal, alt crossing, positive RA
- Bits 11-14: RAC bits (don't cross, etc.) → `acas_rac`
- Bit 15: RAT (RA terminated) → `acas_ra_terminated`
- Bit 16: MTE (multiple threat encounter) → `acas_multi_threat`
Store: `acas_ra_active` (any ARA bit set), `acas_ra_sense` ("CLIMB"/"DESCEND"), raw `acas_ra_hex`.

### FRN 23 — I048/050 Mode-2 Code (2 bytes, lower 12 bits = octal code)
Fields: `mode2` (4-char octal string, same format as `squawk`)

---

## 2. CAT-021 (`decode_cat021_record`)

### FRN 6 — I021/090 Figure of Merit (2 bytes)
- Bits 15-13: NIC supplement → `nic_baro`
- Bits 12-9: NAC position → `nac_p`
- Bits 8-5: NIC → `nic`
- Bits 4-1: NACv → `nac_v`

### FRN 7 — I021/210 Link Technology (FX variable)
Already decoded as skip. Now decode:
- Bit 8: VDL mode 4
- Bit 7: UAT
- Bit 6: 1090 MHz ES
Store: `link_tech` (list, e.g. `["1090ES", "UAT"]`)

### FRN 8 — I021/230 Roll Angle (2 bytes, s16, 45/512 deg per LSB)
Field: `roll_deg`

### FRN 10 — I021/150 Air Speed (2 bytes)
- Bit 16 (IM flag): 0=IAS, 1=Mach
- Bits 15-1 (15-bit value): value × 2^-14
  If IAS: kt = value × (3600/16384)... actually NM/s × 3600 = kt
  If Mach: mach = value × (2^-14)... need to check scale
  → `ias_kt` or `mach`

### FRN 11 — I021/151 True Airspeed (2 bytes)
- Bit 16: RE (range exceeded)
- Bits 15-1 (15-bit unsigned): TAS in kt (LSB = 1 kt, no... LSB = 2^-14 NM/s? or direct kt?)
  Per Ed 2.4 spec: TAS in kt with LSB = 1 kt (unsigned 15-bit direct kt value)
  → `tas_kt`

### FRN 12 — I021/152 Magnetic Heading (2 bytes, u16, 360/65536 deg per LSB)
Field: `mag_hdg_deg`

### FRN 13 — I021/155 Barometric Vertical Rate (2 bytes)
- Bit 16: RE (range exceeded)
- Bits 15-1: signed 15-bit, LSB = 6.25 ft/min
Field: `baro_vr_fpm`

### FRN 14 — I021/157 Geometric Vertical Rate (2 bytes, same encoding)
Field: `geo_vr_fpm`

### FRN 16 — I021/165 Track Angle Rate (2 bytes, s16, 360/65536 deg/s per LSB)
Field: `track_angle_rate_degs`

### FRN 18 — I021/095 Velocity Accuracy (1 byte)
- Bits 8-5: NACv → `nac_v` (if not already from FRN 6)

### FRN 20 — I021/200 Target Status (FX variable)
- Bit 8: ES (intent change flag)
- Bit 7: TCAS ACAS health
Store: `intent_change`, `tcas_operational`

### FRN 21 — I021/020 Emitter Category (1 byte)
Lookup table (14=light aircraft, 15=medium, 16=heavy, etc.) → `emitter_category` (raw int + string label)

### FRN 22 — I021/220 Met Information (compound)
Was `break`. Now decode primary sub-field bitmap then:
- Wind speed (2 bytes, u16, 0.5 kt/LSB) → `wind_speed_kt`
- Wind direction (2 bytes, u16, 360/65536 deg) → `wind_dir_deg`
- Temperature (2 bytes, s16, 0.25 °C) → `temp_c`
- Turbulence (1 byte, 0=nil, 1=light, 2=moderate, 3=severe) → `turbulence`

### FRN 23 — I021/146 Selected Altitude (2 bytes)
- Bits 16-15: source (MCP/FCU vs FMS)
- Bits 14-1: s13, LSB = 25 ft → `selected_alt_ft`, `selected_alt_source`

### FRN 24 — I021/148 Final State Selected Altitude (2 bytes, same encoding)
Field: `final_alt_ft`

### FRN 25 — I021/110 Trajectory Intent (compound)
Was `break`. Now decode TID sub-field:
- NAV accuracy code, turn radius, over point → `trajectory_nav_accuracy`
  (Skip full route profile — too verbose)

---

## 3. CAT-020 (`decode_cat020_record`)

### FRN 7 — I020/100 Mode-C + Confidence (4 bytes: 2+2)
Bytes 0-1: Mode-C code (Gillham) → `mode_c_alt_ft`
Bytes 2-3: confidence bits (skip)
Was `break` — now decode then continue.

### FRN 12 — I020/210 Track Quality (compound)
Same structure as I048/210 but compound variant:
Primary sub-field, then for each present sub-item: σX, σY, σH, σV
→ Same fields: `track_sigma_x_nm`, `track_sigma_y_nm`, etc.
Was `break` — now decode then continue.

### FRN 15 — I020/500 Position Accuracy (compound)
Sub-fields: DOP matrix, standard deviation of position, height.
Decode σ_lat_m and σ_lon_m if present → `pos_accuracy_lat_m`, `pos_accuracy_lon_m`
Was `break` — now decode then continue.

### FRN 17 — I020/250 Mode-S MB Data (compound)
Same as I048/250 (REP × 8-byte BDS records) but ASTERIX MLAT compound wrapper.
Primary sub-field byte indicates presence, then REP × 8 bytes.
Reuse existing BDS 5,0 and 6,0 decoders.
Was `break` — now decode then continue.

---

## 4. CAT-062 (`decode_cat62_record`)

Currently `break`s on compound FRNs 10, 12, 15, 16, 17, 19-25. All converted to proper decoders.

### FRN 10 — I062/290 System Track Update Ages (compound)
Per-sensor age in 0.25s units. Sub-fields: PSR, SSR, MDS, ADS, ES, VDL, UAT, LOP, MLT.
→ `track_age_psr_s`, `track_age_ssr_s`, `track_age_ads_s`, `track_age_mds_s`

### FRN 11 — I062/200 Mode of Movement (2 bytes) — was `pos += 2`, now decode
- Bits 8-7: Trans (transversal acceleration) → `lateral_accel`
- Bits 6-5: Longi (longitudinal acceleration) → `longitudinal_accel`
- Bit 4: Vertical rate → `climb_descend` flag

### FRN 12 — I062/295 Track Data Ages (compound)
14 sub-fields (each 1 byte, 0.25s/LSB). Decode ages for: MFL, MD1-3, MHD, IAS, TAS, BVR, GVR, GV, TAN, GSP, VUN, MET, EMC, POS, GAL, PUN.
→ Store as `data_age_<field>_s` dict key or top-level fields.

### FRN 15 — I062/380 Aircraft Derived Data (compound) ★ HIGHEST VALUE ★
Primary sub-field (PSF) byte + sub-fields, each with fixed size:
- Sub 01 (PSF bit 8): Aircraft Address (3 bytes) → `icao24`
- Sub 02 (PSF bit 7): Aircraft ID (7 bytes: 1B flags + 6B callsign) → `callsign`
- Sub 03 (PSF bit 6): Roll Angle (2 bytes, s16, 45/512 deg) → `roll_deg`
- Sub 04 (PSF bit 5): Track Angle (2 bytes, u16, 360/65536) → `true_track_deg`
- Sub 05 (PSF bit 4): Airspeed (2 bytes, IM flag + 15-bit) → `ias_kt` or `mach`
- Sub 06 (PSF bit 3): TAS (2 bytes, u16, 1 kt/LSB) → `tas_kt`
- Sub 07 (PSF bit 2): SSR modes capability (2 bytes) → skip/raw
- Sub 08 (PSF bit 1 = FX2): Emergency/priority (1 byte) → `emergency_code`, `emergency_str`
  (0=none, 1=general emergency, 2=lifeguard, 3=min fuel, 4=no comms, 5=unlawful, 6=downed)
- FX extension byte (if FX2 set), further subs:
- Sub 09: Met (wind+temp, 8 bytes) → `wind_speed_kt`, `wind_dir_deg`, `temp_c`
- Sub 10: ACAS RA (7 bytes, BDS 3,0) → same decode as I048/260 above
- Sub 11: Barometric Alt (2 bytes, 1/4 FL, s16) → `alt_baro_ft`
- Sub 12: Mode C code → `mode_c_alt_ft`
- Sub 13: ICAO address (3 bytes) → `icao24` (fallback)
- Sub 14: Mode S MB data (REP × 8 bytes) → BDS registers

PSF decode: read PSF byte(s), then for each set bit read the sub-field in order.

### FRN 16 — I062/390 Flight Plan Data (compound)
PSF bitmap, sub-fields:
- CS (7 bytes: 1B quality + 6B callsign) → `fp_callsign`
- IFI (4 bytes: 2B type + 2B number) → `ifps_id`
- FCT (1 byte) → `flight_category` (general, military, non-scheduled, scheduled)
- TAC (4 bytes, char[4]) → `aircraft_type` (ICAO type designator, e.g. "A320")
- WTC (1 byte) → `wake_turb_cat` ("L","M","H","J")
- DEP (4 bytes, char[4]) → `departure_icao`
- DST (4 bytes, char[4]) → `destination_icao`
- CFL (2 bytes, 1/4 FL, s16) → `cleared_fl`

### FRN 17 — I062/270 Target Size and Orientation (FX variable)
- Byte 0 bits 8-2: length in meters → `target_length_m`
- If FX: byte 1 bits 8-2: orientation → `target_orientation_deg`
- If FX: byte 2 bits 8-2: width → `target_width_m`

### FRN 22 — I062/500 Estimated Accuracies (compound)
Sub-fields for position, speed, acceleration uncertainties:
- σ_lat_m, σ_lon_m (from Apc sub-field) → `pos_accuracy_lat_m`, `pos_accuracy_lon_m`

### FRN 23 — I062/340 Measured Information (compound)
Per-sensor measurement data (sensor ID, range, azimuth, Mode-C, etc.).
Useful for showing which sensors contributed to a fused track.
Store: `measured_by` list of SAC/SIC contributing sensor IDs.

### FRNs 19-21 (Mode-5, hidden track, composed track number) — skip (military / classified)

---

## 5. cot_layer.py — AIR Remarks Display

All new fields routed to existing section builders:

**STATUS section** (prepended, before other flags):
- `[⚠ SPI IDENT]` — `spi: True`
- `[⚠ ACAS RA: CLIMB]` / `[⚠ ACAS RA: DESCEND]` / `[⚠ ACAS RA ACTIVE]` — `acas_ra_active`
- `[⚠ EMERGENCY: <str>]` — `emergency_str` from I062/380 sub 08
- `[MSAW]` — `msaw: True`
- `[SQUAWK GARBLED]` — `squawk_garbled`

**IFF / MODES section**:
- `MODE-2: xxxx` — `mode2`
- `COM: <capability>` — `com_capability`
- `ACAS v<n>  ARC: 25ft` — `acas_capable`, `altitude_25ft`
- `LINK: 1090ES / UAT / VDL4` — `link_tech`
- `EMITTER: <category str>` — `emitter_category`

**KINEMATICS section**:
- Vertical rate: existing `baro_vr_fpm`; add `geo_vr_fpm` as secondary fallback
- `MAG HDG: xxx°` — `mag_hdg_deg` (if differs from track heading by > 2°)
- `ROLL: ±x.x°` — `roll_deg` (merged with HDG line if present)
- `DOPPLER: ±xxx kt` — `doppler_kt`
- `SEL ALT: FL xxx` / `FINAL ALT: FL xxx` — `selected_alt_ft`, `final_alt_ft`
- `IAS: xxx kt  TAS: xxx kt  MACH: x.xxx` — already displayed; CAT-21 adds same fields ✓
- `WIND: xxx° / xx kt  T: xx°C` — `wind_dir_deg`, `wind_speed_kt`, `temp_c`
- `TRACK RATE: ±x.x °/s` — `track_angle_rate_degs`

**RADAR section**:
- `ACC: ±x.xx nm ±xxx ft` — `track_sigma_x_nm`, `track_sigma_h_ft`
- `DOPPLER: ±xxx kt` (moved here from KINEMATICS for radar-sourced tracks)
- `AGE — PSR: x.xs  SSR: x.xs  ADS: x.xs` — track update ages

**FLIGHT PLAN section** (new, only shown when `fp_callsign` or `aircraft_type` present):
- `FLT: <fp_callsign>  TYPE: A320  WTC: M`
- `DEP: EYVI → DST: EPWA  CFL: FL350`
- `IFPS: xxx`

---

## 6. Gillham (Mode-C) Decode

Add a standalone `_gillham_to_ft(code: int) -> int | None` helper.
The 12-bit Gillham code uses A1,A2,A4,B1,B2,B4,C1,C2,C4,D1,D2,D4 encoding → Gray code → altitude in 100 ft increments.

---

## Implementation Notes

- I062/380 and I062/390 PSF decoders are written as standalone helper functions
  `_decode_i062_380(data, pos) -> tuple[dict, int]` and
  `_decode_i062_390(data, pos) -> tuple[dict, int]`
- BDS register decode is already in `_decode_bds50` / `_decode_bds60` — add `_decode_bds30` (ACAS RA) as shared helper used by I048/260, I062/380 sub 10, I020/250
- cot_layer.py: no structural changes; only additions to `_build_remarks` AIR block
- All fields nullable — missing fields are silently omitted from remarks

---

## Files Changed

1. `compose/bridge/bridges/asterix_bridge.py` — all decoder additions
2. `compose/bridge/layers/cot_layer.py` — remarks display additions
