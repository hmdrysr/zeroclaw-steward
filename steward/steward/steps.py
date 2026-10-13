"""Plan step types: strict validation, preflight (before approval), execution,
built-in verification and undo.

Everything here treats the step as untrusted input. The risk tier comes from
the actual target found on the system, never from anything the AI says.
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, execute

READ, AUTO, ASK, NO = "read", "auto", "ask", "prohibited"
COMMON = {"do", "expect", "check", "only_if", "on_fail", "note", "again", "timeout", "undoes"}


class Bad(ValueError):
    """The step is wrong; the AI must fix it."""


class Missing(Bad):
    """The target is not in the expected state (may be created by an earlier step)."""


@dataclass
class Pre:
    do: str
    params: dict
    tier: str
    line: str                       # one-line description (<= 76 chars), shown to the user
    run: object = None              # callable(ctx) -> result dict
    recheck: object = None          # callable() -> problem str | None, right before running
    noop: str = None                # already in the wanted state: skipped, no approval needed
    irreversible: bool = False
    network: bool = False
    extra_lines: list = field(default_factory=list)   # continuation lines (e.g. long command)
    detail: str = ""                # full detail for the desktop approval dialog
    fp: str = ""


# ---------------------------------------------------------------- helpers
def _s(p, key, req=True, maxlen=4096):
    v = p.get(key)
    if v is None:
        if req:
            raise Bad(f"missing '{key}'")
        return None
    if not isinstance(v, str) or not v.strip():
        raise Bad(f"'{key}' must be a non-empty string")
    if len(v) > maxlen:
        raise Bad(f"'{key}' longer than {maxlen} chars")
    if "\x00" in v:
        raise Bad(f"'{key}' contains a NUL byte")
    return v


def _allowed(p, keys):
    extra = set(p) - COMMON - set(keys)
    if extra:
        raise Bad(f"unknown field(s) {sorted(extra)}; allowed: {sorted(set(keys) | (COMMON - {'do'}))}")


def short(text, n=76):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def tilde(p):
    s, h = str(p), str(config.HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def short_path(p, n=48):
    s = tilde(p)
    if len(s) <= n:
        return s
    parts = s.split("/")
    head, tail = parts[0], parts[-1]
    out = head + "/…/" + tail
    return out if len(out) <= n else "…" + s[-(n - 1):]


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def fingerprint(do, params):
    return hashlib.sha256((do + json.dumps(params, sort_keys=True)).encode()).hexdigest()[:16]


def safe_path(raw):
    """Expand ~, require absolute, reject '..' components, resolve the parent
    directory (following its symlinks) but NOT the final component."""
    if not isinstance(raw, str) or not raw.strip():
        raise Bad("path must be a non-empty string")
    p = config.expand(raw.strip())
    if not p.is_absolute():
        raise Bad(f"path must start with / or ~: {raw!r}")
    if ".." in p.parts:
        raise Bad(f"'..' is not allowed in paths: {raw!r}")
    if p.name in ("", "."):
        raise Bad(f"path must name a file or folder: {raw!r}")
    return Path(os.path.realpath(p.parent)) / p.name


def _within(p, root):
    try:
        Path(p).relative_to(root)
        return True
    except ValueError:
        return False


def classify_path(p, policy):
    home = Path(os.path.realpath(config.HOME))
    for s in policy.secret_paths:
        sp = Path(os.path.realpath(config.expand(s)))
        if _within(p, sp) or _within(sp, p):
            return NO, f"touches a credential/secret location ({s})"
    for s in policy.protected_paths:
        sp = Path(os.path.realpath(config.expand(s)))
        if _within(p, sp) or _within(sp, p):
            return NO, f"touches a protected location ({s})"
    if p == home or not _within(p, home):
        return NO, "outside your home folder (or the home folder itself)"
    for r in policy.auto_roots:
        rp = Path(os.path.realpath(config.expand(r)))
        if _within(p, rp) and p != rp:
            return AUTO, f"inside {r}"
    return ASK, "in your home folder outside the auto-approved areas"


def _ident(p):
    """Identity of a path for check-then-use protection. Files also bind size and
    change times, so a replaced or modified file (even one that reuses the inode
    number) is detected. Folders bind only device+inode+type, since their
    contents (e.g. caches) legitimately change."""
    st = os.lstat(p)
    d = {"dev": st.st_dev, "ino": st.st_ino, "mode": st.st_mode}
    if not stat.S_ISDIR(st.st_mode):
        d.update(size=st.st_size, mtime=st.st_mtime_ns, ctime=st.st_ctime_ns)
    return d


def _dir_fd(parent, bound):
    """Open the parent directory without following a final symlink and check it
    is the same directory that was approved (closes the swap-the-parent race)."""
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if (st.st_dev, st.st_ino) != (bound["dev"], bound["ino"]):
        os.close(fd)
        raise RuntimeError("the folder changed since approval")
    return fd


def tree_size(p, limit=200000):
    try:
        st = os.lstat(p)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    total, n = 0, 0
    for root, dirs, files in os.walk(p, followlinks=False):
        for f in files:
            n += 1
            if n > limit:
                return total
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def cmd(policy, argv, timeout=60, ctx=None):
    # typed steps choose their own fixed argv, so their (longer) time limits are trusted
    return execute.run(argv, policy=policy, timeout=timeout, cap=max(timeout, policy.max_timeout),
                       cancel_check=(ctx or {}).get("cancel"), on_pid=(ctx or {}).get("on_pid"))


def failure_kind(r):
    if r.timed_out:
        return "timeout"
    if r.cancelled:
        return "cancelled"
    e = (r.stderr or "").lower()
    if "sandbox unavailable" in e:
        return "sandbox_unavailable"
    if "not authorized" in e or "authentication" in e or "dismissed" in e or \
            "authorization" in e or (r.exit_code == 126 and "pkexec" in e):
        return "authorization"
    if "permission denied" in e or "operation not permitted" in e or "read-only file system" in e:
        return "permission_denied"
    if "no such file" in e or "not found" in e or "could not be found" in e or "not loaded" in e \
            or "no match" in e:
        return "not_found"
    if "network is unreachable" in e or "could not resolve" in e or "temporary failure in name" in e:
        return "network"
    return "nonzero_exit"


def res(r, status=None, changed="unknown", verify=None, inverse=None, warnings=None):
    st = status or ("ok" if r.exit_code == 0 else "failed")
    return dict(status=st, exit_code=r.exit_code, stdout=r.stdout, stderr=r.stderr,
                truncated=int(r.stdout_truncated or r.stderr_truncated), changed=changed,
                verify=verify, inverse=inverse, warnings=warnings or [],
                failure=None if st == "ok" else ("verify_failed" if st == "verify_failed"
                                                  else failure_kind(r)),
                sandbox=r.sandbox)


def simple_res(status, out="", err="", changed="no", verify=None, inverse=None, failure=None,
               warnings=None):
    return dict(status=status, exit_code=0 if status == "ok" else 1, stdout=out, stderr=err,
                truncated=0, changed=changed, verify=verify, inverse=inverse, warnings=warnings or [],
                failure=None if status == "ok" else (failure or "error"), sandbox="none")


# ---------------------------------------------------------------- read-only
def pre_inspect(p, policy, kind):
    _allowed(p, {"run", "lines"})
    run = _s(p, "run", maxlen=2000)
    if kind == "check" and "lines" in p:
        raise Bad("'lines' only applies to inspect")
    from .looks import syntax_problem
    sp = syntax_problem(run)
    if sp:
        raise Bad(sp)

    def go(ctx):
        from .looks import run_readonly
        r = run_readonly(run, policy, timeout=p.get("timeout"), cancel=ctx.get("cancel"),
                         on_pid=ctx.get("on_pid"))
        out = res(r, changed="no")
        if kind == "inspect" and not (r.timed_out or r.cancelled or r.exit_code in (126, 127)):
            out["status"], out["failure"] = "ok", None   # an inspect gathers output; use expect to assert
        return out

    return Pre(kind, {"run": run}, READ, short(f"{'look' if kind == 'inspect' else 'check'}: {run}"),
               go, detail=run)


# ---------------------------------------------------------------- shell
SUDO_RE = re.compile(r"(^|[\s;&|(`$])(sudo|su|pkexec|doas|run0)(\s|$)")


def pre_shell(p, policy, kind):
    _allowed(p, {"run", "cwd", "network"})
    run = _s(p, "run", maxlen=policy.shell_max_chars)
    net = p.get("network", False)
    if not isinstance(net, bool):
        raise Bad("'network' must be true or false")
    if net and not policy.allow_network_shell:
        raise Bad("shell steps with network access are disabled by your policy")
    cwd = p.get("cwd")
    if cwd is not None:
        c = safe_path(cwd)
        if not c.is_dir():
            raise Bad(f"cwd {tilde(c)} is not a folder")
        cwd = str(c)
    from .looks import syntax_problem
    sp = syntax_problem(run)
    if sp:
        raise Bad(sp)
    if SUDO_RE.search(run):
        return Pre(kind, p, NO, short("run: " + run),
                   noop=None, detail="shell steps can never gain admin rights (the sandbox sets "
                   "no-new-privileges). Use system_service / dnf_install / dnf_remove / "
                   "flatpak_remove, which ask for your admin password.")

    def go(ctx):
        r = execute.run(["bash", "-c", run], policy=policy, sandbox="rw", net=net,
                        timeout=p.get("timeout"), cwd=cwd, cancel_check=ctx.get("cancel"),
                        on_pid=ctx.get("on_pid"))
        return res(r, changed="unknown", warnings=[] if r.exit_code else
                   ["free-form command: Steward cannot see what it changed"])

    label = "RUN" + (" (network on)" if net else "")
    words = f"{label}: {run}" + (f"  [in {tilde(cwd)}]" if cwd else "")
    chunks = [words[i:i + 74] for i in range(0, len(words), 74)]
    return Pre(kind, {"run": run, "cwd": cwd, "network": net}, ASK, chunks[0],
               go, network=net, extra_lines=["  " + c for c in chunks[1:]],
               detail=f"{label} (in {tilde(cwd) if cwd else 'home'}):\n{run}")


# ---------------------------------------------------------------- services
UNIT_RE = re.compile(r"^[A-Za-z0-9@._:\\-]+$")
SYS_CORE = re.compile(r"^(dbus|dbus-broker|polkit|systemd-.*|gdm|display-manager|firewalld|"
                      r"auditd|sshd|init|getty@.*|user@.*|zeroclaw.*|udisks2|upower|"
                      r"accounts-daemon|rtkit-daemon)(\.service|\.socket)?$")
SYS_NO_STOP = re.compile(r"^NetworkManager(\.service)?$")
USER_CORE = re.compile(r"^(org\.gnome\.Shell.*|gnome-session.*|dbus.*|zeroclaw.*|"
                       r"org\.freedesktop\..*|xdg-permission-store.*)(\.service|\.socket|\.target)?$")
USER_ASK = re.compile(r"^(pipewire.*|wireplumber.*|xdg-.*|gnome-.*|org\.gnome\..*|"
                      r"gcr-ssh-agent.*|ssh-agent.*|at-spi.*)(\.service|\.socket)?$")
OPS = {"start": ("start",), "stop": ("stop",), "restart": ("restart",), "enable": ("enable",),
       "disable": ("disable",), "enable_now": ("enable", "--now"),
       "disable_now": ("disable", "--now")}
INVERSE_OP = {"start": "stop", "stop": "start", "enable": "disable", "disable": "enable",
              "enable_now": "disable_now", "disable_now": "enable_now"}


def unit_state(policy, user, unit):
    base = ["systemctl"] + (["--user"] if user else []) + ["--no-pager"]
    r = cmd(policy, base + ["show", unit, "-p", "LoadState", "-p", "ActiveState", "-p",
                            "UnitFileState", "-p", "Id"])
    d = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d, r


def unit_names(policy, user):
    base = ["systemctl"] + (["--user"] if user else []) + ["--no-pager", "--no-legend", "--plain"]
    names = set()
    for verb in ("list-unit-files", "list-units"):
        r = cmd(policy, base + [verb, "--all"] if verb == "list-units" else base + [verb])
        for line in r.stdout.splitlines():
            if line.split():
                names.add(line.split()[0])
    return names


def pre_service(p, policy, kind):
    _allowed(p, {"unit", "op"})
    unit = _s(p, "unit", maxlen=200)
    if not UNIT_RE.match(unit) or unit.startswith("-"):
        raise Bad(f"invalid unit name {unit!r}")
    if "." not in unit:
        unit += ".service"
    op = _s(p, "op")
    if op not in OPS:
        raise Bad(f"op must be one of {sorted(OPS)}")
    user = kind == "user_service"
    state, r = unit_state(policy, user, unit)
    if r.exit_code != 0 and not state:
        raise Bad(f"could not query {unit}: {r.stderr.strip()[:200]}")
    if state.get("LoadState") == "not-found":
        near = difflib.get_close_matches(unit, unit_names(policy, user), n=4, cutoff=0.5)
        where = "user" if user else "system"
        other = "system_service" if user else "user_service"
        st2, _ = unit_state(policy, not user, unit)
        hint = f"; it exists as a {'system' if user else 'user'} unit: use {other}" \
            if st2.get("LoadState") == "loaded" else ""
        raise Missing(f"no {where} unit {unit}" + (f"; did you mean {', '.join(near)}?" if near else "")
                  + hint)
    active, ufs = state.get("ActiveState", ""), state.get("UnitFileState", "")
    want_active = {"start": True, "restart": None, "stop": False, "enable_now": True,
                   "disable_now": False}.get(op)
    want_enabled = {"enable": True, "enable_now": True, "disable": False,
                    "disable_now": False}.get(op)
    is_active = active in ("active", "activating", "reloading")
    is_enabled = ufs in ("enabled", "enabled-runtime", "alias", "linked")
    noop = None
    if op != "restart":
        a_ok = want_active is None or want_active == is_active
        e_ok = want_enabled is None or want_enabled == is_enabled or \
            (want_enabled is False and ufs in ("static", "masked", "indirect", "generated", "transient"))
        if want_enabled is True and ufs in ("static", "generated", "transient"):
            raise Bad(f"{unit} is '{ufs}': it cannot be enabled, only started")
        if a_ok and e_ok:
            noop = f"already {'active' if is_active else 'inactive'}, {ufs or 'n/a'}"
    tier, why = (AUTO, "") if user else (ASK, "")
    if user:
        if USER_CORE.match(unit) and op not in ("start", "enable", "enable_now"):
            tier, why = NO, f"{unit} is part of your desktop session or of ZeroClaw itself"
        elif USER_ASK.match(unit):
            tier = ASK
    else:
        if SYS_CORE.match(unit) and op not in ("start", "enable", "enable_now"):
            tier, why = NO, f"{unit} is a core system/security service"
        elif SYS_NO_STOP.match(unit) and op in ("stop", "disable", "disable_now"):
            tier, why = NO, "stopping NetworkManager would cut this computer (and ZeroClaw) off"
    systemctl = ["systemctl"] + (["--user"] if user else [])
    argv = (systemctl if user else ["pkexec", "systemctl"]) + list(OPS[op]) + [unit]
    word = {"start": "START", "stop": "STOP", "restart": "RESTART", "enable": "ENABLE at login" if user else "ENABLE at boot",
            "disable": "DISABLE autostart of", "enable_now": "START + ENABLE",
            "disable_now": "STOP + DISABLE"}[op]
    line = short(f"{word} {'user' if user else 'system'} service {unit}" +
                 ("" if user else " (admin password)"))
    bound = {"LoadState": state.get("LoadState"), "Id": state.get("Id")}

    def recheck():
        s, _ = unit_state(policy, user, unit)
        if s.get("LoadState") != bound["LoadState"] or s.get("Id") != bound["Id"]:
            return f"{unit} changed since approval ({s.get('LoadState')})"
        return None

    def go(ctx):
        r = cmd(policy, argv, timeout=300, ctx=ctx)
        if r.exit_code != 0:
            return res(r, changed="no" if user else "unknown")
        s, _ = unit_state(policy, user, unit)
        a = s.get("ActiveState", "")
        u = s.get("UnitFileState", "")
        checks = {}
        if want_active is True or op == "restart":
            checks["active"] = a in ("active", "activating", "reloading")
        if want_active is False:
            checks["inactive"] = a in ("inactive", "failed", "deactivating")
        if want_enabled is True:
            checks["enabled"] = u in ("enabled", "enabled-runtime", "alias", "linked")
        if want_enabled is False:
            checks["not enabled"] = u not in ("enabled", "enabled-runtime")
        ok = all(checks.values())
        inv = {"do": kind, "unit": unit, "op": INVERSE_OP[op]} if op in INVERSE_OP else None
        return res(r, status="ok" if ok else "verify_failed", changed="yes",
                   verify={"observed": f"{a}, {u}", "checks": checks, "passed": ok}, inverse=inv)

    pre = Pre(kind, {"unit": unit, "op": op}, tier, line, go, recheck, noop,
              irreversible=(op == "restart"), detail=" ".join(argv))
    if tier == NO:
        pre.detail = why
    return pre


# ---------------------------------------------------------------- processes
NEVER_KILL = re.compile(r"^(systemd|gnome-shell|gnome-session.*|Xwayland|dbus-.*|"
                        r"zeroclaw.*|steward.*|gdm.*|pipewire.*|wireplumber|kwin.*|plasmashell)$")


def _proc(pid):
    base = Path(f"/proc/{pid}")
    try:
        statline = (base / "stat").read_text()
        comm = statline[statline.index("(") + 1: statline.rindex(")")]
        fields = statline[statline.rindex(")") + 2:].split()
        start = fields[19]
        uid = base.stat().st_uid
        args = (base / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        return {"pid": pid, "comm": comm, "start": start, "uid": uid, "args": args}
    except (OSError, ValueError, IndexError):
        return None


def _ancestors():
    out, pid = set(), os.getpid()
    for _ in range(64):
        out.add(pid)
        info = Path(f"/proc/{pid}/stat")
        try:
            s = info.read_text()
            pid = int(s[s.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            break
        if pid <= 1:
            break
    return out


def pre_kill(p, policy, kind):
    _allowed(p, {"pid", "name", "signal"})
    sig = p.get("signal", "TERM")
    if sig not in ("TERM", "KILL", "INT", "HUP"):
        raise Bad("signal must be TERM, KILL, INT or HUP")
    if ("pid" in p) == ("name" in p):
        raise Bad("give exactly one of 'pid' or 'name'")
    targets = []
    if "pid" in p:
        if not isinstance(p["pid"], int) or isinstance(p["pid"], bool) or p["pid"] <= 1:
            raise Bad("pid must be an integer > 1")
        info = _proc(p["pid"])
        if not info:
            raise Missing(f"no process with pid {p['pid']}")
        targets = [info]
    else:
        name = _s(p, "name", maxlen=64)
        for d in os.listdir("/proc"):
            if d.isdigit():
                info = _proc(int(d))
                if info and info["uid"] == os.getuid() and (
                        info["comm"] == name or os.path.basename(info["args"].split(" ")[0]) == name):
                    targets.append(info)
        if not targets:
            return Pre(kind, {"name": name, "signal": sig}, AUTO, f"END process {name}",
                       noop=f"no process named {name} is running")
        if len(targets) > 30:
            raise Bad(f"{len(targets)} processes match {name!r}; be more specific (use pid)")
    anc = _ancestors()
    for t in targets:
        if t["uid"] != os.getuid():
            return Pre(kind, p, NO, f"END pid {t['pid']}", detail="belongs to another user or root")
        if t["pid"] in anc or NEVER_KILL.match(t["comm"]):
            return Pre(kind, p, NO, f"END {t['comm']} (pid {t['pid']})",
                       detail=f"{t['comm']} is part of the desktop session, ZeroClaw or Steward")
    names = sorted({t["comm"] for t in targets})
    pids = [t["pid"] for t in targets]
    line = short(f"END {'/'.join(names)} (pid {', '.join(map(str, pids[:6]))}"
                 f"{'…' if len(pids) > 6 else ''}) - unsaved work is lost")
    bound = {t["pid"]: t["start"] for t in targets}

    def recheck():
        for pid, start in bound.items():
            info = _proc(pid)
            if info and info["start"] != start:
                return f"pid {pid} now belongs to a different process"
        return None

    def go(ctx):
        out, gone = [], []
        for pid, start in bound.items():
            info = _proc(pid)
            if not info:
                gone.append(pid)
                continue
            if info["start"] != start:
                out.append(f"pid {pid}: reused by another process, not touched")
                continue
            try:
                os.kill(pid, getattr(__import__("signal"), "SIG" + sig))
                out.append(f"pid {pid}: sent SIG{sig}")
            except ProcessLookupError:
                gone.append(pid)
            except PermissionError as e:
                out.append(f"pid {pid}: {e}")
        deadline = time.time() + 6
        alive = []
        while time.time() < deadline:
            alive = [pid for pid in bound if (i := _proc(pid)) and i["start"] == bound[pid]
                     and not _zombie(pid)]
            if not alive:
                break
            time.sleep(0.25)
        ok = not alive
        sent = any("sent SIG" in o for o in out)
        if gone and not sent:
            out.append(f"already gone before Steward acted: {gone}")
        return simple_res("ok" if ok else "verify_failed", "\n".join(out),
                          "" if ok else f"still running after SIG{sig}: {alive}",
                          changed="yes" if sent else "no",
                          verify={"check": "processes gone within 6s", "still_running": alive,
                                  "passed": ok}, failure=None if ok else "verify_failed")

    return Pre(kind, {"pids": pids, "signal": sig}, ASK, line, go, recheck, irreversible=True,
               detail="\n".join(f"pid {t['pid']}: {t['args'][:200] or t['comm']}" for t in targets))


def _zombie(pid):
    try:
        s = Path(f"/proc/{pid}/stat").read_text()
        return s[s.rindex(")") + 2] == "Z"
    except (OSError, IndexError):
        return True


# ---------------------------------------------------------------- files
ENTRY_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


def _new_entry(original, plan, kind):
    eid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = config.TRASH_DIR / eid
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"original": str(original), "at": time.time(),
                                                 "plan": plan, "kind": kind}))
    return eid, d


def pre_trash(p, policy, kind):
    _allowed(p, {"path", "missing_ok"})
    path = safe_path(p.get("path"))
    tier, why = classify_path(path, policy)
    if tier == NO:
        return Pre(kind, {"path": str(path)}, NO, f"TRASH {short_path(path)}", detail=why)
    if not os.path.lexists(path):
        if p.get("missing_ok"):
            return Pre(kind, {"path": str(path)}, AUTO, f"TRASH {short_path(path)}",
                       noop="already absent")
        parent = path.parent
        near = difflib.get_close_matches(path.name, os.listdir(parent), n=3, cutoff=0.5) \
            if parent.is_dir() else []
        raise Missing(f"{tilde(path)} does not exist" +
                  (f"; similar: {', '.join(near)}" if near else "") +
                  " (set missing_ok:true if that is fine)")
    config.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    if os.lstat(path).st_dev != os.stat(config.TRASH_DIR).st_dev and not os.path.islink(path):
        raise Bad(f"{tilde(path)} is on a different disk than Steward's trash; moving it would "
                  "copy everything. Not supported")
    size = tree_size(path)
    kindword = "link" if os.path.islink(path) else ("folder" if path.is_dir() else "file")
    line = short(f"TRASH {kindword} {short_path(path)} ({human(size)}, restorable)")
    bp = _ident(path.parent)
    bt = _ident(path)

    def recheck():
        try:
            if os.path.realpath(path.parent) != str(path.parent) or _ident(path.parent) != bp:
                return "the folder was replaced since approval"
            if _ident(path) != bt:
                return f"{tilde(path)} was replaced since approval"
        except OSError:
            return f"{tilde(path)} no longer exists"
        return None

    def go(ctx):
        eid, d = _new_entry(path, ctx["plan"], "trash")
        fd = None
        try:
            fd = _dir_fd(path.parent, bp)
            os.rename(path.name, str(d / "item"), src_dir_fd=fd)
        except (OSError, RuntimeError) as e:
            shutil.rmtree(d, ignore_errors=True)
            return simple_res("failed", err=str(e), changed="no",
                              failure="permission_denied" if "ermission" in str(e) else "changed")
        finally:
            if fd is not None:
                os.close(fd)
        ok = not os.path.lexists(path) and os.lstat(d / "item").st_ino == bt["ino"]
        return simple_res("ok" if ok else "verify_failed", f"moved to trash entry {eid}",
                          changed="yes", inverse={"do": "restore", "entry": eid},
                          verify={"check": "original gone, same inode in trash", "passed": ok})

    return Pre(kind, {"path": str(path)}, tier, line, go, recheck, detail=f"{path} ({why})")


def _manifest(eid):
    if not isinstance(eid, str) or not ENTRY_RE.match(eid):
        raise Bad(f"invalid trash entry id {eid!r}")
    m = config.TRASH_DIR / eid / "manifest.json"
    if not m.exists():
        raise Bad(f"no trash entry {eid} (already restored or purged?)")
    return json.loads(m.read_text())


def pre_restore(p, policy, kind):
    _allowed(p, {"entry"})
    eid = p.get("entry")
    info = _manifest(eid)
    orig = Path(info["original"])
    tier, why = classify_path(orig, policy)
    if tier == NO:
        return Pre(kind, {"entry": eid}, NO, f"RESTORE {short_path(orig)}", detail=why)
    if os.path.lexists(orig):
        raise Bad(f"{tilde(orig)} exists again; move it away first")
    item = config.TRASH_DIR / eid / "item"

    def go(ctx):
        if os.path.lexists(orig):
            return simple_res("failed", err=f"{orig} exists again", failure="conflict")
        orig.parent.mkdir(parents=True, exist_ok=True)
        os.rename(item, orig)
        (config.TRASH_DIR / eid / "manifest.json").rename(config.TRASH_DIR / eid / "restored.json")
        ok = os.path.lexists(orig)
        return simple_res("ok" if ok else "verify_failed", f"restored {orig}", changed="yes",
                          verify={"check": "path exists again", "passed": ok},
                          inverse={"do": "trash", "path": str(orig)})

    return Pre(kind, {"entry": eid}, AUTO if tier == AUTO else ASK,
               short(f"RESTORE {short_path(orig)} from trash"), go, detail=str(orig))


def pre_purge(p, policy, kind):
    _allowed(p, {"entries", "all", "this_plan"})
    if p.get("this_plan") is True:
        if set(p) - {"this_plan", "do"}:
            raise Bad("this_plan cannot be combined with entries/all")

        def go_plan(ctx):
            mine = []
            if config.TRASH_DIR.exists():
                for d in config.TRASH_DIR.iterdir():
                    m = d / "manifest.json"
                    if m.exists():
                        info = json.loads(m.read_text())
                        if info.get("plan") == ctx["plan"] and info.get("kind") == "trash":
                            mine.append(d)
            size = sum(tree_size(d / "item") for d in mine)
            errs = []
            for d in mine:
                try:
                    shutil.rmtree(d)
                except OSError as ex:
                    errs.append(f"{d.name}: {ex}")
            left = [d.name for d in mine if d.exists()]
            return simple_res("ok" if not left else "verify_failed",
                              f"permanently deleted {len(mine) - len(left)} item(s), {human(size)}",
                              "\n".join(errs), changed="yes" if len(left) < len(mine) else "no",
                              verify={"check": "entries gone", "left": left, "passed": not left})

        return Pre(kind, {"this_plan": True}, ASK,
                   "PURGE what this plan moved to trash - PERMANENT, frees the space", go_plan,
                   irreversible=True, detail="Everything moved to Steward's trash by earlier steps of "
                   "this plan is deleted for good.")
    if p.get("all") is True:
        entries = sorted(d.name for d in config.TRASH_DIR.iterdir()
                         if (d / "manifest.json").exists()) if config.TRASH_DIR.exists() else []
        if not entries:
            return Pre(kind, {"entries": []}, ASK, "PURGE trash", noop="trash is empty")
    else:
        entries = p.get("entries")
        if not isinstance(entries, list) or not entries:
            raise Bad("give 'entries': [ids] or 'all': true")
    infos = [(e, _manifest(e)) for e in entries]
    size = sum(tree_size(config.TRASH_DIR / e / "item") for e, _ in infos)
    line = short(f"PURGE {len(infos)} trash item(s), {human(size)} - PERMANENT")

    def go(ctx):
        errs = []
        for e, _ in infos:
            try:
                shutil.rmtree(config.TRASH_DIR / e)
            except OSError as ex:
                errs.append(f"{e}: {ex}")
        left = [e for e, _ in infos if (config.TRASH_DIR / e).exists()]
        return simple_res("ok" if not left else "verify_failed",
                          f"permanently deleted {len(infos) - len(left)} item(s), {human(size)}",
                          "\n".join(errs), changed="yes" if len(left) < len(infos) else "no",
                          verify={"check": "entries gone", "left": left, "passed": not left})

    return Pre(kind, {"entries": entries}, ASK, line, go, irreversible=True,
               detail="\n".join(i["original"] for _, i in infos))


def _file_target(p, policy, kind):
    path = safe_path(p.get("path"))
    tier, why = classify_path(path, policy)
    return path, tier, why


def _write_atomic(path, content, bp, ctx, backup_kind):
    """Back up the old file to trash (copy), then write a temp file next to it and
    rename it over, all relative to the pinned parent directory."""
    backup = None
    fd = _dir_fd(path.parent, bp)
    try:
        mode = 0o644
        try:
            st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeError(f"{path} is no longer a regular file")
            mode = st.st_mode & 0o7777
            backup, d = _new_entry(path, ctx["plan"], backup_kind)
            src = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(src, "rb") as fsrc, open(d / "item", "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
                fdst.flush()
                os.fsync(fdst.fileno())
        except FileNotFoundError:
            pass
        tmp = f".{path.name}.steward-{uuid.uuid4().hex[:6]}"
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
        with os.fdopen(tfd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode, dir_fd=fd)
        os.replace(tmp, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)
    return backup


def _diffstat(old, new):
    sm = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines())
    plus = minus = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            minus += i2 - i1
        if tag in ("replace", "insert"):
            plus += j2 - j1
    return plus, minus


def _udiff(old, new, path, n=60):
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(), tilde(path), tilde(path),
                                  lineterm="", n=1))
    return "\n".join(d[:n]) + ("\n…" if len(d) > n else "")


def _write_pre(kind, path, tier, why, new, old_text, bt, display):
    sha = hashlib.sha256(new.encode()).hexdigest()
    bp = _ident(path.parent)

    def recheck():
        try:
            if _ident(path.parent) != bp:
                return "the folder was replaced since approval"
            if bt is None:
                if os.path.lexists(path):
                    return f"{tilde(path)} appeared since approval"
            else:
                if _ident(path) != bt["id"] or \
                        hashlib.sha256(path.read_bytes()).hexdigest() != bt["sha"]:
                    return f"{tilde(path)} was changed by something else since approval"
        except OSError as e:
            return str(e)
        return None

    def go(ctx):
        try:
            backup = _write_atomic(path, new, bp, ctx, "backup")
        except (OSError, RuntimeError, UnicodeError) as e:
            return simple_res("failed", err=str(e), changed="no",
                              failure="permission_denied" if "ermission" in str(e) else "error")
        ok = hashlib.sha256(path.read_bytes()).hexdigest() == sha
        inv = {"do": "restore_backup", "entry": backup} if backup else \
            {"do": "trash", "path": str(path)}
        return simple_res("ok" if ok else "verify_failed",
                          f"wrote {tilde(path)}" + (f"; old version kept as trash entry {backup}"
                                                    if backup else ""),
                          changed="yes", inverse=inv,
                          verify={"check": "file content hash matches", "passed": ok})

    return Pre(kind, {"path": str(path), "sha": sha[:16]}, tier, display, go, recheck,
               detail=(f"{path}\n{why}\n\n" + (_udiff(old_text, new, path) if old_text is not None
                                               else new[:3000])))


def pre_write(p, policy, kind):
    _allowed(p, {"path", "content", "mode"})
    content = p.get("content")
    if not isinstance(content, str):
        raise Bad("'content' must be a string")
    if len(content.encode()) > 2 * 1024 * 1024:
        raise Bad("content larger than 2 MB")
    mode = p.get("mode", "create")
    if mode not in ("create", "replace"):
        raise Bad("mode must be 'create' or 'replace'")
    path, tier, why = _file_target(p, policy, kind)
    if tier == NO:
        return Pre(kind, {"path": str(path)}, NO, f"WRITE {short_path(path)}", detail=why)
    if not path.parent.is_dir():
        raise Missing(f"folder {tilde(path.parent)} does not exist")
    exists = os.path.lexists(path)
    if exists and os.path.islink(path):
        raise Bad(f"{tilde(path)} is a symlink; write to its real target path instead")
    if mode == "create" and exists:
        raise Missing(f"{tilde(path)} already exists; use mode 'replace' (old version is kept)")
    if mode == "replace" and not exists:
        raise Missing(f"{tilde(path)} does not exist; use mode 'create'")
    if exists and not path.is_file():
        raise Bad(f"{tilde(path)} is not a regular file")
    old = None
    bt = None
    if exists:
        raw = path.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise Bad("existing file larger than 2 MB")
        try:
            old = raw.decode()
        except UnicodeDecodeError:
            raise Bad(f"{tilde(path)} is not a text file")
        if old == content:
            return Pre(kind, {"path": str(path)}, AUTO, f"WRITE {short_path(path)}",
                       noop="file already has exactly this content")
        bt = {"id": _ident(path), "sha": hashlib.sha256(raw).hexdigest()}
        a, b = _diffstat(old, content)
        line = short(f"REPLACE {short_path(path)} (+{a} -{b} lines, old kept)")
    else:
        line = short(f"CREATE {short_path(path)} ({len(content.splitlines())} lines)")
    return _write_pre(kind, path, tier, why, content, old, bt, line)


def pre_edit(p, policy, kind):
    _allowed(p, {"path", "find", "replace", "count"})
    find = p.get("find")
    rep = p.get("replace")
    if not isinstance(find, str) or not find:
        raise Bad("'find' must be a non-empty string (exact text)")
    if not isinstance(rep, str):
        raise Bad("'replace' must be a string")
    count = p.get("count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise Bad("'count' must be a positive integer (how many matches you expect)")
    path, tier, why = _file_target(p, policy, kind)
    if tier == NO:
        return Pre(kind, {"path": str(path)}, NO, f"EDIT {short_path(path)}", detail=why)
    if not os.path.lexists(path):
        raise Missing(f"{tilde(path)} does not exist")
    if os.path.islink(path) or not path.is_file():
        raise Bad(f"{tilde(path)} is not a regular file")
    raw = path.read_bytes()
    try:
        old = raw.decode()
    except UnicodeDecodeError:
        raise Bad(f"{tilde(path)} is not a text file")
    n = old.count(find)
    if n != count:
        hint = ""
        if n == 0:
            first = find.strip().splitlines()[0] if find.strip() else find
            near = difflib.get_close_matches(first, old.splitlines(), n=2, cutoff=0.6)
            hint = f"; closest lines: {near}" if near else ""
        raise Missing(f"'find' occurs {n} time(s) in {tilde(path)}, expected {count}{hint}")
    if find == rep:
        return Pre(kind, {"path": str(path)}, AUTO, f"EDIT {short_path(path)}",
                   noop="find and replace are identical")
    new = old.replace(find, rep)
    bt = {"id": _ident(path), "sha": hashlib.sha256(raw).hexdigest()}
    a, b = _diffstat(old, new)
    line = short(f"EDIT {short_path(path)} ({count} change, +{a} -{b} lines, old kept)")
    return _write_pre(kind, path, tier, why, new, old, bt, line)


def pre_restore_backup(p, policy, kind):
    _allowed(p, {"entry"})
    eid = p.get("entry")
    info = _manifest(eid)
    orig = Path(info["original"])
    content = (config.TRASH_DIR / eid / "item").read_text()
    pre = pre_write({"path": str(orig), "content": content,
                     "mode": "replace" if os.path.lexists(orig) else "create"}, policy, "write_file")
    pre.do = kind
    pre.params = {"entry": eid}
    pre.line = short(f"PUT BACK old version of {short_path(orig)}")
    return pre


# ---------------------------------------------------------------- packages
PKG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+:@-]*$")
PKG_CORE = re.compile(
    r"^(kernel.*|linux|linux-lts|linux-zen|linux-hardened|linux-image.*|linux-generic.*|"
    r"glibc.*|libc6.*|libc-bin|systemd.*|udev|dnf.*|libdnf.*|rpm.*|zypper|libzypp.*|"
    r"apt|apt-utils|dpkg|pacman|base|base-files|base-passwd|init|sudo|polkit.*|policykit-1|"
    r"bubblewrap|python3|python3-libs|python3-minimal|bash|dash|coreutils.*|util-linux.*|"
    r"NetworkManager.*|network-manager|networkmanager|gnome-shell|gdm.*|sddm|mutter|"
    r"fedora-release.*|selinux-policy.*|shadow-utils|passwd|login|pam.*|libpam.*|openssh.*|"
    r"grub.*|shim.*|dracut.*|mkinitcpio|initramfs-tools.*|filesystem|setup|zenity|flatpak|"
    r"ubuntu-desktop.*|ubuntu-minimal|ubuntu-standard)$")


class Backend:
    """How each distribution's package tool is asked about and changed."""

    def __init__(self, name):
        self.name = name

    def installed(self, policy, pkgs):
        if self.name in ("dnf", "zypper"):
            r = cmd(policy, ["rpm", "-q", "--qf", "%{NAME}\n"] + pkgs)
            return {l.strip() for l in r.stdout.splitlines() if l and "not installed" not in l}
        if self.name == "apt":
            r = cmd(policy, ["dpkg-query", "-W", "-f=${Package} ${db:Status-Abbrev}\n"] + pkgs)
            return {l.split()[0] for l in r.stdout.splitlines() if len(l.split()) > 1 and l.split()[1] == "ii"}
        if self.name == "pacman":
            r = cmd(policy, ["pacman", "-Q"] + pkgs)
            return {l.split()[0] for l in r.stdout.splitlines() if l.split()}
        return set()

    def snapshot(self, policy):
        if self.name in ("dnf", "zypper"):
            r = cmd(policy, ["rpm", "-qa", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n"], timeout=120)
            return set(r.stdout.split())
        if self.name == "apt":
            r = cmd(policy, ["dpkg-query", "-W", "-f=${Package}=${Version} ${db:Status-Abbrev}\n"], timeout=120)
            return {l.split()[0] for l in r.stdout.splitlines() if len(l.split()) > 1 and l.split()[1] == "ii"}
        if self.name == "pacman":
            r = cmd(policy, ["pacman", "-Q"], timeout=120)
            return {"=".join(l.split()[:2]) for l in r.stdout.splitlines() if l.split()}
        return set()

    def name_of(self, entry):
        if self.name in ("apt", "pacman"):
            return entry.split("=", 1)[0].split(":", 1)[0]
        return entry.rsplit("-", 2)[0]

    def also_removed(self, policy, todo):
        """Installed packages that removing `todo` would take with it (or break)."""
        if self.name in ("dnf", "zypper"):
            r = cmd(policy, ["rpm", "-q", "--whatrequires", "--qf", "%{NAME}\n"] + todo)
            found = {l.strip() for l in r.stdout.splitlines()
                     if l.strip() and "no package requires" not in l}
        elif self.name == "apt":
            r = cmd(policy, ["apt-get", "-s", "remove"] + todo)
            found = {l.split()[1] for l in r.stdout.splitlines() if l.startswith("Remv ")}
        elif self.name == "pacman":
            found = set()
            for p in todo:
                r = cmd(policy, ["pacman", "-Qi", p])
                for l in r.stdout.splitlines():
                    if l.startswith("Required By"):
                        v = l.split(":", 1)[1].strip()
                        if v != "None":
                            found |= set(v.split())
        else:
            found = set()
        return sorted(found - set(todo))

    def argv(self, install, todo):
        if self.name == "dnf":
            return ["pkexec", "dnf", "-y", "install" if install else "remove"] + todo
        if self.name == "zypper":
            return ["pkexec", "zypper", "--non-interactive", "install" if install else "remove"] + todo
        if self.name == "apt":
            return ["pkexec", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-y",
                    "install" if install else "remove"] + todo
        if self.name == "pacman":
            return ["pkexec", "pacman", "--noconfirm"] + (["-S", "--needed"] if install else ["-R"]) + todo
        raise Bad("this distribution's package tool is not supported")


def pre_pkg(p, policy, kind):
    from . import distro
    _allowed(p, {"packages", "with_dependents"})
    pkgs = p.get("packages")
    if not isinstance(pkgs, list) or not pkgs or len(pkgs) > 30:
        raise Bad("'packages' must be a list of 1-30 package names")
    for x in pkgs:
        if not isinstance(x, str) or not PKG_RE.match(x) or x.startswith("-"):
            raise Bad(f"invalid package name {x!r}")
    d = distro.detect()
    if not d.known:
        raise Bad(f"package steps are not supported on {d.name} (unknown package tool)")
    if kind.startswith("dnf_") and d.pkg != "dnf":
        raise Bad(f"this is {d.name}; use pkg_install/pkg_remove")
    be = Backend(d.pkg)
    install = kind in ("dnf_install", "pkg_install")
    word = "INSTALL" if install else "REMOVE"
    if not install:
        bad = [x for x in pkgs if PKG_CORE.match(x)]
        if bad:
            return Pre(kind, {"packages": pkgs}, NO, f"REMOVE {', '.join(pkgs)}",
                       detail=f"core system packages are never removed by the AI: {bad}")
    have = be.installed(policy, pkgs)
    todo = [x for x in pkgs if (x not in have) == install]
    if not todo:
        return Pre(kind, {"packages": pkgs}, ASK, f"{word} {', '.join(pkgs)}",
                   noop=f"already {'installed' if install else 'absent'}")
    detail = f"package tool: {d.pkg} on {d.name}"
    if not install:
        bad = [x for x in todo if PKG_CORE.match(x)]
        if bad:
            return Pre(kind, {"packages": todo}, NO, f"REMOVE {', '.join(todo)}",
                       detail=f"core system packages are never removed by the AI: {bad}")
        deps = be.also_removed(policy, todo)
        core_deps = [x for x in deps if PKG_CORE.match(x)]
        if core_deps:
            return Pre(kind, {"packages": todo}, NO, f"REMOVE {', '.join(todo)}",
                       detail=f"would also remove core packages: {core_deps}")
        if deps and not p.get("with_dependents"):
            raise Bad(f"removing {', '.join(todo)} would also remove installed package(s) that "
                      f"need them: {', '.join(deps[:15])}. Add them to the list, or set "
                      "with_dependents:true if that is intended")
        if deps and be.name == "pacman":
            raise Bad(f"pacman cannot remove {', '.join(todo)} while {', '.join(deps[:10])} need "
                      "them; list those packages too")
        detail += f"\nalso removed: {', '.join(deps) or 'nothing else'}"
    line = short(f"{word} package(s) {', '.join(todo)} (admin password" +
                 (", downloads)" if install else ")"))
    argv = be.argv(install, todo)

    def go(ctx):
        before = be.snapshot(policy)
        r = cmd(policy, argv, timeout=900, ctx=ctx)
        after = be.snapshot(policy)
        added, removed = sorted(after - before), sorted(before - after)
        now = be.installed(policy, todo)
        ok = (set(todo) <= now) if install else not (set(todo) & now)
        changed = "yes" if (added or removed) else "no"
        status = "ok" if (r.exit_code == 0 and ok) else ("verify_failed" if r.exit_code == 0 else "failed")
        names = lambda lst: sorted({be.name_of(x) for x in lst})
        inv = None
        if install and added:
            inv = {"do": "pkg_remove", "packages": names(added), "with_dependents": True}
        elif removed:
            inv = {"do": "pkg_install", "packages": names(removed)}
        rr = res(r, status=status, changed=changed, inverse=inv,
                 verify={"installed_now": sorted(now), "added": added, "removed": removed,
                         "passed": ok})
        rr["stdout"] = r.stdout + f"\n[steward] packages added: {added}\n[steward] packages removed: {removed}"
        return rr

    return Pre(kind, {"packages": todo}, ASK, line, go, network=install,
               detail=" ".join(argv[1:]) + "\n" + detail)


def pre_pkg_maint(p, policy, kind):
    from . import distro
    _allowed(p, set())
    d = distro.detect()
    if not d.known:
        raise Bad(f"not supported on {d.name} (unknown package tool)")
    tumble = "tumbleweed" in d.id
    if kind == "pkg_upgrade":
        argv = {"dnf": ["pkexec", "dnf", "-y", "upgrade"],
                "apt": ["pkexec", "sh", "-c", "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get -y upgrade"],
                "pacman": ["pkexec", "pacman", "-Syu", "--noconfirm"],
                "zypper": ["pkexec", "zypper", "--non-interactive", "dist-upgrade" if tumble else "update"]}[d.pkg]
        line = "UPDATE all installed software (admin password, downloads; can't be undone)"
    else:
        argv = {"dnf": ["pkexec", "dnf", "clean", "packages"], "apt": ["pkexec", "apt-get", "clean"],
                "pacman": ["pkexec", "pacman", "-Sc", "--noconfirm"],
                "zypper": ["pkexec", "zypper", "clean"]}[d.pkg]
        line = "DELETE downloaded package files (package cache) (admin password)"
    be = Backend(d.pkg)

    def go(ctx):
        before = be.snapshot(policy) if kind == "pkg_upgrade" else set()
        r = cmd(policy, argv, timeout=3600 if kind == "pkg_upgrade" else 300, ctx=ctx)
        after = be.snapshot(policy) if kind == "pkg_upgrade" else set()
        added, removed = sorted(after - before), sorted(before - after)
        rr = res(r, changed=("yes" if (added or removed) else "no") if kind == "pkg_upgrade" else
                 ("yes" if r.exit_code == 0 else "unknown"),
                 verify={"exit_ok": r.exit_code == 0, "packages_changed": len(added) + len(removed),
                         "passed": r.exit_code == 0})
        if kind == "pkg_upgrade":
            rr["stdout"] = r.stdout + f"\n[steward] {len(added)} package version(s) installed, " \
                f"{len(removed)} replaced or removed"
        return rr

    return Pre(kind, {}, ASK, line, go, irreversible=True, network=(kind == "pkg_upgrade"),
               detail=" ".join(argv[1:]) + f"\npackage tool: {d.pkg} on {d.name}")


FLATPAK_RE = re.compile(r"^[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+$")


def pre_flatpak(p, policy, kind):
    _allowed(p, {"app"})
    app = _s(p, "app", maxlen=200)
    if not FLATPAK_RE.match(app):
        raise Bad(f"invalid flatpak app id {app!r}")
    r = cmd(policy, ["flatpak", "list", "--app", "--columns=application,installation"])
    inst = [l.split()[1] for l in r.stdout.splitlines() if l.split() and l.split()[0] == app
            and len(l.split()) > 1]
    if not inst:
        return Pre(kind, {"app": app}, ASK, f"REMOVE flatpak {app}", noop="not installed")
    scope = inst[0]
    argv = ["flatpak", "uninstall", "-y", "--noninteractive", f"--{scope}", app]
    line = short(f"REMOVE flatpak {app} ({scope}{', admin password' if scope == 'system' else ''})")

    def go(ctx):
        rr = cmd(policy, argv, timeout=600, ctx=ctx)
        r2 = cmd(policy, ["flatpak", "list", "--app", "--columns=application"])
        gone = app not in r2.stdout.split()
        return res(rr, status="ok" if (rr.exit_code == 0 and gone) else
                   ("verify_failed" if rr.exit_code == 0 else "failed"),
                   changed="yes" if gone else "unknown",
                   verify={"check": "app no longer listed", "passed": gone},
                   warnings=["app data in ~/.var/app is kept"])

    return Pre(kind, {"app": app}, ASK, line, go, irreversible=True, detail=" ".join(argv))


TYPES = {
    "inspect": pre_inspect, "check": pre_inspect, "shell": pre_shell,
    "user_service": pre_service, "system_service": pre_service, "kill": pre_kill,
    "trash": pre_trash, "restore": pre_restore, "purge_trash": pre_purge,
    "write_file": pre_write, "edit_file": pre_edit, "restore_backup": pre_restore_backup,
    "dnf_install": pre_pkg, "dnf_remove": pre_pkg, "pkg_install": pre_pkg, "pkg_remove": pre_pkg,
    "flatpak_remove": pre_flatpak, "pkg_upgrade": pre_pkg_maint, "pkg_clean": pre_pkg_maint,
}
# 'undo' is expanded by the engine into the recorded inverse steps.


def preflight(step, policy):
    """Validate one step; returns Pre or raises Bad."""
    if not isinstance(step, dict):
        raise Bad("each step must be an object")
    do = step.get("do")
    if do not in TYPES:
        raise Bad(f"unknown 'do': {do!r}. Valid: {sorted(TYPES) + ['undo']}")
    for k in ("expect",):
        if k in step:
            e = step[k]
            if not isinstance(e, dict) or set(e) - {"exit", "contains", "absent", "regex"}:
                raise Bad("'expect' must be an object with exit/contains/absent/regex")
            if "regex" in e:
                try:
                    re.compile(e["regex"])
                except re.error as ex:
                    raise Bad(f"bad expect.regex: {ex}")
    for k in ("check", "only_if"):
        if k in step:
            _s(step, k, maxlen=1000)
            from .looks import syntax_problem
            sp = syntax_problem(step[k])
            if sp:
                raise Bad(f"{k}: {sp}")
    if step.get("on_fail", "ask") not in ("ask", "skip", "stop"):
        raise Bad("on_fail must be ask, skip or stop")
    t = step.get("timeout")
    if t is not None and (not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= policy.max_timeout):
        raise Bad(f"timeout must be 1..{policy.max_timeout} seconds")
    params = {k: v for k, v in step.items() if k not in COMMON or k == "timeout"}
    params.pop("do", None)
    pre = TYPES[do](params, policy, do)
    pre.fp = fingerprint(pre.do, pre.params)
    return pre


def check_expect(expect, out, exit_code):
    """Returns list of failed expectation descriptions."""
    fails = []
    if not expect:
        return fails
    if "exit" in expect and exit_code != expect["exit"]:
        fails.append(f"exit {exit_code}, expected {expect['exit']}")
    if "contains" in expect and str(expect["contains"]) not in out:
        fails.append(f"output does not contain {expect['contains']!r}")
    if "absent" in expect and str(expect["absent"]) in out:
        fails.append(f"output contains {expect['absent']!r}")
    if "regex" in expect and not re.search(expect["regex"], out, re.M):
        fails.append(f"output does not match /{expect['regex']}/")
    return fails
