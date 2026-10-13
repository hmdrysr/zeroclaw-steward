"""Steward's checks on ZeroClaw's config, and the setup/remove of its entries."""
import tomllib

import pytest

from conftest import HOME, build_home
from steward import config, zcsetup, zeroclaw

BASE = '''schema_version = 3
# my own comment that must survive
[providers.models.openrouter.main]
model = "x-ai/grok-4"
api_key = "sk-or-v1-secretsecretsecret99"

[agents.assistant]
model_provider = "openrouter.main"
'''


def write(text):
    p = HOME / ".zeroclaw/config.toml"
    p.write_text(text)
    return p


def pol(mode="desktop"):
    p = config.load_policy()
    p.zeroclaw_config = str(HOME / ".zeroclaw/config.toml")
    p.approval_mode = mode
    p.require_zeroclaw_gate = True
    return p


@pytest.fixture(autouse=True)
def fresh():
    build_home()


def setup(mode="desktop", text=BASE):
    p = write(text)
    ok, msg = zcsetup.add(str(p), "/opt/steward-server", None, mode)
    assert ok, msg
    return p


def test_setup_produces_a_config_that_passes():
    p = setup()
    text = p.read_text()
    assert "# my own comment that must survive" in text
    cfg = tomllib.loads(text)
    assert cfg["agents"]["steward"]["model_provider"] == "openrouter.main"
    g = zeroclaw.check(pol())
    assert g["errors"] == [], g
    assert any("hosted model" in w for w in g["warnings"])
    assert list(p.parent.glob("config.toml.steward-backup-*"))


def test_setup_refuses_conflicts_and_bad_merges():
    setup()
    ok, msg = zcsetup.add(str(HOME / ".zeroclaw/config.toml"), "/x", None, "desktop")
    assert not ok and "already present" in msg
    write(BASE + '\n[mcp]\nservers = [{name = "other", command = "x"}]\n')
    ok, msg = zcsetup.add(str(HOME / ".zeroclaw/config.toml"), "/x", None, "desktop")
    assert not ok and "would not parse" in msg
    assert "[[mcp.servers]]" not in (HOME / ".zeroclaw/config.toml").read_text()


@pytest.mark.parametrize("edit,frag", [
    (('allowed_tools = ["file_read", "memory_recall", "memory_store", "ask_user"]',
      'allowed_tools = ["file_read", "shell"]'), "shell"),
    (('allowed_tools = ["file_read", "memory_recall", "memory_store", "ask_user"]',
      'allowed_tools = []'), "no allowed_tools"),
    (('allowed_tools = ["file_read", "memory_recall", "memory_store", "ask_user"]',
      'allowed_tools = ["file_read", "delegate"]'), "delegate"),
    (('mcp_bundles = ["steward"]\n', 'mcp_bundles = ["steward", "fs"]\nskill_bundles = ["s"]\n'),
     "skill_bundles"),
    (('model_provider = "openrouter.main"\nrisk_profile', 'model_provider = "nope.x"\nrisk_profile'),
     "not found"),
])
def test_unsafe_agent_configs_block_changes(edit, frag):
    p = setup()
    text = p.read_text()
    assert edit[0] in text
    write(text.replace(edit[0], edit[1], 1))
    g = zeroclaw.check(pol())
    assert any(frag in e for e in g["errors"]), g


def test_other_mcp_server_granted_blocks():
    p = setup()
    write(p.read_text() + '\n[[mcp.servers]]\nname = "fs"\ncommand = "npx"\n\n'
          '[mcp_bundles.files]\nservers = ["fs"]\n')
    t = (HOME / ".zeroclaw/config.toml").read_text().replace(
        'mcp_bundles = ["steward"]\n', 'mcp_bundles = ["steward", "files"]\n')
    write(t)
    assert any("also granted MCP server" in e for e in zeroclaw.check(pol())["errors"])


def test_model_fallback_blocks_silent_paid_switch():
    p = setup()
    write(p.read_text().replace('model = "x-ai/grok-4"',
                                'model = "x-ai/grok-4"\nfallback = ["anthropic.paid"]'))
    errs = zeroclaw.check(pol())["errors"]
    assert any("switch models without asking" in e for e in errs)


def test_chat_mode_requires_always_ask():
    p = setup("chat")
    assert zeroclaw.check(pol("chat"))["errors"] == []
    write(p.read_text().replace('always_ask = ["steward__approve"]', 'always_ask = []'))
    assert any("always_ask" in e for e in zeroclaw.check(pol("chat"))["errors"])


def test_missing_or_garbage_config_fails_closed():
    (HOME / ".zeroclaw/config.toml").unlink()
    assert zeroclaw.check(pol())["errors"]
    write("this is [not toml")
    assert zeroclaw.check(pol())["errors"]


def test_engine_refuses_changes_but_allows_looks_when_gate_fails(tmp_path):
    from steward.engine import Engine
    from steward.store import Store
    write(BASE)             # Steward not configured at all
    e = Engine(store=Store(tmp_path / "db"), policy=pol())
    out = e.submit("g", [{"do": "trash", "path": "~/Downloads/note.txt"}], verify=["true"])
    assert "BLOCKED" in out and (HOME / "Downloads/note.txt").exists()
    assert "hello" not in e.look(run="echo hello") or True
    assert e.look(run="echo hello").splitlines()[1] == "hello"


def test_remove_restores_original():
    p = setup()
    ok, msg = zcsetup.remove(str(p))
    assert ok and p.read_text().strip() == BASE.strip()


def test_remove_after_zeroclaw_rewrote_file():
    p = setup()
    import tomli_w
    p.write_text(tomli_w.dumps(tomllib.loads(p.read_text())))    # markers lost
    ok, msg = zcsetup.remove(str(p))
    cfg = tomllib.loads(p.read_text())
    assert ok and "steward" not in cfg["agents"] and "assistant" in cfg["agents"]
    assert not cfg.get("mcp", {}).get("servers")


def test_route_down_budget_mode_blocks():
    p = setup()
    write(p.read_text() + '\n[cost.enforcement]\nmode = "route_down"\n')
    assert any("route_down" in e for e in zeroclaw.check(pol())["errors"])


def test_agents_md_written_once_and_removed_only_if_unchanged():
    p = setup()
    ok, _ = zcsetup.write_agents_md(str(p))
    f = zcsetup.agents_md_path(str(p))
    assert ok and "steward__plan" in f.read_text()
    assert zcsetup.write_agents_md(str(p))[0] is False
    f.write_text(f.read_text() + "\nmy note\n")
    assert "kept" in zcsetup.remove_agents_md(str(p)) and f.exists()
    f.write_text(zcsetup.AGENTS_MD)
    zcsetup.remove_agents_md(str(p))
    assert not f.exists()
