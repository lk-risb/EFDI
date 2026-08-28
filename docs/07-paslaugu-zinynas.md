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
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | Tik prisijungusių įrenginių apklausa 60 s su atsijungusių pašalinimu / aptikimų apklausa 10 s |
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
