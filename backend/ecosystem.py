"""The ecosystem: how this app knows about your other systems without being tied to any of them.

Every system of yours publishes a small "capability card" (JSON) that says who it is and what it offers or accepts. This app keeps a list of
addresses, reads each card when asked, and shows what it found. Nothing about any other system is written into this code: a new system is
just a new address, and a system that is missing or down only shows as "not reachable". Nothing is sent to those systems from here.

Contract "dericbi.ecosystem/1" (every field except contract, id and name is optional; unknown fields are kept and ignored):
  { "contract": "dericbi.ecosystem/1", "id": "bre", "name": "Business Reality Engine", "version": "1.0",
    "role": "what it is, in a few words", "offers": ["capability", ...], "accepts": ["capability", ...], "health": "/api/health/" }
Capabilities are plain strings such as "events.ingest" or "lead.capture"; any system may invent new ones.
"""
import json
import re
from datetime import datetime
from urllib.parse import quote, urlsplit

import requests

import db
from util import now

CONTRACT = "dericbi.ecosystem/1"
CARD_PATHS = ("/.well-known/dericbi.json", "/dericbi.json", "/api/ecosystem/card")   # tried in order
MAX_BYTES = 64 * 1024


def my_card() -> dict:
    s = db.get_settings()
    return {"contract": CONTRACT, "id": "crm", "name": (s.get("sender_name") or "DericBI") + " CRM", "role": "finds businesses, reaches them, tracks replies and leads",
            "offers": ["leads.list", "replies.list"], "accepts": ["lead.capture"]}


def clean_url(raw: str) -> str:
    u = (raw or "").strip().rstrip("/")
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        raise ValueError("Give the address of the system, starting with https:// (no password in it).")
    return u


def list_links() -> list:
    with db.db() as conn:
        return [dict(r) for r in conn.execute("SELECT id, name, url, added_at FROM ecosystem_links ORDER BY id")]


def add_link(name: str, url: str) -> dict:
    url = clean_url(url)
    name = (name or "").strip()[:60] or urlsplit(url).netloc
    with db.db() as conn:
        if conn.execute("SELECT 1 FROM ecosystem_links WHERE url=?", (url,)).fetchone():
            raise ValueError("That system is already in the list.")
        if conn.execute("SELECT COUNT(*) FROM ecosystem_links").fetchone()[0] >= 30:
            raise ValueError("That is plenty of systems (30). Remove one first.")
        cur = conn.execute("INSERT INTO ecosystem_links (name, url, added_at) VALUES (?,?,?)", (name, url, now()))
        return {"id": cur.lastrowid, "name": name, "url": url}


def remove_link(link_id: int) -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM ecosystem_links WHERE id=?", (link_id,))


def read_card(url: str, get=None) -> dict:
    """Fetch and validate one system's card. Always returns a dict with ok True/False and a plain-words 'say'; never raises."""
    get = get or (lambda u: requests.get(u, timeout=8, allow_redirects=False, headers={"Accept": "application/json"}, stream=True))
    try:
        base = clean_url(url)
    except ValueError as e:
        return {"ok": False, "stage": "address", "say": str(e)}
    saw_reply = False
    for path in CARD_PATHS:
        try:
            r = get(base + path)
        except Exception as e:   # noqa: BLE001  (any network trouble is reported in words)
            return {"ok": False, "stage": "reach", "say": "Could not reach it (" + type(e).__name__ + "). It may be off, asleep, or the address is wrong."}
        saw_reply = True
        code = getattr(r, "status_code", 0)
        if code != 200:
            continue
        try:
            raw = _body(r)
            if len(raw) > MAX_BYTES:
                return {"ok": False, "stage": "card", "say": "Its card is too large to be a real one."}
            card = json.loads(raw)
        except Exception:   # noqa: BLE001
            continue
        if not isinstance(card, dict) or card.get("contract", "").split("/")[0] != CONTRACT.split("/")[0] or not card.get("id") or not card.get("name"):
            continue
        if str(card.get("contract")) != CONTRACT:
            return {"ok": True, "stage": "done", "newer": True, "card": _tidy(card), "say": f"Reached {card.get('name')}. It speaks a different version of the format ({card.get('contract')}), so only the basics are shown."}
        return {"ok": True, "stage": "done", "card": _tidy(card), "say": f"{card['name']} is reachable."}
    return {"ok": False, "stage": "card", "say": ("It answers, but has no capability card yet. Add the card file to that system (see the ecosystem notes) and check again." if saw_reply else "No answer.")}


def _body(r) -> bytes:
    if hasattr(r, "iter_content"):
        out = b""
        for chunk in r.iter_content(8192):
            out += chunk
            if len(out) > MAX_BYTES:
                break
        return out
    return r.content


def _tidy(card: dict) -> dict:
    def strs(v, n=40):
        return [str(x)[:60] for x in v[:n]] if isinstance(v, list) else []
    return {"id": str(card.get("id"))[:40], "name": str(card.get("name"))[:80], "version": str(card.get("version") or "")[:20], "role": str(card.get("role") or "")[:160],
            "offers": strs(card.get("offers")), "accepts": strs(card.get("accepts")), "handoffs": _handoffs(card.get("handoffs")), "feeds": _handoffs(card.get("feeds"))}


def _handoffs(v) -> dict:
    """Links or feeds a system offers ("what" -> a path on that system; a link may hold {name} and {ref}). Only plain relative paths are kept, so a card can
    never point the owner's browser, or this app, at another website."""
    out = {}
    if isinstance(v, dict):
        for k, p in list(v.items())[:10]:
            if isinstance(k, str) and isinstance(p, str) and p.startswith("/") and not p.startswith("//") and "\\" not in p and len(p) <= 200 and ":" not in p.split("?")[0]:
                out[k[:40]] = p
    return out


def overview(get=None) -> dict:
    links = list_links()
    return {"me": my_card(), "systems": [dict(l, **read_card(l["url"], get)) for l in links], "alert_feed": db.get_internal("eco_pull_last")}


FEED = "advisor.alerts"
FEED_MAX_BYTES = 256 * 1024


def pull_alerts(key: str, get=None, card_get=None) -> dict:
    """Ask each linked system that offers the "advisor.alerts" feed for serious alerts from the businesses that chose to share them, and turn each
    new one into a follow-up on the matching lead: due today, with the headline in the lead's history. The key is the one the owner set up on that
    system. Only a headline and a lead reference arrive; nothing is sent from here except the key. Returns a summary in plain words; never raises."""
    get = get or (lambda u, h: requests.get(u, timeout=10, allow_redirects=False, headers=h, stream=True))
    out = {"at": now(), "ok": True, "new": 0, "say": ""}
    if not key:
        return out
    problems, reached = [], 0
    for link in list_links():
        card = read_card(link["url"], card_get)
        path = ((card.get("card") or {}).get("feeds") or {}).get(FEED) if card.get("ok") else None
        if not path:
            continue
        cursor_key = "eco_cursor:" + link["url"]
        since = db.get_internal(cursor_key)
        url = link["url"] + path + ("?since=" + quote(since, safe="") if since else "")
        try:
            r = get(url, {"X-Advisor-Key": key, "Accept": "application/json"})
            code = getattr(r, "status_code", 0)
            if code != 200:
                problems.append(f"{link['name']} did not accept the key" if code in (401, 403, 404) else f"{link['name']} answered {code}")
                continue
            raw = _body_limited(r)
            alerts = json.loads(raw).get("alerts")
            if not isinstance(alerts, list):
                raise ValueError("no list")
        except Exception:   # noqa: BLE001  (any trouble is reported in words)
            problems.append(f"Could not read the alerts from {link['name']}")
            continue
        reached += 1
        newest = since or ""
        for a in alerts[:100]:
            if not isinstance(a, dict):
                continue
            at, aid, ref = str(a.get("at") or "")[:40], str(a.get("id") or "")[:64], str(a.get("ref") or "")
            if at > newest:
                newest = at
            if aid and re.fullmatch(r"[0-9a-f]{32}", ref) and _follow_up(link, aid, ref, str(a.get("severity") or "")[:20], str(a.get("headline") or "")[:200]):
                out["new"] += 1
        if newest and newest != since:
            db.set_internal(cursor_key, newest)
    out["ok"] = not problems
    out["say"] = "; ".join(problems) if problems else (f"{out['new']} new follow-up(s)" if out["new"] else ("Checked, nothing new" if reached else "No linked system offers an alert feed"))
    return out


def _body_limited(r) -> bytes:
    raw = b""
    for chunk in r.iter_content(8192) if hasattr(r, "iter_content") else [r.content]:
        raw += chunk
        if len(raw) > FEED_MAX_BYTES:
            raise ValueError("too large")
    return raw


def _follow_up(link: dict, alert_id: str, lead_uid: str, severity: str, headline: str) -> bool:
    """One alert -> one follow-up, once. An alert for a lead this app does not have is skipped (and remembered, so it is not looked at again)."""
    with db.db() as conn:
        if conn.execute("INSERT OR IGNORE INTO ecosystem_alerts (system_url, alert_id, seen_at) VALUES (?,?,?)", (link["url"], alert_id, now())).rowcount == 0:
            return False
        lead = conn.execute("SELECT id, next_followup FROM leads WHERE uid=?", (lead_uid,)).fetchone()
        if not lead:
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        planned = lead["next_followup"] or ""
        # A follow-up already due today or overdue stays as it is; a later one is brought forward, and the old date is written down so nothing is lost.
        moved = f" (your follow-up of {planned} was brought forward)" if planned > today else ""
        if not planned or planned > today:
            conn.execute("UPDATE leads SET next_followup=? WHERE id=?", (today, lead["id"]))
        db.log_event(conn, lead["id"], "alert", f"{link['name']}: {severity} alert, \"{headline}\". Time to call them.{moved}")
        return True
