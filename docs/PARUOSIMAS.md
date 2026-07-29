# Serverio paruošimas — nuo tuščios Linux mašinos iki pasiruošusios EFDI

Perskaitykite šį dokumentą **prieš** [`DIEGIMAS.md`](DIEGIMAS.md). Jame
laikoma, kad dar niekas neįdiegta — tik švarus Linux serveris su root/sudo
teisėmis ir tinklo ryšiu. Jei Docker, Python, git ir NetBird jau įdiegti ir
veikia, pereikite tiesiai prie `DIEGIMAS.md`.

## 1. Pasirinkite ir parinkite serverio dydį

| | Minimalu | Rekomenduojama |
| --- | --- | --- |
| OS | Ubuntu 22.04/24.04 LTS arba RHEL 9 / Rocky Linux 9 / AlmaLinux 9 | Ubuntu 24.04 LTS |
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Diskas | 20 GB laisvos vietos | 40 GB+ laisvos vietos (daugiau, jei įjungsite ilgalaikį trasų saugojimą) |
| Tinklas | Vienas sąsajos su išeinančiu interneto ryšiu | Statinis arba DHCP rezervuotas adresas |

Bet kuris modernus x86_64 arba arm64 Linux platinys su naujesniu branduoliu ir
systemd tinka; šios dvi šeimos aprašomos žingsnis po žingsnio žemiau, nes jos
dažniausiai naudojamos valstybinėse/gynybos aplinkose. Jei naudojate kitą
platinį, tiesiog pritaikykite paketų tvarkyklės komandas — likusi vadovo dalis
galioja nepakitusi.

Visas žemiau esančias komandas vykdykite kaip paprastas vartotojas su `sudo`
teisėmis — ne tiesiogiai kaip `root`, kad paskutinis „paleisti Docker be root
teisių" žingsnis turėtų prasmę.

## 2. Atnaujinkite OS

**Ubuntu/Debian:**
```bash
sudo apt update && sudo apt upgrade -y
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf upgrade -y
```

Jei buvo atnaujintas branduolys, perkraukite (`sudo reboot`).

## 3. Įdiekite git ir bazinius įrankius

**Ubuntu/Debian:**
```bash
sudo apt install -y git curl ca-certificates
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf install -y git curl ca-certificates
```

Patikrinkite: `git --version`

## 4. Įdiekite Python 3.10+

**Ubuntu 22.04** numatytai turi Python 3.10, **Ubuntu 24.04** — 3.12.
Pirmiausia patikrinkite, ką turite — `python3 --version` — ir diekite tik jei
versija senesnė nei 3.10:
```bash
sudo apt install -y python3 python3-venv python3-pip
```

**RHEL/Rocky/AlmaLinux 9** numatytai turi Python **3.9**, kuris yra žemiau
EFDI minimumo. Įdiekite 3.11 iš AppStream saugyklos šalia esamos versijos
(tai **nepakeičia** sisteminio `python3`, todėl niekas kitas serveryje
nesugadinama):
```bash
sudo dnf install -y python3.11 python3.11-pip
```
RHEL šeimos serveryje visur, kur šio repo scriptai rašo `python3`, naudokite
`python3.11` arba susikurkite venv, nukreiptą į jį.

Patikrinkite: `python3 --version` (arba `python3.11 --version` RHEL šeimoje)
turi rodyti **3.10 arba naujesnę**.

## 5. Įdiekite Docker Engine + Compose papildinį

Naudokite oficialią platinio Docker saugyklą, ne platinio pridedamą
`docker.io`/`podman-docker` paketą — jie dažnai pasenę ir gali neturėti
Compose v2 papildinio, nuo kurio priklauso šis repo (`docker compose`, ne
senasis atskiras `docker-compose`).

**Ubuntu/Debian:**
```bash
# Pridėkite oficialų Docker GPG raktą ir saugyklą
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update

# Įdiekite Docker Engine + Compose papildinį
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

### Paleiskite Docker be root teisių

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

## 6. Įdiekite NetBird klientą

EFDI podai pasiekia fabriką ir vienas kitą per NetBird mesh VPN. Klientą
diekite vienodai abiejuose platiniuose (NetBird savo skriptu atsineša savo
saugyklą, todėl atskiro apt/dnf nustatymo nereikia):
```bash
curl -fsSL https://pkgs.netbird.io/install.sh | sh
```
Dar **neprisijunkite** prie tinklo — setup key duoda jūsų organizacijos
NetBird paskyros administratorius, o prisijungimas aprašytas
[`DIEGIMAS.md`](DIEGIMAS.md). Patikrinkite tik, ar dvejetainis failas įdiegtas:
```bash
netbird version
```

## 7. Atidarykite ugniasienės prievadus

Atidarykite tik tai, ko šiam serveriui reikia įeinančiai srauto krypčiai;
likusi prievadų lentelėje esanti dalis yra išeinanti ir nereikalauja
ugniasienės taisyklės šiame serveryje. Autoritetingas, aktualus prievadų
sąrašas yra `DIEGIMAS.md §1 Prieš pradedant → Tinklas` — atidarykite tuos,
kuriuos jūsų diegimas iš tikrųjų naudoja (dauguma podų nepaleidžia visų
jutiklių tiltų).

**Ubuntu/Debian (ufw):**
```bash
sudo ufw allow 8890/tcp comment 'EFDI admin GUI'
sudo ufw allow 50048/udp comment 'EFDI CAT-048 pavyzdys — pritaikykite savo jutikliams'
# kartokite kiekvienam UDP/TCP prievadui, kurį naudoja jūsų integracijos, pagal DIEGIMAS.md
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

## 8. Jūs pasiruošę

Šiuo metu turėtumėte galėti paleisti (viską be `sudo`):
```bash
git --version
python3 --version      # 3.10+
docker run hello-world
docker compose version
netbird version
```

Jei kiekviena komanda aukščiau pavyko, tęskite prie [`DIEGIMAS.md`](DIEGIMAS.md)
§2 (repozitorijos klonavimas ir podo paleidimas). Jei kas nors nepavyko, iš
naujo įvykdykite atitinkamą žingsnį aukščiau, prieš judėdami toliau — niekas
vėliau diegime negali ištaisyti čia trūkstamos priklausomybės.
