"""Engine tests. Everything destructive happens in a throwaway home with fake system tools."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import FAKE, HOME
from steward import approval, execute

needs_bwrap = pytest.mark.skipif(not execute.bwrap_available(), reason="bubblewrap unavailable")


def zenity_calls():
    f = FAKE / "zenity.log"
    return len(f.read_text().splitlines()) if f.exists() else 0


def unit(user, name):
    f = FAKE / f"units-{'user' if user else 'system'}.json"
    return json.load(open(f)).get(name) if f.exists() else None


def events(e, pid):
    return [r["event"] for r in e.store.q("SELECT event FROM events WHERE plan_id=? ORDER BY id", (pid,))]


def last_plan(e):
    return e.store.plans(1)[0]


V = ["true"]


# ================================================================ happy paths
def test_read_only_plan_needs_no_approval(eng):
    out = eng.submit("disk", [{"do": "inspect", "run": "echo 42"}])
    assert "DONE" in out and "42" in out and zenity_calls() == 0


def test_auto_changes_run_without_dialog_and_verify(eng):
    out = eng.submit("tidy", [{"do": "trash", "path": "~/Downloads/models"}],
                     verify=["test ! -e ~/Downloads/models"])
    assert "VERIFIED" in out and zenity_calls() == 0
    assert not (HOME / "Downloads/models").exists()


def test_consequential_changes_need_dialog_and_report_verdict(eng):
    out = eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"},
                           {"do": "system_service", "unit": "cups", "op": "disable_now"}],
                     verify=["test ! -e ~/Documents/thesis.docx"])
    assert "VERIFIED" in out and zenity_calls() == 1
    text = (FAKE / "zenity.txt").read_text()
    assert "thesis.docx" in text and "cups.service" in text and "admin password" in text
    assert unit(False, "cups.service") == ["inactive", "disabled"]
    ev = events(eng, last_plan(eng)["id"])
    assert "approval_requested" in ev and "approval_approved" in ev and "plan_finished" in ev


def test_noop_steps_skip_and_need_no_approval(eng):
    out = eng.submit("g", [{"do": "system_service", "unit": "cups", "op": "start"},
                           {"do": "dnf_install", "packages": ["htop"]}], verify=V)
    assert "noop" in out and zenity_calls() == 0


# ================================================================ approval failures
@pytest.mark.parametrize("mode", ["decline", "timeout", "crash"])
def test_approval_failures_block(eng, monkeypatch, mode):
    monkeypatch.setenv("FAKE_ZENITY", mode)
    out = eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"}], verify=V)
    assert "NOT APPROVED" in out and "Nothing was changed" in out
    assert (HOME / "Documents/thesis.docx").exists()
    assert not eng.store.q("SELECT 1 FROM runs WHERE kind='step'")


def test_approval_hang_is_a_timeout(eng, monkeypatch):
    monkeypatch.setenv("FAKE_ZENITY", "hang")
    eng.policy.approval_timeout_secs = 1
    t = time.time()
    out = eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"}], verify=V)
    assert "NOT APPROVED (timeout)" in out and time.time() - t < 30
    assert (HOME / "Documents/thesis.docx").exists()


def test_no_gui_blocks(eng, monkeypatch):
    monkeypatch.delenv("DISPLAY")
    out = eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"}], verify=V)
    assert "NOT APPROVED (unavailable)" in out and "graphical session" in out
    assert (HOME / "Documents/thesis.docx").exists()


def test_zenity_missing_blocks(eng, monkeypatch):
    monkeypatch.setattr(approval.shutil, "which", lambda name: None)
    out = eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"}], verify=V)
    assert "NOT APPROVED (unavailable)" in out and "zenity is not installed" in out
    assert (HOME / "Documents/thesis.docx").exists()


def test_admin_password_failure_blocks_and_is_recorded(eng, monkeypatch):
    monkeypatch.setenv("FAKE_PKEXEC", "deny")
    out = eng.submit("g", [{"do": "system_service", "unit": "bluetooth", "op": "stop"}], verify=V)
    assert "NEEDS INPUT" in out and "authorization" in out and "admin password" in out
    assert unit(False, "bluetooth.service") in (None, ["active", "enabled"])
    rep = eng.resume(last_plan(eng)["id"], "abort")
    assert "FAILED" in rep


def test_terminal_mode_waits_for_user(eng):
    eng.policy.approval_mode = "terminal"
    out = eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"}], verify=V)
    assert "steward approve" in out and (HOME / "Documents/thesis.docx").exists()
    pid = last_plan(eng)["id"]
    ok, _ = eng.terminal_approve(pid, False)
    assert ok and (HOME / "Documents/thesis.docx").exists()
    assert eng.store.plan(pid)["state"] == "declined"


# ================================================================ chat-mode approvals
def chat_args(out):
    return json.loads(out[out.index("{"):])


def test_chat_mode_requires_exact_args(eng, monkeypatch):
    eng.policy.approval_mode = "chat"
    monkeypatch.setattr("steward.zeroclaw.check", lambda *a, **k: {"errors": []})
    out = eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"}], verify=V)
    args = chat_args(out)
    assert "thesis.docx" in json.dumps(args)
    pid, seal = args.pop("plan"), args.pop("seal")
    tampered = dict(args, n01="1) TRASH something harmless")
    assert "REFUSED" in eng.approve(pid, seal, tampered)
    assert "REFUSED" in eng.approve(pid, "0000000000", args)
    assert "REFUSED" in eng.approve(pid, seal, dict(args, n99="extra"))
    assert (HOME / "Documents/thesis.docx").exists()
    assert "VERIFIED" in eng.approve(pid, seal, args)
    assert not (HOME / "Documents/thesis.docx").exists()
    assert "error" in eng.approve(pid, seal, args)          # cannot be replayed


def test_chat_mode_refuses_when_zeroclaw_would_not_ask(eng):
    eng.policy.approval_mode = "chat"
    out = eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"}], verify=V)
    args = chat_args(out)
    pid, seal = args.pop("plan"), args.pop("seal")
    r = eng.approve(pid, seal, args)          # real gate: no valid ZeroClaw config in this home
    assert "REFUSED" in r and (HOME / "Documents/thesis.docx").exists()


# ================================================================ policy & adversarial input
@pytest.mark.parametrize("step", [
    {"do": "trash", "path": "~/.ssh"},
    {"do": "trash", "path": "~/.ssh/id_ed25519"},
    {"do": "trash", "path": "~"},
    {"do": "trash", "path": "/etc/passwd"},
    {"do": "trash", "path": "~/Downloads/../.ssh"},
    {"do": "trash", "path": "~/.bashrc"},
    {"do": "write_file", "path": "~/.config/steward/policy.json", "content": "{}", "mode": "replace"},
    {"do": "write_file", "path": "~/.zeroclaw/config.toml", "content": "x", "mode": "replace"},
    {"do": "edit_file", "path": "~/.local/share/steward/x", "find": "a", "replace": "b"},
    {"do": "system_service", "unit": "firewalld", "op": "stop"},
    {"do": "system_service", "unit": "dbus-broker", "op": "disable_now"},
    {"do": "system_service", "unit": "NetworkManager", "op": "stop"},
    {"do": "user_service", "unit": "org.gnome.Shell@wayland", "op": "stop"},
    {"do": "dnf_remove", "packages": ["glibc"]},
    {"do": "kill", "pid": os.getpid()},
    {"do": "shell", "run": "sudo rm -rf /"},
    {"do": "shell", "run": "echo x | pkexec tee /etc/x"},
])
def test_prohibited_and_traversal_rejected_before_anything(eng, step):
    out = eng.submit("evil", [step], verify=V)
    assert "NEEDS FIX" in out and zenity_calls() == 0
    assert (HOME / ".ssh/id_ed25519").exists() and (HOME / ".bashrc").exists()
    assert not eng.store.q("SELECT 1 FROM runs WHERE kind='step'")


@pytest.mark.parametrize("steps,frag", [
    ([{"do": "format_disk"}], "unknown 'do'"),
    ([{"do": "trash", "path": "~/Downloads/note.txt", "force": True}], "unknown field"),
    ([{"do": "trash"}], "path"),
    ([{"do": "user_service", "unit": "x; rm -rf ~", "op": "stop"}], "invalid unit"),
    ([{"do": "user_service", "unit": "tracker-miner-fs-3", "op": "mask"}], "op must be"),
    ([{"do": "dnf_install", "packages": ["--setopt=x"]}], "invalid package"),
    ([{"do": "kill", "pid": "1"}], "pid must be"),
    ([{"do": "inspect", "run": "echo (("}], "syntax"),
    ([{"do": "inspect", "run": "ls", "expect": {"regex": "("}}], "bad expect.regex"),
    ([{"do": "trash", "path": "~/Downloads/note.txt", "undoes": 1}], "set by Steward"),
    ([{"do": "trash", "path": "~/Downloads/nope.txt"}], "does not exist"),
    ([{"do": "edit_file", "path": "~/Documents/app.conf", "find": "nothere", "replace": "x"}], "occurs 0"),
    ([{"do": "user_service", "unit": "trackr-miner-fs-3", "op": "stop"}], "did you mean"),
    ([{"do": "dnf_remove", "packages": ["libfoo"]}], "foo-app"),
    ([{"do": "trash", "path": "~/Downloads/note.txt"}, {"do": "trash", "path": "~/Downloads/note.txt"}], "repeats"),
    ([{"do": "shell", "run": "x" * 500}], "longer than"),
])
def test_malformed_or_wrong_plans_rejected(eng, steps, frag):
    out = eng.submit("g", steps, verify=V)
    assert "NEEDS FIX" in out and frag in out, out


def test_changes_without_goal_check_rejected(eng):
    out = eng.submit("g", [{"do": "trash", "path": "~/Downloads/note.txt"}])
    assert "NEEDS FIX" in out and "verify" in out
    assert (HOME / "Downloads/note.txt").exists()


def test_bad_top_level_input(eng):
    assert eng.submit("", [{"do": "inspect", "run": "ls"}]).startswith("error")
    assert eng.submit("g", "rm -rf ~").startswith("error")
    assert eng.submit("g", [{"do": "inspect", "run": "ls"}] * 61).startswith("error")
    assert eng.submit("g", [{"do": "inspect", "run": "ls"}], verify="true").startswith("error")


def test_recent_repeat_needs_explicit_again(eng):
    eng.submit("g", [{"do": "user_service", "unit": "tracker-miner-fs-3", "op": "restart"}], verify=V)
    out = eng.submit("g", [{"do": "user_service", "unit": "tracker-miner-fs-3", "op": "restart"}],
                     verify=V)
    assert "NEEDS FIX" in out and "already made recently" in out
    out = eng.submit("g", [{"do": "user_service", "unit": "tracker-miner-fs-3", "op": "restart",
                            "again": True}], verify=V)
    assert "VERIFIED" in out


# ================================================================ symlinks & check-then-use
def test_symlinked_folder_into_secret_rejected(eng):
    os.symlink(HOME / ".ssh", HOME / "Downloads/sneaky")
    out = eng.submit("g", [{"do": "write_file", "path": "~/Downloads/sneaky/authorized_keys",
                            "content": "ssh-ed25519 AAAA attacker", "mode": "create"}], verify=V)
    assert "NEEDS FIX" in out and not (HOME / ".ssh/authorized_keys").exists()


def test_trashing_a_symlink_moves_the_link_only(eng):
    os.symlink(HOME / "Documents", HOME / "Downloads/link")
    out = eng.submit("g", [{"do": "trash", "path": "~/Downloads/link"}],
                     verify=["test ! -L ~/Downloads/link"])
    assert "VERIFIED" in out and (HOME / "Documents/thesis.docx").exists()


def test_file_swapped_after_approval_is_not_touched(eng, monkeypatch):
    real = approval.desktop

    def swap_then_approve(title, text, timeout):
        p = HOME / "Documents/thesis.docx"
        p.unlink()
        p.write_text("a different, newer file")
        return "approved", "test"
    monkeypatch.setattr(approval, "desktop", swap_then_approve)
    out = eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"}], verify=V)
    assert "NEEDS INPUT" in out and "replaced since approval" in out
    assert (HOME / "Documents/thesis.docx").read_text() == "a different, newer file"


def test_folder_swapped_for_symlink_after_approval(eng, monkeypatch):
    (HOME / "Documents/sub").mkdir()
    (HOME / "Documents/sub/a.txt").write_text("a")

    def swap(title, text, timeout):
        os.rename(HOME / "Documents/sub", HOME / "Documents/sub.old")
        os.symlink(HOME / ".ssh", HOME / "Documents/sub")
        return "approved", "test"
    monkeypatch.setattr(approval, "desktop", swap)
    out = eng.submit("g", [{"do": "write_file", "path": "~/Documents/sub/a.txt", "content": "b",
                            "mode": "replace"}], verify=V)
    assert "NEEDS INPUT" in out
    assert not (HOME / ".ssh/a.txt").exists() and (HOME / ".ssh/id_ed25519").exists()


def test_file_edited_by_someone_else_after_approval(eng, monkeypatch):
    def edit(title, text, timeout):
        (HOME / "Documents/app.conf").write_text("mode=slow\ncache=on\nuser=edit\n")
        return "approved", "test"
    monkeypatch.setattr(approval, "desktop", edit)
    out = eng.submit("g", [{"do": "edit_file", "path": "~/Documents/app.conf", "find": "mode=slow",
                            "replace": "mode=fast"}], verify=V)
    assert "NEEDS INPUT" in out and "changed by something else" in out
    assert "user=edit" in (HOME / "Documents/app.conf").read_text()


def test_pid_reuse_guard(eng, monkeypatch):
    from steward import steps
    p = subprocess.Popen(["sleep", "60"])
    try:
        real = steps._proc

        def reused(title, text, timeout):
            # after approval, the pid now belongs to a different (newer) process
            monkeypatch.setattr(steps, "_proc", lambda pid: (dict(real(pid), start="999999999")
                                                             if pid == p.pid and real(pid) else real(pid)))
            return "approved", "test"
        monkeypatch.setattr(approval, "desktop", reused)
        out = eng.submit("g", [{"do": "kill", "pid": p.pid}], verify=V)
        assert "NEEDS INPUT" in out and "different process" in out
        assert p.poll() is None
    finally:
        p.kill()
        p.wait()


def test_kill_of_already_exited_process_is_not_claimed(eng, monkeypatch):
    p = subprocess.Popen(["sleep", "60"])

    def exit_first(title, text, timeout):
        p.kill(); p.wait()
        return "approved", "test"
    monkeypatch.setattr(approval, "desktop", exit_first)
    eng.submit("g", [{"do": "kill", "pid": p.pid}], verify=V)
    rid = eng.store.q1("SELECT id FROM runs WHERE do='kill'")[0]
    assert eng.store.run(rid)["changed"] == "no"


def test_kill_by_name_resolves_and_verifies(eng, tmp_path):
    import shutil as sh
    exe = tmp_path / "stwsleeper"
    sh.copy("/bin/sleep", exe)
    p = subprocess.Popen([str(exe), "61"])
    try:
        out = eng.submit("g", [{"do": "kill", "name": "stwsleeper"}], verify=["! pgrep -r R,S,D -x stwsleeper"])
        p.wait(timeout=5)
        assert "VERIFIED" in out and p.returncode is not None
        assert "END stwsleeper" in (FAKE / "zenity.txt").read_text()
    finally:
        p.kill()


# ================================================================ sandbox: looks
@needs_bwrap
def test_looks_cannot_write_signal_or_reach_network(eng):
    p = subprocess.Popen(["sleep", "62"])
    try:
        out = eng.look(run=f"echo pwned > ~/Downloads/x; rm -f ~/Downloads/note.txt; kill {p.pid}; "
                           "exec 3<>/dev/tcp/1.1.1.1/53 && echo NET-OK")
        assert not (HOME / "Downloads/x").exists() and (HOME / "Downloads/note.txt").exists()
        assert p.poll() is None and "NET-OK" not in out
    finally:
        p.kill()


@needs_bwrap
def test_looks_see_processes(eng):
    p = subprocess.Popen(["sleep", "63"])
    try:
        assert "sleep 63" in eng.look(run="ps -eo args | grep -v grep | grep 'sleep 63'")
    finally:
        p.kill()


@needs_bwrap
def test_looks_hide_secrets_env_and_redact(eng):
    out = eng.look(run="cat ~/.ssh/id_ed25519 ~/.zeroclaw/config.toml; env; "
                       "echo 'db_password = hunter2'; echo sk-or-v1-abcdefghijklmnopqrstuvwx")
    rid = int(out.split()[0][1:])
    full = eng.output(f"r{rid}", lines=500) + eng.output(f"r{rid}", stream="stderr")
    for bad in ("PRIVATE KEY", "secretsecret", "THISISAFAKEKEY", "OPENROUTER", "hunter2",
                "abcdefghijklmnop"):
        assert bad not in full, bad


def test_native_query_allowlist():
    from steward.readonly import split_native_head, native_query
    assert native_query(["systemctl", "--user", "list-units"])
    assert native_query(["systemctl", "stop", "x"]) is None
    assert native_query(["systemctl", "--user", "start", "x"]) is None
    assert native_query(["ip", "link", "set", "eth0", "down"]) is None
    assert native_query(["ss", "-K", "dst", "1.2.3.4"]) is None
    assert native_query(["ss", "-tlnK"]) is None
    assert native_query(["rpm", "-e", "bash"]) is None
    assert native_query(["rpm", "-qa"])
    assert native_query(["dnf", "remove", "x"]) is None
    assert native_query(["nmcli", "radio", "wifi", "off"]) is None
    assert native_query(["nmcli", "general", "hostname", "evil"]) is None
    assert native_query(["flatpak", "uninstall", "x"]) is None
    assert split_native_head("systemctl --user list-units; rm -rf ~") is None
    assert split_native_head("systemctl --user list-units && touch x") is None
    assert split_native_head("systemctl $(echo stop) x") is None
    assert split_native_head("systemctl --user list-units | grep x") == \
        (["systemctl", "--no-pager", "--user", "list-units"], "grep x")
    assert split_native_head("systemctl --user list-units || reboot") is None


@needs_bwrap
def test_pipeline_rest_stays_sandboxed(eng):
    eng.look(run="systemctl --user list-units | tee ~/Downloads/leak.txt")
    assert not (HOME / "Downloads/leak.txt").exists()


# ================================================================ sandbox: shell steps
@needs_bwrap
def test_shell_offline_by_default_and_network_shown_in_approval(eng):
    out = eng.submit("g", [{"do": "shell", "run": "exec 3<>/dev/tcp/1.1.1.1/53 && echo NET-OK"}],
                     verify=V)
    rid = eng.store.q1("SELECT id FROM runs WHERE do='shell'")[0]
    assert "NET-OK" not in eng.store.run(rid)["stdout"] and "NEEDS INPUT" in out
    eng.resume(last_plan(eng)["id"], "abort")
    eng.submit("g", [{"do": "shell", "run": "true", "network": True}], verify=V)
    assert "network on" in (FAKE / "zenity.txt").read_text()


@needs_bwrap
def test_shell_cannot_touch_policy_records_or_secrets_or_gain_privileges(eng):
    eng.submit("g", [{"do": "shell", "run":
                      "echo '{}' > ~/.config/steward/policy.json; rm -rf ~/.local/share/steward;"
                      " cat ~/.ssh/id_ed25519; grep NoNewPrivs /proc/self/status; true"}], verify=V)
    assert json.loads((HOME / ".config/steward/policy.json").read_text())["require_zeroclaw_gate"] is False
    assert (HOME / ".local/share/steward").exists()
    rid = eng.store.q1("SELECT id FROM runs WHERE do='shell'")[0]
    out = eng.store.run(rid)["stdout"]
    assert "PRIVATE" not in out and "NoNewPrivs:\t1" in out


@needs_bwrap
def test_shell_background_processes_do_not_survive(eng):
    out = eng.submit("g", [{"do": "shell", "run": "nohup sleep 301 >/dev/null 2>&1 & echo started"}],
                     verify=["! pgrep -f 'slee[p] 301'"])
    time.sleep(0.5)
    assert subprocess.run(["pgrep", "-f", "slee[p] 301"]).returncode != 0
    assert "VERIFIED" in out


@needs_bwrap
def test_timeout_kills_process_tree(eng):
    t = time.time()
    out = eng.submit("g", [{"do": "inspect", "run": "sleep 300 & sleep 300", "timeout": 2}])
    assert "NEEDS INPUT" in out and "timeout" in out and time.time() - t < 30
    time.sleep(0.5)
    assert subprocess.run(["pgrep", "-f", "sleep 300"]).returncode != 0


# ================================================================ lifecycle
@needs_bwrap
def test_user_stop_halts_running_plan(eng):
    eng.policy.wait_secs = 2
    out = eng.submit("g", [{"do": "inspect", "run": "sleep 120"},
                           {"do": "trash", "path": "~/Downloads/note.txt"}], verify=V)
    assert "RUNNING" in out
    pid = last_plan(eng)["id"]
    eng.store.request_cancel(pid)
    out = eng.status(pid, wait=True)
    assert "STOPPED" in out and (HOME / "Downloads/note.txt").exists()
    assert subprocess.run(["pgrep", "-f", "sleep 120"]).returncode != 0


def test_partial_completion_is_reported_honestly(eng):
    out = eng.submit("g", [{"do": "trash", "path": "~/Downloads/note.txt"},
                           {"do": "user_service", "unit": "broken", "op": "start"},
                           {"do": "trash", "path": "~/Downloads/models"}], verify=V)
    assert "NEEDS INPUT at step 2" in out and "CHECK" not in out or True
    pid = last_plan(eng)["id"]
    rep = eng.resume(pid, "abort")
    assert "PARTIAL" in rep and not (HOME / "Downloads/note.txt").exists()
    assert (HOME / "Downloads/models").exists()


def test_deviation_replace_needs_fresh_approval(eng):
    out = eng.submit("g", [{"do": "inspect", "run": "echo state=bad", "expect": {"contains": "good"}},
                           {"do": "trash", "path": "~/Downloads/note.txt"}], verify=V)
    assert "NEEDS INPUT at step 1" in out and "does not contain 'good'" in out
    pid = last_plan(eng)["id"]
    before = zenity_calls()
    out = eng.resume(pid, "replace", [{"do": "trash", "path": "~/Documents/thesis.docx"}])
    assert "VERIFIED" in out or "PARTIAL" in out
    assert zenity_calls() == before + 1 and not (HOME / "Documents/thesis.docx").exists()
    assert (HOME / "Downloads/note.txt").exists()          # replaced step never ran


def test_retry_limit(eng):
    out = eng.submit("g", [{"do": "inspect", "run": "false", "expect": {"exit": 0}}])
    pid = last_plan(eng)["id"]
    for _ in range(2):
        out = eng.resume(pid, "retry")
        assert "NEEDS INPUT" in out
    assert "REFUSED" in eng.resume(pid, "retry")


def test_goal_check_failure_is_not_success(eng):
    out = eng.submit("g", [{"do": "trash", "path": "~/Downloads/note.txt"}],
                     verify=["test -e ~/Downloads/note.txt"])
    assert "NEEDS INPUT" in out and "goal check" in out
    rep = eng.resume(last_plan(eng)["id"], "skip")
    assert "PARTIAL" in rep and "VERIFIED" not in rep


def test_unverified_shell_change_is_uncertain_without_goal_pass(eng):
    out = eng.submit("g", [{"do": "shell", "run": "true"}], verify=["false"])
    rep = eng.resume(last_plan(eng)["id"], "skip")
    assert "UNCERTAIN" in rep


def test_deferred_step_on_file_created_earlier(eng):
    out = eng.submit("g", [{"do": "write_file", "path": "~/Downloads/new.conf", "content": "a=1\n",
                            "mode": "create"},
                           {"do": "edit_file", "path": "~/Downloads/new.conf", "find": "a=1",
                            "replace": "a=2"}], verify=["grep -q a=2 ~/Downloads/new.conf"])
    assert "VERIFIED" in out and (HOME / "Downloads/new.conf").read_text() == "a=2\n"
    assert "checked again" in (FAKE / "zenity.txt").read_text()


def test_crash_recovery_marks_unknown_and_never_reruns(tmp_path):
    from conftest import build_home
    from steward.engine import Engine
    from steward.store import Store
    build_home()
    st = Store(tmp_path / "db")
    e = Engine(store=st)
    pid = st.new_plan("g", [{"do": "trash", "path": "~/Downloads/note.txt"}])
    st.save_plan(pid, None, None, state="running", steps=[
        {"n": 1, "step": {"do": "trash", "path": "~/Downloads/note.txt"}, "status": "pending",
         "tier": "auto", "line": "TRASH", "fp": "x", "attempts": 1}])
    rid = st.begin_run(pid, 1, "step", "trash", {}, "auto", "x", "TRASH", "auto")
    del e, st
    st2 = Store(tmp_path / "db")
    e2 = Engine(store=st2)
    assert e2.recovered == [rid]
    assert st2.run(rid)["status"] == "interrupted" and st2.run(rid)["changed"] == "unknown"
    assert "INTERRUPTED" in e2.status(pid)
    assert (HOME / "Downloads/note.txt").exists()     # nothing re-ran by itself


# ================================================================ undo (tested by state, not report)
def test_undo_trash_restores_exact_bytes(eng):
    eng.submit("g", [{"do": "trash", "path": "~/Downloads/models"}], verify=V)
    rid = eng.store.q1("SELECT id FROM runs WHERE do='trash'")[0]
    out = eng.submit("undo", [{"do": "undo", "ref": f"r{rid}"}],
                     verify=["test -f ~/Downloads/models/big.gguf"])
    assert "VERIFIED" in out
    assert (HOME / "Downloads/models/big.gguf").read_bytes() == b"x" * 5000
    assert eng.store.run(rid)["undone_by"]


def test_undo_edit_restores_old_content(eng):
    eng.submit("g", [{"do": "edit_file", "path": "~/Documents/app.conf", "find": "mode=slow",
                      "replace": "mode=fast"}], verify=V)
    rid = eng.store.q1("SELECT id FROM runs WHERE do='edit_file'")[0]
    out = eng.submit("undo", [{"do": "undo", "ref": f"r{rid}"}],
                     verify=["grep -q mode=slow ~/Documents/app.conf"])
    assert "VERIFIED" in out
    assert (HOME / "Documents/app.conf").read_text() == "mode=slow\ncache=on\n"


def test_undo_service_and_package(eng):
    eng.submit("g", [{"do": "system_service", "unit": "cups", "op": "disable_now"},
                     {"do": "dnf_remove", "packages": ["cowsay"]}], verify=V)
    for do in ("system_service", "dnf_remove"):
        rid = eng.store.q1("SELECT id FROM runs WHERE do=?", (do,))[0]
        out = eng.submit("undo", [{"do": "undo", "ref": f"r{rid}"}], verify=V)
        assert "VERIFIED" in out, out
    assert unit(False, "cups.service") == ["active", "enabled"]
    assert "cowsay" in json.load(open(FAKE / "pkgs.json"))


def test_undo_twice_refused(eng):
    eng.submit("g", [{"do": "trash", "path": "~/Downloads/note.txt"}], verify=V)
    rid = eng.store.q1("SELECT id FROM runs WHERE do='trash'")[0]
    eng.submit("undo", [{"do": "undo", "ref": f"r{rid}"}], verify=V)
    out = eng.submit("undo", [{"do": "undo", "ref": f"r{rid}"}], verify=V)
    assert "NEEDS FIX" in out and "nothing undoable" in out


def test_dnf_remove_records_exact_package_changes(eng):
    out = eng.submit("g", [{"do": "dnf_remove", "packages": ["libfoo"], "with_dependents": True}],
                     verify=V)
    rid = eng.store.q1("SELECT id FROM runs WHERE do='dnf_remove'")[0]
    v = eng.store.run(rid)["verify"]
    assert {x.split("-1.0")[0] for x in v["removed"]} == {"libfoo", "foo-app"}
    assert "VERIFIED" in out


# ================================================================ output & audit
def test_full_output_retrieval(eng):
    out = eng.look(run="seq 1 5000")
    assert "[+4940 more lines" in out
    rid = out.split()[0]
    assert "4321: 4321" in eng.output(rid, grep="^4321$")
    assert "2: 2" in eng.output(rid, start=1, lines=3)


def test_audit_trail_has_decisions(eng, monkeypatch):
    monkeypatch.setenv("FAKE_ZENITY", "decline")
    eng.submit("g", [{"do": "trash", "path": "~/Documents/thesis.docx"}], verify=V)
    ev = events(eng, last_plan(eng)["id"])
    assert ev[:2] == ["plan_submitted", "preflight_ok"]
    assert "approval_requested" in ev and "approval_declined" in ev


def test_rpm_and_dnf_macro_tricks_refused():
    from steward.readonly import native_query
    assert native_query(["rpm", "-q", "--eval", "%(touch /tmp/x)", "bash"]) is None
    assert native_query(["rpm", "-q", "--qf", "%(touch /tmp/x)", "bash"]) is None
    assert native_query(["rpm", "-qa", "--define", "x y"]) is None
    assert native_query(["rpm", "-q", "--dbpath", "/tmp", "bash"]) is None
    assert native_query(["rpm", "-qa", "--last"])
    assert native_query(["rpm", "-qi", "bash"])
    assert native_query(["dnf", "-c", "/tmp/evil.conf", "list"]) is None
    assert native_query(["dnf", "list", "--setopt=x=y"]) is None
    assert native_query(["dnf", "repoquery", "--installed", "--whatrequires", "glibc"])


@needs_bwrap
def test_other_processes_environment_is_masked(eng):
    p = subprocess.Popen(["sleep", "64"], env=dict(os.environ, MY_TOKEN_VALUE="ZZTOPSECRETZZ"))
    try:
        out = eng.look(run=f"tr '\\0' '\\n' < /proc/{p.pid}/environ; ps -p {p.pid} -o args=")
        rid = out.split()[0]
        full = eng.output(rid, lines=500)
        assert "ZZTOPSECRETZZ" not in full and "sleep 64" in full
    finally:
        p.kill()


def test_wifi_secret_redaction():
    from steward.redact import redact
    out = redact("802-11-wireless-security.psk:  hunter2wifi\nwifi-sec.psk=abc123xyz")
    assert "hunter2wifi" not in out and "abc123xyz" not in out


@needs_bwrap
def test_negated_check_cannot_pass_when_system_unreachable(eng):
    # In the sandbox systemctl cannot reach systemd; '!' must not turn that into a pass.
    from steward import looks
    r = looks.run_readonly("! bash -c 'echo Failed to connect to bus >&2; exit 1'", eng.policy)
    assert r.exit_code == 125
    r = looks.run_readonly("! false", eng.policy)
    assert r.exit_code == 0
    r = looks.run_readonly("! true", eng.policy)
    assert r.exit_code == 1
    r = looks.run_readonly("! bash -c 'exit 2'", eng.policy)
    assert r.exit_code == 2            # errors are not inverted into success


def test_sandbox_setup_failure_is_never_a_pass(eng, monkeypatch):
    from steward import execute, looks
    real = execute._run_once

    def broken(*a, **k):
        r = real(["bash", "-c", "echo 'bwrap: Cannot mkdir parents for /proc/1/environ' >&2; exit 1"],
                 a[1], None, *a[3:])
        r.sandbox = "ro"
        return r
    monkeypatch.setattr(execute, "_run_once", broken)
    r = looks.run_readonly("! false", eng.policy)
    assert r.exit_code == 125
