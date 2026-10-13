"""Your own controls. Nothing here goes through the AI.

  steward plans                      recent plans and verdicts
  steward show PLAN                  every step, approval, result and check
  steward output RUN [--stderr] [--grep RE]
  steward approve PLAN               approve in the terminal (approval_mode "terminal")
  steward stop PLAN                  stop a running plan (kills the current command)
  steward undo RUN                   undo one recorded change (you approve here)
  steward trash                      restorable items
  steward doctor                     health check: sandbox, approvals, ZeroClaw config, records
  steward ask "plain English"          do common things in plain English (changes need your OK)
  steward explain "some command"      what a command does and how risky it is, before you run it
  steward english RUN                 a stored run's output or error in plain English
  steward aides setup|test|status|off optional small local models (see README)
  steward setup-zeroclaw [--provider family.alias] [--mode desktop|chat|both|terminal]
  steward remove-zeroclaw            remove Steward's entries from ZeroClaw's config
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config, execute, zeroclaw


def _t(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def _engine(require_gate=None):
    from .engine import Engine
    e = Engine()
    if require_gate is not None:
        e.policy.require_zeroclaw_gate = require_gate
    return e


def cmd_show(st, pid):
    p = st.plan(pid)
    if not p:
        print("no such plan")
        return 1
    rep = p["report"] or {}
    print(f"plan {p['id']}  created {_t(p['created'])}  state {p['state']}  "
          f"verdict {rep.get('verdict', '-')}  revision {p['revision']}\ngoal: {p['goal']}\n")
    for r in p["steps"] or []:
        print(f"[{r['n']}] {r['status']:<14} {r.get('tier') or '-':<10} {r.get('line', '')}"
              + (f"  ({r['noop']})" if r.get("noop") else ""))
    print("\nruns:")
    for r in st.runs(pid):
        print(f"  r{r['id']} {_t(r['started'])} step {r['step_no']} {r['kind']}/{r['do']} "
              f"status={r['status']} changed={r['changed']} exit={r['exit_code']} "
              f"approval={r['approval']} sandbox={r['sandbox']}"
              + (f" UNDONE by r{r['undone_by']}" if r["undone_by"] else ""))
        print(f"     {r['operation']}")
        if r["verify"]:
            print(f"     check: {json.dumps(r['verify'])[:300]}")
        if r["status"] not in ("ok",) and r["stderr"]:
            print("     " + r["stderr"].strip().splitlines()[-1][:200])
    print("\naudit trail:")
    for ev in st.q("SELECT * FROM events WHERE plan_id=? ORDER BY id", (pid,)):
        print(f"  {_t(ev['ts'])} {ev['event']} {ev['detail'] or ''}"[:220])
    return 0


def cmd_approve(pid):
    e = _engine()
    p = e.store.plan(pid)
    if not p or p["state"] != "awaiting_approval":
        print(f"plan {pid} is not waiting for approval")
        return 1
    print(p["approval"]["detail"])
    ans = input(f"\nType the plan id ({pid}) to approve; anything else declines: ").strip()
    ok, msg = e.terminal_approve(pid, ans == pid)
    print(msg)
    if ans == pid:
        e._wait(pid, 3600)
        print(e.describe(pid))
    return 0 if ok else 1


def cmd_undo(rid):
    e = _engine(require_gate=False)       # you, at your own terminal, are the approver
    e.policy.approval_mode = "terminal"
    out = e.submit(f"manual undo of r{rid} by the user", [{"do": "undo", "ref": f"r{rid}"}],
                   ["true"])
    print(out)
    p = e.store.plans(1)[0]
    if p["state"] == "awaiting_approval":
        return cmd_approve(p["id"])
    return 0


def doctor():
    pol = None
    bad = 0

    def line(ok, label, detail=""):
        nonlocal bad
        if ok is False:
            bad += 1
        mark = {True: "OK  ", False: "FAIL", None: "info"}[ok]
        print(f"[{mark}] {label}" + (f": {detail}" if detail else ""))

    print(f"Steward {__version__}  install={config.INSTALL_DIR}  data={config.DATA_DIR}")
    try:
        pol = config.load_policy()
        line(True, "policy", str(config.CONFIG_DIR / "policy.json"))
    except Exception as ex:
        line(False, "policy.json is invalid", str(ex))
        return 1
    line(None, "approval mode", pol.approval_mode)
    ok = execute.bwrap_available()
    line(ok, "bubblewrap sandbox", "" if ok else "install: sudo dnf install bubblewrap")
    if ok:
        probe = config.HOME / ".steward-probe"
        r = execute.run(["bash", "-c", f"touch {probe}"], policy=pol, sandbox="ro")
        line(r.exit_code != 0 and not probe.exists(), "looks cannot write files")
        r = execute.run(["bash", "-c", "exec 3<>/dev/tcp/1.1.1.1/53"], policy=pol, sandbox="ro",
                        timeout=5)
        line(r.exit_code != 0, "looks have no network")
        r = execute.run(["bash", "-c", "exec 3<>/dev/tcp/1.1.1.1/53"], policy=pol, sandbox="rw",
                        timeout=5)
        line(r.exit_code != 0, "shell steps have no network unless approved with network:true")
        r = execute.run(["bash", "-c", "ls -A ~/.ssh 2>/dev/null | wc -l"], policy=pol, sandbox="ro")
        line(r.stdout.strip() in ("0", ""), "secret folders hidden from commands")
        r = execute.run(["bash", "-c", "systemctl --user is-system-running"], policy=pol,
                        sandbox="ro", timeout=10)
        line(r.exit_code != 0, "looks cannot reach systemd/D-Bus")
        victim = subprocess.Popen(["sleep", "30"])
        try:
            r = execute.run(["bash", "-c", f"kill -0 {victim.pid}"], policy=pol, sandbox="ro")
            line(r.exit_code != 0 and victim.poll() is None, "looks cannot signal your processes")
        finally:
            victim.kill()
            victim.wait()
        r = execute.run(["bash", "-c", "sudo -n true"], policy=pol, sandbox="rw", timeout=10)
        line(r.exit_code != 0, "shell steps cannot gain admin rights")
    r = execute.run(["systemctl", "--user", "is-system-running"], policy=pol, timeout=10)
    line(None, "user systemd", r.stdout.strip() or r.stderr.strip()[:80])
    z = shutil.which("zenity")
    if pol.approval_mode in ("desktop", "both"):
        line(bool(z), "zenity (approval dialog)", "" if z else "install: sudo dnf install zenity")
        from .approval import _gui_env
        env, why = _gui_env()
        line(env is not None, "graphical session for the dialog", why or
             (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")))
    line(bool(shutil.which("pkexec")) or None, "pkexec (admin password prompts)",
         "" if shutil.which("pkexec") else "missing: admin steps will fail")
    zc = shutil.which("zeroclaw") or str(Path.home() / ".cargo/bin/zeroclaw")
    if os.path.exists(zc):
        r = execute.run([zc, "--version"], policy=pol, timeout=15)
        line(None, "zeroclaw", r.stdout.strip())
        svc = execute.run(["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain",
                           "zeroclaw*"], policy=pol, timeout=10)
        line(None, "zeroclaw user services", ", ".join(l.split()[0] + " " + l.split()[2]
                                                        for l in svc.stdout.splitlines() if l.split())
             or "none (fine if you run zeroclaw by hand)")
    else:
        line(None, "zeroclaw binary not found on PATH")
    g = zeroclaw.check(pol)
    line(None, "ZeroClaw config", g["config"])
    for a in g["agents"]:
        line(None, f"agent {a['agent']}", f"provider {a.get('model_provider')} model "
             f"{a.get('model')} local={a.get('local')} profile={a.get('risk_profile')}")
    for err in g["errors"]:
        line(False if pol.require_zeroclaw_gate else None, "ZeroClaw", err)
    for w in g["warnings"]:
        line(None, "warning", w)
    if not g["errors"]:
        line(True, "ZeroClaw cannot bypass Steward (config checks passed)")
    try:
        from .store import Store
        st = Store()
        integ = st.q1("PRAGMA integrity_check")[0]
        line(integ == "ok", "records database", integ)
        stale = st.q("SELECT id, state, updated FROM plans WHERE state IN "
                     "('awaiting_approval','needs_input','interrupted','running')")
        for s in stale:
            line(None, f"open plan {s['id']}", f"{s['state']} since {_t(s['updated'])}")
        recent = st.q("SELECT id, do, failure, ended FROM runs WHERE status NOT IN ('ok','running') "
                      "ORDER BY id DESC LIMIT 5")
        for r in recent:
            line(None, f"recent failure r{r['id']}", f"{r['do']} {r['failure']} at {_t(r['ended'])}")
    except Exception as ex:
        line(False, "records database", str(ex))
    size = 0
    if config.TRASH_DIR.exists():
        from .steps import tree_size, human
        size = tree_size(config.TRASH_DIR)
        line(None, "trash", f"{human(size)} (purge with a plan step purge_trash)")
    print("\nresult:", "all checks passed" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="steward", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plans")
    p = sub.add_parser("show"); p.add_argument("plan")
    p = sub.add_parser("output"); p.add_argument("run")
    p.add_argument("--stderr", action="store_true"); p.add_argument("--grep")
    p = sub.add_parser("approve"); p.add_argument("plan")
    p = sub.add_parser("stop"); p.add_argument("plan")
    p = sub.add_parser("undo"); p.add_argument("run")
    sub.add_parser("trash")
    sub.add_parser("doctor")
    p = sub.add_parser("ask"); p.add_argument("text", nargs="+")
    p.add_argument("--all", action="store_true", help="show every fact")
    p.add_argument("-v", "--verbose", action="store_true", help="show what the aides proposed")
    p = sub.add_parser("explain"); p.add_argument("command", nargs="+")
    p = sub.add_parser("english"); p.add_argument("run")
    p = sub.add_parser("aides"); p.add_argument("action", choices=["setup", "test", "status", "off"])
    p.add_argument("--backend", choices=["ollama", "llamacpp"], default="ollama")
    p = sub.add_parser("setup-zeroclaw")
    p.add_argument("--provider"); p.add_argument("--agent", default="steward")
    p.add_argument("--mode", choices=["desktop", "chat", "both", "terminal"])
    p.add_argument("--server", default=str(Path(sys.executable).parent / "steward-server"))
    p = sub.add_parser("remove-zeroclaw"); p.add_argument("--agent", default="steward")
    a = ap.parse_args(argv)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    if a.cmd == "doctor":
        return doctor()
    if a.cmd == "explain":
        from .english.explain import explain
        ex = explain(" ".join(a.command))
        print(ex.text())
        return 0 if ex.readable else 1
    if a.cmd == "ask":
        return run_ask(" ".join(a.text), a.all, a.verbose)
    if a.cmd == "aides":
        return run_aides(a.action, a.backend)
    pol = config.load_policy()
    if a.cmd == "setup-zeroclaw":
        from . import zcsetup
        mode = a.mode or pol.approval_mode
        if a.mode and a.mode != pol.approval_mode:
            f = config.CONFIG_DIR / "policy.json"
            data = json.loads(f.read_text()) if f.exists() else {}
            data["approval_mode"] = a.mode
            config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(data, indent=2) + "\n")
            print(f"approval_mode set to {a.mode} in {f}")
        ok, msg = zcsetup.add(pol.zeroclaw_config, a.server, a.provider, mode, agent=a.agent)
        print(msg)
        if ok:
            print(zcsetup.write_agents_md(pol.zeroclaw_config, a.agent)[1])
        return 0 if ok else 1
    if a.cmd == "remove-zeroclaw":
        from . import zcsetup
        ok, msg = zcsetup.remove(pol.zeroclaw_config, agent=a.agent)
        print(msg)
        extra = zcsetup.remove_agents_md(pol.zeroclaw_config, a.agent)
        if extra:
            print(extra)
        return 0 if ok else 1
    from .store import Store
    st = Store()
    if a.cmd == "plans":
        for p in st.plans(30):
            print(f"{p['id']}  {_t(p['created'])}  {p['state']:<18} "
                  f"{(p['report'] or {}).get('verdict', '-'):<11} {p['goal'][:60]}")
    elif a.cmd == "show":
        return cmd_show(st, a.plan)
    elif a.cmd == "output":
        r = st.run(int(a.run.lstrip("r")))
        if not r:
            print("no such run")
            return 1
        text = r["stderr" if a.stderr else "stdout"] or ""
        if a.grep:
            import re
            text = "\n".join(l for l in text.splitlines() if re.search(a.grep, l))
        print(text)
    elif a.cmd == "approve":
        return cmd_approve(a.plan)
    elif a.cmd == "stop":
        if st.request_cancel(a.plan):
            print("stop requested: the running command is killed within a second and no further "
                  "steps run")
        else:
            print("no such plan")
            return 1
    elif a.cmd == "undo":
        return cmd_undo(int(a.run.lstrip("r")))
    elif a.cmd == "english":
        r = st.run(int(a.run.lstrip("r")))
        if not r:
            print("no such run")
            return 1
        from .english.errors import translate as tr_err
        from .english.outputs import summarise_sections, translate_command
        facts = None
        if (r["stdout"] or "").startswith("## "):
            facts = summarise_sections(r["stdout"])
        elif r["operation"]:
            facts = translate_command(r["operation"], r["stdout"] or "")
        if facts:
            print("\n".join("• " + f for f in facts))
        if r["exit_code"] not in (0, None):
            print(tr_err(r["stderr"], r["exit_code"], r["stdout"])[1])
        elif not facts:
            print("It finished successfully. I have no plain-English summary for this kind of output; "
                  f"see: steward output r{r['id']}")
    elif a.cmd == "trash":
        if config.TRASH_DIR.exists():
            for d in sorted(config.TRASH_DIR.iterdir()):
                m = d / "manifest.json"
                if m.exists():
                    info = json.loads(m.read_text())
                    print(d.name, info.get("kind", "trash"), info["original"])
    return 0


if __name__ == "__main__":
    sys.exit(main())


def _consent(question):
    if not sys.stdin.isatty():
        return False
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def run_ask(text, show_all=False, verbose=False):
    from .engine import Engine
    from .english.aides import Aides
    from .english.ask import Asker
    e = Engine()
    e.policy.require_zeroclaw_gate = False      # only fixed, tested tasks run here; changes still need your OK
    if sys.stdin.isatty() and e.policy.approval_mode in ("desktop", "both", "chat"):
        from .approval import _gui_env
        if _gui_env()[0] is None or not shutil.which("zenity"):
            e.policy.approval_mode = "inline"
    elif sys.stdin.isatty():
        e.policy.approval_mode = "inline"
    aides = Aides()
    try:
        ans = Asker(e, aides if aides.available() else None, consent=_consent, show_all=show_all).ask(text)
    finally:
        aides.close()
    if ans.kind == "escalate":
        print(ans.text)
        return subprocess.call(["zeroclaw", "agent", "-a", "steward", "-m", text])
    print(ans.text)
    if verbose:
        for who, what in aides.log:
            print(f"  [{who}] {what}")
        if ans.via:
            print("  aides used: " + ", ".join(ans.via))
    return 0 if ans.kind in ("facts", "done") else 1


def run_aides(action, backend):
    from .english import models
    from .english.aides import load
    if action == "status":
        cfg = load()
        print(f"aides: {'ON' if cfg.get('enabled') else 'OFF'} (backend {cfg.get('backend')})")
        ex = cfg.get("exam")
        if ex:
            print(f"last exam {ex['when']}: " + ", ".join(f"{k} {v:.0%}" for k, v in ex["scores"].items()) +
                  f" -> {'passed' if ex['passed'] else 'failed'}")
        return 0
    if action == "off":
        cfg = load()
        cfg["enabled"] = False
        models.save(cfg)
        print("aides switched off")
        return 0
    try:
        if action == "setup":
            ok, _ = models.setup(backend)
        else:
            ok, _ = models.exam()
    except Exception as ex:
        print(f"aides setup failed: {ex}\nNothing was enabled; everything works without them.")
        return 1
    return 0 if ok else 1


def ask_main():
    sys.exit(main(["ask"] + sys.argv[1:]))


def explain_main():
    sys.exit(main(["explain"] + sys.argv[1:]))
