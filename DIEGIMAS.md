# EFDI — Diegimo instrukcija

> **Platforma:** Linux · **Zenoh:** 1.9.0 · **Python:** 3.10+
>
> Techniniai terminai, komandų ir failų pavadinimai pateikiami anglų kalba.

Šis vadovas aprašo sensorių bridge'ų steko diegimą Linux serveryje. Stekas priima ASTERIX CAT-48/34 (Giraffe AMB radaras), dronuradaras.lt akustinius jutiklius, Link-16, MAVLink ir SitaWare duomenis, juos nukreipia per vietinę Zenoh magistralę ir pristatyta į ATAK kaip CoT pranešimai — UDP multicast arba TCP per TAK serverį.

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
| UDP `<CAT48_PORT>` (numatytasis 30048) | į serverį | Giraffe AMB ASTERIX srautas |
| UDP multicast `239.2.3.1:6969` | iš serverio | CoT pristatymas į ATAK |
| TCP 7448 | localhost | Vietinis Zenoh router |
| TCP 7447 TLS | iš serverio | Nuotolinis Zenoh router (reikia NetBird) |
| HTTPS 8890 | į serverį | Zenoh administravimo GUI (Caddy TLS, vidinis CA — žr. §10) |
| HTTPS | iš serverio | dronuradaras.lt REST API |

ATAK įrenginiai turi būti tame pačiame L2 tinklo segmente kaip serveris (multicast neperžengia VLAN ribų be maršrutizatoriaus konfigūracijos). Tarpvietiniam diegimui naudokite TAK serverį ir `cot-tcp` paslaugą.

### Sertifikatai

Zenoh mTLS sertifikatai išduodami savarankiškai — jokio išorinio CA ar vendor bundle. `scripts/gen-certs.sh <namespace>` sugeneruoja (vieną kartą) EFDI root CA faile `compose/certs/efdi-ca-root.pem`/`efdi-ca-root-key.pem`, tada pasirašo lapo sertifikatą+raktą nurodytam namespace; tas pats root CA naudojamas visiems vėlesniems namespace'ams.

Sugeneruota medžiaga (`efdi-ca-root.pem`, `<NAMESPACE>-cert.pem`, `<NAMESPACE>-key.pem`) saugoma `compose/certs/` — įtraukta į `.gitignore`, niekada nekomituojama. Numatytasis kelias nustatomas `start.sh`; jei norite laikyti jį visai už repozitorijos ribų, perrašykite per `BUNDLE_DIR` faile `compose/.env`.

---

## 2. Diegimas

### 2.1 Repozitorijos klonavimas

```bash
git clone <repo-url> EFDI
cd EFDI
```

### 2.2 Sertifikatų generavimas

```bash
scripts/gen-certs.sh <namespace>   # pvz. scripts/gen-certs.sh 1851281db70ccc0409dad4ecfc874cf5
```

Tai sukuria:

```text
compose/certs/
├── efdi-ca-root.pem          # EFDI root CA sertifikatas (viešas)
├── efdi-ca-root-key.pem      # EFDI root CA privatus raktas — saugokite, juo pasirašomas kiekvieno pod'o lapo sertifikatas
├── <NAMESPACE>-cert.pem      # Mazgo sertifikatas
└── <NAMESPACE>-key.pem       # Privatus raktas — apribokite prieigą
```

`<NAMESPACE>` turi sutapti su `PARTNER_NAMESPACE` faile `compose/.env`.

```bash
# Patikrinimas
ls compose/certs/*.pem
chmod 600 compose/certs/*-key.pem
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
# Jei nenustatyta, numatytasis kelias yra compose/certs/ (repo viduje, gitignored) —
# perrašykite tik jei norite laikyti sertifikatus visai už repo ribų.
#BUNDLE_DIR=/home/<vartotojas>/efdi-certs

# ── Vykdymo būsena (žurnalai, PID failai, Zenoh config/sertifikatai) ────────
# Jei nenustatyta, numatytasis kelias yra compose/state/ (repo viduje, gitignored).
#POD_STATE_DIR=/var/lib/efdi-pod

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

# ── SitaWare HQ draugiškų pajėgų sekimas (gaunama REST) ─────────────────────
SITAWARE_URL=https://sitaware.example.com
SITAWARE_USER=
SITAWARE_PASS=

# ── NATO NFFI (STANAG 4677) draugiškų pajėgų srautas (gaunamas XML) ─────────
NFFI_HOST=
NFFI_PORT=7010
NFFI_FRAMING=length             # length | newline

# ── SitaWare Edge (siunčiamas NVG) — atskiras produktas/serveris nei HQ ─────
SITAWARE_NVG_URL=
SITAWARE_NVG_USER=
SITAWARE_NVG_PASS=
SITAWARE_NVG_SOURCE=efdi-live

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

```text
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
  [ 6] [ ] sitaware       SitaWare HQ friendly force tracking (inbound REST)  will prompt for address+login
  [ 7] [ ] nffi           NATO NFFI friendly force XML feed (inbound)         NFFI_HOST not set
  [ 8] [ ] dronuradaras   dronuradaras.lt drone detection        ready

  Output layers
  ──────────────────────────────────────────────────────────
  [ 9] [✓] cot-udp        CoT → ATAK UDP multicast 239.2.3.1:6969
  [10] [✓] cot-tcp        CoT → TAK Server TCP
  [11] [ ] sitaware-nvg   EFDI tracks → SitaWare Edge (outbound NVG)          will prompt for address+login
  [12] [✓] track-fusion   Radar/ADS-B track correlation
```

**Paleidiklio valdymas:**

| Įvestis | Veiksmas |
| --- | --- |
| `1`–`12` | Įjungti / išjungti paslaugą (keli skaičiai atskiriami tarpu) |
| `a` | Pasirinkti visas paruoštas paslaugas |
| `n` | Atžymėti visas |
| Enter | Paleisti pažymėtas paslaugas |
| `q` | Išeiti |

**Rekomenduojami rinkiniai:**

| Scenarijus | Pasirinkimas |
| --- | --- |
| Giraffe radaras + ATAK multicast | `1 2 9` |
| Giraffe + drono aptikimai + ATAK | `1 2 8 9` |
| Giraffe + SitaWare + ATAK multicast | `1 2 6 9` |
| Giraffe + SitaWare + drono aptikimai + ATAK | `1 2 6 8 9` |
| EFDI takeliai siunčiami į SitaWare Edge | `1 2 11` |
| Visi jutikliai + TAK serveris | `a`, tada atžymėkite `9` (cot-udp) |
| Tik radaras be ATAK (derinimui) | `1 2 12` |

Procesų PID failai saugomi `$POD_STATE_DIR/.pids/`, žurnalai rašomi į `$POD_STATE_DIR/logs/<paslauga>.log`.

---

## 5. ATAK sąranka

### UDP multicast (tas pats tinklų segmentas)

1. **Settings → Network → Multicast** — įjunkite multicast gaviklį
2. Adresų sąraše patikrinkite, kad yra `239.2.3.1:6969`
3. Objektai turi pasirodyti per vieną apklausinėjimo ciklą (≤ 10 s drono aptikimams, ≤ 60 s radaro keepalive)

### TAK serveris (skirtingi tinklai / VLAN)

Nustatykite `TAK_HOST` ir `TAK_PORT` faile `.env`, tada paleidiklyje pasirinkite `cot-tcp` vietoj `cot-udp`.

### SitaWare HQ draugiškų pajėgų sekimas (gaunama kryptis)

SitaWare vieneto pozicijos automatiškai publikuojamos į ATAK kai veikia `sitaware` paslauga. Jokios papildomos ATAK konfigūracijos nereikia.

Palikite `SITAWARE_URL`/`SITAWARE_USER`/`SITAWARE_PASS` tuščius faile `.env` ir paleidiklis paklaus serverio adreso bei prisijungimo (vartotojo vardas, tada paslėptas slaptažodžio laukas) kaskart pasirinkus `sitaware` — arba užpildykite juos `.env` iš anksto, kad praleistumėte klausimą. (Antrą adresą vis tiek galima nustatyti per `SITAWARE_URL_FALLBACK` tiesiogiai `.env` faile, jei tikrai yra atskiras LAN/mesh kelias — interaktyvus klausimas paklaus tik vieno adreso.)

**`.env` laukai:**

```bash
SITAWARE_URL=https://<sitaware-serveris>
SITAWARE_URL_FALLBACK=https://<netbird-mesh-ip>   # neprivaloma — antras kelias
SITAWARE_USER=<vartotojo vardas>
SITAWARE_PASS=<slaptažodis>
SITAWARE_POLL_S=10   # neprivaloma — apklausos intervalas sekundėmis (numatytasis 10)
```

Bridge'as nuskaito MIL-STD-2525B SIDC kodus iš SitaWare ir nukreipia kiekvieną vienetą į teisingą Zenoh temą pagal priklausomybę ir kovos dimensiją:

| SIDC priklausomybė | SIDC dimensija | Zenoh temos kelias | ATAK CoT tipas |
| --- | --- | --- | --- |
| Draugiškas / Laikomas draugišku | Žemė (G) | `…/land/sitaware/rest/friendly/unit/…` | `a-f-G-U-C` |
| Priešiškas | Žemė (G) | `…/land/sitaware/rest/hostile/unit/…` | `a-h-G-U-C` |
| Neutralus | Žemė (G) | `…/land/sitaware/rest/neutral/unit/…` | `a-n-G-U-C` |
| Draugiškas | Oras (A) | `…/air/sitaware/rest/friendly/aircraft/…` | `a-f-A-M-F` |
| Priešiškas | Oras (A) | `…/air/sitaware/rest/hostile/aircraft/…` | `a-h-A-M-F` |
| Draugiškas | Jūra (S) | `…/sea/sitaware/rest/friendly/vessel/…` | `a-f-S-X-L` |
| Priešiškas | Jūra (S) | `…/sea/sitaware/rest/hostile/vessel/…` | `a-h-S-X-L` |

### NATO NFFI draugiškų pajėgų srautas (gaunama kryptis)

`nffi` prisijungia prie išorinio NFFI (STANAG 4677 / FMN NFFI) TCP šaltinio — pvz., kitos C2 sistemos arba SitaWare pačios NFFI eksporto — ir publikuoja kiekvieną vienetą į `…/land/nato/nffi/friendly/unit/tracks/v1`. Atskira paslauga nuo `sitaware` (kuri traukia SitaWare HQ REST API); naudokite `nffi`, kai vienintelis prieinamas sąveikos kelias yra grynas NFFI srautas.

**`.env` laukai:**

```bash
NFFI_HOST=<nffi-šaltinio-serveris>
NFFI_PORT=7010                  # patikslinkite su NFFI šaltinio operatoriumi — ne fiksuotas standartinis portas
NFFI_FRAMING=length             # length | newline — patikslinkite kadravimo formatą su šaltiniu
```

### SitaWare Edge (siunčiama kryptis, NVG)

`sitaware-nvg` prenumeruoja visas EFDI takelių temas ir siunčia jas į SitaWare **Edge** serverį per jo NVG v2 REST API, todėl bet kuris SitaWare Frontline klientas, prijungtas prie to Edge serverio, automatiškai mato EFDI takelius — atskiros Frontline integracijos nereikia. Tai priešinga kryptis nei `sitaware`/`nffi` aukščiau (EFDI → SitaWare, ne SitaWare → EFDI), ir dažniausiai kitas serveris/prisijungimo duomenys, nes SitaWare HQ ir SitaWare Edge paprastai yra atskiri serveriai.

Palikite `SITAWARE_NVG_URL`/`SITAWARE_NVG_USER`/`SITAWARE_NVG_PASS` tuščius ir paleidiklis paklaus adreso bei prisijungimo pasirinkus `sitaware-nvg`.

**`.env` laukai:**

```bash
SITAWARE_NVG_URL=http://<sitaware-edge-serveris>:<portas>   # portas priklauso nuo diegimo — patikslinkite su SitaWare administratoriumi
SITAWARE_NVG_USER=<vartotojo vardas>
SITAWARE_NVG_PASS=<slaptažodis>
SITAWARE_NVG_SOURCE=efdi-live    # NVG šaltinio pavadinimas, sukuriamas automatiškai pirmo siuntimo metu
```

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
| Žalia/geltona/raudona sensorių dėžutė (ta pati ikona, keičiasi spalva) | `a-n-G-E-S` / `a-u-G-E-S` / `a-h-G-E-S` | dronuradaras.lt akustinis jutiklis — žalia=neaktyvus, geltona=atvėsta, raudona=aptikimas aktyvus (paskutinės 60s) |
| Balta nežinoma orlaivio | `a-u-A-C-F` | Neklasifikuotas radaro takelis |

> Radaro žymeklio pozicija, greitis ir kursas atnaujinami automatiškai iš gyvo CAT-34 srauto. Mobilioje platformoje ATAK rodys greičio vektorių ir judėjimo taką.

---

## 6. Paslaugų žinynas

| Paslauga | Scenarijus | Zenoh tema (sutrumpinta) | Suaktyvinimas |
| --- | --- | --- | --- |
| `asterix` | `bridges/asterix_bridge.py` | `…/air/asterix/cat48/unknown/aircraft/tracks/v1` | Srautinis UDP |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/status/v1` | Įrenginių apklausa 60 s / aptikimų apklausa 10 s |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/rest/friendly/unit/tracks/v1` | Konfigūruojama REST apklausa |
| `nffi` | `layers/nato_nffi_layer.py` | `…/land/nato/nffi/friendly/unit/tracks/v1` | Srautinis TCP (NFFI XML) |
| `link16` | `bridges/link16_bridge.py` | `…/air/link16/jreap/*/aircraft/tracks/v1` | Srautinis UDP/TCP |
| `mavlink` | `bridges/mavlink_bridge.py` | `…/air/mavlink/mav2/*/uav/tracks/v1` | Srautinis UDP/TCP |
| `cot-udp` | `layers/cot_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `cot-tcp` | `layers/cot_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `sitaware-nvg` | `layers/nato_nvg_layer.py` | Prenumeratorius — visos takelių temos | Įvykio valdomas, 10 s atnaujinimas |
| `track-fusion` | `layers/track_fusion_layer.py` | CAT-48 + CAT-21 prenumeratorius | Įvykio valdomas |

### Zenoh temų schema

```text
{VARDAS_ERDVĖ}/{DOMENAS}/{ŠALTINIS}/{PROTOKOLAS}/{PRIKLAUSOMYBĖ}/{TIPAS}/tracks/v1
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
./stop.sh layers       # Stabdo tik išvesties sluoksnius (cot-udp, cot-tcp, track-fusion)
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

SitaWare vienetai be galiojančio 15 simbolių SIDC kodo nukreipiami į `…/land/sitaware/rest/unknown/unit/…` ir rodomi kaip nežinomi žemės vienetai (`a-u-G-U-C`). Patikrinkite SIDC reikšmę žurnale:

```bash
grep "sidc=" $POD_STATE_DIR/logs/sitaware.log | head -10
```

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
# compose/bridge/bridges/<pavadinimas>_bridge.py

import json, os, time
import zenoh

ORG       = "<YOUR_NAMESPACE>"
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", os.path.dirname(__file__))

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

## 10. Zenoh administravimo GUI

Web GUI stebėti routerio būseną ir redaguoti `zenoh/config.json5` be SSH prieigos, stiliaus pavyzdys — TAK admin panelė (reticle kampų kortelės, stiklinis šoninis meniu, akcento švytėjimas, techninio tinklelio fonas).

Skydelio "Connected routers" panelė rodo kiekvieną kitą zenoh egzempliorių (router ar peer), su kuriuo šis routeris turi gyvą ryšį — gaunama iš routerio pačio admin space, tas pats šaltinis kaip prenumeratorių/queryable temų sąrašai, tad papildomos konfigūracijos nereikia be jau esamos `pod-admin-introspect` ACL taisyklės.

### Nustatymas

Pridėkite į `compose/.env` (pilną bloką žr. `compose/.env.example`):

```bash
ZENOH_ADMIN_DB_USER=zenoh_admin
ZENOH_ADMIN_DB_PASSWORD=<atsitiktinis>
ZENOH_ADMIN_DB_PORT=5433                # ne numatytasis: nesikerta su kitu Postgres serveriu hoste
ZENOH_ADMIN_SECRET_KEY=<openssl rand -hex 32>
ZENOH_ADMIN_FIRST_USER=admin
ZENOH_ADMIN_FIRST_PASS=<nustatykite vieną kartą, po pirmo prisijungimo galite ištrinti>
```

`ZENOH_ADMIN_FIRST_PASS` sukuria pirmą `superadmin` paskyrą tik jei ji dar neegzistuoja — po pirmo prisijungimo šį kintamąjį saugu vėl palikti tuščią (paskyra išlieka Postgres duomenų bazėje).

### Paleidimas

```bash
cd compose
docker compose up -d zenoh-admin-db zenoh-admin zenoh-admin-proxy
```

Tada atidarykite `https://<pod-host>:8890`.

Pats skydelis (`zenoh-admin`) klausosi tik `127.0.0.1:8895` — tiesiogiai nepasiekiamas. Caddy reverse proxy (`zenoh-admin-proxy`) baigia tikrą TLS ant `:8890` naudodamas savo vidinį CA (`local_certs` + `tls internal`, be išorinio ACME/CA priklausomybės), išsaugotą `zenoh_admin_caddy_data` tome, kad CA išliktų po perkrovimų. Naršyklė pirmą kartą parodys savarankiškai pasirašyto sertifikato įspėjimą — pasitikėkite Caddy vidiniu CA (arba priimkite įspėjimą), kad tęstumėte; čia sąmoningai nėra viešo sertifikato, nes šis skydelis nėra skirtas interneto prieigai.

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
| Fabric endpoint | Goat pusės / kolegos endpoint, į kurį šis pod'as skambina — įvedamas kaip atskiri Host + Port laukai (schema visada `tls`, niekada nerodoma); yra vieno paspaudimo šablonai anksčiau naudotiems endpoint'ams |
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
| `bridge-syntax` | `py_compile` kiekvienam failui `compose/bridge/bridges/` ir `compose/bridge/layers/` |
| `zenoh-admin-frontend` | `pnpm type-check` + `pnpm build` `compose/zenoh-admin/ui` |
| `docker-build` | Sukuria abu Docker image'us (`compose/bridge` ir `compose/zenoh-admin`), be push |

Tai pagauna sintaksės klaidas, TypeScript klaidas ir Dockerfile lūžimus prieš merge — **nepaleidžia** pačių bridge'ų (dauguma reikalauja tikrų API raktų/tinklo prieigos, kurios CI neturi).

---

## Pakeitimų žurnalas

| Data | Pakeitimas |
| --- | --- |
| 2026-06-14 | Pradinis commit — šakota iš oficialaus `efdi-moon-pod-main` saugyklos |
| 2026-06-15 | Baziniai bridge adapteriai sujungti; saugyklos struktūra nustatyta; pridėtas README |
| 2026-06-16 | `airplanes.live` bridge: regioniniai ADS-B ir pasauliniai kariniai orlaiviai |
| 2026-06-16 | ICAO NOTAM bridge: aktyvių NOTAM priėmimas per ICAO Dataservices API |
| 2026-06-16 | FlightRadar24 bridge: FR24 komercinės transliacijos integracija |
| 2026-06-16 | Windy bridge: taškų orų prognozių API integracija |
| 2026-06-16 | Protocol Buffer aprašai naujiems takelio tipams (`aircraft_track`, `ais_track`, `aprs_track`, `cat62_track`) |
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
| 2026-07-05 | Ištaisyta `dronuradaras_bridge.py` — publikavo tik `is_online` jutiklius (22 iš 199 registruotų) — dabar publikuoja visus jutiklius su žinoma pozicija, atitinka tai, ką rodo viešas dronuradaras.lt puslapis |
| 2026-07-05 | Pridėtas `.github/workflows/ci.yml`: tikrina bridge'ų/sluoksnių sintaksę, type-check + build zenoh-admin frontend'ui, sukuria abu Docker image'us kas kartą pushinant/darant PR |
| 2026-07-05 | Pridėti `shellcheck` ir `compose-validate` CI job'ai; ištaisytas vienintelis realus radinys (`compose/rebuild.sh` trūko `cd ... \|\| exit`) ir nutildytas klaidingas teigiamas (`SC2163` dėl sąmoningo "export pagal dinaminį vardą" idiomo `start.sh`/`stop.sh`/`run.sh`) |
| 2026-07-10 | Ištaisyta: `nato_nvg_layer.py` naudojo tuos pačius aplinkos kintamuosius kaip gaunamas `sitaware_bridge.py` (`SITAWARE_URL`/`USER`/`PASS`) — pervadinta į `SITAWARE_NVG_*`, nes HQ (gaunama) ir Edge (siunčiama) paprastai yra skirtingi serveriai/prisijungimo duomenys |
| 2026-07-10 | Paslaugos `nffi` (`layers/nato_nffi_layer.py`) ir `sitaware-nvg` (`layers/nato_nvg_layer.py`) prijungtos prie `start.sh` — abi egzistavo repozitorijoje, bet niekada nebuvo registruotos kaip paleidžiamos paslaugos |
| 2026-07-10 | `start.sh`: `sitaware` ir `sitaware-nvg` dabar paklausia vartotojo vardo ir paslėpto slaptažodžio paleidimo metu (anksčiau buvo klausiama tik serverio adreso; prisijungimo duomenys turėjo būti iš anksto nustatyti `.env`) |
| 2026-07-10 | Zenoh admin GUI: pridėta "Connected routers" panelė — nuskaito `router/transport/unicast/*` įrašus, jau esančius admin space užklausoje, naudojamoje prenumeratorių/queryable sąrašams, jokios naujos ACL ar užklausos nereikia |
| 2026-07-10 | Zenoh admin GUI: perkeltas TAK-hud vizualinis stilius (`hud-card`, `hud-frame`/reticle kampai, `hud-glass` šoninis meniu, `hud-grid-bg` fonas, akcento švytėjimo mygtukai, laipsniškas atsiradimo animacijos) į `index.css`/`Layout.tsx`/skydelį |

---

*Skirta vidiniam naudojimui — neskleisti už projekto ribų.*
