"""Guards for UI fixes that must never quietly come back. No dependencies. Run:  python tests/test_ui_guards.py

1. The sign-in / create-account screen must scroll on its own. html and body are overflow:hidden (only the app's
   content area scrolls), so without this the create-account form is cut off on a normal-sized window.
2. A background refresh of the Overview must not throw the user back to the top of the page.
"""
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "backend" / "static"
problems = []


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def check(ok, message):
    if not ok:
        problems.append(message)


# 1. sign-in screen scrolls by itself
css = read("style.css")
m = re.search(r"body\.locked\s+\.auth\s*\{([^}]*)\}", css)
check(m is not None, "style.css: the 'body.locked .auth' rule is missing")
if m:
    rule = m.group(1)
    check(re.search(r"position:\s*fixed", rule), "style.css: 'body.locked .auth' must be position: fixed (it fills the window)")
    check(re.search(r"inset:\s*0", rule), "style.css: 'body.locked .auth' must have inset: 0")
    check(re.search(r"overflow-y:\s*auto", rule), "style.css: 'body.locked .auth' must have overflow-y: auto, or the create-account form cannot scroll")

# 2. quiet refresh keeps the scroll position
core = read("core.js")
check("App.refreshQuiet" in core, "core.js: App.refreshQuiet is missing")
check(re.search(r"async function route\(silent\)", core), "core.js: route() must take the 'silent' argument")
check(re.search(r"silent\s*!==\s*true\)\s*\{\s*el\.scrollTop\s*=\s*0", core), "core.js: route() must only jump to the top when it is not a quiet refresh")
check(re.search(r"silent\s*===\s*true\)\s*el\.scrollTop\s*=\s*keep", core), "core.js: route() must restore the scroll position after a quiet refresh")

overview = read("view-overview.js")
# the 30 s refresh is a timer: App.every(...) (pauses while the window is hidden) or a plain setInterval
timer = [ln for ln in overview.splitlines() if ("App.every" in ln or "setInterval" in ln) and "30000" in ln]
check(bool(timer), "view-overview.js: expected the 30-second refresh timer")
check(all("App.refreshQuiet()" in ln and "App.refresh()" not in ln for ln in timer),
      "view-overview.js: the timer must call App.refreshQuiet(), not App.refresh() (that jumps the page to the top)")

if problems:
    print("UI guard FAILED:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("UI guards OK")
