"""A stand-in for WhatsApp Web, served locally, so the real browser driver can be tested without WhatsApp.

It imitates the parts the driver depends on: the chat list, the link code, the message box under <footer> filled in
from the ?text= address, an outgoing bubble with a 'clock' icon that turns into a tick, and the 'phone number is invalid'
popup. It is only as faithful as those parts: the live site can still differ, which is why every page selector the driver
uses lives in one table (whatsapp.SEL).
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

MODE = {"mode": "ok", "tick_ms": 400}   # ok | qr | stuck (the clock never turns into a tick)
SENT = []                                # (phone, text) for every message the page was asked to send

CHATS = '<div id="pane-side"><div>Chats</div></div>'
QR = '<canvas aria-label="Scan this QR code to link a device!" width="40" height="40"></canvas>'

CHAT = """<!doctype html><html><body>
<div id="pane-side"></div>
<div id="main"><div id="msgs"></div>
<footer><div contenteditable="true" id="box" style="min-height:30px;border:1px solid #888"></div></footer></div>
<script>
const p = new URLSearchParams(location.search);
const box = document.getElementById('box');
box.innerText = p.get('text') || '';
box.addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const t = box.innerText.trim(); if (!t) return;
  const d = document.createElement('div'); d.className = 'message-out';
  d.innerHTML = '<span class="txt"></span><span data-icon="msg-time"></span>';
  d.querySelector('.txt').innerText = t;
  document.getElementById('msgs').appendChild(d); box.innerText = '';
  fetch('/sent?phone=' + encodeURIComponent(p.get('phone')) + '&text=' + encodeURIComponent(t));
  if (%(stuck)s) return;
  setTimeout(() => { const i = d.querySelector('[data-icon=msg-time]'); if (i) i.remove(); }, %(tick)d);
});
</script></body></html>"""

INVALID = """<!doctype html><html><body><div id="pane-side"></div>
<div data-animate-modal-popup="true"><div>Phone number shared via url is invalid.</div><div role="button">OK</div></div>
</body></html>"""


def serve():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _html(self, html):
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/sent":
                SENT.append((q.get("phone", ""), q.get("text", "")))
                return self._html("ok")
            if MODE["mode"] == "qr":
                return self._html(f"<html><body>{QR}</body></html>")
            if u.path == "/send":
                if q.get("phone", "").endswith("999"):
                    return self._html(INVALID)
                return self._html(CHAT % {"stuck": "true" if MODE["mode"] == "stuck" else "false", "tick": MODE["tick_ms"]})
            return self._html(f"<html><body>{CHATS}</body></html>")

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
