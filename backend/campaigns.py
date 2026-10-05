"""Campaigns, the background sender and the reply poller.

Safety rules baked in:
  * Never message do-not-contact, archived or bounced leads.
  * One message per lead per campaign (unique index) and, by default, nobody
    contacted in the last N days (or already queued elsewhere) is picked again.
  * Daily caps, delay between messages (with jitter for email) and sending hours.
  * Account problems (bad login, no SMS balance) pause the campaign instead of
    burning through the queue; five failures in a row also pauses it.
  * A crash mid-send marks the message failed rather than re-sending it.
"""
import json
import random
import threading
import time
import traceback
from datetime import datetime, timedelta

import db
import messaging
from messaging import SendError
from util import days_ago, now, today_start

CHANNELS = ("email", "sms")


# ------------------------------------------------------------------- audience
def build_audience(conn, channel: str, filters: dict, s: dict, limit=None):
    f = filters or {}
    where = ["l.archived = 0", "l.do_not_contact = 0"]
    args = []
    if channel == "email":
        where.append("l.email IS NOT NULL AND l.email_bounced = 0")
    else:
        where.append("l.phone_norm IS NOT NULL AND l.is_mobile = 1")

    statuses = f.get("statuses") or ["new"]
    where.append(f"l.status IN ({','.join('?' * len(statuses))})")
    args += statuses
    if f.get("sectors"):
        where.append(f"lower(l.sector) IN ({','.join('?' * len(f['sectors']))})")
        args += [x.lower() for x in f["sectors"]]
    if f.get("towns"):
        where.append(f"lower(l.town) IN ({','.join('?' * len(f['towns']))})")
        args += [x.lower() for x in f["towns"]]
    if f.get("grades"):
        grades = ["" if g == "none" else g for g in f["grades"]]
        where.append(f"l.grade IN ({','.join('?' * len(grades))})")
        args += grades
    if f.get("exclude_replied", True):
        where.append("NOT EXISTS (SELECT 1 FROM messages r WHERE r.lead_id = l.id AND r.direction='in' "
                     "AND r.grade NOT IN ('auto','bounce'))")

    skip_days = int(f.get("skip_recent_days", s.get("skip_recent_days", 14)) or 0)
    recent = "(m.status IN ('queued','sending')"
    recent_args = []
    if skip_days > 0:
        recent += " OR (m.status='sent' AND m.sent_at >= ?)"
        recent_args.append(days_ago(skip_days))
    recent += ")"
    where.append("NOT EXISTS (SELECT 1 FROM messages m WHERE m.lead_id = l.id AND m.direction='out' "
                 f"AND m.channel = ? AND {recent})")
    args += [channel] + recent_args

    sql = f"SELECT l.* FROM leads l WHERE {' AND '.join(where)} ORDER BY l.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def validate_campaign(channel, subject, body):
    if channel not in CHANNELS:
        raise ValueError("Channel must be email or sms")
    if not (body or "").strip():
        raise ValueError("Message text can't be empty")
    if channel == "email" and not (subject or "").strip():
        raise ValueError("Email subject can't be empty")
    bad = messaging.unknown_placeholders(subject, body)
    if bad:
        raise ValueError("Unknown placeholder(s): " + ", ".join("{" + b + "}" for b in bad) +
                         ". You can use {name}, {town}, {sector}, {sender}.")


def preview(conn, channel, subject, body, filters):
    validate_campaign(channel, subject, body)
    s = db.get_settings(conn)
    limit = (filters or {}).get("limit") or None
    full = build_audience(conn, channel, filters, s)
    rows = full[: int(limit)] if limit else full
    sample = None
    if rows:
        lead = rows[0]
        rb = messaging.final_body(channel, messaging.render_template(body, lead, s), s)
        sample = {
            "lead": lead["name"],
            "to": lead["email"] if channel == "email" else lead["phone_norm"],
            "subject": messaging.render_template(subject, lead, s) if channel == "email" else "",
            "body": rb,
            "segments": messaging.sms_segments(rb) if channel == "sms" else 0,
        }
    setup_ok = bool(s["smtp_host"] and s["from_email"]) if channel == "email" else bool(s["at_username"] and s["at_api_key"])
    return {"matching": len(full), "will_send": len(rows), "sample": sample, "channel_ready": setup_ok}


def create_campaign(conn, name, channel, subject, body, filters, launch=False):
    validate_campaign(channel, subject, body)
    name = (name or "").strip() or f"{channel.upper()} campaign {now()}"
    cur = conn.execute(
        "INSERT INTO campaigns(name, channel, subject, body, filters, status, created_at) VALUES(?,?,?,?,?,?,?)",
        (name, channel, subject or "", body, json.dumps(filters or {}), "draft", now()))
    cid = cur.lastrowid
    if launch:
        launch_campaign(conn, cid)
    return cid


def launch_campaign(conn, cid: int):
    c = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not c:
        raise KeyError("Campaign not found")
    if c["status"] != "draft":
        raise ValueError("This campaign has already been launched")
    s = db.get_settings(conn)
    filters = json.loads(c["filters"] or "{}")
    limit = filters.get("limit") or None
    rows = build_audience(conn, c["channel"], filters, s, limit)
    if not rows:
        raise ValueError("No leads match this audience right now.")
    n = 0
    for lead in rows:
        body = messaging.final_body(c["channel"], messaging.render_template(c["body"], lead, s), s)
        subject = messaging.render_template(c["subject"], lead, s) if c["channel"] == "email" else ""
        to = lead["email"] if c["channel"] == "email" else lead["phone_norm"]
        conn.execute(
            "INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, subject, body, status, "
            "scheduled_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (lead["id"], cid, "out", c["channel"], to, subject, body, "queued", now(), now()))
        n += 1
    conn.execute("UPDATE campaigns SET status='running', total=?, launched_at=?, last_error='' WHERE id=?", (n, now(), cid))
    return n


def set_campaign_status(conn, cid: int, action: str):
    c = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not c:
        raise KeyError("Campaign not found")
    if action == "pause" and c["status"] == "running":
        conn.execute("UPDATE campaigns SET status='paused' WHERE id=?", (cid,))
    elif action == "resume" and c["status"] == "paused":
        conn.execute("UPDATE campaigns SET status='running', last_error='' WHERE id=?", (cid,))
    elif action == "cancel" and c["status"] in ("running", "paused", "draft"):
        conn.execute("UPDATE messages SET status='skipped', error='Campaign cancelled' "
                     "WHERE campaign_id=? AND status='queued'", (cid,))
        conn.execute("UPDATE campaigns SET status='cancelled', finished_at=? WHERE id=?", (now(), cid))
    else:
        raise ValueError(f"Can't {action} a campaign that is {c['status']}")


def campaign_stats(conn, cid=None):
    q = """
    SELECT c.*,
      (SELECT COUNT(*) FROM messages m WHERE m.campaign_id=c.id AND m.direction='out' AND m.status='sent') AS sent,
      (SELECT COUNT(*) FROM messages m WHERE m.campaign_id=c.id AND m.direction='out' AND m.status='queued') AS queued,
      (SELECT COUNT(*) FROM messages m WHERE m.campaign_id=c.id AND m.direction='out' AND m.status='failed') AS failed,
      (SELECT COUNT(*) FROM messages m WHERE m.campaign_id=c.id AND m.direction='out' AND m.status='skipped') AS skipped,
      (SELECT COUNT(DISTINCT m.lead_id) FROM messages m WHERE m.campaign_id=c.id AND m.direction='in'
          AND m.grade NOT IN ('auto','bounce')) AS replies,
      (SELECT COUNT(DISTINCT m.lead_id) FROM messages m WHERE m.campaign_id=c.id AND m.direction='in' AND m.grade='hot') AS hot,
      (SELECT COUNT(DISTINCT m.lead_id) FROM messages m WHERE m.campaign_id=c.id AND m.direction='in' AND m.grade='warm') AS warm
    FROM campaigns c """
    if cid is not None:
        r = conn.execute(q + "WHERE c.id=?", (cid,)).fetchone()
        return dict(r) if r else None
    return [dict(r) for r in conn.execute(q + "ORDER BY c.id DESC")]


# ------------------------------------------------------------- one-off sends
def send_now(lead_id: int, channel: str, subject: str, body: str) -> dict:
    """Send a single message to one lead immediately (used from the lead panel)."""
    if channel not in CHANNELS:
        raise ValueError("Channel must be email or sms")
    s = db.get_settings()
    with db.db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            raise KeyError("Lead not found")
        if lead["do_not_contact"]:
            raise ValueError("This lead is marked do-not-contact.")
        if channel == "email":
            if not lead["email"] or lead["email_bounced"]:
                raise ValueError("This lead has no working email address.")
            if not (subject or "").strip():
                raise ValueError("Subject can't be empty")
        elif not lead["phone_norm"] or not lead["is_mobile"]:
            raise ValueError("This lead has no mobile number to text.")
        if not (body or "").strip():
            raise ValueError("Message can't be empty")
        bad = messaging.unknown_placeholders(subject, body)
        if bad:
            raise ValueError("Unknown placeholder(s): " + ", ".join(bad))
        text = messaging.final_body(channel, messaging.render_template(body, lead, s), s)
        subj = messaging.render_template(subject, lead, s) if channel == "email" else ""
        to = lead["email"] if channel == "email" else lead["phone_norm"]
        cur = conn.execute(
            "INSERT INTO messages(lead_id, direction, channel, to_addr, subject, body, status, attempts, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (lead_id, "out", channel, to, subj, text, "sending", 1, now()))
        mid = cur.lastrowid
    try:
        if channel == "email":
            header = messaging.send_email(to, subj, text, s)
            provider = header
        else:
            header, provider = None, messaging.send_sms(to, text, s)
    except SendError as e:
        with db.db() as conn:
            conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(e), mid))
            if channel == "email" and "Recipient refused" in str(e):
                import leads as L
                L.mark_bounced(conn, lead_id, str(e))
        raise
    with db.db() as conn:
        _mark_sent(conn, mid, lead_id, provider, header)
    return {"id": mid, "status": "sent"}


def _mark_sent(conn, mid, lead_id, provider_id, header):
    conn.execute("UPDATE messages SET status='sent', sent_at=?, provider_id=?, message_id_header=?, error='' WHERE id=?",
                 (now(), provider_id or "", header, mid))
    conn.execute("UPDATE leads SET last_contacted_at=?, updated_at=?, "
                 "status=CASE WHEN status='new' THEN 'contacted' ELSE status END WHERE id=?", (now(), now(), lead_id))
    db.log_event(conn, lead_id, "sent", "Message sent")


# -------------------------------------------------------------- sender thread
class Sender(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="sender")
        self.stop_event = threading.Event()
        self.next_ok = {"email": 0.0, "sms": 0.0}
        self.fails = {}
        self.status = {"state": "idle", "detail": ""}

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            with db.db() as conn:
                conn.execute("UPDATE messages SET status='failed', error='Interrupted while sending - may or may not "
                             "have been delivered, check before retrying' WHERE status='sending' AND direction='out'")
        except Exception:
            traceback.print_exc()
        while not self.stop_event.is_set():
            try:
                wait = self.tick()
            except Exception:
                traceback.print_exc()
                wait = 10
            self.stop_event.wait(wait)

    # -- helpers
    def _set(self, state, detail=""):
        self.status = {"state": state, "detail": detail}

    @staticmethod
    def in_window(s, at=None):
        if not s.get("send_window_enabled", True):
            return True
        at = at or datetime.now()
        try:
            start = datetime.strptime(s["send_window_start"], "%H:%M").time()
            end = datetime.strptime(s["send_window_end"], "%H:%M").time()
        except ValueError:
            return True
        return start <= at.time() <= end

    def tick(self) -> float:
        s = db.get_settings()
        waits, sent_any, pending = [], False, False
        for ch in CHANNELS:
            with db.db() as conn:
                row = conn.execute(
                    "SELECT m.* FROM messages m JOIN campaigns c ON c.id = m.campaign_id "
                    "WHERE m.direction='out' AND m.status='queued' AND m.channel=? AND c.status='running' "
                    "AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?) ORDER BY m.id LIMIT 1", (ch, now())).fetchone()
                if not row:
                    continue
                pending = True
                sent_today = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE direction='out' AND channel=? AND status='sent' AND sent_at >= ?",
                    (ch, today_start())).fetchone()[0]
            if not self.in_window(s):
                self._set("waiting", f"Outside sending hours ({s['send_window_start']}–{s['send_window_end']})")
                waits.append(30)
                continue
            if sent_today >= int(s[f"{ch}_daily_cap"]):
                self._set("waiting", f"Daily {ch} limit reached ({sent_today})")
                waits.append(60)
                continue
            gap = self.next_ok[ch] - time.time()
            if gap > 0:
                self._set("waiting", f"Next {ch} in {int(gap)}s")
                waits.append(gap)
                continue
            self._set("sending", f"Sending {ch} to {row['to_addr']}")
            self.send_one(row, s)
            sent_any = True
            delay = float(s[f"{ch}_delay_sec"])
            self.next_ok[ch] = time.time() + delay * (random.uniform(0.8, 1.4) if ch == "email" else 1.0)
        if not pending:
            self._set("idle", "")
            return 3.0
        if sent_any:
            return 0.5
        return max(0.5, min(waits)) if waits else 3.0

    def send_one(self, row, s):
        mid, lead_id, ch, cid = row["id"], row["lead_id"], row["channel"], row["campaign_id"]
        with db.db() as conn:
            claimed = conn.execute(
                "UPDATE messages SET status='sending', attempts=attempts+1 WHERE id=? AND status='queued'", (mid,)).rowcount
            if not claimed:
                return
            lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            reason = None
            if not lead or lead["archived"]:
                reason = "Lead archived"
            elif lead["do_not_contact"]:
                reason = "Lead is do-not-contact"
            elif ch == "email" and (not lead["email"] or lead["email_bounced"]):
                reason = "No working email"
            elif ch == "sms" and (not lead["phone_norm"] or not lead["is_mobile"]):
                reason = "No mobile number"
            if reason:
                conn.execute("UPDATE messages SET status='skipped', error=? WHERE id=?", (reason, mid))
                self._maybe_finish(conn, cid)
                return
            to = lead["email"] if ch == "email" else lead["phone_norm"]

        try:
            if ch == "email":
                header = messaging.send_email(to, row["subject"], row["body"], s)
                provider = header
            else:
                header, provider = None, messaging.send_sms(to, row["body"], s)
        except SendError as e:
            self._on_error(row, e)
            return
        except Exception as e:  # unexpected bug - don't loop forever on it
            traceback.print_exc()
            self._on_error(row, SendError(f"Unexpected error: {e}", "permanent"))
            return

        self.fails[cid] = 0
        with db.db() as conn:
            _mark_sent(conn, mid, lead_id, provider, header)
            self._maybe_finish(conn, cid)

    def _on_error(self, row, e: SendError):
        mid, lead_id, ch, cid = row["id"], row["lead_id"], row["channel"], row["campaign_id"]
        with db.db() as conn:
            attempts = conn.execute("SELECT attempts FROM messages WHERE id=?", (mid,)).fetchone()["attempts"]
            if e.kind == "pause":
                conn.execute("UPDATE messages SET status='queued', attempts=attempts-1, error=? WHERE id=?", (str(e), mid))
                conn.execute("UPDATE campaigns SET status='paused', last_error=? WHERE channel=? AND status='running'",
                             (str(e), ch))
                self._set("paused", str(e))
                return
            if e.kind == "transient" and attempts < 3:
                retry_at = (datetime.now() + timedelta(minutes=5 * attempts)).isoformat(sep=" ", timespec="seconds")
                conn.execute("UPDATE messages SET status='queued', error=?, scheduled_at=? WHERE id=?",
                             (str(e), retry_at, mid))
            else:
                conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(e), mid))
                if ch == "email" and "Recipient refused" in str(e):
                    import leads as L
                    L.mark_bounced(conn, lead_id, str(e))
            self.fails[cid] = self.fails.get(cid, 0) + 1
            if self.fails[cid] >= 5:
                conn.execute("UPDATE campaigns SET status='paused', last_error=? WHERE id=?",
                             ("5 messages in a row failed - check your settings, then resume. Last error: " + str(e), cid))
                self.fails[cid] = 0
            self._maybe_finish(conn, cid)

    @staticmethod
    def _maybe_finish(conn, cid):
        left = conn.execute("SELECT COUNT(*) FROM messages WHERE campaign_id=? AND direction='out' "
                            "AND status IN ('queued','sending')", (cid,)).fetchone()[0]
        if left == 0:
            conn.execute("UPDATE campaigns SET status='done', finished_at=? WHERE id=? AND status='running'", (now(), cid))


# --------------------------------------------------------------- reply poller
class Poller(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="imap-poller")
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.last = {"at": None, "ok": None, "message": "Not run yet", "stats": {}}

    def stop(self):
        self.stop_event.set()

    def poll_now(self) -> dict:
        with self.lock:
            s = db.get_settings()
            try:
                stats = messaging.poll_imap(s)
                self.last = {"at": now(), "ok": True, "message": "Checked inbox", "stats": stats}
            except SendError as e:
                self.last = {"at": now(), "ok": False, "message": str(e), "stats": {}}
                raise
            return stats

    def run(self):
        while not self.stop_event.is_set():
            s = db.get_settings()
            minutes = max(1, int(s.get("imap_poll_minutes") or 5))
            if s.get("imap_host") and s.get("imap_user"):
                try:
                    self.poll_now()
                except SendError:
                    pass
                except Exception:
                    traceback.print_exc()
            self.stop_event.wait(minutes * 60)
