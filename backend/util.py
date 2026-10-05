"""Small shared helpers: time, phone/name/email normalisation, process checks."""
import os
import re
import sys
from datetime import datetime, timedelta


def now() -> str:
    """Local time as 'YYYY-MM-DD HH:MM:SS'. Everything in the DB uses local time."""
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def today_start() -> str:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(sep=" ", timespec="seconds")


def days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(sep=" ", timespec="seconds")


# ---------------------------------------------------------------- phone numbers
def normalize_phone(raw):
    """Return (e164 or None, is_mobile).

    Kenyan numbers are normalised to +254XXXXXXXXX. Mobile = 9 national digits
    starting with 7 or 1 (Safaricom/Airtel/Telkom). Foreign numbers are kept
    as-is with is_mobile False. Anything unusable returns (None, False).
    """
    if not raw:
        return None, False
    s = str(raw).strip()
    # Several numbers in one cell: keep the first
    s = re.split(r"[;,/]|\bor\b|\band\b", s, maxsplit=1, flags=re.I)[0]
    has_plus = s.strip().startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None, False
    if digits.startswith("00254"):
        digits = digits[2:]
    if digits.startswith("254"):
        national = digits[3:]
    elif digits.startswith("0"):
        national = digits[1:]
    elif has_plus:
        return ("+" + digits if 7 <= len(digits) <= 15 else None), False
    else:
        national = digits
    if not (7 <= len(national) <= 9):
        return None, False
    is_mobile = len(national) == 9 and national[0] in "71"
    return "+254" + national, is_mobile


def pretty_phone(e164):
    if not e164:
        return ""
    if e164.startswith("+254") and len(e164) == 13:
        n = e164[4:]
        return f"0{n[:3]} {n[3:6]} {n[6:]}"
    return e164


# ---------------------------------------------------------------------- emails
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]{2,}$")


def normalize_email(raw):
    if not raw:
        return None
    s = str(raw).strip().strip("<>").lower()
    return s if _EMAIL_RE.match(s) else None


# ----------------------------------------------------------------------- names
_STOP = {"ltd", "limited", "co", "company", "the", "inc"}


def normalize_name(name: str) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    words = [w for w in s.split() if w not in _STOP]
    return " ".join(words)


def normalize_town(town: str) -> str:
    return re.sub(r"\s+", " ", (town or "").strip().lower())


def name_key(name: str, town: str) -> str:
    return f"{normalize_name(name)}|{normalize_town(town)}"


_ADDR_NOISE = {"kenya", "road", "rd", "street", "st", "po", "box", "building", "bldg", "along", "opp", "opposite", "near", "the", "and", "of", "town"}


def address_similarity(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+", (a or "").lower())) - _ADDR_NOISE
    tb = set(re.findall(r"[a-z0-9]+", (b or "").lower())) - _ADDR_NOISE
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ------------------------------------------------------------------- processes
def parent_alive(pid) -> bool:
    """True if process `pid` still exists (used so the backend dies with the app)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_alive(pid) -> bool:
    return parent_alive(pid) if pid else False
