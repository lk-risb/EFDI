# 06 — ATAK ir SitaWare sąranka

## ATAK sąranka

### TAK serveris

Nustatykite `TAK_HOST` ir `TAK_PORT` faile `.env`, tada paleidiklyje pasirinkite `tak-layer`. ATAK/WinTAK klientai takelius gauna tik per TAK serverį — tiesioginio multicast/unicast CoT kelio nėra.

#### TAK mTLS kliento kredencialai (įkėlimas per WebUI)

`tak_layer` jungiasi prie TAK serverio per mTLS, naudodamas kliento
kredencialus, sugeneruotus TAK repo su `make add-service NAME=<pod-vardas>`
(įrašo `certs/<pod-vardas>/{ca,cert,key}.pem`). WebUI Integration Settings →
**TAK and CoT** kortelė priima juos dviem būdais — naudokite vieną, ne abu:

- **A variantas — po vieną failą:** įkelkite `ca.pem`, `cert.pem` ir
  `key.pem` atskirai į tris atskirus laukus.
- **B variantas — vienas zip:** suspauskite visą `certs/<pod-vardas>/`
  katalogą kaip yra ir įkelkite jį vienu kartu; WebUI išpakuoja ir
  klasifikuoja kiekvieną PEM pagal turinį (ne tik failo vardą), todėl kitaip
  pavadintas failas zip archyve vis tiek atsidurs teisingoje vietoje, jei tai
  galiojantis CA/sertifikatas/raktas.

Aiškus vieno failo įkėlimas (A variantas) visada nustelbia tos pačios vietos
zip įrašą (B variantas), todėl galite ištaisyti tik vieną failą iš kitu
atveju gero zip, neįkeldami visko iš naujo.

Jei įkėlimas nepavyksta su teisių klaida, o ne patvirtinimo klaida, greičiausiai
šio įkėlimo katalogas hoste (`$POD_STATE_DIR/integrations/tak`) priklauso ne
tam naudotojui — žr.
[Problemų sprendimas → „Prijungtas (bind-mount) būsenos failas priklauso ne tam naudotojui..."](11-dazniausios-problemos.md).

### SitaWare HQ REST sekimas (pasirinktinis gaunamas adapteris)

`sitaware` naudokite tik tada, kai konkretaus diegimo dokumentacijoje nurodytas suderinamas JSON vienetų resursas ir autentifikavimo būdas. `/rest/v2/*` servlet'o maršrutas nereiškia, kad egzistuoja `/rest/v2/units`; patikrintame HQ 6.22 šis spėjamas resursas grąžina 404.

Palikite `SITAWARE_URL`/`SITAWARE_USER`/`SITAWARE_PASS` tuščius faile `.env` ir paleidiklis paklaus serverio adreso bei prisijungimo (vartotojo vardas, tada paslėptas slaptažodžio laukas) kaskart pasirinkus `sitaware` — arba užpildykite juos `.env` iš anksto, kad praleistumėte klausimą. (Antrą adresą vis tiek galima nustatyti per `SITAWARE_URL_FALLBACK` tiesiogiai `.env` faile, jei tikrai yra atskiras LAN/mesh kelias — interaktyvus klausimas paklaus tik vieno adreso.)

**`.env` laukai:**

```bash
SITAWARE_URL=https://<sitaware-serveris>
SITAWARE_URL_FALLBACK=https://sw.efdi.ltu/sw # neprivalomas stabilus mesh-DNS kelias
SITAWARE_USER=<vartotojo vardas>
SITAWARE_PASS=<slaptažodis>
SITAWARE_API_PATH=/<dokumentuotas-resurso-kelias>
SITAWARE_POLL_S=10   # neprivaloma — apklausos intervalas sekundėmis (numatytasis 10)
```

> SitaWare HQ 6.22 ir naujesnės versijos pasiekiamos pagal serverio vardą be
> aiškaus prievado — pati programa yra `https://<host>/sw` (Keycloak
> autentifikacija `https://<host>/auth/`), už jos stovi reverse proxy
> standartiniame HTTPS prievade. IP adresas veikia taip pat, jei dar
> neturite sukonfigūruoto serverio vardo. Neperkelkite senesnio diegimo
> `:<prievadas>` konvencijos.

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

> Du kiti šio Layer Details puslapio laukai veikia, kai takeliai jau teka:
> **Read Only**, jei pažymėtas, gali išjungti spustelėjimą-informacijai ant
> paties sluoksnio taškų (jei taškai rodomi, bet nereaguoja į paspaudimus,
> pirma pabandykite jį atžymėti); ir **Layer Expiration Period (Seconds)**
> veikia kartu su `SITAWARE_HQ_NVG_STALE_S` žemiau — jei takeliai vis dar
> sublyksi pakėlus srauto pačio „stale" ribą, pabandykite pakelti ir šį
> lauką (pradėkite nuo panašios reikšmės, pvz. `120`).

`compose/.env` nustatymai, arba WebUI Integration Settings → **SitaWare HQ**
kortelė (abu būdai įrašo tas pačias reikšmes):

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

> **Dvi WebUI formos spąstai konkrečiai:**
> 1. Keli laukai (prievadas, bind adresas, sertifikato/rakto keliai, „stale"
>    riba) rodo pilką **pavyzdinę** reikšmę, kol iš tikrųjų kažką neįrašote —
>    tas pilkas tekstas nėra išsaugota reikšmė. Jei paslauga vėliau praneša
>    „nenustatyta" laukui, kuris matomai rodė skaičių, įrašykite jį iš naujo
>    ir išsaugokite.
> 2. **„NVG feed bind address" yra grynas IP, nieko daugiau** — `0.0.0.0`
>    arba fiksuotas IP, niekada pilnas URL. Prievadas ir kelias yra atskiri
>    laukai; pridėjus `http://`, prievadą ar kelią prie bind-adreso lauko,
>    paslauga nulūš startuojant su `socket.gaierror`. Žr.
>    [Problemų sprendimas → „Formos laukas tyliai priima reikšmę, sudarytą visai kitokia forma..."](11-dazniausios-problemos.md),
>    jei su tuo susidūrėte.
>
> Dar neturite tikro TLS sertifikato? Nustatykite
> `SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP=1` ir palikite sertifikato/rakto
> laukus tuščius, kad tarnyba veiktų per paprastą HTTP — tik izoliuotame
> laboratoriniame tinkle, ir pakeiskite HQ prenumeratos Remote Endpoint į
> `http://` (ne `https://`), kad atitiktų.
>
> `SITAWARE_HQ_NVG_STALE_S` turėtų būti nustatyta bent **2× lėčiausio
> pirminio tilto atnaujinimo intervalo**, maitinančio šį sluoksnį (pvz. 120s,
> jei lėčiausias šaltinis atsinaujina kas 60s) — per trumpa, ir takeliai
> sublyksta bei vėl atsiranda kiekvieną HQ apklausą, nors šaltinis vis dar
> teikia duomenis. Žr.
> [Problemų sprendimas → „Takeliai sublyksi / dingsta ir vėl atsiranda fiksuotu ciklu"](11-dazniausios-problemos.md).

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

Adresas priima tik GET/HEAD, pagal nutylėjimą reikalauja Basic autentifikavimo — jei HQ prenumerata pasiekia srautą (nebe „connection refused"/timeout), bet srauto žurnale matote `rejected unauthorized request from <hq-ip>`, prenumeratos kredencialai neatitinka `SITAWARE_HQ_NVG_USER`/`_PASS`; ištaisykite juos ten, arba greitam izoliuoto laboratorinio tinklo testui nustatykite `SITAWARE_HQ_NVG_ALLOW_ANONYMOUS=1`. Riboja talpyklos dydį, pašalina ilgiau nei `SITAWARE_HQ_NVG_STALE_S` neatnaujintus takelius ir kiekvienam NVG objektui prideda tokios pačios trukmės `TimeSpan`, kad HQ paslėptų pasenusius objektus net nutrūkus srautui. Kai šaltinyje yra duomenų, standartiniai NVG modifikatoriai ir ribotas `ExtendedData` taip pat perduoda šaukinį, registraciją/ICAO, orlaivio ar laivo tipą, squawk, maršrutą, šaltinį, laivo ID bei sensoriaus tapatybę. Attributes kortelė naudoja tą patį domeno formatavimą kaip CoT/TAK, todėl rodomi tvarkingi skyriai, o ne neapdoroti Python laukų pavadinimai. Orlaiviams atskirai pateikiamas barometrinis ir geometrinis aukštis, pagrindinis aukštis metrais/pėdomis/skrydžio lygiu, kilimo ar leidimosi greitis, pasirinktas/tikslinis aukštis, greitis, kryptis, avarinė/autopiloto būsena ir ADS-B kokybės laukai. dronuradaras.lt aptikimai naudoja HQ palaikomą bendrą neutralaus įrangos sensoriaus simbolį, o orų stebėjimai — atskirą neutralaus stacionaraus sensoriaus simbolį, nes HQ 6.22 standartinius METOC simbolius rodo kaip nežinomus. Nei vienas jų neklasifikuojamas kaip karinės žvalgybos vienetas. Ne lokaliame adrese procesas atsisako startuoti per paprastą HTTP, nebent izoliuotai laboratorijai aiškiai nustatyta `SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP=1`. Nenaudokite Keycloak paskyros ar slaptažodžio šiam srautui.

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
