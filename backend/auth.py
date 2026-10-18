"""Accounts, sessions, security questions and licences.

The owner account ("Deric Marangu") is the mother account. It is created on the owner's own computer by proving
possession of the owner key, and it is the only account that can add people or authorise another computer.
Another computer cannot be used until the owner has signed a licence for that computer's activation code.

Passwords and security answers are never stored, only salted scrypt hashes. Sign-in sessions are random tokens of which
only a hash is kept. Nothing here talks to the internet.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from datetime import date, timedelta

import db
import ed25519
import owner_key
from util import now

OWNER_NAME = "Deric Marangu"
SESSION_IDLE = 12 * 3600          # signed out after 12 hours of no use
SESSION_MAX = 14 * 24 * 3600      # and always after 14 days
MAX_FAILS = 5                      # wrong passwords before a lock-out starts
COOKIE = "dbi_session"

QUESTIONS = (
    "What was the name of your first school?",
    "Which town or village did your mother grow up in?",
    "What was the name of your childhood best friend?",
    "What was the first phone you ever owned?",
    "What was the name of your first pet?",
    "What is your oldest cousin's middle name?",
    "What was the first car brand your family owned?",
    "In which town did you meet your closest friend?",
    "What was your favourite teacher's last name?",
    "What was the first shop you remember buying from?",
)

_COMMON = {
    "password", "passw0rd", "p@ssw0rd", "p@ssword", "qwerty", "qwertyuiop", "letmein", "welcome", "admin", "administrator",
    "iloveyou", "monkey", "dragon", "football", "baseball", "abc123", "123456", "12345678", "123456789", "1234567890",
    "111111", "000000", "changeme", "default", "login", "master", "shadow", "sunshine", "princess", "whatever",
    "trustno1", "kenya", "nairobi", "mombasa", "harambee", "dericbi", "deric", "marangu", "crm", "dericbicrm",
}
_ROWS = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiop", "asdfghjkl", "zxcvbnm")


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


class LicenseError(ValueError):
    pass


# ------------------------------------------------------------------ hashing
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_secret(secret: str) -> str:
    salt = os.urandom(16)
    try:
        return "scrypt$14$8$1$%s$%s" % (_b64(salt), _b64(hashlib.scrypt(secret.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)))
    except (AttributeError, ValueError):   # a Python build without scrypt
        return "pbkdf2$600000$%s$%s" % (_b64(salt), _b64(hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 600000, 32)))


def check_secret(secret: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if parts[0] == "scrypt":
            n, r, p = 2 ** int(parts[1]), int(parts[2]), int(parts[3])
            salt, want = _unb64(parts[4]), _unb64(parts[5])
            got = hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p, dklen=len(want))
        else:
            salt, want = _unb64(parts[2]), _unb64(parts[3])
            got = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, int(parts[1]), len(want))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


_dummy = []


def _dummy_hash() -> str:
    """A hash to check against when the username is unknown, so that costs the same time as a wrong password.
    Made the first time it is needed, not at start-up (a password hash is a noticeable cost on a weak computer)."""
    if not _dummy:
        _dummy.append(hash_secret("not-a-real-password"))
    return _dummy[0]


# ------------------------------------------------------------ strong passwords
def password_problems(password: str, username: str = "") -> list:
    """Everything wrong with a password, in plain words. Empty means it is strong enough."""
    pw, bad = password or "", []
    if len(pw) < 12:
        bad.append("Use at least 12 characters.")
    if len(pw) > 128:
        bad.append("Use at most 128 characters.")
    if not re.search(r"[a-z]", pw):
        bad.append("Add a lowercase letter.")
    if not re.search(r"[A-Z]", pw):
        bad.append("Add a capital letter.")
    if not re.search(r"\d", pw):
        bad.append("Add a number.")
    if not re.search(r"[^A-Za-z0-9\s]", pw):
        bad.append("Add a symbol such as ! ? # % or @.")
    if re.search(r"\s", pw) and len(pw) < 20:
        bad.append("Spaces only count in a long passphrase (20+ characters).")
    low = pw.lower()
    if low in _COMMON or re.sub(r"[^a-z]", "", low) in _COMMON:
        bad.append("That is one of the most common passwords.")
    for token in re.findall(r"[a-z]{3,}", (username or "").lower()):
        if token in low:
            bad.append("Don't put your name in your password.")
            break
    if re.search(r"(.)\1\1", pw):
        bad.append("Avoid the same character three times in a row.")
    for row in _ROWS:
        if any(row[i:i + 4] in low or row[i:i + 4][::-1] in low for i in range(len(row) - 3)):
            bad.append("Avoid runs like abcd, 1234 or qwer.")
            break
    if len(set(pw)) < 7 and len(pw) >= 12:
        bad.append("Use more different characters.")
    return bad


def strong_password(length: int = 16) -> str:
    """A random password that passes the rules, for temporary passwords."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length - 2)) + secrets.choice("!?#%@") + secrets.choice("23456789")
        pw = "".join(secrets.SystemRandom().sample(pw, len(pw)))
        if not password_problems(pw):
            return pw


# ----------------------------------------------------------- security questions
def norm_answer(answer: str) -> str:
    text = unicodedata.normalize("NFKD", answer or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def question_problems(items, username: str, password: str) -> list:
    """items: three {'q': question, 'a': answer}. Returns what is wrong with them."""
    bad = []
    if not isinstance(items, list) or len(items) != 3:
        return ["Choose three security questions."]
    qs = [i.get("q") for i in items]
    if any(q not in QUESTIONS for q in qs):
        bad.append("Pick your questions from the list.")
    if len(set(qs)) != 3:
        bad.append("Choose three different questions.")
    answers = [norm_answer(i.get("a")) for i in items]
    if any(len(a) < 3 for a in answers):
        bad.append("Each answer needs at least 3 letters or numbers.")
    if len(set(answers)) != 3:
        bad.append("Give a different answer to each question.")
    name = norm_answer(username).replace(" ", "")
    pw = norm_answer(password).replace(" ", "")
    squashed = [a.replace(" ", "") for a in answers]
    if any(a and (a == name or (pw and (a in pw or pw in a))) for a in squashed):
        bad.append("An answer can't be your name or part of your password.")
    return bad


# -------------------------------------------------------------------- users
def clean_username(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not 3 <= len(name) <= 40 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._'\-]*", name):
        raise ValueError("A username is 3 to 40 letters, numbers or spaces (. _ ' - allowed).")
    return name


def is_owner_name(name: str) -> bool:
    return re.sub(r"\s+", " ", (name or "").strip()).casefold() == OWNER_NAME.casefold()


def user_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def owner_exists(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM users WHERE role='owner'").fetchone())


def create_user(conn, username: str, password: str, role: str = "member", questions=None, must_change: bool = False) -> int:
    username = clean_username(username)
    if role == "owner":
        if not is_owner_name(username):
            raise ValueError(f"The owner account is called {OWNER_NAME}.")
        username = OWNER_NAME
        if owner_exists(conn):
            raise ValueError("There is already an owner account.")
    elif is_owner_name(username):
        raise ValueError(f"The name {OWNER_NAME} belongs to the owner account.")
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise ValueError("That username is taken.")
    problems = password_problems(password, username)
    if problems:
        raise ValueError("Password too weak: " + " ".join(problems))
    if questions is not None:
        bad = question_problems(questions, username, password)
        if bad:
            raise ValueError(" ".join(bad))
    uid = conn.execute("INSERT INTO users(username, role, pw_hash, must_change, created_at) VALUES(?,?,?,?,?)",
                       (username, role, hash_secret(password), int(must_change), now())).lastrowid
    if questions is not None:
        _store_questions(conn, uid, questions)
    return uid


def _store_questions(conn, uid: int, items):
    conn.execute("DELETE FROM security_answers WHERE user_id=?", (uid,))
    for slot, i in enumerate(items):
        conn.execute("INSERT INTO security_answers(user_id, slot, question, answer_hash) VALUES(?,?,?,?)",
                     (uid, slot, i["q"], hash_secret(norm_answer(i["a"]))))


def get_user(conn, username: str):
    return conn.execute("SELECT * FROM users WHERE username=?", (re.sub(r"\s+", " ", (username or "").strip()),)).fetchone()


def has_questions(conn, uid: int) -> bool:
    return conn.execute("SELECT COUNT(*) FROM security_answers WHERE user_id=?", (uid,)).fetchone()[0] == 3


def public_user(conn, row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"], "active": bool(row["active"]),
            "must_change": bool(row["must_change"]), "needs_questions": not has_questions(conn, row["id"]),
            "last_login_at": row["last_login_at"], "created_at": row["created_at"]}


def _lock_message(until: int) -> str:
    mins = max(1, -(-(until - int(time.time())) // 60))
    return f"Too many wrong attempts. Try again in {mins} minute{'s' if mins != 1 else ''}."


def _lock_seconds(fails: int, base: int) -> int:
    return min(3600, base * 2 ** max(0, fails - MAX_FAILS))


def login(conn, username: str, password: str) -> str:
    """Check the password and return a new session token. One message for 'no such user' and 'wrong password'."""
    user = get_user(conn, username)
    if not user:
        check_secret(password or "", _dummy_hash())
        raise AuthError("Wrong username or password.")
    if user["locked_until"] > time.time():
        raise AuthError(_lock_message(user["locked_until"]), 429)
    if not check_secret(password or "", user["pw_hash"]):
        fails = user["failed"] + 1
        lock = int(time.time()) + _lock_seconds(fails, 60) if fails >= MAX_FAILS else 0
        conn.execute("UPDATE users SET failed=?, locked_until=? WHERE id=?", (fails, lock, user["id"]))
        conn.commit()   # the failed attempt must be remembered even though an error follows (an error rolls a change back)
        if lock:
            raise AuthError(_lock_message(lock), 429)
        left = MAX_FAILS - fails
        raise AuthError(f"Wrong username or password. {left} attempt{'s' if left != 1 else ''} left before a short lock-out.")
    if not user["active"]:
        raise AuthError("This account is switched off. Ask the owner to switch it on.", 403)
    if user["role"] != "owner":
        state = license_state(conn)
        if not state["ok"]:
            raise AuthError(state["message"], 403)
    conn.execute("UPDATE users SET failed=0, locked_until=0, last_login_at=? WHERE id=?", (now(), user["id"]))
    return new_session(conn, user["id"])


# ----------------------------------------------------------------- sessions
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(conn, uid: int) -> str:
    token = secrets.token_urlsafe(32)
    t = int(time.time())
    conn.execute("INSERT INTO sessions(token_hash, user_id, created_ts, seen_ts) VALUES(?,?,?,?)", (_hash_token(token), uid, t, t))
    conn.execute("DELETE FROM sessions WHERE seen_ts < ? OR created_ts < ?", (t - SESSION_IDLE, t - SESSION_MAX))
    return token


def session_user(conn, token):
    """The signed-in user for this token, or None when it is unknown, idle too long, or too old."""
    if not token:
        return None
    row = conn.execute("SELECT s.created_ts, s.seen_ts, s.user_id, u.active FROM sessions s JOIN users u ON u.id = s.user_id "
                       "WHERE s.token_hash=?", (_hash_token(token),)).fetchone()
    t = int(time.time())
    if not row or not row["active"] or t - row["seen_ts"] > SESSION_IDLE or t - row["created_ts"] > SESSION_MAX:
        if row:
            _bookkeeping(conn, "DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        return None
    if t - row["seen_ts"] > 60:
        _bookkeeping(conn, "UPDATE sessions SET seen_ts=? WHERE token_hash=?", (t, _hash_token(token)))
    return conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()


def _bookkeeping(conn, sql, args):
    """A housekeeping write on the sign-in check. If another part of the app has the database busy it is simply skipped (it is
    repeated on the next request): signing in must never wait behind an import or a lead search."""
    try:
        conn.execute(sql, args)
    except sqlite3.OperationalError:
        pass


def end_session(conn, token):
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def end_all_sessions(conn, uid: int, keep_token=None):
    keep = _hash_token(keep_token) if keep_token else ""
    conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (uid, keep))


# ------------------------------------------------------------ password changes
def change_password(conn, user, current: str, new: str, keep_token=None):
    if not check_secret(current or "", user["pw_hash"]):
        raise AuthError("Your current password is wrong.", 403)
    if current == new:
        raise ValueError("Choose a new password, not the one you have now.")
    problems = password_problems(new, user["username"])
    if problems:
        raise ValueError("Password too weak: " + " ".join(problems))
    conn.execute("UPDATE users SET pw_hash=?, must_change=0, failed=0, locked_until=0 WHERE id=?", (hash_secret(new), user["id"]))
    end_all_sessions(conn, user["id"], keep_token)


def change_questions(conn, user, current: str, items):
    if not check_secret(current or "", user["pw_hash"]):
        raise AuthError("Your current password is wrong.", 403)
    bad = question_problems(items, user["username"], current)
    if bad:
        raise ValueError(" ".join(bad))
    _store_questions(conn, user["id"], items)


# ------------------------------------------------------------------- recovery
def recovery_questions(conn, username: str) -> dict:
    """The user's three questions. An unknown name gets three believable ones, so nobody can use this to find names."""
    user = get_user(conn, username)
    if user and has_questions(conn, user["id"]):
        qs = [r["question"] for r in conn.execute("SELECT question FROM security_answers WHERE user_id=? ORDER BY slot", (user["id"],))]
        return {"questions": qs, "owner": user["role"] == "owner"}
    seed = int(hashlib.sha256((username or "").strip().casefold().encode()).hexdigest(), 16)
    return {"questions": [QUESTIONS[(seed + i * 3) % len(QUESTIONS)] for i in range(3)], "owner": is_owner_name(username)}


def recover(conn, username: str, answers, new_password: str, key_text: str = ""):
    """Reset a forgotten password. All three answers must be right (and, for the owner, the owner key presented)."""
    user = get_user(conn, username)
    generic = AuthError("Those answers don't match.", 403)
    if not user or not has_questions(conn, user["id"]) or not isinstance(answers, list) or len(answers) != 3:
        check_secret("x", _dummy_hash())
        raise generic
    if user["recover_locked_until"] > time.time():
        raise AuthError(_lock_message(user["recover_locked_until"]), 429)
    rows = conn.execute("SELECT * FROM security_answers WHERE user_id=? ORDER BY slot", (user["id"],)).fetchall()
    right = [check_secret(norm_answer(a), r["answer_hash"]) for a, r in zip(answers, rows)]
    key_ok = True
    if user["role"] == "owner" and owner_public():
        try:
            key_ok = owner_key_matches(parse_owner_key(key_text))
        except LicenseError:
            key_ok = False
    if not (all(right) and key_ok):
        fails = user["recover_failed"] + 1
        lock = int(time.time()) + _lock_seconds(fails, 900) if fails >= MAX_FAILS else 0
        conn.execute("UPDATE users SET recover_failed=?, recover_locked_until=? WHERE id=?", (fails, lock, user["id"]))
        conn.commit()   # as above: guessing is counted even though an error follows
        if lock:
            raise AuthError(_lock_message(lock), 429)
        if all(right) and not key_ok:
            raise AuthError("The owner key is wrong. The owner account needs the answers and the owner key.", 403)
        raise generic
    problems = password_problems(new_password, user["username"])
    if problems:
        raise ValueError("Password too weak: " + " ".join(problems))
    conn.execute("UPDATE users SET pw_hash=?, must_change=0, failed=0, locked_until=0, recover_failed=0, recover_locked_until=0 "
                 "WHERE id=?", (hash_secret(new_password), user["id"]))
    end_all_sessions(conn, user["id"])


# ------------------------------------------------------- owner key and licences
def owner_public():
    """The owner's public key built into this copy of the app, or None when it hasn't been set."""
    try:
        raw = bytes.fromhex((owner_key.OWNER_PUBLIC_KEY or "").strip())
    except ValueError:
        return None
    return raw if len(raw) == 32 else None


def parse_owner_key(text: str) -> bytes:
    m = re.fullmatch(r"DBI-OWNER-([A-Za-z0-9_\-]{43})", re.sub(r"\s+", "", text or ""))
    if not m:
        raise LicenseError("That isn't an owner key. It starts with DBI-OWNER- and is in your dericbi-owner.key file.")
    return _unb64(m.group(1))


def owner_key_matches(seed: bytes) -> bool:
    pub = owner_public()
    return bool(pub) and hmac.compare_digest(ed25519.public_key(seed), pub)


def make_owner_key():
    """(owner key text, public key hex) for scripts/make-owner-key.py."""
    seed = secrets.token_bytes(32)
    return "DBI-OWNER-" + _b64(seed), ed25519.public_key(seed).hex()


def install_id() -> str:
    """This copy's activation code: the owner signs a licence for exactly this code."""
    code = db.get_internal("install_id")
    if not code:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(12))
        code = "-".join(raw[i:i + 4] for i in range(0, 12, 4))
        db.set_internal("install_id", code)
    return code


def _norm_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def issue_license(seed: bytes, code: str, username: str, days: int) -> dict:
    if not owner_key_matches(seed):
        raise LicenseError("That owner key doesn't belong to this app. Use the key made for your installer.")
    norm = _norm_code(code)
    if len(norm) != 12:
        raise ValueError("The activation code is 12 letters and numbers, like ABCD-EFGH-JKLM.")
    username = clean_username(username)
    if is_owner_name(username):
        raise ValueError(f"The name {OWNER_NAME} belongs to the owner account.")
    if not 1 <= int(days) <= 365:
        raise ValueError("A licence lasts 1 to 365 days.")
    expires = (date.today() + timedelta(days=int(days))).isoformat()
    payload = json.dumps({"v": 1, "for": norm, "user": username, "role": "member", "issued": date.today().isoformat(),
                          "expires": expires}, separators=(",", ":"), sort_keys=True).encode()
    return {"license": "DBI1." + _b64(payload) + "." + _b64(ed25519.sign(seed, payload)), "expires": expires, "username": username}


def read_license(token: str, for_install: str = None) -> dict:
    """Check a licence's signature (made by the owner key), that it is for this computer, and that it hasn't run out."""
    pub = owner_public()
    if not pub:
        raise LicenseError("This copy of the app can't check licences yet (the owner key isn't set in the build).")
    try:
        tag, payload_b64, sig_b64 = re.sub(r"\s+", "", token or "").split(".")
        payload, sig = _unb64(payload_b64), _unb64(sig_b64)
        if tag != "DBI1":
            raise ValueError
        data = json.loads(payload)
    except Exception:
        raise LicenseError("That doesn't look like a licence. Paste the whole text the owner sent you.")
    if not ed25519.verify(pub, payload, sig):
        raise LicenseError("This licence wasn't signed by the owner, so it can't be used.")
    if data.get("for") != _norm_code(for_install or install_id()):
        raise LicenseError("This licence was made for a different computer. Send the owner the activation code shown on this screen.")
    if date.fromisoformat(data["expires"]) < date.today():
        raise LicenseError("This licence has expired. Ask the owner for a new one.")
    return data


def _clock_ok() -> bool:
    """A PC whose clock was wound back to stretch a licence is refused."""
    high = db.get_internal("clock_high") or 0
    t = int(time.time())
    if t < high - 86400:
        return False
    if t > high + 60:
        db.set_internal("clock_high", t)
    return True


def license_state(conn) -> dict:
    """Is this copy allowed to be used? An owner's copy always is; any other copy needs a current, valid licence."""
    if owner_exists(conn):
        return {"ok": True, "kind": "owner", "message": ""}
    token = db.get_internal("license")
    if not token:
        return {"ok": False, "kind": "licensed", "message": "This computer isn't licensed yet."}
    try:
        data = read_license(token)
    except LicenseError as e:
        return {"ok": False, "kind": "licensed", "message": str(e)}
    if not _clock_ok():
        return {"ok": False, "kind": "licensed", "message": "This computer's clock was set back. Fix the date and time to continue."}
    left = (date.fromisoformat(data["expires"]) - date.today()).days
    return {"ok": True, "kind": "licensed", "message": "", "expires": data["expires"], "days_left": left, "user": data["user"]}
