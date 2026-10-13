# Steward 2 for ZeroClaw

Steward is the only way your ZeroClaw AI can change your Fedora computer. The AI thinks once and hands Steward a complete plan. Steward then does the rest:

- checks every step against your actual system
- asks for your OK on anything risky
- runs the steps and checks each result
- checks that your goal was actually reached
- writes the verdict itself

It goes back to the AI only when reality doesn't match the plan.

```
you ─ ZeroClaw (any model) ─ steward__plan ─► Steward ─► your system
                                                │
                       approval dialog ◄────────┤  (your OK, not the AI's)
                    records + trash + undo ◄────┘
```

## Why it uses few tokens

- **Six small tools**, about 935 tokens of definitions in total.
- **One call per task.** A typical job is `plan` → final report. Approval happens inside that call (desktop mode).
- **Short plain-text replies.** Command output appears only for `inspect` steps or when something went wrong, and then only the lines that matter. The AI fetches more with `output` if it needs it.
- **Problems are caught before anything runs.** For example: "no user unit trackr-miner…; did you mean tracker-miner-fs-3.service?", "'find' occurs 0 times", or "removing libfoo would also remove foo-app". The AI fixes the plan once instead of failing halfway.
- **Steps already in the wanted state are skipped** and need no approval.

## What the AI sends

```json
{"goal": "free memory and disk",
 "steps": [
   {"do": "inspect", "run": "du -sh ~/.cache/*"},
   {"do": "user_service", "unit": "tracker-miner-fs-3", "op": "disable_now"},
   {"do": "trash", "path": "~/Downloads/old-models"},
   {"do": "dnf_remove", "packages": ["cowsay"]},
   {"do": "shell", "run": "flatpak uninstall --unused -y", "only_if": "flatpak list --runtime | grep -q ."}],
 "verify": ["! systemctl --user is-active tracker-miner-fs-3", "test ! -e ~/Downloads/old-models"]}
```

**Step types:**

- `inspect`, `check`: read-only commands.
- `shell`: any command, approved by you, with your normal user rights. It has no admin rights and no network unless the step asks for `network: true`.
- `user_service`, `system_service`: change a service.
- `kill`: stop a process by name or process ID.
- `trash`, `restore`, `purge_trash`: delete into a restorable trash, bring things back, or empty the trash. `purge_trash {this_plan: true}` frees the space of what the same plan just trashed, with your approval.
- `write_file`, `edit_file`: change files. The old version is always kept.
- `pkg_install`, `pkg_remove`, `pkg_upgrade`, `pkg_clean`: change installed software with dnf, apt, pacman or zypper (chosen automatically). Before removing anything, Steward lists what else would go with it. Every package that changed is recorded.
- `flatpak_remove`: remove a Flatpak app.
- `undo`: reverse a recorded change.

**Options for any step:**

- `expect`: what the output must contain.
- `check`: a read-only command to run afterwards.
- `only_if`: run the step only if this read-only command succeeds.
- `on_fail`: what to do if the step fails (`ask`, `skip` or `stop`).

## Possible replies

| Reply | Meaning |
|---|---|
| `VERIFIED` | Every change was made and the goal checks passed. |
| `UNVERIFIED` | Changes were made but nothing confirmed the goal. |
| `PARTIAL` | Some things went as planned and some didn't. |
| `FAILED` | The goal was not achieved. |
| `UNCERTAIN` | Something may have half-happened, for example a crash mid-step. |
| `STOPPED` | You stopped the plan. |
| `DONE` | A read-only plan finished. |
| `NEEDS FIX` | The plan is wrong. Nothing ran. |
| `NEEDS INPUT` | A step didn't go as planned. The AI answers with `resume`: retry, skip, abort, or replace the remaining steps. Replacement steps that need approval ask you again. |
| `NEEDS APPROVAL` | Chat mode only (see below). |

The AI cannot report success on its own. Steward computes the verdict from the record.

## Approvals

**Every failure means no:** declining, closing the window, a timeout, no screen, zenity missing or crashing, or a failed admin password.

| Mode (`approval_mode`) | How you approve |
|---|---|
| `desktop` (default) | Steward's own window lists every change in full: diffs for file edits, exact commands, sizes, "cannot be undone" warnings. You tick *I approve exactly these changes* and press Approve. |
| `chat` | ZeroClaw's own prompt on whatever channel you use: CLI, Telegram, and so on. Use this when you're away from the computer. Steward writes the wording, refuses the approval if the AI changes a single character, and refuses if ZeroClaw's config would not force the prompt. |
| `both` | Chat prompt, then the desktop window. |
| `terminal` | You run `steward approve PLAN`. |

System services and packages also go through Fedora's admin password prompt (`pkexec`).

An approval covers exactly the steps shown. If a file, folder, service or process changes between your approval and the moment the step runs, Steward stops and asks instead of acting.

## What the AI cannot do

These are enforced by Steward, not left to the AI's judgment:

- **Bypass Steward.** Steward refuses all changes unless ZeroClaw's config gives the agent:
  - no shell, file-writing or delegation tools
  - no other MCP servers or skills
  - no silent model fallback

  `steward doctor` shows the result.
- **Touch credentials or your shell setup.** That covers `~/.ssh`, `~/.gnupg`, keyrings, browser profiles, ZeroClaw's own config, Steward's policy and records, and your shell start-up files. Steps aiming at these are refused before you're even asked.
- **Leave your home folder, use `..` to escape, or redirect through symlinks.**
- **Take down core parts of the system.** Stopping or removing D-Bus, polkit, the firewall, NetworkManager, GNOME Shell, ZeroClaw itself, or core packages (kernel, glibc, systemd, dnf, sudo…) is refused.
- **Change things while "just looking".** Looks run in a sandbox that:
  - can't write anything
  - has no network
  - can't reach systemd or D-Bus
  - can't signal processes
  - can't see other programs' environment variables (where API keys live)

  A short allowlist of read-only system queries (`systemctl status/list-*`, `nmcli … show`, `ss`, `ip … show`, `rpm -q`, `dnf list` and similar) runs directly with fixed arguments.
- **Repeat itself in a loop.** A failing step can be retried at most twice. Identical changes made recently are refused unless the AI marks them `again`. There are hourly and per-plan limits.
- **Delete for good without asking.** Deleting means moving to Steward's trash. Emptying the trash always needs your approval.

Steward also masks secrets (API keys, tokens, passwords, Wi-Fi keys) in everything it stores or shows. The masking is pattern-based (see Limits).

## Plain English

Steward also turns Linux into plain English for you, on Fedora, RHEL-style systems, Ubuntu/Debian/Mint, Arch/EndeavourOS and openSUSE.

**Explain a command before you run it:**

```
$ explain "curl -fsSL https://example.com/x.sh | sudo bash"
In plain English:
  1. It downloads from or sends to a web address: https://example.com/x.sh — ...
  2. Then it passes that output to a command that, as ADMINISTRATOR (root), runs a shell ...
Risk: CRITICAL
Watch out: It RUNS A SCRIPT DOWNLOADED FROM THE INTERNET without you seeing it first.
```

The explanation comes from a hand-written dictionary of 164 commands and 167 sub-commands, covering the package tools of every supported distribution. It is never generated.

- **Unknown parts:** an unknown command, option or sub-command is listed under "I don't know", and the risk is shown as "at least …".
- **Commands it won't analyse:** commands inside commands (`$(...)`), `eval` and script files are flagged, not understood.

**Ask for things in plain English:**

```
ask how much space do I have?
ask what's using my memory
ask update my system
ask install gimp
ask close firefox
ask stop tracker-miner-fs-3 from starting at login
ask empty the trash
```

A fixed set of tested tasks is matched by code, not guessed by a model:

- **Looking:** memory, disk, biggest files, services, start-up, errors, network, ports, installed software, versions, and "is X installed / running".
- **Changing:** update everything, clean the package cache, install, remove, close a program, start/stop/restart a service, stop autostart, empty the Trash.

Every change goes through Steward exactly as before: preflight checks, your approval (in the terminal, or the desktop window), verification and records.

Some requests are never acted on, however they're worded:

- negations ("don't remove…")
- advice questions ("should I…?", "is it safe…?")
- several actions at once
- alternatives ("this or that")
- names that don't exist on your system (it suggests close matches instead)

Anything that isn't a known task is passed to the ZeroClaw assistant **only if you answer yes**, because that uses your paid model.

**Output and errors in plain English:** looks return short English facts instead of raw output. This is also what the AI sees, so it uses fewer tokens. A check that couldn't run is reported as "COULD NOT CHECK", never as "none". Failures are explained (disk full, package not found, lock held, SELinux denial, failing disk…). Errors that aren't in the catalogue are quoted, not guessed.

```
steward english r42      # a stored run's output or error in plain English
```

## Small helper models (optional)

Two tiny local models can assist `ask`:

- **Router:** SmolLM2-135M, about 0.1B parameters.
- **Helper:** Qwen2.5-0.5B, about 0.5B parameters.

They are **aides, not deciders**:

| Aide | Its only job | Why it can't cause harm |
|---|---|---|
| router (0.1B) | When several known tasks fit, pick one | Its answer is forced to that short list. For tasks that change something, it can only ask "did you mean…?". |
| name picker (0.5B) | Pull "gimp" out of "put the gimp editor on here" | The name must appear word-for-word in what you typed, and exist on your system. |
| fact picker (0.5B) | Pick the 2-3 facts that best answer your question | Facts are always shown word for word. Warnings (full disk, failures, checks that couldn't run) are always shown. |

They run only on your computer, through Ollama or llama.cpp's `llama-server`. Any other address is refused.

- **Size:** about 0.6 GB to download, and about 0.7 GB of memory, only while answering.
- **Downloads:** Ollama checks its own downloads. For llama.cpp, each model file is checked against Hugging Face's published SHA-256, and a mismatch deletes it.
- **Exam:** after setup, a fixed exam with known answers runs **on your PC**. If a model scores below the pass mark, the aides stay off.
- **Without them:** everything works without the aides. They only make `ask` a little more flexible.

```
steward aides setup --backend ollama     # or --backend llamacpp
steward aides status                     # on/off and exam scores
steward aides off
ask -v how much memory                   # also show what each aide proposed and whether it was accepted
```

## Install

You need Linux (Fedora, RHEL-style, Ubuntu/Debian, Arch or openSUSE) with `python3` (3.11 or newer), `bubblewrap`, `polkit` and `zenity`. All are normally present on Fedora Workstation; if not:

```
sudo dnf install zenity bubblewrap
```

Then unpack the folder and run:

```
bash steward/install.sh
```

The installer:

1. Puts Steward in `~/.local/opt/steward` with its own Python environment. Nothing is installed system-wide.
2. Adds `steward` and `steward-server` to `~/.local/bin`, plus the short names `ask` and `explain` if nothing else already uses them.
3. Creates `~/.config/steward/policy.json` if it doesn't exist.
4. After asking you, adds a marked block to ZeroClaw's `config.toml`, keeping a backup and your comments. The block adds the MCP server and a ZeroClaw agent called `steward`, with no shell or file tools of its own and with your existing model provider (change it with `--provider family.alias`).
5. Runs `steward doctor` and the full test suite on your machine. The tests use a throwaway folder and fake system tools, so your files are not touched.

To use it:

```
zeroclaw agent -a steward
```

To reach it from Telegram or another channel, add the channel to `[agents.steward] channels` in ZeroClaw's config and set `approval_mode` to `chat` (or `both`). For example:

```
steward setup-zeroclaw --mode chat
```

Steward runs only while ZeroClaw has it open, as ZeroClaw's child process. It doesn't need its own background service. If you want ZeroClaw always on, use `zeroclaw service`.

## Your controls

```
steward doctor          health: sandbox, approvals, ZeroClaw config, model/provider, records
steward plans           recent plans and verdicts
steward show PLAN       every step, approval, result, check, plus the audit trail
steward output rNN      full output of any step (--stderr, --grep)
steward stop PLAN       stop a running plan (current command killed within a second)
steward approve PLAN    approve from the terminal
steward undo rNN        reverse one recorded change (you approve it in the terminal)
steward trash           what is in Steward's trash
```

Records live in `~/.local/share/steward/` (`steward.db` and `trash/`). Your settings live in `~/.config/steward/policy.json`. For example:

```json
{ "approval_mode": "desktop", "auto_roots": ["~/Downloads", "~/.cache", "~/tmp"] }
```

`auto_roots` lists the folders where reversible tidy-ups run without asking.

## Models and cost

Steward works with any model ZeroClaw can use. Change it in ZeroClaw: the `model_provider` setting of `[agents.steward]`.

Steward refuses to act if that provider has `fallback` entries. Those would let ZeroClaw switch to another, possibly paid, model without asking you. `steward doctor` shows which model the agent uses, whether it is local, and whether ZeroClaw's cost limits only warn. To make them block, set `cost.enforcement.mode = "block"` in ZeroClaw.

## Uninstall

```
bash ~/.local/opt/steward/uninstall.sh
```

This removes the program and Steward's block in ZeroClaw's config, which goes back byte-for-byte to how it was. It keeps your records and trash.

- `--purge-data` also deletes the records and settings, but keeps the trash if it still holds your files.
- `--delete-trash-too` deletes the trash as well.

## Limits

See `TEST-REPORT.md` for what was and wasn't tested, and the remaining risks.
