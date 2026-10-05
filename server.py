#!/usr/bin/env python3
"""RBG Photography website server.

A small, dependency-free web server for a Raspberry Pi. It uses only the
Python standard library (3.9+), so there is nothing to pip install.

It does four things:
  * serves the marketing pages in pages/ wrapped in templates/layout.html
  * accepts session inquiries from the Book form and stores them in SQLite
  * gives Rachel a password-protected /admin page to read and track inquiries
  * delivers private client galleries from galleries/<name>/ behind an access code

Run:  python3 server.py            (settings come from config.ini)
"""
import base64
import configparser
import csv
import email.utils
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
PAGES = ROOT / "pages"
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"
GALLERIES = ROOT / "galleries"
DATA = ROOT / "data"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SESSION_TYPES = ["Mini session", "Family session", "Extended family", "Newborn & lifestyle",
                 "Fall / holiday mini", "Not sure yet"]
STATUSES = ["new", "contacted", "booked", "archived"]
MAX_BODY = 32 * 1024

mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/webp", ".webp")


# --------------------------------------------------------------------------- config

def load_config():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({
        "server": {"host": "0.0.0.0", "port": "8080", "site_url": "http://localhost:8080",
                   "trust_proxy": "false", "secret_key": ""},
        "business": {"name": "RBG Photography", "photographer": "Rachel Goff",
                     "email": "hello@example.com", "phone": "", "city": "Woodstock",
                     "region": "MD", "service_area": "Howard, Baltimore & Carroll Counties",
                     "instagram": "", "facebook": ""},
        "admin": {"username": "rachel", "password": ""},
        "email": {"enabled": "false", "smtp_host": "", "smtp_port": "587", "smtp_user": "",
                  "smtp_password": "", "from_address": "", "notify_address": ""},
    })
    path = Path(os.environ.get("RBG_CONFIG", ROOT / "config.ini"))
    if path.exists():
        cfg.read(path)
    else:
        print(f"note: {path.name} not found, using defaults (copy config.example.ini)", file=sys.stderr)
    return cfg


CFG = load_config()
DATA.mkdir(exist_ok=True)


def secret_key():
    key = CFG["server"]["secret_key"].strip()
    if key:
        return key.encode()
    f = DATA / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip().encode()


SECRET = secret_key()


# --------------------------------------------------------------------------- storage

DB_PATH = DATA / "inquiries.db"
DB_LOCK = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


with db() as _c:
    _c.execute("""CREATE TABLE IF NOT EXISTS inquiries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT NOT NULL,
        name TEXT, email TEXT, phone TEXT, session_type TEXT, people TEXT,
        dates TEXT, location TEXT, heard TEXT, message TEXT,
        status TEXT NOT NULL DEFAULT 'new', ip TEXT)""")


# --------------------------------------------------------------------------- rate limiting

class RateLimiter:
    def __init__(self, limit, window):
        self.limit, self.window, self.hits, self.lock = limit, window, {}, threading.Lock()

    def allow(self, key):
        now = time.time()
        with self.lock:
            recent = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(recent) >= self.limit:
                self.hits[key] = recent
                return False
            recent.append(now)
            self.hits[key] = recent
            if len(self.hits) > 5000:  # keep memory bounded on a small Pi
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window}
            return True


INQUIRY_LIMIT = RateLimiter(5, 3600)      # 5 inquiries per hour per address
GALLERY_LIMIT = RateLimiter(10, 900)      # 10 code attempts per 15 minutes
ADMIN_LIMIT = RateLimiter(20, 900)        # 20 failed admin logins per 15 minutes


# --------------------------------------------------------------------------- templating

def esc(s):
    # braces are escaped too so visitor text can never be read as a {{token}}
    return html.escape(str(s or ""), quote=True).replace("{", "&#123;").replace("}", "&#125;")


def read_page(path):
    """Return (meta, body) for a page file with an optional <!-- key: value --> header."""
    text = path.read_text(encoding="utf-8")
    meta = {}
    m = re.match(r"\s*<!--(.*?)-->", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        text = text[m.end():]
    return meta, text


def business_vars():
    b = CFG["business"]
    phone = b["phone"].strip()
    tel = re.sub(r"[^\d+]", "", phone)
    year = time.strftime("%Y")
    social = []
    if b["instagram"].strip():
        social.append(f'<a href="{esc(b["instagram"])}" rel="me noopener">Instagram</a>')
    if b["facebook"].strip():
        social.append(f'<a href="{esc(b["facebook"])}" rel="me noopener">Facebook</a>')
    return {
        "site_name": esc(b["name"]), "photographer": esc(b["photographer"]),
        "email": esc(b["email"]), "phone": esc(phone),
        "phone_link": (f'<a href="tel:{esc(tel)}">{esc(phone)}</a>' if phone else ""),
        "city": esc(b["city"]), "region": esc(b["region"]),
        "service_area": esc(b["service_area"]), "year": year,
        "social_links": " · ".join(social),
        "site_url": esc(CFG["server"]["site_url"].rstrip("/")),
    }


def fill(text, values):
    # Leaves unknown {{tokens}} alone so a typo is visible instead of silently blank.
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def local_business_jsonld():
    b = CFG["business"]
    url = CFG["server"]["site_url"].rstrip("/")
    data = {
        "@context": "https://schema.org", "@type": "ProfessionalService",
        "name": b["name"], "description": "Family photography in " + b["city"] + ", " + b["region"],
        "url": url + "/", "image": url + "/static/img/photos/og-image.jpg",
        "logo": url + "/static/img/brand/rbg-favicon-512px.png",
        "email": b["email"], "founder": {"@type": "Person", "name": b["photographer"]},
        "address": {"@type": "PostalAddress", "addressLocality": b["city"],
                    "addressRegion": b["region"], "addressCountry": "US"},
        "areaServed": [a.strip() for a in re.split(r",|&| and ", b["service_area"]) if a.strip()],
    }
    if b["phone"].strip():
        data["telephone"] = b["phone"].strip()
    same = [u for u in (b["instagram"].strip(), b["facebook"].strip()) if u]
    if same:
        data["sameAs"] = same
    blob = json.dumps(data, indent=1).replace("</", "<\\/")
    return f'<script type="application/ld+json">{blob}</script>'


NAV = [("/", "Home"), ("/sessions", "Sessions"), ("/portfolio", "Portfolio"),
       ("/locations", "Locations"), ("/about", "About"), ("/gallery", "Client galleries")]


def render(body, title, description="", path="/", extra_head="", noindex=False):
    values = business_vars()
    nav = []
    for href, label in NAV:
        active = path == href or (href != "/" and path.startswith(href + "/"))
        cur = ' aria-current="page"' if active else ""
        nav.append(f'<li><a href="{href}"{cur}>{label}</a></li>')
    full_title = title if values["site_name"] in title else f"{title} | {values['site_name']}"
    values.update({
        "title": esc(full_title), "description": esc(description),
        "canonical": values["site_url"] + esc(path),
        "nav": "\n".join(nav), "extra_head": extra_head,
        "robots": '<meta name="robots" content="noindex">' if noindex else "",
        "content": fill(body, values),
    })
    layout = (TEMPLATES / "layout.html").read_text(encoding="utf-8")
    return fill(layout, values)


# --------------------------------------------------------------------------- galleries

def gallery_info(slug):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug or ""):
        return None
    folder = GALLERIES / slug
    ini = folder / "gallery.ini"
    if not ini.is_file():
        return None
    c = configparser.ConfigParser(interpolation=None)
    c.read(ini)
    g = c["gallery"] if c.has_section("gallery") else {}
    code = g.get("code", "").strip()
    if not code:
        return None
    expires = g.get("expires", "").strip()
    if expires and time.strftime("%Y-%m-%d") > expires:
        return None
    photos = sorted(p.name for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXT)
    return {"slug": slug, "folder": folder, "code": code, "photos": photos,
            "title": g.get("title", slug.replace("-", " ").title()),
            "note": g.get("note", ""), "expires": expires}


def gallery_token(g):
    return hmac.new(SECRET, f"{g['slug']}:{g['code']}".encode(), hashlib.sha256).hexdigest()


def find_gallery_by_code(code):
    code = code.strip()
    if not code or not GALLERIES.is_dir():
        return None
    match = None
    for d in sorted(GALLERIES.iterdir()):
        g = gallery_info(d.name) if d.is_dir() else None
        # compare every gallery so timing does not reveal which codes exist
        if g and hmac.compare_digest(g["code"].lower().encode(), code.lower().encode()):
            match = g
    return match


# --------------------------------------------------------------------------- email

def notify(inquiry):
    e = CFG["email"]
    if e.get("enabled", "false").lower() != "true":
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = f"New session inquiry: {inquiry['name']} ({inquiry['session_type']})"
        msg["From"] = e["from_address"] or e["smtp_user"]
        msg["To"] = e["notify_address"] or CFG["business"]["email"]
        if inquiry["email"]:
            msg["Reply-To"] = inquiry["email"]
        msg.set_content("\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in inquiry.items()))
        with smtplib.SMTP(e["smtp_host"], int(e["smtp_port"]), timeout=20) as s:
            s.starttls()
            if e["smtp_user"]:
                s.login(e["smtp_user"], e["smtp_password"])
            s.send_message(msg)
    except Exception as exc:  # the inquiry is already saved; just log the failure
        print(f"email notification failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- request handler

class Handler(BaseHTTPRequestHandler):
    server_version = "RBG"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ---- helpers
    def client_ip(self):
        if CFG["server"]["trust_proxy"].lower() == "true":
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                         "script-src 'self'; font-src 'self'; form-action 'self'; "
                         "frame-ancestors 'none'; base-uri 'self'")

    def send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, location, headers=None):
        h = {"Location": location, "Cache-Control": "no-store"}
        h.update(headers or {})
        self.send(HTTPStatus.SEE_OTHER, b"", headers=h)

    def page(self, body, title, description="", status=200, **kw):
        out = render(body, title, description, path=self.path_only, **kw)
        self.send(status, out, headers={"Cache-Control": "no-cache"})

    def not_found(self):
        meta, body = read_page(PAGES / "404.html")
        self.page(body, meta.get("title", "Not found"), status=404, noindex=True)

    def read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return None
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            try:
                data = json.loads(raw or b"{}")
                return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except ValueError:
                return {}
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}

    def cookies(self):
        out = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def same_origin(self):
        """Reject cross-site form posts (simple CSRF protection)."""
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if not origin:
            return True  # older browsers / curl
        host = self.headers.get("X-Forwarded-Host") if CFG["server"]["trust_proxy"].lower() == "true" else None
        host = host or self.headers.get("Host", "")
        return urlsplit(origin).netloc == host

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.client_ip(), fmt % args))

    # ---- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.path_only = unquote(urlsplit(self.path).path)
        p = self.path_only
        try:
            if p.startswith("/static/"):
                return self.serve_file(STATIC, p[len("/static/"):], cache=True)
            if p in ("/favicon.ico", "/robots.txt", "/sitemap.xml"):
                return {"/favicon.ico": lambda: self.serve_file(STATIC, "favicon.ico", cache=True),
                        "/robots.txt": self.robots, "/sitemap.xml": self.sitemap}[p]()
            if p == "/admin" or p.startswith("/admin/"):
                return self.admin_get(p)
            if p == "/gallery" or p.startswith("/gallery/"):
                return self.gallery_get(p)
            return self.serve_page(p)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"error on {p}: {exc!r}", file=sys.stderr)
            self.send(500, "<h1>Something went wrong</h1>")

    def do_POST(self):
        self.path_only = unquote(urlsplit(self.path).path)
        p = self.path_only
        try:
            if not self.same_origin():
                return self.send(403, "Forbidden", "text/plain")
            if p == "/api/inquiry":
                return self.inquiry_post()
            if p == "/gallery":
                return self.gallery_login()
            if p.startswith("/admin/"):
                return self.admin_post(p)
            self.send(405, "Method not allowed", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"error on POST {p}: {exc!r}", file=sys.stderr)
            self.send(500, "Something went wrong", "text/plain")

    # ---- static files
    def serve_file(self, base, rel, cache=False, download_name=None):
        target = (base / rel).resolve()
        if base.resolve() not in target.parents or not target.is_file():
            return self.not_found()
        st = target.stat()
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", email.utils.formatdate(st.st_mtime, usegmt=True))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "private, no-cache")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.security_headers()
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(target, "rb") as f:
            while chunk := f.read(64 * 1024):
                self.wfile.write(chunk)

    # ---- pages
    def serve_page(self, p):
        if p != "/" and p.endswith("/"):
            return self.redirect(p.rstrip("/"))
        name = "index" if p == "/" else p.strip("/")
        if not re.fullmatch(r"[a-z0-9-]+(/[a-z0-9-]+)?", name) or name == "404":
            return self.not_found()
        f = PAGES / f"{name}.html"
        if not f.is_file():
            f = PAGES / name / "index.html"
        if not f.is_file():
            return self.not_found()
        meta, body = read_page(f)
        extra = local_business_jsonld() if name == "index" else ""
        self.page(body, meta.get("title", "RBG Photography"), meta.get("description", ""),
                  extra_head=extra, noindex=meta.get("noindex") == "true")

    def robots(self):
        url = CFG["server"]["site_url"].rstrip("/")
        body = f"User-agent: *\nDisallow: /admin\nDisallow: /gallery/\nDisallow: /api/\n\nSitemap: {url}/sitemap.xml\n"
        self.send(200, body, "text/plain; charset=utf-8")

    def sitemap(self):
        url = CFG["server"]["site_url"].rstrip("/")
        locs = []
        for f in sorted(PAGES.rglob("*.html")):
            rel = f.relative_to(PAGES).with_suffix("").as_posix()
            meta, _ = read_page(f)
            if rel in ("404", "thanks") or meta.get("noindex") == "true":
                continue
            rel = rel[:-len("/index")] if rel.endswith("/index") else rel
            path = "/" if rel == "index" else "/" + rel
            mod = time.strftime("%Y-%m-%d", time.gmtime(f.stat().st_mtime))
            locs.append(f"<url><loc>{esc(url + path)}</loc><lastmod>{mod}</lastmod></url>")
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                + "\n".join(locs) + "\n</urlset>\n")
        self.send(200, body, "application/xml; charset=utf-8")

    # ---- inquiries
    def inquiry_post(self):
        wants_json = "application/json" in self.headers.get("Accept", "")
        form = self.read_form()

        def fail(msg, status=400):
            if wants_json:
                return self.send(status, json.dumps({"ok": False, "error": msg}), "application/json")
            body = (f'<section class="narrow"><h1>Almost there</h1><p>{esc(msg)}</p>'
                    f'<p><a class="btn" href="/book">Back to the form</a></p></section>')
            self.page(body, "Please check the form", status=status, noindex=True)

        if form is None:
            return fail("That message was too long.", 413)
        if form.get("website"):  # honeypot field real visitors never see
            return self.redirect("/thanks") if not wants_json else self.send(200, '{"ok":true}', "application/json")
        limits = {"name": 120, "email": 200, "phone": 40, "session_type": 60, "people": 40,
                  "dates": 200, "location": 200, "heard": 200, "message": 4000}
        data = {k: (form.get(k) or "").strip()[:n] for k, n in limits.items()}
        if not data["name"] or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", data["email"]):
            return fail("Please include your name and a valid email address so Rachel can reply.")
        if data["session_type"] not in SESSION_TYPES:
            data["session_type"] = "Not sure yet"
        ip = self.client_ip()
        if not INQUIRY_LIMIT.allow(ip):
            return fail("Thanks! We've received several messages from you already. Rachel will be in touch soon.", 429)
        with DB_LOCK, db() as c:
            c.execute("""INSERT INTO inquiries (created, name, email, phone, session_type, people,
                         dates, location, heard, message, ip) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (time.strftime("%Y-%m-%d %H:%M"), *data.values(), ip))
        threading.Thread(target=notify, args=(dict(data),), daemon=True).start()
        if wants_json:
            return self.send(200, json.dumps({"ok": True}), "application/json")
        self.redirect("/thanks")

    # ---- admin
    def admin_authorized(self):
        a = CFG["admin"]
        if not a["password"]:
            return None  # admin disabled until a password is set
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            ok_user = hmac.compare_digest(user.encode(), a["username"].encode())
            ok_pw = hmac.compare_digest(pw.encode(), a["password"].encode())
            if ok_user and ok_pw:
                return True
            if not ADMIN_LIMIT.allow(self.client_ip()):
                return "locked"
        return False

    def require_admin(self):
        ok = self.admin_authorized()
        if ok is True:
            return True
        if ok is None:
            self.send(503, "Admin is turned off. Set a password under [admin] in config.ini and restart.", "text/plain")
        elif ok == "locked":
            self.send(429, "Too many attempts. Try again in 15 minutes.", "text/plain")
        else:
            self.send(401, "Login required", "text/plain",
                      {"WWW-Authenticate": 'Basic realm="RBG admin", charset="UTF-8"'})
        return False

    def admin_get(self, p):
        if not self.require_admin():
            return
        q = parse_qs(urlsplit(self.path).query)
        show = q.get("status", ["open"])[0]
        with db() as c:
            if p == "/admin/inquiries.csv":
                rows = c.execute("SELECT * FROM inquiries ORDER BY id DESC").fetchall()
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(rows[0].keys() if rows else ["id"])
                for r in rows:  # neutralise spreadsheet formulas
                    w.writerow([("'" + str(v)) if str(v)[:1] in ("=", "+", "-", "@") else v for v in r])
                return self.send(200, buf.getvalue(), "text/csv; charset=utf-8",
                                 {"Content-Disposition": 'attachment; filename="inquiries.csv"',
                                  "Cache-Control": "no-store"})
            if p != "/admin":
                return self.not_found()
            if show == "all":
                rows = c.execute("SELECT * FROM inquiries ORDER BY id DESC").fetchall()
            elif show in STATUSES:
                rows = c.execute("SELECT * FROM inquiries WHERE status=? ORDER BY id DESC", (show,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM inquiries WHERE status!='archived' ORDER BY id DESC").fetchall()
            counts = dict(c.execute("SELECT status, COUNT(*) FROM inquiries GROUP BY status").fetchall())
        tabs = []
        for key, label in [("open", "Open"), *[(s, s.title()) for s in STATUSES], ("all", "All")]:
            n = sum(v for k, v in counts.items() if k != "archived") if key == "open" else \
                sum(counts.values()) if key == "all" else counts.get(key, 0)
            cur = ' aria-current="page"' if key == show else ""
            tabs.append(f'<a href="/admin?status={key}"{cur}>{label} <span>{n}</span></a>')
        cards = []
        for r in rows:
            opts = "".join(f'<option value="{s}"{" selected" if s == r["status"] else ""}>{s.title()}</option>'
                           for s in STATUSES)
            details = "".join(
                f"<dt>{label}</dt><dd>{esc(r[k])}</dd>" for k, label in
                [("phone", "Phone"), ("people", "People"), ("dates", "Dates"),
                 ("location", "Location"), ("heard", "Heard about us")] if r[k])
            cards.append(f"""
<article class="inq status-{esc(r['status'])}">
  <header><h3>{esc(r['name'])}</h3><span class="tag">{esc(r['session_type'])}</span>
    <time>{esc(r['created'])}</time></header>
  <p><a href="mailto:{esc(r['email'])}?subject={quote('Your RBG Photography session')}">{esc(r['email'])}</a></p>
  <dl>{details}</dl>
  {f'<blockquote>{esc(r["message"])}</blockquote>' if r['message'] else ''}
  <form method="post" action="/admin/status" class="inline">
    <input type="hidden" name="id" value="{r['id']}"><input type="hidden" name="back" value="{esc(show)}">
    <label>Status <select name="status">{opts}</select></label> <button class="btn small">Save</button>
  </form>
</article>""")
        body = f"""
<section class="admin">
  <div class="admin-head"><h1>Inquiries</h1><a class="btn ghost small" href="/admin/inquiries.csv">Download CSV</a></div>
  <nav class="tabs">{''.join(tabs)}</nav>
  {''.join(cards) or '<p class="muted">Nothing here yet. New inquiries from the Book page will show up here.</p>'}
</section>"""
        self.page(body, "Inquiries", noindex=True)

    def admin_post(self, p):
        if not self.require_admin():
            return
        form = self.read_form() or {}
        if p == "/admin/status" and form.get("status") in STATUSES and form.get("id", "").isdigit():
            with DB_LOCK, db() as c:
                c.execute("UPDATE inquiries SET status=? WHERE id=?", (form["status"], int(form["id"])))
        back = form.get("back", "open")
        self.redirect("/admin?status=" + (back if back in STATUSES + ["open", "all"] else "open"))

    # ---- galleries
    def gallery_login(self):
        form = self.read_form() or {}
        if not GALLERY_LIMIT.allow(self.client_ip()):
            return self.gallery_form("Too many tries. Please wait a few minutes and try again.", 429)
        g = find_gallery_by_code(form.get("code", ""))
        if not g:
            return self.gallery_form("That code didn't match a gallery. Check the email from Rachel and try again.", 403)
        cookie = (f"gal_{g['slug']}={gallery_token(g)}; Path=/gallery/{g['slug']}; HttpOnly; "
                  f"SameSite=Lax; Max-Age={60 * 60 * 24 * 30}")
        if CFG["server"]["site_url"].startswith("https://"):
            cookie += "; Secure"
        self.redirect(f"/gallery/{g['slug']}", {"Set-Cookie": cookie})

    def gallery_form(self, error="", status=200):
        meta, body = read_page(PAGES / "gallery.html")
        msg = f'<p class="form-error" role="alert">{esc(error)}</p>' if error else ""
        self.page(body.replace("<!--error-->", msg), meta.get("title", "Client galleries"),
                  meta.get("description", ""), status=status)

    def gallery_get(self, p):
        if p == "/gallery":
            return self.gallery_form()
        parts = p.split("/")[2:]  # /gallery/<slug>/...
        g = gallery_info(parts[0] if parts else "")
        if not g:
            return self.not_found()
        token = self.cookies().get(f"gal_{g['slug']}", "")
        if not hmac.compare_digest(token, gallery_token(g)):
            return self.redirect("/gallery")
        rest = parts[1:]
        if not rest or rest == [""]:
            return self.gallery_page(g)
        if rest[0] == "download.zip":
            return self.gallery_zip(g)
        if len(rest) == 2 and rest[0] in ("photo", "thumb", "download") and rest[1] in g["photos"]:
            name = rest[1]
            if rest[0] == "thumb" and (g["folder"] / "thumbs" / name).is_file():
                return self.serve_file(g["folder"] / "thumbs", name)
            return self.serve_file(g["folder"], name,
                                   download_name=name if rest[0] == "download" else None)
        self.not_found()

    def gallery_page(self, g):
        base = f"/gallery/{g['slug']}"
        items = "".join(
            f'<figure><a href="{base}/photo/{quote(n)}" class="lb" data-download="{base}/download/{quote(n)}">'
            f'<img src="{base}/thumb/{quote(n)}" alt="{esc(g["title"])} photo {i}" loading="lazy"></a>'
            f'<figcaption><a href="{base}/download/{quote(n)}">Download</a></figcaption></figure>'
            for i, n in enumerate(g["photos"], 1))
        exp = f'<p class="muted">This gallery is available until {esc(g["expires"])}.</p>' if g["expires"] else ""
        body = f"""
<section class="gallery-head">
  <p class="eyebrow">Your gallery</p>
  <h1>{esc(g['title'])}</h1>
  {f'<p class="lede">{esc(g["note"])}</p>' if g['note'] else ''}
  <p><a class="btn" href="{base}/download.zip">Download all ({len(g['photos'])} photos)</a></p>
  {exp}
</section>
<section class="client-grid">{items or '<p class="muted">Photos are on their way.</p>'}</section>"""
        self.page(body, g["title"], noindex=True)

    def gallery_zip(self, g):
        # Built in a temp file (not memory) so large galleries are fine on a Pi.
        # Photos are already compressed, so ZIP_STORED keeps the CPU free.
        fname = re.sub(r"[^A-Za-z0-9-]+", "-", g["title"]).strip("-") or g["slug"]
        with tempfile.TemporaryFile(dir=DATA) as tmp:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                for n in g["photos"]:
                    z.write(g["folder"] / n, n)
            size = tmp.tell()
            tmp.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{fname}.zip"')
            self.send_header("Cache-Control", "no-store")
            self.security_headers()
            self.end_headers()
            if self.command != "HEAD":
                while chunk := tmp.read(256 * 1024):
                    self.wfile.write(chunk)


def main():
    host = CFG["server"]["host"]
    port = int(os.environ.get("PORT", CFG["server"]["port"]))
    if not CFG["admin"]["password"]:
        print("note: /admin is off until you set [admin] password in config.ini", file=sys.stderr)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"RBG Photography site running on http://{host}:{port}  (Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
