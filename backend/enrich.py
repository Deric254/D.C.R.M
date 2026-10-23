"""Find public contact emails on a business's own website.

Google Maps doesn't list email addresses, but many businesses publish one on
their site. We look at the homepage and a few usual contact pages, skip social
networks, and only ever read pages the business made public.
"""
import html
import re
from urllib.parse import urljoin, urlparse

import requests

import db
import leads as L
from util import days_ago, normalize_email, normalize_phone

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)*\.[a-zA-Z]{2,}")
SKIP_HOSTS = ("facebook.com", "instagram.com", "twitter.com", "x.com", "linktr.ee", "wa.me", "whatsapp.com",
              "tiktok.com", "youtube.com", "linkedin.com", "google.com", "goo.gl", "business.site")
JUNK = ("example.", "sentry", "wixpress", "domain.com", "email.com", "yourdomain", "your-email", "@2x",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", "u003e", "noreply", "no-reply")
CONTACT_PATHS = ("", "/contact", "/contact-us", "/contacts", "/contactus", "/about", "/about-us", "/aboutus", "/get-in-touch",
                 "/reach-us", "/enquiries", "/enquiry", "/inquiry", "/help", "/support", "/customer-care")
LINK_WORDS = re.compile(r"contact|about|reach|enquir|inquir|get-in-touch|support|help|care", re.I)
MAX_PAGES = 6
PREFERRED = ("info", "contact", "sales", "hello", "enquiries", "inquiries", "admin", "office", "support")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DericBI-CRM contact finder)"}


def _cfemail(code: str) -> str:
    """Cloudflare hides addresses as data-cfemail="hex": the first byte is the key, the rest is XORed with it."""
    try:
        raw = bytes.fromhex(code)
        return bytes(b ^ raw[0] for b in raw[1:]).decode("utf-8", "ignore")
    except (ValueError, IndexError):
        return ""


def extract_emails(page: str) -> list:
    """Every address a page shows: plain text, mailto: links, Cloudflare-hidden ones, "name [at] site [dot] com" and JSON-LD."""
    text = html.unescape((page or "")[:500_000])
    extra = [_cfemail(m) for m in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', text)]
    extra += re.findall(r"mailto:([^\"'?>\s]+)", text, re.I)
    extra += re.findall(r'"email"\s*:\s*"([^"]+)"', text, re.I)
    text = re.sub(r"(?i)\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text)
    text = re.sub(r"(?i)\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", text)
    text = re.sub(r"(?i)([\w.+-]+)\s+at\s+([\w-]+)\s+dot\s+([\w.]+)", r"\1@\2.\3", text)   # "info at acme dot co dot ke"
    out = []
    for m in EMAIL_RE.findall(text) + [x for e in extra for x in EMAIL_RE.findall(e)]:
        e = normalize_email(m)
        if e and not any(j in e for j in JUNK) and e not in out:
            out.append(e)
    return out


def extract_whatsapp(page: str) -> list:
    """Mobile numbers behind wa.me / api.whatsapp.com links: a business that publishes one answers on WhatsApp."""
    found = []
    for d in re.findall(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp://send\?phone=)\+?(\d{9,15})", html.unescape(page or "")):
        e164, mobile = normalize_phone("+" + d if not d.startswith("0") else d)
        if e164 and e164 not in found:
            found.append(e164)
    return found


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def find_contacts(website: str, get=None) -> dict:
    """{'emails': [...best first], 'whatsapp': [...]} from the business's own site: the homepage, the usual contact pages
    and any link on the homepage that looks like one (at most MAX_PAGES pages)."""
    get = get or (lambda u: requests.get(u, headers=HEADERS, timeout=10))
    url = website if re.match(r"^https?://", website, re.I) else "http://" + website
    host = _host(url)
    if not host or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        return {"emails": [], "whatsapp": []}
    emails, numbers, seen, queue = [], [], set(), [urljoin(url, p) if p else url for p in CONTACT_PATHS]
    pages = 0
    while queue and pages < MAX_PAGES:
        target = queue.pop(0)
        if target in seen:
            continue
        seen.add(target)
        try:
            r = get(target)
        except Exception:
            continue
        if getattr(r, "status_code", 200) >= 400:
            continue
        pages += 1
        body = r.text or ""
        for e in extract_emails(body):
            if e not in emails:
                emails.append(e)
        for n in extract_whatsapp(body):
            if n not in numbers:
                numbers.append(n)
        if target == url:   # the homepage: follow links that look like contact pages
            for href in re.findall(r'href=["\']([^"\'#]+)["\']', body[:300_000], re.I):
                full = urljoin(url, html.unescape(href))
                if _host(full) == host and LINK_WORDS.search(urlparse(full).path) and full not in seen:
                    queue.insert(0, full)
        if any(_same_site(e, host) for e in emails):
            break   # an address on their own domain: good enough, stop reading their site

    def rank(e):
        local = e.split("@", 1)[0]
        pref = PREFERRED.index(local) if local in PREFERRED else len(PREFERRED)
        return (0 if _same_site(e, host) else 1, pref)
    return {"emails": sorted(emails, key=rank), "whatsapp": numbers}


def _same_site(email_addr: str, host: str) -> bool:
    d = email_addr.split("@", 1)[1].removeprefix("www.")
    return d == host or d.endswith("." + host) or host.endswith("." + d)


def find_emails(website: str, get=None) -> list:
    return find_contacts(website, get)["emails"]


def enrich_leads(log, should_stop, limit=300, get=None) -> dict:
    """Fill in missing emails. Returns counters."""
    stats = {"checked": 0, "found": 0, "errors": 0}
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, name, website FROM leads l WHERE l.archived=0 AND l.email IS NULL AND l.website != '' "
            "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.lead_id=l.id AND e.kind='enrich' AND e.ts >= ?) "
            "ORDER BY l.id LIMIT ?", (days_ago(30), limit)).fetchall()
    log(f"Looking for emails on {len(rows)} websites…")
    for r in rows:
        if should_stop():
            log("Stopped.")
            break
        stats["checked"] += 1
        try:
            found = find_contacts(r["website"], get)
            emails, numbers = found["emails"], found["whatsapp"]
        except Exception as e:
            stats["errors"] += 1
            emails, numbers = [], []
            log(f"  {r['name']}: error {type(e).__name__}")
        def save():
            with db.db() as conn:
                for e in emails[:3]:
                    try:
                        L.update_lead(conn, r["id"], {"email": e})
                        return e
                    except L.Conflict:
                        continue
                db.log_event(conn, r["id"], "enrich", "No email found on website")
                return None
        def save_phone():
            with db.db() as conn:
                have = conn.execute("SELECT phone_norm FROM leads WHERE id=?", (r["id"],)).fetchone()
                if have and not have["phone_norm"]:
                    for n in numbers[:2]:
                        try:
                            L.update_lead(conn, r["id"], {"phone": n})
                            db.log_event(conn, r["id"], "enrich", f"WhatsApp number found on website: {n}")
                            return n
                        except L.Conflict:
                            continue
            return None
        try:
            saved = db.with_retry(save)
        except Exception as ex:   # one busy moment must not abandon the other websites
            stats["errors"] += 1
            log(f"  {r['name']}: couldn't save ({type(ex).__name__}), skipped")
            continue
        if numbers:
            try:
                db.with_retry(save_phone)
            except Exception:
                pass
        if saved:
            stats["found"] += 1
            log(f"  {r['name']}: {saved}")
    log(f"Email search done: {stats['found']} found from {stats['checked']} sites.")
    return stats
