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

Jei `compose/.env` buvo įkeltas paprastu `source compose/.env`, kintamieji
neeksportuojami vaikiniams procesams. Naudokite `./start.sh` (kuris tai
sutvarko), arba:

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

Radaras nesiuntė CAT-34 I034/120 (3D-Position), todėl EFDI negali saugiai
nustatyti vietos. Patikrinkite CAT-34 žurnalą dėl `has no site position`.
Pirmenybę teikite I034/120 įjungimui radare/šliuze; tik vienam radarui
nustatykite atsargines koordinates `.env`:

```bash
grep CAT34_RADAR compose/.env
```

### Drono aptikimai nepublikuojami

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

SitaWare vienetai be galiojančio 15-ženklio SIDC nukreipiami į
`…/land/sitaware/c2/unknown/unit/…` ir vaizduojami kaip nežinomi antžeminiai
vienetai (`a-u-G-U-C`). Patikrinkite žalią SIDC reikšmę žurnale:

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

### Dubliuoti proceso egzemplioriai

Sukelia dukart paleistas `start.sh` be sustabdymo:

```bash
pkill -f "_bridge\.py\|tak_layer\|track_fusion"
rm -f $POD_STATE_DIR/.pids/*.pid
./start.sh
```

### Radaro ikona dingsta iš ATAK

`asterix` tiltas publikuoja keepalive kas 60 s nepriklausomai nuo takelio
aktyvumo. Jei ikona dingsta, tiltas sustojo:

```bash
tail -20 $POD_STATE_DIR/logs/asterix.log | grep -E "keepalive|startup|error"
```

## 11.2 Pastebėti dalykai — jau apmokėtos pamokos

Tai *eksploatacinis/infrastruktūrinis* palydovas
[`../.ai/.claude/CLAUDE.md`](../.ai/.claude/CLAUDE.md) ASTERIX bitų lygio
dekodavimo pastebėtiems dalykams ir [§11.1 Simptomais pagrįstiems sprendimams](#111-simptomais-pagrįsti-sprendimai).
Kiekvienas čia esantis dalykas buvo tikra, patvirtinta problema, su kuria
susidurta valdant šį podą — perskaitykite prieš derindami kažką, kas panašu
į vieną iš šių simptomų, kad tos pačios diagnozės nereikėtų pelnyti iš naujo.

### NetBird split-DNS nematomas konteinerių viduje

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

### TLS/mTLS identiteto profilis turi atitikti galinį tašką, kurį jis rinkis

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

### Vieno failo bind-mount sulaužo atominius rašymus

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

### Identiškai pavadintos dubliuotos funkcijų apibrėžtys tyliai užstoja

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

### Paslaugos rinkiniui reikia savos būsenos agregacijos

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

### Prijungtas (bind-mount) būsenos failas priklauso ne tam naudotojui, ne tik blogai prijungtas

**Simptomas:** Konfigūracijos išsaugojimas per WebUI nepavyksta su
`[Errno 13] Permission denied: '/data-topic-prefix'` (arba
`/namespace-prefix`, arba TAK/SitaWare kredencialų įkėlimo katalogais) — tai
kitokia klaida nei aukščiau aprašytas `EBUSY` atominio rašymo atvejis; čia
tiesiog teisių klaida, o ne pervadinimo per prijungimo tašką klaida.

**Priežastis:** `zenoh-admin` visada veikia fiksuotu ne-root uid/gid
(`10001`). Keli būsenos keliai yra atskirai per bind-mount prijungti failai ar
katalogai (`namespace-prefix`, `data-topic-prefix`, `integrations/tak`,
`$BUNDLE_DIR/efdi`), sukurti **hosto** pusėje to naudotojo, kuris paleido
`install.sh`/`reinstall.sh` — dažniausiai root. Root sukurtas failas su
teisėmis `644` yra rašomas tik savininko (root); uid 10001 neturi nei
savininko bito, nei (nebent grupė jau būtų 10001) grupės rašymo bito, todėl
kiekvienas rašymas iš konteinerio vidaus nepavyksta.

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

`health.sh` interaktyvus meniu (3 punktas, „patikrinti trūkstamus/blogai
sukonfigūruotus būsenos failus") dabar taip pat aptinka ir automatiškai
ištaiso neteisingas šių kelių teises, ne tik trūkstamus failus.

### Neapdorota išimtis API apdorojime pasirodo kaip tuščias HTTP 500

**Simptomas:** WebUI rodo paprastą pranešimą „Unexpected error applying
config" arba „Request failed (HTTP 422)" be jokios papildomos informacijos —
jokios užuominos, kas iš tikrųjų nutrūko, nors serveris *iš tikrųjų* susidūrė
su konkrečia klaida.

**Priežastis:** Apdorojimo funkcija, kuri sugauna `Exception` tik tam, kad
ją užregistruotų, o po to tuščiai `raise` ją perduoda toliau, praranda
originalų pranešimą, kai perima FastAPI numatytasis klaidų apdorojimas —
klientas mato tik bendrą būsenos kodą. Jei pati pagrindinė klaida grąžino
tuščią eilutę kaip savo „detail" (pvz., subprocesas, kuris nulūžo dar
nespėjęs nieko parašyti į stdout/stderr), net teisingai perduota išimtis
neturi ką parodyti.

**Sprendimas:** Neapdorotą išimtį paverskite `HTTPException` su realiu
pranešimu (`raise HTTPException(500, detail=f"...: {exc}") from exc`), o bet
kokį kelią, pranešantį apie subproceso/patikros nesėkmę, priverskite grįžti
prie aprašomojo pakaitalo (`"exited N with no output"`) vietoj tuščios
eilutės — kad *kitas* pasikartojimas būtų diagnozuojamas vien iš atsakymo
turinio, be prieigos prie serverio žurnalo.

### Formos laukas tyliai priima reikšmę, sudarytą visai kitokia forma nei reikia

**Simptomas:** Laukui „bind adresas" arba panašiam vienos paskirties
nustatymui pateikiamas pilnas URL (`http://0.0.0.0:8088/nvg`) arba ne to
kompiuterio IP adresas vietoj gryno vietinio IP, o paslauga, kuri jį skaito,
nulūžta su kažkuo nesuprantamu kaip `socket.gaierror: [Errno -2] Name or
service not known` ties `socket.bind()`.

**Priežastis:** Bind adresas perduodamas tiesiai į `socket.bind((host,
port))` — tai niekada nėra URL (nei schemos, nei prievado, nei kelio), ir tai
yra *šio paties kompiuterio* klausymosi adresas, o ne kompiuterio, kuris prie
jo jungsis, adresas. Paprastas teksto laukas nesustabdo naudotojo įvedus
pilną URL arba ne tos mašinos adresą; klaida pasirodo tik toliau, bibliotekos
viduje, per kelis sluoksnius nuo lauko, kuris ją sukėlė.

**Sprendimas:** Konkrečiai `SITAWARE_HQ_NVG_BIND` reikšmė turi būti grynas
IP — `0.0.0.0`, kad klausytųsi visų sąsajų (kad *kita* mašina, pvz., SitaWare
HQ dėžė, galėtų pasiekti), arba `127.0.0.1` tik vietiniam ryšiui. Prievadas ir
kelias yra atskiri laukai — jų čia neįtraukite. Bendrai: kai laukas nulūžta
toli nuo vietos, kur jis nustatytas, pirmiausia patikrinkite jo saugomą
reikšmę (`grep KEY .env`), o tik tada gilinkitės į nulūžimo vietą.

### Naujai veikiantis srautas vis tiek atmetamas — pirma patikrinkite autentifikaciją, ne maršrutą

**Simptomas:** Nuotolinė sistema jau gali pasiekti srautą (nebe „connection
refused"/timeout), bet kiekviena užklausa vis tiek atmetama, o paties srauto
žurnale rašoma kažkas panašaus į `rejected unauthorized request from <ip>`.

**Priežastis:** Ryšio pasiekiamumas (teisingas prievadas, teisingas bind
adresas) yra atskira problema nuo autentifikacijos. Srautas su sukonfigūruota
Basic autentifikacija (`SITAWARE_HQ_NVG_USER`/`_PASS`) atmeta kiekvieną
užklausą, kuri nepateikia atitinkančių kredencialų — net iš kliento, kuris
kitu atveju yra puikiai pasiekiamas.

**Sprendimas:** Arba sukonfigūruokite *nuotolinę* pusę (šiuo atveju —
SitaWare HQ importo prenumeratą) su tuo pačiu naudotojo vardu/slaptažodžiu
kaip ir srautas, arba — tik greitam izoliuoto laboratorinio tinklo
testui — nustatykite `SITAWARE_HQ_NVG_ALLOW_ANONYMOUS=1`, kad visiškai
praleistumėte autentifikaciją, kol patvirtinsite, jog duomenys teka.

### Takeliai sublyksi / dingsta ir vėl atsiranda fiksuotu ciklu

**Simptomas:** Objektai apklausa pagrįstame sraute (pvz., SitaWare NVG
importe) sublyksi ir atsiranda periodu, sutampančiu su apklausos intervalu,
nors pirminis šaltinis iš tikrųjų vis dar teikia duomenis.

**Priežastis:** Srauto talpykla pašalina bet kokį objektą, kuris nebuvo
atnaujintas per jos „stale" (paseno) langą (`SITAWARE_HQ_NVG_STALE_S`, arba
atitinkamas nustatymas bet kuriame kitame apklausa pagrįstame sluoksnyje).
Jei *pirminis* tiltas atnaujina konkretų objektą tik kas N sekundžių
(pavyzdžiui, `dronuradaras_bridge.py` turi `DEVICE_POLL_S = 60` radaro mazgų
pozicijoms), o „stale" langas trumpesnis už tai, kiekvienas objektas
pasensta ir dingsta daliai kiekvieno pirminio ciklo, tada vėl atsiranda,
kai ateina kitas atnaujinimas — tai slenkantis, nevienalaikis mirgėjimas, o
ne švarus, vienalaikis.

**Sprendimas:** Nustatykite srauto „stale" ribą gerokai virš lėčiausio
pirminio atnaujinimo intervalo, kuris jį maitina (bent 2×) — pvz., `120`
60 sekundžių pirminiam ciklui. Atskirai, žemesnės grandies C2 sistemos
pačios „sluoksnio galiojimo"/„takelio išsaugojimo" nuostata (SitaWare Layer
Details puslapyje yra abi) gali tai sustiprinti arba užmaskuoti; jei vien
„stale" ribos pakėlimas neišsprendžia, patikrinkite ir tą nuostatą.

### `pip install` nepavyksta su „externally-managed-environment"

**Simptomas:** Paleidus `pip install -r requirements.txt` tiesiai prieš
sisteminį `python3` (o ne per `install.sh`/`start.sh`), nepavyksta su
`error: externally-managed-environment` / „This environment is externally
managed" (PEP 668, dažna šiuolaikiniuose Debian/Ubuntu).

**Priežastis:** Sisteminis Python tyčia užrakintas nuo nevaldomų `pip
install`. `install.sh` ir `start.sh` su tuo nesikovoja — jie sukuria ir
naudoja savo virtualią aplinką `compose/venv`, iš kurios iš tikrųjų veikia
kiekviena šio pod'o Python paslauga, paleidžiama hoste.

**Sprendimas:** Naudokite tą virtualią aplinką tiesiogiai, ne sisteminį
interpretatorių:

```bash
compose/venv/bin/pip install -r compose/requirements.txt
compose/venv/bin/python3 layers/some_layer.py
```

Jei `compose/venv` dar neegzistuoja, sukurkite ją taip pat, kaip tai daro
`install.sh`: `python3 -m venv compose/venv`, tada diekite į ją. Niekada
neperduokite `--break-system-packages` sisteminiam `pip` — kiekviena kita
hoste veikianti paslauga jau tikisi virtualios aplinkos, ne sisteminio
interpretatoriaus.

### Du skirtingi „Save" mygtukai tame pačiame puslapyje daro skirtingus dalykus

**Simptomas:** Reikšmės išsaugojimas viename WebUI konfigūracijos puslapio
skyriuje (pvz., Integration Settings lauke, tokiame kaip SitaWare ar TAK
nustatymas) atrodo, tarsi nieko nedaro, arba sukelia nesusijusią klaidą
(Zenoh maršrutizatoriaus konfigūracijos patvirtinimo klaidą), kuri neturi
nieko bendra su tuo, kas iš tikrųjų buvo keičiama.

**Priežastis:** Zenoh Config puslapyje yra dvi nepriklausomos išsaugojimo
funkcijos: viršuje esantis **„Save & Restart"**, kuris patvirtina ir
pritaiko *Zenoh maršrutizatoriaus* konfigūraciją (mTLS prievadas, fabric
galiniai taškai, vardų sritis), ir kiekvienos Integration Settings kortelės
*savas* Save mygtukas, kuris išsaugo tik tos kortelės `.env` reikšmes ir
niekada neliečia maršrutizatoriaus konfigūracijos. Paspaudus ne tą, kas
aktualu, neišsaugoma nieko, o jei maršrutizatoriaus konfigūracija tuo metu
dar nėra galiojanti (pvz., sertifikatai dar neįkelti) — iškyla klaidinantis,
nesusijęs 422/500.

**Sprendimas:** Rinkitės mygtuką pagal skyrių: maršrutizatoriaus lygio
laukams po „Zenoh Config" (Transport, Fabric endpoints, Namespace) reikia
viršutinio „Save & Restart"; kiekvienam laukui Integration Settings kortelėje
(TAK, SitaWare, jutiklių srautai ir t.t.) reikia tos kortelės pačios Save
mygtuko, esančio žemiau puslapyje.

### Kodo pataisymas negalioja, kol veikiantis procesas nepersileidžia

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
