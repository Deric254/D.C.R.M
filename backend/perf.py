"""What kind of computer this is, and 'light mode', which keeps a small one from being overwhelmed.

Chrome costs about 0.7 GB of memory per window. The Maps search and WhatsApp each need one, so on a computer with 4 GB
or less (or only two processor cores) running both at once makes it swap and freeze. Light mode, switched on by itself
for such a computer, runs one browser at a time: WhatsApp waits for a lead search to finish, and a lead search waits for
WhatsApp. It can be forced on or off in Settings.
"""
import ctypes
import functools
import os

WEAK_CORES = 2
WEAK_RAM_MB = 4096


def _ram_mb():
    try:
        if os.name == "nt":
            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MemStatus()
            m.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullTotalPhys // 1048576)
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 1048576)
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def machine() -> dict:
    cores, ram = os.cpu_count() or 1, _ram_mb()
    return {"cores": cores, "ram_mb": ram, "weak": cores <= WEAK_CORES or (ram is not None and ram <= WEAK_RAM_MB)}


def light_mode(settings: dict, info: dict = None) -> bool:
    mode = settings.get("light_mode", "auto")
    return mode == "on" or (mode == "auto" and (info or machine())["weak"])


def browser_conflict(conn, settings: dict, starting: str, info: dict = None):
    """Why this browser job can't start now (light mode: one Chrome at a time), in words, or None when it can."""
    if not light_mode(settings, info):
        return None
    import jobs
    if starting == "scrape":
        if jobs.active_job(conn, "whatsapp") or jobs.active_job(conn, "whatsapp_link"):
            return ("WhatsApp is open right now, and this computer runs one browser at a time to stay fast. Wait until WhatsApp has "
                    "finished sending (or pause the WhatsApp campaign), then search again. You can change this in Settings > Light mode.")
    elif starting in ("whatsapp", "whatsapp_link"):
        if jobs.active_job(conn, "scrape"):
            return "A lead search is running. WhatsApp waits for it to finish, so the computer isn't overloaded."
    return None
