# 02 — Repozitorijos struktūra

Šis skyrius atsako į vieną klausimą: kur ieškoti, jei reikia ką nors rasti
repozitorijoje. Prieš leidžiantis į aklą `grep` paiešką, verta pirma
peržvelgti šį žemėlapį — dažniausiai failas yra būtent ten, kur pagal
paskirtį ir turėtų būti.

## Viršutinis lygis

| Kelias | Kas tai |
| --- | --- |
| `install.sh` | Diegyklė — nuo tuščio serverio iki veikiančio pod'o viena komanda. Žr. [03](03-diegimas-ir-paruosimas.md). |
| `start.sh` | Interaktyvus paslaugų paleidiklis — leidžia pasirinkti, kurie tiltai/sluoksniai veiks, sutvarko prievadus ir juos paleidžia. |
| `stop.sh` | Sustabdo viską, ką paleido `start.sh` arba `run.sh`. |
| `run.sh` | Paleidžia EFDI tiltus kaip foninius procesus be Docker (pats Zenoh maršrutizatorius vis tiek lieka Docker konteineryje). |
| `update.sh` | Greitas atnaujinimas su talpyklos patikra ir automatiniu atsigavimu, jei kas nepavyksta. |
| `reinstall.sh` | Pašalina vietinius atvaizdus ir konteinerius, bet palieka sertifikatus bei duomenis nepaliestus — švarus perstatymas be tapatybės praradimo. |
| `health.sh` | Pirmiausia bando savaime pataisyti diegimą, tada paleidžia visą repozitorijos testų ir statinių patikrų rinkinį. |
| `dev.sh` | Vienkartinė vietinė PostgreSQL aplinka zenoh-admin skydo pakeitimams peržiūrėti — pakelia tik administravimo skydą, be viso fabric. |
| `compose/` | Pats pod'as: tiltai, sluoksniai, protokolų vertėjai, administravimo skydas, Docker Compose apibrėžimai. |
| `clients/` | Prisijungimo SDK ir pavyzdžiai, kuriais remiasi partneriai. |
| `examples/` | Savarankiški vykdomi pavyzdžiai (pirmas leidėjas/prenumeratorius, gyvybingumo patikra, atsparus prenumeratorius ir t.t.) bei `first-boot.sh`. |
| `host/` | Hosto lygio Zenoh maršrutizatoriaus konfigūracijos šablonas. |
| `scripts/` | Vienkartiniai eksploataciniai scenarijai: sertifikatų generavimas, protobuf kodo generavimas, radaro UDP fiksavimas/persiuntimas, hosto aptikimas. |
| `tools/` | ASTERIX zondavimo ir persiuntimo įrankiai gyvo srauto derinimui. |
| `tests/` | Pilnas testų rinkinys — vieneto testai, dūmų testai, saugumo patikros, CI atvaizdų-viešumo patikros. |
| `docs/` | Viskas, ką dabar skaitote; pilną žemėlapį rasite [00](00-pradekite-cia.md). |

## `compose/` — pats pod'as

| Kelias | Kas tai |
| --- | --- |
| `docker-compose.yml` | Zenoh maršrutizatorius ir zenoh-admin konteineriai — vieninteliai du komponentai, kurie iš viso veikia Docker'yje. |
| `.env.example` | Konfigūracijos šablonas. Tikras `.env` yra `.gitignore` sąraše ir jame gyvena kiekvieno konkretaus diegimo paslaptys bei tapatybė. |
| `certs/` *(gitignore)* | Maršrutizatoriaus mTLS sertifikatai. |
| `state/` *(gitignore)* | Vykdymo būsena — žurnalai, PID failai, sugeneruota Zenoh maršrutizatoriaus konfigūracija. |
| `venv/` *(gitignore)* | Python virtuali aplinka, kurią `start.sh`/`install.sh` susikuria pirmo paleidimo metu. |
| `generated/` *(gitignore)* | Sukompiliuoti protobuf ryšiai (`*_pb2.py`) — `scripts/generate-protobuf.sh` sugeneruota išvestis, niekada necommitinama. |
| `control/` | Bendri pagalbininkai ir hosto valdymo plokštuma: `supervisor.py` palaiko natyvius procesus gyvus, `admin_control.py` valdo administravimo API logiką, o šalia jų — `gateway.py`, `namespace_prefix.py`, `zenoh_auth.py`, `presence.py`, `http_json.py`. |
| `bridges/` | Įėjimas: kiekvienas failas čia traukia duomenis iš vieno šaltinio (jutiklio srauto, C2 sistemos, bendrinio protokolo) į fabric. Pavadinimo konvenciją žr. žemiau. |
| `layers/` | Išėjimas: `tak_layer.py` ir `sitaware_layer.py` siunčia sulietus fabric duomenis atgal į C2 sistemą. |
| `protocols/` | Protokolų vertėjai ir jų `.proto` sutartys — dekodavimo/kodavimo logika, kuri pati savaime nuo Zenoh nepriklauso (žr. [08](08-integracijos.md)). |
| `zenoh-admin/` | FastAPI + React administravimo skydas: `api/` (backend), `ui/` (frontend), su savu `Dockerfile` ir `Caddyfile`. |

### Kaip pavadinami `compose/bridges/` ir `compose/layers/` failai

Priešdėlis scenarijaus pavadinime nurodo *kryptį*, o ne tai, kuris
gamintojas ar sistema atidaro tinklo ryšį. **`_bridge`** reiškia, kad
išorinė sistema įnešama **į** fabric; **`_layer`** reiškia, kad fabric
duomenys išvedami **į** C2 sistemą. Todėl `sitaware_layer`, kurio srautą
apklausia pats SitaWare HQ, vis tiek lieka išėjimu ir vadinasi `_layer` —
nesvarbu, kad HTTP užklausą inicijuoja HQ pusė, o ne EFDI.

| Scenarijus | Kryptis |
| --- | --- |
| `tak_bridge.py`, `sitaware_bridge.py` | C2 → Zenoh |
| `tak_layer.py`, `sitaware_layer.py` | Zenoh → C2 |
| `asterix_bridge.py`, `dronuradaras_bridge.py`, `flex335_bridge.py`, `udp_ingress_bridge.py`, `mqtt_bridge.py`, `raw_socket_bridge.py`, `meteolt_forecast_bridge.py`, `4586_bridge.py`, `4609_bridge.py`, `5516_bridge.py` | Šaltinis → Zenoh |

### Kaip išdėstytas `compose/protocols/`

| Kelias | Kas tai |
| --- | --- |
| `gateway.py` | Vienintelis modulis visame šiame medyje, kuris tiesiogiai importuoja `zenoh`. Kiekvienas vertėjas Zenoh sesiją gauna per jį, tad transportą prireikus galima pakeisti vienoje vietoje. |
| `fusion.py` | Kelių šaltinių takelių koreliacija (ASTERIX CAT-48/CAT-21). |
| `data_stats.py` | Baitų ir žinučių skaitikliai, rodomi zenoh-admin skydelyje. |
| `track_views.py` | Pagalbininkas, publikuojantis keturis kodavimus vienu metu (`/sapient`, `/json`, `/proto`, `/raw`). |
| `process_bundle.py` | Bendras įeinančio proceso paleidimo karkasas vertėjų CLI. |
| `vendors/asterix/`, `vendors/sapient/`, `vendors/stanag/`, `vendors/sparkplug/` | ASTERIX (`cat.py`), SAPIENT (`flex335.py`), STANAG (`stanag.py`) ir Sparkplug B vertėjai. |
| `random/` | Vertėjai, nesusieti su konkrečiu gamintoju: CAP 1.2, misijų maršrutai, MQTT-JSON, NFFI, jutiklių sveikatos pranešimai. |
| `proto/` | `.proto` sutartys, po vieną kiekvienai vertėjų šeimai. |

## `docs/` — šis katalogas

Sunumeruoti dokumentai sudaro operatoriaus vadovą ir yra skirti skaityti
maždaug ta pačia tvarka, kuria sunumeruoti; pilną žemėlapį rasite
[00-pradekite-cia.md](00-pradekite-cia.md). Kataloge `references/` laikomos
šaltinio ir pasitikėjimo lygio pastabos apie kiekvieną išorinę
specifikaciją, kuria remiasi ši repozitorija (ASTERIX, SAPIENT, STANAG,
TAK, SitaWare). O `superpowers/` — tai AI-asistuoto projektavimo ir
planavimo archyvas (specifikacijos, datuoti planai); jame kūrimo istorija,
ne operatoriui skirta dokumentacija.

## Testai

Katalogas `tests/` seka pačio pod'o formą: po vieną `test_*.py` failą
kiekvienam tiltui, protokolui ar vertėjui, plius `smoke/` (visapusis
ryšio patikrinimas) ir `security/` (ACL bei autentifikacijos patikros).
`check-images-public.sh` ir `check_service_paths.py` skirti tik CI —
tai sanity patikros, ne vieneto testai.
