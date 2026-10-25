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
import closer
import config
import db
import jobs
from scraper import BROWSER_CHANNELS, StopRequested
from util import now, parent_alive, today_start

BASE = os.environ.get("DERICBI_WA_URL", "https://web.whatsapp.com").rstrip("/")
CONFIRM_SECONDS = 40   # how long to wait for WhatsApp to show a message as sent
SETTLE_MS = 1500       # let the chat's earlier messages appear before looking at them
HEADLESS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.0.0 Safari/537.36")   # a hidden browser must not announce itself as "HeadlessChrome"
WATCH_SEC = 60          # conversation mode ON: the worker stays in its one browser and looks at the chats this often
SCAN_PER_RUN = 8        # unread chats opened in one look
CONVO_STAY = 15 * 60   # while someone is chatting with the assistant, the worker keeps its ONE browser open this long after the last news
CONVO_LOOK = 45        # ... and looks at the chats again every this many seconds
SYNC_PER_RUN = 30      # chats read for replies in one visit
IDLE_LIMIT = 15 * 60   # the worker keeps its ONE browser open while a message is only waiting for its retry time,
                       # unless that is further away than this (then it closes, and the sender starts it again when due)
MAX_IDLE_ROUNDS = 60   # and never idles for more than this many looks, whatever the clock says

SEL = {
    "chat_list": ["#pane-side", "div[aria-label='Chat list']", "div[data-testid='chat-list']"],
    "qr": ["canvas[aria-label*='QR' i]", "div[data-ref]", "div[data-testid='qrcode']"],
    "compose": ["footer div[contenteditable='true']", "div[contenteditable='true'][aria-label*='Type a message' i]",
                "div[contenteditable='true'][data-tab='10']"],
    "outgoing": ["div.message-out", "div[data-id^='true_']"],
    "bubbles": "div.message-in, div.message-out, div[data-id^='true_'], div[data-id^='false_']",
    "chat_rows": "#pane-side [role='listitem'], #pane-side [role='row'], #pane-side div[data-testid='cell-frame-container']",
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
        C.wa_clear_hold()   # linked again: sending may go on at once
        db.set_internal("whatsapp_linked_at", now())


def profile_dir():
    return config.DATA_DIR / "whatsapp-profile"


def _not_installed(err) -> bool:
    """Did the browser fail to start because it is not installed (so the next browser should be tried)?"""
    text = str(err).lower()
    return any(w in text for w in ("executable doesn't exist", "executable does not exist", "is not found", "not installed",
                                   "distribution", "no such file", "cannot find", "could not find"))


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
                if self.headless:
                    kw["user_agent"] = HEADLESS_UA
                self.ctx = self.pw.chromium.launch_persistent_context(
                    str(profile_dir()), headless=self.headless, viewport={"width": 1280, "height": 860}, locale="en-KE", **kw)
                self.log(f"Browser: {channel or 'built-in Chromium'}")
                break
            except Exception as e:
                errors.append(f"{channel or 'chromium'}: {str(e).splitlines()[0][:90]}")
                if not _not_installed(e):
                    # The browser IS there but could not start (its window for this profile is already open, a crash...).
                    # Trying the next browser would only open yet another window, so stop here and say why.
                    raise RuntimeError("The browser could not start for WhatsApp. Close any WhatsApp window this app left "
                                       "open and try again. (" + "; ".join(errors) + ")")
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
    def chat_state(self) -> dict:
        """What the open chat shows right now: who wrote last ('in' = them, 'out' = us, None = nothing yet) and the text
        of our last message. Read from the page itself, which is the one place that knows what really went out."""
        try:
            return self.page.evaluate(
                """(sel) => {
                  const all = Array.from(document.querySelectorAll(sel));
                  const rows = all.filter(el => !all.some(o => o !== el && o.contains(el)));
                  const mine = el => el.classList.contains('message-out') || (el.getAttribute('data-id') || '').startsWith('true_')
                                     || (!el.classList.contains('message-in') && !!el.querySelector('.message-out'));
                  if (!rows.length) return {last: null, lastOut: '', hasOut: false};
                  const outs = rows.filter(mine);
                  return {last: mine(rows[rows.length - 1]) ? 'out' : 'in', hasOut: outs.length > 0,
                          lastOut: outs.length ? (outs[outs.length - 1].innerText || '') : ''};
                }""", SEL["bubbles"]) or {"last": None, "lastOut": "", "hasOut": False}
        except Exception:
            return {"last": None, "lastOut": "", "hasOut": False}

    def read_chat(self, phone: str, inbound_ok: bool = False) -> dict:
        """Open a lead's chat and read what THEY wrote after our first message in it. Read-only: nothing is typed or sent.
        Returns {'replies': [{'id','text'}], 'has_out': bool}, or None when the number is not on WhatsApp."""
        page = self.page
        digits = re.sub(r"\D", "", phone)
        page.goto(f"{self.base}/send?phone={digits}", wait_until="domcontentloaded", timeout=60000)
        end = time.time() + 45
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
                return None
            if self._any("compose"):
                break
            page.wait_for_timeout(500)
        else:
            raise RuntimeError("The chat did not open in time.")
        page.wait_for_timeout(SETTLE_MS)
        return self._read_open_chat(digits, inbound_ok)

    BUBBLES_JS = """(args) => {
              const sel = args[0], inboundOk = args[1];
              const all = Array.from(document.querySelectorAll(sel));
              const rows = all.filter(el => !all.some(o => o !== el && o.contains(el)));
              const mine = el => el.classList.contains('message-out') || (el.getAttribute('data-id') || '').startsWith('true_')
                                 || (!el.classList.contains('message-in') && !!el.querySelector('.message-out'));
              const first = rows.findIndex(mine);
              const idOf = el => el.getAttribute('data-id') || ((el.querySelector('[data-id]') || el).getAttribute('data-id')) || '';
              const textOf = el => { const t = el.querySelector('span.selectable-text, .copyable-text'); return ((t ? t.innerText : el.innerText) || '').trim(); };
              const start = first < 0 ? (inboundOk ? 0 : rows.length) : first + 1;
              const tsOf = el => { const t = el.querySelector('[data-pre-plain-text]'); return t ? (t.getAttribute('data-pre-plain-text') || '') : ''; };
              const replies = rows.slice(start).filter(el => !mine(el)).map(el => ({id: idOf(el), text: textOf(el), ts: tsOf(el)}));
              const thread = rows.slice(-14).map(el => ({id: idOf(el), dir: mine(el) ? 'out' : 'in', text: textOf(el), ts: tsOf(el)}));
              return {hasOut: first >= 0, replies, thread, last: rows.length ? (mine(rows[rows.length - 1]) ? 'out' : 'in') : null};
            }"""

    def _read_open_chat(self, digits: str, inbound_ok: bool = False) -> dict:
        """What the chat that is open on screen holds: what THEY wrote after our first message (all of it, when nothing of ours is in
        it and inbound_ok), and the last bubbles of both sides (to learn what you wrote from your phone). Read-only."""
        import hashlib
        data = self.page.evaluate(self.BUBBLES_JS, [SEL["bubbles"], bool(inbound_ok)]) or {"hasOut": False, "replies": [], "thread": []}

        def clean(rows, tag):
            seen, out = {}, []
            for r in rows:
                text = re.sub(r"\s*\d{1,2}:\d{2}\s*([AaPp]\.?[Mm]\.?)?\s*$", "", r.get("text") or "").strip()
                if not text:
                    continue
                mid = r.get("id") or ""
                if not mid:   # no WhatsApp id on the bubble: a stable one from the number, the words and how often they repeat
                    n = seen.get((tag, text), 0)
                    seen[(tag, text)] = n + 1
                    mid = f"{digits}:{hashlib.sha1(text.encode()).hexdigest()[:12]}:{n}"
                out.append({"id": mid, "text": text, "at": C.parse_wa_time(r.get("ts") or ""), **({"dir": r["dir"]} if "dir" in r else {})})
            return out
        return {"replies": clean(data["replies"], "r"), "has_out": bool(data["hasOut"]), "last": data.get("last"),
                "thread": clean(data.get("thread") or [], "t")}

    def scan_unread(self, limit: int = SCAN_PER_RUN) -> list:
        """The chats WhatsApp shows with unread messages: people who wrote to you first (an enquiry from another source) or who answer
        a chat you started from your phone. Opens each one, which marks it read on your phone. Only chats whose title is a phone
        number (not a saved contact) are returned with a number; saved contacts are matched by name by the caller.
        Returns [{'title','phone','unread','replies','thread','last'}]."""
        page = self.page
        out = []
        for title in [r["title"] for r in self._unread_rows()][:limit]:
            if self.should_stop():
                raise StopRequested()
            # find the chat again by its title (the list may have moved since it was read) and open it
            row = next((r for r in self._unread_rows() if r["title"] == title), None)
            if not row:
                continue
            try:
                page.locator(SEL["chat_rows"]).nth(row["i"]).click(timeout=5000)
                end = time.time() + 20
                while time.time() < end and not self._any("compose"):
                    page.wait_for_timeout(400)
                page.wait_for_timeout(SETTLE_MS)
                digits = re.sub(r"\D", "", title) if re.fullmatch(r"[+\d][\d\s\-().]{7,}", title) else ""
                chat = self._read_open_chat(digits or re.sub(r"\W", "", title).lower(), inbound_ok=True)
            except StopRequested:
                raise
            except Exception:
                continue
            # only the newest bubbles that are theirs and unread count as new; ids make the rest harmless duplicates
            out.append({"title": title, "phone": ("+" + digits) if digits else "", "unread": row["unread"],
                        "replies": chat["replies"][-max(row["unread"], 1):], "thread": chat["thread"], "last": chat["last"], "has_out": chat["has_out"]})
        return out

    def _unread_rows(self) -> list:
        """The chats in the list that show an unread badge: [{'i': position, 'title', 'unread'}]. Goes back to the chat list first if it is not on screen."""
        page = self.page
        if not page.locator(SEL["chat_rows"]).count():
            page.goto(self.base, wait_until="domcontentloaded", timeout=60000)
            end = time.time() + 45
            while time.time() < end and not self._any("chat_list"):
                if self.should_stop():
                    raise StopRequested()
                if self._any("qr"):
                    raise LoggedOut()
                page.wait_for_timeout(500)
            page.wait_for_timeout(SETTLE_MS)
        rows = page.evaluate("""(sel) => Array.from(document.querySelectorAll(sel)).map((el, i) => {
            const t = el.querySelector('span[title]');
            const u = el.querySelector("[aria-label*='unread' i]");
            const m = u ? ((u.getAttribute('aria-label') || '').match(/\\d+/) || ['1'])[0] : '';
            return {i, title: t ? (t.getAttribute('title') || t.innerText || '') : '', unread: m ? parseInt(m, 10) : 0};
          })""", SEL["chat_rows"]) or []
        return [{"i": r["i"], "title": (r["title"] or "").strip(), "unread": r["unread"]} for r in rows if r["unread"] > 0]

    def send(self, phone: str, text: str, followup: bool = False, first_contact: bool = False) -> str:
        """'sent' once WhatsApp shows the message as sent, 'not_on_whatsapp', 'already_sent' (this very message is already
        the last thing we sent in the chat, so it is NOT sent again) or, for a follow-up, 'replied' (they have written
        back since our last message, so the follow-up is not sent).

        Once Send has been pressed the message may be on its way, so from then on NOTHING in here ever asks for it to be
        tried again: any trouble becomes SendUnconfirmed, which the worker records as failed, never as 'try later'."""
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
            raise RuntimeError("The chat did not open in time.")   # nothing was sent yet, so trying again later is safe
        page.wait_for_timeout(SETTLE_MS)
        chat = self.chat_state()
        want = " ".join(text.split())[:40]
        if want and want in " ".join((chat.get("lastOut") or "").split()):
            return "already_sent"   # an earlier try did go out (or it was sent by hand): never send the same thing twice
        if first_contact and chat.get("hasOut"):
            return "already_chatted"   # we have written in this chat before (even from the phone): no "first" message
        if followup and chat.get("last") == "in":
            return "replied"        # they answered (or their business auto-replied) after our message: no nudge
        box = compose.first
        typed = " ".join((box.inner_text() or "").split())
        if not typed.startswith(" ".join(text.split())[:15]):   # WhatsApp didn't fill the text in: type it ourselves
            box.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.keyboard.insert_text(text)
        before = self._count("outgoing")
        box.click()
        try:
            page.keyboard.press("Enter")
            # From here the message may be out. A stop request does not interrupt the wait: it only ends the worker afterwards.
            end = time.time() + CONFIRM_SECONDS
            while time.time() < end:
                if self._count("outgoing") > before and not self._count("pending"):
                    return "sent"
                page.wait_for_timeout(500)
        except Exception as e:
            raise SendUnconfirmed(f"WhatsApp's page changed right after Send was pressed ({type(e).__name__}).")
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


def run_worker(job_id: int, driver_factory=None, sleep=None, watch_rounds=None) -> str:
    """Send every queued WhatsApp message of the running campaigns, one at a time, within the limits."""
    log = lambda m: jobs.log(job_id, m)
    parent = os.environ.get("DERICBI_PARENT")
    stopping = lambda: jobs.should_stop(job_id) or not parent_alive(parent)
    sleep = sleep or _pause_sleep(stopping)
    hidden = bool(db.get_settings().get("whatsapp_hide_window", True))
    driver = (driver_factory or (lambda: WhatsAppWeb(log=log, headless=hidden)))()
    driver.should_stop = stopping
    jobs.update(job_id, status="running", started_at=now(), progress="Opening WhatsApp…")
    final, sent, failures, idle = "done", 0, 0, 0
    talk_until, talk_rounds = time.time() + CONVO_STAY, 0
    watch_left = watch_rounds   # None: watch until conversation mode is switched off (tests give a number)
    try:
        driver.open()
        state = driver.wait_ready(90)
        if state == "qr":
            raise LoggedOut()
        if state != "ready":
            raise RuntimeError("WhatsApp Web did not finish loading (check the internet connection)."
                               + (" If this keeps happening, switch off 'Hide the WhatsApp window' in Settings > WhatsApp." if hidden else ""))
        log("WhatsApp is open and linked.")
        while not stopping():
            s = db.get_settings()
            if not C.Sender.in_window(s):
                log("Outside sending hours: stopping for now.")
                break
            with db.db() as conn:
                sent_today = C.wa_sent_today(conn)   # answers inside a conversation do not count against the campaign limit
            capped = sent_today >= int(s["whatsapp_daily_cap"])
            if capped and not C.wa_next_id(only_single=True):
                log(f"Daily WhatsApp limit reached ({sent_today}): stopping for today.")
                break
            mid = C.wa_next_id(only_single=capped)
            if mid is None:
                # Nothing is due. If something is only waiting for its time (a retry, a lead held back for an hour), stay
                # in this ONE browser instead of closing it and opening a new one later; stop when nothing is left.
                wait = C.wa_wait_seconds()
                if wait is None and s.get("closer_mode", "off") != "off" and hasattr(driver, "scan_unread") and watch_left != 0:
                    # conversation mode is ON: stay in this one (hidden) browser and keep looking at the chats, until switched off
                    if watch_left is not None:
                        watch_left -= 1
                    jobs.update(job_id, progress="Watching for messages")
                    sync_replies(driver, log, stopping, sleep)
                    sleep(WATCH_SEC * random.uniform(0.8, 1.3))
                    continue
                if wait is None and s.get("closer_mode", "off") != "off" and hasattr(driver, "read_chat") \
                        and time.time() < talk_until and talk_rounds < CONVO_STAY // CONVO_LOOK and C.live_conversations():
                    # the assistant is talking with someone: stay in this browser and look at the chats again soon
                    talk_rounds += 1
                    if sync_replies(driver, log, stopping, sleep):
                        talk_until, talk_rounds = time.time() + CONVO_STAY, 0
                    sleep(CONVO_LOOK)
                    continue
                if wait is None or wait > IDLE_LIMIT or idle >= MAX_IDLE_ROUNDS:
                    break
                idle += 1
                sleep(min(max(wait, 1), 30))
                continue
            idle = 0
            msg = C.wa_prepare(mid)
            if not msg:
                continue   # skipped, held back, or couldn't be written: on to the next
            jobs.update(job_id, progress=f"Sending to {msg['name']}")
            try:
                outcome = driver.send(msg["phone"], msg["body"], **({"followup": True} if msg.get("followup") else {}),
                                     **({"first_contact": True} if msg.get("first_contact") else {}))
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
                elif outcome == "already_sent":
                    log(f"  = {msg['name']}: this message is already the last one in the chat, so it was not sent again")
                elif outcome == "already_chatted":
                    log(f"  = {msg['name']}: you already have a chat with them, so no first message was sent")
                elif outcome == "replied":
                    log(f"  - {msg['name']} has written back since our last message: follow-up not sent")
                else:
                    log(f"  - {msg['name']} is not on WhatsApp (skipped)")
                failures = 0
            if failures >= 3:
                C.wa_pause_campaigns("WhatsApp stopped after 3 failed messages in a row. Check WhatsApp Web, then resume.")
                log("Stopped: 3 messages in a row failed. WhatsApp campaigns are paused.")
                final = "failed"
                break
            if C.wa_wait_seconds() is None:
                break
            sleep(float(s["whatsapp_delay_sec"]) * random.uniform(0.8, 1.8))
        if final == "done" and not stopping() and hasattr(driver, "read_chat") and C.Sender.in_window(db.get_settings()):
            sync_replies(driver, log, stopping, sleep)
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
        # Pause instead of leaving the queue to start another browser a minute later, and another, and another.
        C.wa_pause_campaigns(f"WhatsApp could not run ({str(e)[:160]}). Fix that, then resume.")
    finally:
        driver.close()
    jobs.update(job_id, status=final, finished_at=now(), progress=f"{final.capitalize()}: {sent} sent")
    return final


def sync_replies(driver, log, stopping, sleep, limit: int = SYNC_PER_RUN) -> int:
    """In the SAME browser session: (1) with conversation mode on, open the chats WhatsApp shows as unread, which finds people who
    wrote to you first and leads who follow up a chat you started on your phone; (2) open the chats of leads we WhatsApped lately.
    What they wrote is recorded in the campaign that messaged them, and what you wrote from your phone is kept as context.
    Read-only: nothing is typed. Returns how many new replies were recorded."""
    C.wa_sync_begin()
    new = errors = 0
    if hasattr(driver, "scan_unread") and (db.get_settings().get("closer_mode") or "off") != "off":
        try:
            for chat in driver.scan_unread():
                if stopping():
                    break
                lid = C.wa_inbound_lead(chat, log)
                if not lid:
                    continue
                got = C.wa_record_chat(lid, chat)
                C.wa_sync_done(lid)
                if got:
                    new += got
                    log(f"  < new message from {chat.get('title') or chat.get('phone')} on WhatsApp ({got} new)")
                sleep(random.uniform(1.5, 3.0))
        except (LoggedOut, StopRequested):
            raise
        except Exception as e:
            log(f"  ! could not look at the unread chats: {type(e).__name__}: {str(e)[:80]}")
    targets = C.wa_sync_targets(limit)
    for t in targets:
        if stopping():
            break
        try:
            chat = driver.read_chat(t["phone_norm"])
        except (LoggedOut, StopRequested):
            raise
        except Exception as e:
            errors += 1
            log(f"  ! could not read the chat with {t['name']}: {type(e).__name__}: {str(e)[:80]}")
            if errors >= 3:
                break
            continue
        errors = 0
        got = C.wa_record_chat(t["id"], chat) if chat else 0
        C.wa_sync_done(t["id"])
        if got:
            new += got
            log(f"  < {t['name']} replied on WhatsApp ({got} new)")
        sleep(random.uniform(2.0, 4.0))
    if targets:
        log(f"Checked {len(targets)} chat(s) for replies: {new} new.")
    # (the assistant answers from its own thread in the app, so a slow AI never holds this browser)
    return new


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
