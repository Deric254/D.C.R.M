"""Ready-made messages that work without any AI key.

Each message names one pain point that this kind of business really has, opens a curiosity gap (a result is
hinted at, not given away), and asks for one easy next step. Nothing here claims a number, a testimonial or a
guarantee. Placeholders: {name} {town} {sector} {pain} {website} {whatsapp} {email} {sender}.
Standard library only, so any module can import it.
"""
import re

# (keywords found in the lead's sector, group)
GROUPS = (
    (("pharm", "chemist", "drug", "clinic", "hospital", "dental", "medical", "laborator", "optic"), "health"),
    (("agrovet", "agro", "farm", "seed", "feed", "veterin"), "agri"),
    (("hardware", "building", "construction", "timber", "cement", "plumb", "electrical"), "hardware"),
    (("salon", "barber", "spa", "beauty", "cosmetic", "nail"), "beauty"),
    (("restaurant", "cafe", "caf\u00e9", "hotel", "lodge", "pub", "eatery", "butcher", "bakery", "catering", "fish", "dairy"), "food"),
    (("school", "college", "academy", "nursery", "tuition"), "school"),
    (("garage", "motor", "spare", "tyre", "auto", "car wash"), "auto"),
    (("supermarket", "shop", "store", "mini", "wholesale", "retail", "kiosk", "boutique", "mart", "bookshop",
      "electronics", "furniture", "cereal", "courier", "cyber"), "retail"),
)

# What the owner is quietly losing, as a noun phrase that fits "... costs you money".
PAIN = {
    "health": "expired or missing medicines",
    "agri": "unsold stock and stock-outs",
    "hardware": "slow stock and unpaid credit",
    "beauty": "no-shows and lost regulars",
    "food": "food waste and quiet hours",
    "school": "fee arrears",
    "auto": "lost repeat clients",
    "retail": "stock running out unseen",
    "general": "hidden stock and cash leaks",
}

# First messages. SMS must fit 160 characters together with the opt-out line, so these stay very short.
SMS = {
    "health": (
        "{name}: how much do expired or missing drugs cost you monthly? I can show you. 2-min look: {website} - {first}",
        "{name}: want to know which drugs will expire before they sell? WhatsApp {whatsapp} - {first}"),
    "agri": (
        "{name}: which stock sits unsold while fast sellers run out? I can show you. {website} - {first}",
        "{name}: ever stock up and still run short on what sells? Let me show you why. WhatsApp {whatsapp} - {first}"),
    "hardware": (
        "{name}: unpaid credit and slow stock quietly eat profit. I can show you where. {website} - {first}",
        "{name}: do you know which items tie up your cash? 2 mins to show you. WhatsApp {whatsapp} - {first}"),
    "beauty": (
        "{name}: no-shows and lost regulars cost real money. I can show you who. {website} - {first}",
        "{name}: which clients haven't been back in 60 days? I can show you. WhatsApp {whatsapp} - {first}"),
    "food": (
        "{name}: how much food is wasted vs sold daily? I can show you. 2-min look: {website} - {first}",
        "{name}: do you know your real peak hours and best sellers? WhatsApp {whatsapp} - {first}"),
    "school": (
        "{name}: can you see today who owes fees and how much? I can show you. {website} - {first}",
        "{name}: fee arrears are easy to lose track of. I have a simple fix. WhatsApp {whatsapp} - {first}"),
    "auto": (
        "{name}: which repeat clients haven't been back in 60 days? I can show you. {website} - {first}",
        "{name}: do you know which parts and jobs earn you most? WhatsApp {whatsapp} - {first}"),
    "retail": (
        "{name}: can you name the 10 items making most of your profit? I can show you. {website} - {first}",
        "{name}: do you know what runs out before you reorder? I have a simple fix. WhatsApp {whatsapp} - {first}"),
    "general": (
        "{name}: are you guessing which products earn most? I can show you the real answer. {website} - {first}",
        "{name}: what would you change if you could see where money leaks? WhatsApp {whatsapp} - {first}"),
}

# WhatsApp: a little more room, still short enough to be read in one glance.
WHATSAPP = {
    g: (f"Hi {{name}}, {{sender}} here, a data analyst in {{town}}. Quick question: how much money goes to {PAIN[g]} each month? "
        "I help local businesses see this from their own records, and I'd like to show you what I found for businesses like yours. "
        "Curious? Reply YES and I'll send it. Or see how it works: {website}")
    for g in PAIN
}

# Emails: (subject, body). The opt-out footer is added when sending.
EMAIL = {
    g: (f"A quick question about {PAIN[g]}",
        f"Hi {{name}},\n\nHow much money goes to {PAIN[g]} at {{name}} in a month? Most owners can only guess, "
        "because the answer is hiding in their own sales and stock records.\n\n"
        "I'm {sender}, a data analyst. I turn those records into a simple picture of what is selling, what is not, and "
        "where money is slipping away. I'd like to show you what that looks like for a business like yours.\n\n"
        "Worth a 10-minute look? Just reply YES, or reach me directly:\n"
        "Website: {website}\nWhatsApp: {whatsapp}\nEmail: {email}\n\n{sender}")
    for g in PAIN
}

# Follow-ups, in the order they are used when nothing is replied: a nudge, something useful, a gracious last note.
FOLLOWUP_STYLES = ("nudge", "tip", "last")
FOLLOWUP_LABELS = {"nudge": "Gentle nudge", "tip": "Something useful", "last": "Last note"}

FOLLOWUP_SMS = {
    "nudge": "{name}, {first} here. Did my note on {pain} reach you? Reply YES for details.",
    "tip": "{name}: most owners spot {pain} within a week of checking their records. Want me to check yours? {first}",
    "last": "Last note, {name}. If {pain} isn't a priority now, no problem. WhatsApp {whatsapp} if it is. {first}",
}
FOLLOWUP_WHATSAPP = {
    "nudge": "Hi {name}, {sender} again. I wanted to make sure my earlier message about {pain} reached you. "
             "Reply YES and I'll send the details, or see how it works: {website}",
    "tip": "Hi {name}, one thing I keep seeing with businesses like yours: {pain} is rarely visible until someone looks at the "
           "records properly. I can do that quickly for you. Want me to?",
    "last": "Hi {name}, I'll stop here so I don't crowd your phone. If {pain} ever becomes a priority, message me any time "
            "on this number or visit {website}. Wishing {name} a strong season. - {sender}",
}
FOLLOWUP_EMAIL = {
    "nudge": ("Did my note reach you?",
              "Hi {name},\n\nI sent a note about {pain} and wanted to be sure it reached you. If it's useful, reply YES and I'll "
              "send a short example.\n\nWebsite: {website}\nWhatsApp: {whatsapp}\n\n{sender}"),
    "tip": ("One thing I keep noticing",
            "Hi {name},\n\nBusinesses like yours rarely see {pain} until someone looks at their own records properly. It usually "
            "takes minutes, not weeks. Want me to check yours and tell you what I find?\n\nWhatsApp: {whatsapp}\n\n{sender}"),
    "last": ("Closing the loop",
             "Hi {name},\n\nI'll stop here so I don't fill your inbox. If {pain} becomes a priority, you can reach me any time:\n"
             "Website: {website}\nWhatsApp: {whatsapp}\nEmail: {email}\n\nAll the best,\n{sender}"),
}


def group_for(sector: str) -> str:
    low = (sector or "").lower()
    for keys, group in GROUPS:
        if any(k in low for k in keys):
            return group
    return "general"


def pain_for(sector: str) -> str:
    return PAIN[group_for(sector)]


def first_message(channel: str, sector: str, variant: int = 0):
    """(subject, body) with placeholders still in, for the lead's kind of business."""
    g = group_for(sector)
    if channel == "sms":
        options = SMS[g]
        return "", options[variant % len(options)]
    if channel == "whatsapp":
        return "", WHATSAPP[g]
    return EMAIL[g]


def followup_message(channel: str, step: int):
    """(subject, body) of the follow-up for step 1, 2, 3 ... (the last style repeats)."""
    style = FOLLOWUP_STYLES[min(max(step, 1), len(FOLLOWUP_STYLES)) - 1]
    if channel == "sms":
        return "", FOLLOWUP_SMS[style]
    if channel == "whatsapp":
        return "", FOLLOWUP_WHATSAPP[style]
    return FOLLOWUP_EMAIL[style]


def shorten_name(name: str, room: int) -> str:
    """The business name cut down to fit 'room' characters: common filler words go first, then whole words from the end."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if len(name) <= room:
        return name
    words = [w for w in name.split(" ") if w.lower() not in {"ltd", "limited", "and", "&", "co", "company", "the", "enterprises",
                                                              "enterprise", "supplies", "centre", "center"}] or name.split(" ")
    while len(" ".join(words)) > room and len(words) > 1:
        words.pop()
    short = " ".join(words)
    return short if len(short) <= room else short[:max(room, 1)].rstrip()
