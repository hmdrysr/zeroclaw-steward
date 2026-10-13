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
git clone https://github.com/OWNER/REPO.git
cd REPO/steward
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
python -m pytest -q tests
```

The tests use fake versions of `systemctl`, `dnf`, `zenity`, `pkexec` and the other system tools (`steward/tests/fakebin`). They never touch your real system. They need `bubblewrap` installed.

## Before you open a pull request

- `python -m pytest -q tests` passes.
- New behaviour has a test, including the case where it should refuse.
- If you changed anything the installer ships, rebuild it and commit `dist/`:

  ```
  bash tools/build-installer.sh
  ```

  CI fails if `dist/` is out of date. The build is reproducible: the same source gives the same file, byte for byte.

- **Changing the ZeroClaw version:**
  - Update `VER` and `PINNED_SHA` in `setup.sh` and the files in `zeroclaw/`.
  - Take the SHA-256 from ZeroClaw's official release only.
  - Say in the pull request where you got it.

## Whole-install test

`tests/fresh_setup.sh` creates a throwaway user, runs the real installer, downloads ZeroClaw, drives a full plan through a fake model, then uninstalls. It also checks that a tampered installer and a tampered ZeroClaw archive are both refused. Run it as root, only in a container or a VM:

```
sudo bash tests/fresh_setup.sh "$PWD" "$PWD/dist/steward-setup.sh"
```

## Turning on CI

GitHub doesn't let workflows add workflow files, so the test workflow is kept in `ci/tests.yml`. To turn it on:

1. Create `.github/workflows/tests.yml` in the GitHub web editor.
2. Paste in the contents of `ci/tests.yml` and commit.

It runs the tests in a Fedora container, checks that `dist/` is current, and checks the shell scripts' syntax.

## Reporting a security problem

See [SECURITY.md](SECURITY.md). Please don't open a public issue for a way around approvals or the sandbox.
