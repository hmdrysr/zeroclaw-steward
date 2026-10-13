"""Run a command in a pseudo-terminal and answer prompts like a person would.
usage: pty_answer.py LOG 'regex=>answer' ... -- command args"""
import os, pty, re, select, sys, time
log = sys.argv[1]
i = sys.argv.index("--")
rules = [tuple(r.split("=>", 1)) for r in sys.argv[2:i]]
cmd = sys.argv[i + 1:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)
buf, used = "", set()
with open(log, "w") as f:
    t0 = time.time()
    while time.time() - t0 < 1800:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                d = os.read(fd, 4096).decode(errors="replace")
            except OSError:
                break
            if not d:
                break
            f.write(d); f.flush(); buf += d
        for k, (pat, ans) in enumerate(rules):
            if k not in used and re.search(pat, buf[-600:]):
                time.sleep(0.4)
                os.write(fd, (ans + "\r").encode()); used.add(k); buf = ""
                break
_, st = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(st))
