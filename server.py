import os, json, urllib.request, urllib.error, secrets, hashlib, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, urlencode

PORT                = int(os.environ.get("PORT", 3747))
DIR                 = os.path.dirname(os.path.abspath(__file__))
NOTION_TOKEN        = os.environ.get("NOTION_TOKEN", "")
ANTHROPIC_KEY       = os.environ.get("ANTHROPIC_KEY", "")
GOOGLE_CLIENT_ID    = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET= os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL            = os.environ.get("BASE_URL", "https://vigilant-youthfulness-production-896b.up.railway.app")
REDIRECT_URI        = BASE_URL + "/auth/callback"

# In-memory session store: token -> {email, name, exp}
SESSIONS = {}
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days

# In-memory OAuth state store: state -> timestamp (prevents CSRF)
OAUTH_STATES = {}
OAUTH_STATE_TTL = 300  # 5 minutes

def inject_env(html: bytes) -> bytes:
    snippet = (
        f'<script>window.__ENV__={{'
        f'NOTION_TOKEN:"{NOTION_TOKEN}",'
        f'ANTHROPIC_KEY:"{ANTHROPIC_KEY}",'
        f'GOOGLE_CLIENT_ID:"{GOOGLE_CLIENT_ID}",'
        f'BASE_URL:"{BASE_URL}"'
        f'}};</script>'
    )
    return html.replace(b"</head>", snippet.encode() + b"</head>", 1)

def get_version():
    try:
        fp = os.path.join(DIR, "QAToolNotion.html")
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                if "Version:" in line:
                    return line.strip()
        return "unknown"
    except:
        return "error reading file"

def google_get_token(code):
    """Exchange authorization code for access token."""
    data = urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())

def google_get_userinfo(access_token):
    """Get user info from Google."""
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())

def create_session(email, name):
    """Create a session token for verified user."""
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "email": email.lower().strip(),
        "name": name,
        "exp": time.time() + SESSION_TTL
    }
    # Clean expired sessions
    expired = [k for k, v in SESSIONS.items() if v["exp"] < time.time()]
    for k in expired:
        del SESSIONS[k]
    return token

def verify_session(token):
    """Verify session token and return user info or None."""
    session = SESSIONS.get(token)
    if not session:
        return None
    if session["exp"] < time.time():
        del SESSIONS[token]
        return None
    return session

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def _json(self, data, status=200):
        msg = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # ── Version endpoint ──────────────────────────────────────────────
        if path == "/version":
            v = get_version()
            self._json({"version": v, "file": os.path.join(DIR, "QAToolNotion.html"), "exists": os.path.isfile(os.path.join(DIR, "QAToolNotion.html"))})
            return

        # ── Google OAuth: initiate login ──────────────────────────────────
        if path == "/auth/google":
            if not GOOGLE_CLIENT_ID:
                self._json({"error": "Google SSO not configured"}, 500)
                return
            state = secrets.token_urlsafe(16)
            OAUTH_STATES[state] = time.time()
            params = urlencode({
                "client_id": GOOGLE_CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "access_type": "online",
                "hd": "myfreebird.com"  # restrict to myfreebird.com domain
            })
            self._redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
            return

        # ── Google OAuth: callback ─────────────────────────────────────────
        if path == "/auth/callback":
            code = qs.get("code", [""])[0]
            state = qs.get("state", [""])[0]
            error = qs.get("error", [""])[0]

            if error:
                self._redirect(f"{BASE_URL}/?auth_error={error}")
                return

            # Verify state (CSRF protection)
            state_time = OAUTH_STATES.pop(state, None)
            if not state_time or (time.time() - state_time) > OAUTH_STATE_TTL:
                self._redirect(f"{BASE_URL}/?auth_error=invalid_state")
                return

            try:
                token_data = google_get_token(code)
                access_token = token_data.get("access_token")
                if not access_token:
                    raise Exception("No access token")
                userinfo = google_get_userinfo(access_token)
                email = userinfo.get("email", "").lower().strip()
                name = userinfo.get("name", email.split("@")[0])

                if not email:
                    raise Exception("No email returned")

                session_token = create_session(email, name)
                # Redirect back to app with session token
                self._redirect(f"{BASE_URL}/?session={session_token}")
            except Exception as e:
                print(f"[SSO] Auth error: {e}")
                self._redirect(f"{BASE_URL}/?auth_error=auth_failed")
            return

        # ── Session verify endpoint ────────────────────────────────────────
        if path == "/auth/verify":
            auth_header = self.headers.get("Authorization", "")
            token = auth_header.replace("Bearer ", "").strip()
            if not token:
                token = qs.get("token", [""])[0]
            session = verify_session(token)
            if session:
                self._json({"ok": True, "email": session["email"], "name": session["name"]})
            else:
                self._json({"ok": False, "error": "Invalid or expired session"}, 401)
            return

        # ── Static file serving ────────────────────────────────────────────
        if path in ("/", "/index.html"):
            path = "/QAToolNotion.html"
        filepath = os.path.join(DIR, path.lstrip("/"))
        if os.path.isfile(filepath):
            with open(filepath, "rb") as f:
                data = f.read()
            if filepath.endswith(".html"):
                data = inject_env(data)
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "text/html" if filepath.endswith(".html") else "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ── Session logout ─────────────────────────────────────────────────
        if path == "/auth/logout":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
                token = body.get("token", "")
                if token in SESSIONS:
                    del SESSIONS[token]
            except:
                pass
            self._json({"ok": True})
            return

        # ── Proxy ──────────────────────────────────────────────────────────
        if not self.path.startswith("/proxy"):
            self.send_response(404); self.end_headers(); return

        qs     = parse_qs(urlparse(self.path).query)
        target = unquote(qs.get("url", [""])[0])
        if not target.startswith("https://"):
            self.send_response(400); self._cors(); self.end_headers()
            self.wfile.write(b'{"error":"bad url"}'); return

        length  = int(self.headers.get("Content-Length", 0))
        raw     = self.rfile.read(length) if length else b"{}"
        try:    wrapper = json.loads(raw)
        except: wrapper = {}

        method     = wrapper.get("_method", "POST")
        hdrs       = wrapper.get("_headers", {})
        body_obj   = wrapper.get("_body")
        body_bytes = json.dumps(body_obj).encode() if body_obj is not None else None

        fwd = {"Content-Type": "application/json"}
        for k, v in hdrs.items():
            if v: fwd[k] = str(v)

        try:
            req  = urllib.request.Request(target, data=body_bytes, headers=fwd, method=method)
            resp = urllib.request.urlopen(req, timeout=120)
            data = resp.read()
            self.send_response(resp.status); self._cors()
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code); self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        except Exception as e:
            msg = json.dumps({"error": str(e)}).encode()
            self.send_response(502); self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers(); self.wfile.write(msg)

if __name__ == "__main__":
    print(f"Starting on port {PORT}")
    print(f"Serving from: {DIR}")
    print(f"HTML version: {get_version()}")
    print(f"HTML exists: {os.path.isfile(os.path.join(DIR, 'QAToolNotion.html'))}")
    print(f"Google SSO: {'configured' if GOOGLE_CLIENT_ID else 'NOT configured'}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
