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
| `udp-ingress` | `bridges/udp_ingress_bridge.py` | `…/raw/udp/ingress` ir atpažintas `…/raw/asterix/catNN` | Bendras UDP 50000 srautas |
| `asterix-cat10/20/21/34/48/62` | `protocols/vendors/asterix/cat.py --category NN` | ASTERIX kategorijai skirta normali tema | Tiesioginis UDP/TCP arba viena neapdorota Zenoh kategorijos tema procesui |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | Tik prisijungusių įrenginių apklausa 60 s ir atsijungusių pašalinimas / aptikimų apklausa 10 s |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Konfigūruojama REST apklausa |
| `nffi` | `protocols/random/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Pilni XML dokumentai Zenoh temoje `…/raw/nffi/*` |
| `tak-layer` | `layers/tak_layer.py` | Prenumeratorius — visos temos | Įvykio valdomas |
| `sitaware-hq-nvg` | `layers/sitaware_hq_nvg_feed.py` | Prenumeratorius — visos takelių temos | HQ periodiškai ima NVG būseną |
| `track-fusion` | `protocols/fusion.py` | CAT-48 + CAT-21 prenumeratorius | Įvykio valdomas |

### TAK naudotojai ir SitaWare HQ technika

Aktyvus CoT kelias yra `layers/tak_layer.py`: jis prenumeruoja normalizuotas
Zenoh temas ir siunčia CoT į `tak_layer` paskirties TAK Server. Naudokite TAK
išduotą kliento sertifikatą, kai įjungtas `TAK_TLS=1`. Dabartiniame EFDI
runtime nėra atskiro TAK arba SitaWare CoT priėmimo tilto. Jei konkretus
diegimas teikia NFFI, pilnus XML dokumentus skelbkite į
`…/raw/nffi/{source-id}` per prijungtą Zenoh mazgą.

CoT ir abi SitaWare NVG išvestys naudoja tą pačią scenarijaus priklausomybės
taisyklę: orlaiviai iš nustatytų RU/BY ICAO adresų intervalų bei laivai su RU/BY
MMSI MID žymimi kaip priešiški, o kiti partnerių oro/jūros kontaktai — neutralūs.
Vien šalies pavadinimas nepakeičia trūkstamo arba negaliojančio atsakiklio ID.
