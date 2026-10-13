"""How much smaller are the plain-English looks than the raw output, on THIS machine?
Tokens are estimated as characters / 4 (a common rule of thumb for English and code).
usage: python tests/measure_tokens.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from steward.engine import Engine
from steward.readonly import presets
from steward.store import Store

e = Engine(store=Store(Path(tempfile.mkdtemp()) / "m.db"))
e.policy.require_zeroclaw_gate = False
tot_raw = tot_en = 0
print(f"{'preset':<11}{'raw tokens':>11}{'English':>9}{'saved':>8}")
for p in presets():
    en = e.look(preset=p)
    rid = en.split()[0]
    raw = e.output(rid, lines=500)
    r, n = len(raw) // 4, len(en) // 4
    tot_raw += r
    tot_en += n
    print(f"{p:<11}{r:>11}{n:>9}{(1 - n / max(r, 1)):>8.0%}")
print(f"{'total':<11}{tot_raw:>11}{tot_en:>9}{(1 - tot_en / max(tot_raw, 1)):>8.0%}")
