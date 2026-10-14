"""Guards for how outreach messages are written. Run: python tests/test_message_craft.py

The first message to a cold lead must follow the agreed shape (greet, true appreciation, what we do and how it helps, an
invitation to reply, the pay-only-when-satisfied term, one easy step) and must never push the AI, or a ready-made text,
into inventing praise or results. These checks fail the build if that is ever loosened by accident.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DERICBI_DATA"] = tempfile.mkdtemp(prefix="dericbi-craft-")
sys.dont_write_bytecode = True

import ai  # noqa: E402
import library  # noqa: E402
import messaging  # noqa: E402

problems = []


def check(ok, message):
    if not ok:
        problems.append(message)


# 1. the instructions the AI gets for a first message
first = ai._stage({"messages": []})
for needle, why in (("FIRST message", "must say it is the first message"), ("cold lead", "must say the lead is cold"),
                    ("greet them by their business name", "must greet by name"), ("TRUE", "appreciation must be true"),
                    ("never invented praise", "must forbid invented praise"), ("improve that", "must invite a reply about improving"),
                    ("different way", "must offer an alternative way"), ("pay only when satisfied", "must carry the pay-when-satisfied term"),
                    ("otherwise say nothing about price", "must not invent price terms"), ("ONE easy next step", "must end with one easy step")):
    check(needle in first, f"ai._stage(first message) {why} (missing: {needle!r})")
check(all(s in ai.CRAFT for s in ("greet them briefly by name", "never say you saw", "commits them to nothing")),
      "ai.CRAFT must keep the greeting, the no-invented-sightings rule and the low-risk line")

# 2. the follow-up instructions keep what the tests and the dedup logic rely on, and add value
fu = ai._stage({"messages": [{"direction": "out", "channel": "email", "created_at": "2026-01-01 08:00:00", "sent_at": "2026-01-01 08:05:00", "status": "sent", "body": "Hello, a first note."}]})
check("NO reply to our" in fu, "follow-up stage must say there was no reply")
check("genuinely useful insight" in fu and "pay only when satisfied" in fu, "follow-up must add small value and keep the pay term when the offer has one")

# 3. ready-made texts (used when no AI key is set)
LEAD = {"name": "Meru Chemist", "town": "Meru", "sector": "Pharmacy"}
BLANK = {"name": "Meru Chemist", "town": "", "sector": ""}
VALUES = {"sender": "Deric Marangu", "website": "https://dericbi.vercel.app", "whatsapp": "+254700000000", "email": "deric@example.com"}
for group in library.PAIN:
    subject, body = library.EMAIL[group]
    wa = library.WHATSAPP[group]
    for label, text in (("email subject", subject), ("email", body), ("whatsapp", wa)):
        bad = messaging.unknown_placeholders(text) if hasattr(messaging, "unknown_placeholders") else []
        check(not bad, f"{group} {label}: unknown placeholders {bad}")
        check("in {town}" not in text and "{sector}" not in text, f"{group} {label}: must not depend on town/sector, which can be blank")
    for label, text in (("email", body), ("whatsapp", wa)):
        check("only pay if you're satisfied" in text, f"{group} {label}: must carry the pay-only-when-satisfied line")
        check("improve this" in text and "different way" in text, f"{group} {label}: must invite a reply to improve or try another way")
        check("respect" in text, f"{group} {label}: must carry the sincere appreciation line")
        check(not re.search(r"\b(reviews?|rated|stars?|award|years of)\b", text, re.I), f"{group} {label}: must not claim reviews, ratings or awards")
    check(len(body.split()) <= 140, f"{group} email is too long ({len(body.split())} words)")
    check(len(wa) <= 700, f"{group} WhatsApp is too long ({len(wa)} characters)")
    # rendered for a normal lead and for a lead with a blank town and sector
    for lead in (LEAD, BLANK):
        shown = {**lead, **VALUES}
        rendered = [t.replace("{name}", shown["name"]).replace("{sender}", shown["sender"]).replace("{website}", shown["website"])
                     .replace("{whatsapp}", shown["whatsapp"]).replace("{email}", shown["email"]) for t in (subject, body, wa)]
        for t in rendered:
            check("{" not in t and "  " not in t, f"{group}: a rendered text has a leftover placeholder or a double space for {lead['name']}")

if problems:
    print("Message craft guard FAILED:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("Message craft guards OK")
