# EFDI — Diegimo instrukcija

> **Platforma:** Linux · **Zenoh:** 1.9.0 · **Python:** 3.10+
>
> Techniniai terminai, komandų ir failų pavadinimai pateikiami anglų kalba.

Šis vadovas aprašo sensorių bridge'ų steko diegimą Linux serveryje. Stekas gali
priimti mišrias ASTERIX kategorijas (dabartiniai normalizuojantys vertėjai yra
CAT-010, CAT-020, CAT-021, CAT-034, CAT-048 ir CAT-062), dronuradaras.lt
aptikimus, Link-16, MAVLink ir SitaWare duomenis, tada per vietinę Zenoh
magistralę pateikia juos TAK ir SitaWare klientams.
Pasirinktinai jis gali priimti deklaruotus Lietuvos UTM skrydžius, jei Oro
navigacija suteikia autorizuotą JSON/GeoJSON eksportą. Tai nėra nacionalinis
gyvas civilinis UTM srautas.

---

## 1. Reikalavimai

### Programinė įranga

| Priklausomybė | Minimali versija | Tikrinimas |
| --- | --- | --- |
| Python | 3.10 | `python3 --version` |
| Docker Engine | 24.0 | `docker --version` |
| Docker Compose | 2.20 | `docker compose version` |
| Git | bet kuri | `git --version` |

### Tinklas

| Prievadas / adresas | Kryptis | Paskirtis |
| --- | --- | --- |
| UDP 50010 (`CAT10_PORT`) | į serverį | EFDI CAT-010 susitarimas; gamintojo paskirtį nustatykite taip pat |
| UDP 50020 (`CAT20_PORT`) | į serverį | EFDI CAT-020 susitarimas; gamintojo paskirtį nustatykite taip pat |
| UDP 50021 (`CAT21_PORT`) | į serverį | EFDI CAT-021 susitarimas; gamintojo paskirtį nustatykite taip pat |
| UDP 50034 (`CAT34_PORT`) | į serverį | EFDI CAT-034 susitarimas; radaro paskirtį nustatykite taip pat |
| UDP 50048 (`CAT48_PORT`) | į serverį | EFDI CAT-048 susitarimas; radaro paskirtį nustatykite taip pat |
| UDP 50062 (`CAT62_PORT`) | į serverį | EFDI CAT-062 susitarimas; gamintojo paskirtį nustatykite taip pat |
| UDP multicast `239.2.3.1:6969` | iš serverio | CoT pristatymas į ATAK |
| UDP `<TAK_UDP_PORT>` (numatytasis 8087) | iš serverio | Pasirinktinis tiesioginis CoT į WinTAK/ATAK |
| TCP 7448 | localhost | Vietinis Zenoh router |
| TCP 7447 TLS | iš serverio | Nuotolinis Zenoh router (reikia NetBird) |
| HTTPS 8890 | į serverį | Zenoh administravimo GUI (Caddy TLS, vidinis CA — žr. §10) |
| HTTPS | iš serverio | dronuradaras.lt API |
| HTTPS | iš serverio | Autorizuotas `utm.ans.lt` JSON/GeoJSON eksportas (pasirinktinai) |

ATAK įrenginiai turi būti tame pačiame L2 tinklo segmente kaip serveris (multicast neperžengia VLAN ribų be maršrutizatoriaus konfigūracijos). Tarpvietiniam diegimui naudokite TAK serverį ir `cot-bridge` paslaugą.

### Sertifikatai

Zenoh mTLS sertifikatai išduodami savarankiškai — jokio išorinio CA ar vendor bundle. `scripts/gen-certs.sh <namespace>` sugeneruoja (vieną kartą) EFDI root CA kataloge `compose/certs/efdi/`, tada pasirašo lapo sertifikatą+raktą nurodytam namespace; tas pats root CA naudojamas visiems vėlesniems namespace'ams.

Sugeneruota medžiaga (`efdi-ca-root.pem`, `<NAMESPACE>-cert.pem`, `<NAMESPACE>-key.pem`) saugoma `compose/certs/efdi/` — įtraukta į `.gitignore`, niekada nekomituojama. Kataloge taip pat atskirai laikomi `tak/`, `sitaware/`, `efdi-backbone/` (goat backbone, Desert Bread CA) ir `efdi-ltu/` (LTU sandbox) identitetai — žr. `compose/certs/README.md`. Numatytasis kelias nustatomas `start.sh`; jei norite laikyti jį visai už repozitorijos ribų, perrašykite per `BUNDLE_DIR` faile `compose/.env`.

---

## 2. Diegimas

### 2.1 Repozitorijos klonavimas

```bash
git clone <repo-url> EFDI
cd EFDI
```

### 2.2 Sertifikatų generavimas

```bash
scripts/gen-certs.sh <namespace>   # pvz. scripts/gen-certs.sh 0123456789abcdef0123456789abcdef
```

Tai sukuria:

```text
compose/certs/
├── efdi/                     # šio podo nuosavas EFDI CA + identitetas (neperv.)
├── efdi-ltu/                 # LTU sandbox: asusrog klientinis cert + EFDI LTU Root CA
├── efdi-backbone/                 # goat backbone identitetas (Desert Bread CA)
├── sitaware/                 # SitaWare tiekimo CA ir serverio identitetas
└── tak/                      # TAK Serverio identitetas
```

Pilnas paaiškinimas (kuris cert kuriam fabric) — `compose/certs/README.md`. Visas
katalogas yra `.gitignore`.

LTU dalyvio raktas yra užšifruotas, o jo lapo sertifikato faile nėra tarpinio CA.
Pereinant į šį fabric terminale paleiskite `scripts/connect-ltu.sh`: slaptažodis
įvedamas paslėptai, viešas tarpinis CA patikrinamas pagal prisegtą LTU root, o
pilna kliento grandinė ir neužšifruotas vykdymo raktas rašomi tik į ignoruojamą
`compose/state/zenoh/tls/ltu/`. Zenoh neturi privataus rakto slaptažodžio
nustatymo, todėl negali tiesiogiai naudoti užšifruoto šaltinio rakto.

`<NAMESPACE>` turi sutapti su `PARTNER_NAMESPACE` faile `compose/.env`.

```bash
# Patikrinimas
ls compose/certs/efdi/*.pem
chmod 600 compose/certs/efdi/*-key.pem
```

### 2.3 Python virtualios aplinkos kūrimas

`start.sh` sukuria aplinką automatiškai per pirmą paleidimą. Rankinis kūrimas:

```bash
python3 -m venv compose/venv
compose/venv/bin/pip install -r compose/requirements.txt
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
# Jei nenustatyta, numatytasis kelias yra compose/certs/ (repo viduje, gitignored) —
# perrašykite tik jei norite laikyti sertifikatus visai už repo ribų.
#BUNDLE_DIR=/home/<vartotojas>/efdi-certs

# ── Vykdymo būsena (žurnalai, PID failai, Zenoh config/sertifikatai) ────────
# Jei nenustatyta, numatytasis kelias yra compose/state/ (repo viduje, gitignored).
#POD_STATE_DIR=/var/lib/efdi-pod

# ── Mišrus ASTERIX UDP įėjimas (Giraffe pavyzdys: CAT-34/48) ───────────────
# Vienam bendram ASTERIX UDP srautui išvardykite visas jame esančias
# kategorijas. CAT-010/020/021/062 galima pridėti, kai jos yra sraute.
# Kategorijų vertėjai automatiškai skaitys atskiras neapdorotas Zenoh temas.
ASTERIX_PORT=                  # siūlomas bendro srauto susitarimas: 50000
ASTERIX_BIND=0.0.0.0
ASTERIX_CATEGORIES=34,48
ASTERIX_MULTICAST_GROUP=       # pasirinktinė IPv4 multicast grupė
ASTERIX_MULTICAST_INTERFACE=0.0.0.0
ASTERIX_ALLOW_SOURCE=          # pasirinktinai siuntėjo IPv4 adresas arba CIDR

# Atskiri leidėjų srautai gali toliau naudoti šiuos tiesioginius listener'ius.
CAT10_PORT=50010               # EFDI privatus susitarimas; nustatykite gamintojo išvestį
CAT20_PORT=50020               # EFDI privatus susitarimas; nustatykite gamintojo išvestį
CAT21_PORT=50021               # EFDI privatus susitarimas; nustatykite gamintojo išvestį
CAT34_PORT=50034               # EFDI privatus susitarimas; nustatykite radaro išvestį
CAT48_PORT=50048               # EFDI privatus susitarimas; nustatykite radaro išvestį
CAT62_PORT=50062               # EFDI privatus susitarimas; nustatykite gamintojo išvestį
CAT48_RADAR_LAT=<RADAR_LAT>        # Antenos platuma  (WGS-84 dešimtainiai laipsniai)
CAT48_RADAR_LON=<RADAR_LON>        # Antenos ilguma   (WGS-84 dešimtainiai laipsniai)
CAT48_RADAR_SAC=<SAC>            # ASTERIX šaltinio srities kodas (Source Area Code)
CAT48_RADAR_SIC=<SIC>             # ASTERIX šaltinio identifikacijos kodas
CAT48_RADAR_NAME=Giraffe AMB   # Vardas, rodomas ATAK žemėlapyje
```

> **ASTERIX prievadai:** ASTERIX aprašo pranešimų formatą, bet nenustato
> registruoto tinklo prievado. Radaro ar gateway valdymo sąsajoje kaip paskirtį
> nurodykite EFDI host'ą ir naudokite kategorijų susitarimą: CAT-010→UDP 50010,
> CAT-020→50020, CAT-021→50021, CAT-034→50034, CAT-048→50048, CAT-062→50062.
> Tai EFDI susitarimai, ne patvirtinti gamintojų gamykliniai nustatymai.
> Transportą, leidimą, bendrą ar atskirus srautus ir vendor kadravimą
> patvirtinkite pagal ICD.

Bendram srautui `ASTERIX_PORT` nustatykite tikrą paskirties prievadą.
`asterix-udp` vienas priima UDP srautą ir nepakeistus kadrus publikuoja į
`…/raw/asterix/cat34` bei `…/raw/asterix/cat48`; atskiri vertėjai dekoduoja tik
savo kategoriją. `ASTERIX_PORT` turi pirmenybę tik `ASTERIX_CATEGORIES`
išvardytoms kategorijoms. Nežinomą srautą pirmiausia patikrinkite:

```bash
python3 tools/asterix_probe.py --port 30001
```

### Pasirinktiniai laukai

```bash
# ── TAK serveris (naudokite cot-bridge vietoj cot-udp) ─────────────────────────
TAK_HOST=127.0.0.1
TAK_PORT=8087

# ── SitaWare HQ draugiškų pajėgų sekimas (gaunama REST) ─────────────────────
SITAWARE_URL=https://sitaware.example.com
SITAWARE_USER=
SITAWARE_PASS=
SITAWARE_API_PATH=              # privalomas konkretus diegimo REST resursas

# ── NATO NFFI / ADatP-36 (STANAG 5527) XML jau perduodamas per Zenoh ───────
NFFI_INPUT_TOPIC=               # neprivaloma; numatyta: …/raw/nffi/*

# ── SitaWare HQ NVG importas (HQ NVG eksportas → Zenoh, nvg_bridge) ─────────
SITAWARE_NVG_IMPORT_URL=
SITAWARE_NVG_IMPORT_CA=
SITAWARE_NVG_IMPORT_POLL_S=10

# ── SitaWare HQ (siunčiamas NVG srautas, kurį periodiškai ima HQ) ───────────
SITAWARE_HQ_NVG_ENABLE=0
SITAWARE_HQ_NVG_BIND=127.0.0.1  # HQ pasiekiamas EFDI LAN IP arba 0.0.0.0
SITAWARE_HQ_NVG_PORT=8088
SITAWARE_HQ_NVG_PATH=/nvg
SITAWARE_HQ_NVG_USER=
SITAWARE_HQ_NVG_PASS=
SITAWARE_HQ_NVG_TLS_CERT=
SITAWARE_HQ_NVG_TLS_KEY=

# ── Link-16 JREAP-C ─────────────────────────────────────────────────────────
STANAG5516_PORT=                   # Palikite tuščią, jei Link-16 šaltinio nėra
# Link-16 šiuo metu priima tik JREAP-C UDP; TCP reikia šliuzo kadravimo ICD.

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

```text
╔══════════════════════════════════════════════════════════════════╗
║           EFDI Bridge Launcher  —  select services to start      ║
╚══════════════════════════════════════════════════════════════════╝

  Infrastructure
  ──────────────────────────────────────────────────────────
  [ 1] [✓] zenoh          Zenoh message router (Docker)          ready

  Open-data bridges
  ──────────────────────────────────────────────────────────
  [ 2] [✓] aprs           APRS-IS stations, vehicles, vessels    ready
  [ 6] [✓] meteolt        meteo.lt weather stations              ready

  Sensor bridges
  ──────────────────────────────────────────────────────────
  [ 8] [ ] sitaware       SitaWare HQ dokumentuotas JSON resursas will prompt for address+login
  [ 9] [✓] dronuradaras   dronuradaras.lt drone detection        ready
  [10] [ ] dji-cloud      DJI Cloud API aircraft                 DJI_MQTT_HOST not set
  [11] [ ] asterix-udp    Mixed ASTERIX UDP → raw topics         ASTERIX_PORT not set
  [12] [✓] track-fusion   Radar/ADS-B track correlation          ready

  Protocols
  ──────────────────────────────────────────────────────────
  [13] [✓] asterix-cat10  ASTERIX CAT-010 airport surface        UDP 50010
  [14] [✓] asterix-cat20  ASTERIX CAT-020 Ed.1.11 MLAT           UDP 50020
  [15] [✓] asterix-cat21  ASTERIX CAT-021 Ed.2.7 ADS-B           UDP 50021
  [16] [✓] asterix-cat34  ASTERIX CAT-034 radar service          UDP 50034
  [17] [✓] asterix-cat48  ASTERIX CAT-048 radar targets          UDP 50048
  [18] [✓] asterix-cat62  ASTERIX CAT-062 system tracks          UDP 50062
  [19] [ ] stanag5516     Link-16 JREAP-C datalink               STANAG5516_PORT not set
  [20] [ ] mavlink        MAVLink UAV telemetry                  MAVLINK_PORT not set
  [22] [ ] vmf            VMF MIL-STD-47001C messages            VMF_PORT not set
  [23] [✓] nffi           NATO NFFI XML Zenoh translator         ready
  [24] [ ] sapient        SAPIENT / BSI Flex 335                 will prompt for address
  [25] [ ] stanag4586     STANAG 4586 UAV feed                   will prompt for address
  [26] [ ] mavlink-raw    MAVLink socket → Zenoh raw             MAVLINK_RAW_PORT not set
  [27] [ ] stanag5516-raw Link-16 socket → Zenoh raw             STANAG5516_RAW_PORT not set
  [28] [ ] vmf-raw        VMF socket → Zenoh raw                 VMF_RAW_PORT not set
  [29] [ ] sapient-raw    SAPIENT socket → Zenoh raw             SAPIENT_RAW_PORT not set
  [30] [ ] stanag4586-raw STANAG 4586 socket → Zenoh raw         STANAG4586_RAW_PORT not set

  Zenoh-native translators
  ──────────────────────────────────────────────────────────
  [31] [✓] cap            CAP 1.2 XML → alerts                   ready
  [32] [✓] geojson        GeoJSON/OGC Features → areas           ready
  [33] [✓] spectrum       RF spectrum observations               ready
  [34] [✓] sensor-health  Sensor health/heartbeat records         ready
  [35] [✓] mission-route  UAV routes and corridors                ready

  Output layers
  ──────────────────────────────────────────────────────────
  [36] [✓] cot-udp        CoT → ATAK UDP multicast 239.2.3.1:6969
  [37] [ ] cot-udp-tak    CoT → WinTAK/ATAK UDP unicast
  [38] [✓] cot-bridge        CoT → TAK Server TCP
  [39] [ ] nvg_bridge     SitaWare NVG eksportas → Zenoh       will prompt for URL+login
  [40] [ ] sitaware-hq-nvg EFDI tracks → SitaWare HQ pull feed   SITAWARE_HQ_NVG_ENABLE=0
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
| Giraffe CAT-34/48 + ATAK multicast | `1 17 18 40` |
| Giraffe + drono aptikimai + ATAK | `1 10 17 18 40` |
| Giraffe + SitaWare + ATAK multicast | `1 9 17 18 40` |
| SitaWare NVG eksportas įtraukiamas į Zenoh | `1 43` |
| SitaWare HQ periodiškai ima EFDI takelius | `1 44` |
| Visi parengti šaltiniai + TAK serveris | `a`, tada atžymėkite `40` (cot-udp) |
| Tik radaras be TAK išvesties (derinimui) | `1 12 17 18` |

Procesų PID failai saugomi `$POD_STATE_DIR/.pids/`, žurnalai rašomi į `$POD_STATE_DIR/logs/<paslauga>.log`.

Po sėkmingo paleidimo `start.sh` išsaugo pasirinktų paslaugų sąrašą ir paskutinius TAK/SitaWare adresus faile `$POD_STATE_DIR/launcher-state.env` (teisės 600). Jis taip pat įtraukia visus tuo metu veikiančius PID valdomus procesus. Kitą kartą interaktyviai paleidus rodomas visas atkurtas pasirinkimas ir po penkių sekundžių automatiškai paleidžiamas; per atgalinį skaičiavimą paspauskite `c`, jei norite pakeisti nustatymus. Slaptažodžiai, API raktai ir sertifikatai ten nesaugomi. Aiškiai `compose/.env` nustatyti adresai turi pirmenybę.

---

## 5. ATAK sąranka

### UDP multicast (tas pats tinklų segmentas)

1. **Settings → Network → Multicast** — įjunkite multicast gaviklį
2. Adresų sąraše patikrinkite, kad yra `239.2.3.1:6969`
3. Objektai turi pasirodyti per vieną apklausinėjimo ciklą (≤ 10 s drono aptikimams, ≤ 60 s radaro keepalive)

### TAK serveris (skirtingi tinklai / VLAN)

Nustatykite `TAK_HOST` ir `TAK_PORT` faile `.env`, tada paleidiklyje pasirinkite `cot-bridge` vietoj `cot-udp`.

### Tiesioginis WinTAK/ATAK UDP (be TAK serverio)

Faile `compose/.env` nustatykite `TAK_UDP_HOST=<kliento-ip>` ir `TAK_UDP_PORT=<prievadas>`, pasirinkite `cot-udp-tak`, o kliente sukurkite atitinkamą UDP įvestį. Kliento ugniasienėje leiskite šį gaunamą UDP prievadą. Šiam būdui TAK serverio sertifikatų nereikia.

### SitaWare HQ REST sekimas (pasirinktinis gaunamas adapteris)

`sitaware` naudokite tik tada, kai konkretaus diegimo dokumentacijoje nurodytas suderinamas JSON vienetų resursas ir autentifikavimo būdas. `/rest/v2/*` servlet'o maršrutas nereiškia, kad egzistuoja `/rest/v2/units`; patikrintame HQ 6.22 šis spėjamas resursas grąžina 404.

Palikite `SITAWARE_URL`/`SITAWARE_USER`/`SITAWARE_PASS` tuščius faile `.env` ir paleidiklis paklaus serverio adreso bei prisijungimo (vartotojo vardas, tada paslėptas slaptažodžio laukas) kaskart pasirinkus `sitaware` — arba užpildykite juos `.env` iš anksto, kad praleistumėte klausimą. (Antrą adresą vis tiek galima nustatyti per `SITAWARE_URL_FALLBACK` tiesiogiai `.env` faile, jei tikrai yra atskiras LAN/mesh kelias — interaktyvus klausimas paklaus tik vieno adreso.)

**`.env` laukai:**

```bash
SITAWARE_URL=https://<sitaware-serveris>
SITAWARE_URL_FALLBACK=https://swhq.efdi.ltu:10006 # neprivalomas stabilus mesh-DNS kelias
SITAWARE_USER=<vartotojo vardas>
SITAWARE_PASS=<slaptažodis>
SITAWARE_API_PATH=/<dokumentuotas-resurso-kelias>
SITAWARE_POLL_S=10   # neprivaloma — apklausos intervalas sekundėmis (numatytasis 10)
```

Bridge'as nuskaito MIL-STD-2525B SIDC kodus iš SitaWare ir nukreipia kiekvieną vienetą į teisingą Zenoh temą pagal priklausomybę ir kovos dimensiją:

| SIDC priklausomybė | SIDC dimensija | Zenoh temos kelias | ATAK CoT tipas |
| --- | --- | --- | --- |
| Draugiškas / Laikomas draugišku | Žemė (G) | `…/land/sitaware/c2/friendly/unit/…` | `a-f-G-U-C` |
| Priešiškas | Žemė (G) | `…/land/sitaware/c2/hostile/unit/…` | `a-h-G-U-C` |
| Neutralus | Žemė (G) | `…/land/sitaware/c2/neutral/unit/…` | `a-n-G-U-C` |
| Draugiškas | Oras (A) | `…/air/sitaware/c2/friendly/aircraft/…` | `a-f-A-M-F` |
| Priešiškas | Oras (A) | `…/air/sitaware/c2/hostile/aircraft/…` | `a-h-A-M-F` |
| Draugiškas | Jūra (S) | `…/sea/sitaware/c2/friendly/vessel/…` | `a-f-S-X-L` |
| Priešiškas | Jūra (S) | `…/sea/sitaware/c2/hostile/vessel/…` | `a-h-S-X-L` |
| Draugiškas / Priešiškas / Neutralus / Nežinomas | Kosmosas (P) | `…/space/sitaware/c2/<priklausomybė>/satellite/…` | atitinkamas `a-<priklausomybė>-P` |
| Bet koks | Specialiųjų operacijų pajėgos (F) | `…/land/sitaware/c2/<priklausomybė>/unit/…` | atitinkamas sausumos vieneto tipas |

### NATO NFFI draugiškų pajėgų protokolo vertiklis

`nffi` prenumeruoja pilnus NFFI XML dokumentus, kuriuos partnerio imtuvas ar aptikimo sistema jau paskelbė Zenoh temoje `…/raw/nffi/{source-id}`. Kiekvienas vienetas išverčiamas į `…/land/nato/c2/friendly/unit/{type}/{id}/sapient`. Modulis neturi TCP kliento, klausyklės, galinio taško ar kadravimo logikos. Konkrečiam produktui skirtas prisijungimas turi būti atskirame `_bridge.py`, kai žinomas jo galinis taškas ir ICD.

NFFI draugiškų pajėgų sąveiką aprašo ADatP-36 / STANAG 5527. STANAG 4677 yra atskira išlaipinto kario sistemų sąveikos šeima; 4677 JDSSDM-per-NFFI profiliui reikėtų atskiro, konkrečiam profiliui skirto įgyvendinimo.

**`.env` laukai:**

```bash
NFFI_INPUT_TOPIC=               # neprivaloma; numatyta: …/raw/nffi/*
```

### SitaWare HQ (siunčiama kryptis, NVG)

`sitaware-hq-nvg` prenumeruoja visas EFDI takelių temas ir pateikia jas per HQ NVG importo srautą, todėl SitaWare Headquarters automatiškai mato EFDI takelius — atskiros papildomos integracijos nereikia. Tai priešinga kryptis nei `sitaware`/`nffi` aukščiau (EFDI → SitaWare, ne SitaWare → EFDI).

Palikite `SITAWARE_HQ_NVG_URL`/`SITAWARE_HQ_NVG_USER`/`SITAWARE_HQ_NVG_PASS` tuščius ir paleidiklis paklaus adreso bei prisijungimo pasirinkus `sitaware-hq-nvg`.

**`.env` laukai:**

```bash
SITAWARE_HQ_NVG_URL=https://<sitaware-hq-serveris>:<portas>   # HTTPS privalomas; portas priklauso nuo diegimo
SITAWARE_HQ_NVG_USER=<vartotojo vardas>
SITAWARE_HQ_NVG_PASS=<slaptažodis>
SITAWARE_HQ_NVG_SOURCE=efdi-live    # NVG šaltinio pavadinimas, sukuriamas automatiškai pirmo siuntimo metu
```

### SitaWare Headquarters (siunčiamas NVG srautas, kurį ima HQ)

`sitaware-hq-nvg` yra natyvus Python išvesties procesas, skirtas HQ diegimui. Jis prenumeruoja EFDI takelius, laiko riboto dydžio gyvą momentinę būseną ir pateikia NVG 2.0.2 per tik skaitymui skirtą HTTP(S) adresą. SitaWare Headquarters jį periodiškai ima per **SitaWare Communication → NVG → NVG Import Subscriptions**. Atvirkštinį kelią — HQ NVG eksportą atgal į Zenoh — atlieka `nvg_bridge` (`bridges/nvg_bridge.py`).

Pirmiausia HQ sukurkite sluoksnį:

```text
Suggested Layer Key: tuščia
Name:                EFDI Live Tracks
Path:                /efdi-live
Type:                NVG
Persist tracks:      išjungta
```

`compose/.env` nustatymai:

```bash
SITAWARE_HQ_NVG_ENABLE=1
SITAWARE_HQ_NVG_BIND=0.0.0.0
SITAWARE_HQ_NVG_PORT=8088
SITAWARE_HQ_NVG_PATH=/nvg
SITAWARE_HQ_NVG_USER=<atskiras-srauto-vartotojas>
SITAWARE_HQ_NVG_PASS=<atsitiktinis-stiprus-slaptažodis>
SITAWARE_HQ_NVG_TLS_CERT=/kelias/iki/serverio-cert.pem
SITAWARE_HQ_NVG_TLS_KEY=/kelias/iki/serverio-key.pem
SITAWARE_HQ_NVG_STALE_S=120
SITAWARE_HQ_NVG_MAX_TRACKS=10000
```

Paleiskite `sitaware-hq-nvg` per `./start.sh` arba `./run.sh all`. HQ Windows serveryje pirmą ryšį patikrinkite nespausdindami operacinių duomenų:

```powershell
curl.exe -k -u "<srauto-vartotojas>:<srauto-slaptažodis>" -sS -o NUL `
  -w "HTTP %{http_code} %{content_type}`n" `
  https://<efdi-linux-ip>:8088/nvg
```

`-k` naudokite tik pirminiam ryšio patikrinimui. Normaliam darbui į HQ Windows patikimų šakninių sertifikatų saugyklą įdiekite srautą išdavusią CA.

HQ importo prenumeratos reikšmės:

```text
Subscription Name:         EFDI Live Tracks
Remote Endpoint:           https://<efdi-linux-ip>:8088/nvg
Target Layer:              efdi-live / EFDI Live Tracks
Request NVG periodically:  taip
Polling Interval:          10 sekundžių
Reconnect Delay:           90 sekundžių
Authentication:            įjungta, atskiri srauto prisijungimo duomenys
Pause Subscription:        ne
```

Adresas priima tik GET/HEAD, pagal nutylėjimą reikalauja Basic autentifikavimo, riboja talpyklos dydį, pašalina ilgiau nei `SITAWARE_HQ_NVG_STALE_S` neatnaujintus takelius ir kiekvienam NVG objektui prideda tokios pačios trukmės `TimeSpan`, kad HQ paslėptų pasenusius objektus net nutrūkus srautui. Kai šaltinyje yra duomenų, standartiniai NVG modifikatoriai ir ribotas `ExtendedData` taip pat perduoda šaukinį, registraciją/ICAO, orlaivio ar laivo tipą, squawk, maršrutą, šaltinį, APRS kelią/komentarą, laivo ID bei sensoriaus tapatybę. Attributes kortelė naudoja tą patį domeno formatavimą kaip CoT/TAK, todėl rodomi tvarkingi skyriai, o ne neapdoroti Python laukų pavadinimai. Orlaiviams atskirai pateikiamas barometrinis ir geometrinis aukštis, pagrindinis aukštis metrais/pėdomis/skrydžio lygiu, kilimo ar leidimosi greitis, pasirinktas/tikslinis aukštis, greitis, kryptis, avarinė/autopiloto būsena ir ADS-B kokybės laukai. Stacionarūs APRS taškai ir dronuradaras.lt aptikimai naudoja HQ palaikomą bendrą neutralaus įrangos sensoriaus simbolį, o orų stebėjimai — atskirą neutralaus stacionaraus sensoriaus simbolį, nes HQ 6.22 standartinius METOC simbolius rodo kaip nežinomus. Nei vienas jų neklasifikuojamas kaip karinės žvalgybos vienetas. Ne lokaliame adrese procesas atsisako startuoti per paprastą HTTP, nebent izoliuotai laboratorijai aiškiai nustatyta `SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP=1`. Nenaudokite Keycloak paskyros ar slaptažodžio šiam srautui.

### Piktogramų žinynas

| ATAK piktograma | CoT tipas | Šaltinis |
| --- | --- | --- |
| Mėlynas radaro dubuo (su judėjimo pėdsaku jei mobilus) | `a-f-G-E-S-R` | Giraffe AMB radaro vieta |
| Mėlynas žemės vienetas | `a-f-G-U-C` | SitaWare draugiškas žemės vienetas |
| Raudonas žemės vienetas | `a-h-G-U-C` | SitaWare priešiškas žemės vienetas |
| Geltonas/žalias žemės vienetas | `a-n-G-U-C` | SitaWare neutralus žemės vienetas |
| Mėlynas orlaivis | `a-f-A-M-F` | SitaWare draugiškas oro vienetas |
| Raudonas orlaivis | `a-h-A-M-F` | SitaWare priešiškas oro vienetas |
| Mėlynas laivas | `a-f-S-X-L` | SitaWare draugiškas laivas |
| Raudonas laivas | `a-h-S-X-L` | SitaWare priešiškas laivas |
| Žalia/geltona/raudona sensorių dėžutė (ta pati ikona, keičiasi spalva) | `a-n-G-E-S` / `a-u-G-E-S` / `a-h-G-E-S` | šiuo metu prisijungęs dronuradaras.lt akustinis jutiklis — žalia=neaktyvus, geltona=atvėsta, raudona=aptikimas aktyvus (paskutinės 60s); atsijungę jutikliai pašalinami |
| Balta nežinoma orlaivio | `a-u-A-C-F` | Neklasifikuotas radaro takelis |

> Radaro žymeklio pozicija, greitis ir kursas atnaujinami automatiškai iš gyvo CAT-34 srauto. Mobilioje platformoje ATAK rodys greičio vektorių ir judėjimo taką.

---

## 6. Paslaugų žinynas

> **Temų lygiai.** Žemiau nurodytos `…/tracks/v1` temos yra JSON lygis. Kiekviena
> turi dvi protobuf temas su tuo pačiu įvykiu: `…/tracks/v2` (tipizuota žinutė iš
> protokolo `.proto`) ir `…/tracks/native/v1` (`RawEnvelope` su originaliais
> baitais, tiksliai baitas į baitą). Rinkitės `/v2`; `/native/v1` naudokite, kai
> reikia lauko, kurio EFDI nedekoduoja. `/v1` yra pasenęs ir bus pašalintas.
> Išsamiau: [INTEGRATIONS.md → Egress topic tiers](INTEGRATIONS.md#egress-topic-tiers-v1-v2-nativev1).

| Paslauga | Scenarijus | Zenoh tema (sutrumpinta) | Suaktyvinimas |
| --- | --- | --- | --- |
| `asterix-udp` | `bridges/asterix_udp_bridge.py` | `…/raw/asterix/catNN` | Vienas bendras unicast/multicast UDP srautas |
| `asterix-cat10/20/21/34/48/62` | `protocols/vendors/asterix/cat.py --category NN` | ASTERIX kategorijai skirta normali tema | Tiesioginis UDP/TCP arba viena neapdorota Zenoh kategorijos tema procesui |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | Tik prisijungusių įrenginių apklausa 60 s ir atsijungusių pašalinimas / aptikimų apklausa 10 s |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Konfigūruojama REST apklausa |
| `nffi` | `protocols/random/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Pilni XML dokumentai Zenoh temoje `…/raw/nffi/*` |
| `stanag5516` | `protocols/vendors/stanag/5516.py` | `…/air/stanag_5516/c2/*/aircraft/{type}/{id}/sapient` | Srautinis UDP |
| `mavlink` | `protocols/random/mavlink.py` | `…/air/mavlink/telemetry/*/uav/{type}/{id}/sapient` | Srautinis UDP/TCP |
| `dji-cloud` | `bridges/dji_cloud_api_bridge.py` | `…/air/dji/telemetry/friendly/uav/{type}/{id}/sapient` | DJI šaltiniui skirtas autentifikuotas MQTT 5 tiltas |
| `cot-udp` | `layers/cot_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `cot-bridge` | `layers/cot_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `nvg_bridge` | `bridges/nvg_bridge.py` | SitaWare NVG eksportas → Zenoh | Periodinis |
| `sitaware-hq-nvg` | `layers/sitaware_hq_nvg_feed.py` | Prenumeratorius — visos takelių temos | HQ periodiškai ima NVG būseną |
| `track-fusion` | `bridges/track_fusion_bridge.py` | CAT-48 + CAT-21 prenumeratorius | Įvykio valdomas |

### TAK naudotojai ir SitaWare HQ technika

Aktyvus CoT kelias yra `layers/cot_layer.py`: jis prenumeruoja normalizuotas
Zenoh temas ir siunčia CoT į `cot_layer` paskirties TAK Server. Naudokite TAK
išduotą kliento sertifikatą, kai įjungtas `TAK_TLS=1`. Dabartiniame EFDI
runtime nėra atskiro TAK arba SitaWare CoT priėmimo tilto. Jei konkretus
diegimas teikia NFFI, pilnus XML dokumentus skelbkite į
`…/raw/nffi/{source-id}` per prijungtą Zenoh mazgą.

CoT ir abi SitaWare NVG išvestys naudoja tą pačią scenarijaus priklausomybės
taisyklę: orlaiviai iš nustatytų RU/BY ICAO adresų intervalų bei laivai su RU/BY
MMSI MID žymimi kaip priešiški, o kiti partnerių oro/jūros kontaktai — neutralūs.
Vien šalies pavadinimas nepakeičia trūkstamo arba negaliojančio atsakiklio ID.

## C2 ↔ Zenoh abikryptė prijungimo instrukcija

Įvestis ir išvestis yra atskiros paslaugos. Įjungta TAK ar SitaWare išvestis
automatiškai neįjungia atgalinės krypties.

### 1. Bendra Zenoh pusė

Visi Python adapteriai turi jungtis tik į vietinį maršrutizatorių:

```dotenv
ZENOH_LOCAL_ENDPOINT=tcp/127.0.0.1:7448
```

Kintantis tėvinio ar backbone maršrutizatoriaus adresas rašomas tik į
`ZENOH_FABRIC_ENDPOINT` (arba kelių nuorodų `ZENOH_FABRIC_ENDPOINTS` JSON
masyvą). C2 duomenys publikuojami po
`{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/...`; ACL ir federacijos politika
nustato, kuriems partneriams ši vardų sritis perduodama.

### 2. Zenoh → TAK Server

TAK Server administravimo sąsajoje:

1. Prisijunkite administratoriaus tapatybe ir atverkite **User Management**.
2. Sukurkite atskirą EFDI paslaugos tapatybę, ne žmogaus paskyrą.
3. Priskirkite tik leidžiamas misijos grupes ir reikalingą **IN** kryptį.
4. Per konkretaus diegimo sertifikatų/enrollment funkciją išduokite TAK kliento
   sertifikatą ir paimkite sertifikatą, privatų raktą bei TAK CA grandinę.
5. Failus laikykite tik runtime kataloge ir `compose/.env` įrašykite:

```dotenv
TAK_HOST=<tak-serveris>
TAK_PORT=8089
TAK_TLS=1
TAK_TLS_SERVER_NAME=<tak-serverio-sertifikato-dns-san>
TAK_CERT=/runtime/kelias/tak-client.pem
TAK_KEY=/runtime/kelias/tak-client-key.pem
TAK_CA=/runtime/kelias/tak-ca.pem
```

`./start.sh` pasirinkite `cot-bridge`. Tai turi būti TAK, ne Zenoh, išduotas
sertifikatas. `TAK_HOST` yra stabilus prisijungimo vardas; jei senesnio TAK
serverio sertifikate įrašytas kitas DNS SAN, jį nurodykite
`TAK_TLS_SERVER_NAME`, o ne išjunkite vardo tikrinimą.

### 3. Zenoh → SitaWare HQ

HQ administravime įjunkite licencijuotą NVG REST sąsają, sukurkite ne žmogaus
integracijos paskyrą su tikslinio NVG šaltinio/sluosnio rašymo teise ir iš
įdiegto produkto ICD nukopijuokite tikslų URL:

```dotenv
SITAWARE_HQ_NVG_URL=https://<hq-serveris>/<dokumentuotas-nvg-resursas>
SITAWARE_HQ_NVG_USER=<runtime-vartotojas>
SITAWARE_HQ_NVG_PASS=<runtime-slaptažodis>
SITAWARE_HQ_NVG_SOURCE=efdi-live
```

Pasirinkite `sitaware-hq-nvg`; po pirmo sėkmingo siuntimo HQ žemėlapyje
įjunkite/rodykite `efdi-live`, jei nauji šaltiniai pagal nutylėjimą paslėpti.

HQ atveju pirmiausia paleiskite `sitaware-hq-nvg`, tada SitaWare HQ spauskite
**SitaWare Communication → NVG → NVG Import Subscriptions**, sukurkite
prenumeratą ir įveskite:

```text
Subscription Name:         EFDI Live Tracks
Remote Endpoint:           https://<efdi-adresas>:8088/nvg
Target Layer:              efdi-live / EFDI Live Tracks
Request NVG periodically:  taip
Polling Interval:          10 sekundžių
Reconnect Delay:           90 sekundžių
Authentication:            įjungta; atskira srauto paskyra
Pause Subscription:        ne
```

Jei sluoksnio nėra, prieš tai sukurkite `EFDI Live Tracks` tipo NVG sluoksnį ir
HQ Windows saugykloje patikėkite srauto sertifikato CA.

### 5. SitaWare HQ → Zenoh

HQ administratorius turi įjungti licencijuotą API, sukurti tik skaitymo
integracijos paskyrą ir suteikti prieigą prie konkretaus vienetų/takelių
resurso. Iš įdiegto produkto API/ICD būtina gauti keturis dalykus: bazinį URL,
resurso kelią, autentifikavimo būdą ir atsakymo schemos versiją.

```dotenv
SITAWARE_URL=https://<hq-serveris>
SITAWARE_USER=<runtime-vartotojas>
SITAWARE_PASS=<runtime-slaptažodis>
SITAWARE_API_PATH=/<dokumentuotas-resurso-kelias>
SITAWARE_POLL_S=10
SITAWARE_TLS_VERIFY=1
```

Pasirinkite `sitaware`. Universalaus `/rest/v2/units` resurso nėra; jei
administratorius negali parodyti tikro API ekrano/resurso, jo neatspėkite —
naudokite konkretaus diegimo NFFI arba CoT Gateway.

### 6. C2 duomenų perdavimas partneriams

Nerašykite į kito partnerio vardų sritį. Leiskite pradinę vardų sritį
maršrutizatoriaus/federacijos politikoje, o gavėjo pusėje prenumeruokite ją.
Gavėjo `cot-*` ar `sitaware-hq-nvg` sluoksniai leidžiamas
normalizuotas temas išvers taip pat kaip vietinius sensorių duomenis.

### 8. Operacinių naudotojų testas

Bandymui naudokite keturias atskiras tapatybes ar klientus. Tai operacinės
rolės, o ne Zenoh Admin panelės `superadmin`, `admin` ir `readonly` teisės.

| Rolė | Testo klientas ir veiksmas | EFDI paslaugos | Laukiamas rezultatas |
| --- | --- | --- | --- |
| C2 operatorius | TAK/WinTAK/ATAK arba SitaWare naudotojas stebi sukonfigūruotą CoT išvestį. | `cot-bridge` ir/arba `sitaware-hq-nvg`. | Normalizuoti EFDI takeliai pasiekia autorizuotą C2 sistemą. |
| Sensoriaus leidėjas | Prie vietinio Zenoh router prijungtas imtuvas/aptikimo sistema publikuoja pilnus kadrus ar dokumentus į atitinkamą `…/raw/<protokolas>/<source-id>` temą. Laboratoriniam leidėjui administratorius **Publish Script** lange įrašo tuo metu galiojantį šio leidėjo router adresą ir sugeneruoja skriptą. | Atitinkamas protokolo vertėjas ir C2 išvedimo sluoksniai. | Vertėjas sukuria normalizuotus EFDI takelius; C2 sistemos rodo išvestus žymeklius, ne neapdorotą kadrą. |
| Fabric administratorius | Atskira Zenoh Admin panelės paskyra administruoja tik router/federacijos nustatymus. | Infrastruktūra/Admin UI; sensoriaus ar C2 srautas nereikalingas. | Gali atlikti tik jai priskirtus panelės veiksmus; tai nėra TAK/SitaWare operacinė tapatybė. |

Pirmajam bandymui naudokite TAK išduotą tarnybinę tapatybę `cot-bridge` išvesčiai
ir patikrinkite, kad autorizuota C2 sistema gauna normalizuotus EFDI takelius.

Dabartinė router ACL politika riboja vardų sritį, bet dar nėra susieta su
konkrečiomis asmens rolėmis ar sertifikatų subjektais. Šie keturi klientai
patikrina duomenų srautą ir C2 elgseną, bet neįrodo mažiausių Zenoh teisių tarp
rolių. Tam reikia atskiro vėlesnio sertifikatų subjektų ACL sprendimo.

> **ASTERIX leidimai:** įgyvendinti standartiniai UAP yra CAT-010 1.1,
> CAT-020 1.11, CAT-021 2.7, CAT-034 1.29, CAT-048 1.32 ir CAT-062 1.21.
> Prieš jungdami šaltinį patvirtinkite jo leidimą; kitam ar gamintojo UAP
> būtinas atskirai pasirenkamas dekoderio profilis. Link-16 priima tik UDP,
> nes šliuzo TCP kadravimas dar neaprašytas.

### Zenoh temų schema

```text
{VARDAS_ERDVĖ}/{DOMENAS}/{ŠALTINIS}/{MODALUMAS}/{PRIKLAUSOMYBĖ}/{OBJEKTAS}/{TIPAS}/{ID}/{VAIZDAS}
```

| Laukas | Galimos reikšmės |
| --- | --- |
| `DOMENAS` | `air`, `land`, `sea`, `space`, `env` |
| `PRIKLAUSOMYBĖ` | `friendly`, `hostile`, `neutral`, `unknown`, `civ`, `mil` |
| `TIPAS` | `aircraft`, `vessel`, `vehicle`, `unit`, `sensor`, `uav`, `radar` |

---

## 7. Eksploatacija

### Paslaugų stabdymas

```bash
./stop.sh              # Stabdo visus bridge procesus
./stop.sh layers       # Stabdo tik išvesties sluoksnius (cot-udp, cot-bridge, track-fusion)
```

### Žurnalų stebėjimas

```bash
tail -f $POD_STATE_DIR/logs/asterix.log          # Giraffe radaras — ASTERIX dekodavimas ir publikavimas
tail -f $POD_STATE_DIR/logs/cot-udp.log          # CoT išvestis — patvirtina pristatymą į ATAK
tail -f $POD_STATE_DIR/logs/dronuradaras.log     # Drono aptikimo įvykiai
tail -f $POD_STATE_DIR/logs/track-fusion.log     # Sulieta takelio išvestis
```

### Procesų būsenos tikrinimas

```bash
ls $POD_STATE_DIR/.pids/                                          # Veikiančių paslaugų sąrašas
kill -0 $(cat $POD_STATE_DIR/.pids/asterix.pid) && echo ok        # Konkretaus proceso tikrinimas
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
ls $EFDI_CERT_DIR/*.pem
```

Jei `compose/.env` buvo įkeltas paprastu `source compose/.env`, kintamieji neeksportuojami į vaikininius procesus. Naudokite `./start.sh` (kuris tai tvarko automatiškai), arba:

```bash
set -a && source compose/.env && set +a
```

### ATAK nerodo jokių objektų

```bash
# 1. Patikrinkite ar cot-udp veikia
kill -0 $(cat $POD_STATE_DIR/.pids/cot-udp.pid) && echo veikia

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

### SitaWare vienetai nerodomi ATAK

**1. Patikrinkite ar bridge'as veikia ir apklausinėja:**

```bash
tail -f $POD_STATE_DIR/logs/sitaware.log
# Laukiamas: "SitaWare poll: N units published" kas SITAWARE_POLL_S sekundžių
```

**2. Patikrinkite kredencialus ir adresą:**

```bash
curl -s -u "$SITAWARE_USER:$SITAWARE_PASS" "$SITAWARE_URL/..." | python3 -m json.tool | head -20
```

**3. SIDC kodo problema — vienetas rodomas su neteisinga piktograma arba nerodomas:**

SitaWare vienetai be galiojančio 15 simbolių SIDC kodo nukreipiami į `…/land/sitaware/c2/unknown/unit/…` ir rodomi kaip nežinomi žemės vienetai (`a-u-G-U-C`). Patikrinkite SIDC reikšmę žurnale:

```bash
grep "sidc=" $POD_STATE_DIR/logs/sitaware.log | head -10
```

### EFDI takeliai nerodomi SitaWare HQ

```bash
tail -f $POD_STATE_DIR/logs/sitaware-hq-nvg.log
curl -u "$SITAWARE_HQ_NVG_USER:$SITAWARE_HQ_NVG_PASS" \
  -o /dev/null -w '%{http_code} %{content_type}\n' \
  "http://127.0.0.1:${SITAWARE_HQ_NVG_PORT:-8088}${SITAWARE_HQ_NVG_PATH:-/nvg}"
```

Laukiamas atsakymas — `200 application/xml`. HQ NVG valdyme patikrinkite, kad prenumerata nesustabdyta, prisijungusi, kreipiasi į EFDI hosto adresą (ne HQ adresą) ir naudoja `efdi-live / EFDI Live Tracks` sluoksnį. Jei vietinis testas grąžina `200`, o HQ neprisijungia, tikrinkite maršrutizavimą, Windows/Linux ugniasienes ir sertifikato patikimumą, o ne NVG konvertavimą.

### Keli to paties proceso egzemplioriai

Atsiranda paleidus `start.sh` du kartus be sustabdymo:

```bash
pkill -f "_bridge\.py\|cot_layer\|track_fusion"
rm -f $POD_STATE_DIR/.pids/*.pid
./start.sh
```

### Giraffe radaro piktograma dingsta iš ATAK

`asterix` bridge'as kas 60 s publikuoja keepalive nepriklausomai nuo takelio aktyvumo. Jei piktograma dingsta — bridge'as sustojo:

```bash
tail -20 $POD_STATE_DIR/logs/asterix.log | grep -E "keepalive|startup|error"
```

---

## 9. Naujo bridge kūrimas

### Failo struktūra

```python
# compose/bridges/<pavadinimas>_bridge.py

import json, os, time
import zenoh

ORG       = "<YOUR_NAMESPACE>"
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", os.path.dirname(__file__))

# make_config() nukopijuokite iš bet kurio esamo bridge — visiems ji identiška.

def main():
    session = zenoh.open(make_config())
    topic = f"{ORG}/air/<šaltinis>/<modalumas>/unknown/aircraft/{tipas}/{id}/sapient"
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

## 10. Zenoh administravimo GUI

Web GUI podui valdyti be SSH: routerio ir sistemos būsena, bridge'ų ir sluoksnių paleidimas/stabdymas, konfigūracijos ir prisijungimo duomenų redagavimas, sertifikatų įstaiga (CA) ir prekės ženklas. Naudoja modernią minimalistinę tamsią temą (vientisos tamsios kortelės, savarankiškai talpinamas Inter šriftas, žalsvas akcentas), kurią superadmin gali perbrandinti per WebUI Settings.

Skydelio "Connected routers" panelė rodo kiekvieną kitą zenoh egzempliorių (router ar peer), su kuriuo šis routeris turi gyvą ryšį — gaunama iš routerio pačio admin space, tas pats šaltinis kaip prenumeratorių/queryable temų sąrašai, tad papildomos konfigūracijos nereikia be jau esamos `pod-admin-introspect` ACL taisyklės.

### Panelės apžvalga

Panelė veikia adresu `http://127.0.0.1:8890` (arba pod'o adresu) ir valdo vieną
EFDI podą. Jei tik pradedate, tai orientacinė apžvalga; išsamesni poskyriai
(*Runtime Control skydelis*, *Rolės*, *Konfigūracijos skirtuko laukai*) pateikti
žemiau.

**Pirmojo karto eiga.** Prisijunkite administratoriaus paskyra, sukurta diegimo
metu (pirmą kartą gali paprašyti pakeisti slaptažodį). Atsidaro Dashboard —
patikrinkite, ar podas sveikas. Toliau kasdienė eiga paprasta: **Runtime Control**
paslaugoms paleisti, **Config** joms sukonfigūruoti, **Dashboard** būsenai
patikrinti.

**Puslapiai** (šoninio meniu tvarka):

- **Dashboard** — būsenos apžvalga: CPU/RAM/disko/veikimo laiko/apkrovos/tinklo
  rodikliai, pagrindinių paslaugų būsena, federacijos peržiūra ir gyvi Zenoh
  rodikliai (prenumeratoriai, queryable, saugyklos, prijungti routeriai).
- **Network** — *Managed Router Network*. Aktualu tik kai šis podas yra HQ/šaknis,
  valdanti filialų routerius: topologija, tiesioginiai vaikai, pasitikėjimo būsena
  ir **Apply trust ACL**. Vienas savarankiškas podas rodys „0 direct children" ir
  gali šį puslapį ignoruoti.
- **Config** — du sluoksniai viename puslapyje. **Zenoh Config** redaguoja patį
  routerį (vietiniai prievadai, fabric endpoint'ai ir sertifikato tapatybė, vardų
  sritis, ryšio politika — žr. *Konfigūracijos skirtuko laukai* žemiau).
  **Integration Settings** redaguoja paslaugų aplinką be SSH: TAK host/port,
  SitaWare HQ NVG importas/tiekimas ir REST keliai, UTM, MQTT/SensorThings,
  ASTERIX prievadai, DJI. Slaptažodžiai — tik įrašomi. Išsaugoję perkraukite
  atitinkamą paslaugą per Runtime Control.
- **Runtime Control** — paleisti / stabdyti / perkrauti kiekvieną bridge (jutiklio
  įvestį), protokolą (vertėją) ir sluoksnį (C2 išvestį); filtruoti pagal kategoriją
  ar rolę; pasirinkti paleidimo rinkinį (įsimenamas tarp perkrovimų); skaityti
  žurnalus vietoje. Žr. *Runtime Control skydelis* žemiau.
- **Changes** — Zenoh konfigūracijos revizijų istorija ir jų rezultatas
  (applied / rejected / rolled-back) — įrašomas rezultatas ir maiša, ne pati
  konfigūracija.
- **Admin Users** — panelės paskyrų ir jų rolių valdymas (tik superadmin).
- **Certificates** — *Certificate Authority*: kurti vienkartinius kvietimus
  vaikams federacijai (vaikas pats susigeneruoja raktus ir pateikia tik CSR) ir
  stebėti sertifikatų galiojimą. Savarankiškiems podams nereikia.
- **Publish Script** — sudaryti Zenoh publish komandą testavimui ar duomenų
  tiekimui, be rankinio raktų išraiškų rašymo.
- **Shell** — apribotas, audituojamas shell'as į routerio konteinerį diagnostikai.
- **Logs** — gyvas bet kurios paslaugos žurnalo srautas.
- **Audit Logs** — privilegijuotų veiksmų įrašas (konfigūracija, prekės ženklas,
  naudotojai, prisijungimai).
- **WebUI Settings** (paskyros meniu viršuje dešinėje) — **Branding**
  (organizacijos pavadinimas, akcento spalva ir logotipas — superadmin),
  **Appearance** (eilučių animacijos, tankios eilutės), **Live behavior**
  (atnaujinimo intervalas) ir šviesios/tamsios temos jungiklis.

**Dvi apsaugos, kurias galite sutikti — abi tyčinės.** EFDI atsisako veiksmų,
kurie galėtų tyliai sugriauti pasitikėjimo ribas:

1. *„Apply trust ACL" užblokuota — nevaldomas fabric uplink.* Šaknis vis dar
   jungiasi prie išorinio fabric peer, kuris nėra užregistruotas vaikas. Arba
   užregistruokite tą peer, arba pašalinkite uplink per **Config → Fabric
   endpoints → „Root / no upstream"**.
2. *Valdomo routerio ištrynimas išjungtas.* Vietoje to jį decommission/quarantine,
   kad pašalintas routeris negrįžtų kaip nepatikimas peer.

### Nustatymas

Pridėkite į `compose/.env` (pilną bloką žr. `compose/.env.example`):

```bash
ZENOH_ADMIN_DB_USER=zenoh_admin
ZENOH_ADMIN_DB_PASSWORD=<atsitiktinis>
ZENOH_ADMIN_DB_ROOT_PASSWORD=<kita-atsitiktine-reiksme>
ZENOH_ADMIN_DB_PORT=3307                # ne numatytasis: nesikerta su MariaDB/MySQL 3306 prievadu
ZENOH_ADMIN_SECRET_KEY=<openssl rand -hex 32>
ZENOH_ADMIN_FIRST_USER=admin
ZENOH_ADMIN_FIRST_PASS=<nustatykite vieną kartą, po pirmo prisijungimo galite ištrinti>
```

`ZENOH_ADMIN_FIRST_PASS` sukuria pirmą `superadmin` paskyrą tik jei ji dar neegzistuoja — po pirmo prisijungimo šį kintamąjį saugu vėl palikti tuščią (paskyra išlieka MariaDB duomenų bazėje).

#### Vienkartinis perkėlimas iš PostgreSQL

Atnaujinimas palieka seną `${POD_STATE_DIR}/zenoh-admin/pgdata` katalogą ir
MariaDB duomenis kuria `${POD_STATE_DIR}/zenoh-admin/mariadb` kataloge. Sustabdyk
`zenoh-admin` ir seną `zenoh-admin-db`, pasidaryk pod būsenos bei `compose/.env`
atsarginę kopiją,
įrašyk `ZENOH_ADMIN_DB_ROOT_PASSWORD`, o seną `ZENOH_ADMIN_DB_PORT=5433` pakeisk
į `ZENOH_ADMIN_DB_PORT=3307`. Perkėlimo metu senoji PostgreSQL bazė laikinai
naudoja 55433 prievadą. Tada paleisk `INSTALL.md` skyriuje „One-time PostgreSQL
migration“ pateiktą importavimo komandą. Importuotojas atsisako dirbti
su netuščia MariaDB, kopijuoja lenteles viena transakcija ir prieš patvirtindamas
patikrina eilučių skaičius. `pgdata` netrink, kol nepatikrinai prisijungimo,
pasitikėjimo, federacijos, išvaizdos ir audito istorijos. Tai vieno mazgo MariaDB
perkėlimas; Galera klasteris diegiamas atskirai.

### Paleidimas

```bash
cd compose
docker compose up -d zenoh-admin-db zenoh-admin zenoh-admin-proxy
```

Tada atidarykite `https://<pod-host>:8890`.

Pats skydelis (`zenoh-admin`) klausosi tik `127.0.0.1:8895` — tiesiogiai nepasiekiamas. Caddy reverse proxy (`zenoh-admin-proxy`) baigia tikrą TLS ant `:8890` naudodamas savo vidinį CA (`local_certs` + `tls internal`, be išorinio ACME/CA priklausomybės), išsaugotą `zenoh_admin_caddy_data` tome, kad CA išliktų po perkrovimų. Naršyklė pirmą kartą parodys savarankiškai pasirašyto sertifikato įspėjimą — pasitikėkite Caddy vidiniu CA (arba priimkite įspėjimą), kad tęstumėte; čia sąmoningai nėra viešo sertifikato, nes šis skydelis nėra skirtas interneto prieigai.

### Runtime Control skydelis

TAK stiliaus **Runtime Control** skydelyje `superadmin` gali vienoje vietoje:

- paleisti, sustabdyti, perkrauti ir peržiūrėti visų registruotų bridge'ų, protokolų vertėjų, raw ingress bei TAK/SitaWare išvesties sluoksnių log'us;
- keisti endpoint'us, portus, Zenoh temas, API URL ir protokolų nustatymus;
- rodyti ir keisti papildomus konkretaus diegimo `.env` laukus, kurie jau yra pod'e;
- įvesti naudotojų vardus, slaptažodžius, API raktus ir token'us, nerodant jau išsaugotų paslapčių.

Native procesai lieka valdomi host PID failais. `start.sh` ir `run.sh all`
palaiko `admin-control` procesą tik loopback sąsajoje, porte 18896. API kviečia
tuos pačius launcher skriptus, todėl nekuriamas atskiras Docker konteineris
kiekvienam integracijos tipui. Papildomam apsaugos sluoksniui `compose/.env`
galima nustatyti `EFDI_CONTROL_TOKEN`. Po pakeitimo perkraukite paveiktą
servisą, kad jis perskaitytų naują aplinką.

Paleidus `./dev.sh up`, laikinas control agent automatiškai persikelia į 18896
jei kūrimo/numatytasis 8896 jau užimtas, o dev API nukreipiamas į pasirinktą
portą.

### Rolės

| Rolė | Skydelis | Konfigūracija (peržiūra) | Konfigūracija (redagavimas + routerio perkrovimas) | Admin vartotojai |
| --- | --- | --- | --- | --- |
| `readonly` | ✓ | | | |
| `admin` | ✓ | ✓ | | |
| `superadmin` | ✓ | ✓ | ✓ | ✓ |

Išsaugant konfigūracijos pakeitimą, jis pirma patikrinamas kaip galiojantis JSON5, tada įrašomas į primontuotą `${POD_STATE_DIR}/zenoh/config.json5`, ir tik tada perkraunamas `zenoh-router` konteineris — sintaksės klaida atmetama dar prieš paliečiant diską.

### Konfigūracijos skirtuko laukai

Konfigūracijos skirtukas rodo struktūrizuotus laukus, ne žalią JSON5 — kiekvienas išsaugojimas iš naujo atvaizduoja `host/zenoh-router.json5.tmpl` (tą patį šabloną, kurį naudoja `first-boot.sh`) su žemiau esančiomis reikšmėmis, todėl išsaugota konfigūracija niekada negali nukrypti nuo šablono struktūros.

| Laukas | Poveikis |
| --- | --- |
| Vietinis mTLS portas | Tinklui skirtas listen portas skirtas bridges, audit-sink (numatytasis 7447) |
| Vietinis TCP portas | Plaintext, tik vietinis listen portas bridge'ams + šiam GUI (numatytasis 7448) |
| Fabric endpoint | Partnerio / kolegos endpoint, į kurį šis pod'as skambina — įvedamas kaip atskiri Host + Port laukai (schema visada `tls`, niekada nerodoma); yra vieno paspaudimo šablonai anksčiau naudotiems endpoint'ams |
| Partner namespace | Šio pod'o first-party publish/subscribe prefiksas (jo slotas) — **keičiant šią reikšmę, kita fabric pusė taip pat turi leisti naują reikšmę savo ACL, kitaip publikacijos tyliai nustoja pasiekti** |
| Inbound namespace | Bilateral prefiksas, kurį fabric publikuoja Į šį pod'ą |
| Verify name on connect | Pagal nutylėjimą išjungta — gateway sertifikato SAN susietas su tinklo IP, ne su skambinamu DNS vardu; įjungus gali sulūžti fabric ryšys |
| Storage plugin loading | Išjungus, nauji subscriberiai per `get()` nebegauna paskutinės žinomos reikšmės — publish/subscribe vis tiek veikia |

Trys sąmoningai **nerodomi** GUI (per lengva užrakinti visus klientus, įskaitant patį GUI, jei sukonfigūruota blogai): `access_control.enabled`, `default_permission`, `enable_mtls`. Jei reikia, redaguokite juos tiesiogiai `zenoh/config.json5` faile.

### Izoliuotas testinis routeris

Lokaliam pub/sub testavimui, neliečiant tikro pod'o ar jo fabric ryšio: `zenoh-router-test`, už `test` compose profilio (niekada nepasileidžia kartu su likusiu stack'u).

```bash
cd compose
docker compose --profile test up -d zenoh-router-test
```

Konfigūracija yra `${POD_STATE_DIR}/zenoh-test/config.json5` — tie patys sertifikatai/namespace/ACL kaip tikro routerio, bet skirtingi portai (`7457` mTLS / `7458` TCP, vietoj `7447`/`7448`) ir **be `connect.endpoints`** (niekada neskambina fabric). Saugu palikti veikiantį kartu su tikru routeriu — niekas nesikerta.

---

## 11. Tęstinė integracija (CI)

`.github/workflows/ci.yml` paleidžiamas kas kartą pushinant/darant PR į `main`:

| Job | Tikrina |
| --- | --- |
| `shellcheck` | Tikrina kiekvieną `.sh` skriptą repo'je (`-S warning`) |
| `compose-validate` | Patvirtina, kad `compose/docker-compose.yml` yra validus YAML |
| `bridge-syntax` | `py_compile` kiekvienam failui `compose/bridges/`, `compose/protocols/` ir `compose/layers/` |
| `zenoh-admin-frontend` | `pnpm type-check` + `pnpm build` `compose/zenoh-admin/ui` |
| `docker-build` | Sukuria `compose/Dockerfile` ir `compose/zenoh-admin` image'us, be push |

Tai pagauna sintaksės klaidas, TypeScript klaidas ir Dockerfile lūžimus prieš merge — **nepaleidžia** pačių bridge'ų (dauguma reikalauja tikrų API raktų/tinklo prieigos, kurios CI neturi).

---

## Pakeitimų žurnalas

| Data | Pakeitimas |
| --- | --- |
| 2026-06-14 | Pradinis commit — šakota iš oficialaus `efdi-moon-pod-main` saugyklos |
| 2026-06-15 | Baziniai bridge adapteriai sujungti; saugyklos struktūra nustatyta; pridėtas README |
| 2026-06-16 | `airplanes.live` bridge: regioniniai ADS-B ir pasauliniai kariniai orlaiviai |
| 2026-06-16 | Protocol Buffer takelių aprašai; dabar sutartys laikomos šalia vertėjų `compose/protocols/` kataloge |
| 2026-06-17/18 | Kokybės gerinimai: bridge'ų stabilumas, sluoksnių dublikatų filtravimas, takelio suliejimo derinimas |
| 2026-06-18 | ASTERIX pilno dekodavimo projektavimo specifikacijos dokumentas |
| 2026-06-19/22 | Papildomi bridge ir sluoksnių gerinimai; Giraffe ASTERIX bridge užbaigtas |
| 2026-06-22 | `dronuradaras.lt` bridge: akustinių jutiklių tinklas ir drono aptikimo įvykiai |
| 2026-06-22 | CoT DETECTION sekcija su garso įrašo URL ATAK pastabų lauke |
| 2026-06-22 | Radaro vietos žymeklis: publikacija paleidimo metu + 60 s keepalive, kad ATAK neprarastų žymeklio |
| 2026-06-23 | Saugumo patikrinimas: pašalintas koduotas API raktas iš `register_topics.sh`; raktas perkeltas į `$EFDI_PORTAL_KEY` aplinkos kintamąjį |
| 2026-06-23 | Saugumas: asmeninis namespace UUID, el. paštas, IP ir pardavėjo identifikatorius pašalinti iš visų sekamų failų; bridge'ai skaito `PARTNER_NAMESPACE` iš aplinkos |
| 2026-06-23 | Saugumas: `compose/.env` ir `register_topics.sh` pridėti į `.gitignore` — kredencialai lieka tik lokaliai |
| 2026-06-23 | Saugumas: neriboto HTTP kūno skaitymas `rest-http/bridge.py` apribotas iki 10 MB |
| 2026-06-23 | Dokumentacijos atnaujinimas: `INSTALL.md` (anglų), `DIEGIMAS.md` (lietuvių), `README.md` perrašytas kaip architektūros apžvalga |
| 2026-06-23 | ASTERIX CAT-34 I034/120 dekoderis: radaras pats praneša WGS-84 poziciją iš gyvo srauto — rankinis koordinačių nustatymas nebereikalingas |
| 2026-06-23 | Mobiliojo radaro palaikymas: pozicija, greitis ir kursas gaunami iš nuoseklių I034/120 pranešimų; ATAK rodo judėjimo taką ant transporto priemonėje montuojamų radarų |
| 2026-07-05 | Zenoh administravimo GUI: FastAPI + React panelė routerio būsenai ir `config.json5` redagavimui, stiliaus pavyzdys — TAK admin panelė |
| 2026-07-05 | Ištaisytas `zenoh-router.json5.tmpl` neatitikimas: šablone trūko plaintext `tcp/0.0.0.0:7448` vietinio listen endpoint, kurį gyva konfigūracija jau turėjo |
| 2026-07-05 | Zenoh admin GUI konfigūracijos skirtukas: pridėti `verify_name_on_connect` ir storage plugin loading perjungikliai; fabric endpoint dabar įvedamas kaip atskiri Host/Port laukai su vieno paspaudimo šablonais, vietoj žalio `tls/host:port` teksto |
| 2026-07-05 | Zenoh admin GUI: pridėtas `/api/health` (CPU/RAM/diskas/uptime/apkrova/tinklas/sertifikatų galiojimas, TAK admin panelės stiliaus) skydelyje |
| 2026-07-05 | Ištaisyta SPA routing klaida: tiesioginis navigavimas/refresh/back mygtukas į bet kurį GUI sub-route (`/config`, `/admin-users`) grąžindavo žalią JSON 404 vietoj programos užkrovimo — fallback kodas gaudė `fastapi.HTTPException`, bet `StaticFiles.get_response` meta `starlette.exceptions.HTTPException` (kitą, tėvinę klasę), todėl gaudymas niekada nesutapo |
| 2026-07-05 | Pridėtas izoliuotas `zenoh-router-test` servisas (`test` compose profilis) lokaliam pub/sub testavimui, neliečiant tikro pod'o ar jo fabric ryšio |
| 2026-07-05 | Pašalintas `gps-ew` bridge (GPSJam pagrindu) — gpsjam.org neturi viešo API savo apdorotiems duomenims, todėl šis bridge niekada realiai neveikė; pašalintas iš `start.sh` ir `cot_layer.py`, o ne paliktas tyliai sulūžęs |
| 2026-07-05 | Ištaisyti dubliuoti takeliai SitaWare tarp šaltinių/pod'ų: `nato_nvg_layer.py` `_uid()` funkcijoje šaltinio pavadinimas buvo įtraukiamas į takelio ID (skirtingai nuo jau teisingos `cot_layer.py` versijos), todėl tas pats orlaivis iš dviejų šaltinių gaudavo du skirtingus SitaWare takelius |
| 2026-07-05 | `dronuradaras_bridge.py` buvo pakeistas publikuoti visus registruotus jutiklius su pozicija; šį sprendimą pakeitė žemiau aprašyta 2026-07-15 tik prisijungusių jutiklių taisyklė |
| 2026-07-05 | Pridėtas `.github/workflows/ci.yml`: tikrina bridge'ų/sluoksnių sintaksę, type-check + build zenoh-admin frontend'ui, sukuria abu Docker image'us kas kartą pushinant/darant PR |
| 2026-07-05 | Pridėti `shellcheck` ir `compose-validate` CI job'ai; ištaisytas vienintelis realus radinys (`compose/rebuild.sh` trūko `cd ... \|\| exit`) ir nutildytas klaidingas teigiamas (`SC2163` dėl sąmoningo "export pagal dinaminį vardą" idiomo `start.sh`/`stop.sh`/`run.sh`) |
| 2026-07-10 | Ištaisyta: `nato_nvg_layer.py` naudojo tuos pačius aplinkos kintamuosius kaip gaunamas `sitaware_bridge.py` (`SITAWARE_URL`/`USER`/`PASS`) — pervadinta į `SITAWARE_NVG_*`, nes HQ (gaunama) ir Edge (siunčiama) paprastai yra skirtingi serveriai/prisijungimo duomenys |
| 2026-07-10 | Paslaugos `nffi` ir `sitaware-nvg` prijungtos prie `start.sh` — abi egzistavo repozitorijoje, bet niekada nebuvo registruotos kaip paleidžiamos paslaugos |
| 2026-07-10 | `start.sh`: `sitaware` ir `sitaware-nvg` dabar paklausia vartotojo vardo ir paslėpto slaptažodžio paleidimo metu (anksčiau buvo klausiama tik serverio adreso; prisijungimo duomenys turėjo būti iš anksto nustatyti `.env`) |
| 2026-07-10 | Zenoh admin GUI: pridėta "Connected routers" panelė — nuskaito `router/transport/unicast/*` įrašus, jau esančius admin space užklausoje, naudojamoje prenumeratorių/queryable sąrašams, jokios naujos ACL ar užklausos nereikia |
| 2026-07-10 | Zenoh admin GUI: perkeltas TAK-hud vizualinis stilius (`hud-card`, `hud-frame`/reticle kampai, `hud-glass` šoninis meniu, `hud-grid-bg` fonas, akcento švytėjimo mygtukai, laipsniškas atsiradimo animacijos) į `index.css`/`Layout.tsx`/skydelį |
| 2026-07-15 | `dronuradaras_bridge.py` dabar publikuoja tik įrenginius, kurių API būsena yra `is_online=true`; atsijungę įrenginiai siunčia pašalinimo įvykį, todėl CoT, SitaWare Edge ir HQ NVG talpykla ištrina senus žymeklius |
| 2026-07-17 | Pridėti deterministiniai ASTERIX kategorijų listener'ių susitarimai: CAT-010/020/021/034/048/062 pagal nutylėjimą naudoja UDP 50010/50020/50021/50034/50048/50062; tai EFDI, ne gamintojų numatytieji prievadai |
| 2026-07-17 | Pridėti Zenoh-native CAP, GeoJSON/OGC, spektro, jutiklių būklės, misijų maršrutų ir neapdoroto įėjimo vertimo keliai |
| 2026-07-17 | Saugumo atnaujinimas: atnaujintas Vite, prisegti/atnaujinti Compose image'ai, atnaujinti Python image'ų OS paketai, o autentifikuoti SitaWare/UTM endpoint'ai apriboti iki HTTPS |
| 2026-07-18 | Pridėtas TAK stiliaus Runtime Control: host bridge/protokolų/sluoksnių lifecycle veiksmai, apriboti log'ai, endpoint/temų/portų redagavimas, write-only kredencialai, localhost admin-control agent ir veikiantis Vite dev stack su suderintais API/Vite portais |

---

*Skirta vidiniam naudojimui — neskleisti už projekto ribų.*
