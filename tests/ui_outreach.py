"""Drive the seamless outreach flow in Chromium: tick leads on the Leads page, Reach out, best way for each lead,
and prove nobody can be reached twice. Run: python tests/ui_outreach.py [screenshot_dir]"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import authkit  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="outreach-shots-"))
SHOTS.mkdir(parents=True, exist_ok=True)
PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"
S = authkit.Session(BASE)
server = subprocess.Popen([sys.executable, str(ROOT / "backend" / "main.py"), "--port", str(PORT), "--data-dir", tempfile.mkdtemp()],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(80):
        try:
            S.call("GET", "/health"); break
        except Exception:
            time.sleep(0.25)
    S.sign_in()
    # email and the phone are "connected" (nothing is listening, so nothing is really sent); WhatsApp is not linked
    S.call("PUT", "/settings", {"smtp_host": "127.0.0.1", "smtp_port": 9, "smtp_security": "none", "from_email": "me@example.co.ke",
                                "sms_gateway_url": "127.0.0.1:9", "sms_gateway_user": "u", "sms_gateway_pass": "p", "send_window_enabled": False})
    people = [("Meru Chemist", "Pharmacy", "0700000601", "chem@meru.test"), ("Kisii Agrovet", "Agrovet", "0700000602", ""),
              ("Embu Hardware", "Hardware", "", "hw@embu.test"), ("Thika Salon", "Salon", "064 30123", "salon@thika.test"),
              ("Nothing Shop", "Retail", "", "")]
    ids = {n: S.call("POST", "/leads", {"name": n, "sector": sec, "town": "Meru", "phone": p, "email": e})["id"] for n, sec, p, e in people}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1200, "height": 1300}); ctx.add_cookies(S.cookies()); page = ctx.new_page()
        errs = []; page.on("pageerror", lambda e: errs.append(str(e))); page.on("dialog", lambda d: d.accept())

        # ---- Leads page: tick four leads, Reach out
        page.goto(BASE + "/#/leads"); page.wait_for_selector("#table input[data-sel]")
        for name in ("Meru Chemist", "Kisii Agrovet", "Embu Hardware", "Thika Salon"):
            page.locator(f"tr:has-text('{name}') input[data-sel]").check()
        page.screenshot(path=str(SHOTS / "o1-leads-selected.png"))
        page.click("#b-reach"); page.wait_for_selector("#cform"); page.wait_for_selector("#pick label")
        assert "You chose 4 leads" in page.inner_text("#preset-note") and "4 can be messaged now" in page.inner_text("#preset-note")
        assert page.is_checked("input[name=channel][value=auto]"), "best way should be the starting choice"
        assert page.locator("#pick label").count() == 4
        way = lambda name: page.locator(f"#pick label:has-text('{name}')").inner_text()
        assert "Text" in way("Kisii Agrovet") and "Email" in way("Embu Hardware") and "Email" in way("Meru Chemist") and "Email" in way("Thika Salon")
        page.wait_for_selector("#preview .sample")
        pv = page.inner_text("#preview")
        assert "4 leads will get a message: " in pv and "by email" in pv and "by text" in pv, pv
        page.screenshot(path=str(SHOTS / "o2-best-way-form.png"), full_page=True)
        page.wait_for_selector("#go:not([disabled])")
        page.click("#go"); page.wait_for_selector("#dlg-ok"); assert "ONE message" in page.inner_text("#dlg"); page.click("#dlg-ok"); page.wait_for_selector("#clist tr")

        # ---- each lead has exactly one message, on a way that suits them
        camps = S.call("GET", "/campaigns"); first = [c for c in camps if c["channel"] == "auto" and not c["followup_of"]][0]
        assert first["total"] == 4 and (first["n_email"], first["n_sms"], first["n_whatsapp"]) == (3, 1, 0), first
        assert "Best way (3 email, 1 text)" in page.inner_text("#clist")
        for name, i in ids.items():
            d = S.call("GET", f"/leads/{i}")
            n_out = [m for m in d["messages"] if m["direction"] == "out"]
            assert len(n_out) == (0 if name == "Nothing Shop" else 1), (name, n_out)
        page.screenshot(path=str(SHOTS / "o3-campaign-list.png"), full_page=True)

        # ---- the same leads again: nobody can be reached twice
        page.goto(BASE + "/#/leads"); page.wait_for_selector("#table input[data-sel]")
        for name in ("Meru Chemist", "Kisii Agrovet"):
            page.locator(f"tr:has-text('{name}') input[data-sel]").check()
        page.click("#b-reach"); page.wait_for_selector("#cform"); page.wait_for_selector("#preset-note .notice")
        assert "0 can be messaged now" in page.inner_text("#preset-note"), page.inner_text("#preset-note")
        page.wait_for_selector("#pick .empty"); assert page.is_disabled("#go")
        # "Show everyone instead" shows only leads nobody has messaged: the one with nothing to reach is not offered either
        page.click("#preset-clear"); page.wait_for_selector("#pick .empty")
        assert page.locator("#pick label").count() == 0
        # the single-lead drawer refuses a second message too
        r = S.call("POST", f"/leads/{ids['Meru Chemist']}/send", {"channel": "email", "subject": "again", "body": "again"})
        assert r.get("error") == 400 and ("unlocks" in r["detail"] or "already waiting" in r["detail"]), r
        print("page errors:", errs); assert not errs
        browser.close()
    print("UI OUTREACH OK")
finally:
    server.terminate()
