"""How the app behaves on a weak, busy computer with a lot of data.

Seeds a large database, starts the real server as its own process, optionally competes with a CPU hog on the same core,
signs in, then times what every screen asks the server for, and measures the CPU the server burns while someone just
leaves the app open (the polling the screens do). Run:  python tests/perf_bench.py [--leads 8000] [--hog] [--json out.json]
"""
import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
import authkit  # noqa: E402

SECTORS = ["Pharmacy", "Agrovet", "Hardware", "Salon", "Restaurant", "School", "Garage", "Supermarket", "Clinic", "Bookshop"]
TOWNS = ["Meru", "Kisii", "Embu", "Thika", "Nakuru", "Eldoret", "Kisumu", "Nyeri", "Machakos", "Kitale", "Kericho", "Bungoma"]


def seed(data_dir: str, n_leads: int, n_campaigns: int = 40):
    os.environ["DERICBI_DATA"] = data_dir
    import config
    config.set_data_dir(data_dir)
    import db
    import leads as L
    from util import now
    db.init_db()
    rnd = random.Random(7)
    t0 = time.time()
    with db.db() as c:
        ids = []
        for i in range(n_leads):
            phone = f"07{rnd.randint(10000000, 99999999)}" if rnd.random() < 0.75 else ""
            r = L.add_lead(c, {"name": f"{rnd.choice(['Mama', 'Baba', 'Kamau', 'Wanjiru', 'Otieno', 'Akinyi'])} {rnd.choice(SECTORS)} {i}",
                               "sector": rnd.choice(SECTORS), "town": rnd.choice(TOWNS), "phone": phone,
                               "email": f"lead{i}@shop{i % 400}.co.ke" if rnd.random() < 0.45 else "", "address": f"Plot {i}, Main Street"},
                           merge=False)
            if r["created"]:
                ids.append(r["id"])
        msgs = 0
        for k in range(n_campaigns):
            ch = ("email", "sms", "whatsapp", "auto")[k % 4]
            status = "running" if k >= n_campaigns - 2 else "done"
            cid = c.execute("INSERT INTO campaigns(name, channel, subject, body, filters, status, created_at, launched_at, total) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", (f"Campaign {k}", ch, "Hi", "Hello {name}", "{}", status, now(), now(), 0)).lastrowid
            members = rnd.sample(ids, min(len(ids) // 3 // n_campaigns * 3 + 1, len(ids)))
            for lid in members:
                real = ch if ch != "auto" else rnd.choice(("email", "sms", "whatsapp"))
                st = "queued" if status == "running" and rnd.random() < 0.6 else "sent"
                try:
                    c.execute("INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, subject, body, status, sent_at, created_at) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?)", (lid, cid, "out", real, "x", "Hi", "Hello there", st, now() if st == "sent" else None, now()))
                    msgs += 1
                except Exception:
                    pass
            c.execute("UPDATE campaigns SET total=? WHERE id=?", (len(members), cid))
        for lid in rnd.sample(ids, len(ids) // 12):
            c.execute("INSERT INTO messages(lead_id, direction, channel, to_addr, body, status, grade, handled, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (lid, "in", "email", "x", "Interested, call me", "received", rnd.choice(("hot", "warm", "cold", "unclear")), rnd.choice((0, 1)), now()))
        for lid in ids:
            for _ in range(2):
                c.execute("INSERT INTO events(lead_id, kind, detail, ts) VALUES(?,?,?,?)", (lid, "note", "something happened", now()))
        c.execute("INSERT INTO jobs(kind, status, params, created_at) VALUES('scrape','done','{}',?)", (now(),))
        jid = c.execute("SELECT MAX(id) FROM jobs").fetchone()[0]
        for i in range(4000):
            c.execute("INSERT INTO job_logs(job_id, ts, msg) VALUES(?,?,?)", (jid, now(), f"  + Business {i} | 0712 345 678"))
    return {"leads": len(ids), "messages": msgs, "seconds": round(time.time() - t0, 1)}


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")


def rss_mb(pid: int) -> float:
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024
    return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leads", type=int, default=8000)
    ap.add_argument("--hog", action="store_true", help="compete with a CPU-hungry process on the same core")
    ap.add_argument("--idle", type=int, default=30, help="seconds to leave the app open and polling")
    ap.add_argument("--json", default="")
    ap.add_argument("--data", default="", help="reuse a database made earlier")
    a = ap.parse_args()
    data = a.data or tempfile.mkdtemp(prefix="perf-")
    info = {"seeded": seed(data, a.leads)} if not a.data else {}
    print("seeded:", info.get("seeded"))
    port = 8811
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([sys.executable, str(ROOT / "backend" / "main.py"), "--port", str(port), "--data-dir", data],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    hog = subprocess.Popen([sys.executable, "-c", "while True: pass"]) if a.hog else None
    S = authkit.Session(base)
    try:
        t_start = time.time()
        for _ in range(200):
            try:
                S.call("GET", "/health"); break
            except Exception:
                time.sleep(0.1)
        info["startup_seconds"] = round(time.time() - t_start, 2)
        t = time.time(); S.sign_in(); info["first_sign_in_ms"] = round((time.time() - t) * 1000)
        t = time.time(); r = S.call("POST", "/auth/login", {"username": "Deric Marangu", "password": authkit.PASSWORD}); info["login_ms"] = round((time.time() - t) * 1000)
        calls = [("status (polled every 4s)", "GET", "/status", None), ("overview", "GET", "/dashboard", None), ("today", "GET", "/today", None),
                 ("leads page 1", "GET", "/leads?page=1&page_size=50", None), ("leads search", "GET", "/leads?q=mama&page=1&page_size=50", None),
                 ("leads filtered+sorted", "GET", "/leads?sector=Pharmacy&town=Meru&status=new&sort=name_asc", None),
                 ("facets", "GET", "/facets", None), ("replies inbox", "GET", "/inbox", None), ("campaign list (polled every 5s)", "GET", "/campaigns", None),
                 ("outreach: reach counts", "GET", "/campaigns/reach", None),
                 ("outreach: audience (best way)", "POST", "/campaigns/audience", {"channel": "auto", "filters": {}, "limit": 1000}),
                 ("outreach: audience (email)", "POST", "/campaigns/audience", {"channel": "email", "filters": {}, "limit": 1000}),
                 ("outreach: preview (ready)", "POST", "/campaigns/preview", {"channel": "auto", "style": "ready", "body": "", "filters": {}}),
                 ("outreach: preview (sms)", "POST", "/campaigns/preview", {"channel": "sms", "style": "ready", "body": "", "filters": {}}),
                 ("settings", "GET", "/settings", None), ("account", "GET", "/auth/status", None), ("a lead", "GET", "/leads/100", None)]
        rows = []
        for label, method, path, body in calls:
            times = []
            for _ in range(7):
                t = time.time(); res = S.call(method, path, body, timeout=120); times.append((time.time() - t) * 1000)
                if isinstance(res, dict) and res.get("error") and res["error"] != 400:
                    print("  !", label, res)
            rows.append({"call": label, "median_ms": round(statistics.median(times)), "worst_ms": round(max(times))})
        info["calls"] = rows
        pid = server.pid
        info["rss_mb_after_use"] = round(rss_mb(pid))
        # someone leaves the app open on the Outreach screen: status every 4s, campaigns every 5s
        c0, t0, ticks = cpu_seconds(pid), time.time(), 0
        while time.time() - t0 < a.idle:
            if ticks % 4 == 0:
                S.call("GET", "/status")
            if ticks % 5 == 0:
                S.call("GET", "/campaigns"); S.call("GET", "/status")
            ticks += 1
            time.sleep(1)
        wall = time.time() - t0
        info["idle_cpu_percent"] = round((cpu_seconds(pid) - c0) / wall * 100, 1)
    finally:
        server.terminate()
        if hog:
            hog.terminate()
    print(f"startup {info['startup_seconds']}s | sign-in {info['first_sign_in_ms']} ms (later logins {info['login_ms']} ms) | server memory {info['rss_mb_after_use']} MB | "
          f"CPU while just left open: {info['idle_cpu_percent']}%")
    for r in info["calls"]:
        flag = "  <-- slow" if r["median_ms"] > 400 else ""
        print(f"  {r['call']:<34}{r['median_ms']:>6} ms   (worst {r['worst_ms']} ms){flag}")
    if a.json:
        Path(a.json).write_text(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
