"""Integration test: the running app starts scrape worker processes, reports progress, honours Stop.
Run: python tests/test_scrape_api.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from mock_maps import start_mock_maps  # noqa: E402

PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"
DATA = tempfile.mkdtemp(prefix="dericbi-scrape-")


def call(method, path, body=None):
    req = urllib.request.Request(BASE + "/api" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return {"error": e.code, **json.loads(e.read() or b"{}")}


def wait_job(jid, states, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = next(j for j in call("GET", "/jobs") if j["id"] == jid)
        if job["status"] in states:
            return job
        time.sleep(0.5)
    raise AssertionError(f"job {jid} stuck: {job}")


mock = start_mock_maps(delay_for_slow=2.0)
env = dict(os.environ, DERICBI_MAPS_SEARCH_URL=f"http://127.0.0.1:{mock.server_port}/maps/search/")
exe = os.environ.get("BACKEND_EXE")   # test the frozen (PyInstaller) build instead of the Python source
cmd = [exe] if exe else [sys.executable, str(ROOT / "backend" / "main.py")]
server = subprocess.Popen(cmd + ["--port", str(PORT), "--data-dir", DATA, "--parent-pid", str(os.getpid())], env=env)
try:
    for _ in range(240):
        try:
            call("GET", "/health"); break
        except Exception:
            time.sleep(0.25)

    one = {"searches": [{"category": "Pharmacy", "town": "Meru"}], "max_per_search": 100, "headless": True}
    r = call("POST", "/jobs/scrape", one)
    jid = r["id"]
    # a second job while one is active must be refused
    assert call("POST", "/jobs/scrape", one).get("error") == 409
    job = wait_job(jid, {"done", "failed", "stopped"})
    assert job["status"] == "done", job
    assert (job["found"], job["added"], job["duplicates"]) == (10, 10, 0), job
    leads = call("GET", "/leads?page_size=100")
    assert leads["total"] == 10 and all(l["town"] == "Meru" and l["sector"] == "Pharmacy" for l in leads["leads"])
    logs = call("GET", f"/jobs/{jid}/logs")["logs"]
    assert any("Browser:" in l["msg"] for l in logs), [l["msg"] for l in logs][:5]

    # same search again: everything is already saved, nothing duplicated
    jid2 = call("POST", "/jobs/scrape", one)["id"]
    job2 = wait_job(jid2, {"done", "failed", "stopped"})
    assert (job2["added"], job2["duplicates"]) == (0, 10), job2
    assert call("GET", "/leads")["total"] == 10

    # Stop while running (place pages take 2s each)
    slow = {"searches": [{"category": "Slow", "town": "Meru"}], "max_per_search": 100, "headless": True}
    jid3 = call("POST", "/jobs/scrape", slow)["id"]
    t0 = time.time()
    while time.time() - t0 < 60:
        j = next(x for x in call("GET", "/jobs") if x["id"] == jid3)
        if j["added"] >= 1:
            break
        time.sleep(0.4)
    assert j["added"] >= 1, j
    assert call("POST", f"/jobs/{jid3}/stop").get("ok")
    job3 = wait_job(jid3, {"done", "failed", "stopped"}, timeout=60)
    assert job3["status"] == "stopped", job3
    total = call("GET", "/leads")["total"]
    assert 11 <= total < 20, total
    assert call("GET", "/status")["job"] is None

    # url opener refuses anything that isn't a web page
    assert call("POST", "/open-url", {"url": "file:///etc/passwd"}).get("error") == 400
    assert call("POST", "/open-url", {"url": "javascript:alert(1)"}).get("error") == 400
    print("SCRAPE API OK", {"first": job["added"], "again": job2["duplicates"], "stopped_after": total - 10})
finally:
    server.terminate()
    try:
        server.wait(8)
    except Exception:
        server.kill()
