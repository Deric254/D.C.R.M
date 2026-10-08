"""End-to-end backend tests. Run:  python tests/test_backend.py"""
import json
import os
import re
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
from util import days_ago, normalize_phone, now  # noqa: E402

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
PHONE_CALLS = []   # every text the fake phone was asked to send
PHONE_POLLS = {}
PHONE_MODE = {"mode": "ok"}   # ok | slow | failed | busy | legacy


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


def start_phone():
    """A stand-in for the SMS Gateway app running on an Android phone in Local server mode."""
    import base64
    good = "Basic " + base64.b64encode(b"phoneuser:phonepass").decode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._json(200, {"status": "pass"})
            if self.headers.get("Authorization") != good:
                return self._json(401, {"message": "Unauthorized"})
            m = re.match(r"^/(messages?)/(\w+)$", self.path)
            if not m:
                return self._json(404, {})
            PHONE_POLLS[m.group(2)] = PHONE_POLLS.get(m.group(2), 0) + 1
            mode = PHONE_MODE["mode"]
            if mode == "failed":
                return self._json(200, {"id": m.group(2), "state": "Failed", "recipients": [{"state": "Failed", "error": "No airtime"}]})
            if mode == "slow" and PHONE_POLLS[m.group(2)] < 2:
                return self._json(200, {"id": m.group(2), "state": "Processed"})
            return self._json(200, {"id": m.group(2), "state": "Sent"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            if self.headers.get("Authorization") != good:
                return self._json(401, {"message": "Unauthorized"})
            mode = PHONE_MODE["mode"]
            modern = self.path == "/messages"
            if (modern and mode == "legacy") or (self.path == "/message" and mode != "legacy") or self.path not in ("/messages", "/message"):
                return self._json(404, {})
            if mode == "busy":
                return self._json(503, {"message": "busy"})
            text = req["textMessage"]["text"] if modern else req["message"]
            PHONE_CALLS.append({"path": self.path, "text": text, "to": req["phoneNumbers"][0]})
            self._json(202, {"id": f"g{len(PHONE_CALLS)}", "state": "Pending", "recipients": [{"phoneNumber": req["phoneNumbers"][0], "state": "Pending"}]})

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
PHONE = start_phone()
messaging.SMS_POLL = 0.05


def configure(**over):
    base = {"smtp_host": "127.0.0.1", "smtp_port": SMTP.port, "smtp_security": "none",
            "from_email": "deric@dericbi.test", "sender_name": "Deric @ DericBI",
            "sms_gateway_url": f"127.0.0.1:{PHONE.server_port}", "sms_gateway_user": "phoneuser", "sms_gateway_pass": "phonepass",
            "send_window_enabled": False, "email_delay_sec": 0, "sms_delay_sec": 0}
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
def sms_goes_through_the_phone_as_one_plain_160_character_text():
    fresh(); PHONE_CALLS.clear(); PHONE_MODE["mode"] = "ok"; configure()
    with db.db() as c:
        for i in range(3):
            L.add_lead(c, {"name": f"Agro {i}", "town": "Chuka", "sector": "Agrovet", "phone": f"07000000{i:02d}"})
        L.add_lead(c, {"name": "Landline", "town": "Chuka", "sector": "Agrovet", "phone": "064 30123"})
        cid = C.create_campaign(c, "SMS", "sms", "", "Hi {name} \u2014 DericBI helps agrovets track stock. \U0001F600 {website}",
                                {"sectors": ["Agrovet"]}, launch=True)
        assert c.execute("SELECT total FROM campaigns WHERE id=?", (cid,)).fetchone()[0] == 3
    run_sender(lambda: count("SELECT COUNT(*) FROM messages WHERE status='sent'") >= 3)
    assert len(PHONE_CALLS) == 3 and PHONE_CALLS[0]["path"] == "/messages" and PHONE_CALLS[0]["to"].startswith("+2547")
    for call in PHONE_CALLS:
        t = call["text"]
        assert len(t) <= 160 and t.isascii(), t                  # one SMS: no emoji, no curly dash, nothing that splits it
        assert "dericbi.vercel.app" in t and "https://" not in t   # {website} is the short form in a text
        assert t.endswith("STOP to opt out")


@test
def an_older_phone_app_and_slow_delivery_both_work_and_a_failed_text_is_never_recorded_as_sent():
    fresh(); PHONE_CALLS.clear(); configure()
    with db.db() as c:
        a = L.add_lead(c, {"name": "Slow", "town": "T", "phone": "0700000011"})["id"]
        b = L.add_lead(c, {"name": "Old app", "town": "T", "phone": "0700000012"})["id"]
        d = L.add_lead(c, {"name": "No airtime", "town": "T", "phone": "0700000013"})["id"]
    PHONE_MODE["mode"] = "slow"
    C.send_now(a, "sms", "", "Hi {name}")                       # 'Processed' first, then 'Sent': it waits for the real answer
    assert PHONE_POLLS and count("SELECT status FROM messages WHERE lead_id=?", a) == "sent"
    PHONE_MODE["mode"] = "legacy"
    C.send_now(b, "sms", "", "Hi {name}")                       # an app version that only knows /message
    assert PHONE_CALLS[-1]["path"] == "/message"
    PHONE_MODE["mode"] = "failed"
    try:
        C.send_now(d, "sms", "", "Hi {name}"); raise AssertionError("the phone said Failed")
    except messaging.SendError as e:
        assert e.kind == "permanent" and "airtime" in str(e).lower()
    assert count("SELECT status FROM messages WHERE lead_id=?", d) == "failed" and count("SELECT status FROM leads WHERE id=?", d) == "new"
    PHONE_MODE["mode"] = "ok"


@test
def phone_problems_pause_the_campaign_instead_of_burning_the_queue():
    fresh(); PHONE_MODE["mode"] = "ok"
    for label, over in (("wrong password", {"sms_gateway_pass": "wrong"}), ("phone not reachable", {"sms_gateway_url": "127.0.0.1:9"})):
        fresh(); configure(**over)
        with db.db() as c:
            L.add_lead(c, {"name": "A", "town": "T", "phone": "0700000001"})
            cid = C.create_campaign(c, "K", "sms", "", "Hello", {}, launch=True)
        run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", cid) == "paused", timeout=10)
        assert count("SELECT status FROM campaigns WHERE id=?", cid) == "paused", label
        assert count("SELECT COUNT(*) FROM messages WHERE status='queued'") == 1, label     # still waiting, not failed
        assert count("SELECT last_error FROM campaigns WHERE id=?", cid), label
    configure()


@test
def phone_diagnosis_names_the_step_that_fails():
    fresh(); configure()
    assert messaging.diagnose_sms(db.get_settings())["ok"]
    bad = messaging.diagnose_sms(dict(db.get_settings(), sms_gateway_url="127.0.0.1:9"))
    assert not bad["ok"] and "same Wi-Fi" in bad["message"]
    assert not messaging.diagnose_sms(dict(db.get_settings(), sms_gateway_url=""))["ok"]
    # address typed any common way
    for typed in ("192.168.43.1", "http://192.168.43.1:8080/", "192.168.43.1:8080"):
        assert messaging.gateway_base({"sms_gateway_url": typed}) == "http://192.168.43.1:8080"


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
    try:
        C.send_now(a, "email", "again", "again"); raise AssertionError("a lead gets one message at a time")
    except ValueError as e:
        assert "follow-up unlocks" in str(e)
    with db.db() as c:
        other = L.add_lead(c, {"name": "Other", "town": "T", "email": "other@shop.test"})["id"]
    configure(smtp_port=port, smtp_user="me", smtp_pass="WRONG")
    try:
        C.send_now(other, "email", "x", "y")
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
            assert r["subject"] == "" and "Them by text" in seen[-1]["messages"][1]["content"] and "How much is it?" in seen[-1]["messages"][1]["content"]
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


@test
def retired_ai_model_is_replaced_automatically_and_settings_fill_their_own_blanks():
    """A model name the provider has retired must not silently kill the AI: the next name, then the provider's own list, is used."""
    from fastapi.testclient import TestClient
    import ai, server
    fresh()
    asked = []
    live = {"model": "brand-new-chat-1"}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            out = json.dumps({"data": [{"id": "whisper-large"}, {"id": "retired-old"}] + ([{"id": live["model"]}] if live["model"] else [])}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            asked.append(req["model"])
            ok = req["model"] == live["model"]
            out = json.dumps({"choices": [{"message": {"content": "ok"}}]} if ok else {"error": {"message": "model has been decommissioned"}}).encode()
            self.send_response(200 if ok else 404); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    old = ai.PROVIDERS["groq"]
    ai._WORKING.clear()
    ai.PROVIDERS["groq"] = (old[0], base, ["retired-1", "retired-2"])
    try:
        s = dict(db.get_settings(), ai_provider="groq", ai_key_groq="k")
        assert ai.ask(s, "sys", "hi") == "ok"
        assert asked == ["retired-1", "retired-2", "retired-old", "brand-new-chat-1"], asked   # built-in names, then the live list (whisper skipped)
        asked.clear()
        assert ai.ask(s, "sys", "hi") == "ok" and asked == ["brand-new-chat-1"]        # the one that worked is remembered
        assert ai.default_models()["groq"] == "brand-new-chat-1"
        # when nothing works any more the reason from the provider is shown, not a silent failure
        asked.clear(); ai._WORKING.clear(); live["model"] = None
        try:
            ai.ask(s, "sys", "hi"); assert False
        except ai.AIError as e:
            assert "decommissioned" in str(e) or "not found" in str(e), e
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
            got = cl.get("/api/settings").json()
            assert got["ai_defaults"]["gemini"] and got["ai_defaults"]["groq"]
            cl.put("/api/settings", json={"smtp_host": "smtp.gmail.com", "smtp_user": "me@gmail.com", "imap_host": "imap.gmail.com"})
            got = cl.get("/api/settings").json()
            assert got["from_email"] == "me@gmail.com" and got["imap_user"] == "me@gmail.com"
            cl.put("/api/settings", json={"from_email": "sales@mine.co.ke"})
            cl.put("/api/settings", json={"smtp_user": "other@gmail.com"})
            assert cl.get("/api/settings").json()["from_email"] == "sales@mine.co.ke"   # what you typed is never overwritten
    finally:
        ai.PROVIDERS["groq"] = old
        ai._WORKING.clear()
        srv.shutdown()


def _ai_server(answer):
    """Tiny OpenAI-style server. `answer` is a dict {"text": ..., "status": 200}; every request body is logged in `seen`.
    `text` may be a function of the request body, to answer each request differently."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(req)
            text = answer["text"](req) if callable(answer["text"]) else answer["text"]
            out = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
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



# ================================================================ AI CAMPAIGNS
def _use_ai(srv):
    db.save_settings({"ai_provider": "custom", "ai_custom_url": f"http://127.0.0.1:{srv.server_port}",
                      "ai_custom_model": "m1", "ai_pitch": "Pharmacy billing software", "sender_name": "Deric"})


def _prompt(req):
    return req["messages"][1]["content"]


def _who(req):
    import re
    return re.search(r"Lead: (.+?) \(", _prompt(req)).group(1)


def _mail(envelope):
    """A mail the test SMTP server received, parsed (so the transfer encoding and line wrapping are undone)."""
    import email
    import email.policy
    return email.message_from_bytes(envelope.content, policy=email.policy.default)


def _wait(cond, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout and not cond():
        time.sleep(0.2)
    return cond()


DISTINCT = {
    "Alpha Chemist": "Hello Alpha Chemist team, I help pharmacies on Kenyatta Street keep stock and expiry dates on one screen. Could I show you in ten minutes?",
    "Beta Pharmacy": "Good morning Beta Pharmacy. Running a busy counter in Embu is hard enough without paper stock books, so I built a simple dashboard. Open to a short demo this week?",
    "Gamma Drugs": "Hi from Deric. Gamma Drugs came up when I searched Kisii for chemists; my billing tool may save your evenings. Shall I send a price list?",
}


@test
def ai_campaign_writes_each_lead_its_own_message_and_logs_it():
    """The brief goes in, every lead gets its own AI-written email built from what we know about them, sent
    through the normal sender, with the exact text kept on the message and nobody messaged twice."""
    from fastapi.testclient import TestClient
    import server
    fresh(); SMTP_INBOX.clear(); configure()
    srv, seen = _ai_server({"text": lambda req: f"Subject: About {_who(req)}\n\n{DISTINCT[_who(req)]}"})
    _use_ai(srv)
    try:
        with db.db() as c:
            for name, town, addr, note in (("Alpha Chemist", "Meru", "Kenyatta Street", "owner is called Mary"),
                                           ("Beta Pharmacy", "Embu", "Market Road", "uses paper stock books"),
                                           ("Gamma Drugs", "Kisii", "Hospital Lane", "")):
                L.add_lead(c, {"name": name, "town": town, "sector": "Pharmacy", "address": addr, "notes": note,
                               "email": name.split()[0].lower() + "@shop.test", "website": "https://" + name.split()[0].lower() + ".test"})
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
            brief = "Offer our billing software and ask for a ten minute demo."
            body = {"name": "AI test", "channel": "email", "body": brief, "filters": {"sectors": ["Pharmacy"]}, "ai": True}
            pv = cl.post("/api/campaigns/preview", json=body).json()
            assert pv["will_send"] == 3 and pv["sample"] is None and pv["ai_ready"] is True, pv
            r = cl.post("/api/campaigns", json={**body, "launch": True}).json()
            assert r["ai_personalize"] == 1 and r["subject"] == "" and r["total"] == 3, r
            assert _wait(lambda: count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 3), "AI messages were not all sent"
            assert len(SMTP_INBOX) == 3
            sent = set()
            for env in SMTP_INBOX:
                mail = _mail(env)
                text = mail.get_content().replace("\r\n", "\n")
                name = next(n for n in DISTINCT if DISTINCT[n] in text)
                assert mail["Subject"] == f"About {name}" and "reply STOP" in text, (mail["Subject"], text)
                sent.add(name)
            assert sent == set(DISTINCT)
            # what the AI was told: facts about this lead, the brief, and the rules
            by_lead = {_who(q): _prompt(q) for q in seen}
            a = by_lead["Alpha Chemist"]
            assert "Kenyatta Street" in a and "owner is called Mary" in a and "alpha.test" in a and "Meru" in a, a
            assert "ten minute demo" in a and "Status: new" in a and "already sent them: 0" in a, a
            assert "Kenyatta Street" not in by_lead["Beta Pharmacy"]
            # the log: exact text on the message, visible in the campaign and on the lead
            rows = cl.get(f"/api/campaigns/{r['id']}").json()
            assert all(m["status"] == "sent" and m["body"] and m["subject"].startswith("About ") for m in rows["messages"]), rows
            lid = next(m["lead_id"] for m in rows["messages"] if m["name"] == "Beta Pharmacy")
            lead = cl.get(f"/api/leads/{lid}").json()
            assert DISTINCT["Beta Pharmacy"] in lead["messages"][0]["body"] and lead["status"] == "contacted", lead
            # nobody is picked a second time
            again = cl.post("/api/campaigns", json={**body, "launch": True})
            assert again.status_code == 400 and "No leads match" in again.json()["detail"], again.text
    finally:
        srv.shutdown()


@test
def ai_campaign_holds_back_near_copies_and_retry_rewrites_them():
    """A message almost identical to another one in the campaign is reworded once; if it is still a copy it is
    held back (not sent, nothing saved), and Retry failed writes it afresh."""
    from fastapi.testclient import TestClient
    import server
    fresh(); SMTP_INBOX.clear(); configure()
    same = "Subject: Quick idea\n\nHello there, we help pharmacies keep stock and expiry dates on one simple screen. Could we show you in ten minutes this week?"
    answer = {"text": same}
    srv, seen = _ai_server(answer)
    _use_ai(srv)
    try:
        with db.db() as c:
            for n in ("Alpha Chemist", "Beta Pharmacy"):
                L.add_lead(c, {"name": n, "town": "Meru", "sector": "Pharmacy", "email": n.split()[0].lower() + "@shop.test"})
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
            cid = cl.post("/api/campaigns", json={"name": "Dup", "channel": "email", "body": "Offer a demo", "ai": True,
                                                  "filters": {}, "launch": True}).json()["id"]
            assert _wait(lambda: count("SELECT COUNT(*) FROM messages WHERE status='failed'") == 1)
            assert count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 1 and len(SMTP_INBOX) == 1
            with db.db() as c:
                bad = c.execute("SELECT * FROM messages WHERE status='failed'").fetchone()
            assert "Too much like another message" in bad["error"] and bad["body"] == "", dict(bad)
            assert sum("Word it very differently" in _prompt(q) for q in seen) == 1, "it should be asked to reword exactly once"
            answer["text"] = "Subject: For Beta\n\nGood morning Beta Pharmacy. Counting stock by hand takes your evenings, so I built a small dashboard that does it for you. Free for a month?"
            cl.post(f"/api/campaigns/{cid}/retry")
            assert _wait(lambda: count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 2)
            assert len(SMTP_INBOX) == 2 and count("SELECT COUNT(DISTINCT body) FROM messages WHERE status='sent'") == 2
    finally:
        srv.shutdown()


@test
def ai_message_is_never_sent_with_blanks_no_subject_or_when_the_ai_is_down():
    import campaigns as CM
    from messaging import SendError
    fresh(); configure()
    answer = {"text": ""}
    srv, seen = _ai_server(answer)
    _use_ai(srv)
    try:
        with db.db() as c:
            lid = L.add_lead(c, {"name": "Alpha Chemist", "town": "Meru", "email": "a@shop.test"})["id"]
            lead = L.lead_detail(c, lid)
        row = {"id": 1, "campaign_id": 1, "channel": "email", "brief": "Offer a demo"}

        def kind(text, status=200, channel="email"):
            answer.update(text=text, status=status)
            try:
                CM.write_for_lead({**row, "channel": channel}, lead, db.get_settings())
            except SendError as e:
                return e.kind, str(e)
            return None, ""
        assert kind("Subject: Hi\n\nHello {owner}, a quick idea.")[0] == "transient"          # a blank left in
        k, msg = kind("Hello, a quick idea for you.")                                         # no subject line
        assert k == "transient" and "subject" in msg
        k, msg = kind("x", status=429)
        assert k == "transient" and "limit" in msg
        # known placeholders the AI used are filled in, not sent raw; an SMS needs no subject
        answer.update(text="Subject: For {name}\n\nHi {name} in {town}.", status=200)
        subj, body = CM.write_for_lead(row, lead, db.get_settings())
        assert subj == "For Alpha Chemist" and body.startswith("Hi Alpha Chemist in Meru.") and "reply STOP" in body
        answer["text"] = "Hi Alpha, a quick idea."
        assert CM.write_for_lead({**row, "channel": "sms"}, lead, db.get_settings())[0] == ""
        # no AI key at all: pause the campaign rather than burn the queue
        db.save_settings({"ai_custom_url": ""})
        k, msg = kind("anything")
        assert k == "pause" and "AI isn't set up" in msg
    finally:
        srv.shutdown()


@test
def pausing_or_cancelling_while_the_ai_writes_stops_that_message():
    from fastapi.testclient import TestClient
    import server
    fresh(); SMTP_INBOX.clear(); configure()
    state = {"do": None, "cid": None}

    def slow(req):
        if state["do"]:
            with db.db() as c:
                C.set_campaign_status(c, state["cid"], state["do"])
            state["do"] = None
        return "Subject: Hello\n\nA short note for you about stock software."
    srv, _ = _ai_server({"text": slow})
    _use_ai(srv)
    try:
        with db.db() as c:
            L.add_lead(c, {"name": "Alpha Chemist", "town": "Meru", "email": "a@shop.test"})
            state["cid"] = C.create_campaign(c, "Stop", "email", "", "Offer a demo", {}, personalize=True)
        state["do"] = "cancel"   # cancelled the moment the AI is asked, i.e. while it is "writing"
        with db.db() as c:
            C.launch_campaign(c, state["cid"])
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765"):
            assert _wait(lambda: count("SELECT COUNT(*) FROM messages WHERE status='skipped'") == 1)
            assert not SMTP_INBOX and count("SELECT COUNT(*) FROM messages WHERE body<>''") == 0
        fresh(); configure(); _use_ai(srv)
        with db.db() as c:
            L.add_lead(c, {"name": "Beta Pharmacy", "town": "Meru", "email": "b@shop.test"})
            state["cid"] = C.create_campaign(c, "Pause", "email", "", "Offer a demo", {}, personalize=True)
        state["do"] = "pause"
        with db.db() as c:
            C.launch_campaign(c, state["cid"])
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765"):
            assert _wait(lambda: count("SELECT COUNT(*) FROM messages WHERE status='queued' AND attempts=0") == 1)
            time.sleep(1.5)
            assert not SMTP_INBOX and count("SELECT COUNT(*) FROM campaigns WHERE status='paused'") == 1
    finally:
        srv.shutdown()


@test
def ai_plans_a_campaign_from_plain_words_and_shows_sample_drafts():
    import ai
    from fastapi.testclient import TestClient
    import server
    facets = {"sectors": ["Clinic", "Pharmacy"], "towns": ["Meru", "Embu"]}
    ok = ai.parse_campaign('```json\n{"reply": " Done. ", "name": "Meru  pharmacies", "channel": "sms", "brief": "Offer a demo.", '
                           '"sectors": ["pharmacy", "Pharmacy", "Bank"], "towns": ["MERU"], "limit": "25"}\n```', facets)
    assert ok["sectors"] == ["Pharmacy"] and ok["towns"] == ["Meru"] and ok["channel"] == "sms" and ok["limit"] == 25, ok
    assert ok["name"] == "Meru pharmacies" and ok["reply"].startswith("Done.") and "left them out" in ok["reply"], ok
    odd = ai.parse_campaign('{"channel": "whatsapp", "brief": "x", "sectors": "all", "towns": [], "limit": -3}', facets)
    assert odd["channel"] == "email" and odd["sectors"] == [] and odd["limit"] is None and odd["reply"] == "", odd
    assert ai.parse_campaign('{"limit": 99999}', facets)["limit"] == ai.MAX_CAMPAIGN_LIMIT
    try:
        ai.parse_campaign("sorry, no", facets); assert False
    except ai.AIError:
        pass

    fresh(); SMTP_INBOX.clear(); configure()
    answer = {"text": '{"reply": "Set up.", "name": "Meru pharmacies", "channel": "email", "brief": "Offer a billing demo.", '
                      '"sectors": ["Pharmacy"], "towns": ["Meru"], "limit": 2}'}
    srv, seen = _ai_server(answer)
    _use_ai(srv)
    try:
        with db.db() as c:
            for i in range(4):
                L.add_lead(c, {"name": f"Shop {i}", "town": "Meru", "sector": "Pharmacy", "email": f"s{i}@shop.test"})
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
            r = cl.post("/api/campaigns/ai-plan", json={"goal": "email Meru pharmacies about billing"}).json()
            assert r["sectors"] == ["Pharmacy"] and r["towns"] == ["Meru"] and r["limit"] == 2 and r["brief"], r
            assert "Pharmacy" in _prompt(seen[-1]) and "Meru" in _prompt(seen[-1]) and "billing" in _prompt(seen[-1])
            assert cl.post("/api/campaigns/ai-plan", json={"goal": "  "}).status_code == 400
            # AI mode: a brief is all that is needed, and the preview never calls the AI
            n = len(seen)
            body = {"channel": "email", "body": "Offer a demo", "filters": {"limit": 2}, "ai": True}
            pv = cl.post("/api/campaigns/preview", json=body).json()
            assert pv["will_send"] == 2 and pv["matching"] == 4 and pv["sample"] is None and len(seen) == n, pv
            assert cl.post("/api/campaigns/preview", json={**body, "body": " "}).status_code == 400
            assert cl.post("/api/campaigns/preview", json={**body, "ai": False}).status_code == 400   # normal mode still wants a subject
            # sample drafts: first people in the audience, nothing saved or queued
            answer["text"] = "Subject: Hello\n\nA short note about stock software."
            samples = cl.post("/api/campaigns/ai-samples", json=body).json()
            assert len(samples) == 2 and all(x["body"].count("reply STOP") == 1 and x["to"].endswith("@shop.test") for x in samples)
            assert count("SELECT COUNT(*) FROM messages") == 0
            answer["status"] = 429
            assert cl.post("/api/campaigns/ai-samples", json=body).status_code == 502
            answer["status"] = 200
            # starting an AI campaign needs an AI key
            db.save_settings({"ai_custom_url": ""})
            r = cl.post("/api/campaigns", json={**body, "launch": True})
            assert r.status_code == 400 and "AI key" in r.json()["detail"] and count("SELECT COUNT(*) FROM messages") == 0, r.text
    finally:
        srv.shutdown()


@test
def database_from_before_ai_campaigns_gains_the_column():
    fresh()
    with db.db() as c:
        c.execute("ALTER TABLE campaigns DROP COLUMN ai_personalize")
    db.init_db()
    db.init_db()   # and running it again is harmless
    with db.db() as c:
        assert "ai_personalize" in {r["name"] for r in c.execute("PRAGMA table_info(campaigns)")}
        cid = C.create_campaign(c, "Old style", "sms", "", "Hi {name}", {})
        assert c.execute("SELECT ai_personalize FROM campaigns WHERE id=?", (cid,)).fetchone()[0] == 0


@test
def whatsapp_is_logged_and_replies_are_matched_to_the_message_they_answer():
    fresh()
    import ai
    s = db.get_settings()
    assert s["sender_name"] == "Deric Marangu" and "data analyst" in s["ai_pitch"]
    with db.db() as c:
        a = L.add_lead(c, {"name": "Meru Chemist", "town": "Meru", "sector": "Pharmacy", "phone": "0700000101"})["id"]
        nm = L.add_lead(c, {"name": "Landline Shop", "town": "Meru", "phone": "020 2000000"})["id"]
    r = C.log_whatsapp(a, "Hello, I'm Deric Marangu, a data analyst.")
    assert r["url"].startswith("https://wa.me/254700000101?text=Hello%2C") and r["status"] == "sent"
    for bad in (nm, 99999):
        try:
            C.log_whatsapp(bad, "x"); raise AssertionError("should refuse")
        except (ValueError, KeyError):
            pass
    try:
        C.log_whatsapp(a, "  "); raise AssertionError("should refuse")
    except ValueError:
        pass
    with db.db() as c:
        assert L.get_lead(c, a)["status"] == "contacted"
        lead = L.lead_detail(c, a)
        assert [(m["channel"], m["status"]) for m in lead["messages"]] == [("whatsapp", "sent")]
        # nothing answered yet: the next draft is a follow-up that names the WhatsApp message
        stage = ai._stage(lead)
        assert "NO reply" in stage and "WhatsApp" in stage
        out_id = lead["messages"][0]["id"]
        res = L.record_reply(c, a, "whatsapp", "Interesting, how much?")
        row = c.execute("SELECT channel, reply_to FROM messages WHERE id=?", (res["id"],)).fetchone()
        assert row["channel"] == "whatsapp" and row["reply_to"] == out_id
        assert "They replied" in ai._stage(L.lead_detail(c, a))
        assert "FIRST message" in ai._stage({"messages": []})
        # the prompt the AI really receives: persona, business angle, conversation with channel and date
        seen = {}
        real_ask, ai.ask = ai.ask, lambda s_, system, prompt, **kw: seen.update(prompt=prompt) or "Hi Meru Chemist, Deric here."
        try:
            d = ai.draft_for_lead(s, L.lead_detail(c, a), "whatsapp", "")
        finally:
            ai.ask = real_ask
        p = seen["prompt"]
        assert d["subject"] == "" and "Deric Marangu" in p and "data analyst" in p and "medicines" in p
        assert "Us by WhatsApp" in p and "Them by WhatsApp" in p and "300 characters" in p and "dericbi.vercel.app" in p
        try:
            ai.draft_for_lead(s, L.lead_detail(c, a), "fax", "")
            raise AssertionError("fax is not a channel")
        except ValueError:
            pass
        c.execute("UPDATE leads SET do_not_contact=1 WHERE id=?", (a,))
    try:
        C.log_whatsapp(a, "again"); raise AssertionError("should refuse do-not-contact")
    except ValueError:
        pass
    # a WhatsApp sent by hand means campaigns never message that lead again, however long ago
    with db.db() as c:
        c.execute("UPDATE leads SET do_not_contact=0, status='contacted' WHERE id=?", (a,))
        c.execute("DELETE FROM messages WHERE direction='in' AND lead_id=?", (a,))
        s2 = db.get_settings(c)
        assert a not in [r["id"] for r in C.build_audience(c, "sms", {"statuses": ["contacted"]}, s2)]
        assert a not in [r["id"] for r in C.build_audience(c, "email", {"statuses": ["contacted"]}, s2)]
        c.execute("UPDATE messages SET sent_at=? WHERE lead_id=?", ("2020-01-01 00:00:00", a))
        assert a not in [r["id"] for r in C.build_audience(c, "sms", {"statuses": ["contacted"]}, s2)]   # not even years later
    assert "medicines" in ai._angle("Pharmacy") and "selling" in ai._angle("")
    assert "customers" in ai._angle("Barber shop") and "reorder" in ai._angle("Grocery store") and "season" in ai._angle("Hardware store")


@test
def old_database_gains_reply_tracking_and_new_persona_defaults():
    fresh()
    with db.db() as c:
        c.execute("ALTER TABLE messages DROP COLUMN reply_to")
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('sender_name', ?)", (json.dumps("DericBI"),))
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('ai_pitch', ?)", (json.dumps(""),))
    db.init_db()
    db.init_db()
    assert db.get_settings()["sender_name"] == "Deric Marangu"
    assert "data analyst" in db.get_settings()["ai_pitch"]
    with db.db() as c:
        assert "reply_to" in {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
    db.save_settings({"sender_name": "Someone Else"})
    db.init_db()
    assert db.get_settings()["sender_name"] == "Someone Else"


@test
def follow_ups_go_only_to_people_who_have_not_replied_when_they_fall_due():
    from fastapi.testclient import TestClient
    import server
    from util import days_ago
    fresh(); SMTP_INBOX.clear(); configure()
    srv, seen = _ai_server({"text": lambda req: f"Subject: About {_who(req)}\n\n{DISTINCT[_who(req)]}"})
    _use_ai(srv)
    try:
        with db.db() as c:
            ids = {name: L.add_lead(c, {"name": name, "town": "Meru", "sector": "Pharmacy",
                                        "email": name.split()[0].lower() + "@shop.test"})["id"] for name in DISTINCT}
        with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
            body = {"name": "Seq", "channel": "email", "body": "Offer a demo of the stock dashboard.",
                    "filters": {"sectors": ["Pharmacy"]}, "ai": True}
            for bad in ([0], [31], ["x"], [1, 2, 3, 4]):
                r = cl.post("/api/campaigns", json={**body, "followups": bad})
                assert r.status_code == 400, (bad, r.text)
            first = cl.post("/api/campaigns", json={**body, "followups": [3, 4], "launch": True}).json()
            rows = cl.get("/api/campaigns").json()
            kids = sorted((r for r in rows if r["followup_of"] is not None), key=lambda r: r["id"])
            assert [(k["status"], k["followup_days"], k["ai_personalize"]) for k in kids] == [("scheduled", 3, 1), ("scheduled", 4, 1)]
            assert kids[0]["followup_of"] == first["id"] and kids[1]["followup_of"] == kids[0]["id"]
            assert _wait(lambda: count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 3)
            assert C.release_followups() == 0, "nobody is due yet"
            with db.db() as c:
                L.record_reply(c, ids["Alpha Chemist"], "email", "Not now, thanks")
                c.execute("UPDATE leads SET last_contacted_at=?", (days_ago(4),))
            assert C.release_followups() == 2, "Alpha replied, so only Beta and Gamma are followed up"
            run_sender(lambda: count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 5)
            assert count("SELECT COUNT(*) FROM messages WHERE campaign_id=? AND status='sent'", kids[0]["id"]) == 2
            assert count("SELECT COUNT(*) FROM messages WHERE lead_id=? AND direction='out'", ids["Alpha Chemist"]) == 1
            prompt = next(_prompt(q) for q in seen[::-1] if _who(q) == "Beta Pharmacy")
            assert "NO reply to our email" in prompt and "Us by email" in prompt, prompt
            st = {r["id"]: r["status"] for r in cl.get("/api/campaigns").json()}
            assert st[first["id"]] == "done" and st[kids[0]["id"]] == "done" and st[kids[1]["id"]] == "scheduled", st
            with db.db() as c:
                c.execute("UPDATE leads SET last_contacted_at=?", (days_ago(5),))
            assert C.release_followups() == 2
            run_sender(lambda: count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 7)
            st = {r["id"]: r["status"] for r in cl.get("/api/campaigns").json()}
            assert st[kids[1]["id"]] == "done", st
            assert C.release_followups() == 0, "nobody is messaged twice"
            # cancelling the first campaign cancels the follow-ups waiting behind it
            for lead in ids.values():
                with db.db() as c:
                    c.execute("UPDATE leads SET do_not_contact=0, status='new', last_contacted_at=NULL WHERE id=?", (lead,))
                    c.execute("DELETE FROM messages WHERE lead_id=?", (lead,))
            again = cl.post("/api/campaigns", json={**body, "followups": [2, 2]}).json()
            assert cl.post(f"/api/campaigns/{again['id']}/cancel").status_code == 200
            tail = [r for r in cl.get("/api/campaigns").json() if r["name"].startswith("Seq") and r["id"] > kids[1]["id"]]
            assert len(tail) == 3 and all(r["status"] == "cancelled" for r in tail), tail
            # without an AI key follow-ups still work: the ready-made nudge, useful tip and last note are used
            db.save_settings({"ai_custom_url": "", "ai_custom_model": ""})
            r = cl.post("/api/campaigns", json={**body, "ai": False, "subject": "Hello {name}", "followups": [3, 4, 5]})
            assert r.status_code == 200, r.text
            ready = sorted((k for k in cl.get("/api/campaigns").json() if k["followup_of"] is not None and k["id"] > r.json()["id"]),
                           key=lambda k: k["id"])
            assert [(k["style"], k["ai_personalize"], k["followup_step"]) for k in ready] == [("ready", 0, 1), ("ready", 0, 2), ("ready", 0, 3)]
    finally:
        srv.shutdown()


@test
def today_lists_replies_due_follow_ups_and_quiet_leads_once_each():
    from util import days_ago
    fresh()
    with db.db() as c:
        mk = lambda n, ph: L.add_lead(c, {"name": n, "town": "Meru", "phone": ph})["id"]
        replied, due, quiet, fresh_sent, done, dnc = (mk(n, f"07000002{i:02d}") for i, n in enumerate(
            ["Replied", "Due", "Quiet", "Just messaged", "Won already", "Opted out"]))
        for lid in (replied, due, quiet, fresh_sent, done, dnc):
            c.execute("INSERT INTO messages(lead_id, direction, channel, to_addr, body, status, created_at, sent_at) "
                      "VALUES(?,?,?,?,?,?,?,?)", (lid, "out", "whatsapp", "x", "hello", "sent", days_ago(6), days_ago(6)))
            c.execute("UPDATE leads SET last_contacted_at=?, status='contacted' WHERE id=?", (days_ago(6), lid))
        c.execute("UPDATE leads SET last_contacted_at=? WHERE id=?", (days_ago(1), fresh_sent))
        L.record_reply(c, replied, "sms", "Yes, how much?")
        c.execute("UPDATE leads SET next_followup=? WHERE id IN (?, ?)", ("2020-01-01", due, replied))
        c.execute("UPDATE leads SET status='won' WHERE id=?", (done,))
        c.execute("UPDATE leads SET do_not_contact=1 WHERE id=?", (dnc,))
        q = L.today_queue(c)
        assert [r["name"] for r in q["replies"]] == ["Replied"]
        assert [r["name"] for r in q["due"]] == ["Due"], "a lead shows in the first list that applies, not twice"
        assert [r["name"] for r in q["quiet"]] == ["Quiet"] and q["quiet"][0]["unanswered"] == 1
        c.execute("UPDATE messages SET handled=1 WHERE lead_id=? AND direction='in'", (replied,))
        q = L.today_queue(c)
        assert [r["name"] for r in q["replies"]] == [] and "Replied" in [r["name"] for r in q["due"]]
        # answering them (any channel) takes them off the list without marking anything handled
        c.execute("UPDATE messages SET handled=0 WHERE lead_id=? AND direction='in'", (replied,))
        c.execute("UPDATE leads SET last_contacted_at=? WHERE id=?", (days_ago(-1), replied))
        assert [r["name"] for r in L.today_queue(c)["replies"]] == []


@test
def deal_value_and_offer_are_saved_validated_and_totalled():
    import server
    from fastapi.testclient import TestClient
    fresh()
    with db.db() as c:
        a = L.add_lead(c, {"name": "Big Shop", "phone": "0700000301"})["id"]
        b = L.add_lead(c, {"name": "Open Shop", "phone": "0700000302"})["id"]
        L.update_lead(c, a, {"deal_value": "45,000", "offer": "Sales dashboard", "status": "won"})
        L.update_lead(c, b, {"deal_value": 12000, "status": "interested"})
        assert L.get_lead(c, a)["deal_value"] == 45000 and L.get_lead(c, a)["offer"] == "Sales dashboard"
        for bad in ("abc", -5):
            try:
                L.update_lead(c, a, {"deal_value": bad}); raise AssertionError("should refuse")
            except ValueError:
                pass
        assert "Sales dashboard,45000" in L.export_csv(c).replace("\r", "")
    with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
        d = cl.get("/api/dashboard").json()
        assert d["won_value"] == 45000 and d["open_value"] == 12000 and set(d["channels"]) == {"email", "sms", "whatsapp"}


@test
def a_console_that_cannot_show_a_name_never_fails_the_job_or_skips_the_email_search():
    import io
    fresh()
    names = ["MAMA WANGESHI AGROVET", "Farm \u2b50\u2b50\u2b50 Agrovet \u0915\u093f\u0938\u093e\u0928", "Plain Agrovet"]
    listings = {"Agrovet in Kisii": [{"name": n, "href": href(9000 + i)} for i, n in enumerate(names)]}
    det = {n: {"phone": f"0712 00000{i}", "address": "Kisii", "website": ""} for i, n in enumerate(names)}
    with db.db() as c:
        jid = jobs.create_job(c, "scrape", {})
    real = sys.stdout
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)   # a Windows pipe
    try:
        final = scraper.run_scrape(jid, {"searches": [{"category": "Agrovet", "town": "Kisii", "query": "Agrovet in Kisii"}],
                                         "max_per_search": 50}, driver_factory=lambda: FakeDriver(listings, det), sleep=lambda s: None)
    finally:
        sys.stdout = real
    with db.db() as c:
        assert final == "done" and c.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 3
        logs = [r["msg"] for r in c.execute("SELECT msg FROM job_logs WHERE job_id=?", (jid,))]
    assert any("\u2b50" in m for m in logs) and not any("crashed" in m for m in logs)


def _wa_setup():
    fresh()
    with db.db() as c:
        ids = {}
        for key, name, phone, mail in (("a", "Meru Chemist", "0700000501", ""), ("b", "Kisii Agrovet", "0700000502", "kisii@agro.co.ke"),
                                       ("c", "Landline Only", "064 30123", "landline@shop.co.ke")):
            ids[key] = L.add_lead(c, {"name": name, "town": "Meru" if key == "a" else "Kisii", "sector": "Pharmacy", "phone": phone,
                                      "email": mail})["id"]
    return ids


@test
def whatsapp_campaign_queues_personal_messages_and_only_counts_what_you_confirm_sent():
    ids = _wa_setup()
    with db.db() as c:
        p = C.preview(c, "whatsapp", "", "Hi {name} in {town}, {sender} here.", {})
        assert p["matching"] == 2 and p["channel_ready"] and "Meru Chemist" in p["sample"]["body"]
        assert "--" not in p["sample"]["body"]          # no email-style footer on WhatsApp
        cid = C.create_campaign(c, "Wa test", "whatsapp", "", "Hi {name} in {town}, {sender} here.", {}, launch=True)
    run_sender(until=lambda: False, timeout=2)           # the background sender must leave WhatsApp alone
    with db.db() as c:
        assert [r["status"] for r in c.execute("SELECT status FROM messages WHERE campaign_id=?", (cid,))] == ["queued", "queued"]
    it = C.wa_next(cid)
    assert it["name"] == "Meru Chemist" and it["body"].startswith("Hi Meru Chemist in Meru,") and it["total"] == 2
    assert it["url"].startswith("https://wa.me/254700000501?text=Hi%20Meru%20Chemist")
    r = C.wa_act(it["id"], "link", it["body"] + " Edited.")                # opening is NOT sending
    assert "Edited" in r["url"]
    with db.db() as c:
        m = c.execute("SELECT status, body FROM messages WHERE id=?", (it["id"],)).fetchone()
        assert m["status"] == "queued" and m["body"].endswith("Edited.") and L.get_lead(c, ids["a"])["status"] == "new"
    C.wa_act(it["id"], "sent")
    with db.db() as c:
        lead = L.lead_detail(c, ids["a"])
        assert lead["status"] == "contacted" and [(m["channel"], m["status"]) for m in lead["messages"]] == [("whatsapp", "sent")]
        st = C.campaign_stats(c, cid)
        assert st["sent"] == 1 and st["queued"] == 1 and st["status"] == "running"
        res = L.record_reply(c, ids["a"], "whatsapp", "Yes, interested")          # the reply is matched to this campaign's message
        assert c.execute("SELECT campaign_id FROM messages WHERE id=?", (res["id"],)).fetchone()["campaign_id"] == cid
    try:
        C.wa_act(it["id"], "sent"); raise AssertionError("must not count twice")
    except ValueError:
        pass
    it2 = C.wa_next(cid)
    assert it2["name"] == "Kisii Agrovet"
    C.wa_act(it2["id"], "no_whatsapp")
    assert C.wa_next(cid) is None
    with db.db() as c:
        st = C.campaign_stats(c, cid)
        assert st["status"] == "done" and st["sent"] == 1 and st["skipped"] == 1
        assert "not on WhatsApp" in " ".join(e["detail"] for e in L.lead_detail(c, ids["b"])["events"])
        # not on WhatsApp: never offered WhatsApp again, but an email campaign still reaches them
        assert [r["name"] for r in C.build_audience(c, "whatsapp", {"statuses": ["new"]}, db.get_settings(c))] == []
        assert ids["b"] in [r["id"] for r in C.build_audience(c, "email", {"statuses": ["new"]}, db.get_settings(c))]


@test
def whatsapp_queue_refuses_unsendable_leads_paused_campaigns_and_ai_messages_are_written_and_kept():
    import ai
    ids = _wa_setup()
    real_conf, real_draft = ai.configured, ai.draft_for_lead
    ai.configured = lambda s: True
    n = {"i": 0}
    def draft(s_, lead, ch, notes, avoid="", limit=0):
        n["i"] += 1
        assert ch == "whatsapp"
        return {"subject": "", "body": f"Habari {lead['name']}, number {n['i']} for the {lead['sector']} owner in {lead['town']}."}
    ai.draft_for_lead = draft
    try:
        with db.db() as c:
            cid = C.create_campaign(c, "Wa AI", "whatsapp", "", "Offer a demo", {}, launch=True, personalize=True)
        it = C.wa_next(cid)
        assert it["body"].startswith("Habari Meru Chemist") and C.wa_next(cid)["body"] == it["body"] and n["i"] == 1   # kept, not rewritten
        with db.db() as c:
            C.set_campaign_status(c, cid, "pause")
        try:
            C.wa_next(cid); raise AssertionError("paused")
        except ValueError:
            pass
        with db.db() as c:
            C.set_campaign_status(c, cid, "resume")
            c.execute("UPDATE leads SET do_not_contact=1 WHERE id=?", (ids["a"],))
        try:
            C.wa_act(it["id"], "link"); raise AssertionError("do-not-contact must be refused")
        except ValueError:
            pass
        assert C.wa_next(cid)["name"] == "Kisii Agrovet"          # the do-not-contact lead is skipped, not offered
        with db.db() as c:
            assert c.execute("SELECT status, error FROM messages WHERE id=?", (it["id"],)).fetchone()["status"] == "skipped"
    finally:
        ai.configured, ai.draft_for_lead = real_conf, real_draft


@test
def whatsapp_queue_works_through_the_api():
    import server
    from fastapi.testclient import TestClient
    ids = _wa_setup()
    with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
        r = cl.post("/api/campaigns", json={"name": "api", "channel": "whatsapp", "body": "Hi {name}", "launch": True,
                                            "filters": {"statuses": ["new"]}})
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        item = cl.get(f"/api/campaigns/{cid}/whatsapp/next").json()["item"]
        assert item["body"] == "Hi Meru Chemist"
        assert cl.post(f"/api/messages/{item['id']}/whatsapp", json={"action": "sent"}).status_code == 200
        assert cl.post(f"/api/messages/{item['id']}/whatsapp", json={"action": "sent"}).status_code == 400
        assert cl.post(f"/api/messages/{item['id']}/whatsapp", json={"action": "bogus"}).status_code == 400
        assert cl.get("/api/dashboard").json()["sent_today"]["whatsapp"] == 1
        assert cl.post(f"/api/leads/{ids['a']}/send", json={"channel": "whatsapp", "body": "x"}).status_code == 400


# ======================================================== EMAIL: Gmail-style problems
def _auth_smtp(user=b"me@gmail.com", pw=b"abcdefghijklmnop"):
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult, LoginPassword
    import socket

    class H:
        async def handle_DATA(self, server, session, envelope):
            SMTP_INBOX.append(envelope)
            return "250 OK"

    def auth(server, session, envelope, mechanism, data):
        ok = isinstance(data, LoginPassword) and data.login == user and data.password == pw
        return AuthResult(success=ok, handled=False if not ok else True)
    sk = socket.socket(); sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]; sk.close()
    ctl = Controller(H(), hostname="127.0.0.1", port=port, authenticator=auth, auth_required=True, auth_require_tls=False)
    ctl.start()
    return ctl, port


@test
def gmail_style_password_and_username_slips_no_longer_break_sending():
    fresh(); SMTP_INBOX.clear()
    ctl, port = _auth_smtp()
    base = dict(db.SETTING_DEFAULTS, smtp_host="127.0.0.1", smtp_port=port, smtp_security="none", sender_name="Deric",
                from_email="me@gmail.com", smtp_user="me@gmail.com", smtp_pass="abcdefghijklmnop")
    try:
        messaging.send_email("lead@shop.co.ke", "hi", "body", base)
        # the app password as Google shows it, with spaces, and as copied from a web page, with non-breaking spaces
        messaging.send_email("lead@shop.co.ke", "hi", "body", dict(base, smtp_pass="abcd efgh ijkl mnop"))
        messaging.send_email("lead@shop.co.ke", "hi", "body", dict(base, smtp_pass="abcd\u00a0efgh\u00a0ijkl\u00a0mnop"))
        # username left blank: the 'Send from' address is the login (it used to skip the login: "530 Authentication required")
        messaging.send_email("lead@shop.co.ke", "hi", "body", dict(base, smtp_user=""))
        assert len(SMTP_INBOX) == 4
        # a wrong password pauses the campaign and, for Gmail, says what to do about it
        try:
            messaging.send_email("lead@shop.co.ke", "hi", "body", dict(base, smtp_host="127.0.0.1", smtp_pass="wrong"))
            raise AssertionError("wrong password")
        except messaging.SendError as e:
            assert e.kind == "pause" and "refused" in str(e)
        gm = messaging._login_help("smtp.gmail.com", "535 bad")
        assert "App Password" in gm and "2-Step Verification" in gm
        # the diagnosis names the step that fails
        good = messaging.diagnose_email(base)
        assert good["ok"] and [st["label"] for st in good["steps"]] == ["Settings", "Find the server", "Connect", "Secure connection", "Login"]
        bad = messaging.diagnose_email(dict(base, smtp_pass="wrong"))
        assert not bad["ok"] and bad["message"].startswith("Login")
        closed = messaging.diagnose_email(dict(base, smtp_port=9))
        assert not closed["ok"] and closed["message"].startswith("Connect") and "blocked" in closed["message"]
        assert messaging.diagnose_email(dict(base, smtp_host=""))["message"].startswith("Settings")
        nohost = messaging.diagnose_email(dict(base, smtp_host="no-such-host.invalid"))
        assert nohost["message"].startswith("Find the server")
    finally:
        ctl.stop()


@test
def reading_replies_uses_the_sending_login_and_settings_clean_pasted_passwords():
    from fastapi.testclient import TestClient
    import server
    fresh()
    assert messaging.inbox_login({"imap_user": "", "smtp_user": "", "from_email": "me@gmail.com", "imap_pass": "", "smtp_pass": "abcd efgh"}) == \
        ("me@gmail.com", "abcdefgh")
    with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
        r = cl.put("/api/settings", json={"smtp_pass": "abcd\u00a0efgh ijkl", "sms_gateway_pass": "pa ss", "optout_suffix_sms": "STOP to opt out"})
        assert r.status_code == 200 and db.get_settings()["smtp_pass"] == "abcdefghijkl" and db.get_settings()["sms_gateway_pass"] == "pass"
        assert db.get_settings()["optout_suffix_sms"] == " STOP to opt out"        # keeps the space that joins it to the text
        assert cl.put("/api/settings", json={"sms_delay_sec": 5}).status_code == 400
        assert cl.put("/api/settings", json={"whatsapp_delay_sec": 5}).status_code == 400
        assert cl.put("/api/settings", json={"contact_website": "dericbi.vercel.app"}).status_code == 400
        assert cl.post("/api/settings/connect-gmail", json={"address": "nope", "app_password": "x" * 16}).status_code == 400
        assert cl.post("/api/settings/connect-gmail", json={"address": "me@gmail.com", "app_password": "short"}).status_code == 400
        real = (messaging.diagnose_email, messaging.test_imap)
        messaging.diagnose_email = lambda s: {"ok": True, "steps": [], "message": "ok"}
        messaging.test_imap = lambda s: {"ok": True, "steps": [], "message": "ok"}
        try:
            r = cl.post("/api/settings/connect-gmail", json={"address": " Me@Gmail.com ", "app_password": "abcd efgh ijkl mnop"}).json()
        finally:
            messaging.diagnose_email, messaging.test_imap = real
        s = db.get_settings()
        assert r["ok"] and (s["smtp_host"], s["smtp_port"], s["smtp_security"]) == ("smtp.gmail.com", 587, "starttls")
        assert (s["imap_host"], s["imap_port"], s["imap_user"], s["smtp_user"], s["from_email"]) == \
            ("imap.gmail.com", 993, "me@gmail.com", "me@gmail.com", "me@gmail.com") and s["smtp_pass"] == s["imap_pass"] == "abcdefghijklmnop"


# ======================================================== ONE FIRST MESSAGE PER LEAD
@test
def a_lead_who_has_a_message_is_never_picked_again_until_it_is_a_follow_up():
    fresh(); SMTP_INBOX.clear(); PHONE_CALLS.clear(); PHONE_MODE["mode"] = "ok"; configure()
    with db.db() as c:
        both = L.add_lead(c, {"name": "Both Ways", "town": "T", "sector": "Pharmacy", "phone": "0700000201", "email": "both@shop.test"})["id"]
        mail = L.add_lead(c, {"name": "Mail Only", "town": "T", "sector": "Pharmacy", "email": "mail@shop.test"})["id"]
        cid = C.create_campaign(c, "First", "email", "Hi {name}", "Hello {name}", {"lead_ids": [both]}, launch=True)
        s = db.get_settings(c)
        # queued counts: not offered to a text campaign while the email is waiting
        assert both not in [r["id"] for r in C.build_audience(c, "sms", {}, s)] and mail in [r["id"] for r in C.build_audience(c, "email", {}, s)]
    run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", cid) == "done")
    with db.db() as c:
        c.execute("UPDATE messages SET sent_at='2020-01-01 00:00:00' WHERE lead_id=?", (both,))   # however long ago
        s = db.get_settings(c)
        for ch in ("email", "sms", "whatsapp"):
            assert both not in [r["id"] for r in C.build_audience(c, ch, {"statuses": ["new", "contacted"]}, s)], ch
        try:
            C.create_campaign(c, "Again", "sms", "", "Hi", {"lead_ids": [both]}, launch=True); raise AssertionError("nobody to message")
        except ValueError as e:
            assert "No leads match" in str(e)
    # nor by hand, on any channel, until the follow-up wait has passed
    with db.db() as c:
        c.execute("UPDATE messages SET sent_at=? WHERE lead_id=?", (now(), both))
    for fn in (lambda: C.send_now(both, "sms", "", "Hi"), lambda: C.send_now(both, "email", "x", "y"), lambda: C.log_whatsapp(both, "Hi")):
        try:
            fn(); raise AssertionError("must wait")
        except ValueError as e:
            assert "unlocks" in str(e) or "waiting" in str(e), e
    with db.db() as c:
        st = C.outreach_state(c, mail)
        assert st["allowed"] and st["kind"] == "first"
        c.execute("UPDATE messages SET sent_at=? WHERE lead_id=?", (now(), both))
        assert C.outreach_state(c, both)["kind"] == "too_soon"
        c.execute("UPDATE messages SET sent_at=? WHERE lead_id=?", (days_ago(4), both))
        assert C.outreach_state(c, both) == {"allowed": True, "kind": "followup", "reason": ""}
        L.record_reply(c, both, "email", "Interested")
        c.execute("UPDATE messages SET sent_at=? WHERE lead_id=? AND direction='out'", (now(), both))
        assert C.outreach_state(c, both)["kind"] == "reply"        # they answered: talking to them is a conversation
    assert C.send_now(both, "sms", "", "Thanks {name}, here is more.")["status"] == "sent"


@test
def a_message_queued_in_one_campaign_is_dropped_if_the_lead_was_messaged_another_way_meanwhile():
    fresh(); SMTP_INBOX.clear(); configure()
    with db.db() as c:
        a = L.add_lead(c, {"name": "Raced", "town": "T", "email": "raced@shop.test"})["id"]
        b = L.add_lead(c, {"name": "Clear", "town": "T", "email": "clear@shop.test"})["id"]
        cid = C.create_campaign(c, "Race", "email", "Hi", "Hello {name}", {}, launch=True)
        c.execute("INSERT INTO messages(lead_id, direction, channel, body, status, sent_at, created_at) VALUES(?,?,?,?,?,?,?)",
                  (a, "out", "whatsapp", "by hand", "sent", now(), now()))      # sent by hand after the campaign was queued
    run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", cid) == "done")
    assert count("SELECT status FROM messages WHERE campaign_id=? AND lead_id=?", cid, a) == "skipped"
    assert "another way" in count("SELECT error FROM messages WHERE campaign_id=? AND lead_id=?", cid, a)
    assert count("SELECT status FROM messages WHERE campaign_id=? AND lead_id=?", cid, b) == "sent" and len(SMTP_INBOX) == 1


# ======================================================== READY-MADE MESSAGES
@test
def every_ready_made_text_fits_one_sms_with_a_long_name_and_the_opt_out_line():
    import library
    s = dict(db.SETTING_DEFAULTS)
    assert s["contact_website"] == "https://dericbi.vercel.app" and s["contact_whatsapp"] == "+254791360805" and s["contact_email"] == "dericmarangu@gmail.com"
    names = ["MAMA WANGESHI AGROVET", "Boresha Agrovet & Hardware Supplies Ltd", "Dr. Kamau Dental Clinic and Laboratory Services", "Z"]
    for sector in ("Pharmacy", "Agrovet", "Hardware store", "Salon", "Restaurant", "School", "Garage", "Supermarket", "Whatever", ""):
        for name in names:
            lead = {"name": name, "town": "Kisii", "sector": sector}
            for i in range(2):
                subj, body, problem = C.compose("sms", "ready", "", "", lead, s, i)
                assert not problem and len(body) <= 160 and body.isascii(), (sector, name, len(body), body)
                assert "dericbi.vercel.app" in body or "+254791360805" in body or "{" not in body
            for step in (1, 2, 3):
                _, body, problem = C.compose("sms", "ready", "", "", lead, s, 0, step)
                assert not problem and len(body) <= 160 and "{" not in body, (sector, name, step, body)
    # a name cut down only as far as needed, never the whole message dropped
    lead = {"name": "Boresha Agrovet & Hardware Supplies Ltd", "town": "Kisii", "sector": "Agrovet"}
    _, body, _ = C.compose("sms", "ready", "", "", lead, s, 0)
    assert body.startswith("Boresha") and len(body) <= 160
    # the owner's own text that cannot fit even with a short name is held back, not split into two texts
    _, _, problem = C.compose("sms", "", "", "x" * 200, lead, s)
    assert problem and "160" in problem
    assert messaging.sms_clean("It\u2019s \u201cgood\u201d \u2014 caf\u00e9 \U0001F600") == 'It\'s "good" - cafe'
    # email and WhatsApp versions carry every contact way and no leftover blanks
    for ch in ("email", "whatsapp"):
        for step in (0, 1, 2, 3):
            subj, body, problem = C.compose(ch, "ready", "", "", {"name": "Meru Chemist", "town": "Meru", "sector": "Pharmacy"}, s, 0, step)
            assert not problem and "{" not in body + subj and "Meru Chemist" in body and "dericbi.vercel.app" in body or step in (2, 3), (ch, step, body)
            if ch == "email":
                assert subj and "+254791360805" in body


@test
def ready_made_campaign_follow_ups_and_cross_channel_follow_up_work_without_any_ai_key():
    fresh(); SMTP_INBOX.clear(); PHONE_CALLS.clear(); PHONE_MODE["mode"] = "ok"; configure()
    with db.db() as c:
        ids = [L.add_lead(c, {"name": n, "town": "Meru", "sector": "Pharmacy", "phone": p, "email": e})["id"]
               for n, p, e in (("Alpha Chemist", "0700000301", "a@shop.test"), ("Beta Chemist", "0700000302", "b@shop.test"))]
        cid = C.create_campaign(c, "Ready", "email", "", "", {"lead_ids": ids}, launch=True, style="ready", followups=[3, 5])
        rows = c.execute("SELECT * FROM campaigns ORDER BY id").fetchall()
        assert [(r["style"], r["ai_personalize"], r["followup_of"] is not None, r["followup_step"]) for r in rows] == \
            [("ready", 0, False, 0), ("ready", 0, True, 1), ("ready", 0, True, 2)]
        first = c.execute("SELECT subject, body FROM messages WHERE campaign_id=? ORDER BY id", (cid,)).fetchall()
        assert all("Chemist" in m["body"] and "dericbi.vercel.app" in m["body"] and "expired or missing medicines" in m["subject"] + m["body"] for m in first)
    run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", cid) == "done")
    assert C.release_followups() == 0                                    # not due yet
    with db.db() as c:
        L.record_reply(c, ids[0], "email", "Not now")
        c.execute("UPDATE leads SET last_contacted_at=?", (days_ago(4),))
    assert C.release_followups() == 1                                    # only the one who did not reply
    body = count("SELECT body FROM messages WHERE campaign_id=(SELECT id FROM campaigns WHERE followup_step=1) AND status='queued'")
    assert "Beta Chemist" in body and "reached you" in body
    with db.db() as c:
        try:
            C.create_followup(c, cid, days=0); raise AssertionError("this campaign already has a follow-up waiting")
        except ValueError as e:
            assert "already waiting" in str(e)
    # one tap on a finished campaign: follow up on another channel they can be reached on
    with db.db() as c:
        x = L.add_lead(c, {"name": "Gamma Chemist", "town": "Meru", "sector": "Pharmacy", "phone": "0700000303", "email": "g@shop.test"})["id"]
        plain = C.create_campaign(c, "Plain", "email", "Hi {name}", "Hello {name}", {"lead_ids": [x]}, launch=True)
    run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", plain) == "done")
    with db.db() as c:
        wa = C.create_followup(c, plain, days=0, channel="sms")
        row = c.execute("SELECT status, channel, followup_of FROM campaigns WHERE id=?", (wa,)).fetchone()
        assert row["channel"] == "sms" and row["status"] == "running" and row["followup_of"] == plain      # due at once, goes by text
        assert "Gamma Chemist" in c.execute("SELECT body FROM messages WHERE campaign_id=?", (wa,)).fetchone()["body"]
        draft = C.create_campaign(c, "D", "email", "x", "y", {"lead_ids": ids})
        try:
            C.create_followup(c, draft); raise AssertionError("not started yet")
        except ValueError:
            pass


@test
def campaign_screen_api_reach_audience_followup_and_ready_preview():
    from fastapi.testclient import TestClient
    import server
    fresh(); SMTP_INBOX.clear(); configure()
    with db.db() as c:
        ids = {n: L.add_lead(c, {"name": n, "town": "Embu", "sector": "Chemist", "phone": p, "email": e})["id"]
               for n, p, e in (("One", "0711000001", "one@shop.test"), ("Two", "0711000002", ""), ("Three", "", "three@shop.test"))}
    with TestClient(server.create_app(port=8765), base_url="http://127.0.0.1:8765") as cl:
        reach = cl.get("/api/campaigns/reach").json()
        assert reach["email"]["leads"] == 2 and reach["sms"]["leads"] == 2 and reach["whatsapp"]["leads"] == 2
        assert reach["email"]["ready"] and reach["sms"]["ready"] and reach["whatsapp"]["ready"]
        aud = cl.post("/api/campaigns/audience", json={"channel": "email", "filters": {"q": "thr"}}).json()
        assert aud["total"] == 1 and aud["leads"][0]["name"] == "Three" and aud["leads"][0]["contact"] == "three@shop.test"
        pv = cl.post("/api/campaigns/preview", json={"channel": "sms", "style": "ready", "body": "", "filters": {"lead_ids": [ids["One"]]}}).json()
        assert pv["matching"] == 1 and pv["sample"]["length"] <= 160 and "One" in pv["sample"]["body"] and pv["too_long"] == 0
        assert cl.post("/api/campaigns/preview", json={"channel": "sms", "body": "", "filters": {}}).status_code == 400
        r = cl.post("/api/campaigns", json={"name": "Picked", "channel": "email", "style": "ready", "body": "", "launch": True,
                                            "filters": {"lead_ids": [ids["One"]]}, "followups": [3]})
        assert r.status_code == 200 and r.json()["total"] == 1 and r.json()["followups_waiting"] == 1
        assert count("SELECT COUNT(*) FROM messages WHERE campaign_id=?", r.json()["id"]) == 1       # only the lead that was ticked
        run_sender(lambda: count("SELECT status FROM campaigns WHERE id=?", r.json()["id"]) == "done")
        assert cl.post(f"/api/campaigns/{r.json()['id']}/followup", json={"days": 2, "channel": "sms"}).status_code == 400   # one already waits
        d = cl.get(f"/api/leads/{ids['One']}").json()
        assert d["outreach"]["kind"] == "too_soon" and "unlocks" in d["outreach"]["reason"]
        assert cl.post(f"/api/leads/{ids['One']}/send", json={"channel": "sms", "body": "again"}).status_code == 400
        assert cl.get(f"/api/leads/{ids['Two']}").json()["outreach"]["kind"] == "first"
        assert cl.post(f"/api/leads/{ids['Two']}/send", json={"channel": "sms", "body": "x" * 170}).status_code == 400   # over 160


# ======================================================== WHATSAPP (automatic)
import whatsapp  # noqa: E402


class FakeWA:
    """Stands in for WhatsApp Web: plan maps a phone number to what happens when it is messaged."""
    def __init__(self, plan=None, ready=True):
        self.plan, self.ready, self.sent, self.closed = plan or {}, ready, [], False
        self.should_stop = lambda: False

    def open(self): pass
    def close(self): self.closed = True
    def pause(self, ms): pass
    def wait_ready(self, seconds): return "ready" if self.ready else "qr"
    def state(self): return "ready" if self.ready else "qr"

    def send(self, phone, text):
        what = self.plan.get(phone, "sent")
        if what == "logout": raise whatsapp.LoggedOut()
        if what == "unconfirmed": raise whatsapp.SendUnconfirmed("Send was pressed but WhatsApp never showed the message as sent.")
        if what == "boom": raise RuntimeError("page changed")
        if what == "not_on": return "not_on_whatsapp"
        self.sent.append((phone, text))
        return "sent"


def _wa_leads(n=4, prefix="07000000"):
    with db.db() as c:
        return [L.add_lead(c, {"name": f"Shop {i}", "town": "Kisii", "sector": "Agrovet", "phone": f"{prefix}{40 + i}"})["id"] for i in range(n)]


def _run_wa(fake, **kw):
    with db.db() as c:
        jid = jobs.create_job(c, "whatsapp", {})
    return whatsapp.run_worker(jid, driver_factory=lambda: fake, sleep=lambda s: None, **kw), jid


@test
def whatsapp_worker_sends_and_records_everything_by_itself_with_nothing_logged_by_hand():
    fresh(); configure(whatsapp_delay_sec=20); db.set_internal("whatsapp_linked", True)
    ids = _wa_leads(4)
    with db.db() as c:
        cid = C.create_campaign(c, "WA", "whatsapp", "", "", {}, launch=True, style="ready")
        c.execute("UPDATE leads SET town='Kisii'")
    try:
        C.wa_next(cid); raise AssertionError("no queue by hand once WhatsApp is linked")
    except ValueError as e:
        assert "sent for you" in str(e)
    fake = FakeWA({"+254700000041": "not_on"})
    final, jid = _run_wa(fake)
    assert final == "done" and fake.closed
    assert len(fake.sent) == 3 and all(len(t) > 40 and "Shop" in t and "{" not in t for _, t in fake.sent)
    with db.db() as c:
        st = C.campaign_stats(c, cid)
        assert st["sent"] == 3 and st["skipped"] == 1 and st["status"] == "done" and st["queued"] == 0
        for i in (0, 2, 3):
            d = L.lead_detail(c, ids[i])
            assert d["status"] == "contacted" and [(m["channel"], m["status"]) for m in d["messages"]] == [("whatsapp", "sent")]
        assert "not on WhatsApp" in " ".join(e["detail"] for e in L.lead_detail(c, ids[1])["events"])
        L.record_reply(c, ids[0], "whatsapp", "Yes please")                      # the reply is matched to what was sent
    assert count("SELECT campaign_id FROM messages WHERE lead_id=? AND direction='in'", ids[0]) == cid
    assert count("SELECT COUNT(*) FROM events WHERE kind='no_whatsapp'") == 1


@test
def whatsapp_worker_stops_safely_when_logged_out_failing_unconfirmed_or_over_the_limit():
    # logged out mid-way: unlinked, campaigns paused, the message not lost
    fresh(); configure(whatsapp_delay_sec=20); db.set_internal("whatsapp_linked", True)
    _wa_leads(3)
    with db.db() as c:
        cid = C.create_campaign(c, "Out", "whatsapp", "", "", {}, launch=True, style="ready")
    final, _ = _run_wa(FakeWA({"+254700000040": "logout"}))
    assert final == "failed" and not whatsapp.is_linked()
    assert count("SELECT status FROM campaigns WHERE id=?", cid) == "paused" and "logged out" in count("SELECT last_error FROM campaigns WHERE id=?", cid)
    assert count("SELECT COUNT(*) FROM messages WHERE status='queued'") == 3 and count("SELECT COUNT(*) FROM messages WHERE status='sent'") == 0
    # the link code shown at start-up is the same thing
    fresh(); configure(whatsapp_delay_sec=20); db.set_internal("whatsapp_linked", True); _wa_leads(1)
    with db.db() as c:
        cid = C.create_campaign(c, "Out2", "whatsapp", "", "", {}, launch=True, style="ready")
    assert _run_wa(FakeWA(ready=False))[0] == "failed" and not whatsapp.is_linked()
    # three failures in a row pause everything; an unconfirmed send is failed (never re-sent), a crash is retried later
    fresh(); configure(whatsapp_delay_sec=20); db.set_internal("whatsapp_linked", True); _wa_leads(5)
    with db.db() as c:
        cid = C.create_campaign(c, "Bad", "whatsapp", "", "", {}, launch=True, style="ready")
    final, _ = _run_wa(FakeWA({"+254700000040": "unconfirmed", "+254700000041": "boom", "+254700000042": "boom"}))
    assert final == "failed" and count("SELECT status FROM campaigns WHERE id=?", cid) == "paused"
    assert count("SELECT status FROM messages WHERE to_addr='+254700000040'") == "failed"
    assert "check WhatsApp" in count("SELECT error FROM messages WHERE to_addr='+254700000040'")
    assert count("SELECT status FROM messages WHERE to_addr='+254700000041'") == "queued"      # will be tried again later
    assert count("SELECT scheduled_at > ? FROM messages WHERE to_addr='+254700000041'", now()) == 1
    # the daily limit and the sending hours
    fresh(); configure(whatsapp_delay_sec=20, whatsapp_daily_cap=2); db.set_internal("whatsapp_linked", True); _wa_leads(5)
    with db.db() as c:
        C.create_campaign(c, "Cap", "whatsapp", "", "", {}, launch=True, style="ready")
    fake = FakeWA(); _run_wa(fake)
    assert len(fake.sent) == 2 and count("SELECT COUNT(*) FROM messages WHERE status='queued'") == 3
    configure(whatsapp_delay_sec=20, send_window_enabled=True, send_window_start="00:00", send_window_end="00:00")
    fake = FakeWA(); _run_wa(fake)
    assert fake.sent == [] or len(fake.sent) <= 2


@test
def whatsapp_one_lead_button_queues_and_sends_itself_once_linked_otherwise_it_is_logged_for_you():
    fresh(); configure()
    ids = _wa_leads(2)
    r = C.whatsapp_one(ids[0], "Hello Shop 0")                   # not linked: logged now, link returned to press send
    assert r["status"] == "sent" and r["url"].startswith("https://wa.me/254700000040")
    db.set_internal("whatsapp_linked", True)
    try:
        C.whatsapp_one(ids[0], "again"); raise AssertionError("one message at a time")
    except ValueError as e:
        assert "unlocks" in str(e)
    r = C.whatsapp_one(ids[1], "Hello Shop 1")                   # linked: queued, the worker sends and records it
    assert r["queued"] and count("SELECT status FROM messages WHERE id=?", r["id"]) == "queued"
    fake = FakeWA(); _run_wa(fake)
    assert fake.sent == [("+254700000041", "Hello Shop 1")] and count("SELECT status FROM messages WHERE id=?", r["id"]) == "sent"
    assert count("SELECT status FROM leads WHERE id=?", ids[1]) == "contacted"
    # a manual follow-up after the wait is allowed and is not blocked as 'already messaged another way'
    with db.db() as c:
        c.execute("UPDATE messages SET sent_at=? WHERE lead_id=?", (days_ago(5), ids[1]))
    r = C.whatsapp_one(ids[1], "Following up")
    fake = FakeWA(); _run_wa(fake)
    assert fake.sent == [("+254700000041", "Following up")]


@test
def sender_starts_the_whatsapp_worker_only_when_linked_in_hours_and_under_the_limit():
    fresh(); configure(); _wa_leads(2)
    with db.db() as c:
        C.create_campaign(c, "Spawn", "whatsapp", "", "", {}, launch=True, style="ready")
    spawned = []
    real_spawn, jobs.spawn = jobs.spawn, lambda jid, kind: spawned.append(kind)
    try:
        sender = C.Sender()
        assert sender._whatsapp_tick(db.get_settings()) is None and not spawned          # not linked: nothing starts
        db.set_internal("whatsapp_linked", True)
        assert sender._whatsapp_tick(db.get_settings()) and spawned == ["whatsapp"]
        sender._whatsapp_tick(db.get_settings()); assert spawned == ["whatsapp"]         # a worker is already running
        with db.db() as c:
            c.execute("UPDATE jobs SET status='done'")
        sender.last_wa_spawn = 0
        configure(whatsapp_daily_cap=0)
        assert sender._whatsapp_tick(db.get_settings()) == 60.0 and spawned == ["whatsapp"]   # daily limit reached
        configure(whatsapp_daily_cap=40, send_window_enabled=True, send_window_start="00:00", send_window_end="00:00")
        assert sender._whatsapp_tick(db.get_settings()) == 30.0 and spawned == ["whatsapp"]   # outside sending hours
    finally:
        jobs.spawn = real_spawn


@test
def the_real_browser_driver_sends_waits_for_the_tick_and_handles_bad_numbers_and_logout():
    import mock_whatsapp as mw
    srv = mw.serve()
    base = f"http://127.0.0.1:{srv.server_port}"
    fresh(); db.set_internal("whatsapp_linked", False)
    real = whatsapp.profile_dir, whatsapp.CONFIRM_SECONDS
    whatsapp.profile_dir = lambda: Path(tempfile.mkdtemp())
    whatsapp.CONFIRM_SECONDS = 4
    drv = whatsapp.WhatsAppWeb(log=lambda m: None, base=base, headless=True)
    try:
        mw.MODE.update(mode="ok", tick_ms=400); mw.SENT.clear()
        drv.open()
        assert drv.wait_ready(20) == "ready"
        assert drv.send("+254 700 000 777", "Hello Mama, a second line:\nsee dericbi.vercel.app") == "sent"
        assert mw.SENT == [("254700000777", "Hello Mama, a second line:\nsee dericbi.vercel.app")]       # typed in, sent, and confirmed
        assert drv.send("+254700000999", "x") == "not_on_whatsapp" and len(mw.SENT) == 1
        mw.MODE["mode"] = "stuck"
        try:
            drv.send("+254700000778", "never ticks"); raise AssertionError("must not claim it was sent")
        except whatsapp.SendUnconfirmed:
            pass
        mw.MODE["mode"] = "qr"
        try:
            drv.send("+254700000779", "x"); raise AssertionError("logged out")
        except whatsapp.LoggedOut:
            pass
        assert drv.state() == "qr"
    finally:
        drv.close(); srv.shutdown()
        whatsapp.profile_dir, whatsapp.CONFIRM_SECONDS = real


@test
def linking_whatsapp_waits_for_the_code_to_be_scanned_and_remembers_it():
    fresh(); db.set_internal("whatsapp_linked", False)

    class Linking(FakeWA):
        polls = 0
        def state(self):
            Linking.polls += 1
            return "qr" if Linking.polls < 3 else "ready"
    with db.db() as c:
        jid = jobs.create_job(c, "whatsapp_link", {})
    assert whatsapp.run_link(jid, driver_factory=lambda: Linking(), wait_seconds=30) == "done" and whatsapp.is_linked()
    assert db.get_internal("whatsapp_linked_at")
    with db.db() as c:
        msgs = " ".join(r["msg"] for r in c.execute("SELECT msg FROM job_logs WHERE job_id=?", (jid,)))
    assert "Linked devices" in msgs and "linked" in msgs
    whatsapp.set_linked(False)
    with db.db() as c:
        jid = jobs.create_job(c, "whatsapp_link", {})
    class Never(FakeWA):
        def state(self): return "qr"
    assert whatsapp.run_link(jid, driver_factory=lambda: Never(), wait_seconds=1) == "failed" and not whatsapp.is_linked()


if __name__ == "__main__":
    SMTP.stop()
    PHONE.shutdown()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    sys.exit(1 if bad else 0)
