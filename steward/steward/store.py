"""Durable records (SQLite, WAL, synchronous=FULL).

plans  one row per plan: goal, current steps and their states, approval state.
runs   one row per execution of anything (looks, steps, checks), with full
       redacted output. Written as 'running' before the operation starts, so a
       crash can never make an unfinished operation look finished.
events append-only audit trail. No code path deletes from it.
"""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans(
  id TEXT PRIMARY KEY, goal TEXT, created REAL, updated REAL, state TEXT,
  revision INTEGER DEFAULT 1, steps TEXT, approved TEXT DEFAULT '[]',
  approval TEXT, question TEXT, report TEXT, cancel INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, step_no INTEGER, kind TEXT,
  do TEXT, params TEXT, tier TEXT, fp TEXT, operation TEXT, started REAL, ended REAL,
  status TEXT, exit_code INTEGER, failure TEXT, changed TEXT, stdout TEXT, stderr TEXT,
  truncated INTEGER DEFAULT 0, verify TEXT, inverse TEXT, approval TEXT, warnings TEXT,
  undone_by INTEGER, pid INTEGER, sandbox TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, plan_id TEXT, run_id INTEGER,
  event TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS runs_plan ON runs(plan_id);
CREATE INDEX IF NOT EXISTS runs_fp ON runs(fp);
"""

J = lambda v: None if v is None else json.dumps(v, separators=(",", ":"))


class Store:
    def __init__(self, path=None):
        self.path = path or config.DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def event(self, plan_id, run_id, event, detail=None):
        self.db.execute("INSERT INTO events(ts,plan_id,run_id,event,detail) VALUES(?,?,?,?,?)",
                        (time.time(), plan_id, run_id, event, J(detail)))

    # ---------------------------------------------------------------- recovery
    def recover(self):
        """Mark anything left 'running' by a crash as interrupted/unknown."""
        rows = self.q("SELECT id, plan_id FROM runs WHERE status='running'")
        plans = self.q("SELECT id FROM plans WHERE state='running'")
        with self.tx():
            for r in rows:
                self.db.execute("UPDATE runs SET status='interrupted', changed='unknown', "
                                "failure='interrupted', ended=? WHERE id=?", (time.time(), r["id"]))
                self.event(r["plan_id"], r["id"], "marked_interrupted")
            for p in plans:
                self.db.execute("UPDATE plans SET state='interrupted', updated=? WHERE id=?",
                                (time.time(), p["id"]))
                self.event(p["id"], None, "plan_interrupted")
        return [r["id"] for r in rows]

    # ---------------------------------------------------------------- plans
    def new_plan(self, goal, steps):
        pid = "p" + uuid.uuid4().hex[:5]
        now = time.time()
        with self.tx():
            self.db.execute("INSERT INTO plans(id,goal,created,updated,state,steps) "
                            "VALUES(?,?,?,?,'new',?)", (pid, goal, now, now, J(steps)))
            self.event(pid, None, "plan_submitted", {"goal": goal, "steps": len(steps)})
        return pid

    def plan(self, pid):
        r = self.q1("SELECT * FROM plans WHERE id=?", (pid,))
        if not r:
            return None
        d = dict(r)
        for k in ("steps", "approved", "approval", "question", "report"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    def save_plan(self, pid, event=None, detail=None, **f):
        if not f:
            if event:
                with self.tx():
                    self.event(pid, None, event, detail)
            return
        cols = ", ".join(f"{k}=?" for k in f)
        vals = [J(v) if k in ("steps", "approved", "approval", "question", "report") else v
                for k, v in f.items()]
        with self.tx():
            self.db.execute(f"UPDATE plans SET {cols}, updated=? WHERE id=?",
                            (*vals, time.time(), pid))
            if event:
                self.event(pid, None, event, detail)

    def plans(self, limit=20):
        return [self.plan(r["id"]) for r in
                self.q("SELECT id FROM plans ORDER BY created DESC LIMIT ?", (limit,))]

    def plans_since(self, ts):
        return self.q1("SELECT COUNT(*) FROM plans WHERE created > ?", (ts,))[0]

    def request_cancel(self, pid):
        with self.tx():
            n = self.db.execute("UPDATE plans SET cancel=1 WHERE id=?", (pid,)).rowcount
            self.event(pid, None, "stop_requested")
        return n == 1

    def cancelled(self, pid):
        r = self.q1("SELECT cancel FROM plans WHERE id=?", (pid,))
        return bool(r and r[0])

    # ---------------------------------------------------------------- runs
    def begin_run(self, plan_id, step_no, kind, do, params, tier, fp, operation, approval):
        with self.tx():
            cur = self.db.execute(
                "INSERT INTO runs(plan_id,step_no,kind,do,params,tier,fp,operation,started,status,"
                "approval,changed) VALUES(?,?,?,?,?,?,?,?,?, 'running',?, 'unknown')",
                (plan_id, step_no, kind, do, J(params), tier, fp, operation, time.time(), approval))
            rid = cur.lastrowid
            self.event(plan_id, rid, "run_started", {"do": do, "tier": tier, "approval": approval})
        return rid

    def end_run(self, rid, **f):
        f.setdefault("ended", time.time())
        cols = ", ".join(f"{k}=?" for k in f)
        vals = [J(v) if isinstance(v, (dict, list)) else v for v in f.values()]
        with self.tx():
            self.db.execute(f"UPDATE runs SET {cols} WHERE id=?", (*vals, rid))
            r = self.db.execute("SELECT plan_id FROM runs WHERE id=?", (rid,)).fetchone()
            self.event(r[0] if r else None, rid, "run_ended", {"status": f.get("status")})

    update_run = end_run

    def set_pid(self, rid, pid):
        with self.lock:
            self.db.execute("UPDATE runs SET pid=? WHERE id=?", (pid, rid))

    def run(self, rid):
        r = self.q1("SELECT * FROM runs WHERE id=?", (rid,))
        if not r:
            return None
        d = dict(r)
        for k in ("params", "verify", "inverse", "warnings"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    def runs(self, plan_id):
        return [self.run(r["id"]) for r in
                self.q("SELECT id FROM runs WHERE plan_id=? ORDER BY id", (plan_id,))]

    def recent_success(self, fp, since):
        return self.q1("SELECT id, plan_id FROM runs WHERE fp=? AND status='ok' AND changed='yes' "
                       "AND undone_by IS NULL AND ended > ? ORDER BY id DESC", (fp, since))
