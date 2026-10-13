# Changelog

## 2.0.0 (2026-10-14)

First public release.

- Steward 2 for ZeroClaw 0.8.5:
  - plan-then-execute engine
  - desktop, chat or terminal approvals that fail closed
  - read-only sandbox for looking around
  - trash, backups and undo for every change
  - SQLite audit trail
  - a gate that refuses to run if ZeroClaw could act without Steward
- Works on Fedora, Debian/Ubuntu, Arch and openSUSE families (dnf, apt, pacman, zypper).
- Plain English: `ask`, `explain`, plain-English errors and output summaries.
- Optional small local helper models (SmolLM2-135M, Qwen2.5-0.5B), checked by an exam on your machine before use.
- One-file installer (`dist/steward-setup.sh`), built reproducibly. It downloads ZeroClaw and checks it against a pinned SHA-256.
- Automatic upkeep: tests on every push. Once they pass, the workflow rebuilds the installer, keeps version, author and links in step with `steward/pyproject.toml`, and publishes a release for each new version.
