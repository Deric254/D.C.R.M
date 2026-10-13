"""Drive the real sign-in, registration, recovery, people and licence screens in Chromium, across TWO separate installs
(the owner's computer and another computer), against servers built with an owner key like the real installer.
Run: python tests/ui_auth.py [screenshot_dir]"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
import auth  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="auth-shots-"))
SHOTS.mkdir(parents=True, exist_ok=True)
KEY, PUB = auth.make_owner_key()
PW = "Maize&Tractor#2468"
ANSWERS = ["Kamunyaka Primary", "Kisii", "Otieno"]

LAUNCH = ("import sys; sys.path.insert(0, %r); import owner_key; owner_key.OWNER_PUBLIC_KEY = %r; import main; "
          "sys.argv = ['main', '--port', sys.argv[1], '--data-dir', sys.argv[2]]; main.main()")


def start(port, data):
    p = subprocess.Popen([sys.executable, "-c", LAUNCH % (str(ROOT / "backend"), PUB), str(port), data], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    import urllib.request
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2); return p
        except Exception:
            time.sleep(0.25)
    raise SystemExit("server didn't start: " + p.stdout.read())


def fill_questions(page, prefix):
    for i, a in enumerate(ANSWERS):
        page.select_option(f"#{prefix}-q{i}", index=i)
        page.fill(f"#{prefix}-a{i}", a)


def set_password(page, prefix, pw):
    page.fill(f"#{prefix}-pw", pw); page.fill(f"#{prefix}-pw2", pw)


A, B = start(8801, tempfile.mkdtemp()), start(8802, tempfile.mkdtemp())
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        errs = []
        ctx = browser.new_context(viewport={"width": 1200, "height": 1000}); page = ctx.new_page()
        page.on("pageerror", lambda e: errs.append(str(e)))
        base_a = "http://127.0.0.1:8801"

        # ---- first run on the owner's computer
        page.goto(base_a); page.wait_for_selector("#sform")
        assert not page.is_visible(".app"), "the app must be hidden until someone signs in"
        page.screenshot(path=str(SHOTS / "a1-setup.png"), full_page=True)
        assert page.input_value("input[readonly]") == "Deric Marangu"
        # a weak password is explained, then refused by the server too
        page.fill("#s-pw", "weak"); page.wait_for_selector("#s-hint li")
        assert "12 characters" in page.inner_text("#s-hint")
        page.fill("#s-pw2", "weak"); fill_questions(page, "s"); page.fill("#s-key", KEY)
        page.click("#s-go"); page.wait_for_selector("#auth-err .notice"); assert "too weak" in page.inner_text("#auth-err")
        # the wrong key is refused, the suggested password is accepted
        page.click("#s-suggest"); page.wait_for_selector("#s-hint span"); assert "Strong" in page.inner_text("#s-hint")
        suggested = page.input_value("#s-pw"); assert not auth.password_problems(suggested, "Deric Marangu")
        page.fill("#s-key", auth.make_owner_key()[0]); page.click("#s-go"); page.wait_for_selector("#auth-err .notice")
        assert "owner key" in page.inner_text("#auth-err")
        page.fill("#s-key", KEY); set_password(page, "s", PW); page.click("#s-go")
        page.wait_for_selector(".app", state="visible"); assert not page.is_visible(".auth-card")
        assert "Deric Marangu" in page.inner_text("#who") and "Owner" in page.inner_text("#who")

        # ---- sign out, wrong password, forgot password (answers + owner key), sign in
        page.click("#signout"); page.wait_for_selector("#lform")
        page.screenshot(path=str(SHOTS / "a2-login.png"))
        page.fill("#l-pw", "wrong"); page.click("#l-go"); page.wait_for_selector("#auth-err .notice")
        assert "Wrong username or password" in page.inner_text("#auth-err") and "attempts left" in page.inner_text("#auth-err")
        page.click("#l-forgot"); page.wait_for_selector("#rform"); page.click("#r-go"); page.wait_for_selector("#r-a0")
        assert "first school" in page.inner_text("#r-step") and page.is_visible("#r-key")
        for i, a in enumerate(ANSWERS):
            page.fill(f"#r-a{i}", a.upper())
        page.fill("#r-key", KEY); set_password(page, "r", "Brand#New8642Pw"); page.click("#r-go")
        page.wait_for_selector("#lform"); page.fill("#l-pw", "Brand#New8642Pw"); page.click("#l-go")
        page.wait_for_selector(".app", state="visible")

        # ---- Settings: add a person, see the temporary password once
        page.goto(base_a + "/#/settings"); page.wait_for_selector("#a-add")
        page.fill("#a-newname", "Ann Wanjiru"); page.click("#a-add button"); page.wait_for_selector("#a-temp code")
        temp = page.inner_text("#a-temp code"); assert not auth.password_problems(temp)
        page.wait_for_timeout(1500)
        assert page.locator("#a-temp code").inner_text() == temp, "adding a person must not redraw Settings and wipe the temporary password"
        page.screenshot(path=str(SHOTS / "a3-people.png"), full_page=True)
        ctx2 = browser.new_context(viewport={"width": 1200, "height": 1000}); p2 = ctx2.new_page()
        p2.on("pageerror", lambda e: errs.append(str(e)))
        p2.goto(base_a); p2.wait_for_selector("#lform"); p2.fill("#l-user", "Ann Wanjiru"); p2.fill("#l-pw", temp); p2.click("#l-go")
        p2.wait_for_selector("#fform"); assert not p2.is_visible(".app")                       # she must finish setting up first
        p2.fill("#f-cur", temp); set_password(p2, "f", "Mine#Is9753Strong"); fill_questions(p2, "f"); p2.click("#f-go")
        p2.wait_for_selector(".app", state="visible")
        p2.goto(base_a + "/#/settings"); p2.wait_for_selector("#a-pform")
        assert p2.locator("#a-add").count() == 0 and p2.locator("#l-make").count() == 0       # members can't manage people or licences
        ctx2.close()

        # ---- another computer: needs the owner's licence
        ctx3 = browser.new_context(viewport={"width": 1200, "height": 1100}); p3 = ctx3.new_page()
        p3.on("pageerror", lambda e: errs.append(str(e)))
        base_b = "http://127.0.0.1:8802"
        p3.goto(base_b); p3.wait_for_selector("#sform"); p3.click("label.opt:has(input[name=mode][value=member])"); p3.wait_for_selector("#s-code")
        code = p3.inner_text("#s-code"); assert len(code) == 14
        p3.screenshot(path=str(SHOTS / "a4-other-computer.png"), full_page=True)
        # the owner signs a licence for that code in Settings
        page.fill("#l-code", code); page.fill("#l-name", "Bob Otieno"); page.fill("#l-key", KEY); page.click("#l-make")
        page.wait_for_selector("#l-out textarea"); licence = page.input_value("#l-out textarea"); assert licence.startswith("DBI1.")
        assert page.input_value("#l-key") == "", "the owner key must not stay on screen"
        page.screenshot(path=str(SHOTS / "a5-licence.png"), full_page=True)
        # a licence for another code does not work; the right one does and fixes the username
        p3.fill("#s-lic", licence[:-3] + "AAA"); set_password(p3, "s", "Orange#Cattle7531"); fill_questions(p3, "s"); p3.click("#s-go")
        p3.wait_for_selector("#auth-err .notice"); assert "owner" in p3.inner_text("#auth-err").lower()
        p3.fill("#s-lic", licence); p3.click("#s-go"); p3.wait_for_selector(".app", state="visible")
        assert "Bob Otieno" in p3.inner_text("#who") and "Member" in p3.inner_text("#who")
        p3.goto(base_b + "/#/settings"); p3.wait_for_selector("#a-pform"); assert "Licence valid until" in p3.inner_text("#account-panels")
        p3.screenshot(path=str(SHOTS / "a6-licensed-settings.png"), full_page=True)
        print("page errors:", errs); assert not errs
        browser.close()
    print("UI AUTH OK")
finally:
    A.terminate(); B.terminate()
