"""Campaigns, the background sender and the reply poller.

Safety rules baked in:
  * Never message do-not-contact, archived or bounced leads.
  * One message per lead per campaign (unique index) and, by default, nobody
    contacted in the last N days (or already queued elsewhere) is picked again.
  * Daily caps, delay between messages (with jitter for email) and sending hours.
  * Account problems (bad login, no SMS balance) pause the campaign instead of
    burning through the queue; five failures in a row also pauses it.
  * A crash mid-send marks the message failed rather than re-sending it.
  * Follow-ups: a campaign can carry up to three follow-ups. Each one goes, written by the AI, only to leads of the
    campaign before it who have not replied once enough days have passed since their last message of any kind.
    Cancelling the first campaign cancels its follow-ups.
  * AI campaigns (ai_personalize): the message box is a brief and the AI writes each lead's own message just
    before it is sent, using everything known about that lead. A message nearly identical to another one in
    the campaign is reworded once, then held back rather than sent.
"""
import json
import random
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import ai
import db
import leads as L
import messaging
from messaging import SendError
from util import days_ago, now, today_start

CHANNELS = ("email", "sms")
SIMILAR = 0.9   # two messages in one campaign at least this alike count as the same message


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
                 f"AND (m.channel = ? OR m.channel = 'whatsapp') AND {recent})")   # a WhatsApp you sent by hand counts too
    args += [channel] + recent_args

    sql = f"SELECT l.* FROM leads l WHERE {' AND '.join(where)} ORDER BY l.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def validate_campaign(channel, subject, body, personalize=False):
    if channel not in CHANNELS:
        raise ValueError("Channel must be email or sms")
    if not (body or "").strip():
        raise ValueError("Tell the AI what the messages should say" if personalize else "Message text can't be empty")
    if personalize:
        return   # the AI writes the subject and text itself
    if channel == "email" and not (subject or "").strip():
        raise ValueError("Email subject can't be empty")
    bad = messaging.unknown_placeholders(subject, body)
    if bad:
        raise ValueError("Unknown placeholder(s): " + ", ".join("{" + b + "}" for b in bad) +
                         ". You can use {name}, {town}, {sector}, {sender}.")


def preview(conn, channel, subject, body, filters, personalize=False):
    validate_campaign(channel, subject, body, personalize)
    s = db.get_settings(conn)
    limit = (filters or {}).get("limit") or None
    full = build_audience(conn, channel, filters, s)
    rows = full[: int(limit)] if limit else full
    sample = None
    if rows and not personalize:
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
    return {"matching": len(full), "will_send": len(rows), "sample": sample, "channel_ready": setup_ok,
            "ai_ready": bool(ai.configured(s))}


def ai_samples(conn, channel, brief, filters, count=3):
    """Drafts for the first few people in the audience, so the owner can judge the AI before starting.
    They are not kept: each real message is written fresh when it is sent."""
    validate_campaign(channel, "", brief, personalize=True)
    s = db.get_settings(conn)
    limit = int((filters or {}).get("limit") or count)
    rows = build_audience(conn, channel, filters, s, min(count, limit))
    if not rows:
        raise ValueError("Nobody matches this audience yet.")
    leads = [L.lead_detail(conn, r["id"]) for r in rows]

    def one(lead):
        d = ai.draft_for_lead(s, lead, channel, brief)
        return {"lead": lead["name"], "to": lead["email"] if channel == "email" else lead["phone_norm"],
                "subject": d["subject"], "body": messaging.final_body(channel, d["body"], s)}
    with ThreadPoolExecutor(max_workers=len(leads)) as pool:
        return list(pool.map(one, leads))


MAX_FOLLOWUPS = 3


def _parse_followups(followups) -> list:
    gaps = []
    for d in followups or []:
        try:
            n = int(d)
        except (TypeError, ValueError):
            raise ValueError("Follow-up days must be whole numbers")
        if not 1 <= n <= 30:
            raise ValueError("A follow-up goes out 1 to 30 days after the last message")
        gaps.append(n)
    if len(gaps) > MAX_FOLLOWUPS:
        raise ValueError(f"At most {MAX_FOLLOWUPS} follow-ups")
    return gaps


def create_campaign(conn, name, channel, subject, body, filters, launch=False, personalize=False, followups=None):
    validate_campaign(channel, subject, body, personalize)
    gaps = _parse_followups(followups)
    if gaps and not ai.configured(db.get_settings(conn)):
        raise ValueError("Follow-ups are written by the AI so each one fits what was said before. "
                         "Add a free AI key in Settings first.")
    name = (name or "").strip() or f"{channel.upper()} campaign {now()}"
    cur = conn.execute(
        "INSERT INTO campaigns(name, channel, subject, body, filters, status, ai_personalize, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (name, channel, "" if personalize else subject or "", body, json.dumps(filters or {}), "draft", int(personalize), now()))
    cid = cur.lastrowid
    brief = body if personalize else f"Follow up on this earlier message: {body[:400]}"
    previous = cid
    for i, gap in enumerate(gaps, 1):
        previous = conn.execute(
            "INSERT INTO campaigns(name, channel, subject, body, filters, status, ai_personalize, followup_of, followup_days, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"{name} · follow-up {i}", channel, "", brief, "{}", "scheduled", 1, previous, gap, now())).lastrowid
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
    if c["ai_personalize"] and not ai.configured(s):
        raise ValueError("Add an AI key in Settings first. Gemini, Groq and NVIDIA all offer free keys.")
    filters = json.loads(c["filters"] or "{}")
    limit = filters.get("limit") or None
    rows = build_audience(conn, c["channel"], filters, s, limit)
    if not rows:
        raise ValueError("No leads match this audience right now.")
    n = 0
    for lead in rows:
        if c["ai_personalize"]:
            subject = body = ""   # the sender writes this lead's message just before it goes out
        else:
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
    elif action == "cancel" and c["status"] in ("running", "paused", "draft", "scheduled"):
        conn.execute("UPDATE messages SET status='skipped', error='Campaign cancelled' "
                     "WHERE campaign_id=? AND status='queued'", (cid,))
        conn.execute("UPDATE campaigns SET status='cancelled', finished_at=? WHERE id=?", (now(), cid))
        for child in conn.execute("SELECT id FROM campaigns WHERE followup_of=? AND status IN "
                                  "('draft','scheduled','running','paused')", (cid,)).fetchall():
            set_campaign_status(conn, child["id"], "cancel")
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
                L.mark_bounced(conn, lead_id, str(e))
        raise
    with db.db() as conn:
        _mark_sent(conn, mid, lead_id, provider, header)
    return {"id": mid, "status": "sent"}


def log_whatsapp(lead_id: int, body: str) -> dict:
    """Record a WhatsApp message the owner is about to send by hand, so it sits in the lead's history, counts
    as contact and lets later replies be matched to it. Returns the wa.me link that opens the chat."""
    from urllib.parse import quote
    text = (body or "").strip()
    if not text:
        raise ValueError("Message can't be empty")
    with db.db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            raise KeyError("Lead not found")
        if lead["do_not_contact"]:
            raise ValueError("This lead is marked do-not-contact.")
        if not lead["phone_norm"] or not lead["is_mobile"]:
            raise ValueError("This lead has no mobile number for WhatsApp.")
        cur = conn.execute(
            "INSERT INTO messages(lead_id, direction, channel, to_addr, body, status, attempts, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)", (lead_id, "out", "whatsapp", lead["phone_norm"], text, "sending", 1, now()))
        _mark_sent(conn, cur.lastrowid, lead_id, "manual", None)
        digits = re.sub(r"\D", "", lead["phone_norm"])
    return {"id": cur.lastrowid, "status": "sent", "url": f"https://wa.me/{digits}?text={quote(text)}"}


def _mark_sent(conn, mid, lead_id, provider_id, header):
    conn.execute("UPDATE messages SET status='sent', sent_at=?, provider_id=?, message_id_header=?, error='' WHERE id=?",
                 (now(), provider_id or "", header, mid))
    conn.execute("UPDATE leads SET last_contacted_at=?, updated_at=?, "
                 "status=CASE WHEN status='new' THEN 'contacted' ELSE status END WHERE id=?", (now(), now(), lead_id))
    db.log_event(conn, lead_id, "sent", "Message sent")


# ------------------------------------------------------------------ follow-ups
def _followup_leads(conn, c, only_due: bool):
    """Leads a follow-up campaign is still to message: they were sent the campaign before it, have never replied,
    can still be reached on this channel and are not in this campaign yet. only_due keeps those whose last
    message, of any kind (so a WhatsApp you sent by hand counts), is at least followup_days old."""
    where = ["l.archived = 0", "l.do_not_contact = 0", "l.status IN ('new','contacted')",
             "EXISTS (SELECT 1 FROM messages p WHERE p.lead_id = l.id AND p.campaign_id = ? "
             "AND p.direction='out' AND p.status='sent')",
             "NOT EXISTS (SELECT 1 FROM messages x WHERE x.lead_id = l.id AND x.campaign_id = ?)",
             "NOT EXISTS (SELECT 1 FROM messages r WHERE r.lead_id = l.id AND r.direction='in' "
             "AND r.grade NOT IN ('auto','bounce'))"]
    args = [c["followup_of"], c["id"]]
    where.append("l.email IS NOT NULL AND l.email_bounced = 0" if c["channel"] == "email"
                 else "l.phone_norm IS NOT NULL AND l.is_mobile = 1")
    if only_due:
        where.append("l.last_contacted_at <= ?")
        args.append(days_ago(int(c["followup_days"])))
    return conn.execute(f"SELECT l.* FROM leads l WHERE {' AND '.join(where)} ORDER BY l.id", args).fetchall()


def release_followups(s: dict = None) -> int:
    """Queue every follow-up that has come due. Returns how many messages were queued."""
    s = s or db.get_settings()
    if not ai.configured(s):
        return 0
    queued = 0
    with db.db() as conn:
        for c in conn.execute("SELECT * FROM campaigns WHERE followup_of IS NOT NULL "
                              "AND status IN ('scheduled','running')").fetchall():
            rows = _followup_leads(conn, c, True)
            for lead in rows:
                to = lead["email"] if c["channel"] == "email" else lead["phone_norm"]
                conn.execute(
                    "INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, subject, body, status, "
                    "scheduled_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (lead["id"], c["id"], "out", c["channel"], to, "", "", "queued", now(), now()))
            if rows:
                conn.execute("UPDATE campaigns SET status='running', total=total+?, launched_at=COALESCE(launched_at, ?), "
                             "finished_at=NULL, last_error='' WHERE id=?", (len(rows), now(), c["id"]))
                queued += len(rows)
    return queued


# ------------------------------------------------------------ AI-written messages
def _squash(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def _near_duplicate(row, body: str) -> bool:
    """Is this text almost the same as one of the last messages in the same campaign?"""
    mine = _squash(body)
    with db.db() as conn:
        others = conn.execute("SELECT body FROM messages WHERE campaign_id=? AND direction='out' AND id<>? AND body<>'' "
                              "ORDER BY id DESC LIMIT 30", (row["campaign_id"], row["id"])).fetchall()
    return any(SequenceMatcher(None, mine, _squash(o["body"]), autojunk=False).ratio() >= SIMILAR for o in others)


def write_for_lead(row, lead: dict, s: dict):
    """(subject, body) the AI writes for one queued message. Nothing with a leftover {blank} or a missing subject
    is ever returned, and a near-copy of another message is reworded once, then refused."""
    ch = row["channel"]
    if not ai.configured(s):
        raise SendError("AI isn't set up yet (Settings → AI writing help).", "pause")
    avoid = ""
    for _ in range(2):
        try:
            draft = ai.draft_for_lead(s, lead, ch, row["brief"], avoid)
        except ai.AIError as e:
            raise SendError(str(e), "transient")
        subject = messaging.render_template(draft["subject"], lead, s) if ch == "email" else ""
        text = messaging.render_template(draft["body"], lead, s)
        if ch == "email" and not subject.strip():
            raise SendError("The AI didn't write a subject line.", "transient")
        if messaging.unknown_placeholders(subject, text):
            raise SendError("The AI left a blank to fill in, like {name}, in the message.", "transient")
        body = messaging.final_body(ch, text, s)
        if not _near_duplicate(row, body):
            return subject, body
        avoid = text
    raise SendError("Too much like another message in this campaign, so it was held back. Retry failed to have it rewritten.",
                    "permanent")


# -------------------------------------------------------------- sender thread
class Sender(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="sender")
        self.stop_event = threading.Event()
        self.next_ok = {"email": 0.0, "sms": 0.0}
        self.last_release = 0.0
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
        if time.time() - self.last_release >= 60:   # follow-ups that came due since last minute
            self.last_release = time.time()
            try:
                release_followups(s)
            except Exception:
                traceback.print_exc()
        waits, sent_any, pending = [], False, False
        for ch in CHANNELS:
            with db.db() as conn:
                row = conn.execute(
                    "SELECT m.*, c.ai_personalize, c.body AS brief FROM messages m JOIN campaigns c ON c.id = m.campaign_id "
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
            detail = L.lead_detail(conn, lead_id) if row["ai_personalize"] and not row["body"] else None

        subject, body = row["subject"], row["body"]
        try:
            if detail:
                subject, body = write_for_lead(row, detail, s)
                if not self._keep_draft(row, subject, body):
                    return
            if ch == "email":
                header = messaging.send_email(to, subject, body, s)
                provider = header
            else:
                header, provider = None, messaging.send_sms(to, body, s)
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

    @staticmethod
    def _keep_draft(row, subject, body) -> bool:
        """Save what the AI wrote on the message, so the log shows exactly what went out. The AI can take a while:
        if the campaign was paused or cancelled meanwhile, put the message back (or drop it) instead of sending."""
        mid = row["id"]
        with db.db() as conn:
            state = conn.execute("SELECT status FROM campaigns WHERE id=?", (row["campaign_id"],)).fetchone()["status"]
            if state == "running":
                conn.execute("UPDATE messages SET subject=?, body=? WHERE id=?", (subject, body, mid))
                return True
            gone = state == "cancelled"
            conn.execute("UPDATE messages SET status=?, error=?, attempts=attempts-? WHERE id=?",
                         ("skipped" if gone else "queued", "Campaign cancelled" if gone else "", 0 if gone else 1, mid))
        return False

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
        if left:
            return
        c = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if c and c["followup_of"] is not None and _followups_may_come(conn, c):
            conn.execute("UPDATE campaigns SET status='scheduled' WHERE id=? AND status='running'", (cid,))
        else:
            conn.execute("UPDATE campaigns SET status='done', finished_at=? WHERE id=? AND status='running'", (now(), cid))


def _followups_may_come(conn, c) -> bool:
    """Is a follow-up campaign still waiting for someone to fall due? Not once the campaign before it is over and
    everyone it reached has replied, opted out or been messaged."""
    parent = conn.execute("SELECT status FROM campaigns WHERE id=?", (c["followup_of"],)).fetchone()
    if parent and parent["status"] not in ("done", "cancelled"):
        return True
    return bool(_followup_leads(conn, c, False))


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
