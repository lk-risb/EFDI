# 04 — Konfigūracija

## Konfigūracija

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
# ── TAK serveris ─────────────────────────────────────────────────────────────
TAK_HOST=127.0.0.1
TAK_PORT=8087

# ── SitaWare HQ draugiškų pajėgų sekimas (gaunama REST) ─────────────────────
SITAWARE_URL=https://sitaware.example.com
SITAWARE_USER=
SITAWARE_PASS=
SITAWARE_API_PATH=              # privalomas konkretus diegimo REST resursas

# ── NATO NFFI / ADatP-36 (STANAG 5527) XML jau perduodamas per Zenoh ───────
NFFI_INPUT_TOPIC=               # neprivaloma; numatyta: …/raw/nffi/*

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
