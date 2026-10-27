"""Grow: ways to reach many businesses that do not send anything by themselves.

 * Content: turn one finding into ready-to-post drafts (you copy and post them yourself).
 * Partners: associations, chambers, software vendors, accountants: one yes reaches their whole membership. A separate list
   from the leads, so no campaign can ever message a partner by accident.

Nothing here touches the sender, the campaigns or WhatsApp. Its own tables are created on first use.
"""
import re

import ai
import db
from util import now

PLATFORMS = {
    "linkedin": ("LinkedIn", "A LinkedIn post of 110 to 170 words: a one-line hook, three or four short paragraphs, and ONE question at the end."),
    "facebook": ("Facebook", "A Facebook post of 70 to 120 words, warm and plain, for business owners, ending with one easy question."),
    "whatsapp_status": ("WhatsApp Status", "A WhatsApp Status text of at most 230 characters: the finding in one or two sentences and what it means."),
    "tiktok": ("TikTok / Reels script", "A 30-second spoken script on four lines labelled HOOK, FINDING, MEANING, ASK. Short spoken sentences."),
}
PARTNER_KINDS = ("association", "chamber", "software vendor", "accountant", "county office", "other")
PARTNER_STATUS = ("to_contact", "contacted", "replied", "agreed", "declined")

_DIMINISH = re.compile(r"\b(?:small|tiny|little|micro|humble)[- ]?(?:and medium )?(?=(?:business|shop|enterprise|firm|outlet|store|trader|operation))", re.I)

RULES = (
    "Rules: write with respect for the people who run businesses (never call a business small, tiny, little or micro). "
    "Plain words, short sentences, no jargon, no emojis, no hashtags spam (at most three hashtags), no exclamation marks, no hype. "
    "Use ONLY the facts given: never invent numbers, names, clients, results, prices or guarantees. Never name a client or a business. "
    "The call to action is gentle and free: offer to look at their own sales sheet and show them what stands out."
)


def _system(s: dict, what: str) -> str:
    name = (s.get("sender_name") or "").strip() or "the owner"
    return (f"You write {what} as {name}, one person who runs a business analytics service, in the first person ('I'). "
            "Calm, warm, direct, plain words, with respect for the people who run businesses. Reply in English unless asked otherwise. "
            "Output only what was asked, with no preface, no quotation marks and no notes.")


def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS grow_partners(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'other', contact TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'to_contact',
        note TEXT NOT NULL DEFAULT '', next_step TEXT NOT NULL DEFAULT '', next_date TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS grow_posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, finding TEXT NOT NULL, platform TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)""")


def respectful(text: str) -> str:
    out = _DIMINISH.sub("", text or "")
    return out[:1].upper() + out[1:] if out and text[:1].isupper() else out


# ------------------------------------------------------------------ content
def _template(platform: str, finding: str, s: dict) -> str:
    me = (s.get("sender_name") or "DericBI").strip()
    f = finding.strip().rstrip(".")
    if platform == "whatsapp_status":
        return f"{f}. Send me your sales sheet and I will show you what stands out, free. — {me}"[:230]
    if platform == "tiktok":
        return (f"HOOK: Here is something I keep finding in business records.\nFINDING: {f}.\n"
                f"MEANING: It is usually in the numbers already, just not visible.\nASK: Send me your sales sheet and I will show you what stands out, free. — {me}")
    if platform == "linkedin":
        return (f"{f}.\n\nIt is rarely visible day to day, because the money is spread across many small decisions: what is ordered, what sits, "
                f"what sells together.\n\nThe records most businesses already keep can show it. It takes one sales sheet and a careful look.\n\n"
                f"If you would like to see what stands out in yours, send it to me and I will walk you through it, free.\n\nWhat would you most like to know about your own numbers?")
    return (f"{f}.\n\nMost owners already have the answer in their records. They just have not had it laid out clearly.\n\n"
            f"Send me your sales sheet and I will show you what stands out, free. No pressure.\n\nWhat would you want to know first?")


def draft_posts(s: dict, finding: str, platforms: list, audience: str = "") -> dict:
    """{platform: text}. Uses the AI when it is set up and falls back to plain templates, so the screen always gives something."""
    finding = (finding or "").strip()
    if len(finding) < 10:
        raise ValueError("Write the finding in a sentence or two first.")
    if len(finding) > 800:
        raise ValueError("Keep the finding under 800 characters.")
    platforms = [p for p in platforms if p in PLATFORMS] or ["linkedin"]
    out, used_ai = {}, False
    if ai.configured(s):
        spec = "\n".join(f"### {p}\n({PLATFORMS[p][1]})" for p in platforms)
        try:
            text = ai.ask(s, _system(s, "public social media posts"), (
                f"Finding from my work (anonymised): {finding}\nAudience: {audience.strip() or 'business owners in Kenya'}.\n"
                f"My offer in one line: {(s.get('ai_pitch') or '').strip()[:400]}\n{RULES}\n"
                "Write one draft for each platform below. Start each with its '### name' line exactly as given, then the draft.\n" + spec),
                max_tokens=1400, temperature=0.8)
            for chunk in re.split(r"(?m)^###\s*", text):
                head, _, rest = chunk.partition("\n")
                key = head.strip().lower().replace(" ", "_")
                if key in platforms and rest.strip():
                    out[key] = respectful(rest.strip())
            used_ai = bool(out)
        except Exception:
            out = {}
    for p in platforms:
        out.setdefault(p, _template(p, finding, s))
    with db.db() as conn:
        ensure(conn)
        for p in platforms:
            conn.execute("INSERT INTO grow_posts(finding, platform, body, created_at) VALUES(?,?,?,?)", (finding, p, out[p], now()))
        conn.execute("DELETE FROM grow_posts WHERE id NOT IN (SELECT id FROM grow_posts ORDER BY id DESC LIMIT 200)")
    return {"posts": [{"platform": p, "label": PLATFORMS[p][0], "body": out[p]} for p in platforms], "ai": used_ai}


def recent_posts(limit: int = 30) -> list:
    with db.db() as conn:
        ensure(conn)
        return [dict(r) for r in conn.execute("SELECT * FROM grow_posts ORDER BY id DESC LIMIT ?", (limit,))]


def delete_post(pid: int):
    with db.db() as conn:
        ensure(conn)
        conn.execute("DELETE FROM grow_posts WHERE id=?", (pid,))


# ----------------------------------------------------------------- partners
def _clean(d: dict) -> dict:
    out = {}
    for k in ("name", "contact", "email", "phone", "note", "next_step", "next_date"):
        if k in d:
            out[k] = str(d[k] or "").strip()[:400 if k != "note" else 2000]
    if "kind" in d:
        out["kind"] = d["kind"] if d["kind"] in PARTNER_KINDS else "other"
    if "status" in d:
        if d["status"] not in PARTNER_STATUS:
            raise ValueError("Unknown status")
        out["status"] = d["status"]
    if out.get("email") and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", out["email"]):
        raise ValueError("That email address doesn't look right.")
    return out


def list_partners() -> list:
    with db.db() as conn:
        ensure(conn)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM grow_partners ORDER BY CASE status WHEN 'agreed' THEN 0 WHEN 'replied' THEN 1 WHEN 'contacted' THEN 2 "
            "WHEN 'to_contact' THEN 3 ELSE 4 END, id DESC")]


def add_partner(d: dict) -> dict:
    c = _clean(d)
    if not c.get("name"):
        raise ValueError("Give the partner a name.")
    with db.db() as conn:
        ensure(conn)
        cur = conn.execute(
            "INSERT INTO grow_partners(name, kind, contact, email, phone, status, note, next_step, next_date, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (c["name"], c.get("kind", "other"), c.get("contact", ""), c.get("email", ""), c.get("phone", ""), c.get("status", "to_contact"),
             c.get("note", ""), c.get("next_step", ""), c.get("next_date", ""), now(), now()))
        return dict(conn.execute("SELECT * FROM grow_partners WHERE id=?", (cur.lastrowid,)).fetchone())


def update_partner(pid: int, d: dict) -> dict:
    c = _clean(d)
    if "name" in c and not c["name"]:
        raise ValueError("Give the partner a name.")
    with db.db() as conn:
        ensure(conn)
        if not conn.execute("SELECT 1 FROM grow_partners WHERE id=?", (pid,)).fetchone():
            raise KeyError("Partner not found")
        if c:
            conn.execute("UPDATE grow_partners SET " + ", ".join(f"{k}=?" for k in c) + ", updated_at=? WHERE id=?", (*c.values(), now(), pid))
        return dict(conn.execute("SELECT * FROM grow_partners WHERE id=?", (pid,)).fetchone())


def delete_partner(pid: int):
    with db.db() as conn:
        ensure(conn)
        conn.execute("DELETE FROM grow_partners WHERE id=?", (pid,))


def partner_pitch(s: dict, pid: int) -> dict:
    """A short note to a partner offering their members a free session. You send it yourself (it opens in your own mail app)."""
    with db.db() as conn:
        ensure(conn)
        p = conn.execute("SELECT * FROM grow_partners WHERE id=?", (pid,)).fetchone()
    if not p:
        raise KeyError("Partner not found")
    me = (s.get("sender_name") or "DericBI").strip()
    who = (p["contact"] or "").split(" ")[0] or "there"
    subject = f"A free session for {p['name']} members"
    body = (f"Hello {who},\n\nI run DericBI, and I help business owners see what their own sales and stock records are already saying: "
            f"what sits too long, which months bring the most, and where money slips away.\n\n"
            f"I would like to offer {p['name']} members a free session or a short report on their own numbers, at no cost to you or to them. "
            f"Would it help to share this with your members, or would a short talk suit you better?\n\nWarm regards,\n{me}")
    if ai.configured(s):
        try:
            text = ai.ask(s, _system(s, "a short partnership email"), (
                f"Write a short email (at most 110 words) to {p['contact'] or 'the person in charge'} at {p['name']} ({p['kind']}). "
                f"Offer their members a free session or short report on their own sales and stock records. My offer: {(s.get('ai_pitch') or '')[:400]}\n"
                f"Their notes: {p['note'][:300] or 'none'}\n{RULES}\nFirst line 'Subject: ...' (at most 7 words), blank line, then the body. Sign off as {me}."),
                max_tokens=500, temperature=0.7)
            subj, rest = ai._split_subject(text)
            if subj and rest.strip():
                subject, body = respectful(subj), respectful(rest.strip())
        except Exception:
            pass
    return {"subject": subject, "body": body, "to": p["email"]}
