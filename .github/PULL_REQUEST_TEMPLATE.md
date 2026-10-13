**What this changes**

**How it was tested**
- [ ] `python -m pytest -q tests` passes (in `steward/`)
- [ ] New behaviour has a test, including the case where it should refuse
- [ ] `dist/` rebuilt with `bash tools/build-installer.sh` (if shipped files changed)

**What wasn't tested**
(for example: real Fedora desktop, real approval dialog, a distribution you don't have)

**Data safety**
Can this change delete, overwrite or lose anything? If so, how is it kept recoverable?
