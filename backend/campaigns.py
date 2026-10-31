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
import backup
import db
import ecosystem
import jobs
import leads as L
import library
import messaging
import perf
from messaging import SendError
from util import days_ago, normalize_phone, now, today_start

REAL_CHANNELS = ("email", "sms", "whatsapp")
CHANNELS = REAL_CHANNELS + ("auto",)   # "auto": the best way for each lead, chosen per lead (see pick_channel)
AUTO_CHANNELS = ("email", "sms")   # sent by the background sender itself. WhatsApp goes through the WhatsApp worker
                                   # (see _whatsapp_tick), or by hand from the queue (wa_next) until WhatsApp is linked.
SIMILAR = 0.9   # two messages in one campaign at least this alike count as the same message
# A send that was pressed but never confirmed, or cut off mid-send, "may or may not have been delivered". That lead must be
# treated as already messaged, otherwise a new campaign would message them a second time.
AMBIGUOUS = "(m.status = 'failed' AND m.error LIKE '%may or may not%')"
MIN_GAP_MINUTES = 60   # a campaign never sends a lead two messages within this long, on any channel (see _gap_hold)


# ------------------------------------------------------------------- audience
def _audience_where(channel: str, filters: dict):
    f = filters or {}
    where = ["l.archived = 0", "l.do_not_contact = 0"]
    args = []
    if channel == "email":
        where.append("l.email IS NOT NULL AND l.email_bounced = 0")
    elif channel == "auto":   # reachable some way: narrowed to the ways in use by _auto_rows
        where.append("((l.email IS NOT NULL AND l.email_bounced = 0) OR (l.phone_norm IS NOT NULL AND l.is_mobile = 1))")
    elif channel is None:     # no way chosen: used to count every way in one pass (reach_counts)
        pass
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
                 "AND (m.status IN ('queued','sending','sent') OR " + AMBIGUOUS + "))")
    if channel == "whatsapp":
        where.append("NOT EXISTS (SELECT 1 FROM events e WHERE e.lead_id = l.id AND e.kind = 'no_whatsapp')")
    return where, args


def _auto_rows(conn, rows, s: dict):
    """For 'best way' campaigns: [(lead, the way they will be reached)], leaving out leads no usable way reaches."""
    usable = usable_channels(s)
    no_wa = _no_whatsapp_ids(conn) if "whatsapp" in usable else set()
    out = []
    for r in rows:
        ch = pick_channel(conn, r, s, usable, no_whatsapp=no_wa)
        if ch:
            out.append((r, ch))
    return out


def build_audience(conn, channel: str, filters: dict, s: dict, limit=None):
    where, args = _audience_where(channel, filters)
    sql = f"SELECT l.* FROM leads l WHERE {' AND '.join(where)} ORDER BY l.id"
    if channel == "auto":
        rows = [r for r, _ in _auto_rows(conn, conn.execute(sql, args).fetchall(), s)]
        return rows[: int(limit)] if limit else rows
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def count_audience(conn, channel: str, filters: dict) -> int:
    where, args = _audience_where(channel, filters)
    if channel == "auto":
        where = where + [_any_way_sql(usable_channels(db.get_settings(conn)))]
    return conn.execute(f"SELECT COUNT(*) FROM leads l WHERE {' AND '.join(where)}", args).fetchone()[0]


def audience_list(conn, channel: str, filters: dict, limit: int = 1000) -> dict:
    """The leads a campaign on this channel could go to, for the pick-your-leads list."""
    where, args = _audience_where(channel, filters)
    if channel == "auto":
        usable = usable_channels(db.get_settings(conn))
        where = where + [_any_way_sql(usable)]
        total = conn.execute(f"SELECT COUNT(*) FROM leads l WHERE {' AND '.join(where)}", args).fetchone()[0]
        rows = conn.execute(f"SELECT l.id, l.name, l.town, l.sector, l.grade, l.email, l.phone_norm, {_way_sql(usable)} AS via "
                            f"FROM leads l WHERE {' AND '.join(where)} ORDER BY l.name COLLATE NOCASE LIMIT {int(limit)}", args).fetchall()
        return {"total": total, "leads": [
            {"id": r["id"], "name": r["name"], "town": r["town"], "sector": r["sector"], "grade": r["grade"], "via": r["via"],
             "contact": r["email"] if r["via"] == "email" else r["phone_norm"]} for r in rows]}
    total = conn.execute(f"SELECT COUNT(*) FROM leads l WHERE {' AND '.join(where)}", args).fetchone()[0]
    rows = conn.execute(f"SELECT l.id, l.name, l.town, l.sector, l.email, l.phone_norm, l.grade FROM leads l "
                        f"WHERE {' AND '.join(where)} ORDER BY l.name COLLATE NOCASE LIMIT {int(limit)}", args).fetchall()
    return {"total": total, "leads": [
        {"id": r["id"], "name": r["name"], "town": r["town"], "sector": r["sector"], "via": channel,
         "contact": r["email"] if channel == "email" else r["phone_norm"], "grade": r["grade"]} for r in rows]}


def reach_counts(conn) -> dict:
    """How many never-messaged leads each way of reaching them could go to, and which ways are ready to use."""
    s = db.get_settings(conn)
    where, args = _audience_where(None, {})
    # every way's count from ONE pass over the leads (it was one pass per way)
    n = conn.execute(f"SELECT SUM({_WAY_COND['email']}), SUM({_WAY_COND['sms']}), SUM({_WAY_COND['whatsapp']}), "
                     f"SUM({_any_way_sql(usable_channels(s))}) FROM leads l WHERE {' AND '.join(where)}", args).fetchone()
    counts = {"email": n[0] or 0, "sms": n[1] or 0, "whatsapp": n[2] or 0, "auto": n[3] or 0}
    return {ch: {"leads": counts[ch], "ready": channel_ready(ch, s)} for ch in CHANNELS}


def usable_channels(s: dict, linked=None) -> list:
    """The ways that can be used right now without anyone pressing send, best first (the owner's order from Settings).
    WhatsApp counts only once it is connected, so a 'best way' campaign never leaves messages waiting for a tap."""
    linked = bool(db.get_internal("whatsapp_linked")) if linked is None else linked
    order = [c for c in str(s.get("reach_order") or "whatsapp,email,sms").split(",") if c in REAL_CHANNELS]
    return [c for c in order if (linked if c == "whatsapp" else channel_ready(c, s))]


def _can_reach(conn, lead, channel: str, no_whatsapp=None) -> bool:
    """no_whatsapp: ids of leads known not to be on WhatsApp, read once for a whole batch; None = look this lead up."""
    if channel == "email":
        return bool(lead["email"]) and not lead["email_bounced"]
    if not lead["phone_norm"] or not lead["is_mobile"]:
        return False
    if channel == "whatsapp":
        known = (lead["id"] in no_whatsapp) if no_whatsapp is not None else bool(
            conn.execute("SELECT 1 FROM events WHERE lead_id=? AND kind='no_whatsapp' LIMIT 1", (lead["id"],)).fetchone())
        if known:
            return False   # known not to be on WhatsApp
    return True


def pick_channel(conn, lead, s: dict, usable=None, exclude=(), no_whatsapp=None):
    """The one way this lead will be reached: the first usable way they can be reached on, skipping 'exclude'."""
    for ch in (usable_channels(s) if usable is None else usable):
        if ch not in exclude and _can_reach(conn, lead, ch, no_whatsapp):
            return ch
    return None


def _no_whatsapp_ids(conn) -> set:
    return {r[0] for r in conn.execute("SELECT DISTINCT lead_id FROM events WHERE kind='no_whatsapp'")}


# The same choice pick_channel makes, written as SQL, so counting and listing thousands of leads is one query and not a
# lookup per lead. (tests/test_backend.py checks the two always agree.)
_WAY_COND = {
    "email": "(l.email IS NOT NULL AND l.email_bounced = 0)",
    "sms": "(l.phone_norm IS NOT NULL AND l.is_mobile = 1)",
    "whatsapp": "(l.phone_norm IS NOT NULL AND l.is_mobile = 1 AND NOT EXISTS "
                "(SELECT 1 FROM events e WHERE e.lead_id = l.id AND e.kind = 'no_whatsapp'))",
}


def _way_sql(usable) -> str:
    """SQL giving the way each lead is reached (the first usable one that fits them), NULL when none does."""
    return ("CASE " + " ".join(f"WHEN {_WAY_COND[c]} THEN '{c}'" for c in usable) + " END") if usable else "NULL"


def _any_way_sql(usable) -> str:
    return "(" + " OR ".join(_WAY_COND[c] for c in usable) + ")" if usable else "0"


def _prior_channel(conn, lead_id: int):
    row = conn.execute("SELECT channel FROM messages WHERE lead_id=? AND direction='out' AND status='sent' "
                       "ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
    return row["channel"] if row else None


def channel_ready(channel: str, s: dict) -> bool:
    if channel == "auto":
        return bool(usable_channels(s))
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
    if channel in ("email", "auto") and not (subject or "").strip():
        raise ValueError("Email subject can't be empty (an email is one of the ways a lead may be reached)")
    bad = messaging.unknown_placeholders(subject, body)
    if bad:
        raise ValueError("Unknown placeholder(s): " + ", ".join("{" + b + "}" for b in bad) +
                         ". You can use {name}, {town}, {sector}, {pain}, {sender}, {website}, {email}, {whatsapp}.")


def preview(conn, channel, subject, body, filters, personalize=False, style=""):
    validate_campaign(channel, subject, body, personalize, style)
    s = db.get_settings(conn)
    limit = int((filters or {}).get("limit") or 0) or None
    usable = usable_channels(s) if channel == "auto" else None
    if channel == "auto" and limit:   # a capped audience is rare: take the simple route
        via = _auto_rows(conn, conn.execute(f"SELECT l.* FROM leads l WHERE {' AND '.join(_audience_where(channel, filters)[0])} ORDER BY l.id",
                                            _audience_where(channel, filters)[1]).fetchall(), s)
        matching, via = len(via), via[:limit]
        will_send, ways = len(via), {}
        for _, ch in via:
            ways[ch] = ways.get(ch, 0) + 1
        sample_rows = via[:300]
    else:
        matching = count_audience(conn, channel, filters)
        will_send = min(matching, limit) if limit else matching
        if channel == "auto":
            where, args = _audience_where(channel, filters)
            where = where + [_any_way_sql(usable)]
            way = _way_sql(usable)
            ways = dict(conn.execute(f"SELECT way, COUNT(*) FROM (SELECT {way} AS way FROM leads l WHERE {' AND '.join(where)}) "
                                     "GROUP BY way", args).fetchall()) if will_send else {}
            sample_rows = [(r, r["via"]) for r in conn.execute(
                f"SELECT l.*, {way} AS via FROM leads l WHERE {' AND '.join(where)} ORDER BY l.id LIMIT {min(300, will_send)}", args)]
        else:
            ways = {channel: will_send} if will_send else {}
            sample_rows = [(r, channel) for r in build_audience(conn, channel, filters, s, min(300, will_send))] if will_send else []
    sample, too_long = None, 0
    if sample_rows and not personalize:
        lead, ch0 = sample_rows[0]
        subj, text, problem = compose(ch0, style, subject, body, lead, s, 0)
        sample = {"lead": lead["name"], "to": lead["email"] if ch0 == "email" else lead["phone_norm"], "via": ch0,
                  "subject": subj, "body": text or problem, "length": len(text) if ch0 == "sms" else 0}
        too_long = sum(1 for i, (r, ch) in enumerate(sample_rows) if ch == "sms" and compose(ch, style, subject, body, r, s, i)[2])
    return {"matching": matching, "will_send": will_send, "sample": sample, "channel_ready": channel_ready(channel, s),
            "ai_ready": bool(ai.configured(s)), "too_long": too_long, "ways": ways, "usable": usable,
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
    usable = usable_channels(s) if channel == "auto" else None
    # the way each lead would really be reached (read here: a database connection can't be shared with the threads below)
    leads = [(L.lead_detail(conn, r["id"]), pick_channel(conn, r, s, usable) if channel == "auto" else channel) for r in rows]
    room = messaging.SMS_LIMIT - len(messaging.final_body("sms", "", s))

    def one(item):
        lead, ch = item
        d = ai.draft_for_lead(s, lead, ch, brief, limit=room if ch == "sms" else 0)
        text = messaging.sms_clean(d["body"]) if ch == "sms" else d["body"]
        return {"lead": lead["name"], "to": lead["email"] if ch == "email" else lead["phone_norm"], "via": ch,
                "subject": d["subject"], "body": messaging.final_body(ch, text, s)}
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
    db.lock_writes(conn)   # two taps on Start must not both queue the same leads
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
                          "sms": "Your phone isn't connected yet. Open Settings > Text messages first.",
                          "auto": "Connect at least one way to reach people first: Gmail, your phone for texts, or WhatsApp (Settings)."}[c["channel"]])
    filters = json.loads(c["filters"] or "{}")
    limit = filters.get("limit") or None
    rows = build_audience(conn, c["channel"], filters, s, limit)
    if not rows:
        raise ValueError("No leads match this audience right now.")
    n = 0
    usable = usable_channels(s) if c["channel"] == "auto" else None
    for i, lead in enumerate(rows):
        problem = None
        ch = pick_channel(conn, lead, s, usable) if c["channel"] == "auto" else c["channel"]   # ONE way per lead
        if c["ai_personalize"]:
            subject = body = ""   # the sender writes this lead's message just before it goes out
        else:
            subject, body, problem = compose(ch, c["style"], c["subject"], c["body"], lead, s, i)
        to = lead["email"] if ch == "email" else lead["phone_norm"]
        conn.execute(
            "INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, subject, body, status, error, "
            "scheduled_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (lead["id"], cid, "out", ch, to, subject, body, "skipped" if problem else "queued", problem or "",
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
        if c["channel"] in ("whatsapp", "auto"):
            wa_clear_hold(conn)   # you decided to go on: WhatsApp tries again now
        if "emails bounced" in (c["last_error"] or ""):   # it was the bounce brake that paused it: I know, go on (for a day)
            conn.execute("INSERT INTO settings(key, value) VALUES('bounce_ok_until', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (json.dumps((datetime.now() + timedelta(days=1)).isoformat(sep=" ", timespec="seconds")),))
    elif action == "cancel" and c["status"] in ("running", "paused", "draft", "scheduled"):
        conn.execute("UPDATE messages SET status='skipped', error='Campaign cancelled' "
                     "WHERE campaign_id=? AND status='queued'", (cid,))
        conn.execute("UPDATE campaigns SET status='cancelled', finished_at=? WHERE id=?", (now(), cid))
        for child in conn.execute("SELECT id FROM campaigns WHERE followup_of=? AND status IN "
                                  "('draft','scheduled','running','paused')", (cid,)).fetchall():
            set_campaign_status(conn, child["id"], "cancel")
    else:
        raise ValueError(f"Can't {action} a campaign that is {c['status']}")


def campaign_stats(conn, cid=None, statuses=None):
    """Campaigns with their progress counts. One pass over the messages of the campaigns shown, not one per number."""
    if cid is not None:
        camps = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchall()
    elif statuses:
        camps = conn.execute(f"SELECT * FROM campaigns WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id DESC",
                             list(statuses)).fetchall()
    else:
        camps = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
    if not camps:
        return None if cid is not None else []
    agg = {r["campaign_id"]: r for r in conn.execute(
        """SELECT campaign_id,
             SUM(direction='out' AND status='sent') AS sent, SUM(direction='out' AND status='queued') AS queued,
             SUM(direction='out' AND status='failed') AS failed, SUM(direction='out' AND status='skipped') AS skipped,
             COUNT(DISTINCT CASE WHEN direction='in' AND grade NOT IN ('auto','bounce') THEN lead_id END) AS replies,
             COUNT(DISTINCT CASE WHEN direction='in' AND grade='hot' THEN lead_id END) AS hot,
             COUNT(DISTINCT CASE WHEN direction='in' AND grade='warm' THEN lead_id END) AS warm,
             SUM(direction='out' AND channel='email') AS n_email, SUM(direction='out' AND channel='sms') AS n_sms,
             SUM(direction='out' AND channel='whatsapp') AS n_whatsapp,
             SUM(direction='out' AND channel='whatsapp' AND status='queued') AS queued_whatsapp
           FROM messages WHERE campaign_id IN (SELECT value FROM json_each(?)) GROUP BY campaign_id""",
        (json.dumps([c["id"] for c in camps]),))}
    waiting = {r[0]: r[1] for r in conn.execute(
        "SELECT followup_of, COUNT(*) FROM campaigns WHERE followup_of IS NOT NULL "
        "AND status IN ('scheduled','running','paused') GROUP BY followup_of")}
    out = []
    for c in camps:
        d = dict(c)
        a = agg.get(c["id"])
        for key in ("sent", "queued", "failed", "skipped", "replies", "hot", "warm", "n_email", "n_sms", "n_whatsapp", "queued_whatsapp"):
            d[key] = (a[key] or 0) if a else 0
        d["followups_waiting"] = waiting.get(c["id"], 0)
        out.append(d)
    return out[0] if cid is not None else out


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
    last = conn.execute("SELECT MAX(COALESCE(m.sent_at, m.created_at)) AS t FROM messages m WHERE m.lead_id=? AND m.direction='out' "
                        "AND (m.status='sent' OR " + AMBIGUOUS + ")", (lead_id,)).fetchone()["t"]
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
        "SELECT 1 FROM messages m WHERE m.lead_id=? AND m.direction='out' AND (m.status IN ('sent','sending','queued') OR " + AMBIGUOUS + ") "
        "AND (m.campaign_id IS NULL OR m.campaign_id <> ?) LIMIT 1", (lead_id, cid)).fetchone())


def _elsewhere_reason(conn, lead_id: int, cid) -> str:
    """Why a first message was skipped: says WHEN, HOW and in WHICH campaign this lead was already messaged."""
    r = conn.execute(
        "SELECT m.channel, COALESCE(m.sent_at, m.created_at) AS t, c.name AS cname FROM messages m "
        "LEFT JOIN campaigns c ON c.id = m.campaign_id WHERE m.lead_id=? AND m.direction='out' AND "
        "(m.status IN ('sent','sending','queued') OR " + AMBIGUOUS + ") AND (m.campaign_id IS NULL OR m.campaign_id <> ?) "
        "ORDER BY m.id DESC LIMIT 1", (lead_id, cid)).fetchone()
    if not r:
        return "Already messaged another way"
    return (f"Already messaged another way ({r['channel']} on {str(r['t'])[:10]}"
            + (f", in '{r['cname']}')" if r["cname"] else ", by hand)"))


def _replied(conn, lead_id: int) -> bool:
    return bool(conn.execute("SELECT 1 FROM messages WHERE lead_id=? AND direction='in' AND grade NOT IN ('auto','bounce') LIMIT 1",
                             (lead_id,)).fetchone())


def send_now(lead_id: int, channel: str, subject: str, body: str) -> dict:
    """Send a single message to one lead immediately (used from the lead panel)."""
    if channel not in REAL_CHANNELS:
        raise ValueError("Channel must be email, text or WhatsApp")
    if channel == "whatsapp":
        raise ValueError("WhatsApp is sent from WhatsApp itself: use the WhatsApp button on the lead.")
    s = db.get_settings()
    with db.db() as conn:
        db.lock_writes(conn)   # a double tap on Send: the second one waits, then sees the first and is refused
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
            header = messaging.send_email_any(to, subj, text, s)
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
        db.lock_writes(conn)
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


def whatsapp_one(lead_id: int, body: str, delay_sec: int = 0, label: str = "") -> dict:
    """The WhatsApp button on one lead. Linked: the message is queued and the WhatsApp worker sends and records it by
    itself. Not linked: it is logged and the wa.me link is returned so the owner presses send in WhatsApp."""
    if not db.get_internal("whatsapp_linked"):
        return log_whatsapp(lead_id, body)
    text = (body or "").strip()
    if not text:
        raise ValueError("Message can't be empty")
    with db.db() as conn:
        db.lock_writes(conn)
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
            ((f"{label}: " if label else "") + f"WhatsApp to {lead['name']}", "whatsapp", "", text, "{}", "running", "single",
             1 if state["kind"] == "followup" else 0, 1, now(), now())).lastrowid
        mid = conn.execute(
            "INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, body, status, scheduled_at, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (lead_id, cid, "out", "whatsapp", lead["phone_norm"], text, "queued",
                                          (datetime.now() + timedelta(seconds=delay_sec)).isoformat(sep=" ", timespec="seconds") if delay_sec else now(),
                                          now())).lastrowid
    return {"queued": True, "id": mid, "campaign_id": cid}


def single_one(lead_id: int, channel: str, body: str, subject: str = "", delay_sec: int = 0, label: str = "") -> dict:
    """One answer in a conversation, on the channel the lead wrote on. WhatsApp goes through the WhatsApp worker; email and text
    through the normal sender. All three use a one-message 'single' campaign, so they get the same guards as everything else
    (one message in flight per lead, do-not-contact, bounced address) and are never held back by the campaign limits."""
    if channel == "whatsapp":
        return whatsapp_one(lead_id, body, delay_sec, label)
    if channel not in ("email", "sms"):
        raise ValueError("Unknown channel")
    text = (body or "").strip()
    if not text:
        raise ValueError("Message can't be empty")
    s = db.get_settings()
    with db.db() as conn:
        db.lock_writes(conn)
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            raise KeyError("Lead not found")
        if lead["archived"] or lead["do_not_contact"]:
            raise ValueError("This lead can no longer be messaged.")
        if channel == "email":
            if not lead["email"] or lead["email_bounced"]:
                raise ValueError("This lead has no working email address.")
            subj = (subject or "").strip() or "Re: your message"
            to = lead["email"]
            text = messaging.final_body("email", text, s)
        else:
            if not lead["phone_norm"] or not lead["is_mobile"]:
                raise ValueError("This lead has no mobile number to text.")
            subj, to = "", lead["phone_norm"]
            text = messaging.sms_clean(text)   # an answer in a conversation needs no opt-out tag; "STOP" is still honoured
            if len(text) > messaging.SMS_LIMIT:
                raise ValueError(f"That text is {len(text)} characters; one SMS holds {messaging.SMS_LIMIT}.")
        state = outreach_state(conn, lead_id, s)
        if not state["allowed"]:
            raise ValueError(state["reason"])
        cid = conn.execute(
            "INSERT INTO campaigns(name, channel, subject, body, filters, status, style, followup_step, total, launched_at, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ((f"{label}: " if label else "") + f"{channel.upper() if channel == 'sms' else channel.title()} to {lead['name']}", channel, subj, text, "{}", "running", "single",
             1 if state["kind"] == "followup" else 0, 1, now(), now())).lastrowid
        mid = conn.execute(
            "INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, subject, body, status, scheduled_at, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", (lead_id, cid, "out", channel, to, subj, text, "queued",
                                           (datetime.now() + timedelta(seconds=delay_sec)).isoformat(sep=" ", timespec="seconds") if delay_sec else now(),
                                           now())).lastrowid
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
            if not c or c["channel"] not in ("whatsapp", "auto"):
                raise KeyError("WhatsApp campaign not found")
            if c["status"] == "done":
                return None
            if c["status"] != "running":
                raise ValueError("This campaign is " + ("paused. Resume it to carry on." if c["status"] == "paused"
                                                        else f"{c['status']}, so there is nothing to send."))
            row = conn.execute(
                "SELECT m.*, c.ai_personalize, c.body AS brief, c.followup_step AS step FROM messages m "
                "JOIN campaigns c ON c.id = m.campaign_id "
                "WHERE m.campaign_id=? AND m.direction='out' AND m.channel='whatsapp' AND m.status='queued' "
                "AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?) "
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
    """A first-message campaign leaves anyone alone who was messaged some other way (follow-ups are exempt).
    An answer you or the assistant give inside a conversation (campaign style 'single') is exempt too: answering someone
    must never be blocked because they were messaged earlier."""
    if campaign.get("style") == "single":
        return None
    if campaign["followup_of"] is None and not campaign.get("followup_step") and _elsewhere(conn, lead["id"], campaign["id"]):
        return _elsewhere_reason(conn, lead["id"], campaign["id"])
    if (campaign["followup_of"] is not None or campaign.get("followup_step")) and _replied(conn, lead["id"]):
        return "They replied, so no follow-up is sent"
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
            db.log_event(conn, lead["id"], "no_whatsapp", "Number is not on WhatsApp" + (" - email them instead" if lead["email"] else ""))
            if not reroute(conn, mid, "whatsapp", "not on WhatsApp"):
                conn.execute("UPDATE messages SET status='skipped', error='Not on WhatsApp' WHERE id=?", (mid,))
        else:
            conn.execute("UPDATE messages SET status='skipped', error='Skipped by you' WHERE id=?", (mid,))
        Sender._maybe_finish(conn, m["campaign_id"])
    return {"status": "sent" if action == "sent" else "skipped"}


def reroute(conn, mid: int, failed: str, reason: str) -> bool:
    """'Best way' campaigns: when a lead can't be reached the way first chosen (not on WhatsApp, email bounced), their ONE
    message moves to the next way instead of being lost. It is the same message row, so the lead is never messaged twice."""
    m = conn.execute("SELECT m.*, c.channel AS cchannel, c.style AS cstyle, c.subject AS csubject, c.body AS cbody, "
                     "c.ai_personalize AS cai, c.followup_step AS step, c.status AS cstatus, c.filters AS cfilters FROM messages m "
                     "JOIN campaigns c ON c.id = m.campaign_id WHERE m.id=?", (mid,)).fetchone()
    if not m or m["cstatus"] not in ("running", "paused") or m["cstyle"] == "single":
        return False
    try:
        fallback = bool(json.loads(m["cfilters"] or "{}").get("fallback"))
    except ValueError:
        fallback = False
    if m["cchannel"] != "auto" and not fallback:   # a campaign on one fixed way only moves on when 'try other ways' was ticked
        return False
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (m["lead_id"],)).fetchone()
    if not lead or lead["do_not_contact"] or lead["archived"]:
        return False
    s = db.get_settings(conn)
    tried = {failed}
    for e in conn.execute("SELECT detail FROM events WHERE lead_id=? AND kind='rerouted'", (lead["id"],)):
        tried.add(e["detail"].split("->")[0].strip())
    nxt = pick_channel(conn, lead, s, usable_channels(s), exclude=tried)
    if not nxt:
        return False
    subject = body = ""
    if not m["cai"]:
        subject, body, problem = compose(nxt, m["cstyle"], m["csubject"], m["cbody"], lead, s, m["id"], int(m["step"] or 0))
        if problem:
            return False
    conn.execute("UPDATE messages SET channel=?, to_addr=?, subject=?, body=?, status='queued', error='', attempts=0, "
                 "scheduled_at=? WHERE id=?",
                 (nxt, lead["email"] if nxt == "email" else lead["phone_norm"], subject, body, now(), mid))
    db.log_event(conn, lead["id"], "rerouted", f"{failed}->{nxt}: {reason}")
    return True


# --------------------------------------------- WhatsApp worker (sends through WhatsApp Web, records by itself)
def wa_next_id(only_single: bool = False):
    """Id of the next WhatsApp message the worker should send, or None. only_single: just the answers you (or the assistant)
    give in a conversation, which the daily limit for campaign messages does not hold back."""
    with db.db() as conn:
        row = conn.execute(
            "SELECT m.id FROM messages m JOIN campaigns c ON c.id = m.campaign_id WHERE m.direction='out' "
            "AND m.status='queued' AND m.channel='whatsapp' AND c.status='running' "
            + ("AND c.style='single' " if only_single else "") +
            "AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?) ORDER BY m.id LIMIT 1", (now(),)).fetchone()
    return row["id"] if row else None


def wa_sent_today(conn) -> int:
    """WhatsApp messages sent today that count against the daily limit: campaign messages and hand-logged ones, not answers
    inside a conversation."""
    return conn.execute("SELECT COUNT(*) FROM messages m LEFT JOIN campaigns c ON c.id = m.campaign_id WHERE m.direction='out' "
                        "AND m.channel='whatsapp' AND m.status='sent' AND m.sent_at >= ? AND COALESCE(c.style, '') <> 'single'",
                        (today_start(),)).fetchone()[0]


def wa_prepare(mid: int):
    """Claim one queued WhatsApp message and get its text ready. Returns {'id','lead_id','phone','body'} or None when it
    was skipped (lead no longer messageable, already messaged another way, campaign stopped) or couldn't be written."""
    s = db.get_settings()
    with db.db() as conn:
        db.lock_writes(conn)
        row = conn.execute(
            "SELECT m.*, c.ai_personalize, c.body AS brief, c.followup_step AS step, c.followup_of, c.status AS cstatus, "
            "c.style AS style FROM messages m JOIN campaigns c ON c.id = m.campaign_id WHERE m.id=?", (mid,)).fetchone()
        if not row or row["status"] != "queued" or row["cstatus"] != "running":
            return None
        if not conn.execute("UPDATE messages SET status='sending', attempts=attempts+1 WHERE id=? AND status='queued'",
                            (mid,)).rowcount:
            return None
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (row["lead_id"],)).fetchone()
        problem = _wa_lead_problem(lead) or _wa_elsewhere(
            conn, lead, {"id": row["campaign_id"], "followup_of": row["followup_of"], "followup_step": row["step"],
                     "style": row["style"]})
        if problem:
            conn.execute("UPDATE messages SET status='skipped', error=? WHERE id=?", (problem, mid))
            Sender._maybe_finish(conn, row["campaign_id"])
            return None
        held = _gap_hold(conn, row)
        if held:   # they were messaged a moment ago: wait, don't stack a second message on top of it
            conn.execute("UPDATE messages SET status='queued', attempts=attempts-1, scheduled_at=?, error=? WHERE id=?",
                         (held, "Held back: they were messaged less than an hour ago", mid))
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
    return {"id": mid, "lead_id": lead["id"], "phone": lead["phone_norm"], "body": body, "name": lead["name"],
            "followup": row["followup_of"] is not None or int(row["step"] or 0) > 0,
            # a campaign's first message (not a follow-up, not one you sent by hand): never into a chat you already have
            "first_contact": row["style"] != "single" and row["followup_of"] is None and int(row["step"] or 0) == 0}


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
            conn.execute("UPDATE campaigns SET last_error='' WHERE channel='auto' AND status='running' AND last_error LIKE '%WhatsApp%'")   # it works again: drop the old warning
        elif outcome == "already_sent":   # the chat already ends with this very message: record it, never send it twice
            _mark_sent(conn, mid, lead_id, "whatsapp-web", None)
            db.log_event(conn, lead_id, "note", "This message was already the last one in the WhatsApp chat, so it was not sent again")
        elif outcome == "replied":   # a follow-up to someone who has written back (or whose business auto-replied)
            conn.execute("UPDATE messages SET status='skipped', error=? WHERE id=?",
                         ("They have written back on WhatsApp since your last message. Open the chat and answer them yourself.", mid))
            db.log_event(conn, lead_id, "note", "Follow-up not sent: they have written back on WhatsApp. Check the chat")
        elif outcome == "already_chatted":   # our messages are already in this chat (sent earlier, or from the phone)
            conn.execute("UPDATE messages SET status='skipped', error=? WHERE id=?",
                         ("You already have a WhatsApp conversation with them, so no first message was sent.", mid))
            conn.execute("UPDATE leads SET status=CASE WHEN status='new' THEN 'contacted' ELSE status END, updated_at=? WHERE id=?",
                         (now(), lead_id))
            db.log_event(conn, lead_id, "note", "Skipped: you already have a WhatsApp conversation with them")
        elif outcome == "not_on_whatsapp":
            lead = conn.execute("SELECT email FROM leads WHERE id=?", (lead_id,)).fetchone()
            db.log_event(conn, lead_id, "no_whatsapp", "Number is not on WhatsApp" + (" - email them instead" if lead["email"] else ""))
            if not reroute(conn, mid, "whatsapp", "not on WhatsApp"):
                conn.execute("UPDATE messages SET status='skipped', error='Not on WhatsApp' WHERE id=?", (mid,))
        elif outcome == "requeue":
            conn.execute("UPDATE messages SET status='queued', attempts=attempts-1, error=? WHERE id=?", (detail, mid))
        elif outcome == "retry" and m["attempts"] < 3:
            retry_at = (datetime.now() + timedelta(minutes=5 * m["attempts"])).isoformat(sep=" ", timespec="seconds")
            conn.execute("UPDATE messages SET status='queued', error=?, scheduled_at=? WHERE id=?", (detail, retry_at, mid))
        else:
            conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (detail, mid))
        if cid:
            Sender._maybe_finish(conn, cid)


WA_HOLD_MIN = 30   # how long WhatsApp rests after it crashed, logged out or failed three sends in a row


def wa_hold(reason: str, minutes: int = WA_HOLD_MIN):
    """WhatsApp rests for a while (nothing opens a browser again and again). The reason is shown on the Outreach page."""
    until = (datetime.now() + timedelta(minutes=minutes)).isoformat(sep=" ", timespec="seconds")
    db.set_internal("wa_hold", {"until": until, "reason": reason})


def wa_hold_active():
    h = db.get_internal("wa_hold")
    return h if isinstance(h, dict) and str(h.get("until") or "") > now() else None


BOUNCE_MIN_SENT, BOUNCE_LIMIT = 20, 0.08   # of the last 3 days of emails, 8% bouncing is a bad list: stop before the sender name is hurt


def bounce_rate(conn):
    """(sent, bounced) emails over the last 3 days. A sender whose mail keeps bouncing is treated as spam by mail providers."""
    since = days_ago(3)
    sent = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='out' AND channel='email' AND status='sent' AND sent_at >= ?", (since,)).fetchone()[0]
    bad = conn.execute("SELECT COUNT(DISTINCT lead_id) FROM events WHERE kind='bounce' AND ts >= ?", (since,)).fetchone()[0]
    return sent, bad


def bounce_brake(sender):
    """Too many bounces: pause the email campaigns (and rest the email part of Best-way ones) and say why. Resume overrides it."""
    with db.db() as conn:
        sent, bad = bounce_rate(conn)
        if sent < BOUNCE_MIN_SENT or bad / sent < BOUNCE_LIMIT:
            return
        if db.get_internal("bounce_ok_until") and str(db.get_internal("bounce_ok_until")) > now():
            return   # you pressed Resume: you decided to go on
        why = (f"{bad} of the last {sent} emails bounced ({round(100 * bad / sent)}%). Sending more to a list like this can get "
               "your email address marked as spam. Remove or fix the bad addresses (Leads > Bounced), then resume.")
        conn.execute("UPDATE campaigns SET status='paused', last_error=? WHERE channel='email' AND status='running'", (why,))
        held = conn.execute("UPDATE campaigns SET last_error=? WHERE channel='auto' AND status='running' AND id IN "
                            "(SELECT campaign_id FROM messages WHERE channel='email' AND status='queued')", (why,)).rowcount
        if held:
            sender.net["email"] = [0, time.time() + 6 * 3600]
            sender._set("waiting", "Email is resting: too many bounces")


def wa_clear_hold(conn=None):
    """End the rest. Inside an open write (Resume) pass that connection: a second write would wait on the first."""
    if db.get_internal("wa_hold"):
        stale = "UPDATE campaigns SET last_error='' WHERE channel='auto' AND status='running' AND last_error LIKE '%WhatsApp%'"
        if conn is not None:
            conn.execute(stale)
        else:
            with db.db() as c2:
                c2.execute(stale)
        if conn is not None:
            conn.execute("INSERT INTO settings(key, value) VALUES('wa_hold', 'null') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        else:
            db.set_internal("wa_hold", None)


def wa_pause_campaigns(reason: str, minutes: int = WA_HOLD_MIN):
    """Stop every running WhatsApp campaign (WhatsApp logged out, or sending keeps failing) so nothing is burned. A 'best way'
    campaign has other ways to go on, so it is not paused: its WhatsApp part rests (WhatsApp is put on hold) and the reason shows."""
    with db.db() as conn:
        conn.execute("UPDATE campaigns SET status='paused', last_error=? WHERE channel='whatsapp' AND status='running'", (reason,))
        conn.execute("UPDATE campaigns SET last_error=? WHERE channel='auto' AND status='running' AND id IN "
                     "(SELECT campaign_id FROM messages WHERE channel='whatsapp' AND status='queued')", (reason,))
    wa_hold(reason, minutes)


# ------------------------------------------- WhatsApp replies (read from the chats by the worker's own session)
SYNC_WINDOW_DAYS = 14    # chats of leads we WhatsApped within this long are looked at for replies
SYNC_RECHECK_MIN = 5     # a chat is not opened again sooner than this
SYNC_REPLIED_MIN = 60    # ... or this, once they have already answered (to catch a longer conversation)


def wa_sync_due(s: dict = None) -> bool:
    """Is it time for the next look at the chats, and is there any chat worth looking at?"""
    s = s or db.get_settings()
    if db.get_internal("wa_sync_force"):   # "Check replies now"
        return bool(wa_sync_targets(1))
    if (s.get("closer_mode") or "off") != "off" and ai.configured(s):
        return True   # conversation mode is ON: a worker stays in the hidden browser and watches the chats
    minutes = int(s.get("whatsapp_sync_minutes") or 0)
    if minutes <= 0:
        return False
    if (s.get("closer_mode") or "off") != "off" and live_conversations():
        minutes = min(minutes, 2)   # someone is talking to the assistant: look often
    last = db.get_internal("wa_sync_last") or 0
    if time.time() - float(last) < minutes * 60:
        return False
    return bool(wa_sync_targets(1))


LIVE_HOURS = 36   # a chat with a reply this recent counts as a live conversation


def live_conversations() -> int:
    with db.db() as conn:
        return conn.execute("SELECT COUNT(*) FROM leads WHERE archived=0 AND do_not_contact=0 AND last_reply_at >= ?",
                            ((datetime.now() - timedelta(hours=LIVE_HOURS)).isoformat(sep=" ", timespec="seconds"),)).fetchone()[0]


def wa_sync_targets(limit: int = 30) -> list:
    """Chats to read for replies: leads we sent a WhatsApp to lately, never-checked and longest-unchecked first."""
    checked = db.get_internal("wa_sync_state") or {}
    nowts = time.time()
    with db.db() as conn:
        rows = conn.execute(
            "SELECT l.id, l.name, l.phone_norm, l.last_reply_at FROM leads l WHERE l.archived=0 AND l.do_not_contact=0 "
            "AND l.phone_norm IS NOT NULL AND EXISTS (SELECT 1 FROM messages m WHERE m.lead_id=l.id AND m.direction='out' "
            "AND m.channel='whatsapp' AND m.status='sent' AND m.sent_at >= ?)", (days_ago(SYNC_WINDOW_DAYS),)).fetchall()
    out = []
    for r in rows:
        last = float(checked.get(str(r["id"]), 0))
        live = bool(r["last_reply_at"]) and r["last_reply_at"] >= (datetime.now() - timedelta(hours=LIVE_HOURS)).isoformat(sep=" ", timespec="seconds")
        gap = (SYNC_RECHECK_MIN if (live or not r["last_reply_at"]) else SYNC_REPLIED_MIN) * 60
        if live:
            gap = 90
        if nowts - last >= gap:
            out.append((last, dict(r)))
    out.sort(key=lambda x: x[0])
    return [r for _, r in out[:limit]]


def wa_sync_begin():
    db.set_internal("wa_sync_last", time.time())
    db.set_internal("wa_sync_force", False)


def wa_sync_done(lead_id: int):
    state = db.get_internal("wa_sync_state") or {}
    state[str(lead_id)] = time.time()
    if len(state) > 5000:   # keep it small: forget the oldest
        for k in sorted(state, key=state.get)[:1000]:
            state.pop(k, None)
    db.set_internal("wa_sync_state", state)


def wa_record_replies(lead_id: int, items: list, answered_outside: bool = False) -> int:
    """Store the replies read from a lead's chat. Each WhatsApp message id is stored once, so reading a chat again
    never doubles anything. Returns how many were new."""
    new = 0
    for it in items or []:
        text, mid = (it.get("text") or "").strip(), it.get("id")
        if not text or not mid:
            continue
        with db.db() as conn:
            db.lock_writes(conn)
            res = L.record_reply(conn, lead_id, "whatsapp", text, "", "wa:" + str(mid))
            if answered_outside and not res.get("duplicate"):   # you already answered them (from the phone): nothing to draft
                conn.execute("UPDATE messages SET handled=1 WHERE id=?", (res["id"],))
        if not res.get("duplicate"):
            new += 1
    return new


def parse_wa_time(pre: str) -> str:
    """WhatsApp's '[10:32, 21/10/2026] Name: ' (or '[10:32 AM, 10/21/2026]') as 'YYYY-MM-DD HH:MM:SS'; '' when it can't be read."""
    m = re.search(r"\[(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?,\s*(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})\]", pre or "")
    if not m:
        return ""
    hh, mm, ap, a, b, c = m.groups()
    hh = int(hh) % 12 + (12 if ap and ap.lower() == "pm" else 0) if ap else int(hh)
    a, b, c = int(a), int(b), int(c)
    if a > 31:
        y, mo, d = a, b, c            # 2026/10/21
    else:
        y, d, mo = c, a, b            # 21/10/2026 (day first, as in Kenya)
        if mo > 12 and d <= 12:
            d, mo = mo, d             # 10/21/2026
    try:
        t = datetime(y if y > 99 else 2000 + y, mo, d, hh, int(mm))
    except ValueError:
        return ""
    return "" if t > datetime.now() + timedelta(hours=1) else t.strftime("%Y-%m-%d %H:%M:%S")


def wa_inbound_lead(chat: dict, log=print):
    """The lead a chat with unread messages belongs to. An unknown mobile number that wrote to you becomes a new lead (source
    'inbound whatsapp'), so the assistant and your pipeline see it; a saved contact is matched to a lead by its exact name, else skipped."""
    e164 = ""
    if chat.get("phone"):
        e164, _ = normalize_phone(chat["phone"])
    created = False
    with db.db() as conn:
        db.lock_writes(conn)
        if e164:
            row = conn.execute("SELECT id, archived FROM leads WHERE phone_norm=?", (e164,)).fetchone()
            if row:
                return None if row["archived"] else row["id"]
            res = L.add_lead(conn, {"name": f"WhatsApp {e164}", "phone": e164, "notes": "Wrote to us on WhatsApp first."}, "inbound whatsapp")
            lead_id, created = res["id"], res["created"]
        else:
            title = (chat.get("title") or "").strip()
            rows = conn.execute("SELECT id FROM leads WHERE archived=0 AND lower(name)=lower(?)", (title,)).fetchall() if title else []
            return rows[0]["id"] if len(rows) == 1 else None
    if created:   # (logged after the write lock is released: the log is written on its own connection)
        log(f"  + new lead from an inbound WhatsApp: {e164}")
    return lead_id


def wa_import_out(lead_id: int, mid: str, text: str, at: str = "") -> bool:
    """A message you sent from your phone: kept in the lead's history (so the assistant knows what was said) and counted as contact.
    Skipped when the same words are already there (our own sends carry no WhatsApp id)."""
    text = (text or "").strip()
    if not text or not mid:
        return False
    with db.db() as conn:
        db.lock_writes(conn)
        if conn.execute("SELECT 1 FROM messages WHERE lead_id=? AND direction='out' AND (message_id_header=? OR (body=? AND channel='whatsapp')) LIMIT 1",
                        (lead_id, "wa:" + str(mid), text)).fetchone():
            return False
        lead = conn.execute("SELECT phone_norm FROM leads WHERE id=?", (lead_id,)).fetchone()
        ts = at or now()
        conn.execute("INSERT INTO messages(lead_id, direction, channel, to_addr, body, status, provider_id, message_id_header, sent_at, created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (lead_id, "out", "whatsapp", (lead["phone_norm"] if lead else "") or "", text, "sent", "manual",
                                                    "wa:" + str(mid), ts, ts))
        conn.execute("UPDATE leads SET last_contacted_at=MAX(COALESCE(last_contacted_at, ''), ?), "
                     "status=CASE WHEN status='new' THEN 'contacted' ELSE status END WHERE id=?", (ts, lead_id))
    return True


def wa_record_chat(lead_id: int, chat: dict) -> int:
    """Store a whole read chat in order: what you wrote from your phone, and what they wrote. Returns how many of THEIR messages were new."""
    thread = chat.get("thread") or []
    replies = {str(r["id"]): r for r in chat.get("replies") or []}
    outside = chat.get("last") == "out"
    new, done = 0, set()
    for it in thread:
        if it.get("dir") == "out":
            wa_import_out(lead_id, it["id"], it.get("text"), it.get("at") or "")
        elif str(it["id"]) in replies:
            new += _wa_one_reply(lead_id, replies[str(it["id"])], outside)
            done.add(str(it["id"]))
    rest = [r for k, r in replies.items() if k not in done]
    return new + (wa_record_replies(lead_id, rest, answered_outside=outside) if rest else 0)


def _wa_one_reply(lead_id: int, it: dict, answered_outside: bool) -> int:
    text, mid = (it.get("text") or "").strip(), it.get("id")
    if not text or not mid:
        return 0
    with db.db() as conn:
        db.lock_writes(conn)
        res = L.record_reply(conn, lead_id, "whatsapp", text, "", "wa:" + str(mid), received_at=it.get("at") or None)
        if answered_outside and not res.get("duplicate"):
            conn.execute("UPDATE messages SET handled=1 WHERE id=?", (res["id"],))
    return 0 if res.get("duplicate") else 1


def _gap_hold(conn, row):
    """When this queued campaign message has to wait: if the lead was sent something else less than MIN_GAP_MINUTES ago,
    the time they can be messaged again, else None. Whatever the cause (a follow-up set to 0 days, a retry, a message put
    back in the queue), a lead is never hit by two campaign messages in a row. A message the owner sends by hand
    (style 'single') and anything to a lead who has replied is exempt: answering someone must never be held up."""
    if (row["style"] if "style" in row.keys() else None) == "single":
        return None
    if conn.execute("SELECT 1 FROM messages WHERE lead_id=? AND direction='in' AND grade NOT IN ('auto','bounce') LIMIT 1",
                    (row["lead_id"],)).fetchone():
        return None
    # the same "last message of any kind" clock the follow-up timing uses (a WhatsApp sent by hand counts too)
    last = conn.execute("SELECT last_contacted_at AS t FROM leads WHERE id=?", (row["lead_id"],)).fetchone()
    last = last["t"] if last else None
    if not last:
        return None
    unlock = datetime.strptime(last, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=MIN_GAP_MINUTES)
    return unlock.strftime("%Y-%m-%d %H:%M:%S") if unlock > datetime.now() else None


def wa_wait_seconds():
    """None when no WhatsApp message is waiting in a running campaign; 0 when one is due now; else the seconds until the
    first waiting one is due. Lets the worker stay in its one browser while a retry waits instead of closing and reopening."""
    with db.db() as conn:
        row = conn.execute(
            "SELECT MIN(COALESCE(m.scheduled_at, '')) AS t, COUNT(*) AS n FROM messages m JOIN campaigns c ON c.id = m.campaign_id "
            "WHERE m.direction='out' AND m.status='queued' AND m.channel='whatsapp' AND c.status='running'").fetchone()
    if not row["n"]:
        return None
    if not row["t"]:
        return 0
    return max(0.0, (datetime.strptime(row["t"], "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds())


def sending_report(conn, s: dict = None, linked: bool = None) -> list:
    """Why queued messages are not going out right now, one plain sentence per channel, for the Outreach page.
    Each item: {channel, queued, level ('ok' | 'info' | 'warn' | 'bad'), text, fix (where to go, or '')}. Empty when nothing is waiting."""
    s = s or db.get_settings(conn)
    linked = bool(db.get_internal("whatsapp_linked")) if linked is None else linked
    rows = conn.execute(
        "SELECT m.channel AS ch, COUNT(*) AS n, SUM(CASE WHEN m.scheduled_at IS NULL OR m.scheduled_at <= ? THEN 1 ELSE 0 END) AS ready, "
        "MIN(m.scheduled_at) AS first_at FROM messages m JOIN campaigns c ON c.id = m.campaign_id "
        "WHERE m.direction='out' AND m.status='queued' AND c.status='running' GROUP BY m.channel", (now(),)).fetchall()
    names = {"whatsapp": "WhatsApp", "email": "email", "sms": "text"}
    out = []
    for r in rows:
        ch, n, ready = r["ch"], r["n"], r["ready"] or 0
        if ch not in names or not n:
            continue
        def add(level, text, fix=""):
            out.append({"channel": ch, "queued": n, "level": level, "text": text, "fix": fix})
        label = names[ch]
        if ch == "whatsapp" and not linked:
            add("bad", f"{n} WhatsApp message{'s' if n != 1 else ''} are waiting, but WhatsApp is not connected, so nothing can be sent. "
                       "Connect it once (scan the QR code with your phone); sending then starts by itself.", "#/settings")
            continue
        hold = wa_hold_active() if ch == "whatsapp" else None
        if hold:
            add("warn", f"WhatsApp is resting: {hold.get('reason')} {n} message{'s' if n != 1 else ''} wait. It tries again by itself at "
                        f"{str(hold['until'])[11:16]}, or press Resume on the campaign to try now.", "#/settings")
            continue
        if ch in ("email", "sms") and not channel_ready(ch, s):
            add("bad", f"{n} {label} message{'s' if n != 1 else ''} are waiting, but {label} is not set up yet. Finish it in Settings and sending starts by itself.", "#/settings")
            continue
        if not ready:
            first = r["first_at"] or ""
            add("info", f"{n} {label} message{'s' if n != 1 else ''} are scheduled" + (f" for {first[11:16]}" if first else "") + ". They go out then.")
            continue
        if not Sender.in_window(s):
            add("warn", f"Outside sending hours ({s['send_window_start']} to {s['send_window_end']}). {n} {label} message{'s' if n != 1 else ''} will go out when the window opens. "
                        "You can change the hours in Settings > Limits and opt-outs.", "#/settings")
            continue
        sent = wa_sent_today(conn) if ch == "whatsapp" else conn.execute(
            "SELECT COUNT(*) FROM messages m LEFT JOIN campaigns c ON c.id = m.campaign_id WHERE m.direction='out' AND m.channel=? "
            "AND m.status='sent' AND m.sent_at >= ? AND COALESCE(c.style, '') <> 'single'", (ch, today_start())).fetchone()[0]
        cap = messaging.total_email_cap(s) if ch == "email" else int(s.get(f"{ch}_daily_cap") or 0)
        if cap and sent >= cap:
            add("warn", f"Today's {label} limit ({cap}) is reached. {n} message{'s' if n != 1 else ''} will go out tomorrow, or raise the limit in Settings > Limits and opt-outs.", "#/settings")
            continue
        if ch == "whatsapp":
            wait = perf.browser_conflict(conn, s, "whatsapp")
            if wait:
                job = jobs.active_job(conn, "scrape")
                add("warn", "A lead search is using the browser" + (f" ({job['progress']})" if job and job.get("progress") else "") +
                            ". This computer runs one browser at a time to stay fast, so WhatsApp waits. Stop the search to send now, or let it finish.", "#/find")
                continue
            if jobs.active_job(conn, "whatsapp"):
                add("ok", f"Sending on WhatsApp now. {n} waiting.")
                continue
            add("info", f"{n} WhatsApp message{'s' if n != 1 else ''} waiting. WhatsApp is about to start.")
            continue
        add("ok", f"Sending {label}. {n} waiting, one at a time with a pause between each.")
    return out


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
                 else "((l.email IS NOT NULL AND l.email_bounced = 0) OR (l.phone_norm IS NOT NULL AND l.is_mobile = 1))"
                 if c["channel"] == "auto" else "l.phone_norm IS NOT NULL AND l.is_mobile = 1")
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
        db.lock_writes(conn)   # the sender's own look and the one made when a follow-up is created must not both queue it
        queued = 0
        for c in conn.execute("SELECT * FROM campaigns WHERE followup_of IS NOT NULL "
                              "AND status IN ('scheduled','running')").fetchall():
            if c["ai_personalize"] and not ai.configured(s):
                continue   # an AI follow-up waits until there is a key
            rows = _followup_leads(conn, c, True)
            usable = usable_channels(s) if c["channel"] == "auto" else None
            queued_here = 0
            for i, lead in enumerate(rows):
                ch = c["channel"]
                if ch == "auto":   # keep to the way they were first reached when it still works, else the best way now
                    prior = _prior_channel(conn, lead["id"])
                    ch = prior if prior in (usable or []) and _can_reach(conn, lead, prior) else pick_channel(conn, lead, s, usable)
                    if not ch:
                        continue
                subject = body = problem = ""
                if not c["ai_personalize"]:   # ready-made: the nudge / tip / last note for this step
                    subject, body, problem = compose(ch, "ready", "", "", lead, s, i, max(int(c["followup_step"] or 1), 1))
                to = lead["email"] if ch == "email" else lead["phone_norm"]
                conn.execute(
                    "INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, subject, body, status, error, "
                    "scheduled_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (lead["id"], c["id"], "out", ch, to, subject, body, "skipped" if problem else "queued",
                     problem or "", now(), now()))
                queued_here += 1
            if queued_here:
                conn.execute("UPDATE campaigns SET status='running', total=total+?, launched_at=COALESCE(launched_at, ?), "
                             "finished_at=NULL, last_error='' WHERE id=?", (queued_here, now(), c["id"]))
                queued += queued_here
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
        if draft.get("too_long") and ch in ("email", "whatsapp"):
            # still too big after one rewrite: a good short ready-made message beats a long one
            ready_subject, ready_body, problem = compose(ch, "ready", "", "", lead, s, row["id"], int(row["step"] or 0))
            if problem:
                raise SendError("The AI's message stayed too long. " + problem, "transient")
            return ready_subject, ready_body
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
        self.last_eco = 0.0
        self.last_wa_spawn = 0.0
        self.wa_job = None       # the WhatsApp worker started last
        self.wa_crashes = 0      # workers in a row that died without a word
        self.fails = {}
        self.last_bounce_check = 0.0
        self.net = {}            # channel -> [failures in a row because the internet or server could not be reached, rest-until time]
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
        if not backup.enter_work():   # a restore is running: stand still until it is done
            return 2.0
        try:
            return self._tick()
        finally:
            backup.exit_work()

    def _tick(self) -> float:
        s = db.get_settings()
        if time.time() - self.last_release >= 60:   # follow-ups that came due since last minute
            self.last_release = time.time()
            try:
                release_followups(s)
            except Exception:
                traceback.print_exc()
        if s.get("advisor_feed_key") and time.time() - self.last_eco >= 300:   # serious alerts from businesses that chose to share them, every 5 minutes
            self.last_eco = time.time()
            try:
                db.set_internal("eco_pull_last", ecosystem.pull_alerts(s["advisor_feed_key"]))
            except Exception:
                traceback.print_exc()
        waits, sent_any, pending = [], False, False
        for ch in AUTO_CHANNELS:
            with db.db() as conn:
                row = conn.execute(
                    "SELECT m.*, c.ai_personalize, c.body AS brief, c.followup_step AS step, c.followup_of, c.style FROM messages m "
                    "JOIN campaigns c ON c.id = m.campaign_id "
                    "WHERE m.direction='out' AND m.status='queued' AND m.channel=? AND c.status='running' "
                    "AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?) ORDER BY m.id LIMIT 1", (ch, now())).fetchone()
                if not row:
                    continue
                pending = True
                sent_today = conn.execute(
                    "SELECT COUNT(*) FROM messages m LEFT JOIN campaigns c ON c.id = m.campaign_id WHERE m.direction='out' "
                    "AND m.channel=? AND m.status='sent' AND m.sent_at >= ? AND COALESCE(c.style, '') <> 'single'",
                    (ch, today_start())).fetchone()[0]
            if not self.in_window(s):
                self._set("waiting", f"Outside sending hours ({s['send_window_start']}–{s['send_window_end']})")
                waits.append(30)
                continue
            cap_now = messaging.total_email_cap(s) if ch == "email" else int(s[f"{ch}_daily_cap"])
            if sent_today >= cap_now and row["style"] != "single":   # answers in a conversation are never capped
                self._set("waiting", f"Daily {ch} limit reached ({sent_today})")
                waits.append(60)
                continue
            if ch == "email" and time.time() - self.last_bounce_check >= 60:
                self.last_bounce_check = time.time()
                try:
                    bounce_brake(self)
                except Exception:
                    traceback.print_exc()
            rest = self.net.get(ch, [0, 0.0])[1] - time.time()
            if rest > 0:   # the internet or mail server could not be reached: rest, then try again by itself
                self._set("waiting", f"{ch.upper() if ch == 'sms' else ch.title()} is resting: can't reach it. Trying again in {int(rest // 60) + 1} min")
                waits.append(rest)
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
            with db.db() as conn:   # WhatsApp messages are waiting but it is not connected: say so (the pill shows this), never sit silent
                waiting = conn.execute(
                    "SELECT COUNT(*) FROM messages m JOIN campaigns c ON c.id = m.campaign_id WHERE m.channel='whatsapp' "
                    "AND m.direction='out' AND m.status='queued' AND c.status='running'").fetchone()[0]
            if waiting:
                self._set("waiting", "WhatsApp is not connected: connect it in Settings so its messages can go")
                return 10.0
            return None
        sync_only = False
        with db.db() as conn:
            queued = conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN campaigns c ON c.id = m.campaign_id WHERE m.channel='whatsapp' "
                "AND m.direction='out' AND m.status='queued' AND c.status='running' "
                "AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?)", (now(),)).fetchone()[0]
        if not queued:
            if not wa_sync_due(s):
                return None
            sync_only = True   # nothing to send: the worker only reads the chats for replies
        hold = wa_hold_active()
        if hold:   # it crashed, logged out or kept failing: rest, say why, and try again by itself afterwards
            if not sync_only:
                self._set("waiting", "WhatsApp is resting: " + str(hold.get("reason") or "")[:140])
            return 60.0
        with db.db() as conn:
            if jobs.active_job(conn, "whatsapp") or jobs.active_job(conn, "whatsapp_link"):
                return 5.0
            if self.wa_job is not None:   # how did the last worker end?
                prev, self.wa_job = jobs.get_job(conn, self.wa_job), None
                died = bool(prev) and prev["status"] == "failed" and str(prev.get("progress") or "").startswith("Worker exited")
                self.wa_crashes = self.wa_crashes + 1 if died else 0
            sent_today = wa_sent_today(conn)
        if self.wa_crashes >= 2:   # the worker keeps dying at start-up: stop, rather than open a browser every minute
            self.wa_crashes = 0
            wa_pause_campaigns("The WhatsApp browser keeps closing by itself, so sending was paused. Close any WhatsApp "
                               "window left open, then resume.")
            return None
        if not self.in_window(s):
            self._set("waiting", f"Outside sending hours ({s['send_window_start']}–{s['send_window_end']})")
            return 30.0
        with db.db() as conn:
            wait = perf.browser_conflict(conn, s, "whatsapp")
        if wait:   # light mode: a lead search has the browser; WhatsApp goes on when it is done
            self._set("waiting", "WhatsApp waits for the lead search to finish")
            return 15.0
        only_single_left = sent_today >= int(s["whatsapp_daily_cap"]) and bool(wa_next_id(only_single=True))
        if sent_today >= int(s["whatsapp_daily_cap"]) and not sync_only and not only_single_left:
            self._set("waiting", f"Daily whatsapp limit reached ({sent_today})")
            return 60.0
        if time.time() - self.last_wa_spawn < 45:   # a worker that stops at once is not restarted in a loop
            return 10.0
        self.last_wa_spawn = time.time()
        if sync_only:
            wa_sync_begin()   # a reply-only run counts from now, even if the worker dies at start-up
        with db.db() as conn:
            jid = jobs.create_job(conn, "whatsapp", {})
        self.wa_job = jid
        try:
            jobs.spawn(jid, "whatsapp")
        except Exception as e:   # the browser worker could not even start: never leave a job "queued" that blocks every later try
            with db.db() as conn:
                conn.execute("UPDATE jobs SET status='failed', finished_at=?, progress=? WHERE id=?", (now(), f"Could not start: {str(e)[:120]}", jid))
            self.wa_job = None
            wa_pause_campaigns(f"WhatsApp could not start ({str(e)[:120]}). Restart the app; if it stays, reinstall it.")
            return 60.0
        self._set("sending", "Reading WhatsApp replies" if sync_only else "Sending on WhatsApp")
        return 5.0

    def send_one(self, row, s):
        mid, lead_id, ch, cid = row["id"], row["lead_id"], row["channel"], row["campaign_id"]
        with db.db() as conn:
            db.lock_writes(conn)
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
            elif row["followup_of"] is None and row["style"] != "single" and _elsewhere(conn, lead_id, cid):
                reason = _elsewhere_reason(conn, lead_id, cid)
            elif row["followup_of"] is not None and _replied(conn, lead_id):
                reason = "They replied, so no follow-up is sent"
            if reason:
                conn.execute("UPDATE messages SET status='skipped', error=? WHERE id=?", (reason, mid))
                self._maybe_finish(conn, cid)
                return
            held = _gap_hold(conn, dict(row))
            if held:   # they were messaged a moment ago: wait, don't stack a second message on top of it
                conn.execute("UPDATE messages SET status='queued', attempts=attempts-1, scheduled_at=?, error=? WHERE id=?",
                             (held, "Held back: they were messaged less than an hour ago", mid))
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
                header = messaging.send_email_any(to, subject, body, s)
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
        if self.net.pop(ch, None):   # it got through: the connection is back, forget the trouble
            with db.db() as conn:
                conn.execute("UPDATE campaigns SET last_error='' WHERE status='running' AND (last_error LIKE '%could not be reached%' OR last_error LIKE '%reach the mail server%')")
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
                # a 'best way' campaign has other ways to keep going: only this way waits (and the reason is shown)
                held = conn.execute("UPDATE campaigns SET last_error=? WHERE channel='auto' AND status='running' AND id IN "
                                    "(SELECT campaign_id FROM messages WHERE channel=? AND status='queued')", (str(e), ch)).rowcount
                if held:
                    self.next_ok[ch] = time.time() + 120   # try this way again in two minutes; the others carry on
                self._set("paused", str(e))
                return
            if e.kind == "offline":
                # Slow or no internet: the message is fine and is not "failed". Put it back, rest longer each time, and only
                # after many tries in a row pause the campaign so that you are told.
                n = (self.net.get(ch) or [0, 0.0])[0] + 1
                mins = (2, 5, 10, 20, 30)[min(n - 1, 4)]
                self.net[ch] = [n, time.time() + mins * 60]
                what = "phone" if ch == "sms" else "server"
                conn.execute("UPDATE messages SET status='queued', attempts=MAX(attempts-1,0), error=? WHERE id=?", (str(e), mid))
                if n >= 8:
                    self.net.pop(ch, None)
                    conn.execute("UPDATE campaigns SET status='paused', last_error=? WHERE channel=? AND status='running'",
                                 (f"Could not reach the {what} {n} times in a row. Check the connection, then resume. Last error: {e}", ch))
                else:
                    conn.execute("UPDATE campaigns SET last_error=? WHERE channel IN (?, 'auto') AND status='running' AND id IN "
                                 "(SELECT campaign_id FROM messages WHERE channel=? AND status='queued')",
                                 (f"The {what} could not be reached (try {n}). Trying again in {mins} min. {e}", ch, ch))
                self._set("waiting", f"{ch.upper() if ch == 'sms' else ch.title()} is resting: can't reach the {what} (try {n}). Trying again in {mins} min")
                return
            if e.kind == "transient" and attempts < 3:
                retry_at = (datetime.now() + timedelta(minutes=5 * attempts)).isoformat(sep=" ", timespec="seconds")
                conn.execute("UPDATE messages SET status='queued', error=?, scheduled_at=? WHERE id=?",
                             (str(e), retry_at, mid))
            else:
                bounced = ch == "email" and "Recipient refused" in str(e)
                if bounced:
                    L.mark_bounced(conn, lead_id, str(e))
                if bounced and reroute(conn, mid, "email", "the email address was refused"):
                    return
                if ch == "sms" and e.kind == "permanent" and reroute(conn, mid, "sms", "the text could not be delivered"):
                    return
                conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(e), mid))
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
        if not backup.enter_work():   # a restore is running: the inbox is checked again right after
            return {"at": now(), "ok": True, "message": "Waiting for a restore to finish", "stats": {}}
        try:
            return self._poll_now()
        finally:
            backup.exit_work()

    def _poll_now(self) -> dict:
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
