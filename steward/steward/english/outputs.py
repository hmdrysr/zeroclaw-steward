"""Command output -> a few plain-English facts.

Each translator parses one well-known output format. If the format is not what
it expects, it says it could not summarise that part (the raw output stays
available). Facts only restate what the output says, plus clearly labelled
judgements with fixed thresholds (e.g. "nearly full" means 90% or more).
"""
import os
import re
from collections import Counter, defaultdict

from .errors import translate as translate_error

UNITS = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}


def to_bytes(s):
    m = re.fullmatch(r"([\d.,]+)\s*([BKMGTP]?)(i?B?)?", s.strip(), re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ".")) * UNITS[(m.group(2) or "B").upper()]
    except ValueError:
        return None


def human(n):
    for u, f in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= f:
            return f"{n / f:.1f} {u}"
    return f"{int(n)} B"


def nice_size(s):
    """'7.8Gi' -> '7.8 GB' (binary units, labelled the way people say them)."""
    b = to_bytes(s)
    return human(b) if b is not None else s


def _tilde(p):
    h = str(os.environ.get("STEWARD_TEST_HOME") or os.path.expanduser("~"))
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


# ---------------------------------------------------------------- translators
def _duration(s):
    s = s.strip().rstrip(",")
    parts = []
    m = re.search(r"(\d+)\s+days?", s)
    if m:
        parts.append(f"{m.group(1)} day{'s' if m.group(1) != '1' else ''}")
    m = re.search(r"(\d+):(\d+)$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h:
            parts.append(f"{h} hour{'s' if h != 1 else ''}")
        parts.append(f"{mi} minute{'s' if mi != 1 else ''}")
    m = re.search(r"(\d+)\s+min", s)
    if m and not re.search(r"\d+:\d+$", s):
        parts.append(f"{m.group(1)} minute{'s' if m.group(1) != '1' else ''}")
    return " ".join(parts) if parts else s


def uptime(text):
    m = re.search(r"up\s+(.*?),\s+\d+\s+users?.*load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", text)
    if not m:
        m = re.search(r"up\s+(.*?),\s+load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", text)
    if not m:
        return None
    up = _duration(m.group(1))
    load = float(m.group(2))
    cpus = os.cpu_count() or 1
    level = "light" if load < 0.7 * cpus else ("busy" if load < 1.5 * cpus else "overloaded")
    return [f"On for {up}. Workload right now: {level} (load {m.group(2)} on {cpus} processor threads)."]


def free(text):
    out = []
    mem = re.search(r"^Mem:\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", text, re.M)
    if not mem:
        return None
    total, used, avail = mem.group(1), mem.group(2), mem.group(6)
    tb, ab = to_bytes(total), to_bytes(avail)
    s = f"Memory: {nice_size(used)} of {nice_size(total)} in use, {nice_size(avail)} available."
    if tb and ab is not None:
        pct = 100 * (1 - ab / tb)
        if pct >= 90:
            s += " Memory is nearly full (90% or more in use)."
    out.append(s)
    sw = re.search(r"^Swap:\s+(\S+)\s+(\S+)", text, re.M)
    if sw:
        if to_bytes(sw.group(1)) in (0, None) and sw.group(1) in ("0", "0B"):
            out.append("Swap: none.")
        else:
            out.append(f"Swap: {nice_size(sw.group(2))} of {nice_size(sw.group(1))} used.")
    return out


def df(text):
    rows = [l.split() for l in text.splitlines()[1:] if l.strip()]
    groups = {}
    for r in rows:
        if len(r) < 6 or not r[4].endswith("%"):
            continue
        dev, size, used, avail, pct, mnt = r[0], r[1], r[2], r[3], r[4], " ".join(r[5:])
        key = (dev, size, used, avail, pct)        # same numbers = same filesystem (e.g. btrfs subvolumes)
        groups.setdefault(key, []).append(mnt)
    if not groups:
        return None
    out = []
    for (dev, size, used, avail, pct), mnts in groups.items():
        pct = int(pct.rstrip("%"))
        where = ", ".join(sorted(mnts, key=len)[:3])
        s = f"Disk {where}: {nice_size(used)} used of {nice_size(size)} ({pct}%), {nice_size(avail)} free."
        if pct >= 97:
            s += " It is FULL."
        elif pct >= 90:
            s += " It is nearly full (90% or more)."
        out.append(s)
    return out


def ps_mem(text, top=5):
    """ps output with a header including RSS and COMMAND -> biggest programs, grouped by name."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines or "RSS" not in lines[0].split():
        return None
    head = lines[0].split()
    i_rss, i_cmd = head.index("RSS"), head.index("COMMAND") if "COMMAND" in head else None
    if i_cmd is None:
        return None
    tot, cnt = Counter(), Counter()
    for l in lines[1:]:
        f = l.split()
        if len(f) <= max(i_rss, i_cmd) or not f[i_rss].isdigit():
            continue
        name = " ".join(f[i_cmd:]) if i_cmd == len(head) - 1 else f[i_cmd]
        tot[name] += int(f[i_rss]) * 1024
        cnt[name] += 1
    if not tot:
        return None
    parts = [f"{n} {human(b)}" + (f" ({cnt[n]} processes)" if cnt[n] > 1 else "")
             for n, b in tot.most_common(top)]
    return ["Biggest memory users (of the top processes): " + ", ".join(parts) + "."]


def unit_list(text, label):
    names = [l.split()[0] for l in text.splitlines() if l.split() and not l.startswith("0 loaded")
             and l.split()[0] not in ("UNIT", "●")]
    names = [n.lstrip("●").strip() for n in names if n.strip("●")]
    if "failed" in label.lower():
        return [f"{label}: none." if not names else
                f"{label}: {len(names)} — {', '.join(names[:10])}{' …' if len(names) > 10 else ''}."]
    if not names:
        return [f"{label}: none."]
    return [f"{label}: {len(names)} — {', '.join(n.replace('.service', '') for n in names[:12])}"
            f"{' …' if len(names) > 12 else ''}."]


def boot_time(text):
    m = re.search(r"Startup finished in (.*?)= ([\d.]+(?:min )?[\d.]*s)", text)
    if not m:
        return None
    parts = re.findall(r"([\d.]+(?:min )?[\d.]*m?s) \((\w+)\)", m.group(1))
    detail = ", ".join(f"{w} {t}" for t, w in parts)
    return [f"Start-up took {m.group(2)} ({detail})."]


def ss_listen(text):
    seen = {}
    for l in text.splitlines():
        f = l.split()
        if len(f) < 5:
            continue
        proto = f[0]
        local = f[4] if f[1] in ("LISTEN", "UNCONN") else f[3]
        m = re.match(r"^(.*):(\d+|\*)$", local)
        if not m:
            continue
        addr, port = m.group(1).strip("[]"), m.group(2)
        pm = re.search(r'users:\(\("([^"]+)"', l)
        prog = pm.group(1) if pm else "an unknown program"
        if addr in ("127.0.0.1", "::1", "127.0.0.53", "127.0.0.54") or addr.startswith("127.") or addr.endswith("%lo"):
            scope = "this computer only"
        elif addr in ("0.0.0.0", "*", "::", "[::]"):
            scope = "reachable from the network"
        else:
            scope = f"on {addr}"
        key = (prog, port, proto[:3])
        if key not in seen or scope == "reachable from the network":
            seen[key] = scope
    if not seen:
        return ["Nothing is listening for network connections."] if not text.strip() else None
    net = [f"{p} ({pr} port {po})" for (p, po, pr), s in seen.items() if s == "reachable from the network"]
    loc = [f"{p} ({pr} port {po})" for (p, po, pr), s in seen.items() if s != "reachable from the network"]
    out = []
    if net:
        out.append("Reachable from the network: " + ", ".join(net[:10]) + ".")
    if loc:
        out.append("Only on this computer: " + ", ".join(loc[:10]) + ".")
    return out


def journal_errors(text):
    if "-- No entries --" in text or not text.strip():
        return ["No errors logged since start-up."]
    groups = Counter()
    sample = {}
    total = 0
    for l in text.splitlines():
        m = re.match(r"^\S+\s+\S+\s+([^\s:\[]+)(?:\[\d+\])?:\s*(.*)$", l)
        if not m:
            continue
        total += 1
        src, msg = m.group(1), m.group(2)
        key = (src, re.sub(r"\d+", "#", msg)[:80])
        groups[key] += 1
        sample.setdefault(key, msg)
    if not total:
        return None
    out = [f"{total} error message{'s' if total != 1 else ''} logged since start-up."]
    for (src, k), n in groups.most_common(3):
        msg = sample[(src, k)][:140]
        _, meaning = translate_error(msg)
        out.append(f"{src} ({n}×): \"{msg}\"" + (f" — {meaning}" if meaning and "don't recognise" not in meaning else ""))
    return out


def du_list(text, label, home=None, top=8):
    rows = []
    for l in text.splitlines():
        if "\t" not in l:
            continue
        size, path = l.split("\t", 1)
        rows.append((to_bytes(size) or 0, size, path))
    if not rows:
        return None
    total = None
    if home:
        for b, s, p in rows:
            if p.rstrip("/") == home.rstrip("/"):
                total = s
        rows = [r for r in rows if r[2].rstrip("/") != home.rstrip("/")]
    rows.sort(reverse=True)
    parts = [f"{_tilde(p)} {nice_size(s)}" for b, s, p in rows[:top]]
    lead = f"{label}" + (f" (total {nice_size(total)})" if total else "")
    return [f"{lead}: " + ", ".join(parts) + "."]


def journal_usage(text):
    m = re.search(r"take up ([\d.]+[KMGT]?B?)", text)
    return [f"System logs use {nice_size(m.group(1))}."] if m else None


def big_files(text):
    rows = [l.split("\t", 1) for l in text.splitlines() if "\t" in l]
    if not rows:
        return ["No files over 200 MB in your home folder."] if "(none)" in text or not text.strip() else None
    parts = [f"{_tilde(p)} ({s})" for s, p in rows[:5]]
    return [f"{len(rows)} file{'s' if len(rows) != 1 else ''} over 200 MB. Biggest: " + ", ".join(parts) + "."]


def ip_addr(text):
    out = []
    kinds = (("wl", "Wi-Fi"), ("en", "wired"), ("eth", "wired"), ("ww", "mobile"), ("tun", "VPN"),
             ("wg", "VPN"), ("virbr", "virtual"), ("docker", "virtual"), ("br", "bridge"), ("veth", "virtual"))
    for l in text.splitlines():
        f = l.split()
        if len(f) < 2 or f[0] == "lo":
            continue
        kind = next((k for p, k in kinds if f[0].startswith(p)), "network")
        v4 = [a.split("/")[0] for a in f[2:] if "." in a]
        state = {"UP": "connected", "DOWN": "down", "UNKNOWN": "up"}.get(f[1], f[1].lower())
        out.append(f"{f[0]} ({kind}) is {state}" + (f", address {', '.join(v4)}" if v4 else ", no IPv4 address") + ".")
    return out or ["No network interfaces besides the internal loopback."]


def ip_route(text):
    m = re.search(r"^default via (\S+) dev (\S+)", text, re.M)
    if not m:
        return ["No default route: this computer has no path to the internet right now."]
    return [f"Internet traffic goes through {m.group(1)} on {m.group(2)}."]


def nm_status(text):
    f = text.strip().splitlines()[0].split(":") if text.strip() else []
    if len(f) < 4:
        return None
    return [f"NetworkManager: {f[0]}; internet: {f[1]}; Wi-Fi: {f[3]}."]


def swapon(text):
    rows = [l.split() for l in text.splitlines()[1:] if l.split()]
    if not rows:
        return ["Swap: none."]
    return ["Swap: " + "; ".join(f"{r[0]}{' (compressed memory)' if 'zram' in r[0] else ''} "
                                  f"{nice_size(r[2])}, {nice_size(r[3])} used" for r in rows if len(r) >= 4) + "."]


def count_list(text, label, n=10):
    items = [l.split()[0] for l in text.splitlines() if l.strip() and l.strip() != "(none)"]
    if not items:
        return [f"{label}: none."]
    return [f"{label}: {len(items)}" + (f" — {', '.join(items[:n])}{' …' if len(items) > n else ''}" if n else "") + "."]


TITLES = {
    "uptime/load": uptime, "memory": free, "disk": df, "filesystems": df,
    "top memory": ps_mem, "top processes by memory (rss KiB)": ps_mem,
    "failed system units": lambda t: unit_list(t, "Failed system services"),
    "failed user units": lambda t: unit_list(t, "Failed user services"),
    "failed": lambda t: unit_list(t, "Failed services"),
    "running system services": lambda t: unit_list(t, "Running system services"),
    "running user services": lambda t: unit_list(t, "Running user services"),
    "enabled system services": lambda t: unit_list(t, "System services that start at boot"),
    "enabled user services": lambda t: unit_list(t, "User services that start at login"),
    "autostart entries": lambda t: count_list(t, "Apps that start at login"),
    "boot time": boot_time, "listening sockets": ss_listen, "errors this boot": journal_errors,
    "home top-level sizes": lambda t: du_list(t, "Biggest folders in your home", home=_home()),
    "usual space users": lambda t: du_list(t, "Caches and app data"),
    "journal": journal_usage, "files over 200 MB in home": big_files,
    "addresses": ip_addr, "routes": ip_route, "NetworkManager": nm_status, "swap/zram": swapon,
    "installed packages": lambda t: [f"Installed packages: {len([l for l in t.splitlines() if l.strip()])}."],
    "recently installed": lambda t: count_list(t, "Most recently installed", n=8),
    "flatpak apps": lambda t: count_list(t, "Flatpak apps", n=12),
}


def _home():
    return str(os.environ.get("STEWARD_TEST_HOME") or os.path.expanduser("~"))


def summarise_sections(text):
    """'## title\\nbody...' blocks (from presets) -> list of English fact sentences."""
    facts = []
    for block in re.split(r"^## ", text, flags=re.M):
        if not block.strip():
            continue
        title, _, body = block.partition("\n")
        if body.strip() == "(failed)":
            t = title.strip()
            facts.append(f"{t[0].upper() + t[1:]}: COULD NOT CHECK (command failed; not the same as none).")
            continue
        fn = TITLES.get(title.strip())
        res = None
        if fn:
            try:
                res = fn(body)
            except Exception:
                res = None
        if res:
            facts.extend(res)
        elif body.strip() in ("(none)", ""):
            facts.append(f"{title.strip()[0].upper() + title.strip()[1:]}: none.")
        else:
            facts.append(f"({title.strip()}: I couldn't summarise this part; the raw output is kept.)")
    return list(dict.fromkeys(facts))


def translate_command(command, stdout):
    """Facts for a single well-known read-only command, else None."""
    c = command.strip()
    table = [
        (r"^free(\s+-[hmgb]+)?$", free), (r"^df(\s+-[hTx]+.*)?$", df), (r"^uptime$", uptime),
        (r"^ss\s+-[a-zA-Z]*l[a-zA-Z]*", ss_listen), (r"^ip\s+(-br|-brief)\s+(a|addr|address)$", ip_addr),
        (r"^ip\s+(r|route)(\s+show)?$", ip_route), (r"^swapon\s+--show$", swapon),
        (r"^systemd-analyze(\s+time)?$", boot_time),
        (r"^journalctl\b.*-p\s*(3|err)", journal_errors),
    ]
    for rx, fn in table:
        if re.match(rx, c):
            try:
                return fn(stdout)
            except Exception:
                return None
    return None
