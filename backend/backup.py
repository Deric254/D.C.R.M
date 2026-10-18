"""Backups you can restore, and the way to move your business to another computer.

A backup is ONE file (a zip) holding your business: leads, messages, campaigns, history, settings and your logo. It never
holds what belongs to the computer or to who may use it: sign-in accounts, security answers, open sessions, and the
licence and install code. A restore therefore never changes who may sign in or whether this computer is licensed, so it
cannot be used to copy a licence to another computer. Passwords and API keys are left out unless you ask for them.

Safety rules, each of which a test locks in:
  * the copy is taken in one consistent step while the app keeps working, then checked before it is offered;
  * the file carries a checksum, so damage or tampering is caught before anything is touched;
  * a restore first saves a safety copy of what is there now, then replaces everything in ONE transaction: it all
    happens or none of it does, and the old data is never half-mixed with the new;
  * campaigns that were running come back PAUSED and messages caught mid-send come back as "check before retrying", so
    moving to a new computer can never make two computers message the same people;
  * sending and inbox checking stand still while a restore runs (a send that finishes afterwards could be recorded
    against the wrong message).
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import config
import db
from version import VERSION

FORMAT = 1
PREFIX = "DericBI-backup-"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(zip|sqlite3)$")

# What belongs to this computer, not to the business: never saved in a backup and never restored.
IDENTITY_KEYS = {"install_id", "license", "clock_high", "whatsapp_linked", "whatsapp_linked_at"}
KEEP_IF_PRESENT = {"imap_last_uid"}          # where this computer is in the mailbox: kept if it has one, taken from the backup if not
ACCOUNT_TABLES = ("sessions", "security_answers", "users")
BUSINESS_TABLES = ["campaigns", "leads", "messages", "events", "jobs", "job_logs"]    # parents before children
DELETE_ORDER = ["job_logs", "jobs", "events", "messages", "campaigns", "leads"]
SQLITE_HEADER = b"SQLite format 3\x00"

INTERRUPTED_SEND = ("Interrupted by a restore - may or may not have been delivered, check before retrying")


class BackupError(ValueError):
    """Something wrong with a backup or a restore, worded for a non-technical person."""


# ------------------------------------------------------------------ keeping background work still during a restore
maintenance = threading.Event()
_work_lock = threading.Condition()
_active_work = 0


def enter_work() -> bool:
    """A background worker asks to do one unit of work (a send, an inbox check). False while a restore is running."""
    global _active_work
    with _work_lock:
        if maintenance.is_set():
            return False
        _active_work += 1
        return True


def exit_work() -> None:
    global _active_work
    with _work_lock:
        _active_work = max(0, _active_work - 1)
        _work_lock.notify_all()


def quiesce(timeout: float = 150.0) -> bool:
    """Stop new background work and wait for what is in progress to finish."""
    maintenance.set()
    end = time.time() + timeout
    with _work_lock:
        while _active_work > 0:
            left = end - time.time()
            if left <= 0:
                maintenance.clear()
                return False
            _work_lock.wait(min(left, 1.0))
    return True


# ------------------------------------------------------------------ one backup or restore at a time, run in the background
_op_lock = threading.Lock()
_state = {"op": None, "step": "", "error": "", "result": None, "started": 0.0, "finished": 0.0}


def status() -> dict:
    with _op_lock:
        out = dict(_state)
    out["running"] = out["op"] is not None and not out["finished"]
    return out


def busy() -> bool:
    return status()["running"]


def start(kind: str, fn):
    """Run fn(progress) in the background as the one current operation. Raises if another is running."""
    with _op_lock:
        if _state["op"] is not None and not _state["finished"]:
            raise BackupError("A backup or restore is already running. Wait for it to finish.")
        _state.update(op=kind, step="Starting", error="", result=None, started=time.time(), finished=0.0)

    def progress(step):
        with _op_lock:
            _state["step"] = step

    def run():
        try:
            result = fn(progress)
            with _op_lock:
                _state.update(result=result, step="Done")
        except BackupError as e:
            with _op_lock:
                _state.update(error=str(e), step="Stopped")
        except Exception as e:   # never leave the screen waiting forever
            with _op_lock:
                _state.update(error=f"That did not work ({type(e).__name__}: {e}). Your data was not changed.", step="Stopped")
        finally:
            maintenance.clear()
            with _op_lock:
                _state["finished"] = time.time()
    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------ places and small helpers
def backups_dir() -> Path:
    p = config.DATA_DIR / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha256(path, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


def _need_space(folder, needed: int, what: str):
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if free < needed:
        raise BackupError(f"There is not enough free space to {what}: it needs about {needed // 1_000_000} MB and "
                          f"{free // 1_000_000} MB is free. Free some space, or choose another folder.")


def _live_size() -> int:
    try:
        return os.path.getsize(config.db_path())
    except OSError:
        return 0


def _tmpdir(prefix) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(backups_dir())))


# ------------------------------------------------------------------ making a backup
def create_package(*, include_secrets: bool = False, label: str = "manual", progress=lambda s: None, dest_dir=None) -> dict:
    """Make a backup file. Returns {name, path, bytes, counts, seconds}."""
    began = time.time()
    dest = Path(dest_dir) if dest_dir else backups_dir()
    dest.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "manual"
    _need_space(backups_dir(), int(_live_size() * 1.5) + 5_000_000, "make a backup")
    name = f"{PREFIX}{datetime.now():%Y%m%d-%H%M%S}-{label}.zip"
    work = _tmpdir(".making-")
    try:
        progress("Copying your data")
        copy = work / "data.sqlite3"
        src = db.connect()
        try:
            dst = sqlite3.connect(copy)
            src.backup(dst)   # one step: a consistent snapshot, and the app keeps working (WAL)
            dst.close()
        finally:
            src.close()

        progress("Taking out sign-in accounts and this computer's licence")
        c = sqlite3.connect(copy)
        try:
            for t in ACCOUNT_TABLES:
                c.execute(f"DELETE FROM {t}")
            marks = ",".join("?" * len(IDENTITY_KEYS))
            c.execute(f"DELETE FROM settings WHERE key IN ({marks})", tuple(IDENTITY_KEYS))
            if not include_secrets:
                marks = ",".join("?" * len(db.SECRET_KEYS))
                c.execute(f"DELETE FROM settings WHERE key IN ({marks})", tuple(db.SECRET_KEYS))
            c.commit()
            progress("Checking the copy")
            verdict = c.execute("PRAGMA quick_check").fetchone()[0]
            if verdict != "ok":
                raise BackupError("The copy of your data failed its safety check, so your live data may be damaged. "
                                  "Nothing was saved. Close the app, and restore your latest backup.")
            counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in BUSINESS_TABLES}
            schema = {t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")] for t in BUSINESS_TABLES + ["settings"]}
            c.execute("PRAGMA journal_mode=DELETE")   # one self-contained file
        finally:
            c.close()

        progress("Packing it into one file")
        size = copy.stat().st_size
        manifest = {"format": FORMAT, "app": "DericBI CRM", "app_version": VERSION,
                    "created": datetime.now().isoformat(timespec="seconds"), "label": label,
                    "includes_secrets": bool(include_secrets), "counts": counts, "schema": schema,
                    "db_sha256": _sha256(copy), "db_bytes": size}
        _need_space(dest, int(size * 0.5) + 5_000_000, "save the backup there")
        part = dest / (name + ".part")
        try:
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                z.writestr("manifest.json", json.dumps(manifest, indent=1))
                z.write(copy, "data.sqlite3")
                for logo in config.DATA_DIR.glob("logo.*"):
                    z.write(logo, f"files/{logo.name}")
            os.replace(part, dest / name)
        finally:
            part.unlink(missing_ok=True)
        return {"name": name, "path": str(dest / name), "bytes": (dest / name).stat().st_size, "counts": counts,
                "seconds": round(time.time() - began, 1), "includes_secrets": bool(include_secrets)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ reading a backup
def describe(path) -> dict:
    """What a backup file holds, read quickly without unpacking it. Raises BackupError if it is not a usable backup."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError as e:
        raise BackupError(f"Could not open that file ({e.strerror or e}).")
    if head.startswith(SQLITE_HEADER):   # a plain copy of the database, as the app saved before backups had their own format
        return {"format": 0, "legacy": True, "created": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "label": "older backup", "includes_secrets": True, "counts": {}, "app_version": "", "db_bytes": path.stat().st_size}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "manifest.json" not in names or "data.sqlite3" not in names:
                raise BackupError("That file is not a DericBI backup.")
            manifest = json.loads(z.read("manifest.json"))
    except zipfile.BadZipFile:
        raise BackupError("That file is not a DericBI backup, or it was damaged while it was copied.")
    except (ValueError, KeyError):
        raise BackupError("That backup's description is unreadable, so it can't be trusted.")
    if not isinstance(manifest, dict) or manifest.get("app") != "DericBI CRM":
        raise BackupError("That file is not a DericBI backup.")
    if int(manifest.get("format", 0)) > FORMAT:
        raise BackupError("That backup was made by a newer version of the app. Update this app first, then restore it.")
    return manifest


def list_backups(folder=None) -> list:
    folder = Path(folder) if folder else backups_dir()
    out = []
    for p in sorted(folder.glob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if p.is_dir() or not NAME_RE.match(p.name) or p.name.endswith(".part"):
            continue
        try:
            m = describe(p)
            out.append({"name": p.name, "bytes": p.stat().st_size, "created": m.get("created", ""), "label": m.get("label", ""),
                        "counts": m.get("counts", {}), "includes_secrets": bool(m.get("includes_secrets")),
                        "legacy": bool(m.get("legacy")), "app_version": m.get("app_version", "")})
        except BackupError as e:
            out.append({"name": p.name, "bytes": p.stat().st_size, "created": "", "label": "damaged", "counts": {}, "error": str(e)})
    return out


def _unpack(path, work: Path, progress) -> tuple:
    """Open a backup into `work`, check it fully, and return (manifest, path of the unpacked database)."""
    manifest = describe(path)
    data = work / "data.sqlite3"
    if manifest.get("legacy"):
        _need_space(work, manifest["db_bytes"] + 5_000_000, "unpack that backup")
        progress("Copying the backup")
        shutil.copyfile(path, data)
    else:
        _need_space(work, int(manifest["db_bytes"]) + 5_000_000, "unpack that backup")
        progress("Unpacking and checking the file")
        h = hashlib.sha256()
        with zipfile.ZipFile(path) as z, z.open("data.sqlite3") as src, open(data, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
        if h.hexdigest() != manifest.get("db_sha256"):
            raise BackupError("This backup is damaged or was changed after it was made (its checksum does not match), so it "
                              "was not used. Your data was not touched. Use another copy of the backup.")
    progress("Checking the data inside")
    c = sqlite3.connect(data)
    try:
        try:
            # a packaged backup was checked when it was made and its checksum has just matched: the quick check is enough.
            # A plain old copy has nothing to vouch for it, so it gets the full check.
            verdict = c.execute("PRAGMA quick_check" if not manifest.get("legacy") else "PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError:
            raise BackupError("This backup's data is damaged, so it was not used. Your data was not touched.")
        if verdict != "ok":
            raise BackupError("This backup's data failed its integrity check, so it was not used. Your data was not touched.")
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "leads" not in tables:
            raise BackupError("That file is not a DericBI backup: it has no leads table.")
        cols = {r[1] for r in c.execute("PRAGMA table_info(leads)")}
        if not {"id", "name"} <= cols:
            raise BackupError("That file's leads are not in a form this app understands.")
        if not manifest.get("counts"):
            manifest["counts"] = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in BUSINESS_TABLES if t in tables}
    finally:
        c.close()
    return manifest, data


# ------------------------------------------------------------------ restoring
def restore(path, progress=lambda s: None, after=None) -> dict:
    """Replace this computer's business data with a backup. All or nothing. Returns what was restored."""
    path = Path(path)
    live_files = []
    with db.db() as conn:   # a lead search or enrichment running right now would write against ids that are about to change
        import jobs
        for r in conn.execute("SELECT pid FROM jobs WHERE status IN ('queued','running')").fetchall():
            if jobs.pid_alive(r["pid"]):
                raise BackupError("A lead search is still running. Stop it (Find leads, then Stop), then restore.")
    work = _tmpdir(".restoring-")
    try:
        manifest, data = _unpack(path, work, progress)
        progress("Saving a safety copy of what is here now")
        safety = create_package(label="before-restore", progress=lambda s: None)
        progress("Waiting for sending and inbox checks to stand still")
        if not quiesce():
            raise BackupError("Sending is busy finishing a message. Try again in a minute. Nothing was changed.")
        progress("Replacing your data")
        summary = _apply(data, manifest)
        summary["safety_copy"] = safety["name"]
        progress("Finishing up")
        _restore_logo(path, manifest)
        try:
            c = db.connect()
            try:
                db.refresh_planner_stats(c)
                c.commit()
            finally:
                c.close()
        except Exception:
            pass
        if after:
            after()
        return summary
    finally:
        maintenance.clear()   # sending and inbox checks carry on, however this ended
        shutil.rmtree(work, ignore_errors=True)


def _cols(conn, schema, table):
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _apply(data_path, manifest) -> dict:
    conn = db.connect()
    conn.isolation_level = None   # this function decides exactly when the transaction starts and ends
    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS bak", (str(data_path),))
        attached = True
        bak_tables = {r[0] for r in conn.execute("SELECT name FROM bak.sqlite_master WHERE type='table'")}
        plan = []
        for t in BUSINESS_TABLES:
            if t not in bak_tables:
                continue
            live, bak = _cols(conn, "main", t), set(_cols(conn, "bak", t))
            cols = [c for c in live if c in bak]   # only columns both sides know: an older or newer backup still fits
            if cols:
                plan.append((t, ", ".join(f'"{c}"' for c in cols)))
        conn.execute("PRAGMA cache_size=-262144")   # 256 MB of working memory for this one job
        marks = ",".join("?" * len(BUSINESS_TABLES))
        indexes = conn.execute(f"SELECT name, sql FROM main.sqlite_master WHERE type='index' AND sql IS NOT NULL AND tbl_name IN ({marks})",
                               BUSINESS_TABLES).fetchall()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("PRAGMA defer_foreign_keys=ON")
            for name, _ in indexes:                  # rows go in fastest with no indexes to keep up to date...
                conn.execute(f'DROP INDEX main."{name}"')
            for t in DELETE_ORDER:
                conn.execute(f"DELETE FROM main.{t}")
            restored = {}
            for t, cols in plan:
                conn.execute(f"INSERT INTO main.{t} ({cols}) SELECT {cols} FROM bak.{t}")
                restored[t] = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
            for _, create in indexes:                # ...and they are built afterwards, in one sorted pass each (a UNIQUE one also re-checks the data)
                conn.execute(create)
            # settings: what the backup holds, except what belongs to this computer; never invent keys the app does not know
            kept = 0
            if "settings" in bak_tables:
                for key, value in conn.execute("SELECT key, value FROM bak.settings").fetchall():
                    if key in IDENTITY_KEYS or (key not in db.SETTING_DEFAULTS and key not in KEEP_IF_PRESENT):
                        continue
                    if key in db.SECRET_KEYS and value in ('""', "", None):
                        continue          # an empty secret never wipes a working one
                    if key in KEEP_IF_PRESENT and conn.execute("SELECT 1 FROM main.settings WHERE key=?", (key,)).fetchone():
                        continue
                    conn.execute("INSERT INTO main.settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (key, value))
                    kept += 1
            # nothing may carry on by itself on this computer
            paused = conn.execute("UPDATE campaigns SET status='paused', last_error=? WHERE status IN ('running','scheduled')",
                                  ("Paused after a restore. Check it, then resume.",)).rowcount
            conn.execute("UPDATE messages SET status='failed', error=? WHERE status='sending' AND direction='out'", (INTERRUPTED_SEND,))
            conn.execute("UPDATE jobs SET status='failed', finished_at=?, progress='Interrupted (restored from a backup)' "
                         "WHERE status IN ('queued','running')", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
            conn.execute("COMMIT")
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(e, sqlite3.IntegrityError):
                raise BackupError("That backup is not consistent with this version of the app, so nothing was changed "
                                  f"({e}).")
            if isinstance(e, sqlite3.Error):
                raise BackupError(f"The restore could not be completed, so nothing was changed ({e}).")
            raise
        return {"restored": restored, "settings": kept, "paused_campaigns": paused,
                "secrets_in_backup": bool(manifest.get("includes_secrets"))}
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE bak")
            except sqlite3.Error:
                pass
        conn.close()


def _restore_logo(path, manifest):
    if manifest.get("legacy"):
        return
    try:
        with zipfile.ZipFile(path) as z:
            logos = [n for n in z.namelist() if n.startswith("files/logo.") and "/" not in n[len("files/"):]]
            if not logos:
                return
            name = logos[0][len("files/"):]
            if not re.fullmatch(r"logo\.(png|jpg|jpeg|gif|webp|svg)", name):
                return
            for old in config.DATA_DIR.glob("logo.*"):
                old.unlink(missing_ok=True)
            (config.DATA_DIR / name).write_bytes(z.read(logos[0]))
    except (OSError, zipfile.BadZipFile):
        pass


# ------------------------------------------------------------------ keeping the folder tidy, and automatic backups
def prune(keep: int, folder=None) -> int:
    """Keep the newest `keep` automatic backups (and the last three safety copies); never touch manual ones."""
    folder = Path(folder) if folder else backups_dir()
    removed = 0
    for label, limit in (("auto", max(1, int(keep))), ("before-restore", 3)):
        files = sorted((p for p in folder.glob(f"{PREFIX}*-{label}.zip")), key=lambda p: p.name, reverse=True)
        for p in files[limit:]:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def latest_time(label="auto", folder=None):
    folder = Path(folder) if folder else backups_dir()
    files = sorted((p for p in folder.glob(f"{PREFIX}*-{label}.zip")), key=lambda p: p.name, reverse=True)
    for p in files:
        try:
            return datetime.strptime(p.name[len(PREFIX):len(PREFIX) + 15], "%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            continue
    return None


def auto_due(s: dict, now_ts=None) -> bool:
    if not s.get("backup_auto", True):
        return False
    hours = max(1, int(s.get("backup_hours") or 24))
    last = latest_time("auto")
    return last is None or (now_ts or time.time()) - last >= hours * 3600


def run_auto(s: dict, progress=lambda x: None) -> dict:
    """One automatic backup: into the backups folder, a second copy to the chosen folder (if any), then tidy both."""
    info = create_package(label="auto", progress=progress)
    info["external"] = ""
    ext = (s.get("backup_dir") or "").strip()
    if ext:
        try:
            dest = Path(ext)
            dest.mkdir(parents=True, exist_ok=True)
            _need_space(dest, info["bytes"] + 5_000_000, "copy the backup there")
            part = dest / (info["name"] + ".part")
            shutil.copyfile(info["path"], part)
            os.replace(part, dest / info["name"])
            prune(int(s.get("backup_keep") or 7), dest)
            info["external"] = str(dest)
        except (OSError, BackupError) as e:
            info["external_error"] = str(e)
    prune(int(s.get("backup_keep") or 7))
    return info


# ------------------------------------------------------------------ a damaged data file
HEALTH = {"checked": 0.0, "ok": None, "detail": ""}
_DAMAGE_WORDS = ("malformed", "not a database", "disk image")


def open_database() -> None:
    """Open the database for the app. If the file is damaged beyond opening, set it aside (it is never deleted, in case it
    can still be rescued), start a fresh one, and restore the newest backup into it. Any other problem, such as a database
    that is only busy right now, is left alone and raised as before."""
    try:
        db.init_db()
        return
    except sqlite3.DatabaseError as e:
        if not any(w in str(e).lower() for w in _DAMAGE_WORDS):
            raise
        error = str(e)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = Path(config.db_path())
    aside = base.with_name(f"{base.stem}.damaged-{stamp}{base.suffix}")
    for ext in ("", "-wal", "-shm"):
        part = Path(str(base) + ext)
        if part.exists():
            os.replace(part, Path(str(aside) + ext))
    db.init_db()
    note = {"at": stamp, "aside": aside.name, "error": error, "restored_from": "", "restore_error": ""}
    newest = next((b for b in list_backups() if not b.get("error") and b.get("label") != "before-restore"), None)
    if newest:
        try:
            restore(backups_dir() / newest["name"])
            note["restored_from"] = newest["name"]
        except Exception as e2:   # the app must still start; the notice explains what to do
            note["restore_error"] = str(e2)
    db.set_internal("recovered_from_damage", note)
    HEALTH.update(ok=False, detail=error)


def health_check() -> dict:
    """A full structural check of the live database, for the Backups panel to report on."""
    c = db.connect()
    try:
        verdict = c.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.DatabaseError as e:
        verdict = str(e)
    finally:
        c.close()
    HEALTH.update(checked=time.time(), ok=(verdict == "ok"), detail="" if verdict == "ok" else verdict)
    return dict(HEALTH)


def notice():
    return db.get_internal("recovered_from_damage")


def dismiss_notice():
    db.set_internal("recovered_from_damage", None)


def safe_path(name: str) -> Path:
    """The path of a file in the backups folder, refusing anything that is not a plain backup file name."""
    if not NAME_RE.match(name or "") or name.endswith(".part"):
        raise BackupError("That is not a backup file name.")
    p = backups_dir() / name
    if not p.is_file():
        raise BackupError("That backup is not in the backups folder.")
    return p
