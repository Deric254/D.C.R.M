"""Sending (SMTP email, Africa's Talking SMS) and receiving (IMAP replies).

SendError.kind tells the caller what to do:
  transient  network/server hiccup  -> retry later
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
import ssl
from email.message import EmailMessage

import requests

import db
import grading
import leads as leadsmod
from util import normalize_email, normalize_phone

DEFAULT_AT_URL = "https://api.africastalking.com"
SANDBOX_AT_URL = "https://api.sandbox.africastalking.com"


class SendError(Exception):
    def __init__(self, message, kind="transient"):
        super().__init__(message)
        self.kind = kind


# ------------------------------------------------------------------ rendering
def render_template(template: str, lead, settings: dict) -> str:
    values = {
        "name": lead["name"],
        "town": lead["town"],
        "sector": lead["sector"],
        "sender": settings.get("sender_name", ""),
    }
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1).lower(), m.group(0))), template or "")


def unknown_placeholders(*templates) -> list:
    allowed = {"name", "town", "sector", "sender"}
    found = set()
    for t in templates:
        found |= {p.lower() for p in re.findall(r"\{(\w+)\}", t or "")}
    return sorted(found - allowed)


def final_body(channel: str, body: str, settings: dict) -> str:
    body = (body or "").rstrip()
    if not settings.get("append_optout", True):
        return body
    if channel == "sms":
        return body + (settings.get("optout_suffix_sms") or "")
    footer = (settings.get("optout_footer_email") or "").strip()
    sig = (settings.get("sender_name") or "").strip()
    tail = "\n\n--\n" + (sig + "\n" if sig else "") + footer
    return body + tail


def sms_segments(text: str) -> int:
    n = len(text)
    if n == 0:
        return 0
    gsm = all(ord(c) < 128 for c in text)
    single, multi = (160, 153) if gsm else (70, 67)
    return 1 if n <= single else -(-n // multi)


# ----------------------------------------------------------------------- email
def send_email(to: str, subject: str, body: str, s: dict) -> str:
    if not s["smtp_host"] or not s["from_email"]:
        raise SendError("Email isn't set up yet (Settings → Email).", "pause")
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((s["sender_name"], s["from_email"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    mid = email.utils.make_msgid(domain=s["from_email"].split("@")[-1] or "localhost")
    msg["Message-ID"] = mid
    reply_to = s.get("reply_to") or s["from_email"]
    if s.get("reply_to"):
        msg["Reply-To"] = s["reply_to"]
    msg["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=unsubscribe>"
    msg.set_content(body)

    security = s.get("smtp_security", "starttls")
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(s["smtp_host"], int(s["smtp_port"]), timeout=30, context=ctx)
        else:
            smtp = smtplib.SMTP(s["smtp_host"], int(s["smtp_port"]), timeout=30)
        with smtp:
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if s.get("smtp_user"):
                smtp.login(s["smtp_user"], s["smtp_pass"])
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise SendError(f"Email login failed: {_smtp_text(e)}", "pause")
    except smtplib.SMTPRecipientsRefused as e:
        raise SendError(f"Recipient refused: {_first_refusal(e)}", "permanent")
    except smtplib.SMTPResponseException as e:
        kind = "permanent" if e.smtp_code >= 500 else "transient"
        raise SendError(f"SMTP {e.smtp_code}: {_smtp_text(e)}", kind)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise SendError(f"Couldn't reach the mail server: {e}. {_smtp_hint(s)}", "transient")
    return mid


def _smtp_hint(s: dict) -> str:
    """Why a timeout usually happens, in words, so it can be fixed without guessing."""
    port, sec = int(s.get("smtp_port") or 0), s.get("smtp_security", "starttls")
    if (port == 465 and sec != "ssl") or (port == 587 and sec == "ssl"):
        return (f"Port {port} needs the Connection set to " + ("SSL" if port == 465 else "STARTTLS") +
                ". Change Connection in Settings to match the port.")
    return ("Check the internet connection first. If other sites work, something on this PC or network is blocking "
            f"mail on port {port or 587}: try the other pair (SSL with port 465, or STARTTLS with port 587), turn off "
            "any antivirus 'email shield' for a minute, or try a phone hotspot.")


def _smtp_text(e) -> str:
    err = getattr(e, "smtp_error", b"")
    return err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)


def _first_refusal(e) -> str:
    for addr, (code, text) in e.recipients.items():
        t = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
        return f"{addr} ({code} {t})"
    return "unknown"


# ------------------------------------------------------------------------- SMS
def send_sms(to: str, text: str, s: dict) -> str:
    user, key = s.get("at_username"), s.get("at_api_key")
    if not user or not key:
        raise SendError("SMS isn't set up yet (Settings → SMS).", "pause")
    base = (s.get("at_base_url") or DEFAULT_AT_URL).rstrip("/")
    if user == "sandbox" and base == DEFAULT_AT_URL:
        base = SANDBOX_AT_URL
    data = {"username": user, "to": to, "message": text}
    if s.get("at_sender_id"):
        data["from"] = s["at_sender_id"]
    try:
        r = requests.post(base + "/version1/messaging", data=data,
                          headers={"apiKey": key, "Accept": "application/json"}, timeout=30)
    except requests.RequestException as e:
        raise SendError(f"Couldn't reach Africa's Talking: {e}", "transient")
    if r.status_code in (401, 403):
        raise SendError("Africa's Talking rejected the username or API key.", "pause")
    if r.status_code >= 500:
        raise SendError(f"Africa's Talking error {r.status_code}", "transient")
    try:
        j = r.json()
    except ValueError:
        if r.status_code >= 400:
            raise SendError(f"Africa's Talking said: {r.text[:200]}", "permanent")
        raise SendError("Unexpected response from Africa's Talking", "transient")
    data_ = j.get("SMSMessageData") or {}
    recipients = data_.get("Recipients") or []
    if not recipients:
        raise SendError(f"Africa's Talking: {data_.get('Message') or r.text[:200]}", "permanent")
    r0 = recipients[0]
    if r0.get("statusCode") in (100, 101, 102):
        return str(r0.get("messageId", ""))
    status = r0.get("status", "Failed")
    kind = "pause" if status == "InsufficientBalance" else "permanent"
    raise SendError(f"Africa's Talking: {status}", kind)


# ------------------------------------------------------------------------ IMAP
def _imap_connect(s: dict):
    if not s.get("imap_host") or not s.get("imap_user"):
        raise SendError("Reply tracking isn't set up yet (Settings → Replies).", "pause")
    host, port, sec = s["imap_host"], int(s["imap_port"]), s.get("imap_security", "ssl")
    try:
        if sec == "ssl":
            M = imaplib.IMAP4_SSL(host, port, timeout=30)
        else:
            M = imaplib.IMAP4(host, port, timeout=30)
            if sec == "starttls":
                M.starttls()
        M.login(s["imap_user"], s["imap_pass"])
    except imaplib.IMAP4.error as e:
        raise SendError(f"Reply-inbox login failed: {e}", "pause")
    except (OSError, ssl.SSLError) as e:
        raise SendError(f"Couldn't reach the reply inbox: {e}", "transient")
    return M


def _max_uid(M) -> int:
    typ, data = M.uid("SEARCH", None, "ALL")
    ids = data[0].split() if typ == "OK" and data and data[0] else []
    return max((int(x) for x in ids), default=0)


def test_imap(s: dict) -> dict:
    M = _imap_connect(s)
    try:
        M.select("INBOX", readonly=True)
        top = _max_uid(M)
        if db.get_internal("imap_last_uid") is None:
            db.set_internal("imap_last_uid", top)  # only track replies that arrive from now on
        return {"ok": True, "message": f"Connected. Inbox has messages up to #{top}."}
    finally:
        try:
            M.logout()
        except Exception:
            pass


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


def process_inbound_email(raw: bytes, s: dict = None) -> dict:
    s = s or db.get_settings()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    from_addr = normalize_email(email.utils.parseaddr(str(msg.get("From", "")))[1]) or ""
    subject = str(msg.get("Subject", "") or "")
    mid = (str(msg.get("Message-ID", "")) or "").strip() or None
    refs = re.findall(r"<[^>]+>", f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}")
    body = _plain_text(msg)

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
            return {"matched": False}
        res = leadsmod.record_reply(conn, lead_id, "email", body.strip(), subject, mid)
        return {"matched": True, "lead_id": lead_id, "grade": res.get("grade"), "duplicate": res.get("duplicate", False)}


def handle_sms_inbound(number: str, text: str) -> dict:
    norm, _ = normalize_phone(number)
    if not norm:
        return {"matched": False}
    with db.db() as conn:
        row = conn.execute("SELECT id FROM leads WHERE phone_norm=?", (norm,)).fetchone()
        if not row:
            return {"matched": False}
        res = leadsmod.record_reply(conn, row["id"], "sms", text)
        return {"matched": True, "lead_id": row["id"], "grade": res.get("grade")}
