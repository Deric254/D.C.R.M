"""The sign-in gate and the account, user and licence routes."""
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import auth
import db

# Open without signing in: the sign-in routes themselves, the launcher's health check, and the logo on the sign-in screen.
PUBLIC = ("/api/auth/", "/api/health", "/api/branding/logo")
_license_cache = {"t": 0.0, "state": None}


class SetupIn(BaseModel):
    mode: str                       # "owner" (the mother account) or "member" (a licensed computer)
    username: str = ""
    password: str
    questions: list
    owner_key: str = ""
    license: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordCheckIn(BaseModel):
    password: str = ""
    username: str = ""


class RecoverQIn(BaseModel):
    username: str


class RecoverIn(BaseModel):
    username: str
    answers: list
    new_password: str
    owner_key: str = ""


class ChangePasswordIn(BaseModel):
    current: str
    new: str


class ChangeQuestionsIn(BaseModel):
    current: str
    questions: list


class RenewIn(BaseModel):
    license: str


class NewUserIn(BaseModel):
    username: str


class ActiveIn(BaseModel):
    active: bool


class IssueIn(BaseModel):
    code: str
    username: str
    days: int = 90
    owner_key: str


def _licence_state(conn):
    """The licence check, remembered for a minute because it verifies a signature."""
    if time.time() - _license_cache["t"] > 60 or _license_cache["state"] is None:
        _license_cache.update(t=time.time(), state=auth.license_state(conn))
    return _license_cache["state"]


def install(app):
    def need_user(request: Request):
        if not request.state.user:
            raise auth.AuthError("Please sign in.", 401)
        return request.state.user

    def need_owner(request: Request):
        user = need_user(request)
        if user["role"] != "owner":
            raise auth.AuthError("Only the owner account can do that.", 403)
        return user

    def set_cookie(resp, token):
        resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_MAX, httponly=True, samesite="strict", path="/")

    @app.exception_handler(auth.AuthError)
    async def _auth_error(_, e):
        return JSONResponse({"detail": str(e), "auth": "login" if e.status == 401 else "denied"}, status_code=e.status)

    def _identify(token):
        """Who is signed in. This runs in a worker thread, never in the event loop (a wait for the database in the event loop
        freezes EVERY screen), and with a short wait: its writes are housekeeping that can be skipped."""
        with db.db() as conn:
            conn.execute("PRAGMA busy_timeout=300")
            user = auth.session_user(conn, token)
            if user and user["role"] != "owner" and not _licence_state(conn)["ok"]:
                user = None   # the licence ran out while signed in
            return user

    def _unfinished(user):
        with db.db() as conn:
            conn.execute("PRAGMA busy_timeout=300")
            return user["must_change"] or not auth.has_questions(conn, user["id"])

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        request.state.user = None
        token = request.cookies.get(auth.COOKIE)
        if token:
            request.state.user = await run_in_threadpool(_identify, token)
        if path.startswith("/api/") and not path.startswith(PUBLIC):
            user = request.state.user
            if not user:
                return JSONResponse({"detail": "Please sign in.", "auth": "login"}, status_code=401)
            unfinished = await run_in_threadpool(_unfinished, user)
            if unfinished:
                return JSONResponse({"detail": "Finish setting up your account first.", "auth": "finish"}, status_code=403)
        return await call_next(request)

    # ------------------------------------------------------------- state
    def describe(request: Request) -> dict:
        with db.db() as conn:
            n = auth.user_count(conn)
            owner = auth.owner_exists(conn)
            user = request.state.user
            out = {"state": "ok" if user else ("login" if n else "setup"), "owner_name": auth.OWNER_NAME,
                   "install_id": auth.install_id(), "licensing_configured": bool(auth.owner_public()),
                   "kind": "owner" if owner else ("licensed" if n else "new"), "questions": list(auth.QUESTIONS),
                   "user": auth.public_user(conn, user) if user else None}
            if n and not owner:
                out["license"] = auth.license_state(conn)
        return out

    @app.get("/api/auth/status")
    def status(request: Request):
        return describe(request)

    @app.post("/api/auth/password-check")
    def password_check(body: PasswordCheckIn):
        return {"problems": auth.password_problems(body.password, body.username)}

    # ------------------------------------------------------ first account
    @app.post("/api/auth/setup")
    def setup(body: SetupIn, request: Request):
        with db.db() as conn:
            if auth.user_count(conn):
                raise auth.AuthError("This app already has an account. Sign in instead.", 409)
            warning = ""
            if body.mode == "owner":
                if not auth.is_owner_name(body.username):
                    raise ValueError(f"The first account is the owner account, called {auth.OWNER_NAME}.")
                if auth.owner_public():
                    if not auth.owner_key_matches(auth.parse_owner_key(body.owner_key)):
                        raise ValueError("That owner key doesn't belong to this app, so it can't create the owner account.")
                else:
                    warning = ("The owner key isn't set in this build, so other computers can't be licensed yet. "
                               "Run scripts/make-owner-key.py and paste the public key into backend/owner_key.py before you build.")
                role, username = "owner", auth.OWNER_NAME
            elif body.mode == "member":
                data = auth.read_license(body.license)
                role, username = "member", data["user"]
            else:
                raise ValueError("Choose owner or licensed computer.")
            uid = auth.create_user(conn, username, body.password, role, body.questions)
            token = auth.new_session(conn, uid)
            conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (auth.now(), uid))
        if body.mode == "member":
            db.set_internal("license", body.license.strip())   # kept only once the account really exists
        _license_cache["state"] = None
        resp = JSONResponse({"ok": True, "warning": warning})
        set_cookie(resp, token)
        return resp

    # ------------------------------------------------------- sign in / out
    @app.post("/api/auth/login")
    def login(body: LoginIn):
        try:
            with db.db() as conn:
                token = auth.login(conn, body.username, body.password)
        except auth.AuthError:
            time.sleep(0.4)   # slows down guessing
            raise
        _license_cache["state"] = None
        resp = JSONResponse({"ok": True})
        set_cookie(resp, token)
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request):
        with db.db() as conn:
            auth.end_session(conn, request.cookies.get(auth.COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    # ----------------------------------------------------- forgotten password
    @app.post("/api/auth/recover-questions")
    def recover_questions(body: RecoverQIn):
        with db.db() as conn:
            return auth.recovery_questions(conn, body.username)

    @app.post("/api/auth/recover")
    def recover(body: RecoverIn):
        try:
            with db.db() as conn:
                auth.recover(conn, body.username, body.answers, body.new_password, body.owner_key)
        except auth.AuthError:
            time.sleep(0.4)
            raise
        return {"ok": True}

    # --------------------------------------------------- the signed-in user
    @app.post("/api/auth/change-password")
    def change_password(body: ChangePasswordIn, request: Request):
        user = need_user(request)
        with db.db() as conn:
            auth.change_password(conn, user, body.current, body.new, request.cookies.get(auth.COOKIE))
        return {"ok": True}

    @app.post("/api/auth/change-questions")
    def change_questions(body: ChangeQuestionsIn, request: Request):
        user = need_user(request)
        with db.db() as conn:
            auth.change_questions(conn, user, body.current, body.questions)
        return {"ok": True}

    @app.post("/api/auth/renew-license")
    def renew_license(body: RenewIn):
        """For a licensed computer whose licence ran out: the owner's new licence for the same person."""
        with db.db() as conn:
            if auth.owner_exists(conn) or not auth.user_count(conn):
                raise ValueError("This computer doesn't need a licence.")
            data = auth.read_license(body.license)
            if not auth.get_user(conn, data["user"]):
                raise ValueError("This licence is for a different person than the one who uses this computer.")
        db.set_internal("license", body.license.strip())
        _license_cache["state"] = None
        return {"ok": True, "expires": data["expires"]}

    # ---------------------------------------------- the owner: people and computers
    @app.get("/api/users")
    def list_users(request: Request):
        need_owner(request)
        with db.db() as conn:
            return [auth.public_user(conn, r) for r in conn.execute("SELECT * FROM users ORDER BY role='owner' DESC, username")]

    @app.post("/api/users")
    def add_user(body: NewUserIn, request: Request):
        need_owner(request)
        temp = auth.strong_password()
        with db.db() as conn:
            uid = auth.create_user(conn, body.username, temp, "member", None, must_change=True)
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            return {"user": auth.public_user(conn, row), "temporary_password": temp}

    def target(conn, uid: int, owner):
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise KeyError("Person not found")
        if row["id"] == owner["id"] or row["role"] == "owner":
            raise ValueError("The owner account can't be changed here. Use Your account to change your own password.")
        return row

    @app.post("/api/users/{uid}/reset")
    def reset_user(uid: int, request: Request):
        owner = need_owner(request)
        temp = auth.strong_password()
        with db.db() as conn:
            row = target(conn, uid, owner)
            conn.execute("UPDATE users SET pw_hash=?, must_change=1, failed=0, locked_until=0, recover_failed=0, "
                         "recover_locked_until=0 WHERE id=?", (auth.hash_secret(temp), row["id"]))
            auth.end_all_sessions(conn, row["id"])
        return {"temporary_password": temp}

    @app.post("/api/users/{uid}/active")
    def set_active(uid: int, body: ActiveIn, request: Request):
        owner = need_owner(request)
        with db.db() as conn:
            row = target(conn, uid, owner)
            conn.execute("UPDATE users SET active=? WHERE id=?", (int(body.active), row["id"]))
            if not body.active:
                auth.end_all_sessions(conn, row["id"])
        return {"ok": True}

    @app.delete("/api/users/{uid}")
    def delete_user(uid: int, request: Request):
        owner = need_owner(request)
        with db.db() as conn:
            row = target(conn, uid, owner)
            conn.execute("DELETE FROM users WHERE id=?", (row["id"],))
        return {"ok": True}

    @app.post("/api/license/issue")
    def issue_license(body: IssueIn, request: Request):
        need_owner(request)
        return auth.issue_license(auth.parse_owner_key(body.owner_key), body.code, body.username, body.days)
