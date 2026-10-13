"""Sign-in helper for the tests that run against a real server process (a build with no owner key set lets the
owner account be made with just the name and a strong password)."""
import http.cookiejar
import json
import urllib.error
import urllib.request

PASSWORD = "Maize&Tractor#2468"
QUESTIONS = [{"q": "What was the name of your first school?", "a": "Kamunyaka Primary"},
             {"q": "Which town or village did your mother grow up in?", "a": "Kisii"},
             {"q": "What was the name of your childhood best friend?", "a": "Otieno"}]


class Session:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, timeout=30):
        req = urllib.request.Request(self.base + "/api" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return {"error": e.code, **json.loads(e.read() or b"{}")}

    def sign_in(self):
        r = self.call("POST", "/auth/setup", {"mode": "owner", "username": "Deric Marangu", "password": PASSWORD, "questions": QUESTIONS})
        if r.get("error") == 409:
            r = self.call("POST", "/auth/login", {"username": "Deric Marangu", "password": PASSWORD})
        assert r.get("ok"), r
        return self

    def cookies(self):
        """The session as cookies a browser context can use, so a browser test starts signed in."""
        return [{"name": c.name, "value": c.value, "url": self.base} for c in self.jar]
