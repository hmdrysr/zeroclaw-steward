"""Checks ZeroClaw's configuration so Steward stays the only way to change
your system, and so approvals and model usage behave as you expect.

Steward refuses to make changes (looks still work) when any ERROR is found:
  - an agent that can use Steward can also use a built-in tool that runs
    commands or writes files (shell, file_write, delegate, ...),
  - that agent is granted other MCP servers or skill bundles (could execute),
  - in "chat"/"both" approval mode, ZeroClaw is not forced to ask you before
    <name>__approve (always_ask),
  - the agent's model provider has fallbacks that could silently switch to
    another (possibly paid) provider or model.
"""
import os
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

# Built-in ZeroClaw tools that cannot change the system or run programs.
SAFE_BUILTINS = {"file_read", "glob_search", "content_search", "memory_recall", "memory_store",
                 "memory_search", "web_fetch", "web_search", "web_search_tool", "ask_user",
                 "time", "TodoWrite", "sessions_current", "sessions_list", "sessions_history",
                 "image_info", "weather", "poll", "reaction"}
LOCAL_FAMILIES = {"ollama", "llamacpp", "lmstudio", "vllm", "localai", "sglang", "llamafile",
                  "jan", "koboldcpp", "mlx", "osaurus", "atomic_chat"}

_cache = {}


def load(path):
    p = Path(path)
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size)
    if key not in _cache:
        with open(p, "rb") as f:
            _cache.clear()
            _cache[key] = tomllib.loads(f.read().decode())
    return _cache[key]


def _servers_for(cfg, agent):
    bundles = cfg.get("mcp_bundles", {}) or {}
    granted, excluded = set(), set()
    for b in agent.get("mcp_bundles", []) or []:
        bd = bundles.get(b) or {}
        granted |= set(bd.get("servers", []) or [])
        excluded |= set(bd.get("exclude", []) or [])
    return granted - excluded


def _provider(cfg, ref):
    if not ref or "." not in ref:
        return None, None
    fam, alias = ref.split(".", 1)
    return fam, ((cfg.get("providers", {}) or {}).get("models", {}) or {}).get(fam, {}).get(alias)


def check(policy, server_name="steward"):
    """Returns dict(errors, warnings, agents)."""
    out = {"errors": [], "warnings": [], "agents": [], "config": policy.zeroclaw_config}
    if tomllib is None:
        out["errors"].append("Python 3.11+ is needed to read ZeroClaw's config")
        return out
    path = policy.zeroclaw_config
    if not path or not os.path.exists(path):
        out["errors"].append(f"ZeroClaw config not found at {path}")
        return out
    try:
        cfg = load(path)
    except Exception as e:
        out["errors"].append(f"cannot parse ZeroClaw config: {e}")
        return out
    mcp = cfg.get("mcp", {}) or {}
    servers = {s.get("name"): s for s in (mcp.get("servers") or []) if isinstance(s, dict)}
    if server_name not in servers:
        out["errors"].append(f"no [[mcp.servers]] entry named {server_name!r} in {path}")
        return out
    approve = f"{server_name}__approve"
    agents = {k: v for k, v in (cfg.get("agents", {}) or {}).items()
              if isinstance(v, dict) and v.get("enabled", True)}
    profiles = cfg.get("risk_profiles", {}) or {}
    mine = {k: v for k, v in agents.items() if server_name in _servers_for(cfg, v)}
    if not mine:
        out["errors"].append(f"no enabled agent is granted the {server_name!r} server "
                             "(mcp_bundles)")
    for alias, a in mine.items():
        info = {"agent": alias}
        e, w = out["errors"], out["warnings"]
        pname = a.get("risk_profile") or "default"
        prof = profiles.get(pname)
        if prof is None:
            e.append(f"agent {alias}: risk profile {pname!r} not found")
            continue
        info["risk_profile"] = pname
        info["level"] = prof.get("level", "supervised")
        allowed = prof.get("allowed_tools") or []
        if prof.get("deny_all_tools"):
            e.append(f"agent {alias}: deny_all_tools=true also blocks Steward's tools")
        elif not allowed:
            e.append(f"agent {alias}: risk profile {pname!r} has no allowed_tools list, so ZeroClaw's "
                     "own shell/file tools are available and could bypass Steward. Set allowed_tools "
                     f"to safe tools only, e.g. {sorted(['file_read', 'memory_recall', 'ask_user'])}")
        else:
            risky = sorted(t for t in allowed if t not in SAFE_BUILTINS and "__" not in t)
            if risky:
                e.append(f"agent {alias}: allowed_tools includes {risky}, which can run commands, "
                         "change files or hand work to other agents outside Steward")
        others = sorted(_servers_for(cfg, a) - {server_name})
        if others and not policy.allow_other_mcp:
            e.append(f"agent {alias}: also granted MCP server(s) {others}; their tools are not "
                     "controlled by Steward (set allow_other_mcp in Steward's policy if intended)")
        if (a.get("skill_bundles") or []) and not policy.allow_skills:
            e.append(f"agent {alias}: has skill_bundles; skills can define shell tools outside "
                     "Steward (set allow_skills in Steward's policy if intended)")
        always = prof.get("always_ask") or []
        if policy.approval_mode in ("chat", "both") and approve not in always and "*" not in always:
            e.append(f"agent {alias}: approval_mode is {policy.approval_mode!r} but risk profile "
                     f"{pname!r} does not list {approve!r} in always_ask, so ZeroClaw might not ask you")
        if prof.get("level") == "full" and policy.approval_mode == "chat" and approve not in always:
            e.append(f"agent {alias}: autonomy 'full' without always_ask would auto-approve plans")
        if prof.get("workspace_only") is False or prof.get("level") == "full":
            if "file_read" in allowed:
                w.append(f"agent {alias}: file_read can read outside its workspace "
                         "(workspace_only off); secrets are not protected from it")
        ref = a.get("model_provider", "")
        fam, prov = _provider(cfg, ref)
        info["model_provider"] = ref
        if prov is None:
            e.append(f"agent {alias}: model_provider {ref!r} not found in [providers.models]")
        else:
            uri = str(prov.get("uri", ""))
            local = fam in LOCAL_FAMILIES or any(h in uri for h in ("127.0.0.1", "localhost", "[::1]"))
            info["model"] = prov.get("model")
            info["local"] = local
            fb = list(prov.get("fallback") or [])
            fbm = list(prov.get("fallback_models") or [])
            if (fb or fbm):
                msg = (f"agent {alias}: provider {ref} falls back to {fb + fbm}; ZeroClaw would "
                       "switch models without asking")
                (e if policy.forbid_model_fallback else w).append(msg)
            if not local:
                w.append(f"agent {alias}: uses a hosted model ({ref}, {prov.get('model')}); "
                         "requests cost money per token")
        cost = cfg.get("cost", {}) or {}
        mode = ((cost.get("enforcement") or {}).get("mode")) if isinstance(cost.get("enforcement"), dict) else None
        if mode == "route_down":
            msg = ("ZeroClaw's cost.enforcement.mode is 'route_down': over budget it switches to "
                   "another model without asking")
            (e if policy.forbid_model_fallback else w).append(msg)
        elif mode != "block":
            w.append("ZeroClaw cost limits only warn (cost.enforcement.mode is not 'block')")
        out["agents"].append(info)
    srv = servers[server_name]
    t = srv.get("tool_timeout_secs")
    if t is None or t < policy.wait_secs + 30:
        out["warnings"].append(f"mcp server {server_name}: tool_timeout_secs should be >= "
                               f"{policy.wait_secs + 30} (max 600) so long steps are not cut off")
    out["warnings"] = sorted(set(out["warnings"]))
    return out
