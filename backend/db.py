"""SQLite schema, connections and settings storage.

The DB is shared by the API server and the scraper worker process, so it runs in
WAL mode with a generous busy timeout. Leads are never hard-deleted (only
archived) which is what guarantees a lead can never be re-imported as new.
"""
import json
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
CREATE INDEX IF NOT EXISTS ix_leads_contacted ON leads(last_contacted_at) WHERE last_contacted_at IS NOT NULL;   -- Today: "messaged a few days ago"
-- The Leads page shows 50 rows in some order. With these the rows come out already in order and the database stops
-- after 50; without them it sorted every lead on every page view.
CREATE INDEX IF NOT EXISTS ix_leads_archived_id ON leads(archived, id);
CREATE INDEX IF NOT EXISTS ix_leads_archived_name ON leads(archived, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_leads_archived_updated ON leads(archived, updated_at);
CREATE INDEX IF NOT EXISTS ix_leads_archived_reply ON leads(archived, last_reply_at);
CREATE INDEX IF NOT EXISTS ix_leads_archived_sector ON leads(archived, sector);   -- the sector and town pick-lists read these alone
CREATE INDEX IF NOT EXISTS ix_leads_archived_town ON leads(archived, town);

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
CREATE INDEX IF NOT EXISTS ix_msg_lead_dir ON messages(lead_id, direction, status);     -- "has this lead been messaged": answered from the index alone
CREATE INDEX IF NOT EXISTS ix_msg_sent_lead ON messages(direction, status, channel, lead_id);   -- distinct leads reached, per channel

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

-- People who may sign in. Passwords and security answers are stored only as salted scrypt hashes.
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  role TEXT NOT NULL DEFAULT 'member',            -- 'owner' (the mother account) or 'member'
  pw_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  must_change INTEGER NOT NULL DEFAULT 0,         -- set after the owner issues a temporary password
  failed INTEGER NOT NULL DEFAULT 0,
  locked_until INTEGER NOT NULL DEFAULT 0,
  recover_failed INTEGER NOT NULL DEFAULT 0,
  recover_locked_until INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS security_answers (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL,
  question TEXT NOT NULL,
  answer_hash TEXT NOT NULL,
  PRIMARY KEY (user_id, slot)
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,                    -- the browser holds the token; only its hash is kept
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_ts INTEGER NOT NULL,
  seen_ts INTEGER NOT NULL
);
"""

# The pitch a copy that was never edited used to carry (third person, no brand): a saved copy of it is dropped on start-up so
# the current one below applies.
OLD_DEFAULT_PITCH = (
    "Deric Marangu is a data analyst. He helps local businesses use their own sales and stock records to see "
    "what is selling, what is not, and where money is being lost, so they can decide with facts. He also has "
    "ready-made tools that he customises to each business."
)
# The pitch of the version before this one (first person, "data analyst"): a saved, unedited copy of it is replaced too.
PREVIOUS_DEFAULT_PITCH = (
    "I'm Deric Marangu, a data analyst, and I run DericBI. I help local businesses use their own sales and stock records "
    "to see what is selling, what is not, and where money is being lost, so they can decide with facts. I also have "
    "ready-made tools that I adapt to each business. You pay only when you are satisfied with my work."
)
DEFAULT_PITCH = (
    "I'm Deric Marangu, a Business Intelligence specialist, and I run DericBI. I help micro and small businesses turn their "
    "existing data into better decisions, improved profitability and clear visibility into where money is being lost. I can "
    "identify slow-moving stock tying up cash, their best-performing sales periods and profitable product combinations based "
    "on actual buying patterns. I can provide a ready-to-use tracking system or a clear report showing what needs attention "
    "and where the opportunities lie. You only pay when you see value."
)

SETTING_DEFAULTS = {
    # identity
    "sender_name": "Deric Marangu",
    "brand_name": "DericBI",   # the business name the AI writes from ("I'm Deric, from DericBI")
    "backup_auto": True, "backup_hours": 24, "backup_keep": 7, "backup_dir": "",   # automatic backups, and an optional second folder (a USB stick, a synced folder)
    "slogan": "",
    # where every message tells people to find you (placeholders {website} {email} {whatsapp})
    "contact_website": "https://dericbi.vercel.app", "contact_email": "dericmarangu@gmail.com",
    "contact_whatsapp": "+254791360805",
    # outgoing email
    "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",  # starttls | ssl | none
    "smtp_user": "", "smtp_pass": "", "from_email": "", "reply_to": "",
    # incoming email (reply tracking)
    "imap_host": "", "imap_port": 993, "imap_security": "ssl",
    "imap_user": "", "imap_pass": "", "imap_poll_minutes": 5,
    # SMS through your own Android phone (the "SMS Gateway for Android" app in Local server mode)
    "sms_gateway_url": "", "sms_gateway_user": "", "sms_gateway_pass": "", "sms_sim": 0,
    # WhatsApp is sent from WhatsApp Web on this PC (linked once by scanning a code)
    # sending limits
    "email_daily_cap": 80, "sms_daily_cap": 150, "whatsapp_daily_cap": 40,
    "email_delay_sec": 25, "sms_delay_sec": 65, "whatsapp_delay_sec": 60,
    # WhatsApp runs in a hidden browser (no window), and reads replies from the chats every this many minutes (0 = never)
    "whatsapp_hide_window": True, "whatsapp_sync_minutes": 10,
    # the AI sales assistant (closer.py): off | suggest (you press send) | auto (low-risk turns only)
    "closer_mode": "off", "closer_kb": "", "closer_booking_link": "", "closer_min_confidence": 80, "closer_max_auto": 3,
    "closer_delay_sec": 60, "closer_disclose": "first", "closer_on_mode": "suggest", "closer_deal_desk": True,
    "closer_report_max": 2000, "closer_system_once": 20000, "closer_system_start": 1000, "closer_system_monthly": 200,
    "send_window_enabled": True, "send_window_start": "08:00", "send_window_end": "18:00",
    # "auto": on for a computer with 4 GB of memory or less, or two cores; "on" / "off" to force it
    "light_mode": "auto",
    # a lead who got a message is only messaged again as a follow-up, and only after this many days (or if they reply)
    "followup_wait_days": 3,
    # "best way for each lead": which way to try first, then next ("whatsapp,email,sms" = WhatsApp, then email, then text)
    "reach_order": "whatsapp,email,sms",
    # opt-out wording
    "append_optout": True,
    "optout_footer_email": "If you'd rather not hear from us, reply STOP and we won't contact you again.",
    "optout_suffix_sms": " STOP to opt out",
    # AI writing help (free keys from Google Gemini, Groq, NVIDIA, OpenRouter, Mistral, or your own server)
    "ai_provider": "gemini", "ai_model": "", "ai_pitch": DEFAULT_PITCH, "ai_grade_replies": True,
    "ai_key_gemini": "", "ai_key_groq": "", "ai_key_nvidia": "", "ai_key_openrouter": "", "ai_key_mistral": "",
    "ai_custom_url": "", "ai_custom_model": "", "ai_key_custom": "",
}
SECRET_KEYS = {"smtp_pass", "imap_pass", "sms_gateway_pass", "ai_key_gemini", "ai_key_groq", "ai_key_nvidia",
               "ai_key_openrouter", "ai_key_mistral", "ai_key_custom"}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.db_path()), timeout=30, isolation_level="IMMEDIATE")   # take the write lock up front, honouring the wait
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # Kind to a slow disk and little memory. With the write-ahead log, NORMAL waits for the disk once per checkpoint
    # instead of after every commit; the file can never be corrupted by it, and only a power cut at the very moment
    # of a write can lose that last write.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-16000")   # 16 MB of cache per connection
    return conn


def lock_writes(conn):
    """Take the database's write lock NOW, before reading what decides a write. Without it two requests (a double-click, the
    sender and a button press) can both read 'nobody has been messaged yet' and then both write. The second one waits here
    until the first has committed, and so sees what it did."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


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


BIG_DATABASE = 50_000   # rows in leads or messages from which planner statistics pay off


def refresh_planner_stats(conn, force: bool = False, big: int = BIG_DATABASE) -> bool:
    """On a big database, teach SQLite how big the tables really are, so it picks the fastest plan for each question
    (a screen that took 2 seconds at a million leads takes 0.08). Only plans change, never an answer.

    Small databases are deliberately left alone: statistics taken while a table is nearly empty make SQLite plan for a
    one-row table, which is far slower once it fills up, whereas with none it plans sensibly for any size.
    Re-done when a table has grown or shrunk by a quarter (and by at least 500 rows), or when never done. It reads every
    row once (about 1.5 seconds per million leads on a fast computer), so the server runs it in the background; a sampled
    version is quicker but plans measurably worse. Returns True if it ran."""
    try:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("leads", "messages")}
        if not force and max(counts.values()) < big:
            return False
        known = {r[0]: int(str(r[1]).split()[0]) for r in conn.execute(
            "SELECT idx, stat FROM sqlite_stat1 WHERE idx IN ('ix_leads_archived_id', 'ix_msg_lead')")} \
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone() else {}
        stale = force or not known
        for idx, table in (("ix_leads_archived_id", "leads"), ("ix_msg_lead", "messages")):
            before = known.get(idx, 0)
            if abs(counts[table] - before) >= 500 and abs(counts[table] - before) * 4 >= max(before, 1):
                stale = True
        if not stale:
            return False
        conn.execute("ANALYZE")
        return True
    except sqlite3.Error:
        return False   # never let this stop the app from starting


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
                ("campaigns", "followup_days", "INTEGER NOT NULL DEFAULT 0"),
                ("campaigns", "style", "TEXT NOT NULL DEFAULT ''"),          # 'ready' = ready-made message per kind of business
                ("campaigns", "followup_step", "INTEGER NOT NULL DEFAULT 0")):
            if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # Settings still on the old built-in defaults move to the current ones (anything the owner wrote is kept).
        for key, old in (("sender_name", "DericBI"), ("ai_pitch", ""), ("ai_pitch", OLD_DEFAULT_PITCH), ("ai_pitch", PREVIOUS_DEFAULT_PITCH), ("sms_delay_sec", 4),
                         ("optout_suffix_sms", " Reply STOP to opt out.")):
            conn.execute("DELETE FROM settings WHERE key=? AND value=?", (key, json.dumps(old)))
        # Let the database learn how the data is spread, so it picks the fastest index for each query. Done once; after that
        # "optimize" refreshes it only when the data has changed a lot.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone():
            conn.execute("ANALYZE")
        conn.execute("PRAGMA optimize")
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
