# 12 — Zenoh Admin valdymo skydas

Web valdymo skydas, per kurį pod'ą galima eksploatuoti be SSH: matoma
maršrutizatoriaus ir sistemos būsena, iš čia paleidžiami ir stabdomi
tiltai bei sluoksniai, redaguojama konfigūracija ir kredencialai, valdoma
sertifikatų institucija ir prekės ženklas. Skydas naudoja modernų,
minimalistinį tamsų apipavidalinimą — vientisos švelniai tamsios kortelės,
savarankiškai talpinamas Inter šriftas, žalsvai mėlynas akcentas —, kurį
superadmin gali pats perkurti iš WebUI Settings.

Dashboard skydelyje „Connected routers" matote kiekvieną kitą zenoh
egzempliorių (maršrutizatorių ar tarpusavio mazgą), su kuriuo šis
maršrutizatorius šiuo metu turi gyvą ryšį. Ši informacija paimama iš
paties maršrutizatoriaus admin erdvės — to paties šaltinio, iš kurio
gaunami ir prenumeratorių bei queryable temų sąrašai, tad jokios
papildomos konfigūracijos, be jau esamos `pod-admin-introspect` ACL
taisyklės, nereikia.

## Skydo apžvalga

Skydas veikia adresu `http://127.0.0.1:8890` (arba pod'o adresu) ir valdo
vieną konkretų EFDI pod'ą. Jei su juo susiduriate pirmą kartą, ši dalis
padės susiorientuoti; gilesni poskyriai (*Runtime Control puslapis*,
*Rolės*, *Config kortelės laukai*) seka toliau.

**Pirmas prisijungimas.** Prisijunkite administratoriaus paskyra, sukurta
diegimo metu — pirmą kartą prisijungus gali būti paprašyta pakeisti
slaptažodį. Atsivers Dashboard: čia iškart matysite, ar pod'as sveikas.
Nuo šio taško kasdienis darbo ciklas paprastas: **Runtime Control**
paslaugoms paleisti, **Config** joms konfigūruoti, **Dashboard** sveikatai
sekti.

**Puslapiai** (tokia tvarka, kaip išdėstyti šoninėje juostoje):

- **Dashboard** — bendra sveikatos apžvalga: CPU, RAM, diskas, veikimo
  laikas, apkrova, tinklas, pagrindinių paslaugų būsena, trumpa federacijos
  peržiūra ir gyva Zenoh statistika (prenumeratoriai, queryable, saugyklos,
  prisijungę maršrutizatoriai). Nuo šio puslapio verta pradėti, jei norite
  greitai atsakyti į klausimą „ar viskas veikia?".
- **Network** — *Valdomas maršrutizatorių tinklas*. Šis puslapis aktualus
  tik tada, kai šis pod'as pats yra HQ/root, valdantis kitus, šakinius
  maršrutizatorius: čia matoma topologija, tiesioginiai vaikai, jų
  pasitikėjimo būsena ir mygtukas **Apply trust ACL**. Vienišam pod'ui šis
  puslapis tiesiog rodys „0 direct children" — jį drąsiai ignoruokite.
- **Config** — vienas puslapis, du sluoksniai. **Zenoh Config** redaguoja
  patį maršrutizatorių: vietinius prievadus, fabric uplink galinius
  taškus ir sertifikato tapatybę, vardų sritį, ryšio politiką (žr. *Config
  kortelės laukai* žemiau). **Integration Settings** leidžia redaguoti
  paslaugų aplinką be SSH — TAK host/port, SitaWare HQ NVG importo/srauto
  ir REST kelius, MQTT bei ASTERIX prievadus. Slaptažodžius čia galima tik
  įrašyti, ne perskaityti atgal. Išsaugoję pakeitimą, nepamirškite
  persileisti paveiktos paslaugos per Runtime Control, kad pakeitimas
  iš tikrųjų įsigaliotų.
- **Runtime Control** — vienoje vietoje galite paleisti, sustabdyti,
  persileisti ir stebėti žurnalus kiekvienam tiltui (jutiklio įvesčiai),
  protokolui (vertėjui) ir sluoksniui (C2 išvesčiai); galima filtruoti
  pagal kategoriją ar rolę, pasirinkti, kurios paslaugos bus paleistos
  (šis pasirinkimas prisimenamas ir po persileidimo). Žr. skyrių *Runtime
  Control puslapis* žemiau.
- **Changes** — Zenoh konfigūracijos revizijų istorija: kiekvienam
  pakeitimui matote, ar jis pritaikytas, atmestas, ar atstatytas atgal
  (įrašomas tik rezultatas ir maiša, ne pati konfigūracija).
- **Admin Users** — čia valdomos skydo paskyros ir jų rolės (prieinama
  tik superadmin).
- **Certificates** — *Sertifikatų institucija*: čia kuriami vienkartiniai
  kvietimai vaikams federacijoje prisijungti (vaikas pats susigeneruoja
  raktus ir atsiunčia tik pasirašymo užklausą) bei stebima, kada baigiasi
  sertifikatų galiojimas. Vienišam pod'ui šio puslapio nereikia.
- **Publish Script** — leidžia susidėlioti Zenoh publikavimo komandą
  testavimui ar duomenų įvedimui, neverčiant rašyti raktų išraiškų ranka.
- **Shell** — ribotas, audituojamas apvalkalas tiesiai į maršrutizatoriaus
  konteinerį, skirtas diagnostikai.
- **Logs** — bet kurios hosto valdomos paslaugos žurnalas realiu laiku.
- **Audit Logs** — kas per skydą buvo daroma su privilegijuotomis
  teisėmis: konfigūracijos pakeitimai, prekės ženklo redagavimas,
  naudotojų valdymas, prisijungimai.
- **WebUI Settings** (viršuje dešinėje, paskyros meniu) — čia rasite
  **Branding** (organizacijos pavadinimas, akcento spalva, logotipas — tik
  superadmin), **Appearance** (eilučių animacijos, tankesnės eilutės),
  **Live behavior** (kaip dažnai atnaujinami duomenys) ir šviesios/tamsios
  temos perjungiklį.

**Dvi apsaugos, su kuriomis anksčiau ar vėliau susidursite — abi
tyčinės.** EFDI federacijos sluoksnis sąmoningai atsisako veiksmų, kurie
galėtų tyliai sulaužyti pasitikėjimo ribas:

1. *„Apply trust ACL" neveikia — nevaldomas fabric uplink.* Tai reiškia,
   kad root vis dar skambina išeinančiam fabric peer'iui, kuris nėra
   įregistruotas kaip vaikas. Sprendimas: arba tą peer'į įregistruokite,
   arba išvalykite uplink per **Config → Fabric endpoints → „Root / no
   upstream"**.
2. *Valdomo maršrutizatoriaus ištrinti negalima.* Vietoj to jį galima
   nurašyti (decommission) arba izoliuoti (karantinuoti) — taip pašalintas
   maršrutizatorius negalės vėliau vėl pasirodyti tinkle kaip nepatikimas
   peer'is.

## Sąranka

Prie `compose/.env` pridėkite (pilną bloką rasite `compose/.env.example`):

```bash
ZENOH_ADMIN_DB_USER=zenoh_admin
ZENOH_ADMIN_DB_PASSWORD=<atsitiktinis>
ZENOH_ADMIN_DB_ROOT_PASSWORD=<kitas-atsitiktinis>
ZENOH_ADMIN_DB_PORT=3307                # ne numatytasis: išvengia konflikto su MariaDB/MySQL ant 3306
ZENOH_ADMIN_SECRET_KEY=<openssl rand -hex 32>
ZENOH_ADMIN_FIRST_USER=admin
ZENOH_ADMIN_FIRST_PASS=<nustatykite kartą, tada išvalykite po pirmo prisijungimo>
```

`ZENOH_ADMIN_FIRST_PASS` sukuria pirmą `superadmin` paskyrą, tik jei tokios
dar visai nėra — todėl po pirmo prisijungimo šį laukelį galite drąsiai
vėl ištuštinti: pati paskyra jau saugiai lieka MariaDB duomenų bazėje.

Administravimo paslauga dirba tik su MariaDB. Senesni PostgreSQL
migracijos įrankiai jau pašalinti po perkėlimo prie MariaDB; jei
atnaujinate diegimą, prieš perstatydami būtinai pasidarykite atsarginę
kopiją katalogo `${POD_STATE_DIR}/zenoh-admin/mariadb` ir failo
`compose/.env`.

## Paleidimas

```bash
cd compose
docker compose up -d zenoh-admin-db zenoh-admin zenoh-admin-proxy
```

Tada naršyklėje atverkite `https://<pod-host>:8890`.

Pats skydas (`zenoh-admin`) klausosi tik `127.0.0.1:8895` — tiesiogiai iš
išorės jo pasiekti negalima. Realų TLS ryšį adresu `:8890` užbaigia
priešais stovintis Caddy reverse proxy (`zenoh-admin-proxy`), naudodamas
savo vidinę CA (`local_certs` + `tls internal`, be jokios išorinės
ACME/CA priklausomybės); ši CA saugoma tome `zenoh_admin_caddy_data`, tad
išgyvena persileidimus. Pirmą kartą apsilankius naršyklė parodys
įspėjimą apie savarankiškai pasirašytą sertifikatą — tiesiog patikėkite
Caddy vietine CA arba priimkite įspėjimą, kad galėtumėte tęsti. Viešo
sertifikato čia sąmoningai nėra: šis skydas iš principo nėra skirtas būti
prieinamas iš viešo interneto.

## Runtime Control puslapis

TAK-stiliaus **Runtime Control** puslapyje `superadmin` viename lange gali:

- paleisti, stabdyti, persileisti ir tikrinti žurnalus kiekvienam
  registruotam tiltui, protokolo vertėjui, neapdorotam (raw) priėmimui ir
  TAK/SitaWare išvesties sluoksniui;
- redaguoti galinius taškus, prievadus, Zenoh temas, API adresus ir
  protokolų nustatymus;
- peržiūrėti ir redaguoti papildomus, konkrečiam diegimui specifinius
  `.env` laukus, kurie jau yra pod'e;
- įvesti naudotojų vardus, slaptažodžius, API raktus ir žetonus, nematant
  jau esamų paslapčių reikšmių.

Natyvūs procesai ir toliau valdomi per hosto PID. `start.sh` ir `run.sh
all` palaiko `admin-control` paleistą vietiniame 18896 prievade — API tik
deleguoja komandas tiems patiems paleidiklio scenarijams, o ne kuria po
atskirą konteinerį kiekvienai integracijai. Faile `compose/.env`
nustatykite `EFDI_CONTROL_TOKEN` — tai bearer žetonas ryšiui tarp admin
API ir vietinio valdymo proceso. Išsaugoję nustatymą, persileiskite
paveiktą paslaugą, kad ji perskaitytų naują aplinką.

Naudojant `./dev.sh up`, vienkartinis vystymo valdymo agentas pats
persikelia į 18896 prievadą, jei vystymui skirtas numatytasis 8896 jau
užimtas — dev API tiesiog nukreipiamas į tą pasirinktą prievadą.

## Valdomų maršrutizatorių hierarchija ir deleguota CA

Pirmo valdomo maršrutizatoriaus ribotą pavaldžią CA inicijuokite per
neprisijungimo (offline) ceremoniją. Šiai vienai komandai reikalingas
tėvinės/globalios CA privatus raktas, bet jis niekur nekopijuojamas į
maršrutizatoriaus būseną — panaudojamas ir pamirštamas:

```bash
scripts/pki/init-router-ca.sh \
  <šio-maršrutizatoriaus-vardų-sritis> \
  /offline/efdi-global-root.pem \
  /offline/efdi-global-root-key.pem \
  "${POD_STATE_DIR}/pki"
```

Iškart po to grąžinkite globalios šaknies raktą atgal į neprisijungusią
saugyklą. Toliau sukurkite maršrutizatoriaus ne-CA politikos pasirašytoją
ir, jei reikia, inicijuokite neprivalomą prisijungiantį (online) lapo
išdavėją po ribota maršrutizatoriaus CA:

```bash
scripts/pki/init-policy-signer.sh \
  <vardų-srities-priešdėlis>/<šio-maršrutizatoriaus-vardų-sritis> \
  "${POD_STATE_DIR}/pki/router-ca-cert.pem" \
  "${POD_STATE_DIR}/pki/router-ca-key.pem" \
  "${POD_STATE_DIR}/pki"

scripts/pki/init-step-ca.sh \
  "${POD_STATE_DIR}/pki/router-ca-cert.pem" \
  "${POD_STATE_DIR}/pki/router-ca-key.pem" \
  "${POD_STATE_DIR}/pki/step-ca" \
  <vpn-dns-vardas-ar-ip>
```

Politikos raktas pasirašo delegavimo ir valdymo vokus, bet pats
sertifikatų išduoti negali. step-ca savo ruožtu gauna sugeneruotą
prisijungiantį tarpinį sertifikatą ir niekada nelaiko paties ribotos
maršrutizatoriaus-CA rakto. Sukonfigūravę hosto kelius `compose/.env`
faile, persileiskite `admin-control`:

```bash
EFDI_ROUTER_CA_CERT_PATH=/absoliutus/vykdymo/pki/router-ca-cert.pem
EFDI_ROUTER_CA_KEY_PATH=/absoliutus/vykdymo/pki/router-ca-key.pem
EFDI_ROUTER_CA_CHAIN_PATH=/absoliutus/vykdymo/pki/router-ca-chain.pem
EFDI_POLICY_SIGNER_CERT_PATH=/absoliutus/vykdymo/pki/policy-signer-cert.pem
EFDI_POLICY_SIGNER_KEY_PATH=/absoliutus/vykdymo/pki/policy-signer-key.pem
EFDI_STEP_CA_STATE_PATH=/absoliutus/vykdymo/pki/step-ca
./stop.sh admin-control
./start.sh --service admin-control
```

Skiltyje **Certificate Authority** sukurkite vienkartinį kvietimą vaiko
vardų sričiai ir nurodykite, kiek papildomų CA lygių tas vaikas gali
deleguoti toliau. UI pats apskaičiuoja leistiną maksimumą pagal išdavėjo
sertifikatą — kiekvieno vaiko gylis privalo būti griežtai mažesnis už jo
tėvo X.509 kelio-ilgio apribojimą.

Vaiko pusėje visas tris tapatybes sugeneruokite ir įregistruokite vietoje:

```bash
scripts/pki/enroll-router.sh \
  https://<tėvo-valdymo-host>:8890 \
  <vaiko-vardų-sritis> \
  "${BUNDLE_DIR}/efdi" \
  "${POD_STATE_DIR}/pki"
```

Scenarijus paklaus kvietimo žetono (nerodydamas jo argv), o į tėvą
nusiunčia tik router-CA, transporto ir policy-signer sertifikatų
pasirašymo užklausas (CSR). Gautas atsakymas jau turi pilną pasirašytą
delegavimo grandinę, viešą tėvo pasitikėjimo sertifikatą ir vienkartinį
nuorodos kredencialą — patys privatūs raktai niekada nepalieka vaiko.
Sukonfigūruokite atspausdintus kelius ir paleiskite įprastą
first-boot/perstatymo eigą. CA privatūs raktai visą laiką lieka už
localhost hosto valdymo ribos.

Jei tėvas jau turi inicijuotą step-ca, įregistravimo metu vaikas gauna
atsinaujinantį 24 valandų transporto sertifikatą. Vaiko pusėje nustatykite
`EFDI_STEP_CA_URL` į tėvo VPN adresą ir, jei reikia, perrašykite
`EFDI_STEP_RENEW_*_PATH`. Nuo šiol `start.sh` ir `run.sh` palaiko
PID-valdomą `cert-renewer` procesą nuolat veikiantį: jis kas 15 minučių
patikrina būseną, per aštuonių valandų langą (nustatomą per
`EFDI_STEP_RENEW_BEFORE_SECONDS`) atnaujina sertifikatą, atnaujina aktyvų
maršrutizatoriaus sertifikatą ir persileidžia tiek maršrutizatorių, tiek
admin sertifikatų vartotojus. Pati Router-CA ir politikos autoriteto
rotacija lieka atskira, aiškiai atliekama pakartotinio įregistravimo
operacija — tai niekada nevyksta automatiškai kaip prisijungusi
privilegijų eskalacija.

Kiekvienas maršrutizatorius toliau gali valdyti savo tiesioginius vaikus.
**Zenoh Config** gali pasiekti bet kurį patvirtintą palikuonį, bet komanda
kas kartą pasirašoma ir persiunčiama po vieną tėvo/vaiko žingsnį. Gavėjas
pirmiausia patvirtina visą gautą failą, paleisdamas prisegtą Zenoh
programą su išjungtu tinklu; tik tada jį atomiškai aktyvuoja, laukia,
kol paslauga taps sveika, o nepavykus — atstato paskutinę žinomą-gerą
konfigūraciją. **Changes** puslapyje matote visą revizijos kelią ir
galutinę būseną. Praradus ryšį su tėvu, naujos komandos „iš viršaus"
nustoja ateiti, bet tai nesustabdo nei šakos jau veikiančios duomenų
plokštumos, nei vietinio WebUI, nei jos pačios potinklio valdymo.

Nuotolinis redaktorius visada pradeda nuo paskutinio to maršrutizatoriaus
paties pateikto struktūrizuoto momentinio vaizdo. Tėvo atsiųstas
pakeitimas negali paliesti vaiko tapatybės, klausymosi prievadų, fabric
CA profilio, sertifikato vardo patikros politikos ar organizacijos
valdymo priešdėlio. Uplink pakeitimas sąmoningai atliekamas dviem
etapais: pirmiausia pridedamas naujas galinis taškas, senąjį paliekant
kaip yra, patikrinamas pakeitimas, ir tik tada senasis galinis taškas
pašalinamas. Jei po persileidimo neatsiranda nė viena nuotolinio
maršrutizatoriaus sesija, vaikas pats atstato ankstesnę konfigūraciją.

Topologijos ir būsenos duomenys apima visą ribotą, viešai patikrinamą
delegavimo įrodymą. Prieš rodydamas palikuonį kaip patvirtintą, root
pats patikrina kiekvieną CA parašą, politikos pasirašytoją, vardų
srities susiaurinimą, gylį, galiojimo trukmę ir atšaukimo būseną.
Sugeneruotas ACL aktyvavimas sąmoningai atmetamas, jei root vis dar turi
nevaldomą fabric uplink — pirmiau tokį peer'į reikia įregistruoti arba
perkelti kitur, o tik tada taikyti valdomus ACL.

Prieš diegdami, paleiskite vienkartinį vykdymo testą:

```bash
tests/smoke/loopback.sh
```

## Rolės

| Rolė | Dashboard | Config (žiūrėti) | Config (redaguoti + persileisti maršrutizatorių) | Admin Users |
| --- | --- | --- | --- | --- |
| `readonly` | ✓ | | | |
| `admin` | ✓ | ✓ | | |
| `superadmin` | ✓ | ✓ | ✓ | ✓ |

Kai išsaugote konfigūracijos pakeitimą, pirmiausia sugeneruojami
struktūrizuoti laukai, o visas kandidatas patvirtinamas tuo pačiu
prisegtu Zenoh binaru, kuris naudojamas ir vykdymo metu — tik bandymui
jame išjungti klausymasis, jungtys, scouting ir papildiniai. Įrašomas
atomiškai tik toks kandidatas, kuris šią patikrą praėjo. Po to
maršrutizatorius persileidžiamas ir tikrinama jo sveikata; jei kažkas
nepavyksta, sistema pati atstato paskutinę žinomą-gerą konfigūraciją ir
grąžina ankstesnę būseną.

## Config kortelės laukai

Config kortelėje matote struktūrizuotus laukus, ne neapdorotą JSON5 —
kiekvieną kartą išsaugant, šablonas `../examples/zenoh-router.json5.tmpl`
(tas pats, kurį naudoja ir `first-boot.sh`) iš naujo sugeneruojamas su
žemiau nurodytomis reikšmėmis. Todėl išsaugota konfigūracija niekada
negali nuklysti nuo šablono struktūros.

| Laukas | Poveikis |
| --- | --- |
| Local mTLS port | Mesh-nukreiptas klausymosi prievadas tiltams ir audito imtuvui (numatytasis 7447) |
| Local TCP port | Paprastas, tik-vietinis klausymosi prievadas tiltams ir šiam skydui (numatytasis 7448) |
| Fabric endpoint | Galinis taškas, į kurį šis pod'as pats skambina — įvedamas kaip atskiri Host ir Port laukai (schema visada `tls`, atskirai niekur nerodoma); anksčiau naudotiems galiniams taškams pasiūlomi vienu paspaudimu pasirenkami šablonai |
| Partner namespace | Šio pod'o pirmosios šalies publikavimo/prenumeravimo priešdėlis (jo lizdas) — **jį pakeitus, kita fabric pusė taip pat turi leisti naują reikšmę savo ACL, kitaip publikacijos tyliai nustos ją pasiekti** |
| Inbound namespace | Dvišalis priešdėlis, kuriuo fabric publikuoja duomenis Į šį pod'ą |
| Verify name on connect | Pagal nutylėjimą išjungta, nes gateway sertifikato SAN susietas su mesh IP, o ne su rinktu DNS vardu; šią parinktį įjungus, fabric ryšys gali nutrūkti |
| Storage plugin loading | Išjungus, nauji prenumeratoriai per `get()` nebegauna paskutinės žinomos reikšmės — pats publikavimas ir prenumeravimas ir toliau veikia įprastai |

Trys nustatymai skyde sąmoningai **nerodomi** — juos netyčia sukonfigūravus
blogai, per lengva atkirsti nuo prieigos kiekvieną klientą, įskaitant ir
patį skydą: `access_control.enabled`, `default_permission`,
`enable_mtls`. Jei kada nors jų prireiktų, redaguokite juos tiesiogiai
faile `zenoh/config.json5`.

### Galinio taško pagalbininko naudojimas

Config puslapio skyrius `Fabric endpoints` ir yra tas pagalbininkas,
matomas ekrano nuotraukoje:

- įveskite host ir port,
- paspauskite `Add direct link`, jei norite pridėti dar vieną
  `connect.endpoints` įrašą,
- pasirinkite `Root / no upstream`, jei norite išvalyti visą sąrašą, arba
  vieną iš siūlomų šablonų, jei norite iškart užpildyti žinomą galinį
  tašką,
- išsaugokite konfigūraciją — tik tada `connect.endpoints` masyvas
  iš tikrųjų atsiras faile `config.json5`.

Panašų trumpinį turi ir publikavimo kūrėjas, veikiantis neapdoroto
konfigūracijos teksto lygyje: `Add to connect.endpoints` įterpia
kandidatinį galinį tašką tiesiai į dabartinį maršrutizatoriaus
konfigūracijos tekstą.

### Trijų maršrutizatorių mesh pavyzdys

`zenoh1` / `zenoh2` / `zenoh3` klasteriui vienodas šablonas atrodo taip:

```json5
zenoh1: {
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447"] },
  connect: { endpoints: ["tls/zenoh2.efdi.ltu:7447", "tls/zenoh3.efdi.ltu:7447"] },
  transport: { link: { tls: { root_ca_certificate: "/root/.zenoh/certs/efdi_ca.crt", listen_certificate: "/root/.zenoh/certs/zenoh1.pem", listen_private_key: "/root/.zenoh/certs/zenoh1.key", connect_certificate: "/root/.zenoh/certs/zenoh1.pem", connect_private_key: "/root/.zenoh/certs/zenoh1.key", enable_mtls: true, verify_name_on_connect: true } } },
  plugins: { rest: { http_port: 8000 } },
  plugins_loading: { enabled: true },
  access_control: { enabled: true, default_permission: "allow", rules: [], subjects: [], policies: [] }
}
```

```json5
zenoh2: {
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447"] },
  connect: { endpoints: ["tls/zenoh1.efdi.ltu:7447", "tls/zenoh3.efdi.ltu:7447"] },
  transport: { link: { tls: { root_ca_certificate: "/root/.zenoh/certs/efdi_ca.crt", listen_certificate: "/root/.zenoh/certs/zenoh2.pem", listen_private_key: "/root/.zenoh/certs/zenoh2.key", connect_certificate: "/root/.zenoh/certs/zenoh2.pem", connect_private_key: "/root/.zenoh/certs/zenoh2.key", enable_mtls: true, verify_name_on_connect: true } } },
  plugins: { rest: { http_port: 8000 } },
  plugins_loading: { enabled: true },
  access_control: { enabled: true, default_permission: "allow", rules: [], subjects: [], policies: [] }
}
```

```json5
zenoh3: {
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447"] },
  connect: { endpoints: ["tls/zenoh1.efdi.ltu:7447", "tls/zenoh2.efdi.ltu:7447"] },
  transport: { link: { tls: { root_ca_certificate: "/root/.zenoh/certs/efdi_ca.crt", listen_certificate: "/root/.zenoh/certs/zenoh3.pem", listen_private_key: "/root/.zenoh/certs/zenoh3.key", connect_certificate: "/root/.zenoh/certs/zenoh3.pem", connect_private_key: "/root/.zenoh/certs/zenoh3.key", enable_mtls: true, verify_name_on_connect: true } } },
  plugins: { rest: { http_port: 8000 } },
  plugins_loading: { enabled: true },
  access_control: { enabled: true, default_permission: "allow", rules: [], subjects: [], policies: [] }
}
```

Jei prie šio klasterio norite prijungti ir ketvirtą maršrutizatorių,
pridėkite jo DNS vardą ar IP adresą į visų trijų `connect.endpoints`
sąrašus ir patikrinkite, kad sertifikato SAN atitinka rinktą vardą.

## Izoliuotas testinis maršrutizatorius

Vietiniam pub/sub testavimui, neliečiant nei tikro pod'o, nei jo fabric
ryšio, skirtas `zenoh-router-test` po `test` compose profiliu — jis
niekada nepasileidžia kartu su likusiu steku savaime.

```bash
cd compose
docker compose --profile test up -d zenoh-router-test
```

Jo konfigūracija saugoma `${POD_STATE_DIR}/zenoh-test/config.json5` —
naudoja tuos pačius sertifikatus, vardų sritį ir ACL kaip tikrasis
maršrutizatorius, bet skirtingus prievadus (`7457` mTLS / `7458` TCP,
vietoj `7447`/`7448`) ir **niekada neturi `connect.endpoints`** (tad prie
fabric apskritai neprisijungia). Jį galima drąsiai palikti veikti šalia
tikrojo maršrutizatoriaus — jokio konflikto nekyla.
