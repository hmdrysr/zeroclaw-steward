"""Adds/removes Steward's entries in ZeroClaw's config.toml.

Adding appends a clearly marked block (your comments and layout are kept),
after a timestamped backup, and only if the merged file still parses.
Removing cuts that block; if ZeroClaw has rewritten the file and the markers
are gone, the entries are removed structurally (comments may be lost; a
backup is kept either way).
"""
import hashlib
import shutil
import time
from pathlib import Path

import tomllib

BEGIN = "# >>> steward: managed block (added by `steward setup-zeroclaw`)"
END = "# <<< steward"
SAFE_TOOLS = ["file_read", "memory_recall", "memory_store", "ask_user"]


def _q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def block(server_cmd, provider, mode, name="steward", agent="steward"):
    auto = [f"{name}__{t}" for t in ("look", "plan", "resume", "status", "output")]
    always = [f"{name}__approve"] if mode in ("chat", "both") else []
    lst = lambda xs: "[" + ", ".join(_q(x) for x in xs) + "]"
    return f"""
{BEGIN}
[[mcp.servers]]
name = {_q(name)}
command = {_q(server_cmd)}
args = ["--name", {_q(name)}]
tool_timeout_secs = 600

[mcp_bundles.{name}]
servers = [{_q(name)}]

# The agent that maintains this computer. It has no shell or file-writing tools
# of its own: every change goes through Steward.
[risk_profiles.{agent}]
level = "supervised"
workspace_only = true
allowed_tools = {lst(SAFE_TOOLS)}
auto_approve = {lst(auto)}
always_ask = {lst(always)}

[agents.{agent}]
model_provider = {_q(provider)}
risk_profile = {_q(agent)}
mcp_bundles = [{_q(name)}]
{END}
"""


def backup(path):
    p = Path(path)
    b = p.with_name(p.name + time.strftime(".steward-backup-%Y%m%d-%H%M%S"))
    shutil.copy2(p, b)
    return b


def _atomic_write(path, text):
    p = Path(path)
    tmp = p.with_name(p.name + ".steward-tmp")
    tmp.write_text(text)
    shutil.copymode(p, tmp)
    tmp.replace(p)


def add(path, server_cmd, provider=None, mode="desktop", name="steward", agent="steward"):
    p = Path(path)
    text = p.read_text()
    cfg = tomllib.loads(text)
    if BEGIN in text:
        return False, "Steward's block is already present"
    if agent in (cfg.get("agents") or {}):
        return False, f"an agent named {agent!r} already exists; pick another with --agent"
    if any(s.get("name") == name for s in ((cfg.get("mcp") or {}).get("servers") or [])):
        return False, f"an MCP server named {name!r} already exists"
    if not provider:
        refs = [a.get("model_provider") for a in (cfg.get("agents") or {}).values()
                if isinstance(a, dict) and a.get("model_provider")]
        if not refs:
            return False, "no model provider found; pass --provider family.alias"
        provider = refs[0]
    new = text.rstrip("\n") + "\n" + block(server_cmd, provider, mode, name, agent)
    try:
        tomllib.loads(new)
    except Exception as e:
        return False, f"merged config would not parse ({e}); nothing written"
    b = backup(p)
    _atomic_write(p, new)
    return True, f"added (backup: {b}); agent {agent!r} uses provider {provider}"


def remove(path, name="steward", agent="steward"):
    p = Path(path)
    if not p.exists():
        return True, "no ZeroClaw config; nothing to remove"
    text = p.read_text()
    if BEGIN in text and END in text:
        a = text.index(BEGIN)
        z = text.index(END) + len(END)
        new = (text[:a].rstrip("\n") + "\n" + text[z:].lstrip("\n")).rstrip("\n") + "\n"
        tomllib.loads(new)
        b = backup(p)
        _atomic_write(p, new)
        return True, f"removed Steward's block (backup: {b})"
    cfg = tomllib.loads(text)
    changed = False
    servers = (cfg.get("mcp") or {}).get("servers") or []
    keep = [s for s in servers if s.get("name") != name]
    if len(keep) != len(servers):
        cfg["mcp"]["servers"] = keep
        changed = True
    for sect, key in (("mcp_bundles", name), ("risk_profiles", agent), ("agents", agent)):
        if key in (cfg.get(sect) or {}):
            del cfg[sect][key]
            changed = True
    if not changed:
        return True, "no Steward entries found"
    import tomli_w
    b = backup(p)
    _atomic_write(p, tomli_w.dumps(cfg))
    return True, f"removed Steward's entries structurally; comments may be lost (backup: {b})"


AGENTS_MD = """# Steward agent

You look after this Fedora computer for the user. You can only act through the
steward tools; you have no shell of your own.

1. Look first: steward__look (presets: overview, memory, disk, services, startup,
   failed, ports, logs, big_files, packages, network; or run a read-only command).
   Looks answer in short plain-English facts; raw output only if you need it.
2. Then send ONE complete plan with steward__plan: every step, plus `verify`
   commands that prove the user's goal. Do not do changes one by one.
3. Follow the reply: NEEDS FIX -> correct the plan; NEEDS APPROVAL -> call
   steward__approve with exactly the given arguments; NEEDS INPUT -> call
   steward__resume; RUNNING -> steward__status with wait=true.
4. If something is PROHIBITED, declined or not approved, do not try another
   route to the same result. Tell the user.
5. Report Steward's verdict honestly (VERIFIED, PARTIAL, UNCERTAIN, ...) in a
   few plain sentences. Mention anything left for the user and how to undo.
"""


def agents_md_path(config_path, agent="steward"):
    return Path(config_path).parent / "agents" / agent / "workspace" / "AGENTS.md"


def write_agents_md(config_path, agent="steward"):
    p = agents_md_path(config_path, agent)
    if p.exists():
        return False, f"kept existing {p}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(AGENTS_MD)
    return True, f"wrote {p}"


def remove_agents_md(config_path, agent="steward"):
    p = agents_md_path(config_path, agent)
    if p.exists() and hashlib.sha256(p.read_bytes()).digest() == \
            hashlib.sha256(AGENTS_MD.encode()).digest():
        p.unlink()
        return f"removed {p}"
    return f"kept {p} (missing or edited by you)" if p.exists() else ""
