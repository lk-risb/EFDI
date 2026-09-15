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
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Konfigūruojama REST apklausa |
| `nffi` | `protocols/random/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Pilni XML dokumentai Zenoh temoje `…/raw/nffi/*` |
| `stanag` | `protocols/vendors/stanag/stanag.py --proto {4586,4607,4609,5516}` | `…/raw/stanag_4609/klv`, `…/air/stanag_4609/camera/unknown/uav`, STANAG 4586 takelių temos ir `…/{air,sea,land}/stanag_5516/c2/**` | Paleidiklis kiekvieną sukonfigūruotą `--proto` startuoja tiesiogiai |
| `sapient-raw`, `stanag4586-raw`, `stanag5516-raw` | `bridges/*_bridge.py` | `…/raw/<protocol>/<source>` | Neprivalomas lizdo (socket) priėmimas; atitinkamas protokolas veikia su `*_ZENOH_RAW=1` |
| `cap` | `protocols/random/cap.py` | `…/land/cap/c2/neutral/sensor/{type}/{id}/sapient` | Pilnas CAP 1.2 XML temoje `…/raw/cap/**` |
| `mqtt` | `protocols/random/mqtt_json.py` | `…/land/mqtt/iot/unknown/sensor/{type}/{id}/sapient` | Gamintojo JSON temoje `…/raw/mqtt/**` (tiltas persiunčia bet kokį turinį nepakeistą) |
| `sparkplug` | `protocols/vendors/sparkplug/sparkplug.py` | `…/land/sparkplug/iot/unknown/sensor/{type}/{id}/sapient` | Sparkplug B protobuf temoje `…/raw/mqtt/spBv1.0/**` |
| `sensor-health` / `mission-route` | Atitinkami `protocols/random/*.py` | `…/land/health/**`, `…/air/mission/**` | JSON jų pačių `…/raw/**` temose |
| `tak_layer` | `layers/tak_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `tak-bridge` | `bridges/tak_bridge.py` | Prenumeratorius — visos temos | TAK matomo CoT srauto priėmimas |
| `sitaware-hq-nvg` | `layers/sitaware_layer.py` | Prenumeratorius — visos takelių temos | HQ periodiškai ima NVG būseną |
| `track-fusion` | `protocols/fusion.py` | CAT-48 + CAT-21 prenumeratorius | Įvykio valdomas |

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

`mediamtx` (`bridges/mediamtx/mediamtx`, konfigūracija
`compose/bridges/mediamtx/mediamtx.yml`) yra atskiras vaizdo kelias — jis
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

**STANAG 4609 vaizdo srautas, ne tik drono RTMP.** `bridges/4609_bridge.py`
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
