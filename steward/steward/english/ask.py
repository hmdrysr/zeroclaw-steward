"""`ask "plain English"`: handle common requests locally, deterministically.

Flow: match a known task (intents.py) -> check names against the real system ->
read-only tasks run at once, changes go through Steward (preflight, your
approval, verification) -> the answer is written from verified facts.
Anything else is passed to the main assistant ONLY if you say yes.
Optional small-model aides may help choose between candidate tasks, pull a name
out of the request, or pick the most relevant facts; every aide output is
checked and ignored if it fails a check, and facts are always shown verbatim.
"""
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from .. import distro, execute, steps
from . import intents
from .outputs import summarise_sections, translate_command

LOOK_PRESETS = {"health": ["overview"], "memory": ["memory"], "disk": ["disk"], "big_files": ["big_files", "disk"],
                "services": ["services"], "startup": ["startup"], "errors": ["logs", "failed"],
                "ports": ["ports"], "network": ["network"], "packages": ["packages"]}


@dataclass
class Answer:
    text: str
    kind: str                        # facts | done | not_done | escalate | question | refused
    facts: list = field(default_factory=list)
    plan: str = None
    via: list = field(default_factory=list)   # which parts used an aide


def _names_running():
    out = {}
    uid = os.getuid()
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                if os.stat(f"/proc/{d}").st_uid != uid:
                    continue
                comm = open(f"/proc/{d}/comm").read().strip()
                out.setdefault(comm.lower(), comm)
            except OSError:
                pass
    return out


def _unit(policy, name):
    """Resolve a service name: user units first, then system. Returns (scope, unit) or (None, suggestions)."""
    unit = name if "." in name else name + ".service"
    for user in (True, False):
        st, _ = steps.unit_state(policy, user, unit)
        if st.get("LoadState") == "loaded":
            return ("user" if user else "system"), unit
    names = steps.unit_names(policy, True) | steps.unit_names(policy, False)
    return None, intents.closest(unit, names) + intents.closest(name, names)


def _verify_unit(scope, unit, want):
    base = "systemctl " + ("--user " if scope == "user" else "") + f"show -p {want[0]} --value {unit}"
    return f"{base} | grep -Eqx '{want[1]}'"


class Asker:
    def __init__(self, engine, aides=None, consent=None, out=print, show_all=False):
        self.show_all = show_all
        self.e = engine
        self.p = engine.policy
        self.aides = aides
        self.consent = consent or (lambda q: False)
        self.out = out

    # ------------------------------------------------------------------ entry
    def ask(self, text):
        m = intents.match(text)
        via = []
        if m.status == "ambiguous" and self.aides and self.aides.available():
            pick = self.aides.route(text, [(c, intents.BY_ID[c].title) for c in m.candidates])
            if pick in m.candidates and intents.BY_ID[pick].kind != "plan":
                m = intents.Match("matched", recipe=pick)
                via.append("router")
            elif pick in m.candidates:
                return Answer(f"Did you mean: {intents.BY_ID[pick].title}? Please say it more exactly "
                              "(I don't guess when a change is involved).", "question")
        if m.status == "ambiguous":
            opts = "; ".join(intents.BY_ID[c].title for c in m.candidates)
            return Answer(f"That could mean several things: {opts}. Which one?", "question")
        if m.status == "need_slot":
            rec = intents.BY_ID[m.recipe]
            if self.aides and self.aides.available() and "or" not in text.lower().split():
                name = self.aides.extract_name(text, rec.slot.split("_")[0])
                if name:
                    m = intents.Match("matched", recipe=m.recipe, slot=name)
                    via.append("extractor")
            if m.status == "need_slot":
                return Answer(f"Which {rec.slot.split('_')[0]} exactly? ({m.reason})", "question")
        if m.status in ("escalate", "refuse"):
            return self.escalate(text, m.reason)
        rec = intents.BY_ID[m.recipe]
        a = getattr(self, "do_" + rec.id, None)
        if a is None:
            a = self._look if rec.kind == "look" else None
        ans = a(rec, m.slot) if a.__name__ != "_look" else self._look(rec)
        ans.via += via
        if ans.kind == "facts" and self.aides and self.aides.available() and not self.show_all:
            keep = self.aides.select_facts(text, ans.facts)
            if keep:
                shown = [ans.facts[i] for i in keep]
                rest = len(ans.facts) - len(shown)
                ans.text = "\n".join("• " + f for f in shown) + \
                    (f"\n({rest} more detail{'s' if rest != 1 else ''}: ask again with --all)" if rest else "")
                ans.via.append("selector")
        return ans

    def escalate(self, text, reason):
        zc = shutil.which("zeroclaw")
        q = (f"I won't do this on my own ({reason}).\nSend it to the main assistant? That uses your paid "
             "AI model (a few cents or less for most requests).")
        if zc and self.consent(q):
            return Answer("Passing it to the main assistant...", "escalate",
                          facts=[f"zeroclaw agent -a steward -m {text!r}"])
        return Answer(f"I won't do this on my own: {reason}.\nTo ask the main assistant yourself, run:\n"
                      f"  zeroclaw agent -a steward", "refused")

    # ------------------------------------------------------------------ read-only
    def _look(self, rec):
        facts = []
        for preset in LOOK_PRESETS[rec.id]:
            out = self.e.look(preset=preset)
            facts += [l[2:] for l in out.splitlines() if l.startswith("- ")]
            rest = [l for l in out.splitlines()[1:] if not l.startswith("- ")]
            if rest:
                facts += [" ".join(rest)[:300]]
        facts = list(dict.fromkeys(facts))
        return Answer("\n".join("• " + f for f in facts), "facts", facts)

    def do_os_version(self, rec, slot):
        d = distro.detect()
        r = execute.run(["uname", "-r"], policy=self.p, timeout=10)
        f = [f"This is {d.name}" + (f" (version {d.version})" if d.version and d.version not in d.name else "") + ".",
             f"Linux kernel {r.stdout.strip()}." if r.exit_code == 0 else "Kernel version: could not check."]
        return Answer("\n".join("• " + x for x in f), "facts", f)

    def do_uptime(self, rec, slot):
        r = execute.run(["uptime"], policy=self.p, timeout=10)
        f = translate_command("uptime", r.stdout) or ["Could not read the uptime."]
        return Answer("\n".join("• " + x for x in f), "facts", f)

    def do_is_installed(self, rec, slot):
        d = distro.detect()
        names = [n for n in slot.split() if steps.PKG_RE.match(n)]
        if not names:
            return Answer("That doesn't look like a package name.", "question")
        if not d.known:
            return Answer(f"I can't check packages on {d.name}.", "refused")
        have = steps.Backend(d.pkg).installed(self.p, names)
        f = [f"{n} is installed." if n in have else f"{n} is not installed as a system package." for n in names]
        return Answer("\n".join("• " + x for x in f), "facts", f)

    def do_service_status(self, rec, slot):
        name = slot.split()[0]
        scope, unit = _unit(self.p, name)
        if scope is None:
            sug = f" Similar names: {', '.join(unit[:4])}." if unit else ""
            return Answer(f"There is no service called {name}.{sug}", "facts", [f"No service called {name}."])
        st, _ = steps.unit_state(self.p, scope == "user", unit)
        a, u = st.get("ActiveState", "?"), st.get("UnitFileState", "?")
        state = {"active": "is running", "inactive": "is not running", "failed": "has FAILED",
                 "activating": "is starting", "deactivating": "is stopping"}.get(a, f"is in state '{a}'")
        auto = {"enabled": "starts automatically", "disabled": "does not start automatically",
                "static": "starts only when something needs it", "masked": "is blocked from starting"}.get(u, "")
        f = [f"{unit} ({scope} service) {state}" + (f" and {auto}" if auto else "") + "."]
        return Answer("• " + f[0], "facts", f)

    # ------------------------------------------------------------------ changes
    def _run_plan(self, goal, plan_steps, verify, require_verify=True):
        out = self.e.submit(goal, plan_steps, verify, require_verify=require_verify)
        p = self.e.store.plans(1)[0]
        return self._plan_answer(p, out)

    def _plan_answer(self, p, raw):
        rep = p.get("report") or {}
        st = p["state"]
        if st in ("declined", "expired"):
            return Answer(f"Not done: {rep.get('why', 'not approved')}. Nothing was changed.", "not_done", plan=p["id"])
        if st == "rejected":
            lines = [l[2:] for l in raw.splitlines() if l.startswith("- ")]
            return Answer("I can't do that as asked:\n" + "\n".join("• " + l for l in lines), "not_done", plan=p["id"])
        if st == "needs_input":
            q = p.get("question") or {}
            self.e.resume(p["id"], "abort")
            ev = [l.strip() for l in raw.splitlines() if l.startswith("in plain English:")]
            return Answer(f"It didn't go as planned: {q.get('problem')}.\n" + ("\n".join(ev) + "\n" if ev else "") +
                          f"Stopped there. Details: steward show {p['id']}", "not_done", plan=p["id"])
        verdict = rep.get("verdict", "?")
        words = {"verified": "Done, and checked.", "unverified": "Done (the command succeeded; there was no "
                 "separate check of the result).", "partial": "Partly done.", "failed": "It did not work.",
                 "uncertain": "Unclear: something may have only half happened.", "done": "Done.",
                 "stopped": "Stopped."}
        lines = [r["line"] for r in p["steps"] if r["status"] == "ok" and r.get("tier") != "read"]
        noop = [f"{r['line']} - {r['noop']}" for r in p["steps"] if r["status"] == "noop"]
        undo = re.findall(r'\{"do":"undo","ref":"(r\d+)"\}', raw)
        text = words.get(verdict, verdict)
        if lines:
            text += "\n" + "\n".join("• " + l for l in lines)
        if noop:
            text += "\nAlready the case:\n" + "\n".join("• " + l for l in noop)
        if undo:
            text += f"\nTo undo: steward undo {undo[0]}"
        text += f"\n(record: steward show {p['id']})"
        return Answer(text, "done" if verdict in ("verified", "unverified", "done") else "not_done", plan=p["id"])

    def do_update_system(self, rec, slot):
        d = distro.detect()
        verify = {"dnf": ["dnf check-update -q"], "apt": ["apt-get -s upgrade | grep -q '^0 upgraded'"],
                  "pacman": ["! pacman -Qu"]}.get(d.pkg, [])
        return self._run_plan("update all installed software", [{"do": "pkg_upgrade"}], verify,
                              require_verify=False)

    def do_clean_package_cache(self, rec, slot):
        return self._run_plan("delete downloaded package files", [{"do": "pkg_clean"}], [], require_verify=False)

    def _pkg_names(self, slot):
        names = [n for n in slot.split() if n not in intents.STOPWORDS]
        bad = [n for n in names if not steps.PKG_RE.match(n)]
        return names, bad

    def do_install(self, rec, slot):
        d = distro.detect()
        names, bad = self._pkg_names(slot)
        if bad or not names:
            return Answer(f"I'm not sure which package you mean ({slot}).", "question")
        if not d.known:
            return Answer(f"I can't install packages on {d.name}.", "refused")
        missing = []
        if d.pkg == "dnf":
            for n in names:
                r = execute.run(["dnf", "-C", "-q", "info", n], policy=self.p, timeout=60)
                if r.exit_code != 0 and "No matching" in (r.stdout + r.stderr):
                    missing.append(n)
        if missing:
            return Answer(f"I couldn't find a package called {', '.join(missing)} in your software sources. "
                          "Check the exact name (e.g. with: dnf search NAME).", "not_done")
        check = {"dnf": "rpm -q {}", "zypper": "rpm -q {}", "apt": "dpkg-query -W {}", "pacman": "pacman -Q {}"}[d.pkg]
        return self._run_plan(f"install {', '.join(names)}", [{"do": "pkg_install", "packages": names}],
                              [check.format(n) for n in names])

    def do_remove(self, rec, slot):
        d = distro.detect()
        names, bad = self._pkg_names(slot)
        if bad or not names:
            return Answer(f"I'm not sure which package you mean ({slot}).", "question")
        if not d.known:
            return Answer(f"I can't remove packages on {d.name}.", "refused")
        have = steps.Backend(d.pkg).installed(self.p, names)
        absent = [n for n in names if n not in have]
        if absent:
            return Answer(f"{', '.join(absent)} is not installed as a system package, so there is nothing to remove. "
                          "(If it's a Flatpak app, ask: what apps are installed.)", "not_done")
        check = {"dnf": "! rpm -q {}", "zypper": "! rpm -q {}", "apt": "! dpkg-query -W -f='${{db:Status-Abbrev}}' {} | grep -q ^ii",
                 "pacman": "! pacman -Q {}"}[d.pkg]
        return self._run_plan(f"remove {', '.join(names)}", [{"do": "pkg_remove", "packages": names}],
                              [check.format(n) for n in names])

    def do_stop_program(self, rec, slot):
        name = slot.split()[0]
        running = _names_running()
        if name.lower() not in running:
            scope, unit = _unit(self.p, name)
            if scope:
                return self._service(scope, unit, "stop")
            sug = intents.closest(name.lower(), running)
            return Answer(f"No program called {name} is running." +
                          (f" Running programs with similar names: {', '.join(running[s] for s in sug)}." if sug else ""),
                          "not_done")
        real = running[name.lower()]
        return self._run_plan(f"close {real}", [{"do": "kill", "name": real}],
                              [f"! pgrep -r R,S,D -x {real}"])

    def _service(self, scope, unit, op):
        do = "user_service" if scope == "user" else "system_service"
        want = {"start": ("ActiveState", "active"), "restart": ("ActiveState", "active"),
                "stop": ("ActiveState", "inactive|failed"),
                "disable": ("UnitFileState", "disabled|static|masked|indirect")}[op]
        return self._run_plan(f"{op} {unit}", [{"do": do, "unit": unit, "op": op}],
                              [_verify_unit(scope, unit, want)])

    def _service_recipe(self, slot, op):
        name = slot.split()[0]
        scope, unit = _unit(self.p, name)
        if scope is None:
            return Answer(f"There is no service called {name}." +
                          (f" Similar: {', '.join(unit[:4])}." if unit else ""), "not_done")
        return self._service(scope, unit, op)

    def do_restart_service(self, rec, slot):
        return self._service_recipe(slot, "restart")

    def do_stop_service(self, rec, slot):
        return self._service_recipe(slot, "stop")

    def do_start_service(self, rec, slot):
        return self._service_recipe(slot, "start")

    def do_disable_autostart(self, rec, slot):
        return self._service_recipe(slot, "disable")

    def do_empty_trash(self, rec, slot):
        return self._run_plan("empty the desktop Trash", [
            {"do": "trash", "path": "~/.local/share/Trash/files", "missing_ok": True},
            {"do": "trash", "path": "~/.local/share/Trash/info", "missing_ok": True},
            {"do": "purge_trash", "this_plan": True}],
            ["test ! -e ~/.local/share/Trash/files"])
