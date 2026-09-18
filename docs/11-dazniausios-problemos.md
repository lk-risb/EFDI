# 11 — Dažniausios problemos

## 11.1 Simptomais pagrįsti sprendimai

Simptomais pagrįsti dažniausių diegimo problemų sprendimai. Infrastruktūros
lygio pamokoms (DNS, TLS profiliai, atominiai rašymai — dalykai, netinkantys
vienam simptomui), žr. [§11.2 Pastebėti dalykai](#112-pastebėti-dalykai--jau-apmokėtos-pamokos) žemiau.

### Zenoh ryšio klaida

**Simptomas:** `zenoh.ZError: Unable to connect to any of [tls/zenoh.efdi...]`

```bash
# 1. Patikrinkite, ar routeris sveikas
docker compose -f compose/docker-compose.yml ps zenoh-router

# 2. Patikrinkite, ar nustatytas galinio taško kintamasis
echo $ZENOH_LOCAL_ENDPOINT   # tikimasi: tcp/127.0.0.1:7448

# 3. Patikrinkite, ar egzistuoja sertifikatų failai
ls $EFDI_CERT_DIR/*.pem
```

Paprastas `source compose/.env` neeksportuoja kintamųjų vaikiniams procesams.
Naudokite `./start.sh` (jis tuo pasirūpina), arba:

```bash
set -a && source compose/.env && set +a
```

### Takeliai neatsiranda ATAK

```bash
# 1. Patvirtinkite, kad tak-layer veikia
kill -0 $(cat $POD_STATE_DIR/.pids/tak-layer.pid) && echo running

# 2. Patvirtinkite, kad TAK Server ryšys užmegztas
ss -tn "( dport = :$TAK_PORT )"

# 3. Patvirtinkite, kad TAK_HOST/TAK_PORT/TAK_TLS .env atitinka tikrą TAK Server galinį tašką
```

### CAT-34 radaro žymeklis trūksta

Radaras nesiunčia CAT-34 I034/120 (3D-Position), todėl EFDI negali saugiai
nustatyti savo vietos. Patikrinkite CAT-34 žurnalą — ieškokite `has no site
position`. Geriausia įjungti I034/120 pačiame radare ar šliuze; jei radaras
vienas, atsargines koordinates galima nustatyti ir `.env`:

```bash
grep CAT34_RADAR compose/.env
```

### Drono aptikimai nepublikuojami

Tiltas atmeta aptikimus, senesnius nei 300 s. Patikrinkite, ar API pasiekiamas
ir ar duomenys šviežūs:

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

### SitaWare vienetai neatsiranda ATAK

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

Jei SitaWare vieneto SIDC negalioja (nėra 15 ženklų), jis nukeliauja į
`…/land/sitaware/c2/unknown/unit/…` ir rodomas kaip nežinomas antžeminis
vienetas (`a-u-G-U-C`). Patikrinkite neapdorotą SIDC reikšmę žurnale:

```bash
grep "sidc=" $POD_STATE_DIR/logs/sitaware.log | head -10
```

### EFDI takeliai neatsiranda SitaWare HQ

```bash
tail -f $POD_STATE_DIR/logs/sitaware-hq-nvg.log
curl -u "$SITAWARE_HQ_NVG_USER:$SITAWARE_HQ_NVG_PASS" \
  -o /dev/null -w '%{http_code} %{content_type}\n' \
  "http://127.0.0.1:${SITAWARE_HQ_NVG_PORT:-8088}${SITAWARE_HQ_NVG_PATH:-/nvg}"
```

Turi būti `200 application/xml`. HQ NVG valdytoje patikrinkite, ar prenumerata
neuždaryta, prijungta, apklausia EFDI serverio adresą (ne HQ adresą) ir
taikosi į `efdi-live / EFDI Live Tracks`. Jei sukonfigūruotas TLS, `-k`
praleiskite tik tada, kai jau pasitikima išduodančia CA. Jei vietinis testas
grąžina `200`, bet HQ vis tiek nepavyksta prisijungti — tai maršrutizavimo,
ugniasienės (Windows ar Linux) ar sertifikato pasitikėjimo problema, ne NVG
konversijos klaida.

**Latest replication** laiko žyma turi judėti pirmyn. Jei ji sena, o
**Reload** rodo nežinomą klaidą, patikrinkite tą patį URL iš PowerShell HQ
serveryje: ryšio nesėkmė reiškia maršrutizavimą/ugniasienę, HTTP 401 —
trūkstamus ar pasenusius prenumeratos kredencialus, sėkmė tik su `-k` — kad
srauto CA nepatikima paskyros/paslaugos, atliekančios importą. Pirma
pataisykite replikaciją, tik tada keiskite senesnį sluoksnį — kitaip pakaitinis
sluoksnis liks tuščias.

Autentifikuotas sveikatos galinis taškas parodo serverio pusės būseną,
neregistruodamas kredencialų ar NVG turinio:

```bash
curl -ksS -u "$SITAWARE_HQ_NVG_USER:$SITAWARE_HQ_NVG_PASS" \
  "https://127.0.0.1:${SITAWARE_HQ_NVG_PORT:-8088}/healthz" | python3 -m json.tool
```

- `successful_requests` lieka nulis — HQ apskritai nepasiekė srauto.
- `unauthorized_requests` didėja — HQ pasiekė srautą, bet su trūkstamais ar
  pasenusiais Basic kredencialais.
- `successful_requests` didėja, o HQ vis tiek rodo Pending — problema NVG
  analizavime ar pasirinktame tiksliniame sluoksnyje, ne maršrutizavime ar
  autentifikacijoje.

Srauto prieigos žurnaluose lieka tik rezultatas, takelių skaičius ir kliento
adresas — po vieną eilutę per minutę tiek sėkmingiems, tiek neautorizuotiems
paėmimams.

### Dubliuoti proceso egzemplioriai

Sukelia dukart paleistas `start.sh` be sustabdymo:

```bash
pkill -f "_bridge\.py\|tak_layer\|track_fusion"
rm -f $POD_STATE_DIR/.pids/*.pid
./start.sh
```

### Radaro ikona dingsta iš ATAK

`asterix` tiltas publikuoja keepalive kas 60 s, nesvarbu ar takelis aktyvus.
Jei ikona dingo, reiškia tiltas sustojo:

```bash
tail -20 $POD_STATE_DIR/logs/asterix.log | grep -E "keepalive|startup|error"
```

## 11.2 Pastebėti dalykai — jau apmokėtos pamokos

Tai *eksploatacijos/infrastruktūros* pastebėjimų sąrašas — porininkas
[`../.ai/.claude/CLAUDE.md`](../.ai/.claude/CLAUDE.md) (ten ASTERIX bitų lygio
dekodavimo pastebėjimai) ir [§11.1 Simptomais pagrįstiems sprendimams](#111-simptomais-pagrįsti-sprendimai).
Kiekvienas žemiau aprašytas dalykas — reali, patvirtinta problema, su kuria
susidurta eksploatuojant šį podą. Perskaitykite prieš derindami ką nors panašaus
į šiuos simptomus, kad nereikėtų iš naujo atrasti tos pačios diagnozės.

### NetBird split-DNS nematomas konteinerių viduje

**Simptomas:** Routerio konfigūracija nurodo mesh vardą (pvz.,
`zenoh2.efdi.ltu`); konteineris net nebando prisijungti — jokio lizdo,
jokios TLS klaidos, tik tyla.

**Priežastis:** `network_mode: host` dalinasi tinklo *vardų sritimi*, bet ne
`/etc/resolv.conf`. NetBird split-DNS resolveris mesh domenui veikia tik
**serveryje** — konteineris vis tiek gauna Docker sugeneruotą resolverį
(dažniausiai jūsų LAN DNS), kuris apie mesh domeną nieko nežino. Serveryje
vardas išsisprendžia be problemų (`getent hosts` ten veikia), o konteineryje
tyliai nepavyksta — iš išorės atrodo lygiai taip, lyg konteineris net
nebandytų prisijungti.

**Sprendimas:** Pridėkite `extra_hosts` įrašus, kurie kiekvieną mesh vardą
tiesiogiai susieja su jo dabartiniu NetBird IP toje konteinerio compose
paslaugoje. Domenų vardai lieka programos konfigūracijoje — reikia tik
konteinerio vietinio vardų sprendimo. Jei NetBird kada nors perpriskiria IP,
šiuos įrašus reikės atnaujinti.

### TLS/mTLS identiteto profilis turi atitikti galinį tašką, kurį jis rinkis

**Simptomas:** Magistralės ryšio bandymas nesukuria jokios klaidos ir jokio
ryšio — atrodo identiškai kaip DNS problema aukščiau, ar ugniasienės blokas.

**Priežastis:** Kiekvieną nuotolinį fabriką (backbone, partnerio sandbox,
paties podo vietinį mesh) pasirašo **skirtinga** CA. Jei teisingas galinis
taškas gauna neteisingą sertifikato identitetą, mTLS rankos paspaudimas
žlunga — o priklausomai nuo žlugimo tipo, tai gali atrodyti taip, lyg nieko
visai neįvyko, ne kaip aiškus atmetimas.

**Sprendimas:** Galinis taškas ir TLS identiteto profilis — vienas atominis
pasirinkimas, jų negalima koreguoti atskirai. Jei įrankiai siūlo išankstinius
nustatymus, sujunkite galinį tašką su atitinkamu sertifikato profiliu į vieną
išankstinį nustatymą, o ne į du atskirus laukus, kuriuos lengva sumaišyti.

### Vieno failo bind-mount sulaužo atominius rašymus

**Simptomas:** Konfigūracijos-taikymo galinis taškas, kuris rašo mažą
būsenos failą (pvz., vardų-srities-priešdėlio failą), nesėkmingai baigiasi
su `OSError: [Errno 16] Device or resource busy`, nors pagrindinio
konfigūracijos failo rašymas visai šalia veikia gerai.

**Priežastis:** Standartinis atominio rašymo šablonas — rašyti į laikiną
failą, tada `os.replace(temp, target)`; būtent pervadinimas garantuoja, kad
skaitytojas niekada nematys pusiau įrašyto failo. Bet šis pervadinimas
nepavyksta, kai `target` pats yra vieno failo Docker bind-mount
(`-v host/file:/container/file`) — kelias *yra* mount taškas, o per mount
tašką pervadinti negalima. Katalogu sumontuotam failui šios problemos nėra,
nes pervadinimas vyksta pačiame sumontuotame kataloge, ne per mount'ą.

**Sprendimas:** Kai `os.replace` žlunga su `EBUSY`, grįžkite prie perrašymo
vietoje (open-write-fsync, be pervadinimo). Tai nėra atomiška, bet tai
vienintelė galimybė bind-mounted vienam failui — ir geriau nei nulaužti visą
taikymo operaciją dėl nesusijusio failo.

### Identiškai pavadintos dubliuotos funkcijų apibrėžtys tyliai užstoja

**Simptomas:** Dekoderis/tvarkyklė atrodo akivaizdžiai neteisinga, kai
skaitote ją (neteisingas lauko plotis, neteisinga skalė, klaida, kuri turėtų
būti labai matoma išvestyje) — bet produkcijos duomenys, ateinantys iš kito
galo, atrodo gerai.

**Priežastis:** Python leidžia be jokio įspėjimo iš naujo apibrėžti funkciją
tame pačiame modulyje. Jei faile du kartus yra `def handler(...)`, tyliai
laimi **antroji** apibrėžtis — pirmoji tampa visiškai negyvu kodu, kuris vis
tiek *atrodo* gyvas (ta pati įtrauka, jokios apsaugos, dažnai net abi
teisingai dokumentuotos). Joks šio repo linteris to numatytai nepažymi. Taip
realiai sugadintas kodo kelias gali metų metus išgulėti faile nieko
neįtakodamas — ir kainuoti tikrą derinimo laiką, kai žmogus pirmiausia
perskaito būtent tą "akivaizdžiai sugadintą" kopiją.

**Sprendimas:** Prieš pasitikėdami, kad skaitoma funkcija yra ta pati, kuri
iš tikrųjų vykdoma, patikrinkite vykdymo metu:
`inspect.getsourcelines(module.the_func)` parodys, kurios apibrėžties eilutė
realiai susieta. Jei repo augo organiškai (kategorijos ar variantai pridėti
laikui bėgant, kiekvienas su "savo" panašios logikos kopija), kai kas nors
neatrodo teisingai, ieškokite funkcijos vardo visame faile — ne tik ten, kur
radote pirmą kartą.

### Paslaugos rinkiniui reikia savos būsenos agregacijos

**Simptomas:** WebUI ar būsenos galinis taškas rodo daugiaprocesį rinkinį
(keli vaikai po viena logine "paslauga") kaip nuolat sustabdytą, nors
kiekvienas vaiko procesas iš tikrųjų veikia.

**Priežastis:** Bendra būsenos logika tikrina vieną pidfile, pavadintą pagal
paslaugą — bet jo niekada neras, jei rinkinio paleidiklis rašo po vieną
pidfile *kiekvienam vaikui* (pvz., `asterix-cat10.pid`, `asterix-cat48.pid`,
...). Pats rinkinys savo pidfile neturi, todėl visada rodo "sustabdyta".

**Sprendimas:** Rinkinio paslaugai reikia atskiros būsenos logikos, kuri
surenka ir apibendrina visų vaikų pidfile, ir pagal tai, kiek jų gyva,
praneša veikia/sutrikusi/sustabdyta — ne naiviai tikrina vieną pidfile.

### Prijungtas (bind-mount) būsenos failas priklauso ne tam naudotojui, ne tik blogai prijungtas

**Simptomas:** Konfigūracijos išsaugojimas per WebUI nepavyksta su
`[Errno 13] Permission denied: '/data-topic-prefix'` (arba
`/namespace-prefix`, arba TAK/SitaWare kredencialų įkėlimo katalogais). Tai
kitokia klaida nei aukščiau aprašytas `EBUSY` atominio rašymo atvejis — čia
paprasčiausiai teisių klaida, ne pervadinimas per mount tašką.

**Priežastis:** `zenoh-admin` visada veikia fiksuotu ne-root uid/gid
(`10001`). Keli būsenos keliai — atskirai per bind-mount prijungti failai ar
katalogai (`namespace-prefix`, `data-topic-prefix`, `integrations/tak`,
`$BUNDLE_DIR/efdi`) — sukurti **hosto** pusėje to naudotojo, kuris paleido
`install.sh`/`reinstall.sh` (dažniausiai root). Root sukurtas failas su
teisėmis `644` rašomas tik savininko (root); uid 10001 neturi nei savininko
bito, nei (nebent grupė jau būtų 10001) grupės rašymo bito, todėl kiekvienas
rašymas iš konteinerio vidaus žlunga.

**Sprendimas:** `install.sh` ir `reinstall.sh` dabar visiems šiems keliams po
jų sukūrimo atlieka `chgrp 10001` + `chmod 664` (failams) / `775`
(katalogams). Jei tvarkote dėžę, sukonfigūruotą prieš šį pataisymą, arba
paleiskite `reinstall.sh` iš naujo, arba ištaisykite tiesiogiai:

```bash
chgrp 10001 "$POD_STATE_DIR/namespace-prefix" "$POD_STATE_DIR/data-topic-prefix"
chmod 664   "$POD_STATE_DIR/namespace-prefix" "$POD_STATE_DIR/data-topic-prefix"
chgrp 10001 "$POD_STATE_DIR/integrations/tak" "$BUNDLE_DIR/efdi"
chmod 775   "$POD_STATE_DIR/integrations/tak" "$BUNDLE_DIR/efdi"
```

`health.sh` interaktyvus meniu (3 punktas — „patikrinti trūkstamus/blogai
sukonfigūruotus būsenos failus") dabar aptinka ir automatiškai ištaiso
neteisingas šių kelių teises, ne tik trūkstamus failus.

### Neapdorota išimtis API apdorojime pasirodo kaip tuščias HTTP 500

**Simptomas:** WebUI rodo paprastą pranešimą „Unexpected error applying
config" arba „Request failed (HTTP 422)" be jokios papildomos informacijos —
jokios užuominos, kas iš tikrųjų nutrūko, nors serveris *iš tikrųjų* susidūrė
su konkrečia klaida.

**Priežastis:** Apdorojimo funkcija sugauna `Exception` tik tam, kad ją
užregistruotų, o po to tuščiu `raise` perduoda toliau — bet originalus
pranešimas prarandamas, kai jį perima FastAPI numatytasis klaidų
apdorojimas, ir klientas mato tik bendrą būsenos kodą. Jei pati pradinė
klaida savo „detail" grąžino tuščią eilutę (pvz., subprocesas nulūžo dar
nespėjęs nieko parašyti į stdout/stderr), tai net teisingai perduota
išimtis neturi ką parodyti.

**Sprendimas:** Neapdorotą išimtį paverskite `HTTPException` su realiu
pranešimu (`raise HTTPException(500, detail=f"...: {exc}") from exc`). Bet
kokį kelią, kuriuo pranešama apie subproceso ar patikros nesėkmę, priverskite
grąžinti aprašomąjį pakaitalą (`"exited N with no output"`) vietoj tuščios
eilutės — kad *kitą* kartą būtų galima diagnozuoti vien iš atsakymo turinio,
be prieigos prie serverio žurnalo.

### Formos laukas tyliai priima reikšmę, sudarytą visai kitokia forma nei reikia

**Simptomas:** Laukui „bind adresas" arba panašiam vienos paskirties
nustatymui pateikiamas pilnas URL (`http://0.0.0.0:8088/nvg`) arba ne to
kompiuterio IP adresas vietoj gryno vietinio IP, o paslauga, kuri jį skaito,
nulūžta su kažkuo nesuprantamu kaip `socket.gaierror: [Errno -2] Name or
service not known` ties `socket.bind()`.

**Priežastis:** Bind adresas perduodamas tiesiai į `socket.bind((host,
port))` — tai niekada nebūna URL (be schemos, prievado ar kelio), ir tai yra
*šio paties kompiuterio* klausymosi adresas, o ne kompiuterio, kuris prie jo
jungsis. Paprastas teksto laukas neapsaugo, jei naudotojas įveda pilną URL
ar ne tos mašinos adresą — klaida iššoka tik vėliau, bibliotekos viduje, per
kelis sluoksnius nuo lauko, kuris ją sukėlė.

**Sprendimas:** `SITAWARE_HQ_NVG_BIND` reikšmė turi būti grynas IP —
`0.0.0.0`, kad klausytų visų sąsajų (kad pasiektų *kita* mašina, pvz.,
SitaWare HQ dėžė), arba `127.0.0.1` tik vietiniam ryšiui. Prievadas ir kelias
yra atskiri laukai — jų čia nerašykite. Apskritai: kai laukas nulūžta toli
nuo vietos, kur jis nustatytas, pirmiausia patikrinkite jo saugomą reikšmę
(`grep KEY .env`) ir tik po to gilinkitės į nulūžimo vietą.

### Naujai veikiantis srautas vis tiek atmetamas — pirma patikrinkite autentifikaciją, ne maršrutą

**Simptomas:** Nuotolinė sistema jau gali pasiekti srautą (nebe „connection
refused"/timeout), bet kiekviena užklausa vis tiek atmetama, o paties srauto
žurnale rašoma kažkas panašaus į `rejected unauthorized request from <ip>`.

**Priežastis:** Ryšio pasiekiamumas (teisingas prievadas, teisingas bind
adresas) — atskira problema nuo autentifikacijos. Srautas su sukonfigūruota
Basic autentifikacija (`SITAWARE_HQ_NVG_USER`/`_PASS`) atmeta kiekvieną
užklausą be tinkamų kredencialų — net iš kliento, kuris kitu atveju puikiai
pasiekiamas.

**Sprendimas:** Arba sukonfigūruokite *nuotolinę* pusę (šiuo atveju —
SitaWare HQ importo prenumeratą) su tuo pačiu naudotojo vardu/slaptažodžiu
kaip ir srautas, arba — tik greitam izoliuoto laboratorinio tinklo
testui — nustatykite `SITAWARE_HQ_NVG_ALLOW_ANONYMOUS=1`, kad visiškai
praleistumėte autentifikaciją, kol patvirtinsite, jog duomenys teka.

### Takeliai sublyksi / dingsta ir vėl atsiranda fiksuotu ciklu

**Simptomas:** Objektai apklausa pagrįstame sraute (pvz., SitaWare NVG
importe) sublyksi ir atsiranda periodu, sutampančiu su apklausos intervalu,
nors pirminis šaltinis iš tikrųjų vis dar teikia duomenis.

**Priežastis:** Srauto talpykla pašalina objektą, kuris neatsinaujino per
„stale" (paseno) langą (`SITAWARE_HQ_NVG_STALE_S` arba atitinkamas
nustatymas kitame apklausa pagrįstame sluoksnyje). Jei *pirminis* tiltas
atnaujina konkretų objektą tik kas N sekundžių (pvz.,
`dronuradaras_bridge.py` radaro mazgų pozicijoms turi `DEVICE_POLL_S = 60`),
o „stale" langas trumpesnis už šį intervalą — objektas dalį kiekvieno
pirminio ciklo pasensta ir dingsta, tada vėl atsiranda su kitu atnaujinimu.
Tai slenkantis, nevienalaikis mirgėjimas, ne švarus vienalaikis.

**Sprendimas:** Nustatykite srauto „stale" ribą gerokai virš lėčiausio jį
maitinančio pirminio atnaujinimo intervalo — bent dvigubai, pvz., `120`
60 sekundžių pirminiam ciklui. Atskirai, tą patį efektą gali sustiprinti ar
užmaskuoti žemesnės grandies C2 sistemos pačios „sluoksnio galiojimo"/
„takelio išsaugojimo" nuostata (SitaWare Layer Details puslapyje yra abi) —
jei vien „stale" ribos pakėlimas neišsprendžia, patikrinkite ir ją.

### `pip install` nepavyksta su „externally-managed-environment"

**Simptomas:** Paleidus `pip install -r requirements.txt` tiesiai prieš
sisteminį `python3` (o ne per `install.sh`/`start.sh`), nepavyksta su
`error: externally-managed-environment` / „This environment is externally
managed" (PEP 668, dažna šiuolaikiniuose Debian/Ubuntu).

**Priežastis:** Sisteminis Python tyčia užrakintas nuo nevaldomų `pip
install`. `install.sh` ir `start.sh` su tuo nesikovoja — jie tiesiog sukuria
ir naudoja savo virtualią aplinką `compose/venv`, iš kurios ir veikia
kiekviena šio podo hoste paleista Python paslauga.

**Sprendimas:** Naudokite tą virtualią aplinką tiesiogiai, ne sisteminį
interpretatorių:

```bash
compose/venv/bin/pip install -r compose/requirements.txt
compose/venv/bin/python3 layers/some_layer.py
```

Jei `compose/venv` dar neegzistuoja, sukurkite ją taip, kaip tai daro
`install.sh`: `python3 -m venv compose/venv`, tada diekite į ją. Niekada
neperduokite `--break-system-packages` sisteminiam `pip` — kiekviena kita
hoste veikianti paslauga jau tikisi rasti virtualią aplinką, ne sisteminį
interpretatorių.

### Du skirtingi „Save" mygtukai tame pačiame puslapyje daro skirtingus dalykus

**Simptomas:** Reikšmės išsaugojimas viename WebUI konfigūracijos puslapio
skyriuje (pvz., Integration Settings lauke, tokiame kaip SitaWare ar TAK
nustatymas) atrodo, tarsi nieko nedaro, arba sukelia nesusijusią klaidą
(Zenoh maršrutizatoriaus konfigūracijos patvirtinimo klaidą), kuri neturi
nieko bendra su tuo, kas iš tikrųjų buvo keičiama.

**Priežastis:** Zenoh Config puslapyje yra dvi nepriklausomos išsaugojimo
funkcijos. Viršuje esantis **„Save & Restart"** patvirtina ir pritaiko
*Zenoh maršrutizatoriaus* konfigūraciją (mTLS prievadas, fabric galiniai
taškai, vardų sritis). Kiekviena Integration Settings kortelė turi *savo*
Save mygtuką, kuris išsaugo tik tos kortelės `.env` reikšmes ir
maršrutizatoriaus konfigūracijos neliečia. Paspaudus ne tą mygtuką,
neišsaugoma nieko — o jei tuo metu maršrutizatoriaus konfigūracija dar
negalioja (pvz., sertifikatai dar neįkelti), iškyla klaidinantis, nesusijęs
422/500.

**Sprendimas:** Rinkitės mygtuką pagal skyrių. Maršrutizatoriaus lygio
laukams po „Zenoh Config" (Transport, Fabric endpoints, Namespace) reikia
viršutinio „Save & Restart". Kiekvienam Integration Settings kortelės laukui
(TAK, SitaWare, jutiklių srautai ir t.t.) reikia tos pačios kortelės Save
mygtuko, esančio žemiau puslapyje.

### Kodo pataisymas negalioja, kol veikiantis procesas nepersileidžia

**Simptomas:** Ištaisote klaidą (dekoderyje, admin API, bet kur),
patvirtinate, kad failas diske pasikeitė, ir veikiančios sistemos elgsena
nesikeičia — arba WebUI toliau rodo paslaugas/duomenis, kurie ką tik
pašalinti iš kodo.

**Priežastis:** `.py` failo redagavimas jokios įtakos neturi jau veikiančiam
interpretatoriui, kuris seną baitkodą laiko atmintyje. Skamba akivaizdžiai,
bet lengva pamiršti tyrimo viduryje, kai iš eilės redaguojami keli failai ir
nebeaišku, *kuris* veikiantis procesas pasenęs.

**Sprendimas:** Po bet kokio ilgai veikiančios paslaugos kodo pataisymo
persileiskite tą konkretų procesą (neužtenka tik iš naujo sukompiliuoti ar
testuoti), prieš darydami išvadą, kad pataisymas neveikė, arba pranešdami,
kad simptomas dar neišspręstas.

---
