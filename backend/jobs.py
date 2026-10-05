"""Background jobs (scrape / enrich) run as separate worker processes.

A separate process keeps Playwright away from the web server's event loop,
makes "Stop" reliable, and means a browser crash can never take the app down.
The worker talks to the server only through the shared SQLite database.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import config
import db
from util import now, pid_alive

PROCS = {}  # job_id -> Popen (only for jobs started by this server process)
CREATE_NO_WINDOW = 0x08000000


def create_job(conn, kind: str, params: dict) -> int:
    cur = conn.execute("INSERT INTO jobs(kind, params, status, created_at) VALUES(?,?,?,?)",
                       (kind, json.dumps(params), "queued", now()))
    return cur.lastrowid


def log(job_id: int, msg: str):
    print(f"[job {job_id}] {msg}", flush=True)
    with db.db() as conn:
        conn.execute("INSERT INTO job_logs(job_id, ts, msg) VALUES(?,?,?)", (job_id, now(), msg))


def update(job_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db.db() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", list(fields.values()) + [job_id])


def bump(job_id: int, **deltas):
    cols = ", ".join(f"{k}={k}+?" for k in deltas)
    with db.db() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", list(deltas.values()) + [job_id])


def should_stop(job_id: int) -> bool:
    with db.db() as conn:
        row = conn.execute("SELECT stop_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(row and row["stop_requested"])


def get_job(conn, job_id: int):
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["params"] = json.loads(d["params"] or "{}")
    return d


def active_job(conn, kind=None):
    sql = "SELECT * FROM jobs WHERE status IN ('queued','running')"
    args = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT 1"
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def worker_command(kind: str, job_id: int):
    if getattr(sys, "frozen", False):
        return [sys.executable, "worker", kind, str(job_id)]
    return [sys.executable, str(Path(__file__).resolve().parent / "main.py"), "worker", kind, str(job_id)]


def spawn(job_id: int, kind: str):
    env = dict(os.environ)
    env["DERICBI_DATA"] = str(config.DATA_DIR)
    env["DERICBI_PARENT"] = str(os.getpid())
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    proc = subprocess.Popen(worker_command(kind, job_id), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **kwargs)
    PROCS[job_id] = proc
    update(job_id, pid=proc.pid)

    def _drain():
        # Keep the pipe from filling up; surface unexpected crashes in the job log.
        tail = []
        for line in iter(proc.stdout.readline, b""):
            tail.append(line.decode("utf-8", "replace").rstrip())
            tail = tail[-15:]
        code = proc.wait()
        with db.db() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row and row["status"] in ("queued", "running"):
                msg = "Worker exited unexpectedly" + (f" (code {code})" if code else "")
                detail = "\n".join(t for t in tail if "Traceback" in t or "Error" in t or "error" in t)[-600:]
                conn.execute("UPDATE jobs SET status='failed', finished_at=?, progress=? WHERE id=?",
                             (now(), msg, job_id))
                conn.execute("INSERT INTO job_logs(job_id, ts, msg) VALUES(?,?,?)",
                             (job_id, now(), msg + (": " + detail if detail else "")))
    threading.Thread(target=_drain, daemon=True).start()


def kill_tree(proc):
    """Kill a worker and everything it started (the browser). Plain terminate() would orphan Chrome on Windows."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            return
        except Exception:
            pass
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


def request_stop(job_id: int):
    update(job_id, stop_requested=1)

    def _force():
        proc = PROCS.get(job_id)
        with db.db() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if proc and proc.poll() is None and row and row["status"] in ("queued", "running"):
            kill_tree(proc)
            with db.db() as conn:
                conn.execute("UPDATE jobs SET status='stopped', finished_at=?, progress='Stopped' WHERE id=? "
                             "AND status IN ('queued','running')", (now(), job_id))
            log(job_id, "Stopped (worker was force-closed)")
    t = threading.Timer(20.0, _force)
    t.daemon = True
    t.start()


def reconcile_stale():
    """On server start: jobs left 'running' by a previous session are marked failed."""
    with db.db() as conn:
        for r in conn.execute("SELECT id, pid FROM jobs WHERE status IN ('queued','running')").fetchall():
            if not pid_alive(r["pid"]):
                conn.execute("UPDATE jobs SET status='failed', finished_at=?, progress='Interrupted (app was closed)' "
                             "WHERE id=?", (now(), r["id"]))
