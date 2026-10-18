"""Tests for backups, restore and moving to another computer. Run: python tests/test_backup.py

What they lock in: a backup holds the business and never what belongs to the computer (accounts, licence, install code);
a restore reproduces the business exactly, all or nothing, after saving a safety copy; damaged or altered files are refused
before anything is touched; older and newer backups still fit; nothing carries on sending by itself after a restore; and
only the owner can do any of it.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True
os.environ["DERICBI_DATA"] = tempfile.mkdtemp(prefix="dericbi-bk-")

import auth  # noqa: E402
import backup  # noqa: E402
import campaigns as C  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import owner_key  # noqa: E402

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


def raises(fn, text):
    try:
        fn()
    except backup.BackupError as e:
        return text.lower() in str(e).lower(), str(e)
    return False, "did not raise"


T = "2026-03-01 09:00:00"


def new_computer(name="A"):
    """A brand-new data folder, as on a fresh computer."""
    d = tempfile.mkdtemp(prefix=f"dericbi-bk-{name}-")
    config.set_data_dir(d)
    backup.maintenance.clear()
    db.init_db()
    return d


def sql(statement, args=(), many=None):
    c = db.connect()
    try:
        if many is not None:
            c.executemany(statement, many)
        else:
            c.execute(statement, args)
        c.commit()
    finally:
        c.close()


def rows(statement, args=()):
    c = db.connect()
    try:
        return [tuple(r) for r in c.execute(statement, args).fetchall()]
    finally:
        c.close()


def seed_business(n=240, messy=False):
    """A business with leads, campaigns, messages, history. messy=True adds the states a restore must make safe."""
    sql("INSERT INTO leads(name, name_key, email, phone_norm, sector, town, status, notes, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        many=[(f"Shop {i}", f"shop{i}", f"shop{i}@example.com" if i % 2 else None, f"2547{i:08d}" if i % 3 == 0 else None,
               "Pharmacy" if i % 2 else "Salon", "Meru" if i % 4 else "Embu", "new", f"note {i}", T, T) for i in range(1, n + 1)])
    sql("INSERT INTO campaigns(id, name, channel, body, status, created_at) VALUES(?,?,?,?,?,?)",
        many=[(1, "Spring", "email", "Hello", "running" if messy else "done", T), (2, "Spring follow-up", "email", "Again", "scheduled" if messy else "done", T),
              (3, "Old", "sms", "Hi", "paused", T), (4, "Dropped", "email", "x", "cancelled", T)])
    msgs = []
    for i in range(1, min(n, 120) + 1):
        msgs.append((i, 1, "out", "email", "sent", f"shop{i}@example.com", "Hello", "first note", T, T))
        if i % 10 == 0:   # a reply is not part of any campaign
            msgs.append((i, None, "in", "email", "received", f"shop{i}@example.com", "Re: Hello", "yes please", T, None))
        if i % 7 == 0:
            msgs.append((i, 2, "out", "email", "queued", f"shop{i}@example.com", "Again", "follow up", T, None))
    if messy:
        msgs.append((min(n, 130), 1, "out", "email", "sending", "late@example.com", "Hello", "mid-send", T, None))   # a lead that has no sent message yet
    sql("INSERT INTO messages(lead_id, campaign_id, direction, channel, status, to_addr, subject, body, created_at, sent_at) VALUES(?,?,?,?,?,?,?,?,?,?)", many=msgs)
    sql("INSERT INTO events(lead_id, ts, kind, detail) VALUES(?,?,?,?)", many=[(i, T, "note", f"called {i}") for i in range(1, min(n, 59) + 1)])
    sql("INSERT INTO jobs(id, kind, status, pid, created_at, progress) VALUES(1, 'scrape', ?, 999999, ?, '')", ("running" if messy else "done", T))
    sql("INSERT INTO job_logs(job_id, ts, msg) VALUES(1, ?, 'found 12')", (T,))


def seed_computer(owner="Deric Marangu", install="AAAA-BBBB-CCCC", licence="LIC.A", smtp="smtp-secret-A", sender="Owner A"):
    """What belongs to the computer and to who may use it."""
    sql("INSERT INTO users(id, username, role, pw_hash, created_at) VALUES(1, ?, 'owner', 'hash-owner', ?)", (owner, T))
    sql("INSERT INTO users(id, username, role, pw_hash, created_at) VALUES(2, 'Staff Person', 'member', 'hash-member', ?)", (T,))
    sql("INSERT INTO sessions(token_hash, user_id, created_ts, seen_ts) VALUES('tok1', 1, 1, 1)")
    sql("INSERT INTO security_answers(user_id, slot, question, answer_hash) VALUES(1, 1, 'q', 'h')")
    db.set_internal("install_id", install)
    db.set_internal("license", licence)
    db.set_internal("clock_high", 1_700_000_000)
    db.set_internal("whatsapp_linked", True)
    db.save_settings({"smtp_pass": smtp, "sender_name": sender, "ai_pitch": f"Pitch of {sender}"})


def fingerprint():
    out = {}
    c = db.connect()
    try:
        for t in backup.BUSINESS_TABLES:
            h = hashlib.sha256()
            for r in c.execute(f"SELECT * FROM {t} ORDER BY id"):
                h.update(repr(tuple(r)).encode())
            out[t] = h.hexdigest()
    finally:
        c.close()
    return out


def unpack(path, dest):
    with zipfile.ZipFile(path) as z:
        z.extractall(dest)
    return json.loads((Path(dest) / "manifest.json").read_text())


def repack(path, mutate, fix_hash=True, manifest_edit=None):
    """Make a changed copy of a backup (to test damaged, older and newer files)."""
    work = Path(tempfile.mkdtemp(prefix="repack-"))
    manifest = unpack(path, work)
    data = work / "data.sqlite3"
    c = sqlite3.connect(data)
    mutate(c)
    c.commit()
    c.close()
    if fix_hash:
        manifest["db_sha256"] = backup._sha256(data)
        manifest["db_bytes"] = data.stat().st_size
    if manifest_edit:
        manifest_edit(manifest)
    out = work / "changed.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.write(data, "data.sqlite3")
    return out


# ============================================================ what a backup holds
@test
def a_backup_holds_the_business_and_nothing_that_belongs_to_the_computer():
    new_computer("A")
    seed_business()
    seed_computer()
    info = backup.create_package()
    assert Path(info["path"]).exists() and info["counts"]["leads"] == 240 and info["counts"]["messages"] > 120
    work = tempfile.mkdtemp()
    manifest = unpack(info["path"], work)
    assert manifest["format"] == backup.FORMAT and manifest["includes_secrets"] is False and manifest["counts"] == info["counts"]
    c = sqlite3.connect(Path(work) / "data.sqlite3")
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for t in ("users", "sessions", "security_answers"):      # named here on purpose: a test that reads the code's own list proves nothing
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, f"{t} must not be in a backup"
    keys = {r[0] for r in c.execute("SELECT key FROM settings")}
    for k in ("install_id", "license", "clock_high", "whatsapp_linked", "whatsapp_linked_at"):
        assert k not in keys, f"{k} belongs to the computer and must not be in a backup"
    assert "smtp_pass" not in keys and "sender_name" in keys and "ai_pitch" in keys
    assert backup._sha256(Path(work) / "data.sqlite3") == manifest["db_sha256"]
    # the live data was not changed by making the backup
    assert rows("SELECT COUNT(*) FROM users")[0][0] == 2 and db.get_internal("install_id") == "AAAA-BBBB-CCCC"


@test
def passwords_and_keys_are_included_only_when_asked():
    new_computer("A")
    seed_business(30)
    seed_computer()
    work = tempfile.mkdtemp()
    manifest = unpack(backup.create_package(include_secrets=True)["path"], work)
    c = sqlite3.connect(Path(work) / "data.sqlite3")
    assert manifest["includes_secrets"] is True and c.execute("SELECT COUNT(*) FROM settings WHERE key='smtp_pass'").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM settings WHERE key IN ('install_id','license','clock_high')").fetchone()[0] == 0, "the licence is never saved, even then"
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


# ============================================================ restoring
@test
def a_restore_reproduces_the_business_exactly_and_keeps_the_new_computers_own_identity():
    new_computer("A")
    seed_business()
    seed_computer()
    package = backup.create_package()["path"]
    source = fingerprint()
    new_computer("B")                                    # another computer, with its own owner, licence and passwords
    sql("INSERT INTO leads(name, name_key, created_at, updated_at) VALUES('Left-over lead', 'leftover', ?, ?)", (T, T))
    seed_computer(owner="Deric Marangu", install="ZZZZ-YYYY-XXXX", licence="LIC.B", smtp="smtp-secret-B", sender="Owner B")
    summary = backup.restore(package)
    assert fingerprint() == source, "every lead, message, campaign, event and job must come back exactly"
    assert summary["restored"]["leads"] == 240 and rows("SELECT COUNT(*) FROM leads WHERE name='Left-over lead'")[0][0] == 0
    # this computer's identity and who may sign in is untouched
    assert db.get_internal("install_id") == "ZZZZ-YYYY-XXXX" and db.get_internal("license") == "LIC.B"
    assert rows("SELECT username, role, pw_hash FROM users ORDER BY id") == [("Deric Marangu", "owner", "hash-owner"), ("Staff Person", "member", "hash-member")]
    assert rows("SELECT COUNT(*) FROM sessions")[0][0] == 1
    s = db.get_settings()
    assert s["sender_name"] == "Owner A" and s["ai_pitch"] == "Pitch of Owner A", "business settings travel"
    assert s["smtp_pass"] == "smtp-secret-B", "a backup without passwords must not wipe this computer's working password"
    c = db.connect()
    try:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == [] and c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()


@test
def after_a_restore_nothing_carries_on_by_itself_so_two_computers_never_message_the_same_people():
    new_computer("A")
    seed_business(messy=True)
    package = backup.create_package()["path"]
    queued_before = rows("SELECT COUNT(*) FROM messages WHERE status='queued'")[0][0]
    new_computer("B")
    summary = backup.restore(package)
    statuses = dict(rows("SELECT id, status FROM campaigns"))
    assert statuses[1] == "paused" and statuses[2] == "paused", "running and scheduled follow-up campaigns must come back paused"
    assert statuses[3] == "paused" and statuses[4] == "cancelled" and summary["paused_campaigns"] == 2
    assert rows("SELECT last_error FROM campaigns WHERE id=1")[0][0].startswith("Paused after a restore")
    sending = rows("SELECT status, error FROM messages WHERE body='mid-send'")[0]
    assert sending[0] == "failed" and sending[1].startswith("Interrupted"), "a message caught mid-send must say check before retrying"
    assert rows("SELECT COUNT(*) FROM messages WHERE status='sending'")[0][0] == 0
    assert rows("SELECT COUNT(*) FROM messages WHERE status='queued'")[0][0] == queued_before, "queued messages stay queued, ready for a deliberate resume"
    assert rows("SELECT status FROM jobs WHERE id=1")[0][0] == "failed"


@test
def a_restore_saves_a_safety_copy_first_and_that_copy_can_undo_it():
    new_computer("A")
    seed_business(60)
    package = backup.create_package()["path"]
    new_computer("B")
    seed_business(25)
    before = fingerprint()
    summary = backup.restore(package)
    assert summary["safety_copy"].endswith("-before-restore.zip") and (backup.backups_dir() / summary["safety_copy"]).exists()
    assert fingerprint() != before
    backup.restore(backup.backups_dir() / summary["safety_copy"])
    assert fingerprint() == before, "restoring the safety copy must put back exactly what was there"


@test
def a_restore_is_all_or_nothing():
    new_computer("A")
    seed_business(80)
    good = backup.create_package()["path"]
    broken = repack(good, lambda c: c.execute("INSERT INTO messages(lead_id, direction, channel, status, created_at) VALUES(999999,'out','email','sent','x')"))
    new_computer("B")
    seed_business(30)
    before = fingerprint()
    ok, msg = raises(lambda: backup.restore(broken), "nothing was changed")
    assert ok, msg
    assert fingerprint() == before, "a restore that fails part-way must leave the current data exactly as it was"
    assert not backup.maintenance.is_set()


@test
def damaged_or_altered_files_are_refused_before_anything_is_touched():
    new_computer("A")
    seed_business(40)
    good = backup.create_package()["path"]
    new_computer("B")
    seed_business(15)
    before = fingerprint()
    junk = Path(tempfile.mkdtemp()) / "notes.zip"
    junk.write_text("this is not a backup")
    cut = Path(tempfile.mkdtemp()) / "cut.zip"
    cut.write_bytes(Path(good).read_bytes()[:3000])
    newer = repack(good, lambda c: None, manifest_edit=lambda m: m.update(format=99))
    tampered = repack(good, lambda c: c.execute("UPDATE leads SET name='Hacked'"), fix_hash=False)
    def lose_leads(c):
        c.execute("DROP TABLE messages")
        c.execute("DROP TABLE events")
        c.execute("ALTER TABLE leads RENAME TO people")
    no_leads = repack(good, lose_leads)
    for label, path, needle in (("not a backup", junk, "not a dericbi backup"), ("truncated", cut, "not a dericbi backup"),
                                ("from a newer app", newer, "newer version"), ("changed after it was made", tampered, "checksum"),
                                ("no leads", no_leads, "no leads table")):
        ok, msg = raises(lambda p=path: backup.restore(p), needle)
        assert ok, f"{label}: {msg}"
        assert fingerprint() == before, f"{label}: the current data must be untouched"
    assert not list(backup.backups_dir().glob("*before-restore*")), "a refused file must not even cost a safety copy"
    assert not any(p.name.startswith(".restoring-") for p in backup.backups_dir().iterdir()), "no half-unpacked files are left behind"


@test
def an_older_and_a_newer_backup_still_fit_this_version():
    new_computer("A")
    seed_business(50)
    good = backup.create_package()["path"]
    older = repack(good, lambda c: c.execute("ALTER TABLE leads DROP COLUMN offer"))                 # made before 'offer' existed
    newer = repack(good, lambda c: (c.execute("ALTER TABLE leads ADD COLUMN future_thing TEXT"), c.execute("CREATE TABLE future_table(x)")))
    for label, path in (("older", older), ("newer", newer)):
        new_computer("B")
        summary = backup.restore(path)
        assert summary["restored"]["leads"] == 50 and rows("SELECT COUNT(*) FROM messages")[0][0] > 50, label
        assert rows("SELECT COUNT(DISTINCT offer) FROM leads")[0][0] <= 1, f"{label}: a column the backup lacks gets its default"


@test
def the_plain_database_copy_the_old_button_made_can_still_be_restored_without_taking_over_who_signs_in():
    new_computer("A")
    seed_business(70)
    seed_computer(smtp="smtp-secret-A")
    raw = Path(tempfile.mkdtemp()) / "old-backup.sqlite3"
    src = db.connect()
    dst = sqlite3.connect(raw)
    src.backup(dst)
    dst.close()
    src.close()
    source = fingerprint()
    new_computer("B")
    seed_computer(owner="Deric Marangu", install="NEW1-NEW2-NEW3", licence="LIC.B", smtp="smtp-secret-B", sender="Owner B")
    assert backup.describe(raw)["legacy"] is True
    summary = backup.restore(raw)
    assert fingerprint() == source and summary["restored"]["leads"] == 70
    assert db.get_internal("install_id") == "NEW1-NEW2-NEW3" and db.get_internal("license") == "LIC.B", "an old copy must never carry a licence over"
    assert rows("SELECT COUNT(*) FROM users")[0][0] == 2 and rows("SELECT pw_hash FROM users WHERE id=1")[0][0] == "hash-owner"
    assert db.get_settings()["smtp_pass"] == "smtp-secret-A", "the old file's own passwords are what the owner chose to restore"


@test
def settings_are_restored_by_rules_not_blindly():
    new_computer("A")
    seed_business(10)
    db.set_internal("imap_last_uid", 500)
    sql("INSERT INTO settings(key, value) VALUES('totally_unknown_key', '\"x\"')")
    package = backup.create_package()["path"]
    new_computer("B")
    db.set_internal("imap_last_uid", 9000)             # this computer already knows where it is in the mailbox
    backup.restore(package)
    assert db.get_internal("imap_last_uid") == 9000
    assert db.get_internal("totally_unknown_key") is None, "keys the app does not know are never imported"
    new_computer("C")                                  # a brand-new computer takes the position from the backup
    backup.restore(package)
    assert db.get_internal("imap_last_uid") == 500


@test
def the_logo_travels_with_the_business():
    new_computer("A")
    seed_business(5)
    (config.DATA_DIR / "logo.png").write_bytes(b"\x89PNG-logo-of-A")
    package = backup.create_package()["path"]
    new_computer("B")
    (config.DATA_DIR / "logo.jpg").write_bytes(b"old logo of B")
    backup.restore(package)
    assert (config.DATA_DIR / "logo.png").read_bytes() == b"\x89PNG-logo-of-A" and not (config.DATA_DIR / "logo.jpg").exists()


# ============================================================ automatic backups and tidiness
@test
def automatic_backups_are_taken_on_time_kept_tidy_and_copied_to_a_second_folder():
    new_computer("A")
    seed_business(20)
    s = db.get_settings()
    assert backup.auto_due(s) is True and backup.auto_due({**s, "backup_auto": False}) is False
    external = Path(tempfile.mkdtemp(prefix="usb-"))
    info = backup.run_auto({**s, "backup_dir": str(external), "backup_keep": 2})
    assert (external / info["name"]).exists() and info["external"] == str(external)
    assert backup.auto_due(s) is False, "a backup just taken must not be due again"
    assert backup.auto_due(s, now_ts=time.time() + 25 * 3600) is True
    for stamp in ("20260101-010101", "20260102-010101", "20260103-010101"):
        for folder in (backup.backups_dir(), external):
            shutil.copy(info["path"], folder / f"{backup.PREFIX}{stamp}-auto.zip")
    manual = backup.create_package(label="manual")
    backup.prune(2)
    backup.prune(2, external)
    autos = sorted(p.name for p in backup.backups_dir().glob("*-auto.zip"))
    assert len(autos) == 2 and info["name"] in autos, autos
    assert Path(manual["path"]).exists(), "a backup you made yourself is never deleted automatically"
    assert len(list(external.glob("*-auto.zip"))) == 2


@test
def an_unreachable_second_folder_never_stops_the_main_backup():
    new_computer("A")
    seed_business(5)
    blocker = Path(tempfile.mkdtemp()) / "afile"
    blocker.write_text("x")
    info = backup.run_auto({**db.get_settings(), "backup_dir": str(blocker / "inside")})
    assert Path(info["path"]).exists() and info.get("external_error"), "the main copy is saved and the problem is reported"


@test
def the_listing_shows_good_backups_and_flags_damaged_ones():
    new_computer("A")
    seed_business(12)
    backup.create_package(label="manual")
    (backup.backups_dir() / "DericBI-backup-20250101-000000-manual.zip").write_text("garbage")
    items = {i["name"]: i for i in backup.list_backups()}
    good = [i for i in items.values() if not i.get("error")]
    bad = [i for i in items.values() if i.get("error")]
    assert len(good) == 1 and good[0]["counts"]["leads"] == 12 and len(bad) == 1


# ============================================================ keeping sending still, and one operation at a time
@test
def sending_and_the_inbox_stand_still_while_a_restore_runs():
    backup.maintenance.clear()
    assert backup.enter_work() is True
    backup.exit_work()
    held = threading.Event()
    release = threading.Event()

    def worker():
        assert backup.enter_work()
        held.set()
        release.wait(5)
        backup.exit_work()
    t = threading.Thread(target=worker)
    t.start()
    held.wait(5)
    assert backup.quiesce(timeout=0.3) is False, "it must not claim to be still while a send is in progress"
    assert not backup.maintenance.is_set(), "and it must let work carry on if it could not stop it"
    release.set()
    t.join()
    assert backup.quiesce(timeout=2) is True and backup.maintenance.is_set()
    assert backup.enter_work() is False, "no new send may begin during a restore"
    sender = C.Sender()
    sender._tick = lambda: (_ for _ in ()).throw(AssertionError("the sender must not work during a restore"))
    assert sender.tick() == 2.0
    backup.maintenance.clear()


@test
def only_one_backup_or_restore_runs_at_a_time():
    new_computer("A")
    gate = threading.Event()
    backup.start("backup", lambda progress: gate.wait(5))
    ok, msg = raises(lambda: backup.start("restore", lambda progress: None), "already running")
    assert ok, msg
    assert backup.status()["running"] is True
    gate.set()
    for _ in range(50):
        if not backup.status()["running"]:
            break
        time.sleep(0.05)
    assert backup.status()["running"] is False and backup.status()["error"] == ""


# ============================================================ through the real app
def owner_client():
    from fastapi.testclient import TestClient
    import server
    new_computer("api")
    key, pub = auth.make_owner_key()
    owner_key.OWNER_PUBLIC_KEY = pub
    qs = [{"q": auth.QUESTIONS[i], "a": a} for i, a in enumerate(("Kamunyaka Primary", "Kisii", "Otieno"))]
    cl = TestClient(server.create_app(port=8767), base_url="http://127.0.0.1:8767")
    cl.__enter__()
    r = cl.post("/api/auth/setup", json={"mode": "owner", "username": "Deric Marangu", "password": "Maize&Tractor#2468", "questions": qs, "owner_key": key})
    assert r.status_code == 200, r.text
    return cl


def wait_done(cl, limit=60):
    for _ in range(int(limit * 10)):
        st = cl.get("/api/backups/status").json()
        if st["op"] and not st["running"]:
            return st
        time.sleep(0.1)
    raise AssertionError("the operation did not finish")


@test
def through_the_app_the_owner_can_back_up_download_receive_and_restore():
    cl = owner_client()
    try:
        seed_business(90)
        assert cl.post("/api/backups/create", json={}).status_code == 200
        st = wait_done(cl)
        assert st["error"] == "" and st["result"]["counts"]["leads"] == 90
        listing = cl.get("/api/backups").json()
        assert len(listing["items"]) >= 1 and listing["dir"] and listing["free_mb"] > 0
        name = st["result"]["name"]
        got = cl.get(f"/api/backups/{name}/download")
        assert got.status_code == 200 and got.content[:2] == b"PK"
        source = fingerprint()
        # "another computer": wipe the business, then bring the file in by upload, and restore it
        sql("DELETE FROM job_logs"); sql("DELETE FROM jobs"); sql("DELETE FROM events"); sql("DELETE FROM messages"); sql("DELETE FROM campaigns"); sql("DELETE FROM leads")
        assert rows("SELECT COUNT(*) FROM leads")[0][0] == 0
        up = cl.put("/api/backups/upload?name=from-the-old-computer.zip", content=got.content)
        assert up.status_code == 200, up.text
        assert cl.post(f"/api/backups/{up.json()['name']}/restore").status_code == 200
        st = wait_done(cl)
        assert st["error"] == "" and st["result"]["restored"]["leads"] == 90, st
        assert fingerprint() == source
        assert cl.get("/api/leads").status_code == 200, "the app works normally right after"
        assert not backup.maintenance.is_set()
    finally:
        cl.__exit__(None, None, None)


@test
def through_the_app_a_bad_upload_is_refused_and_leaves_nothing_behind():
    cl = owner_client()
    try:
        before = sorted(p.name for p in backup.backups_dir().iterdir())
        r = cl.put("/api/backups/upload?name=photo.zip", content=b"definitely not a backup")
        assert r.status_code == 400 and "not a dericbi backup" in r.text.lower(), r.text
        assert sorted(p.name for p in backup.backups_dir().iterdir()) == before
    finally:
        cl.__exit__(None, None, None)


@test
def through_the_app_file_names_cannot_escape_the_backups_folder():
    cl = owner_client()
    try:
        for bad in ("..%2F..%2Fdericbi_crm.sqlite3", "%2E%2E%2Fsecret.zip", "x.txt", "a%00b.zip", ".hidden.zip"):
            r = cl.get(f"/api/backups/{bad}/download")
            assert r.status_code in (400, 404), (bad, r.status_code)
        for bad in ("..%2Fdericbi_crm.sqlite3", "%2E%2E%2Fdericbi_crm.sqlite3", "..", "x.zip"):
            assert cl.delete(f"/api/backups/{bad}").status_code in (400, 404, 405), bad   # refused, never a deletion
        assert Path(config.db_path()).exists()
    finally:
        cl.__exit__(None, None, None)


@test
def while_a_restore_runs_changes_are_refused_politely_but_reading_and_progress_still_work():
    cl = owner_client()
    try:
        backup.maintenance.set()
        r = cl.post("/api/leads", json={"name": "Blocked while restoring"})
        assert r.status_code == 503 and r.json().get("maintenance") is True
        assert cl.get("/api/leads").status_code == 200 and cl.get("/api/backups/status").status_code == 200
        backup.maintenance.clear()
        assert cl.post("/api/leads", json={"name": "Fine afterwards"}).status_code == 200
    finally:
        backup.maintenance.clear()
        cl.__exit__(None, None, None)


@test
def only_the_owner_may_touch_backups_and_a_staff_account_cannot_take_a_raw_copy():
    cl = owner_client()
    try:
        token = "staff-session-token-1234567890"
        sql("INSERT INTO users(id, username, role, pw_hash, created_at) VALUES(7, 'Staff Person', 'member', 'x', ?)", (T,))
        for slot in (1, 2, 3):
            sql("INSERT INTO security_answers(user_id, slot, question, answer_hash) VALUES(7, ?, 'q', 'h')", (slot,))
        t = int(time.time())
        sql("INSERT INTO sessions(token_hash, user_id, created_ts, seen_ts) VALUES(?, 7, ?, ?)", (auth._hash_token(token), t, t))
        owner_cookie = cl.cookies.get(auth.COOKIE)
        cl.cookies.set(auth.COOKIE, token)
        assert cl.get("/api/leads").status_code == 200, "the staff session itself is valid"
        for method, path in (("get", "/api/backups"), ("get", "/api/backups/status"), ("post", "/api/backups/create"), ("get", "/api/backup"),
                             ("get", "/api/backups/x.zip/download"), ("post", "/api/backups/x.zip/restore"), ("delete", "/api/backups/x.zip")):
            r = getattr(cl, method)(path, **({"json": {}} if method == "post" else {}))
            assert r.status_code == 403, (path, r.status_code)
        assert cl.put("/api/backups/upload?name=a.zip", content=b"x").status_code == 403
        cl.cookies.set(auth.COOKIE, owner_cookie)
        assert cl.get("/api/backup").status_code == 200 and cl.get("/api/backups").status_code == 200
    finally:
        cl.__exit__(None, None, None)


@test
def restoring_refuses_while_a_lead_search_is_still_running():
    new_computer("A")
    seed_business(10)
    package = backup.create_package()["path"]
    new_computer("B")
    sql("INSERT INTO jobs(kind, status, pid, created_at) VALUES('scrape', 'running', ?, ?)", (os.getpid(), T))
    ok, msg = raises(lambda: backup.restore(package), "lead search is still running")
    assert ok, msg


# ============================================================ a damaged data file
def ruin(path):
    """Overwrite the start of a SQLite file, as a bad disk or a half-finished copy might."""
    with open(path, "r+b") as f:
        f.write(b"\x00garbage-not-a-database" * 40)


@test
def a_damaged_data_file_is_set_aside_not_deleted_and_the_newest_backup_takes_its_place():
    new_computer("A")
    seed_business(120)
    source = fingerprint()
    backup.create_package(label="auto")
    newest = backup.create_package(label="auto")
    path = Path(config.db_path())
    ruin(path)
    damaged_bytes = path.read_bytes()
    backup.open_database()
    asides = sorted(path.parent.glob("*.damaged-*"))
    assert len(asides) == 1 and asides[0].read_bytes() == damaged_bytes, "the damaged file must be kept exactly as it was"
    assert fingerprint() == source, "the business must be back, from the newest backup"
    note = backup.notice()
    assert note and note["restored_from"] == newest["name"] and note["aside"] == asides[0].name and not note["restore_error"]
    assert rows("SELECT COUNT(*) FROM users")[0][0] == 0, "accounts are not in a backup, so they are made again (and that is said)"
    backup.dismiss_notice()
    assert backup.notice() is None


@test
def with_no_backup_a_damaged_file_still_lets_the_app_start_and_says_so():
    new_computer("A")
    seed_business(30)
    ruin(Path(config.db_path()))
    backup.open_database()
    note = backup.notice()
    assert note and note["restored_from"] == "" and rows("SELECT COUNT(*) FROM leads")[0][0] == 0
    assert list(Path(config.db_path()).parent.glob("*.damaged-*")), "the damaged file is still there to be rescued"


@test
def a_database_that_is_only_busy_is_never_mistaken_for_a_damaged_one():
    new_computer("A")
    seed_business(30)
    real = db.init_db

    def busy():
        raise sqlite3.OperationalError("database is locked")
    db.init_db = busy
    try:
        try:
            backup.open_database()
            assert False, "a busy database must be reported, not handled"
        except sqlite3.OperationalError:
            pass
    finally:
        db.init_db = real
    assert not list(Path(config.db_path()).parent.glob("*.damaged-*")) and rows("SELECT COUNT(*) FROM leads")[0][0] == 30


@test
def the_health_check_reports_a_good_database_and_the_panel_data_includes_it():
    cl = owner_client()
    try:
        seed_business(40)
        h = backup.health_check()
        assert h["ok"] is True and h["checked"] > 0
        body = cl.get("/api/backups").json()
        assert body["health"]["ok"] is True and "notice" in body
        assert cl.post("/api/backups/dismiss-notice").status_code == 200
    finally:
        cl.__exit__(None, None, None)


bad = RESULTS.count(False)
print(f"\n{len(RESULTS) - bad}/{len(RESULTS)} passed")
sys.exit(1 if bad else 0)
