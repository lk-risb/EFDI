# MIP / STANAG 4778 (Metadata Binding) sources

This directory has no decoder for MIP (the Multilateral Interoperability
Programme) or STANAG 4778 itself — nothing in `compose/protocols/vendors/*`
parses a MIP exchange or a STANAG 4778 metadata-binding document. This file
exists because the *vocabulary* STANAG 4778 defines for classification
metadata (confidentiality labels) turned out to be the concept underlying
fields two other real, implemented protocols carry, and is worth tracking
as its own source rather than leaving it buried as a footnote in whichever
file happened to need it first.

## Source

`4778.xsd` (`urn:nato:stanag:4778:bindinginformation:1:0`), authored by
**NC3A** (the NATO Consultation, Command and Control Agency — the same body
behind the NFFI 1.4 XSD in `../nffi/NFFI.md`), titled "Metadata Binding
Schema" — *"Used within NATO to bind metadata to data objects, including
the NATO Core Metadata."* Found 2026-09-06 among real MIP4/NCDF (NATO Core
Data Framework) canonical-schema files inside the user's own INT-CORE
installation media (`~/Downloads/INTCORE/INTCORE5/IntCoreMappingUtilities`
— a real customer's deployed integration-mapping project for a NATO C2
interoperability platform; see `../sitaware/SITAWARE.md`'s INT-CORE
section for how that media was found and what else came out of it).

## What it defines

A generic XML-dsig-based mechanism (`BindingInformation` →
`MetadataBindingContainer` → `MetadataBinding` → `Metadata`/`Data`) for
attaching arbitrary metadata to a data object, with a documented example
use case of exactly this: a `ConfidentialityLabel` carrying
`ConfidentialityInformation`:

- `PolicyIdentifier` — the security policy body (e.g. `NATO`)
- `Classification` — the sensitivity level (e.g. `UNCLASSIFIED`)
- `Category` — additional sensitivity/dissemination/informational markings

**This is not implemented here** — the full binding mechanism (XML digital
signatures, arbitrary metadata/data references) is a separate, much larger
transport concern than anything this project currently decodes.

## Where the vocabulary is actually used in this project

NFFI 1.4's `secPolicyName`/`secClassification`/`secCategory` attributes
(present on every section of a `<track>` — see `../nffi/NFFI.md`) are a
flat, non-XML-dsig instance of exactly this three-part model —
`PolicyIdentifier`→`secPolicyName`, `Classification`→`secClassification`,
`Category`→`secCategory`. `nffi.py` decodes these into
`nffi_sec_policy`/`nffi_sec_classification`/`nffi_sec_category`, and copies
`secClassification`/`secCategory` onto generic `classification`/
`classification_caveat` keys — deliberately generic because this
vocabulary is NATO Core Metadata in general, not an NFFI-specific
invention, so any future decoder that carries classification data should
set the same two keys rather than inventing its own. Those two keys reach
both C2 egress paths: `tak_layer.py` sets CoT's real `access`/`caveat`
event attributes (see `../tak/TAK.md`), and `sitaware_layer.py` sets NVG's
real root `classification` attribute (see `../sitaware/SITAWARE.md`) — NVG
has no `caveat` equivalent, so `classification_caveat` only reaches CoT.

## What hasn't been verified

No real STANAG 4778 binding document, and no real classified NFFI traffic,
has been received by EFDI to date. The three-field correspondence above is
a structural reading of both schemas' own documentation text, not a
live-traffic confirmation.
