"""Error messages and exit codes -> what went wrong and what to do.

Each entry: (regex, meaning, what to do, kind). Matching is on the real error
text. If nothing matches, the answer says so and quotes the original message;
nothing is guessed. Hints never tell the user to disable a protection.
"""
import re
from dataclasses import dataclass

E = []


def _e(pattern, meaning, todo, kind, flags=re.I):
    E.append((re.compile(pattern, flags), meaning, todo, kind))


# ---------------------------------------------------------------- files and permissions
_e(r"No space left on device", "The disk is full.",
   "Free up space (look at the disk preset), then try again.", "disk_full")
_e(r"Disk quota exceeded", "You have used all the space you are allowed.", "Delete or move some files.", "disk_full")
_e(r"Read-only file system", "That disk or folder can't be changed right now (it is mounted read-only).",
   "If this is unexpected, the disk may have an error; check the log for disk problems.", "readonly")
_e(r"Permission denied \(publickey", "The other computer refused your SSH key.",
   "Check that your public key is on that computer, and that you used the right user name.", "ssh_auth")
_e(r"Permission denied|EACCES", "You don't have permission for that file, folder or action.",
   "If it is a system file, it needs an admin step; if it is yours, check its permissions.", "permission")
_e(r"Operation not permitted|EPERM", "The system does not allow this action for your account (or at all).",
   "System-wide changes need administrator rights; some files are protected even from root.", "permission")
_e(r"No such file or directory|ENOENT|cannot stat|cannot access", "A file or folder it needed does not exist.",
   "Check the exact name and location (names are case-sensitive).", "not_found")
_e(r"Not a directory", "Something expected to be a folder is actually a file.", "Check the path.", "path")
_e(r"Is a directory", "Something expected to be a file is actually a folder.",
   "Check the path (folders need options like -r).", "path")
_e(r"File exists", "Something with that name already exists.", "Use another name or remove the old one first.", "exists")
_e(r"Directory not empty", "The folder still has files in it.", "Empty it first, or remove it with its contents.", "not_empty")
_e(r"Device or resource busy|target is busy", "It is in use right now (a program has it open or it is mounted).",
   "Close programs using it, then try again.", "busy")
_e(r"Text file busy", "The program file is running right now and can't be changed.", "Close the program first.", "busy")
_e(r"Input/output error|EIO|I/O error", "Reading or writing the disk failed at the hardware level.",
   "This can mean a failing disk or cable. Back up important files now and check the disk's health.", "io_error")
_e(r"Too many open files", "A program opened more files than allowed.", "Restart that program.", "limits")
_e(r"Argument list too long", "Too many file names were given at once.", "Do it in smaller groups.", "limits")
_e(r"Cannot allocate memory|Out of memory|oom-kill|Killed process", "The computer ran out of memory and a program was stopped.",
   "Close memory-hungry programs (see the memory preset).", "oom")
# ---------------------------------------------------------------- commands and admin
_e(r"command not found|No such file or directory.*: line \d+", "That command is not installed (or is misspelled).",
   "Check the spelling; if it is a real program, install the package that provides it.", "cmd_missing")
_e(r"is not in the sudoers file|not allowed to execute", "Your account is not allowed to use sudo.",
   "An administrator must add you to the 'wheel' (Fedora) or 'sudo' (Ubuntu) group.", "admin_denied")
_e(r"sudo: a password is required|sudo: \d+ incorrect password|incorrect password attempt",
   "The administrator password was missing or wrong.", "Try again with the right password.", "admin_password")
_e(r"Not authorized|Request dismissed|authentication failed|AUTHENTICATION FAILED|Error executing command as another user",
   "The administrator password prompt was cancelled or failed.", "Try again and enter your password when asked.", "admin_password")
_e(r"cannot open display|Cannot open display|could not connect to display|Failed to connect to the display",
   "A graphical program could not reach your screen.", "Run it from your desktop session, not over SSH or a service.", "display")
# ---------------------------------------------------------------- services
_e(r"Unit (\S+) not found|Unit (\S+) could not be found", "There is no service with that name.",
   "Check the exact name with the services preset.", "unit_missing")
_e(r"Job for (\S+) failed", "The service failed to start or stop.",
   "Its log explains why: look at the service's status or the error log.", "unit_failed")
_e(r"(\S+) is masked", "That service is blocked from starting (masked).", "It must be unmasked first, deliberately.", "unit_masked")
_e(r"Failed to connect to bus|Failed to connect to user scope bus", "It could not talk to systemd (no session bus).",
   "Run it inside your normal desktop session.", "bus")
_e(r"Interactive authentication required", "This needs administrator rights.", "Use an admin step (it will ask for your password).", "admin_needed")
# ---------------------------------------------------------------- SELinux (Fedora/RHEL)
_e(r"avc:\s+denied", "SELinux blocked a program from doing something.",
   "This is a security protection. Don't turn SELinux off; the SELinux troubleshooter (setroubleshoot) "
   "explains the safe fix.", "selinux")
# ---------------------------------------------------------------- networking
_e(r"Could not resolve host|Temporary failure in name resolution|Name or service not known|NXDOMAIN",
   "It could not look up the web address (no internet, or DNS is not working).",
   "Check you are online (network preset).", "dns")
_e(r"Network is unreachable|No route to host", "There is no network connection to that place.", "Check your connection.", "network")
_e(r"Connection refused", "The other side is reachable but nothing is accepting connections there.",
   "The service there may be stopped, or the port is wrong.", "refused")
_e(r"Connection timed out|timed out", "The other side did not answer in time.", "Check the connection or try later.", "timeout")
_e(r"Address already in use", "Another program is already using that network port.",
   "Find it with the ports preset, or use a different port.", "port_in_use")
_e(r"SSL certificate problem|certificate verify failed|CERTIFICATE_VERIFY_FAILED",
   "The website's security certificate could not be verified.",
   "Do not bypass this (no -k / --insecure). Check the date and time on this computer, or the site may be unsafe.", "tls")
# ---------------------------------------------------------------- packages: dnf / rpm
_e(r"No match for argument: (\S+)", "No software package has that name.", "Search for the right name first.", "pkg_missing")
_e(r"Nothing to do", "Nothing needed doing (already installed / up to date).", "No action needed.", "noop")
_e(r"Error: Unable to find a match", "No software package has that name.", "Search for the right name first.", "pkg_missing")
_e(r"(Failed to download metadata|Cannot download repomd|Curl error)", "It could not download the package lists.",
   "Check your internet connection; a software source may be down.", "repo")
_e(r"GPG check FAILED|public key.*is not installed|NO_PUBKEY|signature.*(invalid|bad)",
   "A package's signature could not be verified.",
   "Don't skip the check. The package source's signing key may be missing or the download corrupted.", "signature")
_e(r"Problem: .*conflicting requests|conflicts with|nothing provides|cannot install both",
   "The requested packages conflict with what is installed.", "Ask for help resolving it; don't force it.", "conflict")
_e(r"(waiting for process|another app is currently holding|rpmdb.*lock|Waiting for cache lock)",
   "Another program is already installing or updating software.", "Wait for it to finish, then try again.", "pkg_locked")
# ---------------------------------------------------------------- packages: apt / dpkg
_e(r"Unable to locate package (\S+)", "No software package has that name (or the package lists are outdated).",
   "Check the name; refresh the package lists (apt update) if needed.", "pkg_missing")
_e(r"Could not get lock|Unable to acquire the dpkg frontend lock|dpkg frontend lock",
   "Another program is already installing or updating software.", "Wait for it to finish, then try again.", "pkg_locked")
_e(r"dpkg was interrupted", "An earlier install was interrupted.", "Finish it with: sudo dpkg --configure -a", "pkg_interrupted")
_e(r"has no installation candidate", "That package exists but can't be installed from your sources.", "Check the name or sources.", "pkg_missing")
_e(r"held broken packages|Unmet dependencies", "Some packages' requirements can't be satisfied.", "Ask for help; don't force it.", "conflict")
# ---------------------------------------------------------------- packages: pacman
_e(r"target not found: (\S+)", "No software package has that name.", "Search for the right name first.", "pkg_missing")
_e(r"unable to lock database|db\.lck", "Another program is using the package database (or one crashed).",
   "Wait; if no update is running, the lock file may be left over from a crash.", "pkg_locked")
_e(r"could not satisfy dependencies|breaks dependency", "Removing or installing this would break other packages.",
   "List the dependent packages too, or keep it.", "conflict")
_e(r"invalid or corrupted package|signature from .* is (unknown|invalid)", "A downloaded package is damaged or not trusted.",
   "Update the archlinux-keyring package first, then retry.", "signature")
_e(r"exists in filesystem", "Files from the package would overwrite existing files.", "Find out why they exist; don't overwrite blindly.", "conflict")
# ---------------------------------------------------------------- packages: zypper
_e(r"System management is locked", "Another program is already installing or updating software.", "Wait, then retry.", "pkg_locked")
_e(r"No provider of '(\S+)' found|'(\S+)' not found in package names", "No software package has that name.", "Search for the right name.", "pkg_missing")
# ---------------------------------------------------------------- flatpak
_e(r"No remote refs found|Nothing matches (\S+)", "No Flatpak app with that name was found.", "Search for the exact app id.", "pkg_missing")
_e(r"(\S+) not installed", "That isn't installed.", "Nothing to remove.", "noop")

EXIT = {
    0: "It finished successfully.",
    1: "It reported a general failure.",
    2: "It was used the wrong way (bad options or arguments).",
    126: "The command exists but could not be run (not executable or blocked).",
    127: "The command was not found.",
    124: "It was stopped because it took too long (time limit).",
    130: "It was cancelled (Ctrl+C or a stop request).",
    137: "It was forcibly killed (often because memory ran out, or it was stopped by force).",
    139: "It crashed (segmentation fault: a bug in the program).",
    143: "It was asked to stop (terminated).",
}


@dataclass
class Finding:
    meaning: str
    todo: str
    kind: str
    evidence: str


def translate(stderr, exit_code=None, stdout=""):
    """Return (findings, summary_text). Findings are ordered by where they appear."""
    text = (stderr or "") + "\n" + (stdout or "")
    found, seen = [], set()
    for line in text.splitlines():
        for rx, meaning, todo, kind in E:
            if rx.search(line) and kind not in seen:
                seen.add(kind)
                found.append(Finding(meaning, todo, kind, line.strip()[:200]))
                break
    lines = []
    if exit_code is not None and exit_code in EXIT and exit_code != 0:
        lines.append(EXIT[exit_code])
    elif exit_code not in (None, 0):
        lines.append(f"It failed (exit code {exit_code}).")
    for f in found[:3]:
        lines.append(f"{f.meaning} What to do: {f.todo}")
    if exit_code not in (None, 0) and not found:
        last = [l for l in (stderr or "").splitlines() if l.strip()]
        if last:
            lines.append(f"I don't recognise this error. The program said: \"{last[-1].strip()[:200]}\"")
        else:
            lines.append("It gave no error message.")
    return found, " ".join(lines)
