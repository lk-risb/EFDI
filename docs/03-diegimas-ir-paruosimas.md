# 03 — Diegimas ir paruošimas

## Reikalavimai

### Serverio paruošimas nuo tuščios mašinos

Debian 13 arba RHEL/Rocky/AlmaLinux 9/10 sistemoje rankiniu būdu įdiegiate
tik `curl` (paprastai jau yra) — visa kita yra viena komanda:
```bash
curl -fsSL https://raw.githubusercontent.com/lk-risb/EFDI/main/install.sh | bash
```
`./install.sh` atnaujina OS (`apt`/`dnf` upgrade) ir savaime įdiegia git,
Python 3.10+, Docker Engine + Compose papildinį (iš oficialios Docker
saugyklos, ne distributyvo paketą), openssl ir gettext tiek Debian (apt),
tiek RHEL/Rocky/Alma (dnf) sistemose, jei jų trūksta — visiškai tuščiame
serveryje su vien `sudo` teisėmis ir išeinančiu interneto ryšiu prieš tai
nereikia nieko rankiniu būdu. Jei po OS atnaujinimo reikia perkrauti
(branduolio ar bazinės bibliotekos atnaujinimas), diegyklė sustoja ir apie
tai praneša — tiesiog perkraukite ir vėl paleiskite tą pačią komandą. Jis
taip pat pasiūlo įdiegti ir prijungti NetBird arba Tailscale, jei nei
vienas dar neprijungtas, klausdamas tik setup/auth rakto — vienintelio
dalyko, kurio diegyklė pati sugalvoti negali.

<details>
<summary>Rankinis/offline žingsnis po žingsnio (tik jei nepaleidžiate
<code>install.sh</code> — izoliuoto tinklo diegimas, kitas distributyvas,
ar norite suprasti, ką jis daro)</summary>

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
[Diegimas](#diegimas) žemiau (būtent tai daro jo automatinis Tinklo žingsnis).
Patikrinkite tik, ar dvejetainis failas įdiegtas:
```bash
netbird version
tailscale version
```

#### Atidarykite ugniasienės prievadus

Skirtingai nei visa kita šiame poskyryje, `./install.sh` **neatlieka** šio
žingsnio už jus — kuriuos prievadus atidaryti priklauso nuo to, kuriuos
jutiklių tiltus įjungsite, o tai yra po-diegimo, WebUI valdomas pasirinkimas
(žr. [Konfigūracija](04-konfiguracija.md)), o ne kažkas, ką diegyklė gali nuspręsti iš anksto.
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

Jei kiekviena komanda aukščiau pavyko, tęskite prie [Diegimas](#diegimas) žemiau
(repozitorijos klonavimas ir podo paleidimas). Jei kas nors nepavyko, iš
naujo įvykdykite atitinkamą žingsnį aukščiau, prieš judėdami toliau — niekas
vėliau diegime negali ištaisyti čia trūkstamos priklausomybės.

</details>

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
| HTTPS 8890 | į serverį | Zenoh administravimo GUI (Caddy TLS, vidinis CA — žr. [Eksploatacija](05-paleidimas-ir-eksploatacija.md)) |
| HTTPS | iš serverio | dronuradaras.lt API |

ATAK/WinTAK klientai takelius gauna tik per TAK serverį (`tak-layer` paslauga); tiesioginio multicast/unicast CoT kelio nėra.

### Sertifikatai

Zenoh mTLS sertifikatai išduodami savarankiškai — jokio išorinio CA ar vendor bundle. `scripts/gen-certs.sh <namespace>` sugeneruoja (vieną kartą) EFDI root CA kataloge `compose/certs/efdi/`, tada pasirašo lapo sertifikatą+raktą nurodytam namespace; tas pats root CA naudojamas visiems vėlesniems namespace'ams.

Sugeneruota medžiaga (`efdi-ca-root.pem`, `<NAMESPACE>-cert.pem`, `<NAMESPACE>-key.pem`) saugoma `compose/certs/efdi/` — įtraukta į `.gitignore`, niekada nekomituojama. Kataloge taip pat atskirai laikomi `tak/`, `sitaware/`, `efdi-backbone/` (goat backbone, Desert Bread CA) ir `efdi-ltu/` (LTU sandbox) identitetai — žr. [§3.2](03-bootstrap-and-install.md#32-generate-certificates) (anglų k.). Numatytasis kelias nustatomas `start.sh`; jei norite laikyti jį visai už repozitorijos ribų, perrašykite per `BUNDLE_DIR` faile `compose/.env`.

---

## Diegimas

### 3.1 Repozitorijos klonavimas

Švariame serveryje, kur dar nieko neįdiegta, viena komanda nuklonuoja
repozitoriją ir paleidžia diegyklę, kuri pati įdiegia visus 1 skyriaus
reikalavimus:

```bash
curl -fsSL https://raw.githubusercontent.com/lk-risb/EFDI/main/install.sh | bash
```

Tas pats, tik pirma nuklonavus rankiniu būdu:

```bash
git clone <repo-url> EFDI
cd EFDI
./install.sh
```

### 3.1a Pasirinkite Production arba Testing režimą

`install.sh` to klausia anksti, dar prieš sertifikatus:

```text
Production  — reikia sertifikatų iš scripts/gen-certs.sh (mTLS, fabric ryšys)
Testing     — sugeneruoja savarankiškai pasirašytus sertifikatus, tik vietinis Zenoh (be fabric)
```

**Testing režimas** skirtas išbandyti EFDI vienoje mašinoje be tikro fabric
ryšio: automatiškai sugeneruojama vardų sritis ir savarankiškai pasirašyti
sertifikatai, o Zenoh veikia per paprastą TCP be mTLS. Jis taip pat pakeičia
du kelius, kurių prireiks derinant klaidas:

```text
BUNDLE_DIR     = <repo>/compose/test-certs   (ne compose/certs/)
POD_STATE_DIR  = <repo>/.test-pod-state      (šalia compose/, ne jo viduje)
```

Jei ieškote žurnalų, PID failų ar sugeneruotos Zenoh konfigūracijos
testing-režimo diegime, tikrinkite `<repo>/.test-pod-state/` (pvz.,
`.test-pod-state/logs/sitaware_layer.log`), **ne** `compose/state/` —
numatytasis `compose/state/` kelias galioja tik production diegimui, kai
`POD_STATE_DIR` paliktas nenustatytas. Patikrinkite, kurį iš jų iš tikrųjų
naudoja konkreti dėžė: `grep '^POD_STATE_DIR=' compose/.env`.

**Production režimui** reikia tikrų sertifikatų iš `scripts/gen-certs.sh`
(arba partnerio išduoto rinkinio) ir jis priverstinai naudoja mTLS — žr.
[§3.2](#32-sertifikatų-generavimas) žemiau.

Abu režimai dabar visada paklausia Zenoh WebUI administratoriaus vartotojo
vardo ir slaptažodžio diegimo metu (anksčiau testing režime tai būdavo
praleidžiama su automatiškai sugeneruotu slaptažodžiu, kuris tyliai
išvalomas iš `.env` po pirmo prisijungimo, ir jokio kito jo įrašo niekur
neliko — pačiam nustatant slaptažodį šio spąstų išvengiama). `reinstall.sh`
taip pat gali vėliau atstatyti šiuos kredencialus, jei juos pamiršote, arba
naudokite `health.sh` interaktyvų problemų sprendimo meniu — žr.
[Eksploatacija](05-paleidimas-ir-eksploatacija.md).

### 3.2 Sertifikatų generavimas

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

Pilnas paaiškinimas (kuris cert kuriam fabric) — žr. [§3.2](03-bootstrap-and-install.md#32-generate-certificates)
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

### 3.3 Python virtualios aplinkos kūrimas

`start.sh` sukuria aplinką automatiškai per pirmą paleidimą. Rankinis kūrimas:

```bash
python3 -m venv compose/venv
compose/venv/bin/pip install -r compose/requirements.txt
```

> `eclipse-zenoh` versija turi būti **tiksliai 1.9.0** — net nedideli versijų skirtumai gali pakeisti API.

Visada diekite į šią venv, niekada tiesiai į sisteminį `python3`. Paleidus
`pip install` prieš sisteminį interpretatorių šiuolaikiniame Debian/Ubuntu
nepavyksta su `error: externally-managed-environment` (PEP 668) — ta klaida
yra sistemos apsauga, ne riktas, kurį reikia apeiti su
`--break-system-packages`. Kiekviena šio pod'o hoste veikianti paslauga
(tiltai, sluoksniai, protokolų vertėjai) jau tikisi `compose/venv`, tad
„greitas" klaidos apėjimas paliktų tas paslaugas veikti kitokioje Python
aplinkoje nei ką tik pakeitėte.

`install.sh` taip pat atlieka `chgrp`/`chmod` keliems atskirai per bind-mount
prijungtiems būsenos failams ir katalogams (`namespace-prefix`,
`data-topic-prefix`, `$BUNDLE_DIR/efdi`, `$POD_STATE_DIR/integrations/tak`),
kad `zenoh-admin` konteineris — visada veikiantis fiksuotu ne-root uid
`10001` — galėtų į juos rašyti. Jei *vėliau* WebUI išsaugojimas
(konfigūracija, TAK/SitaWare kredencialai) nepavyksta su „Permission denied"
viename iš šių kelių, `health.sh` interaktyvus meniu (3 punktas) tai aptinka
ir ištaiso automatiškai; žr.
[Problemų sprendimas](11-dazniausios-problemos.md), jei reikia ištaisyti iš
karto rankiniu būdu.

### 3.4 Zenoh router paleidimas

```bash
docker compose -f compose/docker-compose.yml up -d zenoh-router
```

Prieš tęsiant patikrinkite, kad konteineris veikia:

```bash
docker compose -f compose/docker-compose.yml ps zenoh-router
# Stulpelyje "Status" turi būti "healthy"
```

---
