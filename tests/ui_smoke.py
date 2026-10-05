"""Drive the real UI in Chromium against a live backend. Run: python tests/ui_smoke.py [screenshot_dir]"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="shots-"))
SHOTS.mkdir(parents=True, exist_ok=True)
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
DATA = tempfile.mkdtemp(prefix="dericbi-ui-")


def call(method, path, body=None):
    req = urllib.request.Request(BASE + "/api" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return {"error": e.code, **json.loads(e.read() or b"{}")}


server = subprocess.Popen([sys.executable, str(ROOT / "backend" / "main.py"), "--port", str(PORT), "--data-dir", DATA],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(60):
        try:
            call("GET", "/health")
            break
        except Exception:
            time.sleep(0.25)
    else:
        raise SystemExit("server didn't start: " + server.stdout.read())

    # ---------------------------------------------------------------- seed data
    names = [("Meru Central Pharmacy", "Pharmacy", "Meru"), ("Chuka Agrovet Supplies", "Agrovet", "Chuka"),
             ("Nkubu Wholesalers", "Wholesale", "Nkubu"), ("Embu Family Chemist", "Pharmacy", "Embu"),
             ("Thika Road Supermarket", "Supermarket", "Thika"), ("Karatina Minimart", "Minimart", "Karatina"),
             ("Kenyatta Pharmacy <script>alert(1)</script>", "Pharmacy", "Meru")]
    ids = []
    for i in range(24):
        n, s, t = names[i % len(names)]
        r = call("POST", "/leads", {"name": f"{n} {i}" if i >= len(names) else n, "sector": s, "town": t,
                                    "phone": f"07{10 + i:02d}{300 + i:03d}{i:03d}", "email": f"shop{i}@example.co.ke" if i % 3 == 0 else ""})
        ids.append(r["id"])
    call("POST", f"/leads/{ids[0]}/reply", {"text": "Interested, how much is it? Please call me", "channel": "sms"})
    call("POST", f"/leads/{ids[1]}/reply", {"text": "Maybe later, I'm busy this month", "channel": "sms"})
    call("POST", f"/leads/{ids[2]}/reply", {"text": "We already have a system, not interested", "channel": "sms"})
    call("POST", f"/leads/{ids[3]}/reply", {"text": "STOP", "channel": "sms"})
    call("POST", f"/leads/{ids[4]}/reply", {"text": "who is this?", "channel": "sms"})
    call("PATCH", f"/leads/{ids[5]}", {"status": "meeting", "next_followup": "2020-01-01"})
    call("PUT", "/settings", {"at_username": "sandbox", "at_api_key": "k", "at_base_url": "http://127.0.0.1:9", "send_window_enabled": False})

    from playwright.sync_api import sync_playwright
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 860})
        page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e} @ {page.url} | {(e.stack or '')[:300]}"))
        page.on("dialog", lambda d: (problems.append(f"UNEXPECTED JS dialog: {d.message}"), d.dismiss()))

        def shot(name):
            page.screenshot(path=str(SHOTS / f"{name}.png"))

        # ---- overview
        page.goto(BASE + "/#/overview")
        page.wait_for_selector(".pipeline .seg")
        assert page.locator(".seg").count() == 6
        assert "Hot leads" in page.inner_text("#content")
        shot("1-overview")

        # ---- leads
        page.click('#nav a[data-view="leads"]')
        page.wait_for_selector("table.t tbody tr")
        assert page.locator("table.t tbody tr").count() == 24
        page.fill("#q", "chuka")
        page.wait_for_function("document.querySelectorAll('table.t tbody tr').length < 24")
        page.fill("#q", "")
        page.wait_for_function("document.querySelectorAll('table.t tbody tr').length == 24")
        # XSS: the script-tag name must be shown as text, not executed
        assert page.locator("table.t tbody tr", has_text="<script>").count() >= 1
        shot("2-leads")
        page.locator("table.t tbody tr").first.click()
        page.wait_for_selector(".drawer")
        assert "Interested" in page.inner_text(".drawer") or "Hot" in page.inner_text(".drawer")
        page.wait_for_timeout(450)
        shot("3-drawer")
        page.click("#log-reply")
        page.fill("#reply-form textarea", "Yes send price list")
        page.click("#reply-form button[type=submit]")
        page.wait_for_selector(".toast")
        page.wait_for_selector(".drawer")
        page.keyboard.press("Escape")
        page.wait_for_selector(".drawer", state="detached")

        # ---- add lead + duplicate rejection
        page.click("#btn-add")
        page.fill('dialog input[name=name]', "UI Test Pharmacy")
        page.fill('dialog input[name=phone]', "0799 111 222")
        page.fill('dialog input[name=town]', "Meru")
        page.click("dialog button[value=ok]")
        page.wait_for_selector("dialog", state="hidden")
        page.click("#btn-add")
        page.fill('dialog input[name=name]', "Totally Other Name")
        page.fill('dialog input[name=phone]', "+254799111222")
        page.click("dialog button[value=ok]")
        page.wait_for_selector(".toast.bad")
        assert "Already in your CRM" in page.inner_text(".toasts")
        page.wait_for_selector(".drawer")      # shows the lead that already exists
        assert "UI Test Pharmacy" in page.inner_text(".drawer")
        page.keyboard.press("Escape")
        page.wait_for_selector(".drawer", state="detached")
        assert call("GET", "/leads?q=799111222")["total"] == 1

        # ---- replies
        page.click('#nav a[data-view="replies"]')
        page.wait_for_selector(".reply")
        assert page.locator(".reply").count() >= 3
        shot("4-replies")
        page.click('[data-tab="optout"]')
        page.wait_for_function("document.querySelector('#list').innerText.includes('STOP')")
        assert page.locator(".reply").count() == 1

        # ---- outreach preview
        page.click('#nav a[data-view="outreach"]')
        page.wait_for_selector("#cform")
        page.fill("#cform textarea[name=body]", "Hi {name}, DericBI helps pharmacies in {town} track stock. Free demo?")
        page.click('#cform label.opt:has(input[name=sectors][value="Pharmacy"])')
        page.wait_for_selector("#preview .sample")
        txt = page.inner_text("#preview")
        assert "will get this" in txt and "Reply STOP" in txt, txt
        assert not page.is_disabled("#go")
        shot("5-outreach")
        page.fill("#cform textarea[name=body]", "Hi {nmae}")
        page.wait_for_selector("#preview .notice")
        assert "nmae" in page.inner_text("#preview")
        assert page.is_disabled("#go")

        # ---- find leads (build searches, don't run)
        page.click('#nav a[data-view="find"]')
        page.wait_for_selector("#builder")
        page.click('label.opt:has(input[data-cat="Chemist"])')
        page.click("#mix")
        page.click("#orig")
        assert "Searches to run" in page.inner_text("#queue")
        shot("6-find")

        # ---- settings
        page.click('#nav a[data-view="settings"]')
        page.wait_for_selector("#sform")
        page.fill('input[name=email_daily_cap]', "55")
        page.click("#sform button[type=submit]")
        page.wait_for_selector(".toast")
        shot("7-settings")
        assert call("GET", "/settings")["email_daily_cap"] == 55

        # ---- overview again, mobile-ish width
        page.set_viewport_size({"width": 820, "height": 800})
        page.click('#nav a[data-view="overview"]')
        page.wait_for_selector(".pipeline")
        shot("8-narrow")

        # ---- dark mode
        page.emulate_media(color_scheme="dark")
        page.set_viewport_size({"width": 1366, "height": 860})
        page.click('#nav a[data-view="leads"]')
        page.wait_for_selector("table.t tbody tr")
        shot("9-dark-leads")
        browser.close()

    print("screens:", SHOTS)
    # failed requests with 4xx are expected here (duplicate lead, bad placeholder) - the app handles them
    real = [x for x in problems if "ERR_" not in x and "status of 40" not in x]
    if real:
        print("PROBLEMS:\n" + "\n".join(real))
        sys.exit(1)
    print("UI SMOKE OK")
finally:
    server.terminate()
    try:
        server.wait(5)
    except Exception:
        server.kill()
