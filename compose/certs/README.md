# Certificate bundle layout

The directory structure and README files are tracked. Every certificate,
private key, chain, and generated credential below this directory is ignored by
Git. Never force-add one.

Copy each identity into its documented fixed-name folder:

| folder | purpose | preparation |
|---|---|---|
| `efdi/` | this router's local EFDI identity | staged by `host/first-boot.sh` |
| `efdi-ltu/` | LTU fabric client/server identity | run `scripts/connect-ltu.sh` |
| `efdi-backbone/` | Backbone client identity | staged by `host/first-boot.sh` |

The three profiles use different certificate authorities. A certificate from
one profile cannot authenticate to another profile.

TAK and SitaWare credentials may use sibling `tak/` and `sitaware/` directories,
but those integration-specific directories and all of their contents remain
entirely local.

`BUNDLE_DIR` may point at an equivalent directory outside the repository. The
same fixed filenames apply there.
