"""Guards for how outreach messages are written. Run: python tests/test_message_craft.py

The AI must know who is writing (a person, in the first person, from their own business), be given a hard length and a few
short style rules, and a message that still comes out too long must never be sent: it is sent back once to be shortened,
and failing that the ready-made message is used. A fake AI stands in for the real one, so this runs offline.
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DERICBI_DATA"] = tempfile.mkdtemp(prefix="dericbi-craft-")
sys.dont_write_bytecode = True

import config  # noqa: E402
config.set_data_dir(os.environ["DERICBI_DATA"])
import ai  # noqa: E402
import campaigns  # noqa: E402
import db  # noqa: E402
import library  # noqa: E402
import messaging  # noqa: E402

problems = []


def check(ok, message):
    if not ok:
        problems.append(message)


def words(text):
    return len(re.findall(r"\S+", text))


S = dict(db.SETTING_DEFAULTS)                                   # your settings as they ship
S_NO_PAY = dict(S, ai_pitch="I help local businesses with their sales and stock records.")
LEAD = {"name": "Kamau Pharmacy", "sector": "Pharmacy", "town": "Meru", "address": "Moi Avenue", "website": "", "status": "new",
        "grade": "", "notes": "", "messages": [], "do_not_contact": 0}
SENT = [{"direction": "out", "channel": "email", "status": "sent", "body": "Hello, a first note.",
         "sent_at": "2026-01-01 08:00:00", "created_at": "2026-01-01 08:00:00"}]
LONG_EMAIL = "Subject: Hello\n\n" + " ".join(["word"] * 140)
SHORT_EMAIL = "Subject: quick idea for Kamau Pharmacy\n\nHello Kamau Pharmacy, a short note. Worth a look?\n\nDeric"
LONG_WA = "x " * 200
SHORT_WA = "Hello Kamau Pharmacy, Deric from DericBI. Worth a quick look?"


class FakeAI:
    """Replaces ai.ask: gives the answers in turn and remembers every request."""
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, s, system, user, **k):
        self.calls.append((system, user))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def with_ai(fake, fn):
    real, ai.ask = ai.ask, fake
    try:
        return fn()
    finally:
        ai.ask = real


# 1. the AI knows who is writing
fake = FakeAI(SHORT_EMAIL)
with_ai(fake, lambda: ai.draft_for_lead(S, dict(LEAD), "email", ""))
system, request = fake.calls[0]
for needle, why in (("Deric Marangu", "the sender's name"), ("DericBI", "the brand name"), ("first person", "first-person writing"),
                    ("never 'we'", "no company voice"), ("never 'the team'", "no company voice")):
    check(needle in system, f"the AI's system instructions must carry {why} (missing {needle!r})")
check("owner of DericBI" in request and "I'm Deric Marangu, a Business Intelligence specialist, and I run DericBI" in request,
      "the request must carry who I am and my offer, in the first person")
check("He helps" not in request, "the offer must not be told in the third person")

# 2. the pay-when-satisfied term is stated only when the offer says so
check(ai._pay_line(S) == "You only pay when you see value." and 'add this line exactly: "You only pay when you see value."' in request,
      "with a pay line in the offer, the job must tell the AI to use that exact sentence")
check(request.count("You only pay when you see value.") >= 3, "the offer, the job and the example must all carry the owner's own pay line")
check(ai._pay_line({"ai_pitch": "I help shops. Payment is after the work is delivered. Call me."}) == "Payment is after the work is delivered.",
      "any sentence about paying is found, whatever its wording")
check(ai._pay_line(S_NO_PAY) == "", "an offer that says nothing about paying has no pay line")
fake = FakeAI(SHORT_EMAIL)
with_ai(fake, lambda: ai.draft_for_lead(S_NO_PAY, dict(LEAD), "email", ""))
no_pay = fake.calls[0][1]
check("You only pay when you see value" not in no_pay, "without a pay line in the offer, nothing may promise one")
check("otherwise say nothing about price" in no_pay, "without the term, price must not be mentioned")

# 3. hard limits and short style rules
check("at most 100 words" in request and "at most 6 words" in request, "the email limits must be stated")
job = ai._stage({"messages": []}, S)
for needle in ("I came across their business online", "who I am in one line", "two or three concrete things", "how I deliver it",
               "no-pressure close", "no response is needed", "no pressure, no urgency, no hype"):
    check(needle in job, f"the first-message plan must follow the owner's own message (missing {needle!r})")
check("no pressure" in ai.CRAFT and "not replying is fine" in ai.CRAFT, "the style rules must keep every message pressure-free")
fake = FakeAI(SHORT_WA)
with_ai(fake, lambda: ai.draft_for_lead(S, dict(LEAD), "whatsapp", ""))
check("at most 300 characters" in fake.calls[0][1], "the WhatsApp limit must be stated")
for needle in ("I hope this finds you well", "My name is", "I am writing to", "no exclamation marks", "no emojis", "ONE easy question",
               "never say I saw their work"):
    check(needle in ai.CRAFT, f"the style rules must say: {needle}")
check("FIRST message" in ai._stage({"messages": []}), "the first-message job must say it is the first message")

# 4. the examples obey the limits they teach
for cfg in (S, S_NO_PAY):
    for ch in ("email", "whatsapp"):
        for convo in ([], SENT):
            ex = ai._example(cfg, ch, convo)
            if ch == "email":
                subject, body = ex.split("\n\n", 1)
                check(not ai._too_long("email", subject.split(": ", 1)[1], body), f"an email example is over the limit: {words(body)} words")
                check(words(re.sub(r"(?im)^\s*(website|whatsapp)\b.*$", "", body)) <= 100, "an email example is over the 100 words it asks for")
            else:
                check(not ai._too_long("whatsapp", "", ex) and len(ex) <= 300, f"a WhatsApp example is over the limit: {len(ex)} characters")
check(ai._example(S, "email", [{"direction": "in", "channel": "email", "status": "received", "body": "yes"}]) == "", "no example for a reply")

# 5. a draft that is too long is sent back once, and never passed on long
for channel, long_, short, count in (("email", LONG_EMAIL, SHORT_EMAIL, 2), ("whatsapp", LONG_WA, SHORT_WA, 2)):
    fake = FakeAI(long_, short)
    out = with_ai(fake, lambda: ai.draft_for_lead(S, dict(LEAD), channel, ""))
    check(len(fake.calls) == count and "too_long" not in out and "too long" in fake.calls[1][1], f"{channel}: a long draft must be sent back once to be shortened")
    check(not ai._too_long(channel, out["subject"], out["body"]), f"{channel}: the shortened draft must be inside the limit")
    fake = FakeAI(long_)
    out = with_ai(fake, lambda: ai.draft_for_lead(S, dict(LEAD), channel, ""))
    check(out.get("too_long") is True and len(fake.calls) == 2, f"{channel}: a draft that stays too long must be flagged after exactly one retry")
    fake = FakeAI(short)
    with_ai(fake, lambda: ai.draft_for_lead(S, dict(LEAD), channel, ""))
    check(len(fake.calls) == 1, f"{channel}: a draft inside the limit costs one request, not two")
contact = "Hello Kamau Pharmacy,\n\n" + " ".join(["word"] * 90) + "\n\nDeric\n\nWebsite: https://dericbi.vercel.app | WhatsApp: +254791360805 | Email: dericmarangu@gmail.com"
check(not ai._too_long("email", "quick idea", contact), "the contact line must not count toward the 100 words")

# 6. the sender never sends a message that stayed too long: it uses the ready-made one
row = {"id": 7, "channel": "email", "brief": "", "step": 0, "followup_of": None}
lead = dict(LEAD, email="owner@example.com", phone_norm="", is_mobile=0)
s_ai = dict(S, ai_key_gemini="test-key")
subject, body = with_ai(FakeAI(LONG_EMAIL), lambda: campaigns.write_for_lead(row, lead, s_ai))
check("came across" in body and "You only pay when you see value." in body and subject.strip(), "a too-long AI email must be replaced by the ready-made one")
check(words(body) <= 130, "the ready-made fallback must itself be short")
subject, body = with_ai(FakeAI(LONG_WA), lambda: campaigns.write_for_lead(dict(row, channel="whatsapp"), lead, s_ai))
check("came across" in body and "You only pay when you see value." in body, "a too-long AI WhatsApp message must be replaced by the ready-made one")

# 7. who you are is the default, and an old default is replaced
check(db.DEFAULT_PITCH.startswith("I'm Deric Marangu, a Business Intelligence specialist") and "DericBI" in db.DEFAULT_PITCH
      and db.DEFAULT_PITCH.rstrip().endswith("You only pay when you see value."),
      "the default pitch must be in the first person, name the brand and end with the owner's own pay line")
for offer in ("slow-moving stock", "best-performing sales periods", "profitable product combinations", "ready-to-use tracking system", "clear report"):
    check(offer in db.DEFAULT_PITCH, f"the default pitch must carry the owner's offer: {offer}")
check(db.SETTING_DEFAULTS.get("brand_name") == "DericBI", "the brand name must default to DericBI")
check("brand_name" in (ROOT / "backend" / "static" / "view-settings.js").read_text(encoding="utf-8"), "the Settings screen must have the business name field")
db.init_db()
for earlier in (db.OLD_DEFAULT_PITCH, db.PREVIOUS_DEFAULT_PITCH):
    conn = db.connect()
    conn.execute("DELETE FROM settings WHERE key='ai_pitch'")
    conn.execute("INSERT INTO settings(key, value) VALUES('ai_pitch', ?)", (json.dumps(earlier),))
    conn.commit(); conn.close()
    db.init_db()
    check(db.get_settings()["ai_pitch"] == db.DEFAULT_PITCH, "a saved, unedited copy of an earlier default pitch must be replaced by the current one")
conn = db.connect()
conn.execute("DELETE FROM settings WHERE key='ai_pitch'")
conn.execute("INSERT INTO settings(key, value) VALUES('ai_pitch', ?)", (json.dumps("My own words about what I do."),))
conn.commit(); conn.close()
db.init_db()
check(db.get_settings()["ai_pitch"] == "My own words about what I do.", "a pitch you wrote yourself must never be touched")

# 8. ready-made texts (used when no AI key is set)
LEADS = {"name": "Meru Chemist", "town": "Meru", "sector": "Pharmacy"}
BLANK = {"name": "Meru Chemist", "town": "", "sector": ""}
VALUES = {"sender": "Deric Marangu", "website": "https://dericbi.vercel.app", "whatsapp": "+254700000000", "email": "deric@example.com"}
PAIN_PHRASE = lambda g: library.PAIN[g]  # noqa: E731
for group in library.PAIN:
    subject, body = library.EMAIL[group]
    wa = library.WHATSAPP[group]
    for label, text in (("email subject", subject), ("email", body), ("whatsapp", wa)):
        bad = messaging.unknown_placeholders(text) if hasattr(messaging, "unknown_placeholders") else []
        check(not bad, f"{group} {label}: unknown placeholders {bad}")
        check("in {town}" not in text and "{sector}" not in text, f"{group} {label}: must not depend on town/sector, which can be blank")
    for label, text in (("email", body), ("whatsapp", wa)):
        check("You only pay when you see value." in text, f"{group} {label}: must carry the owner's pay line")
        check("came across" in text and "impressed by what you're doing" in text, f"{group} {label}: must open as the owner's own message does")
        check("Business Intelligence specialist" in text and "tracking system" in text or label == "whatsapp", f"{group} {label}: must say who I am and how I deliver")
        check("Just reply. If not, no response is needed." in text, f"{group} {label}: must invite a reply with no pressure")
        check("slow-moving" in text or PAIN_PHRASE(group) in text, f"{group} {label}: must name what this kind of business can learn")
        check(not re.search(r"\b(urgent|hurry|limited|act now|guarantee[d]?|free|last chance|today only)\b", text, re.I), f"{group} {label}: no pressure or hype words")
        check(not re.search(r"\b(reviews?|rated|stars?|award|years of)\b", text, re.I), f"{group} {label}: must not claim reviews, ratings or awards")
    check(words(re.sub(r"(?im)^\s*website\b.*$", "", body)) <= 115, f"{group} email is too long ({words(body)} words)")
    check(len(wa) <= 700, f"{group} WhatsApp is too long ({len(wa)} characters)")
    for lead_ in (LEADS, BLANK):   # through the app's own renderer, the very code that fills in a real message
        for ch, t in (("email", subject), ("email", body), ("whatsapp", wa)):
            out = messaging.render_template(t, lead_, S, ch)
            check("{" not in out and "  " not in out, f"{group}: a rendered {ch} text has a leftover placeholder or a double space for {lead_['name']}")
            check("Deric" in out or t == subject, f"{group}: the sender's first name must be filled in")

if problems:
    print("Message craft guard FAILED:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("Message craft guards OK")
