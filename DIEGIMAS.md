# EFDI Moon-Pod — Diegimo ir Naudojimo Instrukcija

> **Dokumentas atnaujinamas nuolat** — pridėkite pastabas prie kiekvieno skyriaus jei kažkas pasikeitė.
>
> Kalba: lietuvių. Komandų pavadinimai, failų pavadinimai ir techniniai terminai paliekami anglų kalba.

---

## Turinys

1. [Sistemos apžvalga](#1-sistemos-apžvalga)
2. [Reikalavimai](#2-reikalavimai)
3. [Pradinė sąranka](#3-pradinė-sąranka)
4. [Sertifikatų ir bundle diegimas](#4-sertifikatų-ir-bundle-diegimas)
5. [Konfigūracija `.env`](#5-konfigūracija-env)
6. [Paleidimas](#6-paleidimas)
7. [Komponentų aprašymas](#7-komponentų-aprašymas)
8. [ATAK konfigūracija](#8-atak-konfigūracija)
9. [Dažniausios problemos](#9-dažniausios-problemos)
10. [Naujo bridge kūrimas](#10-naujo-bridge-kūrimas)
11. [Atnaujinimų žurnalas](#11-atnaujinimų-žurnalas)

---

## 1. Sistemos apžvalga

```
[Giraffe AMB radaras]──ASTERIX UDP──►[asterix_bridge]──►┐
[dronuradaras.lt API]──REST poll────►[dronuradaras_bridge]►│
[SitaWare sistema]────REST poll────►[sitaware_bridge]────►│  ZENOH
[Link-16 šaltinis]────UDP/TCP──────►[link16_bridge]──────►│  tinklas
[MAVLink UAV]─────────UDP/TCP──────►[mavlink_bridge]─────►│
                                                           │
                    ┌──────────────────────────────────────┘
                    ▼
             [cot_layer] ──UDP multicast 239.2.3.1:6969──► [ATAK CIV]
             [track_fusion_layer] ──► (sulietų takelius)
```

**Kas yra Zenoh?** — greitas publish/subscribe tinklas (panašus į MQTT, bet greitesnis ir su TLS šifravimu). Visi bridge'ai publikuoja duomenis į Zenoh, o layer'iai juos gauna ir persiunčia toliau.

**Kas yra CoT?** — Cursor-on-Target (CoT) — XML formato pranešimų standartas, kurį naudoja ATAK. Kiekvienas objektas (lėktuvas, radaras, drono aptikimas) siunčiamas kaip CoT XML pranešimas.

---

## 2. Reikalavimai

### Programinė įranga

| Programa | Minimali versija | Tikrinimo komanda |
|---|---|---|
| Python | 3.10+ | `python3 --version` |
| Docker | 24+ | `docker --version` |
| Docker Compose | 2.20+ | `docker compose version` |
| Git | bet kuri | `git --version` |

### Tinklas

- **UDP prievadas 30048** — atidarytas iš Giraffe AMB radaro pusės (ASTERIX CAT-48/34)
- **UDP multicast 239.2.3.1:6969** — ATAK įrenginiai turi būti tame pačiame tinkle
- **TCP 7448** — vietinis Zenoh router (localhost)
- **TCP 7447 TLS** — nuotolinis Zenoh router (reikalingas VPN/NetBird)
- Interneto prieiga — dronuradaras.lt API

### Aparatinė įranga

- Linux serveris (testuota su Arch Linux / kernel 7.x)
- ATAK CIV 5.x įdiegtas Android įrenginyje tame pačiame tinkle

---

## 3. Pradinė sąranka

### 3.1 Projekto klonavimas

```bash
git clone <repo-url> efdi-moon-pod
cd efdi-moon-pod
```

### 3.2 Python virtualios aplinkos sukūrimas

Bridge'ai naudoja atskirą Python aplinką kad nekonfliktuotų su sistemos paketais:

```bash
python3 -m venv compose/bridge/venv
compose/bridge/venv/bin/pip install eclipse-zenoh==1.9.0
```

> **⚠ Svarbu:** eclipse-zenoh versija turi būti **tiksliai 1.9.0**. Kitos versijos gali turėti nesuderinamus API pakeitimus.

Patikrinimas:

```bash
compose/bridge/venv/bin/python3 -c "import zenoh; print(zenoh.__version__)"
# Turi išvesti: 1.9.0
```

### 3.3 Zenoh router paleidimas (Docker)

```bash
docker compose -f compose/docker-compose.yml up -d zenoh-router
```

Patikrinimas:

```bash
docker compose -f compose/docker-compose.yml ps zenoh-router
# Turi rodyti "healthy"
```

> **Pastaba:** Zenoh router yra vienintelis Docker konteineris — visi kiti komponentai veikia tiesiogiai kaip Python procesai.

---

## 4. Sertifikatų ir bundle diegimas

Zenoh TLS ryšiui reikalingi sertifikatai. Jie **negali būti saugomi repozitorijoje**.

### 4.1 Bundle struktūra

Sertifikatai saugomi atskirame kataloge (numatytasis: `/home/<vartotojas>/goat-bundle`):

```
~/goat-bundle/
├── efdi-ca-root.pem              # CA sertifikatas (viešas)
├── <NAMESPACE>-cert.pem          # Jūsų pod sertifikatas
└── <NAMESPACE>-key.pem           # Privatus raktas (SAUGOTI SLAPTAI)
```

kur `<NAMESPACE>` = jūsų organizacijos unikalus identifikatorius (pvz. `1851281db70ccc0409dad4ecfc874cf5`).

### 4.2 Bundle gavimas

Bundle gaunamas iš EFDI administratoriaus arba per `goat-cli`:

```bash
# Jei turite goat-cli:
goat-cli bundle download --output ~/goat-bundle/
```

### 4.3 Aplinkos kintamieji

Nustatykite prieš paleidžiant bet kurį bridge:

```bash
export BUNDLE_DIR=~/goat-bundle
export GOAT_CERT_DIR=~/goat-bundle
export ZENOH_LOCAL_ENDPOINT=tcp/127.0.0.1:7448
```

> **⚠ Dažna klaida:** Jei bundle katalogo kelias neteisingas arba sertifikatų failai neegzistuoja — Zenoh meta klaidą `Unable to connect` ir bridge nepasileidžia. Tikrinkite failus komanda `ls ~/goat-bundle/*.pem`.

---

## 5. Konfigūracija `.env`

Konfigūracijos failas: `compose/.env`

> **⚠ Šis failas NIEKADA neturi patekti į git repozitoriją** — jame yra API raktai. Patikrinkite `.gitignore`.

### 5.1 Sukūrimas

```bash
cp compose/.env.example compose/.env
# Redaguokite:
nano compose/.env
```

### 5.2 Svarbiausi parametrai

```bash
# ── Zenoh ──────────────────────────────────────────────────────
ZENOH_LOCAL_ENDPOINT=tcp/127.0.0.1:7448
BUNDLE_DIR=/home/<vartotojas>/goat-bundle

# ── Giraffe AMB radaras (ASTERIX CAT-48/34) ────────────────────
CAT48_PORT=30048                  # UDP prievadas iš radaro
CAT48_RADAR_LAT=54.9639           # Radaro geografinė platuma
CAT48_RADAR_LON=24.0848           # Radaro geografinė ilguma
CAT48_RADAR_SAC=122               # Source Area Code (iš radaro konfig.)
CAT48_RADAR_SIC=65                # Source Identification Code (iš radaro konfig.)

# ── TAK serveris (neprivaloma) ──────────────────────────────────
TAK_HOST=127.0.0.1                # Jei nėra TAK serverio — palikite 127.0.0.1
TAK_PORT=8087

# ── SitaWare (jei naudojate) ───────────────────────────────────
SITAWARE_URL=https://sitaware.example.com
SITAWARE_USER=vartotojas
SITAWARE_PASS=slaptazodis

# ── Link-16 (jei yra šaltinis) ─────────────────────────────────
LINK16_PORT=                      # Palikite tuščią jei nenaudojate
LINK16_TCP=                       # 1 = TCP, tuščia = UDP
```

### 5.3 Svarbi pastaba dėl specialių simbolių

Kai kurios reikšmės (pvz. FR24_KEY) gali turėti `|` simbolį. `.env` faile tai **nesukelia problemų**, nes `run.sh` ir `start.sh` skaito jį saugiu būdu (be `eval` ar `source` shell komandų).

---

## 6. Paleidimas

### 6.1 Interaktyvus paleidimas (rekomenduojamas)

```bash
cd efdi-moon-pod
./start.sh
```

Pasirodys meniu:

```
╔══════════════════════════════════════════════════════════════════╗
║           EFDI Bridge Launcher  —  select services to start      ║
╚══════════════════════════════════════════════════════════════════╝

  Infrastructure
  [ 1] [✓] zenoh         Zenoh message router (Docker)           ready

  Sensor bridges
  [ 2] [✓] asterix       ASTERIX CAT-48/34 radar tracks          ready
  [ 3] [ ] link16        Link-16 JREAP-C datalink                LINK16_PORT not set
  ...

> _
```

**Valdymas:**
- Rašykite skaičių (pvz. `2`) — įjungti/išjungti paslaugą
- Keli skaičiai iš karto (pvz. `1 2 6`) — keisti kelis
- `a` — pasirinkti visus galimus
- `n` — atžymėti visus
- `Enter` — paleisti pažymėtus
- `q` — išeiti

**Tipinis paleidimo rinkinys (Giraffe radaras + ATAK):**

Pažymėkite: `1` (zenoh) + `2` (asterix) + `7` (dronuradaras) + `8` (cot-udp)

### 6.2 Sustabdymas

```bash
./stop.sh         # Sustabdo viską
./stop.sh layers  # Sustabdo tik output layers
```

### 6.3 Žurnalai (logs)

Visi žurnalai rašomi į `logs/` katalogą:

```bash
tail -f logs/asterix.log         # Giraffe radaro bridge
tail -f logs/cot-udp.log         # CoT → ATAK srautas
tail -f logs/dronuradaras.log    # dronuradaras.lt bridge
tail -f logs/track-fusion.log    # Takelio koreliacijos sluoksnis
```

### 6.4 Procesų tikrinimas

```bash
ls .pids/          # Rodo veikiančių procesų PID failus
cat .pids/asterix.pid
kill -0 $(cat .pids/asterix.pid) && echo "veikia" || echo "neveikia"
```

---

## 7. Komponentų aprašymas

### 7.1 `asterix_bridge.py` — Giraffe AMB radaras

**Ką daro:** Klauso ASTERIX UDP srautą iš Giraffe AMB radaro, dekodina CAT-48 (sekamieji objektai) ir CAT-34 (radaro būsena) pranešimus, publikuoja į Zenoh.

**Zenoh temos:**
- `…/air/asterix/cat48/unknown/aircraft/tracks/v1` — oro objektai
- `…/land/asterix/cat34/neutral/radar/status/v1` — radaro būsena

**Ypatingumas:** Paleidimo metu iš karto publikuoja pradinį radaro žymeklį į ATAK (net jei radaras dar nesiuntė duomenų). Kiekvieną 60 sekundžių atnaujina žymeklį kad jis neišnyktų iš ATAK žemėlapio (keepalive mechanizmas).

**Paleidimo parametrai (automatiškai nuskaitomi iš `.env`):**
```bash
compose/bridge/venv/bin/python3 compose/bridge/bridges/asterix_bridge.py \
  --cat48-port 30048 \
  --radar-lat 54.9639 \
  --radar-lon 24.0848 \
  --radar-name "Giraffe AMB" \
  --radar-sac 122 \
  --radar-sic 65
```

---

### 7.2 `dronuradaras_bridge.py` — dronuradaras.lt akustinio radaro tinklas

**Ką daro:** Apklausinėja `radar-api.mainline.inc` viešąjį API (dronuradaras.lt tinklo backend'as). Publikuoja:
1. Jutiklių mazgų pozicijas (tik **online** būsenos) — žali sensorių dėžutės ATAK žemėlapyje
2. Aptiktus dronus — raudoni priešiški UAV žymekliai su informacine kortele

**API:**
- `GET https://radar-api.mainline.inc/api/v1/public/devices` — jutikliai (kas 60 s)
- `GET https://radar-api.mainline.inc/api/v1/public/detections` — aptikimai (kas 10 s)
- `GET https://radar-api.mainline.inc/api/v1/public/detections/{id}/audio` — WAV garso įrašas

**API rakto nereikia** — naudojama ta pati viešoji CORS kilmė kaip dronuradaras.lt svetainė.

**Zenoh temos:**
- `…/land/dronuradaras/acoustic/neutral/sensor/status/v1` — jutikliai
- `…/air/dronuradaras/acoustic/hostile/uav/tracks/v1` — drono aptikimai

**ATAK žymekliai:**
- Jutikliai: `a-n-G-E-S` (žalia sensorių dėžutė)
- Drono aptikimas: `a-h-A-M-F-Q` (raudona priešiška UAV piktograma)

**Informacinė kortelė drono aptikimui (ATAK):**
```
─── IDENTITY ───
CS: DRONE

─── KINEMATICS ───
LAT/LON/MGRS

─── DETECTION ───
TIME: 2026-06-21 13:59:39 UTC
SENSOR: radar-58264
[AUDIO RECORDED]
AUDIO: https://radar-api.mainline.inc/...audio
REF: 7cd608d3-392e-4
```

---

### 7.3 `cot_layer.py` — CoT išvesties sluoksnis

**Ką daro:** Prenumeruoja visas Zenoh temas, konvertuoja objektus į CoT XML ir siunčia į ATAK.

**Du veikimo režimai:**
- `--udp --host 239.2.3.1 --port 6969` — multicast visiems ATAK tinkle (nenaudojamas TAK serveris)
- `--host <TAK_HOST> --port 8087` — TCP į FreeTAK arba TAK serverį

**CoT tipų žemėlapis (pasirinkimas):**

| Zenoh tema | CoT tipas | ATAK piktograma |
|---|---|---|
| `air/**/unknown/**` | `a-u-A-C-F` | Balta nežinoma piktograma |
| `air/**/hostile/uav/**` | `a-h-A-M-F-Q` | Raudona priešiška UAV |
| `land/**/neutral/radar/**` | `a-f-G-E-S-R` | Mėlynas radaro dubuo |
| `land/**/neutral/sensor/**` | `a-n-G-E-S` | Žalia sensorių dėžutė |
| `sea/**/civ/vessel/**` | (pagal tipą) | Laivas |

---

### 7.4 `track_fusion_layer.py` — takelio koreliacijos sluoksnis

**Ką daro:** Priima radaro takelius (ASTERIX CAT-48) ir ADS-B duomenis, koreliuoja juos pagal erdvę ir laiką, išveda sulietus takelius. Pašalina dublikatus (tą patį lėktuvą matantį ir radaras, ir ADS-B).

**Prenumeruoja:**
- `…/air/asterix/cat48/**` (radaras — pirminė pozicijų valdžia)
- `…/air/asterix/cat21/**` (ADS-B — tapatybės praturtinimas)

**Publikuoja:**
- `…/air/fused/*/aircraft/tracks/v1`

> **Pastaba:** Jei nenorite naudoti koreliacijos — galite paleisti tik `asterix` + `cot-udp` be `track-fusion`. Tada ATAK matys tiesioginius radaro takelius.

---

### 7.5 `sitaware_bridge.py` — SitaWare draugiškų pajėgų stebėjimas

**Ką daro:** Apklausinėja SitaWare REST API, gauna draugiškų vienetų pozicijas ir publikuoja į Zenoh kaip žemės takelius.

**Reikia:** `SITAWARE_URL`, `SITAWARE_USER`, `SITAWARE_PASS` `.env` faile.

---

## 8. ATAK konfigūracija

### 8.1 UDP Multicast gavimas

ATAK įrenginyje:

1. **Settings → Network → Multicast**
2. Įjungti multicast
3. Adresų sąraše turi būti `239.2.3.1:6969`

> Jei ATAK ir serveris skirtinguose tinkluose — multicast neveiks. Reikia arba TAK serverio, arba NetBird VPN.

### 8.2 Tinklo tikrinimas

Serverio pusėje patikrinkite ar multicast siunčiamas:

```bash
# Klausykite ar ateina CoT duomenys (reikia tcpdump):
sudo tcpdump -i any -n udp and host 239.2.3.1 and port 6969 -A | head -50
```

### 8.3 ATAK piktogramų reikšmės

| Spalva | Reikšmė | Pavyzdys |
|---|---|---|
| Mėlyna | Draugiškas (Friendly) | Giraffe radaras |
| Žalia | Neutralus (Neutral) | dronuradaras.lt jutikliai |
| Raudona | Priešiškas (Hostile) | Aptiktas dronas |
| Geltona | Nežinomas (Unknown) | Neklasifikuotas objektas |

---

## 9. Dažniausios problemos

### 9.1 Zenoh negali prisijungti

**Klaida:**
```
zenoh.ZError: Unable to connect to any of [tls/zenoh.efdi...]
```

**Priežastys ir sprendimai:**

```bash
# 1. Patikrinkite ar Zenoh router veikia:
docker compose -f compose/docker-compose.yml ps zenoh-router

# 2. Patikrinkite ar ZENOH_LOCAL_ENDPOINT nustatytas teisingai:
echo $ZENOH_LOCAL_ENDPOINT
# Turi būti: tcp/127.0.0.1:7448

# 3. Eksportuokite rankiniu būdu:
export ZENOH_LOCAL_ENDPOINT=tcp/127.0.0.1:7448
export GOAT_CERT_DIR=/home/<vartotojas>/goat-bundle
export BUNDLE_DIR=/home/<vartotojas>/goat-bundle
```

> **⚠ Svarbu:** `source compose/.env` be `export` neeksportuoja kintamųjų į vaikinių procesų aplinką. Naudokite `set -a && source compose/.env && set +a` arba eksportuokite rankiniu būdu.

---

### 9.2 ATAK nieko nerodo

**Tikrinkite eilės tvarka:**

```bash
# 1. Ar cot-udp procesas veikia?
cat .pids/cot-udp.pid && kill -0 $(cat .pids/cot-udp.pid) && echo "veikia"

# 2. Ar siunčia duomenis? (žurnale turi būti CoT XML eilutės)
tail -20 logs/cot-udp.log

# 3. Ar tinklas leidžia multicast?
sudo tcpdump -i any udp and host 239.2.3.1 -c 5

# 4. Ar ATAK įrenginys tame pačiame tinkle?
# Patikrinkite IP adresus abiejų pusių
```

**Dažna klaida:** ATAK ir serveris skirtinguose VLAN — multicast nepraeina tarp VLAN be maršrutizatoriaus konfigūracijos.

---

### 9.3 Keli to paties proceso egzemplioriai

**Problema:** Paleidus `start.sh` du kartus — veikia du to paties bridge procesai.

**Diagnozė:**
```bash
ps aux | grep "asterix_bridge\|cot_layer" | grep -v grep
```

**Sprendimas:**
```bash
pkill -f "asterix_bridge.py"
pkill -f "cot_layer.py"
rm -f .pids/*.pid
./start.sh
```

> **Kodėl taip nutinka:** PID faile saugomas bash wrapper proceso PID, ne Python proceso. `start.sh` tikrina PID failą, bet procesas jau gali būti kitas. **Niekada nenaudokite** `kill -9` prieš tikrinant — gali palikti Zenoh jungtis neuždarytomis.

---

### 9.4 Giraffe radaras išnyksta iš ATAK po kiek laiko

**Priežastis:** CoT žymeklio galiojimo laikas (stale timer) pasibaigė — ATAK automatiškai ištrina senus žymeklius.

**Sprendimas jau įdiegtas:** `asterix_bridge.py` kas 60 sekundžių iš naujo publikuoja paskutinę žinomą radaro poziciją (keepalive mechanizmas). Jei radaras visiškai atsijungė ir bridge buvo paleistas iš naujo — pradinį žymeklį bridge publikuoja iš karto paleidimo metu.

**Jei vis tiek dingsta:**
```bash
tail -f logs/asterix.log | grep -i "keepalive\|startup\|Published"
```

---

### 9.5 Radaras rodo 0°/0° koordinatėse

**Priežastis:** `CAT48_RADAR_LAT` / `CAT48_RADAR_LON` nenustatyti `.env` faile arba neeksportuoti.

```bash
grep "CAT48_RADAR" compose/.env
# Turi rodyti konkretų skaičių, ne tuščią reikšmę
```

---

### 9.6 dronuradaras.lt bridge nepublikuoja aptikimų

**Galimos priežastys:**

1. **API grąžina tik senus aptikimus** — bridge filtruoja aptikimus senesnius nei 5 minutes. Jei sistemos laikrodis neteisingas arba aptikimai seni — nebus publikuojama.

```bash
# Patikrinkite paskutinį aptikimo laiką:
curl -s -H "Origin: https://dronuradaras.lt" \
  https://radar-api.mainline.inc/api/v1/public/detections | python3 -m json.tool
```

2. **Visi aptikimai jau matyti** — bridge deduplikuoja pagal ID. Perkraukite bridge jei norite vėl pamatyti.

3. **Nėra interneto ryšio:**
```bash
curl -s -o /dev/null -w "%{http_code}" \
  -H "Origin: https://dronuradaras.lt" \
  https://radar-api.mainline.inc/api/v1/public/devices
# Turi grąžinti 200
```

---

### 9.7 `.env` failo kintamieji neskaitomi

**Problema:** FR24_KEY ir panašūs raktai su `|` simboliu gali sugadinti `source .env` komandą standartiniame shell.

**Mūsų sprendimas:** `start.sh` ir `run.sh` skaito `.env` saugiu būdu (eilutė po eilutės, be `eval`). **Niekada nenaudokite:**

```bash
# ❌ BLOGAI — gali interpretuoti | kaip pipe:
source compose/.env

# ✓ GERAI — naudokite start.sh arba run.sh
./start.sh
```

---

## 10. Naujo bridge kūrimas

Sekite šį šabloną kai pridedate naują duomenų šaltinį:

### 10.1 Failo struktūra

```python
# compose/bridge/bridges/<pavadinimas>_bridge.py

import json, os, time, urllib.request
import zenoh

ORG       = "1851281db70ccc0409dad4ecfc874cf5"
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tls/zenoh.efdi...")
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", os.path.dirname(__file__))

def make_config():
    # Kopijuokite iš esamo bridge (pvz. opensky_states_bridge.py)
    ...

def main():
    session = zenoh.open(make_config())
    # Pasirinkite temą pagal duomenų tipą (žr. žemiau)
    topic = "{}/air/<šaltinis>/<protokolas>/unknown/aircraft/tracks/v1".format(ORG)
    pub = session.declare_publisher(topic)

    while True:
        data = fetch_data()
        for item in data:
            payload = {"_src": "<šaltinis>", "_ts": time.time(), "lat_deg": ..., "lon_deg": ...}
            pub.put(json.dumps(payload).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
        time.sleep(POLL_INTERVAL)
```

### 10.2 Temos pavadinimo taisyklės

```
{DOMAIN}/{ŠALTINIS}/{PROTOKOLAS}/{PRIKLAUSOMYBĖ}/{TIPAS}/tracks/v1
```

| Laukas | Galimos reikšmės |
|---|---|
| DOMAIN | `air`, `land`, `sea`, `space`, `env` |
| PRIKLAUSOMYBĖ | `friendly`, `hostile`, `neutral`, `unknown`, `civ`, `mil` |
| TIPAS | `aircraft`, `vessel`, `vehicle`, `unit`, `sensor`, `uav`, `radar` |

**Pavyzdžiai:**
```
air/asterix/cat48/unknown/aircraft/tracks/v1       ← Giraffe radaras
air/dronuradaras/acoustic/hostile/uav/tracks/v1    ← drono aptikimas
land/dronuradaras/acoustic/neutral/sensor/status/v1 ← akustinis jutiklis
land/asterix/cat34/neutral/radar/status/v1          ← Giraffe radaro vieta
```

### 10.3 Privalomi laukai JSON payload

```json
{
  "_src":    "šaltinio_pavadinimas",
  "_ts":     1234567890.123,
  "lat_deg": 54.6712,
  "lon_deg": 25.2791
}
```

Papildomi rekomenduojami laukai:
```json
{
  "sensor_id":  "unikalus_id",
  "callsign":   "rodomas_pavadinimas",
  "speed_ms":   15.2,
  "heading_deg": 270.0,
  "baro_alt_m": 1500.0
}
```

### 10.4 Pridėjimas į `start.sh`

```bash
# 1. Pridėkite į SERVICES masyvą:
SERVICES=(... <pavadinimas> ...)

# 2. Pridėkite į SVC_CAT:
[<pavadinimas>]="Sensor bridges"

# 3. Pridėkite į SVC_DESC:
[<pavadinimas>]="Trumpas aprašymas"

# 4. Pridėkite į svc_ready():
<pavadinimas>) return 0 ;;    # arba tikrinkite env kintamąjį

# 5. Pridėkite į launch():
<pavadinimas>)
    _start <pavadinimas> bridges/<pavadinimas>_bridge.py
    ;;
```

### 10.5 CoT tipo pridėjimas (jei reikia naujo)

`cot_layer.py` faile, `_TOPIC_COT` žodyne:

```python
"air/**/hostile/uav/**": ("a-h-A-M-F-Q", AIR_STALE_S),   # UAV aptikimas
"land/**/neutral/sensor/**": ("a-n-G-E-S", LAND_STALE_S * 2),  # Akustinis jutiklis
```

---

## 11. Atnaujinimų žurnalas

| Data | Kas pakeitė | Aprašymas |
|---|---|---|
| 2026-06-22 | G. Ndukve | Pradinė sąranka, Giraffe ASTERIX bridge |
| 2026-06-22 | G. Ndukve | `start.sh` interaktyvus paleidiklis |
| 2026-06-22 | G. Ndukve | dronuradaras.lt bridge (akustiniai jutikliai + drono aptikimai) |
| 2026-06-22 | G. Ndukve | Drono aptikimo informacinė kortelė su garso URL |
| 2026-06-22 | G. Ndukve | Giraffe radaro keepalive + pradinė publikacija paleidimo metu |
| | | |

> Pridėkite naują eilutę kiekvieną kartą kai atliekate reikšmingą pakeitimą.

---

*Dokumentas skirtas vidiniam naudojimui. Neskleisti už projekto ribų.*
