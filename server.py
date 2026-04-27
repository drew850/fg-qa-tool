import os, json, urllib.request, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

PORT          = int(os.environ.get("PORT", 3747))
DIR           = os.path.dirname(os.path.abspath(__file__))
NOTION_TOKEN  = os.environ.get("NOTION_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

def inject_env(html: bytes) -> bytes:
    snippet = f'<script>window.__ENV__={{NOTION_TOKEN:"{NOTION_TOKEN}",ANTHROPIC_KEY:"{ANTHROPIC_KEY}"}};</script>'
    return html.replace(b"</head>", snippet.encode() + b"</head>", 1)

def get_version():
    """Read version from HTML comment at top of file."""
    try:
        fp = os.path.join(DIR, "QAToolNotion.html")
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                if "Version:" in line:
                    return line.strip()
        return "unknown"
    except:
        return "error reading file"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        # Version check endpoint
        if path == "/version":
            v = get_version()
            msg = json.dumps({"version": v, "file": os.path.join(DIR, "QAToolNotion.html"), "exists": os.path.isfile(os.path.join(DIR, "QAToolNotion.html"))}).encode()
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers(); self.wfile.write(msg)
            return

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
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
