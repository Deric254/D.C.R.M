"""AI writing help. Every provider here speaks the same OpenAI-style chat API, so one code path covers
all of them (including free tiers). Providers with a key are tried in order, so when one hits its
free limit the next one answers."""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

TIMEOUT = (5, 25)   # connect, read
BUDGET = 45         # seconds for one request across all providers
# id: (label, base address, default model). The model can be overridden in Settings.
PROVIDERS = {
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "nvidia": ("NVIDIA", "https://integrate.api.nvidia.com/v1", "meta/llama-3.3-70b-instruct"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
    "mistral": ("Mistral", "https://api.mistral.ai/v1", "mistral-small-latest"),
    "custom": ("Your own", "", ""),
}
KEY_SETTINGS = {pid: f"ai_key_{pid}" for pid in PROVIDERS}

GRADES = ("hot", "warm", "cold", "optout")
SYSTEM = (
    "You write short, warm, honest outreach and follow-up messages for a small business in Kenya. "
    "Sound like a real person, not an advertisement: friendly, specific, no hype, no pressure, no made-up claims. "
    "Reply in the language the other person uses (English or Swahili); default to English. "
    "Never invent facts about the person or the offer. Output only the message, with no preface and no quotation marks."
)


class AIError(Exception):
    pass


def _target(pid: str, s: dict):
    label, base, model = PROVIDERS[pid]
    if pid == "custom":
        return label, s["ai_custom_url"].rstrip("/"), s["ai_custom_model"]
    return label, base, (s["ai_model"] if pid == s["ai_provider"] and s["ai_model"] else model)


def configured(s: dict) -> list:
    """Providers that can be used, the chosen one first."""
    ready = [pid for pid in PROVIDERS if (s["ai_custom_url"] if pid == "custom" else s[KEY_SETTINGS[pid]])]
    return sorted(ready, key=lambda pid: pid != s["ai_provider"])


def _reason(r) -> str:
    return {401: "key rejected", 403: "key rejected", 404: "model or address not found",
            429: "free limit reached, try again shortly"}.get(r.status_code, f"error {r.status_code}")


def _call(pid: str, s: dict, system: str, user: str, max_tokens: int, temperature: float) -> str:
    label, base, model = _target(pid, s)
    if not model:
        raise AIError("no model set")
    headers = {"Authorization": f"Bearer {s[KEY_SETTINGS[pid]]}"} if s[KEY_SETTINGS[pid]] else {}
    try:
        r = requests.post(f"{base}/chat/completions", headers=headers, timeout=TIMEOUT, json={
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    except requests.RequestException:
        raise AIError("can't connect")
    if r.status_code != 200:
        raise AIError(_reason(r))
    try:
        text = r.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError("unexpected answer")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if not text:
        raise AIError("empty answer")
    return text


def ask(s: dict, system: str, user: str, max_tokens=1000, temperature=0.7, budget=BUDGET) -> str:
    providers = configured(s)
    if not providers:
        raise AIError("Add an AI key in Settings first. Gemini, Groq and NVIDIA all offer free keys.")
    problems, end = [], time.monotonic() + budget
    for pid in providers:
        if time.monotonic() >= end:
            problems.append("out of time")
            break
        try:
            return _call(pid, s, system, user, max_tokens, temperature)
        except AIError as e:
            problems.append(f"{PROVIDERS[pid][0]}: {e}")
    raise AIError("The AI didn't answer. " + "; ".join(problems))


def check(s: dict) -> list:
    """(label, ok, detail) for every provider that has a key, tried side by side."""
    def one(pid):
        try:
            _call(pid, s, "Reply with the single word: ok", "ok", 200, 0)
            return PROVIDERS[pid][0], True, "works"
        except AIError as e:
            return PROVIDERS[pid][0], False, str(e)
    ids = configured(s)
    with ThreadPoolExecutor(max_workers=max(1, len(ids))) as pool:
        return list(pool.map(one, ids))


def _split_subject(text: str):
    m = re.match(r"\s*subject:\s*(.+?)\s*(?:\n+|$)", text, flags=re.I)
    return (m.group(1), text[m.end():].strip()) if m else ("", text)


def _context(s: dict, extra: str) -> str:
    pitch = s["ai_pitch"].strip() or "not described, so keep the offer general"
    return f"Sender: {s['sender_name']}. What we offer: {pitch}.\n{extra}"


def _thread(lead: dict, last: int) -> str:
    return "\n".join(f"{'Us' if m['direction'] == 'out' else 'Them'}: {(m['body'] or '').strip()[:600]}"
                     for m in lead["messages"][-last:])


def grade_reply(s: dict, text: str):
    """hot / warm / cold / optout for a reply the built-in rules couldn't read, or None if the AI can't say."""
    if not configured(s):
        return None
    try:
        answer = ask(s, "You grade replies from local businesses to a sales message. Answer with exactly one word: "
                        "hot (wants price, details, a call or a demo), warm (some interest or maybe later), "
                        "cold (not interested), optout (asks us to stop, or wrong number), unclear (cannot tell).",
                     text[:1000], max_tokens=400, temperature=0, budget=20)
    except AIError:
        return None
    words = re.findall(r"[a-z]+", answer.lower())
    return words[0] if words and words[0] in GRADES else None


def summarize(s: dict, lead: dict) -> str:
    thread = _thread(lead, 20)
    if not thread:
        raise ValueError("There are no messages with this lead yet.")
    return ask(s, "You summarise sales conversations for a small business owner. Be factual and brief and never invent details.", (
        f"Lead: {lead['name']} ({lead['sector'] or 'business'}, {lead['town'] or 'town unknown'}). Our notes: {lead['notes'].strip() or 'none'}.\n"
        f"Conversation:\n{thread}\n"
        "Give a two-sentence summary, then how interested they seem, then the best next step. Plain text, no markdown, under 90 words."),
        max_tokens=700, temperature=0.3)


def draft_template(s: dict, channel: str, brief: str) -> dict:
    """One message for many recipients, using {name} {town} {sector} {sender} placeholders."""
    if channel not in ("sms", "email"):
        raise ValueError("Channel must be email or sms")
    form = ("A text message, plain text, under 300 characters." if channel == "sms" else
            "An email: first line 'Subject: ...', then a blank line, then a body under 120 words.")
    text = ask(s, SYSTEM, _context(s, (
        f"Extra guidance: {brief.strip() or 'none'}.\n"
        f"Write one message that will be sent to many local businesses. {form} "
        "Use the placeholders {name}, {town}, {sector} and {sender} in curly braces where natural, and no other "
        "placeholders. Do not add an opt-out line; the app adds it.")))
    subject, body = _split_subject(text) if channel == "email" else ("", text)
    return {"subject": subject, "body": body}


def draft_for_lead(s: dict, lead: dict, channel: str, notes: str) -> dict:
    """A first message, or a reply that follows the conversation so far, for one lead."""
    if channel not in ("sms", "email"):
        raise ValueError("Channel must be email or sms")
    if lead["do_not_contact"]:
        raise ValueError("This lead asked not to be contacted.")
    thread = _thread(lead, 10)
    form = "A text message under 300 characters." if channel == "sms" else \
        "An email: first line 'Subject: ...', then a blank line, then a body under 120 words."
    task = ("Reply to their latest message: answer what they said, then offer one clear next step."
            if thread else "Write a friendly first message introducing us.")
    text = ask(s, SYSTEM, _context(s, (
        f"Lead: {lead['name']} ({lead['sector'] or 'business'}, {lead['town'] or 'town unknown'}). "
        f"Our notes: {lead['notes'].strip() or 'none'}.\n"
        f"Conversation so far:\n{thread or '(none yet)'}\n"
        f"Our rough notes or instructions for this message: {notes.strip() or 'none'}.\n"
        f"{task} {form} Write the final text with real names, no placeholders, and sign off as {s['sender_name']}. "
        "Do not add an opt-out line; the app adds it.")))
    subject, body = _split_subject(text) if channel == "email" else ("", text)
    return {"subject": subject, "body": body}


# ------------------------------------------------------------ search planning
PLAN_SYSTEM = (
    "You help a small business owner in Kenya decide which Google Maps searches will find good sales leads. "
    "A search is a business type plus a town, for example category 'Pharmacy' and town 'Meru'. "
    "Answer with ONLY a JSON object, no markdown and no text around it, shaped exactly like: "
    '{"reply": "...", "searches": [{"category": "...", "town": "..."}]}. '
    "reply: one or two friendly sentences (under 60 words) saying what you chose and why; if the goal is too vague "
    "to choose, ask ONE short question and return an empty searches list. "
    "category: a business type exactly as people type it into Google Maps, singular, 1 to 3 words. "
    "town: a real Kenyan town or city. "
    "Never list individual business names, never invent contact details, and return at most 40 searches. "
    "Match the owner's wording: only use towns they named or clearly implied (a county means its main towns)."
)
MAX_PLAN = 40


def _clean_search(item):
    if not isinstance(item, dict):
        return None
    cat, town = (re.sub(r"\s+", " ", str(item.get(k) or "")).strip(" .,;:-") for k in ("category", "town"))
    if not (2 <= len(cat) <= 40 and 2 <= len(town) <= 40):
        return None
    return cat[:1].upper() + cat[1:], town[:1].upper() + town[1:]


def parse_plan(text: str) -> dict:
    """Pull {reply, searches} out of the model's answer, tolerating code fences and stray text."""
    raw = re.sub(r"```(?:json)?", "", text or "")
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start:end + 1]) if 0 <= start < end else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AIError("The AI answered in a way I couldn't read. Please try again.")
    seen, searches = set(), []
    for item in data.get("searches") if isinstance(data.get("searches"), list) else []:
        pair = _clean_search(item)
        if pair and tuple(x.lower() for x in pair) not in seen:
            seen.add(tuple(x.lower() for x in pair))
            searches.append({"category": pair[0], "town": pair[1]})
    return {"reply": re.sub(r"\s+", " ", str(data.get("reply") or "")).strip()[:600], "searches": searches[:MAX_PLAN]}


def plan_searches(s: dict, goal: str, history: list) -> dict:
    """Turn what the owner says (over several turns if they like) into searches the finder can run."""
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("Tell the AI what kind of customers you want")
    turns = []
    for m in (history or [])[-10:]:
        if isinstance(m, dict) and str(m.get("text") or "").strip():
            who = "Owner" if m.get("role") == "user" else "You"
            turns.append(f"{who}: {str(m['text']).strip()[:600]}")
    pitch = s["ai_pitch"].strip() or "not described"
    prompt = (f"What the owner sells: {pitch}.\n"
              + (f"Conversation so far:\n" + "\n".join(turns) + "\n" if turns else "")
              + f"Owner's latest message: {goal[:800]}\n"
              "Choose the searches now (or ask your one question).")
    return parse_plan(ask(s, PLAN_SYSTEM, prompt, max_tokens=1500, temperature=0.3, budget=40))

