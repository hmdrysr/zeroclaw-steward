"""Over the real MCP stdio protocol: a client launches `python -m steward.server`
exactly as ZeroClaw does, and calls its tools."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import FAKE, HOME, ROOT, build_home


def params():
    return StdioServerParameters(command=sys.executable, args=["-m", "steward.server"],
                                 env=dict(os.environ, FAKE_ZENITY="approve"), cwd=str(ROOT))


def call(seq):
    async def go():
        async with stdio_client(params()) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                out = []
                for name, args in seq:
                    if name == "__list__":
                        out.append((await s.list_tools()).tools)
                        continue
                    res = await s.call_tool(name, args)
                    out.append(res.content[0].text)
                return out
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def fresh():
    build_home()


def test_six_compact_tools():
    tools = call([("__list__", {})])[0]
    assert [t.name for t in tools] == ["look", "plan", "approve", "resume", "status", "output"]
    size = sum(len(json.dumps(t.model_dump(exclude_none=True))) for t in tools)
    assert size < 7000, size          # whole tool surface stays small (~1.7k tokens)


def test_look_and_plan_end_to_end():
    out = call([("look", {"preset": "memory", "lines": 10}),
                ("plan", {"goal": "tidy", "steps": [{"do": "trash", "path": "~/Documents/thesis.docx"}],
                          "verify": ["test ! -e ~/Documents/thesis.docx"]}),
                ("plan", {"goal": "bad", "steps": [{"do": "trash", "path": "~/.ssh"}],
                          "verify": ["true"]}),
                ("output", {"ref": "r1", "grep": "Mem"})])
    assert out[0].startswith("r1 memory (raw: output") and "- Memory:" in out[0]
    assert "VERIFIED" in out[1] and not (HOME / "Documents/thesis.docx").exists()
    assert "NEEDS FIX" in out[2] and "PROHIBITED" in out[2]
    assert "Mem" in out[3]
    assert not out[1].lstrip().startswith("{")      # plain text, not JSON


def test_schema_validation_rejects_garbage():
    out = call([("plan", {"goal": "x"}),
                ("resume", {"plan": "p00000", "action": "nuke"}),
                ("look", {"run": "ls", "sudo": True})])
    assert all("validation" in o.lower() or "error" in o.lower() for o in out), out


def test_server_crash_mid_step_is_recovered_honestly():
    async def first():
        async with stdio_client(params()) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                t = asyncio.create_task(s.call_tool("plan", {
                    "goal": "slow", "verify": ["true"],
                    "steps": [{"do": "shell", "run": "sleep 40; touch ~/Downloads/after"},
                              {"do": "trash", "path": "~/Downloads/note.txt"}]}))
                await asyncio.sleep(3)
                for pid in subprocess.run(["pgrep", "-f", "steward.server"], capture_output=True,
                                          text=True).stdout.split():
                    os.kill(int(pid), signal.SIGKILL)
                try:
                    await asyncio.wait_for(t, 5)
                except BaseException:
                    pass
    try:
        asyncio.run(first())
    except BaseException:
        pass
    time.sleep(1)
    assert subprocess.run(["pgrep", "-f", "slee[p] 40"]).returncode != 0, "orphan left running"
    from steward.store import Store
    pid = Store().plans(1)[0]["id"]
    calls_before = len((FAKE / "zenity.log").read_text().splitlines())
    out = call([("status", {"plan": pid}),
                ("resume", {"plan": pid, "action": "skip"})])
    assert "INTERRUPTED" in out[0]
    assert not (HOME / "Downloads/after").exists()
    assert "UNCERTAIN" in out[1], out[1]
    st = Store()
    shell_run = st.q1("SELECT status, changed FROM runs WHERE do='shell'")
    assert tuple(shell_run) == ("interrupted", "unknown")
