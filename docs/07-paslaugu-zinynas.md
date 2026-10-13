# 07 — Paslaugų žinynas

## Paslaugų žinynas

> **Temų lygiai.** Žemiau nurodytos `…/tracks/v1` temos yra JSON lygis. Kiekviena
> turi dvi protobuf temas su tuo pačiu įvykiu: `…/tracks/v2` (tipizuota žinutė iš
> protokolo `.proto`) ir `…/tracks/native/v1` (`RawEnvelope` su originaliais
> baitais, tiksliai baitas į baitą). Rinkitės `/v2`; `/native/v1` naudokite, kai
> reikia lauko, kurio EFDI nedekoduoja. `/v1` yra pasenęs ir bus pašalintas.
> Išsamiau: [Integracijos → Išvesties temos](08-integracijos.md#išvesties-temos-sapient-json-proto-raw).

| Paslauga | Scenarijus | Zenoh tema (sutrumpinta) | Suaktyvinimas |
| --- | --- | --- | --- |
| `asterix` | `protocols/vendors/asterix/cat.py` | `…/raw/asterix/catNN` ir kategorijai pritaikytos normalizuotos ASTERIX temos | ASTERIX gamintojo CAT protokolų rinkinys: bendras UDP srautas plius kategorijų vertėjai |
| `dronuradaras` | `bridges/vendors/mainline/dronuradaras_bridge.py` | `…/land/mainline_dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | Tik prisijungusių įrenginių apklausa 60 s su atsijungusių pašalinimu / aptikimų apklausa 10 s |
| `dangausakis` | `bridges/vendors/dangausakis/dangausakis_bridge.py` | `…/land/dangausakis/alert/neutral/zone/status` | dangausakis.lt regioninių įspėjimų (LT/LV/PL/EE/UA) apklausa kas 60 s; senesni nei 6 val. atmetami; Rusiją, Baltarusiją ir Moldovą piešia kaip jų regionus (geoBoundaries kontūrai, blankūs užpildai; `DANGAUSAKIS_REGION_SPLIT`) ir spalvina kiekvieno krašto regionus pagal tai, ar šalis kariauja: oranžinė – kariauja, balta – ne, geltona bet kuriame regione ne Ukrainoje ir ne Rusijoje, kuriame yra kas nors priešiško – taikos metu taip pat (Rusijos daliniai iš DeepState, dronai ir raketos iš MAPA ir NEPTUN, bendruomenės pranešimai apie dronus, Drone Strike Map fiksuoti dronų incidentai), o 2 ir 3 pavojaus lygiai – geltona ir raudona (svetainės oranžinis lygis piešiamas geltonai). Šalis laikoma kariaujančia pagal atvirus duomenis, o ne pagal nustatymą: svetainė ją piešia nuolatiniu tonu, ji turi raudonų įspėjimų bent trijuose regionuose vienu metu, Drone Strike Map praneša apie smūgius jai arba joje stovi Rusijos daliniai; statusas laikomas 24 val. ir išlieka po perkrovimo (`war-status.json`); aktyvių įspėjimų regionus piešia kaip raudonus/oranžinius/geltonus poligonus (LT/LV/PL/EE/UA, tik JSON zonų takeliai `…/land/dangausakis/airzone/…`); taip pat skelbia adsb.lol orlaivius kaip `…/air/dangausakis/{adsb,mlat}/friendly/aircraft` takelius (svetainės orlaivių sluoksnis yra sąjunginių / karinių orlaivių) pagal icao24; `tak_alert_layer` kiekvieną paverčia TAK GeoChat žinute; taip pat vykdo NEPTUN grėsmių srautą (`bridges/vendors/dangausakis/neptun.py`): gyvi Ukrainos dronai/raketos kaip priešiški takeliai `…/air/neptun/…`, apklausa kas 10 s, tik srities įrašai praleidžiami, žymė paslepiama, kai `track-fusion` ją priskiria netoliese esančiam nebendradarbiaujančiam radaro takeliui, matoma NEPTUN nuoroda pagal sąlygas; vietovių pavadinimai transliteruojami į anglų kalbą, pranešimas apie kelis dronus rodo jų skaičių, o grėsmė su pėdsaku TAK programoje gauna ir liniją (neseną kelią) už žymeklio UA įspėjimai imami tiesiai iš NEPTUN rajonų įspėjimų, LT – iš LT72 perspėjimų RSS (visos šalies, spalvinamos visos apskritys, vienas iššokantis langas); Lenkijos RSO pranešimai ir Latvijos 112.lv įspėjimų srautas tik pridedami prie svetainės įspėjimų ir tik tada, kai juose minimas oro pavojus; EE neturi mašininiu būdu nuskaitomo šaltinio, jis imamas iš svetainės srauto, o jei tiesioginis šaltinis neveikia, grįžtama prie jo. NEPTUN pavadinimai rodomi angliškai (transliteruoti), o NEPTUN įspėjimai neturi amžiaus ribos, todėl ilgai galiojantys, pvz., okupuoti regionai, lieka nubraižyti. Svetainės bendruomenės pranešimai apie dronus ir incidentų žymekliai skelbiami kaip takeliai (UAV ir antžeminė žymė, baigiasi kartu su pranešimu), o už jų braižomas blyškus užpildas kiekvienai sričiai su aktyviu rajono įspėjimu ir nuolatinės Baltarusijos (oranžinė) bei Rusijos (raudona) spalvos, kaip svetainėje, ir šalies kontūrą be užpildo virš regionų (`DANGAUSAKIS_BORDERS`: raudonas agresorėms Rusijai ir Baltarusijai, mėlynas Lietuvai, Latvijai, Estijai, Lenkijai ir Ukrainai; geoBoundaries ADM0, ODbL), ir tak_alert_layer siunčia GeoChat apie regionų būsenos pokyčius (pakelta/pakeista/atšaukta, viena žinutė šaliai per apklausą), apie neveikiantį ar sustingusį duomenų šaltinį (ir jo atsigavimą) bei apie naujus pranešimus apie dronus ir incidentus. |
| `deepstate` | `bridges/vendors/deepstate/deepstate_bridge.py` | `…/land/deepstate/frontline/hostile/unit/…` | Kas valandą atsisiunčia DeepStateMap (deepstatemap.live) žemėlapį, Ukrainos savanorių projektą, atnaujinamą maždaug kasdien; skelbia Rusijos/Baltarusijos dalinių ir atakos krypčių žymeklius kaip priešiškus antžeminius vienetus, siunčiamus iš naujo kas 240 s (jie galioja 600 s). Teritorijų daugiakampiai nebraižomi (jie nėra regionų pagrindu). Kontekstiniai duomenys, ne realiu laiku; nurodykite DeepState; jo naudojimo sąlygos nepatikrintos. `DEEPSTATE_POLL_S`, `DEEPSTATE_MARKERS` |
| `mapa` | `bridges/vendors/mapa/mapa_bridge.py` | `…/air/mapa/hostile/{uav,missile,bomb}/…`, `…/air/mapa/trail/line/…` | Kas 20 s atsisiunčia viešą MAPA.UA API (mapa.ua/api/v1/current), savanorių OSINT Ukrainos oro grėsmių pranešimų agregatorių, ir skelbia kiekvieną aktyvų droną, raketą ar valdomą bombą su kryptimi, greičiu, kilmės vieta, tikslo miestu ir trajektorijos linija; nebeaktyvūs objektai atšaukiami. Tai ne radaras ir ne oficialus perspėjimas; dubliuoja NEPTUN, todėl tas pats objektas gali rodytis du kartus, kol jie palyginami. Aplinkos kintamieji: `MAPA_*`. |
| `dronestrikemap` | `bridges/vendors/dronestrikemap/dronestrikemap_bridge.py` | `…/land/dronestrikemap/strike/{hostile,friendly}/incident/…`, `…/land/dronestrikemap/airzone/neutral/zone/…` | Kas 10 minučių klausia Drone Strike Map (dronestrikemap.com, CC BY 4.0: nurodykite šaltinį; pranešimai priklauso juos paskelbusioms redakcijoms) šiandienos ir vakar dienos pataikytų vietų bei oro pavojaus signalų ir skelbia po vieną sprogimo žymeklį kiekvienai pataikytai vietai per dieną (a-f-G-I-E / SFGPIE---- mėlyna vietai Rusijoje ar Rusijos okupuotoje Ukrainoje, t. y. Ukrainos smūgiui; a-h-G-I-E / SHGPIE---- raudona vietai Ukrainoje, t. y. Rusijos smūgiui – smogėjas nustatomas pagal tai, kur nukrito; pavadinime nurodytas patikimumas, pastabose – šaltiniai) ir Rusijos regionus, kurie yra daugiau nei „kare“: raudonas su oro pavojaus signalu, geltonas, jei šiandien ar vakar pataikyta į vietą; jie piešiami virš oranžinių regionų, kuriuos `dangausakis` piešia visai Rusijai (šalis, kurioje vyksta karas: niekur nėra saugu; kontūrai iš geoBoundaries, ODbL 1.0). Pozicija yra vietovė, o ne smūgio taškas. Neoficialu: Rusijos signalus teikia trečiosios šalies stebėsena. Aplinkos kintamieji: `DSM_*`. Taip pat skelbia šalis, kurioms fiksuoja smūgius (ir NATO šalis su neseniu smūgio incidentu), kad `dangausakis` jas nuspalvintų oranžine. |
| `geoconfirmed` | `bridges/vendors/geoconfirmed/geoconfirmed_bridge.py` | `…/land/geoconfirmed/event/{hostile,friendly,neutral,unknown}/incident/…` | Kas 15 minučių klausia GeoConfirmed (geoconfirmed.org, savanorių vykdoma konflikto vaizdo medžiagos geolokacija; licencijos nerasta, todėl kiekvienas žymeklis nurodo šaltinį) jų žemėlapio su If-None-Match (nepakitęs žemėlapis – vienas tuščias 304 atsakas) ir kartą per parą įvykių rūšių pavadinimų ir skelbia Ukrainos konflikto žymas už pastarąsias 3 dienas kaip 2525B incidento „Sprogimas“ žymeklius (a-h/f/n/u-G-I-E, SHGPIE---- ir pan.) to, kas smogė, spalva, kaip Drone Strike Map: apšaudymo, naudojamo dronų ar raketų ir nukautų civilių žymos spalva yra užpuoliko; sunaikintos ar apgadintos technikos ir nukentėjusių karių – savininko, todėl smūgis priklauso priešingai pusei; kitos žymos (važiuojantis tankas, belaisvis) išlaiko savo frakcijos pusę ir nurodo, kad tai ne smūgis; nežinomos kilmės sprogimas – neutralus. Pasikartojimus sujungia srautų sulieju (žr. eilutę `track-fusion`). Pavadinime – įvykio rūšis ir data, pozicija – geolokuota vieta. Priešiška žyma taip pat nuspalvina taikos šalies regioną geltonai. Aplinkos kintamieji: `GC_*`. |
| `sitaware` | `bridges/vendors/sitaware/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Konfigūruojama REST apklausa |
| `nffi` | `protocols/vendors/nffi/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Pilni XML dokumentai Zenoh temoje `…/raw/nffi/*` |
| `stanag` | `protocols/vendors/stanag/stanag.py --proto {4586,4607,4609,5516}` | `…/raw/stanag_4609/klv`, `…/air/stanag_4609/camera/unknown/uav`, STANAG 4586 takelių temos ir `…/{air,sea,land}/stanag_5516/c2/**` | Paleidiklis kiekvieną sukonfigūruotą `--proto` startuoja tiesiogiai |
| `sapient-raw`, `stanag4586-raw`, `stanag5516-raw` | `bridges/vendors/{sapient,milspec}/*_bridge.py` | `…/raw/<protocol>/<source>` | Neprivalomas lizdo (socket) priėmimas; atitinkamas protokolas veikia su `*_ZENOH_RAW=1` |
| `cap` | `protocols/vendors/random/cap.py` | `…/land/cap/c2/neutral/sensor/{type}/{id}/sapient` | Pilnas CAP 1.2 XML temoje `…/raw/cap/**` |
| `mqtt` | `protocols/vendors/random/mqtt_json.py` | `…/land/mqtt/iot/unknown/sensor/{type}/{id}/sapient` | Gamintojo JSON temoje `…/raw/mqtt/**` (tiltas persiunčia bet kokį turinį nepakeistą) |
| `sparkplug` | `protocols/vendors/sparkplug/sparkplug.py` | `…/land/sparkplug/iot/unknown/sensor/{type}/{id}/sapient` | Sparkplug B protobuf temoje `…/raw/mqtt/spBv1.0/**` |
| `sensor-health` / `mission-route` | Atitinkami `protocols/vendors/random/*.py` | `…/land/health/**`, `…/air/mission/**` | JSON jų pačių `…/raw/**` temose |
| `tak_layer` | `layers/vendors/tak/tak_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `tak-bridge` | `bridges/vendors/tak/tak_bridge.py` | Prenumeratorius — visos temos | TAK matomo CoT srauto priėmimas |
| `sitaware-hq-nvg` | `layers/vendors/tak/systematic/sitaware_layer.py` | Prenumeratorius — visos takelių temos | HQ periodiškai ima NVG būseną |
| `track-fusion` | `protocols/fusion.py` | CAT-48 + CAT-21 prenumeratorius; sujungia Drone Strike Map ir GeoConfirmed smūgių žymeklius, apie tą patį išpuolį (ta pati pusė, ta pati diena, per `FUSION_STRIKE_M` = 100 m; paliekama tikslesnė pozicija, kitos atšaukiamos žyme „ištrinta“) | Įvykio valdomas |

### TAK naudotojai ir išoriniai CoT šaltiniai

### Zenoh-native neapdorotas (raw) priėmimas

Jei priėmimo mazgas turi pats valdyti tinklo lizdą, pasirinkite atitinkamą
`*-raw` tiltą ir nustatykite jo neapdoroto srauto prievadą. Protokolo vertėją
įjunkite atskirai su jo `*_ZENOH_RAW=1` nustatymu.

Neapdoroto srauto tiltas skelbia tik baitus — jis jų neklasifikuoja ir
nekeičia. SAPIENT/FLEX 335 ir STANAG 4586 vertėjai skaito tas Zenoh temas ir
skelbia normalizuotą JSON. SAPIENT priėmimas naudoja viešą BSI Flex 335 v2
protobuf kontraktą. Išlaikytas STANAG 4586 dvejetainis formatas yra istorinio
diegimo aproksimacija, o ne bendras standarto profilis: jis lieka išjungtas,
kol aiškiai nenustatoma `STANAG4586_PROFILE=legacy_ed3_approx`, patvirtinus
formatą pagal diegiamo VSM ICD.

CAP, sveikatos (`sensor-health`) ir maršrutų (`mission-route`) vertėjai yra
neaktyvūs saugūs Zenoh prenumeratoriai. Partneris paskelbia pilną JSON/XML/NMEA
turinį po atitinkama `raw/**` tema; vertėjyje neįterpta jokio interneto adreso
ar imtuvo.

`mqtt` yra bendrinis MQTT jutiklio JSON vertėjas, pakartotinai naudojamas
bet kokiam MQTT formos srautui, kuris neturi savo vardinės gamintojo
integracijos — pavyzdžiui, dronų aptikimo JSON srautas su
`latitude`/`longitude`/`altitude`/`heading` laukais tinka tiesiogiai:
nukreipkite srautą į `mqtt` vertėjo įėjimo temą (arba perrašykite
`MQTT_INPUT_TOPIC`) ir jokio naujo kodo rašyti nereikia.

CoT ir SitaWare HQ NVG išvestys naudoja tą pačią scenarijaus priklausomybės
taisyklę: orlaiviai iš nustatytų RU/BY ICAO adresų intervalų bei laivai su RU/BY
MMSI MID žymimi kaip priešiški, o kiti partnerių oro/jūros kontaktai — neutralūs.
Vien šalies pavadinimas nepakeičia trūkstamo arba negaliojančio atsakiklio ID.

`tak-bridge` yra atvirkštinis CoT kelias: jis prisijungia prie TAK matomo CoT
srauto per dokumentuotą TCP/TLS sesiją, išskiria pilnus `<event>...</event>`
kadrus ir persiskelbia normalizuotą JSON į Zenoh. Jis nepakeičia CoT išvesties
sluoksnio ir nenaudoja Zenoh kaip TAK ryšio transporto.

### Vaizdo srautas (mediamtx)

`mediamtx` (`bridges/vendors/mediamtx/mediamtx`, konfigūracija
`compose/bridges/vendors/mediamtx/mediamtx.yml`) yra atskiras vaizdo kelias — jis
visiškai nesiliečia su Zenoh ar temomis. Jis priima vieno drono vaizdo srautą
ir tą patį srautą paskirsto TAK, SitaWare bei WebUI "Streams" skirtukui.
Šiuo metu `mediamtx.yml` įjungta:

| Kryptis | Protokolas | Prievadas | Naudojimas |
| --- | --- | --- | --- |
| Įėjimas (siunčiama į) | RTMP | `1935` | FreeFlight srauto adresas |
| Išėjimas (traukiama iš) | RTSP (tik TCP — žr. [11 — Trikčių šalinimas](11-dazniausios-problemos.md)) | `8554` | TAK, SitaWare |
| Išėjimas (traukiama iš) | WebRTC (WHEP) | `8889` | WebUI "Streams" skirtukas, gyvi langeliai |
| Įrašymas | fMP4 segmentai, po 1 min., paskutinių 10 min. langas | — | WebUI "Streams" skirtukas, peržiūros slankiklis |

mediamtx taip pat palaiko HLS ir SRT (abiem kryptimis) — nei vienas dar
neįjungtas. SRT verta įjungti kaip alternatyvų įėjimo kelią, jei dronas ar
antžeminė stotis kada nors siūlys jį vietoje RTMP: skirtingai nuo RTMP
(paprastas TCP), SRT turi įmontuotą prarastų paketų persiuntimą (ARQ),
sukurtą būtent tokiam nutrūkinėjančiam ryšiui, koks jau sukelia RTMP per
persiunčiamą (relayed) NetBird jungtį problemą, aprašytą trikčių šalinimo
dokumente. Nereikalinga, kol koks nors realus šaltinis to tikrai paprašys.

**STANAG 4609 vaizdo srautas, ne tik drono RTMP.** `bridges/vendors/milspec/4609_bridge.py`
valdo SRT ryšį, nešantį STANAG 4609 MPEG-TS+KLV, kaip jo *klausytojas*
(sensorius/antžeminė stotis prisijungia į jį) tik tam, kad išgautų KLV
metaduomenis — mediamtx negali prisirišti prie to paties prievado, o
srauto nukreipimas per mediamtx pirmiau taip pat netinka: MediaMTX šiuo
metu visiškai nepalaiko MPEG-TS KLV/duomenų srauto perdavimo (atviras,
dar neįgyvendintas prašymas jų projekte), todėl metaduomenys būtų tyliai
prarasti dar prieš pasiekiant šį vertėją. Nustatę
`STANAG4609_VIDEO_RELAY_ENABLE=1` (numatytai išjungta) taip pat
persiunčiate vaizdo/garso srautą į mediamtx iš to paties jau atidaryto
ryšio — ffmpeg savo `tee` mikšeriu padalija vieną demultipleksavimą į KLV
išvestį (nepakitusią) ir geriausių pastangų perkodavimą be transkodavimo
(`-c copy`) į mediamtx RTMP įėjimą, pažymėtą `onfail=ignore`, todėl
neveikiantis ar iš naujo paleidžiamas mediamtx niekada neveikia KLV
išgavimo. Persiuntus, srautas atsiranda WebUI "Streams" skirtuke ir
pasiekia TAK/SitaWare lygiai taip pat, kaip drono srautas — kelio
pavadinimą žr. `STANAG4609_VIDEO_PATH` faile `.env.example`.
