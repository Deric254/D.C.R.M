"""A tiny fake of the parts of Google Maps the scraper reads (feed, place links, lazily loaded detail panel)."""
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def start_mock_maps(delay_for_slow=0.0):
    """Queries containing 'Slow' return shops 101-110 and each place page takes `delay_for_slow` seconds."""
    holder = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def page(self, html):
            b = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            base = f"http://127.0.0.1:{holder['port']}"
            if self.path.startswith("/maps/search/"):
                off = 100 if "Slow" in self.path else 0

                def a(n):
                    return (f'<a style="display:block;height:60px" href="{base}/maps/place/Shop+{n}/data=!4m7!3m6!1s0x1:0x{n:x}!8m2" '
                            f'aria-label="Shop {n}"></a>')
                first = "".join(a(n) for n in range(off + 1, off + 6))
                more = "".join(a(n) for n in range(off + 6, off + 11))
                self.page(f"""<html><body><div id=feed role="feed" style="height:150px;overflow:auto">{first}
                  <div style="height:400px">filler</div></div>
                  <script>let added=false;const f=document.getElementById('feed');
                  f.addEventListener('scroll',()=>{{if(!added && f.scrollTop+f.clientHeight>=f.scrollHeight-5){{added=true;
                  f.insertAdjacentHTML('afterbegin', `{more}`);}}}});</script></body></html>""")
            elif self.path.startswith("/maps/place/"):
                n = int(self.path.split("Shop+")[1].split("/")[0])
                if n > 100 and delay_for_slow:
                    time.sleep(delay_for_slow)
                self.page(f"""<html><body><h1>Shop {n}</h1><div id=d></div><script>setTimeout(()=>{{document.getElementById('d').innerHTML=
                  `<button data-item-id="phone:tel:0712000{n:03d}" aria-label="Phone: 0712 000 {n:03d}"></button>
                   <button data-item-id="address" aria-label="Address: {n} Main St, Meru"></button>
                   <a data-item-id="authority" href="http://shop{n}.co.ke">site</a>
                   <button jsaction="pane.rating.category">Pharmacy</button>`}},400);</script></body></html>""")
            else:
                self.send_response(404)
                self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), H)
    holder["port"] = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
