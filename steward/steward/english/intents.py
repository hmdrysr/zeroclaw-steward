"""Plain-English request -> one known, tested task (a "recipe"), deterministically.

Safety rules, applied before anything else:
  - Negation ("don't remove firefox", "never update") never triggers an action.
  - Advice questions ("should I remove X?", "is it safe to ...") never trigger
    an action; they go to the main assistant (with your consent) or get facts only.
  - Several actions in one request ("update and remove X") go to the main assistant.
  - Names (packages, services, programs) must exist on this system, or the
    request is not acted on; similar names are suggested instead.
  - Read-only tasks run directly. Every change still goes through Steward's
    normal checks and your approval.
"""
import difflib
import re
from dataclasses import dataclass, field

CONTRACTIONS = {"don't": "do not", "dont": "do not", "doesn't": "does not", "isn't": "is not",
                "can't": "cannot", "cant": "cannot", "won't": "will not", "shouldn't": "should not",
                "what's": "what is", "whats": "what is", "how's": "how is", "it's": "it is",
                "i'm": "i am", "where's": "where is", "who's": "who is", "that's": "that is",
                "never": "never", "wanna": "want to"}
SYN = {  # word -> canonical
    "ram": "memory", "mem": "memory", "memory": "memory",
    "storage": "disk", "space": "disk", "drive": "disk", "ssd": "disk", "hdd": "disk", "disk": "disk",
    "harddrive": "disk", "partition": "disk",
    "slow": "slow", "sluggish": "slow", "laggy": "slow", "lagging": "slow", "freezing": "slow", "hanging": "slow",
    "app": "app", "apps": "app", "application": "app", "applications": "app", "program": "app",
    "programs": "app", "software": "app", "package": "app", "packages": "app",
    "service": "service", "services": "service", "daemon": "service", "daemons": "service",
    "wifi": "network", "wi-fi": "network", "internet": "network", "network": "network", "online": "network",
    "connection": "network", "connected": "network", "ethernet": "network", "ip": "network",
    "error": "error", "errors": "error", "problem": "error", "problems": "error", "broken": "error",
    "failing": "error", "failed": "error", "crash": "error", "crashes": "error", "crashed": "error",
    "wrong": "error", "issue": "error", "issues": "error", "warnings": "error",
    "update": "update", "updates": "update", "upgrade": "update", "upgrades": "update", "patch": "update",
    "uninstall": "remove", "remove": "remove", "delete": "remove", "purge": "remove", "get rid of": "remove",
    "install": "install", "add": "install",
    "big": "big", "biggest": "big", "large": "big", "largest": "big", "huge": "big", "heavy": "big",
    "file": "file", "files": "file", "folder": "file", "folders": "file", "directories": "file",
    "boot": "boot", "startup": "boot", "start-up": "boot", "login": "boot", "autostart": "boot",
    "restart": "restart", "reboot": "restart", "reload": "restart",
    "stop": "stop", "close": "stop", "quit": "stop", "kill": "stop", "end": "stop", "exit": "stop",
    "start": "start", "launch": "start", "run": "start", "open": "start",
    "trash": "trash", "bin": "trash", "recycle": "trash", "wastebasket": "trash",
    "cache": "cache", "caches": "cache",
    "version": "version", "release": "version", "kernel": "kernel",
    "port": "port", "ports": "port", "listening": "port", "listen": "port",
    "running": "running", "uptime": "uptime",
    "installed": "installed", "have": "have",
}
NEGATIONS = {"not", "never", "no", "without", "cannot", "dont", "nothing", "stop me", "avoid"}
ADVICE = re.compile(r"^(should|would|could i|can i|is it (safe|ok|okay|wise|a good idea)|"
                    r"what happens if|what if|why would|do i need|is there any risk)\b")
POLITE = re.compile(r"^(please |can you |could you |would you |will you |i want to |i'd like to |"
                    r"i would like to |help me |kindly |pls |plz |i need to |go ahead and |just )+")
QUESTION = re.compile(r"^(what|which|how|why|where|when|is|are|do|does|did|am|who|was|were|has|have|"
                      r"show|tell|list|check|give|any)\b")


def normalise(text):
    t = text.lower().strip()
    t = t.replace("’", "'")
    for a, b in CONTRACTIONS.items():
        t = re.sub(rf"\b{re.escape(a)}\b", b, t)
    t = re.sub(r"[^a-z0-9@.+_\- ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    t = re.sub(r"\bget rid of\b", "remove", t)
    return t


def canon_tokens(t):
    toks = t.split()
    out = []
    for w in toks:
        w2 = w.strip(".")
        out.append(SYN.get(w2, w2))
    if "get rid of" in t:
        out.append("remove")
    return out


@dataclass
class Recipe:
    id: str
    title: str
    kind: str                      # look | answer | plan
    need: list                     # list of sets of canonical tokens/phrases; each set must hit
    avoid: set = field(default_factory=set)
    slot: str = None               # package | service | process | None
    verbs: set = field(default_factory=set)   # action words, for negation checks
    weight: int = 0
    pattern: str = None            # optional regex the request must match


R = [
    Recipe("health", "check how the computer is doing", "look",
           [{"computer", "system", "pc", "laptop", "machine", "health", "slow", "status", "everything"},
            {"check", "how", "health", "slow", "ok", "okay", "status", "doing", "fine", "why"}],
           avoid={"install", "remove", "update", "restart"}),
    Recipe("memory", "show what is using memory", "look", [{"memory"}], avoid={"install", "remove"}, weight=2),
    Recipe("disk", "show disk space", "look", [{"disk"}],
           avoid={"install", "remove", "big", "memory", "format", "partition"}, weight=2),
    Recipe("big_files", "find what takes up the most space", "look",
           [{"big", "taking", "eating", "using", "hog", "hogging"}, {"disk", "file"}],
           avoid={"install", "remove", "memory"}, weight=3),
    Recipe("services", "list running services", "look", [{"service"}, {"running", "what", "list", "show", "which"}],
           avoid={"restart", "stop", "start", "remove", "install", "boot"}, slot=None, weight=1),
    Recipe("startup", "show what starts automatically and how long start-up takes", "look", [{"boot"}],
           avoid={"install", "remove", "restart", "stop", "disable", "prevent"}, weight=1),
    Recipe("errors", "show recent errors", "look", [{"error"}], avoid={"install", "remove", "update"}, weight=1),
    Recipe("ports", "show which programs listen for network connections", "look", [{"port"}], weight=2),
    Recipe("network", "check the network connection", "look", [{"network"}],
           avoid={"install", "remove", "port", "restart", "stop"}, weight=1),
    Recipe("packages", "show installed software", "look",
           [{"app"}, {"installed", "list", "show", "recently", "what", "which"}],
           avoid={"remove", "update"}, weight=0),
    Recipe("os_version", "show which Linux version this is", "answer",
           [{"version", "kernel", "fedora", "linux", "distro", "distribution", "os", "ubuntu", "debian", "arch",
             "opensuse"}, {"version", "which", "what", "kernel"}],
           avoid={"install", "remove", "update"}, weight=1),
    Recipe("uptime", "show how long the computer has been on", "answer",
           [{"uptime", "long", "since"}, {"uptime", "on", "running", "booted", "boot", "up"}],
           avoid={"install", "remove"}, weight=1),
    Recipe("is_installed", "check whether something is installed", "answer",
           [{"installed", "have"}], avoid={"remove", "update"}, slot="package_any", weight=2),
    Recipe("service_status", "check whether a service is running", "answer",
           [{"running", "active", "status", "working", "up"}],
           avoid={"install", "remove", "what", "which", "list", "network", "memory", "disk", "app"},
           slot="service", weight=4, pattern=r"^(is|are)\b"),
    Recipe("update_system", "update all installed software", "plan",
           [{"update"}], avoid={"remove", "install", "check", "available", "any", "are", "how"},
           verbs={"update"}, weight=2),
    Recipe("clean_package_cache", "delete downloaded package files (cache)", "plan",
           [{"cache"}, {"clean", "clear", "remove", "empty", "delete", "free"}, {"app", "dnf", "apt", "pacman",
                                                                                "zypper", "update"}],
           verbs={"clean", "clear", "remove", "empty"}, weight=3),
    Recipe("install", "install a program", "plan", [{"install"}], avoid={"installed", "update", "remove"},
           slot="package_new", verbs={"install"}, weight=2),
    Recipe("remove", "remove a program", "plan", [{"remove"}], avoid={"file", "trash", "cache", "update",
                                                                       "install"},
           slot="package_installed", verbs={"remove"}, weight=2),
    Recipe("stop_program", "close a program", "plan", [{"stop"}], avoid={"boot", "service", "start", "from"},
           slot="process", verbs={"stop"}, weight=1),
    Recipe("restart_service", "restart a service", "plan", [{"restart"}, {"service"}], slot="service",
           verbs={"restart"}, avoid={"computer", "pc", "laptop", "system", "machine"}, weight=2),
    Recipe("stop_service", "stop a service", "plan", [{"stop"}, {"service"}], slot="service",
           verbs={"stop"}, avoid={"boot"}, weight=2),
    Recipe("start_service", "start a service", "plan", [{"start"}, {"service"}], slot="service",
           verbs={"start"}, avoid={"boot", "stop"}, weight=2),
    Recipe("disable_autostart", "stop something starting automatically", "plan",
           [{"stop", "disable", "prevent"}, {"boot", "automatically"}], slot="service",
           verbs={"stop", "disable", "prevent"}, weight=3),
    Recipe("empty_trash", "permanently empty the desktop Trash", "plan", [{"trash"}, {"empty", "clear", "clean"}],
           verbs={"empty", "clear", "clean"}, weight=3),
]
BY_ID = {r.id: r for r in R}
CHANGE_VERBS = {"install", "remove", "update", "restart", "stop", "start", "disable", "empty", "clean",
                "clear", "kill", "delete", "uninstall", "upgrade", "enable", "format", "reboot", "shutdown"}
STOPWORDS = {"the", "a", "an", "my", "this", "that", "please", "app", "program", "package", "software",
             "service", "for", "me", "it", "now", "from", "on", "computer", "pc", "system", "is", "running",
             "up", "and", "to", "of", "all", "service.", "named", "called", "do", "i", "have", "installed",
             "both"}


@dataclass
class Match:
    status: str                  # matched | ambiguous | need_slot | escalate | refuse
    recipe: str = None
    slot: str = None
    candidates: list = field(default_factory=list)
    reason: str = ""


def _hits(need, toks, text):
    for group in need:
        if not any((g in toks) or (" " in g and g in text) for g in group):
            return False
    return True


def _negated(norm, verbs):
    words = norm.split()
    for i, w in enumerate(words):
        cw = SYN.get(w, w)
        if cw in verbs or w in verbs:
            window = words[max(0, i - 4):i]
            if any(n in window for n in ("not", "never", "no", "without", "cannot", "avoid")):
                return True
    return False


def slot_candidates(norm, recipe):
    """Words after the action verb, minus filler; the system check decides."""
    words = norm.split()
    vset = {v for v in recipe.verbs} | {k for k, v in SYN.items() if v in recipe.verbs}
    if recipe.slot == "package_any":
        m = re.search(r"\b(?:is|are|do i have|have i got|got)\s+(.+?)\s+(?:installed|on|there)\b", norm)
        if m:
            words = m.group(1).split()
            return [w for w in words if w not in STOPWORDS]
    if recipe.slot == "service" and recipe.id == "service_status":
        m = re.search(r"\bis\s+(?:the\s+)?(.+?)\s+(?:service\s+)?(?:running|active|working|up)\b", norm)
        if m:
            return [w for w in m.group(1).split() if w not in STOPWORDS]
    idx = None
    for i, w in enumerate(words):
        if w in vset or SYN.get(w) in recipe.verbs:
            idx = i
            break
    rest = words[idx + 1:] if idx is not None else words
    if recipe.id == "disable_autostart":
        rest = [w for w in rest if w not in ("starting", "automatically", "at", "boot", "login", "startup",
                                             "start", "running", "launching")]
    return [w for w in rest if w not in STOPWORDS and SYN.get(w) not in ("app", "service", "boot")]


def match(text):
    if not isinstance(text, str) or not text.strip():
        return Match("refuse", reason="empty request")
    if len(text) > 500:
        return Match("escalate", reason="long requests go to the main assistant")
    norm = normalise(text)
    core = POLITE.sub("", norm)
    toks = canon_tokens(core)
    tokset = set(toks)
    advice = bool(ADVICE.match(core))
    question = bool(QUESTION.match(core))
    change_words = [SYN.get(w, w) for w in core.split() if SYN.get(w, w) in CHANGE_VERBS or w in CHANGE_VERBS]
    multi = len(set(change_words)) > 1 and re.search(r"\b(and|then|also|plus|after|or)\b", core)
    if any(_negated(core, {v}) for v in CHANGE_VERBS | {"remove", "delete", "uninstall"}):
        return Match("escalate", reason="it says NOT to do something; I never act on that automatically")
    if multi:
        return Match("escalate", reason="several actions in one request; the main assistant plans those")
    cands = []
    asked_change = False
    for r in R:
        if not _hits(r.need, tokset, core):
            continue
        if r.avoid & tokset:
            continue
        if r.pattern and not re.search(r.pattern, core):
            continue
        if r.kind == "plan" and (question or advice):
            asked_change = True
            continue                      # questions never trigger changes
        cands.append(r)
    change = [r for r in cands if r.kind == "plan"]
    if advice and asked_change and not cands:
        return Match("escalate", reason="it asks for advice; I never act on that automatically")
    if change and (advice or any(_negated(core, r.verbs | {"remove", "delete", "uninstall"}) for r in change)):
        reason = "it asks for advice" if advice else "it says NOT to do something"
        return Match("escalate", reason=f"{reason}; I never act on that automatically",
                     candidates=[r.id for r in change])
    if not cands:
        if asked_change:
            return Match("escalate", reason="it is a question about a change; I only answer those with facts "
                                            "or pass them to the main assistant")
        return Match("escalate", reason="this is not one of the tasks I can do on my own")
    cands.sort(key=lambda r: (-(r.weight + sum(1 for g in r.need if any(x in tokset for x in g))), r.id))
    top = cands[0]
    if len(cands) > 1:
        s0 = top.weight + len(top.need)
        s1 = cands[1].weight + len(cands[1].need)
        if s1 >= s0 and (top.kind == "plan" or cands[1].kind == "plan"):
            return Match("ambiguous", candidates=[r.id for r in cands[:4]], reason="more than one task fits")
        if s1 >= s0 and top.kind != "plan" and cands[1].kind != "plan":
            pass   # two read-only looks: harmless, take the first (stable order)
    if top.slot:
        words = slot_candidates(core, top)
        if "or" in words or "either" in words:
            return Match("need_slot", recipe=top.id, reason="it names alternatives ('or'); which one exactly?")
        if not words:
            return Match("need_slot", recipe=top.id, reason=f"which {top.slot.split('_')[0]}?")
        return Match("matched", recipe=top.id, slot=" ".join(words[:3]), candidates=[r.id for r in cands[:4]])
    return Match("matched", recipe=top.id, candidates=[r.id for r in cands[:4]])


def closest(name, names, n=3):
    return difflib.get_close_matches(name, list(names), n=n, cutoff=0.6)
