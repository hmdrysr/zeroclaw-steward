"""MCP stdio server launched by ZeroClaw. Six small tools, plain-text results.

usage: steward-server [--name steward]   (--name = the [[mcp.servers]] name in ZeroClaw)
"""
import argparse
import asyncio
import json
import sys

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from . import __version__, config
from .readonly import presets

PLAN_DESC = """Run a complete plan on the user's Fedora PC. Think once and put every step in; Steward checks it, gets the user's OK for risky changes, runs it, verifies, and only comes back if reality differs from the plan.
steps: [{do, ...params, expect?, check?, only_if?, on_fail?}]
 inspect{run,lines?} read-only command, output returned | check{run} read-only, must exit 0
 shell{run,cwd?,network?} any command, user rights, no admin, offline unless network:true
 user_service|system_service{unit,op: start|stop|restart|enable|disable|enable_now|disable_now}
 kill{name|pid,signal?} | trash{path,missing_ok?} (restorable) | restore{entry} | purge_trash{entries|all|this_plan}
 write_file{path,content,mode:create|replace} | edit_file{path,find,replace,count?}
 pkg_install|pkg_remove{packages,with_dependents?} | pkg_upgrade{} | pkg_clean{} (dnf/apt/pacman/zypper chosen automatically)
 flatpak_remove{app} | purge_trash{this_plan:true} frees space of what this plan trashed | undo{ref: rN}
expect{exit,contains,absent,regex} tests the step's output; check: read-only cmd after the step; only_if: read-only cmd, step runs only if it exits 0; on_fail: ask(default)|skip|stop.
verify: read-only commands that exit 0 only if the user's goal is achieved (required when changing anything).
Reply is one of: final report | NEEDS FIX (resubmit) | NEEDS APPROVAL (call approve with the given args) | NEEDS INPUT (call resume) | RUNNING (call status wait=true)."""

TOOLS = [
    types.Tool(name="look", description=(
        "Read-only look at the system; never changes anything, no approval needed. preset: " +
        "|".join(presets()) + "; or run: a shell command (read-only sandbox, no network; "
        "systemctl/loginctl/nmcli/ss/ip/flatpak/rpm/dnf queries run natively when first in the line). "
        "Presets and common commands return short plain-English facts; raw=true for raw output. "
        "lines: max raw lines (default 60)."),
        inputSchema={"type": "object", "properties": {
            "preset": {"type": "string", "enum": list(presets())},
            "run": {"type": "string"}, "lines": {"type": "integer"},
            "raw": {"type": "boolean", "description": "raw output instead of the plain-English summary"}},
            "additionalProperties": False}),
    types.Tool(name="plan", description=PLAN_DESC, inputSchema={
        "type": "object", "required": ["goal", "steps"], "additionalProperties": False,
        "properties": {"goal": {"type": "string"},
                       "steps": {"type": "array", "items": {"type": "object"}},
                       "verify": {"type": "array", "items": {"type": "string"}}}}),
    types.Tool(name="approve", description=(
        "Ask the user to approve a plan (only when a plan reply says NEEDS APPROVAL). Pass exactly "
        "the arguments it gave; the user sees them."),
        inputSchema={"type": "object", "required": ["plan", "seal"],
                     "properties": {"plan": {"type": "string"}, "seal": {"type": "string"}},
                     "additionalProperties": {"type": "string"}}),
    types.Tool(name="resume", description=(
        "Answer NEEDS INPUT. action: retry | skip | abort | replace (steps replace the failed step "
        "and everything after it; same format as plan)."),
        inputSchema={"type": "object", "required": ["plan", "action"], "additionalProperties": False,
                     "properties": {"plan": {"type": "string"},
                                    "action": {"type": "string",
                                               "enum": ["retry", "skip", "abort", "replace"]},
                                    "steps": {"type": "array", "items": {"type": "object"}}}}),
    types.Tool(name="status", description=(
        "Plan status or final report. wait=true blocks until it finishes or needs you (~7 min max). "
        "No plan: list recent plans."),
        inputSchema={"type": "object", "additionalProperties": False,
                     "properties": {"plan": {"type": "string"}, "wait": {"type": "boolean"}}}),
    types.Tool(name="output", description=(
        "Full stored output of a run (ref like r12): grep regex, or start/lines range."),
        inputSchema={"type": "object", "required": ["ref"], "additionalProperties": False,
                     "properties": {"ref": {"type": "string"}, "grep": {"type": "string"},
                                    "start": {"type": "integer"}, "lines": {"type": "integer"},
                                    "stream": {"type": "string", "enum": ["stdout", "stderr"]}}}),
]


def build(engine_factory):
    server = Server("steward", version=__version__)
    state = {"engine": None, "error": None}

    def engine():
        if state["engine"] is None and state["error"] is None:
            try:
                state["engine"] = engine_factory()
            except Exception as e:  # config invalid: fail closed, but say why
                state["error"] = f"Steward cannot start: {e}. Tell the user to run `steward doctor`."
        return state["engine"]

    @server.list_tools()
    async def list_tools():
        return TOOLS

    @server.call_tool(validate_input=True)
    async def call_tool(name, args):
        e = await asyncio.to_thread(engine)
        if e is None:
            return [types.TextContent(type="text", text=state["error"])]
        args = args or {}
        try:
            if name == "look":
                out = await asyncio.to_thread(e.look, args.get("run"), args.get("preset"),
                                              args.get("lines"), bool(args.get("raw")))
            elif name == "plan":
                out = await asyncio.to_thread(e.submit, args.get("goal"), args.get("steps"),
                                              args.get("verify"))
            elif name == "approve":
                lines = {k: v for k, v in args.items() if k not in ("plan", "seal")}
                out = await asyncio.to_thread(e.approve, args.get("plan"), args.get("seal"), lines)
            elif name == "resume":
                out = await asyncio.to_thread(e.resume, args.get("plan"), args.get("action"),
                                              args.get("steps"))
            elif name == "status":
                out = await asyncio.to_thread(e.status, args.get("plan"), bool(args.get("wait")))
            elif name == "output":
                out = await asyncio.to_thread(e.output, args.get("ref"), args.get("grep"),
                                              args.get("start", 1), args.get("lines", 150),
                                              args.get("stream", "stdout"))
            else:
                out = f"error: unknown tool {name}"
        except Exception as ex:
            out = f"error: Steward internal error: {ex!r} (recorded; nothing further was run)"
        return [types.TextContent(type="text", text=out)]

    server.steward_engine = engine
    return server


async def amain(name):
    from .engine import Engine
    server = build(lambda: Engine(server_name=name))
    await asyncio.to_thread(server.steward_engine)   # recover interrupted work at start-up
    async with mcp.server.stdio.stdio_server() as (r, w):
        await server.run(r, w, InitializationOptions(
            server_name="steward", server_version=__version__,
            capabilities=server.get_capabilities(notification_options=NotificationOptions(),
                                                 experimental_capabilities={})))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="steward-server")
    ap.add_argument("--name", default="steward", help="the [[mcp.servers]] name in ZeroClaw")
    a = ap.parse_args(argv)
    asyncio.run(amain(a.name))


if __name__ == "__main__":
    main()
