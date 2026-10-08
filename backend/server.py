"""FastAPI app: REST API + the static front end, bound to 127.0.0.1 only."""
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import ai
import branding
import campaigns as C
import config
import db
import jobs
import leads as L
import messaging
from messaging import SendError
from util import now, parent_alive, today_start
from version import VERSION


# ---------------------------------------------------------------------- models
class LeadIn(BaseModel):
    name: str
    sector: str = ""
    town: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    notes: str = ""


class ReplyIn(BaseModel):
    text: str
    channel: str = "sms"
    grade: Optional[str] = None


class WhatsAppIn(BaseModel):
    body: str


class WaActIn(BaseModel):
    action: str
    body: Optional[str] = None


class GmailIn(BaseModel):
    address: str
    app_password: str


class FollowupIn(BaseModel):
    days: Optional[int] = None
    channel: Optional[str] = None
    ai: bool = False


class AudienceIn(BaseModel):
    channel: str
    filters: dict = {}
    limit: int = 1000


class SendIn(BaseModel):
    channel: str
    subject: str = ""
    body: str


class BulkIn(BaseModel):
    ids: list[int]
    action: str
    value: Optional[str] = None


class ImportIn(BaseModel):
    csv: str


class PlanIn(BaseModel):
    goal: str
    history: list[dict] = []


class SearchIn(BaseModel):
    category: str
    town: str


class ScrapeIn(BaseModel):
    searches: list[SearchIn]
    max_per_search: int = Field(60, ge=1, le=300)
    headless: bool = True
    enrich: bool = False


class CampaignIn(BaseModel):
    name: str = ""
    channel: str
    subject: str = ""
    body: str
    filters: dict = {}
    launch: bool = False
    ai: bool = False   # body is a brief; the AI writes each lead's own message as it is sent
    followups: list = []   # days to wait before each follow-up to people who have not replied, e.g. [3, 4]
    style: str = ""        # "ready": a ready-made message written for each kind of business


class GoalIn(BaseModel):
    goal: str


class GradeIn(BaseModel):
    grade: str


class HandledIn(BaseModel):
    handled: bool = True


class TestSend(BaseModel):
    to: str


class UrlIn(BaseModel):
    url: str


class AIDraftIn(BaseModel):
    channel: str
    notes: str = ""


# ------------------------------------------------------------------------- app
def create_app(port: int = config.DEFAULT_PORT, parent_pid=None) -> FastAPI:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                       "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db()
        jobs.reconcile_stale()
        app.state.sender = C.Sender()
        app.state.poller = C.Poller()
        app.state.sender.start()
        app.state.poller.start()
        if parent_pid:
            def watch():
                while True:
                    time.sleep(4)
                    if not parent_alive(parent_pid):
                        os._exit(0)
            threading.Thread(target=watch, daemon=True).start()
        yield
        app.state.sender.stop()
        app.state.poller.stop()

    app = FastAPI(title="DericBI CRM", version=VERSION, lifespan=lifespan)

    # ---- security: only our own window may talk to this server
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in allowed_origins:
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
        resp = await call_next(request)
        if not request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(ValueError)
    async def _bad(_, e):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(KeyError)
    async def _missing(_, e):
        return JSONResponse({"detail": str(e).strip("'\"") or "Not found"}, status_code=404)

    @app.exception_handler(L.Conflict)
    async def _conflict(_, e):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(ai.AIError)
    async def _ai(_, e):
        return JSONResponse({"detail": str(e)}, status_code=502)

    @app.exception_handler(SendError)
    async def _send(_, e):
        return JSONResponse({"detail": str(e), "kind": e.kind}, status_code=502)

    # ------------------------------------------------------------------ basics
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION, "data_dir": str(config.DATA_DIR)}

    @app.get("/api/status")
    def status(request: Request):
        with db.db() as conn:
            job = jobs.active_job(conn)
            running = conn.execute("SELECT COUNT(*) FROM campaigns WHERE status='running'").fetchone()[0]
            paused = conn.execute("SELECT COUNT(*) FROM campaigns WHERE status='paused'").fetchone()[0]
            unreviewed = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE direction='in' AND handled=0 AND grade IN ('hot','warm','unclear')"
            ).fetchone()[0]
        s = db.get_settings()
        return {
            "sender": request.app.state.sender.status,
            "poller": request.app.state.poller.last,
            "job": job,
            "campaigns_running": running,
            "campaigns_paused": paused,
            "inbox_unhandled": unreviewed,
            "email_ready": C.channel_ready("email", s),
            "sms_ready": C.channel_ready("sms", s),
            "whatsapp_linked": bool(db.get_internal("whatsapp_linked")),
            "replies_ready": bool(s["imap_host"] and messaging.inbox_login(s)[0]),
        }

    @app.get("/api/dashboard")
    def dashboard():
        with db.db() as conn:
            q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
            total = q("SELECT COUNT(*) FROM leads WHERE archived=0")
            by_status = {r[0]: r[1] for r in conn.execute(
                "SELECT status, COUNT(*) FROM leads WHERE archived=0 GROUP BY status")}
            by_grade = {(r[0] or "none"): r[1] for r in conn.execute(
                "SELECT grade, COUNT(*) FROM leads WHERE archived=0 GROUP BY grade")}
            contacted = q("SELECT COUNT(DISTINCT lead_id) FROM messages WHERE direction='out' AND status='sent'")
            # "of contacted leads replied": only leads we actually sent to can count, so the rate never passes 100%
            replied = q("SELECT COUNT(DISTINCT lead_id) FROM messages WHERE direction='in' AND grade NOT IN ('auto','bounce') "
                        "AND lead_id IN (SELECT lead_id FROM messages WHERE direction='out' AND status='sent')")
            week = datetime.now().timestamp() - 7 * 86400
            week_s = datetime.fromtimestamp(week).isoformat(sep=" ", timespec="seconds")
            hot = [dict(r) for r in conn.execute(
                "SELECT id, name, town, sector, phone_norm, email, status, last_reply_at FROM leads "
                "WHERE archived=0 AND grade='hot' AND status NOT IN ('won','lost') "
                "ORDER BY last_reply_at DESC LIMIT 8")]
            due = [dict(r) for r in conn.execute(
                "SELECT id, name, town, next_followup, status FROM leads WHERE archived=0 AND next_followup != '' "
                "AND next_followup <= ? AND status NOT IN ('won','lost') ORDER BY next_followup LIMIT 8",
                (datetime.now().strftime("%Y-%m-%d"),))]
            activity = [dict(r) for r in conn.execute(
                "SELECT e.ts, e.kind, e.detail, l.id AS lead_id, l.name FROM events e JOIN leads l ON l.id=e.lead_id "
                "WHERE e.kind != 'enrich' ORDER BY e.id DESC LIMIT 12")]
            return {
                "total": total, "by_status": by_status, "by_grade": by_grade,
                "with_phone": q("SELECT COUNT(*) FROM leads WHERE archived=0 AND phone_norm IS NOT NULL"),
                "with_mobile": q("SELECT COUNT(*) FROM leads WHERE archived=0 AND is_mobile=1"),
                "with_email": q("SELECT COUNT(*) FROM leads WHERE archived=0 AND email IS NOT NULL"),
                "dnc": q("SELECT COUNT(*) FROM leads WHERE do_not_contact=1"),
                "archived": q("SELECT COUNT(*) FROM leads WHERE archived=1"),
                "sent_today": {ch: q("SELECT COUNT(*) FROM messages WHERE direction='out' AND channel=? AND status='sent' "
                                     "AND sent_at >= ?", ch, today_start()) for ch in ("email", "sms", "whatsapp")},
                "sent_week": q("SELECT COUNT(*) FROM messages WHERE direction='out' AND status='sent' AND sent_at >= ?", week_s),
                "replies_week": q("SELECT COUNT(*) FROM messages WHERE direction='in' AND grade NOT IN ('auto','bounce') "
                                  "AND created_at >= ?", week_s),
                "channels": {ch: {
                    "contacted": q("SELECT COUNT(DISTINCT lead_id) FROM messages WHERE direction='out' AND status='sent' "
                                   "AND channel=?", ch),
                    "replied": q("SELECT COUNT(DISTINCT i.lead_id) FROM messages i JOIN messages o ON o.id = i.reply_to "
                                 "WHERE i.direction='in' AND i.grade NOT IN ('auto','bounce') AND o.channel=?", ch)}
                    for ch in ("email", "sms", "whatsapp")},
                "contacted": contacted, "replied": replied,
                "reply_rate": round(100 * replied / contacted, 1) if contacted else 0,
                "won_value": q("SELECT COALESCE(SUM(deal_value), 0) FROM leads WHERE archived=0 AND status='won'"),
                "open_value": q("SELECT COALESCE(SUM(deal_value), 0) FROM leads WHERE archived=0 "
                                "AND status IN ('interested','meeting')"),
                "hot": hot, "due": due, "activity": activity,
                "campaigns": [c for c in C.campaign_stats(conn) if c["status"] in ("running", "paused")][:5],
            }

    @app.get("/api/today")
    def today():
        with db.db() as conn:
            return L.today_queue(conn)

    @app.get("/api/facets")
    def facets():
        with db.db() as conn:
            return L.facets(conn)

    # ------------------------------------------------------------------- leads
    @app.get("/api/leads")
    def list_leads(q: str = "", sector: str = "", town: str = "", status: str = "", grade: str = "",
                   has_phone: Optional[bool] = None, has_email: Optional[bool] = None,
                   archived: bool = False, dnc: Optional[bool] = None,
                   page: int = 1, page_size: int = 50, sort: str = "created_desc"):
        with db.db() as conn:
            rows, total = L.list_leads(conn, q, sector, town, status, grade, has_phone, has_email,
                                       archived, dnc, page, page_size, sort)
        return {"leads": rows, "total": total, "page": page, "page_size": page_size}

    @app.post("/api/leads")
    def add_lead(body: LeadIn):
        with db.db() as conn:
            res = L.add_lead(conn, body.model_dump(), source="manual", merge=False)
            if not res["created"]:
                dup = L.get_lead(conn, res["id"])
                return JSONResponse({"detail": f"Already in your CRM as “{dup['name']}” ({res['reason']}).",
                                     "existing_id": res["id"]}, status_code=409)
            return L.get_lead(conn, res["id"])

    @app.post("/api/leads/import")
    def import_leads(body: ImportIn):
        with db.db() as conn:
            return L.import_csv(conn, body.csv)

    @app.get("/api/leads/export")
    def export_leads(archived: bool = False):
        with db.db() as conn:
            data = L.export_csv(conn, include_archived=archived)
        name = f"dericbi-leads-{datetime.now():%Y%m%d}.csv"
        return Response(("﻿" + data).encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/leads/bulk")
    def bulk(body: BulkIn):
        if not body.ids:
            raise ValueError("No leads selected")
        with db.db() as conn:
            if body.action == "archive":
                L.set_archived(conn, body.ids, True)
            elif body.action == "restore":
                L.set_archived(conn, body.ids, False)
            elif body.action == "status":
                for i in body.ids:
                    L.update_lead(conn, i, {"status": body.value})
            elif body.action == "grade":
                for i in body.ids:
                    L.update_lead(conn, i, {"grade": body.value or ""})
            elif body.action == "dnc":
                for i in body.ids:
                    L.update_lead(conn, i, {"do_not_contact": body.value == "1"})
            else:
                raise ValueError("Unknown bulk action")
        return {"ok": True, "count": len(body.ids)}

    @app.get("/api/leads/{lead_id}")
    def get_lead(lead_id: int):
        with db.db() as conn:
            d = L.lead_detail(conn, lead_id)
            if d:
                d["outreach"] = C.outreach_state(conn, lead_id)   # whether they may be messaged by hand right now
        if not d:
            raise KeyError("Lead not found")
        return d

    @app.patch("/api/leads/{lead_id}")
    def patch_lead(lead_id: int, patch: dict):
        with db.db() as conn:
            return L.update_lead(conn, lead_id, patch)

    @app.post("/api/leads/{lead_id}/reply")
    def log_reply(lead_id: int, body: ReplyIn):
        if not body.text.strip():
            raise ValueError("Paste or type the reply text")
        if body.channel not in ("sms", "email", "whatsapp", "other"):
            raise ValueError("Unknown channel")
        with db.db() as conn:
            res = L.record_reply(conn, lead_id, body.channel, body.text.strip(), forced_grade=body.grade or None)
        return res

    @app.post("/api/leads/{lead_id}/whatsapp")
    def whatsapp_one(lead_id: int, body: WhatsAppIn):
        return C.whatsapp_one(lead_id, body.body)

    @app.post("/api/leads/{lead_id}/send")
    def send_one(lead_id: int, body: SendIn):
        return C.send_now(lead_id, body.channel, body.subject, body.body)

    # -------------------------------------------------------------------- jobs
    @app.post("/api/jobs/scrape")
    def start_scrape(body: ScrapeIn):
        searches = []
        for s in body.searches:
            cat, town = s.category.strip(), s.town.strip()
            if cat and town:
                searches.append({"category": cat, "town": town, "query": f"{cat} in {town}"})
        if not searches:
            raise ValueError("Pick at least one category and town")
        if len(searches) > 500:
            raise ValueError("That's a lot of searches at once - keep it under 500 per run")
        with db.db() as conn:
            if jobs.active_job(conn):
                return JSONResponse({"detail": "A job is already running. Stop it or wait for it to finish."}, status_code=409)
            jid = jobs.create_job(conn, "scrape", {"searches": searches, "max_per_search": body.max_per_search,
                                                   "headless": body.headless, "enrich": body.enrich})
        jobs.spawn(jid, "scrape")
        return {"id": jid}

    @app.post("/api/jobs/enrich")
    def start_enrich():
        with db.db() as conn:
            if jobs.active_job(conn):
                return JSONResponse({"detail": "A job is already running."}, status_code=409)
            jid = jobs.create_job(conn, "enrich", {"limit": 300})
        jobs.spawn(jid, "enrich")
        return {"id": jid}

    @app.get("/api/jobs")
    def list_jobs():
        with db.db() as conn:
            rows = conn.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 15").fetchall()
            return [jobs.get_job(conn, r["id"]) for r in rows]

    @app.get("/api/jobs/{job_id}/logs")
    def job_logs(job_id: int, after: int = 0):
        with db.db() as conn:
            job = jobs.get_job(conn, job_id)
            if not job:
                raise KeyError("Job not found")
            logs = [dict(r) for r in conn.execute(
                "SELECT id, ts, msg FROM job_logs WHERE job_id=? AND id>? ORDER BY id LIMIT 500", (job_id, after))]
        return {"job": job, "logs": logs}

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: int):
        with db.db() as conn:
            job = jobs.get_job(conn, job_id)
        if not job:
            raise KeyError("Job not found")
        if job["status"] not in ("queued", "running"):
            raise ValueError("That job isn't running")
        jobs.request_stop(job_id)
        return {"ok": True}

    # --------------------------------------------------------------- campaigns
    @app.get("/api/campaigns")
    def list_campaigns():
        with db.db() as conn:
            return C.campaign_stats(conn)

    @app.post("/api/campaigns/preview")
    def preview_campaign(body: CampaignIn):
        with db.db() as conn:
            return C.preview(conn, body.channel, body.subject, body.body, body.filters, body.ai, body.style)

    @app.post("/api/campaigns/ai-samples")
    def campaign_ai_samples(body: CampaignIn):
        with db.db() as conn:
            return C.ai_samples(conn, body.channel, body.body, body.filters)

    @app.post("/api/campaigns/ai-plan")
    def campaign_ai_plan(body: GoalIn):
        with db.db() as conn:
            facets = L.facets(conn)
        return ai.plan_campaign(db.get_settings(), body.goal, facets)

    @app.post("/api/campaigns")
    def create_campaign(body: CampaignIn):
        with db.db() as conn:
            cid = C.create_campaign(conn, body.name, body.channel, body.subject, body.body, body.filters, body.launch, body.ai,
                                  body.followups, body.style)
            return C.campaign_stats(conn, cid)

    @app.get("/api/campaigns/reach")
    def campaign_reach():
        with db.db() as conn:
            return C.reach_counts(conn)

    @app.post("/api/campaigns/audience")
    def campaign_audience(body: AudienceIn):
        if body.channel not in C.CHANNELS:
            raise ValueError("Channel must be email, text or WhatsApp")
        with db.db() as conn:
            return C.audience_list(conn, body.channel, body.filters, min(max(body.limit, 1), 1000))

    @app.get("/api/campaigns/{cid}")
    def get_campaign(cid: int):
        with db.db() as conn:
            c = C.campaign_stats(conn, cid)
            if not c:
                raise KeyError("Campaign not found")
            msgs = [dict(r) for r in conn.execute(
                "SELECT m.id, m.lead_id, l.name, m.to_addr, m.subject, m.body, m.status, m.error, m.sent_at, m.attempts "
                "FROM messages m JOIN leads l ON l.id=m.lead_id WHERE m.campaign_id=? AND m.direction='out' "
                "ORDER BY m.id LIMIT 1000", (cid,))]
        c["messages"] = msgs
        return c

    @app.get("/api/campaigns/{cid}/whatsapp/next")
    def whatsapp_next(cid: int):
        return {"item": C.wa_next(cid)}

    @app.post("/api/messages/{mid}/whatsapp")
    def whatsapp_act(mid: int, body: WaActIn):
        return C.wa_act(mid, body.action, body.body)

    @app.post("/api/campaigns/{cid}/followup")
    def campaign_followup(cid: int, body: FollowupIn):
        with db.db() as conn:
            C.create_followup(conn, cid, body.days, body.channel, body.ai)
            return C.campaign_stats(conn, cid)

    @app.post("/api/campaigns/{cid}/{action}")
    def campaign_action(cid: int, action: str):
        with db.db() as conn:
            if action == "launch":
                C.launch_campaign(conn, cid)
            elif action in ("pause", "resume", "cancel"):
                C.set_campaign_status(conn, cid, action)
            elif action == "retry":
                n = conn.execute(
                    "UPDATE messages SET status='queued', error='', attempts=0, scheduled_at=? "
                    "WHERE campaign_id=? AND direction='out' AND status='failed' "
                    "AND error NOT LIKE 'Recipient refused%' AND error NOT LIKE 'Interrupted%'", (now(), cid)).rowcount
                if n:
                    conn.execute("UPDATE campaigns SET status='running', finished_at=NULL, last_error='' "
                                 "WHERE id=? AND status='done'", (cid,))
            else:
                raise ValueError("Unknown action")
            return C.campaign_stats(conn, cid)

    # ------------------------------------------------------------------- inbox
    @app.get("/api/inbox")
    def inbox(grade: str = "", handled: Optional[bool] = None, limit: int = 200):
        where, args = ["m.direction='in'"], []
        if grade:
            where.append("m.grade=?"); args.append(grade)
        if handled is not None:
            where.append("m.handled=?"); args.append(1 if handled else 0)
        with db.db() as conn:
            rows = conn.execute(
                "SELECT m.id, m.lead_id, m.channel, m.subject, m.body, m.grade, m.score, m.reasons, m.handled, "
                "m.created_at, l.name, l.town, l.sector, l.status, l.phone_norm, l.email, l.do_not_contact, "
                "c.name AS campaign_name FROM messages m JOIN leads l ON l.id=m.lead_id "
                "LEFT JOIN campaigns c ON c.id=m.campaign_id "
                f"WHERE {' AND '.join(where)} ORDER BY m.id DESC LIMIT ?", args + [min(limit, 500)]).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/inbox/check")
    def check_inbox(request: Request):
        return request.app.state.poller.poll_now()

    @app.post("/api/messages/{mid}/regrade")
    def regrade(mid: int, body: GradeIn):
        with db.db() as conn:
            L.regrade_message(conn, mid, body.grade)
        return {"ok": True}

    @app.post("/api/messages/{mid}/handled")
    def handled(mid: int, body: HandledIn):
        with db.db() as conn:
            conn.execute("UPDATE messages SET handled=? WHERE id=? AND direction='in'", (1 if body.handled else 0, mid))
        return {"ok": True}

    # ---------------------------------------------------------------- settings
    def masked(s):
        out = dict(s)
        for k in db.SECRET_KEYS:
            out[k + "_set"] = bool(out.get(k))
            out[k] = ""
        out["ai_defaults"] = ai.default_models()   # shown as the grey hint in the model box
        return out

    @app.get("/api/settings")
    def get_settings():
        return masked(db.get_settings())

    @app.put("/api/settings")
    def put_settings(patch: dict, request: Request):
        clean = {}
        for k, v in patch.items():
            if k not in db.SETTING_DEFAULTS:
                continue
            default = db.SETTING_DEFAULTS[k]
            if k in db.SECRET_KEYS and v in ("", None):
                continue  # blank = leave unchanged
            if k in ("smtp_pass", "imap_pass", "sms_gateway_pass"):
                clean[k] = messaging.clean_secret(v)   # app passwords are copied with spaces; none belong in them
                continue
            if isinstance(default, bool):
                v = bool(v)
            elif isinstance(default, int):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{k} must be a number")
                if v < 0:
                    raise ValueError(f"{k} can't be negative")
            else:
                v = messaging.clean_text(v)
                if k == "optout_suffix_sms" and v:
                    v = " " + v   # it is glued to the end of the text, so it needs its space (stripping removed it)
            clean[k] = v
        for k in ("send_window_start", "send_window_end"):
            if k in clean:
                try:
                    datetime.strptime(clean[k], "%H:%M")
                except ValueError:
                    raise ValueError("Sending hours must look like 08:00")
        if "sms_delay_sec" in clean and clean["sms_delay_sec"] < 30:
            raise ValueError("Wait at least 30 seconds between texts. Android stops an app that sends faster than about "
                             "30 texts in 30 minutes.")
        if "whatsapp_delay_sec" in clean and clean["whatsapp_delay_sec"] < 20:
            raise ValueError("Wait at least 20 seconds between WhatsApp messages. Faster than that gets numbers banned.")
        if "sms_sim" in clean and clean["sms_sim"] > 3:
            raise ValueError("SIM number must be 0 (phone's default), 1, 2 or 3")
        if "followup_wait_days" in clean and not 0 <= clean["followup_wait_days"] <= 30:
            raise ValueError("Follow-up wait must be between 0 and 30 days")
        if clean.get("contact_website") and not clean["contact_website"].startswith(("http://", "https://")):
            raise ValueError("Your website address must start with https://")
        if clean.get("smtp_security") not in (None, "starttls", "ssl", "none"):
            raise ValueError("Email security must be starttls, ssl or none")
        if clean.get("imap_security") not in (None, "ssl", "starttls", "none"):
            raise ValueError("Reply-inbox security must be ssl, starttls or none")
        if clean.get("ai_provider", "gemini") not in ai.PROVIDERS:
            raise ValueError("Unknown AI provider")
        if clean.get("ai_custom_url") and not clean["ai_custom_url"].startswith(("http://", "https://")):
            raise ValueError("The AI server address must start with http:// or https://")
        if len(clean.get("slogan", "")) > 80:
            raise ValueError("The slogan is too long (80 characters at most)")
        # Fill the obvious blanks: the sender address is normally the login, and so is the inbox login.
        merged = {**db.get_settings(), **clean}
        if not merged["from_email"] and "@" in merged["smtp_user"]:
            clean["from_email"] = merged["smtp_user"]
        if not merged["imap_user"] and merged["imap_host"] and merged["smtp_user"]:
            clean["imap_user"] = merged["smtp_user"]
        db.save_settings(clean)
        s = db.get_settings()
        if s["imap_host"] and s["imap_user"] and db.get_internal("imap_last_uid") is None:
            threading.Thread(target=lambda: _safe(request.app.state.poller.poll_now), daemon=True).start()
        return masked(s)

    @app.get("/api/branding/logo")
    def get_logo():
        path, mime = branding.current()
        if not path.is_file():
            raise KeyError("No logo")   # the page hides the image; a missing logo never breaks the app
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-cache"})

    @app.put("/api/branding/logo")
    async def put_logo(request: Request):
        branding.save(await request.body())
        return {"ok": True}

    @app.delete("/api/branding/logo")
    def delete_logo():
        branding.reset()
        return {"ok": True}

    @app.post("/api/ai/plan-searches")
    def ai_plan_searches(body: PlanIn):
        return ai.plan_searches(db.get_settings(), body.goal, body.history)

    @app.post("/api/ai/template")
    def ai_template(body: AIDraftIn):
        return ai.draft_template(db.get_settings(), body.channel, body.notes)

    @app.post("/api/leads/{lead_id}/ai-draft")
    def ai_draft(lead_id: int, body: AIDraftIn):
        with db.db() as conn:
            lead = L.lead_detail(conn, lead_id)
        if not lead:
            raise KeyError("Lead not found")
        s = db.get_settings()
        room = messaging.SMS_LIMIT - len(messaging.final_body("sms", "", s)) if body.channel == "sms" else 0
        return ai.draft_for_lead(s, lead, body.channel, body.notes, limit=room)

    @app.post("/api/leads/{lead_id}/ai-summary")
    def ai_summary(lead_id: int):
        with db.db() as conn:
            lead = L.lead_detail(conn, lead_id)
        if not lead:
            raise KeyError("Lead not found")
        return {"summary": ai.summarize(db.get_settings(), lead)}

    @app.post("/api/settings/test-ai")
    def test_ai():
        results = ai.check(db.get_settings())
        if not results:
            raise ValueError("Add an AI key first")
        return {"ok": all(r[1] for r in results),
                "message": ". ".join(f"{label}: {detail}" for label, _, detail in results)}

    @app.post("/api/settings/test-email")
    def test_email(body: TestSend):
        s = db.get_settings()
        to = body.to.strip()
        res = messaging.diagnose_email(s)
        if res["ok"]:
            try:
                messaging.send_email(to, "DericBI CRM test email",
                                     "If you're reading this, your email settings work.\n\nSent from DericBI CRM.", s)
                res["steps"].append({"label": "Send", "ok": True, "detail": f"sent to {to}"})
                res["message"] = f"Test email sent to {to}. If you don't see it, check Spam."
            except SendError as e:
                res["steps"].append({"label": "Send", "ok": False, "detail": str(e)})
                res.update(ok=False, message=f"Send: {e}")
        return res

    @app.post("/api/settings/connect-gmail")
    def connect_gmail(body: GmailIn):
        """Everything Gmail needs from two boxes: sending and reading replies are both set up and then both tested."""
        address = messaging.clean_text(body.address).lower()
        password = messaging.clean_secret(body.app_password)
        if "@" not in address or " " in address:
            raise ValueError("Enter your full Gmail address, like you@gmail.com")
        if len(password) < 8:
            raise ValueError("Paste the app password Google gave you (16 letters). Your normal password won't work.")
        db.save_settings({"smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_security": "starttls", "smtp_user": address,
                          "from_email": address, "smtp_pass": password, "imap_host": "imap.gmail.com", "imap_port": 993,
                          "imap_security": "ssl", "imap_user": address, "imap_pass": password})
        s = db.get_settings()
        sending, reading = messaging.diagnose_email(s), messaging.test_imap(s)
        msg = ("Gmail is connected: sending and reply tracking both work." if sending["ok"] and reading["ok"]
               else ("Sending works, but reading replies didn't: " + reading["message"]) if sending["ok"]
               else "Gmail didn't accept it: " + sending["message"])
        return {"ok": sending["ok"] and reading["ok"], "message": msg, "sending": sending, "reading": reading}

    @app.post("/api/settings/check-phone")
    def check_phone():
        return messaging.diagnose_sms(db.get_settings())

    @app.post("/api/settings/test-sms")
    def test_sms(body: TestSend):
        from util import normalize_phone
        norm, mob = normalize_phone(body.to)
        if not norm or not mob:
            raise ValueError("Enter a Kenyan mobile number like 0712 345 678")
        s = db.get_settings()
        res = messaging.diagnose_sms(s)
        if res["ok"]:
            try:
                messaging.send_sms(norm, "DericBI CRM test: your phone is connected and can send texts.", s)
                res["steps"].append({"label": "Send", "ok": True, "detail": f"the phone sent a text to {norm}"})
                res["message"] = f"Test text sent to {norm}."
            except SendError as e:
                res["steps"].append({"label": "Send", "ok": False, "detail": str(e)})
                res.update(ok=False, message=f"Send: {e}")
        return res

    # ---------------------------------------------------------------- WhatsApp
    @app.get("/api/whatsapp/status")
    def whatsapp_status():
        with db.db() as conn:
            link, worker = jobs.active_job(conn, "whatsapp_link"), jobs.active_job(conn, "whatsapp")
            last = conn.execute("SELECT id FROM jobs WHERE kind='whatsapp_link' ORDER BY id DESC LIMIT 1").fetchone()
            logs = ([r["msg"] for r in conn.execute("SELECT msg FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT 4",
                                                    (last["id"],))][::-1] if last else [])
        return {"linked": bool(db.get_internal("whatsapp_linked")), "linked_at": db.get_internal("whatsapp_linked_at"),
                "linking": bool(link), "sending": bool(worker), "link_log": logs}

    @app.post("/api/whatsapp/link")
    def whatsapp_link():
        with db.db() as conn:
            if jobs.active_job(conn, "whatsapp") or jobs.active_job(conn, "whatsapp_link"):
                raise ValueError("WhatsApp is busy right now. Wait a moment, or pause your WhatsApp campaigns first.")
            jid = jobs.create_job(conn, "whatsapp_link", {})
        jobs.spawn(jid, "whatsapp_link")
        return {"job": jid}

    @app.post("/api/whatsapp/unlink")
    def whatsapp_unlink():
        import shutil
        import whatsapp
        with db.db() as conn:
            worker = jobs.active_job(conn, "whatsapp")
        if worker:
            jobs.request_stop(worker["id"])
        whatsapp.set_linked(False)
        shutil.rmtree(whatsapp.profile_dir(), ignore_errors=True)   # forget the login completely
        return {"linked": False}

    @app.post("/api/settings/test-imap")
    def test_imap():
        return messaging.test_imap(db.get_settings())

    @app.post("/api/open-url")
    def open_url(body: UrlIn):
        """Open a web page in the user's normal browser (the app window itself never navigates away)."""
        import webbrowser
        from urllib.parse import urlparse
        u = urlparse(body.url.strip())
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError("Only web addresses can be opened")
        webbrowser.open(body.url.strip())
        return {"ok": True}

    # ----------------------------------------------------------- backup / hooks
    @app.get("/api/backup")
    def backup():
        fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        src = db.connect()
        try:
            dst = sqlite3.connect(tmp)
            src.backup(dst)
            dst.close()
        finally:
            src.close()
        name = f"dericbi-crm-backup-{datetime.now():%Y%m%d-%H%M}.sqlite3"
        return FileResponse(tmp, filename=name, media_type="application/octet-stream",
                            background=_Cleanup(tmp))

    # ---------------------------------------------------------------- front end
    static = config.static_dir()
    if static.exists():
        app.mount("/", StaticFiles(directory=str(static), html=True), name="static")
    return app


def _safe(fn):
    try:
        fn()
    except Exception:
        pass


class _Cleanup:
    """Starlette background task that removes the temp backup file after sending."""
    def __init__(self, path):
        self.path = path

    async def __call__(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
