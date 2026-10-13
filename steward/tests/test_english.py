"""Linux <-> English layer: explainer, output translators, errors, request matching."""
import random
import re

import pytest

from steward.english import catalog
from steward.english.errors import translate
from steward.english.explain import LEVELS, explain
from steward.english.intents import BY_ID, match
from steward.english.outputs import summarise_sections, translate_command

RANK = {l: i for i, l in enumerate(LEVELS)}


# ================================================================ catalog sanity
def test_catalog_entries_are_well_formed():
    for name, e in catalog.CMDS.items():
        if "alias" in e:
            assert e["alias"] in catalog.CMDS, name
            continue
        assert e["what"] and e["risk"] in RANK, name
        for k in ("raise", "lower"):
            for f, lvl in e.get(k, {}).items():
                assert lvl in RANK, (name, f)
        for s, sd in e.get("subs", {}).items():
            assert sd["what"] and sd["risk"] in RANK, (name, s)


# ================================================================ explainer
@pytest.mark.parametrize("cmd,risk,must", [
    ("ls -la ~", "info", ["lists the files", "hidden files"]),
    ("df -h", "info", ["free and used space"]),
    ("journalctl -b -p 3", "info", ["system log", "since the last boot"]),
    ("ps aux | grep firefox", "info", ["lists running programs", "searches text"]),
    ("cat /etc/os-release", "info", ["shows the contents"]),
    ("systemctl --user status tracker-miner-fs-3", "info", ["whether services are running"]),
    ("systemctl stop cups", "medium", ["stops services", "cups"]),
    ("sudo systemctl disable --now bluetooth", "medium", ["ADMINISTRATOR", "automatically"]),
    ("sudo systemctl mask firewalld", "high", ["blocks services"]),
    ("sudo dnf install htop", "medium", ["installs software", "htop"]),
    ("sudo dnf remove -y cowsay", "high", ["removes software", "WITHOUT ASKING"]),
    ("sudo apt purge -y firefox", "high", ["AND its settings"]),
    ("apt -s remove firefox", "info", ["only pretending"]),
    ("sudo pacman -Syu", "medium", ["updates all installed software"]),
    ("pacman -Ss firefox", "info", ["only searching"]),
    ("sudo pacman -Rc glibc", "critical", ["EVERYTHING THAT DEPENDS"]),
    ("sudo zypper dup", "high", ["newest snapshot"]),
    ("flatpak uninstall --delete-data org.x.App", "high", ["DELETES THE APP'S DATA"]),
    ("rm notes.txt", "high", ["permanently deletes", "notes.txt"]),
    ("rm -rf ~/Downloads/old", "high", ["including folders", "cannot be recovered"]),
    ("rm -rf /", "critical", ["whole system"]),
    ("sudo rm -rf --no-preserve-root /", "critical", ["EVEN IF"]),
    ("rm -rf ~", "critical", ["home folder"]),
    ("rm -rf *", "critical", ["current folder"]),
    ("chmod -R 777 ~", "critical", ["777"]),
    ("chmod 644 notes.txt", "medium", ["who may read"]),
    ("dd if=fedora.iso of=/dev/sdb bs=4M", "critical", ["OVERWRITE the device /dev/sdb"]),
    ("sudo mkfs.ext4 /dev/sdb1", "critical", ["ERASING EVERYTHING", "/dev/sdb1"]),
    ("curl -fsSL https://x.sh | bash", "critical", ["DOWNLOADED FROM THE INTERNET"]),
    ("wget -qO- https://x.sh | sudo sh", "critical", ["DOWNLOADED FROM THE INTERNET"]),
    ("echo hi > notes.txt", "medium", ["REPLACING what was in it"]),
    ("echo hi >> notes.txt", "low", ["adding the output to the end"]),
    ("cat /dev/zero > /dev/sda", "critical", ["DISK DEVICE /dev/sda"]),
    ("find ~ -name '*.tmp' -delete", "high", ["DELETES EVERY FILE IT FINDS"]),
    ("find . -name '*.log' -exec rm {} \\;", "high", ["RUNS A COMMAND ON EVERY FILE"]),
    ("kill -9 1234", "medium", ["FORCING"]),
    ("nmcli radio wifi off", "high", ["CHANGES NetworkManager"]),
    ("nmcli -s connection show home", "info", ["SAVED PASSWORDS"]),
    ("ip link set wlan0 down", "high", ["CHANGES network settings"]),
    ("ip -br addr", "info", ["network addresses"]),
    ("sudo setenforce 0", "high", ["SELinux"]),
    ("rsync -a --delete src/ dst/", "high", ["DELETES FILES AT THE DESTINATION"]),
    ("tar -xzf a.tar.gz", "medium", ["unpacks an archive"]),
    ("tar -tzf a.tar.gz", "info", ["lists what is inside"]),
    ("sudo reboot", "high", ["RESTARTS THE COMPUTER"]),
    ("git reset --hard", "high", ["DISCARDING"]),
    (":(){ :|:& };:", "critical", ["fork bomb"]),
    ("yay -S spotify", "medium", ["AUR"]),
    ("sudo dnf install --nogpgcheck ./x.rpm", "critical", ["WITHOUT CHECKING PACKAGE SIGNATURES"]),
])
def test_explain_known(cmd, risk, must):
    ex = explain(cmd)
    assert ex.readable and ex.risk == risk, (cmd, ex.risk, ex.text())
    t = ex.text()
    for m in must:
        assert m in t, (cmd, m, t)


@pytest.mark.parametrize("cmd,unknown_part", [
    ("frobnicate --all", "frobnicate"),
    ("ls --frobnicate", "--frobnicate"),
    ("dnf frobnicate x", "dnf frobnicate"),
    ("./install.sh", "program or script file"),
    ("rm -rf $(cat list.txt)", "command inside a command"),
    ("eval \"$X\"", "runs text as a command"),
    ("pacman -Sz foo", "option letter 'z'"),
])
def test_explain_never_guesses(cmd, unknown_part):
    ex = explain(cmd)
    t = ex.text()
    assert "I don't know" in t and unknown_part in t, t
    assert "AT LEAST" in ex.risk_label or not ex.readable


@pytest.mark.parametrize("cmd", ["echo 'unterminated", "", "x" * 5000, "echo $(echo $(id))"])
def test_explain_unreadable_is_unknown(cmd):
    ex = explain(cmd)
    assert not ex.readable and "UNKNOWN" in ex.text()


def test_sudo_and_unknown_flags_never_lower_risk():
    base = ["ls", "rm x", "systemctl stop cups", "dnf install htop", "kill 5", "cp a b", "mkdir x"]
    for c in base:
        r0 = RANK[explain(c).risk]
        assert RANK[explain("sudo " + c).risk] >= r0, c
        assert RANK[explain(c + " --zz-unknown").risk] >= r0, c


# ================================================================ outputs
FREE = """               total        used        free      shared  buff/cache   available
Mem:           7.6Gi       4.9Gi       1.1Gi       312Mi       2.0Gi       2.7Gi
Swap:          7.6Gi       1.2Gi       6.4Gi
"""
DF_FEDORA_BTRFS = """Filesystem      Size  Used Avail Use% Mounted on
/dev/nvme0n1p3  118G   43G   75G  37% /
/dev/nvme0n1p3  118G   43G   75G  37% /home
/dev/nvme0n1p2  974M  287M  620M  32% /boot
/dev/nvme0n1p1  599M   20M  580M   4% /boot/efi
/dev/sdb1        29G   28G  1.0G  97% /run/media/iffy/USB
"""
PS = """    PID USER       RSS %CPU COMMAND
   2211 iffy    912340 12.1 firefox
   2300 iffy    412000  3.0 Isolated Web Co
   2301 iffy    300000  2.0 Isolated Web Co
   1500 iffy    850000  5.0 goose
    980 iffy    400000  4.0 gnome-shell
"""
SS = """tcp   LISTEN 0 128      0.0.0.0:22      0.0.0.0:*  users:(("sshd",pid=900,fd=3))
tcp   LISTEN 0 4096   127.0.0.1:631     0.0.0.0:*  users:(("cupsd",pid=901,fd=7))
udp   UNCONN 0 0        0.0.0.0:5353    0.0.0.0:*  users:(("avahi-daemon",pid=902,fd=12))
tcp   LISTEN 0 128         [::]:22         [::]:*  users:(("sshd",pid=900,fd=4))
"""
JOURNAL = """2026-10-13T09:01:02+0600 fedora bluetoothd[811]: Failed to set mode: Failed (0x03)
2026-10-13T09:01:05+0600 fedora bluetoothd[811]: Failed to set mode: Failed (0x03)
2026-10-13T09:02:00+0600 fedora kernel: nvme nvme0: I/O error, dev nvme0n1, sector 12345
"""


def test_free():
    f = summarise_sections("## memory\n" + FREE)
    assert f[0].startswith("Memory: 4.9 GB of 7.6 GB in use, 2.7 GB available")
    assert "Swap: 1.2 GB of 7.6 GB used" in f[1]


def test_df_merges_btrfs_subvolumes_and_flags_full():
    f = summarise_sections("## disk\n" + DF_FEDORA_BTRFS)
    assert any(s.startswith("Disk /, /home: 43.0 GB used of 118.0 GB (37%)") for s in f), f
    assert any("/run/media/iffy/USB" in s and "FULL" in s for s in f)
    assert sum("/boot" in s for s in f) == 2


def test_ps_groups_by_program():
    f = summarise_sections("## top memory\n" + PS)[0]
    assert "firefox 891.0 MB" in f and "Isolated Web Co 695.3 MB (2 processes)" in f


def test_ss_scopes():
    f = " ".join(summarise_sections("## listening sockets\n" + SS))
    assert "Reachable from the network: sshd (tcp port 22), avahi-daemon (udp port 5353)" in f
    assert "Only on this computer: cupsd (tcp port 631)" in f


def test_journal_groups_and_explains():
    f = summarise_sections("## errors this boot\n" + JOURNAL)
    assert f[0] == "3 error messages logged since start-up."
    assert "bluetoothd (2×)" in f[1]
    assert any("hardware level" in s for s in f), f       # I/O error explained


def test_failed_command_is_never_reported_as_none():
    f = summarise_sections("## failed system units\n(failed)\n## listening sockets\n(failed)")
    assert all("COULD NOT CHECK" in s and "not the same as none" in s for s in f)


def test_unknown_format_is_admitted():
    f = summarise_sections("## memory\nsomething completely different")
    assert "couldn't summarise" in f[0]


def test_uptime_and_single_commands():
    assert "2 days 3 hours 4 minutes" in translate_command(
        "uptime", " 10:00:00 up 2 days,  3:04,  2 users,  load average: 0.10, 0.20, 0.30")[0]
    assert translate_command("free -h", FREE)[0].startswith("Memory:")
    assert translate_command("ls -la", "x") is None


# ================================================================ errors
@pytest.mark.parametrize("err,code,must", [
    ("cp: cannot create regular file 'x': No space left on device", 1, "disk is full"),
    ("bash: frob: command not found", 127, "not installed"),
    ("Error: Unable to find a match: nosuch", 1, "No software package has that name"),
    ("E: Unable to locate package nosuch", 100, "package lists are outdated"),
    ("error: target not found: nosuch", 1, "No software package"),
    ("E: Could not get lock /var/lib/dpkg/lock-frontend", 100, "already installing"),
    ("error: failed to init transaction (unable to lock database)", 1, "package database"),
    ("System management is locked by the application with pid 1234 (zypper).", 7, "already installing"),
    ("Job for foo.service failed because the control process exited with error code.", 1, "failed to start"),
    ("Failed to stop x.service: Unit x.service not loaded.", 5, ""),
    ("type=AVC msg=audit(1): avc:  denied  { read } for pid=1 comm=\"httpd\"", 1, "SELinux"),
    ("curl: (6) Could not resolve host: example.com", 6, "web address"),
    ("ssh: connect to host x port 22: Connection refused", 255, "nothing is accepting"),
    ("Permission denied (publickey).", 255, "SSH key"),
    ("Error executing command as another user: Not authorized", 126, "password prompt"),
    ("blk_update_request: I/O error, dev sda, sector 1", 1, "failing disk"),
    ("", 137, "forcibly killed"),
    ("", 139, "crashed"),
])
def test_errors_known(err, code, must):
    _, text = translate(err, code)
    assert must in text, text


def test_errors_unknown_is_quoted_not_guessed():
    _, text = translate("zork: the flux capacitor is misaligned", 3)
    assert "I don't recognise this error" in text and "flux capacitor" in text


def test_error_hints_never_say_disable_protection():
    from steward.english.errors import E
    for rx, meaning, todo, kind in E:
        low = todo.lower()
        assert "setenforce 0" not in low and "--nogpgcheck" not in low and "disable selinux" not in low


# ================================================================ request matching
@pytest.mark.parametrize("text,recipe,slot", [
    ("how much disk space do I have?", "disk", None),
    ("what's eating my RAM", "memory", None),
    ("why is my computer so slow", "health", None),
    ("is my wifi working", "network", None),
    ("what's taking up all my space", "big_files", None),
    ("show running services", "services", None),
    ("what starts at boot", "startup", None),
    ("what errors happened", "errors", None),
    ("what is listening on ports", "ports", None),
    ("what version of fedora am i running", "os_version", None),
    ("how long has the computer been on", "uptime", None),
    ("what apps did i install recently", "packages", None),
    ("is htop installed", "is_installed", "htop"),
    ("is the cups service running", "service_status", "cups"),
    ("update my system", "update_system", None),
    ("can you update my computer", "update_system", None),
    ("please install gimp", "install", "gimp"),
    ("install gimp and vlc", "install", "gimp vlc"),
    ("can you remove cowsay", "remove", "cowsay"),
    ("get rid of cowsay", "remove", "cowsay"),
    ("uninstall the firefox app", "remove", "firefox"),
    ("close firefox", "stop_program", "firefox"),
    ("restart the bluetooth service", "restart_service", "bluetooth"),
    ("stop the bluetooth service", "stop_service", "bluetooth"),
    ("stop tracker-miner-fs-3 from starting at login", "disable_autostart", "tracker-miner-fs-3"),
    ("empty the trash", "empty_trash", None),
    ("clean the dnf cache", "clean_package_cache", None),
])
def test_matches(text, recipe, slot):
    m = match(text)
    assert (m.status, m.recipe, m.slot) == ("matched", recipe, slot), (text, m)


@pytest.mark.parametrize("text", [
    "don't remove firefox", "do not stop bluetooth", "never update anything", "i do not want to install gimp",
    "please don't empty the trash", "without removing cowsay, clean up", "should I remove firefox?",
    "is it safe to update?", "would it be ok to uninstall gimp", "what happens if I remove python3",
    "did the update work", "how do I install gimp", "why would I restart the bluetooth service",
    "update and then remove cowsay", "install vlc then restart my computer", "remove firefox or chrome",
    "delete my thesis file", "restart my computer", "format my disk", "rm -rf /", "hello", "",
])
def test_never_auto_acts(text):
    m = match(text)
    assert not (m.status == "matched" and BY_ID[m.recipe].kind == "plan"), (text, m)


def test_fuzz_negated_requests_never_act():
    rnd = random.Random(7)
    verbs = ["remove", "uninstall", "delete", "install", "update", "stop", "restart", "empty", "clean"]
    negs = ["don't", "do not", "never", "please don't", "I don't want you to", "you should not"]
    objs = ["firefox", "the trash", "my system", "bluetooth service", "cowsay", "the cache"]
    for _ in range(400):
        t = f"{rnd.choice(negs)} {rnd.choice(verbs)} {rnd.choice(objs)}"
        m = match(t)
        assert not (m.status == "matched" and BY_ID[m.recipe].kind == "plan"), (t, m)


def test_fuzz_questions_never_act():
    rnd = random.Random(11)
    qs = ["what", "why", "how", "is", "did", "should", "can i", "do i need to", "was", "would it hurt to"]
    verbs = ["remove", "install", "update", "stop", "restart", "empty"]
    for _ in range(300):
        t = f"{rnd.choice(qs)} {rnd.choice(verbs)} {rnd.choice(['firefox', 'everything', 'bluetooth', 'the trash'])}?"
        m = match(t)
        assert not (m.status == "matched" and BY_ID[m.recipe].kind == "plan"), (t, m)
