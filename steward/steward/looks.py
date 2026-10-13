"""Running read-only commands: allow-listed system queries natively (fixed
argv), everything else in the read-only sandbox."""
import subprocess

from . import execute, readonly


def syntax_problem(command):
    """bash -n: catch syntax errors before anything runs or is approved."""
    try:
        p = subprocess.run(["bash", "-n", "-c", command], capture_output=True, text=True,
                           timeout=5, env=execute.clean_env(keep_session=False))
    except Exception as e:
        return f"could not syntax-check command: {e}"
    if p.returncode != 0:
        return "bash syntax error: " + p.stderr.strip().replace("bash: -c: ", "")[:300]
    return None


UNRELIABLE = ("Failed to connect to bus", "System has not been booted with systemd",
              "Failed to connect to user scope bus", "Could not connect to NetworkManager",
              "Error: NetworkManager is not running", "Couldn't connect to D-Bus", "cannot connect to D-Bus")


def run_readonly(command, policy, timeout=None, cancel=None, on_pid=None):
    """Run a read-only command. A leading '! ' inverts ONLY a clean yes/no answer
    (exit 0 <-> 1). A command that could not reach the system (e.g. no D-Bus in the
    sandbox) is reported as exit 125 'could not check' and is never inverted into a pass."""
    neg = command.lstrip().startswith("! ")
    inner = command.lstrip()[2:] if neg else command
    r = _run_readonly(inner, policy, timeout, cancel, on_pid)
    if any(s in (r.stderr or "") for s in UNRELIABLE) or r.exit_code == 125 or \
            (r.stderr or "").lstrip().startswith("bwrap: "):
        r.exit_code = 125
        r.stderr += ("\n[steward] could not check: this needs system services the read-only sandbox "
                     "cannot reach; put the query first (e.g. systemctl show ... | grep ...)")
        return r
    if neg and r.exit_code in (0, 1) and not (r.timed_out or r.cancelled):
        r.exit_code = 1 - r.exit_code
    return r


def _run_readonly(command, policy, timeout=None, cancel=None, on_pid=None):
    split = readonly.split_native_head(command)
    if split is None:
        return execute.run(["bash", "-c", command], policy=policy, sandbox="ro",
                           timeout=timeout, cancel_check=cancel, on_pid=on_pid)
    argv, rest = split
    r = execute.run(argv, policy=policy, timeout=timeout, cancel_check=cancel, on_pid=on_pid)
    r.sandbox = "native-query"
    if rest is None or r.timed_out or r.cancelled:
        return r
    r2 = execute.run(["bash", "-c", rest], policy=policy, sandbox="ro", timeout=timeout,
                     cancel_check=cancel, on_pid=on_pid, stdin_data=r.stdout)
    r2.stderr = (r.stderr + r2.stderr).strip()
    r2.sandbox = "native-query|ro"
    if r.exit_code not in (0,) and r2.exit_code == 0 and not r2.stdout.strip():
        r2.exit_code = r.exit_code
    return r2


def run_preset(name, policy, cancel=None):
    """Returns (exit_code, text, errors)."""
    sets = readonly.presets()
    if name not in sets:
        return None
    outs, errs, code = [], [], 0
    for item in sets[name]:
        title, argv = item[0], item[1]
        limit = item[2] if len(item) > 2 else 0
        post = item[3] if len(item) > 3 else None
        r = execute.run(argv, policy=policy, timeout=90, cancel_check=cancel)
        lines = r.stdout.rstrip("\n").splitlines()
        if post == "sort_h":
            units = {"K": 1, "M": 2, "G": 3, "T": 4}

            def key(l):
                s = l.split("\t")[0]
                try:
                    return float(s[:-1]) * 1024 ** units.get(s[-1], 0) if s[-1] in units else float(s)
                except ValueError:
                    return 0
            lines = sorted(lines, key=key, reverse=True)[:20]
        elif post == "sort_n":
            def keyn(l):
                try:
                    return int(l.split("\t")[0])
                except ValueError:
                    return 0
            lines = sorted(lines, key=keyn, reverse=True)
            lines = [f"{int(l.split(chr(9))[0]) / 1024 ** 3:.2f} GB\t{l.split(chr(9), 1)[1]}"
                     if "\t" in l else l for l in lines]
        if limit:
            lines = lines[:limit + 1] if lines and lines[0].lstrip().startswith("PID") else lines[:limit]
        if r.exit_code != 0 and not lines:
            body = "(failed)"
        else:
            body = "\n".join(lines) if lines else "(none)"
        outs.append(f"## {title}\n" + body)
        if r.exit_code != 0 and r.stderr.strip():
            errs.append(f"{title}: {r.stderr.strip().splitlines()[-1][:200]}")
            code = code or r.exit_code
    return code, "\n".join(outs), "\n".join(errs)
