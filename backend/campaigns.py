"""Campaigns, the background sender and the reply poller.

Safety rules baked in:
  * Never message do-not-contact, archived or bounced leads.
  * ONE first message per lead, ever: anyone who already has a message sent, queued or being sent (on any channel,
    from any campaign or by hand) is never picked for a new campaign. They are only messaged again as a follow-up,
    or by hand once the follow-up wait has passed, or after they reply.
  * Daily caps, delay between messages (with jitter for email) and sending hours.
  * Account problems (bad login, no SMS balance) pause the campaign instead of
    burning through the queue; five failures in a row also pauses it.
  * A crash mid-send marks the message failed rather than re-sending it.
  * Follow-ups: a campaign can carry up to three follow-ups. Each one goes (written by the AI, or ready-made when
    there is no AI key), only to leads of the campaign before it who have not replied once enough days have passed
    since their last message of any kind.
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
import jobs
import leads as L
import library
import messaging
from messaging import SendError
from util import days_ago, now, today_start

CHANNELS = ("email", "sms", "whatsapp")
AUTO_CHANNELS = ("email", "sms")   # sent by the background sender itself. WhatsApp goes through the WhatsApp worker
                                   # (see _whatsapp_tick), or by hand from the queue (wa_next) until WhatsApp is linked.
SIMILAR = 0.9   # two messages in one campaign at least this alike count as the same message


# ------------------------------------------------------------------- audience
def _audience_where(channel: str, filters: dict):
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
    if f.get("lead_ids"):   # the leads ticked by hand
        where.append("l.id IN (SELECT value FROM json_each(?))")
        args.append(json.dumps([int(x) for x in f["lead_ids"]]))
    if f.get("q"):
        where.append("lower(l.name) LIKE ?")
        args.append("%" + str(f["q"]).lower().strip() + "%")
    if f.get("exclude_replied", True):
        where.append("NOT EXISTS (SELECT 1 FROM messages r WHERE r.lead_id = l.id AND r.direction='in' "
                     "AND r.grade NOT IN ('auto','bounce'))")
    # One first message per lead, ever: nobody who already has one sent, waiting or being sent, on any channel.
    where.append("NOT EXISTS (SELECT 1 FROM messages m WHERE m.lead_id = l.id AND m.direction='out' "
                 "AND m.status IN ('queued','sending','sent'))")
    if channel == "whatsapp":
        where.append("NOT EXISTS (SELECT 1 FROM events e WHERE e.lead_id = l.id AND e.kind = 'no_whatsapp')")
    return where, args


def build_audience(conn, channel: str, filters: dict, s: dict, limit=None):
    where, args = _audience_where(channel, filters)
    sql = f"SELECT l.* FROM leads l WHERE {' AND '.join(where)} ORDER BY l.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def count_audience(conn, channel: str, filters: dict) -> int:
    where, args = _audience_where(channel, filters)
    return conn.execute(f"SELECT COUNT(*) FROM leads l WHERE {' AND '.join(where)}", args).fetchone()[0]


def audience_list(conn, channel: str, filters: dict, limit: int = 1000) -> dict:
    """The leads a campaign on this channel could go to, for the pick-your-leads list."""
    where, args = _audience_where(channel, filters)
    total = conn.execute(f"SELECT COUNT(*) FROM leads l WHERE {' AND '.join(where)}", args).fetchone()[0]
    rows = conn.execute(f"SELECT l.id, l.name, l.town, l.sector, l.email, l.phone_norm, l.grade FROM leads l "
                        f"WHERE {' AND '.join(where)} ORDER BY l.name COLLATE NOCASE LIMIT {int(limit)}", args).fetchall()
    return {"total": total, "leads": [
        {"id": r["id"], "name": r["name"], "town": r["town"], "sector": r["sector"],
         "contact": r["email"] if channel == "email" else r["phone_norm"], "grade": r["grade"]} for r in rows]}


def reach_counts(conn) -> dict:
    """How many never-messaged leads each way of reaching them could go to, and which ways are ready to use."""
    s = db.get_settings(conn)
    return {ch: {"leads": count_audience(conn, ch, {}), "ready": channel_ready(ch, s)} for ch in CHANNELS}


def channel_ready(channel: str, s: dict) -> bool:
    if channel == "email":
        return bool(messaging.clean_text(s["smtp_host"]) and messaging.clean_text(s["from_email"]))
    if channel == "sms":
        return bool(messaging.clean_text(s["sms_gateway_url"]) and messaging.clean_text(s["sms_gateway_user"])
                    and messaging.clean_secret(s["sms_gateway_pass"]))
    return True   # WhatsApp always works: automatically once linked, otherwise by hand from the queue


def compose(channel: str, style: str, subject_t: str, body_t: str, lead, s: dict, index: int = 0, step: int = 0):
    """(subject, body, problem) for one lead. 'ready' picks the ready-made message for the lead's kind of business
    (a follow-up message when step > 0); otherwise the owner's own template is filled in. For a text, the result is
    ONE message of at most 160 characters including the opt-out line, or problem says why not."""
    if style == "ready":
        if step > 0:
            subject_t, body_t = library.followup_message(channel, step)
            candidates = [body_t]
        else:
            subject_t, body_t = library.first_message(channel, lead["sector"], index)
            options = list(library.SMS[library.group_for(lead["sector"])])
            candidates = options[index % len(options):] + options[:index % len(options)]
    else:
        candidates = [body_t]
    if channel == "sms":
        text = messaging.fit_sms(candidates, lead, s)
        if text is None:
            return "", "", f"Too long for one text ({messaging.SMS_LIMIT} characters)"
        return "", messaging.final_body("sms", text, s), None
    body = messaging.final_body(channel, messaging.render_template(body_t, lead, s, channel), s)
    subject = messaging.render_template(subject_t, lead, s, channel) if channel == "email" else ""
    return subject, body, None


def validate_campaign(channel, subject, body, personalize=False, style=""):
    if channel not in CHANNELS:
        raise ValueError("Channel must be email, text or WhatsApp")
    if style == "ready":
        return   # the ready-made messages are written for each kind of business
    if not (body or "").strip():
        raise ValueError("Tell the AI what the messages should say" if personalize else "Message text can't be empty")
    if personalize:
        return   # the AI writes the subject and text itself
    if channel == "email" and not (subject or "").strip():
        raise ValueError("Email subject can't be empty")
    bad = messaging.unknown_placeholders(subject, body)
    if bad:
        raise ValueError("Unknown placeholder(s): " + ", ".join("{" + b + "}" for b in bad) +
                         ". You can use {name}, {town}, {sector}, {pain}, {sender}, {website}, {email}, {whatsapp}.")


def preview(conn, channel, subject, body, filters, personalize=False, style=""):
    validate_campaign(channel, subject, body, personalize, style)
    s = db.get_settings(conn)
    limit = (filters or {}).get("limit") or None
    full = build_audience(conn, channel, filters, s)
    rows = full[: int(limit)] if limit else full
    sample, too_long = None, 0
    if rows and not personalize:
        subj, text, problem = compose(channel, style, subject, body, rows[0], s, 0)
        lead = rows[0]
        sample = {
            "lead": lead["name"],
            "to": lead["email"] if channel == "email" else lead["phone_norm"],
            "subject": subj,
            "body": text or problem,
            "length": len(text) if channel == "sms" else 0,
        }
        if channel == "sms":
            too_long = sum(1 for i, r in enumerate(rows[:300]) if compose(channel, style, subject, body, r, s, i)[2])
    return {"matching": len(full), "will_send": len(rows), "sample": sample, "channel_ready": channel_ready(channel, s),
            "ai_ready": bool(ai.configured(s)), "too_long": too_long,
            "whatsapp_linked": bool(db.get_internal("whatsapp_linked"))}


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


def create_campaign(conn, name, channel, subject, body, filters, launch=False, personalize=False, followups=None, style=""):
    style = "ready" if style == "ready" else ""
    validate_campaign(channel, subject, body, personalize, style)
    gaps = _parse_followups(followups)
    s = db.get_settings(conn)
    name = (name or "").strip() or f"{channel.upper()} campaign {now()}"
    cur = conn.execute(
        "INSERT INTO campaigns(name, channel, subject, body, filters, status, ai_personalize, style, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (name, channel, "" if personalize or style else subject or "", body if not style else "", json.dumps(filters or {}),
         "draft", int(personalize), style, now()))
    cid = cur.lastrowid
    # Follow-ups are written by the AI when there is a key and the first message was not a ready-made one;
    # otherwise the ready-made nudge / useful tip / last note is used, so follow-ups never need an AI key.
    use_ai = bool(gaps) and bool(ai.configured(s)) and not style
    brief = body if personalize else f"Follow up on this earlier message: {(body or '')[:400]}"
    previous = cid
    for i, gap in enumerate(gaps, 1):
        previous = _insert_followup(conn, f"{name} · follow-up {i}", channel, previous, gap, i, use_ai, brief)
    if launch:
        launch_campaign(conn, cid)
    return cid


def _insert_followup(conn, name, channel, parent, days, step, use_ai, brief):
    return conn.execute(
        "INSERT INTO campaigns(name, channel, subject, body, filters, status, ai_personalize, style, followup_of, "
        "followup_days, followup_step, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, channel, "", brief if use_ai else "", "{}", "scheduled", int(use_ai), "" if use_ai else "ready", parent,
         days, step, now())).lastrowid


def create_followup(conn, parent_cid: int, days=None, channel=None, use_ai=False) -> int:
    """One tap: follow up everyone in this campaign who got a message and has not replied, after 'days' days,
    on the same channel or another one they can be reached on."""
    parent = conn.execute("SELECT * FROM campaigns WHERE id=?", (parent_cid,)).fetchone()
    if not parent:
        raise KeyError("Campaign not found")
    if parent["status"] in ("draft", "cancelled"):
        raise ValueError("Start this campaign first. A follow-up goes to people who were already messaged.")
    s = db.get_settings(conn)
    channel = channel or parent["channel"]
    if channel not in CHANNELS:
        raise ValueError("Channel must be email, text or WhatsApp")
    days = int(s["followup_wait_days"] if days in (None, "") else days)
    if not 0 <= days <= 30:
        raise ValueError("A follow-up goes out 0 to 30 days after the last message")
    if use_ai and not ai.configured(s):
        raise ValueError("Add a free AI key in Settings first, or use the ready-made follow-up.")
    if conn.execute("SELECT 1 FROM campaigns WHERE followup_of=? AND status IN ('scheduled','running','paused')",
                    (parent_cid,)).fetchone():
        raise ValueError("A follow-up is already waiting for this campaign.")
    step = int(parent["followup_step"] or 0) + 1
    brief = f"Follow up on this earlier message: {(parent['body'] or '')[:400]}"
    cid = _insert_followup(conn, f"{parent['name'].split(' · follow-up')[0]} · follow-up {step}", channel, parent_cid, days,
                           step, use_ai, brief)
    release_followups(s, conn)   # goes out at once if it is already due
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
    if not channel_ready(c["channel"], s):
        raise ValueError({"email": "Email isn't connected yet. Open Settings and connect Gmail first.",
                          "sms": "Your phone isn't connected yet. Open Settings > Text messages first."}[c["channel"]])
    filters = json.loads(c["filters"] or "{}")
    limit = filters.get("limit") or None
    rows = build_audience(conn, c["channel"], filters, s, limit)
    if not rows:
        raise ValueError("No leads match this audience right now.")
    n = 0
    for i, lead in enumerate(rows):
        problem = None
        if c["ai_personalize"]:
            subject = body = ""   # the sender writes this lead's message just before it goes out
        else:
            subject, body, problem = compose(c["channel"], c["style"], c["subject"], c["body"], lead, s, i)
        to = lead["email"] if c["channel"] == "email" else lead["phone_norm"]
        conn.execute(
            "INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, subject, body, status, error, "
            "scheduled_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (lead["id"], cid, "out", c["channel"], to, subject, body, "skipped" if problem else "queued", problem or "",
             now(), now()))
        n += 1
    conn.execute("UPDATE campaigns SET status='running', total=?, launched_at=?, last_error='' WHERE id=?", (n, now(), cid))
    Sender._maybe_finish(conn, cid)   # nothing left to send (every text was too long): finish at once
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
      (SELECT COUNT(DISTINCT m.lead_id) FROM messages m WHERE m.campaign_id=c.id AND m.direction='in' AND m.grade='warm') AS warm,
      (SELECT COUNT(*) FROM campaigns k WHERE k.followup_of=c.id AND k.status IN ('scheduled','running','paused')) AS followups_waiting
    FROM campaigns c """
    if cid is not None:
        r = conn.execute(q + "WHERE c.id=?", (cid,)).fetchone()
        return dict(r) if r else None
    return [dict(r) for r in conn.execute(q + "ORDER BY c.id DESC")]


# ------------------------------------------------------------- one-off sends
def outreach_state(conn, lead_id: int, s: dict = None) -> dict:
    """May this lead be messaged by hand right now? A lead who has never been messaged, who has replied, or whose
    follow-up wait has passed can be; anyone else already has a message out and must not be messaged again yet."""
    s = s or db.get_settings(conn)
    if conn.execute("SELECT 1 FROM messages WHERE lead_id=? AND direction='in' AND grade NOT IN ('auto','bounce') LIMIT 1",
                    (lead_id,)).fetchone():
        return {"allowed": True, "kind": "reply", "reason": ""}
    if conn.execute("SELECT 1 FROM messages WHERE lead_id=? AND direction='out' AND status IN ('queued','sending') LIMIT 1",
                    (lead_id,)).fetchone():
        return {"allowed": False, "kind": "waiting",
                "reason": "A message to them is already waiting in a campaign. They only get one at a time."}
    last = conn.execute("SELECT MAX(COALESCE(sent_at, created_at)) AS t FROM messages WHERE lead_id=? AND direction='out' "
                        "AND status='sent'", (lead_id,)).fetchone()["t"]
    if not last:
        return {"allowed": True, "kind": "first", "reason": ""}
    wait = int(s["followup_wait_days"])
    unlock = datetime.strptime(last, "%Y-%m-%d %H:%M:%S") + timedelta(days=wait)
    if datetime.now() >= unlock:
        return {"allowed": True, "kind": "followup", "reason": ""}
    return {"allowed": False, "kind": "too_soon", "unlock": unlock.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": f"They already have a message from you. A follow-up unlocks on {unlock:%a %d %b}, or sooner if they reply."}


def _elsewhere(conn, lead_id: int, cid) -> bool:
    """Has this lead been messaged outside campaign cid? A first-message campaign must then leave them alone."""
    return bool(conn.execute(
        "SELECT 1 FROM messages WHERE lead_id=? AND direction='out' AND status IN ('sent','sending','queued') "
        "AND (campaign_id IS NULL OR campaign_id <> ?) LIMIT 1", (lead_id, cid)).fetchone())


def send_now(lead_id: int, channel: str, subject: str, body: str) -> dict:
    """Send a single message to one lead immediately (used from the lead panel)."""
    if channel not in CHANNELS:
        raise ValueError("Channel must be email, text or WhatsApp")
    if channel == "whatsapp":
        raise ValueError("WhatsApp is sent from WhatsApp itself: use the WhatsApp button on the lead.")
    s = db.get_settings()
    with db.db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            raise KeyError("Lead not found")
        if lead["do_not_contact"]:
            raise ValueError("This lead is marked do-not-contact.")
        state = outreach_state(conn, lead_id, s)
        if not state["allowed"]:
            raise ValueError(state["reason"])
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
        text = messaging.final_body(channel, messaging.render_template(body, lead, s, channel), s)
        if channel == "sms":
            text = messaging.sms_clean(text)
            if len(text) > messaging.SMS_LIMIT:
                raise ValueError(f"That text is {len(text)} characters. Keep it to {messaging.SMS_LIMIT} so it goes as one SMS.")
        subj = messaging.render_template(subject, lead, s, channel) if channel == "email" else ""
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
        state = outreach_state(conn, lead_id)
        if not state["allowed"]:
            raise ValueError(state["reason"])
        cur = conn.execute(
            "INSERT INTO messages(lead_id, direction, channel, to_addr, body, status, attempts, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)", (lead_id, "out", "whatsapp", lead["phone_norm"], text, "sending", 1, now()))
        _mark_sent(conn, cur.lastrowid, lead_id, "manual", None)
        digits = re.sub(r"\D", "", lead["phone_norm"])
    return {"id": cur.lastrowid, "status": "sent", "url": f"https://wa.me/{digits}?text={quote(text)}"}


def whatsapp_one(lead_id: int, body: str) -> dict:
    """The WhatsApp button on one lead. Linked: the message is queued and the WhatsApp worker sends and records it by
    itself. Not linked: it is logged and the wa.me link is returned so the owner presses send in WhatsApp."""
    if not db.get_internal("whatsapp_linked"):
        return log_whatsapp(lead_id, body)
    text = (body or "").strip()
    if not text:
        raise ValueError("Message can't be empty")
    with db.db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            raise KeyError("Lead not found")
        problem = _wa_lead_problem(lead)
        if problem:
            raise ValueError(problem + ", so it can't be sent.")
        state = outreach_state(conn, lead_id)
        if not state["allowed"]:
            raise ValueError(state["reason"])
        cid = conn.execute(
            "INSERT INTO campaigns(name, channel, subject, body, filters, status, style, followup_step, total, launched_at, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"WhatsApp to {lead['name']}", "whatsapp", "", text, "{}", "running", "single",
             1 if state["kind"] == "followup" else 0, 1, now(), now())).lastrowid
        mid = conn.execute(
            "INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, body, status, scheduled_at, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (lead_id, cid, "out", "whatsapp", lead["phone_norm"], text, "queued", now(), now())).lastrowid
    return {"queued": True, "id": mid, "campaign_id": cid}


# ------------------------------------------------------- WhatsApp queue (you press send)
def _wa_lead_problem(lead):
    if not lead or lead["archived"]:
        return "Lead archived"
    if lead["do_not_contact"]:
        return "Lead is do-not-contact"
    if not lead["phone_norm"] or not lead["is_mobile"]:
        return "No mobile number"
    return None


def wa_next(cid: int):
    """The next lead of a running WhatsApp campaign, with the message ready to open in WhatsApp (None when none is left).
    A lead that can no longer be messaged is skipped here; an AI campaign's message is written now and kept, so it is
    the same one if the page is reopened. Nothing counts as sent until wa_act('sent'). Only used while WhatsApp is
    not linked: once it is, the WhatsApp worker sends and records everything itself."""
    _by_hand_only()
    s = db.get_settings()
    while True:
        with db.db() as conn:
            c = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
            if not c or c["channel"] != "whatsapp":
                raise KeyError("WhatsApp campaign not found")
            if c["status"] == "done":
                return None
            if c["status"] != "running":
                raise ValueError("This campaign is " + ("paused. Resume it to carry on." if c["status"] == "paused"
                                                        else f"{c['status']}, so there is nothing to send."))
            row = conn.execute(
                "SELECT m.*, c.ai_personalize, c.body AS brief, c.followup_step AS step FROM messages m "
                "JOIN campaigns c ON c.id = m.campaign_id "
                "WHERE m.campaign_id=? AND m.direction='out' AND m.status='queued' AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?) "
                "ORDER BY m.id LIMIT 1", (cid, now())).fetchone()
            stats = conn.execute(
                "SELECT COUNT(*) AS total, SUM(status='sent') AS sent, SUM(status='queued') AS left FROM messages "
                "WHERE campaign_id=? AND direction='out'", (cid,)).fetchone()
            if not row:
                Sender._maybe_finish(conn, cid)
                return None
            lead = conn.execute("SELECT * FROM leads WHERE id=?", (row["lead_id"],)).fetchone()
            problem = _wa_lead_problem(lead) or _wa_elsewhere(conn, lead, dict(c))
            if problem:
                conn.execute("UPDATE messages SET status='skipped', error=? WHERE id=?", (problem, row["id"]))
                continue
            detail = L.lead_detail(conn, lead["id"]) if row["ai_personalize"] and not row["body"] else None
        body = row["body"]
        if detail:
            try:
                _, body = write_for_lead(row, detail, s)
            except SendError as e:
                raise ValueError(str(e))
            with db.db() as conn:
                conn.execute("UPDATE messages SET body=? WHERE id=? AND status='queued'", (body, row["id"]))
        return {"id": row["id"], "lead_id": lead["id"], "name": lead["name"], "town": lead["town"], "sector": lead["sector"],
                "phone": lead["phone_norm"], "body": body, "url": _wa_url(lead["phone_norm"], body),
                "total": stats["total"], "sent": stats["sent"] or 0, "left": stats["left"] or 0}


def _by_hand_only():
    if db.get_internal("whatsapp_linked"):
        raise ValueError("WhatsApp is connected, so these messages are sent for you. Nothing to do by hand.")


def _wa_elsewhere(conn, lead, campaign):
    """A first-message campaign leaves anyone alone who was messaged some other way (follow-ups are exempt)."""
    if campaign["followup_of"] is None and not campaign.get("followup_step") and _elsewhere(conn, lead["id"], campaign["id"]):
        return "Already messaged another way"
    return None


def _wa_url(phone: str, body: str) -> str:
    from urllib.parse import quote
    digits = re.sub(r"\D", "", phone)
    return f"https://wa.me/{digits}?text={quote(body)}"


def wa_act(mid: int, action: str, body: str = None) -> dict:
    """link: keep the (possibly edited) text and return the wa.me address. sent: you pressed send, so it counts as
    contact and replies can be matched to it. no_whatsapp: that number isn't on WhatsApp, so it is skipped here and
    stays available for email. skip: leave this lead out of this campaign."""
    if action not in ("link", "sent", "no_whatsapp", "skip"):
        raise ValueError("Unknown action")
    _by_hand_only()
    with db.db() as conn:
        m = conn.execute("SELECT m.*, c.status AS cstatus FROM messages m JOIN campaigns c ON c.id=m.campaign_id "
                         "WHERE m.id=? AND m.direction='out' AND m.channel='whatsapp'", (mid,)).fetchone()
        if not m:
            raise KeyError("Message not found")
        if m["status"] != "queued":
            raise ValueError("That message was already dealt with.")
        if m["cstatus"] != "running":
            raise ValueError("This campaign is not running.")
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (m["lead_id"],)).fetchone()
        text = (body if body is not None else m["body"]).strip()
        if action in ("link", "sent"):
            problem = _wa_lead_problem(lead)
            if problem:
                raise ValueError(problem + ", so it can't be sent.")
            if not text:
                raise ValueError("Message can't be empty")
            if body is not None:
                conn.execute("UPDATE messages SET body=? WHERE id=?", (text, mid))
        if action == "link":
            return {"url": _wa_url(lead["phone_norm"], text)}
        if action == "sent":
            _mark_sent(conn, mid, lead["id"], "manual", None)
        elif action == "no_whatsapp":
            conn.execute("UPDATE messages SET status='skipped', error='Not on WhatsApp' WHERE id=?", (mid,))
            db.log_event(conn, lead["id"], "no_whatsapp", "Number is not on WhatsApp" + (" - email them instead" if lead["email"] else ""))
        else:
            conn.execute("UPDATE messages SET status='skipped', error='Skipped by you' WHERE id=?", (mid,))
        Sender._maybe_finish(conn, m["campaign_id"])
    return {"status": "sent" if action == "sent" else "skipped"}


# --------------------------------------------- WhatsApp worker (sends through WhatsApp Web, records by itself)
def wa_next_id():
    """Id of the next WhatsApp message the worker should send, or None."""
    with db.db() as conn:
        row = conn.execute(
            "SELECT m.id FROM messages m JOIN campaigns c ON c.id = m.campaign_id WHERE m.direction='out' "
            "AND m.status='queued' AND m.channel='whatsapp' AND c.status='running' "
            "AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?) ORDER BY m.id LIMIT 1", (now(),)).fetchone()
    return row["id"] if row else None


def wa_prepare(mid: int):
    """Claim one queued WhatsApp message and get its text ready. Returns {'id','lead_id','phone','body'} or None when it
    was skipped (lead no longer messageable, already messaged another way, campaign stopped) or couldn't be written."""
    s = db.get_settings()
    with db.db() as conn:
        row = conn.execute(
            "SELECT m.*, c.ai_personalize, c.body AS brief, c.followup_step AS step, c.followup_of, c.status AS cstatus "
            "FROM messages m JOIN campaigns c ON c.id = m.campaign_id WHERE m.id=?", (mid,)).fetchone()
        if not row or row["status"] != "queued" or row["cstatus"] != "running":
            return None
        if not conn.execute("UPDATE messages SET status='sending', attempts=attempts+1 WHERE id=? AND status='queued'",
                            (mid,)).rowcount:
            return None
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (row["lead_id"],)).fetchone()
        problem = _wa_lead_problem(lead) or _wa_elsewhere(
            conn, lead, {"id": row["campaign_id"], "followup_of": row["followup_of"], "followup_step": row["step"]})
        if problem:
            conn.execute("UPDATE messages SET status='skipped', error=? WHERE id=?", (problem, mid))
            Sender._maybe_finish(conn, row["campaign_id"])
            return None
        detail = L.lead_detail(conn, lead["id"]) if row["ai_personalize"] and not row["body"] else None
    body = row["body"]
    if detail:
        try:
            _, body = write_for_lead(row, detail, s)
        except SendError as e:
            wa_result(mid, "failed" if e.kind == "permanent" else "retry", str(e))
            return None
        with db.db() as conn:
            conn.execute("UPDATE messages SET body=? WHERE id=?", (body, mid))
    return {"id": mid, "lead_id": lead["id"], "phone": lead["phone_norm"], "body": body, "name": lead["name"]}


def wa_result(mid: int, outcome: str, detail: str = ""):
    """What happened to a claimed WhatsApp message: sent | not_on_whatsapp | retry (try again later) |
    requeue (put back untouched, e.g. WhatsApp was logged out) | failed."""
    with db.db() as conn:
        m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not m or m["status"] != "sending":
            return
        cid, lead_id = m["campaign_id"], m["lead_id"]
        if outcome == "sent":
            _mark_sent(conn, mid, lead_id, "whatsapp-web", None)
        elif outcome == "not_on_whatsapp":
            conn.execute("UPDATE messages SET status='skipped', error='Not on WhatsApp' WHERE id=?", (mid,))
            lead = conn.execute("SELECT email FROM leads WHERE id=?", (lead_id,)).fetchone()
            db.log_event(conn, lead_id, "no_whatsapp", "Number is not on WhatsApp" + (" - email them instead" if lead["email"] else ""))
        elif outcome == "requeue":
            conn.execute("UPDATE messages SET status='queued', attempts=attempts-1, error=? WHERE id=?", (detail, mid))
        elif outcome == "retry" and m["attempts"] < 3:
            retry_at = (datetime.now() + timedelta(minutes=5 * m["attempts"])).isoformat(sep=" ", timespec="seconds")
            conn.execute("UPDATE messages SET status='queued', error=?, scheduled_at=? WHERE id=?", (detail, retry_at, mid))
        else:
            conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (detail, mid))
        if cid:
            Sender._maybe_finish(conn, cid)


def wa_pause_campaigns(reason: str):
    """Stop every running WhatsApp campaign (WhatsApp logged out, or sending keeps failing) so nothing is burned."""
    with db.db() as conn:
        conn.execute("UPDATE campaigns SET status='paused', last_error=? WHERE channel='whatsapp' AND status='running'", (reason,))


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


def release_followups(s: dict = None, conn=None) -> int:
    """Queue every follow-up that has come due. Returns how many messages were queued."""
    s = s or db.get_settings()
    own = conn is None
    ctx = db.db() if own else None
    conn = ctx.__enter__() if own else conn
    try:
        queued = 0
        for c in conn.execute("SELECT * FROM campaigns WHERE followup_of IS NOT NULL "
                              "AND status IN ('scheduled','running')").fetchall():
            if c["ai_personalize"] and not ai.configured(s):
                continue   # an AI follow-up waits until there is a key
            rows = _followup_leads(conn, c, True)
            for i, lead in enumerate(rows):
                subject = body = problem = ""
                if not c["ai_personalize"]:   # ready-made: the nudge / tip / last note for this step
                    subject, body, problem = compose(c["channel"], "ready", "", "", lead, s, i, max(int(c["followup_step"] or 1), 1))
                to = lead["email"] if c["channel"] == "email" else lead["phone_norm"]
                conn.execute(
                    "INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, subject, body, status, error, "
                    "scheduled_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (lead["id"], c["id"], "out", c["channel"], to, subject, body, "skipped" if problem else "queued",
                     problem or "", now(), now()))
            if rows:
                conn.execute("UPDATE campaigns SET status='running', total=total+?, launched_at=COALESCE(launched_at, ?), "
                             "finished_at=NULL, last_error='' WHERE id=?", (len(rows), now(), c["id"]))
                queued += len(rows)
                Sender._maybe_finish(conn, c["id"])
        return queued
    finally:
        if own:
            ctx.__exit__(None, None, None)


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
    is ever returned, and a near-copy of another message is reworded once, then refused. A text that the AI
    cannot get into 160 characters is replaced by the ready-made one rather than split into two."""
    ch = row["channel"]
    if not ai.configured(s):
        raise SendError("AI isn't set up yet (Settings → AI writing help).", "pause")
    avoid = ""
    # room for the AI's own words: 160 less the opt-out line
    room = messaging.SMS_LIMIT - len(messaging.final_body("sms", "", s)) if ch == "sms" else 0
    for _ in range(3 if ch == "sms" else 2):
        try:
            draft = ai.draft_for_lead(s, lead, ch, row["brief"], avoid, limit=room)
        except ai.AIError as e:
            raise SendError(str(e), "transient")
        subject = messaging.render_template(draft["subject"], lead, s, ch) if ch == "email" else ""
        text = messaging.render_template(draft["body"], lead, s, ch)
        if ch == "email" and not subject.strip():
            raise SendError("The AI didn't write a subject line.", "transient")
        if messaging.unknown_placeholders(subject, text):
            raise SendError("The AI left a blank to fill in, like {name}, in the message.", "transient")
        if ch == "sms":
            text = messaging.sms_clean(text)
            if messaging.sms_total(text, s) > messaging.SMS_LIMIT:
                avoid = text   # too long for one text: ask again, shorter
                continue
        body = messaging.final_body(ch, text, s)
        if not _near_duplicate(row, body):
            return subject, body
        avoid = text
    if ch == "sms":
        _, body, problem = compose("sms", "ready", "", "", lead, s, row["id"], int(row["step"] or 0))
        if not problem:
            return "", body
    raise SendError("Too much like another message in this campaign, so it was held back. Retry failed to have it rewritten.",
                    "permanent")


# -------------------------------------------------------------- sender thread
class Sender(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="sender")
        self.stop_event = threading.Event()
        self.next_ok = {ch: 0.0 for ch in AUTO_CHANNELS}
        self.last_release = 0.0
        self.last_wa_spawn = 0.0
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
        for ch in AUTO_CHANNELS:
            with db.db() as conn:
                row = conn.execute(
                    "SELECT m.*, c.ai_personalize, c.body AS brief, c.followup_step AS step, c.followup_of FROM messages m "
                    "JOIN campaigns c ON c.id = m.campaign_id "
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
        wa_wait = self._whatsapp_tick(s)
        if wa_wait:
            pending = True
            waits.append(wa_wait)
        if not pending:
            self._set("idle", "")
            return 3.0
        if sent_any:
            return 0.5
        return max(0.5, min(waits)) if waits else 3.0

    def _whatsapp_tick(self, s):
        """Starts the WhatsApp worker while there is something to send (and sending hours and the daily limit allow).
        Returns how long to wait before looking again, or None when there is nothing WhatsApp to do."""
        if not db.get_internal("whatsapp_linked"):
            return None
        with db.db() as conn:
            queued = conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN campaigns c ON c.id = m.campaign_id WHERE m.channel='whatsapp' "
                "AND m.direction='out' AND m.status='queued' AND c.status='running' "
                "AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?)", (now(),)).fetchone()[0]
            if not queued:
                return None
            if jobs.active_job(conn, "whatsapp") or jobs.active_job(conn, "whatsapp_link"):
                return 5.0
            sent_today = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='out' AND channel='whatsapp' "
                                      "AND status='sent' AND sent_at >= ?", (today_start(),)).fetchone()[0]
        if not self.in_window(s):
            self._set("waiting", f"Outside sending hours ({s['send_window_start']}–{s['send_window_end']})")
            return 30.0
        if sent_today >= int(s["whatsapp_daily_cap"]):
            self._set("waiting", f"Daily whatsapp limit reached ({sent_today})")
            return 60.0
        if time.time() - self.last_wa_spawn < 45:   # a worker that stops at once is not restarted in a loop
            return 10.0
        self.last_wa_spawn = time.time()
        with db.db() as conn:
            jid = jobs.create_job(conn, "whatsapp", {})
        jobs.spawn(jid, "whatsapp")
        self._set("sending", "Sending on WhatsApp")
        return 5.0

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
            elif row["followup_of"] is None and _elsewhere(conn, lead_id, cid):
                reason = "Already messaged another way"
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
            if ch == "sms" and len(body) > messaging.SMS_LIMIT:
                raise SendError(f"Too long for one text ({len(body)} of {messaging.SMS_LIMIT} characters)", "permanent")
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
