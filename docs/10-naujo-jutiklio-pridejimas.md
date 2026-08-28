# 10 — Naujo jutiklio ar protokolo pridėjimas

Tai žingsnis po žingsnio kelias nuo "turiu naują jutiklį/srautą" iki "jis
automatiškai atsiranda TAK ir SitaWare." Laikoma, kad podas jau įdiegtas ir
veikia (§§1-6 aukščiau).

Perskaitykite [§7 Integracijos](08-integracijos.md#integracijos) pirmiausia, jei dar
neskaitėte — ji paaiškina magistralę, į kurią jungiasi šis vadovas (temos
taksonomiją, keturias išvesties temas, kas jau sujungta). Šis skyrius yra
konkretūs "dabar sukurkite" žingsniai; tas skyrius — nuoroda, kas jau
egzistuoja.

## 10.0 Nuspręskite: tiltas ar protokolas?

- **`compose/bridges/`** — jūsų nauja integracija *jungiasi prie produkto ar
  paslaugos*: apklausia HTTP API, atidaro TCP lizdą į tiekėjo dėžę, klausosi
  UDP prievado konkrečiam įrenginiui. Po vieną failą kiekvienam išoriniam
  dalykui, su kuriuo ji kalbasi.
- **`compose/protocols/`** — jūsų nauja integracija *dekoduoja jau apibrėžtą*
  laidinį formatą, nesusietą su vienu tiekėju (standartą, specifikaciją,
  schemą).

Dauguma naujų jutiklių yra tiltai — fizinis ar tinklinis įrenginys, prie
kurio šis routeris jungiasi tiesiogiai. Jei abejojate, rinkitės `bridges/`;
tai dažnesnis atvejis ir niekam žemiau nesvarbu, kuriame kataloge gyvena
skriptas.

## 10.1 Ar reikia naujos pranešimo schemos, ar tinka esama?

Jei jūsų jutiklis praneša judantį objektą — poziciją, pasirinktinai
greitį/kursą/aukštį/identitetą — jis beveik tikrai tinka esamai bendrai
`NormalizedTrack` schemai (`../compose/protocols/proto/normalized_track.proto`)
ir jums **visai nereikia naujo protobuf darbo**. Praleiskite iki 2 žingsnio.

Naują `.proto` pranešimą apibrėžkite tik tada, kai jūsų duomenys turi
struktūrinius laukus, kurių `NormalizedTrack` iš tikrųjų negali išreikšti
(pvz., daugiataškė zona/plotas, arba domenui specifinė sudėtinė reikšmė).
Jei taip:

1. Pridėkite naują `.proto` failą po `compose/protocols/proto/` — kiekviena
   EFDI-autorystės schema gyvena ten, nepriklausomai nuo to, kuris vertėjas
   ją valdo (tikra vendoruota/licencijuota trečiosios šalies schema, kaip
   SAPIENT ar Sparkplug B laidiniai kontraktai, yra vienintelė išimtis ir
   lieka po `compose/protocols/vendors/<name>/` šalia savo LICENSE) —
   pagal esamą pavyzdį — `geojson_features.proto` yra trumpas pavyzdys.
2. Iš naujo sugeneruokite Python bindingus: `scripts/generate-protobuf.sh`
   (reikia `grpc_tools.protoc` + `protobuf` — jau `compose/requirements.txt`).
   Tai rašo į `compose/generated/`, kuris yra gitignored — kiekvienas
   kūrėjas/diegimas sugeneruoja jį vietiškai, niekas sugeneruotas
   necommitinamas.

## 10.2 Rašykite skriptą

Kiekvienas tilto/protokolo skriptas seka tą pačią formą. Tai pilna, veikianti
nuoroda — `compose/protocols/random/geojson_features.py` (127 eilutės) —
sutrumpinta iki dalių, kurios svarbu:

```python
from namespace_prefix import topic_root
from gateway import open_session, publish_dual
# Naudokite bendrą schemą — naujo .proto nereikia paprastam judančiam objektui:
from protocols.proto.normalized_track_pb2 import NormalizedTrack

TOPIC_ROOT = topic_root()
OUTPUT_TOPIC = TOPIC_ROOT + "/<domain>/<your-source-name>/<modality>/<affiliation>/<entity>"

def normalize(raw: dict) -> dict | None:
    """Paverskite vieną jūsų jutiklio įrašą į bendrą takelio formą.
    Privaloma: _ts (epoch sekundės), _src (jūsų šaltinio vardas), uid (stabilus per-objektą id).
    Viskas kita pasirinktinai — nustatykite tik tai, ką iš tikrųjų turite."""
    return {
        "_ts": time.time(),
        "_src": "your-sensor-name",
        "uid": "YOURSENSOR-" + raw["id"],
        "lat_deg": raw["lat"],
        "lon_deg": raw["lon"],
        # pasirinktinai: "speed_ms", "heading_deg", "baro_alt_m", "callsign", ...
    }

def run() -> None:
    session = open_session()
    for raw in your_data_source():          # apklauskite API, skaitykite lizdą ir t.t.
        record = normalize(raw)
        if record:
            publish_dual(session, OUTPUT_TOPIC, record, NormalizedTrack)
```

Jūsų skriptas pats niekada neimportuoja `zenoh` — `gateway.py` yra vienintelis
modulis, kuris tai daro. Jei reikia prenumeruoti neapdorotą įėjimo temą vietoj
apklausos, naudokite `gateway.subscribe(session, topic, callback)` tuo pačiu
būdu.

`publish_dual` atlieka likusią dalį: jis publikuoja visas keturias magistralės
temas (`/sapient`, `/json`, `/proto`, `/raw`) tuo vienu iškvietimu — žr.
[§7 Integracijos "Išvesties temos"](08-integracijos.md#išvesties-temos-sapient-json-proto-raw),
kam skirta kiekviena tema. Niekada nepublikuojate tiesiogiai į TAK ar
SitaWare — `tak_layer`/`sitaware_layer` automatiškai prenumeruoja kiekvieną
normalizuotą temą magistralėje, todėl teisingai publikuotas takelis atsiranda
abiejose be jokio papildomo kodo.

**Temos kelias.** Sekite taksonomiją iš [§7 Integracijos](08-integracijos.md):
`{domain}/{source}/{modality}/{affiliation}/{entity}` — pvz., `land` (arba
`air`/`sea`), jūsų jutiklio trumpas vardas, kokio tipo stebėsena tai yra,
`neutral`, jei neturite tikrų priklausomybės duomenų, ir koks objektas yra
(`vehicle`, `vessel`, `unit`, ...). Pažvelkite į keletą esamų temų
(`docs/13-topic-taxonomy.md`) dėl šablono prieš išrandant naują formą.

**Konfigūracija — nieko fiksuoto kode.** Bet koks serveris, prievadas, URL ar
kredencialas, kurio reikia jūsų skriptui, ateina iš aplinkos kintamojo,
niekada iš tiesioginio kodo įrašo (`compose/bridges/sitaware_bridge.py` yra
geras visiškai env-valdomo tilto pavyzdys). Pridėkite kiekvieną naują
kintamąjį į `compose/.env.example` su vienos eilutės komentaru,
paaiškinančiu, kam jis skirtas — tas failas yra vienintelis tiesos šaltinis,
ką galima sukonfigūruoti diegime, ir tai, ką kitas administratorius skaito,
norėdamas sužinoti, ką užpildyti.

**Patikrinkite, ar kompiliuojasi:**
```bash
python3 -m py_compile compose/bridges/your_new_bridge.py
```

## 10.3 Registruokite jį paleidiklyje

Keturi maži pakeitimai `start.sh`, sekant esamu `cap` įrašu kaip šablonu
(ieškokite `cap` `start.sh`, kad matytumėte visus keturis iš karto):

1. **`SERVICES` masyvas** — pridėkite savo paslaugos trumpą vardą į sąrašą.
2. **`SVC_CAT`** — po kokia kategorija ji rodoma meniu/WebUI
   (`"Sensor bridges"`, `"Protocols"`, ir t.t.).
3. **`SVC_DESC`** — vienos eilutės žmogui suprantamas aprašymas.
4. **`svc_ready()`** — kada saugu/prasminga ją paleisti? Jei nereikia jokios
   konfigūracijos, kad būtų naudinga, pridėkite savo vardą į `return 0`
   atvejį šalia `cap`/`mqtt`/ir t.t. Jei reikia env kintamojo iš pradžių
   (URL, serveris), remkitės tuo — pvz., `admin-control` sąlyga tikrina, ar
   nustatytas slaptas raktas; jūsų gali tikrinti
   `[[ -n "${YOUR_SENSOR_URL:-}" ]]`.
5. **Paleidimo atvejis** — pridėkite `_start your-service-name path/to/your_script.py`
   didžiajame `case` bloke, kuris iš tikrųjų paleidžia paslaugas.

## 10.4 Patikrinkite nuo galo iki galo

```bash
./start.sh --service your-service-name
```
Tada patvirtinkite, kad duomenys iš tikrųjų teka — prenumeruokite savo temą
bet kokiu Zenoh klientu (repo `clients/examples/` turi paruoštus prenumeravimo
skriptus) ir patvirtinkite, kad įrašai atvyksta. Jei TAK ar SitaWare išvestis
įjungta, atidarykite ATAK/WinTAK ar SitaWare žemėlapį ir patvirtinkite, kad
jūsų objektas atsiranda be jokios papildomos konfigūracijos — tai įrodymas,
kad magistralės sutartis buvo teisingai laikomasi.

## 10.5 Reikia naujo CoT simbolio? (tik TAK išvesčiai)

Jei jūsų jutiklio priklausomybės/objekto derinys dar neatvaizduoja į esamą
CoT tipą, pridėkite jį į `_TOPIC_COT` faile `compose/layers/tak_layer.py`:
```python
"air/**/hostile/uav/**":      ("a-h-A-M-F-Q", AIR_STALE_S),
"land/**/neutral/sensor/**":  ("a-n-G-E-S",   LAND_STALE_S * 2),
```
Raktas yra temos-priesagos šablonas; reikšmė — MIL-STD-2525C/APP-6 CoT tipo
kodas ir pasenimo langas. Dauguma naujų jutiklių jau atitinka esamą šabloną —
naują pridėkite tik tada, jei jūsų temos kelias iš tikrųjų nesutampa.

## 10.6 Dokumentuokite tai

Pridėkite eilutę į atitinkamą lentelę [§7 Integracijos](08-integracijos.md) (po "Šaltinio-specifiniai
tiltai" ar protokolo lentele), aprašančią, ko jai reikia sukonfigūruoti. Tai
yra tai, kas suteikia *kitam* administratoriui tą pačią vieno-skaitymo, be
spėliojimo patirtį, kurią suteikė šis dokumentas jums.

### Kontrolinis sąrašas prieš sakant, kad baigta

- [ ] Jokio fiksuoto serverio/prievado/URL/kredencialo skripte — viskas yra
      env kintamasis, dokumentuotas `compose/.env.example`.
- [ ] `python3 -m py_compile` praeina.
- [ ] Registruotas visose keturiose `start.sh` vietose (`SERVICES`, `SVC_CAT`,
      `SVC_DESC`, `svc_ready`) plius paleidimo atvejis.
- [ ] Patvirtinta tema magistralėje, tada patvirtinta TAK/SitaWare be jokių
      kodo pakeitimų nė vienam.
- [ ] Eilutė pridėta į [§7 Integracijos](08-integracijos.md).

---
