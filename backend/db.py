"""SQLite schema, connections and settings storage.

The DB is shared by the API server and the scraper worker process, so it runs in
WAL mode with a generous busy timeout. Leads are never hard-deleted (only
archived) which is what guarantees a lead can never be re-imported as new.
"""
import json
import secrets
import time
import sqlite3
from contextlib import contextmanager

import config
from util import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  sector TEXT NOT NULL DEFAULT '',
  town TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  phone_norm TEXT,
  is_mobile INTEGER NOT NULL DEFAULT 0,
  email TEXT,
  website TEXT NOT NULL DEFAULT '',
  maps_url TEXT NOT NULL DEFAULT '',
  place_key TEXT,
  name_key TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',
  status TEXT NOT NULL DEFAULT 'new',
  grade TEXT NOT NULL DEFAULT '',
  grade_locked INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  do_not_contact INTEGER NOT NULL DEFAULT 0,
  email_bounced INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  next_followup TEXT NOT NULL DEFAULT '',
  offer TEXT NOT NULL DEFAULT '',            -- which of your services or tools you are offering them
  deal_value INTEGER NOT NULL DEFAULT 0,     -- what the deal is worth (KES)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_contacted_at TEXT,
  last_reply_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_place ON leads(place_key) WHERE place_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_phone ON leads(phone_norm) WHERE phone_norm IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_email ON leads(email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_leads_name_key ON leads(name_key);
CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS ix_leads_archived_grade ON leads(archived, grade);
CREATE INDEX IF NOT EXISTS ix_leads_followup ON leads(next_followup) WHERE next_followup != '';

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  channel TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  filters TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'draft',
  ai_personalize INTEGER NOT NULL DEFAULT 0,   -- 1: body is a brief and the AI writes each lead's message as it is sent
  followup_of INTEGER,                         -- a follow-up: sent to leads of this campaign who have not replied
  followup_days INTEGER NOT NULL DEFAULT 0,    -- ... once this many days have passed since their last message
  total INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  launched_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  campaign_id INTEGER REFERENCES campaigns(id),
  direction TEXT NOT NULL,                 -- 'out' | 'in'
  channel TEXT NOT NULL,                   -- 'email' | 'sms' | 'whatsapp' | 'other'
  to_addr TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,                    -- queued|sending|sent|failed|skipped|received
  error TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  provider_id TEXT NOT NULL DEFAULT '',
  message_id_header TEXT,
  grade TEXT NOT NULL DEFAULT '',          -- inbound only
  score INTEGER NOT NULL DEFAULT 0,
  reasons TEXT NOT NULL DEFAULT '',
  handled INTEGER NOT NULL DEFAULT 0,
  reply_to INTEGER,                        -- inbound only: the message of ours this answers
  scheduled_at TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_campaign_lead ON messages(campaign_id, lead_id)
  WHERE direction = 'out' AND campaign_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_inbound_id ON messages(message_id_header)
  WHERE direction = 'in' AND message_id_header IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_msg_lead ON messages(lead_id);
CREATE INDEX IF NOT EXISTS ix_msg_queue ON messages(status, channel);
-- the top bar asks "any unreviewed replies?" every few seconds, and the dashboard counts sent mail
CREATE INDEX IF NOT EXISTS ix_msg_inbox ON messages(direction, handled, grade);
CREATE INDEX IF NOT EXISTS ix_msg_sent ON messages(direction, status, sent_at);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_events_lead ON events(lead_id);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                      -- scrape | enrich
  params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|stopped|failed
  pid INTEGER,
  stop_requested INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  added INTEGER NOT NULL DEFAULT 0,
  duplicates INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  progress TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS job_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  ts TEXT NOT NULL,
  msg TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_job_logs ON job_logs(job_id, id);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

DEFAULT_PITCH = (
    "Deric Marangu is a data analyst. He helps local businesses use their own sales and stock records to see "
    "what is selling, what is not, and where money is being lost, so they can decide with facts. He also has "
    "ready-made tools that he customises to each business."
)

SETTING_DEFAULTS = {
    # identity
    "sender_name": "Deric Marangu",
    "slogan": "",
    # outgoing email
    "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",  # starttls | ssl | none
    "smtp_user": "", "smtp_pass": "", "from_email": "", "reply_to": "",
    # incoming email (reply tracking)
    "imap_host": "", "imap_port": 993, "imap_security": "ssl",
    "imap_user": "", "imap_pass": "", "imap_poll_minutes": 5,
    # SMS (Africa's Talking)
    "at_username": "", "at_api_key": "", "at_sender_id": "",
    "at_base_url": "https://api.africastalking.com",
    # sending limits
    "email_daily_cap": 80, "sms_daily_cap": 150,
    "email_delay_sec": 25, "sms_delay_sec": 4,
    "send_window_enabled": True, "send_window_start": "08:00", "send_window_end": "18:00",
    "skip_recent_days": 14,
    # opt-out wording
    "append_optout": True,
    "optout_footer_email": "If you'd rather not hear from us, reply STOP and we won't contact you again.",
    "optout_suffix_sms": " Reply STOP to opt out.",
    # AI writing help (free keys from Google Gemini, Groq, NVIDIA, OpenRouter, Mistral, or your own server)
    "ai_provider": "gemini", "ai_model": "", "ai_pitch": DEFAULT_PITCH, "ai_grade_replies": True,
    "ai_key_gemini": "", "ai_key_groq": "", "ai_key_nvidia": "", "ai_key_openrouter": "", "ai_key_mistral": "",
    "ai_custom_url": "", "ai_custom_model": "", "ai_key_custom": "",
    # optional webhook for inbound SMS (needs the app reachable from the internet)
    "webhook_token": "",
}
SECRET_KEYS = {"smtp_pass", "imap_pass", "at_api_key", "ai_key_gemini", "ai_key_groq", "ai_key_nvidia",
               "ai_key_openrouter", "ai_key_mistral", "ai_key_custom"}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.db_path()), timeout=30, isolation_level="IMMEDIATE")   # take the write lock up front, honouring the wait
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def with_retry(fn, tries: int = 6):
    """Run a small database action again if another part of the app held the database for too long ("database is locked")."""
    for attempt in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # Databases made before a column existed gain it here.
        for table, column, ddl in (
                ("messages", "reply_to", "INTEGER"),
                ("leads", "offer", "TEXT NOT NULL DEFAULT ''"),
                ("leads", "deal_value", "INTEGER NOT NULL DEFAULT 0"),
                ("campaigns", "ai_personalize", "INTEGER NOT NULL DEFAULT 0"),
                ("campaigns", "followup_of", "INTEGER"),
                ("campaigns", "followup_days", "INTEGER NOT NULL DEFAULT 0")):
            if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # Settings still on the old built-in defaults move to the current ones (anything the owner wrote is kept).
        for key, old in (("sender_name", "DericBI"), ("ai_pitch", "")):
            conn.execute("DELETE FROM settings WHERE key=? AND value=?", (key, json.dumps(old)))
        # A secret for the incoming-SMS callback is made once, so nobody has to invent one.
        if not conn.execute("SELECT 1 FROM settings WHERE key='webhook_token'").fetchone():
            conn.execute("INSERT INTO settings(key, value) VALUES('webhook_token', ?)", (json.dumps(secrets.token_hex(16)),))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- settings
def get_settings(conn=None) -> dict:
    def _load(c):
        out = dict(SETTING_DEFAULTS)
        for row in c.execute("SELECT key, value FROM settings"):
            if row["key"] in out:
                try:
                    out[row["key"]] = json.loads(row["value"])
                except ValueError:
                    pass
        return out
    if conn is not None:
        return _load(conn)
    with db() as c:
        return _load(c)


def save_settings(patch: dict):
    with db() as c:
        for k, v in patch.items():
            if k not in SETTING_DEFAULTS:
                continue
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)),
            )


def set_internal(key: str, value):
    """Internal state (e.g. last IMAP UID) stored alongside settings but not user-editable."""
    with db() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def get_internal(key: str, default=None):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def log_event(conn, lead_id: int, kind: str, detail: str = ""):
    conn.execute(
        "INSERT INTO events(lead_id, ts, kind, detail) VALUES(?,?,?,?)",
        (lead_id, now(), kind, detail),
    )
