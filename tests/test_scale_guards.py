"""Guards for what keeps the app smooth on big databases. Run: python tests/test_scale_guards.py

1. The result cache (backend/resultcache.py): only slow answers are remembered, for a limited time, and every change made
   through the app forgets them at once. Small databases must behave exactly as before.
2. The planner statistics (db.refresh_planner_stats): on a big database SQLite must learn how big the data has become,
   or queries are several times slower; on a small one it must NOT be told (statistics from a nearly empty database make
   it plan for one row). Only plans change, never answers.
"""
import os
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True
TMP = tempfile.mkdtemp(prefix="dericbi-scale-")
os.environ["DERICBI_DATA"] = TMP

import config  # noqa: E402
config.set_data_dir(TMP)
import auth  # noqa: E402
import db  # noqa: E402
import owner_key  # noqa: E402
import resultcache  # noqa: E402
from resultcache import ResultCache  # noqa: E402

RESULTS = []


def test(fn):
    try:
        fn()
        RESULTS.append(True)
        print(f"PASS {fn.__name__}")
    except Exception:
        RESULTS.append(False)
        print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    return fn


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def tick(self, s): self.t += s


def slow_compute(clock, calls, seconds=1.0, value="answer"):
    def f():
        calls.append(1); clock.tick(seconds); return value
    return f


@test
def a_fast_answer_is_never_remembered_so_small_databases_stay_live():
    clock, calls = Clock(), []
    c = ResultCache(slow=0.15, clock=clock)
    f = slow_compute(clock, calls, seconds=0.01)
    c.get("k", 30, f); c.get("k", 30, f)
    assert len(calls) == 2


@test
def a_slow_answer_is_remembered_until_it_expires():
    clock, calls = Clock(), []
    c = ResultCache(slow=0.15, clock=clock)
    f = slow_compute(clock, calls)
    assert c.get("k", 30, f) == "answer" and c.get("k", 30, f) == "answer"
    assert len(calls) == 1
    clock.tick(31)
    c.get("k", 30, f)
    assert len(calls) == 2


@test
def any_change_made_through_the_app_forgets_it_at_once():
    clock, calls = Clock(), []
    c = ResultCache(slow=0.15, clock=clock)
    f = slow_compute(clock, calls)
    c.get("k", 30, f); c.bump(); c.get("k", 30, f)
    assert len(calls) == 2


@test
def a_change_made_while_it_is_being_worked_out_makes_it_stale_immediately():
    clock, calls = Clock(), []
    c = ResultCache(slow=0.15, clock=clock)

    def f():
        calls.append(1); clock.tick(1.0); c.bump(); return "computed before the change finished"
    c.get("k", 30, f); c.get("k", 30, f)
    assert len(calls) == 2, "an answer computed across a change must not be served afterwards"


@test
def questions_are_remembered_separately_and_a_speed_up_stops_remembering():
    clock, a, b = Clock(), [], []
    c = ResultCache(slow=0.15, clock=clock)
    c.get("a", 30, slow_compute(clock, a, value="A")); c.get("b", 30, slow_compute(clock, b, value="B"))
    assert c.get("a", 30, slow_compute(clock, a)) == "A" and c.get("b", 30, slow_compute(clock, b)) == "B"
    assert len(a) == 1 and len(b) == 1
    clock.tick(31)
    quick = []
    c.get("a", 30, slow_compute(clock, quick, seconds=0.0)); c.get("a", 30, slow_compute(clock, quick, seconds=0.0))
    assert len(quick) == 2, "once the question is fast again it must stay live"


# ------------------------------------------------------------------ the planner statistics
def stats_rows(conn):
    return conn.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0] if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone() else 0


def _add(conn, prefix, n):
    conn.executemany("INSERT INTO leads(name, name_key, created_at, updated_at) VALUES(?,?,?,?)",
                     [(f"{prefix} {i}", f"{prefix}{i}".lower(), "2026-01-01 00:00:00", "2026-01-01 00:00:00") for i in range(n)])
    conn.commit()


@test
def a_small_database_is_left_alone_so_it_never_plans_for_a_one_row_table():
    # the trap this guards against: statistics taken while the tables were nearly empty made Today do 300 times more work
    db.init_db()
    conn = db.connect()
    try:
        assert db.refresh_planner_stats(conn) is False and stats_rows(conn) == 0, "an empty database must get no statistics"
        _add(conn, "Tiny", 40)
        assert db.refresh_planner_stats(conn) is False and stats_rows(conn) == 0, "40 leads must get no statistics"
        _add(conn, "Mid", 2000)
        assert db.refresh_planner_stats(conn) is False and stats_rows(conn) == 0, "2,000 leads is still small: no statistics"
        conn.execute("DELETE FROM leads"); conn.commit()
    finally:
        conn.close()


@test
def a_big_database_gets_statistics_which_follow_its_growth():
    db.init_db()
    conn = db.connect()
    try:
        _add(conn, "Big", 2000)
        assert db.refresh_planner_stats(conn, big=1500) is True, "past the threshold and never done: must run"
        conn.commit()
        assert stats_rows(conn) > 0
        assert db.refresh_planner_stats(conn, big=1500) is False, "nothing changed: no work"
        _add(conn, "More", 100)
        assert db.refresh_planner_stats(conn, big=1500) is False, "a 5% change is not worth a refresh"
        _add(conn, "Lots", 900)
        assert db.refresh_planner_stats(conn, big=1500) is True, "a 50% change is"
        conn.commit()
        assert db.refresh_planner_stats(conn, force=True) is True
        conn.commit()
        conn.execute("DELETE FROM leads"); conn.execute("DELETE FROM sqlite_stat1"); conn.commit()
    finally:
        conn.close()


@test
def refreshing_statistics_never_stops_the_app_from_starting():
    class Broken:
        def execute(self, *a, **k): raise sqlite3.OperationalError("database is locked")
    assert db.refresh_planner_stats(Broken()) is False


# ------------------------------------------------------------------ through the real app
@test
def through_the_app_a_change_you_make_shows_at_once_but_a_background_change_waits_for_the_timer():
    from fastapi.testclient import TestClient
    import server
    key, pub = auth.make_owner_key()
    owner_key.OWNER_PUBLIC_KEY = pub
    resultcache.SLOW = 0.0   # remember every answer, as a slow computer with a huge database would
    qs = [{"q": auth.QUESTIONS[i], "a": a} for i, a in enumerate(("Kamunyaka Primary", "Kisii", "Otieno"))]
    with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
        r = cl.post("/api/auth/setup", json={"mode": "owner", "username": "Deric Marangu", "password": "Maize&Tractor#2468",
                                             "questions": qs, "owner_key": key})
        assert r.status_code == 200, r.text
        before = cl.get("/api/dashboard").json()["total"]
        conn = db.connect()
        conn.execute("INSERT INTO leads(name, name_key, created_at, updated_at) VALUES('Background lead','backgroundlead','2026-01-01 00:00:00','2026-01-01 00:00:00')")
        conn.commit(); conn.close()
        assert cl.get("/api/dashboard").json()["total"] == before, "a change made outside the app waits for the timer"
        r = cl.post("/api/leads", json={"name": "Typed in by the owner", "phone": "0712345678"})
        assert r.status_code == 200, r.text
        assert cl.get("/api/dashboard").json()["total"] == before + 2, "a change made in the app must show at once, and include the background one"
        # looking something up must not throw the remembered answers away
        calls = cl.post("/api/campaigns/audience", json={"channel": "email", "filters": {}, "limit": 5})
        assert calls.status_code == 200, calls.text


bad = RESULTS.count(False)
print(f"\n{len(RESULTS) - bad}/{len(RESULTS)} passed")
sys.exit(1 if bad else 0)
