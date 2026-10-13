# Contributing

Thanks for helping. Steward runs on people's own computers, so its rules are strict.

## The rules

1. **Never lose or corrupt user data.** Deleting goes to the trash. Editing keeps the old version. Every change is recorded and can be undone. A new step type must keep all three.
2. **Fail closed.** If an approval can't be shown, is refused, times out or can't be checked, nothing runs.
3. **Decisions are made by code, not models.** Small local models may only do the narrow, checked jobs in `steward/steward/english/aides.py`. Their output is validated, and they can never start a change.
4. **No secrets in logs, arguments, prompts or errors.**
5. **Offline first.** Nothing may quietly call a paid API or a remote service.
6. **Say what was and wasn't tested.** If your change can't be tested in CI, say so in the pull request and in `steward/TEST-REPORT.md`.

## Set up

```
git clone https://github.com/hmdrysr/zeroclaw-steward.git
cd zeroclaw-steward/steward
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
python -m pytest -q tests
```

The tests use fake versions of `systemctl`, `dnf`, `zenity`, `pkexec` and the other system tools (`steward/tests/fakebin`). They never touch your real system. They need `bubblewrap` installed.

## Before you open a pull request

- `python -m pytest -q tests` passes.
- New behaviour has a test, including the case where it should refuse.
- You don't need to rebuild `dist/` or touch `CITATION.cff`. Once your change is merged, the workflow does both.

- **Changing the ZeroClaw version:**
  - Update `VER` and `PINNED_SHA` in `setup.sh` and the files in `zeroclaw/`.
  - Take the SHA-256 from ZeroClaw's official release only.
  - Say in the pull request where you got it.

## Whole-install test

`tests/fresh_setup.sh` creates a throwaway user, runs the real installer, downloads ZeroClaw, drives a full plan through a fake model, then uninstalls. It also checks that a tampered installer and a tampered ZeroClaw archive are both refused. Run it as root, only in a container or a VM:

```
sudo bash tests/fresh_setup.sh "$PWD" "$PWD/dist/steward-setup.sh"
```

## Project details

`steward/pyproject.toml` is the one place for the project's name, version, description, author, keywords and links. Don't edit copies of these elsewhere. The workflow copies them into:

- `CITATION.cff`
- the README's credit line and paths
- the package version
- the installer's header
- the repository's About box

To check by hand:

```
python3 tools/metadata.py check
```

## Releasing

1. Raise `version` in `steward/pyproject.toml`.
2. Add a section to `CHANGELOG.md` headed `## <version> (YYYY-MM-DD)`.
3. Push to `main`.

Once the tests pass, the workflow rebuilds the installer and publishes a GitHub release with it, its SHA-256 and the changelog notes. The install line in the README always gets the latest release.

## Turning on the workflow (maintainers)

GitHub doesn't let a workflow add workflow files, so the workflow is kept in `ci/tests.yml`. To turn it on:

1. Create `.github/workflows/tests.yml` in the GitHub web editor.
2. Paste in the contents of `ci/tests.yml` and commit.

If `ci/tests.yml` changes later, paste it in again.

The workflow can't change the repository's About box by itself; GitHub reserves that for an admin token. To let it:

1. Create a fine-grained personal access token at <https://github.com/settings/personal-access-tokens/new>. Give it access to this repository only, with **Administration: Read and write**.
2. Add it under **Settings → Secrets and variables → Actions → New repository secret**, named `ABOUT_TOKEN`.

Without the token, everything else still runs.

## Reporting a security problem

See [SECURITY.md](SECURITY.md). Please don't open a public issue for a way around approvals or the sandbox.
