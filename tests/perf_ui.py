"""How each screen feels on a weak computer: Chrome with the CPU slowed down, against a large database.
Measures the time until the screen is usable and how long the page freezes (long tasks) while it loads.
Run:  python tests/perf_ui.py --data /path/to/seeded/db [--throttle 6] [--json out.json]
(make a big database with: python -c "import sys; sys.path.insert(0,'tests'); import perf_bench; perf_bench.seed('/tmp/db', 30000)")"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import authkit  # noqa: E402

SCREENS = [("Overview", "#/overview", ".kpi"), ("Today", "#/today", "#content"), ("Leads", "#/leads", "#table"),
           ("Replies", "#/replies", "#content"), ("Find leads", "#/find", "#content"),
           ("Outreach (list)", "#/outreach", "#clist"), ("Settings", "#/settings", "#a-pform")]

OBSERVER = """
window.__long = 0; window.__longest = 0; window.__inflight = 0; window.__done = 0; window.__lastDone = 0;
const _fetch = window.fetch;
window.fetch = function () { window.__inflight++; return _fetch.apply(this, arguments).finally(() => { window.__inflight--; window.__done++; window.__lastDone = performance.now(); }); };
try { new PerformanceObserver((l) => { for (const e of l.getEntries()) { window.__long += e.duration; window.__longest = Math.max(window.__longest, e.duration); } })
      .observe({ type: 'longtask', buffered: true }); } catch (e) {}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--throttle", type=float, default=6.0)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    port = 8812
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([sys.executable, str(ROOT / "backend" / "main.py"), "--port", str(port), "--data-dir", a.data],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    S = authkit.Session(base)
    results = []
    try:
        for _ in range(200):
            try:
                S.call("GET", "/health"); break
            except Exception:
                time.sleep(0.1)
        S.sign_in()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1366, "height": 768}); ctx.add_cookies(S.cookies())
            page = ctx.new_page()
            cdp = ctx.new_cdp_session(page)
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": a.throttle})
            page.add_init_script(OBSERVER)
            page.goto(base + "/#/overview"); page.wait_for_selector(".app", state="visible")
            for name, hashpath, ready in SCREENS:
                page.goto(base + ("/#/settings" if hashpath == "#/overview" else "/#/overview")); page.wait_for_timeout(300)   # always arrive from another screen
                page.evaluate("window.__long = 0; window.__longest = 0; window.__done = 0")
                t0 = time.time()
                page.goto(base + "/" + hashpath)
                try:
                    page.wait_for_selector(ready, state="attached", timeout=120000)
                    # ready = this screen's own data calls have all finished and the page has drawn them
                    page.wait_for_function("window.__done > 0 && window.__inflight === 0 && performance.now() - window.__lastDone > 150", timeout=120000)
                    ms = round((time.time() - t0) * 1000)
                except Exception as e:
                    print("  could not measure", name, "-", str(e).splitlines()[0][:100])
                    ms = -1
                page.wait_for_timeout(400)
                long_total, longest = page.evaluate("[window.__long, window.__longest]")
                heap = page.evaluate("performance.memory ? Math.round(performance.memory.usedJSHeapSize/1048576) : 0")
                nodes = page.evaluate("document.getElementsByTagName('*').length")
                results.append({"screen": name, "ready_ms": ms, "frozen_ms": round(long_total), "longest_freeze_ms": round(longest), "dom_nodes": nodes, "js_mb": heap})
            # the New campaign form is the heaviest screen: audience list of up to 1000 leads, previews, counts
            page.goto(base + "/#/outreach"); page.wait_for_selector("#clist")
            page.evaluate("window.__long = 0; window.__longest = 0")
            t0 = time.time()
            page.click("#new"); page.wait_for_selector("#pick label", timeout=120000); page.wait_for_selector("#preview .sample", timeout=120000); page.wait_for_function("window.__inflight === 0")
            ms = round((time.time() - t0) * 1000); page.wait_for_timeout(400)
            long_total, longest = page.evaluate("[window.__long, window.__longest]")
            results.append({"screen": "Outreach: open New campaign", "ready_ms": ms, "frozen_ms": round(long_total), "longest_freeze_ms": round(longest),
                            "dom_nodes": page.evaluate("document.getElementsByTagName('*').length"),
                            "js_mb": page.evaluate("performance.memory ? Math.round(performance.memory.usedJSHeapSize/1048576) : 0")})
            # ticking all of the list, as someone would
            page.evaluate("window.__long = 0; window.__longest = 0")
            t0 = time.time(); page.click("#pick-all"); page.wait_for_selector("#preview .sample"); page.wait_for_timeout(600)
            long_total, longest = page.evaluate("[window.__long, window.__longest]")
            results.append({"screen": "Outreach: Select all shown", "ready_ms": round((time.time() - t0) * 1000), "frozen_ms": round(long_total),
                            "longest_freeze_ms": round(longest), "dom_nodes": 0, "js_mb": 0})
            browser.close()
    finally:
        server.terminate()
    print(f"CPU slowed {a.throttle}x. 'ready' = until the screen is usable; 'frozen' = total time the page could not react.")
    for r in results:
        flag = "  <-- slow" if r["ready_ms"] > 3000 or r["longest_freeze_ms"] > 500 or r["ready_ms"] < 0 else ""
        print(f"  {r['screen']:<30} ready {r['ready_ms']:>6} ms | frozen {r['frozen_ms']:>5} ms (longest {r['longest_freeze_ms']:>4}) | {r['dom_nodes']:>5} nodes{flag}")
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
