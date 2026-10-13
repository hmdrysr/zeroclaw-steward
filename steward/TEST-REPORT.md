# Steward 2 test report

**Date:** 14 October 2026

**Where it ran:** Claude's cloud sandbox, Ubuntu 24.04 with Python 3.13 and bubblewrap. Everything ran as an unprivileged user except where noted. **Nothing here ran on your Fedora 43 machine yet.** `install.sh` reruns the automated suite there.

**ZeroClaw:** the real release binary v0.8.5 (`zeroclaw-x86_64-unknown-linux-gnu`, SHA-256 checked against the release's SHA256SUMS). An OpenAI-compatible stand-in model plays the AI's part.

## How to rerun

| Suite | Command | Result here |
|---|---|---|
| Engine, sandbox, policy, approvals, undo, recovery (unit and adversarial) | `python -m pytest tests/test_engine.py` | 87 passed |
| MCP stdio protocol (server launched as ZeroClaw does) | `python -m pytest tests/test_mcp.py` | 4 passed |
| ZeroClaw config checks, setup and removal | `python -m pytest tests/test_zeroclaw_config.py` | 16 passed |
| Distributions: detection, and package steps on dnf/apt/pacman/zypper | `python -m pytest tests/test_distros.py` | 24 passed |
| Linux→English: explainer, output translators, errors, request matching (incl. fuzzing) | `python -m pytest tests/test_english.py` | 141 passed |
| `ask` end to end, and the small-model aides against a deliberately misbehaving stand-in model | `python -m pytest tests/test_ask_aides.py` | 27 passed |
| Real ZeroClaw end to end | `python tests/zc_e2e/run_e2e.py /path/to/zeroclaw` | 36 checks passed |
| Clean install → real use → uninstall of Steward, as a new user (needs root) | `bash tests/clean_install/run.sh SRC ZEROCLAW` | 13 checks passed |
| Whole bundle on a fresh user: ZeroClaw install, fresh config, encrypted key, use, uninstall, tampered archive (needs root) | `bash tests/fresh_setup.sh BUNDLE` (in the bundle) | 21 checks passed |
| Token saving of plain-English looks on the machine it runs on | `python tests/measure_tokens.py` | 74% fewer tokens here |

All 299 automated tests passed together, and on repeat runs.

## Your checklist

| Required result | Status | Evidence |
|---|---|---|
| User rejects an approval → action does not execute | **Passed** | Desktop mode: `test_approval_failures_block[decline]`, e2e "desktop approval declined". Chat mode, real ZeroClaw prompt answered No: e2e "user answers No". Terminal mode: `test_terminal_mode_waits_for_user`. |
| Approval times out → does not execute | **Passed** | `[timeout]`, `test_approval_hang_is_a_timeout` (a frozen dialog is killed and counts as no), and the e2e test where the ZeroClaw prompt gets no answer. |
| GUI or authentication fails → blocked | **Passed** | No screen, zenity missing, zenity crashing, and a dismissed admin password (`test_admin_password_failure…`). The password test used a stand-in pkexec (see below). |
| ZeroClaw tries a direct shell bypass → denied | **Passed** | e2e: the model called ZeroClaw's own `shell`, `file_write` and `delegate`. All got "Unknown tool" and no file appeared. Steward also refuses to act on configs that would allow them (`test_unsafe_agent_configs_block_changes`). |
| Steward is stopped → no unguarded fallback | **Passed** | e2e: with Steward's server missing, the agent had only read-only tools and nothing ran. |
| Local model becomes unavailable → no silent paid-API fallback | **Passed** | e2e: with the model endpoint down and no fallback configured, ZeroClaw failed and a second "paid" endpoint got 0 requests. With a fallback configured, Steward blocks all changes. |
| Path traversal or unsafe symlinks → blocked | **Passed** | `..`, absolute paths outside home, a symlinked folder pointing into `~/.ssh`, a folder swapped for a symlink after approval, and a symlink being trashed (only the link moves). |
| Process crashes mid-job → recoverable, honest outcome | **Passed** | `test_server_crash_mid_step…` kills the real server mid-step. The step is recorded as interrupted with "changed: unknown", no stray process is left behind, nothing reruns by itself, remaining changes need approval again, and the verdict is UNCERTAIN. |
| Destructive action fails halfway → partial completion recorded | **Passed** | `test_partial_completion_is_reported_honestly`: the verdict is PARTIAL, step 1's change is recorded and step 3 never ran. `dnf` changes are recorded as an exact before/after package list. |
| User cancels a running job → work stops safely | **Passed** | `test_user_stop_halts_running_plan`: the running command is killed, later steps don't run, and the verdict is STOPPED. |
| Undo requested → restoration tested, not just reported | **Passed** | The tests check actual state after undo: identical file bytes after trash, old file content after an edit, a service back to running and enabled, a package back installed. Undoing twice is refused. |
| Malformed or adversarial agent output → rejected | **Passed** | 16 kinds of bad plan, schema violations at the protocol level, tampered approval wording, a wrong seal, replaying an approval, and Steward-only fields injected by the AI. |
| Unauthorised network access → blocked | **Passed** | Looks and shell steps have no network (checked by trying to connect). `network: true` must be approved and is shown in the dialog. |
| Service installed on a clean system → install and use work | **Passed, but on Ubuntu** | Clean-install test as a new user: installed, then a real ZeroClaw → Steward plan was approved and verified. Not yet run on Fedora 43. |
| Service uninstalled → Steward's own files removed safely | **Passed** | Program removed. ZeroClaw's config is byte-identical to before the install. Every other file in the user's home is unchanged (SHA-256 compared). A non-empty trash is kept unless you explicitly ask to delete it. |

### Further adversarial and isolation tests that passed

- A file replaced, or edited by something else, between approval and execution is not touched.
- A process ID reused by a different process is not killed.
- A process that had already exited is not claimed as "stopped by Steward".
- Looks cannot write, cannot signal processes, cannot reach D-Bus, cannot see secret folders, and cannot see other programs' environment variables.
- Read-only queries that hide tricks are refused: `rpm --eval '%(…)'`, `dnf -c evil.conf`, `nmcli … --show-secrets`, and `systemctl … ; rm -rf ~`.
- Shell steps cannot change Steward's policy or records, cannot read secret folders, run with no-new-privileges, and anything left in the background is killed.
- A step that times out has its whole process tree killed.
- Secrets are masked in stored output, including API keys and Wi-Fi keys.
- A step that keeps failing can be retried twice, then is refused. Identical recent changes are refused.
- In chat mode, "Always" in ZeroClaw doesn't stop later approval prompts.

## Your release gates

| Gate | Met? |
|---|---|
| ZeroClaw cannot bypass Steward's execution controls | Yes, for the agent that `setup-zeroclaw` creates. This is enforced by checking ZeroClaw's config: Steward refuses to act if the agent has other ways to execute. It does not cover a *different* ZeroClaw agent you configure with shell access. |
| Every protected action is subject to independently enforced policy | Yes |
| Approval rejection, timeout and failure all block execution | Yes |
| Jobs have persistent state, cancellation, verification and recovery | Yes |
| Filesystem, process and network restrictions are tested | Yes |
| Audit records accurately describe what happened | Yes. Each plan has an append-only event trail, and `steward show` prints it. |
| Local-model failure cannot silently incur paid API charges | Yes, as far as Steward can control it: it blocks provider fallbacks. You are using hosted models now, so normal use costs per token. Setting `cost.enforcement.mode = "block"` in ZeroClaw adds a hard spending cap; `steward doctor` reminds you. |
| End-to-end adversarial tests pass | Yes, in the sandbox |
| Installation, diagnostics and uninstall are verified | Yes, in the sandbox, not on Fedora |

## Plain English and the small models

**Tested here:**

- **Explainer (49 known-answer cases, plus fail-safe cases):** risk levels for everyday and dangerous commands, including `rm -rf /`, `curl … | sudo bash`, `dd of=/dev/sdb` and fork bombs. Unknown commands and options are reported as unknown. Unreadable commands are shown as UNKNOWN risk. Adding `sudo` or an unknown option never lowers the risk.
- **Output translators:** checked against real output from this machine and against Fedora-style samples (btrfs subvolumes, a full USB disk, program names with spaces). A failed check is never reported as "none".
- **Error catalogue:** examples from all four package tools, systemd, SELinux, network and disk errors. Unknown errors are quoted, never guessed. No hint ever suggests switching off a protection.
- **Request matching:** 27 paraphrases map to the right task. 22 adversarial requests are never acted on, and 700 generated negated or question-form requests never trigger a change.
- **`ask` end to end:** install (declined, then approved), remove with dependents, update on all four distribution families, service name suggestions, closing a program, permanently emptying the Trash, and escalation only with consent.
- **Aides, against a stand-in model that misbehaves on purpose:**
  - Requests are schema-forced and deterministic (temperature 0).
  - Out-of-list, garbled, wrongly typed and hung answers are ignored.
  - The router can never start a change.
  - A name that wasn't in the request is rejected.
  - The fact picker cannot drop warnings, repeat or invent facts.
  - Non-local endpoints are refused.
  - The on-PC exam enables the aides only when they pass.
  - A model download with the wrong checksum is deleted.

**Bugs these tests found and that are now fixed:**

- A failed check was summarised as "none".
- Disks sharing a device name were merged, mixing their numbers.
- A negated check (`! …`) could turn "couldn't reach systemd" into a pass.
- A sandbox that failed to start could also be turned into a pass.
- The fact picker treated "Failed services: none" as a warning.

**Not testable here:** the real small models. Hugging Face and Ollama are blocked in this sandbox, so no model was downloaded or run. How well SmolLM2-135M and Qwen2.5-0.5B actually do is first measured by the exam on your PC, and they stay off if they fail it. Even with them on, nothing they produce can change your system without passing the same checks and your approval.

## Not tested

- **Real package changes on Debian/Ubuntu, Arch and openSUSE.** The apt backend's read-only queries were run against the sandbox's real Ubuntu `dpkg`/`apt`; installs, removals and upgrades used stand-ins on every distribution.
- **On Fedora 43 itself.** Real `systemctl`, `pkexec` (the real polkit password window), `dnf`, `rpm` and `flatpak` were replaced with stand-ins that behave like them. The real `zenity` window was not shown, because the sandbox has no screen. A stand-in returned approve, decline, timeout, crash or a frozen window.
- **ZeroClaw approvals on Telegram, Discord and other channels.** Only the CLI prompt was exercised. Other channels use the same approval gate inside ZeroClaw, but that wasn't observed.
- **Newer ZeroClaw versions.** Tested with v0.8.5, the latest release; their development branch is newer. A config-format change could make `steward doctor` fail the checks. That fails closed (Steward blocks changes), not open.
- **A real AI model driving the tools.** The stand-in model sends fixed tool calls. How well a given model writes plans was not measured.

## Remaining risks

1. **Chat mode trusts ZeroClaw to show the prompt.** Steward checks that ZeroClaw's config forces it, and that the wording wasn't changed, but it cannot see the prompt itself. Desktop mode, the default, doesn't have this dependency.
2. **Approved shell commands run with your normal rights.** They can't gain admin rights, reach the network (unless approved), or touch secrets or Steward's records, but they can change anything else you own. Steward can't tell what a free-form command changed, so it relies on goal checks. A shell change with no passing goal check is reported as UNCERTAIN.
3. **Secret masking is pattern-based.** A secret printed in an unusual format could still appear in stored output. Hiding other processes' environment variables covers processes that exist when a command starts; one started during a look is not covered.
4. **Read-only queries run outside the sandbox.** The allow-listed queries (`systemctl` status and list commands, `nmcli … show`, `ss`, `ip … show`, `rpm -q`, `dnf list`…) need system access, so their safety rests on the allowlist. It was reviewed and tested against known tricks, not proven complete.
5. **ZeroClaw's own `file_read`** is limited to its workspace by the setup (`workspace_only = true`). If you widen that, the AI could read files Steward would hide. `steward doctor` warns about this.
6. **Some recovery decisions stay with the AI.** After a crash, Steward never reruns anything by itself and asks for approval again. But it is the AI that decides whether to retry, skip or abort, so check `steward show PLAN` after any UNCERTAIN result.
