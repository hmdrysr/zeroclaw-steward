"""Which Linux is this? Read from /etc/os-release (the standard file every
modern distribution ships). Unknown distributions are reported as unknown;
nothing is guessed."""
import os
import shutil
from dataclasses import dataclass

FAMILIES = {
    # family: (package tool, installed-query, words used in English)
    "fedora": "dnf", "rhel": "dnf", "centos": "dnf", "rocky": "dnf", "almalinux": "dnf",
    "debian": "apt", "ubuntu": "apt", "linuxmint": "apt", "pop": "apt", "elementary": "apt",
    "arch": "pacman", "manjaro": "pacman", "endeavouros": "pacman", "cachyos": "pacman",
    "opensuse": "zypper", "opensuse-tumbleweed": "zypper", "opensuse-leap": "zypper",
    "suse": "zypper", "sles": "zypper",
}


@dataclass
class Distro:
    id: str
    name: str
    version: str
    like: list
    pkg: str            # dnf | apt | pacman | zypper | unknown

    @property
    def known(self):
        return self.pkg != "unknown"


def parse_os_release(text):
    d = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        d[k.strip()] = v
    return d


def detect(path=None):
    path = path or os.environ.get("STEWARD_OS_RELEASE") or \
        ("/etc/os-release" if os.path.exists("/etc/os-release") else "/usr/lib/os-release")
    try:
        d = parse_os_release(open(path).read())
    except OSError:
        d = {}
    did = d.get("ID", "unknown").lower()
    like = d.get("ID_LIKE", "").lower().split()
    pkg = "unknown"
    for cand in [did] + like:
        if cand in FAMILIES:
            pkg = FAMILIES[cand]
            break
    if pkg == "unknown":
        for cand in [did] + like:
            if cand.startswith("opensuse"):
                pkg = "zypper"
    # Trust the file, but confirm the tool really exists.
    if pkg != "unknown" and not shutil.which(pkg if pkg != "apt" else "apt-get"):
        if not os.environ.get("STEWARD_TEST_HOME"):
            pkg = "unknown"
    return Distro(did, d.get("PRETTY_NAME") or d.get("NAME") or did, d.get("VERSION_ID", ""), like, pkg)
