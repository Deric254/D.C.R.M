"""Automatic WhatsApp sending through WhatsApp Web on this PC.

You link WhatsApp once (scan the code with your phone, like linking any computer); the login is kept in a private
browser profile in the data folder. After that a worker opens each lead's chat with the message already typed, presses
send, WAITS until WhatsApp shows the message as sent, and only then records it: so what is logged is what really went out.

Safety, because WhatsApp bans numbers that behave like a bot:
  * the daily limit, the pause between messages (randomised) and the sending hours from Settings apply;
  * a number that is not on WhatsApp is skipped and remembered, never retried;
  * if WhatsApp logs out, or three messages in a row fail, every WhatsApp campaign is paused rather than pushed on;
  * a message that was sent but could not be confirmed is marked failed with a warning, never silently re-sent.

Every page selector lives in SEL below, so if WhatsApp changes its page only that table needs to change.
"""
import os
import random
import re
import sys
import time
from urllib.parse import quote

import campaigns as C
import config
import db
import jobs
from scraper import BROWSER_CHANNELS, StopRequested
from util import now, parent_alive, today_start

BASE = os.environ.get("DERICBI_WA_URL", "https://web.whatsapp.com").rstrip("/")
CONFIRM_SECONDS = 40   # how long to wait for WhatsApp to show a message as sent

SEL = {
    "chat_list": ["#pane-side", "div[aria-label='Chat list']", "div[data-testid='chat-list']"],
    "qr": ["canvas[aria-label*='QR' i]", "div[data-ref]", "div[data-testid='qrcode']"],
    "compose": ["footer div[contenteditable='true']", "div[contenteditable='true'][aria-label*='Type a message' i]",
                "div[contenteditable='true'][data-tab='10']"],
    "outgoing": ["div.message-out", "div[data-id^='true_']"],
    "pending": ["span[data-icon='msg-time']"],
    "invalid": "text=/phone number shared via url is invalid|not on whatsapp|isn.t on whatsapp/i",
    "dialog_ok": ["div[role='button']:has-text('OK')", "button:has-text('OK')"],
}


class LoggedOut(Exception):
    """WhatsApp Web is showing the link code: this PC is no longer linked."""


class SendUnconfirmed(Exception):
    """Send was pressed but WhatsApp never showed the message as sent."""


def is_linked() -> bool:
    return bool(db.get_internal("whatsapp_linked"))


def set_linked(on: bool):
    db.set_internal("whatsapp_linked", bool(on))
    if on:
        db.set_internal("whatsapp_linked_at", now())


def profile_dir():
    return config.DATA_DIR / "whatsapp-profile"


class WhatsAppWeb:
    """Real Playwright driver for WhatsApp Web."""

    def __init__(self, log=print, base=None, headless=False):
        self.log = log
        self.base = (base or BASE).rstrip("/")
        self.headless = headless
        self.should_stop = lambda: False
        self.pw = self.ctx = self.page = None

    # -- browser
    def open(self):
        from playwright.sync_api import sync_playwright
        profile_dir().mkdir(parents=True, exist_ok=True)
        self.pw = sync_playwright().start()
        errors = []
        for channel in BROWSER_CHANNELS:
            try:
                kw = {"channel": channel} if channel else {}
                self.ctx = self.pw.chromium.launch_persistent_context(
                    str(profile_dir()), headless=self.headless, viewport={"width": 1280, "height": 860}, locale="en-KE", **kw)
                self.log(f"Browser: {channel or 'built-in Chromium'}")
                break
            except Exception as e:
                errors.append(f"{channel or 'chromium'}: {str(e).splitlines()[0][:90]}")
        if self.ctx is None:
            raise RuntimeError("No browser found. Install Google Chrome or Microsoft Edge and try again. (" + "; ".join(errors) + ")")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.on("dialog", lambda d: d.accept())   # "leave this page?" when moving to the next chat
        self.page.goto(self.base + "/", wait_until="domcontentloaded", timeout=60000)

    def close(self):
        for obj, fn in ((self.ctx, "close"), (self.pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception:
                pass

    def pause(self, ms: int):
        self.page.wait_for_timeout(ms)

    # -- page state
    def _any(self, key):
        for sel in SEL[key]:
            try:
                loc = self.page.locator(sel)
                if loc.count():
                    return loc
            except Exception:
                pass
        return None

    def state(self) -> str:
        """'ready' (chats are showing), 'qr' (needs linking) or 'loading'."""
        if self._any("chat_list"):
            return "ready"
        if self._any("qr"):
            return "qr"
        return "loading"

    def wait_ready(self, seconds: int = 90) -> str:
        end = time.time() + seconds
        state = "loading"
        while time.time() < end:
            if self.should_stop():
                raise StopRequested()
            state = self.state()
            if state != "loading":
                return state
            self.page.wait_for_timeout(1000)
        return state

    # -- sending
    def send(self, phone: str, text: str) -> str:
        """'sent' once WhatsApp shows the message as sent, or 'not_on_whatsapp'."""
        page = self.page
        digits = re.sub(r"\D", "", phone)
        page.goto(f"{self.base}/send?phone={digits}&text={quote(text)}", wait_until="domcontentloaded", timeout=60000)
        compose = None
        end = time.time() + 60
        while time.time() < end:
            if self.should_stop():
                raise StopRequested()
            if self._any("qr"):
                raise LoggedOut()
            if page.locator(SEL["invalid"]).count():
                ok = self._any("dialog_ok")
                if ok:
                    try:
                        ok.first.click(timeout=2000)
                    except Exception:
                        pass
                return "not_on_whatsapp"
            compose = self._any("compose")
            if compose:
                break
            page.wait_for_timeout(500)
        if compose is None:
            raise SendUnconfirmed("The chat did not open in time.")
        box = compose.first
        typed = " ".join((box.inner_text() or "").split())
        if not typed.startswith(" ".join(text.split())[:15]):   # WhatsApp didn't fill the text in: type it ourselves
            box.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.keyboard.insert_text(text)
        before = self._count("outgoing")
        box.click()
        page.keyboard.press("Enter")
        end = time.time() + CONFIRM_SECONDS
        while time.time() < end:
            if self.should_stop():
                raise StopRequested()
            if self._count("outgoing") > before and not self._count("pending"):
                return "sent"
            page.wait_for_timeout(500)
        raise SendUnconfirmed("Send was pressed but WhatsApp never showed the message as sent.")

    def _count(self, key) -> int:
        loc = self._any(key)
        return loc.count() if loc else 0


# ---------------------------------------------------------------------------- workers
def _pause_sleep(stopping):
    def sleep(seconds):
        end = time.time() + seconds
        while time.time() < end and not stopping():
            time.sleep(min(1.0, max(0.0, end - time.time())))
    return sleep


def run_worker(job_id: int, driver_factory=None, sleep=None) -> str:
    """Send every queued WhatsApp message of the running campaigns, one at a time, within the limits."""
    log = lambda m: jobs.log(job_id, m)
    parent = os.environ.get("DERICBI_PARENT")
    stopping = lambda: jobs.should_stop(job_id) or not parent_alive(parent)
    sleep = sleep or _pause_sleep(stopping)
    driver = (driver_factory or (lambda: WhatsAppWeb(log=log)))()
    driver.should_stop = stopping
    jobs.update(job_id, status="running", started_at=now(), progress="Opening WhatsApp…")
    final, sent, failures = "done", 0, 0
    try:
        driver.open()
        if driver.wait_ready(90) != "ready":
            raise LoggedOut()
        log("WhatsApp is open and linked.")
        while not stopping():
            s = db.get_settings()
            if not C.Sender.in_window(s):
                log("Outside sending hours: stopping for now.")
                break
            with db.db() as conn:
                sent_today = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='out' AND channel='whatsapp' "
                                          "AND status='sent' AND sent_at >= ?", (today_start(),)).fetchone()[0]
            if sent_today >= int(s["whatsapp_daily_cap"]):
                log(f"Daily WhatsApp limit reached ({sent_today}): stopping for today.")
                break
            mid = C.wa_next_id()
            if mid is None:
                break
            msg = C.wa_prepare(mid)
            if not msg:
                continue   # skipped, or couldn't be written: on to the next
            jobs.update(job_id, progress=f"Sending to {msg['name']}")
            try:
                outcome = driver.send(msg["phone"], msg["body"])
            except LoggedOut:
                C.wa_result(mid, "requeue", "WhatsApp logged out before this was sent")
                raise
            except StopRequested:
                C.wa_result(mid, "requeue", "")
                final = "stopped"
                break
            except SendUnconfirmed as e:
                C.wa_result(mid, "failed", f"{e} It may or may not have been delivered: check WhatsApp before sending again.")
                log(f"  ! {msg['name']}: {e}")
                failures += 1
            except Exception as e:
                C.wa_result(mid, "retry", f"{type(e).__name__}: {str(e)[:120]}")
                log(f"  ! {msg['name']}: {type(e).__name__}: {str(e)[:100]}")
                failures += 1
            else:
                C.wa_result(mid, outcome)
                if outcome == "sent":
                    sent += 1
                    jobs.bump(job_id, added=1)
                    log(f"  + sent to {msg['name']}")
                else:
                    log(f"  - {msg['name']} is not on WhatsApp (skipped)")
                failures = 0
            if failures >= 3:
                C.wa_pause_campaigns("WhatsApp stopped after 3 failed messages in a row. Check WhatsApp Web, then resume.")
                log("Stopped: 3 messages in a row failed. WhatsApp campaigns are paused.")
                final = "failed"
                break
            if C.wa_next_id() is None:
                break
            sleep(float(s["whatsapp_delay_sec"]) * random.uniform(0.8, 1.8))
    except LoggedOut:
        set_linked(False)
        C.wa_pause_campaigns("WhatsApp was logged out. Link it again in Settings > WhatsApp, then resume.")
        log("WhatsApp is showing the link code: this PC is no longer linked. WhatsApp campaigns are paused.")
        final = "failed"
    except StopRequested:
        final = "stopped"
    except Exception as e:
        log(f"WhatsApp worker crashed: {type(e).__name__}: {str(e)[:200]}")
        final = "failed"
    finally:
        driver.close()
    jobs.update(job_id, status=final, finished_at=now(), progress=f"{final.capitalize()}: {sent} sent")
    return final


def run_link(job_id: int, driver_factory=None, wait_seconds: int = 300) -> str:
    """Open WhatsApp Web so the owner can scan the code; remember when it is linked."""
    log = lambda m: jobs.log(job_id, m)
    parent = os.environ.get("DERICBI_PARENT")
    stopping = lambda: jobs.should_stop(job_id) or not parent_alive(parent)
    driver = (driver_factory or (lambda: WhatsAppWeb(log=log)))()
    driver.should_stop = stopping
    jobs.update(job_id, status="running", started_at=now(), progress="Opening WhatsApp Web…")
    final, told = "failed", False
    try:
        driver.open()
        end = time.time() + wait_seconds
        while time.time() < end and not stopping():
            state = driver.state()
            if state == "ready":
                set_linked(True)
                log("WhatsApp is linked. You can close this window; campaigns will now send by themselves.")
                final = "done"
                break
            if state == "qr" and not told:
                told = True
                jobs.update(job_id, progress="Scan the code with your phone")
                log("On your phone open WhatsApp > Linked devices > Link a device, then scan the code in the window.")
            driver.pause(1000)
        else:
            final = "stopped" if stopping() else "failed"
            if final == "failed":
                log("The code wasn't scanned in time. Press Connect WhatsApp to try again.")
    except Exception as e:
        log(f"Couldn't open WhatsApp Web: {type(e).__name__}: {str(e)[:200]}")
    finally:
        driver.close()
    jobs.update(job_id, status=final, finished_at=now(), progress=final.capitalize())
    return final


def worker_main(kind: str, job_id: int):
    with db.db() as conn:
        if not jobs.get_job(conn, job_id):
            sys.exit(f"No such job {job_id}")
    if kind == "whatsapp":
        run_worker(job_id)
    elif kind == "whatsapp_link":
        run_link(job_id)
    else:
        sys.exit(f"Unknown worker kind {kind}")
