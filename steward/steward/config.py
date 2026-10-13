"""Paths and policy.

Policy lives in ~/.config/steward/policy.json (optional, JSON, edited only by
you). Nothing the AI can do reaches that file: typed steps refuse it and
shell steps see it read-only.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("STEWARD_TEST_HOME") or Path.home())


def _env_path(var, default):
    return Path(os.environ[var]) if os.environ.get(var) else default


DATA_DIR = _env_path("STEWARD_DATA", HOME / ".local" / "share" / "steward")
CONFIG_DIR = _env_path("STEWARD_CONFIG", HOME / ".config" / "steward")
DB_PATH = DATA_DIR / "steward.db"
TRASH_DIR = DATA_DIR / "trash"
INSTALL_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Policy:
    # Reversible changes here run without asking (trash, write, edit).
    auto_roots: list = field(default_factory=lambda: ["~/Downloads", "~/.cache", "~/tmp"])
    # Never readable by sandboxed commands, never changeable.
    secret_paths: list = field(default_factory=lambda: [
        "~/.ssh", "~/.gnupg", "~/.local/share/keyrings", "~/.password-store",
        "~/.mozilla", "~/.config/google-chrome", "~/.config/chromium",
        "~/.config/BraveSoftware", "~/.netrc", "~/.aws", "~/.kube", "~/.docker/config.json",
        "~/.git-credentials", "~/.pki", "~/.config/goose", "~/.zeroclaw", "~/.grok",
        "~/.config/gh", "~/.local/share/zeroclaw"])
    # Readable, never changeable by the AI.
    protected_paths: list = field(default_factory=lambda: [
        "~/.config/steward", "~/.local/share/steward", "~/.bashrc", "~/.bash_profile",
        "~/.profile", "~/.zshrc", "~/.config/autostart", "~/.config/systemd",
        "~/.local/bin", "~/.config/environment.d", "~/.local/opt/steward"])
    # Loop and blast-radius limits.
    max_steps_per_plan: int = 60
    max_changes_per_plan: int = 40
    max_revisions_per_plan: int = 8
    max_retries_per_step: int = 2
    max_plans_per_hour: int = 40
    repeat_window_secs: int = 900          # identical change done recently -> asks AI to confirm
    approval_valid_secs: int = 1800         # a plan waiting longer must be resubmitted
    default_timeout: int = 120
    max_timeout: int = 900
    wait_secs: int = 420                    # max time one tool call blocks (ZeroClaw caps at 600)
    max_stored_bytes: int = 4 * 1024 * 1024
    look_lines: int = 60
    shell_max_chars: int = 400
    allow_network_shell: bool = True        # a shell step may ask for network (shown in approval)
    # How Steward gets your OK for changes: "desktop" (its own zenity dialog,
    # default), "chat" (ZeroClaw's prompt on your chat channel, verified via
    # ZeroClaw's config), "both", or "terminal" (`steward approve`).
    approval_mode: str = "desktop"
    approval_timeout_secs: int = 300
    require_zeroclaw_gate: bool = True      # refuse changes unless ZeroClaw's config is safe
    allow_other_mcp: bool = False           # other MCP servers granted to the same agent
    allow_skills: bool = False              # skill bundles on the same agent (may run shell)
    forbid_model_fallback: bool = True      # provider fallbacks could silently switch to paid models
    zeroclaw_config: str = ""               # set by the installer; "" = ~/.zeroclaw/config.toml


def expand(p) -> Path:
    s = str(p)
    if s.startswith("~"):
        s = str(HOME) + s[1:]
    return Path(s)


def load_policy() -> Policy:
    pol = Policy()
    f = CONFIG_DIR / "policy.json"
    if f.exists():
        data = json.loads(f.read_text())
        for k, v in data.items():
            if not hasattr(pol, k):
                raise ValueError(f"policy.json: unknown key {k!r}")
            setattr(pol, k, v)
    for p in (str(CONFIG_DIR), str(DATA_DIR), str(INSTALL_DIR)):
        if p not in pol.protected_paths:
            pol.protected_paths.append(p)
    if pol.zeroclaw_config == "":
        zc = os.environ.get("ZEROCLAW_CONFIG_DIR")
        pol.zeroclaw_config = str(Path(zc) / "config.toml") if zc else \
            str(HOME / ".zeroclaw" / "config.toml")
    zdir = str(Path(pol.zeroclaw_config).parent)
    if zdir not in pol.secret_paths:          # ZeroClaw's config/secrets: never readable or writable
        pol.secret_paths.append(zdir)
    return pol
