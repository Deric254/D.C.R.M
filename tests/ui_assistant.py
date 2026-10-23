"""Drive the Assistant page in Chromium against a live backend and a stand-in AI. Run: python tests/ui_assistant.py [screenshot_dir]"""
import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import authkit  # noqa: E402
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="shots-"))
SHOTS.mkdir(parents=True, exist_ok=True)
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"
DATA = tempfile.mkdtemp(prefix="dericbi-ui-asst-")
SESSION = authkit.Session(BASE)
call = SESSION.call


class FakeAI(BaseHTTPRequestHandler):
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = req["messages"][0]["content"]
        if system.startswith("You are a strict reviewer"):
            out = {"ok": True, "issues": []}
        elif "This is an email" in system:
            out = {"reply": "Hello,\n\nThank you for writing. What do you use to track stock today?\n\nKind regards,\nDeric's AI assistant at DericBI",
                   "subject": "Re: Stock help", "intent": "question", "stage": "discovery", "confidence": 90, "needs_human": False, "pain": "", "value_pieces": [], "offer": "none", "reason": "ok"}
        else:
            out = {"reply": "A ready-to-use system is KES 20,000 once, or KES 1,000 to start then KES 200 per month. Which suits you?", "intent": "price", "stage": "value_shown",
                   "confidence": 90, "needs_human": False, "pain": "stock kept in books", "value_pieces": ["no system"], "offer": "system_once", "promise": "", "reason": "ok"}
        body = json.dumps({"choices": [{"message": {"content": json.dumps(out)}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


ai = HTTPServer(("127.0.0.1", 0), FakeAI)
threading.Thread(target=ai.serve_forever, daemon=True).start()
server = subprocess.Popen([sys.executable, str(ROOT / "backend" / "main.py"), "--port", str(PORT), "--data-dir", DATA],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(60):
        try:
            call("GET", "/health"); break
        except Exception:
            time.sleep(0.25)
    else:
        raise SystemExit("server didn't start: " + server.stdout.read())
    SESSION.sign_in()
    lead = call("POST", "/leads", {"name": "Meru Chemist", "sector": "Pharmacy", "town": "Meru", "phone": "0711300111"})["id"]
    call("PUT", "/settings", {"ai_provider": "custom", "ai_custom_url": f"http://127.0.0.1:{ai.server_port}", "ai_custom_model": "m", "send_window_enabled": False,
                              "sms_gateway_url": "127.0.0.1:9", "sms_gateway_user": "u", "sms_gateway_pass": "p"})
    call("POST", f"/leads/{lead}/reply", {"text": "How much for the system?", "channel": "sms"})

    from playwright.sync_api import sync_playwright
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_cookies(SESSION.cookies())
        page = context.new_page()
        page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("dialog", lambda d: (problems.append(f"UNEXPECTED JS dialog: {d.message}"), d.dismiss()))

        page.goto(BASE + "/#/closer")
        page.wait_for_selector("#c-switch")
        assert page.get_attribute("#c-switch", "aria-checked") == "false" and "OFF" in page.inner_text("#cconvo")
        page.click("#c-switch")                                                           # conversation mode ON
        page.wait_for_function("document.querySelector('#c-switch').getAttribute('aria-checked') === 'true'")
        assert "ON" in page.inner_text("#cconvo")
        page.wait_for_function("document.querySelector('#pills').innerText.includes('Assistant on')", timeout=15000)
        assert call("GET", "/closer/status")["mode"] == "suggest"
        page.select_option("#c-way", "auto")                                              # auto needs the Test pack: the page says so
        page.wait_for_function("document.querySelector('#cstat').innerText.includes('paused until the Test pack passes')")
        page.select_option("#c-way", "suggest")
        page.wait_for_function("!document.querySelector('#cstat').innerText.includes('paused until the Test pack')")

        # the reply the lead sent over SMS is drafted by the assistant's own thread (or press-to-draft)
        r = call("POST", f"/closer/leads/{lead}/draft")
        assert r["action"] == "draft", r
        page.reload(); page.wait_for_selector("article.reply")
        card = page.inner_text("article.reply")
        assert "Text" in card and "How much for the system?" in card and "no system" in card and "Send by Text" in card, card
        page.screenshot(path=str(SHOTS / "assistant-inbox.png"))
        page.fill("[data-body]", "KES 20,000 once, or KES 1,000 to start then KES 200 per month. Which suits you best?")
        page.click("[data-send]")
        page.wait_for_selector("text=Queued")
        time.sleep(0.5)
        st = call("GET", "/closer/status")["stats"]
        assert st["pending"] == 0 and st["edited"] + st["approved"] == 1, st
        assert "Assistant: SMS to Meru Chemist" in json.dumps(call("GET", "/campaigns"))

        page.click('[data-tab="try"]'); page.wait_for_selector("#t-run")
        page.select_option("#t-channel", "email"); page.select_option("#t-gap", "72")
        page.fill("#t-them", "Hello, do you do stock reports for pharmacies?"); page.click("#t-run")
        page.wait_for_selector("#t-out .quote")
        assert "Subject: Re:" in page.inner_text("#t-out") and "Second check: approved" in page.inner_text("#t-out")
        page.screenshot(path=str(SHOTS / "assistant-try.png"))

        page.click('[data-tab="pack"]'); page.wait_for_selector("#p-run")
        assert "Not run yet" in page.inner_text("#cbody")
        page.click('[data-tab="setup"]'); page.wait_for_selector("input[name=closer_report_max]")
        page.fill("input[name=closer_report_max]", "1500"); page.click("#c-form button[type=submit]")
        page.wait_for_selector("text=Saved")
        assert call("GET", "/settings")["closer_report_max"] == 1500
        page.screenshot(path=str(SHOTS / "assistant-setup.png"))

        page.click('[data-tab="inbox"]'); page.wait_for_selector("#c-switch")
        page.click("#c-switch")                                                           # OFF: silent
        page.wait_for_function("document.querySelector('#c-switch').getAttribute('aria-checked') === 'false'")
        assert call("GET", "/closer/status")["mode"] == "off"
        page.set_viewport_size({"width": 390, "height": 800})                             # phone width: nothing spills sideways
        page.goto(BASE + "/#/closer"); page.wait_for_selector("#c-switch")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        browser.close()
    real = [x for x in problems if "ERR_" not in x and "status of 40" not in x]
    if real:
        print("PROBLEMS:\n" + "\n".join(real)); sys.exit(1)
    print("ASSISTANT UI OK", SHOTS)
finally:
    server.terminate()
    try:
        server.wait(5)
    except Exception:
        server.kill()
