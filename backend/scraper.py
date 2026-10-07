"""Google Maps scraper worker.

Runs as its own process (see jobs.py) and writes straight into the CRM database
through leads.add_lead(), so duplicates are rejected at the door and nothing
is lost if it crashes: every lead is saved the moment it is read.

How a search works:
  1. open the Maps search page, scroll the results list to load more
  2. collect each listing's link (name + Google place id)
  3. skip listings whose place id is already in the database (no page load)
  4. open every remaining listing directly (not by clicking through the list,
     which is unreliable) and read phone, address, website and category
  5. add_lead() applies the full duplicate rules (phone / email / name+town)

Scraping Google Maps is against Google's terms of service and Google may show
a CAPTCHA or block you. The Google Places API is the supported alternative.
"""
import os
import random
import re
import sys
import time
import traceback
from urllib.parse import quote_plus

import db
import enrich
import jobs
import leads as L
from util import now, parent_alive

FEED = 'div[role="feed"]'
CARD = 'a[href*="/maps/place/"]'


class Blocked(Exception):
    pass


class StopRequested(Exception):
    """Raised inside the driver when the user pressed Stop while it was waiting."""


DEFAULT_SEARCH_URL = os.environ.get("DERICBI_MAPS_SEARCH_URL", "https://www.google.com/maps/search/")
# Prefer the browser already installed on the PC (Edge ships with Windows), so nothing has to be downloaded.
BROWSER_CHANNELS = ["chrome", "msedge", None]   # None = Playwright's own Chromium


class MapsDriver:
    """Real Playwright driver."""

    def __init__(self, headless=False, log=print, search_url=None):
        self.headless = headless
        self.log = log
        self.search_url = search_url or DEFAULT_SEARCH_URL
        self.should_stop = lambda: False
        self.pw = self.browser = self.ctx = self.page = None

    def open(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        errors = []
        for channel in BROWSER_CHANNELS:
            try:
                kw = {"channel": channel} if channel else {}
                self.browser = self.pw.chromium.launch(headless=self.headless, **kw)
                self.log(f"Browser: {channel or 'built-in Chromium'}")
                break
            except Exception as e:
                errors.append(f"{channel or 'chromium'}: {str(e).splitlines()[0][:90]}")
        if self.browser is None:
            raise RuntimeError("No browser found. Install Google Chrome or Microsoft Edge and try again. (" + "; ".join(errors) + ")")
        self.ctx = self.browser.new_context(locale="en-KE", viewport={"width": 1366, "height": 900})
        self.page = self.ctx.new_page()

    def close(self):
        for obj, fn in ((self.browser, "close"), (self.pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception:
                pass

    # -- helpers
    def _consent(self):
        for label in ("Accept all", "I agree", "Reject all"):
            try:
                btn = self.page.get_by_role("button", name=label)
                if btn.count():
                    btn.first.click(timeout=2000)
                    self.page.wait_for_timeout(1200)
                    return
            except Exception:
                pass

    def _check_blocked(self):
        url = self.page.url
        blocked = "/sorry/" in url or "recaptcha" in url
        if not blocked:
            try:
                blocked = self.page.locator("text=unusual traffic").count() > 0
            except Exception:
                blocked = False
        if not blocked:
            return
        if self.headless:
            raise Blocked("Google is showing a CAPTCHA. Run again with the browser window visible and solve it.")
        self.log("Google is asking for a CAPTCHA - solve it in the browser window (waiting up to 3 minutes)…")
        for _ in range(90):
            if self.should_stop():
                raise StopRequested()
            self.page.wait_for_timeout(2000)
            if "/sorry/" not in self.page.url and "recaptcha" not in self.page.url:
                return
        raise Blocked("CAPTCHA wasn't solved in time.")

    # -- API used by the worker
    def search(self, query: str, max_scrolls=12):
        page = self.page
        page.goto(f"{self.search_url}{quote_plus(query)}", wait_until="domcontentloaded", timeout=45000)
        self._consent()
        self._check_blocked()
        try:
            page.wait_for_selector(f"{CARD}, h1", timeout=15000)
        except Exception:
            return []
        if "/maps/place/" in page.url and not page.locator(FEED).count():
            # Google jumped straight to the only match
            return [{"name": (page.locator("h1").first.inner_text() or "").strip(), "href": page.url}]
        if page.locator(FEED).count():
            feed = page.locator(FEED).first
            stale, last = 0, feed.evaluate("el => el.scrollHeight")
            for _ in range(max_scrolls):
                feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                page.wait_for_timeout(1800)
                h = feed.evaluate("el => el.scrollHeight")
                if h == last:
                    stale += 1
                    if stale >= 2:
                        break
                else:
                    stale = 0
                last = h
        out, seen = [], set()
        for a in page.locator(CARD).all():
            try:
                href = a.get_attribute("href") or ""
                name = (a.get_attribute("aria-label") or "").strip()
            except Exception:
                continue
            if not href or not name or href in seen:
                continue
            seen.add(href)
            out.append({"name": name, "href": href})
        return out

    def details(self, item: dict) -> dict:
        page = self.page
        page.goto(item["href"], wait_until="domcontentloaded", timeout=45000)
        self._check_blocked()
        page.wait_for_selector("h1", timeout=10000)
        try:
            page.wait_for_selector('button[data-item-id^="phone:tel:"], button[data-item-id="address"]', timeout=3500)
        except Exception:
            pass  # some listings genuinely have neither
        d = {"name": item["name"], "maps_url": item["href"], "phone": "", "address": "", "website": "", "category": ""}
        try:
            btn = page.locator('button[data-item-id^="phone:tel:"]')
            if btn.count():
                did = btn.first.get_attribute("data-item-id") or ""
                label = btn.first.get_attribute("aria-label") or ""
                d["phone"] = re.sub(r"^\s*Phone:\s*", "", label).strip() or did.split("tel:", 1)[-1]
            btn = page.locator('button[data-item-id="address"]')
            if btn.count():
                d["address"] = re.sub(r"^\s*Address:\s*", "", btn.first.get_attribute("aria-label") or "").strip()
            link = page.locator('a[data-item-id="authority"]')
            if link.count():
                d["website"] = link.first.get_attribute("href") or ""
            cat = page.locator('button[jsaction*="category"]')
            if cat.count():
                d["category"] = (cat.first.inner_text() or "").strip()
        except Exception:
            pass
        return d


# ----------------------------------------------------------------- worker loop
def run_scrape(job_id: int, params: dict, driver_factory=None, sleep=time.sleep) -> str:
    log = lambda m: jobs.log(job_id, m)
    searches = params.get("searches") or []
    cap = int(params.get("max_per_search") or 60)
    headless = bool(params.get("headless", False))
    parent = os.environ.get("DERICBI_PARENT")

    def stopping():
        return jobs.should_stop(job_id) or not parent_alive(parent)

    driver = (driver_factory or (lambda: MapsDriver(headless=headless, log=log)))()
    driver.should_stop = stopping
    jobs.update(job_id, status="running", started_at=now(), progress="Starting browser…")
    final = "done"
    failed_searches = 0
    try:
        driver.open()
        for si, s in enumerate(searches, 1):
            if stopping():
                final = "stopped"
                break
            query = s.get("query") or f"{s.get('category', '')} in {s.get('town', '')}".strip()
            sector, town = s.get("category", ""), s.get("town", "")
            jobs.update(job_id, progress=f"Search {si}/{len(searches)}: {query}")
            log(f"Searching: {query}")
            try:
                items = driver.search(query)
            except StopRequested:
                final = "stopped"
                break
            except Blocked as e:
                log(f"Blocked: {e}")
                jobs.update(job_id, progress=str(e))
                final = "failed"
                break
            except Exception as e:
                log(f"  Search failed: {type(e).__name__}: {str(e)[:120]}")
                jobs.bump(job_id, errors=1)
                failed_searches += 1
                continue
            log(f"  {len(items)} listings found")
            jobs.bump(job_id, found=len(items))
            added_here = 0
            for item in items:
                if stopping():
                    final = "stopped"
                    break
                if added_here >= cap:
                    log(f"  Reached the limit of {cap} new leads for this search")
                    break
                pk = L.extract_place_key(item["href"])
                if pk:
                    with db.db() as conn:
                        known = conn.execute("SELECT 1 FROM leads WHERE place_key=?", (pk,)).fetchone()
                    if known:
                        jobs.bump(job_id, duplicates=1)
                        continue
                try:
                    d = driver.details(item)
                except StopRequested:
                    final = "stopped"
                    break
                except Blocked as e:
                    log(f"Blocked: {e}")
                    jobs.update(job_id, progress=str(e))
                    final = "failed"
                    break
                except Exception as e:
                    log(f"  Couldn't read '{item['name']}': {type(e).__name__}")
                    jobs.bump(job_id, errors=1)
                    continue
                data = {
                    "name": d.get("name") or item["name"], "sector": sector or d.get("category", ""),
                    "town": town, "address": d.get("address", ""), "phone": d.get("phone", ""),
                    "website": d.get("website", ""), "maps_url": d.get("maps_url", item["href"]),
                    "place_key": pk,
                }
                def save():
                    with db.db() as conn:
                        return L.add_lead(conn, data, source="maps")
                res = db.with_retry(save)
                if res["created"]:
                    added_here += 1
                    jobs.bump(job_id, added=1)
                    log(f"  + {data['name']} | {data['phone'] or 'no phone'}")
                else:
                    jobs.bump(job_id, duplicates=1)
                    log(f"  = {data['name']} already saved ({res['reason']})")
                sleep(random.uniform(0.8, 2.0))
            if final in ("failed", "stopped"):
                break

        if final == "done" and params.get("enrich") and not stopping():
            jobs.update(job_id, progress="Finding emails on websites…")
            try:
                enrich.enrich_leads(log, stopping)
            except Exception as e:   # the leads are already saved: a problem finding emails must not turn the run into a failure
                traceback.print_exc()
                log(f"Email search stopped early ({type(e).__name__}: {e}). All the leads above were saved. "
                    "Press 'Look for emails now' to continue.")
    except Exception as e:
        traceback.print_exc()
        log(f"Job crashed: {type(e).__name__}: {e}")
        final = "failed"
    finally:
        try:
            driver.close()
        except Exception:
            pass
    if final == "done" and stopping():
        final = "stopped"
    failure_note = ""
    if final == "done" and searches and failed_searches == len(searches):
        final = "failed"
        failure_note = " - every search failed to load (check your internet connection, or Google may be blocking you)"
        log("Every search failed to load. Check your internet connection; Google may also be blocking the browser.")
    with db.db() as conn:
        j = conn.execute("SELECT added, duplicates, found FROM jobs WHERE id=?", (job_id,)).fetchone()
    if final == "done" and searches and not failure_note and j["found"] == 0:
        failure_note = " - no listings were found. Check the category and town spelling; Google may also have changed its page"
        log("No listings were found for any search. Check the category and town spelling; Google may also have changed its page.")
    msg = {"done": "Finished", "stopped": "Stopped", "failed": "Failed"}[final]
    jobs.update(job_id, status=final, finished_at=now(), progress=f"{msg}: {j['added']} new, {j['duplicates']} duplicates skipped{failure_note}")
    log(f"{msg}. {j['added']} new leads, {j['duplicates']} duplicates skipped.")
    return final


def run_enrich(job_id: int, params: dict) -> str:
    log = lambda m: jobs.log(job_id, m)
    parent = os.environ.get("DERICBI_PARENT")
    stopping = lambda: jobs.should_stop(job_id) or not parent_alive(parent)
    jobs.update(job_id, status="running", started_at=now(), progress="Finding emails…")
    final = "done"
    try:
        stats = enrich.enrich_leads(log, stopping, limit=int(params.get("limit") or 300))
        jobs.update(job_id, found=stats["checked"], added=stats["found"], errors=stats["errors"])
        if stopping():
            final = "stopped"
    except Exception as e:
        traceback.print_exc()
        log(f"Job crashed: {type(e).__name__}: {e}")
        final = "failed"
    jobs.update(job_id, status=final, finished_at=now(), progress=final.capitalize())
    return final


def worker_main(kind: str, job_id: int):
    with db.db() as conn:
        job = jobs.get_job(conn, job_id)
    if not job:
        sys.exit(f"No such job {job_id}")
    if kind == "scrape":
        run_scrape(job_id, job["params"])
    elif kind == "enrich":
        run_enrich(job_id, job["params"])
    else:
        sys.exit(f"Unknown worker kind {kind}")
