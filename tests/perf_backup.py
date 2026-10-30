"""Moves a business from one computer to another at a given size and measures it. Run: python tests/perf_backup.py [--data DIR | --leads N]

Starts two real servers (the old computer, and a brand-new one with an empty data folder), makes a backup on the old one,
brings the file to the new one over HTTP and restores it there, timing every step. A probe asks each server for its status
every 100 ms the whole time, to show whether the app stays responsive. At the end it checks the new computer holds exactly
what the old one did. Like perf_bench.py it needs a build with no owner key set (it signs in with a test password).
"""
import argparse
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backend"))
import authkit  # noqa: E402


def start(port, data):
    p = subprocess.Popen([sys.executable, str(ROOT / "backend" / "main.py"), "--port", str(port), "--data-dir", data],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    S = authkit.Session(f"http://127.0.0.1:{port}")
    for _ in range(300):
        try:
            S.call("GET", "/health")
            break
        except Exception:
            time.sleep(0.1)
    S.sign_in()
    return p, S


class Probe(threading.Thread):
    """Asks for the status every 100 ms and remembers how long each answer took."""
    def __init__(self, S):
        super().__init__(daemon=True)
        self.S, self.stop_flag, self.times = S, threading.Event(), []

    def run(self):
        while not self.stop_flag.is_set():
            t = time.time()
            try:
                self.S.call("GET", "/status", timeout=60)
                self.times.append((time.time() - t) * 1000)
            except Exception:
                self.times.append(60000)
            time.sleep(0.1)

    def finish(self):
        self.stop_flag.set()
        self.join()
        t = sorted(self.times) or [0]
        return {"asked": len(t), "median_ms": round(statistics.median(t)), "worst_ms": round(t[-1])}


def run_op(S, kind_path, timeout=1800):
    t = time.time()
    r = S.call("POST", kind_path, {} if kind_path.endswith("create") else None)
    assert not r.get("error"), r
    while time.time() - t < timeout:
        st = S.call("GET", "/backups/status")
        if st.get("op") and not st.get("running"):
            return time.time() - t, st
        time.sleep(0.25)
    raise SystemExit("did not finish")


def aggregate(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {}
    for t in ("leads", "messages", "campaigns", "events"):
        out[t] = c.execute(f"SELECT COUNT(*), COALESCE(SUM(id),0) FROM {t}").fetchone()
    out["sample"] = c.execute("SELECT COUNT(*), SUM(LENGTH(name)||LENGTH(COALESCE(email,''))) FROM (SELECT * FROM leads ORDER BY id LIMIT 5000)").fetchone()
    c.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="", help="reuse an existing data folder as the old computer")
    ap.add_argument("--leads", type=int, default=100_000)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    import perf_bench
    src = a.data or tempfile.mkdtemp(prefix="move-old-")
    if not a.data:
        print("seeding the old computer:", perf_bench.seed(src, a.leads), flush=True)
    dst = tempfile.mkdtemp(prefix="move-new-")
    info = {}
    pa, A = start(8821, src)
    pb, B = start(8822, dst)
    try:
        probe = Probe(A); probe.start()
        secs, st = run_op(A, "/backups/create")
        info["backup"] = {"seconds": round(secs, 1), "mb": round(st["result"]["bytes"] / 1e6), "counts": st["result"]["counts"], "responsive": probe.finish()}
        print("1. backup on the old computer:", info["backup"], flush=True)
        pkg = Path(src) / "backups" / st["result"]["name"]
        t = time.time()
        with open(pkg, "rb") as f:
            req = urllib.request.Request(B.base + "/api/backups/upload?name=moved.zip", data=f, method="PUT",
                                         headers={"Content-Length": str(pkg.stat().st_size), "Content-Type": "application/octet-stream"})
            with B.opener.open(req, timeout=1800) as r:
                up = json.loads(r.read())
        info["upload"] = {"seconds": round(time.time() - t, 1)}
        print("2. brought to the new computer:", info["upload"], flush=True)
        probe = Probe(B); probe.start()
        t = time.time()
        r = B.call("POST", f"/backups/{up['name']}/restore")
        assert not r.get("error"), r
        while True:
            stt = B.call("GET", "/backups/status")
            if stt.get("op") == "restore" and not stt.get("running"):
                break
            time.sleep(0.25)
        info["restore"] = {"seconds": round(time.time() - t, 1), "error": stt.get("error"), "responsive": probe.finish()}
        print("3. restore on the new computer:", info["restore"], flush=True)
        pa.terminate(); pb.terminate(); pa.wait(20); pb.wait(20)
        old, new = aggregate(os.path.join(src, "dericbi_crm.sqlite3")), aggregate(os.path.join(dst, "dericbi_crm.sqlite3"))
        info["identical"] = old == new
        print("4. the new computer holds exactly what the old one did:", info["identical"], "| leads", new["leads"][0], "messages", new["messages"][0], flush=True)
        if not info["identical"]:
            print("   old:", old, "\n   new:", new)
        if a.json:
            Path(a.json).write_text(json.dumps(info, indent=1))
    finally:
        for p in (pa, pb):
            if p.poll() is None:
                p.terminate()
        if not a.data:
            shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


if __name__ == "__main__":
    main()
