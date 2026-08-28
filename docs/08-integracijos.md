# 08 — Integracijos

## Integracijos

### Integracijų apžvalga

> **Norite prijungti naują jutiklį?** Šis puslapis yra nuoroda, kas jau
> sujungta. Žingsnis po žingsnio vadovą "kaip pridėti naują" žr.
> [Naujo jutiklio ar protokolo pridėjimas](10-naujo-jutiklio-pridejimas.md) žemiau.

EFDI atskiria šaltinio-specifinius kolektorius, daugkartinio naudojimo
protokolų vertėjus ir TAK/SitaWare išvesties sluoksnius:

- `compose/bridges/` — jungiasi prie vardinio produkto ar paslaugos.
- `compose/protocols/` — po vieną nepriklausomai paleidžiamą laidinį/API
  protokolą faile. ASTERIX kategorijos atskiros, nes skiriasi jų UAP ir
  leidimai.
- `compose/layers/` — jungia normalizuotus Zenoh duomenis su TAK/CoT ir
  SitaWare/NVG.

Kai tik gaunamas skriptas paskelbia normalizuotą temą, veikiantys CoT ir NVG
sluoksniai automatiškai ją prenumeruoja. Imtuvai ir aptikimo sistemos
paprastai prisijungia prie netoliese esančio Zenoh routerio ir skelbia ten;
routeris perduoda jų duomenis. Todėl daugumai routerio serverių nereikia
jokios imtuvo aparatinės įrangos ar tiekėjo tvarkyklės.

### Protokolo prijungimo reikalavimai

ASTERIX kategorijos numeris nenustato TCP ar UDP prievado numerio. Šio
diegimo radaras/šliuzas kategorijų neskirsto po atskirus prievadus — CAT-034
ir CAT-048 abi ateina sumaišytos viename bendrame UDP dump'e per 50000
prievadą (`UDP_INGRESS_PORT`). `udp_ingress_bridge.py` išsaugo kiekvieną
datagramą ir saugiai publikuoja pilnus ASTERIX kadrus nepakeistus į
`…/raw/asterix/catNN`; kiekvienas kategorijos vertėjas lieka atskiru procesu
ir iš to paties bendro srauto prenumeruoja tik savo temą.
`ASTERIX_CATEGORIES` pasirenka, kurios kategorijos automatiškai
išsiunčiamos. Žemiau esančioje lentelėje minimas `CATNN_PORT` dedikuoto
prievado galimybė `vendors/asterix/cat.py` viduje tebeegzistuoja gamintojui,
kuris tikrai siunčia vieną kategoriją savo atskiru prievadu — tiesiog šio
diegimo radaras/šliuzas CAT-034/048 taip nesiunčia.

Kai radaro pusės nešiojamas kompiuteris jau publikuoja pilnus kadrus per kitą
Zenoh routerį, nustatykite `ASTERIX_ZENOH_UPSTREAM_ENDPOINT` ir pasirinktinai
`ASTERIX_ZENOH_UPSTREAM_ROOT`. `asterix_bridge.py` prenumeruoja kiekvieną
`…/raw/asterix/catN` temą tame routeryje, patikrina, ar temos kategorija ir
ASTERIX antraštė sutampa, ir republikuoja nepakeistą kadrą vietiniame
routeryje. Tie patys kategorijos vertėjai jį tada dekoduoja; pats tiltas
neinterpretuoja UAP. Paprastas tekstas `tcp/...:7448` skirtas tik izoliuotam
testavimui.

Mišrus tiltas taip pat palaiko `ASTERIX_BIND`, `ASTERIX_MULTICAST_GROUP`,
`ASTERIX_MULTICAST_INTERFACE` ir IPv4/CIDR `ASTERIX_ALLOW_SOURCE` filtrą.
Prieš konfigūruodami naują srautą, stebėkite jį be Zenoh publikavimo:

```bash
python3 tools/asterix_probe.py --port 30001
```

Zondas praneša siuntėjo IP, paskirties prievadą, kategoriją, pirmo FRN
SAC/SIC (jei yra), kadrų skaičių ir dažnį. Multicast srautui pridėkite
`--multicast-group` ir `--multicast-interface`.

`asterix_probe.py` yra `tools/` kataloge, šalia `asterix_relay.py`, o ne
`compose/` — niekas `tools/` kataloge neimportuojama podo, nepaleidžiama
`start.sh`/`run.sh` ir neįtraukiama į jokį image'ą. Tai savarankiški
operatoriaus įrankiai, paleidžiami ranka, dažniausiai iš nešiojamo kompiuterio
arba prie radaro prijungto PC, konfigūruojant srautą: `compose/bridges/` ir
`compose/protocols/` yra veikiantis duomenų sluoksnis, `tools/` — lauko
įrankiai. `asterix_relay.py` persiunčia ASTERIX UDP datagramas nepakeistas,
baitas į baitą, iš vietinio prievado į nuotolinį `IP:PORT` — paleiskite jį
mašinoje, prijungtoje prie radaro, kai ta mašina yra NetBird tinkle, bet pats
radaras nepasiekiamas iš podo:

```bash
python3 tools/asterix_relay.py --dest 100.x.y.z:30048
```

`tests/test_asterix_raw_pipeline.py` tikrina kadravimo logiką, kurią šie
įrankiai dalinasi su dekoderiais, todėl pakeitimai čia pagaunami įprastu
testų paleidimu.

| Protokolo skriptas | Transporto vaidmuo | Reikalinga partnerio/veikimo konfigūracija | Dabartinė sutartis |
|---|---|---|---|
| `vendors/asterix/cat.py --category 1` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT1_PORT`; nustatykite `CAT1_RADAR_LAT/LON` polinių/kartezinių planų/takelių georeferencijai | EUROCONTROL CAT-001 Ed.1.4 monoradaro plano/takelio pranešimai (paveldėta, dažniausiai pakeista CAT-048) |
| `vendors/asterix/cat.py --category 2` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT2_PORT` | EUROCONTROL CAT-002 Ed.1.2 monoradaro paslaugų pranešimai (šiaurės žymeklis, sektoriaus kirtimas, stoties būsena) |
| `vendors/asterix/cat.py --category 4` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT4_PORT` | EUROCONTROL CAT-004 Ed.1.13 saugos tinklo įspėjimai (STCA/MSAW/APW/RIMCA/...) |
| `vendors/asterix/cat.py --category 7` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT7_PORT`; nustatykite `CAT7_RADAR_LAT/LON` polinių/kartezinių pranešimų georeferencijai | EUROCONTROL CAT-007 Ed.1.12 nukreiptos apklausos pranešimai (karinė Mode 4/5/S apklausos kontrolė; downlink ir uplink UAP) |
| `vendors/asterix/cat.py --category 8` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT8_PORT` | EUROCONTROL CAT-008 Ed.1.3 monoradaro išvestinė orų informacija (orų vaizdo vektoriai/kontūrai) |
| `vendors/asterix/cat.py --category 9` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT9_PORT` | EUROCONTROL CAT-009 Ed.2.1 sudėtiniai orų pranešimai (sujungtas daugiaradaro orų vaizdas) |
| `vendors/asterix/cat.py --category 10` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT10_PORT`; nustatykite oro uosto atskaitos koordinates, jei pranešimai naudoja tik vietinį X/Y arba polinę poziciją | EUROCONTROL CAT-010 Ed.1.1, oro uosto paviršiaus taikiniai/būsena |
| `vendors/asterix/cat.py --category 11` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT11_PORT`; nustatykite `CAT11_SITE_LAT/LON` kartezinių pranešimų georeferencijai | EUROCONTROL CAT-011 Ed.1.3 A-SMGCS sistemos takeliai (sujungti oro uosto paviršiaus orlaiviai + transporto priemonės su skrydžio plano koreliacija) |
| `vendors/asterix/cat.py --category 15` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT15_PORT`; nustatykite `CAT15_SITE_LAT/LON` diapazono/azimuto pranešimų georeferencijai | EUROCONTROL CAT-015 Ed.1.2 nepriklausomos nekooperatyvios stebėsenos (pasyvios/daugiastotės) taikinių pranešimai |
| `vendors/asterix/cat.py --category 16` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT16_PORT` | EUROCONTROL CAT-016 Ed.1.0 nepriklausomos nekooperatyvios stebėsenos sistemos konfigūracijos pranešimai (INCS antžeminės sistemos pačios vietos pozicijos/siųstuvo/imtuvo konfigūracija, CAT-015 sesuo statuso kategorija) |
| `vendors/asterix/cat.py --category 17` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT17_PORT` | EUROCONTROL CAT-017 Ed.1.3 Mode S stebėsenos koordinavimo funkcijos pranešimai (paveldėtas tarpradarinio klasterio/perdavimo protokolas; „Track Data" pranešimai turi poziciją, tinklo valdymo pranešimai — ne) |
| `vendors/asterix/cat.py --category 18` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT18_PORT`; nustatykite `CAT18_SITE_LAT/LON` vietinių polinių/kartezinių pozicijos elementų georeferencijai | EUROCONTROL CAT-018 Ed.1.8 Mode S duomenų perdavimo funkcijos pranešimai (GDLP/apklausiklio uplink-downlink koordinavimas: orlaivių pranešimai, uplink paketo/transliacijos/GICB-ištraukimo užklausos ir patvirtinimai) |
| `vendors/asterix/cat.py --category 19` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT19_PORT` | EUROCONTROL CAT-019 Ed.1.3 MLT sistemos būsena |
| `vendors/asterix/cat.py --category 20` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT20_PORT` ir patvirtina 1.11 leidimą | EUROCONTROL CAT-020 Ed.1.11 MLAT pranešimai |
| `vendors/asterix/cat.py --category 21` | UDP klausytojas arba TCP serveris | ADS-B šliuzas siunčia į `CAT21_PORT` ir patvirtina 2.7 leidimą | EUROCONTROL CAT-021 Ed.2.7 ADS-B pranešimai |
| `vendors/asterix/cat.py --category 23` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT23_PORT` | EUROCONTROL CAT-023 Ed.1.3 CNS/ATM antžeminės stoties paslaugų pranešimai (ADS-B/TIS-B/FIS-B/GRAS/MLT stoties būsena) |
| `vendors/asterix/cat.py --category 25` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT25_PORT` | EUROCONTROL CAT-025 Ed.1.6 CNS/ATM antžeminės sistemos būsenos pranešimai (CAT-023 įpėdinis/palydovas: atskirta sistemos/paslaugos būsena, komponentų sąrašas, paslaugų statistika, vietos pozicija) |
| `vendors/asterix/cat.py --category 32` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT32_PORT` | EUROCONTROL CAT-032 Ed.1.2 Miniplan pranešimai SDPS (FPPS/SDPS skrydžio plano-takelio numerio koreliacija; šioje kategorijoje pozicijos lauko nėra) |
| `vendors/asterix/cat.py --category 34` | Automatiškai išsiunčiama iš bendro UDP 50000 įėjimo (`ASTERIX_CATEGORIES`) | Šio diegimo radaras siunčia CAT-034 sumaišytą su CAT-048 per UDP 50000, ne atskiru prievadu | EUROCONTROL CAT-034 Ed.1.29 radaro paslaugų pranešimai |
| `vendors/asterix/cat.py --category 48` | Automatiškai išsiunčiama iš bendro UDP 50000 įėjimo (`ASTERIX_CATEGORIES`) | Šio diegimo radaras siunčia CAT-048 sumaišytą su CAT-034 per UDP 50000, ne atskiru prievadu; vietinei polinei pozicijai reikia `CAT48_RADAR_LAT/LON` | EUROCONTROL CAT-048 Ed.1.32 taikiniai |
| `vendors/asterix/cat.py --category 62` | TCP klientas arba UDP klausytojas | Nustatykite `CAT62_HOST/PORT`, arba `CAT62_UDP=1`; patvirtinkite 1.21 leidimą | EUROCONTROL CAT-062 Ed.1.21 sistemos takeliai |
| `vendors/asterix/cat.py --category 63` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT63_PORT` | EUROCONTROL CAT-063 Ed.1.7 jutiklio būsenos pranešimai (jutikliai, maitinantys CAT-062 sekiklį) |
| `vendors/asterix/cat.py --category 65` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT65_PORT` | EUROCONTROL CAT-065 Ed.1.6 SDPS paslaugos būsenos pranešimai (SDPS pusės partneris CAT-062, tas pats ryšys kaip CAT-019 su CAT-020) |
| `vendors/asterix/cat.py --category 150` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT150_PORT` | EUROCONTROL CAT-150 Ed.3.0 MADAP plano serverio skrydžio duomenų pranešimas (Maastrichto UAC paveldėtas skrydžio plano paskirstymas/koreliacija/konflikto duomenys; šiame leidime pozicijos lauko nėra) |
| `vendors/asterix/cat.py --category 205` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT205_PORT`; nustatykite `CAT205_SITE_LAT/LON` kartezinių pranešimų georeferencijai | EUROCONTROL CAT-205 Ed.1.0 radijo krypties nustatymo pranešimai (RDF tinklas triangulizuoja radijo siųstuvo poziciją, dažniausiai orlaivio VHF radiją) |
| `vendors/asterix/cat.py --category 240` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT240_PORT` | EUROCONTROL CAT-240 Ed.1.3 radaro vaizdo perdavimas (žalias prieš-plot-ekstrakcijos signalo lygio vaizdas, ne taikinio pranešimas; pranešimai gali nešti iki ~64KB vaizdo duomenų) |
| `vendors/asterix/cat.py --category 247` | UDP klausytojas arba TCP serveris | Gamintojas siunčia į `CAT247_PORT` | EUROCONTROL CAT-247 Ed.1.3 versijos numerio mainai (šaltinis praneša, kurį kiekvienos ASTERIX kategorijos leidimą jis siunčia) |
| `vendors/sapient/flex335.py` | TCP klausytojas arba klientas | Kraštinis mazgas jungiasi prie `SAPIENT_LISTEN_PORT`, arba nustatykite tarpinės programinės įrangos `SAPIENT_HOST/PORT`; nuotoliniai klausytojai reikalauja leistino šaltinio CIDR | BSI FLEX 335 v2 kadravimas ir viešas SAPIENT protobuf poaibis |
| `nffi.py` | Zenoh prenumeratorius/vertėjas | Leidėjas rašo vieną pilną XML dokumentą į `…/raw/nffi/{source-id}` | NATO NFFI / ADatP-36 (STANAG 5527) XML poaibis |
| `vendors/stanag/stanag.py --proto 4586` | TCP klientas | Nustatykite CUCS/VSM `STANAG4586_HOST/PORT`; patvirtinkite VSM ICD prieš pasirinkdami `STANAG4586_PROFILE=legacy_ed3_approx` | Istorinis diegimo išdėstymas, numatytai išjungtas; nesiūlomas kaip bendras STANAG 4586 dekoderis |
| `vendors/stanag/stanag.py --proto 4607` | Zenoh raw prenumeratorius | Tiltas patalpina pilnus paketus į `…/raw/stanag_4607/**`; STANAG apibrėžia pranešimą, ne nešėją | NATO GMTI (Ground Moving Target Indicator) formatas — Mission/Dwell/Job Definition/Platform Location segmentai, po vieną takelį kiekvienam Target Report |
| `vendors/stanag/stanag.py --proto 4609` | SRT/KLV įėjimas | Nustatykite `STANAG4609_SRT_URL` judesio vaizdo metaduomenų srautui | MISB ST 0601 KLV local-set poaibis per STANAG 4609 judesio vaizdą; SRT yra sukonfigūruotas transportas, ne KLV schemos dalis |
| `vendors/stanag/stanag.py --proto 5516` | UDP klausytojas | Nustatykite `STANAG5516_PORT` (numatytai 3010); šliuzas siunčia JREAP-C įkapsuliuotą Link 16 J-seriją | MIL-STD-6016F / STANAG 5516 Ed.5 J2.2/J2.5/J3.2/J3.5/J3.7 poaibis per JREAP-C (MIL-STD-3011) |

`stanag.py` sujungia visus keturis STANAG variantus, kuriuos kalba EFDI, į
vieną failą (dekodavimas ir, kur taikoma, kodavimas kartu) —
`--proto {4586,4607,4609,5516}` pasirenka, kurį iš jų paleisti konkrečiame
procese; laidinių pranešimų formas žr. `proto/stanag.proto`.

Visi dvidešimt septyni ASTERIX vertėjai taip pat priima `--zenoh-raw` (arba
atitinkamą `CATNN_ZENOH_RAW=1`) tiksliam pilnam kadrui į `…/raw/asterix/catNN`.
Paleidikliai automatiškai pasirenka šį režimą kategorijoms, išvardytoms
`ASTERIX_CATEGORIES`, kai sukonfigūruotas bendras UDP įėjimas arba upstream
Zenoh ASTERIX tiltas.

VERA-NG pasyvūs jutikliai, teikiantys CAT-34 ir CAT-48, naudoja tą patį
neapdoroto ASTERIX kelią; jiems nereikia VERA-specifinio tilto. Kiekvienam
pranešėjui suteikite unikalią SAC/SIC porą ir palikite `CAT34_RADAR_NAME`
tuščią, kai Giraffe ir VERA šaltiniai dalinasi vienu srautu, kad jų vieta,
būsena, aprėptis ir taikinio būsena liktų nepriklausomai identifikuoti kaip
`RADAR SACx/SICy`. Pirmenybę teikite gyviems CAT-34 I034/120 vietos pozicijos
ir I034/100 aprėpties reikšmėms. Prieš eksploatacinį naudojimą užfiksuokite
reprezentatyvius kadrus ir patvirtinkite gamintojo CAT-34/CAT-48 leidimus ir
UAP pagal sukonfigūruotus Ed.1.29/Ed.1.32 dekoderius. Pasyviam jutikliui
negalima priskirti dirbtinio besisukančio šlavimo: EFDI atkuria šlavimo
judesį tik tada, kai šaltinis iš tikrųjų siunčia atitinkamus CAT-34 laiko
pranešimus.

ASTERIX yra bitų lygio stebėsenos mainų šeima; kategorija ir leidimas turi
sutapti su gamintoju. EUROCONTROL publikuoja CAT-010 paviršiaus judėjimui,
CAT-021 ADS-B taikinių pranešimams, CAT-062 sistemos takeliams ir CAT-240
žaliam radaro vaizdui. CAT-240 nėra žemėlapio-takelio srautas ir prieš
TAK/SitaWare publikavimą reikalauja radaro vaizdo apdorojimo. Žr.
[EUROCONTROL ASTERIX katalogą](https://www.eurocontrol.int/asterix),
[CAT-010 Ed.1.1 specifikaciją](https://www.eurocontrol.int/sites/default/files/service/content/documents/nm/asterix/cat010-asterix-monoradar-surface-movement-data-part-7.pdf),
[CAT-021](https://www.eurocontrol.int/publication/cat021-eurocontrol-specification-surveillance-data-exchange-asterix-part-12-category-21),
[CAT-062](https://www.eurocontrol.int/publication/cat062-eurocontrol-specification-surveillance-data-exchange-asterix-part-9-category-062)
ir [CAT-240](https://www.eurocontrol.int/publication/cat240-eurocontrol-specification-surveillance-data-exchange-asterix).

### Išvesties temos: `/sapient`, `/json`, `/proto`, `/raw`

Kiekvienas dekoduotas takelis publikuojamas ant vieno objekto rakto su
keturiomis susijusiomis temomis. Jos neša tą patį įvykį skirtingu tikslumu,
todėl vartotojas prenumeruoja tik vieną temą ir ignoruoja likusias. Niekas
nėra numanomas — vartotojas, skaitantis raktą, visada žino, kas yra baitai.

```
{prefix}/{pod}/{domain}/{source}/{modality}/{affiliation}/{entity}/{type}/{id}/{view}
```

| Tema | Temos priesaga | Zenoh kodavimas | Turinys |
|---|---|---|---|
| SAPIENT | `…/{id}/sapient` | `application/protobuf` | BSI Flex 335 v2 `SapientMessage`. **Magistralės sutartis.** |
| JSON | `…/{id}/json` | `application/json` | Plokščias JSON objektas. Tik dekoderio modeliuojami laukai. |
| Protobuf | `…/{id}/proto` | `application/protobuf` | Tipizuota žinutė iš protokolo `.proto` (`compose/protocols/`). |
| Raw | `…/{id}/raw` | `application/protobuf` | `RawEnvelope`, apgaubiantis **originalius laidinio baitus**, nepakeistus. |

**Kurią temą naudoti.** Numatytai naudokite `/sapient`: tai sutarta sutartis
duomenims, paliekantiems magistralę. `/proto` naudokite, kai reikia pilno
protokolo-specifinio jutiklio detalumo, kurio SAPIENT nemodeliuoja, o `/raw` —
kai reikia lauko, kurio EFDI visai nedekoduoja, arba norite paleisti tiekėjo
paties dekoderį ant tikslių baitų. `/json` skirtas žmonėms ir vartotojams,
negalintiems susieti protobuf vykdymo laiko.

**Originalūs turiniai yra tiksliai baitas į baitą.** Jie perpakuojami, bet
niekada perkoduojami:

- ASTERIX — po vieną savarankišką duomenų bloką kiekvienam pranešimui: CAT
  baitas ir 2 baitų ilgio antraštė pridedami iš naujo, todėl bet koks
  standartinis ASTERIX dekoderis jį perskaito.
- SAPIENT — originalus BSI Flex 335 v2 `SapientMessage`, jau be 32 bitų
  ilgio priešdėlio.
- STANAG 4609 — žalias MISB KLV paketas.

`RawEnvelope` (`../compose/protocols/proto/raw_envelope.proto`) neša
`protocol`, `profile` (pvz., `cat048`, `misb-st0601`), `content_type` ir
`payload` baitus.

**Tikslumo išlyga.** `/sapient`, `/json` ir `/proto` yra tik tiek pilni, kiek
pilnas dekoderis. Kai reikšmės negalima atvaizduoti tikslinėje sutartyje, ji
išmetama iš tos temos ir užrašoma `protobuf encode failed …` eilutė — vienos
temos nesėkmė niekada neblokuoja kitų. `/raw` yra vienintelė tema, kuri
niekada neprarandama lauko.

Visos keturios temos yra po podo pirminio publikavimo priešdėliu, todėl esama
`${DATA_TOPIC_ROOT}/**` routerio ACL jau jas apima — naujai temai pridėti
ACL keisti nereikia.

#### Suderinamumas su išoriniu katalogu

Kai kurie aukštesnio lygio portalai atskiria sertifikato pagrindu paremtą
prizę nuo žmogui suprantamo tiekėjo alternatyvaus vardo. Autentifikuotą
`whoami`/identiteto atsakymą laikykite autoritetingu: alternatyvus vardas yra
rodomi metaduomenys ir neturi pakeisti sertifikato prizės Zenoh raktuose,
nebent aukštesnio lygio ACL tai aiškiai leidžia.

Bandomojo portalo registras priima tikslius temos raktus, ne Zenoh `*` ar `**`
išraiškas. Todėl didelio dažnio kolekcijos leidėjai identitetą laiko
turinyje ir naudoja stabilius susijusius raktus:

```text
.../aircraft/tracks/v1          JSON
.../aircraft/sapient/tracks/v1  BSI FLEX 335 v2 SAPIENT protobuf
.../aircraft/proto/tracks/v1    šaltinio-specifinis protobuf
```

`publish_collection()` užtikrina vieną kodavimą kiekvienam tiksliam raktui
ADS-B ir sujungtoms orlaivių kolekcijoms. Objekto-lygio temos lieka naudingos
magistralėse, kurių katalogas palaiko šablonus, bet neturi būti vienintelė
išvestis, kai išorinis katalogas reikalauja tikslios registracijos.

### Trečiųjų šalių schemos

`compose/protocols/vendors/sapient/sapient_msg/` neša BSI Flex 335 v2.0
(SAPIENT) `.proto` schemas tiksliai taip, kaip yra
[github.com/dstl/SAPIENT-Proto-Files](https://github.com/dstl/SAPIENT-Proto-Files)
(`bsi_flex_335_v2_0/`) — **šių failų nekeiskite**; tai aukštesnio lygio
sutartys, ir vietinis pakeitimas tyliai atskirtų EFDI laidinio formato nuo
standarto, kurį jis teigia kalbantis. Vietoj to atnaujinkite iš naujo iš
aukštesnio lygio. Licencijuota Apache License 2.0 (žr. `sapient_msg/LICENCE.txt`,
kuri leidžia naudoti, keisti ir platinti, įskaitant komercinį/gynybos
naudojimą, kol licencija ir autorių teisių pranešimai išlaikomi); British
Standards Institution išlaiko BSI Flex 335 nuosavybę ir autorių teises,
publikavimo teises turi BSI Standards Ltd.

Ji gyvena `compose/protocols/vendors/sapient/` kataloge, o ne tiesiogiai
`compose/protocols/proto/`, nes tas katalogas skirtas EFDI *pačios* sutartims,
o tai — kažkieno kito: ji neša savo paketą (`sapient_msg.bsi_flex_335_v2_0`) ir
vidinius importavimo kelius `sapient_msg/bsi_flex_335_v2_0/<file>.proto`, kurie
išsisprendžia tik jei šis katalogas yra savas protoc include root, todėl
`scripts/generate-protobuf.sh` perduoda jį kaip antrą `-I` root šalia `-I
compose`. EFDI ir skaito, ir rašo SAPIENT: `compose/protocols/vendors/sapient/flex335.py`
dekoduoja gaunamą SAPIENT ranka rašytu protobuf skaitytuvu (lauko numeriai
patikrinti pagal šiuos failus) ir tame pačiame faile koduoja siunčiamus
takelius į tikrą `SapientMessage`/`DetectionReport`, todėl vartotojui reikia
suprasti tik SAPIENT, o ne kiekvieną šaltinio protokolą.

### Šaltinio-specifiniai tiltai

| Tiltas | Galinio taško elgsena | Reikalinga konfigūracija |
|---|---|---|
| Bendras UDP | Išsaugo kiekvieną datagramą ir saugiai automatiškai išsiunčia pilnus ASTERIX kadrus | `UDP_INGRESS_PORT`, pasirinktinai bind/multicast/šaltinio filtras ir ASTERIX išsiuntimo kategorijos |
| dronuradaras.lt | Apklausia savo fiksuotą viešą HTTPS API | Nereikia |
| meteo.lt | Apklausia fiksuotą viešą HTTPS API | Pasirinktinai vietos/dažnis |
| SitaWare HQ REST įėjimas | Apklausia diegimui specifinį resursą | URL, kredencialai ir tikras `SITAWARE_API_PATH`; universalaus vienetų URL nėra |
| Takelio sujungimas | Prenumeruoja vietines Zenoh temas | Nėra išorinio galinio taško; pradeda veikti, kai atvyksta normalizuoti takeliai |

#### Radaro operatoriaus UDP relė

Nukopijuokite `scripts/radar_udp_relay.py` į radaro operatoriaus Windows
kompiuterį. Jis neturi trečiųjų šalių priklausomybių. Jei radaras siunčia UDP
į vietinį prievadą 50048, pavyzdžiui, paleiskite:

```powershell
py .\radar_udp_relay.py --listen-port 50048
```

Relė persiunčia kiekvieną datagramą nepakeistą į `asusrog.efdi.ltu:50000`.
Perrašykite `--destination-host`, kai mesh DNS nepasiekiamas. Sukonfigūruokite
šį routerį su `UDP_INGRESS_PORT=50000`. Bendras imtuvas išsaugo kiekvieną
datagramą savo neapdorotoje Zenoh temoje ir automatiškai išsiunčia tik
protokolus, kurių kadravimas nedviprasmiškas — atskiro klausytojo kiekvienai
kategorijai nėra; CAT-034 ir CAT-048 abi dekoduojamos iš to paties bendro
srauto.

EFDI nešiojamame kompiuteryje patikrinkite srautą, neperimant UDP lizdo
nuosavybės:

```bash
./scripts/capture-radar-udp.sh
./scripts/capture-radar-udp.sh any giraffe-50000.pcap
```

Pirma komanda rodo paketo baitus; antra išsaugo pilną paketo fiksavimą
darbui su dekoderiu vėliau. Abi naudoja tcpdump ir gali veikti, kol bendras
UDP įėjimas prijungtas prie prievado 50000.

### Išvesties sluoksniai

| Sluoksnis | Automatinis įėjimas | Išorinė konfigūracija |
|---|---|---|
| CoT/TAK išvestis | Prenumeruoja atitinkamas normalizuotas Zenoh temas | TAK TCP/mTLS serveris, arba ATAK/WinTAK UDP paskirties taškas |
| CoT imtuvas | Konvertuoja prijungtą TAK ar SitaWare CoT srautą į Zenoh | Klausymo prievadas ar nuotolinis serveris; TAK naudoja TAK išduotus mTLS kredencialus |
| SitaWare HQ NVG | Palaiko automatinę normalizuoto takelio momentinę nuotrauką | HQ sukonfigūruotas apklausti EFDI URL; TLS ir dedikuoti kredencialai reikalingi už izoliuotos laboratorijos ribų |

### C2 į Zenoh ir atgal

Išvestis ir įvestis yra atskiros paslaugos. TAK ar SitaWare išvesties
įjungimas tyliai neįjungia atvirkštinio kelio.

#### TAK Server

Zenoh → TAK kryptimi sukonfigūruokite `TAK_HOST/TAK_PORT` ir pasirinkite
`tak_layer` (`layers/tak_layer.py`). Jis prenumeruoja normalizuotas Zenoh
temas ir siunčia CoT per TCP/mTLS į TAK Server. TAK išduoti kliento
kredencialai reikalingi, kai `TAK_TLS=1`. TAK → Zenoh kryptimi pasirinkite
`tak-bridge` (`bridges/tak_bridge.py`), kuris normalizuoja gaunamą CoT srautą
atgal į magistralę. Pirmenybę teikite stabiliam DNS `TAK_HOST`; jei TAK
serverio sertifikatas turi kitokį paveldėtą DNS SAN, nustatykite
`TAK_TLS_SERVER_NAME` į tą SAN, kad vardo tikrinimas liktų įjungtas.

#### SitaWare

Zenoh → SitaWare HQ kryptimi pasirinkite `sitaware_layer`
(`layers/sitaware_layer.py`) ir sukonfigūruokite HQ NVG Import Subscription
apklausti autentifikuotą NVG 2.0.2 srautą, kurį jis teikia.

SitaWare HQ → Zenoh kryptimi gaukite tikrą REST resursą iš diegimo ICD:

```dotenv
SITAWARE_URL=https://sitaware.example
SITAWARE_USER=<vykdymo-vartotojas>
SITAWARE_PASS=<vykdymo-paslaptis>
SITAWARE_API_PATH=/<dokumentuotas-resursas>
SITAWARE_TLS_VERIFY=1
```

Pasirinkite `sitaware`; jis publikuoja normalizuotus vienetus žemiau
`…/{domain}/sitaware/c2/{affiliation}/{entity}/{type}/{id}/sapient`.

Dabartinis vykdymo laikas laiko SitaWare HQ REST ir NVG kelius atskirus. Jei
diegimas eksportuoja NFFI vietoj to, publikuokite pilnus NFFI XML dokumentus
į `…/raw/nffi/{source-id}` ir paleiskite nepriklausomą `nffi` vertėją.

Visi gauti įrašai lieka gaminančio podo temoje. Autorizuoti federacijos
maršrutai gali perduoti tą temą kitiems partnerių routeriams, kurių TAK ir
SitaWare išvesties sluoksniai automatiškai vartoja normalizuotas temas.
Adapteris niekada neturi rašyti tiesiai į kito partnerio temą.

Operatoriaus pusės konfigūracija aprašyta žingsnis po žingsnio
[C2 ↔ Zenoh abikryptė prijungimo instrukcija](09-c2-zenoh-instrukcija.md)
žemiau. Trumpai: TAK Server reikalauja dedikuoto kliento identiteto,
teisingų IN/OUT grupių ir TAK išduoto sertifikato; SitaWare HQ NVG įėjimas
sukuriamas per **SitaWare Communication → NVG → NVG Import Subscriptions**;
o licencijuotam SitaWare CoT Gateway reikia vieno TCP vaidmens, EFDI galinio
taško, patvirtinto eksporto sluoksnių rinkinio ir aiškios `EFDI Live Tracks`
išimties. Produkto ekranų, kurių nėra įdiegtoje licencijoje/leidime, negalima
pakeisti spėjamu REST keliu.

### Kliento SDK — jungimasis prie podo (`clients/`)

Šis skyrius skirtas žmonėms, kurie **vartoja** podą: publikuoja duomenis į
EFDI magistralę ir gauna duomenis iš jos, savo kalba ir įrankiais —
partneriams, integruojantiems su jūsų podu, ne jutikliams/protokolams,
sujungtiems į jį (tai likusi dokumento dalis). Kodas gyvena `clients/`:

```text
clients/
├── connect/             minimalus "sertifikatų komplektas -> Zenoh sesija" pagalbininkas kiekvienai kalbai
├── examples/
│   ├── modern/          idiomatiškas pub/sub/request-reply kiekvienai kalbai
│   ├── military-legacy/ senesnės įrankių grandinės, offline/izoliuotos, failo/HTTP atsarginiai variantai
│   └── bridges/         naudokite protokolą, kurį jau kalbate — jokio Zenoh kodo jūsų programoje
└── README.md
```

| Jūs esate… | Naudokite |
|---|---|
| Modernus kūrėjas (Python/TS/Go/Rust/Java/C++) | `examples/modern/<lang>/` |
| Senesnėje / mažiau paplitusioje sistemoje (C, Java 8, .NET Framework, MATLAB) | `examples/military-legacy/` |
| Kalbate protokolą, kurį jau turite (HTTP, failai) — jokio Zenoh kodo | `examples/bridges/` |
| Tiesiog norite minimalaus prisijungimo fragmento | `connect/<lang>/` |

#### Modelis per 30 sekundžių

Podas veikia kaip **Zenoh routeris**; klientas jam kalba kaip **Zenoh
klientas per mTLS**. Trys operacijos, tiek ir yra visas API:

1. **Publikuoti** (`put`) raktus po **jūsų vardų sritimi** — pvz.,
   `release/<jūs>/sensors/temp`.
2. **Prenumeruoti** (`sub`) raktus, kuriuos leidžiama skaityti — savo, plius
   `release/<partner>/**` duomenims, kuriuos siunčia partneris (dvišalis
   ryšys).
3. **Užklausti** (`get`) naujausios/istorinės rakto reikšmės (pasirinktinai).

Raktai yra brūkšneliais atskirti keliai (`a/b/c`); prenumeratos naudoja `*`
(vienas segmentas) ir `**` (bet koks gylis).

Kiekvienas pavyzdys skaito tuos pačius penkis dalykus iš **aplinkos
kintamųjų**, todėl kredencialai niekada neįkoduojami tiesiogiai:

| Env kintamasis | Kas tai yra | Pavyzdys |
|---|---|---|
| `EFDI_ROUTER` | podo Zenoh galinis taškas | `tls/127.0.0.1:7447` (podas jūsų kompiuteryje) |
| `EFDI_CERT` | jūsų mTLS kliento sertifikatas (PEM) | `/etc/efdi/mycert.pem` |
| `EFDI_KEY` | jūsų mTLS privatus raktas (PEM) | `/etc/efdi/mykey.pem` |
| `EFDI_CA` | CA šaknis, pasirašanti routerį (PEM) | `/etc/efdi/ca-root.pem` |
| `PARTNER_NAMESPACE` | priešdėlis, kurį valdote (publikuokite po juo) | `release/acme` |

`scripts/gen-certs.sh <namespace>` įrašo juos į `compose/certs/`
(`<namespace>-cert.pem`, `<namespace>-key.pem`, `efdi-ca-root.pem`); žemiau
esančiam vartotojui EFDI administratorius perduoda tų pačių trijų failų
kopiją už sistemos ribų. Jei podas yra vartotojo paties mašinoje,
`EFDI_ROUTER` yra `tls/127.0.0.1:7447`; per mesh — to serverio mesh IP.

Taikinys **Zenoh 1.9.0** visur (flotilei fiksuota versija — žr.
`compose/docker-compose.yml`); naudokite atitinkamos didžiosios versijos
kliento biblioteką (`eclipse-zenoh`/`zenoh-c`/`zenoh-cpp`/`zenoh-go`/`zenoh-java`/`zenoh`
crate/`zenoh-ts`, visos 1.x).

#### Vienintelė prisijungimo keblybė (kiekvienas nuosavas ryšys su ja susiduria)

Zenoh TLS konfigūracija turi būti įterpta kaip **vienas visas blokas**
`transport/link/tls`, su **`enable_mtls: true`**. Sub-raktų nustatymas po
vieną (`transport/link/tls/connect_certificate` ir t.t.) tyliai
**neįjungia** kliento sertifikato siuntimo kelio Zenoh 1.x — sesija
atsidaro, bet routeris atmeta klientą, arba jis prisijungia tik skaitymui.
Kiekvienas `connect/` pagalbininkas sukuria *visą* bloką
(`root_ca_certificate` / `connect_certificate` / `connect_private_key` /
`enable_mtls` / `verify_name_on_connect`) kaip vieną dokumentą ir pritaiko
jį vienu iškvietimu — kalbai specifinis mechanizmas skiriasi
(`zc_config_from_str` C kalboje, `Config::from_str` C++, `InsertJson5("transport/link/tls", …)`
Go, `Config.fromJson5` Java, `insert_json5(...)` Rust, vienas
`conf.insert_json5("transport/link/tls", …)` Python), bet taisyklė ta pati
visur.

Taip pat: kai routerio sertifikato SAN sieja **IP/mesh adresą**, o ne DNS
vardą, kuris rinkis, nustatykite `verify_name_on_connect`/`EFDI_VERIFY_NAME`
į `false` (podo vietinis routeris `127.0.0.1` to reikalauja; DNS-vardu
nuotolinis routeris palieka `true`).

#### Tiltai — kalbėkite su podu protokolu, kurį jau kalbate

**Tiltas** yra mažas procesas, kuris pats yra Zenoh mTLS klientas, bet
vartotojui rodo **kitokį protokolą** — HTTP, stebimą katalogą. Programa
niekada nesusieja Zenoh bibliotekos ir neturi jokio Zenoh kodo; ji kalba
protokolą, kurį jau žino, o tiltas atlieka Zenoh dalį. Tai kelias
paveldėtoms/gynybos organizacijoms, kurios negali arba nenori susieti
`eclipse-zenoh`: MATLAB, PLC, senas .NET Framework, Java 8, izoliuotos
sistemos — bet kas, kas gali padaryti HTTP užklausą ar įrašyti failą.

| Naudokite **nuosavą klientą** (`connect/` + `examples/modern/`) | Naudokite **tiltą** |
|---|---|
| Galite susieti Zenoh klientą (Python/Go/Rust/Java/C++) | Negalite susieti (įrankių grandinė, politika, sertifikavimas) |
| Norite mažiausio delsimo, pilno pub/sub/query | Norite jokio Zenoh kodo programoje |
| Moderni kalba, kontroliuoja build | MATLAB / PLC / senas .NET / izoliuota / tik failai |
| Ilgai gyvenančios in-process prenumeratos | "HTTP iškvietimas" ar "failo įrašymas" — tiek ir yra |

Tiltas laiko vartotojo mTLS kliento identitetą, todėl jo paprasto teksto
pusė (HTTP, stebimas katalogas) yra neautentifikuotos durys į magistralę —
paleiskite jį **kartu su vartojančia programa, prijungtą tik prie
`127.0.0.1`**. Jei programa yra kitame serveryje, dėkite tiltą šalia *tos*
programos, nukreiptą į podo mesh IP — pasitikėjimo riba persikelia į
tiltas↔podas ryšį (vis dar mTLS), bet paprasto teksto pusė niekada neturi
būti pasiekiama iš nepatikimo tinklo. Abu žemiau esantys tiltai yra tik
stdlib + `eclipse-zenoh` (jokio web framework, jokios failų stebėjimo
bibliotekos) ir siunčiami su pasirinktinu `Dockerfile`, veikiančiu kaip
compose sidecar.

**`bridges/file-drop/`** — keiskitės duomenimis kaip failais kataloge:
universaliausias kelias MATLAB, PLC/SCADA, paveldėtam .NET, shell
konvejeriams ir visiškai izoliuotiems kraštams. Failas, įrašytas po
`OUTBOX_DIR`, publikuojamas raktu, suformuotu iš jo kelio santykinai su
outbox (`OUTBOX_DIR/sensors/temp` → `<namespace>/sensors/temp`); failas tada
perkeliamas į `OUTBOX_DIR/.sent/`. Gaunami pavyzdžiai, atitinkantys
`SUB_KEYEXPR`, įrašomi į `INBOX_DIR` kaip failai, pavadinti pagal jų raktą
(brūkšneliai → `__`) plius milisekundžių laiko žyma, įrašomi atomiškai
(laikinas vardas, po to pervardijimas), kad skaitytuvas niekada neskaitytų
pusiau įrašyto failo. Apklausa pagrįsta, tik stdlib (jokios inotify
priklausomybės); reguliuokite `POLL_SECONDS` (numatytai 1s); palikite
`SUB_KEYEXPR` tuščią, kad išjungtumėte gaunamą pusę.

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
export OUTBOX_DIR=./outbox INBOX_DIR=./inbox SUB_KEYEXPR='release/<partner>/**'
python3 bridge.py
```

**`bridges/rest-http/`** — paprastas HTTP, `curl`, MATLAB (`webwrite`),
senam .NET (`HttpClient`), Java 8 (`HttpURLConnection`), shell skriptams
arba PLC HTTP blokui. Numatytai prisijungia tik prie `127.0.0.1`
(`BRIDGE_BIND`/`BRIDGE_PORT`).

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
python3 bridge.py                 # veikia http://127.0.0.1:8080

curl -X POST http://127.0.0.1:8080/pub/sensors/temp -d '21.5'                 # publikuoti
curl 'http://127.0.0.1:8080/sub/sensors/temp?count=3'                          # gauti N (blokuoja)
curl -N http://127.0.0.1:8080/stream/sensors/temp                              # SSE srautas
WEBHOOK_URL=https://my-system.local/ingest WEBHOOK_KEYEXPR='release/<partner>/**' python3 bridge.py  # išeinantis webhook
```

Paprastas kelias (`sensors/temp`) apribojamas iškviečiančiojo vardų srityje;
pilnas raktas, kurį iškviečiantysis turi teisę skaityti (pvz.,
`release/<partner>/...`), praeina kaip yra. Gautas tekstas grįžta kaip
`"text"` JSON atsakyme, arba `"b64"`, jei baitai nėra tinkamas UTF-8.

#### Kariniai / paveldėti / mažiau paplitę stekai (`examples/military-legacy/`)

Fiksuotam JDK 8, .NET Framework 4.x, MATLAB, C89/C99 ir **izoliuotoms**
organizacijoms, kurios negali pasiekti interneto, negali `pip install` ir
dažnai negali visai susieti nuosavo Zenoh kliento. Dirbkite iš viršaus žemyn,
sustokite prie pirmos eilutės, kuri teisinga:

| Jei… | Naudokite | Kodėl |
|---|---|---|
| Nuosavas Zenoh bindingas kompiliuojasi ir susieja (turite `zenoh-c`, C kompiliatorių, politika leidžia) | **nuosavas** — `c99/` | mažiausias delsimas, pilnas pub/sub/query, jokio papildomo proceso |
| Galite padaryti **HTTP užklausą** (bet kokia kalba) | **REST tiltas** — `bridges/rest-http/` + `java8/`, `dotnet-framework/`, `matlab/` pavyzdžiai | jokio Zenoh kodo; veikia bet kurioje sistemoje su HTTP klientu |
| Galite tik **skaityti/rašyti failus** (užrakinta dėžė, SCADA/PLC, shell konvejeris) | **file-drop tiltas** — `bridges/file-drop/` + `matlab/receive_filedrop.m` | universaliausias kelias — jei gali įrašyti failą, gali publikuoti |

```text
paveldėta programa  ──HTTP / failai──▶  tiltas (localhost, laiko mTLS)  ──Zenoh mTLS──▶  podo routeris
```

Java/.NET/MATLAB pavyzdžiai **nėra** Zenoh klientai — ~80 eilučių programos,
naudojančios tik kalbos stdlib prieš vietinį tiltą; jos kompiliuojasi
įrankiais, jau esančiais dėžėje (`javac`, `csc.exe`, MATLAB redaktorius),
jokio Maven, NuGet ar Gradle.

**Offline / izoliuota, vieną kartą, visiems keturiems stekams:**

1. Gaukite dalis per sneakernet: patį podą (perduotą operatoriaus), mTLS
   sertifikatų komplektą (`mtls.cert.pem`, `mtls.key.pem`, `ca-roots.pem`,
   vardų sritis) ir — tik tilto Python vykdymo laikui — vendoruotą
   `eclipse-zenoh` wheelhouse:
   ```sh
   # PRIJUNGTOJE mašinoje, atitinkančioje izoliuotos dėžės OS/architektūrą/python:
   pip download eclipse-zenoh==1.9.0 -d zenoh-wheelhouse/
   # perkelkite zenoh-wheelhouse/ per, tada IZOLIUOTOJE dėžėje:
   pip install --no-index --find-links zenoh-wheelhouse/ eclipse-zenoh==1.9.0
   ```
   Pačiai paveldėtai programai vendoruoti nieko nereikia — tai ir yra
   ėjimo per tiltą esmė.
2. Viskas yra localhost: podas, tiltas ir programa visi veikia vienoje
   dėžėje. Jokio DNS, jokio proxy, jokio interneto; vienintelis šuolis yra
   tiltas→podas (`tls/127.0.0.1:7447`).
3. **Laikrodžio sinchronizacija yra tylusis žudikas.** mTLS atmeta
   sertifikatus, kurių galiojimo langas neapima *dabar*. Dėžė su negyva RTC
   ar be NTP nuklys, ir **tilto** sesija podui nepavyks su neaiškia
   "sertifikatas dar negalioja/pasibaigė" klaida — net jei programa→tiltas
   HTTP iškvietimas atrodo gerai. Simptomas: tiltas paleidimo metu užrašo
   TLS klaidą ir niekada neparodo `bridge on http://…`. Pirmiausia
   ištaisykite laikrodį (`date`; `sudo date -s '2026-06-02 14:30:00'` Linux,
   `w32tm /resync` ar rankiniu būdu Windows), prieš derindami bet ką kita —
   vienas taisymas apima podą + tiltą, nes jie dalinasi dėže.

**`military-legacy/c99/`** — grynas C99, tik libc + `libzenohc` + `Makefile`
(pačiam pavyzdžiui CMake nereikia, nors `zenoh-c` build'ui reikia), prieš
[`zenoh-c`](https://github.com/eclipse-zenoh/zenoh-c) 1.9.0 tiesiogiai (jokio
tilto — tai nuosavas klientas). Kiekvienas pavyzdys yra savarankiškas `.c`
su prisijungimo logika viduje. Reikalauja `zenoh-c`, sukompiliuoto su
`-DZENOHC_BUILD_WITH_UNSTABLE_API=ON` (`zc_config_from_str` įėjimo taškas,
kurio reikia vieno-bloko mTLS konfigūracijai, uždarytas už nestabilaus API) —
sukompiliuoto iš šaltinio, iš paruošto GitHub Releases artefakto arba pilnai
vendoruoto (`cargo vendor`) offline buildams.

```sh
make                        # dinaminis susiejimas
make static                 # statinis susiejimas libzenohc.a -> vienas savarankiškas dvejetainis failas
                             # (macOS statiniam susiejimui taip pat reikia -framework Security -framework CoreFoundation)
./publish                   # vienas JSON pavyzdys; ./publish 50 200 50 pavyzdžių, 200ms tarpais
./subscribe                 # viskas jūsų vardų srityje; ./subscribe 'release/<partner>/**'
```

**`military-legacy/java8/`** — JDK 8 (modernus `zenoh-java` bindingas
reikalauja JDK 17+), per REST tiltą naudojant tik `java.net.HttpURLConnection`.
Jokio Maven/Gradle/jar'ų.

```sh
javac Publish.java Subscribe.java
java Publish sensors/temp '{"temp_c":21.5}'
java Subscribe sensors/temp stream          # tęsti nuolat
```

**`military-legacy/dotnet-framework/`** — .NET Framework 4.x (4.5–4.8, ne
modernus .NET), per REST tiltą naudojant `System.Net.HttpWebRequest` (yra nuo
Framework 2.0; nuspėjamesnis nei `HttpClient` atviro SSE srauto atveju).
Kompiliuokite su `csc.exe` tiesiogiai, arba pridedamu klasikiniu (ne-SDK)
`.csproj` per `msbuild` — nė vienas neliečia NuGet.

```bat
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /out:EfdiBridgeClient.exe Program.cs
EfdiBridgeClient.exe pub sensors/temp {"temp_c":21.5}
EfdiBridgeClient.exe stream sensors/temp
```

**`military-legacy/matlab/`** — MATLAB per `webwrite`/`webread` (REST tiltas,
`publish.m`/`receive_rest.m`) arba paprastą failų I/O (file-drop tiltas,
`receive_filedrop.m`) pačioms užrakintoms dėžėms — jokio toolbox, jokio MEX,
jokio tinklo iškvietimo file-drop kelyje.

```matlab
publish('sensors/temp', '{"temp_c":21.5}')
s = receive_rest('sensors/temp', 'Count', 5, 'TimeoutSec', 60);
receive_filedrop('./inbox', 'Callback', @(key,bytes) disp(key))
```

#### Modernūs kalbos bindingai (`examples/modern/`)

Idiomatiškas pub/sub/request-reply kiekvienai kalbai, kiekvienas suporuojantis
oficialų Zenoh bindingą su mažu `connect/<lang>/` pagalbininku, taikančiu
aukščiau esantį vieno-bloko mTLS konfigūraciją. Visi taikosi į Zenoh 1.9.0;
jei simbolis neišsisprendžia kitoje fiksuotoje mažoje versijoje,
persitikrinkite tos žymos pačius aukštesnio lygio pavyzdžius — tai galioja
kiekvienai žemiau esančiai kalbai ir nekartojama kiekvienam įrašui.

**`modern/python/`** — oficialus `eclipse-zenoh`. `pip install eclipse-zenoh`,
tada `python3 publish.py` / `python3 subscribe.py` / `python3 request_reply.py
{serve,get}`. Windows naudokite `python` (ne `python3`) venv viduje, kitaip
pataiko į sisteminį interpretatorių ir meta `ModuleNotFoundError`.

**`modern/cpp/`** — oficialus [`zenoh-cpp`](https://github.com/eclipse-zenoh/zenoh-cpp),
**tik antraštėse esantis wrapper virš `zenoh-c`** — pirmiausia įdiekite
`zenoh-c` 1.9.0 (nestabilus API įjungtas), tada `zenoh-cpp` 1.9.0, tada
CMake-build pavyzdžius. `find_package(zenohcxx)` nepavykimas reiškia, kad
zenoh-cpp nėra `CMAKE_PREFIX_PATH`; susiejimo nepavykimas ties `libzenohc`
reiškia, kad zenoh-c neįdiegtas — nė vienas nėra API problema.

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build
./build/publish; ./build/subscribe
```

**`modern/go/`** — oficialus bindingas (atsirado su Zenoh 1.9.x "Longwang",
2026 m. balandis) yra **cgo wrapper virš `zenoh-c`**, ne grynas Go — pirmiausia
įdiekite `zenoh-c` 1.9.0 (nestabilus API įjungtas), reikalingas
`CGO_ENABLED=1`, cross-kompiliavimas nepatogus. Importavimo kelias
`github.com/eclipse-zenoh/zenoh-go/zenoh` (senasis viršutinio lygio/`zenoh-net`
0.4.x API apleistas nuo 2020 m. — jo nenaudokite). Grynas Go/be-cgo variantas
šiandien neegzistuoja; jei tai griežtas reikalavimas, naudokite tiltą.

```sh
go run publish.go; go run subscribe.go 'release/<partner>/**'
```

**`modern/java/`** — oficialus `zenoh-java` (JDK 17+ — Kotlin/JVM bindingas).
Kitaip nei Go, publikuotas `zenoh-java-jvm` artefaktas **įtraukia nuosavą
biblioteką kaip JAR resursą**, todėl paprastos Gradle priklausomybės
(`org.eclipse.zenoh:zenoh-java-jvm:1.9.0`) pakanka — atskiro `zenoh-c`
diegimo nereikia. Sugeneruokite wrapper kartą su `gradle wrapper`, tada:

```sh
./gradlew run -Pmain=Publish
./gradlew run -Pmain=Subscribe --args="release/<partner>/**"
```

**`modern/rust/`** — oficialus [`zenoh`](https://crates.io/crates/zenoh)
crate — **grynas Rust** (referencinė implementacija, jokios C bibliotekos
diegti nereikia), asinchroninis (tokio). `cargo build` atsineša `zenoh =1.9.0`
kartu su tokio.

```sh
cargo run --bin publish; cargo run --bin subscribe -- 'release/<partner>/**'
```

**`modern/typescript/`** — oficialus `@eclipse-zenoh/zenoh-ts`. **Šis
architektūriškai skiriasi nuo likusių:** jis neatidaro tiesioginės Zenoh
sesijos per mTLS. Jis kalbasi su `zenoh-plugin-remote-api`, įkeltu viduje
`zenohd`, per **WebSocket** (`ws://`/`wss://`); `Config` priima tik lokatoriaus
eilutę, be jokio kliento-sertifikato/TLS bloko TS pusėje. Mesh pusės mTLS
konfigūruojamas **podo `zenohd`**, ryšiuose, kuriuos daro pats routeris —
papildinys yra pasitikėjimo riba tarp WebSocket kliento ir mesh. Podo
operatorius turi įjungti papildinį (`plugins.remote_api.websocket_port`
`zenohd` konfigūracijoje; numatytai neįjungtas); priešais jį dėkite
`wss://` visur, išskyrus loopback. Jei tipizuoto nuosavo API konkrečiai
nereikia, aukščiau esantis REST/WebSocket tiltas dažnai yra paprastesnis
kelias Node/naršyklės vartotojams.

Env kintamieji skiriasi nuo nuosavų bindingų: `EFDI_WS` (pirmenybė; pvz.,
`ws/127.0.0.1:10000`) arba `EFDI_ROUTER` kaip atsarginis variantas (tas
pats serveris su prievadu `10000`); `EFDI_CERT`/`EFDI_KEY`/`EFDI_CA`
nenaudojami, nebent papildinys priešais turi `wss://` su privačiu CA, tokiu
atveju `NODE_EXTRA_CA_CERTS` (Node) juo pasitiki (naršyklėms CA reikia OS/naršyklės
pasitikėjimo saugykloje).

zenoh-ts pirmiausia taikosi į naršyklę/Deno; **Node** aplinkoje reikia
globalaus `WebSocket`, per `ws` paketą (Node 22+ turi jį natūraliai, todėl
shim tampa no-op; Node 18/20 reikia jo) ir įkeliamo prieš pavyzdį per `tsx`.
Deno nereikia jokio polyfill (`deno run --allow-net --allow-env --allow-read
subscribe.ts`) ir yra aukštesnio lygio palaimintas vykdymo laikas, jei Node
shim'as pasirodo trapus. Rakto sprendimas eina per **WASM** modulį —
įsitikinkite, kad bundler/vykdymo laikas gali įkelti `.wasm` (tsx/Deno tai
daro numatytai).

```sh
npm install
npm run publish; npm run subscribe -- 'release/<partner>/**'
```

### Kiti protokolų kandidatai

| Prioritetas | Protokolas | Naudojimas | Vartai prieš įgyvendinimą |
|---|---|---|---|
| Aukštas | ONVIF Profile M | Kameros analitikos objektai, metaduomenys, geolokacija ir įvykiai | Įrenginio profilis, atradimo/autentifikavimo metodas, pavyzdinis metaduomenų srautas. [ONVIF Profile M](https://www.onvif.org/profiles/profile-m/) |
| Vidutinis | VITA 49.2 | Žali RF/spektro stebėjimai | DSP/geolokacijos etapas, konvertuojantis pavyzdžius į žemėlapiui tinkamas kryptis/pozicijas. [VITA Radio Transport](https://www.vita.com/page-1855484) |
| Vidutinis | STANAG 4607 / 4676 | GMTI ir NATO takelių mainai | Licencijuotas ICD/profilis ir reprezentatyvūs pranešimai; neišvesti išdėstymų |
| Tiekėjo-specifinis | Akustinis/RF counter-UAS API | Kryptys, klasifikacijos, takeliai, jutiklio būsena | Tiekėjo ICD/API schema, koordinačių rėmas, laiko bazė, gyvavimo ciklas ir autentifikavimas |

SAPIENT yra pageidautina vieša, tiekėjo-neutrali counter-UAS jutiklio
sąsaja: MOD-valdoma architektūra standartizuota kaip BSI FLEX 335 ir
publikuoja savo protobuf schemas. Žr.
[oficialias SAPIENT gaires](https://www.gov.uk/guidance/sapient-autonomous-sensor-system)
ir [Dstl schemas](https://github.com/dstl/SAPIENT-Proto-Files). Jos TCP
kadravimas yra keturių baitų mažo endian protobuf ilgis, naudojamas
[oficialaus BSI FLEX 335 v2 test harness](https://github.com/dstl/BSI-Flex-335-v2-Test-Harness/blob/main/SAPIENTMessageProcessor/ByteDataMessageBuilder.cs).

### Hakatono partnerio priėmimo kontrolinis sąrašas

Prieš prijungdami srautą, gaukite:

- protokolą, kategoriją, leidimą/profilį, transportą ir srauto kadravimą;
- gamintojo IP/prievadą arba URL/broker plius kas inicijuoja ryšį;
- autentifikavimo/TLS metodą, neįsipareigojant jokių kredencialų;
- reprezentatyvius pranešimus arba anonimizuotą PCAP, apimantį
  create/update/delete;
- koordinačių atskaitą, kilmę/datumą, aukščio atskaitą, kampus ir vienetus;
- laiko žymas/laiko juostą, atnaujinimo dažnį, stabilius identifikatorius ir
  pasenusio/ištrynimo taisykles;
- klasifikacijos/priklausomybės semantiką ir patikimumo skalę;
- laukiamą maksimalų pranešimo dydį, objektų skaičių ir dažnį.

Jei kategorija/leidimas, kadravimas ar koordinačių atskaita nežinomi,
vertėjas turi atmesti ar karantinuoti srautą, o ne tyliai spėti.

---
