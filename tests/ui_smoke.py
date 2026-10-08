"""Drive the real UI in Chromium against a live backend. Run: python tests/ui_smoke.py [screenshot_dir]"""
import json
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
    call("PUT", "/settings", {"sms_gateway_url": "127.0.0.1:9", "sms_gateway_user": "u", "sms_gateway_pass": "p", "send_window_enabled": False})

    # A stand-in AI server so the Find screen's planner can be driven end to end.
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class FakeAI(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            plan = {"reply": "Pharmacies and a clinic in your two towns.", "searches": [
                {"category": "Pharmacy", "town": "Kisii"}, {"category": "Pharmacy", "town": "Migori"}, {"category": "Clinic", "town": "Kisii"}]}
            out = json.dumps({"choices": [{"message": {"content": json.dumps(plan)}}]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        def log_message(self, *a): pass
    fake_ai = HTTPServer(("127.0.0.1", 0), FakeAI)
    threading.Thread(target=fake_ai.serve_forever, daemon=True).start()
    call("PUT", "/settings", {"ai_provider": "custom", "ai_custom_url": f"http://127.0.0.1:{fake_ai.server_port}", "ai_custom_model": "m"})

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
        page.click('#cform label:has(input[name=channel][value=sms])')
        page.click('#cform label.opt:has(input[name=sectors][value="Pharmacy"])')
        page.wait_for_selector("#preview .sample")
        txt = page.inner_text("#preview")
        assert "will get a message" in txt and "of 160 characters" in txt and "Meru" not in txt.split("Example")[0], txt   # ready-made text, one SMS
        assert not page.is_disabled("#go")
        picked = page.locator("#pick input[type=checkbox]:checked").count()
        assert picked > 3 and f"{picked} of" in page.inner_text("#pickhead")
        shot("5-outreach")
        page.click("#pick-none")                                    # nobody ticked: cannot start
        page.wait_for_selector("#preview .notice")
        assert page.is_disabled("#go")
        page.locator("#pick input[type=checkbox]").first.check()    # tick one lead by hand
        page.wait_for_selector("#preview .sample")
        assert "1</strong> lead " in page.inner_html("#preview") and not page.is_disabled("#go")
        page.click('#cform label.opt:has(input[name=mode][value=own])')
        page.fill("#cform textarea[name=body]", "Hi {nmae}")
        page.wait_for_selector("#preview .notice")
        assert "nmae" in page.inner_text("#preview")
        assert page.is_disabled("#go")

        # ---- find leads (build searches, don't run)
        page.click('#nav a[data-view="find"]')
        page.wait_for_selector("#builder")
        page.fill('input[data-filter="cat"]', "chem")
        assert page.locator("#chips-cat label.opt").count() == 1
        page.click('#chips-cat label.opt:has(input[value="Chemist"])')
        page.fill('input[data-filter="cat"]', "")
        page.fill('form[data-add="town"] input', "Mwingi, Kyuso")   # several at once
        page.press('form[data-add="town"] input', "Enter")
        assert "3 selected" in page.inner_text("#count-town"), page.inner_text("#count-town")
        page.click("#mix")
        page.click("#orig")
        assert "Searches to run" in page.inner_text("#queue")
        assert page.locator("#queue [data-rm]").count() >= 9
        shot("6-find")

        # ---- the AI planner: talk, review the suggestions, add them to the list (nothing runs by itself)
        before = page.locator("#queue [data-rm]").count()
        page.fill("#plan-text", "pharmacies in Kisii <b>and</b> Migori")
        page.press("#plan-text", "Enter")                                   # Enter sends
        page.wait_for_selector("#plan-out .plan-msg.ai")
        assert page.locator("#plan-out [data-plan-rm]").count() == 3
        assert page.locator("#plan-chat b").count() == 0, "what you type must show as text, never as HTML"
        assert "Pharmacies and a clinic" in page.inner_text("#plan-chat")
        page.locator("#plan-out [data-plan-rm]").first.click()                 # drop the first suggestion
        assert page.locator("#plan-out [data-plan-rm]").count() == 2
        shot("6a-planner-suggestions")
        page.click("#plan-add")
        assert page.locator("#plan-out [data-plan-rm]").count() == 0
        queue = page.inner_text("#queue")
        assert "Clinic in Kisii" in queue and "Pharmacy in Migori" in queue and "Pharmacy in Kisii" not in queue, queue
        assert page.locator("#queue [data-rm]").count() == before + 2
        assert page.locator("#plan-new").is_visible()
        page.click("#plan-new")
        assert not page.locator("#plan-new").is_visible() and page.locator("#plan-chat").count() == 0
        shot("6b-find-planner")

        # ---- settings
        page.click('#nav a[data-view="settings"]')
        page.wait_for_selector("#sform")
        page.fill('input[name=email_daily_cap]', "55")
        page.click("#sform button[type=submit]")
        page.wait_for_selector(".toast")
        shot("7-settings")
        assert call("GET", "/settings")["email_daily_cap"] == 55
        page.fill('input[name=slogan]', "Leads that reply")
        page.fill('textarea[name=ai_pitch]', "Stock software")
        page.click("#sform button[type=submit]")
        page.wait_for_function("document.querySelector('#slogan').textContent === 'Leads that reply'")
        assert call("GET", "/settings")["ai_pitch"] == "Stock software"
        page.click("#test-ai")
        page.wait_for_selector(".toast.bad")   # no key yet: a clear error, not a crash

        # ---- Today: replies to answer, with the message already written when a button is pressed
        page.click('#nav a[data-view="today"]')
        page.wait_for_selector("text=They replied")
        assert "Follow-ups due" in page.inner_text("#content") and "No reply after" in page.inner_text("#content")
        shot("4-today")
        page.locator('[data-ch="whatsapp"]').first.click()
        page.wait_for_selector("#send-form")
        page.wait_for_function("document.querySelector('#send-form textarea').value.length > 0")   # the AI wrote it
        assert "Open WhatsApp" in page.inner_text("#send-form") and "press send" in page.inner_text("#send-form")
        page.keyboard.press("Escape")
        page.wait_for_selector(".drawer", state="detached")
        # deal value and the offer are saved from the lead
        page.click('#nav a[data-view="leads"]')
        page.wait_for_selector("table.t tbody tr")
        page.locator("table.t tbody tr").nth(5).click()
        page.wait_for_selector(".drawer")
        page.fill('input[name="offer"]', "Sales dashboard")
        page.fill('input[name="deal_value"]', "45000")
        page.click('#edit button[type=submit]')
        page.wait_for_selector(".toast")
        page.wait_for_selector(".drawer")
        assert page.input_value('input[name="deal_value"]') == "45000"
        page.keyboard.press("Escape")
        page.wait_for_selector(".drawer", state="detached")
        # follow-ups can be chosen when writing a campaign
        page.click('#nav a[data-view="outreach"]')
        page.wait_for_selector("#cform")
        assert page.locator('select[name="followups"] option').count() == 4
        shot("5-outreach")

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
