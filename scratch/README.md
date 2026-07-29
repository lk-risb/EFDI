# scratch/

Throwaway verification only. Nothing in this directory is part of the
product, is ever imported by anything under `compose/`, or is expected to
survive between sessions.

Use it for things like:

- one-off scripts cross-checking a decoder against a reference implementation
  before changing production code
- disposable fuzz/property-test harnesses
- pulled-down reference source used only to verify a fix, not to ship

The whole directory is gitignored — nothing here ever reaches a commit or a
shared remote. Delete anything in here at any time; nothing depends on it.
