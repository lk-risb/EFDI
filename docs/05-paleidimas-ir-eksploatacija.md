# 05 — Paleidimas ir eksploatacija

## Steko paleidimas

```bash
./start.sh
```

Interaktyvus paleidiklis rodo visas paslaugas su jų parengties būsena. Įjunkite/išjunkite numeriu, tada paspauskite **Enter** pasirinktoms paslaugoms paleisti.

```text
╔══════════════════════════════════════════════════════════════════╗
║           EFDI Bridge Launcher  —  select services to start      ║
╚══════════════════════════════════════════════════════════════════╝

  Infrastructure
  ──────────────────────────────────────────────────────────
  [ 1] [✓] zenoh          Zenoh message router (Docker)          ready

  Open-data bridges
  ──────────────────────────────────────────────────────────
  [ 6] [✓] meteolt        meteo.lt weather stations              ready

  Sensor bridges
  ──────────────────────────────────────────────────────────
  [ 8] [ ] sitaware       SitaWare HQ dokumentuotas JSON resursas will prompt for address+login
  [ 9] [✓] dronuradaras   dronuradaras.lt drone detection        ready
  [10] [✓] udp-ingress    Generic UDP → raw topics               UDP 50000
  [11] [✓] track-fusion   Radar/ADS-B track correlation          ready

  Protocols
  ──────────────────────────────────────────────────────────
  [13] [✓] asterix-cat10  ASTERIX CAT-010 airport surface        UDP 50010
  [14] [✓] asterix-cat20  ASTERIX CAT-020 Ed.1.11 MLAT           UDP 50020
  [15] [✓] asterix-cat21  ASTERIX CAT-021 Ed.2.7 ADS-B           UDP 50021
  [16] [✓] asterix-cat34  ASTERIX CAT-034 radar service          UDP 50034
  [17] [✓] asterix-cat48  ASTERIX CAT-048 radar targets          UDP 50048
  [18] [✓] asterix-cat62  ASTERIX CAT-062 system tracks          UDP 50062
  [19] [✓] nffi           NATO NFFI XML Zenoh translator         ready
  [20] [ ] sapient        SAPIENT / BSI Flex 335                 will prompt for address
  [21] [ ] stanag4586     STANAG 4586 UAV feed                   will prompt for address
  [22] [ ] sapient-raw    SAPIENT socket → Zenoh raw             SAPIENT_RAW_PORT not set
  [23] [ ] stanag4586-raw STANAG 4586 socket → Zenoh raw         STANAG4586_RAW_PORT not set

  Zenoh-native translators
  ──────────────────────────────────────────────────────────
  [24] [✓] cap            CAP 1.2 XML → alerts                   ready
  [25] [✓] geojson        GeoJSON/OGC Features → areas           ready
  [33] [✓] spectrum       RF spectrum observations               ready
  [34] [✓] sensor-health  Sensor health/heartbeat records         ready
  [35] [✓] mission-route  UAV routes and corridors                ready

  Output layers
  ──────────────────────────────────────────────────────────
  [38] [✓] tak-layer         CoT → TAK Server TCP
  [40] [ ] sitaware-hq-nvg EFDI tracks → SitaWare HQ pull feed   SITAWARE_HQ_NVG_PORT not set
```

**Paleidiklio valdymas:**

| Įvestis | Veiksmas |
| --- | --- |
| `1`–`44` | Įjungti / išjungti paslaugą (keli skaičiai atskiriami tarpu) |
| `a` | Pasirinkti visas paruoštas paslaugas |
| `n` | Atžymėti visas |
| Enter | Paleisti pažymėtas paslaugas |
| `q` | Išeiti |

**Rekomenduojami rinkiniai:**

| Scenarijus | Pasirinkimas |
| --- | --- |
| Giraffe CAT-34/48 + TAK serveris | `1 17 18 38` |
| Giraffe + drono aptikimai + TAK serveris | `1 10 17 18 38` |
| Giraffe + SitaWare + TAK serveris | `1 9 17 18 38` |
| SitaWare HQ periodiškai ima EFDI takelius | `1 40` |
| Visi parengti šaltiniai + TAK serveris | `a` |
| Tik radaras be TAK išvesties (derinimui) | `1 12 17 18` |

Procesų PID failai saugomi `$POD_STATE_DIR/.pids/`, žurnalai rašomi į `$POD_STATE_DIR/logs/<paslauga>.log`.

Po sėkmingo paleidimo `start.sh` išsaugo pasirinktų paslaugų sąrašą ir paskutinius TAK/SitaWare adresus faile `$POD_STATE_DIR/launcher-state.env` (teisės 600). Jis taip pat įtraukia visus tuo metu veikiančius PID valdomus procesus. Kitą kartą interaktyviai paleidus rodomas visas atkurtas pasirinkimas ir po penkių sekundžių automatiškai paleidžiamas; per atgalinį skaičiavimą paspauskite `c`, jei norite pakeisti nustatymus. Slaptažodžiai, API raktai ir sertifikatai ten nesaugomi. Aiškiai `compose/.env` nustatyti adresai turi pirmenybę.

---

## Eksploatacija

### Paslaugų stabdymas

```bash
./stop.sh              # Stabdo visus bridge procesus
./stop.sh layers       # Stabdo tik išvesties sluoksnius (tak-layer, track-fusion)
```

### Žurnalų stebėjimas

```bash
tail -f $POD_STATE_DIR/logs/asterix.log          # Giraffe radaras — ASTERIX dekodavimas ir publikavimas
tail -f $POD_STATE_DIR/logs/dronuradaras.log     # Drono aptikimo įvykiai
tail -f $POD_STATE_DIR/logs/track-fusion.log     # Sulieta takelio išvestis
```

### Procesų būsenos tikrinimas

```bash
ls $POD_STATE_DIR/.pids/                                          # Veikiančių paslaugų sąrašas
kill -0 $(cat $POD_STATE_DIR/.pids/asterix.pid) && echo ok        # Konkretaus proceso tikrinimas
```

---
