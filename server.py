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
import shutil
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
CLIENT_STATUSES = ["lead", "active", "past"]
CURRENT = ' aria-current="page"'
MAX_BODY = 32 * 1024
MAX_UPLOAD = 60 * 1024 * 1024  # per photo

ADMIN_SECTIONS = [("dashboard", "/admin", "Dashboard"), ("inquiries", "/admin/inquiries", "Inquiries"),
                  ("clients", "/admin/clients", "Clients"), ("galleries", "/admin/galleries", "Galleries"),
                  ("photos", "/admin/photos", "Site photos")]
# Simple line icons for the admin sidebar (24x24, stroke = currentColor)
ADMIN_ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    "inquiries": '<path d="M4 4h16v12H5.5L4 17.5z"/><path d="M8 9h8M8 12h5"/>',
    "clients": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><circle cx="17" cy="9" r="2.5"/><path d="M17 14.5c2.3 0 4 1.5 4.5 4"/>',
    "galleries": '<rect x="3" y="5" width="18" height="14" rx="1.5"/><circle cx="9" cy="10" r="1.8"/><path d="M3 17l5-4.5 4 3.5 3-2.5 6 4.5"/>',
    "photos": '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
}
NOTICES = {"client-saved": "Client saved.", "client-deleted": "Client deleted.",
           "gallery-saved": "Gallery saved.", "gallery-deleted": "Gallery moved to the trash folder.",
           "photo-removed": "Photo removed.", "code-taken": "Another gallery already uses that code. Pick a different one."}
PHOTO_HINTS = {
    "hero.jpg": "Home page banner · wide, about 2000×1250",
    "og-image.jpg": "Preview when the site is shared · 1200×630",
    "about-rachel.jpg": "Rachel's portrait · tall, about 900×1125",
    "session-mini.jpg": "Mini session card · 4:3", "session-family.jpg": "Family session card · 4:3",
    "session-extended.jpg": "Extended family card · 4:3", "session-newborn.jpg": "Newborn card · 4:3",
    "location-patapsco.jpg": "Patapsco Valley page · wide", "location-ellicott-city.jpg": "Ellicott City page · wide",
    "location-sykesville.jpg": "Sykesville page · wide",
}

try:  # optional: faster gallery previews when Pillow (apt install python3-pil) is present
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

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
    _c.execute("""CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT NOT NULL,
        name TEXT NOT NULL, email TEXT, phone TEXT, family TEXT, notes TEXT,
        status TEXT NOT NULL DEFAULT 'active')""")
    if "client_id" not in [r[1] for r in _c.execute("PRAGMA table_info(inquiries)")]:
        _c.execute("ALTER TABLE inquiries ADD COLUMN client_id INTEGER")


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
    })
    if "{{portfolio_" in body:
        values.update(portfolio_values())
    values["content"] = fill(body, values)
    layout = (TEMPLATES / "layout.html").read_text(encoding="utf-8")
    return version_photo_urls(fill(layout, values))


PHOTOS = STATIC / "img" / "photos"


def version_photo_urls(page):
    """Add ?v=<modified time> to site photo links so a replaced photo shows up
    right away instead of the browser's cached copy."""
    def stamp(m):
        f = PHOTOS / m.group(2)
        return m.group(0) if not f.is_file() else f"{m.group(1)}{m.group(2)}?v={int(f.stat().st_mtime)}"
    return re.sub(r"(/static/img/photos/)([\w.-]+\.(?:jpe?g|png|webp))(?![?\w.])", stamp, page)


def portfolio_photos():
    return sorted(p.name for p in PHOTOS.glob("portfolio-*") if p.suffix.lower() in IMAGE_EXT)


def portfolio_values():
    names = portfolio_photos()
    grid = "\n".join(
        f'<a class="lb" href="/static/img/photos/{n}"><img src="/static/img/photos/{n}" '
        f'alt="Family photo by {esc(CFG["business"]["name"])}" loading="lazy"></a>' for n in names)
    strip = "\n".join(
        f'<img src="/static/img/photos/{n}" alt="Recent family session" loading="lazy">' for n in names[:4])
    return {"portfolio_grid": grid, "portfolio_strip": strip}


# --------------------------------------------------------------------------- galleries

def read_gallery(slug):
    """Every gallery folder with a gallery.ini, active or not (used by admin)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug or ""):
        return None
    folder = GALLERIES / slug
    ini = folder / "gallery.ini"
    if not ini.is_file():
        return None
    c = configparser.ConfigParser(interpolation=None)
    c.read(ini, encoding="utf-8")
    g = c["gallery"] if c.has_section("gallery") else {}
    code = g.get("code", "").strip()
    expires = g.get("expires", "").strip()
    expired = bool(expires and time.strftime("%Y-%m-%d") > expires)
    photos = sorted(p.name for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXT)
    client = g.get("client_id", "").strip()
    return {"slug": slug, "folder": folder, "code": code, "photos": photos,
            "title": g.get("title", slug.replace("-", " ").title()),
            "note": g.get("note", ""), "expires": expires, "expired": expired,
            "client_id": int(client) if client.isdigit() else None,
            "created": g.get("created", ""), "active": bool(code) and not expired}


def gallery_info(slug):
    """A gallery clients can open right now (has a code and hasn't expired)."""
    g = read_gallery(slug)
    return g if g and g["active"] else None


def all_galleries():
    if not GALLERIES.is_dir():
        return []
    out = [read_gallery(d.name) for d in GALLERIES.iterdir() if d.is_dir()]
    return sorted((g for g in out if g), key=lambda g: (g["created"], g["slug"]), reverse=True)


def write_gallery_ini(folder, data):
    c = configparser.ConfigParser(interpolation=None)
    c["gallery"] = {k: str(v).replace("\n", " ").strip() for k, v in data.items() if v not in (None, "")}
    tmp = folder / "gallery.ini.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("; Managed from /admin/galleries. You can also edit this file by hand.\n")
        c.write(f)
    tmp.replace(folder / "gallery.ini")


WORDS = ("maple oak birch willow cedar meadow river creek valley harbor autumn summer "
         "clover juniper aspen sparrow robin finch acorn pine fern sunny golden amber").split()


def new_gallery_code():
    return f"{secrets.choice(WORDS)}-{secrets.choice(WORDS)}-{secrets.randbelow(90) + 10}"


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "gallery"


def snippet(text, n=160):
    return f'<br><span class="muted">{esc(text[:n])}</span>' if text else ""


def sniff_image(head):
    """Return the right extension for JPEG/PNG/WebP bytes, else None."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def make_thumb(path):
    if not HAVE_PIL:
        return
    try:
        out = path.parent / "thumbs"
        out.mkdir(exist_ok=True)
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((1000, 1000))
            if path.suffix.lower() == ".jpg":
                im.convert("RGB").save(out / path.name, quality=82, optimize=True, progressive=True)
            else:
                im.save(out / path.name)
    except Exception as exc:
        print(f"thumbnail failed for {path.name}: {exc}", file=sys.stderr)


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

    # ---- admin: auth
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

    def admin_page(self, body, title, section, notice="", crumbs=None):
        with db() as c:
            new_count = c.execute("SELECT COUNT(*) FROM inquiries WHERE status='new'").fetchone()[0]
        items = []
        for key, href, label in ADMIN_SECTIONS:
            badge = f'<span class="badge">{new_count}</span>' if key == "inquiries" and new_count else ""
            icon = (f'<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" '
                    f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                    f'{ADMIN_ICONS[key]}</svg>')
            items.append(f'<li><a href="{href}"{CURRENT if key == section else ""}>{icon}<span>{label}</span>{badge}</a></li>')
        trail = ""
        if crumbs:
            trail = '<nav class="crumbs" aria-label="Breadcrumb">' + " <span>›</span> ".join(
                f'<a href="{h}">{esc(t)}</a>' if h else f'<span aria-current="page">{esc(t)}</span>'
                for t, h in crumbs) + "</nav>"
        values = business_vars()
        values.update({
            "title": esc(title), "nav": "\n".join(items), "crumbs": trail,
            "notice": f'<p class="notice" role="status">{esc(notice)}</p>' if notice else "",
            "admin_user": esc(CFG["admin"]["username"]), "content": body,
        })
        layout = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
        self.send(200, version_photo_urls(fill(layout, values)), headers={"Cache-Control": "no-store"})

    def query(self):
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    # ---- admin: routing
    def admin_get(self, p):
        if not self.require_admin():
            return
        q = self.query()
        notice = NOTICES.get(q.get("done", ""), "")
        if p == "/admin":
            return self.admin_dashboard(notice)
        if p == "/admin/inquiries":
            return self.admin_inquiries(q.get("status", "open"), notice)
        if p == "/admin/inquiries.csv":
            return self.admin_csv()
        if p == "/admin/clients":
            return self.admin_clients(q.get("q", ""), q.get("status", "all"), notice)
        if p == "/admin/clients/new":
            return self.admin_client_form(None, notice)
        m = re.fullmatch(r"/admin/clients/(\d+)", p)
        if m:
            return self.admin_client_form(int(m.group(1)), notice)
        if p == "/admin/galleries":
            return self.admin_galleries(notice)
        if p == "/admin/galleries/new":
            return self.admin_gallery_form(None, q.get("client", ""), notice)
        m = re.fullmatch(r"/admin/galleries/([a-z0-9-]+)", p)
        if m:
            return self.admin_gallery_form(m.group(1), "", notice)
        m = re.fullmatch(r"/admin/galleries/([a-z0-9-]+)/(thumb|photo)/([^/]+)", p)
        if m:
            g = read_gallery(m.group(1))
            if not g or m.group(3) not in g["photos"]:
                return self.not_found()
            if m.group(2) == "thumb" and (g["folder"] / "thumbs" / m.group(3)).is_file():
                return self.serve_file(g["folder"] / "thumbs", m.group(3))
            return self.serve_file(g["folder"], m.group(3))
        if p == "/admin/photos":
            return self.admin_photos(notice)
        self.not_found()

    def admin_post(self, p):
        if not self.require_admin():
            return
        if p == "/admin/upload":
            return self.admin_upload()
        form = self.read_form() or {}
        if p == "/admin/status":
            if form.get("status") in STATUSES and form.get("id", "").isdigit():
                with DB_LOCK, db() as c:
                    c.execute("UPDATE inquiries SET status=? WHERE id=?", (form["status"], int(form["id"])))
            back = form.get("back", "open")
            return self.redirect("/admin/inquiries?status=" + (back if back in STATUSES + ["open", "all"] else "open"))
        m = re.fullmatch(r"/admin/inquiries/(\d+)/client", p)
        if m:
            return self.admin_inquiry_to_client(int(m.group(1)))
        if p == "/admin/clients/save":
            return self.admin_client_save(form)
        m = re.fullmatch(r"/admin/clients/(\d+)/delete", p)
        if m:
            with DB_LOCK, db() as c:
                c.execute("UPDATE inquiries SET client_id=NULL WHERE client_id=?", (int(m.group(1)),))
                c.execute("DELETE FROM clients WHERE id=?", (int(m.group(1)),))
            return self.redirect("/admin/clients?done=client-deleted")
        if p == "/admin/galleries/save":
            return self.admin_gallery_save(form)
        m = re.fullmatch(r"/admin/galleries/([a-z0-9-]+)/(delete|photo-delete)", p)
        if m:
            return self.admin_gallery_delete(m.group(1), form.get("name") if m.group(2) == "photo-delete" else None)
        if p == "/admin/photos/delete":
            return self.admin_photo_delete(form.get("name", ""))
        if p == "/admin/photos/move":
            return self.admin_photo_move(form.get("name", ""), form.get("dir", ""))
        self.not_found()

    # ---- admin: dashboard
    def admin_dashboard(self, notice):
        with db() as c:
            new = c.execute("SELECT * FROM inquiries WHERE status='new' ORDER BY id DESC LIMIT 5").fetchall()
            new_count = c.execute("SELECT COUNT(*) FROM inquiries WHERE status='new'").fetchone()[0]
            clients = c.execute("SELECT COUNT(*) FROM clients WHERE status!='past'").fetchone()[0]
        gals = all_galleries()
        live = sum(1 for g in gals if g["active"])
        names = self.client_names()

        def stat(n, label, href, hint):
            return (f'<a class="stat" href="{href}"><strong>{n}</strong><span>{label}</span>'
                    f'<small>{hint}</small></a>')
        stats = "".join([
            stat(new_count, "New inquiries", "/admin/inquiries?status=new", "Waiting for a reply"),
            stat(clients, "Clients", "/admin/clients", "Leads and active families"),
            stat(live, "Live galleries", "/admin/galleries", "Clients can open these now"),
            stat(len(portfolio_photos()), "Portfolio photos", "/admin/photos", "Shown on the Portfolio page"),
        ])
        inbox = "".join(
            f'<li><a href="/admin/inquiries?status=new"><strong>{esc(r["name"])}</strong></a> '
            f'<span class="muted">· {esc(r["session_type"])} · {esc(r["created"])}</span>'
            f'{snippet(r["message"], 120)}</li>' for r in new) or '<li class="muted">You\'re all caught up.</li>'
        recent = "".join(
            f'<li><a href="/admin/galleries/{g["slug"]}"><strong>{esc(g["title"])}</strong></a> '
            f'<span class="muted">· {esc(names.get(g["client_id"], "No client"))} · {len(g["photos"])} photos</span></li>'
            for g in gals[:5]) or '<li class="muted">No galleries yet.</li>'
        body = f"""
  <div class="admin-head"><h1>Hello, {esc(CFG["business"]["photographer"].split()[0])}</h1></div>
  <div class="stats">{stats}</div>
  <div class="quick">
    <a class="btn small" href="/admin/galleries/new">New gallery</a>
    <a class="btn ghost small" href="/admin/clients/new">Add client</a>
    <a class="btn ghost small" href="/admin/photos">Update site photos</a>
  </div>
  <div class="admin-cols">
    <section class="card-pad"><div class="admin-head"><h2>New inquiries</h2><a href="/admin/inquiries">See all</a></div><ul class="plain">{inbox}</ul></section>
    <section class="card-pad"><div class="admin-head"><h2>Recent galleries</h2><a href="/admin/galleries">See all</a></div><ul class="plain">{recent}</ul></section>
  </div>"""
        self.admin_page(body, "Dashboard", "dashboard", notice)

    # ---- admin: inquiries
    def admin_inquiries(self, show, notice):
        with db() as c:
            if show == "all":
                rows = c.execute("SELECT * FROM inquiries ORDER BY id DESC").fetchall()
            elif show in STATUSES:
                rows = c.execute("SELECT * FROM inquiries WHERE status=? ORDER BY id DESC", (show,)).fetchall()
            else:
                show = "open"
                rows = c.execute("SELECT * FROM inquiries WHERE status!='archived' ORDER BY id DESC").fetchall()
            counts = dict(c.execute("SELECT status, COUNT(*) FROM inquiries GROUP BY status").fetchall())
        tabs = []
        for key, label in [("open", "Open"), *[(s, s.title()) for s in STATUSES], ("all", "All")]:
            n = sum(v for k, v in counts.items() if k != "archived") if key == "open" else \
                sum(counts.values()) if key == "all" else counts.get(key, 0)
            cur = ' aria-current="page"' if key == show else ""
            tabs.append(f'<a href="/admin/inquiries?status={key}"{cur}>{label} <span>{n}</span></a>')
        cards = []
        for r in rows:
            opts = "".join(f'<option value="{s}"{" selected" if s == r["status"] else ""}>{s.title()}</option>'
                           for s in STATUSES)
            details = "".join(
                f"<dt>{label}</dt><dd>{esc(r[k])}</dd>" for k, label in
                [("phone", "Phone"), ("people", "People"), ("dates", "Dates"),
                 ("location", "Location"), ("heard", "Heard about us")] if r[k])
            if r["client_id"]:
                client_btn = f'<a class="btn ghost small" href="/admin/clients/{r["client_id"]}">View client</a>'
            else:
                client_btn = (f'<form method="post" action="/admin/inquiries/{r["id"]}/client">'
                              f'<button class="btn ghost small">Add to clients</button></form>')
            cards.append(f"""
<article class="inq status-{esc(r['status'])}">
  <header><h3>{esc(r['name'])}</h3><span class="tag">{esc(r['session_type'])}</span>
    <time>{esc(r['created'])}</time></header>
  <p><a href="mailto:{esc(r['email'])}?subject={quote('Your RBG Photography session')}">{esc(r['email'])}</a></p>
  <dl>{details}</dl>
  {f'<blockquote>{esc(r["message"])}</blockquote>' if r['message'] else ''}
  <div class="row-actions">
    <form method="post" action="/admin/status" class="inline">
      <input type="hidden" name="id" value="{r['id']}"><input type="hidden" name="back" value="{esc(show)}">
      <label>Status <select name="status">{opts}</select></label> <button class="btn small">Save</button>
    </form>
    {client_btn}
  </div>
</article>""")
        body = f"""
  <div class="admin-head"><h1>Inquiries</h1><a class="btn ghost small" href="/admin/inquiries.csv">Download CSV</a></div>
  <nav class="tabs">{''.join(tabs)}</nav>
  {''.join(cards) or '<p class="muted">Nothing here yet. New inquiries from the Book page will show up here.</p>'}"""
        self.admin_page(body, "Inquiries", "inquiries", notice)

    def admin_csv(self):
        with db() as c:
            rows = c.execute("SELECT * FROM inquiries ORDER BY id DESC").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(rows[0].keys() if rows else ["id"])
        for r in rows:  # neutralise spreadsheet formulas
            w.writerow([("'" + str(v)) if str(v)[:1] in ("=", "+", "-", "@") else v for v in r])
        self.send(200, buf.getvalue(), "text/csv; charset=utf-8",
                  {"Content-Disposition": 'attachment; filename="inquiries.csv"', "Cache-Control": "no-store"})

    def admin_inquiry_to_client(self, inquiry_id):
        with DB_LOCK, db() as c:
            r = c.execute("SELECT * FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()
            if not r:
                return self.not_found()
            if r["client_id"]:
                return self.redirect(f"/admin/clients/{r['client_id']}")
            existing = c.execute("SELECT id FROM clients WHERE lower(email)=lower(?) AND email!=''",
                                 (r["email"],)).fetchone()
            if existing:
                cid = existing["id"]
            else:
                note = "\n".join(x for x in [
                    f"From inquiry {r['created']}: {r['session_type']}",
                    f"Dates: {r['dates']}" if r["dates"] else "",
                    f"Location: {r['location']}" if r["location"] else "",
                    f"Heard about us: {r['heard']}" if r["heard"] else ""] if x)
                cid = c.execute("""INSERT INTO clients (created, name, email, phone, family, notes, status)
                                   VALUES (?,?,?,?,?,?, 'lead')""",
                                (time.strftime("%Y-%m-%d"), r["name"], r["email"], r["phone"],
                                 r["people"], note)).lastrowid
            c.execute("UPDATE inquiries SET client_id=? WHERE id=?", (cid, inquiry_id))
            c.execute("UPDATE inquiries SET client_id=? WHERE client_id IS NULL AND lower(email)=lower(?)",
                      (cid, r["email"]))
        self.redirect(f"/admin/clients/{cid}?done=client-saved")

    # ---- admin: clients
    def admin_clients(self, search, status, notice):
        sql, args = "SELECT * FROM clients", []
        where = []
        if search.strip():
            like = f"%{search.strip()}%"
            where.append("(name LIKE ? OR email LIKE ? OR phone LIKE ? OR family LIKE ? OR notes LIKE ?)")
            args += [like] * 5
        if status in CLIENT_STATUSES:
            where.append("status=?")
            args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with db() as c:
            rows = c.execute(sql + " ORDER BY name COLLATE NOCASE", args).fetchall()
            counts = dict(c.execute("SELECT status, COUNT(*) FROM clients GROUP BY status").fetchall())
        gal_counts = {}
        for g in all_galleries():
            if g["client_id"]:
                gal_counts[g["client_id"]] = gal_counts.get(g["client_id"], 0) + 1
        tabs = "".join(
            f'<a href="/admin/clients?status={k}&amp;q={quote(search)}"{CURRENT if k == status else ""}>'
            f'{label} <span>{sum(counts.values()) if k == "all" else counts.get(k, 0)}</span></a>'
            for k, label in [("all", "All"), *[(s, s.title()) for s in CLIENT_STATUSES]])
        trs = "".join(f"""<tr>
  <td><a href="/admin/clients/{r['id']}"><strong>{esc(r['name'])}</strong></a></td>
  <td>{f'<a href="mailto:{esc(r["email"])}">{esc(r["email"])}</a>' if r['email'] else ''}<br><span class="muted">{esc(r['phone'])}</span></td>
  <td>{esc(r['family'])}</td>
  <td><span class="tag">{esc(r['status'])}</span></td>
  <td>{gal_counts.get(r['id'], 0) or ''}</td>
</tr>""" for r in rows)
        table = (f'<div class="table-wrap"><table class="data"><thead><tr><th>Name</th><th>Contact</th><th>Family</th>'
                 f'<th>Status</th><th>Galleries</th></tr></thead><tbody>{trs}</tbody></table></div>'
                 if rows else '<p class="muted">No clients yet. Add one, or use “Add to clients” on an inquiry.</p>')
        body = f"""
  <div class="admin-head"><h1>Clients</h1><a class="btn small" href="/admin/clients/new">Add client</a></div>
  <form class="search" method="get" action="/admin/clients">
    <input type="hidden" name="status" value="{esc(status)}">
    <input name="q" value="{esc(search)}" placeholder="Search names, emails, notes" aria-label="Search clients">
    <button class="btn ghost small">Search</button>
  </form>
  <nav class="tabs">{tabs}</nav>
  {table}"""
        self.admin_page(body, "Clients", "clients", notice)

    def admin_client_form(self, cid, notice):
        r, inquiries, galleries = None, [], []
        if cid is not None:
            with db() as c:
                r = c.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
                if not r:
                    return self.not_found()
                inquiries = c.execute("""SELECT * FROM inquiries WHERE client_id=? OR
                                         (email!='' AND lower(email)=lower(?)) ORDER BY id DESC""",
                                      (cid, r["email"] or "")).fetchall()
            galleries = [g for g in all_galleries() if g["client_id"] == cid]
        v = (lambda k: esc(r[k]) if r else "")
        opts = "".join(f'<option value="{s}"{" selected" if r and r["status"] == s else ""}>{s.title()}</option>'
                       for s in CLIENT_STATUSES)
        form = f"""
  <form method="post" action="/admin/clients/save" class="form card-pad">
    <input type="hidden" name="id" value="{cid or ''}">
    <label>Name<input name="name" value="{v('name')}" required maxlength="120"></label>
    <label>Status<select name="status">{opts}</select></label>
    <label>Email<input name="email" type="email" value="{v('email')}" maxlength="200"></label>
    <label>Phone<input name="phone" value="{v('phone')}" maxlength="40"></label>
    <label class="full">Family <span class="opt">(names, kids' ages, pets)</span><input name="family" value="{v('family')}" maxlength="300"></label>
    <label class="full">Notes<textarea name="notes" maxlength="8000">{v('notes')}</textarea></label>
    <div class="full row-actions"><button class="btn">Save client</button>
      <a class="btn ghost" href="/admin/clients">Back to clients</a></div>
  </form>"""
        extra = ""
        if r:
            gal_rows = "".join(
                f'<li><a href="/admin/galleries/{g["slug"]}">{esc(g["title"])}</a> '
                f'<span class="muted">· {len(g["photos"])} photos{" · expired" if g["expired"] else ""}</span></li>'
                for g in galleries) or '<li class="muted">No galleries yet.</li>'
            inq_rows = "".join(
                f'<li>{esc(i["created"])} · {esc(i["session_type"])} <span class="tag">{esc(i["status"])}</span>'
                f'{snippet(i["message"])}</li>'
                for i in inquiries) or '<li class="muted">No inquiries.</li>'
            extra = f"""
  <div class="admin-cols">
    <section><div class="admin-head"><h2>Galleries</h2>
      <a class="btn small" href="/admin/galleries/new?client={cid}">New gallery</a></div><ul class="plain">{gal_rows}</ul></section>
    <section><h2>Inquiries</h2><ul class="plain">{inq_rows}</ul></section>
  </div>
  <form method="post" action="/admin/clients/{cid}/delete" data-confirm="Delete {esc(r['name'])} from your client list? Their galleries are kept.">
    <button class="btn ghost small danger">Delete client</button></form>"""
        title = r["name"] if r else "New client"
        body = f'<div class="admin-head"><h1>{esc(title)}</h1></div>{form}{extra}'
        self.admin_page(body, title, "clients", notice, crumbs=[("Clients", "/admin/clients"), (title, None)])

    def admin_client_save(self, form):
        f = {k: (form.get(k) or "").strip() for k in ("id", "name", "email", "phone", "family", "notes", "status")}
        if not f["name"]:
            return self.redirect("/admin/clients/new")
        if f["status"] not in CLIENT_STATUSES:
            f["status"] = "active"
        vals = (f["name"][:120], f["email"][:200], f["phone"][:40], f["family"][:300], f["notes"][:8000], f["status"])
        with DB_LOCK, db() as c:
            if f["id"].isdigit():
                cid = int(f["id"])
                c.execute("UPDATE clients SET name=?, email=?, phone=?, family=?, notes=?, status=? WHERE id=?",
                          (*vals, cid))
            else:
                cid = c.execute("""INSERT INTO clients (name, email, phone, family, notes, status, created)
                                   VALUES (?,?,?,?,?,?,?)""", (*vals, time.strftime("%Y-%m-%d"))).lastrowid
        self.redirect(f"/admin/clients/{cid}?done=client-saved")

    def client_names(self):
        with db() as c:
            return {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM clients ORDER BY name COLLATE NOCASE")}

    # ---- admin: galleries
    def admin_galleries(self, notice):
        names = self.client_names()
        rows = "".join(f"""<tr>
  <td><a href="/admin/galleries/{g['slug']}"><strong>{esc(g['title'])}</strong></a><br><span class="muted">{esc(g['created'])}</span></td>
  <td>{esc(names.get(g['client_id'], ''))}</td>
  <td>{len(g['photos'])}</td>
  <td><code>{esc(g['code']) or '—'}</code></td>
  <td>{'<span class="tag">Expired</span>' if g['expired'] else '<span class="tag">No code</span>' if not g['code'] else '<span class="tag ok">Live</span>'}</td>
</tr>""" for g in all_galleries())
        table = (f'<div class="table-wrap"><table class="data"><thead><tr><th>Gallery</th><th>Client</th><th>Photos</th>'
                 f'<th>Code</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>'
                 if rows else '<p class="muted">No galleries yet.</p>')
        body = f"""
  <div class="admin-head"><h1>Client galleries</h1><a class="btn small" href="/admin/galleries/new">New gallery</a></div>
  <p class="muted">Clients open their gallery at <strong>{esc(CFG['server']['site_url'].rstrip('/'))}/gallery</strong> with its code.</p>
  {table}"""
        self.admin_page(body, "Galleries", "galleries", notice)

    def admin_gallery_form(self, slug, client_param, notice):
        g = None
        if slug:
            g = read_gallery(slug)
            if not g:
                return self.not_found()
        names = self.client_names()
        current = g["client_id"] if g else (int(client_param) if client_param.isdigit() else None)
        copts = '<option value="">No client</option>' + "".join(
            f'<option value="{cid}"{" selected" if cid == current else ""}>{esc(n)}</option>' for cid, n in names.items())
        default_title = ""
        if not g and current in names:
            default_title = f"{names[current]} · {time.strftime('%B %Y')}"
        v = (lambda k, d="": esc(g[k]) if g else esc(d))
        form = f"""
  <form method="post" action="/admin/galleries/save" class="form card-pad">
    <input type="hidden" name="slug" value="{esc(slug or '')}">
    <label class="full">Gallery title<input name="title" value="{v('title', default_title)}" required maxlength="120" placeholder="The Smith Family · Fall 2026"></label>
    <label>Client<select name="client_id">{copts}</select></label>
    <label>Access code<input name="code" value="{v('code', new_gallery_code())}" required maxlength="60" autocomplete="off"></label>
    <label>Available until <span class="opt">(optional)</span><input name="expires" type="date" value="{v('expires')}"></label>
    <label class="full">Note to the client <span class="opt">(shown at the top of the gallery)</span><textarea name="note" maxlength="1000">{v('note')}</textarea></label>
    <div class="full row-actions"><button class="btn">{'Save changes' if g else 'Create gallery'}</button>
      <a class="btn ghost" href="/admin/galleries">Back to galleries</a></div>
  </form>"""
        extra = ""
        if g:
            site = CFG["server"]["site_url"].rstrip("/")
            client_name = names.get(g["client_id"], "")
            share = (f"Hi{(' ' + client_name.split()[0]) if client_name else ''}! Your photos are ready. "
                     f"Open {site}/gallery and enter the code {g['code']} to view and download them.")
            client_email = ""
            if g["client_id"]:
                with db() as c:
                    row = c.execute("SELECT email FROM clients WHERE id=?", (g["client_id"],)).fetchone()
                    client_email = row["email"] if row else ""
            mailto = (f"mailto:{quote(client_email)}?subject={quote('Your photos are ready!')}&amp;body={quote(share)}")
            photos = "".join(f"""
    <figure class="admin-thumb">
      <img src="/admin/galleries/{g['slug']}/thumb/{quote(n)}" alt="" loading="lazy">
      <figcaption><span>{esc(n)}</span>
        <form method="post" action="/admin/galleries/{g['slug']}/photo-delete" data-confirm="Remove {esc(n)} from this gallery?">
          <input type="hidden" name="name" value="{esc(n)}"><button class="link-btn" aria-label="Remove {esc(n)}">Remove</button></form>
      </figcaption>
    </figure>""" for n in g["photos"])
            status = ("This gallery has expired, so the code no longer opens it." if g["expired"]
                      else "This gallery is live. Clients can open it with the code.")
            extra = f"""
  <section class="share card-pad">
    <h2>Send it to your client</h2>
    <p class="muted">{status}</p>
    <textarea readonly id="share-text" rows="3">{esc(share)}</textarea>
    <div class="row-actions"><button class="btn small" type="button" data-copy="share-text">Copy message</button>
      <a class="btn ghost small" href="{mailto}">Open in email</a></div>
  </section>
  <section>
    <div class="admin-head"><h2>Photos <span class="muted">({len(g['photos'])})</span></h2></div>
    <div class="dropzone" data-upload="/admin/upload?kind=gallery&amp;slug={g['slug']}">
      <p><strong>Add photos</strong>: drag them here or <label class="link-btn">choose files<input type="file" accept="image/jpeg,image/png,image/webp" multiple hidden></label></p>
      <p class="muted small">JPEG, PNG or WebP, up to {MAX_UPLOAD // 1024 // 1024} MB each.{'' if HAVE_PIL else ' Tip: install python3-pil on the Pi so previews load faster.'}</p>
      <div class="progress" aria-live="polite"></div>
    </div>
    <div class="admin-grid">{photos or '<p class="muted">No photos yet.</p>'}</div>
  </section>
  <form method="post" action="/admin/galleries/{g['slug']}/delete" data-confirm="Delete the whole gallery “{esc(g['title'])}”? It's moved to data/trash on the Pi, not erased.">
    <button class="btn ghost small danger">Delete gallery</button></form>"""
        title = g["title"] if g else "New gallery"
        self.admin_page(f'<div class="admin-head"><h1>{esc(title)}</h1></div>{form}{extra}', title, "galleries", notice,
                        crumbs=[("Galleries", "/admin/galleries"), (title, None)])

    def admin_gallery_save(self, form):
        f = {k: (form.get(k) or "").strip() for k in ("slug", "title", "client_id", "code", "expires", "note")}
        code = re.sub(r"\s+", "-", f["code"])[:60]
        if not f["title"] or not code:
            return self.redirect("/admin/galleries/new")
        if f["expires"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["expires"]):
            f["expires"] = ""
        clash = [g for g in all_galleries() if g["code"].lower() == code.lower() and g["slug"] != f["slug"]]
        if clash:
            return self.redirect((f"/admin/galleries/{f['slug']}" if f["slug"] else "/admin/galleries/new") + "?done=code-taken")
        if f["slug"]:
            g = read_gallery(f["slug"])
            if not g:
                return self.not_found()
            folder, slug, created = g["folder"], g["slug"], g["created"]
        else:
            base = slugify(f["title"])
            slug, n = base, 2
            while (GALLERIES / slug).exists():
                slug, n = f"{base}-{n}", n + 1
            folder, created = GALLERIES / slug, time.strftime("%Y-%m-%d")
            folder.mkdir(parents=True)
        write_gallery_ini(folder, {"title": f["title"][:120], "code": code, "note": f["note"][:1000],
                                   "expires": f["expires"], "created": created,
                                   "client_id": f["client_id"] if f["client_id"].isdigit() else ""})
        self.redirect(f"/admin/galleries/{slug}?done=gallery-saved")

    def admin_gallery_delete(self, slug, photo):
        g = read_gallery(slug)
        if not g:
            return self.not_found()
        trash = DATA / "trash" / time.strftime("%Y%m%d-%H%M%S")
        trash.mkdir(parents=True, exist_ok=True)
        if photo is None:
            g["folder"].rename(trash / slug)
            return self.redirect("/admin/galleries?done=gallery-deleted")
        if photo in g["photos"]:
            (g["folder"] / photo).rename(trash / photo)
            thumb = g["folder"] / "thumbs" / photo
            if thumb.is_file():
                thumb.unlink()
        self.redirect(f"/admin/galleries/{slug}?done=photo-removed")

    # ---- admin: site photos
    def admin_photos(self, notice):
        pages = {f: f.read_text(encoding="utf-8") for f in PAGES.rglob("*.html")}

        def used_on(name):
            hits = []
            for f, text in pages.items():
                if name in text:
                    rel = f.relative_to(PAGES).with_suffix("").as_posix().replace("/index", "")
                    hits.append("Home" if rel == "index" else rel.replace("-", " ").replace("/", " › ").title())
            return ", ".join(sorted(hits)) or "Not used on any page"

        def card(name, portfolio=False, first=False, last=False):
            f = PHOTOS / name
            kb = f.stat().st_size / 1024
            size = f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"
            hint = PHOTO_HINTS.get(name, "Portfolio photo, any shape" if portfolio else "")
            move = ""
            if portfolio:
                move = "".join(
                    f'<form method="post" action="/admin/photos/move"><input type="hidden" name="name" value="{esc(name)}">'
                    f'<input type="hidden" name="dir" value="{d}"><button class="link-btn"{" disabled" if dis else ""} '
                    f'aria-label="Move {esc(name)} {d}">{lab}</button></form>'
                    for d, lab, dis in (("up", "◀ Earlier", first), ("down", "Later ▶", last)))
                move += (f'<form method="post" action="/admin/photos/delete" data-confirm="Remove {esc(name)} from the portfolio?">'
                         f'<input type="hidden" name="name" value="{esc(name)}"><button class="link-btn">Remove</button></form>')
            return f"""
    <figure class="admin-thumb photo-card">
      <img src="/static/img/photos/{esc(name)}?v={int(f.stat().st_mtime)}" alt="" loading="lazy">
      <figcaption>
        <strong>{esc(name)}</strong>
        <span class="muted small">{esc(hint)}{' · ' if hint else ''}{size}</span>
        {'' if portfolio else f'<span class="muted small">Used on: {esc(used_on(name))}</span>'}
        <div class="row-actions">
          <label class="btn ghost small" data-upload="/admin/upload?kind=site&amp;name={quote(name)}">Replace<input type="file" accept="image/*" hidden></label>
          {move}
        </div>
        <div class="progress" aria-live="polite"></div>
      </figcaption>
    </figure>"""

        port = portfolio_photos()
        site = sorted(p.name for p in PHOTOS.iterdir()
                      if p.is_file() and p.suffix.lower() in IMAGE_EXT and p.name not in port)
        body = f"""
  <div class="admin-head"><h1>Site photos</h1></div>
  <p class="muted">Replace a photo to update it everywhere it appears. Use a JPEG for .jpg photos. Changes show up right away.</p>
  <h2>Portfolio <span class="muted">({len(port)})</span></h2>
  <p class="muted">These appear on the Portfolio page in this order; the first four also show on the home page.</p>
  <div class="dropzone" data-upload="/admin/upload?kind=portfolio">
    <p><strong>Add portfolio photos</strong>: drag them here or <label class="link-btn">choose files<input type="file" accept="image/jpeg,image/png,image/webp" multiple hidden></label></p>
    <div class="progress" aria-live="polite"></div>
  </div>
  <div class="admin-grid">{''.join(card(n, True, i == 0, i == len(port) - 1) for i, n in enumerate(port))}</div>
  <h2 class="mt-xl">Page photos</h2>
  <div class="admin-grid">{''.join(card(n) for n in site)}</div>"""
        self.admin_page(body, "Site photos", "photos", notice)

    def admin_photo_delete(self, name):
        if name in portfolio_photos():
            trash = DATA / "trash" / time.strftime("%Y%m%d-%H%M%S")
            trash.mkdir(parents=True, exist_ok=True)
            (PHOTOS / name).rename(trash / name)
        self.redirect("/admin/photos?done=photo-removed")

    def admin_photo_move(self, name, direction):
        names = portfolio_photos()
        if name in names and direction in ("up", "down"):
            i = names.index(name)
            j = i - 1 if direction == "up" else i + 1
            if 0 <= j < len(names):
                a, b = PHOTOS / names[i], PHOTOS / names[j]
                tmp = PHOTOS / f".swap-{secrets.token_hex(4)}"
                a.rename(tmp)
                b.rename(PHOTOS / (Path(names[i]).stem + b.suffix))
                tmp.rename(PHOTOS / (Path(names[j]).stem + a.suffix))
        self.redirect("/admin/photos")

    # ---- admin: uploads (one file per request, streamed to disk)
    def admin_upload(self):
        q = self.query()
        kind, raw_name = q.get("kind", ""), q.get("name", "")

        def fail(msg, status=400):
            # drain the body so the browser sees the error instead of a reset connection
            remaining = int(self.headers.get("Content-Length") or 0)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 256 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.send(status, json.dumps({"ok": False, "error": msg}), "application/json")

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return fail("The file was empty.")
        if length > MAX_UPLOAD:
            return fail(f"That file is over {MAX_UPLOAD // 1024 // 1024} MB.", 413)

        if kind == "gallery":
            g = read_gallery(q.get("slug", ""))
            if not g:
                return fail("Gallery not found.", 404)
            folder = g["folder"]
        elif kind in ("site", "portfolio"):
            folder = PHOTOS
        else:
            return fail("Unknown upload type.")

        tmp = folder / f".upload-{secrets.token_hex(6)}"
        try:
            with open(tmp, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 256 * 1024))
                    if not chunk:
                        raise ConnectionError("upload interrupted")
                    f.write(chunk)
                    remaining -= len(chunk)
            with open(tmp, "rb") as f:
                ext = sniff_image(f.read(16))
            if not ext:
                tmp.unlink()
                return self.send(400, json.dumps({"ok": False, "error": "That doesn't look like a JPEG, PNG or WebP photo."}),
                                 "application/json")

            if kind == "site":
                if raw_name not in os.listdir(PHOTOS):
                    tmp.unlink()
                    return self.send(404, json.dumps({"ok": False, "error": "Photo not found."}), "application/json")
                want = Path(raw_name).suffix.lower().replace(".jpeg", ".jpg")
                if want != ext:
                    tmp.unlink()
                    return self.send(400, json.dumps({"ok": False, "error": f"Please upload a {want[1:].upper()} file to replace {raw_name}."}),
                                     "application/json")
                dest = PHOTOS / raw_name
                trash = DATA / "trash" / time.strftime("%Y%m%d-%H%M%S")
                trash.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, trash / raw_name)  # keep the old one, just in case
            elif kind == "portfolio":
                nums = [int(m.group(1)) for n in portfolio_photos() if (m := re.match(r"portfolio-(\d+)", n))]
                dest = PHOTOS / f"portfolio-{(max(nums) + 1 if nums else 1):02d}{ext}"
            else:
                stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(raw_name).stem).strip("-")[:60] or "photo"
                dest = folder / f"{stem}{ext}"
                n = 2
                while dest.exists():
                    dest, n = folder / f"{stem}-{n}{ext}", n + 1
            tmp.replace(dest)
            if kind == "gallery":
                make_thumb(dest)
            return self.send(200, json.dumps({"ok": True, "name": dest.name}), "application/json")
        finally:
            if tmp.exists():
                tmp.unlink()

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
