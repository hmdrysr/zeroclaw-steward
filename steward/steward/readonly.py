"""Read-only looks: an allow-list of system queries that need D-Bus/netlink
(and so cannot run in the network-less, D-Bus-less sandbox), plus presets.

Allow-listed queries run with a fixed argv (no shell). Anything else runs in
the read-only sandbox. A pipeline whose FIRST command is allow-listed runs
that command natively and feeds its output into the rest of the pipeline,
which runs in the sandbox: e.g. `systemctl --user list-units | grep tracker`.
"""
import os
import re
import shlex

from . import config

SYSTEMCTL_VERBS = {"status", "is-active", "is-enabled", "is-failed", "is-system-running",
                   "list-units", "list-unit-files", "list-timers", "list-sockets",
                   "list-dependencies", "list-jobs", "list-paths", "show", "cat", "get-default"}
SYSTEMCTL_FLAGS = {"--user", "--system", "--no-pager", "--plain", "--no-legend", "--all", "-a",
                   "--full", "-l", "--failed", "--value", "--quiet", "-q", "--recursive", "-r",
                   "--reverse", "--no-ask-password"}
SYSTEMCTL_VFLAGS = {"--type", "-t", "--state", "--property", "-p", "--lines", "-n",
                    "--output", "-o"}
LOGINCTL_VERBS = {"list-sessions", "list-users", "list-seats", "show-session", "show-user",
                  "show-seat", "session-status", "user-status", "seat-status"}
FLATPAK_VERBS = {"list", "info", "remotes", "history", "ps", "--version"}
IP_OBJECTS = {"addr", "address", "a", "link", "l", "route", "r", "ro", "neigh", "neighbour",
              "n", "rule", "maddr", "-br", "-brief"}
IP_BAD = {"add", "del", "delete", "set", "flush", "change", "replace", "append", "prepend",
          "save", "restore", "exec", "up", "down", "dev-set", "monitor"}
DNF_VERBS = {"list", "info", "repoquery", "search", "provides", "leaves", "history", "check-update"}
DNF_FLAGS = {"-q", "--quiet", "-C", "--cacheonly", "--installed", "--available", "--upgrades",
             "--userinstalled", "--recent", "--all", "--whatrequires", "--whatprovides",
             "--whatdepends", "--qf", "--queryformat", "--info", "--list", "--requires",
             "--provides", "--latest-limit", "--arch", "--showduplicates", "--extras",
             "--leaves", "--unneeded", "--duplicates", "--reverse"}
RPM_FLAGS = {"-q", "--query", "-a", "--all", "-i", "--info", "-l", "--list", "-f", "--file",
             "-c", "--configfiles", "-d", "--docfiles", "-R", "--requires", "--provides",
             "--whatrequires", "--whatprovides", "--qf", "--queryformat", "--last",
             "--changelog", "-V", "--verify", "--scripts", "--conflicts", "--obsoletes",
             "--recommends", "--suggests", "-s", "--state", "--dump", "-p", "--package",
             "--nodeps", "--nofiledigest", "--nosize", "--nomtime", "-g", "--group"}


def _rpm_ok(args):
    """Only query/verify forms; every flag from an allow-list. Macros can run shell
    commands (e.g. --eval '%(...)'), so any macro syntax is refused outright."""
    if not args:
        return False
    first = args[0]
    if not (first in ("-q", "--query", "-V", "--verify") or re.fullmatch(r"-[qV][a-zA-Z]*", first)):
        return False
    for a in args:
        if "%(" in a or "%{lua" in a or "%{expand" in a:
            return False
        if a.startswith("--"):
            if a.split("=", 1)[0] not in RPM_FLAGS:
                return False
        elif a.startswith("-"):
            if not re.fullmatch(r"-[qVailfdcRspg]+", a):
                return False
    return True


def _flags_ok(args, plain, valued):
    i = 0
    pos = []
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            name = a.split("=", 1)[0]
            if name in plain and "=" not in a:
                pass
            elif name in valued:
                if "=" not in a:
                    i += 1
            else:
                return None
        else:
            pos.append(a)
        i += 1
    return pos


def native_query(argv):
    """Returns an argv to run natively if this is an allow-listed read-only query, else None."""
    if not argv:
        return None
    cmd, args = os.path.basename(argv[0]), argv[1:]
    if argv[0] != cmd and not argv[0].startswith(("/usr/bin/", "/bin/", "/usr/sbin/")):
        return None
    if cmd == "systemctl":
        pos = _flags_ok(args, SYSTEMCTL_FLAGS, SYSTEMCTL_VFLAGS)
        if pos is None:
            return None
        if pos and pos[0] not in SYSTEMCTL_VERBS:
            return None
        return [cmd, "--no-pager"] + args
    if cmd == "loginctl":
        pos = _flags_ok(args, {"--no-pager", "--no-legend", "--value", "-a", "--all"},
                        {"-p", "--property"})
        if pos is None or (pos and pos[0] not in LOGINCTL_VERBS):
            return None
        return [cmd, "--no-pager"] + args
    if cmd == "systemd-analyze":
        if args and args[0] in ("blame", "critical-chain", "time") and \
                all(a in ("--user", "--no-pager") or not a.startswith("-") for a in args[1:]):
            return [cmd, "--no-pager"] + args
        return None
    if cmd in ("timedatectl", "hostnamectl", "localectl"):
        if not args or (args[0] in ("status", "show") and len(args) == 1):
            return [cmd] + (args or ["status"])
        return None
    if cmd == "resolvectl":
        return [cmd] + args if args in (["status"], ["statistics"], []) else None
    if cmd == "powerprofilesctl":
        return [cmd] + args if args in (["get"], ["list"], []) else None
    if cmd == "upower":
        return [cmd] + args if args and args[0] in ("-e", "--enumerate", "-d", "--dump", "-i",
                                                    "--show-info") and len(args) <= 2 else None
    if cmd == "flatpak":
        pos = _flags_ok(args, {"--app", "--runtime", "--user", "--system", "-d",
                               "--show-details", "--all", "-a"}, {"--columns"})
        if pos is None or not pos or pos[0] not in FLATPAK_VERBS:
            return None
        return [cmd] + args
    if cmd == "nmcli":
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] in ("-t", "--terse", "-p", "--pretty"):
                i += 1
            elif args[i] in ("-f", "--fields", "-g", "--get-values", "-c", "--colors") and i + 1 < len(args):
                i += 2
            else:
                return None
        rest = args[i:]
        ok = (rest in ([], ["general"], ["g"], ["general", "status"], ["general", "permissions"],
                       ["device"], ["d"], ["dev"], ["device", "status"], ["d", "status"],
                       ["dev", "status"], ["connection"], ["c"], ["con"], ["connection", "show"],
                       ["c", "show"], ["con", "show"], ["radio"], ["r"])
              or (len(rest) == 3 and rest[0] in ("device", "d", "dev") and rest[1] == "show")
              or (len(rest) == 3 and rest[0] in ("connection", "c", "con") and rest[1] == "show"))
        return ["nmcli"] + args if ok else None
    if cmd == "ss":
        if any(a in ("-K", "--kill", "-D", "--diag", "-N", "--net") or
               (a.startswith("-") and not a.startswith("--") and "K" in a) for a in args):
            return None
        return [cmd] + args
    if cmd == "ip":
        if not args or any(a in IP_BAD for a in args) or "netns" in args or "-batch" in args \
                or "-b" in args or "-force" in args:
            return None
        objs = [a for a in args if not a.startswith("-")]
        if not objs or objs[0] not in IP_OBJECTS:
            return None
        if len(objs) > 1 and objs[1] not in ("show", "list", "ls", "sh", "get", "dev") \
                and objs[0] not in ("route", "r", "ro"):
            return None
        return [cmd] + args
    if cmd == "rpm":
        return [cmd] + args if _rpm_ok(args) else None
    if cmd == "dnf":
        pos = [a for a in args if not a.startswith("-")]
        if not pos or pos[0] not in DNF_VERBS:
            return None
        if pos[0] == "history" and len(pos) > 1 and pos[1] not in ("list", "info"):
            return None
        if any(a.startswith("-") and a.split("=", 1)[0] not in DNF_FLAGS for a in args):
            return None
        if any("%(" in a or "%{lua" in a for a in args):
            return None
        return [cmd, "-C", "-q"] + args
    if cmd in ("gsettings",):
        return [cmd] + args if args and args[0] in ("get", "list-schemas", "list-keys",
                                                     "list-children", "list-recursively",
                                                     "range", "describe", "writable") else None
    if cmd == "wpctl":
        return [cmd] + args if args and args[0] in ("status", "inspect", "get-volume") else None
    if cmd == "pactl":
        return [cmd] + args if args and (args[0] in ("info", "list", "stat") or
                                         args[0].startswith("get-")) else None
    return None


def _scan(command):
    """Yield (index, char, in_quotes) for each char, tracking quotes and escapes."""
    q = None
    esc = False
    for i, ch in enumerate(command):
        if esc:
            esc = False
            yield i, ch, True
            continue
        if ch == "\\" and q != "'":
            esc = True
            yield i, ch, True
            continue
        if q:
            if ch == q:
                q = None
            yield i, ch, True
            continue
        if ch in ("'", '"'):
            q = ch
            yield i, ch, True
            continue
        yield i, ch, False


def split_native_head(command):
    """If the command (or the first stage of a pipeline) is an allow-listed query,
    return (argv, rest_or_None). Otherwise None."""
    cut = None
    prev = ""
    chars = list(_scan(command))
    for k, (i, ch, quoted) in enumerate(chars):
        if quoted:
            prev = ""
            continue
        if ch in ";&<>()`$\n":
            if cut is None:
                return None
        if ch == "|":
            nxt = chars[k + 1][1] if k + 1 < len(chars) else ""
            if prev == "|" or nxt == "|":
                return None
            cut = i
            break
        prev = ch
    head = command if cut is None else command[:cut]
    if "$" in head or "`" in head:
        return None
    try:
        argv = shlex.split(head)
    except ValueError:
        return None
    nat = native_query(argv)
    if nat is None:
        return None
    rest = command[cut + 1:].strip() if cut is not None else None
    if rest == "":
        return None
    return nat, rest


def _home():
    return str(config.HOME)


def presets():
    h = _home()
    du_targets = [p for p in (f"{h}/.cache", f"{h}/Downloads", f"{h}/.local/share/Trash",
                              f"{h}/.local/share/flatpak", f"{h}/.var/app",
                              f"{h}/.local/share/containers", f"{h}/.ollama",
                              f"{h}/.cache/huggingface", f"{h}/.local/share/steward/trash")
                  if os.path.exists(p)]
    return {
        "overview": [
            ("uptime/load", ["uptime"]),
            ("memory", ["free", "-h"]),
            ("disk", ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs", "-x",
                      "overlay", "-x", "efivarfs"]),
            ("top memory", ["ps", "-eo", "pid,user,rss,pcpu,comm", "--sort=-rss"], 13),
            ("failed system units", ["systemctl", "--no-pager", "--failed", "--no-legend", "--plain"]),
            ("failed user units", ["systemctl", "--user", "--no-pager", "--failed", "--no-legend",
                                   "--plain"]),
        ],
        "memory": [
            ("memory", ["free", "-h"]),
            ("swap/zram", ["swapon", "--show"]),
            ("top processes by memory (rss KiB)",
             ["ps", "-eo", "pid,user,rss,pcpu,etime,comm", "--sort=-rss"], 26),
        ],
        "disk": [
            ("filesystems", ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs",
                             "-x", "overlay", "-x", "efivarfs"]),
            ("home top-level sizes", ["du", "-xh", "--max-depth=1", h], 0, "sort_h"),
            ("usual space users", ["du", "-xsh"] + du_targets if du_targets else ["true"]),
            ("journal", ["journalctl", "--disk-usage"]),
        ],
        "services": [
            ("running system services", ["systemctl", "--no-pager", "list-units",
                                         "--type=service", "--state=running", "--no-legend", "--plain"]),
            ("running user services", ["systemctl", "--user", "--no-pager", "list-units",
                                       "--type=service", "--state=running", "--no-legend", "--plain"]),
            ("failed", ["systemctl", "--no-pager", "--failed", "--no-legend", "--plain"]),
        ],
        "startup": [
            ("enabled system services", ["systemctl", "--no-pager", "list-unit-files",
                                         "--type=service", "--state=enabled", "--no-legend", "--plain"]),
            ("enabled user services", ["systemctl", "--user", "--no-pager", "list-unit-files",
                                       "--type=service", "--state=enabled", "--no-legend", "--plain"]),
            ("autostart entries", ["find", f"{h}/.config/autostart", "-maxdepth", "1", "-name", "*.desktop",
                                   "-printf", "%f\\n"] if os.path.isdir(f"{h}/.config/autostart") else ["true"]),
            ("boot time", ["systemd-analyze", "--no-pager", "time"]),
        ],
        "ports": [("listening sockets", ["ss", "-ltnupH"])],
        "failed": [("failed system units", ["systemctl", "--no-pager", "--failed", "--no-legend", "--plain"]),
                   ("failed user units", ["systemctl", "--user", "--no-pager", "--failed", "--no-legend",
                                          "--plain"])],
        "logs": [("errors this boot", ["journalctl", "--no-pager", "-p", "3", "-b", "-n", "60",
                                       "-o", "short-iso"])],
        "big_files": [("files over 200 MB in home",
                       ["find", h, "-xdev", "-type", "f", "-size", "+200M", "-printf",
                        "%s\t%p\n"], 25, "sort_n")],
        "packages": _package_preset(),
        "network": [("addresses", ["ip", "-br", "addr"]), ("routes", ["ip", "route"]),
                    ("NetworkManager", ["nmcli", "-t", "general", "status"])],
    }


def _package_preset():
    from .distro import detect
    pkg = detect().pkg
    items = []
    if pkg in ("dnf", "zypper"):
        items += [("installed packages", ["rpm", "-qa", "--qf", "%{NAME}\n"]),
                  ("recently installed", ["rpm", "-qa", "--last"], 30)]
    elif pkg == "apt":
        items += [("installed packages", ["dpkg-query", "-W", "-f=${Package}\n"]),
                  ("recently installed", ["bash", "-c", "grep -h ' install ' /var/log/dpkg.log 2>/dev/null"
                                          " | tail -n 30 | awk '{print $4}' | tac"])]
    elif pkg == "pacman":
        items += [("installed packages", ["pacman", "-Qq"]),
                  ("recently installed", ["bash", "-c", "grep -h ' installed ' /var/log/pacman.log 2>/dev/null"
                                          " | tail -n 30 | awk '{print $4}' | tac"])]
    if __import__("shutil").which("flatpak"):
        items.append(("flatpak apps", ["flatpak", "list", "--app", "--columns=application"]))
    return items
