"""Getting your OK for a plan's changes. Every failure path means NO.

desktop   Steward's own dialog (zenity) listing every step in full. You must
          tick "I approve exactly these changes" and press Approve.
          Decline, closing the window, timeout, no graphical session, zenity
          missing or crashing: all refuse.
chat      ZeroClaw's own approval prompt on your chat channel (Telegram, CLI,
          ...), for the steward__approve call that carries Steward's exact
          wording. Steward checks ZeroClaw's config forces that prompt
          (always_ask) and that the wording was not altered.
both      chat first, then the desktop dialog.
terminal  you run `steward approve PLAN` in a terminal.
"""
import os
import shutil
import subprocess
import tempfile
import time

from . import execute


def _gui_env():
    env = execute.clean_env()
    if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
        return env, None
    # A ZeroClaw daemon started as a systemd user service may lack the display
    # variables; the desktop session normally exports them to the user manager.
    try:
        p = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                           text=True, timeout=10, env=env)
        for line in p.stdout.splitlines():
            k, _, v = line.partition("=")
            if k in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                     "XAUTHORITY") and v:
                env.setdefault(k, v)
    except Exception:
        pass
    if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
        return env, None
    return None, "no graphical session found (no WAYLAND_DISPLAY/DISPLAY)"


def desktop(title, text, timeout):
    """Returns (decision, detail). decision: approved|declined|timeout|unavailable|error."""
    z = shutil.which("zenity")
    if not z:
        return "unavailable", "zenity is not installed (sudo dnf install zenity)"
    env, why = _gui_env()
    if env is None:
        return "unavailable", why
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(text)
        fname = f.name
    try:
        p = subprocess.Popen([z, "--text-info", f"--title={title}", f"--filename={fname}",
                              "--checkbox=I approve exactly these changes",
                              "--ok-label=Approve", "--cancel-label=Decline",
                              "--width=780", "--height=560", f"--timeout={int(timeout)}"],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             start_new_session=True)
        try:
            _, err = p.communicate(timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            return "timeout", f"no answer within {timeout}s"
        code = p.returncode
    except OSError as e:
        return "error", f"could not start zenity: {e}"
    finally:
        try:
            os.unlink(fname)
        except OSError:
            pass
    if code == 0:
        return "approved", "approved in the desktop dialog"
    if code == 1:
        return "declined", "declined (or window closed) in the desktop dialog"
    if code == 5:
        return "timeout", f"no answer within {timeout}s"
    msg = (err or b"").decode(errors="replace").strip().splitlines()
    return "error", f"zenity failed (exit {code}): {msg[-1][:200] if msg else 'no message'}"


def inline(title, text, timeout):
    """Approval in the terminal the user is typing in (used by `ask`).
    No terminal, no answer in time, or anything but 'yes' means no."""
    import select
    try:
        tty = open("/dev/tty", "r+")
    except OSError:
        return "unavailable", "no terminal to ask in"
    with tty:
        tty.write(f"\n===== {title} =====\n{text}\n\nType 'yes' to approve (anything else declines): ")
        tty.flush()
        r, _, _ = select.select([tty], [], [], timeout)
        if not r:
            tty.write("\n(no answer in time: declined)\n")
            return "timeout", f"no answer within {timeout}s"
        ans = tty.readline().strip().lower()
    return ("approved", "approved in the terminal") if ans == "yes" else ("declined", "declined in the terminal")
