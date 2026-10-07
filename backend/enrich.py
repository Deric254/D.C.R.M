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
from util import days_ago, normalize_email

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)*\.[a-zA-Z]{2,}")
SKIP_HOSTS = ("facebook.com", "instagram.com", "twitter.com", "x.com", "linktr.ee", "wa.me", "whatsapp.com",
              "tiktok.com", "youtube.com", "linkedin.com", "google.com", "goo.gl", "business.site")
JUNK = ("example.", "sentry", "wixpress", "domain.com", "email.com", "yourdomain", "your-email", "@2x",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", "u003e", "noreply", "no-reply")
CONTACT_PATHS = ("", "/contact", "/contact-us", "/contacts", "/about", "/about-us")
PREFERRED = ("info", "contact", "sales", "hello", "enquiries", "inquiries", "admin", "office", "support")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DericBI-CRM contact finder)"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def find_emails(website: str, get=None) -> list:
    get = get or (lambda u: requests.get(u, headers=HEADERS, timeout=10))
    url = website if re.match(r"^https?://", website, re.I) else "http://" + website
    host = _host(url)
    if not host or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        return []
    found = []
    for path in CONTACT_PATHS:
        try:
            r = get(urljoin(url, path) if path else url)
        except Exception:
            continue
        if getattr(r, "status_code", 200) >= 400:
            continue
        text = html.unescape((r.text or "")[:500_000])
        text = re.sub(r"(?i)\s*\[\s*at\s*\]\s*|\s*\(\s*at\s*\)\s*", "@", text)
        for m in EMAIL_RE.findall(text):
            e = normalize_email(m)
            if e and not any(j in e for j in JUNK) and e not in found:
                found.append(e)
        if found:
            break

    def rank(e):
        local, domain = e.split("@", 1)
        d = domain.removeprefix("www.")
        same = d == host or d.endswith("." + host) or host.endswith("." + d)
        pref = PREFERRED.index(local) if local in PREFERRED else len(PREFERRED)
        return (0 if same else 1, pref)
    return sorted(found, key=rank)


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
            emails = find_emails(r["website"], get)
        except Exception as e:
            stats["errors"] += 1
            emails = []
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
        try:
            saved = db.with_retry(save)
        except Exception as ex:   # one busy moment must not abandon the other websites
            stats["errors"] += 1
            log(f"  {r['name']}: couldn't save ({type(ex).__name__}), skipped")
            continue
        if saved:
            stats["found"] += 1
            log(f"  {r['name']}: {saved}")
    log(f"Email search done: {stats['found']} found from {stats['checked']} sites.")
    return stats
