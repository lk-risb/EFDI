# 09 — C2 ↔ Zenoh abikryptė prijungimo instrukcija

Kryptys yra nepriklausomos. Užbaikite tik tuos kelius, kurie atskleisti ir
licencijuoti konkretaus diegimo, tada pasirinkite jų paslaugas `./start.sh`.

### 9.1 Patikrinkite bendrą Zenoh pusę

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

### 9.2 Zenoh → TAK Server

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

### 9.3 TAK Server → Zenoh

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

### 9.4 Zenoh → SitaWare HQ

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

### 9.5 SitaWare HQ → Zenoh

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

### 9.6 Dalinkitės C2 kilmės duomenimis su partneriais

Nerašykite įrašo iš naujo į kito partnerio vardų sritį. Patvirtinkite, kad
kilmės vardų sritis leidžiama routerio/federacijos politikos ir kad gaunantis
partneris ją prenumeruoja. Jų `cot-*` ar `sitaware-hq-nvg` išvesties
sluoksniai išvers autorizuotas normalizuotas temas taip pat, kaip vietiniai
sugeneruoti jutiklio duomenys.

### 9.7 Eksploatacinio personažo testinis pratimas

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

### 9.8 Zenoh temos schema

```text
{NAMESPACE}/{DOMAIN}/{SOURCE}/{MODALITY}/{AFFILIATION}/{ENTITY}/{TYPE}/{ID}/{VIEW}
```

| Laukas | Reikšmės |
| --- | --- |
| `DOMAIN` | `air`, `land`, `sea`, `space`, `env` |
| `AFFILIATION` | `friendly`, `hostile`, `neutral`, `unknown`, `civ`, `mil` |
| `TYPE` | `aircraft`, `vessel`, `vehicle`, `unit`, `sensor`, `uav`, `radar` |

---

