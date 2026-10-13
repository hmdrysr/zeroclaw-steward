"""Plan lifecycle: validate -> preflight -> approve -> execute -> verify -> report.

The AI thinks once and submits a whole plan. Steward does the rest and only
comes back to the AI when reality differs from the plan (needs_input), or
when the plan itself is wrong (needs_fix). Steward, not the AI, decides risk,
asks you, verifies, and writes the verdict.
"""
import hashlib
import json
import re
import threading
import time

from . import approval, config, looks, steps, zeroclaw
from .redact import redact
from .steps import ASK, AUTO, NO, READ, Bad
from .store import Store

CHANGE_TIERS = (AUTO, ASK)
TERMINAL_STATES = ("finished", "rejected", "declined", "expired")


def _tail_lines(text, n):
    lines = (text or "").rstrip("\n").splitlines()
    return lines[-n:], len(lines)


def evidence(out, err, n=14):
    """Most useful lines for diagnosing a failure: error-looking lines first, then the tail."""
    lines = [l for l in (err or "").splitlines() if l.strip()] + \
            [l for l in (out or "").splitlines() if l.strip()]
    hot = [l for l in lines if re.search(r"(?i)error|fail|denied|not found|no such|cannot|"
                                         r"refus|invalid|timed out|killed|unable", l)]
    picked, seen = [], set()
    for l in hot[-8:] + lines[-n:]:
        if l not in seen:
            seen.add(l)
            picked.append(l[:300])
    return picked[-n:]


def target_key(step):
    do = step.get("do")
    if do in ("trash", "write_file", "edit_file"):
        try:
            return ("path", str(steps.safe_path(step.get("path"))))
        except Bad:
            return None
    if do in ("user_service", "system_service"):
        u = str(step.get("unit", ""))
        return (do, u if "." in u else u + ".service")
    if do in ("dnf_install", "dnf_remove", "pkg_install", "pkg_remove"):
        return ("pkg",)
    return None


def generic_line(step):
    do = step.get("do", "?")
    bits = [f"{k}={v}" for k, v in step.items() if k not in steps.COMMON and k != "content"]
    return steps.short(f"{do.upper()} {' '.join(bits)} (checked again when it runs)")


class Engine:
    def __init__(self, store=None, policy=None, server_name="steward"):
        self.policy = policy or config.load_policy()
        self.store = store or Store()
        self.server_name = server_name
        self.recovered = self.store.recover()
        for rid in self.recovered:
            run = self.store.run(rid)
            plan = self.store.plan(run["plan_id"]) if run and run["plan_id"] else None
            if plan and run["step_no"] is not None:
                for row in plan["steps"] or []:
                    if row["n"] == run["step_no"]:
                        row["status"], row["run"] = "interrupted", rid
                self.store.save_plan(plan["id"], "step_marked_interrupted", {"run": rid},
                                     steps=plan["steps"])
        self.pres = {}            # plan_id -> {n: Pre or "deferred"}
        self.lock = threading.RLock()
        self.active = None
        self.wakes = {}

    # ================================================================ gates
    def gate(self):
        if not self.policy.require_zeroclaw_gate:
            return []
        return zeroclaw.check(self.policy, self.server_name)["errors"]

    # ================================================================ looks
    def look(self, run=None, preset=None, lines=None, raw=False):
        lines = max(5, min(int(lines or self.policy.look_lines), 400))
        if bool(run) == bool(preset):
            return "error: give exactly one of run or preset (overview, memory, disk, services, " \
                   "startup, ports, logs, big_files, packages, network)"
        if preset:
            if preset not in steps_presets():
                return f"error: unknown preset {preset!r}; valid: {', '.join(steps_presets())}"
            rid = self.store.begin_run(None, None, "look", "preset", {"preset": preset}, READ,
                                       None, preset, "auto")
            code, text, errs = looks.run_preset(preset, self.policy)
            self.store.end_run(rid, status="ok", exit_code=code, stdout=redact(text),
                               stderr=redact(errs), changed="no", sandbox="native-fixed")
            if raw:
                return self._show(rid, lines, f"r{rid} {preset}")
            from .english.outputs import summarise_sections
            facts = summarise_sections(redact(text))
            out = f"r{rid} {preset} (raw: output(ref='r{rid}')):\n" + "\n".join("- " + f for f in facts)
            if errs.strip():
                out += "\nerrors: " + "; ".join(l.split(": ", 1)[-1] for l in redact(errs).splitlines())[:300]
            return out
        if not isinstance(run, str) or len(run) > 2000:
            return "error: run must be a command string (max 2000 chars)"
        sp = looks.syntax_problem(run)
        if sp:
            return f"error: {sp}"
        rid = self.store.begin_run(None, None, "look", "inspect", {"run": run}, READ, None, run,
                                   "auto")
        r = looks.run_readonly(run, self.policy, timeout=60,
                               on_pid=lambda pid: self.store.set_pid(rid, pid))
        self.store.end_run(rid, status="ok" if r.exit_code == 0 else "failed",
                           exit_code=r.exit_code, stdout=r.stdout, stderr=r.stderr,
                           truncated=int(r.stdout_truncated or r.stderr_truncated), changed="no",
                           failure=None if r.exit_code == 0 else steps.failure_kind(r),
                           sandbox=r.sandbox)
        hint = ""
        if r.exit_code == 0 and not raw:
            from .english.outputs import translate_command
            facts = translate_command(run, r.stdout)
            if facts:
                return (f"r{rid} exit 0 (raw: output(ref='r{rid}')):\n"
                        + "\n".join("- " + f for f in facts))
        if r.exit_code != 0:
            from .english.errors import translate as tr_err
            _, meaning = tr_err(r.stderr, r.exit_code, r.stdout)
            if meaning:
                hint += "\nin plain English: " + meaning
        if r.exit_code != 0 and r.sandbox == "ro":
            hint += ("\nnote: read-only sandbox (no network, no D-Bus, private /tmp). systemctl, "
                    "loginctl, nmcli, ss, ip, flatpak, rpm and dnf queries run natively only when "
                    "they are the first command of the line.")
        return self._show(rid, lines, f"r{rid} exit {r.exit_code} [{r.sandbox}]") + hint

    def _show(self, rid, n, head):
        run = self.store.run(rid)
        out = (run["stdout"] or "").rstrip("\n").splitlines()
        err = (run["stderr"] or "").strip()
        body = out[:n]
        text = head + "\n" + "\n".join(body)
        if len(out) > n:
            text += f"\n[+{len(out) - n} more lines: output(ref='r{rid}', grep=..., start={n + 1})]"
        if err:
            el, total = _tail_lines(err, 12)
            text += "\nstderr:\n" + "\n".join(el)
        return text

    def output(self, ref, grep=None, start=1, lines=150, stream="stdout"):
        m = re.fullmatch(r"r?(\d+)", str(ref or ""))
        if not m:
            return "error: ref must look like r12"
        run = self.store.run(int(m.group(1)))
        if not run:
            return f"error: no run {ref}"
        if stream not in ("stdout", "stderr"):
            return "error: stream must be stdout or stderr"
        all_lines = (run[stream] or "").splitlines()
        lines = max(1, min(int(lines or 150), 500))
        if grep:
            try:
                rx = re.compile(grep)
            except re.error as e:
                return f"error: bad regex: {e}"
            sel = [(i + 1, l) for i, l in enumerate(all_lines) if rx.search(l)]
        else:
            s = max(1, int(start or 1))
            sel = [(i, all_lines[i - 1]) for i in range(s, min(len(all_lines), s + lines - 1) + 1)]
        shown = sel[:lines]
        more = f"\n[{len(sel) - len(shown)} more matches]" if len(sel) > len(shown) else ""
        return f"r{run['id']} {stream}: {len(all_lines)} lines, showing {len(shown)}\n" + \
            "\n".join(f"{n}: {l}" for n, l in shown) + more

    # ================================================================ submit
    def submit(self, goal, plan_steps, verify=None, require_verify=True):
        if not isinstance(goal, str) or not goal.strip():
            return "error: goal must be a short non-empty string"
        if not isinstance(plan_steps, list) or not plan_steps:
            return "error: steps must be a non-empty list"
        if len(plan_steps) > self.policy.max_steps_per_plan:
            return f"error: at most {self.policy.max_steps_per_plan} steps per plan"
        if verify is not None and (not isinstance(verify, list) or
                                   not all(isinstance(v, str) and v.strip() for v in verify)):
            return "error: verify must be a list of read-only command strings"
        if self.store.plans_since(time.time() - 3600) >= self.policy.max_plans_per_hour:
            return "blocked: too many plans in the last hour (loop guard). Stop and tell the user."
        with self.lock:
            if self.active:
                return f"blocked: plan {self.active} is still running; use status(plan='{self.active}', wait=true)"
        pid = self.store.new_plan(redact(goal.strip())[:300], plan_steps)
        rows, problems, pres = self._preflight_all(pid, plan_steps, 1)
        verify = verify or []
        for v in verify:
            sp = looks.syntax_problem(v)
            if sp:
                problems.append(f"verify {v!r}: {sp}")
        n_changes = sum(1 for r in rows if r["tier"] in CHANGE_TIERS and not r.get("noop"))
        if n_changes and not verify and require_verify:
            problems.append("plan changes the system but has no 'verify': add read-only command(s) "
                            "that exit 0 only when the user's goal is achieved")
        if n_changes > self.policy.max_changes_per_plan:
            problems.append(f"more than {self.policy.max_changes_per_plan} changes in one plan")
        if n_changes:
            gate = self.gate()
            if gate:
                self.store.save_plan(pid, "gate_blocked", {"errors": gate}, state="rejected",
                                     steps=rows)
                return (f"plan {pid} BLOCKED before anything ran: Steward will not make changes "
                        "while ZeroClaw's setup lets the AI bypass it or approve silently:\n- " +
                        "\n- ".join(gate) + "\nTell the user to run `steward doctor`. "
                        "Read-only looks still work.")
        if problems:
            self.store.save_plan(pid, "rejected", {"problems": problems}, state="rejected", steps=rows)
            return (f"plan {pid} NEEDS FIX (nothing ran). Fix and call plan again:\n- " +
                    "\n- ".join(problems) +
                    "\nIf a step is prohibited, do not try another route to the same thing; tell the user.")
        self.pres[pid] = pres
        self.store.save_plan(pid, "preflight_ok", None, steps=rows, state="new",
                             report={"verify": verify})
        return self._seek_approval(pid)

    def _preflight_all(self, pid, plan_steps, first_n, prior=None):
        rows, problems, pres = [], [], {}
        seen = {}
        prior = list(prior or [])
        for i, st in enumerate(plan_steps):
            n = first_n + i
            row = {"n": n, "step": st, "status": "pending", "attempts": 0}
            if isinstance(st, dict) and "undoes" in st:
                problems.append(f"step {n}: 'undoes' is set by Steward, not by the plan")
                rows.append(row | {"do": st.get("do"), "tier": None, "line": ""})
                continue
            if isinstance(st, dict) and st.get("do") == "undo":
                try:
                    exp = self._expand_undo(st)
                except Bad as e:
                    problems.append(f"step {n} (undo): {e}")
                    rows.append(row | {"do": "undo", "tier": NO, "line": "UNDO"})
                    continue
                st = exp
                row["step"] = st
            try:
                pre = steps.preflight(st, self.policy)
            except steps.Missing as e:
                key = target_key(st)
                earlier = [r["step"] for r in prior + rows]
                touched = any(isinstance(s, dict) and (s.get("do") == "shell" or
                                                       (key and target_key(s) == key)) for s in earlier)
                if touched:
                    raw = hashlib.sha256(json.dumps(st, sort_keys=True).encode()).hexdigest()[:16]
                    rows.append(row | {"do": st.get("do"), "tier": ASK, "line": generic_line(st),
                                       "fp": "raw:" + raw, "deferred": str(e)[:200]})
                    pres[n] = "deferred"
                    continue
                problems.append(f"step {n} ({st.get('do')}): {e}")
                rows.append(row | {"do": st.get("do"), "tier": None, "line": ""})
                continue
            except Bad as e:
                problems.append(f"step {n} ({st.get('do') if isinstance(st, dict) else '?'}): {e}")
                rows.append(row | {"do": st.get("do") if isinstance(st, dict) else None,
                                   "tier": None, "line": ""})
                continue
            except OSError as e:
                problems.append(f"step {n}: could not inspect target: {e}")
                rows.append(row | {"do": st.get("do"), "tier": None, "line": ""})
                continue
            row.update(do=pre.do, tier=pre.tier, line=pre.line, extra=pre.extra_lines,
                       fp=pre.fp, irreversible=pre.irreversible, network=pre.network,
                       detail=pre.detail[:4000])
            if pre.noop:
                row["noop"] = pre.noop
            if isinstance(st, dict) and st.get("undoes"):
                row["undoes"] = st["undoes"]
            if pre.tier == NO:
                problems.append(f"step {n}: PROHIBITED by local policy: {pre.line} - {pre.detail}")
            elif pre.tier in CHANGE_TIERS and not pre.noop:
                if pre.fp in seen:
                    problems.append(f"step {n} repeats step {seen[pre.fp]}")
                seen[pre.fp] = n
                recent = self.store.recent_success(pre.fp, time.time() - self.policy.repeat_window_secs)
                if recent and not st.get("again"):
                    problems.append(f"step {n}: the same change was already made recently (plan "
                                    f"{recent['plan_id']}, r{recent['id']}); add again:true only if "
                                    "it really must be repeated")
            pres[n] = pre
            rows.append(row)
        return rows, problems, pres

    def _expand_undo(self, st):
        extra = set(st) - {"do", "ref"} - steps.COMMON
        if extra:
            raise Bad(f"unknown field(s) {sorted(extra)}")
        ref = str(st.get("ref", ""))
        if re.fullmatch(r"r\d+", ref):
            runs = [self.store.run(int(ref[1:]))]
            if not runs[0]:
                raise Bad(f"no run {ref}")
        elif re.fullmatch(r"p[0-9a-f]{5}", ref):
            runs = [r for r in self.store.runs(ref) if r["kind"] == "step"][::-1]
            if not runs:
                raise Bad(f"no plan {ref} or it made no changes")
        else:
            raise Bad("ref must be a plan id (p1a2b3) or run id (r12)")
        inv = [r for r in runs if r and r["status"] == "ok" and r["changed"] == "yes"
               and r["inverse"] and not r["undone_by"]]
        if not inv:
            raise Bad(f"nothing undoable in {ref} (no recorded undo, or already undone)")
        if len(inv) > 1:
            raise Bad(f"{ref} has {len(inv)} undoable changes; undo them one by one: " +
                      ", ".join(f"r{r['id']}" for r in inv))
        return dict(inv[0]["inverse"]) | {"undoes": inv[0]["id"]}

    # ================================================================ approval
    def _approval_package(self, pid, plan):
        rows = plan["steps"]
        need = [r for r in rows if r["status"] == "pending" and r["tier"] == ASK and not r.get("noop")
                and r["fp"] not in plan["approved"]]
        auto = [r for r in rows if r["status"] == "pending" and r["tier"] == AUTO and not r.get("noop")]
        reads = [r for r in rows if r["status"] == "pending" and r["tier"] == READ]
        noops = [r for r in rows if r["status"] == "pending" and r.get("noop")]
        if not need:
            return None
        head = steps.short(f"Plan {pid} '{plan['goal']}': {len(need)} change(s) need your OK"
                           + (f", {len(auto)} automatic" if auto else ""), 78)
        lines = [head]
        for r in need:
            lines.append(steps.short(f"{r['n']}) {r['line']}", 78))
            lines += [steps.short("   " + x.strip(), 78) for x in r.get("extra") or []]
            if r.get("irreversible"):
                lines.append("   cannot be undone")
            if r["step"].get("only_if"):
                lines.append(steps.short("   only if: " + r["step"]["only_if"], 78))
        if len(lines) > 31:
            lines = lines[:30] + [f"... and more: see `steward show {pid}`"]
        detail = [f"Plan {pid}: {plan['goal']}", f"Revision {plan['revision']}", "",
                  "CHANGES THAT NEED YOUR APPROVAL:"]
        for r in need:
            detail += [f"\n[{r['n']}] {r['line']}"] + [f"    {x.strip()}" for x in r.get("extra") or []]
            if r.get("detail"):
                detail.append("    " + r["detail"].replace("\n", "\n    "))
            if r.get("deferred"):
                detail.append(f"    (target does not exist yet: {r['deferred']}; checked again "
                              "right before it runs)")
            if r["step"].get("only_if"):
                detail.append(f"    only if this succeeds: {r['step']['only_if']}")
            if r.get("irreversible"):
                detail.append("    CANNOT BE UNDONE")
            if r.get("network"):
                detail.append("    has network access")
        if auto:
            detail += ["", "AUTOMATIC (reversible, inside your auto-approved areas):"] + \
                [f"  [{r['n']}] {r['line']}" for r in auto]
        if reads:
            detail += ["", "READ-ONLY LOOKS/CHECKS:"] + [f"  [{r['n']}] {r['line']}" for r in reads]
        if noops:
            detail += ["", "ALREADY DONE (will be skipped):"] + \
                [f"  [{r['n']}] {r['line']} - {r['noop']}" for r in noops]
        v = (plan.get("report") or {}).get("verify") or []
        if v:
            detail += ["", "GOAL CHECKS AFTERWARDS:"] + [f"  {c}" for c in v]
        hashes = sorted(r["fp"] for r in need)
        seal = hashlib.sha256(json.dumps([pid, plan["revision"], lines, hashes]).encode()).hexdigest()[:10]
        return {"lines": lines, "hashes": hashes, "seal": seal, "created": time.time(),
                "detail": "\n".join(detail)}

    def _seek_approval(self, pid):
        plan = self.store.plan(pid)
        pkg = self._approval_package(pid, plan)
        if pkg is None:
            self.store.save_plan(pid, "no_approval_needed", None, state="approved")
            return self._start_and_wait(pid)
        self.store.save_plan(pid, "approval_requested",
                             {"mode": self.policy.approval_mode, "lines": pkg["lines"]},
                             state="awaiting_approval", approval=pkg)
        mode = self.policy.approval_mode
        if mode == "desktop":
            return self._desktop_then_run(pid, pkg)
        if mode == "inline":
            decision, why = approval.inline(f"Steward: approve plan {pid}?", pkg["detail"],
                                            self.policy.approval_timeout_secs)
            self.store.save_plan(pid, f"approval_{decision}", {"source": "terminal-inline", "detail": why})
            if decision != "approved":
                return self._not_approved(pid, decision, why)
            self._grant(pid, pkg, "terminal-inline")
            return self._start_and_wait(pid)
        if mode == "terminal":
            return (f"plan {pid} WAITING for the user's OK in a terminal. Tell the user to run:\n"
                    f"  steward approve {pid}\nthen call status(plan='{pid}', wait=true). "
                    f"Changes needing OK:\n" + "\n".join(pkg["lines"][1:]))
        args = {"plan": pid, "seal": pkg["seal"]} | {f"n{i:02d}": l for i, l in enumerate(pkg["lines"])}
        return (f"plan {pid} NEEDS APPROVAL. Call {self.server_name}__approve with exactly these "
                f"arguments (the user sees them in the approval prompt; do not change them):\n" +
                json.dumps(args, ensure_ascii=False))

    def _desktop_then_run(self, pid, pkg, t_used=0.0):
        t0 = time.time()
        decision, why = approval.desktop(f"Steward: approve plan {pid}?", pkg["detail"],
                                         self.policy.approval_timeout_secs)
        self.store.save_plan(pid, f"approval_{decision}", {"source": "desktop", "detail": why})
        if decision != "approved":
            return self._not_approved(pid, decision, why)
        self._grant(pid, pkg, "desktop")
        return self._start_and_wait(pid, budget=self.policy.wait_secs - (time.time() - t0) - t_used)

    def _not_approved(self, pid, decision, why):
        state = "declined" if decision == "declined" else "declined"
        self.store.save_plan(pid, None, None, state=state,
                             report=(self.store.plan(pid).get("report") or {}) |
                             {"verdict": "not_approved", "why": why})
        hint = {"declined": "The user said no. Do not retry or work around it; ask what they want.",
                "timeout": "No answer in time. Ask the user before resubmitting.",
                "unavailable": "The approval dialog could not be shown. Tell the user (run "
                               "`steward doctor`); do not try another way.",
                "error": "The approval dialog failed. Tell the user (run `steward doctor`)."}
        return (f"plan {pid} NOT APPROVED ({decision}): {why}. Nothing was changed.\n"
                f"{hint.get(decision, '')}")

    def _grant(self, pid, pkg, source):
        plan = self.store.plan(pid)
        approved = sorted(set(plan["approved"] or []) | set(pkg["hashes"]))
        self.store.save_plan(pid, "approved", {"source": source, "hashes": pkg["hashes"]},
                             approved=approved, state="approved")

    def approve(self, pid, seal, line_args):
        plan = self.store.plan(pid) if isinstance(pid, str) else None
        if not plan:
            return f"error: no plan {pid!r}"
        if plan["state"] != "awaiting_approval":
            return f"error: plan {pid} is {plan['state']}, not waiting for approval"
        pkg = plan["approval"]
        if time.time() - pkg["created"] > self.policy.approval_valid_secs:
            self.store.save_plan(pid, "approval_expired", None, state="expired")
            return f"plan {pid} EXPIRED (waited too long). Submit it again if still wanted."
        mode = self.policy.approval_mode
        if mode not in ("chat", "both"):
            return f"error: approval mode is {mode!r}; approve is only used in chat/both mode"
        given = [line_args.get(f"n{i:02d}") for i in range(len(pkg["lines"]))]
        extra = set(line_args) - {f"n{i:02d}" for i in range(len(pkg["lines"]))}
        if seal != pkg["seal"] or given != pkg["lines"] or extra:
            self.store.save_plan(pid, "approval_args_mismatch", {"seal": seal})
            return ("REFUSED: approve arguments differ from what Steward issued. Call approve again "
                    "with exactly the arguments from the needs-approval message.")
        gate = zeroclaw.check(self.policy, self.server_name)["errors"]
        if gate:
            self.store.save_plan(pid, "approval_gate_failed", {"errors": gate})
            return ("REFUSED: cannot trust this approval because ZeroClaw's config does not "
                    "guarantee the user was asked:\n- " + "\n- ".join(gate))
        self.store.save_plan(pid, "approval_chat_ok", {"source": "zeroclaw"})
        if mode == "both":
            return self._desktop_then_run(pid, pkg)
        self._grant(pid, pkg, "zeroclaw-chat")
        return self._start_and_wait(pid)

    def terminal_approve(self, pid, yes):
        plan = self.store.plan(pid)
        if not plan or plan["state"] != "awaiting_approval":
            return False, f"plan {pid} is not waiting for approval"
        pkg = plan["approval"]
        if not yes:
            self.store.save_plan(pid, "approval_declined", {"source": "terminal"})
            self._not_approved(pid, "declined", "declined in the terminal")
            return True, "declined"
        self._grant(pid, pkg, "terminal")
        self._start(pid)
        return True, "approved; running"

    # ================================================================ execution
    def _start(self, pid):
        with self.lock:
            if self.active and self.active != pid:
                return False
            if self.active == pid:
                return True
            self.active = pid
            ev = self.wakes.setdefault(pid, threading.Event())
            ev.clear()
        self.store.save_plan(pid, "execution_started", None, state="running")
        threading.Thread(target=self._run_plan, args=(pid,), daemon=True).start()
        return True

    def _start_and_wait(self, pid, budget=None):
        if not self._start(pid):
            return f"blocked: another plan ({self.active}) is running"
        return self._wait(pid, budget)

    def _wait(self, pid, budget=None):
        budget = max(30.0, budget if budget is not None else self.policy.wait_secs)
        ev = self.wakes.setdefault(pid, threading.Event())
        ev.wait(timeout=budget)
        return self.describe(pid)

    def _run_plan(self, pid):
        try:
            self._execute(pid)
        except Exception as e:  # never leave a plan silently 'running'
            self.store.save_plan(pid, "engine_error", {"error": repr(e)}, state="needs_input",
                                 question={"n": None, "problem": f"internal error: {e!r}"})
        finally:
            with self.lock:
                if self.active == pid:
                    self.active = None
            self.wakes.setdefault(pid, threading.Event()).set()

    def _execute(self, pid):
        plan = self.store.plan(pid)
        rows = plan["steps"]
        pres = self.pres.get(pid, {})
        for row in rows:
            if row["status"] != "pending":
                continue
            if self.store.cancelled(pid):
                self._finish(pid, stopped=True)
                return
            pause = self._exec_step(pid, plan, row, pres)
            self.store.save_plan(pid, None, None, steps=rows)
            if self.store.cancelled(pid):
                self._finish(pid, stopped=True)
                return
            if pause == "ask":
                return
            if pause == "stop":
                self._finish(pid)
                return
        self._goal_checks(pid)

    def _exec_step(self, pid, plan, row, pres):
        st = row["step"]
        n = row["n"]
        cancel = lambda: self.store.cancelled(pid)
        if st.get("only_if"):
            rid = self.store.begin_run(pid, n, "probe", "only_if", {"run": st["only_if"]}, READ,
                                       None, st["only_if"], "auto")
            r = looks.run_readonly(st["only_if"], self.policy, timeout=60, cancel=cancel)
            self.store.end_run(rid, status="ok", exit_code=r.exit_code, stdout=r.stdout,
                               stderr=r.stderr, changed="no", sandbox=r.sandbox)
            if r.exit_code != 0:
                row["status"] = "skipped_if"
                return None
        pre = pres.get(n)
        if pre is None or pre == "deferred" or row.get("noop"):
            try:
                fresh = steps.preflight(st, self.policy)
            except (Bad, OSError) as e:
                return self._deviation(pid, row, None, f"step is not valid now: {e}", "no")
            if fresh.tier == NO:
                return self._deviation(pid, row, None, f"now prohibited: {fresh.detail}", "no")
            if fresh.noop:
                row["status"] = "noop"
                row["noop"] = fresh.noop
                return None
            if row.get("noop") or pre is None:
                # situation changed since approval: only automatic steps may proceed
                if fresh.tier == ASK and fresh.fp not in (plan["approved"] or []):
                    return self._deviation(pid, row, None, "the situation changed since the plan "
                                           f"was approved; this now needs a change: {fresh.line}", "no")
            elif pre == "deferred":
                if fresh.tier == ASK and row["fp"] not in (plan["approved"] or []):
                    return self._deviation(pid, row, None, "not approved", "no")
            pre = fresh
            row.update(line=pre.line, tier=pre.tier)
        elif pre.tier == ASK and pre.fp not in (self.store.plan(pid)["approved"] or []):
            return self._deviation(pid, row, None, "not approved (internal guard)", "no")
        if pre.tier == NO:
            return self._deviation(pid, row, None, "prohibited", "no")
        if pre.recheck:
            prob = pre.recheck()
            if prob:
                return self._deviation(pid, row, None, f"target changed since approval: {prob}", "no")
        kind = "step" if pre.tier in CHANGE_TIERS else "read"
        approval_src = "auto" if pre.tier in (READ, AUTO) else "approved"
        rid = self.store.begin_run(pid, n, kind, pre.do, pre.params, pre.tier, pre.fp,
                                   pre.line, approval_src)
        row["run"] = rid
        row["attempts"] = row.get("attempts", 0) + 1
        ctx = {"plan": pid, "cancel": cancel, "on_pid": lambda p: self.store.set_pid(rid, p)}
        try:
            r = pre.run(ctx)
        except Exception as e:
            r = steps.simple_res("failed", err=f"internal error: {e!r}", changed="unknown",
                                 failure="internal_error")
        fails = steps.check_expect(st.get("expect"), (r["stdout"] or "") + (r["stderr"] or ""),
                                   r["exit_code"])
        check_info = None
        if st.get("check") and r["status"] == "ok":
            cr = looks.run_readonly(st["check"], self.policy, timeout=60, cancel=cancel)
            check_info = {"run": st["check"], "exit": cr.exit_code,
                          "out": "\n".join(_tail_lines(cr.stdout + cr.stderr, 6)[0])}
            if cr.exit_code != 0:
                fails.append(f"check failed (exit {cr.exit_code}): {st['check']}")
        verify = r.get("verify") or {}
        if check_info:
            verify = dict(verify) | {"step_check": check_info}
        status = r["status"]
        if status == "ok" and fails:
            status = "deviated"
        self.store.end_run(rid, status=status, exit_code=r["exit_code"],
                           stdout=redact(r["stdout"] or ""), stderr=redact(r["stderr"] or ""),
                           truncated=r.get("truncated", 0), changed=r["changed"], verify=verify,
                           inverse=r.get("inverse"), failure=r.get("failure") or
                           ("expectation" if fails else None), warnings=r.get("warnings"),
                           sandbox=r.get("sandbox"))
        if status == "ok" and st.get("undoes"):
            self.store.update_run(st["undoes"], undone_by=rid)
        row["status"] = status
        if status == "ok":
            return None
        problem = "; ".join(fails) if fails else \
            f"{status} (exit {r['exit_code']}, {r.get('failure')})"
        return self._deviation(pid, row, rid, problem, r["changed"])

    def _deviation(self, pid, row, rid, problem, changed):
        st = row["step"]
        if row["status"] in ("pending",):
            row["status"] = "blocked"
        on_fail = st.get("on_fail", "ask")
        self.store.save_plan(pid, "deviation", {"n": row["n"], "problem": problem,
                                                "on_fail": on_fail})
        if on_fail == "skip" and changed in ("no", "yes"):
            row["status"] = row["status"] + "+skipped"
            return None
        if on_fail == "stop":
            return "stop"
        q = {"n": row["n"], "problem": problem, "changed": changed, "run": rid}
        self.store.save_plan(pid, None, None, state="needs_input", question=q)
        return "ask"

    def _goal_checks(self, pid):
        plan = self.store.plan(pid)
        verify = (plan.get("report") or {}).get("verify") or []
        results = []
        for c in verify:
            rid = self.store.begin_run(pid, None, "goal", "verify", {"run": c}, READ, None, c, "auto")
            r = looks.run_readonly(c, self.policy, timeout=60,
                                   cancel=lambda: self.store.cancelled(pid))
            self.store.end_run(rid, status="ok" if r.exit_code == 0 else "failed",
                               exit_code=r.exit_code, stdout=r.stdout, stderr=r.stderr,
                               changed="no", sandbox=r.sandbox)
            results.append({"run": c, "rid": rid, "passed": r.exit_code == 0})
        rep = (plan.get("report") or {}) | {"goal_checks": results}
        self.store.save_plan(pid, "goal_checked", {"results": results}, report=rep)
        if results and not all(x["passed"] for x in results) and not rep.get("goal_accepted"):
            bad = [x for x in results if not x["passed"]]
            self.store.save_plan(pid, None, None, state="needs_input",
                                 question={"n": "goal", "problem": "goal check(s) failed: " +
                                           "; ".join(x["run"] for x in bad),
                                           "changed": "yes", "run": bad[0]["rid"]})
            return
        self._finish(pid)

    def _finish(self, pid, stopped=False):
        plan = self.store.plan(pid)
        rows = plan["steps"]
        rep = plan.get("report") or {}
        runs = {r["id"]: r for r in self.store.runs(pid)}
        goal = rep.get("goal_checks") or []
        goal_ok = bool(goal) and all(x["passed"] for x in goal)
        changed_ok, uncertain, bad = [], False, False
        for r in rows:
            run = runs.get(r.get("run")) if r.get("run") else None
            s = r["status"]
            if s not in ("ok", "noop", "skipped_if", "pending", "replaced"):
                bad = True
            if s == "interrupted":
                uncertain = True
            if not run or run["kind"] != "step":
                continue
            if run["status"] == "ok" and run["changed"] in ("yes", "unknown"):
                changed_ok.append(r)
                if run["changed"] == "unknown" and not goal_ok:
                    uncertain = True
            elif run["status"] == "interrupted" or (run["changed"] == "unknown" and
                                                    run["status"] != "ok"):
                uncertain = True
        for run in runs.values():   # belt and braces: any half-finished change anywhere in the plan
            if run["kind"] == "step" and (run["status"] == "interrupted" or
                                          (run["status"] != "ok" and run["changed"] == "unknown")):
                uncertain = True
        has_changes = any(r["tier"] in CHANGE_TIERS for r in rows)
        if stopped or self.store.cancelled(pid):
            verdict = "stopped"
        elif uncertain:
            verdict = "uncertain"
        elif bad or (goal and not goal_ok):
            verdict = "partial" if (changed_ok or goal_ok) else "failed"
        elif has_changes and changed_ok:
            verdict = "verified" if goal_ok else "unverified"
        elif has_changes:
            verdict = "verified" if goal_ok else "unverified"
        else:
            verdict = "done"
        for r in rows:
            if r["status"] == "pending":
                r["status"] = "not_run"
        rep = rep | {"verdict": verdict, "changes": len(changed_ok)}
        self.store.save_plan(pid, "plan_finished", {"verdict": verdict}, state="finished",
                             report=rep, steps=rows, question=None)

    # ================================================================ resume
    def resume(self, pid, action, new_steps=None):
        plan = self.store.plan(pid) if isinstance(pid, str) else None
        if not plan:
            return f"error: no plan {pid!r}"
        if plan["state"] not in ("needs_input", "interrupted"):
            return f"error: plan {pid} is {plan['state']}; resume only answers needs_input"
        if action not in ("retry", "skip", "abort", "replace"):
            return "error: action must be retry, skip, abort or replace"
        q = plan["question"] or {}
        rows = plan["steps"]
        if action == "abort":
            self.store.save_plan(pid, "aborted_by_ai", None, question=None, steps=rows)
            self._finish(pid)
            return self.describe(pid)
        if q.get("n") == "goal":
            if action == "skip":
                rep = plan["report"] | {"goal_accepted": True}
                self.store.save_plan(pid, "goal_failure_accepted", None, report=rep)
                self._finish(pid)
                return self.describe(pid)
            if action == "retry":
                self.store.save_plan(pid, None, None, state="running")
                self._goal_checks(pid)
                return self.describe(pid)
            idx = len(rows)
        else:
            idx = next((i for i, r in enumerate(rows) if r["n"] == q.get("n")), None)
            if idx is None:
                idx = next((i for i, r in enumerate(rows) if r["status"] in
                            ("pending", "interrupted", "blocked")), len(rows))
        if plan["state"] == "interrupted":
            # After a crash, nothing approved earlier is trusted for unexecuted steps.
            plan["approved"] = []
            self.store.save_plan(pid, "approvals_reset_after_interruption", None, approved=[])
        if action == "skip":
            if idx < len(rows):
                rows[idx]["status"] = rows[idx]["status"] + "+skipped" \
                    if rows[idx]["status"] not in ("pending",) else "skipped"
        elif action == "retry":
            if idx >= len(rows):
                return "error: nothing to retry"
            r = rows[idx]
            if r.get("attempts", 0) > self.policy.max_retries_per_step:
                return (f"REFUSED: step {r['n']} already ran {r['attempts']} times. Choose skip, "
                        "replace or abort.")
            r["status"] = "pending"
        elif action == "replace":
            if not isinstance(new_steps, list) or not new_steps:
                return "error: replace needs steps (they replace the failed step and everything after it)"
            if plan["revision"] >= self.policy.max_revisions_per_plan:
                return "REFUSED: too many revisions of this plan; abort and tell the user"
            keep = rows[:idx]
            first_n = max(r["n"] for r in rows) + 1
            new_rows, problems, pres = self._preflight_all(pid, new_steps, first_n, prior=keep)
            if problems:
                return (f"plan {pid} replacement NEEDS FIX (nothing ran, plan still paused):\n- " +
                        "\n- ".join(problems))
            for r in rows[idx:]:
                if r["status"] == "pending":
                    r["status"] = "replaced"
            rows = keep + [r for r in rows[idx:] if r["status"] != "pending"] + new_rows
            self.pres.setdefault(pid, {}).update(pres)
            plan["revision"] += 1
        self.store.save_plan(pid, f"resume_{action}", None, steps=rows, question=None,
                             revision=plan["revision"], state="new")
        if self.pres.get(pid) is None or plan["state"] == "interrupted":
            fresh = {}
            for r in rows:
                if r["status"] == "pending":
                    try:
                        fresh[r["n"]] = steps.preflight(r["step"], self.policy)
                    except (Bad, OSError):
                        fresh[r["n"]] = None
            self.pres[pid] = fresh
        if any(r["status"] == "pending" and r["tier"] in CHANGE_TIERS for r in rows):
            if self.gate():
                return "BLOCKED: " + "; ".join(self.gate())
        return self._seek_approval(pid)

    # ================================================================ describe
    def status(self, pid=None, wait=False):
        if not pid:
            ps = self.store.plans(8)
            if not ps:
                return "no plans yet"
            return "\n".join(f"{p['id']} {p['state']} {(p['report'] or {}).get('verdict', '')} "
                             f"{p['goal'][:60]}" for p in ps)
        plan = self.store.plan(pid)
        if not plan:
            return f"error: no plan {pid!r}"
        if wait and plan["state"] in ("running", "approved"):
            return self._wait(pid)
        if plan["state"] == "awaiting_approval" and self.policy.approval_mode == "terminal" and wait:
            deadline = time.time() + self.policy.wait_secs
            while time.time() < deadline:
                p = self.store.plan(pid)
                if p["state"] != "awaiting_approval":
                    if p["state"] in ("running", "approved"):
                        return self._wait(pid, deadline - time.time())
                    break
                time.sleep(1)
        return self.describe(pid)

    def describe(self, pid):
        plan = self.store.plan(pid)
        rows = plan["steps"] or []
        st = plan["state"]
        runs = {r["id"]: r for r in self.store.runs(pid)}
        if st in ("running", "approved"):
            done = sum(1 for r in rows if r["status"] != "pending")
            return (f"plan {pid} RUNNING ({done}/{len(rows)} steps done). "
                    f"Call status(plan='{pid}', wait=true) to keep waiting.")
        if st == "awaiting_approval":
            return f"plan {pid} WAITING FOR APPROVAL"
        if st == "needs_input":
            q = plan["question"] or {}
            out = [f"plan {pid} NEEDS INPUT at step {q.get('n')}: {self._row_line(rows, q.get('n'))}",
                   f"problem: {q.get('problem')}", f"changed system: {q.get('changed')}"]
            if q.get("run"):
                r = runs.get(q["run"]) or self.store.run(q["run"])
                ev = evidence(r["stdout"], r["stderr"])
                if ev:
                    out.append(f"evidence (r{r['id']}; full: output(ref='r{r['id']}')):")
                    out += ["  " + l for l in ev]
                from .english.errors import translate as tr_err
                _, meaning = tr_err(r["stderr"], r["exit_code"], r["stdout"])
                if meaning and r["status"] not in ("deviated",):
                    out.append("in plain English: " + meaning)
                hint = self._hint(r)
                if hint:
                    out.append("hint: " + hint)
            done = [str(r["n"]) for r in rows if r["status"] in ("ok", "noop", "skipped_if")]
            rest = [str(r["n"]) for r in rows if r["status"] == "pending"]
            out.append(f"done: {','.join(done) or '-'}  remaining: {','.join(rest) or '-'}")
            out.append(f"answer: resume(plan='{pid}', action='retry'|'skip'|'abort'|'replace', "
                       "steps=[...] replaces this step and the rest)")
            return "\n".join(out)
        if st == "interrupted":
            return (f"plan {pid} INTERRUPTED (Steward or ZeroClaw restarted mid-plan). Steps marked "
                    "'interrupted' may have partly happened. Inspect with look, then "
                    f"resume(plan='{pid}', ...) — remaining changes need approval again.\n" +
                    self._step_table(rows, runs))
        rep = plan.get("report") or {}
        if st in ("declined", "expired", "rejected"):
            return f"plan {pid} {st.upper()}: {rep.get('why', '')} Nothing was changed."
        verdict = rep.get("verdict", "?").upper()
        out = [f"plan {pid} {verdict}: {plan['goal']}", self._step_table(rows, runs)]
        g = rep.get("goal_checks") or []
        if g:
            out.append("goal: " + ", ".join(f"{'PASS' if x['passed'] else 'FAIL'} {x['run']}"
                                            for x in g))
        shown = 0
        for r in rows:
            run = runs.get(r.get("run")) if r.get("run") else None
            if run and run["kind"] == "read" and shown < 6:
                limit = int(r["step"].get("lines", 25))
                lines, total = _tail_lines(run["stdout"], 10 ** 6)
                body = lines[:limit]
                out.append(f"--- [{r['n']}] r{run['id']} {steps.short(run['operation'], 60)}")
                out += ["  " + l[:300] for l in body]
                if total > limit:
                    out.append(f"  [+{total - limit} lines: output(ref='r{run['id']}')]")
                shown += 1
        undo = [r for r in rows if r.get("run") and runs[r["run"]]["inverse"] and
                runs[r["run"]]["status"] == "ok" and not runs[r["run"]]["undone_by"]]
        n_ch = rep.get("changes", 0)
        summary = {"verified": f"{n_ch} change(s) made and the goal check passed.",
                   "unverified": f"{n_ch} change(s) made; goal not confirmed by a check.",
                   "partial": f"{n_ch} change(s) made, but not everything went as planned.",
                   "failed": "The goal was not achieved.",
                   "uncertain": "Something may have half-happened; the system state is uncertain.",
                   "stopped": "The user stopped this plan.", "done": "Read-only; nothing changed."}
        out.append("summary: " + summary.get(rep.get("verdict"), ""))
        if undo:
            out.append("undo: " + ", ".join(f"{{\"do\":\"undo\",\"ref\":\"r{runs[r['run']]['id']}\"}}"
                                            for r in undo[:6]))
        return "\n".join(out)

    @staticmethod
    def _row_line(rows, n):
        r = next((r for r in rows if r["n"] == n), None)
        return r["line"] if r else ("goal checks" if n == "goal" else "?")

    @staticmethod
    def _step_table(rows, runs):
        word = {"ok": "ok", "noop": "noop", "skipped_if": "skip(if)", "failed": "FAILED",
                "verify_failed": "CHECK-FAILED", "deviated": "UNEXPECTED", "blocked": "BLOCKED",
                "not_run": "not run", "replaced": "replaced", "skipped": "skipped",
                "interrupted": "INTERRUPTED", "pending": "pending"}
        out = []
        for r in rows:
            s = r["status"]
            w = word.get(s, s.replace("+skipped", " (skipped)").upper())
            extra = ""
            if r.get("noop"):
                extra = f" ({r['noop']})"
            run = runs.get(r.get("run")) if r.get("run") else None
            if run:
                extra += f" [r{run['id']}]"
            out.append(f" {r['n']:>2} {w:<12} {steps.short(r.get('line') or '', 70)}{extra}")
        return "\n".join(out)

    @staticmethod
    def _hint(run):
        f = run.get("failure") or ""
        return {"authorization": "the admin password prompt was dismissed or failed; ask the user",
                "permission_denied": "needs rights this step does not have; for system files use "
                                     "a typed admin step (system_service, dnf_*)",
                "not_found": "check the exact name with look first",
                "timeout": "it took too long; consider a longer timeout or a smaller step",
                "network": "no network: shell steps are offline unless network:true",
                "sandbox_unavailable": "bubblewrap is missing; tell the user to run steward doctor",
                "verify_failed": "the command ran but the result is not what was intended",
                "expectation": "the output did not match your expect/check",
                }.get(f, "")


def steps_presets():
    from .readonly import presets
    return list(presets())
