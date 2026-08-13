# 14 — Tęstinė integracija (CI)

## Tęstinė integracija (CI)

`.github/workflows/ci.yml` paleidžiamas kas kartą pushinant/darant PR į `main`:

| Job | Tikrina |
| --- | --- |
| `shellcheck` | Tikrina kiekvieną `.sh` skriptą repo'je (`-S warning`) |
| `compose-validate` | Patvirtina, kad `compose/docker-compose.yml` yra validus YAML |
| `bridge-syntax` | `py_compile` kiekvienam failui `compose/bridges/`, `compose/protocols/` ir `compose/layers/` |
| `zenoh-admin-frontend` | `pnpm type-check` + `pnpm build` `compose/zenoh-admin/ui` |
| `docker-build` | Sukuria `compose/Dockerfile` ir `compose/zenoh-admin` image'us, be push |

Tai pagauna sintaksės klaidas, TypeScript klaidas ir Dockerfile lūžimus prieš merge — **nepaleidžia** pačių bridge'ų (dauguma reikalauja tikrų API raktų/tinklo prieigos, kurios CI neturi).

---

## Pakeitimų žurnalas

| Data | Pakeitimas |
| --- | --- |
| 2026-06-14 | Pradinis commit — šakota iš oficialaus `efdi-moon-pod-main` saugyklos |
| 2026-06-15 | Baziniai bridge adapteriai sujungti; saugyklos struktūra nustatyta; pridėtas README |
| 2026-06-16 | Protocol Buffer takelių aprašai; dabar sutartys laikomos šalia vertėjų `compose/protocols/` kataloge |
| 2026-06-17/18 | Kokybės gerinimai: bridge'ų stabilumas, sluoksnių dublikatų filtravimas, takelio suliejimo derinimas |
| 2026-06-18 | ASTERIX pilno dekodavimo projektavimo specifikacijos dokumentas |
| 2026-06-19/22 | Papildomi bridge ir sluoksnių gerinimai; Giraffe ASTERIX bridge užbaigtas |
| 2026-06-22 | `dronuradaras.lt` bridge: akustinių jutiklių tinklas ir drono aptikimo įvykiai |
| 2026-06-22 | CoT DETECTION sekcija su garso įrašo URL ATAK pastabų lauke |
| 2026-06-22 | Radaro vietos žymeklis: publikacija paleidimo metu + 60 s keepalive, kad ATAK neprarastų žymeklio |
| 2026-06-23 | Saugumo patikrinimas: pašalintas koduotas API raktas iš `register_topics.sh`; raktas perkeltas į `$EFDI_PORTAL_KEY` aplinkos kintamąjį |
| 2026-06-23 | Saugumas: asmeninis namespace UUID, el. paštas, IP ir pardavėjo identifikatorius pašalinti iš visų sekamų failų; bridge'ai skaito `PARTNER_NAMESPACE` iš aplinkos |
| 2026-06-23 | Saugumas: `compose/.env` ir `register_topics.sh` pridėti į `.gitignore` — kredencialai lieka tik lokaliai |
| 2026-06-23 | Saugumas: neriboto HTTP kūno skaitymas `rest-http/bridge.py` apribotas iki 10 MB |
| 2026-06-23 | Dokumentacijos atnaujinimas: `INSTALL.md` (anglų), `DIEGIMAS.md` (lietuvių), `README.md` perrašytas kaip architektūros apžvalga |
| 2026-06-23 | ASTERIX CAT-34 I034/120 dekoderis: radaras pats praneša WGS-84 poziciją iš gyvo srauto — rankinis koordinačių nustatymas nebereikalingas |
| 2026-06-23 | Mobiliojo radaro palaikymas: pozicija, greitis ir kursas gaunami iš nuoseklių I034/120 pranešimų; ATAK rodo judėjimo taką ant transporto priemonėje montuojamų radarų |
| 2026-07-05 | Zenoh administravimo GUI: FastAPI + React panelė routerio būsenai ir `config.json5` redagavimui, stiliaus pavyzdys — TAK admin panelė |
| 2026-07-05 | Ištaisytas `zenoh-router.json5.tmpl` neatitikimas: šablone trūko plaintext `tcp/0.0.0.0:7448` vietinio listen endpoint, kurį gyva konfigūracija jau turėjo |
| 2026-07-05 | Zenoh admin GUI konfigūracijos skirtukas: pridėti `verify_name_on_connect` ir storage plugin loading perjungikliai; fabric endpoint dabar įvedamas kaip atskiri Host/Port laukai su vieno paspaudimo šablonais, vietoj žalio `tls/host:port` teksto |
| 2026-07-05 | Zenoh admin GUI: pridėtas `/api/health` (CPU/RAM/diskas/uptime/apkrova/tinklas/sertifikatų galiojimas, TAK admin panelės stiliaus) skydelyje |
| 2026-07-05 | Ištaisyta SPA routing klaida: tiesioginis navigavimas/refresh/back mygtukas į bet kurį GUI sub-route (`/config`, `/admin-users`) grąžindavo žalią JSON 404 vietoj programos užkrovimo — fallback kodas gaudė `fastapi.HTTPException`, bet `StaticFiles.get_response` meta `starlette.exceptions.HTTPException` (kitą, tėvinę klasę), todėl gaudymas niekada nesutapo |
| 2026-07-05 | Pridėtas izoliuotas `zenoh-router-test` servisas (`test` compose profilis) lokaliam pub/sub testavimui, neliečiant tikro pod'o ar jo fabric ryšio |
| 2026-07-05 | Pašalintas `gps-ew` bridge (GPSJam pagrindu) — gpsjam.org neturi viešo API savo apdorotiems duomenims, todėl šis bridge niekada realiai neveikė; pašalintas iš `start.sh` ir `tak_layer.py`, o ne paliktas tyliai sulūžęs |
| 2026-07-05 | Ištaisyti dubliuoti takeliai SitaWare tarp šaltinių/pod'ų: `nato_sitaware_layer.py` `_uid()` funkcijoje šaltinio pavadinimas buvo įtraukiamas į takelio ID (skirtingai nuo jau teisingos `tak_layer.py` versijos), todėl tas pats orlaivis iš dviejų šaltinių gaudavo du skirtingus SitaWare takelius |
| 2026-07-05 | `dronuradaras_bridge.py` buvo pakeistas publikuoti visus registruotus jutiklius su pozicija; šį sprendimą pakeitė žemiau aprašyta 2026-07-15 tik prisijungusių jutiklių taisyklė |
| 2026-07-05 | Pridėtas `.github/workflows/ci.yml`: tikrina bridge'ų/sluoksnių sintaksę, type-check + build zenoh-admin frontend'ui, sukuria abu Docker image'us kas kartą pushinant/darant PR |
| 2026-07-05 | Pridėti `shellcheck` ir `compose-validate` CI job'ai; ištaisytas vienintelis realus radinys (`compose/rebuild.sh` trūko `cd ... \|\| exit`) ir nutildytas klaidingas teigiamas (`SC2163` dėl sąmoningo "export pagal dinaminį vardą" idiomo `start.sh`/`stop.sh`/`run.sh`) |
| 2026-07-10 | Ištaisyta: `nato_sitaware_layer.py` naudojo tuos pačius aplinkos kintamuosius kaip gaunamas `sitaware_bridge.py` (`SITAWARE_URL`/`USER`/`PASS`) — pervadinta į `SITAWARE_NVG_*`, nes HQ (gaunama) ir Edge (siunčiama) paprastai yra skirtingi serveriai/prisijungimo duomenys |
| 2026-07-10 | Paslaugos `nffi` ir `sitaware-nvg` prijungtos prie `start.sh` — abi egzistavo repozitorijoje, bet niekada nebuvo registruotos kaip paleidžiamos paslaugos |
| 2026-07-10 | `start.sh`: `sitaware` ir `sitaware-nvg` dabar paklausia vartotojo vardo ir paslėpto slaptažodžio paleidimo metu (anksčiau buvo klausiama tik serverio adreso; prisijungimo duomenys turėjo būti iš anksto nustatyti `.env`) |
| 2026-07-10 | Zenoh admin GUI: pridėta "Connected routers" panelė — nuskaito `router/transport/unicast/*` įrašus, jau esančius admin space užklausoje, naudojamoje prenumeratorių/queryable sąrašams, jokios naujos ACL ar užklausos nereikia |
| 2026-07-10 | Zenoh admin GUI: perkeltas TAK-hud vizualinis stilius (`hud-card`, `hud-frame`/reticle kampai, `hud-glass` šoninis meniu, `hud-grid-bg` fonas, akcento švytėjimo mygtukai, laipsniškas atsiradimo animacijos) į `index.css`/`Layout.tsx`/skydelį |
| 2026-07-15 | `dronuradaras_bridge.py` dabar publikuoja tik įrenginius, kurių API būsena yra `is_online=true`; atsijungę įrenginiai siunčia pašalinimo įvykį, todėl CoT, SitaWare Edge ir HQ NVG talpykla ištrina senus žymeklius |
| 2026-07-17 | Pridėti deterministiniai ASTERIX kategorijų listener'ių susitarimai: CAT-010/020/021/034/048/062 pagal nutylėjimą naudoja UDP 50010/50020/50021/50034/50048/50062; tai EFDI, ne gamintojų numatytieji prievadai |
| 2026-07-17 | Pridėti Zenoh-native CAP, GeoJSON/OGC, spektro, jutiklių būklės, misijų maršrutų ir neapdoroto įėjimo vertimo keliai |
| 2026-07-17 | Saugumo atnaujinimas: atnaujintas Vite, prisegti/atnaujinti Compose image'ai, atnaujinti Python image'ų OS paketai, o autentifikuoti SitaWare/UTM endpoint'ai apriboti iki HTTPS |
| 2026-07-18 | Pridėtas TAK stiliaus Runtime Control: host bridge/protokolų/sluoksnių lifecycle veiksmai, apriboti log'ai, endpoint/temų/portų redagavimas, write-only kredencialai, localhost admin-control agent ir veikiantis Vite dev stack su suderintais API/Vite portais |
| 2026-08-02 | Sujungti `PARUOSIMAS.md`, `INTEGRATIONS.md`, `C2_RUNBOOK.md`, `ADDING_A_SENSOR.md`, `TROUBLESHOOTING.md` ir `GOTCHAS.md` (visi pilnai išversti į lietuvių kalbą) į šį dokumentą ([Diegimas ir paruošimas](03-diegimas-ir-paruosimas.md) §1; [Integracijos](08-integracijos.md), [C2 ↔ Zenoh instrukcija](09-c2-zenoh-instrukcija.md), [Naujo jutiklio pridėjimas](10-naujo-jutiklio-pridejimas.md) §§7-9; [Dažniausios problemos](11-dazniausios-problemos.md) §11) — vienas diegimo vadovas vietoj aštuonių; [ZENOH_ADMIN.md](12-zenoh-admin-gui.md) lieka atskirai |
| 2026-08-02 | Pridėtas BDS 1,0/1,7 (Data Link Capability / Common Usage GICB Capability) dekodavimas 7 ASTERIX kategorijoms, kurios jau naudoja BDS 3,0/4,0/5,0/6,0 GICB-ištraukimo pagalbininkus (CAT-010/011/018/020/021/048/062), pagal pyModeS |
| 2026-08-02 | Pervadinti `layers/cot_layer.py` → `layers/tak_layer.py` ir `layers/nvg_layer.py` → `layers/sitaware_layer.py` (tiekėjo pavadintas išvestinis sluoksnis, atitinkantis `tak_bridge.py`/`sitaware_bridge.py` gaunamųjų pavadinimus); pašalinti nenaudojami `cot-udp`/`cot-udp-tak` UDP multicast/unicast paleidiklio įrašai ir `nvg_bridge.py` NVG-XML gaunamasis tiltas (SitaWare įėjimas dabar tik REST) |
| 2026-08-02 | Sujungtos visos EFDI-autorystės `.proto` schemos po `compose/protocols/proto/` (anksčiau paskirstyta tarp `compose/protocols/random/`, `compose/protocols/vendors/proto/` ir `compose/protocols/vendors/sparkplug/`); vendoruotos trečiųjų šalių schemos (SAPIENT `sapient_msg/`, Sparkplug B) lieka savo `vendors/<name>/` kataloge |

---

*Skirta vidiniam naudojimui — neskleisti už projekto ribų.*
