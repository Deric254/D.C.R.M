"""Lead storage: de-duplication, CRUD, search, CSV import/export, reply recording.

De-duplication happens in layers and is also enforced by unique indexes in
SQLite, so a duplicate can't slip in even if this code has a bug:
  1. same Google place id        (unique index)
  2. same phone number           (unique index, normalised to +254...)
  3. same email                  (unique index)
  4. same name + town, and same/unknown address  (checked here)
Leads are archived, never deleted, so removed leads can't come back either.
"""
import csv
import io
import re
import sqlite3

import ai
import db
import grading
from db import log_event
from util import (address_similarity, name_key, normalize_email, normalize_phone,
                  now, pretty_phone)

STATUSES = ["new", "contacted", "replied", "interested", "meeting", "won", "lost"]
GRADES = ["hot", "warm", "cold", "unclear"]
EDITABLE = {"name", "sector", "town", "address", "phone", "email", "website", "maps_url",
            "notes", "status", "grade", "next_followup", "do_not_contact"}


class Conflict(Exception):
    pass


def extract_place_key(url: str):
    if not url:
        return None
    m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", url, re.I)
    if m:
        return m.group(1).lower()
    m = re.search(r"!1s(ChIJ[\w-]+)", url)
    return m.group(1) if m else None


# ------------------------------------------------------------------ duplicates
def find_duplicate(conn, d: dict):
    """d must be the output of prepare(). Returns (row, reason) or (None, None)."""
    if d["place_key"]:
        row = conn.execute("SELECT * FROM leads WHERE place_key=?", (d["place_key"],)).fetchone()
        if row:
            return row, "same Google place"
    if d["phone_norm"]:
        row = conn.execute("SELECT * FROM leads WHERE phone_norm=?", (d["phone_norm"],)).fetchone()
        if row:
            return row, "same phone number"
    if d["email"]:
        row = conn.execute("SELECT * FROM leads WHERE email=?", (d["email"],)).fetchone()
        if row:
            return row, "same email"
    for row in conn.execute("SELECT * FROM leads WHERE name_key=?", (d["name_key"],)):
        if not row["address"] or not d["address"] or address_similarity(row["address"], d["address"]) >= 0.5:
            return row, "same name and town"
    return None, None


def prepare(data: dict) -> dict:
    name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()
    if not name:
        raise ValueError("Business name is required")
    town = str(data.get("town") or "").strip()
    phone_raw = str(data.get("phone") or "").strip()
    phone_norm, is_mobile = normalize_phone(phone_raw)
    maps_url = str(data.get("maps_url") or "").strip()
    place_key = (data.get("place_key") or extract_place_key(maps_url) or None)
    return {
        "name": name,
        "sector": str(data.get("sector") or "").strip(),
        "town": town,
        "address": str(data.get("address") or "").strip(),
        "phone": phone_raw if phone_raw.lower() not in ("n/a", "na", "none", "-") else "",
        "phone_norm": phone_norm,
        "is_mobile": 1 if is_mobile else 0,
        "email": normalize_email(data.get("email")),
        "website": str(data.get("website") or "").strip(),
        "maps_url": maps_url,
        "place_key": place_key,
        "name_key": name_key(name, town),
        "notes": str(data.get("notes") or "").strip(),
    }


def add_lead(conn, data: dict, source: str = "manual", merge: bool = True) -> dict:
    """Insert a lead unless it already exists.

    Returns {"id", "created", "reason"}; for duplicates `id` is the existing
    lead and, when merge=True, any blank fields on it are filled from `data`.
    """
    d = prepare(data)
    existing, reason = find_duplicate(conn, d)
    if existing:
        if merge:
            _fill_blanks(conn, existing, d)
        return {"id": existing["id"], "created": False, "reason": reason}

    status = data.get("status") if data.get("status") in STATUSES else "new"
    ts = now()
    try:
        cur = conn.execute(
            """INSERT INTO leads(name, sector, town, address, phone, phone_norm, is_mobile, email,
                                 website, maps_url, place_key, name_key, source, status, notes,
                                 created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d["name"], d["sector"], d["town"], d["address"], d["phone"], d["phone_norm"],
             d["is_mobile"], d["email"], d["website"], d["maps_url"], d["place_key"],
             d["name_key"], source, status, d["notes"], ts, ts),
        )
    except sqlite3.IntegrityError:
        # Lost a race with another process; re-check and report as duplicate.
        existing, reason = find_duplicate(conn, d)
        if existing:
            return {"id": existing["id"], "created": False, "reason": reason or "duplicate"}
        raise
    lead_id = cur.lastrowid
    log_event(conn, lead_id, "created", f"Added from {source}")
    return {"id": lead_id, "created": True, "reason": ""}


def _fill_blanks(conn, existing, d: dict):
    """Enrich an existing lead with details it was missing (never overwrites)."""
    groups = []  # each group is applied atomically so related columns never get out of step
    if not existing["phone_norm"] and d["phone_norm"]:
        groups.append({"phone": d["phone"], "phone_norm": d["phone_norm"], "is_mobile": d["is_mobile"]})
    if not existing["email"] and d["email"]:
        groups.append({"email": d["email"]})
    for k in ("website", "address", "maps_url", "sector"):
        if not existing[k] and d[k]:
            groups.append({k: d[k]})
    if not existing["place_key"] and d["place_key"]:
        groups.append({"place_key": d["place_key"]})
    for g in groups:
        cols = ", ".join(f"{k}=?" for k in g)
        try:
            conn.execute("SAVEPOINT fill")
            conn.execute(f"UPDATE leads SET {cols}, updated_at=? WHERE id=?", list(g.values()) + [now(), existing["id"]])
            conn.execute("RELEASE fill")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK TO fill")
            conn.execute("RELEASE fill")


# ----------------------------------------------------------------------- read
def row_to_dict(r) -> dict:
    d = dict(r)
    d["phone_display"] = pretty_phone(d.get("phone_norm")) or d.get("phone", "")
    return d


def get_lead(conn, lead_id: int):
    r = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    return row_to_dict(r) if r else None


def lead_detail(conn, lead_id: int):
    lead = get_lead(conn, lead_id)
    if not lead:
        return None
    msgs = [dict(r) for r in conn.execute(
        "SELECT m.*, c.name AS campaign_name FROM messages m "
        "LEFT JOIN campaigns c ON c.id = m.campaign_id WHERE m.lead_id=? ORDER BY m.id", (lead_id,))]
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE lead_id=? ORDER BY id", (lead_id,))]
    lead["messages"] = msgs
    lead["events"] = events
    return lead


SORTS = {
    "created_desc": "l.id DESC", "created_asc": "l.id ASC",
    "name": "l.name COLLATE NOCASE ASC", "updated": "l.updated_at DESC",
    "last_reply": "l.last_reply_at DESC", "grade": "CASE l.grade WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 WHEN 'unclear' THEN 2 WHEN 'cold' THEN 3 ELSE 4 END, l.id DESC",
}


def list_leads(conn, q="", sector="", town="", status="", grade="", has_phone=None, has_email=None,
               archived=False, dnc=None, page=1, page_size=50, sort="created_desc"):
    where, args = [], []
    where.append("l.archived = ?")
    args.append(1 if archived else 0)
    if q:
        like = f"%{q.strip().lower()}%"
        where.append("(lower(l.name) LIKE ? OR lower(l.address) LIKE ? OR l.phone LIKE ? OR l.phone_norm LIKE ? "
                     "OR lower(ifnull(l.email,'')) LIKE ? OR lower(l.notes) LIKE ?)")
        args += [like] * 6
    if sector:
        where.append("lower(l.sector) = ?"); args.append(sector.lower())
    if town:
        where.append("lower(l.town) = ?"); args.append(town.lower())
    if status:
        where.append("l.status = ?"); args.append(status)
    if grade == "none":
        where.append("l.grade = ''")
    elif grade:
        where.append("l.grade = ?"); args.append(grade)
    if has_phone is True:
        where.append("l.phone_norm IS NOT NULL")
    if has_email is True:
        where.append("l.email IS NOT NULL")
    if has_email is False:
        where.append("l.email IS NULL")
    if dnc is True:
        where.append("l.do_not_contact = 1")
    sql_where = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM leads l WHERE {sql_where}", args).fetchone()[0]
    order = SORTS.get(sort, SORTS["created_desc"])
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), 500)
    rows = conn.execute(
        f"SELECT l.* FROM leads l WHERE {sql_where} ORDER BY {order} LIMIT ? OFFSET ?",
        args + [page_size, (page - 1) * page_size],
    ).fetchall()
    return [row_to_dict(r) for r in rows], total


def facets(conn) -> dict:
    def col(c):
        return [r[0] for r in conn.execute(
            f"SELECT DISTINCT {c} FROM leads WHERE archived=0 AND {c} != '' ORDER BY {c} COLLATE NOCASE")]
    return {"sectors": col("sector"), "towns": col("town"), "statuses": STATUSES, "grades": GRADES}


# --------------------------------------------------------------------- update
def update_lead(conn, lead_id: int, patch: dict) -> dict:
    cur = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not cur:
        raise KeyError("Lead not found")
    sets, args = {}, []
    for k, v in patch.items():
        if k not in EDITABLE:
            continue
        if k == "name":
            v = re.sub(r"\s+", " ", str(v or "")).strip()
            if not v:
                raise ValueError("Business name can't be empty")
        elif k in ("sector", "town", "address", "website", "maps_url", "notes", "next_followup"):
            v = str(v or "").strip()
        sets[k] = v

    if "phone" in sets:
        raw = str(sets["phone"] or "").strip()
        norm, mob = normalize_phone(raw)
        sets["phone"], sets["phone_norm"], sets["is_mobile"] = raw, norm, 1 if mob else 0
    if "email" in sets:
        raw = str(sets["email"] or "").strip()
        em = normalize_email(raw)
        if raw and not em:
            raise ValueError("That doesn't look like a valid email address")
        sets["email"] = em
        sets["email_bounced"] = 0 if em != cur["email"] else cur["email_bounced"]
    if "maps_url" in sets:
        sets["place_key"] = extract_place_key(sets["maps_url"]) or cur["place_key"]
    if "status" in sets and sets["status"] not in STATUSES:
        raise ValueError("Unknown status")
    if "grade" in sets:
        g = sets["grade"] or ""
        if g and g not in GRADES:
            raise ValueError("Unknown grade")
        sets["grade"] = g
        sets["grade_locked"] = 1 if g else 0
    if "do_not_contact" in sets:
        sets["do_not_contact"] = 1 if sets["do_not_contact"] else 0
    if "name" in sets or "town" in sets:
        sets["name_key"] = name_key(sets.get("name", cur["name"]), sets.get("town", cur["town"]))

    if not sets:
        return get_lead(conn, lead_id)
    sets["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in sets)
    try:
        conn.execute(f"UPDATE leads SET {cols} WHERE id=?", list(sets.values()) + [lead_id])
    except sqlite3.IntegrityError as e:
        msg = str(e)
        which = "phone number" if "phone_norm" in msg else "email" if "email" in msg else "Google place" if "place_key" in msg else "record"
        raise Conflict(f"Another lead already has that {which}.")

    if "status" in sets and sets["status"] != cur["status"]:
        log_event(conn, lead_id, "status", f"{cur['status']} → {sets['status']}")
    if "grade" in patch and sets["grade"] != cur["grade"]:
        log_event(conn, lead_id, "grade", f"Grade set to {sets['grade'] or 'auto'}")
    if "do_not_contact" in sets and sets["do_not_contact"] != cur["do_not_contact"]:
        log_event(conn, lead_id, "dnc", "Marked do-not-contact" if sets["do_not_contact"] else "Do-not-contact removed")
    if "notes" in sets and sets["notes"] != cur["notes"]:
        log_event(conn, lead_id, "note", "Notes updated")
    return get_lead(conn, lead_id)


def set_archived(conn, lead_ids, archived: bool):
    for lid in lead_ids:
        conn.execute("UPDATE leads SET archived=?, updated_at=? WHERE id=?", (1 if archived else 0, now(), lid))
        log_event(conn, lid, "archive", "Archived" if archived else "Restored")


# ---------------------------------------------------------------- replies
def record_reply(conn, lead_id: int, channel: str, text: str, subject: str = "",
                 message_id: str = None, campaign_id=None, forced_grade: str = None,
                 received_at: str = None) -> dict:
    """Store an inbound message, grade it and update the lead. Returns {id, grade, ...}."""
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        raise KeyError("Lead not found")
    if message_id:
        dup = conn.execute("SELECT id FROM messages WHERE direction='in' AND message_id_header=?", (message_id,)).fetchone()
        if dup:
            return {"id": dup["id"], "duplicate": True}

    res = grading.classify(text)
    if res["grade"] == "unclear" and not forced_grade:
        s = db.get_settings(conn)
        guess = ai.grade_reply(s, text) if s["ai_grade_replies"] else None
        if guess:
            res = {"grade": guess, "score": res["score"], "reasons": ["AI"]}
    grade = forced_grade or res["grade"]
    ts = received_at or now()
    if campaign_id is None:
        row = conn.execute(
            "SELECT campaign_id FROM messages WHERE lead_id=? AND direction='out' AND campaign_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
        campaign_id = row["campaign_id"] if row else None

    cur = conn.execute(
        """INSERT INTO messages(lead_id, campaign_id, direction, channel, to_addr, subject, body, status,
                                message_id_header, grade, score, reasons, created_at, sent_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (lead_id, campaign_id, "in", channel,
         (lead["email"] if channel == "email" else lead["phone_norm"]) or "",
         subject, text, "received", message_id, grade, res["score"], ", ".join(res["reasons"]), ts, ts),
    )
    msg_id = cur.lastrowid

    updates = {"updated_at": now()}
    if grade not in ("auto", "bounce"):
        updates["last_reply_at"] = ts
    if grade == "optout":
        updates["do_not_contact"] = 1
        log_event(conn, lead_id, "dnc", f"Opted out via {channel} reply ({', '.join(res['reasons'])})")
    if grade in ("hot", "warm", "cold") and not lead["grade_locked"]:
        updates["grade"] = grade
    elif grade == "unclear" and not lead["grade"] and not lead["grade_locked"]:
        updates["grade"] = "unclear"
    if grade not in ("auto", "bounce", "optout"):
        if lead["status"] in ("new", "contacted"):
            updates["status"] = "replied"
        if grade == "hot" and lead["status"] in ("new", "contacted", "replied"):
            updates["status"] = "interested"
    cols = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE leads SET {cols} WHERE id=?", list(updates.values()) + [lead_id])
    log_event(conn, lead_id, "reply", f"{channel} reply graded {grade}")
    return {"id": msg_id, "grade": grade, "score": res["score"], "reasons": res["reasons"], "duplicate": False}


def mark_bounced(conn, lead_id: int, detail: str = ""):
    conn.execute("UPDATE leads SET email_bounced=1, updated_at=? WHERE id=?", (now(), lead_id))
    log_event(conn, lead_id, "bounce", detail or "Email bounced")


def regrade_message(conn, message_id: int, grade: str):
    if grade not in GRADES + ["optout"]:
        raise ValueError("Unknown grade")
    m = conn.execute("SELECT * FROM messages WHERE id=? AND direction='in'", (message_id,)).fetchone()
    if not m:
        raise KeyError("Message not found")
    conn.execute("UPDATE messages SET grade=? WHERE id=?", (grade, message_id))
    if grade == "optout":
        conn.execute("UPDATE leads SET do_not_contact=1, updated_at=? WHERE id=?", (now(), m["lead_id"]))
        log_event(conn, m["lead_id"], "dnc", "Marked do-not-contact after manual re-grade")
    else:
        conn.execute("UPDATE leads SET grade=?, grade_locked=1, updated_at=? WHERE id=?", (grade, now(), m["lead_id"]))
        log_event(conn, m["lead_id"], "grade", f"Reply re-graded to {grade}")


# ---------------------------------------------------------------- import/export
_HEADER_MAP = {
    "name": "name", "business name": "name", "business": "name", "company": "name", "title": "name",
    "sector": "sector", "category": "sector", "type": "sector", "industry": "sector",
    "town": "town", "location": "town", "city": "town", "area": "town",
    "address": "address",
    "phone": "phone", "telephone": "phone", "mobile": "phone", "tel": "phone", "phone number": "phone",
    "contact info": "contact", "contact": "contact",
    "email": "email", "e-mail": "email", "email address": "email",
    "website": "website", "web": "website", "url": "website",
    "maps url": "maps_url", "google maps": "maps_url",
    "status": "status", "pitch status": "status",
    "notes": "notes", "note": "notes",
}


def import_csv(conn, text: str) -> dict:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("The file looks empty")
    mapping = {h: _HEADER_MAP.get(h.strip().lower()) for h in reader.fieldnames}
    if "name" not in mapping.values():
        raise ValueError("No business name column found (expected a header like 'Business Name' or 'Name')")
    created = dups = invalid = 0
    samples = []
    for i, row in enumerate(reader, start=2):
        rec = {}
        for h, v in row.items():
            key = mapping.get(h)
            if key and v is not None:
                rec[key] = v.strip()
        contact = rec.pop("contact", "")
        if contact:
            if "@" in contact and not rec.get("email"):
                rec["email"] = contact
            elif not rec.get("phone"):
                rec["phone"] = contact
        if rec.get("status", "").lower() not in STATUSES:
            rec.pop("status", None)
        else:
            rec["status"] = rec["status"].lower()
        try:
            res = add_lead(conn, rec, source="import")
        except ValueError:
            invalid += 1
            continue
        if res["created"]:
            created += 1
        else:
            dups += 1
            if len(samples) < 8:
                samples.append(f"Row {i}: {rec.get('name', '')} ({res['reason']})")
    return {"created": created, "duplicates": dups, "invalid": invalid, "samples": samples}


EXPORT_COLUMNS = ["id", "name", "sector", "town", "address", "phone", "email", "website", "status",
                  "grade", "do_not_contact", "email_bounced", "next_followup", "notes", "source",
                  "created_at", "last_contacted_at", "last_reply_at"]


def export_csv(conn, include_archived=False) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(EXPORT_COLUMNS)
    sql = "SELECT * FROM leads" + ("" if include_archived else " WHERE archived=0") + " ORDER BY id"
    for r in conn.execute(sql):
        w.writerow([r[c] if c != "phone" else (r["phone_norm"] or r["phone"]) for c in EXPORT_COLUMNS])
    return out.getvalue()
