# ZeroClaw + Steward

Let an AI look after your Linux computer without handing it the keys.

- **[ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw)** is the AI agent you chat with. It works with any model provider.
- **Steward** is the only way that AI can change your computer. The AI hands Steward a complete plan. Steward checks every step against your real system, asks for your OK on anything risky, runs the steps, checks the result and keeps a record of everything. Deleted files go to a trash you can restore from; edited files keep their old version; changes can be undone.
- **Plain English:** `ask` and `explain` turn Linux into plain English on your own computer, without spending tokens. The AI also gets short English facts instead of raw command output, so it uses fewer tokens.
- **Small local helper models (optional):** two tiny models that help `ask`. They are tested on your computer first and stay off if they don't pass.

Made for Fedora; also works on Debian/Ubuntu, Arch and openSUSE families. Linux x86-64 only.

## Install

Open a terminal and paste:

```
curl -fsSLo /tmp/steward-setup.sh https://raw.githubusercontent.com/hmdrysr/zeroclaw-steward/main/dist/steward-setup.sh && bash /tmp/steward-setup.sh
```

The installer checks itself before running. If it was damaged or changed, it stops and installs nothing. It doesn't run through `curl … | bash`, because it needs a terminal to ask you questions.

To check the download yourself first, compare it with [`dist/steward-setup.sh.sha256`](dist/steward-setup.sh.sha256):

```
cd /tmp && curl -fsSLO https://raw.githubusercontent.com/hmdrysr/zeroclaw-steward/main/dist/steward-setup.sh.sha256 && sha256sum -c steward-setup.sh.sha256
```

### What the installer does

1. **Checks your computer:** Python 3.11+, bubblewrap, zenity, polkit.
2. **Installs ZeroClaw 0.8.5** into `~/.local/opt/zeroclaw-0.8.5`, downloaded from its official GitHub release and refused unless it matches a pinned SHA-256.
3. **Configures ZeroClaw:**
   - Keeps your existing config, or starts fresh by moving the old one aside. Nothing is deleted.
   - Asks for your provider and model.
   - Sets spending caps that block, not just warn.
   - Your API key goes into ZeroClaw's own masked prompt and is stored encrypted. The installer never sees it.
4. **Installs Steward** and adds a ZeroClaw agent called `steward` that has no shell of its own. Steward's full test suite then runs on your machine, using fake files.
5. **Optionally** sends one test message to your model, and sets up the small helper models.

Nothing runs as root and nothing is installed system-wide.

### Options

Add these after `bash /tmp/steward-setup.sh`:

- `--provider openrouter|xai|anthropic|openai|ollama|custom`
- `--model ID`, `--uri URL`
- `--daily USD`, `--monthly USD`
- `--mode desktop|chat|both|terminal` (how you approve plans)
- `--keep-config` or `--fresh-config`
- `--aides` or `--no-aides`
- `--no-model-test`, `--skip-tests`

## Use

```
ask how much space do I have?
ask update my system
explain "sudo dnf remove -y something"
zeroclaw agent -a steward          # chat with the AI; it acts only through Steward
steward plans                      # what was done, and when
steward undo <plan>                # reverse a plan's recorded changes
steward doctor                     # health check
```

## Remove

```
bash ~/.local/src/zeroclaw-steward-2.0.0/uninstall-all.sh
```

This removes the programs and Steward's entries in ZeroClaw's config. It keeps your ZeroClaw folder (config, encrypted key, memory), plus Steward's records and trash, unless you ask otherwise:

- `--remove-zeroclaw-config` moves the ZeroClaw folder aside.
- `--purge-steward-data` deletes Steward's records. It still keeps a trash that has your files in it, unless you also pass `--delete-trash-too`.

## What's here

| Path | What |
|---|---|
| `steward/` | Steward: source, tests, its own [README](steward/README.md) and [test report](steward/TEST-REPORT.md) |
| `setup.sh`, `uninstall-all.sh` | Install and remove ZeroClaw + Steward |
| `zeroclaw/` | ZeroClaw's official `SHA256SUMS` and licences. The binary itself is downloaded at install time. |
| `dist/` | The one-file installer, built from this repository by `tools/build-installer.sh` |
| `tests/` | Whole-install test on a fresh user account (needs root; use a throwaway machine or container) |
| `ci/` | GitHub Actions workflow for tests (see [CONTRIBUTING](CONTRIBUTING.md)) |

## Safety and honesty

Read [`steward/TEST-REPORT.md`](steward/TEST-REPORT.md) for what was tested, where it was tested, and what wasn't. In short:

- **Tested:** everything was tested in clean Linux containers with fake system tools.
- **Not tested here:** real Fedora desktops, real approval dialogs, and the helper models running on real hardware.

Please report what happens on your machine.

## Licence

Steward and the scripts in this repository: MIT ([LICENSE](LICENSE)). ZeroClaw: MIT or Apache-2.0, at your option, by its authors ([zeroclaw/](zeroclaw/)).
