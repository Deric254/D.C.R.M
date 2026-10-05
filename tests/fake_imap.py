"""Tiny in-memory IMAP server, just enough for imaplib: LOGIN, SELECT, UID SEARCH, UID FETCH."""
import re
import socketserver
import threading


class FakeImap(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, user="u", password="p"):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.user, self.password = user, password
        self.messages = {}  # uid -> raw bytes
        self.next_uid = 1
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def add(self, raw: bytes) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = raw
        return uid

    def stop(self):
        self.shutdown()
        self.server_close()


class _Handler(socketserver.StreamRequestHandler):
    def send(self, line: bytes):
        self.wfile.write(line + b"\r\n")
        self.wfile.flush()

    def handle(self):
        srv = self.server
        self.send(b"* OK fake imap ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            parts = line.decode().strip().split(" ", 2)
            tag = parts[0]
            cmd = parts[1].upper() if len(parts) > 1 else ""
            rest = parts[2] if len(parts) > 2 else ""
            if cmd == "CAPABILITY":
                self.send(b"* CAPABILITY IMAP4rev1")
                self.send(f"{tag} OK done".encode())
            elif cmd == "LOGIN":
                args = re.findall(r'"([^"]*)"|(\S+)', rest)
                u, p = [a or b for a, b in args][:2]
                if u == srv.user and p == srv.password:
                    self.send(f"{tag} OK logged in".encode())
                else:
                    self.send(f"{tag} NO [AUTHENTICATIONFAILED] bad credentials".encode())
            elif cmd in ("SELECT", "EXAMINE"):
                self.send(f"* {len(srv.messages)} EXISTS".encode())
                self.send(f"{tag} OK [READ-ONLY] selected".encode())
            elif cmd == "UID":
                sub, _, args = rest.partition(" ")
                if sub.upper() == "SEARCH":
                    uids = sorted(srv.messages)
                    m = re.search(r"UID (\d+):\*", args)
                    if m:
                        lo = int(m.group(1))
                        hit = [u for u in uids if u >= lo]
                        if not hit and uids:
                            hit = [uids[-1]]  # RFC quirk: n:* always includes the newest
                        uids = hit
                    self.send(("* SEARCH " + " ".join(map(str, uids))).encode())
                    self.send(f"{tag} OK done".encode())
                elif sub.upper() == "FETCH":
                    uid = int(args.split()[0])
                    raw = srv.messages.get(uid)
                    if raw is None:
                        self.send(f"{tag} OK done".encode())
                    else:
                        self.wfile.write(f"* 1 FETCH (UID {uid} BODY[] {{{len(raw)}}}\r\n".encode() + raw + b")\r\n")
                        self.wfile.flush()
                        self.send(f"{tag} OK done".encode())
                else:
                    self.send(f"{tag} BAD unknown".encode())
            elif cmd == "NOOP":
                self.send(f"{tag} OK".encode())
            elif cmd == "LOGOUT":
                self.send(b"* BYE")
                self.send(f"{tag} OK bye".encode())
                return
            else:
                self.send(f"{tag} BAD unknown".encode())
