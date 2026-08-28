# 13 — Temų taksonomija

Būsena: **įgyvendinta**.

## Fabric sutartis (v1) — pirmiausia perskaitykite šitą

Goat backbone priima tik tuos paskelbtus takelio raktus, kurie **baigiasi
`/tracks/v1`**. Tai griežta įėjimo taisyklė, ne tik konvencija, ir iš jos
kiekviename pavyzdyje žemiau seka du dalykai:

- **`json` yra kanoninė peržiūra ir peržiūros segmento apskritai neturi** —
  raktas tiesiog baigiasi `…/{id}/tracks/v1`. Visos kitos peržiūros savo
  pavadinimą įterpia prieš tai: `…/{id}/sapient/tracks/v1`,
  `…/{id}/proto/tracks/v1`, `…/{id}/raw/tracks/v1`.
- **Backbone tinkle `{prefix}` yra organizacijos UUID** (pvz.,
  `0123456789abcdef0123456789abcdef`) be jokių papildomų dalių. Žemiau
  pavyzdžiuose matomas `LTU/CISB` priešdėlis — tai tik **vietinės smėlio
  dėžės** susitarimas; kiekvienas diegimas jį konfigūruoja savaip, ir nuo to
  niekas kitas rakte nepriklauso.

Šalia takelių raktų gyvena dar dvi ne-takelinių raktų šeimos:

- **Buvimas (Presence):** `{prefix}/_meta/alive/<service>` — Zenoh
  gyvybingumo (liveliness) žetonas kiekvienam veikiančiam srautui,
  deklaruojamas per `compose/control/presence.py`. Būtent šitą fabric
  inspektorius (panoscope) piešia kaip **mazgą** — vien takelio PUT
  operacijos savaime piešia tik ryšius (edges) tarp mazgų, ne pačius
  mazgus. Plačiau žr. [01-architektura.md](01-architektura.md).
- **Valdymo plokštuma:** `{root}/**/@config/v1` ir panašūs raktai (aprašyti
  žemiau), kurie savo turinį versijuoja.

Protobuf peržiūros publikuojamos taip, kad **pačios apie save pasako
viską**: Zenoh `Encoding` laukas turi reikšmę
`application/protobuf;<Message.full_name>` (tai atlieka
`track_views.proto_encoding`), tad inspektoriaus schemos peržiūrėtojas gali
rasti atitinkamą `.proto` aprašą, net neieškodamas jo kitur.

## Raktas

```
{prefix}/{pod}/{domain}/{source}/{modality}/{affiliation}/{entity}/{type}/{id}[/{view}]/tracks/v1
```

```
LTU/CISB/hq/air/partner-adsb/adsb/civ/aircraft/b738/ly-abc/sapient
LTU/CISB/hq/air/010-042/radar/unknown/aircraft/unknown/cat48-010-042-4211/json
LTU/CISB/hq/land/dronuradaras/acoustic/unknown/drone/unknown/1/sapient
```

| Segmentas | Reikšmė | Pavyzdys |
|---|---|---|
| `prefix` | Organizacijos priešdėlis, priklauso nuo diegimo | `LTU/CISB` |
| `pod` | Kuris pod'as tai paskelbė | `hq` |
| `domain` | Fizinė sritis | `air` · `land` · `sea` · `space` |
| `source` | Kas tai pastebėjo — konkretaus jutiklio ar srauto tapatybė | `010-042` · `partner-adsb` · `dronuradaras` |
| `modality` | Kaip tai buvo pastebėta | `radar` · `acoustic` · `adsb` |
| `affiliation` | Draugas/priešas priklausomybė | `civ` · `mil` · `friendly` · `hostile` · `neutral` · `unknown` |
| `entity` | Kokio tipo objektas | `aircraft` · `uav` · `vessel` · `person` · `vehicle` |
| `type` | Konkretus tipas; `unknown`, jei jutiklis to žinoti negali | `b738` · `unknown` |
| `id` | Stabili šio konkretaus objekto tapatybė | uodegos numeris, ICAO24, MMSI, radaro takelio numeris |
| `view` | To paties objekto kodavimo variantas | `sapient` · `json` · `proto` · `raw` |

## Kodėl `source` ir `modality` yra du atskiri segmentai, o ne vienas

Kiekvienas iš jų atsako į savo klausimą, ir vienas segmentas negali
atsakyti į abu vienu metu.

`source` — tai kilmė: *kas konkrečiai tai pasakė*. Du ADS-B srautai turi
likti atskiruose segmentuose, nes tam ir egzistuoja `fusion`
(`compose/protocols/fusion.py`) — kad juos vėliau sujungtų ir palygintų
tarpusavyje. Tas pats pasakytina apie du radarus, tiekiančius duomenis
tam pačiam maršrutizatoriui: jei jų `source` sutaptų, jų takeliai vienas
kitą pradangintų.

`modality` — tai metodas: *kaip tai buvo aptikta*. Būtent pagal šį
segmentą C2 vartotojas dažniausiai ir filtruoja, nes pasitikėjimo lygis,
vėlavimas ir tikslumas priklauso nuo aptikimo metodo kur kas labiau, nei
nuo to, koks konkrečiai gamintojas jutiklį pagamino.

Sujungę juos į vieną segmentą, prarastumėte vieną iš dviejų galimybių
filtruoti. Palaikydami abu atskirai, gaunate teisingą atsakymą tiek į
`**/acoustic/**`, tiek į `**/dronuradaras/**` užklausas.

### Modality žodynas

Žodynas paimtas iš SAPIENT `NodeType` enum
(`compose/vendor/sapient_msg/bsi_flex_335_v2_0/registration.proto`) — kad
temos segmentas visada atitiktų patį turinį, o ne nuo jo pamažu nukryptų:

`radar` · `lidar` · `camera` · `seismic` · `acoustic` · `proximity_sensor` ·
`passive_rf` · `human` · `chemical` · `biological` · `radiation` · `kinetic` ·
`jammer` · `cyber` · `ldew` · `rfdew` · `mobile_node` · `pointable_node` ·
`fusion_node`

Yra ir keturios papildomos reikšmės, kurias pats SAPIENT sumestų į vieną
`PASSIVE_RF` arba `OTHER` kategoriją, bet EFDI jas laiko atskirai, nes
pagal jas realiai reikia maršrutizuoti skirtingai:

| Papildoma reikšmė | Reikšmė |
|---|---|
| `adsb` | Orlaivio savaiminis pranešimas, perduotas per antžeminę stotį |
| `mlat` | Pozicija apskaičiuota iš atvykimo laiko skirtumų |
| `c2` | Gauta per komandavimo/valdymo ar mūšio valdymo sistemą |
| `telemetry` | Pati platforma praneša apie savo būseną |
| `fused` | Sekiklio, jau sujungusio kelis jutiklius, išvestis |
| `unknown` | Iš viso ne stebėjimas (pvz., stoties sveikatos pranešimas, papildomas sluoksnis) |

## Iš kur kiekvienas segmentas atsiranda

**ASTERIX pats įvardija savo jutiklį.** Kiekviena kategorija neša SAC/SIC
lauką (I0xx/010), tad `source` čia yra `{SAC:03d}-{SIC:03d}` — jis
dekoduojamas iš pačio įrašo, o ne konfigūruojamas iš išorės. Būtent todėl
`cat.py` temų konstantos yra šablonai su `{source}` viduje.

**ASTERIX kategorija savaime nurodo modality**, nes kategorija jau
užkoduoja aptikimo metodą:

| Kategorija | Fiziškai reiškia | modality |
|---|---|---|
| CAT-010, CAT-034, CAT-048 | pirminis/antrinis radaras | `radar` |
| CAT-021 | ADS-B, perduota | `adsb` |
| CAT-020 | multilateracija | `mlat` |
| CAT-062 | sistemos takeliai, jau sulieti | `fused` |

**SAPIENT atveju modality nuskaitoma iš paties srauto.** Mazgas savo
`NodeType` deklaruoja registruodamasis, tad įeinanti SAPIENT kamera pati
atsiduria po `/camera/`, radaras — po `/radar/`, ir visi mazgai nesuplaukia
į vieną bendrą segmentą.

## Formatai

Visos keturios peržiūros yra pavadintos, todėl niekas jose nėra numanomas
— vartotojas, tiesiog perskaitęs raktą, iš karto žino, kas yra baituose:

| Tema | Turinys | Kodavimas |
|---|---|---|
| `…/{id}/tracks/v1` | Plokščias, skaitomas JSON — **kanoninė** peržiūra, be atskiro peržiūros segmento | `application/json` |
| `…/{id}/sapient/tracks/v1` | BSI Flex 335 v2 `SapientMessage` — fabric sutartis | `application/protobuf;…SapientMessage` |
| `…/{id}/proto/tracks/v1` | EFDI protokolo-specifinis protobuf su pilna jutiklio detale | `application/protobuf;…<Track>` |
| `…/{id}/raw/tracks/v1` | Originalūs srauto baitai, suvynioti į `RawEnvelope` | `application/protobuf;…RawEnvelope` |

## Valdymo plokštuma versijuojama, duomenų formatai — ne

Duomenų formatai vienas kito niekada nepakeičia, todėl jie tiesiog
pavadinti, o ne sunumeruoti. Valdymo plokštumos sutartys, priešingai, iš
tikrųjų kinta laikui bėgant, todėl jos versijuojamos:

```
{root}/**/@config/v1          {root}/**/@config/status/v1
{root}/**/@config/relay/v1    {root}/**/@topology          (be versijos)
```

## Prenumeravimas

```
**/air/**                       visi oro objektai, bet koks šaltinis
**/radar/**                     viskas, aptikta bet kuriuo radaru
**/010-042/**                   viskas iš VIENO konkretaus radaro
**/hostile/**                   visi priešiški kontaktai
**/aircraft/**                  tik orlaiviai
…/aircraft/b738/**              vienas konkretus orlaivio tipas
…/{id}/sapient/tracks/v1        vienas objektas, tik SAPIENT peržiūra
**/sapient/tracks/v1            kiekvienas objektas, tik SAPIENT peržiūra
```

Repozitorijos viduje esantys sluoksniai prenumeruoja su galiniu `/**`,
kuris jau apima ir `/tracks/v1`, ir `/{view}/tracks/v1` galūnę — todėl,
pridėjus versijos galūnę prie rakto, senų prenumeratų keisti nereikėjo.

## Žinomi kompromisai

**`type` gali keistis laikui bėgant.** ADS-B iš karto žino `b738` iš
registro, o radaras nežino nieko ir skelbia `unknown`. Jei takelio tipas
vėliau paaiškėja, jo tema PASIKEIČIA — prenumeratoriai tiesiog pamato,
kaip senas raktas nutyla, o vietoje jo atsiranda naujas. Tai sąmoningas
sprendimas: tipas taip pat yra ir SAPIENT `classification` lauke, kurį
galima pataisyti nekeičiant paties rakto.

**`id` padaro temas atskiras kiekvienam objektui.** Kardinalumas šoka nuo
maždaug 40 temų iki vienos temos kiekvienam sekamam objektui. Zenoh su tuo
susitvarko, bet tai keičia išlaikymo (retention) elgseną: talpyklos
papildinys dabar laiko paskutinę žinomą reikšmę kiekvienam atskiram
objektui, o ne kiekvienai bendrai klasei.

**`modality` ne-jutikliams yra tiesiog nereikalingas balastas.** Papildomi
sluoksniai ir sveikatos signalai (`mission`, `cap`, `ogc`, `health`) neša
`unknown` šiame segmente, nes jiems jis niekada nieko realiai nereiškia.
Tai priimta sąmoningai — svarbiau, kad kiekvienas raktas turėtų fiksuotas
segmentų pozicijas, ir vartotojas galėtų raktą skaidyti pagal `/`,
netikrindamas kiekvieną kartą, kurie segmentai jame iš tikrųjų prasmingi.

**Kai šaltinis dinamiškas, naudojami pakaitos simboliai (wildcards).**
Radaras save įvardija pagal SAC/SIC, tad jo tema iš anksto, paleidimo
metu, dar nežinoma. Todėl prenumeratoriai vietoje konkretaus šaltinio
naudoja `*` (`…/air/*/radar/**`) — irgi sąmoningai, kad apimtų kiekvieną
radarą, koks tik prisijungtų.

## Kaip tai sukonstruota

Vienintelė vieta, kur iš tikrųjų sudaromas raktas, yra `semantic_topic()`
faile `compose/protocols/track_views.py`. Leidėjai jai perduoda tik
semantinį priešdėlį (`…/{domain}/{source}/{modality}/{affiliation}/{entity}`);
pati funkcija prideda `{type}/{id}` iš paties takelio, o kiekviena
publikavimo šaka papildomai prideda savo peržiūros segmentą. Taip visa
taksonomija gyvena vienoje funkcijoje, o ne išbarstyta po 26 skirtingas
publikavimo vietas.

`id` parenkamas pagal prioritetą: `registration` → `icao24` → `mmsi` →
`uid` → `callsign` → `track_num` → `radar_id`. Pirmas atitikęs laukas
laimi ir tampa objekto raktu visam jo gyvavimo laikui.

Verta žinoti apie šiuos konfliktus — visi jie jau sutvarkyti kode, bet
naudinga suprasti, kodėl:

- **`sapient` yra kartu ir peržiūros pavadinimas, ir galimas SOURCE
  pavadinimas.** Todėl raktas tipo `…/air/sapient/acoustic/unknown/uav/…`
  neturi turėti savo šaltinio perrašyto, kai vėliau išvedama `/raw`
  peržiūra — dėl to peržiūra laikoma tik PASKUTINIU rakto segmentu, niekada
  ne bet kuriuo kitu.
- **`object_id` privalo būti kildinamas tik iš tapatybės, ne iš laiko.**
  Jei jo pusę sudarytų atsitiktinė ULID dalis, o kita pusė judėtų kartu su
  laiko žyma, tai du to paties orlaivio pranešimai, atskirti vos
  milisekundėmis, gautų skirtingus raktus ir būtų klaidingai perskaityti
  kaip du atskiri kontaktai.
- **`source` ir `modality` negali sutapti žodis į žodį.** `fused/fused`
  savaime nieko nereiškia — šaltinis visada įvardijamas pagal patį mazgą,
  o modality lieka aptikimo metodas, ir šie du dalykai neturi susilieti.
- **Prenumerata pagal modality gali persidengti su jūsų paties išvestimi.**
  `fusion` prenumeruoja `…/air/*/fused/**`, kad priimtų ASTERIX CAT-062 —
  bet tai sutampa ir su jo paties skelbiamais takeliais. Todėl jis aiškiai
  atmeta savo pačio priešdėlį; kitaip sulietas takelis būtų iš naujo
  priimtas ir sulietas pats su savimi.

Maršrutizatoriaus ACL dėl to nekeičiama: duomenų raktai ir toliau lieka
po `${DATA_TOPIC_ROOT}/**`.

## Kas dėl to gali sulūžti

Bet koks jau egzistuojantis prenumeratorius, kuris šias temas jau naudoja.
Kol kas gamyboje jų dar niekas nevartoja, tad kaina keisti šią schemą
šiandien yra mažiausia, kokia tik bus — ir tik augs su kiekvienu nauju
vartotoju.

Repozitorijos viduje esantys prenumeratoriai jau perkelti prie naujos
schemos: `tak_layer` ir `sitaware_layer` prenumeruoja su `**`, kuris jau
apima pridėtą segmentą savaime; o `fusion` bei `tak_layer` radaro būsenos
prenumerata perrašyta taip, kad raktuotų pagal modality.
