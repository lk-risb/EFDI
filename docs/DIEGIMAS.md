# EFDI — Diegimo instrukcija

> **Platforma:** Linux · **Zenoh:** 1.9.0 · **Python:** 3.10+
>
> Techniniai terminai, komandų ir failų pavadinimai pateikiami anglų kalba.

Šis vadovas aprašo sensorių bridge'ų steko diegimą Linux serveryje. Stekas gali
priimti mišrias ASTERIX kategorijas (dabartiniai normalizuojantys vertėjai yra
CAT-010, CAT-020, CAT-021, CAT-034, CAT-048 ir CAT-062), dronuradaras.lt
aptikimus, SAPIENT, STANAG 4586/4609 ir SitaWare duomenis, tada per vietinę Zenoh
magistralę pateikia juos TAK ir SitaWare klientams.
Pasirinktinai jis gali priimti deklaruotus Lietuvos UTM skrydžius, jei Oro
navigacija suteikia autorizuotą JSON/GeoJSON eksportą. Tai nėra nacionalinis
gyvas civilinis UTM srautas.

---

> **Pradedate nuo tuščio serverio, kuriame dar nieko neįdiegta?** Pirmiausia
> perskaitykite [`PARUOSIMAS.md`](PARUOSIMAS.md) — jame žingsnis po žingsnio
> aprašomas Docker, Python, git ir NetBird diegimas nuo nulies Ubuntu arba
> RHEL šeimos Linux sistemoje. Praleiskite, jei tai jau įdiegta ir veikia.

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
├── efdi/                     # vidinis maršrutizatoriaus identitetas + EFDI CA
├── efdi-backbone/            # Backbone: cert.pem, key.pem, ca-roots.pem
├── efdi-ltu/                 # LTU sandbox: client.pem, client.key, ca.crt
├── sitaware/                 # SitaWare tiekimo CA ir serverio identitetas
└── tak/                      # TAK Serverio identitetas
```

Pilnas paaiškinimas (kuris cert kuriam fabric) — `compose/certs/README.md`. Visas
katalogų išdėstymas ir README failai yra sekami, tačiau visi sertifikatai,
privatūs raktai, grandinės ir sugeneruoti kredencialai yra `.gitignore`.

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

# ── Mišrus ASTERIX UDP įėjimas (radarai, pvz. VERA-NG: CAT-34/48) ──────────
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
CAT34_RADAR_LAT=               # Vieno radaro atsarginė reikšmė; pirmenybė I034/120
CAT34_RADAR_LON=               # Vieno radaro atsarginė reikšmė; pirmenybė I034/120
CAT34_RADAR_NAME=              # Tuščia = atskiri RADAR SACx/SICy vardai; nustatykite vienam radarui
CAT34_RADAR_RANGE_M=           # Operatoriaus patvirtintas maksimumas; pirmenybė I034/100
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

UDP 50000 yra bendras neapdorotų UDP duomenų įėjimas. Jis išsaugo kiekvieną
datagramą `…/raw/udp/ingress` temoje ir papildomai nukreipia vienareikšmiškai
atpažintus ASTERIX kadrus į `…/raw/asterix/catNN`. UDP 50034 ir 50048 lieka
atskiri CAT-034 ir CAT-048 prievadai. Nežinomą srautą pirmiausia patikrinkite:

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

Adresas priima tik GET/HEAD, pagal nutylėjimą reikalauja Basic autentifikavimo, riboja talpyklos dydį, pašalina ilgiau nei `SITAWARE_HQ_NVG_STALE_S` neatnaujintus takelius ir kiekvienam NVG objektui prideda tokios pačios trukmės `TimeSpan`, kad HQ paslėptų pasenusius objektus net nutrūkus srautui. Kai šaltinyje yra duomenų, standartiniai NVG modifikatoriai ir ribotas `ExtendedData` taip pat perduoda šaukinį, registraciją/ICAO, orlaivio ar laivo tipą, squawk, maršrutą, šaltinį, laivo ID bei sensoriaus tapatybę. Attributes kortelė naudoja tą patį domeno formatavimą kaip CoT/TAK, todėl rodomi tvarkingi skyriai, o ne neapdoroti Python laukų pavadinimai. Orlaiviams atskirai pateikiamas barometrinis ir geometrinis aukštis, pagrindinis aukštis metrais/pėdomis/skrydžio lygiu, kilimo ar leidimosi greitis, pasirinktas/tikslinis aukštis, greitis, kryptis, avarinė/autopiloto būsena ir ADS-B kokybės laukai. dronuradaras.lt aptikimai naudoja HQ palaikomą bendrą neutralaus įrangos sensoriaus simbolį, o orų stebėjimai — atskirą neutralaus stacionaraus sensoriaus simbolį, nes HQ 6.22 standartinius METOC simbolius rodo kaip nežinomus. Nei vienas jų neklasifikuojamas kaip karinės žvalgybos vienetas. Ne lokaliame adrese procesas atsisako startuoti per paprastą HTTP, nebent izoliuotai laboratorijai aiškiai nustatyta `SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP=1`. Nenaudokite Keycloak paskyros ar slaptažodžio šiam srautui.

### Piktogramų žinynas

| ATAK piktograma | CoT tipas | Šaltinis |
| --- | --- | --- |
| Neutralus radaro sensorius (kliento standartinis MIL simbolis) | `a-n-G-E-S-R` | CAT-34 radaro vieta, įskaitant VERA-NG |
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
| `udp-ingress` | `bridges/udp_ingress_bridge.py` | `…/raw/udp/ingress` ir atpažintas `…/raw/asterix/catNN` | Bendras UDP 50000 srautas |
| `asterix-cat10/20/21/34/48/62` | `protocols/vendors/asterix/cat.py --category NN` | ASTERIX kategorijai skirta normali tema | Tiesioginis UDP/TCP arba viena neapdorota Zenoh kategorijos tema procesui |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | Tik prisijungusių įrenginių apklausa 60 s ir atsijungusių pašalinimas / aptikimų apklausa 10 s |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Konfigūruojama REST apklausa |
| `nffi` | `protocols/random/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Pilni XML dokumentai Zenoh temoje `…/raw/nffi/*` |
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

Operatoriaus pusės sąranka abiem kryptims (TAK ir SitaWare). Perkelta į
bendrą (anglų kalba, nes tai env kintamųjų ir komandų vadovas) dokumentą:
**[C2_RUNBOOK.md](C2_RUNBOOK.md)**.

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

Simptomais pagrįsti sprendimai. Perkelta į atskirą dokumentą:
**[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** (anglų kalba — komandos ir log
eilutės vis tiek būtų angliškos).

## 9. Naujo jutiklio ar protokolo pridėjimas

Žingsnis po žingsnio vadovas — dabar atskiras (anglų kalba, nes tai kodo ir
komandų vadovas) dokumentas: **[ADDING_A_SENSOR.md](ADDING_A_SENSOR.md)**.

---

## 10. Zenoh administravimo GUI

Žiniatinklio GUI podui valdyti be SSH. Techninis turinys perkeltas į bendrą
(anglišką) dokumentą, nes komandos ir laukų pavadinimai vis tiek liktų
angliški: **[ZENOH_ADMIN.md](ZENOH_ADMIN.md)**.

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
