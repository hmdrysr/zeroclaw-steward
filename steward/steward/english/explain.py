"""Explain a shell command in plain English, with a risk level, BEFORE it runs.

Fail-safe rules:
  - Only curated knowledge is used (catalog.py). An unknown command, sub-command
    or option is reported as unknown, never guessed.
  - If anything is unknown, the risk is shown as "at least X" and the unknown
    parts are listed.
  - Commands inside commands ($(...), `...`), eval and scripts are never
    "understood": they are flagged and the risk is raised.
"""
import os
import re
import shlex
from dataclasses import dataclass, field

from .catalog import CMDS, PACMAN_MODS, PACMAN_OPS, R

LEVELS = ["info", "low", "medium", "high", "critical"]
WORDS = {"info": "NONE (it only reads or shows things)", "low": "LOW", "medium": "MEDIUM",
         "high": "HIGH", "critical": "CRITICAL"}


def _max(a, b):
    return a if R[a] >= R[b] else b


@dataclass
class Explanation:
    command: str
    parts: list = field(default_factory=list)       # one sentence per simple command
    risk: str = "info"
    unknown: list = field(default_factory=list)     # things we could not explain
    warnings: list = field(default_factory=list)    # important notes
    admin: bool = False
    readable: bool = True

    @property
    def risk_label(self):
        if not self.readable:
            return "UNKNOWN"
        base = WORDS[self.risk]
        if self.unknown:
            return f"AT LEAST {base.split(' ')[0]} (some parts are unknown, so it could be higher)"
        return base

    def text(self):
        if not self.readable:
            return (f"I can't read this command: {self.parts[0] if self.parts else 'unknown error'}.\n"
                    "Risk: UNKNOWN. Don't run it until you know what it does.")
        out = ["In plain English:"] + [f"  {i}. {p}" if len(self.parts) > 1 else f"  {p}"
                                       for i, p in enumerate(self.parts, 1)]
        out.append(f"Risk: {self.risk_label}")
        for w in dict.fromkeys(self.warnings):
            out.append(f"Watch out: {w}")
        for u in dict.fromkeys(self.unknown):
            out.append(f"I don't know: {u}")
        return "\n".join(out)


FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
SUBST = re.compile(r"\$\([^()]*\)|`[^`]*`")
SHELLS = {"bash", "sh", "zsh", "dash", "fish", "python", "python3", "perl", "ruby"}
FETCHERS = {"curl", "wget"}


def explain(command):
    ex = Explanation(command)
    if not isinstance(command, str) or not command.strip():
        ex.readable = False
        ex.parts = ["it is empty"]
        return ex
    if len(command) > 4000:
        ex.readable = False
        ex.parts = ["it is too long to check reliably"]
        return ex
    if FORK_BOMB.search(command):
        ex.parts = ["This is a 'fork bomb': it copies itself endlessly until the computer freezes."]
        ex.risk = "critical"
        ex.warnings.append("Running it will lock up the computer until it is restarted.")
        return ex
    text = command
    if SUBST.search(text):
        ex.unknown.append("it contains a command inside a command ($(...) or `...`); that inner "
                          "command runs first and I don't analyse what it produces")
        ex.risk = _max(ex.risk, "medium")
        text = SUBST.sub(" COMMAND_OUTPUT ", text)
    if "$(" in text or "`" in text:
        ex.readable = False
        ex.parts = ["it has nested commands-inside-commands"]
        return ex
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError as e:
        ex.readable = False
        ex.parts = [f"it is malformed ({e}, e.g. a missing closing quote)"]
        return ex
    if any(t in ("(", ")", "{", "}") for t in tokens):
        ex.unknown.append("it groups commands with brackets; I explain the parts but not the grouping")
    segments = []
    cur, joiner = [], None
    for t in tokens:
        if t in ("|", "||", "&&", ";", "&", "|&"):
            segments.append((joiner, cur))
            cur, joiner = [], t
        elif t in ("(", ")"):
            continue
        else:
            cur.append(t)
    segments.append((joiner, cur))
    intro = {"|": "Then it passes that output to a command that ", "|&": "Then it passes that output to a command that ",
             "&&": "Then, only if that worked, it ", "||": "Then, only if that failed, it ",
             ";": "Then it ", "&": "Then it ", None: "It "}
    prev_cmd = None
    for i, (j, seg) in enumerate(segments):
        if not seg:
            continue
        sentence, risk, name = _simple(seg, ex)
        if j == "&" or (i + 1 < len(segments) and segments[i + 1][0] == "&"):
            sentence += " (running in the background)"
        if j in ("|", "|&") and prev_cmd in FETCHERS and name in SHELLS:
            ex.warnings.append("It RUNS A SCRIPT DOWNLOADED FROM THE INTERNET without you seeing it first.")
            risk = "critical"
        lead = intro[j]
        if sentence.startswith("\x00ADMIN\x00"):
            sentence = sentence[len("\x00ADMIN\x00"):]
            lead = lead.rstrip() + ", as ADMINISTRATOR (root), "
        ex.parts.append(lead + sentence.replace("\x00ADMIN\x00", "as administrator, ") + ".")
        ex.risk = _max(ex.risk, risk)
        prev_cmd = name
    if ex.parts:
        ex.parts[0] = ex.parts[0][0].upper() + ex.parts[0][1:]
    return ex


def _redirections(seg, ex):
    """Pull redirections out of a token list. Returns (tokens, phrases, risk)."""
    out, phrases, risk = [], [], "info"
    i = 0
    while i < len(seg):
        t = seg[i]
        fd = None
        if t.isdigit() and i + 1 < len(seg) and seg[i + 1] in (">", ">>", ">&", "<", "&>"):
            fd, i = t, i + 1
            t = seg[i]
        if t in (">", ">>", "&>", ">&", "<", ">|"):
            target = seg[i + 1] if i + 1 < len(seg) else "?"
            i += 2
            if t == ">&" and target.isdigit():
                if fd == "2" and target == "1":
                    phrases.append("including error messages in the output")
                continue
            if t == "<":
                phrases.append(f"reading its input from {target}")
                continue
            if target == "/dev/null":
                phrases.append("throwing " + ("error messages" if fd == "2" else "the output") + " away")
                continue
            if re.match(r"^/dev/(sd|nvme|vd|hd|mmcblk|dm-|mapper/)", target):
                phrases.append(f"WRITING DIRECTLY ONTO THE DISK DEVICE {target}")
                ex.warnings.append(f"Writing to {target} destroys the data on that disk.")
                risk = _max(risk, "critical")
                continue
            if t == ">>":
                phrases.append(f"adding {'error messages' if fd == '2' else 'the output'} to the end of {target}")
                risk = _max(risk, "low")
            else:
                phrases.append(f"writing {'error messages' if fd == '2' else 'the output'} into {target}, "
                               "REPLACING what was in it")
                risk = _max(risk, "medium")
            continue
        out.append(t)
        i += 1
    return out, phrases, risk


def _join_args(args, n=6):
    args = ["(the output of another command)" if a == "COMMAND_OUTPUT" else a for a in args]
    shown = [a if len(a) <= 60 else a[:57] + "..." for a in args[:n]]
    more = f" and {len(args) - n} more" if len(args) > n else ""
    return ", ".join(shown) + more


DANGER_TARGETS = re.compile(r"^(/|/\*|/(bin|boot|dev|etc|lib|lib64|proc|sbin|sys|usr|var)(/\*?)?|~/?|~/\*|\$HOME/?|\*|\.\*?)$")


def _simple(seg, ex, depth=0):
    """Explain one simple command. Returns (sentence, risk, command name)."""
    seg, red_phr, red_risk = _redirections(seg, ex)
    assigns = []
    while seg and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
        assigns.append(seg.pop(0).split("=", 1)[0])
    if not seg:
        s = "sets " + ", ".join(assigns) if assigns else "does nothing"
        return s, "low", None
    raw = seg[0]
    name = os.path.basename(raw)
    args = seg[1:]
    if name.startswith("mkfs."):
        name = "mkfs"
    if raw.startswith(("./", "../")) or (raw.startswith("/") and name not in CMDS):
        ex.unknown.append(f"'{raw}' is a program or script file; I can't know what it does")
        return f"runs the program file {raw}", "medium", name
    entry = CMDS.get(name)
    aur = False
    if entry and "alias" in entry:
        aur = entry.get("aur", False)
        name_alias = entry["alias"]
        entry = CMDS[name_alias]
        name = name_alias
    if entry is None:
        if name in ("eval", "exec"):
            ex.unknown.append(f"'{name}' runs text as a command; I can't know what that text does")
            return f"runs other text as a command ({name})", "high", name
        ex.unknown.append(f"the command '{name}' is not in my dictionary")
        return f"runs '{name}', a command I don't know", "low", name
    tail = ""
    if red_phr:
        tail = ", " + ", ".join(red_phr)
    if assigns:
        tail += f" (with setting{'s' if len(assigns) > 1 else ''} {', '.join(assigns)})"
    # ---- prefixes: sudo, env, nice, ...
    if entry.get("prefix"):
        i = 0
        takes = set(entry.get("takes", []))
        while i < len(args) and (args[i].startswith("-") or (name == "env" and "=" in args[i])):
            i += 2 if args[i] in takes else 1
        i += entry.get("positional_prefix", 0)
        inner = args[i:]
        if name in ("sudo", "pkexec", "doas", "run0"):
            ex.admin = True
            if not inner:
                return "opens an administrator (root) session" + tail, "high", name
            s, r, n = _simple(inner, ex, depth + 1)
            r = _max(r, "medium")            # anything done as administrator is at least medium
            return "\x00ADMIN\x00" + s + tail, _max(r, red_risk), n
        if not inner:
            return entry["what"] + tail, entry["risk"], name
        s, r, n = _simple(inner, ex, depth + 1)
        lead = {"env": "with changed settings, ", "nice": "at low priority, ", "nohup": "so it keeps running after the terminal closes, ",
                "time": "while timing it, ", "timeout": "with a time limit, ", "watch": "repeatedly, every few seconds, "}.get(name, "")
        return lead + s + tail, _max(r, red_risk), n
    if name == "xargs":
        inner = [a for a in args if not a.startswith("-")]
        if inner:
            s, r, n = _simple(inner, ex, depth + 1)
            return "for each item it receives, " + s + tail, _max(_max(r, "medium"), red_risk), n
        return entry["what"] + tail, "medium", name
    if entry.get("pacman"):
        s, r = _pacman(args, ex, aur)
        return s + tail, _max(r, red_risk), name
    # ---- subcommands
    flags = dict(entry.get("flags", {}))
    takes = set(entry.get("takes", []))
    raises = dict(entry.get("raise", {}))
    lowers = dict(entry.get("lower", {}))
    what, risk, note = entry["what"], entry["risk"], entry.get("note")
    positional = []
    i = 0
    sub = None
    subs = entry.get("subs")
    toks = list(args)
    # find subcommand: first non-flag token (skipping values of 'takes' flags)
    if subs:
        j = 0
        while j < len(toks):
            t = toks[j]
            if t.startswith("-"):
                j += 2 if (t in takes and "=" not in t) else 1
                continue
            sub = t
            toks.pop(j)
            break
        if sub is not None:
            if sub not in subs:
                ex.unknown.append(f"'{name} {sub}' is not in my dictionary")
                return f"runs '{name} {sub}', which I don't know{tail}", _max(risk, "medium"), name
            sd = subs[sub]
            what, risk = sd["what"], sd["risk"]
            note = sd.get("note", note)
            flags.update(sd.get("flags", {}))
            takes |= set(sd.get("takes", []))
            raises.update(sd.get("raise", {}))
    phrases, unknown_flags = [], []
    lowered = None
    while i < len(toks):
        t = toks[i]
        if t == "--":
            positional += toks[i + 1:]
            break
        if t.startswith("-") and len(t) > 1 and not re.fullmatch(r"-\d+(\.\d+)?", t) or \
                (name == "kill" and re.fullmatch(r"-\d+", t)):
            key = t.split("=", 1)[0]
            if key in flags or key in raises or key in lowers:
                if flags.get(key):
                    phrases.append(flags[key])
                if key in raises:
                    risk = _max(risk, raises[key])
                if key in lowers:
                    lowered = lowers[key]
                i += 2 if (key in takes and "=" not in t) else 1
                continue
            if not t.startswith("--") and len(t) > 2:
                letters = ["-" + c for c in t[1:]]
                if all(l in flags or l in raises for l in letters):
                    for l in letters:
                        if flags.get(l):
                            phrases.append(flags[l])
                        if l in raises:
                            risk = _max(risk, raises[l])
                    i += 1
                    continue
            if key in takes:
                i += 2
                continue
            unknown_flags.append(t)
            i += 1
            continue
        if name in ("head", "tail") and re.fullmatch(r"-\d+", t):
            phrases.append(f"({'first' if name == 'head' else 'last'} {t[1:]} lines)")
            i += 1
            continue
        if name == "ps" and t in ("aux", "-aux", "ax", "-ef"):
            phrases.append("(all of them, with details)")
            i += 1
            continue
        positional.append(t)
        i += 1
    if lowered and not any(k in raises for k in toks):
        risk = lowered
    for u in unknown_flags:
        ex.unknown.append(f"the option {u} of '{name}'")
    if entry.get("danger_words"):
        for p in positional:
            if p in entry["danger_words"]:
                risk = _max(risk, entry["danger_words"][p])
                what = entry["change_what"]
    if name == "tar":
        modes = "".join(t.lstrip("-") for t in toks if t.startswith("-") and not t.startswith("--"))
        if "x" in modes:
            what = "unpacks an archive (files with the same names are overwritten)"
        elif "c" in modes:
            what, risk = "packs files into an archive", "low"
        elif "t" in modes:
            what, risk = "lists what is inside an archive", "info"
        phrases = [p for p in phrases if not p.startswith(("unpacking", "creating", "only listing"))]
    # ---- dangerous targets
    if name in ("rm", "chmod", "chown", "chgrp", "shred") or (name == "find" and "-delete" in toks):
        recursive = any(f in toks for f in ("-r", "-R", "-rf", "-fr", "-Rf", "-fR", "--recursive")) or \
            any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", t) for t in toks)
        for p in positional:
            if DANGER_TARGETS.match(p) and (recursive or name in ("rm",) and p in ("*", ".*")):
                risk = "critical"
                ex.warnings.append(f"The target '{p}' is your whole system, your home folder or "
                                   "everything in the current folder.")
    if name == "chmod" and positional and re.fullmatch(r"0?777|a\+rwx|o\+w", positional[0]):
        ex.warnings.append("777 / o+w lets every user and program change these files.")
        risk = _max(risk, "high")
    if name == "dd":
        for p in args:
            if p.startswith("of="):
                target = p[3:]
                phrases.append(f"writing to {target}")
                if target.startswith("/dev/"):
                    risk = "critical"
                    ex.warnings.append(f"dd will OVERWRITE the device {target}; everything on it is lost.")
            elif p.startswith("if="):
                phrases.append(f"reading from {p[3:]}")
        positional = []
    if name == "mkfs" and positional:
        ex.warnings.append(f"Everything on {positional[-1]} will be erased.")
    if name in ("dnf", "apt", "zypper") and sub in ("remove", "erase", "rm", "purge", "autoremove") and \
            any(f in toks for f in ("-y", "--assumeyes", "--yes", "-n", "--non-interactive")):
        ex.warnings.append("It will not stop to show you what else gets removed.")
    if note:
        ex.warnings.append(note)
    sentence = what
    if positional:
        sentence += f": {_join_args(positional)}"
    real = [p for p in phrases if p]
    if real:
        sentence += " — " + ", ".join(real)
    return sentence + tail, _max(risk, red_risk), name


def _pacman(args, ex, aur=False):
    ops = [a for a in args if a.startswith("-") and not a.startswith("--")]
    longs = [a for a in args if a.startswith("--")]
    pos = [a for a in args if not a.startswith("-")]
    if not ops:
        ex.unknown.append("pacman without an operation flag")
        return "runs pacman (operation unclear)", "medium"
    op = ops[0][1:2]
    if op not in PACMAN_OPS:
        ex.unknown.append(f"pacman operation {ops[0]}")
        return "runs pacman (unknown operation)", "medium"
    what, risk = PACMAN_OPS[op]
    mods = "".join(o[1:] for o in ops)[1:] if ops else ""
    if op == "S" and "u" in mods and not pos:
        what = "updates all installed software"
    phrases = []
    for m in mods:
        if m == op:
            continue
        if op == "S" and m == "u" and not pos:
            continue
        ph = PACMAN_MODS.get(op, {}).get(m)
        if ph is None:
            ex.unknown.append(f"pacman option letter '{m}' with -{op}")
        else:
            phrases.append(ph)
    if op == "S" and any(m in mods for m in "si") and pos == [] or (op == "S" and ("s" in mods or "i" in mods)):
        risk = "info"
    if op == "S" and "c" in mods:
        risk = "low"
    if op == "S" and "u" in mods:
        risk = _max(risk, "medium")
    if op == "R" and "c" in mods:
        risk = "critical"
        ex.warnings.append("-Rc also removes every package that depends on these.")
    for l in longs:
        if l == "--noconfirm":
            phrases.append("WITHOUT ASKING FOR CONFIRMATION")
        elif l in ("--needed",):
            phrases.append("skipping what is already up to date")
        elif l in ("--overwrite",):
            phrases.append("OVERWRITING CONFLICTING FILES")
            risk = _max(risk, "high")
        else:
            ex.unknown.append(f"pacman option {l}")
    if "u" in mods and "y" in mods and op == "S" and pos:
        pass
    if op == "S" and "y" in mods and "u" not in mods and pos:
        ex.warnings.append("Refreshing package lists (-Sy) without upgrading (-u) and then installing "
                           "can leave the system half-updated (a 'partial upgrade').")
        risk = _max(risk, "high")
    if aur:
        ex.warnings.append("This helper also builds software from the AUR, which is not checked by Arch.")
        risk = _max(risk, "medium")
    s = what
    if pos:
        s += f": {_join_args(pos)}"
    if phrases:
        s += " — " + ", ".join(phrases)
    return s, risk
