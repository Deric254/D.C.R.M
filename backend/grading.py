"""Rule-based reply grading.

classify(text) -> {"grade", "score", "reasons"}

Grades:
  hot      clear buying signal (asks for price/details/call, says yes)
  warm     engaged but not committed (maybe later, who is this, send email)
  cold     says no / not interested / already has a system
  unclear  nothing recognisable, needs a human look
  optout   asked to stop being contacted (or wrong number) -> do-not-contact
  auto     out-of-office / automatic reply (ignored for pipeline purposes)

It is deliberately transparent: every pattern is listed below, edit freely.
The first matching group "consumes" its text so that e.g. "not interested"
doesn't also count as "interested".
"""
import re

# (regex, label, weight)
OPTOUT = [
    (r"^\s*stop\W*$", "STOP", 0),
    (r"\bstop\b.{0,20}\b(messag|text|email|sms|send|contact|disturb|call)", "stop messaging", 0),
    (r"\bunsubscribe\b", "unsubscribe", 0),
    (r"\bopt[\s-]?out\b", "opt out", 0),
    (r"\bremove (me|us|my|our)\b", "remove me", 0),
    (r"\b(do not|don'?t|dont|never)\b.{0,12}\b(contact|message|text|email|sms|call|disturb|send)\b", "do not contact", 0),
    (r"\bacha kunitumia\b|\bacha kutuma\b", "acha kunitumia", 0),
    (r"\bsitaki (ujumbe|message|sms)\b", "sitaki ujumbe", 0),
    (r"\bniondoe\b|\btuondoe\b|\bnitoe\b", "niondoe", 0),
    (r"\busi(nitumie|tume|tuma|nitafute|tupigie|nipigie)\b", "usinitumie", 0),
    (r"\bwrong (number|person)\b|\bnambari (si sahihi|umekosea)\b|\bumekosea nambari\b", "wrong number", 0),
]

AUTO = [
    (r"\bout of (the )?office\b", "out of office", 0),
    (r"\bauto[\s-]?(matic )?(reply|response|responder)\b", "auto-reply", 0),
    (r"\bautomatic(ally)? (reply|response|generated)\b", "automatic reply", 0),
    (r"\bi am (currently )?away\b|\bi'?m (currently )?away\b", "away", 0),
    (r"\bon (annual )?(leave|vacation|holiday)\b", "on leave", 0),
    (r"\bdo not reply to this (email|message)\b", "no-reply notice", 0),
]

COLD = [
    (r"\bnot interested\b|\bno interest\b|\bsi interested\b|\bsiko interested\b|\bhatuna interest\b", "not interested", -3),
    (r"\bno,? thank(s| you)\b", "no thanks", -3),
    (r"\b(we|i) (do not|don'?t|dont) (need|want|require)\b", "don't need", -3),
    (r"\b(no need|not needed|not required|not looking)\b", "no need", -3),
    (r"\b(already|we)\s+(have|use|using|got)\b.{0,25}\b(system|software|app|erp|pos|provider)\b", "already has a system", -3),
    (r"\bnot for us\b|\bnot applicable\b|\bnot relevant\b", "not for us", -3),
    (r"\bsitaki\b|\bhatutaki\b|\bhatuna haja\b|\bhatuhitaji\b|\bhatuitaji\b|\bhatuna shida\b", "sitaki / hatuhitaji", -3),
    (r"\btuna (system|mfumo)\b", "tuna system", -3),
    (r"^\s*(no|hapana|la)\W*$", "no", -3),
]

HOT = [
    (r"\b(very |so )?interested\b|\btunavutiwa\b|\bnimevutiwa\b|\bnavutiwa\b", "interested", 3),
    (r"\b(call|phone|ring|reach) (me|us)\b|\bgive (me|us) a (call|ring)\b|\bplease call\b|\bkindly call\b|\bnipigie\b|\btupigie\b", "call me", 3),
    (r"\b(send|share|forward|tuma|nitumie|tutumie)\b.{0,20}\b(details|info|information|price|prices|pricing|quote|quotation|proposal|brochure|catalog(ue)?|demo|maelezo|bei)\b", "send details", 3),
    (r"\bhow much\b|\bwhat('?s| is| are) (the |your )?(price|cost|pricing|charges|fees?)\b|\bprice list\b|\bquotation\b|\bpricing\b|\bbei gani\b|\bbei ni\b|\bgharama\b", "asks price", 3),
    (r"\blet'?s (talk|meet|discuss|chat|connect)\b|\btuongee\b|\btukutane\b", "let's talk", 3),
    (r"\bwhen can (we|you|i)\b.{0,20}\b(meet|come|visit|call|start|see)\b|\bbook (a |the )?(demo|meeting|call)\b|\b(demo|meeting) (please|tomorrow|today)\b", "wants meeting/demo", 3),
    (r"\bnaomba (maelezo|details|bei|demo|info)\b|\bnataka (kujua|kuona|kujaribu|system|mfumo)\b", "naomba maelezo", 3),
    (r"\btell (me|us) more\b|\bmore (details|information|info)\b|\bnijulishe zaidi\b", "tell me more", 2),
    (r"\bsounds (good|great|interesting)\b|\bsounds like (a )?(plan|deal)\b", "sounds good", 2),
    (r"^\s*(yes|yeah|yep|ndio|ndiyo)\b[\s\W]*(please|pls|plz|kindly|tafadhali)?[\s\W]*$", "yes", 3),
    (r"^\s*(sure|ok(ay)?|sawa)\b[\s\W]*(please|pls|plz|kindly|tafadhali)?[\s\W]*$", "ok / sure", 2),
    (r"\byes\b", "yes", 2),
    (r"\bkaribu\b", "karibu", 1),
]

WARM = [
    (r"\b(maybe|perhaps|possibly)\b|\blabda\b", "maybe", 1),
    (r"\b(later|next (week|month)|not now|some other time)\b|\bbaadaye\b|\bwiki ijayo\b|\bmwezi ujao\b", "later", 1),
    (r"\bwho (is|are) (this|you)\b|\bwhat (is|are) (this|you)\b|\bwhat('?s| is) it (about|for)\b|\bwewe ni nani\b|\bni nini hii\b|\bmnauza nini\b", "who is this", 1),
    (r"\bhow does (it|this) work\b|\bhow do (you|they) work\b", "how does it work", 1),
    (r"\b(email|mail|whatsapp) (me|us)\b|\bsend (an )?e-?mail\b|\bwhatsapp\b", "use email/whatsapp", 1),
    (r"\b(talk|speak) to (the |my |our )?(owner|manager|boss|director)\b|\b(owner|manager|boss) (is|will|isn'?t)\b|\bmmiliki\b|\bmeneja\b", "talk to owner", 1),
    (r"\b(i|we)('ll| will| shall) (check|get back|think|consider|revert|ask|let you know)\b|\blet me (check|think|ask|see|consult)\b|\bnitaangalia\b|\bnitakujulisha\b|\btutakujulisha\b|\bnitafikiria\b", "will get back", 1),
    (r"\b(busy|currently busy|nimebanwa)\b", "busy", 1),
]

_GROUPS = [("optout", OPTOUT), ("auto", AUTO), ("cold", COLD), ("hot", HOT), ("warm", WARM)]

_QUOTE_START = re.compile(
    r"^(on .{5,120} wrote:|-{2,}\s*(original message|forwarded message)|from:\s.+|sent from my .+|get outlook for .+)\s*$",
    re.I,
)


def strip_quoted(text: str) -> str:
    """Remove quoted history and signatures so we only grade the new reply."""
    out = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if _QUOTE_START.match(stripped):
            break
        out.append(line)
    return "\n".join(out).strip()


def classify(text: str) -> dict:
    t = strip_quoted(text or "")
    t = t.replace("’", "'").replace("‘", "'").lower()
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return {"grade": "unclear", "score": 0, "reasons": []}

    work = t
    reasons = []
    score = 0
    warm_score = 0
    flags = set()
    for group, patterns in _GROUPS:
        for rx, label, weight in patterns:
            m = re.search(rx, work, flags=re.I)
            if not m:
                continue
            flags.add(group)
            if group == "warm":
                warm_score += weight
            else:
                score += weight
            reasons.append(label)
            # blank out what matched so overlapping patterns don't double count
            work = work[:m.start()] + (" " * (m.end() - m.start())) + work[m.end():]
    # Several soft signals ("maybe", "later", "busy") never add up to a buying signal.
    score += min(warm_score, 1 if score < 0 else 2)

    if "optout" in flags:
        return {"grade": "optout", "score": score, "reasons": reasons}
    if "auto" in flags and score == 0:
        return {"grade": "auto", "score": 0, "reasons": reasons}
    if score >= 3:
        grade = "hot"
    elif score >= 1:
        grade = "warm"
    elif score <= -2:
        grade = "cold"
    else:
        grade = "unclear"
    return {"grade": grade, "score": score, "reasons": reasons}


BOUNCE_SENDERS = re.compile(r"mailer-daemon|postmaster|mail delivery (sub)?system", re.I)
BOUNCE_SUBJECTS = re.compile(
    r"undeliver|delivery (status notification|has failed|failure)|returned mail|failure notice|mail delivery failed|could not be delivered",
    re.I,
)


def looks_like_bounce(sender: str, subject: str) -> bool:
    return bool(BOUNCE_SENDERS.search(sender or "") or BOUNCE_SUBJECTS.search(subject or ""))
