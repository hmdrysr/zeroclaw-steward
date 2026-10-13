"""Process execution.

Sandbox modes (bubblewrap; refused if bubblewrap is unavailable, never run
unsandboxed instead):
  "ro"  read-only looks and checks. Root filesystem read-only, private /tmp,
        no network, no D-Bus/systemd/pipewire sockets, secret folders hidden,
        own PID namespace with the host's /proc mounted read-only: it can SEE
        your processes but cannot signal or change them.
  "rw"  an approved shell step. Normal user rights, except Steward's own
        policy/records are read-only, secret folders hidden, and its own PID
        namespace, so anything it leaves running in the background dies with it.
  None  typed actions and allow-listed read-only system queries: fixed argv
        chosen by Steward, never a shell.
"""
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

from . import config
from .redact import redact

SAFE_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ",
                 "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY")


def clean_env(keep_session=True):
    keys = SAFE_ENV_KEYS + tuple(k for k in os.environ.get("STEWARD_PASS_ENV", "").split(",") if k)
    env = {k: os.environ[k] for k in keys if k in os.environ}
    if not keep_session:
        for k in ("DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY"):
            env.pop(k, None)
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    env.update({"LC_ALL": env.get("LC_ALL", "C.UTF-8"), "SYSTEMD_PAGER": "", "PAGER": "cat",
                "TERM": "dumb", "SYSTEMD_COLORS": "0", "NO_COLOR": "1"})
    return env


@dataclass
class Result:
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    cancelled: bool
    duration: float
    sandbox: str = "none"


_BWRAP_OK = None


def bwrap_available():
    global _BWRAP_OK
    if _BWRAP_OK is None:
        b = shutil.which("bwrap")
        _BWRAP_OK = False
        if b:
            try:
                p = subprocess.run([b, "--ro-bind", "/", "/", "--ro-bind", "/proc", "/proc",
                                    "--dev", "/dev", "--unshare-pid", "--unshare-net", "true"],
                                   capture_output=True, timeout=10)
                _BWRAP_OK = p.returncode == 0
            except Exception:
                _BWRAP_OK = False
    return _BWRAP_OK


def _hide(args, policy):
    for p in policy.secret_paths:
        path = config.expand(p)
        if path.is_dir():
            args += ["--tmpfs", str(path)]
        elif path.exists():
            args += ["--ro-bind", "/dev/null", str(path)]


def bwrap_args(mode, policy, net=False):
    if mode == "ro":
        a = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--ro-bind", "/proc", "/proc",
             "--tmpfs", "/tmp", "--tmpfs", "/var/tmp",
             "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
             "--die-with-parent", "--new-session"]
        for d in ("/run/user", "/run/dbus", "/run/systemd", "/run/pipewire"):
            if os.path.isdir(d):
                a += ["--tmpfs", d]
        # The host /proc is visible so ps/top work, but other processes' environments
        # (which hold API keys, e.g. ZeroClaw's) are masked.
        uid = os.getuid()
        now = time.time()
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    st = os.stat(f"/proc/{pid}")
                    # processes that just started are skipped: they may exit before the
                    # sandbox mounts, and long-running key holders (e.g. ZeroClaw) are older
                    if st.st_uid == uid and now - st.st_mtime > 2:
                        a += ["--ro-bind-try", "/dev/null", f"/proc/{pid}/environ"]
                except OSError:
                    pass
    elif mode == "rw":
        a = ["bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "--unshare-pid", "--die-with-parent", "--new-session"]
        if not net:
            a.append("--unshare-net")
        for p in policy.protected_paths:
            path = config.expand(p)
            if path.exists():
                a += ["--ro-bind", str(path), str(path)]
    else:
        raise ValueError(mode)
    _hide(a, policy)
    return a


def _read_capped(f, cap):
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size <= cap:
        return f.read().decode("utf-8", "replace"), False
    half = cap // 2
    head = f.read(half)
    f.seek(size - half)
    tail = f.read(half)
    mid = f"\n...[{size - 2 * half} bytes not stored]...\n"
    return head.decode("utf-8", "replace") + mid + tail.decode("utf-8", "replace"), True


def run(argv, *, policy, sandbox=None, timeout=None, cwd=None, cancel_check=None,
        on_pid=None, stdin_data=None, env=None, net=False, cap=None):
    timeout = max(1, min(int(timeout or policy.default_timeout), int(cap or policy.max_timeout)))
    if sandbox:
        for attempt in range(3):
            r = _run_once(argv, policy, sandbox, timeout, cwd, cancel_check, on_pid, stdin_data, env, net)
            if not _setup_failed(r):
                return r
        r.exit_code = 125
        r.stderr += "\n[steward] the sandbox could not be set up; nothing was run"
        return r
    return _run_once(argv, policy, sandbox, timeout, cwd, cancel_check, on_pid, stdin_data, env, net)


def _setup_failed(r):
    """bubblewrap itself failed before running the command (e.g. a process vanished
    while its /proc entry was being masked)."""
    first = (r.stderr or "").lstrip().splitlines()[:1]
    return bool(first) and first[0].startswith("bwrap: ") and r.duration < 5


def _run_once(argv, policy, sandbox, timeout, cwd, cancel_check, on_pid, stdin_data, env, net):
    if sandbox:
        if not bwrap_available():
            return Result(126, "", "sandbox unavailable: bubblewrap (bwrap) missing or cannot "
                          "create namespaces; refusing to run unsandboxed", False, False, False,
                          False, 0.0, sandbox)
        argv = bwrap_args(sandbox, policy, net) + (["--chdir", str(cwd)] if cwd else []) + ["--"] + argv
        cwd = None
        env = env or clean_env(keep_session=(sandbox == "rw"))
    env = env or clean_env()
    t0 = time.time()
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=fo, stderr=fe,
                                 stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                                 start_new_session=True)
        except (FileNotFoundError, PermissionError, NotADirectoryError) as e:
            return Result(127, "", f"could not start {argv[0]!r}: {e}", False, False, False,
                          False, 0.0, sandbox or "none")
        if on_pid:
            on_pid(p.pid)
        if stdin_data is not None:
            try:
                p.stdin.write(stdin_data.encode())
                p.stdin.close()
            except BrokenPipeError:
                pass
        timed_out = cancelled = False
        while True:
            try:
                p.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.time() - t0 > timeout:
                timed_out = True
            elif cancel_check and cancel_check():
                cancelled = True
            if timed_out or cancelled:
                _kill_group(p)
                break
        code = p.returncode if p.returncode is not None else -9
        out, otr = _read_capped(fo, policy.max_stored_bytes)
        err, etr = _read_capped(fe, policy.max_stored_bytes)
    if timed_out:
        err += f"\n[steward] killed after {timeout}s timeout (whole process group)"
        code = 124
    if cancelled:
        err += "\n[steward] killed: stopped by the user"
        code = 130
    return Result(code, redact(out), redact(err), otr, etr, timed_out, cancelled,
                  round(time.time() - t0, 2), sandbox or "none")


def _kill_group(p):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            return
        try:
            p.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue
