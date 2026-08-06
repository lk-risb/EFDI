# EFDI — Diegimo instrukcija

> **Platforma:** Linux · **Zenoh:** 1.9.0 · **Python:** 3.10+
>
> Techniniai terminai, komandų ir failų pavadinimai pateikiami anglų kalba.

Šis vadovas aprašo sensorių bridge'ų steko diegimą Linux serveryje. Stekas gali
priimti mišrias ASTERIX kategorijas (dabartiniai normalizuojantys vertėjai yra
CAT-001, CAT-002, CAT-004, CAT-007, CAT-008, CAT-009, CAT-010, CAT-011, CAT-015,
CAT-016, CAT-017, CAT-018, CAT-019, CAT-020, CAT-021, CAT-023, CAT-025, CAT-032, CAT-034, CAT-048, CAT-062, CAT-063, CAT-065, CAT-150, CAT-205, CAT-240 ir CAT-247 — visas viešas ASTERIX katalogas), dronuradaras.lt aptikimus, SAPIENT, STANAG 4586/4607/4609/5516 ir SitaWare duomenis,
tada per vietinę Zenoh magistralę pateikia juos TAK ir SitaWare klientams.

---

## 1. Reikalavimai

### Serverio paruošimas nuo tuščios mašinos (pasirinktinai)

`./install.sh` atnaujina OS (`apt`/`dnf` upgrade) ir savaime įdiegia git,
Python 3.10+, Docker Engine + Compose papildinį (iš oficialios Docker
saugyklos, ne distributyvo paketą), openssl ir gettext tiek Debian (apt),
tiek RHEL/Rocky/Alma (dnf) sistemose, jei jų trūksta — visiškai tuščiame
serveryje su vien `sudo` teisėmis ir išeinančiu interneto ryšiu pakanka
paleisti `./install.sh` be jokio rankinio paruošimo. Jei po OS atnaujinimo
reikia perkrauti (branduolio ar bazinės bibliotekos atnaujinimas), diegyklė
sustoja ir apie tai praneša — tiesiog perkraukite ir vėl paleiskite tą pačią
komandą. Jis taip pat pasiūlo įdiegti ir prijungti NetBird arba Tailscale,
jei nei vienas dar neprijungtas, klausdamas tik setup/auth rakto —
vienintelio dalyko, kurio diegyklė pati sugalvoti negali.

Likusi šio poskyrio dalis yra rankinė nuoroda, ką diegyklė atlieka
automatiškai — naudinga suprasti klaidas, izoliuoto tinklo (offline)
diegimus, ar kitokio distributyvo serverius. Praleiskite ją ir pereikite
tiesiai prie **Programinė įranga**, jei tiesiog paleidžiate `./install.sh`
palaikomame distributyve.

#### Pasirinkite ir parinkite serverio dydį

| | Minimalu | Rekomenduojama |
| --- | --- | --- |
| OS | Debian 13 (trixie) arba RHEL 9/10, Rocky Linux 9/10, AlmaLinux 9/10 | Debian 13 (trixie) |
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Diskas | 20 GB laisvos vietos | 40 GB+ laisvos vietos (daugiau, jei įjungsite ilgalaikį trasų saugojimą) |
| Tinklas | Vienas sąsajos su išeinančiu interneto ryšiu | Statinis arba DHCP rezervuotas adresas |

Bet kuris modernus x86_64 arba arm64 Linux platinys su naujesniu branduoliu ir
systemd tinka; šios dvi šeimos aprašomos žingsnis po žingsnio žemiau, nes jos
dažniausiai naudojamos valstybinėse/gynybos aplinkose. Ubuntu taip pat veikia
(ta pati apt įrankių bazė), bet Debian yra tikrasis šio projekto taikinys ir
tai, ką `install.sh` naudoja pagal nutylėjimą — jei naudojate visiškai kitą
platinį, tiesiog pritaikykite paketų tvarkyklės komandas — likusi vadovo dalis
galioja nepakitusi.

Visas žemiau esančias komandas vykdykite kaip paprastas vartotojas su `sudo`
teisėmis — ne tiesiogiai kaip `root`, kad paskutinis „paleisti Docker be root
teisių" žingsnis turėtų prasmę.

#### Atnaujinkite OS

**Debian:**
```bash
sudo apt update && sudo apt upgrade -y
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf upgrade -y
```

Jei buvo atnaujintas branduolys, perkraukite (`sudo reboot`). `./install.sh` šį žingsnį atlieka automatiškai — čia jis pateiktas kaip rankinio/offline diegimo nuoroda.

#### Įdiekite git ir bazinius įrankius

**Debian:**
```bash
sudo apt install -y git curl ca-certificates
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf install -y git curl ca-certificates
```

Patikrinkite: `git --version`

#### Įdiekite Python 3.10+

**Debian 13 (trixie)** numatytai turi Python **3.13** — jau gerokai virš EFDI
minimumo, papildomo žingsnio nereikia, tereikia įsitikinti, kad venv/pip yra:
```bash
sudo apt install -y python3 python3-venv python3-pip
```

**RHEL/Rocky/AlmaLinux 10** numatytai turi Python **3.12** — jau virš EFDI
minimumo, tas pats vienas žingsnis kaip Debian:
```bash
sudo dnf install -y python3 python3-pip
```

**RHEL/Rocky/AlmaLinux 9** numatytai turi Python **3.9**, kuris yra žemiau
EFDI minimumo. Įdiekite 3.11 iš AppStream saugyklos šalia esamos versijos
(tai **nepakeičia** sisteminio `python3`, todėl niekas kitas serveryje
nesugadinama):
```bash
sudo dnf install -y python3.11 python3.11-pip
```
RHEL 9 serveryje visur, kur šio repo scriptai rašo `python3`, naudokite
`python3.11` arba susikurkite venv, nukreiptą į jį.

Patikrinkite: `python3 --version` (arba `python3.11 --version` RHEL 9)
turi rodyti **3.10 arba naujesnę**.

#### Įdiekite Docker Engine + Compose papildinį

Naudokite oficialią platinio Docker saugyklą, ne platinio pridedamą
`docker.io`/`podman-docker` paketą — jie dažnai pasenę ir gali neturėti
Compose v2 papildinio, nuo kurio priklauso šis repo (`docker compose`, ne
senasis atskiras `docker-compose`).

**Debian:**
```bash
# Pridėkite oficialų Docker GPG raktą ir saugyklą
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update

# Įdiekite Docker Engine + Compose papildinį
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```
(Ubuntu sistemoje abiejose eilutėse pakeiskite `linux/debian` į `linux/ubuntu`
— Docker skelbia atskiras saugyklas kiekvienam platiniui.)

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

##### Paleiskite Docker be root teisių

Kiekvienas šio repo scriptas tikisi, kad `docker`/`docker compose` veiks be
`sudo`. Sutvarkykite tai dabar:
```bash
sudo groupadd docker 2>/dev/null || true   # daugumoje sistemų jau egzistuoja
sudo usermod -aG docker "$USER"
newgrp docker                              # aktyvuoja naują grupę šiame apvalkale
```
Atsijunkite ir vėl prisijunkite (arba perkraukite), kad grupės narystė
galiotų kiekvienam naujam apvalkalui, ne tik dabartiniam. Patikrinkite:
```bash
docker run hello-world
docker compose version
```
Abi komandos turi pavykti **be `sudo`**, prieš tęsiant toliau.

#### Įdiekite NetBird arba Tailscale

EFDI podai pasiekia fabriką ir vienas kitą per mesh VPN — NetBird arba
Tailscale, abu tinka. Įdiekite tą, kurį naudoja jūsų organizacija (abu
skriptai patys atsineša savo saugyklą, todėl atskiro apt/dnf nustatymo
nereikia):
```bash
curl -fsSL https://pkgs.netbird.io/install.sh | sh      # NetBird
curl -fsSL https://tailscale.com/install.sh | sh        # Tailscale
```
Dar **neprisijunkite** prie tinklo — setup/auth raktą duoda jūsų organizacijos
paskyros administratorius, o `./install.sh` pats to paklaus ir prisijungs
**§2 Diegimas** žemiau (būtent tai daro jo automatinis Tinklo žingsnis).
Patikrinkite tik, ar dvejetainis failas įdiegtas:
```bash
netbird version
tailscale version
```

#### Atidarykite ugniasienės prievadus

Atidarykite tik tai, ko šiam serveriui reikia įeinančiai srauto krypčiai;
likusi žemiau esančioje prievadų lentelėje esanti dalis yra išeinanti ir
nereikalauja ugniasienės taisyklės šiame serveryje. Autoritetingas, aktualus
prievadų sąrašas yra **Tinklas** lentelė žemiau — atidarykite tuos, kuriuos
jūsų diegimas iš tikrųjų naudoja (dauguma podų nepaleidžia visų jutiklių
tiltų).

**Debian (ufw):**
```bash
sudo apt install -y ufw   # Debian, skirtingai nei Ubuntu, jo neįdiegia pagal nutylėjimą
sudo ufw allow 8890/tcp comment 'EFDI admin GUI'
sudo ufw allow 50048/udp comment 'EFDI CAT-048 pavyzdys — pritaikykite savo jutikliams'
# kartokite kiekvienam UDP/TCP prievadui, kurį naudoja jūsų integracijos, pagal lentelę žemiau
```

**RHEL/Rocky/AlmaLinux (firewalld):**
```bash
sudo firewall-cmd --permanent --add-port=8890/tcp
sudo firewall-cmd --permanent --add-port=50048/udp
sudo firewall-cmd --reload
```

Jei serveris yra už atskiros tinklo ugniasienės ar saugumo grupės (debesis,
vietinis prietaisas), tuos pačius prievadus reikės atidaryti ir ten — šis
žingsnis apima tik paties serverio vietinę ugniasienę.

#### Jūs pasiruošę

Šiuo metu turėtumėte galėti paleisti (viską be `sudo`):
```bash
git --version
python3 --version      # 3.10+
docker run hello-world
docker compose version
netbird version         # arba: tailscale version
```

Jei kiekviena komanda aukščiau pavyko, tęskite prie **§2** žemiau
(repozitorijos klonavimas ir podo paleidimas). Jei kas nors nepavyko, iš
naujo įvykdykite atitinkamą žingsnį aukščiau, prieš judėdami toliau — niekas
vėliau diegime negali ištaisyti čia trūkstamos priklausomybės.

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
| TCP `<TAK_PORT>` (mTLS, numatytasis 8089) | iš serverio | CoT pristatymas į TAK serverį |
| TCP 7448 | localhost | Vietinis Zenoh router |
| TCP 7447 TLS | iš serverio | Nuotolinis Zenoh router (reikia NetBird) |
| HTTPS 8890 | į serverį | Zenoh administravimo GUI (Caddy TLS, vidinis CA — žr. §10) |
| HTTPS | iš serverio | dronuradaras.lt API |

ATAK/WinTAK klientai takelius gauna tik per TAK serverį (`tak-layer` paslauga); tiesioginio multicast/unicast CoT kelio nėra.

### Sertifikatai

Zenoh mTLS sertifikatai išduodami savarankiškai — jokio išorinio CA ar vendor bundle. `scripts/gen-certs.sh <namespace>` sugeneruoja (vieną kartą) EFDI root CA kataloge `compose/certs/efdi/`, tada pasirašo lapo sertifikatą+raktą nurodytam namespace; tas pats root CA naudojamas visiems vėlesniems namespace'ams.

Sugeneruota medžiaga (`efdi-ca-root.pem`, `<NAMESPACE>-cert.pem`, `<NAMESPACE>-key.pem`) saugoma `compose/certs/efdi/` — įtraukta į `.gitignore`, niekada nekomituojama. Kataloge taip pat atskirai laikomi `tak/`, `sitaware/`, `efdi-backbone/` (goat backbone, Desert Bread CA) ir `efdi-ltu/` (LTU sandbox) identitetai — žr. `docs/INSTALL.md` §2.2 (anglų k.). Numatytasis kelias nustatomas `start.sh`; jei norite laikyti jį visai už repozitorijos ribų, perrašykite per `BUNDLE_DIR` faile `compose/.env`.

---

## 2. Diegimas

### 2.1 Repozitorijos klonavimas

Švariame serveryje, kur dar nieko neįdiegta, viena komanda nuklonuoja
repozitoriją ir paleidžia diegyklę, kuri pati įdiegia visus 1 skyriaus
reikalavimus:

```bash
curl -fsSL https://raw.githubusercontent.com/risblicencijos/EFDI/main/install.sh | bash
```

Tas pats, tik pirma nuklonavus rankiniu būdu:

```bash
git clone <repo-url> EFDI
cd EFDI
./install.sh
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

Pilnas paaiškinimas (kuris cert kuriam fabric) — žr. `docs/INSTALL.md` §2.2
(anglų k.). Katalogų išdėstymas ir kiekvieno profilio README yra sekami,
tačiau visi sertifikatai, privatūs raktai, grandinės ir sugeneruoti
kredencialai yra `.gitignore`.

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

## 5. ATAK sąranka

### TAK serveris

Nustatykite `TAK_HOST` ir `TAK_PORT` faile `.env`, tada paleidiklyje pasirinkite `tak-layer`. ATAK/WinTAK klientai takelius gauna tik per TAK serverį — tiesioginio multicast/unicast CoT kelio nėra.

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

`sitaware-hq-nvg` yra natyvus Python išvesties procesas, skirtas HQ diegimui. Jis prenumeruoja EFDI takelius, laiko riboto dydžio gyvą momentinę būseną ir pateikia NVG 2.0.2 per tik skaitymui skirtą HTTP(S) adresą. SitaWare Headquarters jį periodiškai ima per **SitaWare Communication → NVG → NVG Import Subscriptions**. Atskiro NVG-XML gavimo tilto nėra — SitaWare įvestis eina per `sitaware` REST paslaugą.

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
> Išsamiau: [§7 Integracijos → Išvesties temos](#išvesties-temos-sapient-json-proto-raw).

| Paslauga | Scenarijus | Zenoh tema (sutrumpinta) | Suaktyvinimas |
| --- | --- | --- | --- |
| `udp-ingress` | `bridges/udp_ingress_bridge.py` | `…/raw/udp/ingress` ir atpažintas `…/raw/asterix/catNN` | Bendras UDP 50000 srautas |
| `asterix-cat10/20/21/34/48/62` | `protocols/vendors/asterix/cat.py --category NN` | ASTERIX kategorijai skirta normali tema | Tiesioginis UDP/TCP arba viena neapdorota Zenoh kategorijos tema procesui |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | Tik prisijungusių įrenginių apklausa 60 s ir atsijungusių pašalinimas / aptikimų apklausa 10 s |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Konfigūruojama REST apklausa |
| `nffi` | `protocols/random/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Pilni XML dokumentai Zenoh temoje `…/raw/nffi/*` |
| `tak-layer` | `layers/tak_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `sitaware-hq-nvg` | `layers/sitaware_hq_nvg_feed.py` | Prenumeratorius — visos takelių temos | HQ periodiškai ima NVG būseną |
| `track-fusion` | `protocols/fusion.py` | CAT-48 + CAT-21 prenumeratorius | Įvykio valdomas |

### TAK naudotojai ir SitaWare HQ technika

Aktyvus CoT kelias yra `layers/tak_layer.py`: jis prenumeruoja normalizuotas
Zenoh temas ir siunčia CoT į `tak_layer` paskirties TAK Server. Naudokite TAK
išduotą kliento sertifikatą, kai įjungtas `TAK_TLS=1`. Dabartiniame EFDI
runtime nėra atskiro TAK arba SitaWare CoT priėmimo tilto. Jei konkretus
diegimas teikia NFFI, pilnus XML dokumentus skelbkite į
`…/raw/nffi/{source-id}` per prijungtą Zenoh mazgą.

CoT ir abi SitaWare NVG išvestys naudoja tą pačią scenarijaus priklausomybės
taisyklę: orlaiviai iš nustatytų RU/BY ICAO adresų intervalų bei laivai su RU/BY
MMSI MID žymimi kaip priešiški, o kiti partnerių oro/jūros kontaktai — neutralūs.
Vien šalies pavadinimas nepakeičia trūkstamo arba negaliojančio atsakiklio ID.

## 7. Integracijos

### Integracijų apžvalga

> **Norite prijungti naują jutiklį?** Šis puslapis yra nuoroda, kas jau
> sujungta. Žingsnis po žingsnio vadovą "kaip pridėti naują" žr.
> [§9 Naujo jutiklio ar protokolo pridėjimas](#9-naujo-jutiklio-ar-protokolo-pridėjimas) žemiau.

EFDI atskiria šaltinio-specifinius kolektorius, daugkartinio naudojimo
protokolų vertėjus ir TAK/SitaWare išvesties sluoksnius:

- `compose/bridges/` — jungiasi prie vardinio produkto ar paslaugos.
- `compose/protocols/` — po vieną nepriklausomai paleidžiamą laidinį/API
  protokolą faile. ASTERIX kategorijos atskiros, nes skiriasi jų UAP ir
  leidimai.
- `compose/layers/` — jungia normalizuotus Zenoh duomenis su TAK/CoT ir
  SitaWare/NVG.

Kai tik gaunamas skriptas paskelbia normalizuotą temą, veikiantys CoT ir NVG
sluoksniai automatiškai ją prenumeruoja. Imtuvai ir aptikimo sistemos
paprastai prisijungia prie netoliese esančio Zenoh routerio ir skelbia ten;
routeris perduoda jų duomenis. Todėl daugumai routerio serverių nereikia
jokios imtuvo aparatinės įrangos ar tiekėjo tvarkyklės.

### Protokolo prijungimo reikalavimai

ASTERIX kategorijos numeris nenustato TCP ar UDP prievado numerio. Radaro
arba stebėjimo šliuzo valdymo sąsaja turi būti sukonfigūruota su EFDI serveriu
kaip paskirties tašku ir su tuo pačiu transportu/prievadu, pasirinktu žemiau.
EFDI naudoja UDP 50034 CAT-034 ir UDP 50048 CAT-048 kaip determinuotus vietinius
susitarimus; tai nėra EUROCONTROL ar Saab numatytieji nustatymai. UDP 50000
yra bendras neapdorotas įėjimas. `udp_ingress_bridge.py` išsaugo kiekvieną
datagramą ir saugiai publikuoja pilnus ASTERIX kadrus nepakeistus į
`…/raw/asterix/catNN`; kiekvienas kategorijos vertėjas lieka atskiru procesu
ir prenumeruoja tik savo temą. `ASTERIX_CATEGORIES` pasirenka, kurios
kategorijos automatiškai išsiunčiamos. Dedikuoti UDP/TCP įėjimai lieka
aktyvūs tuo pačiu metu.

Kai radaro pusės nešiojamas kompiuteris jau publikuoja pilnus kadrus per kitą
Zenoh routerį, nustatykite `ASTERIX_ZENOH_UPSTREAM_ENDPOINT` ir pasirinktinai
`ASTERIX_ZENOH_UPSTREAM_ROOT`. `asterix_bridge.py` prenumeruoja kiekvieną
`…/raw/asterix/catN` temą tame routeryje, patikrina, ar temos kategorija ir
ASTERIX antraštė sutampa, ir republikuoja nepakeistą kadrą vietiniame
routeryje. Tie patys kategorijos vertėjai jį tada dekoduoja; pats tiltas
neinterpretuoja UAP. Paprastas tekstas `tcp/...:7448` skirtas tik izoliuotam
testavimui.

Mišrus tiltas taip pat palaiko `ASTERIX_BIND`, `ASTERIX_MULTICAST_GROUP`,
`ASTERIX_MULTICAST_INTERFACE` ir IPv4/CIDR `ASTERIX_ALLOW_SOURCE` filtrą.
Prieš konfigūruodami naują srautą, stebėkite jį be Zenoh publikavimo:

```bash
python3 tools/asterix_probe.py --port 30001
```

Zondas praneša siuntėjo IP, paskirties prievadą, kategoriją, pirmo FRN
SAC/SIC (jei yra), kadrų skaičių ir dažnį. Multicast srautui pridėkite
`--multicast-group` ir `--multicast-interface`.

`asterix_probe.py` yra `tools/` kataloge, šalia `asterix_relay.py`, o ne
`compose/` — niekas `tools/` kataloge neimportuojama podo, nepaleidžiama
`start.sh`/`run.sh` ir neįtraukiama į jokį image'ą. Tai savarankiški
operatoriaus įrankiai, paleidžiami ranka, dažniausiai iš nešiojamo kompiuterio
arba prie radaro prijungto PC, konfigūruojant srautą: `compose/bridges/` ir
`compose/protocols/` yra veikiantis duomenų sluoksnis, `tools/` — lauko
įrankiai. `asterix_relay.py` persiunčia ASTERIX UDP datagramas nepakeistas,
baitas į baitą, iš vietinio prievado į nuotolinį `IP:PORT` — paleiskite jį
mašinoje, prijungtoje prie radaro, kai ta mašina yra NetBird tinkle, bet pats
radaras nepasiekiamas iš podo:

```bash
python3 tools/asterix_relay.py --dest 100.x.y.z:30048
```

`tests/test_asterix_raw_pipeline.py` tikrina kadravimo logiką, kurią šie
įrankiai dalinasi su dekoderiais, todėl pakeitimai čia pagaunami įprastu
testų paleidimu.

| Protokolo skriptas | Transporto vaidmuo | Reikalinga partnerio/veikimo konfigūracija | Dabartinė sutartis |
|---|---|---|---|
| `vendors/asterix/cat.py --category 1` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT1_PORT`; nustatykite `CAT1_RADAR_LAT/LON` polinių/kartezinių planų/takelių georeferencijai | EUROCONTROL CAT-001 Ed.1.4 monoradaro plano/takelio pranešimai (paveldėta, dažniausiai pakeista CAT-048) |
| `vendors/asterix/cat.py --category 2` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT2_PORT` | EUROCONTROL CAT-002 Ed.1.2 monoradaro paslaugų pranešimai (šiaurės žymeklis, sektoriaus kirtimas, stoties būsena) |
| `vendors/asterix/cat.py --category 4` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT4_PORT` | EUROCONTROL CAT-004 Ed.1.13 saugos tinklo įspėjimai (STCA/MSAW/APW/RIMCA/...) |
| `vendors/asterix/cat.py --category 7` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT7_PORT`; nustatykite `CAT7_RADAR_LAT/LON` polinių/kartezinių pranešimų georeferencijai | EUROCONTROL CAT-007 Ed.1.12 nukreiptos apklausos pranešimai (karinė Mode 4/5/S apklausos kontrolė; downlink ir uplink UAP) |
| `vendors/asterix/cat.py --category 8` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT8_PORT` | EUROCONTROL CAT-008 Ed.1.3 monoradaro išvestinė orų informacija (orų vaizdo vektoriai/kontūrai) |
| `vendors/asterix/cat.py --category 9` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT9_PORT` | EUROCONTROL CAT-009 Ed.2.1 sudėtiniai orų pranešimai (sujungtas daugiaradaro orų vaizdas) |
| `vendors/asterix/cat.py --category 10` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT10_PORT`; nustatykite oro uosto atskaitos koordinates, jei pranešimai naudoja tik vietinį X/Y arba polinę poziciją | EUROCONTROL CAT-010 Ed.1.1, oro uosto paviršiaus taikiniai/būsena |
| `vendors/asterix/cat.py --category 11` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT11_PORT`; nustatykite `CAT11_SITE_LAT/LON` kartezinių pranešimų georeferencijai | EUROCONTROL CAT-011 Ed.1.3 A-SMGCS sistemos takeliai (sujungti oro uosto paviršiaus orlaiviai + transporto priemonės su skrydžio plano koreliacija) |
| `vendors/asterix/cat.py --category 15` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT15_PORT`; nustatykite `CAT15_SITE_LAT/LON` diapazono/azimuto pranešimų georeferencijai | EUROCONTROL CAT-015 Ed.1.2 nepriklausomos nekooperatyvios stebėsenos (pasyvios/daugiastotės) taikinių pranešimai |
| `vendors/asterix/cat.py --category 16` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT16_PORT` | EUROCONTROL CAT-016 Ed.1.0 nepriklausomos nekooperatyvios stebėsenos sistemos konfigūracijos pranešimai (INCS antžeminės sistemos pačios vietos pozicijos/siųstuvo/imtuvo konfigūracija, CAT-015 sesuo statuso kategorija) |
| `vendors/asterix/cat.py --category 17` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT17_PORT` | EUROCONTROL CAT-017 Ed.1.3 Mode S stebėsenos koordinavimo funkcijos pranešimai (paveldėtas tarpradarinio klasterio/perdavimo protokolas; „Track Data" pranešimai turi poziciją, tinklo valdymo pranešimai — ne) |
| `vendors/asterix/cat.py --category 18` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT18_PORT`; nustatykite `CAT18_SITE_LAT/LON` vietinių polinių/kartezinių pozicijos elementų georeferencijai | EUROCONTROL CAT-018 Ed.1.8 Mode S duomenų perdavimo funkcijos pranešimai (GDLP/apklausiklio uplink-downlink koordinavimas: orlaivių pranešimai, uplink paketo/transliacijos/GICB-ištraukimo užklausos ir patvirtinimai) |
| `vendors/asterix/cat.py --category 19` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT19_PORT` | EUROCONTROL CAT-019 Ed.1.3 MLT sistemos būsena |
| `vendors/asterix/cat.py --category 20` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT20_PORT` ir patvirtina 1.11 leidimą | EUROCONTROL CAT-020 Ed.1.11 MLAT pranešimai |
| `vendors/asterix/cat.py --category 21` | UDP klausytojas arba TCP serveris | ADS-B šliuzas siunčia į `CAT21_PORT` ir patvirtina 2.7 leidimą | EUROCONTROL CAT-021 Ed.2.7 ADS-B pranešimai |
| `vendors/asterix/cat.py --category 23` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT23_PORT` | EUROCONTROL CAT-023 Ed.1.3 CNS/ATM antžeminės stoties paslaugų pranešimai (ADS-B/TIS-B/FIS-B/GRAS/MLT stoties būsena) |
| `vendors/asterix/cat.py --category 25` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT25_PORT` | EUROCONTROL CAT-025 Ed.1.6 CNS/ATM antžeminės sistemos būsenos pranešimai (CAT-023 įpėdinis/palydovas: atskirta sistemos/paslaugos būsena, komponentų sąrašas, paslaugų statistika, vietos pozicija) |
| `vendors/asterix/cat.py --category 32` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT32_PORT` | EUROCONTROL CAT-032 Ed.1.2 Miniplan pranešimai SDPS (FPPS/SDPS skrydžio plano-takelio numerio koreliacija; šioje kategorijoje pozicijos lauko nėra) |
| `vendors/asterix/cat.py --category 34` | UDP klausytojas arba TCP serveris | Radaras siunčia CAT-034 atskirai į `CAT34_PORT` (EFDI susitarimas: UDP 50034) | EUROCONTROL CAT-034 Ed.1.29 radaro paslaugų pranešimai |
| `vendors/asterix/cat.py --category 48` | UDP klausytojas arba TCP serveris | Radaras siunčia CAT-048 atskirai į `CAT48_PORT` (EFDI susitarimas: UDP 50048); vietinei polinei pozicijai reikia `CAT48_RADAR_LAT/LON` | EUROCONTROL CAT-048 Ed.1.32 taikiniai |
| `vendors/asterix/cat.py --category 62` | TCP klientas arba UDP klausytojas | Nustatykite `CAT62_HOST/PORT`, arba `CAT62_UDP=1`; patvirtinkite 1.21 leidimą | EUROCONTROL CAT-062 Ed.1.21 sistemos takeliai |
| `vendors/asterix/cat.py --category 63` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT63_PORT` | EUROCONTROL CAT-063 Ed.1.7 jutiklio būsenos pranešimai (jutikliai, maitinantys CAT-062 sekiklį) |
| `vendors/asterix/cat.py --category 65` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT65_PORT` | EUROCONTROL CAT-065 Ed.1.6 SDPS paslaugos būsenos pranešimai (SDPS pusės partneris CAT-062, tas pats ryšys kaip CAT-019 su CAT-020) |
| `vendors/asterix/cat.py --category 150` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT150_PORT` | EUROCONTROL CAT-150 Ed.3.0 MADAP plano serverio skrydžio duomenų pranešimas (Maastrichto UAC paveldėtas skrydžio plano paskirstymas/koreliacija/konflikto duomenys; šiame leidime pozicijos lauko nėra) |
| `vendors/asterix/cat.py --category 205` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT205_PORT`; nustatykite `CAT205_SITE_LAT/LON` kartezinių pranešimų georeferencijai | EUROCONTROL CAT-205 Ed.1.0 radijo krypties nustatymo pranešimai (RDF tinklas triangulizuoja radijo siųstuvo poziciją, dažniausiai orlaivio VHF radiją) |
| `vendors/asterix/cat.py --category 240` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT240_PORT` | EUROCONTROL CAT-240 Ed.1.3 radaro vaizdo perdavimas (žalias prieš-plot-ekstrakcijos signalo lygio vaizdas, ne taikinio pranešimas; pranešimai gali nešti iki ~64KB vaizdo duomenų) |
| `vendors/asterix/cat.py --category 247` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT247_PORT` | EUROCONTROL CAT-247 Ed.1.3 versijos numerio mainai (šaltinis praneša, kurį kiekvienos ASTERIX kategorijos leidimą jis siunčia) |
| `vendors/sapient/flex335.py` | TCP klausytojas arba klientas | Kraštinis mazgas jungiasi prie `SAPIENT_LISTEN_PORT`, arba nustatykite tarpinės programinės įrangos `SAPIENT_HOST/PORT`; nuotoliniai klausytojai reikalauja leistino šaltinio CIDR | BSI FLEX 335 v2 kadravimas ir viešas SAPIENT protobuf poaibis |
| `nffi.py` | Zenoh prenumeratorius/vertėjas | Leidėjas rašo vieną pilną XML dokumentą į `…/raw/nffi/{source-id}` | NATO NFFI / ADatP-36 (STANAG 5527) XML poaibis |
| `vendors/stanag/stanag.py --proto 4586` | TCP klientas | Nustatykite CUCS/VSM `STANAG4586_HOST/PORT`; patvirtinkite VSM ICD prieš pasirinkdami `STANAG4586_PROFILE=legacy_ed3_approx` | Istorinis diegimo išdėstymas, numatytai išjungtas; nesiūlomas kaip bendras STANAG 4586 dekoderis |
| `vendors/stanag/stanag.py --proto 4607` | Zenoh raw prenumeratorius | Tiltas patalpina pilnus paketus į `…/raw/stanag_4607/**`; STANAG apibrėžia pranešimą, ne nešėją | NATO GMTI (Ground Moving Target Indicator) formatas — Mission/Dwell/Job Definition/Platform Location segmentai, po vieną takelį kiekvienam Target Report |
| `vendors/stanag/stanag.py --proto 4609` | SRT/KLV įėjimas | Nustatykite `STANAG4609_SRT_URL` judesio vaizdo metaduomenų srautui | MISB ST 0601 KLV local-set poaibis per STANAG 4609 judesio vaizdą; SRT yra sukonfigūruotas transportas, ne KLV schemos dalis |
| `vendors/stanag/stanag.py --proto 5516` | UDP klausytojas | Nustatykite `STANAG5516_PORT` (numatytai 3010); šliuzas siunčia JREAP-C įkapsuliuotą Link 16 J-seriją | MIL-STD-6016F / STANAG 5516 Ed.5 J2.2/J2.5/J3.2/J3.5/J3.7 poaibis per JREAP-C (MIL-STD-3011) |

`stanag.py` sujungia visus keturis STANAG variantus, kuriuos kalba EFDI, į
vieną failą (dekodavimas ir, kur taikoma, kodavimas kartu) —
`--proto {4586,4607,4609,5516}` pasirenka, kurį iš jų paleisti konkrečiame
procese; laidinių pranešimų formas žr. `proto/stanag.proto`.

Visi dvidešimt septyni ASTERIX vertėjai taip pat priima `--zenoh-raw` (arba
atitinkamą `CATNN_ZENOH_RAW=1`) tiksliam pilnam kadrui į `…/raw/asterix/catNN`.
Paleidikliai automatiškai pasirenka šį režimą kategorijoms, išvardytoms
`ASTERIX_CATEGORIES`, kai sukonfigūruotas bendras UDP įėjimas arba upstream
Zenoh ASTERIX tiltas.

VERA-NG pasyvūs jutikliai, teikiantys CAT-34 ir CAT-48, naudoja tą patį
neapdoroto ASTERIX kelią; jiems nereikia VERA-specifinio tilto. Kiekvienam
pranešėjui suteikite unikalią SAC/SIC porą ir palikite `CAT34_RADAR_NAME`
tuščią, kai Giraffe ir VERA šaltiniai dalinasi vienu srautu, kad jų vieta,
būsena, aprėptis ir taikinio būsena liktų nepriklausomai identifikuoti kaip
`RADAR SACx/SICy`. Pirmenybę teikite gyviems CAT-34 I034/120 vietos pozicijos
ir I034/100 aprėpties reikšmėms. Prieš eksploatacinį naudojimą užfiksuokite
reprezentatyvius kadrus ir patvirtinkite gamintojo CAT-34/CAT-48 leidimus ir
UAP pagal sukonfigūruotus Ed.1.29/Ed.1.32 dekoderius. Pasyviam jutikliui
negalima priskirti dirbtinio besisukančio šlavimo: EFDI atkuria šlavimo
judesį tik tada, kai šaltinis iš tikrųjų siunčia atitinkamus CAT-34 laiko
pranešimus.

ASTERIX yra bitų lygio stebėsenos mainų šeima; kategorija ir leidimas turi
sutapti su gamintoju. EUROCONTROL publikuoja CAT-010 paviršiaus judėjimui,
CAT-021 ADS-B taikinių pranešimams, CAT-062 sistemos takeliams ir CAT-240
žaliam radaro vaizdui. CAT-240 nėra žemėlapio-takelio srautas ir prieš
TAK/SitaWare publikavimą reikalauja radaro vaizdo apdorojimo. Žr.
[EUROCONTROL ASTERIX katalogą](https://www.eurocontrol.int/asterix),
[CAT-010 Ed.1.1 specifikaciją](https://www.eurocontrol.int/sites/default/files/service/content/documents/nm/asterix/cat010-asterix-monoradar-surface-movement-data-part-7.pdf),
[CAT-021](https://www.eurocontrol.int/publication/cat021-eurocontrol-specification-surveillance-data-exchange-asterix-part-12-category-21),
[CAT-062](https://www.eurocontrol.int/publication/cat062-eurocontrol-specification-surveillance-data-exchange-asterix-part-9-category-062)
ir [CAT-240](https://www.eurocontrol.int/publication/cat240-eurocontrol-specification-surveillance-data-exchange-asterix).

### Išvesties temos: `/sapient`, `/json`, `/proto`, `/raw`

Kiekvienas dekoduotas takelis publikuojamas ant vieno objekto rakto su
keturiomis susijusiomis temomis. Jos neša tą patį įvykį skirtingu tikslumu,
todėl vartotojas prenumeruoja tik vieną temą ir ignoruoja likusias. Niekas
nėra numanomas — vartotojas, skaitantis raktą, visada žino, kas yra baitai.

```
{prefix}/{pod}/{domain}/{source}/{modality}/{affiliation}/{entity}/{type}/{id}/{view}
```

| Tema | Temos priesaga | Zenoh kodavimas | Turinys |
|---|---|---|---|
| SAPIENT | `…/{id}/sapient` | `application/protobuf` | BSI Flex 335 v2 `SapientMessage`. **Magistralės sutartis.** |
| JSON | `…/{id}/json` | `application/json` | Plokščias JSON objektas. Tik dekoderio modeliuojami laukai. |
| Protobuf | `…/{id}/proto` | `application/protobuf` | Tipizuota žinutė iš protokolo `.proto` (`compose/protocols/`). |
| Raw | `…/{id}/raw` | `application/protobuf` | `RawEnvelope`, apgaubiantis **originalius laidinio baitus**, nepakeistus. |

**Kurią temą naudoti.** Numatytai naudokite `/sapient`: tai sutarta sutartis
duomenims, paliekantiems magistralę. `/proto` naudokite, kai reikia pilno
protokolo-specifinio jutiklio detalumo, kurio SAPIENT nemodeliuoja, o `/raw` —
kai reikia lauko, kurio EFDI visai nedekoduoja, arba norite paleisti tiekėjo
paties dekoderį ant tikslių baitų. `/json` skirtas žmonėms ir vartotojams,
negalintiems susieti protobuf vykdymo laiko.

**Originalūs turiniai yra tiksliai baitas į baitą.** Jie perpakuojami, bet
niekada perkoduojami:

- ASTERIX — po vieną savarankišką duomenų bloką kiekvienam pranešimui: CAT
  baitas ir 2 baitų ilgio antraštė pridedami iš naujo, todėl bet koks
  standartinis ASTERIX dekoderis jį perskaito.
- SAPIENT — originalus BSI Flex 335 v2 `SapientMessage`, jau be 32 bitų
  ilgio priešdėlio.
- STANAG 4609 — žalias MISB KLV paketas.

`RawEnvelope` (`../compose/protocols/proto/raw_envelope.proto`) neša
`protocol`, `profile` (pvz., `cat048`, `misb-st0601`), `content_type` ir
`payload` baitus.

**Tikslumo išlyga.** `/sapient`, `/json` ir `/proto` yra tik tiek pilni, kiek
pilnas dekoderis. Kai reikšmės negalima atvaizduoti tikslinėje sutartyje, ji
išmetama iš tos temos ir užrašoma `protobuf encode failed …` eilutė — vienos
temos nesėkmė niekada neblokuoja kitų. `/raw` yra vienintelė tema, kuri
niekada neprarandama lauko.

Visos keturios temos yra po podo pirminio publikavimo priešdėliu, todėl esama
`${DATA_TOPIC_ROOT}/**` routerio ACL jau jas apima — naujai temai pridėti
ACL keisti nereikia.

#### Suderinamumas su išoriniu katalogu

Kai kurie aukštesnio lygio portalai atskiria sertifikato pagrindu paremtą
prizę nuo žmogui suprantamo tiekėjo alternatyvaus vardo. Autentifikuotą
`whoami`/identiteto atsakymą laikykite autoritetingu: alternatyvus vardas yra
rodomi metaduomenys ir neturi pakeisti sertifikato prizės Zenoh raktuose,
nebent aukštesnio lygio ACL tai aiškiai leidžia.

Bandomojo portalo registras priima tikslius temos raktus, ne Zenoh `*` ar `**`
išraiškas. Todėl didelio dažnio kolekcijos leidėjai identitetą laiko
turinyje ir naudoja stabilius susijusius raktus:

```text
.../aircraft/tracks/v1          JSON
.../aircraft/sapient/tracks/v1  BSI FLEX 335 v2 SAPIENT protobuf
.../aircraft/proto/tracks/v1    šaltinio-specifinis protobuf
```

`publish_collection()` užtikrina vieną kodavimą kiekvienam tiksliam raktui
ADS-B ir sujungtoms orlaivių kolekcijoms. Objekto-lygio temos lieka naudingos
magistralėse, kurių katalogas palaiko šablonus, bet neturi būti vienintelė
išvestis, kai išorinis katalogas reikalauja tikslios registracijos.

### Trečiųjų šalių schemos

`compose/protocols/vendors/sapient/sapient_msg/` neša BSI Flex 335 v2.0
(SAPIENT) `.proto` schemas tiksliai taip, kaip yra
[github.com/dstl/SAPIENT-Proto-Files](https://github.com/dstl/SAPIENT-Proto-Files)
(`bsi_flex_335_v2_0/`) — **šių failų nekeiskite**; tai aukštesnio lygio
sutartys, ir vietinis pakeitimas tyliai atskirtų EFDI laidinio formato nuo
standarto, kurį jis teigia kalbantis. Vietoj to atnaujinkite iš naujo iš
aukštesnio lygio. Licencijuota Apache License 2.0 (žr. `sapient_msg/LICENCE.txt`,
kuri leidžia naudoti, keisti ir platinti, įskaitant komercinį/gynybos
naudojimą, kol licencija ir autorių teisių pranešimai išlaikomi); British
Standards Institution išlaiko BSI Flex 335 nuosavybę ir autorių teises,
publikavimo teises turi BSI Standards Ltd.

Ji gyvena `compose/protocols/vendors/sapient/` kataloge, o ne tiesiogiai
`compose/protocols/proto/`, nes tas katalogas skirtas EFDI *pačios* sutartims,
o tai — kažkieno kito: ji neša savo paketą (`sapient_msg.bsi_flex_335_v2_0`) ir
vidinius importavimo kelius `sapient_msg/bsi_flex_335_v2_0/<file>.proto`, kurie
išsisprendžia tik jei šis katalogas yra savas protoc include root, todėl
`scripts/generate-protobuf.sh` perduoda jį kaip antrą `-I` root šalia `-I
compose`. EFDI ir skaito, ir rašo SAPIENT: `compose/protocols/vendors/sapient/flex335.py`
dekoduoja gaunamą SAPIENT ranka rašytu protobuf skaitytuvu (lauko numeriai
patikrinti pagal šiuos failus) ir tame pačiame faile koduoja siunčiamus
takelius į tikrą `SapientMessage`/`DetectionReport`, todėl vartotojui reikia
suprasti tik SAPIENT, o ne kiekvieną šaltinio protokolą.

### Šaltinio-specifiniai tiltai

| Tiltas | Galinio taško elgsena | Reikalinga konfigūracija |
|---|---|---|
| Bendras UDP | Išsaugo kiekvieną datagramą ir saugiai automatiškai išsiunčia pilnus ASTERIX kadrus | `UDP_INGRESS_PORT`, pasirinktinai bind/multicast/šaltinio filtras ir ASTERIX išsiuntimo kategorijos |
| dronuradaras.lt | Apklausia savo fiksuotą viešą HTTPS API | Nereikia |
| meteo.lt | Apklausia fiksuotą viešą HTTPS API | Pasirinktinai vietos/dažnis |
| SitaWare HQ REST įėjimas | Apklausia diegimui specifinį resursą | URL, kredencialai ir tikras `SITAWARE_API_PATH`; universalaus vienetų URL nėra |
| Takelio sujungimas | Prenumeruoja vietines Zenoh temas | Nėra išorinio galinio taško; pradeda veikti, kai atvyksta normalizuoti takeliai |

#### Radaro operatoriaus UDP relė

Nukopijuokite `scripts/radar_udp_relay.py` į radaro operatoriaus Windows
kompiuterį. Jis neturi trečiųjų šalių priklausomybių. Jei radaras siunčia UDP
į vietinį prievadą 50048, pavyzdžiui, paleiskite:

```powershell
py .\radar_udp_relay.py --listen-port 50048
```

Relė persiunčia kiekvieną datagramą nepakeistą į `asusrog.efdi.ltu:50000`.
Perrašykite `--destination-host`, kai mesh DNS nepasiekiamas. Sukonfigūruokite
šį routerį su `UDP_INGRESS_PORT=50000`. Bendras imtuvas išsaugo kiekvieną
datagramą savo neapdorotoje Zenoh temoje ir automatiškai išsiunčia tik
protokolus, kurių kadravimas nedviprasmiškas. UDP 50034 ir 50048 lieka
atskiri determinuoti CAT-034 ir CAT-048 klausytojai.

EFDI nešiojamame kompiuteryje patikrinkite srautą, neperimant UDP lizdo
nuosavybės:

```bash
./scripts/capture-radar-udp.sh
./scripts/capture-radar-udp.sh any giraffe-50000.pcap
```

Pirma komanda rodo paketo baitus; antra išsaugo pilną paketo fiksavimą
darbui su dekoderiu vėliau. Abi naudoja tcpdump ir gali veikti, kol bendras
UDP įėjimas prijungtas prie prievado 50000.

### Išvesties sluoksniai

| Sluoksnis | Automatinis įėjimas | Išorinė konfigūracija |
|---|---|---|
| CoT/TAK išvestis | Prenumeruoja atitinkamas normalizuotas Zenoh temas | TAK TCP/mTLS serveris, arba ATAK/WinTAK UDP paskirties taškas |
| CoT imtuvas | Konvertuoja prijungtą TAK ar SitaWare CoT srautą į Zenoh | Klausymo prievadas ar nuotolinis serveris; TAK naudoja TAK išduotus mTLS kredencialus |
| SitaWare HQ NVG | Palaiko automatinę normalizuoto takelio momentinę nuotrauką | HQ sukonfigūruotas apklausti EFDI URL; TLS ir dedikuoti kredencialai reikalingi už izoliuotos laboratorijos ribų |

### C2 į Zenoh ir atgal

Išvestis ir įvestis yra atskiros paslaugos. TAK ar SitaWare išvesties
įjungimas tyliai neįjungia atvirkštinio kelio.

#### TAK Server

Zenoh → TAK kryptimi sukonfigūruokite `TAK_HOST/TAK_PORT` ir pasirinkite
`tak_layer` (`layers/tak_layer.py`). Jis prenumeruoja normalizuotas Zenoh
temas ir siunčia CoT per TCP/mTLS į TAK Server. TAK išduoti kliento
kredencialai reikalingi, kai `TAK_TLS=1`. TAK → Zenoh kryptimi pasirinkite
`tak-bridge` (`bridges/tak_bridge.py`), kuris normalizuoja gaunamą CoT srautą
atgal į magistralę. Pirmenybę teikite stabiliam DNS `TAK_HOST`; jei TAK
serverio sertifikatas turi kitokį paveldėtą DNS SAN, nustatykite
`TAK_TLS_SERVER_NAME` į tą SAN, kad vardo tikrinimas liktų įjungtas.

#### SitaWare

Zenoh → SitaWare HQ kryptimi pasirinkite `sitaware_layer`
(`layers/sitaware_layer.py`) ir sukonfigūruokite HQ NVG Import Subscription
apklausti autentifikuotą NVG 2.0.2 srautą, kurį jis teikia.

SitaWare HQ → Zenoh kryptimi gaukite tikrą REST resursą iš diegimo ICD:

```dotenv
SITAWARE_URL=https://sitaware.example
SITAWARE_USER=<vykdymo-vartotojas>
SITAWARE_PASS=<vykdymo-paslaptis>
SITAWARE_API_PATH=/<dokumentuotas-resursas>
SITAWARE_TLS_VERIFY=1
```

Pasirinkite `sitaware`; jis publikuoja normalizuotus vienetus žemiau
`…/{domain}/sitaware/c2/{affiliation}/{entity}/{type}/{id}/sapient`.

Dabartinis vykdymo laikas laiko SitaWare HQ REST ir NVG kelius atskirus. Jei
diegimas eksportuoja NFFI vietoj to, publikuokite pilnus NFFI XML dokumentus
į `…/raw/nffi/{source-id}` ir paleiskite nepriklausomą `nffi` vertėją.

Visi gauti įrašai lieka gaminančio podo temoje. Autorizuoti federacijos
maršrutai gali perduoti tą temą kitiems partnerių routeriams, kurių TAK ir
SitaWare išvesties sluoksniai automatiškai vartoja normalizuotas temas.
Adapteris niekada neturi rašyti tiesiai į kito partnerio temą.

Operatoriaus pusės konfigūracija aprašyta žingsnis po žingsnio
[§8 C2 ↔ Zenoh abikryptė prijungimo instrukcija](#8-c2--zenoh-abikryptė-prijungimo-instrukcija)
žemiau. Trumpai: TAK Server reikalauja dedikuoto kliento identiteto,
teisingų IN/OUT grupių ir TAK išduoto sertifikato; SitaWare HQ NVG įėjimas
sukuriamas per **SitaWare Communication → NVG → NVG Import Subscriptions**;
o licencijuotam SitaWare CoT Gateway reikia vieno TCP vaidmens, EFDI galinio
taško, patvirtinto eksporto sluoksnių rinkinio ir aiškios `EFDI Live Tracks`
išimties. Produkto ekranų, kurių nėra įdiegtoje licencijoje/leidime, negalima
pakeisti spėjamu REST keliu.

### Kliento SDK — jungimasis prie podo (`clients/`)

Šis skyrius skirtas žmonėms, kurie **vartoja** podą: publikuoja duomenis į
EFDI magistralę ir gauna duomenis iš jos, savo kalba ir įrankiais —
partneriams, integruojantiems su jūsų podu, ne jutikliams/protokolams,
sujungtiems į jį (tai likusi dokumento dalis). Kodas gyvena `clients/`:

```text
clients/
├── connect/             minimalus "sertifikatų komplektas -> Zenoh sesija" pagalbininkas kiekvienai kalbai
├── examples/
│   ├── modern/          idiomatiškas pub/sub/request-reply kiekvienai kalbai
│   ├── military-legacy/ senesnės įrankių grandinės, offline/izoliuotos, failo/HTTP atsarginiai variantai
│   └── bridges/         naudokite protokolą, kurį jau kalbate — jokio Zenoh kodo jūsų programoje
└── README.md
```

| Jūs esate… | Naudokite |
|---|---|
| Modernus kūrėjas (Python/TS/Go/Rust/Java/C++) | `examples/modern/<lang>/` |
| Senesnėje / mažiau paplitusioje sistemoje (C, Java 8, .NET Framework, MATLAB) | `examples/military-legacy/` |
| Kalbate protokolą, kurį jau turite (HTTP, failai) — jokio Zenoh kodo | `examples/bridges/` |
| Tiesiog norite minimalaus prisijungimo fragmento | `connect/<lang>/` |

#### Modelis per 30 sekundžių

Podas veikia kaip **Zenoh routeris**; klientas jam kalba kaip **Zenoh
klientas per mTLS**. Trys operacijos, tiek ir yra visas API:

1. **Publikuoti** (`put`) raktus po **jūsų vardų sritimi** — pvz.,
   `release/<jūs>/sensors/temp`.
2. **Prenumeruoti** (`sub`) raktus, kuriuos leidžiama skaityti — savo, plius
   `release/<partner>/**` duomenims, kuriuos siunčia partneris (dvišalis
   ryšys).
3. **Užklausti** (`get`) naujausios/istorinės rakto reikšmės (pasirinktinai).

Raktai yra brūkšneliais atskirti keliai (`a/b/c`); prenumeratos naudoja `*`
(vienas segmentas) ir `**` (bet koks gylis).

Kiekvienas pavyzdys skaito tuos pačius penkis dalykus iš **aplinkos
kintamųjų**, todėl kredencialai niekada neįkoduojami tiesiogiai:

| Env kintamasis | Kas tai yra | Pavyzdys |
|---|---|---|
| `EFDI_ROUTER` | podo Zenoh galinis taškas | `tls/127.0.0.1:7447` (podas jūsų kompiuteryje) |
| `EFDI_CERT` | jūsų mTLS kliento sertifikatas (PEM) | `/etc/efdi/mycert.pem` |
| `EFDI_KEY` | jūsų mTLS privatus raktas (PEM) | `/etc/efdi/mykey.pem` |
| `EFDI_CA` | CA šaknis, pasirašanti routerį (PEM) | `/etc/efdi/ca-root.pem` |
| `PARTNER_NAMESPACE` | priešdėlis, kurį valdote (publikuokite po juo) | `release/acme` |

`scripts/gen-certs.sh <namespace>` įrašo juos į `compose/certs/`
(`<namespace>-cert.pem`, `<namespace>-key.pem`, `efdi-ca-root.pem`); žemiau
esančiam vartotojui EFDI administratorius perduoda tų pačių trijų failų
kopiją už sistemos ribų. Jei podas yra vartotojo paties mašinoje,
`EFDI_ROUTER` yra `tls/127.0.0.1:7447`; per mesh — to serverio mesh IP.

Taikinys **Zenoh 1.9.0** visur (flotilei fiksuota versija — žr.
`compose/docker-compose.yml`); naudokite atitinkamos didžiosios versijos
kliento biblioteką (`eclipse-zenoh`/`zenoh-c`/`zenoh-cpp`/`zenoh-go`/`zenoh-java`/`zenoh`
crate/`zenoh-ts`, visos 1.x).

#### Vienintelė prisijungimo keblybė (kiekvienas nuosavas ryšys su ja susiduria)

Zenoh TLS konfigūracija turi būti įterpta kaip **vienas visas blokas**
`transport/link/tls`, su **`enable_mtls: true`**. Sub-raktų nustatymas po
vieną (`transport/link/tls/connect_certificate` ir t.t.) tyliai
**neįjungia** kliento sertifikato siuntimo kelio Zenoh 1.x — sesija
atsidaro, bet routeris atmeta klientą, arba jis prisijungia tik skaitymui.
Kiekvienas `connect/` pagalbininkas sukuria *visą* bloką
(`root_ca_certificate` / `connect_certificate` / `connect_private_key` /
`enable_mtls` / `verify_name_on_connect`) kaip vieną dokumentą ir pritaiko
jį vienu iškvietimu — kalbai specifinis mechanizmas skiriasi
(`zc_config_from_str` C kalboje, `Config::from_str` C++, `InsertJson5("transport/link/tls", …)`
Go, `Config.fromJson5` Java, `insert_json5(...)` Rust, vienas
`conf.insert_json5("transport/link/tls", …)` Python), bet taisyklė ta pati
visur.

Taip pat: kai routerio sertifikato SAN sieja **IP/mesh adresą**, o ne DNS
vardą, kuris rinkis, nustatykite `verify_name_on_connect`/`EFDI_VERIFY_NAME`
į `false` (podo vietinis routeris `127.0.0.1` to reikalauja; DNS-vardu
nuotolinis routeris palieka `true`).

#### Tiltai — kalbėkite su podu protokolu, kurį jau kalbate

**Tiltas** yra mažas procesas, kuris pats yra Zenoh mTLS klientas, bet
vartotojui rodo **kitokį protokolą** — HTTP, stebimą katalogą. Programa
niekada nesusieja Zenoh bibliotekos ir neturi jokio Zenoh kodo; ji kalba
protokolą, kurį jau žino, o tiltas atlieka Zenoh dalį. Tai kelias
paveldėtoms/gynybos organizacijoms, kurios negali arba nenori susieti
`eclipse-zenoh`: MATLAB, PLC, senas .NET Framework, Java 8, izoliuotos
sistemos — bet kas, kas gali padaryti HTTP užklausą ar įrašyti failą.

| Naudokite **nuosavą klientą** (`connect/` + `examples/modern/`) | Naudokite **tiltą** |
|---|---|
| Galite susieti Zenoh klientą (Python/Go/Rust/Java/C++) | Negalite susieti (įrankių grandinė, politika, sertifikavimas) |
| Norite mažiausio delsimo, pilno pub/sub/query | Norite jokio Zenoh kodo programoje |
| Moderni kalba, kontroliuoja build | MATLAB / PLC / senas .NET / izoliuota / tik failai |
| Ilgai gyvenančios in-process prenumeratos | "HTTP iškvietimas" ar "failo įrašymas" — tiek ir yra |

Tiltas laiko vartotojo mTLS kliento identitetą, todėl jo paprasto teksto
pusė (HTTP, stebimas katalogas) yra neautentifikuotos durys į magistralę —
paleiskite jį **kartu su vartojančia programa, prijungtą tik prie
`127.0.0.1`**. Jei programa yra kitame serveryje, dėkite tiltą šalia *tos*
programos, nukreiptą į podo mesh IP — pasitikėjimo riba persikelia į
tiltas↔podas ryšį (vis dar mTLS), bet paprasto teksto pusė niekada neturi
būti pasiekiama iš nepatikimo tinklo. Abu žemiau esantys tiltai yra tik
stdlib + `eclipse-zenoh` (jokio web framework, jokios failų stebėjimo
bibliotekos) ir siunčiami su pasirinktinu `Dockerfile`, veikiančiu kaip
compose sidecar.

**`bridges/file-drop/`** — keiskitės duomenimis kaip failais kataloge:
universaliausias kelias MATLAB, PLC/SCADA, paveldėtam .NET, shell
konvejeriams ir visiškai izoliuotiems kraštams. Failas, įrašytas po
`OUTBOX_DIR`, publikuojamas raktu, suformuotu iš jo kelio santykinai su
outbox (`OUTBOX_DIR/sensors/temp` → `<namespace>/sensors/temp`); failas tada
perkeliamas į `OUTBOX_DIR/.sent/`. Gaunami pavyzdžiai, atitinkantys
`SUB_KEYEXPR`, įrašomi į `INBOX_DIR` kaip failai, pavadinti pagal jų raktą
(brūkšneliai → `__`) plius milisekundžių laiko žyma, įrašomi atomiškai
(laikinas vardas, po to pervardijimas), kad skaitytuvas niekada neskaitytų
pusiau įrašyto failo. Apklausa pagrįsta, tik stdlib (jokios inotify
priklausomybės); reguliuokite `POLL_SECONDS` (numatytai 1s); palikite
`SUB_KEYEXPR` tuščią, kad išjungtumėte gaunamą pusę.

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
export OUTBOX_DIR=./outbox INBOX_DIR=./inbox SUB_KEYEXPR='release/<partner>/**'
python3 bridge.py
```

**`bridges/rest-http/`** — paprastas HTTP, `curl`, MATLAB (`webwrite`),
senam .NET (`HttpClient`), Java 8 (`HttpURLConnection`), shell skriptams
arba PLC HTTP blokui. Numatytai prisijungia tik prie `127.0.0.1`
(`BRIDGE_BIND`/`BRIDGE_PORT`).

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
python3 bridge.py                 # veikia http://127.0.0.1:8080

curl -X POST http://127.0.0.1:8080/pub/sensors/temp -d '21.5'                 # publikuoti
curl 'http://127.0.0.1:8080/sub/sensors/temp?count=3'                          # gauti N (blokuoja)
curl -N http://127.0.0.1:8080/stream/sensors/temp                              # SSE srautas
WEBHOOK_URL=https://my-system.local/ingest WEBHOOK_KEYEXPR='release/<partner>/**' python3 bridge.py  # išeinantis webhook
```

Paprastas kelias (`sensors/temp`) apribojamas iškviečiančiojo vardų srityje;
pilnas raktas, kurį iškviečiantysis turi teisę skaityti (pvz.,
`release/<partner>/...`), praeina kaip yra. Gautas tekstas grįžta kaip
`"text"` JSON atsakyme, arba `"b64"`, jei baitai nėra tinkamas UTF-8.

#### Kariniai / paveldėti / mažiau paplitę stekai (`examples/military-legacy/`)

Fiksuotam JDK 8, .NET Framework 4.x, MATLAB, C89/C99 ir **izoliuotoms**
organizacijoms, kurios negali pasiekti interneto, negali `pip install` ir
dažnai negali visai susieti nuosavo Zenoh kliento. Dirbkite iš viršaus žemyn,
sustokite prie pirmos eilutės, kuri teisinga:

| Jei… | Naudokite | Kodėl |
|---|---|---|
| Nuosavas Zenoh bindingas kompiliuojasi ir susieja (turite `zenoh-c`, C kompiliatorių, politika leidžia) | **nuosavas** — `c99/` | mažiausias delsimas, pilnas pub/sub/query, jokio papildomo proceso |
| Galite padaryti **HTTP užklausą** (bet kokia kalba) | **REST tiltas** — `bridges/rest-http/` + `java8/`, `dotnet-framework/`, `matlab/` pavyzdžiai | jokio Zenoh kodo; veikia bet kurioje sistemoje su HTTP klientu |
| Galite tik **skaityti/rašyti failus** (užrakinta dėžė, SCADA/PLC, shell konvejeris) | **file-drop tiltas** — `bridges/file-drop/` + `matlab/receive_filedrop.m` | universaliausias kelias — jei gali įrašyti failą, gali publikuoti |

```text
paveldėta programa  ──HTTP / failai──▶  tiltas (localhost, laiko mTLS)  ──Zenoh mTLS──▶  podo routeris
```

Java/.NET/MATLAB pavyzdžiai **nėra** Zenoh klientai — ~80 eilučių programos,
naudojančios tik kalbos stdlib prieš vietinį tiltą; jos kompiliuojasi
įrankiais, jau esančiais dėžėje (`javac`, `csc.exe`, MATLAB redaktorius),
jokio Maven, NuGet ar Gradle.

**Offline / izoliuota, vieną kartą, visiems keturiems stekams:**

1. Gaukite dalis per sneakernet: patį podą (perduotą operatoriaus), mTLS
   sertifikatų komplektą (`mtls.cert.pem`, `mtls.key.pem`, `ca-roots.pem`,
   vardų sritis) ir — tik tilto Python vykdymo laikui — vendoruotą
   `eclipse-zenoh` wheelhouse:
   ```sh
   # PRIJUNGTOJE mašinoje, atitinkančioje izoliuotos dėžės OS/architektūrą/python:
   pip download eclipse-zenoh==1.9.0 -d zenoh-wheelhouse/
   # perkelkite zenoh-wheelhouse/ per, tada IZOLIUOTOJE dėžėje:
   pip install --no-index --find-links zenoh-wheelhouse/ eclipse-zenoh==1.9.0
   ```
   Pačiai paveldėtai programai vendoruoti nieko nereikia — tai ir yra
   ėjimo per tiltą esmė.
2. Viskas yra localhost: podas, tiltas ir programa visi veikia vienoje
   dėžėje. Jokio DNS, jokio proxy, jokio interneto; vienintelis šuolis yra
   tiltas→podas (`tls/127.0.0.1:7447`).
3. **Laikrodžio sinchronizacija yra tylusis žudikas.** mTLS atmeta
   sertifikatus, kurių galiojimo langas neapima *dabar*. Dėžė su negyva RTC
   ar be NTP nuklys, ir **tilto** sesija podui nepavyks su neaiškia
   "sertifikatas dar negalioja/pasibaigė" klaida — net jei programa→tiltas
   HTTP iškvietimas atrodo gerai. Simptomas: tiltas paleidimo metu užrašo
   TLS klaidą ir niekada neparodo `bridge on http://…`. Pirmiausia
   ištaisykite laikrodį (`date`; `sudo date -s '2026-06-02 14:30:00'` Linux,
   `w32tm /resync` ar rankiniu būdu Windows), prieš derindami bet ką kita —
   vienas taisymas apima podą + tiltą, nes jie dalinasi dėže.

**`military-legacy/c99/`** — grynas C99, tik libc + `libzenohc` + `Makefile`
(pačiam pavyzdžiui CMake nereikia, nors `zenoh-c` build'ui reikia), prieš
[`zenoh-c`](https://github.com/eclipse-zenoh/zenoh-c) 1.9.0 tiesiogiai (jokio
tilto — tai nuosavas klientas). Kiekvienas pavyzdys yra savarankiškas `.c`
su prisijungimo logika viduje. Reikalauja `zenoh-c`, sukompiliuoto su
`-DZENOHC_BUILD_WITH_UNSTABLE_API=ON` (`zc_config_from_str` įėjimo taškas,
kurio reikia vieno-bloko mTLS konfigūracijai, uždarytas už nestabilaus API) —
sukompiliuoto iš šaltinio, iš paruošto GitHub Releases artefakto arba pilnai
vendoruoto (`cargo vendor`) offline buildams.

```sh
make                        # dinaminis susiejimas
make static                 # statinis susiejimas libzenohc.a -> vienas savarankiškas dvejetainis failas
                             # (macOS statiniam susiejimui taip pat reikia -framework Security -framework CoreFoundation)
./publish                   # vienas JSON pavyzdys; ./publish 50 200 50 pavyzdžių, 200ms tarpais
./subscribe                 # viskas jūsų vardų srityje; ./subscribe 'release/<partner>/**'
```

**`military-legacy/java8/`** — JDK 8 (modernus `zenoh-java` bindingas
reikalauja JDK 17+), per REST tiltą naudojant tik `java.net.HttpURLConnection`.
Jokio Maven/Gradle/jar'ų.

```sh
javac Publish.java Subscribe.java
java Publish sensors/temp '{"temp_c":21.5}'
java Subscribe sensors/temp stream          # tęsti nuolat
```

**`military-legacy/dotnet-framework/`** — .NET Framework 4.x (4.5–4.8, ne
modernus .NET), per REST tiltą naudojant `System.Net.HttpWebRequest` (yra nuo
Framework 2.0; nuspėjamesnis nei `HttpClient` atviro SSE srauto atveju).
Kompiliuokite su `csc.exe` tiesiogiai, arba pridedamu klasikiniu (ne-SDK)
`.csproj` per `msbuild` — nė vienas neliečia NuGet.

```bat
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /out:EfdiBridgeClient.exe Program.cs
EfdiBridgeClient.exe pub sensors/temp {"temp_c":21.5}
EfdiBridgeClient.exe stream sensors/temp
```

**`military-legacy/matlab/`** — MATLAB per `webwrite`/`webread` (REST tiltas,
`publish.m`/`receive_rest.m`) arba paprastą failų I/O (file-drop tiltas,
`receive_filedrop.m`) pačioms užrakintoms dėžėms — jokio toolbox, jokio MEX,
jokio tinklo iškvietimo file-drop kelyje.

```matlab
publish('sensors/temp', '{"temp_c":21.5}')
s = receive_rest('sensors/temp', 'Count', 5, 'TimeoutSec', 60);
receive_filedrop('./inbox', 'Callback', @(key,bytes) disp(key))
```

#### Modernūs kalbos bindingai (`examples/modern/`)

Idiomatiškas pub/sub/request-reply kiekvienai kalbai, kiekvienas suporuojantis
oficialų Zenoh bindingą su mažu `connect/<lang>/` pagalbininku, taikančiu
aukščiau esantį vieno-bloko mTLS konfigūraciją. Visi taikosi į Zenoh 1.9.0;
jei simbolis neišsisprendžia kitoje fiksuotoje mažoje versijoje,
persitikrinkite tos žymos pačius aukštesnio lygio pavyzdžius — tai galioja
kiekvienai žemiau esančiai kalbai ir nekartojama kiekvienam įrašui.

**`modern/python/`** — oficialus `eclipse-zenoh`. `pip install eclipse-zenoh`,
tada `python3 publish.py` / `python3 subscribe.py` / `python3 request_reply.py
{serve,get}`. Windows naudokite `python` (ne `python3`) venv viduje, kitaip
pataiko į sisteminį interpretatorių ir meta `ModuleNotFoundError`.

**`modern/cpp/`** — oficialus [`zenoh-cpp`](https://github.com/eclipse-zenoh/zenoh-cpp),
**tik antraštėse esantis wrapper virš `zenoh-c`** — pirmiausia įdiekite
`zenoh-c` 1.9.0 (nestabilus API įjungtas), tada `zenoh-cpp` 1.9.0, tada
CMake-build pavyzdžius. `find_package(zenohcxx)` nepavykimas reiškia, kad
zenoh-cpp nėra `CMAKE_PREFIX_PATH`; susiejimo nepavykimas ties `libzenohc`
reiškia, kad zenoh-c neįdiegtas — nė vienas nėra API problema.

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build
./build/publish; ./build/subscribe
```

**`modern/go/`** — oficialus bindingas (atsirado su Zenoh 1.9.x "Longwang",
2026 m. balandis) yra **cgo wrapper virš `zenoh-c`**, ne grynas Go — pirmiausia
įdiekite `zenoh-c` 1.9.0 (nestabilus API įjungtas), reikalingas
`CGO_ENABLED=1`, cross-kompiliavimas nepatogus. Importavimo kelias
`github.com/eclipse-zenoh/zenoh-go/zenoh` (senasis viršutinio lygio/`zenoh-net`
0.4.x API apleistas nuo 2020 m. — jo nenaudokite). Grynas Go/be-cgo variantas
šiandien neegzistuoja; jei tai griežtas reikalavimas, naudokite tiltą.

```sh
go run publish.go; go run subscribe.go 'release/<partner>/**'
```

**`modern/java/`** — oficialus `zenoh-java` (JDK 17+ — Kotlin/JVM bindingas).
Kitaip nei Go, publikuotas `zenoh-java-jvm` artefaktas **įtraukia nuosavą
biblioteką kaip JAR resursą**, todėl paprastos Gradle priklausomybės
(`org.eclipse.zenoh:zenoh-java-jvm:1.9.0`) pakanka — atskiro `zenoh-c`
diegimo nereikia. Sugeneruokite wrapper kartą su `gradle wrapper`, tada:

```sh
./gradlew run -Pmain=Publish
./gradlew run -Pmain=Subscribe --args="release/<partner>/**"
```

**`modern/rust/`** — oficialus [`zenoh`](https://crates.io/crates/zenoh)
crate — **grynas Rust** (referencinė implementacija, jokios C bibliotekos
diegti nereikia), asinchroninis (tokio). `cargo build` atsineša `zenoh =1.9.0`
kartu su tokio.

```sh
cargo run --bin publish; cargo run --bin subscribe -- 'release/<partner>/**'
```

**`modern/typescript/`** — oficialus `@eclipse-zenoh/zenoh-ts`. **Šis
architektūriškai skiriasi nuo likusių:** jis neatidaro tiesioginės Zenoh
sesijos per mTLS. Jis kalbasi su `zenoh-plugin-remote-api`, įkeltu viduje
`zenohd`, per **WebSocket** (`ws://`/`wss://`); `Config` priima tik lokatoriaus
eilutę, be jokio kliento-sertifikato/TLS bloko TS pusėje. Mesh pusės mTLS
konfigūruojamas **podo `zenohd`**, ryšiuose, kuriuos daro pats routeris —
papildinys yra pasitikėjimo riba tarp WebSocket kliento ir mesh. Podo
operatorius turi įjungti papildinį (`plugins.remote_api.websocket_port`
`zenohd` konfigūracijoje; numatytai neįjungtas); priešais jį dėkite
`wss://` visur, išskyrus loopback. Jei tipizuoto nuosavo API konkrečiai
nereikia, aukščiau esantis REST/WebSocket tiltas dažnai yra paprastesnis
kelias Node/naršyklės vartotojams.

Env kintamieji skiriasi nuo nuosavų bindingų: `EFDI_WS` (pirmenybė; pvz.,
`ws/127.0.0.1:10000`) arba `EFDI_ROUTER` kaip atsarginis variantas (tas
pats serveris su prievadu `10000`); `EFDI_CERT`/`EFDI_KEY`/`EFDI_CA`
nenaudojami, nebent papildinys priešais turi `wss://` su privačiu CA, tokiu
atveju `NODE_EXTRA_CA_CERTS` (Node) juo pasitiki (naršyklėms CA reikia OS/naršyklės
pasitikėjimo saugykloje).

zenoh-ts pirmiausia taikosi į naršyklę/Deno; **Node** aplinkoje reikia
globalaus `WebSocket`, per `ws` paketą (Node 22+ turi jį natūraliai, todėl
shim tampa no-op; Node 18/20 reikia jo) ir įkeliamo prieš pavyzdį per `tsx`.
Deno nereikia jokio polyfill (`deno run --allow-net --allow-env --allow-read
subscribe.ts`) ir yra aukštesnio lygio palaimintas vykdymo laikas, jei Node
shim'as pasirodo trapus. Rakto sprendimas eina per **WASM** modulį —
įsitikinkite, kad bundler/vykdymo laikas gali įkelti `.wasm` (tsx/Deno tai
daro numatytai).

```sh
npm install
npm run publish; npm run subscribe -- 'release/<partner>/**'
```

### Kiti protokolų kandidatai

| Prioritetas | Protokolas | Naudojimas | Vartai prieš įgyvendinimą |
|---|---|---|---|
| Aukštas | ONVIF Profile M | Kameros analitikos objektai, metaduomenys, geolokacija ir įvykiai | Įrenginio profilis, atradimo/autentifikavimo metodas, pavyzdinis metaduomenų srautas. [ONVIF Profile M](https://www.onvif.org/profiles/profile-m/) |
| Vidutinis | VITA 49.2 | Žali RF/spektro stebėjimai | DSP/geolokacijos etapas, konvertuojantis pavyzdžius į žemėlapiui tinkamas kryptis/pozicijas. [VITA Radio Transport](https://www.vita.com/page-1855484) |
| Vidutinis | STANAG 4607 / 4676 | GMTI ir NATO takelių mainai | Licencijuotas ICD/profilis ir reprezentatyvūs pranešimai; neišvesti išdėstymų |
| Tiekėjo-specifinis | Akustinis/RF counter-UAS API | Kryptys, klasifikacijos, takeliai, jutiklio būsena | Tiekėjo ICD/API schema, koordinačių rėmas, laiko bazė, gyvavimo ciklas ir autentifikavimas |

SAPIENT yra pageidautina vieša, tiekėjo-neutrali counter-UAS jutiklio
sąsaja: MOD-valdoma architektūra standartizuota kaip BSI FLEX 335 ir
publikuoja savo protobuf schemas. Žr.
[oficialias SAPIENT gaires](https://www.gov.uk/guidance/sapient-autonomous-sensor-system)
ir [Dstl schemas](https://github.com/dstl/SAPIENT-Proto-Files). Jos TCP
kadravimas yra keturių baitų mažo endian protobuf ilgis, naudojamas
[oficialaus BSI FLEX 335 v2 test harness](https://github.com/dstl/BSI-Flex-335-v2-Test-Harness/blob/main/SAPIENTMessageProcessor/ByteDataMessageBuilder.cs).

### Hakatono partnerio priėmimo kontrolinis sąrašas

Prieš prijungdami srautą, gaukite:

- protokolą, kategoriją, leidimą/profilį, transportą ir srauto kadravimą;
- gamintojo IP/prievadą arba URL/broker plius kas inicijuoja ryšį;
- autentifikavimo/TLS metodą, neįsipareigojant jokių kredencialų;
- reprezentatyvius pranešimus arba anonimizuotą PCAP, apimantį
  create/update/delete;
- koordinačių atskaitą, kilmę/datumą, aukščio atskaitą, kampus ir vienetus;
- laiko žymas/laiko juostą, atnaujinimo dažnį, stabilius identifikatorius ir
  pasenusio/ištrynimo taisykles;
- klasifikacijos/priklausomybės semantiką ir patikimumo skalę;
- laukiamą maksimalų pranešimo dydį, objektų skaičių ir dažnį.

Jei kategorija/leidimas, kadravimas ar koordinačių atskaita nežinomi,
vertėjas turi atmesti ar karantinuoti srautą, o ne tyliai spėti.

---

## 8. C2 ↔ Zenoh abikryptė prijungimo instrukcija

Kryptys yra nepriklausomos. Užbaikite tik tuos kelius, kurie atskleisti ir
licencijuoti konkretaus diegimo, tada pasirinkite jų paslaugas `./start.sh`.

### 8.1 Patikrinkite bendrą Zenoh pusę

Laikykite kiekvieną Python adapterį nukreiptą į vietinį routerį:

```dotenv
ZENOH_LOCAL_ENDPOINT=tcp/127.0.0.1:7448
```

Nustatykite `ZENOH_FABRIC_ENDPOINT` tik `zenoh-router`, arba naudokite
`ZENOH_FABRIC_ENDPOINTS` JSON masyvą dviem ar daugiau aiškiai sukonfigūruotų
uplink'ų. Tiltai ir sluoksniai tiesiogiai nesijungia prie kintančių backbone
adresų. C2 kilmės įrašai publikuojami po
`{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/...`. Federacijos ACL nusprendžia,
kurie partnerių routeriai gali gauti šią temą.

### 8.2 Zenoh → TAK Server

Sukonfigūruokite TAK TCP paskirties tašką ir pasirinkite `tak-layer`:

```dotenv
TAK_HOST=<tak-serveris>
TAK_PORT=8089
TAK_TLS=1
TAK_TLS_SERVER_NAME=<dns-san-tak-serverio-sertifikate>
TAK_CERT=/vykdymo/kelias/tak-client.pem
TAK_KEY=/vykdymo/kelias/tak-client-key.pem
TAK_CA=/vykdymo/kelias/tak-ca.pem
```

Šie turi būti TAK išduoti kredencialai. Zenoh sertifikatas negalioja TAK
Server. `TAK_HOST` yra stabilus prisijungimo vardas; kai įdiegtas TAK
serverio sertifikatas naudoja kitokį paveldėtą DNS SAN, nustatykite
`TAK_TLS_SERVER_NAME` į tą SAN, o ne išjunkite vardo tikrinimą. Laboratorijos
paprastam tekstui naudokite diegimo sukonfigūruotą TCP prievadą ir palikite
`TAK_TLS=0`. `tak-layer` išvestis yra vienakryptė; įjunkite `tak-bridge`
grįžtamajam srautui.

TAK Server pusėje:

1. Prisijunkite prie TAK Server administravimo UI su administratoriaus
   identitetu.
2. Atidarykite **User Management** ir sukurkite dedikuotą EFDI kliento
   identitetą; nenaudokite pakartotinai žmogaus operatoriaus paskyros.
3. Priskirkite misijos grupes, į kurias EFDI turi publikuoti, ir misijos
   grupes, kurias jis turi stebėti. `efdi-bridge` kliento identitetui
   suteikite plačiausią autorizuotą matomumą, kurį leidžia diegimas, kad ta
   pati CoT sesija galėtų ir publikuoti, ir gauti serveryje matomus
   žymeklius.
4. Naudokite diegimo sertifikatų/registracijos darbo eigą, kad išduotumėte
   kliento sertifikatą tam identitetui, ir eksportuokite jo sertifikatą,
   privatų raktą ir TAK CA grandinę. Dabartinis TAK Server atskleidžia
   vartotojo/grupės ir sertifikatų valdytojo operacijas savo
   [oficialiame API](https://docs.tak.gov/api/takserver); tikslūs mygtukai
   skiriasi tarp file-user, LDAP ir external-identity diegimų.
5. Įdėkite PEM failus vykdymo-tik kataloge EFDI serveryje, įveskite jų
   kelius aukščiau, pasirinkite `tak-layer` `./start.sh` ir patvirtinkite,
   kad identitetas rodomas prisijungęs TAK Server.

### 8.3 TAK Server → Zenoh

Naudokite tą patį TAK išduotą kliento identitetą atvirkštiniam CoT srautui,
paprastai dedikuotą `efdi-bridge` paskyrą/sertifikatą. Pasirinkite
`tak-bridge` ir nukreipkite jį į TAK Server CoT galinį tašką:

```dotenv
TAK_HOST=<tak-serveris>
TAK_PORT=8089
TAK_TLS=1
TAK_TLS_SERVER_NAME=<dns-san-tak-serverio-sertifikate>
TAK_CERT=/vykdymo/kelias/efdi-bridge.pem
TAK_KEY=/vykdymo/kelias/efdi-bridge-key.pem
TAK_CA=/vykdymo/kelias/tak-ca.pem
```

Tiltas naudoja tą patį TAK sesijos modelį kaip paprastas klientas: jei
serveris autorizuoja identitetą abiem kryptims, jis gali publikuoti į TAK ir
prenumeruoti serveryje matomą CoT tuo pačiu metu. Tiltas republikuoja gautus
`<event>...</event>` kadrus į Zenoh ir pažymi juos kaip TAK įėjimą, kad
išvesties CoT sluoksnis jų neatsiųstų atgal į serverį uždarame rate.

### 8.4 Zenoh → SitaWare HQ

Įjunkite `sitaware-hq-nvg`, sukonfigūruokite TLS ir dedikuotus srauto
kredencialus, tada sukurkite HQ NVG Import Subscription, nukreiptą į
gautą `SITAWARE_HQ_NVG_PATH`:

```dotenv
SITAWARE_HQ_NVG_ENABLE=1
SITAWARE_HQ_NVG_BIND=<efdi-adresas>
SITAWARE_HQ_NVG_PORT=8088
SITAWARE_HQ_NVG_PATH=/nvg
SITAWARE_HQ_NVG_USER=<dedikuotas-srauto-vartotojas>
SITAWARE_HQ_NVG_PASS=<vykdymo-paslaptis>
SITAWARE_HQ_NVG_TLS_CERT=/vykdymo/kelias/feed-cert.pem
SITAWARE_HQ_NVG_TLS_KEY=/vykdymo/kelias/feed-key.pem
```

SitaWare HQ viduje spauskite **SitaWare Communication → NVG → NVG Import
Subscriptions**, sukurkite prenumeratą ir įveskite:

```text
Subscription Name:         EFDI Live Tracks
Remote Endpoint:           https://<efdi-adresas-ar-tailscale-ip>:8088/nvg
Target Layer:              efdi-live / EFDI Live Tracks
Request NVG periodically:  yes
Polling Interval:          10 seconds
Reconnect Delay:           90 seconds
Authentication:            enabled; naudokite dedikuotą srauto vartotoją/slaptažodį
Pause Subscription:        no
```

Jei sluoksnio `EFDI Live Tracks` nėra, pirmiausia sukurkite jį. Windows
sistemoje patikimai nustatykite srauto sertifikatą išduodančią CA; po
ryšio testo neišjunkite sertifikatų tikrinimo.

### 8.5 SitaWare HQ → Zenoh

Tam reikia tikro JSON vienetų resurso, dokumentuoto tam HQ diegimui; nespėkite
`/rest/v2/units`. Sukonfigūruokite ir pasirinkite `sitaware`:

```dotenv
SITAWARE_URL=https://<hq-serveris>
SITAWARE_USER=<vykdymo-vartotojas>
SITAWARE_PASS=<vykdymo-paslaptis>
SITAWARE_API_PATH=/<dokumentuotas-resurso-kelias>
SITAWARE_POLL_S=10
SITAWARE_TLS_VERIFY=1
```

Tiltas publikuoja žemiau `…/{domain}/sitaware/rest/{affiliation}/{entity}/
tracks/v1`. Patikrinkite su:

```bash
tail -f "${POD_STATE_DIR:-compose/state}/logs/sitaware.log"
```

SitaWare HQ pusėje administratorius turi įjungti licencijuotą API, sukurti
tik-skaitymo integracijos paskyrą ir suteikti tai paskyrai prieigą prie
tikslaus vienetų/takelių resurso, skirto eksportui. Nukopijuokite šias
keturias reikšmes iš įdiegto produkto API/ICD į perdavimą: bazinį URL,
resurso kelią, autentifikavimo metodą ir atsakymo schemą/versiją. Nėra
saugios bendros viešų HQ meniu paspaudimų sekos šiai operacijai ir nėra
universalaus vienetų resurso; jei administratorius negali identifikuoti to
ekrano/resurso, neįjunkite `sitaware`. Vietoj to naudokite diegimo NFFI ar
CoT Gateway sąsają.

### 8.6 Dalinkitės C2 kilmės duomenimis su partneriais

Nerašykite įrašo iš naujo į kito partnerio vardų sritį. Patvirtinkite, kad
kilmės vardų sritis leidžiama routerio/federacijos politikos ir kad gaunantis
partneris ją prenumeruoja. Jų `cot-*` ar `sitaware-hq-nvg` išvesties
sluoksniai išvers autorizuotas normalizuotas temas taip pat, kaip vietiniai
sugeneruoti jutiklio duomenys.

### 8.7 Eksploatacinio personažo testinis pratimas

Naudokite keturis atskirus identitetus ar klientus teste. Tai eksploataciniai
personažai, ne Zenoh Admin panelio `superadmin`, `admin` ir `readonly`
vaidmenų pakaitalai.

| Personažas | Testinio kliento veiksmas | EFDI paslaugos | Laukiamas rezultatas |
| --- | --- | --- | --- |
| C2 operatorius | TAK/WinTAK/ATAK ar SitaWare HQ operatoriaus paskyra stebi sukonfigūruotą CoT išvestį. | `tak-layer` ir/arba `sitaware-hq-nvg`. | Normalizuoti EFDI takeliai atsiranda autorizuotoje C2 sistemoje. |
| Jutiklio leidėjas | Imtuvo/aptikimo sistema, prijungta prie vietinio Zenoh routerio, publikuoja pilnus kadrus/dokumentus į to protokolo `…/raw/<protocol>/<source-id>` temą. Laboratorijos leidėjui administratorius gali sugeneruoti skriptą **Publish Script** įvedus to leidėjo dabartinį routerio galinį tašką. | Atitinkamas protokolo vertėjas ir norimi C2 išvesties sluoksniai. | Vertėjas sukuria normalizuotus EFDI takelius; C2 sistemos rodo išvestus žymeklius, ne žalią kadrą. |
| Magistralės administratorius | Atskira Zenoh Admin panelio paskyra valdo tik routerio/federacijos konfigūraciją. | Infrastruktūros/admin UI; jutiklio ar C2 srauto nereikia. | Gali atlikti savo priskirtus panelio veiksmus, bet nėra eksploatacinis TAK/SitaWare identitetas. |

Pirmam pratimui naudokite dedikuotą TAK išduotą paslaugos identitetą
`tak-layer` ir patvirtinkite, kad autorizuota C2 sistema gauna normalizuotus
EFDI takelius. Laikykite žalio jutiklio publikavimą atskirame jutiklio
identitete/temoje; jis neturi apsimesti operatoriaus identitetu.

Dabartinis routerio ACL yra vardų-srities apimties, dar ne
personažo/sertifikato apimties. Keturi testiniai klientai įrodo duomenų
srautą ir C2 elgseną; jie **neįrodo** mažiausios privilegijos Zenoh
autorizacijos tarp personažų. Vykdomai personažo prieigai reikia vėlesnio
sertifikato-subjekto ACL dizaino su atskirais kliento kredencialais ir temos
leidimais.

> **ASTERIX leidimai:** įgyvendinti standartiniai UAP yra CAT-010 1.1,
> CAT-020 1.11, CAT-021 2.7, CAT-034 1.29, CAT-048 1.32 ir CAT-062 1.21.
> Patvirtinkite gamintojo leidimą prieš jungdami jį; kitokiam ar
> tiekėjo-specifiniam UAP reikia aiškaus dekoderio profilio.

### 8.8 Zenoh temos schema

```text
{NAMESPACE}/{DOMAIN}/{SOURCE}/{MODALITY}/{AFFILIATION}/{ENTITY}/{TYPE}/{ID}/{VIEW}
```

| Laukas | Reikšmės |
| --- | --- |
| `DOMAIN` | `air`, `land`, `sea`, `space`, `env` |
| `AFFILIATION` | `friendly`, `hostile`, `neutral`, `unknown`, `civ`, `mil` |
| `TYPE` | `aircraft`, `vessel`, `vehicle`, `unit`, `sensor`, `uav`, `radar` |

---

## 9. Naujo jutiklio ar protokolo pridėjimas

Tai žingsnis po žingsnio kelias nuo "turiu naują jutiklį/srautą" iki "jis
automatiškai atsiranda TAK ir SitaWare." Laikoma, kad podas jau įdiegtas ir
veikia (§§1-6 aukščiau).

Perskaitykite [§7 Integracijos](#7-integracijos) pirmiausia, jei dar
neskaitėte — ji paaiškina magistralę, į kurią jungiasi šis vadovas (temos
taksonomiją, keturias išvesties temas, kas jau sujungta). Šis skyrius yra
konkretūs "dabar sukurkite" žingsniai; tas skyrius — nuoroda, kas jau
egzistuoja.

### 9.0 Nuspręskite: tiltas ar protokolas?

- **`compose/bridges/`** — jūsų nauja integracija *jungiasi prie produkto ar
  paslaugos*: apklausia HTTP API, atidaro TCP lizdą į tiekėjo dėžę, klausosi
  UDP prievado konkrečiam įrenginiui. Po vieną failą kiekvienam išoriniam
  dalykui, su kuriuo ji kalbasi.
- **`compose/protocols/`** — jūsų nauja integracija *dekoduoja jau apibrėžtą*
  laidinį formatą, nesusietą su vienu tiekėju (standartą, specifikaciją,
  schemą).

Dauguma naujų jutiklių yra tiltai — fizinis ar tinklinis įrenginys, prie
kurio šis routeris jungiasi tiesiogiai. Jei abejojate, rinkitės `bridges/`;
tai dažnesnis atvejis ir niekam žemiau nesvarbu, kuriame kataloge gyvena
skriptas.

### 9.1 Ar reikia naujos pranešimo schemos, ar tinka esama?

Jei jūsų jutiklis praneša judantį objektą — poziciją, pasirinktinai
greitį/kursą/aukštį/identitetą — jis beveik tikrai tinka esamai bendrai
`NormalizedTrack` schemai (`../compose/protocols/proto/normalized_track.proto`)
ir jums **visai nereikia naujo protobuf darbo**. Praleiskite iki 2 žingsnio.

Naują `.proto` pranešimą apibrėžkite tik tada, kai jūsų duomenys turi
struktūrinius laukus, kurių `NormalizedTrack` iš tikrųjų negali išreikšti
(pvz., daugiataškė zona/plotas, arba domenui specifinė sudėtinė reikšmė).
Jei taip:

1. Pridėkite naują `.proto` failą po `compose/protocols/proto/` — kiekviena
   EFDI-autorystės schema gyvena ten, nepriklausomai nuo to, kuris vertėjas
   ją valdo (tikra vendoruota/licencijuota trečiosios šalies schema, kaip
   SAPIENT ar Sparkplug B laidiniai kontraktai, yra vienintelė išimtis ir
   lieka po `compose/protocols/vendors/<name>/` šalia savo LICENSE) —
   pagal esamą pavyzdį — `geojson_features.proto` yra trumpas pavyzdys.
2. Iš naujo sugeneruokite Python bindingus: `scripts/generate-protobuf.sh`
   (reikia `grpc_tools.protoc` + `protobuf` — jau `compose/requirements.txt`).
   Tai rašo į `compose/generated/`, kuris yra gitignored — kiekvienas
   kūrėjas/diegimas sugeneruoja jį vietiškai, niekas sugeneruotas
   necommitinamas.

### 9.2 Rašykite skriptą

Kiekvienas tilto/protokolo skriptas seka tą pačią formą. Tai pilna, veikianti
nuoroda — `compose/protocols/random/geojson_features.py` (127 eilutės) —
sutrumpinta iki dalių, kurios svarbu:

```python
from namespace_prefix import topic_root
from gateway import open_session, publish_dual
# Naudokite bendrą schemą — naujo .proto nereikia paprastam judančiam objektui:
from protocols.proto.normalized_track_pb2 import NormalizedTrack

TOPIC_ROOT = topic_root()
OUTPUT_TOPIC = TOPIC_ROOT + "/<domain>/<your-source-name>/<modality>/<affiliation>/<entity>"

def normalize(raw: dict) -> dict | None:
    """Paverskite vieną jūsų jutiklio įrašą į bendrą takelio formą.
    Privaloma: _ts (epoch sekundės), _src (jūsų šaltinio vardas), uid (stabilus per-objektą id).
    Viskas kita pasirinktinai — nustatykite tik tai, ką iš tikrųjų turite."""
    return {
        "_ts": time.time(),
        "_src": "your-sensor-name",
        "uid": "YOURSENSOR-" + raw["id"],
        "lat_deg": raw["lat"],
        "lon_deg": raw["lon"],
        # pasirinktinai: "speed_ms", "heading_deg", "baro_alt_m", "callsign", ...
    }

def run() -> None:
    session = open_session()
    for raw in your_data_source():          # apklauskite API, skaitykite lizdą ir t.t.
        record = normalize(raw)
        if record:
            publish_dual(session, OUTPUT_TOPIC, record, NormalizedTrack)
```

Jūsų skriptas pats niekada neimportuoja `zenoh` — `gateway.py` yra vienintelis
modulis, kuris tai daro. Jei reikia prenumeruoti neapdorotą įėjimo temą vietoj
apklausos, naudokite `gateway.subscribe(session, topic, callback)` tuo pačiu
būdu.

`publish_dual` atlieka likusią dalį: jis publikuoja visas keturias magistralės
temas (`/sapient`, `/json`, `/proto`, `/raw`) tuo vienu iškvietimu — žr.
[§7 Integracijos "Išvesties temos"](#išvesties-temos-sapient-json-proto-raw),
kam skirta kiekviena tema. Niekada nepublikuojate tiesiogiai į TAK ar
SitaWare — `tak_layer`/`sitaware_layer` automatiškai prenumeruoja kiekvieną
normalizuotą temą magistralėje, todėl teisingai publikuotas takelis atsiranda
abiejose be jokio papildomo kodo.

**Temos kelias.** Sekite taksonomiją iš §7 Integracijos:
`{domain}/{source}/{modality}/{affiliation}/{entity}` — pvz., `land` (arba
`air`/`sea`), jūsų jutiklio trumpas vardas, kokio tipo stebėsena tai yra,
`neutral`, jei neturite tikrų priklausomybės duomenų, ir koks objektas yra
(`vehicle`, `vessel`, `unit`, ...). Pažvelkite į keletą esamų temų
(`docs/topic-taxonomy.md`) dėl šablono prieš išrandant naują formą.

**Konfigūracija — nieko fiksuoto kode.** Bet koks serveris, prievadas, URL ar
kredencialas, kurio reikia jūsų skriptui, ateina iš aplinkos kintamojo,
niekada iš tiesioginio kodo įrašo (`compose/bridges/sitaware_bridge.py` yra
geras visiškai env-valdomo tilto pavyzdys). Pridėkite kiekvieną naują
kintamąjį į `compose/.env.example` su vienos eilutės komentaru,
paaiškinančiu, kam jis skirtas — tas failas yra vienintelis tiesos šaltinis,
ką galima sukonfigūruoti diegime, ir tai, ką kitas administratorius skaito,
norėdamas sužinoti, ką užpildyti.

**Patikrinkite, ar kompiliuojasi:**
```bash
python3 -m py_compile compose/bridges/your_new_bridge.py
```

### 9.3 Registruokite jį paleidiklyje

Keturi maži pakeitimai `start.sh`, sekant esamu `geojson` įrašu kaip šablonu
(ieškokite `geojson` `start.sh`, kad matytumėte visus keturis iš karto):

1. **`SERVICES` masyvas** — pridėkite savo paslaugos trumpą vardą į sąrašą.
2. **`SVC_CAT`** — po kokia kategorija ji rodoma meniu/WebUI
   (`"Sensor bridges"`, `"Protocols"`, ir t.t.).
3. **`SVC_DESC`** — vienos eilutės žmogui suprantamas aprašymas.
4. **`svc_ready()`** — kada saugu/prasminga ją paleisti? Jei nereikia jokios
   konfigūracijos, kad būtų naudinga, pridėkite savo vardą į `return 0`
   atvejį šalia `cap`/`geojson`/ir t.t. Jei reikia env kintamojo iš pradžių
   (URL, serveris), remkitės tuo — pvz., `admin-control` sąlyga tikrina, ar
   nustatytas slaptas raktas; jūsų gali tikrinti
   `[[ -n "${YOUR_SENSOR_URL:-}" ]]`.
5. **Paleidimo atvejis** — pridėkite `_start your-service-name path/to/your_script.py`
   didžiajame `case` bloke, kuris iš tikrųjų paleidžia paslaugas.

### 9.4 Patikrinkite nuo galo iki galo

```bash
./start.sh --service your-service-name
```
Tada patvirtinkite, kad duomenys iš tikrųjų teka — prenumeruokite savo temą
bet kokiu Zenoh klientu (repo `clients/examples/` turi paruoštus prenumeravimo
skriptus) ir patvirtinkite, kad įrašai atvyksta. Jei TAK ar SitaWare išvestis
įjungta, atidarykite ATAK/WinTAK ar SitaWare žemėlapį ir patvirtinkite, kad
jūsų objektas atsiranda be jokios papildomos konfigūracijos — tai įrodymas,
kad magistralės sutartis buvo teisingai laikomasi.

### 9.5 Reikia naujo CoT simbolio? (tik TAK išvesčiai)

Jei jūsų jutiklio priklausomybės/objekto derinys dar neatvaizduoja į esamą
CoT tipą, pridėkite jį į `_TOPIC_COT` faile `compose/layers/tak_layer.py`:
```python
"air/**/hostile/uav/**":      ("a-h-A-M-F-Q", AIR_STALE_S),
"land/**/neutral/sensor/**":  ("a-n-G-E-S",   LAND_STALE_S * 2),
```
Raktas yra temos-priesagos šablonas; reikšmė — MIL-STD-2525C/APP-6 CoT tipo
kodas ir pasenimo langas. Dauguma naujų jutiklių jau atitinka esamą šabloną —
naują pridėkite tik tada, jei jūsų temos kelias iš tikrųjų nesutampa.

### 9.6 Dokumentuokite tai

Pridėkite eilutę į atitinkamą lentelę §7 Integracijos (po "Šaltinio-specifiniai
tiltai" ar protokolo lentele), aprašančią, ko jai reikia sukonfigūruoti. Tai
yra tai, kas suteikia *kitam* administratoriui tą pačią vieno-skaitymo, be
spėliojimo patirtį, kurią suteikė šis dokumentas jums.

### Kontrolinis sąrašas prieš sakant, kad baigta

- [ ] Jokio fiksuoto serverio/prievado/URL/kredencialo skripte — viskas yra
      env kintamasis, dokumentuotas `compose/.env.example`.
- [ ] `python3 -m py_compile` praeina.
- [ ] Registruotas visose keturiose `start.sh` vietose (`SERVICES`, `SVC_CAT`,
      `SVC_DESC`, `svc_ready`) plius paleidimo atvejis.
- [ ] Patvirtinta tema magistralėje, tada patvirtinta TAK/SitaWare be jokių
      kodo pakeitimų nė vienam.
- [ ] Eilutė pridėta į §7 Integracijos.

---

## 10. Eksploatacija

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

## 11. Dažniausios problemos

### 11.1 Simptomais pagrįsti sprendimai

Simptomais pagrįsti dažniausių diegimo problemų sprendimai. Infrastruktūros
lygio pamokoms (DNS, TLS profiliai, atominiai rašymai — dalykai, netinkantys
vienam simptomui), žr. §11.2 Pastebėti dalykai žemiau.

#### Zenoh ryšio klaida

**Simptomas:** `zenoh.ZError: Unable to connect to any of [tls/zenoh.efdi...]`

```bash
# 1. Patikrinkite, ar routeris sveikas
docker compose -f compose/docker-compose.yml ps zenoh-router

# 2. Patikrinkite, ar nustatytas galinio taško kintamasis
echo $ZENOH_LOCAL_ENDPOINT   # tikimasi: tcp/127.0.0.1:7448

# 3. Patikrinkite, ar egzistuoja sertifikatų failai
ls $EFDI_CERT_DIR/*.pem
```

Jei `compose/.env` buvo įkeltas paprastu `source compose/.env`, kintamieji
neeksportuojami vaikiniams procesams. Naudokite `./start.sh` (kuris tai
sutvarko), arba:

```bash
set -a && source compose/.env && set +a
```

#### Takeliai neatsiranda ATAK

```bash
# 1. Patvirtinkite, kad tak-layer veikia
kill -0 $(cat $POD_STATE_DIR/.pids/tak-layer.pid) && echo running

# 2. Patvirtinkite, kad TAK Server ryšys užmegztas
ss -tn "( dport = :$TAK_PORT )"

# 3. Patvirtinkite, kad TAK_HOST/TAK_PORT/TAK_TLS .env atitinka tikrą TAK Server galinį tašką
```

#### CAT-34 radaro žymeklis trūksta

Radaras nesiuntė CAT-34 I034/120 (3D-Position), todėl EFDI negali saugiai
nustatyti vietos. Patikrinkite CAT-34 žurnalą dėl `has no site position`.
Pirmenybę teikite I034/120 įjungimui radare/šliuze; tik vienam radarui
nustatykite atsargines koordinates `.env`:

```bash
grep CAT34_RADAR compose/.env
```

#### Drono aptikimai nepublikuojami

Tiltas atmeta aptikimus, senesnius nei 300 s. Patikrinkite API ryšį ir
duomenų šviežumą:

```bash
curl -s -H "Origin: https://dronuradaras.lt" \
  https://radar-api.mainline.inc/api/v1/public/detections \
  | python3 -c "
import sys, json, time
d = json.load(sys.stdin).get('detections', [])
now = time.time()
fresh = [x for x in d if (now - x.get('detected_at', 0)/1000) < 300]
print(f'{len(fresh)} fresh / {len(d)} total detections')
"
```

#### SitaWare vienetai neatsiranda ATAK

**1. Patikrinkite, ar tiltas veikia ir apklausia:**

```bash
tail -f $POD_STATE_DIR/logs/sitaware.log
# Tikimasi: "SitaWare poll: N units published" kas SITAWARE_POLL_S sekundžių
```

**2. Patikrinkite kredencialus ir galinį tašką:**

```bash
curl -s -u "$SITAWARE_USER:$SITAWARE_PASS" "$SITAWARE_URL/..." | python3 -m json.tool | head -20
```

**3. SIDC nesuderintas — vienetas rodomas su neteisinga ikona arba visai
nerodomas:**

SitaWare vienetai be galiojančio 15-ženklio SIDC nukreipiami į
`…/land/sitaware/c2/unknown/unit/…` ir vaizduojami kaip nežinomi antžeminiai
vienetai (`a-u-G-U-C`). Patikrinkite žalią SIDC reikšmę žurnale:

```bash
grep "sidc=" $POD_STATE_DIR/logs/sitaware.log | head -10
```

#### EFDI takeliai neatsiranda SitaWare HQ

```bash
tail -f $POD_STATE_DIR/logs/sitaware-hq-nvg.log
curl -u "$SITAWARE_HQ_NVG_USER:$SITAWARE_HQ_NVG_PASS" \
  -o /dev/null -w '%{http_code} %{content_type}\n' \
  "http://127.0.0.1:${SITAWARE_HQ_NVG_PORT:-8088}${SITAWARE_HQ_NVG_PATH:-/nvg}"
```

Laukiama būsena — `200 application/xml`. HQ NVG valdytoje patvirtinkite, kad
prenumerata neuždaryta, prijungta, apklausia EFDI serverio adresą (ne HQ
adresą) ir taikosi į `efdi-live / EFDI Live Tracks`. Jei sukonfigūruotas TLS,
praleiskite `-k` po to, kai išduodanti CA yra patikima. Vietinis `200` plius
HQ ryšio nesėkmė rodo maršrutizavimo, Windows ugniasienės, Linux ugniasienės
ar sertifikato pasitikėjimo problemą — ne NVG konversijos nesėkmę.

**Latest replication** laiko žyma turi judėti pirmyn. Jei ji lieka sena ir
**Reload** praneša nežinomą klaidą, patikrinkite tą patį URL iš PowerShell HQ
serveryje. Ryšio nesėkmė yra maršrutizavimas/ugniasienė; HTTP 401 reiškia
trūkstamus ar pasenusius prenumeratos kredencialus; sėkmė tik su `-k` reiškia,
kad srauto CA nėra patikima paskyros/paslaugos, atliekančios importą. Prieš
pakeisdami senesnį sluoksnį pataisykite replikaciją, kitaip pakaitinis
sluoksnis liks tuščias.

Autentifikuotas sveikatos galinis taškas suteikia serverio pusės įrodymų
neregistruojant kredencialų ar NVG turinio:

```bash
curl -ksS -u "$SITAWARE_HQ_NVG_USER:$SITAWARE_HQ_NVG_PASS" \
  "https://127.0.0.1:${SITAWARE_HQ_NVG_PORT:-8088}/healthz" | python3 -m json.tool
```

- `successful_requests` lieka nulis: HQ nepasiekė srauto.
- `unauthorized_requests` didėja: HQ pasiekė jį su trūkstamais/pasenusiais
  Basic kredencialais.
- `successful_requests` didėja, o HQ lieka Pending: tyrinėkite NVG
  analizavimą ar pasirinktą tikslinį sluoksnį, ne maršrutizavimą ar
  autentifikavimą.

Srauto prieigos žurnalai turi tik rezultatą, takelio skaičių ir kliento
adresą, ir yra riboti iki vienos eilutės per minutę sėkmingiems ir
neautorizuotiems paėmimams.

#### Dubliuoti proceso egzemplioriai

Sukelia dukart paleistas `start.sh` be sustabdymo:

```bash
pkill -f "_bridge\.py\|tak_layer\|track_fusion"
rm -f $POD_STATE_DIR/.pids/*.pid
./start.sh
```

#### Radaro ikona dingsta iš ATAK

`asterix` tiltas publikuoja keepalive kas 60 s nepriklausomai nuo takelio
aktyvumo. Jei ikona dingsta, tiltas sustojo:

```bash
tail -20 $POD_STATE_DIR/logs/asterix.log | grep -E "keepalive|startup|error"
```

### 11.2 Pastebėti dalykai — jau apmokėtos pamokos

Tai *eksploatacinis/infrastruktūrinis* palydovas
[`../.ai/.claude/CLAUDE.md`](../.ai/.claude/CLAUDE.md) ASTERIX bitų lygio
dekodavimo pastebėtiems dalykams ir §11.1 Simptomais pagrįstiems sprendimams.
Kiekvienas čia esantis dalykas buvo tikra, patvirtinta problema, su kuria
susidurta valdant šį podą — perskaitykite prieš derindami kažką, kas panašu
į vieną iš šių simptomų, kad tos pačios diagnozės nereikėtų pelnyti iš naujo.

#### NetBird split-DNS nematomas konteinerių viduje

**Simptomas:** Routerio konfigūracija nurodo mesh vardą (pvz.,
`zenoh2.efdi.ltu`); konteineris net nebando prisijungti — jokio lizdo,
jokios TLS klaidos, tik tyla.

**Priežastis:** `network_mode: host` dalinasi tinklo *vardų sritimi*, ne
`/etc/resolv.conf`. NetBird split-DNS resolveris jo mesh domenui veikia tik
**serveryje** — konteineris vis tiek gauna Docker sugeneruotą resolverį
(paprastai jūsų LAN DNS), kuris niekada negirdėjo apie mesh domeną. Vardas
išsisprendžia serveryje (`getent hosts` ten veikia gerai) ir tyliai
nepavyksta konteineryje — kas atrodo identiškai "niekas net nebando
prisijungti".

**Sprendimas:** Pridėkite aiškius `extra_hosts` įrašus, susiejančius
kiekvieną mesh vardą su jo dabartiniu NetBird IP konteinerio compose
paslaugoje. Domenų vardai lieka programos konfigūracijoje; tik konteinerio
vietinio hosts-sprendimo reikia atvaizdavimo. Iš naujo pridėkite/atnaujinkite
juos, jei NetBird kada nors perpriskiria IP.

#### TLS/mTLS identiteto profilis turi atitikti galinį tašką, kurį jis rinkis

**Simptomas:** Magistralės ryšio bandymas nesukuria jokios klaidos ir jokio
ryšio — atrodo identiškai kaip DNS problema aukščiau, ar ugniasienės blokas.

**Priežastis:** Kiekvienas nuotolinis fabrikas (backbone, partnerio
sandbox, šio podo paties vietinis mesh) pasirašytas **skirtingos** CA.
Teisingo galinio taško nukreipimas prie neteisingo sertifikato identiteto
nesėkmingai baigia mTLS rankos paspaudimą, ir priklausomai nuo nesėkmės
tipo tai gali atrodyti taip, lyg nieko visai neįvyko, o ne aiškus atmetimas.

**Sprendimas:** Galinis taškas ir TLS identiteto profilis yra vienas
atominis pasirinkimas, niekada nekoreguojamas nepriklausomai. Jei jūsų
įrankiai siūlo išankstinius nustatymus, sujunkite galinį tašką ir atitinkamą
sertifikato profilį į vieną išankstinį nustatymą, o ne du atskirus laukus,
kuriuos žmogus gali sumaišyti.

#### Vieno failo bind-mount sulaužo atominius rašymus

**Simptomas:** Konfigūracijos-taikymo galinis taškas, kuris rašo mažą
būsenos failą (pvz., vardų-srities-priešdėlio failą), nesėkmingai baigiasi
su `OSError: [Errno 16] Device or resource busy`, nors pagrindinio
konfigūracijos failo rašymas visai šalia veikia gerai.

**Priežastis:** Standartinis "atominio rašymo" šablonas yra
rašymas-į-laikiną-failą, tada `os.replace(temp, target)` — pervadinimas yra
tai, kas garantuoja, kad skaitytojas niekada nemato pusiau įrašyto failo.
Tas pervadinimas nepavyksta, kai `target` pats yra vieno-failo Docker
bind-mount (`-v host/file:/container/file`): kelias *yra* mount taškas, ir
negalima pervadinti per mount tašką. Katalogu-mounted failas neturi šios
problemos, nes pervadinimas vyksta mounted kataloge, ne per patį mount'ą.

**Sprendimas:** Grįžkite prie perrašymo vietoje (open-write-fsync, be
pervadinimo), kai `os.replace` nepavyksta su `EBUSY`. Tai nėra atomiška, bet
tai vienintelė galimybė bind-mounted vienam failui, ir tai geriau nei visos
taikymo operacijos nesėkmė dėl nesusijusio failo.

#### Identiškai pavadintos dubliuotos funkcijų apibrėžtys tyliai užstoja

**Simptomas:** Dekoderis/tvarkyklė atrodo akivaizdžiai neteisinga, kai
skaitote ją (neteisingas lauko plotis, neteisinga skalė, klaida, kuri turėtų
būti labai matoma išvestyje) — bet produkcijos duomenys, ateinantys iš kito
galo, atrodo gerai.

**Priežastis:** Python leidžia iš naujo apibrėžti funkciją modulio apimtyje
be jokio įspėjimo. Jei failas turi `def handler(...)` du kartus, **antra**
apibrėžtis tyliai laimi — pirma tampa 100% negyvu kodu, kuris vis tiek
*atrodo* gyvas (ta pati įtrauka, jokios apsaugos, dažnai net abi teisingai
dokumentuotos). Joks šio repo įrankių grandinės linteris to nepažymi
numatytai. Tai tiksliai taip, kaip iš tikrųjų sugadintas kodo kelias gali
išsilaikyti faile ilgą laiką, niekada nieko neįtakodamas, ir tai gali
kainuoti tikro derinimo laiko, kai "akivaizdžiai sugadinta" kopija yra ta,
kurią žmogus perskaito pirmiausia.

**Sprendimas:** Prieš pasitikėdami, kad funkcija, kurią skaitote, yra ta,
kuri iš tikrųjų veikia, patvirtinkite vykdymo metu:
`inspect.getsourcelines(module.the_func)` pasako, kurios apibrėžties eilutės
numeris iš tikrųjų susietas. Jei repo augo organiškai (kategorijos/variantai
pridėti laikui bėgant, kiekvienas su "savo" panašios logikos kopija),
ieškokite funkcijos vardo visame faile — ne tik ten, kur radote pirmą kartą —
kai kažkas neatrodo teisingai.

#### Paslaugos rinkiniui reikia savos būsenos agregacijos

**Simptomas:** WebUI ar būsenos galinis taškas rodo daugiaprocesį rinkinį
(keli vaikai po viena logine "paslauga") kaip nuolat sustabdytą, nors
kiekvienas vaiko procesas iš tikrųjų veikia.

**Priežastis:** Bendra per-paslaugos būsenos logika, tikrinanti vieną
pidfile, pavadintą pagal paslaugą, niekada jo neras, jei rinkinio
paleidiklis rašo po vieną pidfile *kiekvienam vaikui* (pvz.,
`asterix-cat10.pid`, `asterix-cat48.pid`, ...). Pats rinkinys neturi
pidfile, todėl visada skaito "sustabdyta."

**Sprendimas:** Rinkinio paslaugai reikia specialios būsenos logikos,
kuri surašo ir agreguoja savo vaikų pidfile, pranešant
veikia/sutrikusi/sustabdyta pagal tai, kiek jų gyva — ne naivus vieno-pidfile
tikrinimas.

#### Kodo pataisymas negalioja, kol veikiantis procesas nepersileidžia

**Simptomas:** Ištaisote klaidą (dekoderyje, admin API, bet kur),
patvirtinate, kad failas diske pasikeitė, ir veikiančios sistemos elgsena
nesikeičia — arba WebUI toliau rodo paslaugas/duomenis, kurie ką tik
pašalinti iš kodo.

**Priežastis:** `.py` failo redagavimas neturi jokio poveikio jau veikiančiam
interpretatoriui, laikančiam seną baitkodą atmintyje. Tai skamba akivaizdžiai
pasakyta paprastai, bet lengva pamiršti tyrimo viduryje, kai keli failai
redaguojami iš eilės ir nėra akivaizdu, *kuris* veikiantis procesas yra
pasenęs.

**Sprendimas:** Po bet kokio ilgai veikiančios paslaugos kodo pataisymo
persileiskite tą konkretų procesą (ne tik iš naujo sukompiliuokite/testuokite)
prieš darant išvadą, kad pataisymas neveikė, ir prieš pranešant, kad
simptomas vis dar neišspręstas.

---

## 12. Zenoh administravimo GUI

Žiniatinklio GUI podui valdyti be SSH. Techninis turinys perkeltas į bendrą
(anglišką) dokumentą, nes komandos ir laukų pavadinimai vis tiek liktų
angliški: **[ZENOH_ADMIN.md](ZENOH_ADMIN.md)**.

## 13. Tęstinė integracija (CI)

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
| 2026-07-05 | Pašalintas `gps-ew` bridge (GPSJam pagrindu) — gpsjam.org neturi viešo API savo apdorotiems duomenims, todėl šis bridge niekada realiai neveikė; pašalintas iš `start.sh` ir `tak_layer.py`, o ne paliktas tyliai sulūžęs |
| 2026-07-05 | Ištaisyti dubliuoti takeliai SitaWare tarp šaltinių/pod'ų: `nato_sitaware_layer.py` `_uid()` funkcijoje šaltinio pavadinimas buvo įtraukiamas į takelio ID (skirtingai nuo jau teisingos `tak_layer.py` versijos), todėl tas pats orlaivis iš dviejų šaltinių gaudavo du skirtingus SitaWare takelius |
| 2026-07-05 | `dronuradaras_bridge.py` buvo pakeistas publikuoti visus registruotus jutiklius su pozicija; šį sprendimą pakeitė žemiau aprašyta 2026-07-15 tik prisijungusių jutiklių taisyklė |
| 2026-07-05 | Pridėtas `.github/workflows/ci.yml`: tikrina bridge'ų/sluoksnių sintaksę, type-check + build zenoh-admin frontend'ui, sukuria abu Docker image'us kas kartą pushinant/darant PR |
| 2026-07-05 | Pridėti `shellcheck` ir `compose-validate` CI job'ai; ištaisytas vienintelis realus radinys (`compose/rebuild.sh` trūko `cd ... \|\| exit`) ir nutildytas klaidingas teigiamas (`SC2163` dėl sąmoningo "export pagal dinaminį vardą" idiomo `start.sh`/`stop.sh`/`run.sh`) |
| 2026-07-10 | Ištaisyta: `nato_sitaware_layer.py` naudojo tuos pačius aplinkos kintamuosius kaip gaunamas `sitaware_bridge.py` (`SITAWARE_URL`/`USER`/`PASS`) — pervadinta į `SITAWARE_NVG_*`, nes HQ (gaunama) ir Edge (siunčiama) paprastai yra skirtingi serveriai/prisijungimo duomenys |
| 2026-07-10 | Paslaugos `nffi` ir `sitaware-nvg` prijungtos prie `start.sh` — abi egzistavo repozitorijoje, bet niekada nebuvo registruotos kaip paleidžiamos paslaugos |
| 2026-07-10 | `start.sh`: `sitaware` ir `sitaware-nvg` dabar paklausia vartotojo vardo ir paslėpto slaptažodžio paleidimo metu (anksčiau buvo klausiama tik serverio adreso; prisijungimo duomenys turėjo būti iš anksto nustatyti `.env`) |
| 2026-07-10 | Zenoh admin GUI: pridėta "Connected routers" panelė — nuskaito `router/transport/unicast/*` įrašus, jau esančius admin space užklausoje, naudojamoje prenumeratorių/queryable sąrašams, jokios naujos ACL ar užklausos nereikia |
| 2026-07-10 | Zenoh admin GUI: perkeltas TAK-hud vizualinis stilius (`hud-card`, `hud-frame`/reticle kampai, `hud-glass` šoninis meniu, `hud-grid-bg` fonas, akcento švytėjimo mygtukai, laipsniškas atsiradimo animacijos) į `index.css`/`Layout.tsx`/skydelį |
| 2026-07-15 | `dronuradaras_bridge.py` dabar publikuoja tik įrenginius, kurių API būsena yra `is_online=true`; atsijungę įrenginiai siunčia pašalinimo įvykį, todėl CoT, SitaWare Edge ir HQ NVG talpykla ištrina senus žymeklius |
| 2026-07-17 | Pridėti deterministiniai ASTERIX kategorijų listener'ių susitarimai: CAT-010/020/021/034/048/062 pagal nutylėjimą naudoja UDP 50010/50020/50021/50034/50048/50062; tai EFDI, ne gamintojų numatytieji prievadai |
| 2026-07-17 | Pridėti Zenoh-native CAP, GeoJSON/OGC, spektro, jutiklių būklės, misijų maršrutų ir neapdoroto įėjimo vertimo keliai |
| 2026-07-17 | Saugumo atnaujinimas: atnaujintas Vite, prisegti/atnaujinti Compose image'ai, atnaujinti Python image'ų OS paketai, o autentifikuoti SitaWare/UTM endpoint'ai apriboti iki HTTPS |
| 2026-07-18 | Pridėtas TAK stiliaus Runtime Control: host bridge/protokolų/sluoksnių lifecycle veiksmai, apriboti log'ai, endpoint/temų/portų redagavimas, write-only kredencialai, localhost admin-control agent ir veikiantis Vite dev stack su suderintais API/Vite portais |
| 2026-08-02 | Sujungti `PARUOSIMAS.md`, `INTEGRATIONS.md`, `C2_RUNBOOK.md`, `ADDING_A_SENSOR.md`, `TROUBLESHOOTING.md` ir `GOTCHAS.md` (visi pilnai išversti į lietuvių kalbą) į šį dokumentą (§§1, 7-9, 11) — vienas diegimo vadovas vietoj aštuonių; `ZENOH_ADMIN.md` lieka atskirai |
| 2026-08-02 | Pridėtas BDS 1,0/1,7 (Data Link Capability / Common Usage GICB Capability) dekodavimas 7 ASTERIX kategorijoms, kurios jau naudoja BDS 3,0/4,0/5,0/6,0 GICB-ištraukimo pagalbininkus (CAT-010/011/018/020/021/048/062), pagal pyModeS |
| 2026-08-02 | Pervadinti `layers/cot_layer.py` → `layers/tak_layer.py` ir `layers/nvg_layer.py` → `layers/sitaware_layer.py` (tiekėjo pavadintas išvestinis sluoksnis, atitinkantis `tak_bridge.py`/`sitaware_bridge.py` gaunamųjų pavadinimus); pašalinti nenaudojami `cot-udp`/`cot-udp-tak` UDP multicast/unicast paleidiklio įrašai ir `nvg_bridge.py` NVG-XML gaunamasis tiltas (SitaWare įėjimas dabar tik REST) |
| 2026-08-02 | Sujungtos visos EFDI-autorystės `.proto` schemos po `compose/protocols/proto/` (anksčiau paskirstyta tarp `compose/protocols/random/`, `compose/protocols/vendors/proto/` ir `compose/protocols/vendors/sparkplug/`); vendoruotos trečiųjų šalių schemos (SAPIENT `sapient_msg/`, Sparkplug B) lieka savo `vendors/<name>/` kataloge |

---

*Skirta vidiniam naudojimui — neskleisti už projekto ribų.*
