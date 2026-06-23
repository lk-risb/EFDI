# EFDI Moon-Pod — Diegimo instrukcija

> **Platforma:** Linux · **Zenoh:** 1.9.0 · **Python:** 3.10+
>
> Techniniai terminai, komandų ir failų pavadinimai pateikiami anglų kalba.

Šis vadovas aprašo sensorių bridge'ų steko diegimą Linux serveryje. Stekas priima ASTERIX CAT-48/34 (Giraffe AMB radaras), dronuradaras.lt akustinius jutiklius, Link-16, MAVLink ir SitaWare duomenis, juos nukreipia per vietinę Zenoh magistralę ir pristatyta į ATAK kaip CoT pranešimai — UDP multicast arba TCP per TAK serverį.

---

## Turinys

1. [Reikalavimai](#1-reikalavimai)
2. [Diegimas](#2-diegimas)
3. [Konfigūracija](#3-konfigūracija)
4. [Steko paleidimas](#4-steko-paleidimas)
5. [ATAK sąranka](#5-atak-sąranka)
6. [Paslaugų žinynas](#6-paslaugų-žinynas)
7. [Eksploatacija](#7-eksploatacija)
8. [Dažniausios problemos](#8-dažniausios-problemos)
9. [Naujo bridge kūrimas](#9-naujo-bridge-kūrimas)

---

## 1. Reikalavimai

### Programinė įranga

| Priklausomybė | Minimali versija | Tikrinimas |
|---|---|---|
| Python | 3.10 | `python3 --version` |
| Docker Engine | 24.0 | `docker --version` |
| Docker Compose | 2.20 | `docker compose version` |
| Git | bet kuri | `git --version` |

### Tinklas

| Prievadas / adresas | Kryptis | Paskirtis |
|---|---|---|
| UDP `<CAT48_PORT>` (numatytasis 30048) | į serverį | Giraffe AMB ASTERIX srautas |
| UDP multicast `239.2.3.1:6969` | iš serverio | CoT pristatymas į ATAK |
| TCP 7448 | localhost | Vietinis Zenoh router |
| TCP 7447 TLS | iš serverio | Nuotolinis Zenoh router (reikia NetBird) |
| HTTPS | iš serverio | dronuradaras.lt REST API |

ATAK įrenginiai turi būti tame pačiame L2 tinklo segmente kaip serveris (multicast neperžengia VLAN ribų be maršrutizatoriaus konfigūracijos). Tarpvietiniam diegimui naudokite TAK serverį ir `cot-tcp` paslaugą.

### Sertifikatai

Zenoh mTLS autentifikacijai reikalingas EFDI išduotas `goat-bundle`. Gaukite jį iš EFDI administratoriaus. **Bundle niekada nesaugomas šioje repozitorijoje.**

---

## 2. Diegimas

### 2.1 Repozitorijos klonavimas

```bash
git clone <repo-url> efdi-moon-pod
cd efdi-moon-pod
```

### 2.2 goat-bundle įdiegimas

Bundle patalpinkite į `$HOME/goat-bundle/` (numatytasis kelias; keičiamas per `BUNDLE_DIR`):

```
~/goat-bundle/
├── efdi-ca-root.pem          # CA sertifikatas (viešas)
├── <NAMESPACE>-cert.pem      # Mazgo sertifikatas
└── <NAMESPACE>-key.pem       # Privatus raktas — apribokite prieigą
```

`<NAMESPACE>` — jūsų pod'ui priskirtas šešioliktainis UUID (pvz. `<YOUR_NAMESPACE>`).

```bash
# Patikrinimas
ls ~/goat-bundle/*.pem
chmod 600 ~/goat-bundle/*-key.pem
```

### 2.3 Python virtualios aplinkos kūrimas

`start.sh` sukuria aplinką automatiškai per pirmą paleidimą. Rankinis kūrimas:

```bash
python3 -m venv compose/bridge/venv
compose/bridge/venv/bin/pip install eclipse-zenoh==1.9.0
```

> `eclipse-zenoh` versija turi būti **tiksliai 1.9.0** — net nedideli versijų skirtumai gali pakeisti API.

### 2.4 Zenoh router paleidimas

```bash
docker compose -f compose/docker-compose.yml up -d zenoh-router
```

Prieš tęsiant patikrinkite, kad konteineris veikia:

```bash
docker compose -f compose/docker-compose.yml ps zenoh-router
# Stulpelyje "Status" turi būti "healthy"
```

---

## 3. Konfigūracija

```bash
cp compose/.env.example compose/.env
```

Redaguokite `compose/.env`. Failą `start.sh` nuskaito eilutė po eilutės saugiu būdu — be `eval`, be subapvalkalo vykdymo.

> `compose/.env` įtrauktas į `.gitignore`. **Niekada jo nekomituokite.**

### Privalomi laukai

```bash
# ── Bundle kelias ────────────────────────────────────────────────────────────
BUNDLE_DIR=/home/<vartotojas>/goat-bundle

# ── Giraffe AMB radaras (ASTERIX CAT-48/34) ─────────────────────────────────
CAT48_PORT=30048               # UDP prievadas, iš kurio radaras siunčia duomenis
CAT48_RADAR_LAT=<RADAR_LAT>        # Antenos platuma  (WGS-84 dešimtainiai laipsniai)
CAT48_RADAR_LON=<RADAR_LON>        # Antenos ilguma   (WGS-84 dešimtainiai laipsniai)
CAT48_RADAR_SAC=<SAC>            # ASTERIX šaltinio srities kodas (Source Area Code)
CAT48_RADAR_SIC=<SIC>             # ASTERIX šaltinio identifikacijos kodas
CAT48_RADAR_NAME=Giraffe AMB   # Vardas, rodomas ATAK žemėlapyje
```

### Pasirinktiniai laukai

```bash
# ── TAK serveris (naudokite cot-tcp vietoj cot-udp) ─────────────────────────
TAK_HOST=127.0.0.1
TAK_PORT=8087

# ── SitaWare draugiškų pajėgų sekimas ───────────────────────────────────────
SITAWARE_URL=https://sitaware.example.com
SITAWARE_USER=
SITAWARE_PASS=

# ── Link-16 JREAP-C ─────────────────────────────────────────────────────────
LINK16_PORT=                   # Palikite tuščią, jei Link-16 šaltinio nėra
LINK16_TCP=                    # 1 = TCP režimas, tuščia = UDP

# ── MAVLink ─────────────────────────────────────────────────────────────────
MAVLINK_PORT=
MAVLINK_TCP=
```

---

## 4. Steko paleidimas

```bash
./start.sh
```

Interaktyvus paleidiklis rodo visas paslaugas su jų parengties būsena. Įjunkite/išjunkite numeriu, tada paspauskite **Enter** pasirinktoms paslaugoms paleisti.

```
╔══════════════════════════════════════════════════════════════════╗
║           EFDI Bridge Launcher  —  select services to start      ║
╚══════════════════════════════════════════════════════════════════╝

  Infrastructure
  ──────────────────────────────────────────────────────────
  [ 1] [✓] zenoh          Zenoh message router (Docker)          ready

  Sensor bridges
  ──────────────────────────────────────────────────────────
  [ 2] [✓] asterix        ASTERIX CAT-48/34 radar tracks         ready
  [ 3] [ ] link16         Link-16 JREAP-C datalink               LINK16_PORT not set
  [ 4] [ ] mavlink        MAVLink UAV telemetry                   MAVLINK_PORT not set
  [ 5] [ ] vmf            VMF MIL-STD-47001C messages            VMF_PORT not set
  [ 6] [ ] sitaware       SitaWare friendly force tracking       SITAWARE_URL not set
  [ 7] [ ] dronuradaras   dronuradaras.lt drone detection        ready

  Output layers
  ──────────────────────────────────────────────────────────
  [ 8] [✓] cot-udp        CoT → ATAK UDP multicast 239.2.3.1:6969
  [ 9] [✓] cot-tcp        CoT → TAK Server TCP
  [10] [✓] track-fusion   Radar/ADS-B track correlation
```

**Paleidiklio valdymas:**

| Įvestis | Veiksmas |
|---|---|
| `1`–`10` | Įjungti / išjungti paslaugą (keli skaičiai atskiriami tarpu) |
| `a` | Pasirinkti visas paruoštas paslaugas |
| `n` | Atžymėti visas |
| Enter | Paleisti pažymėtas paslaugas |
| `q` | Išeiti |

**Rekomenduojami rinkiniai:**

| Scenarijus | Pasirinkimas |
|---|---|
| Giraffe radaras + ATAK multicast | `1 2 8` |
| Giraffe + drono aptikimai + ATAK | `1 2 7 8` |
| Visi jutikliai + TAK serveris | `a`, tada atžymėkite `8` (cot-udp) |
| Tik radaras be ATAK (derinimui) | `1 2 10` |

Procesų PID failai saugomi `.pids/`, žurnalai rašomi į `logs/<paslauga>.log`.

---

## 5. ATAK sąranka

### UDP multicast (tas pats tinklų segmentas)

1. **Settings → Network → Multicast** — įjunkite multicast gaviklį
2. Adresų sąraše patikrinkite, kad yra `239.2.3.1:6969`
3. Objektai turi pasirodyti per vieną apklausinėjimo ciklą (≤ 10 s drono aptikimams, ≤ 60 s radaro keepalive)

### TAK serveris (skirtingi tinklai / VLAN)

Nustatykite `TAK_HOST` ir `TAK_PORT` faile `.env`, tada paleidiklyje pasirinkite `cot-tcp` vietoj `cot-udp`.

### Piktogramų žinynas

| ATAK piktograma | CoT tipas | Šaltinis |
|---|---|---|
| Mėlynas radaro dubuo | `a-f-G-E-S-R` | Giraffe AMB radaro vieta |
| Žalia sensorių dėžutė | `a-n-G-E-S` | dronuradaras.lt akustinis jutiklis |
| Raudona priešiška UAV | `a-h-A-M-F-Q` | Drono aptikimo įvykis |
| Balta nežinoma orlaivio | `a-u-A-C-F` | Neklasifikuotas radaro takelis |

---

## 6. Paslaugų žinynas

| Paslauga | Scenarijus | Zenoh tema (sutrumpinta) | Suaktyvinimas |
|---|---|---|---|
| `asterix` | `bridges/asterix_bridge.py` | `…/air/asterix/cat48/unknown/aircraft/tracks/v1` | Srautinis UDP |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/air/dronuradaras/acoustic/hostile/uav/tracks/v1` | REST apklausa 10 s |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/rest/friendly/unit/tracks/v1` | Konfigūruojama REST |
| `link16` | `bridges/link16_bridge.py` | `…/air/link16/jreap/*/aircraft/tracks/v1` | Srautinis UDP/TCP |
| `mavlink` | `bridges/mavlink_bridge.py` | `…/air/mavlink/mav2/*/uav/tracks/v1` | Srautinis UDP/TCP |
| `cot-udp` | `layers/cot_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `cot-tcp` | `layers/cot_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `track-fusion` | `layers/track_fusion_layer.py` | CAT-48 + CAT-21 prenumeratorius | Įvykio valdomas |

### Zenoh temų schema

```
{VARDAS_ERDVĖ}/{DOMENAS}/{ŠALTINIS}/{PROTOKOLAS}/{PRIKLAUSOMYBĖ}/{TIPAS}/tracks/v1
```

| Laukas | Galimos reikšmės |
|---|---|
| `DOMENAS` | `air`, `land`, `sea`, `space`, `env` |
| `PRIKLAUSOMYBĖ` | `friendly`, `hostile`, `neutral`, `unknown`, `civ`, `mil` |
| `TIPAS` | `aircraft`, `vessel`, `vehicle`, `unit`, `sensor`, `uav`, `radar` |

---

## 7. Eksploatacija

### Paslaugų stabdymas

```bash
./stop.sh              # Stabdo visus bridge procesus
./stop.sh layers       # Stabdo tik išvesties sluoksnius (cot-udp, cot-tcp, track-fusion)
```

### Žurnalų stebėjimas

```bash
tail -f logs/asterix.log          # Giraffe radaras — ASTERIX dekodavimas ir publikavimas
tail -f logs/cot-udp.log          # CoT išvestis — patvirtina pristatymą į ATAK
tail -f logs/dronuradaras.log     # Drono aptikimo įvykiai
tail -f logs/track-fusion.log     # Sulieta takelio išvestis
```

### Procesų būsenos tikrinimas

```bash
ls .pids/                                          # Veikiančių paslaugų sąrašas
kill -0 $(cat .pids/asterix.pid) && echo ok        # Konkretaus proceso tikrinimas
```

---

## 8. Dažniausios problemos

### Zenoh ryšio klaida

**Simptomas:** `zenoh.ZError: Unable to connect to any of [tls/zenoh.efdi...]`

```bash
# 1. Patikrinkite ar router konteineris sveikas
docker compose -f compose/docker-compose.yml ps zenoh-router

# 2. Patikrinkite ar endpoint kintamasis nustatytas
echo $ZENOH_LOCAL_ENDPOINT   # turi būti: tcp/127.0.0.1:7448

# 3. Patikrinkite ar sertifikatų failai egzistuoja
ls $GOAT_CERT_DIR/*.pem
```

Jei `compose/.env` buvo įkeltas paprastu `source compose/.env`, kintamieji neeksportuojami į vaikininius procesus. Naudokite `./start.sh` (kuris tai tvarko automatiškai), arba:

```bash
set -a && source compose/.env && set +a
```

### ATAK nerodo jokių objektų

```bash
# 1. Patikrinkite ar cot-udp veikia
kill -0 $(cat .pids/cot-udp.pid) && echo veikia

# 2. Patikrinkite ar multicast srautas išeina iš serverio
sudo tcpdump -i any udp and host 239.2.3.1 and port 6969 -c 5

# 3. Patikrinkite ar ATAK ir serveris tame pačiame L2 segmente
```

### Giraffe radaras rodomas 0°Š 0°R koordinatėse

`CAT48_RADAR_LAT` arba `CAT48_RADAR_LON` nenustatytas. Patikrinkite:

```bash
grep CAT48_RADAR compose/.env
```

### Drono aptikimai nepublikuojami

Bridge'as atmeta aptikimus, senesnius nei 300 s. Patikrinkite API pasiekiamumą ir duomenų aktualumą:

```bash
curl -s -H "Origin: https://dronuradaras.lt" \
  https://radar-api.mainline.inc/api/v1/public/detections \
  | python3 -c "
import sys, json, time
d = json.load(sys.stdin).get('detections', [])
now = time.time()
fresh = [x for x in d if (now - x.get('detected_at', 0)/1000) < 300]
print(f'{len(fresh)} nauji / {len(d)} iš viso aptikimų')
"
```

### Keli to paties proceso egzemplioriai

Atsiranda paleidus `start.sh` du kartus be sustabdymo:

```bash
pkill -f "_bridge\.py\|cot_layer\|track_fusion"
rm -f .pids/*.pid
./start.sh
```

### Giraffe radaro piktograma dingsta iš ATAK

`asterix` bridge'as kas 60 s publikuoja keepalive nepriklausomai nuo takelio aktyvumo. Jei piktograma dingsta — bridge'as sustojo:

```bash
tail -20 logs/asterix.log | grep -E "keepalive|startup|error"
```

---

## 9. Naujo bridge kūrimas

### Failo struktūra

```python
# compose/bridge/bridges/<pavadinimas>_bridge.py

import json, os, time
import zenoh

ORG       = "<YOUR_NAMESPACE>"
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", os.path.dirname(__file__))

# make_config() nukopijuokite iš bet kurio esamo bridge — visiems ji identiška.

def main():
    session = zenoh.open(make_config())
    topic = f"{ORG}/air/<šaltinis>/<protokolas>/unknown/aircraft/tracks/v1"
    pub = session.declare_publisher(topic)

    while True:
        for item in fetch_data():
            payload = {
                "_src": "<šaltinis>", "_ts": time.time(),
                "lat_deg": item["lat"], "lon_deg": item["lon"],
            }
            pub.put(json.dumps(payload).encode(),
                    encoding=zenoh.Encoding.APPLICATION_JSON)
        time.sleep(POLL_INTERVAL)
```

### Minimalūs privalomi JSON payload laukai

```json
{
  "_src":    "šaltinio_pavadinimas",
  "_ts":     1234567890.123,
  "lat_deg": 54.6712,
  "lon_deg": 25.2791
}
```

Pasirinktiniai laukai, atpažįstami išvesties sluoksnių:

```json
{
  "sensor_id":   "unikalus_id",
  "callsign":    "rodomas_vardas",
  "speed_ms":    15.2,
  "heading_deg": 270.0,
  "baro_alt_m":  1500.0
}
```

### Registravimas `start.sh`

```bash
# 1. Pridėkite į SERVICES masyvą
SERVICES=(... <pavadinimas> ...)

# 2. Pridėkite kategoriją
[<pavadinimas>]="Sensor bridges"

# 3. Pridėkite aprašymą
[<pavadinimas>]="Trumpas aprašymas"

# 4. Pridėkite parengties tikrinimą (arba return 0 jei visada paruošta)
<pavadinimas>) [[ "${MANO_KINTAMASIS:-}" ]] ;;

# 5. Pridėkite launch case
<pavadinimas>)
    _start <pavadinimas> bridges/<pavadinimas>_bridge.py ;;
```

### CoT tipo pridėjimas (jei reikia naujo)

`layers/cot_layer.py` faile, `_TOPIC_COT` žodyne:

```python
"air/**/hostile/uav/**":      ("a-h-A-M-F-Q", AIR_STALE_S),
"land/**/neutral/sensor/**":  ("a-n-G-E-S",   LAND_STALE_S * 2),
```

---

## Pakeitimų žurnalas

| Data | Autorius | Pakeitimas |
|---|---|---|
| 2026-06-22 | — | Pradinis diegimas: Giraffe ASTERIX bridge, `start.sh` paleidiklis |
| 2026-06-22 | — | dronuradaras.lt bridge — akustiniai jutikliai ir drono aptikimai |
| 2026-06-22 | — | CoT DETECTION sekcija su garso URL ATAK pastabose |
| 2026-06-22 | — | Radaro keepalive ir pradinė publikacija paleidimo metu |

---

*Skirta vidiniam naudojimui — neskleisti už projekto ribų.*
