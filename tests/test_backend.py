"""End-to-end backend tests. Run:  python tests/test_backend.py"""
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))

TMP = tempfile.mkdtemp(prefix="dericbi-test-")
os.environ["DERICBI_DATA"] = TMP

import config  # noqa: E402
config.set_data_dir(TMP)
import db  # noqa: E402
import grading  # noqa: E402
import leads as L  # noqa: E402
import messaging  # noqa: E402
import campaigns as C  # noqa: E402
import jobs  # noqa: E402
import scraper  # noqa: E402
import enrich  # noqa: E402
from util import normalize_phone, now  # noqa: E402

RESULTS = []


def test(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, True, ""))
        print(f"PASS {fn.__name__}")
    except Exception:
        RESULTS.append((fn.__name__, False, traceback.format_exc()))
        print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    return fn


# ----------------------------------------------------------------- mock servers
SMTP_INBOX = []
SMS_CALLS = []
SMS_MODE = {"status": 101}


def start_smtp(auth=False):
    from aiosmtpd.controller import Controller

    class H:
        async def handle_DATA(self, server, session, envelope):
            if "refuse@" in ",".join(envelope.rcpt_tos):
                return "550 5.1.1 mailbox unavailable"
            SMTP_INBOX.append(envelope)
            return "250 OK"

        async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
            if address.startswith("refuse@"):
                return "550 5.1.1 no such user"
            envelope.rcpt_tos.append(address)
            return "250 OK"

    kw = {}
    if auth:
        from aiosmtpd.smtp import AuthResult, LoginPassword

        def authenticator(server, session, envelope, mechanism, auth_data):
            ok = isinstance(auth_data, LoginPassword) and auth_data.login == b"me" and auth_data.password == b"secret"
            return AuthResult(success=ok, handled=False if not ok else True)
        kw = dict(authenticator=authenticator, auth_required=True, auth_require_tls=False)
    import socket
    sk = socket.socket(); sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]; sk.close()
    c = Controller(H(), hostname="127.0.0.1", port=port, **kw)
    c.start()
    return c


def start_at():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
            SMS_CALLS.append((dict(self.headers), form))
            if self.headers.get("apiKey") != "atkey":
                self.send_response(401)
                self.end_headers()
                return
            code = SMS_MODE["status"]
            status = {101: "Success", 403: "InvalidPhoneNumber", 405: "InsufficientBalance"}.get(code, "Failed")
            body = json.dumps({"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [
                {"statusCode": code, "number": form["to"], "status": status, "messageId": "ATPid_123"}]}}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


db.init_db()


def fresh():
    with db.db() as c:
        for t in ("events", "messages", "campaigns", "job_logs", "jobs", "leads", "settings"):
            c.execute(f"DELETE FROM {t}")


# ======================================================================= UNIT
@test
def phone_normalisation():
    assert normalize_phone("0712 345 678") == ("+254712345678", True)
    assert normalize_phone("+254 712-345-678") == ("+254712345678", True)
    assert normalize_phone("254712345678") == ("+254712345678", True)
    assert normalize_phone("712345678") == ("+254712345678", True)
    assert normalize_phone("0112345678") == ("+254112345678", True)
    assert normalize_phone("064 30123")[1] is False       # landline
    assert normalize_phone("N/A") == (None, False)
    assert normalize_phone("") == (None, False)
    assert normalize_phone("0712345678 / 0733111222")[0] == "+254712345678"
    assert normalize_phone("+44 20 7946 0958")[1] is False


@test
def grading_cases():
    g = lambda t: grading.classify(t)["grade"]
    assert g("Yes please") == "hot"
    assert g("Interested, how much is it?") == "hot"
    assert g("Nipigie tafadhali") == "hot"
    assert g("Naomba maelezo zaidi") == "hot"
    assert g("Please send details") == "hot"
    assert g("not interested") == "cold"
    assert g("We already have a system, no thanks") == "cold"
    assert g("Hapana") == "cold"
    assert g("sitaki") == "cold"
    assert g("STOP") == "optout"
    assert g("Please remove me from your list") == "optout"
    assert g("Usinitumie ujumbe tena") == "optout"
    assert g("wrong number") == "optout"
    assert g("Maybe later, I'm busy") == "warm"
    assert g("Who is this?") == "warm"
    assert g("ok") == "warm"
    assert g("I am out of office until Monday. This is an automatic reply.") == "auto"
    assert g("asdf qwerty") == "unclear"
    assert g("") == "unclear"
    assert g("Not interested now, maybe later") == "cold"
    # quoted history must not be graded
    assert g("Not interested\n\nOn Mon, 5 Oct 2026 at 10:00, DericBI wrote:\n> Are you interested in analytics?") == "cold"
    assert g("thanks\n> please call me, how much") == "unclear"


@test
def dedupe_layers():
    fresh()
    with db.db() as c:
        a = L.add_lead(c, {"name": "Meru Pharmacy", "town": "Meru", "phone": "0712 345 678", "address": "Kenyatta Hwy"})
        assert a["created"]
        # same phone, different spelling / name
        b = L.add_lead(c, {"name": "Totally Different", "town": "Embu", "phone": "+254712345678"})
        assert not b["created"] and b["reason"] == "same phone number" and b["id"] == a["id"]
        # same name+town, no phone, same address
        d = L.add_lead(c, {"name": "MERU PHARMACY LTD", "town": "meru", "address": "Kenyatta Hwy"})
        assert not d["created"] and d["reason"] == "same name and town"
        # same name+town but clearly a different branch
        e = L.add_lead(c, {"name": "Meru Pharmacy", "town": "Meru", "address": "Makutano Road Opposite Stage"})
        assert e["created"], "different branch should be allowed"
        # place key
        f = L.add_lead(c, {"name": "X", "town": "Y", "maps_url": "https://www.google.com/maps/place/X/data=!4m7!3m6!1s0x182789:0xabc123!8m2"})
        assert f["created"]
        g = L.add_lead(c, {"name": "Renamed X", "town": "Y2", "maps_url": "https://www.google.com/maps/place/Z/data=!1s0x182789:0xabc123"})
        assert not g["created"] and g["reason"] == "same Google place"
        # email
        h = L.add_lead(c, {"name": "Mail Shop", "town": "Thika", "email": "Info@Shop.co.ke"})
        i = L.add_lead(c, {"name": "Other", "town": "Nyeri", "email": "info@shop.co.ke"})
        assert h["created"] and not i["created"] and i["reason"] == "same email"
        n = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        assert n == 4, n


@test
def merge_fills_blanks_only():
    fresh()
    with db.db() as c:
        a = L.add_lead(c, {"name": "Chem One", "town": "Embu", "address": "Main St"})
        L.add_lead(c, {"name": "Chem One", "town": "Embu", "address": "Main St", "phone": "0722000111", "website": "http://x.co.ke"})
        r = L.get_lead(c, a["id"])
        assert r["phone_norm"] == "+254722000111" and r["website"] == "http://x.co.ke" and r["is_mobile"] == 1
        L.add_lead(c, {"name": "Chem One", "town": "Embu", "address": "Main St", "phone": "0733999888"})
        assert L.get_lead(c, a["id"])["phone_norm"] == "+254722000111", "must not overwrite"


@test
def archive_keeps_dedupe():
    fresh()
    with db.db() as c:
        a = L.add_lead(c, {"name": "Gone Pharmacy", "town": "Meru", "phone": "0700111222"})
        L.set_archived(c, [a["id"]], True)
        b = L.add_lead(c, {"name": "Gone Pharmacy", "town": "Meru", "phone": "0700111222"})
        assert not b["created"], "archived lead must still block re-adding"
        rows, total = L.list_leads(c)
        assert total == 0
        rows, total = L.list_leads(c, archived=True)
        assert total == 1


@test
def unique_index_backstop():
    import sqlite3
    fresh()
    with db.db() as c:
        L.add_lead(c, {"name": "A", "town": "T", "phone": "0700000001"})
        try:
            c.execute("INSERT INTO leads(name,name_key,phone_norm,created_at,updated_at) VALUES('B','b|t','+254700000001','x','x')")
            raise AssertionError("duplicate phone got into the DB")
        except sqlite3.IntegrityError:
            pass


@test
def update_conflicts_and_locks():
    fresh()
    with db.db() as c:
        a = L.add_lead(c, {"name": "A", "town": "T", "phone": "0700000001"})["id"]
        b = L.add_lead(c, {"name": "B", "town": "T", "phone": "0700000002"})["id"]
        try:
            L.update_lead(c, b, {"phone": "0700000001"})
            raise AssertionError("should conflict")
        except L.Conflict:
            pass
        x = L.update_lead(c, a, {"grade": "hot"})
        assert x["grade"] == "hot" and x["grade_locked"] == 1
        L.record_reply(c, a, "sms", "not interested")
        assert L.get_lead(c, a)["grade"] == "hot", "manual grade must stick"
        x = L.update_lead(c, a, {"grade": ""})
        assert x["grade_locked"] == 0
        try:
            L.update_lead(c, a, {"email": "not-an-email"})
            raise AssertionError("bad email accepted")
        except ValueError:
            pass


@test
def csv_import_old_format():
    fresh()
    csv_text = ("Business Name,Sector,Location,Address,Contact Info,Pitch Status\n"
                "Meru Chemist,Pharmacy,Meru,Main St,0712345678,Ready to Pitch\n"
                "Meru Chemist,Pharmacy,Meru,Main St,N/A,Ready to Pitch\n"
                "Chuka Agrovet,Agrovet,Chuka,,info@chukaagro.co.ke,Ready to Pitch\n"
                "Nkubu Wholesale,Wholesale,Nkubu,,N/A,Ready to Pitch\n"
                ",Pharmacy,Meru,,,\n")
    with db.db() as c:
        r = L.import_csv(c, csv_text)
        assert r["created"] == 3 and r["duplicates"] == 1 and r["invalid"] == 1, r
        ag = c.execute("SELECT * FROM leads WHERE name='Chuka Agrovet'").fetchone()
        assert ag["email"] == "info@chukaagro.co.ke" and ag["phone_norm"] is None
        r2 = L.import_csv(c, csv_text)
        assert r2["created"] == 0 and r2["duplicates"] == 4, r2
        out = L.export_csv(c)
        assert "Meru Chemist" in out and "+254712345678" in out


@test
def reply_flow_updates_lead():
    fresh()
    with db.db() as c:
        a = L.add_lead(c, {"name": "A", "town": "T", "phone": "0700000001"})["id"]
        c.execute("UPDATE leads SET status='contacted' WHERE id=?", (a,))
        r = L.record_reply(c, a, "sms", "Yes call me, how much?")
        lead = L.get_lead(c, a)
        assert r["grade"] == "hot" and lead["grade"] == "hot" and lead["status"] == "interested"
        r = L.record_reply(c, a, "sms", "STOP")
        lead = L.get_lead(c, a)
        assert lead["do_not_contact"] == 1
        # auto reply doesn't count
        b = L.add_lead(c, {"name": "B", "town": "T", "phone": "0700000002"})["id"]
        L.record_reply(c, b, "email", "I am out of office. This is an automatic reply.")
        lb = L.get_lead(c, b)
        assert lb["status"] == "new" and lb["last_reply_at"] is None
        # inbound dedupe by message id
        r1 = L.record_reply(c, b, "email", "hello", message_id="<x@y>")
        r2 = L.record_reply(c, b, "email", "hello", message_id="<x@y>")
        assert r2["duplicate"] and r1["id"] == r2["id"]


# ============================================================= SENDING (mocked)
SMTP = start_smtp()
AT = start_at()


def configure(**over):
    base = {"smtp_host": "127.0.0.1", "smtp_port": SMTP.port, "smtp_security": "none",
            "from_email": "deric@dericbi.test", "sender_name": "Deric @ DericBI",
            "at_username": "me", "at_api_key": "atkey", "at_base_url": f"http://127.0.0.1:{AT.server_port}",
            "send_window_enabled": False, "email_delay_sec": 0, "sms_delay_sec": 0, "skip_recent_days": 14}
    base.update(over)
    db.save_settings(base)


def run_sender(until=lambda: False, timeout=20):
    s = C.Sender()
    s.start()
    t0 = time.time()
    while time.time() - t0 < timeout and not until():
        time.sleep(0.2)
    s.stop()
    s.join(timeout=5)
    return s


def count(sql, *a):
    with db.db() as c:
        return c.execute(sql, a).fetchone()[0]


@test
def email_campaign_sends_and_dedupes():
    fresh(); SMTP_INBOX.clear(); configure()
    with db.db() as c:
        for i in range(5):
            L.add_lead(c, {"name": f"Shop {i}", "town": "Meru", "sector": "Pharmacy", "email": f"s{i}@shop.test"})
        L.add_lead(c, {"name": "Silent", "town": "Meru", "sector": "Pharmacy", "phone": "0700000009"})   # no email
        dnc = L.add_lead(c, {"name": "Nope", "town": "Meru", "sector": "Pharmacy", "email": "nope@shop.test"})["id"]
        L.update_lead(c, dnc, {"do_not_contact": True})
        pv = C.preview(c, "email", "Hi {name}", "Hello {name} from {town}", {"sectors": ["Pharmacy"]})
        assert pv["matching"] == 5, pv
        assert "STOP" in pv["sample"]["body"] and "Meru" in pv["sample"]["body"]
        cid = C.create_campaign(c, "Test", "email", "Hi {name}", "Hello {name} from {town}", {"sectors": ["Pharmacy"]}, launch=True)
    run_sender(lambda: count("SELECT COUNT(*) FROM messages WHERE status='sent'") >= 5)
    assert count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 5
    assert len(SMTP_INBOX) == 5
    env = SMTP_INBOX[0]
    body = env.content.decode() if isinstance(env.content, bytes) else env.content
    assert "Hello Shop" in body and "reply STOP" in body and "List-Unsubscribe" in body
    assert count("SELECT status FROM campaigns WHERE id=?", cid) == "done"
    assert count("SELECT COUNT(*) FROM leads WHERE status='contacted'") == 5
    # a second campaign must not pick anyone up (all contacted recently)
    with db.db() as c:
        pv = C.preview(c, "email", "Hi", "Hello", {"statuses": ["new", "contacted"]})
        assert pv["matching"] == 0, pv
        try:
            C.create_campaign(c, "Again", "email", "Hi", "Hello", {"statuses": ["new", "contacted"]}, launch=True)
            raise AssertionError("expected empty audience error")
        except ValueError:
            pass


@test
def sms_campaign_and_errors():
    fresh(); SMS_CALLS.clear(); SMS_MODE["status"] = 101; configure()
    with db.db() as c:
        for i in range(3):
            L.add_lead(c, {"name": f"Agro {i}", "town": "Chuka", "sector": "Agrovet", "phone": f"07000000{i:02d}"})
        L.add_lead(c, {"name": "Landline", "town": "Chuka", "sector": "Agrovet", "phone": "064 30123"})
        cid = C.create_campaign(c, "SMS", "sms", "", "Hi {name}, DericBI helps agrovets track stock.", {"sectors": ["Agrovet"]}, launch=True)
        assert c.execute("SELECT total FROM campaigns WHERE id=?", (cid,)).fetchone()[0] == 3
    run_sender(lambda: count("SELECT COUNT(*) FROM messages WHERE status='sent'") >= 3)
    assert len(SMS_CALLS) == 3
    hdr, form = SMS_CALLS[0]
    assert hdr.get("apiKey") == "atkey" and form["username"] == "me" and form["to"].startswith("+2547")
    assert "Reply STOP" in form["message"]

    # insufficient balance pauses the campaign and keeps the message queued
    fresh(); SMS_MODE["status"] = 405; configure()
    with db.db() as c:
        for i in range(3):
            L.add_lead(c, {"name": f"B {i}", "town": "T", "phone": f"07111111{i:02d}"})
        cid = C.create_campaign(c, "Bal", "sms", "", "Hello {name}", {}, launch=True)
    run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", cid) == "paused", timeout=10)
    assert count("SELECT status FROM campaigns WHERE id=?", cid) == "paused"
    assert "InsufficientBalance" in count("SELECT last_error FROM campaigns WHERE id=?", cid)
    assert count("SELECT COUNT(*) FROM messages WHERE status='queued'") == 3
    assert count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 0
    SMS_MODE["status"] = 101


@test
def bad_sms_key_pauses():
    fresh(); configure(at_api_key="wrong")
    with db.db() as c:
        L.add_lead(c, {"name": "A", "town": "T", "phone": "0700000001"})
        cid = C.create_campaign(c, "K", "sms", "", "Hello", {}, launch=True)
    run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", cid) == "paused", timeout=10)
    assert count("SELECT status FROM campaigns WHERE id=?", cid) == "paused"


@test
def refused_recipient_marks_bounce():
    fresh(); SMTP_INBOX.clear(); configure()
    with db.db() as c:
        L.add_lead(c, {"name": "Ghost", "town": "T", "email": "refuse@shop.test"})
        L.add_lead(c, {"name": "Real", "town": "T", "email": "real@shop.test"})
        cid = C.create_campaign(c, "B", "email", "Hi", "Hello {name}", {}, launch=True)
    run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", cid) == "done")
    assert count("SELECT email_bounced FROM leads WHERE name='Ghost'") == 1
    assert count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 1
    assert count("SELECT COUNT(*) FROM messages WHERE status='failed'") == 1


@test
def daily_cap_and_window_respected():
    fresh(); SMTP_INBOX.clear(); configure(email_daily_cap=2)
    with db.db() as c:
        for i in range(5):
            L.add_lead(c, {"name": f"S{i}", "town": "T", "email": f"c{i}@shop.test"})
        C.create_campaign(c, "Cap", "email", "Hi", "Hello", {}, launch=True)
    run_sender(lambda: False, timeout=5)
    assert count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 2
    assert count("SELECT COUNT(*) FROM messages WHERE status='queued'") == 3
    # outside sending hours: nothing goes out
    fresh(); SMTP_INBOX.clear()
    from datetime import datetime, timedelta
    t = datetime.now()
    configure(send_window_enabled=True, send_window_start=(t + timedelta(hours=2)).strftime("%H:%M"),
              send_window_end=(t + timedelta(hours=3)).strftime("%H:%M"))
    if (t + timedelta(hours=3)).day == t.day:
        with db.db() as c:
            L.add_lead(c, {"name": "W", "town": "T", "email": "w@shop.test"})
            C.create_campaign(c, "Win", "email", "Hi", "Hello", {}, launch=True)
        run_sender(lambda: False, timeout=3)
        assert count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 0


@test
def dnc_after_queueing_is_skipped():
    fresh(); SMTP_INBOX.clear(); configure()
    with db.db() as c:
        a = L.add_lead(c, {"name": "A", "town": "T", "email": "a@shop.test"})["id"]
        L.add_lead(c, {"name": "B", "town": "T", "email": "b@shop.test"})
        cid = C.create_campaign(c, "D", "email", "Hi", "Hello", {}, launch=True)
        L.update_lead(c, a, {"do_not_contact": True})   # opts out after launch, before send
    run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", cid) == "done")
    assert len(SMTP_INBOX) == 1
    assert count("SELECT COUNT(*) FROM messages WHERE status='skipped'") == 1


@test
def pause_resume_cancel():
    fresh(); SMTP_INBOX.clear(); configure()
    with db.db() as c:
        for i in range(3):
            L.add_lead(c, {"name": f"P{i}", "town": "T", "email": f"p{i}@shop.test"})
        cid = C.create_campaign(c, "PR", "email", "Hi", "Hello", {}, launch=True)
        C.set_campaign_status(c, cid, "pause")
    run_sender(lambda: False, timeout=2)
    assert len(SMTP_INBOX) == 0
    with db.db() as c:
        C.set_campaign_status(c, cid, "cancel")
    assert count("SELECT COUNT(*) FROM messages WHERE status='skipped'") == 3


@test
def smtp_auth_and_one_off_send():
    fresh(); SMTP_INBOX.clear()
    c2 = start_smtp(auth=True)
    port = c2.port
    configure(smtp_port=port, smtp_user="me", smtp_pass="secret")
    with db.db() as c:
        a = L.add_lead(c, {"name": "One Off", "town": "T", "email": "one@shop.test"})["id"]
    C.send_now(a, "email", "Quick hello {name}", "Hi {name}!")
    assert len(SMTP_INBOX) == 1 and b"Quick hello One Off" in SMTP_INBOX[0].content
    configure(smtp_port=port, smtp_user="me", smtp_pass="WRONG")
    try:
        C.send_now(a, "email", "x", "y")
        raise AssertionError("expected login failure")
    except messaging.SendError as e:
        assert e.kind == "pause", e
    with db.db() as c:
        L.update_lead(c, a, {"do_not_contact": True})
    try:
        C.send_now(a, "email", "x", "y")
        raise AssertionError("DNC must block")
    except ValueError:
        pass
    c2.stop()


@test
def placeholder_validation():
    fresh()
    with db.db() as c:
        try:
            C.preview(c, "sms", "", "Hi {nmae}", {})
            raise AssertionError("typo placeholder accepted")
        except ValueError as e:
            assert "nmae" in str(e)


# ================================================================ INBOUND EMAIL
def make_email(frm, subject, body, in_reply_to=None, mid=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "deric@dericbi.test", subject
    m["Message-ID"] = mid or f"<{time.time_ns()}@mail.test>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content(body)
    return m.as_bytes()


@test
def inbound_email_matching_and_bounce():
    fresh(); configure()
    with db.db() as c:
        a = L.add_lead(c, {"name": "Reply Co", "town": "T", "email": "owner@reply.test"})["id"]
        b = L.add_lead(c, {"name": "Thread Co", "town": "T", "email": "thread@thread.test"})["id"]
        c.execute("INSERT INTO messages(lead_id,direction,channel,status,message_id_header,created_at,body) "
                  "VALUES(?,?,?,?,?,?,?)", (b, "out", "email", "sent", "<out-1@dericbi.test>", now(), "hi"))
        bad = L.add_lead(c, {"name": "Bad Co", "town": "T", "email": "dead@bad.test"})["id"]
    r = messaging.process_inbound_email(make_email("Owner <owner@reply.test>", "Re: Hello", "Yes interested, call me\n\n> original"))
    assert r["matched"] and r["grade"] == "hot"
    # reply from a different address but threaded to our message
    r = messaging.process_inbound_email(make_email("assistant@other.test", "Re: Hello", "Not interested, thanks", in_reply_to="<out-1@dericbi.test>"))
    assert r["matched"] and r["lead_id"] == b and r["grade"] == "cold"
    # unknown sender ignored
    assert not messaging.process_inbound_email(make_email("spam@x.test", "Buy now", "cheap"))["matched"]
    # bounce
    r = messaging.process_inbound_email(make_email("MAILER-DAEMON@mx.test", "Undelivered Mail Returned to Sender",
                                                   "Your message to dead@bad.test could not be delivered."))
    assert r["matched"] and r["bounce"]
    with db.db() as c:
        assert L.get_lead(c, bad)["email_bounced"] == 1
        assert L.get_lead(c, bad)["last_reply_at"] is None
        assert L.get_lead(c, a)["status"] == "interested"


@test
def imap_polling_end_to_end():
    from fake_imap import FakeImap
    fresh(); configure()
    srv = FakeImap("inbox@dericbi.test", "pw")
    try:
        with db.db() as c:
            a = L.add_lead(c, {"name": "Poll Co", "town": "T", "email": "poll@co.test"})["id"]
        srv.add(make_email("old@x.test", "old mail", "pre-existing"))
        db.save_settings({"imap_host": "127.0.0.1", "imap_port": srv.port, "imap_security": "none",
                          "imap_user": "inbox@dericbi.test", "imap_pass": "pw"})
        res = messaging.test_imap(db.get_settings())
        assert res["ok"] and db.get_internal("imap_last_uid") == 1
        # nothing new: the 'n:*' RFC quirk must not re-import old mail
        st = messaging.poll_imap()
        assert st["fetched"] == 0, st
        srv.add(make_email("poll@co.test", "Re: hi", "Please send price list", mid="<p1@mail.test>"))
        srv.add(make_email("stranger@z.test", "hello", "who knows"))
        st = messaging.poll_imap()
        assert st["fetched"] == 2 and st["replies"] == 1, st
        st = messaging.poll_imap()
        assert st["fetched"] == 0
        with db.db() as c:
            lead = L.get_lead(c, a)
            assert lead["grade"] == "hot" and lead["status"] == "interested"
        # bad login -> pause-kind error
        db.save_settings({"imap_pass": "wrong"})
        try:
            messaging.poll_imap()
            raise AssertionError("expected login error")
        except messaging.SendError as e:
            assert e.kind == "pause"
    finally:
        srv.stop()


# ====================================================================== SCRAPER
class FakeDriver:
    def __init__(self, listings, details):
        self.listings, self.det = listings, details
        self.opened = self.closed = False
        self.detail_calls = []

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def search(self, query):
        return list(self.listings.get(query, []))

    def details(self, item):
        self.detail_calls.append(item["name"])
        if item["name"] == "Explodes":
            raise RuntimeError("boom")
        return dict(self.det[item["name"]], name=item["name"], maps_url=item["href"])


def href(n):
    return f"https://www.google.com/maps/place/x/data=!4m7!3m6!1s0x1:0x{n:x}!8m2"


@test
def scraper_worker_dedupes_and_counts():
    fresh()
    with db.db() as c:   # one place already known
        L.add_lead(c, {"name": "Known Chem", "town": "Meru", "maps_url": href(1), "phone": "0700000001"})
    listings = {"Pharmacy in Meru": [
        {"name": "Known Chem", "href": href(1)},
        {"name": "New Chem", "href": href(2)},
        {"name": "Phone Dup", "href": href(3)},       # same phone as Known Chem, different place
        {"name": "Explodes", "href": href(4)},
        {"name": "Third", "href": href(5)},
    ]}
    det = {"New Chem": {"phone": "0711111111", "address": "A St", "website": "", "category": "Pharmacy"},
           "Phone Dup": {"phone": "0700000001", "address": "B St", "website": "", "category": ""},
           "Third": {"phone": "N/A", "address": "C St", "website": "http://third.co.ke", "category": ""}}
    drv = FakeDriver(listings, det)
    with db.db() as c:
        jid = jobs.create_job(c, "scrape", {"searches": [{"category": "Pharmacy", "town": "Meru", "query": "Pharmacy in Meru"}],
                                            "max_per_search": 50})
    final = scraper.run_scrape(jid, {"searches": [{"category": "Pharmacy", "town": "Meru", "query": "Pharmacy in Meru"}],
                                     "max_per_search": 50}, driver_factory=lambda: drv, sleep=lambda s: None)
    assert final == "done" and drv.opened and drv.closed
    assert "Known Chem" not in drv.detail_calls, "known place must be skipped without opening it"
    with db.db() as c:
        j = jobs.get_job(c, jid)
        assert (j["found"], j["added"], j["duplicates"], j["errors"]) == (5, 2, 2, 1), j
        names = {r["name"] for r in c.execute("SELECT name FROM leads")}
        assert names == {"Known Chem", "New Chem", "Third"}, names
        assert c.execute("SELECT COUNT(*) FROM job_logs WHERE job_id=?", (jid,)).fetchone()[0] > 3
    # run again: nothing new, no duplicates created
    with db.db() as c:
        jid2 = jobs.create_job(c, "scrape", {})
    scraper.run_scrape(jid2, {"searches": [{"category": "Pharmacy", "town": "Meru", "query": "Pharmacy in Meru"}]},
                       driver_factory=lambda: FakeDriver(listings, det), sleep=lambda s: None)
    assert count("SELECT COUNT(*) FROM leads") == 3


@test
def scraper_zero_listings_is_flagged_not_clean_success():
    """A search that loads but finds nothing (typo, consent wall, changed page) must say so."""
    fresh()
    sp = {"searches": [{"category": "Zzz", "town": "Nowhere", "query": "Zzz in Nowhere"}]}
    with db.db() as c:
        jid = jobs.create_job(c, "scrape", sp)
    final = scraper.run_scrape(jid, sp, driver_factory=lambda: FakeDriver({}, {}), sleep=lambda s: None)
    assert final == "done"                    # an empty result is not a failure...
    with db.db() as c:
        j = jobs.get_job(c, jid)
    assert "no listings were found" in j["progress"], j["progress"]   # ...but it is never reported as a plain success


@test
def scraper_stop_and_cap():
    fresh()
    listings = {"Shops in Thika": [{"name": f"Shop{i}", "href": href(100 + i)} for i in range(10)]}
    det = {f"Shop{i}": {"phone": f"07220000{i:02d}", "address": f"{i} Rd", "website": "", "category": ""} for i in range(10)}
    sp = {"searches": [{"category": "Shops", "town": "Thika", "query": "Shops in Thika"}], "max_per_search": 3}
    with db.db() as c:
        jid = jobs.create_job(c, "scrape", sp)
    scraper.run_scrape(jid, sp, driver_factory=lambda: FakeDriver(listings, det), sleep=lambda s: None)
    assert count("SELECT COUNT(*) FROM leads") == 3, "cap per search"
    # stop request honoured mid-run
    fresh()
    with db.db() as c:
        jid = jobs.create_job(c, "scrape", sp)
    jobs.request_stop(jid)
    final = scraper.run_scrape(jid, sp, driver_factory=lambda: FakeDriver(listings, det), sleep=lambda s: None)
    assert final == "stopped" and count("SELECT COUNT(*) FROM leads") == 0


class _R:
    def __init__(self, text, code=200):
        self.text, self.status_code = text, code


@test
def email_enrichment():
    get = lambda u: _R('<a href="mailto:Sales@Acme.co.ke">x</a> logo@2x.png <p>info [at] acme.co.ke</p>') if "acme" in u else _R("", 404)
    es = enrich.find_emails("https://www.acme.co.ke", get)
    assert es[0] == "info@acme.co.ke" or es[0] == "sales@acme.co.ke", es
    assert all("png" not in e for e in es)
    assert enrich.find_emails("https://facebook.com/acme", get) == []
    fresh()
    with db.db() as c:
        a = L.add_lead(c, {"name": "Acme", "town": "T", "website": "http://acme.co.ke", "phone": "0700000001"})["id"]
        L.add_lead(c, {"name": "NoMail", "town": "T", "website": "http://nomail.co.ke", "phone": "0700000002"})
    stats = enrich.enrich_leads(lambda m: None, lambda: False, get=get)
    assert stats["found"] == 1 and stats["checked"] == 2
    with db.db() as c:
        assert L.get_lead(c, a)["email"] in ("info@acme.co.ke", "sales@acme.co.ke")
    stats = enrich.enrich_leads(lambda m: None, lambda: False, get=get)
    assert stats["checked"] == 0, "must not retry sites with no email within 30 days"


# ========================================================================== API
@test
def api_end_to_end():
    from fastapi.testclient import TestClient
    import server
    fresh()
    app = server.create_app(port=8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as cl:
        assert cl.get("/api/health").json()["ok"]
        # host / origin guards
        assert TestClient(app, base_url="http://evil.example").get("/api/health").status_code == 403
        r = cl.post("/api/leads", json={"name": "X"}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        # add + duplicate rejection
        r = cl.post("/api/leads", json={"name": "Api Pharmacy", "town": "Meru", "sector": "Pharmacy", "phone": "0712 000 111"})
        assert r.status_code == 200, r.text
        lid = r.json()["id"]
        r = cl.post("/api/leads", json={"name": "Whatever", "phone": "+254712000111"})
        assert r.status_code == 409 and r.json()["existing_id"] == lid
        assert cl.post("/api/leads", json={"name": ""}).status_code == 400
        # list / filter / detail / patch
        r = cl.get("/api/leads", params={"q": "api", "has_phone": True}).json()
        assert r["total"] == 1
        d = cl.get(f"/api/leads/{lid}").json()
        assert d["events"][0]["kind"] == "created"
        r = cl.patch(f"/api/leads/{lid}", json={"status": "contacted", "notes": "met owner"})
        assert r.json()["status"] == "contacted"
        assert cl.patch(f"/api/leads/{lid}", json={"status": "bogus"}).status_code == 400
        assert cl.get("/api/leads/99999").status_code == 404
        # import
        r = cl.post("/api/leads/import", json={"csv": "Business Name,Contact Info\nImp One,0733000111\nApi Pharmacy,0712000111\n"})
        assert r.json()["created"] == 1 and r.json()["duplicates"] == 1
        # manual reply logging + grading
        r = cl.post(f"/api/leads/{lid}/reply", json={"text": "Interested! how much?", "channel": "sms"})
        assert r.json()["grade"] == "hot"
        inbox = cl.get("/api/inbox").json()
        assert len(inbox) == 1 and inbox[0]["grade"] == "hot"
        mid = inbox[0]["id"]
        assert cl.post(f"/api/messages/{mid}/regrade", json={"grade": "warm"}).status_code == 200
        assert cl.get(f"/api/leads/{lid}").json()["grade"] == "warm"
        cl.post(f"/api/messages/{mid}/handled", json={"handled": True})
        assert cl.get("/api/inbox", params={"handled": False}).json() == []
        # bulk + archive
        r = cl.post("/api/leads/bulk", json={"ids": [lid], "action": "archive"})
        assert r.status_code == 200
        assert cl.get("/api/leads").json()["total"] == 1
        assert cl.get("/api/leads", params={"archived": True}).json()["total"] == 1
        assert cl.post("/api/leads", json={"name": "Api Pharmacy", "town": "Meru", "phone": "0712000111"}).status_code == 409
        # export
        r = cl.get("/api/leads/export")
        assert r.status_code == 200 and b"Imp One" in r.content
        # settings: secrets masked, blank leaves unchanged
        r = cl.put("/api/settings", json={"smtp_host": "h.test", "smtp_pass": "s3cret", "email_daily_cap": "40"})
        j = r.json()
        assert j["smtp_pass"] == "" and j["smtp_pass_set"] is True and j["email_daily_cap"] == 40
        cl.put("/api/settings", json={"smtp_pass": ""})
        assert db.get_settings()["smtp_pass"] == "s3cret"
        assert cl.put("/api/settings", json={"send_window_start": "8am"}).status_code == 400
        assert cl.put("/api/settings", json={"email_daily_cap": "abc"}).status_code == 400
        # branding: slogan trimmed and capped, logo validated, reset falls back to the default
        assert cl.put("/api/settings", json={"slogan": "  Data that sells  "}).json()["slogan"] == "Data that sells"
        assert cl.put("/api/settings", json={"slogan": "x" * 81}).status_code == 400
        default_logo = cl.get("/api/branding/logo").content
        assert cl.put("/api/branding/logo", content=b"not an image").status_code == 400
        fake_png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        assert cl.put("/api/branding/logo", content=fake_png).status_code == 200
        r = cl.get("/api/branding/logo")
        assert r.content == fake_png and r.headers["content-type"] == "image/png"
        assert cl.delete("/api/branding/logo").status_code == 200
        assert cl.get("/api/branding/logo").content == default_logo != fake_png
        # campaign through the API
        configure()
        for i in range(3):
            cl.post("/api/leads", json={"name": f"Camp {i}", "town": "Embu", "sector": "Chemist", "phone": f"0744000{i:03d}"})
        pv = cl.post("/api/campaigns/preview", json={"channel": "sms", "body": "Hi {name}", "filters": {"towns": ["Embu"]}}).json()
        assert pv["matching"] == 3 and pv["channel_ready"]
        r = cl.post("/api/campaigns", json={"name": "API SMS", "channel": "sms", "body": "Hi {name}", "filters": {"towns": ["Embu"]}, "launch": True})
        assert r.json()["status"] == "running" and r.json()["total"] == 3
        cid = r.json()["id"]
        assert cl.post(f"/api/campaigns/{cid}/pause").json()["status"] == "paused"
        assert cl.post(f"/api/campaigns/{cid}/resume").json()["status"] == "running"
        det = cl.get(f"/api/campaigns/{cid}").json()
        assert len(det["messages"]) == 3
        # a phone number typed in any common format finds the lead saved as +254744000001
        for typed in ("0744000001", "0744 000 001", "+254 744 000 001", "254744000001", "744000001"):
            hits = cl.get("/api/leads", params={"q": typed}).json()["leads"]
            assert [h["name"] for h in hits] == ["Camp 1"], (typed, [h["name"] for h in hits])
        assert cl.get("/api/leads", params={"q": "Camp"}).json()["total"] == 3      # text search still works
        # dashboard / status / facets
        dash = cl.get("/api/dashboard").json()
        assert dash["total"] >= 4 and "by_status" in dash
        # reply rate counts only leads we actually sent to, so it can never pass 100%
        with db.db() as c:
            stray = c.execute("INSERT INTO leads(name, name_key, created_at, updated_at) VALUES('Never contacted','never contacted|',?,?)",
                              (now(), now())).lastrowid
            c.execute("INSERT INTO messages(lead_id, direction, channel, status, grade, created_at) VALUES(?,?,?,?,?,?)",
                      (stray, "in", "email", "received", "warm", now()))
        d2 = cl.get("/api/dashboard").json()
        assert d2["replied"] == dash["replied"] and d2["reply_rate"] <= 100, (dash["replied"], d2["replied"], d2["reply_rate"])
        st = cl.get("/api/status").json()
        assert "sender" in st and st["email_ready"] is False or True
        assert "Embu" in cl.get("/api/facets").json()["towns"]
        # webhook
        db.save_settings({"webhook_token": "tok"})
        assert cl.post("/api/webhooks/sms?token=bad", content="from=%2B254744000000&text=STOP").status_code == 403
        r = cl.post("/api/webhooks/sms?token=tok", content="from=%2B254744000000&text=STOP",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.json()["matched"]
        with db.db() as c:
            assert c.execute("SELECT do_not_contact FROM leads WHERE phone_norm='+254744000000'").fetchone()[0] == 1
        # backup is a valid sqlite file
        r = cl.get("/api/backup")
        assert r.status_code == 200 and r.content[:15] == b"SQLite format 3"
        # jobs: validation + single-job rule
        assert cl.post("/api/jobs/scrape", json={"searches": []}).status_code == 400
        # static front end (if present)
        if (ROOT / "backend" / "static" / "index.html").exists():
            assert cl.get("/").status_code == 200


# ================================================ REAL BROWSER vs MOCK MAPS PAGE
from mock_maps import start_mock_maps  # noqa: E402


@test
def real_browser_driver_against_mock_maps():
    fresh()
    srv = start_mock_maps()
    url = f"http://127.0.0.1:{srv.server_port}/maps/search/"
    sp = {"searches": [{"category": "Pharmacy", "town": "Meru", "query": "Pharmacy in Meru"}], "max_per_search": 100}
    with db.db() as c:
        jid = jobs.create_job(c, "scrape", sp)
    final = scraper.run_scrape(jid, sp, driver_factory=lambda: scraper.MapsDriver(headless=True, search_url=url),
                               sleep=lambda s: None)
    assert final == "done", final
    with db.db() as c:
        j = jobs.get_job(c, jid)
        assert j["added"] == 10 and j["found"] == 10 and j["errors"] == 0, j
        r = c.execute("SELECT * FROM leads WHERE name='Shop 7'").fetchone()
        assert r["phone_norm"] == "+254712000007" and r["address"] == "7 Main St, Meru"
        assert r["website"] == "http://shop7.co.ke" and r["town"] == "Meru" and r["place_key"] == "0x1:0x7"
        phones = {x[0] for x in c.execute("SELECT phone_norm FROM leads")}
        assert len(phones) == 10, "every lead must carry its own phone (no stale detail panel)"
    with db.db() as c:
        jid2 = jobs.create_job(c, "scrape", sp)
    scraper.run_scrape(jid2, sp, driver_factory=lambda: scraper.MapsDriver(headless=True, search_url=url), sleep=lambda s: None)
    with db.db() as c:
        j2 = jobs.get_job(c, jid2)
        assert j2["added"] == 0 and j2["duplicates"] == 10, j2
        assert c.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 10
    srv.shutdown()


@test
def all_searches_failing_marks_job_failed():
    fresh()
    sp = {"searches": [{"category": "X", "town": "Y", "query": "X in Y"}], "max_per_search": 1}
    class Broken(FakeDriver):
        def search(self, q):
            raise RuntimeError("no network")
    with db.db() as c:
        jid = jobs.create_job(c, "scrape", sp)
    final = scraper.run_scrape(jid, sp, driver_factory=lambda: Broken({}, {}), sleep=lambda s: None)
    assert final == "failed"


# ======================================================================== AI
@test
def ai_drafts_providers_and_errors():
    """Drafts come back from an OpenAI-style server; failures give a readable 502, not a crash."""
    from fastapi.testclient import TestClient
    import server
    fresh()
    seen = []
    mode = {"status": 200, "text": "Subject: Hello {name}\n\nHi {name}, a quick idea for {town}."}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            out = json.dumps({"choices": [{"message": {"content": "<think>hmm</think>" + mode["text"]}}]}).encode()
            self.send_response(mode["status"]); self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out)
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
            r = cl.post("/api/ai/template", json={"channel": "sms"})
            assert r.status_code == 502 and "key" in r.json()["detail"].lower()
            assert cl.put("/api/settings", json={"ai_provider": "nope"}).status_code == 400
            assert cl.put("/api/settings", json={"ai_custom_url": "ftp://x"}).status_code == 400
            cl.put("/api/settings", json={"ai_provider": "custom", "ai_custom_url": f"http://127.0.0.1:{srv.server_port}",
                                          "ai_custom_model": "m1", "ai_pitch": "Pharmacy stock software"})
            r = cl.post("/api/ai/template", json={"channel": "email", "notes": "mention a demo"}).json()
            assert r["subject"] == "Hello {name}" and r["body"].startswith("Hi {name}"), r
            assert "Pharmacy stock software" in seen[-1]["messages"][1]["content"] and seen[-1]["model"] == "m1"
            assert cl.post("/api/ai/template", json={"channel": "fax"}).status_code == 400
            lid = cl.post("/api/leads", json={"name": "Reply Chem", "town": "Embu", "phone": "0755000111"}).json()["id"]
            cl.post(f"/api/leads/{lid}/reply", json={"text": "How much is it?", "channel": "sms"})
            r = cl.post(f"/api/leads/{lid}/ai-draft", json={"channel": "sms"}).json()
            assert r["subject"] == "" and "Them: How much is it?" in seen[-1]["messages"][1]["content"]
            assert cl.post("/api/leads/99999/ai-draft", json={"channel": "sms"}).status_code == 404
            cl.patch(f"/api/leads/{lid}", json={"do_not_contact": True})
            assert cl.post(f"/api/leads/{lid}/ai-draft", json={"channel": "sms"}).status_code == 400
            mode["status"] = 429
            r = cl.post("/api/settings/test-ai")
            assert r.status_code == 200 and r.json()["ok"] is False and "limit" in r.json()["message"]
            assert cl.post("/api/ai/template", json={"channel": "sms"}).status_code == 502
            mode["status"] = 200
            assert cl.post("/api/settings/test-ai").json()["ok"] is True
    finally:
        srv.shutdown()


def _ai_server(answer):
    """Tiny OpenAI-style server. `answer` is a dict {"text": ..., "status": 200}; every request body is logged in `seen`."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            out = json.dumps({"choices": [{"message": {"content": answer["text"]}}]}).encode()
            self.send_response(answer.get("status", 200)); self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out)
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen

@test
def ai_plans_searches_from_a_conversation():
    """The owner describes who to reach; the AI answers with clean, de-duplicated searches the finder can run."""
    import ai
    from fastapi.testclient import TestClient
    import server
    # --- reading the model's answer: fences, chatter, junk entries, duplicates, a hard cap
    ok = ai.parse_plan('Sure!\n```json\n{"reply": " Pharmacies  first. ", "searches": ['
                       '{"category": "pharmacy", "town": "kisii"}, {"category": "Pharmacy", "town": "Kisii"},'
                       '{"category": "Clinic", "town": ""}, {"category": "x", "town": "Migori"}, "junk", {"category": "Clinic", "town": "Migori"}]}\n```')
    assert ok["reply"] == "Pharmacies first."
    assert ok["searches"] == [{"category": "Pharmacy", "town": "Kisii"}, {"category": "Clinic", "town": "Migori"}], ok
    many = json.dumps({"reply": "", "searches": [{"category": f"Shop {i}", "town": "Embu"} for i in range(80)]})
    assert len(ai.parse_plan(many)["searches"]) == ai.MAX_PLAN
    assert ai.parse_plan('{"reply": "Who do you want to reach?", "searches": []}')["searches"] == []
    for bad in ("", "no json here", "{broken", "[1, 2]"):
        try:
            ai.parse_plan(bad)
            assert False, bad
        except ai.AIError:
            pass
    # --- through the API
    fresh()
    answer = {"text": json.dumps({"reply": "Pharmacies and clinics in both towns.", "searches": [
        {"category": "Pharmacy", "town": "Kisii"}, {"category": "Pharmacy", "town": "Migori"},
        {"category": "Clinic", "town": "Kisii"}]})}
    srv, seen = _ai_server(answer)
    try:
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
            r = cl.post("/api/ai/plan-searches", json={"goal": "pharmacies in Kisii"})
            assert r.status_code == 502 and "key" in r.json()["detail"].lower()     # no AI key yet: a readable message
            cl.put("/api/settings", json={"ai_provider": "custom", "ai_custom_url": f"http://127.0.0.1:{srv.server_port}",
                                          "ai_custom_model": "m1", "ai_pitch": "Pharmacy billing software"})
            assert cl.post("/api/ai/plan-searches", json={"goal": "   "}).status_code == 400
            hist = [{"role": "user", "text": "I sell to chemists"}, {"role": "ai", "text": "Which towns?"}]
            r = cl.post("/api/ai/plan-searches", json={"goal": "Kisii and Migori, also clinics", "history": hist})
            assert r.status_code == 200, r.text
            assert [(x["category"], x["town"]) for x in r.json()["searches"]] == [("Pharmacy", "Kisii"), ("Pharmacy", "Migori"), ("Clinic", "Kisii")]
            assert r.json()["reply"].startswith("Pharmacies and clinics")
            prompt = seen[-1]["messages"][1]["content"]
            assert "Pharmacy billing software" in prompt and "Owner: I sell to chemists" in prompt \
                and "You: Which towns?" in prompt and "Kisii and Migori, also clinics" in prompt, prompt
            answer["text"] = "I cannot help with that"
            assert cl.post("/api/ai/plan-searches", json={"goal": "x"}).status_code == 502   # unreadable answer: clear error, no crash
            answer["status"] = 429
            assert cl.post("/api/ai/plan-searches", json={"goal": "x"}).status_code == 502
    finally:
        srv.shutdown()



@test
def ai_grades_unclear_replies_and_summarises():
    import ai
    fresh()
    answer = {"text": "Hot."}
    srv, seen = _ai_server(answer)
    try:
        with db.db() as c:
            lid = L.add_lead(c, {"name": "Grade Me", "phone": "0766000111"}, source="manual", merge=False)["id"]
            assert L.record_reply(c, lid, "sms", "zzz qqq")["grade"] == "unclear" and not seen   # no key: rules only
        db.save_settings({"ai_provider": "custom", "ai_custom_url": f"http://127.0.0.1:{srv.server_port}", "ai_custom_model": "m"})
        with db.db() as c:
            r = L.record_reply(c, lid, "sms", "zzz qqq")
            assert r["grade"] == "hot" and r["reasons"] == ["AI"], r
            r = L.record_reply(c, lid, "sms", "not interested at all")    # the rules already know this one
            assert r["grade"] == "cold" and len(seen) == 1
        answer["text"] = "banana"                                          # unusable answer: fall back to the rules
        with db.db() as c:
            assert L.record_reply(c, lid, "sms", "zzz qqq")["grade"] == "unclear"
        answer["text"] = "hot"
        db.save_settings({"ai_grade_replies": False})
        with db.db() as c:
            assert L.record_reply(c, lid, "sms", "zzz qqq")["grade"] == "unclear"
        # summary needs a conversation
        from fastapi.testclient import TestClient
        import server
        answer["text"] = "They wanted details. Send a demo."
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
            assert cl.post(f"/api/leads/{lid}/ai-summary").json()["summary"].startswith("They wanted")
            empty = cl.post("/api/leads", json={"name": "Quiet One", "phone": "0766000222"}).json()["id"]
            assert cl.post(f"/api/leads/{empty}/ai-summary").status_code == 400
            assert cl.post("/api/leads/99999/ai-summary").status_code == 404
        # the time budget is a hard stop: nothing is sent once it has run out
        n = len(seen)
        try:
            ai.ask(db.get_settings(), "s", "u", budget=0)
            assert False, "should have run out of time"
        except ai.AIError as e:
            assert "out of time" in str(e) and len(seen) == n
    finally:
        srv.shutdown()


@test
def missing_logo_and_empty_slogan_never_break_the_app():
    from fastapi.testclient import TestClient
    import server
    fresh()
    real = config.static_dir
    config.static_dir = lambda: Path(TMP) / "nowhere"
    try:
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
            assert cl.get("/api/branding/logo").status_code == 404          # the page just hides the image
            assert cl.get("/api/settings").json()["slogan"] == ""
            assert cl.get("/api/health").status_code == 200 and cl.get("/api/dashboard").status_code == 200
            png = b"\x89PNG\r\n\x1a\n" + b"x" * 16
            assert cl.put("/api/branding/logo", content=png).status_code == 200
            assert cl.get("/api/branding/logo").content == png               # a chosen logo works without the bundled one
            assert cl.delete("/api/branding/logo").status_code == 200 and cl.delete("/api/branding/logo").status_code == 200
    finally:
        config.static_dir = real


# ============================================================ WORKER PROCESS
@test
def worker_subprocess_runs_and_reports():
    """Spawn the real worker process with a bogus Playwright setup: it must fail cleanly, not hang."""
    if os.environ.get("CI"):
        return  # needs a machine that cannot reach Google; CI runners can
    import subprocess
    fresh()
    sp = {"searches": [{"category": "X", "town": "Y", "query": "X in Y"}], "max_per_search": 1, "headless": True}
    with db.db() as c:
        jid = jobs.create_job(c, "scrape", sp)
    env = dict(os.environ, DERICBI_DATA=TMP, DERICBI_PARENT=str(os.getpid()))
    p = subprocess.run([sys.executable, str(ROOT / "backend" / "main.py"), "worker", "scrape", str(jid)],
                       env=env, capture_output=True, text=True, timeout=120)
    with db.db() as c:
        j = jobs.get_job(c, jid)
    # Offline it fails; online it finds nothing for "X in Y". Either way it must finish and say what happened.
    assert j["status"] in ("failed", "done") and j["finished_at"], (j, p.stdout[-500:], p.stderr[-500:])
    if j["status"] == "done":
        assert "no listings were found" in j["progress"], j["progress"]
    print("   worker finished with status:", j["status"], "| progress:", j["progress"])


if __name__ == "__main__":
    SMTP.stop()
    AT.shutdown()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    sys.exit(1 if bad else 0)
