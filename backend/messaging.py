"""Sending (SMTP email, SMS through your own Android phone) and receiving (IMAP replies).

SendError.kind tells the caller what to do:
  offline    the internet, the mail server or the phone can't be reached -> put the message back, rest, retry by itself
  transient  the server answered with a temporary error  -> retry later
  permanent  this recipient/message can't be delivered -> mark failed
  pause      account-level problem (bad login, no SMS balance, not configured)
             -> pause the campaign so the queue isn't burned
"""
import email
import email.policy
import email.utils
import imaplib
import re
import smtplib
import queue
import socket
import ssl
import threading
import time
import unicodedata
from email.message import EmailMessage
from urllib.parse import urlparse

import requests

import db
import grading
import leads as leadsmod
import library
from util import normalize_email

SMS_LIMIT = 160   # one text; anything longer is split and billed as several
SMTP_TIMEOUT = 60   # seconds per step once connected (log in, send): a slow line needs more than the default patience
CONNECT_TIMEOUT = 40   # seconds one way of connecting (connect + secure) may take before it is given up on
CONNECT_STAGGER = 5    # seconds the preferred way gets alone; after that the other standard way is tried alongside it


class SendError(Exception):
    def __init__(self, message, kind="transient"):
        super().__init__(message)
        self.kind = kind


# ------------------------------------------------------------------ cleaning
_INVISIBLE = "\u00a0\u200b\u200c\u200d\u2060\ufeff"


def clean_secret(v) -> str:
    """A password or key with every space removed. Gmail shows app passwords in groups ('abcd efgh ...') and copying
    them brings along non-breaking spaces, which the mail login cannot even encode."""
    return re.sub(r"[\s" + _INVISIBLE + r"]+", "", str(v or ""))


def clean_text(v) -> str:
    return re.sub("[\u200b\u200c\u200d\u2060\ufeff]", "", str(v or "")).replace("\u00a0", " ").strip()


def mail_login(s: dict):
    """(username, password) for sending. The username is the Gmail/Outlook address, so when it was left blank the
    'Send from' address is used instead of silently skipping the login."""
    return clean_text(s.get("smtp_user")) or clean_text(s.get("from_email")), clean_secret(s.get("smtp_pass"))


def inbox_login(s: dict):
    """(username, password) for reading replies; both fall back to the sending login (same Gmail account)."""
    user = clean_text(s.get("imap_user")) or clean_text(s.get("smtp_user")) or clean_text(s.get("from_email"))
    return user, clean_secret(s.get("imap_pass")) or clean_secret(s.get("smtp_pass"))


# ------------------------------------------------------------------ rendering
def _first_name(full: str) -> str:
    return (clean_text(full).split(" ") or [""])[0]


def render_template(template: str, lead, settings: dict, channel: str = "", **override) -> str:
    site = clean_text(settings.get("contact_website"))
    whatsapp = clean_text(settings.get("contact_whatsapp"))
    if channel == "sms":   # every character counts in a text
        site = re.sub(r"^https?://(www\.)?", "", site).rstrip("/")
        whatsapp = whatsapp.replace(" ", "")
    values = {
        "name": lead["name"],
        "town": lead["town"],
        "sector": lead["sector"],
        "sender": settings.get("sender_name", ""),
        "first": _first_name(settings.get("sender_name", "")),
        "pain": library.pain_for(lead["sector"]),
        "website": site,
        "email": clean_text(settings.get("contact_email")),
        "whatsapp": whatsapp,
    }
    values.update(override)
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1).lower(), m.group(0))), template or "")


def unknown_placeholders(*templates) -> list:
    allowed = {"name", "town", "sector", "sender", "first", "pain", "website", "email", "whatsapp"}
    found = set()
    for t in templates:
        found |= {p.lower() for p in re.findall(r"\{(\w+)\}", t or "")}
    return sorted(found - allowed)


def final_body(channel: str, body: str, settings: dict) -> str:
    body = (body or "").rstrip()
    if channel == "whatsapp" or not settings.get("append_optout", True):
        return body
    if channel == "sms":
        return body + (settings.get("optout_suffix_sms") or "")
    footer = (settings.get("optout_footer_email") or "").strip()
    sig = (settings.get("sender_name") or "").strip()
    tail = "\n\n--\n" + (sig + "\n" if sig else "") + footer
    return body + tail


_SMS_MAP = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u2026": "...",
            "\u00a0": " ", "\u20ac": "EUR", "\u2022": "-"}


def sms_clean(text: str) -> str:
    """Plain letters only. One curly quote or emoji turns a 160-character text into a 70-character one."""
    t = "".join(_SMS_MAP.get(c, c) for c in str(text or ""))
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[ \t]+", " ", t).strip()


def sms_segments(text: str) -> int:
    n = len(text)
    if n == 0:
        return 0
    gsm = all(ord(c) < 128 for c in text)
    single, multi = (160, 153) if gsm else (70, 67)
    return 1 if n <= single else -(-n // multi)


def sms_total(body: str, s: dict) -> int:
    """Length of the text exactly as it will be sent, opt-out line included."""
    return len(final_body("sms", body, s))


def fit_sms(templates, lead, s: dict):
    """The text for this lead as ONE message of at most 160 characters including the opt-out line, or None.
    Tries each template in turn; for each, a long business name is shortened rather than the message dropped."""
    if isinstance(templates, str):
        templates = [templates]
    for t in templates:
        full = sms_clean(render_template(t, lead, s, "sms"))
        if sms_total(full, s) <= SMS_LIMIT:
            return full
        over = sms_total(full, s) - SMS_LIMIT
        name = clean_text(lead["name"])
        room = len(name) - over
        if "{name}" in t and room >= 6:
            short = sms_clean(render_template(t, lead, s, "sms", name=library.shorten_name(name, room)))
            if sms_total(short, s) <= SMS_LIMIT:
                return short
    return None


# ----------------------------------------------------------------------- email
def _is_gmail(host: str) -> bool:
    return any(k in (host or "").lower() for k in ("gmail", "googlemail", "google.com"))


def _login_help(host: str, detail: str, what: str = "mail") -> str:
    detail = (detail or "").strip()
    if _is_gmail(host):
        return (f"Gmail refused the login ({detail}). Gmail never accepts your normal password here. It needs an App Password: "
                "in your Google Account open Security, turn on 2-Step Verification, then search 'App passwords', create one and "
                "paste its 16 letters. The username must be your full Gmail address."
                + (" Also check that IMAP is on: Gmail > Settings > See all settings > Forwarding and POP/IMAP > Enable IMAP."
                   if what == "inbox" else ""))
    return (f"The server refused the username or password ({detail}). Check the Username (usually your full email address) "
            "and the Password.")


_SMTP_WAYS = {(587, "starttls"): (465, "ssl"), (465, "ssl"): (587, "starttls")}
_IMAP_WAYS = {(993, "ssl"): (143, "starttls"), (143, "starttls"): (993, "ssl")}
_HOW = {"ssl": "SSL", "starttls": "STARTTLS", "none": "no encryption"}


def _way(pair) -> str:
    return f"{_HOW.get(pair[1], pair[1])} on port {pair[0]}"


def _open_either(first, second, open_one):
    """Opens a connection the first way, or the second way when the first fails or is slow.

    The preferred way starts at once. If it fails, or has not answered after CONNECT_STAGGER seconds, the other way is started
    alongside it, and whichever connects first is used (the other is closed). So a network that silently drops one port costs a
    few seconds, not a minute, and nothing is left for the person to change by hand. Nothing is sent or logged in here, so
    trying two ways is safe. Returns (pair that worked, connection). When both fail, the first way's error is raised with
    .also_tried listing why the other way failed too (so the person sees both reasons, not only one)."""
    ways = [first] + ([second] if second else [])
    results = queue.Queue()

    def run(pair):
        try:
            results.put((pair, open_one(pair), None))
        except BaseException as e:   # reported to the caller below; a thread must never die silently
            results.put((pair, None, e))

    def start(pair):
        threading.Thread(target=run, args=(pair,), daemon=True).start()

    def reap(n):   # a way that finishes after another already won: close it
        def drain():
            for _ in range(n):
                try:
                    _, conn, _e = results.get(timeout=CONNECT_TIMEOUT + 15)
                except queue.Empty:
                    return
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        threading.Thread(target=drain, daemon=True).start()

    started, failures = 1, {}
    start(ways[0])
    while True:
        try:
            pair, conn, err = results.get(timeout=CONNECT_STAGGER if started < len(ways) else None)
        except queue.Empty:
            start(ways[started]); started += 1   # the first way is slow: try the other one alongside it
            continue
        if conn is not None:
            reap(started - len(failures) - 1)
            return pair, conn
        failures[pair] = err
        if started < len(ways):
            start(ways[started]); started += 1
        elif len(failures) == started:
            break
    err = failures[ways[0]]
    if isinstance(err, UnicodeError) or len(ways) == 1:
        raise err
    try:
        err.also_tried = [f"{_way(p)}: {e}" for p, e in failures.items() if p != ways[0]]
    except Exception:
        pass
    raise err


def _smtp_open(s: dict):
    """A connected, secured SMTP session (not yet logged in). Gmail and most providers accept both STARTTLS on 587 and SSL on 465,
    and many networks, antivirus 'email shields' and phone hotspots block or stall one of them but not the other. So both are
    tried (see _open_either) and the one that worked is tried first next time."""
    port, sec = int(s.get("smtp_port") or 587), s.get("smtp_security", "starttls")
    other = _SMTP_WAYS.get((port, sec))
    host = clean_text(s.get("smtp_host"))
    memo = db.get_internal("smtp_pair")
    memo = memo if isinstance(memo, dict) else {}
    first, second = (port, sec), other
    if other and memo.get(host) == list(other):   # this network worked with the other way last time: start there
        first, second = other, (port, sec)
    won, smtp = _open_either(first, second, lambda pair: _smtp_open_once(dict(s, smtp_port=pair[0], smtp_security=pair[1]), timeout=CONNECT_TIMEOUT))
    if other and memo.get(host) != list(won):
        try:
            memo[host] = list(won)
            db.set_internal("smtp_pair", memo)
        except Exception:
            pass
    return smtp


def _smtp_open_once(s: dict, timeout: int = CONNECT_TIMEOUT):
    host, port = clean_text(s.get("smtp_host")), int(s.get("smtp_port") or 587)
    security = s.get("smtp_security", "starttls")
    ctx = ssl.create_default_context()
    # local_hostname is fixed so Python never has to look up this PC's own name, which can be slow on a hotspot
    # and fails outright when the computer's name has a character that is not plain English.
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx, local_hostname="localhost")
    else:
        smtp = smtplib.SMTP(host, port, timeout=timeout, local_hostname="localhost")
    try:
        smtp.ehlo()
        if security == "starttls":
            smtp.starttls(context=ctx)
            smtp.ehlo()
        smtp.sock.settimeout(SMTP_TIMEOUT)   # connecting is judged by CONNECT_TIMEOUT; logging in and sending get the patient one
    except Exception:
        try:
            smtp.close()
        except Exception:
            pass
        raise
    return smtp


def _smtp_login(smtp, s: dict):
    user, pw = mail_login(s)
    if pw:
        smtp.login(user, pw)


def send_email(to: str, subject: str, body: str, s: dict) -> str:
    if not clean_text(s.get("smtp_host")) or not clean_text(s.get("from_email")):
        raise SendError("Email isn't set up yet (Settings → Email).", "pause")
    from_email = clean_text(s["from_email"])
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((s["sender_name"], from_email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    mid = email.utils.make_msgid(domain=from_email.split("@")[-1] or "localhost")
    msg["Message-ID"] = mid
    reply_to = clean_text(s.get("reply_to")) or from_email
    if clean_text(s.get("reply_to")):
        msg["Reply-To"] = reply_to
    msg["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=unsubscribe>"
    msg.set_content(body)
    handing_over = False
    try:
        smtp = _smtp_open(s)
        with smtp:
            _smtp_login(smtp, s)
            handing_over = True
            smtp.send_message(msg)
    except SendError:
        raise
    except Exception as e:
        if handing_over and (isinstance(e, smtplib.SMTPServerDisconnected) or (isinstance(e, OSError) and not isinstance(e, smtplib.SMTPException))):
            # The connection died AFTER the message was handed over: the server may have taken it. Sending it again could
            # mean the person gets it twice, so it is never retried by itself.
            raise SendError(f"The connection dropped while sending ({type(e).__name__}). This email may or may not have been "
                            "delivered: check your Sent folder before sending it again.", "permanent")
        raise _explain_smtp(e, s)
    return mid


# ------------------------------------------------------------ backup email accounts
_cool: dict = {}      # account number -> time before which it is not tried again (it just failed)
_last_err: dict = {}  # account number -> what went wrong the last time (cleared by the next success)


def email_health(s: dict) -> list:
    """One row per email account for the Settings screen: how much it sent today, its limit, and whether it is resting and why."""
    out = []
    for a in smtp_accounts(s):
        n = a["_acct"]
        rest = max(0, int(_cool.get(n, 0) - time.time()))
        out.append({"n": n, "from": a["from_email"], "host": a["smtp_host"], "sent_today": _used_today(n), "limit": account_cap(a),
                    "warming_up": bool(warmup_limit(n, a)), "resting_min": (rest // 60 + 1) if rest else 0, "last_error": _last_err.get(n, "")})
    return out


def smtp_accounts(s: dict) -> list:
    """The email accounts to send from, main one first. A backup that is not filled in is left out. Each carries its own login
    and From address; replies still go to the main address, which is the inbox the app reads."""
    main = clean_text(s.get("from_email"))
    out = []
    if clean_text(s.get("smtp_host")) and main:
        out.append(dict(s, _acct=1))
    for n in range(2, 11):
        host, frm = clean_text(s.get(f"smtp{n}_host")), clean_text(s.get(f"smtp{n}_from"))
        if host and frm:
            a = dict(s)
            a.update(smtp_host=host, smtp_port=int(s.get(f"smtp{n}_port") or 587), smtp_security=s.get(f"smtp{n}_security") or "starttls",
                     smtp_user=s.get(f"smtp{n}_user") or "", smtp_pass=s.get(f"smtp{n}_pass") or "", from_email=frm,
                     reply_to=clean_text(s.get("reply_to")) or main or frm, _acct=n)
            out.append(a)
    return out


WARMUP = ((3, 20), (7, 40), (14, 80), (21, 150))   # (days since the account first sent, emails per day allowed until then)


def _first_day(n: int) -> str:
    f = db.get_internal("smtp_first")
    return (f or {}).get(str(n), "") if isinstance(f, dict) else ""


def warmup_limit(n: int, s: dict) -> int:
    """A new account that suddenly sends hundreds of emails is marked as spam. Extra accounts therefore start small and grow for
    three weeks (the main account is left exactly as you set it). 0 = no ramp."""
    if n == 1 or not s.get("email_warmup", True):
        return 0
    first = _first_day(n)
    if not first:
        return WARMUP[0][1]
    try:
        age = (time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) - time.mktime(time.strptime(first, "%Y-%m-%d"))) / 86400
    except ValueError:
        return 0
    for days, lim in WARMUP:
        if age < days:
            return lim
    return 0


def account_cap(a: dict) -> int:
    n = a.get("_acct", 1)
    cap = int(a.get("email_daily_cap") or 0) if n == 1 else int(a.get(f"smtp{n}_cap") or 0)
    ramp = warmup_limit(n, a)
    return min(cap, ramp) if cap and ramp else (ramp or cap)


def total_email_cap(s: dict) -> int:
    accts = smtp_accounts(s)
    return sum(account_cap(a) for a in accts) if len(accts) > 1 else int(s.get("email_daily_cap") or 0)


def _used_today(n: int) -> int:
    u = db.get_internal("smtp_used")
    return int(u["n"].get(str(n), 0)) if isinstance(u, dict) and u.get("day") == time.strftime("%Y-%m-%d") else 0


def _count_use(n: int):
    u = db.get_internal("smtp_used")
    if not isinstance(u, dict) or u.get("day") != time.strftime("%Y-%m-%d"):
        u = {"day": time.strftime("%Y-%m-%d"), "n": {}}
    u["n"][str(n)] = int(u["n"].get(str(n), 0)) + 1
    db.set_internal("smtp_used", u)
    f = db.get_internal("smtp_first")
    f = f if isinstance(f, dict) else {}
    if str(n) not in f:   # the warm-up clock starts the day the account first sends
        f[str(n)] = time.strftime("%Y-%m-%d")
        db.set_internal("smtp_first", f)


def send_email_any(to: str, subject: str, body: str, s: dict) -> str:
    """send_email, with backup accounts: if the main account fails (login, its sending limit, cannot be reached) or has reached
    its own daily limit, the next account is used. Nothing is retried after the message was handed over (that could mean twice)."""
    accts = smtp_accounts(s)
    if len(accts) <= 1:
        try:
            mid = send_email(to, subject, body, s)
        except SendError as e:
            _last_err[1] = str(e)[:300]
            raise
        _last_err.pop(1, None)
        try:
            _count_use(1)
        except Exception:
            pass
        return mid
    now_t = time.time()
    order = [a for a in accts if _cool.get(a["_acct"], 0) <= now_t and (not account_cap(a) or _used_today(a["_acct"]) < account_cap(a))]
    # spread the load: the account that has used the smallest share of its own limit goes first (each stays quiet and safe)
    order.sort(key=lambda a: (_used_today(a["_acct"]) / account_cap(a)) if account_cap(a) else 0.5)
    last = None
    for a in order or accts:
        try:
            mid = send_email(to, subject, body, a)
        except SendError as e:
            if e.kind not in ("pause", "transient", "offline"):
                raise   # refused recipient, unconfirmed delivery: not the account's fault, never try another
            _cool[a["_acct"]] = time.time() + (300 if e.kind == "offline" else 1800)
            _last_err[a["_acct"]] = str(e)[:300]
            last = e
            continue
        _last_err.pop(a["_acct"], None)
        try:
            _count_use(a["_acct"])
        except Exception:
            pass
        return mid
    raise last or SendError("No email account is ready.", "pause")


def _explain_smtp(e: Exception, s: dict) -> SendError:
    """Any failure while sending, in plain words and with the right next step (pause the campaign, retry, or give up)."""
    host = clean_text(s.get("smtp_host"))
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return SendError("Email login failed. " + _login_help(host, _smtp_text(e)), "pause")
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return SendError("This server doesn't offer a login on this connection. Set Connection to STARTTLS with port 587, "
                         "or SSL with port 465.", "pause")
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return SendError(f"Recipient refused: {_first_refusal(e)}", "permanent")
    if isinstance(e, smtplib.SMTPResponseException):
        text = _smtp_text(e)
        low = text.lower()
        if e.smtp_code == 530 or "authentication required" in low or "5.7.0" in low:
            return SendError("The mail server wants you to log in. " + _login_help(host, text), "pause")
        if any(k in low for k in ("quota", "limit exceeded", "too many", "rate limit")):
            return SendError(f"The mail server says the sending limit is reached ({text[:120]}). Try again tomorrow, "
                             "or lower 'Emails per day' in Settings.", "pause")
        return SendError(f"SMTP {e.smtp_code}: {text}", "permanent" if e.smtp_code >= 500 else "transient")
    if isinstance(e, UnicodeError):
        return SendError("A mail setting has a character the mail server can't accept (usually a hidden one in the "
                         "password). Type the password again by hand and save.", "pause")
    if isinstance(e, (smtplib.SMTPServerDisconnected, OSError, ssl.SSLError)):
        also = getattr(e, "also_tried", None)
        return SendError(f"Couldn't reach the mail server: {e}." + (f" The other standard way failed too ({'; '.join(also)})." if also else "")
                         + f" {_smtp_hint(s, bool(also))}", "offline")
    if isinstance(e, smtplib.SMTPException):
        return SendError(f"Mail server error: {e}. {_smtp_hint(s)}", "transient")
    return SendError(f"Unexpected email error: {type(e).__name__}: {e}", "transient")


def _smtp_hint(s: dict, both_tried: bool = False) -> str:
    """Why a timeout usually happens, in words, so it can be fixed without guessing."""
    port, sec = int(s.get("smtp_port") or 0), s.get("smtp_security", "starttls")
    if (port == 465 and sec != "ssl") or (port == 587 and sec == "ssl"):
        return (f"Port {port} needs the Connection set to " + ("SSL" if port == 465 else "STARTTLS") +
                ". Change Connection in Settings to match the port.")
    if both_tried:   # the app has already tried STARTTLS on 587 and SSL on 465 by itself: only the network is left
        return ("The app tried both standard ways (STARTTLS on port 587 and SSL on port 465) and this network stops both. Check the "
                "internet connection first. If other sites work, something on this PC or network is blocking mail: turn off any "
                "antivirus 'email shield' or mail scanning for a minute, or try a phone hotspot.")
    return ("Check the internet connection first. If other sites work, something on this PC or network is blocking "
            f"mail on port {port or 587}: turn off any antivirus 'email shield' for a minute, or try a phone hotspot.")


def _smtp_text(e) -> str:
    err = getattr(e, "smtp_error", b"")
    return err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)


def _first_refusal(e) -> str:
    for addr, (code, text) in e.recipients.items():
        t = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
        return f"{addr} ({code} {t})"
    return "unknown"


def _result(steps: list) -> dict:
    bad = next((st for st in steps if not st["ok"]), None)
    return {"ok": bad is None, "steps": steps,
            "message": (f"{bad['label']}: {bad['detail']}" if bad else "Everything checked out.")}


def _reach(host: str, port: int, alternatives):
    """Steps that find the server and open a connection; on failure says which other port is open, if any."""
    steps = []
    try:
        socket.getaddrinfo(host, port)
        steps.append({"label": "Find the server", "ok": True, "detail": host})
    except OSError:
        steps.append({"label": "Find the server", "ok": False,
                      "detail": f"Couldn't find '{host}'. Check the spelling and that this PC is online."})
        return steps
    try:
        socket.create_connection((host, port), timeout=10).close()
        steps.append({"label": "Connect", "ok": True, "detail": f"port {port} is open"})
    except OSError as e:
        open_alt = None
        for alt_port, how in alternatives:
            if alt_port != port:
                try:
                    socket.create_connection((host, alt_port), timeout=5).close()
                    open_alt = (alt_port, how)
                    break
                except OSError:
                    pass
        hint = (f"Port {open_alt[0]} is open though: set Connection to {open_alt[1]} and Port to {open_alt[0]}."
                if open_alt else "The usual ports are blocked here (firewall, antivirus or the network). Try a phone hotspot.")
        steps.append({"label": "Connect", "ok": False, "detail": f"Couldn't connect on port {port} ({e}). {hint}"})
    return steps


def diagnose_email(s: dict) -> dict:
    """Walks the sending path one step at a time and says which step fails and what to change. Sends nothing."""
    host = clean_text(s.get("smtp_host"))
    port = int(s.get("smtp_port") or 587)
    steps = []
    if not host or not clean_text(s.get("from_email")):
        steps.append({"label": "Settings", "ok": False, "detail": "Fill in the Mail server and the Send from address first."})
        return _result(steps)
    steps.append({"label": "Settings", "ok": True, "detail": f"{host}, port {port}, {s.get('smtp_security', 'starttls')}"})
    sec = s.get("smtp_security", "starttls")
    steps += _reach(host, port, ((587, "STARTTLS"), (465, "SSL")))
    blocked = None
    if not steps[-1]["ok"]:
        if (port, sec) not in _SMTP_WAYS:
            return _result(steps)
        blocked = steps.pop()   # this port is shut, but the other standard one may be open: sending tries it by itself
    try:
        smtp = _smtp_open(s)
    except Exception as e:
        steps.append({"label": "Connect" if blocked else "Secure connection", "ok": False, "detail": str(_explain_smtp(e, s))})
        return _result(steps)
    memo = db.get_internal("smtp_pair")
    used = memo.get(host) if isinstance(memo, dict) else None
    used = tuple(used) if used else (port, sec)
    if blocked:
        steps.append({"label": "Connect", "ok": True, "detail": f"port {port} is blocked here, so {_way(used)} is used instead"})
    steps.append({"label": "Secure connection", "ok": True,
                  "detail": f"encrypted ({_way(used)})" if sec != "none" else "no encryption"})
    try:
        user, pw = mail_login(s)
        if pw:
            smtp.login(user, pw)
            steps.append({"label": "Login", "ok": True, "detail": f"signed in as {user}"})
        elif s.get("smtp_security") == "none":
            steps.append({"label": "Login", "ok": True, "detail": "no login needed"})
        else:
            steps.append({"label": "Login", "ok": False,
                          "detail": "No password entered. " + _login_help(host, "no password") if _is_gmail(host)
                          else "No password entered."})
    except Exception as e:
        steps.append({"label": "Login", "ok": False, "detail": str(_explain_smtp(e, s))})
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return _result(steps)


# ------------------------------------------------------------------------- SMS
# SMS goes out through your own Android phone running the free open-source "SMS Gateway for Android" app in
# Local server mode: the PC talks to the phone over the same Wi-Fi / hotspot, and the phone's SIM sends the text.
SMS_PATHS = (("/messages", "textMessage"), ("/message", "message"))   # newer app versions, then older ones
SMS_STATE_WAIT = 40   # seconds to wait for the phone to say the text was sent
SMS_POLL = 1.5        # seconds between asking the phone how the text is going


def gateway_base(s: dict) -> str:
    raw = clean_text(s.get("sms_gateway_url")).rstrip("/")
    if not raw:
        raise SendError("Your phone isn't connected yet (Settings → Text messages).", "pause")
    if not re.match(r"^https?://", raw, re.I):
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname:
        raise SendError("The phone address looks wrong. It should look like 192.168.43.1:8080.", "pause")
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 8080}"


def _gateway_auth(s: dict):
    return clean_text(s.get("sms_gateway_user")), clean_secret(s.get("sms_gateway_pass"))


def _phone_unreachable(base: str, e) -> SendError:
    return SendError(f"Couldn't reach the phone at {base} ({type(e).__name__}). Check that the phone is on the same Wi-Fi or "
                     "hotspot as this PC, that the SMS Gateway app is open with its Local server switched on, and that the "
                     "address matches the one the app shows (it can change when the phone reconnects). Texts wait and are tried again by "
                     "themselves; nothing is lost.", "offline")


def _sms_state(j) -> str:
    """The delivery state in the phone's reply, lower case ('pending', 'sent', 'delivered', 'failed' ...)."""
    if not isinstance(j, dict):
        return ""
    state = j.get("state") or j.get("status") or ""
    recipients = j.get("recipients") or []
    if recipients and isinstance(recipients[0], dict):
        r0 = recipients[0]
        if str(r0.get("state") or "").lower() in ("failed",):
            return "failed"
        state = state or r0.get("state") or ""
    return str(state).lower()


def _sms_failure_reason(j) -> str:
    recipients = (j or {}).get("recipients") or []
    reason = (recipients[0].get("error") if recipients and isinstance(recipients[0], dict) else "") or (j or {}).get("reason") or ""
    return str(reason)


def send_sms(to: str, text: str, s: dict) -> str:
    base = gateway_base(s)
    auth = _gateway_auth(s)
    if not auth[0] or not auth[1]:
        raise SendError("Add the username and password the SMS Gateway app shows (Settings → Text messages).", "pause")
    text = sms_clean(text)
    if not text:
        raise SendError("Message is empty", "permanent")
    sim = int(s.get("sms_sim") or 0)
    r = path = None
    for path, field in SMS_PATHS:
        body = {field: {"text": text} if field == "textMessage" else text, "phoneNumbers": [to]}
        if sim:
            body["simNumber"] = sim
        try:
            r = requests.post(base + path, json=body, auth=auth, timeout=(6, 20))
        except requests.RequestException as e:
            raise _phone_unreachable(base, e)
        if r.status_code not in (404, 405):
            break
    if r.status_code in (401, 403):
        raise SendError("Your phone rejected the username or password. Copy them again from the SMS Gateway app "
                        "(Home tab, Local server).", "pause")
    if r.status_code in (404, 405):
        raise SendError("The phone answered but doesn't know the send address. Update the SMS Gateway app and check the "
                        "address in Settings.", "pause")
    if r.status_code == 429 or r.status_code >= 500:
        raise SendError(f"The phone is busy or refusing more texts right now (error {r.status_code}). Android limits how "
                        "fast an app may send; the app will retry.", "transient")
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code >= 400:
        detail = (j.get("message") if isinstance(j, dict) else "") or r.text[:160]
        raise SendError(f"The phone refused the text: {detail}", "permanent")
    mid = str(j.get("id") or "") if isinstance(j, dict) else ""
    return _await_sms(base, path, mid, auth, j)


def _await_sms(base: str, path: str, mid: str, auth, first) -> str:
    """The phone replies 'accepted' at once; the text is really sent a moment later. Wait for 'sent' so a text that
    fails (no airtime, no signal, Android's rate limit) is never recorded as delivered."""
    state, last = _sms_state(first), first
    deadline = time.time() + SMS_STATE_WAIT
    while state not in ("sent", "delivered", "failed") and mid and time.time() < deadline:
        time.sleep(SMS_POLL)
        try:
            g = requests.get(f"{base}{path}/{mid}", auth=auth, timeout=(4, 10))
        except requests.RequestException:
            break
        if g.status_code != 200:
            break
        try:
            last = g.json()
        except ValueError:
            break
        state = _sms_state(last)
    if state == "failed":
        raise SendError("The phone couldn't send this text" + (f" ({_sms_failure_reason(last)})" if _sms_failure_reason(last) else "")
                        + ". Check the SIM has airtime and signal.", "permanent")
    return mid


def diagnose_sms(s: dict) -> dict:
    """Is the phone there, and does it accept the login? Sends nothing (the test-text button does the real thing)."""
    steps = []
    try:
        base = gateway_base(s)
    except SendError as e:
        return _result([{"label": "Settings", "ok": False, "detail": str(e)}])
    user, pw = _gateway_auth(s)
    steps.append({"label": "Settings", "ok": bool(user and pw), "detail": base if user and pw
                  else "Add the username and password from the SMS Gateway app."})
    if not (user and pw):
        return _result(steps)
    try:
        r = requests.get(base + "/health", auth=(user, pw), timeout=(6, 8))
        steps.append({"label": "Reach the phone", "ok": True, "detail": f"answered from {base}"})
        if r.status_code in (401, 403):
            steps.append({"label": "Login", "ok": False, "detail": "The phone rejected the username or password."})
    except requests.RequestException as e:
        steps.append({"label": "Reach the phone", "ok": False, "detail": str(_phone_unreachable(base, e))})
    return _result(steps)


# ------------------------------------------------------------------------ IMAP
def _imap_open_once(host: str, pair, timeout: int = 30):
    port, sec = pair
    if sec == "ssl":
        return imaplib.IMAP4_SSL(host, port, timeout=timeout)
    M = imaplib.IMAP4(host, port, timeout=timeout)
    try:
        if sec == "starttls":
            M.starttls(ssl_context=ssl.create_default_context())
    except Exception:
        try:
            M.shutdown()
        except Exception:
            pass
        raise
    return M


def _imap_connect(s: dict):
    """The reply inbox, connected and logged in. Like sending, it uses SSL on 993 or STARTTLS on 143, whichever this network lets
    through (the one that worked is tried first next time). A wrong password is never retried the other way."""
    user, pw = inbox_login(s)
    host = clean_text(s.get("imap_host"))
    if not host or not user:
        raise SendError("Reply tracking isn't set up yet (Settings → Replies).", "pause")
    port, sec = int(s.get("imap_port") or 993), s.get("imap_security", "ssl")
    other = _IMAP_WAYS.get((port, sec))
    memo = db.get_internal("imap_pair")
    memo = memo if isinstance(memo, dict) else {}
    first, second = (port, sec), other
    if other and memo.get(host) == list(other):
        first, second = other, (port, sec)
    logging_in = False
    try:
        won, M = _open_either(first, second, lambda pair: _imap_open_once(host, pair))
        if other and memo.get(host) != list(won):
            try:
                memo[host] = list(won)
                db.set_internal("imap_pair", memo)
            except Exception:
                pass
        logging_in = True
        try:
            M.login(user, pw)
        except BaseException:
            try:
                M.shutdown()
            except Exception:
                pass
            raise
    except imaplib.IMAP4.error as e:
        if not logging_in:   # the server greeted us badly before any login: that is a connection problem, not a password one
            raise SendError(f"Couldn't reach the reply inbox: {e}", "transient")
        raise SendError("Reply-inbox login failed. " + _login_help(host, str(e), "inbox"), "pause")
    except UnicodeError:
        raise SendError("The reply-inbox password has a hidden character. Type it again by hand and save.", "pause")
    except (OSError, ssl.SSLError) as e:
        also = getattr(e, "also_tried", None)
        raise SendError(f"Couldn't reach the reply inbox: {e}" + (f" (the other standard way failed too: {'; '.join(also)})" if also else ""), "transient")
    return M


def _max_uid(M) -> int:
    typ, data = M.uid("SEARCH", None, "ALL")
    ids = data[0].split() if typ == "OK" and data and data[0] else []
    return max((int(x) for x in ids), default=0)


def test_imap(s: dict) -> dict:
    """Same step-by-step check as email sending, for the inbox that reads replies."""
    host = clean_text(s.get("imap_host"))
    port = int(s.get("imap_port") or 993)
    user, _ = inbox_login(s)
    if not host or not user:
        return _result([{"label": "Settings", "ok": False, "detail": "Fill in the Inbox server and the login first."}])
    steps = [{"label": "Settings", "ok": True, "detail": f"{host}, port {port}"}]
    steps += _reach(host, port, ((993, "SSL"), (143, "STARTTLS")))
    blocked = None
    if not steps[-1]["ok"]:
        if (port, s.get("imap_security", "ssl")) not in _IMAP_WAYS:
            return _result(steps)
        blocked = steps.pop()   # this port is shut, but the other standard one may be open: the inbox tries it by itself
    try:
        M = _imap_connect(s)
    except SendError as e:
        steps.append({"label": "Connect" if blocked else "Login", "ok": False, "detail": str(e)})
        return _result(steps)
    if blocked:
        memo = db.get_internal("imap_pair")
        used = tuple(memo.get(host)) if isinstance(memo, dict) and memo.get(host) else (port, s.get("imap_security", "ssl"))
        steps.append({"label": "Connect", "ok": True, "detail": f"port {port} is blocked here, so {_way(used)} is used instead"})
    steps.append({"label": "Login", "ok": True, "detail": f"signed in as {user}"})
    try:
        M.select("INBOX", readonly=True)
        top = _max_uid(M)
        if db.get_internal("imap_last_uid") is None:
            db.set_internal("imap_last_uid", top)  # only track replies that arrive from now on
        steps.append({"label": "Open the inbox", "ok": True, "detail": f"inbox has messages up to #{top}"})
    except Exception as e:
        steps.append({"label": "Open the inbox", "ok": False, "detail": str(e)})
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return _result(steps)


def poll_imap(s: dict = None) -> dict:
    s = s or db.get_settings()
    M = _imap_connect(s)
    stats = {"fetched": 0, "replies": 0, "bounces": 0}
    try:
        M.select("INBOX", readonly=True)
        last = db.get_internal("imap_last_uid")
        if last is None:
            db.set_internal("imap_last_uid", _max_uid(M))
            return stats
        typ, data = M.uid("SEARCH", None, f"UID {last + 1}:*")
        uids = sorted(int(x) for x in (data[0].split() if typ == "OK" and data and data[0] else []))
        for uid in uids:
            if uid <= last:
                continue  # "n:*" always returns the newest message even if it's old
            typ, parts = M.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None) if typ == "OK" else None
            if raw:
                stats["fetched"] += 1
                res = process_inbound_email(raw, s)
                if res.get("matched"):
                    stats["bounces" if res.get("bounce") else "replies"] += 1
            last = uid
            db.set_internal("imap_last_uid", last)
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return stats


def _plain_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    html = False
    if part is None:
        part = msg.get_body(preferencelist=("html",))
        html = True
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", "replace")
    if html:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
    return text


def _report_lead(body: str, subject: str, mid, s: dict) -> dict:
    """A lead left by the free-report page: the page's small web function emails you the details, signed with a secret that only the
    page and this app know. Without a matching secret the email is ignored (anyone can write 'Health Check lead' in a subject)."""
    import hmac
    token = str(s.get("free_report_token") or "").strip()
    fields = {}
    for line in body.splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip().lower() in ("name", "phone", "email", "business", "note", "summary", "source", "token", "consent"):
            fields.setdefault(k.strip().lower(), v.strip())
    if not token or not hmac.compare_digest(fields.get("token", "").encode(), token.encode()):
        return {"matched": False}
    if fields.get("consent", "").lower() != "yes":
        return {"matched": False}
    addr = normalize_email(fields.get("email")) or None
    phone = (fields.get("phone") or "").strip()
    came = re.sub(r"[^a-z0-9_-]", "", fields.get("source", "").lower())[:30]   # which link brought them: popup, chatbot, partner...
    name = (fields.get("business") or fields.get("name") or "").strip()[:120]
    if not name or not (addr or phone):
        return {"matched": False}
    note = ("Used your free Business Health Check and asked you to look at their records. They agreed to be contacted."
            + (f" Contact person: {fields['name']}." if fields.get("business") and fields.get("name") else "")
            + (f" Came from: {came}." if came else ""))
    said = fields.get("note", "")[:600]
    summary = fields.get("summary", "")[:300]
    text = "I tried your free Business Health Check and would like you to look at my full records." + (f" {said}" if said else "") + (f" (Their report: {summary})" if summary else "")
    with db.db() as conn:
        res = leadsmod.add_lead(conn, {"name": name, "email": addr, "phone": phone or None, "notes": note + (f" They wrote: {said}" if said else "")}, "free report")
        lead_id = res["id"]
        ch = "email" if addr else "whatsapp"
        r = leadsmod.record_reply(conn, lead_id, ch, text, "My Business Health Check", mid)
    return {"matched": True, "lead_id": lead_id, "grade": r.get("grade"), "duplicate": r.get("duplicate", False)}


def process_inbound_email(raw: bytes, s: dict = None) -> dict:
    s = s or db.get_settings()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    from_addr = normalize_email(email.utils.parseaddr(str(msg.get("From", "")))[1]) or ""
    subject = str(msg.get("Subject", "") or "")
    mid = (str(msg.get("Message-ID", "")) or "").strip() or None
    refs = re.findall(r"<[^>]+>", f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}")
    body = _plain_text(msg)

    if subject.lower().startswith("health check lead:"):
        return _report_lead(body, subject, mid, s)

    if from_addr and from_addr == normalize_email(s.get("from_email")):
        return {"matched": False}

    with db.db() as conn:
        if grading.looks_like_bounce(from_addr, subject):
            lead_row = None
            for addr in {normalize_email(a) for a in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", body)}:
                if addr:
                    lead_row = conn.execute("SELECT id FROM leads WHERE email=?", (addr,)).fetchone()
                    if lead_row:
                        break
            if not lead_row and refs:
                q = ",".join("?" * len(refs))
                lead_row = conn.execute(
                    f"SELECT lead_id AS id FROM messages WHERE direction='out' AND message_id_header IN ({q})", refs).fetchone()
            if not lead_row:
                return {"matched": False, "bounce": True}
            leadsmod.mark_bounced(conn, lead_row["id"], "Delivery failure notice received")
            leadsmod.record_reply(conn, lead_row["id"], "email", body[:2000], subject, mid, forced_grade="bounce")
            return {"matched": True, "bounce": True, "lead_id": lead_row["id"]}

        lead_id = None
        if refs:
            q = ",".join("?" * len(refs))
            row = conn.execute(
                f"SELECT lead_id FROM messages WHERE direction='out' AND message_id_header IN ({q})", refs).fetchone()
            if row:
                lead_id = row["lead_id"]
        if lead_id is None and from_addr:
            row = conn.execute("SELECT id FROM leads WHERE email=?", (from_addr,)).fetchone()
            if row:
                lead_id = row["id"]
        if lead_id is None:
            # someone who is not in the CRM wrote to you (an enquiry from another source): with conversation mode ON they become a lead
            # so the assistant and your pipeline see them. Mailing lists, no-reply senders and automatic mail never do.
            local = from_addr.split("@", 1)[0] if from_addr else ""
            automated = (re.search(r"no-?reply|do-?not-?reply|mailer-daemon|postmaster|notification|newsletter|bounce|alerts?$", local, re.I)
                         or msg.get("List-Unsubscribe") or str(msg.get("Auto-Submitted", "no")).lower() != "no"
                         or str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk")
                         or grading.classify(body)["grade"] in ("auto", "bounce"))
            # someone who used the free Business Health Check page and wrote to you from it always becomes a lead, with or without conversation mode
            from_report = "business health check" in subject.lower()
            if not from_addr or automated or ((s.get("closer_mode") or "off") == "off" and not from_report):
                return {"matched": False}
            display = email.utils.parseaddr(str(msg.get("From", "")))[0].strip().strip('"')
            name = display if display and "@" not in display else from_addr.split("@")[0].replace(".", " ").replace("_", " ").title()
            res0 = leadsmod.add_lead(conn, {"name": name, "email": from_addr, "notes": (
                "Used your free Business Health Check and asked you to look at their records." if from_report
                else f"Wrote to us first by email: {subject[:120]}")}, "free report" if from_report else "inbound email")
            lead_id = res0["id"]
        res = leadsmod.record_reply(conn, lead_id, "email", body.strip(), subject, mid)
        return {"matched": True, "lead_id": lead_id, "grade": res.get("grade"), "duplicate": res.get("duplicate", False)}
