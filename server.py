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
STATUSES = ["new", "contacted", "booked", "completed", "archived"]
CLIENT_STATUSES = ["lead", "active", "past"]
DEPOSIT_DUE = ' <span class="tag due">Deposit due</span>'
CURRENT = ' aria-current="page"'
MAX_BODY = 32 * 1024
MAX_UPLOAD = 60 * 1024 * 1024  # per photo

ADMIN_SECTIONS = [("dashboard", "/admin", "Dashboard"), ("sessions", "/admin/sessions", "Sessions"),
                  ("minis", "/admin/minis", "Mini sessions"), ("clients", "/admin/clients", "Clients"),
                  ("galleries", "/admin/galleries", "Galleries"),
                  ("emails", "/admin/emails", "Emails"),
                  ("photos", "/admin/photos", "Site photos")]
# Simple line icons for the admin sidebar (24x24, stroke = currentColor)
ADMIN_ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    "sessions": '<path d="M4 4h16v12H5.5L4 17.5z"/><path d="M8 9h8M8 12h5"/>',
    "minis": '<rect x="3.5" y="5" width="17" height="15" rx="1.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/><path d="M8 14h2M12 14h2M16 14h0.5M8 17h2"/>',
    "clients": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><circle cx="17" cy="9" r="2.5"/><path d="M17 14.5c2.3 0 4 1.5 4.5 4"/>',
    "galleries": '<rect x="3" y="5" width="18" height="14" rx="1.5"/><circle cx="9" cy="10" r="1.8"/><path d="M3 17l5-4.5 4 3.5 3-2.5 6 4.5"/>',
    "photos": '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
    "emails": '<rect x="3" y="5" width="18" height="14" rx="1.5"/><path d="M3.5 6l8.5 7 8.5-7"/>',
}
NOTICES = {"client-saved": "Client saved.", "client-deleted": "Client deleted.",
           "gallery-saved": "Gallery saved.", "gallery-deleted": "Gallery moved to the trash folder.",
           "photo-removed": "Photo removed.", "mini-saved": "Mini session saved.",
           "mini-deleted": "Mini session deleted.", "booking-cancelled": "Booking cancelled. That time is open again.",
           "mini-invalid": "Please fill in the title, date, start and end times, and minutes per session.", "code-taken": "Another gallery already uses that code. Pick a different one.",
           "email-sent": "Email sent.", "booking-confirmed": "Confirmation sent. The session is marked booked.",
           "session-saved": "Session details saved.", "payment-saved": "Payment updated.",
           "template-saved": "Template saved.", "test-sent": "Test email sent. Check the inbox.",
           "test-failed": "The test email didn't go through. Check the [email] settings in config.ini."}
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
                     "instagram": "", "facebook": "", "deposit_link": "", "deposit": "$50"},
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
    _c.execute("""CREATE TABLE IF NOT EXISTS mini_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
        date TEXT NOT NULL, location TEXT, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
        slot_minutes INTEGER NOT NULL DEFAULT 20, gap_minutes INTEGER NOT NULL DEFAULT 10,
        price TEXT, details TEXT, status TEXT NOT NULL DEFAULT 'draft')""")
    _c.execute("""CREATE TABLE IF NOT EXISTS mini_bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT NOT NULL, event_id INTEGER NOT NULL, slot TEXT NOT NULL,
        name TEXT, email TEXT, phone TEXT, people TEXT, notes TEXT,
        client_id INTEGER, status TEXT NOT NULL DEFAULT 'booked', ref TEXT, ip TEXT)""")
    _c.execute("""CREATE TABLE IF NOT EXISTS email_templates (
        key TEXT PRIMARY KEY, name TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, sort INTEGER DEFAULT 0)""")
    _c.execute("""CREATE TABLE IF NOT EXISTS email_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, to_addr TEXT, subject TEXT, body TEXT,
        kind TEXT, client_id INTEGER, inquiry_id INTEGER, status TEXT, error TEXT)""")
    # one live booking per slot, enforced by the database itself
    _c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_booking_per_slot
                  ON mini_bookings(event_id, slot) WHERE status='booked'""")
    # Bring databases made by older versions up to date by adding any missing columns.
    for _table, _cols in {
        "inquiries": {"client_id": "INTEGER", "adults": "INTEGER", "kids": "INTEGER",
                      "session_date": "TEXT", "session_time": "TEXT", "session_location": "TEXT",
                      "price": "TEXT", "deposit": "TEXT", "deposit_link": "TEXT",
                      "deposit_paid": "TEXT", "paid_full": "TEXT", "gallery": "TEXT",
                      "pref_date": "TEXT", "pref_time": "TEXT"},
        "clients": {"created": "TEXT NOT NULL DEFAULT ''", "email": "TEXT", "phone": "TEXT",
                    "family": "TEXT", "notes": "TEXT", "status": "TEXT NOT NULL DEFAULT 'active'",
                    "adults": "INTEGER", "kids": "INTEGER"},
        "mini_events": {"price": "TEXT", "details": "TEXT", "gap_minutes": "INTEGER NOT NULL DEFAULT 0",
                        "status": "TEXT NOT NULL DEFAULT 'draft'"},
        "mini_bookings": {"phone": "TEXT", "people": "TEXT", "notes": "TEXT", "client_id": "INTEGER",
                          "ref": "TEXT", "ip": "TEXT", "adults": "INTEGER", "kids": "INTEGER"},
    }.items():
        _have = {r[1] for r in _c.execute(f"PRAGMA table_info({_table})")}
        for _col, _ddl in _cols.items():
            if _col not in _have:
                _c.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_ddl}")


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
    page = re.sub(r"(/static/img/photos/)([\w.-]+\.(?:jpe?g|png|webp))(?![?\w.])", stamp, page)
    return re.sub(r"/static/(css|js)/([\w.-]+\.(?:css|js))(?![?\w.])", version_asset, page)


def version_asset(m):
    """Same idea for stylesheets and scripts, so a site update shows without a hard refresh."""
    f = STATIC / m.group(1) / m.group(2)
    return m.group(0) if not f.is_file() else f"{m.group(0)}?v={int(f.stat().st_mtime)}"


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


def headcount(form, adults=2, kids=0):
    """Adults and kids from the two number pickers, kept to sensible whole numbers."""
    def num(key, default, low):
        try:
            return max(low, min(30, int((form.get(key) or "").strip())))
        except ValueError:
            return default
    return num("adults", adults, 1), num("kids", kids, 0)


def party(adults, kids):
    """'2 adults, 1 kid' from stored counts; empty when the counts were never recorded."""
    if adults is None and kids is None:
        return ""
    a, k = adults or 0, kids or 0
    return f"{a} adult{'' if a == 1 else 's'}, {k} kid{'' if k == 1 else 's'}"


def party_of(row):
    """Party size for a row, falling back to the free-text answer older versions stored."""
    keys = row.keys()
    text = party(row["adults"], row["kids"]) if "adults" in keys else ""
    return text or (row["people"] if "people" in keys else "") or ""


def pickers(adults, kids, legend="Who's coming?"):
    """The adults and kids number pickers (static/js adds the minus and plus buttons)."""
    return f"""<fieldset class="headcount"><legend>{legend}</legend>
          <div class="count"><label for="f-adults">Adults</label><input id="f-adults" name="adults" type="number" inputmode="numeric" min="1" max="30" required value="{adults}"></div>
          <div class="count"><label for="f-kids">Kids</label><input id="f-kids" name="kids" type="number" inputmode="numeric" min="0" max="30" required value="{kids}"></div>
        </fieldset>"""


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
    send_mail(f"New session inquiry: {inquiry['name']} ({inquiry['session_type']})",
              "\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in inquiry.items()),
              CFG["email"]["notify_address"] or CFG["business"]["email"], inquiry.get("email", ""))


# --------------------------------------------------------------------------- mini sessions

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("RBG_TIMEZONE", "America/New_York"))
except Exception:  # no tz database: calendar times are left "floating" (local)
    LOCAL_TZ = None

MINI_STATUSES = ["draft", "open", "closed"]


def mini_event(event_id=None, slug=None):
    with db() as c:
        if slug is not None:
            return c.execute("SELECT * FROM mini_events WHERE slug=?", (slug,)).fetchone()
        return c.execute("SELECT * FROM mini_events WHERE id=?", (event_id,)).fetchone()


def mini_slots(ev):
    """Start times ("HH:MM") for an event, from start to end in slot+gap steps."""
    def mins(t):
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    try:
        start, end = mins(ev["start_time"]), mins(ev["end_time"])
        length, gap = int(ev["slot_minutes"]), int(ev["gap_minutes"] or 0)
    except (ValueError, TypeError, AttributeError):
        return []
    out, t = [], start
    while length > 0 and t + length <= end and len(out) < 200:
        out.append(f"{t // 60:02d}:{t % 60:02d}")
        t += length + gap
    return out


def mini_booked(event_id):
    with db() as c:
        return {r["slot"]: r for r in c.execute(
            "SELECT * FROM mini_bookings WHERE event_id=? AND status='booked'", (event_id,))}


def nice_time(hhmm):
    h, m = map(int, hhmm.split(":"))
    return f"{(h - 1) % 12 + 1}:{m:02d} {'am' if h < 12 else 'pm'}"


def nice_date(iso):
    try:
        d = time.strptime(iso, "%Y-%m-%d")
    except (ValueError, TypeError):
        return iso or ""
    return time.strftime("%A, %B ", d) + str(d.tm_mday) + time.strftime(", %Y", d)


def end_of(hhmm, minutes):
    h, m = map(int, hhmm.split(":"))
    t = h * 60 + m + int(minutes)
    return f"{t // 60:02d}:{t % 60:02d}"


def calendar_token():
    return hmac.new(SECRET, b"calendar-feed", hashlib.sha256).hexdigest()[:32]


def ics_escape(s):
    return str(s or "").replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def ics_time(date, hhmm):
    """UTC timestamp for a local date+time (floating local time if no tz database)."""
    import datetime as _dt
    local = _dt.datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H:%M")
    if LOCAL_TZ is None:
        return local.strftime("%Y%m%dT%H%M%S")
    return local.replace(tzinfo=LOCAL_TZ).astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ics_calendar(events, name):
    """events: dicts with uid, date, start, end, summary, location, description."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//RBG Photography//Website//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_escape(name)}"]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{stamp}",
                  f"DTSTART:{ics_time(e['date'], e['start'])}", f"DTEND:{ics_time(e['date'], e['end'])}",
                  f"SUMMARY:{ics_escape(e['summary'])}", f"LOCATION:{ics_escape(e['location'])}",
                  f"DESCRIPTION:{ics_escape(e['description'])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    # RFC 5545 wants lines folded at 75 octets
    out = []
    for line in lines:
        while len(line.encode()) > 75:
            cut = 74
            while len(line[:cut].encode()) > 74:
                cut -= 1
            out.append(line[:cut])
            line = " " + line[cut:]
        out.append(line)
    return "\r\n".join(out) + "\r\n"


# Starting points for emails Rachel sends from the admin. She can edit them under Emails.
# Words in {braces} are filled in from the client, inquiry, booking or gallery.
EMAIL_TEMPLATES = [
    ("confirm", "Booking confirmation", "Your {session_type} is confirmed",
     "Hi {first_name},\n\nThank you for booking with {business}! Your {session_type} is confirmed:\n\n"
     "Date: {date}\nTime: {time}\nLocation: {location}\n\n"
     "To hold your date, please pay the {deposit} deposit here:\n{deposit_link}\n\n"
     "I'll send a few tips on what to wear and what to bring a week before. If anything changes, "
     "just reply to this email.\n\nSee you soon!\n{photographer}\n{business}"),
    ("reply", "Reply to a session request", "Your session with {business}",
     "Hi {first_name},\n\nThank you so much for reaching out about a {session_type}! "
     "I'd love to work with your family.\n\n\n\nTalk soon,\n{photographer}\n{business}"),
    ("reminder", "Session reminder", "See you {date}!",
     "Hi {first_name},\n\nJust a reminder that your {session_type} is coming up on {date} at {time}, "
     "at {location}.\n\nPlease arrive a few minutes early. If the weather looks iffy, I'll reach out "
     "the day before.\n\nSee you soon!\n{photographer}"),
    ("gallery", "Gallery ready", "Your photos are ready!",
     "Hi {first_name},\n\nYour photos are ready! Open {gallery_link} and enter the code {gallery_code} "
     "to view and download them.\n\nIt was such a joy photographing your family.\n\n{photographer}\n{business}"),
    ("thanks", "Thank you", "Thank you from {business}",
     "Hi {first_name},\n\nThank you again for choosing {business}. If you loved your photos, a short review "
     "would mean the world to me, and I'd be happy to photograph your family again anytime.\n\n{photographer}"),
    ("message", "Blank message", "", "Hi {first_name},\n\n\n\n{photographer}\n{business}"),
]
OLD_CONFIRM_BODY = (
    "Hi {first_name},\n\nThank you for booking with {business}! Your {session_type} is confirmed:\n\n"
    "Date: {date}\nTime: {time}\nLocation: {location}\n\n"
    "I'll send a few tips on what to wear and what to bring a week before. If anything changes, "
    "just reply to this email.\n\nSee you soon!\n{photographer}\n{business}")
with db() as _c:
    # templates still exactly as first shipped pick up the deposit line and new wording
    _c.execute("UPDATE email_templates SET body=? WHERE key='confirm' AND body=?", (EMAIL_TEMPLATES[0][3], OLD_CONFIRM_BODY))
    _c.execute("UPDATE email_templates SET name=? WHERE key='reply' AND name='Reply to an inquiry'", (EMAIL_TEMPLATES[1][1],))
    _c.executemany("INSERT OR IGNORE INTO email_templates (key, name, subject, body, sort) VALUES (?,?,?,?,?)",
                   [(*t, n) for n, t in enumerate(EMAIL_TEMPLATES)])


def email_enabled():
    e = CFG["email"]
    return e.get("enabled", "false").lower() == "true" and bool(e.get("smtp_host"))


def fill_template(text, values):
    """Replace {word} with values[word]; words with no value are left for Rachel to fill in."""
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(values[m.group(1)]) if values.get(m.group(1)) else m.group(0), text)


def unfilled(text):
    return sorted(set(re.findall(r"\{([a-z_]+)\}", text)))


def send_mail(subject, body, to, reply_to="", log=None):
    """Send an email if [email] is enabled in config.ini. Never raises.
    Returns None when sent, otherwise a short reason. Pass log={kind, client_id, inquiry_id}
    to record a client email in the admin's email history."""
    e = CFG["email"]
    error = None
    if not to:
        return "No email address."
    if not email_enabled():
        error = "Email sending is turned off in config.ini."
    else:
        error = _smtp_send(e, subject, body, to, reply_to)
    if log is not None:
        with DB_LOCK, db() as c:
            c.execute("""INSERT INTO email_log (created, to_addr, subject, body, kind, client_id, inquiry_id, status, error)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (time.strftime("%Y-%m-%d %H:%M"), to, subject, body, log.get("kind", ""), log.get("client_id"),
                       log.get("inquiry_id"), "failed" if error else "sent", error))
    return error


def _smtp_send(e, subject, body, to, reply_to):
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = email.utils.formataddr((CFG["business"]["name"], e["from_address"] or e["smtp_user"]))
        msg["To"] = to
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=(e["from_address"] or e["smtp_user"]).rpartition("@")[2] or None)
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)
        with smtplib.SMTP(e["smtp_host"], int(e["smtp_port"]), timeout=20) as s:
            s.starttls()
            if e["smtp_user"]:
                s.login(e["smtp_user"], e["smtp_password"])
            s.send_message(msg)
        return None
    except Exception as exc:  # the booking is already saved; just log the failure
        print(f"email failed ({subject}): {exc}", file=sys.stderr)
        return str(exc)[:300] or exc.__class__.__name__


def client_for_inquiry(c, r):
    """The client record for an inquiry, creating a lead from it when there isn't one yet."""
    if r["client_id"]:
        return r["client_id"]
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
        counted = r["adults"] is not None
        cid = c.execute("""INSERT INTO clients (created, name, email, phone, family, notes, status, adults, kids)
                           VALUES (?,?,?,?,?,?, 'lead', ?, ?)""",
                        (time.strftime("%Y-%m-%d"), r["name"], r["email"], r["phone"],
                         "" if counted else r["people"], note, r["adults"], r["kids"])).lastrowid
    c.execute("UPDATE inquiries SET client_id=? WHERE id=?", (cid, r["id"]))
    c.execute("UPDATE inquiries SET client_id=? WHERE client_id IS NULL AND lower(email)=lower(?)",
              (cid, r["email"]))
    return cid


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
            if p == "/minis" or p.startswith("/minis/"):
                return self.minis_get(p)
            m = re.fullmatch(r"/calendar/([0-9a-f]{32})\.ics", p)
            if m:
                return self.calendar_feed(m.group(1))
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
            m = re.fullmatch(r"/minis/([a-z0-9-]+)/book", p)
            if m:
                return self.mini_book(m.group(1))
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
        body = f"User-agent: *\nDisallow: /admin\nDisallow: /gallery/\nDisallow: /api/\nDisallow: /calendar/\n\nSitemap: {url}/sitemap.xml\n"
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
        locs.append(f"<url><loc>{esc(url)}/minis</loc></url>")
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
        adults, kids = headcount(form)
        data["people"] = party(adults, kids)
        pref_date = (form.get("pref_date") or "").strip()
        pref_date = pref_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", pref_date) else None
        pref_time = (form.get("pref_time") or "").strip()
        pref_time = pref_time if re.fullmatch(r"\d{1,2}:\d{2}", pref_time) else None
        ip = self.client_ip()
        if not INQUIRY_LIMIT.allow(ip):
            return fail("Thanks! We've received several messages from you already. Rachel will be in touch soon.", 429)
        with DB_LOCK, db() as c:
            c.execute("""INSERT INTO inquiries (created, name, email, phone, session_type, people,
                         dates, location, heard, message, ip, adults, kids, pref_date, pref_time)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (time.strftime("%Y-%m-%d %H:%M"), *data.values(), ip, adults, kids, pref_date, pref_time))
        data["preferred"] = ", ".join(x for x in [nice_date(pref_date) if pref_date else "",
                                                  nice_time(pref_time) if pref_time else ""] if x)
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
            badge = f'<span class="badge">{new_count}</span>' if key == "sessions" and new_count else ""
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
        if p == "/admin/sessions":
            return self.admin_sessions(q.get("status", "open"), notice)
        if p in ("/admin/sessions.csv", "/admin/inquiries.csv"):
            return self.admin_csv()
        if p == "/admin/inquiries":  # old address
            return self.redirect("/admin/sessions" + (f"?status={quote(q['status'])}" if q.get("status") else ""))
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
            return self.admin_gallery_form(None, q.get("client", ""), notice, q.get("session", ""))
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
        if p == "/admin/minis":
            return self.admin_minis(notice)
        if p == "/admin/minis/new":
            return self.admin_mini_form(None, notice)
        m = re.fullmatch(r"/admin/minis/(\d+)", p)
        if m:
            return self.admin_mini_form(int(m.group(1)), notice)
        if p == "/admin/emails":
            return self.admin_emails(q.get("edit", ""), notice)
        if p == "/admin/email":
            return self.admin_compose(q)
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
            return self.redirect("/admin/sessions?status=" + (back if back in STATUSES + ["open", "all"] else "open")
                                 + (f"#s{form['id']}" if form.get("id", "").isdigit() else ""))
        m = re.fullmatch(r"/admin/sessions/(\d+)/(save|pay)", p)
        if m:
            sid = int(m.group(1))
            return self.admin_session_save(sid, form) if m.group(2) == "save" else self.admin_session_pay(sid, form)
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
        if p == "/admin/minis/save":
            return self.admin_mini_save(form)
        m = re.fullmatch(r"/admin/minis/(\d+)/(cancel|delete)", p)
        if m:
            if m.group(2) == "cancel":
                return self.admin_mini_cancel(int(m.group(1)), form)
            return self.admin_mini_delete(int(m.group(1)))
        if p == "/admin/email/send":
            return self.admin_email_send(form)
        if p == "/admin/emails/template":
            return self.admin_template_save(form)
        if p == "/admin/emails/test":
            err = send_mail(f"Test email from {CFG['business']['name']}",
                            "This is a test from your website. Email sending works!",
                            CFG["email"]["notify_address"] or CFG["business"]["email"])
            return self.redirect("/admin/emails?done=" + ("test-failed" if err else "test-sent"))
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
            stat(new_count, "New requests", "/admin/sessions?status=new", "Waiting for a reply"),
            stat(clients, "Clients", "/admin/clients", "Leads and active families"),
            stat(live, "Live galleries", "/admin/galleries", "Clients can open these now"),
            stat(len(portfolio_photos()), "Portfolio photos", "/admin/photos", "Shown on the Portfolio page"),
        ])
        inbox = "".join(
            f'<li><a href="/admin/sessions?status=new#s{r["id"]}"><strong>{esc(r["name"])}</strong></a> '
            f'<span class="muted">· {esc(r["session_type"])} · {esc(r["created"])}</span>'
            f'{snippet(r["message"], 120)}</li>' for r in new) or '<li class="muted">You\'re all caught up.</li>'
        recent = "".join(
            f'<li><a href="/admin/galleries/{g["slug"]}"><strong>{esc(g["title"])}</strong></a> '
            f'<span class="muted">· {esc(names.get(g["client_id"], "No client"))} · {len(g["photos"])} photos</span></li>'
            for g in gals[:5]) or '<li class="muted">No galleries yet.</li>'
        with db() as c:
            upcoming = c.execute("""SELECT b.name, b.slot, e.date, e.title, e.id AS eid FROM mini_bookings b
                                    JOIN mini_events e ON e.id=b.event_id WHERE b.status='booked' AND e.date>=?
                                    ORDER BY e.date, b.slot LIMIT 6""", (time.strftime("%Y-%m-%d"),)).fetchall()
        with db() as c:
            booked = c.execute("""SELECT * FROM inquiries WHERE status='booked' AND (session_date IS NULL
                                  OR session_date='' OR session_date>=?) ORDER BY COALESCE(NULLIF(session_date, ''), '9999'),
                                  session_time LIMIT 6""", (time.strftime("%Y-%m-%d"),)).fetchall()
        sessions = "".join(
            f'<li><a href="/admin/sessions?status=booked#s{r["id"]}"><strong>{esc(r["name"])}</strong></a> '
            f'<span class="muted">· {esc(nice_date(r["session_date"]).rsplit(",", 1)[0]) if r["session_date"] else "Date not set"}'
            f'{", " + nice_time(r["session_time"]) if r["session_time"] else ""}</span>'
            f'{"" if r["deposit_paid"] else DEPOSIT_DUE}</li>'
            for r in booked) or '<li class="muted">No booked sessions coming up.</li>'
        minis = "".join(
            f'<li><a href="/admin/minis/{r["eid"]}"><strong>{esc(r["name"])}</strong></a> '
            f'<span class="muted">· {esc(nice_date(r["date"]).rsplit(",", 1)[0])}, {nice_time(r["slot"])}</span></li>'
            for r in upcoming) or '<li class="muted">No upcoming mini session bookings.</li>'
        body = f"""
  <div class="admin-head"><h1>Hello, {esc(CFG["business"]["photographer"].split()[0])}</h1></div>
  <div class="stats">{stats}</div>
  <div class="quick">
    <a class="btn small" href="/admin/galleries/new">New gallery</a>
    <a class="btn ghost small" href="/admin/minis/new">New mini session</a>
    <a class="btn ghost small" href="/admin/clients/new">Add client</a>
    <a class="btn ghost small" href="/admin/photos">Update site photos</a>
  </div>
  <div class="admin-cols">
    <section class="card-pad"><div class="admin-head"><h2>New requests</h2><a href="/admin/sessions">See all</a></div><ul class="plain">{inbox}</ul></section>
    <section class="card-pad"><div class="admin-head"><h2>Upcoming sessions</h2><a href="/admin/sessions?status=booked">See all</a></div><ul class="plain">{sessions}</ul></section>
    <section class="card-pad"><div class="admin-head"><h2>Upcoming minis</h2><a href="/admin/minis">See all</a></div><ul class="plain">{minis}</ul></section>
    <section class="card-pad"><div class="admin-head"><h2>Recent galleries</h2><a href="/admin/galleries">See all</a></div><ul class="plain">{recent}</ul></section>
  </div>"""
        self.admin_page(body, "Dashboard", "dashboard", notice)

    # ---- admin: sessions (booking requests from the Book page, then the booked session itself)
    def admin_sessions(self, show, notice):
        with db() as c:
            if show == "all":
                rows = c.execute("SELECT * FROM inquiries ORDER BY id DESC").fetchall()
            elif show == "booked":  # soonest session first, undated ones last
                rows = c.execute("""SELECT * FROM inquiries WHERE status='booked'
                                    ORDER BY COALESCE(NULLIF(session_date, ''), '9999'), session_time, id""").fetchall()
            elif show in STATUSES:
                rows = c.execute("SELECT * FROM inquiries WHERE status=? ORDER BY id DESC", (show,)).fetchall()
            else:
                show = "open"
                rows = c.execute("SELECT * FROM inquiries WHERE status NOT IN ('archived', 'completed') "
                                 "ORDER BY id DESC").fetchall()
            counts = dict(c.execute("SELECT status, COUNT(*) FROM inquiries GROUP BY status").fetchall())
        tabs = []
        for key, label in [("open", "Open"), *[(s, s.title()) for s in STATUSES], ("all", "All")]:
            n = sum(v for k, v in counts.items() if k not in ("archived", "completed")) if key == "open" else \
                sum(counts.values()) if key == "all" else counts.get(key, 0)
            tabs.append(f'<a href="/admin/sessions?status={key}"{CURRENT if key == show else ""}>{label} <span>{n}</span></a>')
        cards = "".join(self.session_card(r, show) for r in rows)
        body = f"""
  <div class="admin-head"><h1>Sessions</h1><a class="btn ghost small" href="/admin/sessions.csv">Download CSV</a></div>
  <nav class="tabs">{''.join(tabs)}</nav>
  {cards or '<p class="muted">Nothing here yet. Requests from the Book page show up here.</p>'}"""
        self.admin_page(body, "Sessions", "sessions", notice)

    def session_card(self, r, show):
        sid = r["id"]
        opts = "".join(f'<option value="{s}"{" selected" if s == r["status"] else ""}>{s.title()}</option>'
                       for s in STATUSES)
        details = "".join(
            f"<dt>{label}</dt><dd>{esc(val)}</dd>" for label, val in
            [("Phone", r["phone"]), ("Who's coming", party_of(r)),
             ("Asked for", ", ".join(x for x in [nice_date(r["pref_date"]) if r["pref_date"] else "",
                                                 nice_time(r["pref_time"]) if r["pref_time"] else ""] if x)),
             ("Other dates", r["dates"]),
             ("Location idea", r["location"]), ("Heard about us", r["heard"])] if val)
        when = ", ".join(x for x in [nice_date(r["session_date"]) if r["session_date"] else "",
                                     nice_time(r["session_time"]) if r["session_time"] else ""] if x)
        flags = "".join([
            f'<span class="tag ok">Paid in full</span>' if r["paid_full"] else
            f'<span class="tag ok">Deposit paid</span>' if r["deposit_paid"] else
            ('<span class="tag due">Deposit due</span>' if r["status"] == "booked" else ""),
        ])

        def pay(what, label, done_on):
            if done_on:
                return (f'<form method="post" action="/admin/sessions/{sid}/pay" class="paid">'
                        f'<input type="hidden" name="what" value="{what}"><input type="hidden" name="undo" value="1">'
                        f'<span class="check on" aria-hidden="true">✓</span> <strong>{label}</strong> '
                        f'<span class="muted">{esc(nice_date(done_on))}</span> '
                        f'<button class="link-btn small">Undo</button></form>')
            return (f'<form method="post" action="/admin/sessions/{sid}/pay" class="paid">'
                    f'<input type="hidden" name="what" value="{what}">'
                    f'<button class="btn ghost small"><span class="check" aria-hidden="true"></span>Mark {label.lower()}</button></form>')
        if r["gallery"] and read_gallery(r["gallery"]):
            gallery_btn = f'<a class="btn ghost small" href="/admin/galleries/{esc(r["gallery"])}">View gallery</a>'
        else:
            gallery_btn = f'<a class="btn ghost small" href="/admin/galleries/new?session={sid}">Create gallery</a>'
        client_btn = (f'<a class="btn ghost small" href="/admin/clients/{r["client_id"]}">View client</a>' if r["client_id"] else
                      f'<form method="post" action="/admin/inquiries/{sid}/client"><button class="btn ghost small">Add to clients</button></form>')
        v = (lambda k: esc(r[k] or ""))
        booked = r["status"] in ("booked", "completed")
        session_box = f"""
  <details class="session-box"{" open" if booked else ""}>
    <summary>Session details{f' · {esc(when)}' if when else ''}</summary>
    <form method="post" action="/admin/sessions/{sid}/save" class="form compact">
      <label>Date<input type="date" name="session_date" value="{esc(r['session_date'] or r['pref_date'] or '')}"></label>
      <label>Time<input type="time" name="session_time" value="{esc(r['session_time'] or r['pref_time'] or '')}"></label>
      <label class="full">Location<input name="session_location" maxlength="200" value="{esc(r['session_location'] or r['location'] or '')}"></label>
      <label>Price<input name="price" maxlength="40" value="{v('price')}" placeholder="$425"></label>
      <label>Deposit<input name="deposit" maxlength="40" value="{esc(r['deposit'] or CFG['business']['deposit'])}"></label>
      <label class="full">Deposit link <span class="opt">(goes in the confirmation email)</span>
        <input name="deposit_link" type="url" maxlength="500" value="{esc(r['deposit_link'] or CFG['business']['deposit_link'])}" placeholder="https://…"></label>
      <div class="full row-actions"><button class="btn small">Save details</button></div>
    </form>
    <div class="payments">{pay("deposit", "Deposit paid", r["deposit_paid"])}{pay("full", "Paid in full", r["paid_full"])}</div>
  </details>"""
        return f"""
<article class="inq status-{esc(r['status'])}" id="s{sid}">
  <header><h3>{esc(r['name'])}</h3><span class="tag">{esc(r['session_type'])}</span>{flags}
    <time>Requested {esc(r['created'])}</time></header>
  <p><a href="mailto:{esc(r['email'])}">{esc(r['email'])}</a></p>
  <dl>{details}</dl>
  {f'<blockquote>{esc(r["message"])}</blockquote>' if r['message'] else ''}
  {session_box}
  <div class="row-actions">
    <form method="post" action="/admin/status" class="inline">
      <input type="hidden" name="id" value="{sid}"><input type="hidden" name="back" value="{esc(show)}">
      <label>Status <select name="status">{opts}</select></label> <button class="btn small">Save</button>
    </form>
    {client_btn}{gallery_btn}
    <a class="btn small" href="/admin/email?inquiry={sid}&amp;template=confirm">{'Resend confirmation' if booked else 'Confirm booking'}</a>
    <a class="btn ghost small" href="/admin/email?inquiry={sid}&amp;template=reply">Email</a>
  </div>
</article>"""

    def admin_session_save(self, sid, form):
        f = {k: (form.get(k) or "").strip() for k in
             ("session_date", "session_time", "session_location", "price", "deposit", "deposit_link")}
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["session_date"]):
            f["session_date"] = ""
        if not re.fullmatch(r"\d{1,2}:\d{2}", f["session_time"]):
            f["session_time"] = ""
        if f["deposit_link"] and not re.match(r"https?://", f["deposit_link"]):
            f["deposit_link"] = "https://" + f["deposit_link"]
        with DB_LOCK, db() as c:
            c.execute("""UPDATE inquiries SET session_date=?, session_time=?, session_location=?, price=?, deposit=?,
                         deposit_link=? WHERE id=?""",
                      (f["session_date"], f["session_time"], f["session_location"][:200], f["price"][:40],
                       f["deposit"][:40], f["deposit_link"][:500], sid))
        self.redirect(f"/admin/sessions?status={self.session_tab(sid)}&done=session-saved#s{sid}")

    def admin_session_pay(self, sid, form):
        col = {"deposit": "deposit_paid", "full": "paid_full"}.get(form.get("what", ""))
        if not col:
            return self.not_found()
        today = time.strftime("%Y-%m-%d")
        with DB_LOCK, db() as c:
            if form.get("undo"):
                c.execute(f"UPDATE inquiries SET {col}=NULL WHERE id=?", (sid,))
            else:
                c.execute(f"UPDATE inquiries SET {col}=? WHERE id=?", (today, sid))
                if col == "paid_full":  # paid in full covers the deposit too
                    c.execute("UPDATE inquiries SET deposit_paid=? WHERE id=? AND deposit_paid IS NULL", (today, sid))
        self.redirect(f"/admin/sessions?status={self.session_tab(sid)}&done=payment-saved#s{sid}")

    def session_tab(self, sid):
        with db() as c:
            r = c.execute("SELECT status FROM inquiries WHERE id=?", (sid,)).fetchone()
        status = r["status"] if r else ""
        return "booked" if status == "booked" else "open" if status in ("new", "contacted") else "all"

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
            cid = client_for_inquiry(c, r)
        self.redirect(f"/admin/clients/{cid}?done=client-saved")

    # ---- admin: emails
    CONTEXT_KEYS = ("inquiry", "client", "booking", "gallery")
    FILL_KEYS = ("date", "time", "location", "deposit", "deposit_link")  # typed on the email screen

    def email_context(self, q):
        """Who an email goes to and the values its template can use, from ?inquiry= / client= / booking= / gallery=."""
        b = CFG["business"]
        site = CFG["server"]["site_url"].rstrip("/")
        ctx = {"to": "", "who": "", "client_id": None, "inquiry_id": None, "back": "/admin/clients", "hint": "",
               "ids": {k: q[k] for k in self.CONTEXT_KEYS if (q.get(k) or "").strip()},
               "raw": {"deposit": b.get("deposit", ""), "deposit_link": b.get("deposit_link", "")}}
        v = {"photographer": b["photographer"], "business": b["name"], "business_email": b["email"],
             "business_phone": b.get("phone", ""), "site": site, "gallery_link": f"{site}/gallery"}
        with db() as c:
            if q.get("inquiry", "").isdigit():
                r = c.execute("SELECT * FROM inquiries WHERE id=?", (int(q["inquiry"]),)).fetchone()
                if r:
                    ctx.update(to=r["email"], who=r["name"], inquiry_id=r["id"], client_id=r["client_id"],
                               back=f"/admin/sessions#s{r['id']}")
                    v.update(name=r["name"], session_type=(r["session_type"] or "session").lower(),
                             location=r["session_location"] or r["location"], price=r["price"])
                    ctx["raw"].update({k: val for k, val in [
                        ("date", r["session_date"] or r["pref_date"]), ("time", r["session_time"] or r["pref_time"]),
                        ("location", r["session_location"] or r["location"]),
                        ("deposit", r["deposit"]), ("deposit_link", r["deposit_link"])] if val})
                    if r["dates"]:
                        ctx["hint"] = f"They asked for: {r['dates']}"
            if q.get("booking", "").isdigit():
                r = c.execute("""SELECT b.*, e.title, e.date, e.location, e.id AS eid FROM mini_bookings b
                                 JOIN mini_events e ON e.id=b.event_id WHERE b.id=?""", (int(q["booking"]),)).fetchone()
                if r:
                    ctx.update(to=r["email"], who=r["name"], client_id=r["client_id"], back=f"/admin/minis/{r['eid']}")
                    v.update(name=r["name"], session_type="mini session", date=nice_date(r["date"]),
                             time=nice_time(r["slot"]), location=r["location"])
            if q.get("gallery"):
                g = read_gallery(q["gallery"])
                if g:
                    ctx.update(client_id=ctx["client_id"] or g["client_id"], back=f"/admin/galleries/{g['slug']}")
                    v.update(gallery_code=g["code"], gallery_title=g["title"])
            cid = q.get("client", "")
            if cid.isdigit() or ctx["client_id"]:
                r = c.execute("SELECT * FROM clients WHERE id=?", (int(cid) if cid.isdigit() else ctx["client_id"],)).fetchone()
                if r:
                    ctx.update(client_id=r["id"], to=ctx["to"] or r["email"] or "", who=ctx["who"] or r["name"])
                    v.setdefault("name", r["name"])
                    if ctx["back"] == "/admin/clients":
                        ctx["back"] = f"/admin/clients/{r['id']}"
        name = v.get("name", "")
        v["first_name"] = name if name.lower().startswith("the ") else name.split(" ")[0] if name else ""
        # Saved session details, then anything Rachel typed on the email screen
        raw = ctx["raw"]
        raw.update({k: q[k].strip() for k in self.FILL_KEYS if (q.get(k) or "").strip()})
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw.get("date", "")):
            v["date"] = nice_date(raw["date"])
        if re.fullmatch(r"\d{1,2}:\d{2}", raw.get("time", "")):
            v["time"] = nice_time(raw["time"])
        for k, n in (("location", 200), ("deposit", 40), ("deposit_link", 500)):
            if raw.get(k):
                v[k] = raw[k][:n]
        v.setdefault("session_type", "session")
        ctx["values"] = v
        return ctx

    def email_templates(self):
        with db() as c:
            return c.execute("SELECT * FROM email_templates ORDER BY sort, name").fetchall()

    def admin_compose(self, q, error="", subject=None, body=None, to=None):
        ctx = self.email_context(q)
        tpls = self.email_templates()
        default = "confirm" if ctx["inquiry_id"] else "gallery" if q.get("gallery") else "message"
        tpl = next((t for t in tpls if t["key"] == q.get("template")), None) or \
            next(t for t in tpls if t["key"] == default)
        v = ctx["values"]
        subject = fill_template(tpl["subject"], v) if subject is None else subject
        body = fill_template(tpl["body"], v) if body is None else body
        to = ctx["to"] if to is None else to
        ids = "".join(f'<input type="hidden" name="{k}" value="{esc(val)}">' for k, val in ctx["ids"].items())
        uses = set(unfilled(tpl["subject"] + tpl["body"]))
        raw = ctx["raw"]
        fill_in = "".join(f'<input type="hidden" name="{k}" value="{esc(raw[k])}">' for k in self.FILL_KEYS
                          if raw.get(k) and k in uses)
        fields = ""
        if "booking" not in ctx["ids"]:
            for k, label, attrs in (("date", "Date", 'type="date"'), ("time", "Time", 'type="time"'),
                                    ("location", "Location", 'maxlength="200"'), ("deposit", "Deposit amount", 'maxlength="40"'),
                                    ("deposit_link", "Deposit link", 'type="url" maxlength="500"')):
                if k in uses:
                    wide = ' class="full"' if k in ("location", "deposit_link") else ""
                    fields += f'<label{wide}>{label}<input name="{k}" {attrs} value="{esc(raw.get(k, ""))}"></label>'
        opts = "".join(f'<option value="{t["key"]}"{" selected" if t["key"] == tpl["key"] else ""}>{esc(t["name"])}</option>'
                       for t in tpls)
        left = unfilled(subject + body)
        hint = f'<p class="muted small full">{esc(ctx["hint"])}</p>' if ctx["hint"] else ""
        if email_enabled():
            send = '<button class="btn">Send email</button>'
            setup = ""
        else:
            mailto = f"mailto:{quote(to)}?subject={quote(subject)}&amp;body={quote(body)}"
            send = f'<a class="btn" href="{mailto}">Open in my email app</a>'
            setup = ('<p class="notice warn">The website can\'t send email yet, so this opens your own email app instead. '
                     '<a href="/admin/emails">Turn on sending</a> to send straight from here.</p>')
        err = f'<p class="form-error" role="alert">{esc(error)}</p>' if error else ""
        title = f"Email {ctx['who']}" if ctx["who"] else "New email"
        body_html = f"""
  <div class="admin-head"><h1>{esc(title)}</h1></div>
  {setup}{err}
  <form method="get" action="/admin/email" class="form card-pad">
    {ids}
    <label class="full">Start from<select name="template" data-autosubmit>{opts}</select></label>
    {fields}{hint}
    <div class="full row-actions"><button class="btn ghost small">{'Fill in' if fields else 'Use this template'}</button></div>
  </form>
  <form method="post" action="/admin/email/send" class="form card-pad">
    {ids}{fill_in}<input type="hidden" name="template" value="{tpl['key']}">
    <label class="full">To<input name="to" type="email" required maxlength="200" value="{esc(to)}"></label>
    <label class="full">Subject<input name="subject" required maxlength="200" value="{esc(subject)}"></label>
    <label class="full">Message<textarea name="body" rows="14" required maxlength="20000">{esc(body)}</textarea></label>
    {f'<p class="form-error full">Still to fill in: {esc(", ".join("{" + w + "}" for w in left))}. Use the fields above or type over them.</p>' if left else ''}
    <div class="full row-actions">{send}<a class="btn ghost" href="{esc(ctx['back'])}">Cancel</a></div>
    <p class="muted small full">Replies go to {esc(CFG['business']['email'])}.</p>
  </form>"""
        self.admin_page(body_html, title, "emails", crumbs=[("Emails", "/admin/emails"), (title, None)])

    def admin_email_send(self, form):
        q = {k: form[k] for k in (*self.CONTEXT_KEYS, *self.FILL_KEYS, "template") if form.get(k)}
        to, subject, body = (form.get("to") or "").strip(), (form.get("subject") or "").strip(), form.get("body") or ""
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", to):
            return self.admin_compose(q, "Please enter one valid email address.", subject, body, to)
        if not subject or not body.strip():
            return self.admin_compose(q, "Please add a subject and a message.", subject, body, to)
        left = unfilled(subject + body)
        if left:
            return self.admin_compose(q, "Fill in " + ", ".join("{" + w + "}" for w in left) + " before sending.",
                                      subject, body, to)
        ctx = self.email_context(q)
        kind = next((t["name"] for t in self.email_templates() if t["key"] == q.get("template")), "Message")
        err = send_mail(subject[:200], body[:20000], to, CFG["business"]["email"],
                        log={"kind": kind, "client_id": ctx["client_id"], "inquiry_id": ctx["inquiry_id"]})
        if err:
            return self.admin_compose(q, f"The email wasn't sent: {err}", subject, body, to)
        done = "email-sent"
        if q.get("template") == "confirm" and ctx["inquiry_id"]:
            with DB_LOCK, db() as c:
                r = c.execute("SELECT * FROM inquiries WHERE id=?", (ctx["inquiry_id"],)).fetchone()
                cid = client_for_inquiry(c, r)
                c.execute("UPDATE inquiries SET status='booked' WHERE id=?", (r["id"],))
                raw = ctx["raw"]
                saved = {"session_date": raw.get("date") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw.get("date", "")) else None,
                         "session_time": raw.get("time") if re.fullmatch(r"\d{1,2}:\d{2}", raw.get("time", "")) else None,
                         "session_location": raw.get("location"), "deposit": raw.get("deposit"),
                         "deposit_link": raw.get("deposit_link")}
                for col, val in saved.items():
                    if val:
                        c.execute(f"UPDATE inquiries SET {col}=? WHERE id=?", (val[:500], r["id"]))
                c.execute("UPDATE clients SET status='active' WHERE id=? AND status='lead'", (cid,))
                c.execute("UPDATE email_log SET client_id=? WHERE inquiry_id=? AND client_id IS NULL", (cid, r["id"]))
            return self.redirect(f"/admin/sessions?status=booked&done=booking-confirmed#s{r['id']}")
        if ctx["inquiry_id"]:
            return self.redirect(f"/admin/sessions?status=all&done={done}#s{ctx['inquiry_id']}")
        back = f"/admin/clients/{ctx['client_id']}" if ctx["client_id"] else ctx["back"]
        self.redirect(f"{back}?done={done}")

    def email_history(self, rows):
        if not rows:
            return '<p class="muted">No emails yet.</p>'
        trs = "".join(f"""<tr>
  <td class="nowrap">{esc(r['created'])}</td><td>{esc(r['to_addr'])}</td>
  <td><details><summary>{esc(r['subject'])}</summary><pre class="email-body">{esc(r['body'])}</pre></details></td>
  <td>{esc(r['kind'])}</td>
  <td>{'<span class="tag ok">Sent</span>' if r['status'] == 'sent' else f'<span class="tag" title="{esc(r["error"])}">Not sent</span>'}</td>
</tr>""" for r in rows)
        return (f'<div class="table-wrap"><table class="data"><thead><tr><th>When</th><th>To</th><th>Subject</th>'
                f'<th>Type</th><th>Status</th></tr></thead><tbody>{trs}</tbody></table></div>')

    def admin_emails(self, edit, notice):
        e = CFG["email"]
        tpls = self.email_templates()
        with db() as c:
            log = c.execute("SELECT * FROM email_log ORDER BY id DESC LIMIT 50").fetchall()
        if email_enabled():
            setup = f"""
  <section class="card-pad">
    <h2>Sending is on</h2>
    <p>Emails go out from <strong>{esc(e['from_address'] or e['smtp_user'])}</strong>, and replies go to
       <strong>{esc(CFG['business']['email'])}</strong>.</p>
    <form method="post" action="/admin/emails/test"><button class="btn ghost small">Send me a test email</button></form>
  </section>"""
        else:
            setup = """
  <section class="card-pad">
    <h2>Turn on sending</h2>
    <p>Until this is set up, the Send button opens your own email app with the message ready to go.
       To send straight from the website with a Gmail account:</p>
    <ol>
      <li>Turn on 2-Step Verification for the Google account, then create an app password at
          <strong>myaccount.google.com/apppasswords</strong>.</li>
      <li>In <code>config.ini</code>, under <code>[email]</code>, set <code>enabled = true</code>,
          <code>smtp_host = smtp.gmail.com</code>, <code>smtp_port = 587</code>, <code>smtp_user</code> and
          <code>from_address</code> to the Gmail address, and <code>smtp_password</code> to the app password.</li>
      <li>Set <code>email</code> under <code>[business]</code> to the address replies should go to.</li>
      <li>Restart the website, then come back here and send a test email.</li>
    </ol>
  </section>"""
        editing = next((t for t in tpls if t["key"] == edit), None)
        editor = ""
        if editing:
            editor = f"""
  <form method="post" action="/admin/emails/template" class="form card-pad" id="edit">
    <input type="hidden" name="key" value="{editing['key']}">
    <h2 class="full">Edit “{esc(editing['name'])}”</h2>
    <label class="full">Subject<input name="subject" maxlength="200" value="{esc(editing['subject'])}"></label>
    <label class="full">Message<textarea name="body" rows="12" maxlength="20000">{esc(editing['body'])}</textarea></label>
    <p class="muted small full">Words in braces are filled in for you: {{first_name}}, {{name}}, {{session_type}},
       {{date}}, {{time}}, {{location}}, {{gallery_link}}, {{gallery_code}}, {{photographer}}, {{business}}.</p>
    <div class="full row-actions"><button class="btn">Save template</button><a class="btn ghost" href="/admin/emails">Cancel</a></div>
  </form>"""
        rows = "".join(f'<tr><td><strong>{esc(t["name"])}</strong></td><td>{esc(t["subject"]) or "<span class=muted>—</span>"}</td>'
                       f'<td class="num"><a href="/admin/emails?edit={t["key"]}#edit">Edit</a></td></tr>' for t in tpls)
        body = f"""
  <div class="admin-head"><h1>Emails</h1><a class="btn small" href="/admin/email">New email</a></div>
  {setup}
  {editor}
  <section>
    <div class="admin-head"><h2>Templates</h2></div>
    <p class="muted">Starting points for the emails you send from Sessions, Clients, Galleries and Mini sessions.</p>
    <div class="table-wrap"><table class="data"><thead><tr><th>Template</th><th>Subject</th><th></th></tr></thead>
    <tbody>{rows}</tbody></table></div>
  </section>
  <section class="mt-xl">
    <div class="admin-head"><h2>Recently sent</h2></div>
    {self.email_history(log)}
  </section>"""
        self.admin_page(body, "Emails", "emails", notice)

    def admin_template_save(self, form):
        key = form.get("key", "")
        with DB_LOCK, db() as c:
            c.execute("UPDATE email_templates SET subject=?, body=? WHERE key=?",
                      ((form.get("subject") or "").strip()[:200], (form.get("body") or "")[:20000], key))
        self.redirect("/admin/emails?done=template-saved")

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
        dash = '<span class="muted">—</span>'
        trs = "".join(f"""<tr>
  <td><a href="/admin/clients/{r['id']}"><strong>{esc(r['name'])}</strong></a></td>
  <td>{f'<a href="mailto:{esc(r["email"])}">{esc(r["email"])}</a>' if r['email'] else dash}</td>
  <td class="nowrap">{esc(r['phone']) or dash}</td>
  <td class="nowrap">{esc(party(r['adults'], r['kids'])) or dash}</td>
  <td>{esc(r['family']) or dash}</td>
  <td><span class="tag {'ok' if r['status'] == 'active' else ''}">{esc(r['status'])}</span></td>
  <td class="num">{gal_counts.get(r['id'], 0) or dash}</td>
</tr>""" for r in rows)
        table = (f'<div class="table-wrap"><table class="data"><thead><tr><th>Name</th><th>Email</th><th>Phone</th>'
                 f'<th>Family size</th><th>Family details</th><th>Status</th><th class="num">Galleries</th></tr></thead>'
                 f'<tbody>{trs}</tbody></table></div>'
                 if rows else '<p class="muted">No clients yet. Add one, or use “Add to clients” on a session.</p>')
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
        r, inquiries, galleries, minis = None, [], [], []
        if cid is not None:
            with db() as c:
                r = c.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
                if not r:
                    return self.not_found()
                inquiries = c.execute("""SELECT * FROM inquiries WHERE client_id=? OR
                                         (email!='' AND lower(email)=lower(?)) ORDER BY id DESC""",
                                      (cid, r["email"] or "")).fetchall()
                minis = c.execute("""SELECT b.slot, b.status, e.title, e.date, e.id AS eid FROM mini_bookings b
                                     JOIN mini_events e ON e.id=b.event_id WHERE b.client_id=? ORDER BY e.date DESC""",
                                  (cid,)).fetchall()
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
    {pickers(r["adults"] if r and r["adults"] is not None else 2, r["kids"] if r and r["kids"] is not None else 0, "Family size")}
    <label>Family details <span class="opt">(names, kids' ages, pets)</span><input name="family" value="{v('family')}" maxlength="300"></label>
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
                f'<li><a href="/admin/sessions?status=all#s{i["id"]}">{esc(i["session_type"])}</a> · '
                f'{esc(nice_date(i["session_date"])) if i["session_date"] else "requested " + esc(i["created"][:10])} '
                f'<span class="tag">{esc(i["status"])}</span>'
                f'{" <span class=tag>paid in full</span>" if i["paid_full"] else " <span class=tag>deposit paid</span>" if i["deposit_paid"] else ""}'
                f'{snippet(i["message"])}</li>'
                for i in inquiries) or '<li class="muted">No sessions yet.</li>'
            inq_rows += "".join(
                f'<li><a href="/admin/minis/{b["eid"]}">{esc(b["title"])}</a> · {esc(nice_date(b["date"]))}, '
                f'{nice_time(b["slot"])}{" <span class=tag>cancelled</span>" if b["status"] != "booked" else ""}</li>'
                for b in minis)
            extra = f"""
  <div class="admin-cols">
    <section><div class="admin-head"><h2>Galleries</h2>
      <a class="btn small" href="/admin/galleries/new?client={cid}">New gallery</a></div><ul class="plain">{gal_rows}</ul></section>
    <section><h2>Sessions &amp; bookings</h2><ul class="plain">{inq_rows}</ul></section>
  </div>
  <form method="post" action="/admin/clients/{cid}/delete" data-confirm="Delete {esc(r['name'])} from your client list? Their galleries are kept.">
    <button class="btn ghost small danger">Delete client</button></form>"""
        title = r["name"] if r else "New client"
        email_btn = f'<a class="btn small" href="/admin/email?client={cid}">Send email</a>' if r else ""
        if r:
            with db() as c:
                sent = c.execute("SELECT * FROM email_log WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()
            extra = extra.replace('<form method="post" action="/admin/clients/',
                                  f'<section class="mb"><div class="admin-head"><h2>Emails</h2></div>{self.email_history(sent)}</section>'
                                  '<form method="post" action="/admin/clients/', 1)
        body = f'<div class="admin-head"><h1>{esc(title)}</h1>{email_btn}</div>{form}{extra}'
        self.admin_page(body, title, "clients", notice, crumbs=[("Clients", "/admin/clients"), (title, None)])

    def admin_client_save(self, form):
        f = {k: (form.get(k) or "").strip() for k in ("id", "name", "email", "phone", "family", "notes", "status")}
        if not f["name"]:
            return self.redirect("/admin/clients/new")
        if f["status"] not in CLIENT_STATUSES:
            f["status"] = "active"
        vals = (f["name"][:120], f["email"][:200], f["phone"][:40], f["family"][:300], f["notes"][:8000], f["status"],
                *headcount(form))
        with DB_LOCK, db() as c:
            if f["id"].isdigit():
                cid = int(f["id"])
                c.execute("""UPDATE clients SET name=?, email=?, phone=?, family=?, notes=?, status=?, adults=?, kids=?
                             WHERE id=?""", (*vals, cid))
            else:
                cid = c.execute("""INSERT INTO clients (name, email, phone, family, notes, status, adults, kids, created)
                                   VALUES (?,?,?,?,?,?,?,?,?)""", (*vals, time.strftime("%Y-%m-%d"))).lastrowid
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

    def admin_gallery_form(self, slug, client_param, notice, session_param=""):
        g, session = None, None
        if not slug and session_param.isdigit():
            with db() as c:
                session = c.execute("SELECT * FROM inquiries WHERE id=?", (int(session_param),)).fetchone()
            if session and session["client_id"]:
                client_param = str(session["client_id"])
        if slug:
            g = read_gallery(slug)
            if not g:
                return self.not_found()
        names = self.client_names()
        current = g["client_id"] if g else (int(client_param) if client_param.isdigit() else None)
        copts = '<option value="">No client</option>' + "".join(
            f'<option value="{cid}"{" selected" if cid == current else ""}>{esc(n)}</option>' for cid, n in names.items())
        default_title = ""
        if session:
            when = time.strftime("%B %Y", time.strptime(session["session_date"], "%Y-%m-%d")) \
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", session["session_date"] or "") else time.strftime("%B %Y")
            default_title = f"{session['name']} · {session['session_type']} · {when}"
        elif not g and current in names:
            default_title = f"{names[current]} · {time.strftime('%B %Y')}"
        v = (lambda k, d="": esc(g[k]) if g else esc(d))
        form = f"""
  <form method="post" action="/admin/galleries/save" class="form card-pad">
    <input type="hidden" name="slug" value="{esc(slug or '')}">
    {f'<input type="hidden" name="session" value="{session["id"]}"><p class="muted full">For the {esc(session["session_type"])} with {esc(session["name"])}. Saving links this gallery to that session and their client record.</p>' if session else ''}
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
      <a class="btn small" href="/admin/email?gallery={g['slug']}&amp;template=gallery">Email the client</a>
      <a class="btn ghost small" href="{mailto}">Open in my email app</a></div>
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
        f = {k: (form.get(k) or "").strip() for k in ("slug", "title", "client_id", "code", "expires", "note", "session")}
        session = None
        if f["session"].isdigit() and not f["slug"]:
            with DB_LOCK, db() as c:
                session = c.execute("SELECT * FROM inquiries WHERE id=?", (int(f["session"]),)).fetchone()
                if session and not f["client_id"].isdigit():
                    f["client_id"] = str(client_for_inquiry(c, session))
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
        if session:
            with DB_LOCK, db() as c:
                c.execute("UPDATE inquiries SET gallery=? WHERE id=?", (slug, session["id"]))
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

    # ---- mini sessions (public)
    def minis_get(self, p):
        today = time.strftime("%Y-%m-%d")
        if p == "/minis":
            with db() as c:
                events = c.execute("""SELECT * FROM mini_events WHERE status='open' AND date>=?
                                      ORDER BY date, start_time""", (today,)).fetchall()
            cards = []
            for ev in events:
                slots = mini_slots(ev)
                left = len(slots) - len(mini_booked(ev["id"]))
                avail = (f'{left} of {len(slots)} spots open' if left > 0 else 'Fully booked')
                cards.append(f"""
      <a class="card mini-card" href="/minis/{esc(ev['slug'])}">
        <div class="card-body">
          <p class="eyebrow">{esc(nice_date(ev['date']))}</p>
          <h3>{esc(ev['title'])}</h3>
          <p class="price">{esc(ev['price'])}</p>
          <p>{esc(ev['location'])}<br><span class="muted">{nice_time(ev['start_time'])} to {nice_time(ev['end_time'])}</span></p>
          <span class="btn{' ghost' if left <= 0 else ''}">{'See times' if left > 0 else avail}</span>
          {f'<p class="muted small mt-s">{avail}</p>' if left > 0 else ''}
        </div>
      </a>""")
            listing = (f'<div class="cards">{"".join(cards)}</div>' if cards else
                       '<div class="aside-box"><h3>No mini sessions open right now</h3>'
                       '<p>New dates are announced on Instagram and by email. Want a heads-up? '
                       '<a href="/book?session=Mini+session">Join the list</a> and Rachel will let you know.</p></div>')
            body = f"""
<section class="section">
  <div class="wrap">
    <div class="section-head">
      <p class="eyebrow">Mini sessions</p>
      <h1>Pick your <em>mini session</em> time</h1>
      <p class="lede">Short, sweet sessions on set dates and locations. Choose an open time below and it's yours.</p>
    </div>
    {listing}
  </div>
</section>"""
            return self.page(body, "Mini Sessions",
                             "Book a family mini session with RBG Photography in Woodstock, Ellicott City and nearby Maryland towns.")
        m = re.fullmatch(r"/minis/([a-z0-9-]+)(/booked)?", p)
        ev = mini_event(slug=m.group(1)) if m else None
        if not ev or ev["status"] == "draft":
            return self.not_found()
        if m.group(2):
            return self.mini_confirmation(ev)
        if self.query().get("ics") == "1":
            return self.mini_client_ics(ev)
        self.mini_page(ev)

    def mini_page(self, ev, error="", form=None, status=200):
        form = form or {}
        booked = mini_booked(ev["id"])
        slots = mini_slots(ev)
        is_open = ev["status"] == "open" and ev["date"] >= time.strftime("%Y-%m-%d")
        chosen = form.get("slot", "")
        buttons = "".join(
            f'<label class="slot{" taken" if t in booked else ""}">'
            f'<input type="radio" name="slot" value="{t}" required{" disabled" if t in booked or not is_open else ""}'
            f'{" checked" if t == chosen and t not in booked else ""}>'
            f'<span>{nice_time(t)}</span>{"<small>Booked</small>" if t in booked else ""}</label>'
            for t in slots)
        left = len(slots) - len(booked)
        v = (lambda k: esc(form.get(k, "")))
        if not is_open:
            book = '<div class="aside-box"><h3>Booking is closed</h3><p>This mini session is no longer taking bookings.</p></div>'
        elif left <= 0:
            book = ('<div class="aside-box"><h3>All spots are taken</h3><p>Join the waitlist and Rachel will let you know '
                    'if a time opens up.</p><a class="btn" href="/book?session=Mini+session">Join the waitlist</a></div>')
        else:
            err = f'<p class="form-error full" role="alert">{esc(error)}</p>' if error else ""
            book = f"""
      <form class="form" method="post" action="/minis/{esc(ev['slug'])}/book">
        {err}
        <fieldset class="full slots"><legend>Choose a time <span class="opt">({left} open)</span></legend>{buttons}</fieldset>
        <label>Your name<input name="name" autocomplete="name" required maxlength="120" value="{v('name')}"></label>
        <label>Email<input name="email" type="email" autocomplete="email" required maxlength="200" value="{v('email')}"></label>
        <label>Phone <span class="opt">(optional)</span><input name="phone" type="tel" autocomplete="tel" maxlength="40" value="{v('phone')}"></label>
        {pickers(*headcount(form))}
        <label class="full">Anything Rachel should know? <span class="opt">(kids' ages, pets, optional)</span><textarea name="notes" maxlength="2000">{v('notes')}</textarea></label>
        <div class="hp" aria-hidden="true"><label>Leave this empty<input name="website" tabindex="-1" autocomplete="off"></label></div>
        <div class="full"><button class="btn" type="submit">Reserve my time</button></div>
      </form>"""
        details = esc(ev["details"]).replace("\n", "<br>") if ev["details"] else ""
        body = f"""
<section class="section">
  <div class="wrap">
    <p class="crumbs-public"><a href="/minis">All mini sessions</a></p>
    <div class="book-grid">
      <div>
        <p class="eyebrow">{esc(nice_date(ev['date']))}</p>
        <h1>{esc(ev['title'])}</h1>
        {book}
      </div>
      <aside class="aside-box">
        <h3>The details</h3>
        <p><strong>When:</strong> {esc(nice_date(ev['date']))}, {nice_time(ev['start_time'])} to {nice_time(ev['end_time'])}<br>
           <strong>Where:</strong> {esc(ev['location'])}<br>
           <strong>Length:</strong> {int(ev['slot_minutes'])} minutes<br>
           {f"<strong>Price:</strong> {esc(ev['price'])}" if ev['price'] else ''}</p>
        {f'<p>{details}</p>' if details else ''}
        <p class="muted small">Questions? Email <a href="mailto:{{{{email}}}}">{{{{email}}}}</a>.</p>
      </aside>
    </div>
  </div>
</section>"""
        self.page(body, ev["title"], f"Mini session on {nice_date(ev['date'])} at {ev['location']}.", status=status)

    def mini_book(self, slug):
        ev = mini_event(slug=slug)
        if not ev or ev["status"] != "open" or ev["date"] < time.strftime("%Y-%m-%d"):
            return self.not_found()
        form = self.read_form() or {}
        if form.get("website"):
            return self.redirect(f"/minis/{slug}")
        data = {k: (form.get(k) or "").strip()[:n] for k, n in
                {"slot": 5, "name": 120, "email": 200, "phone": 40, "notes": 2000}.items()}
        adults, kids = headcount(form)
        data["people"] = party(adults, kids)
        if data["slot"] not in mini_slots(ev):
            return self.mini_page(ev, "Please choose one of the open times.", form, 400)
        if not data["name"] or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", data["email"]):
            return self.mini_page(ev, "Please include your name and a valid email address.", form, 400)
        ip = self.client_ip()
        if not INQUIRY_LIMIT.allow(ip):
            return self.mini_page(ev, "We've received several requests from you already. Rachel will be in touch.", form, 429)
        ref = secrets.token_urlsafe(9)
        try:
            with DB_LOCK, db() as c:
                row = c.execute("SELECT id FROM clients WHERE email!='' AND lower(email)=lower(?)",
                                (data["email"],)).fetchone()
                if row:
                    cid = row["id"]
                    c.execute("UPDATE clients SET status='active' WHERE id=? AND status!='active'", (cid,))
                else:
                    cid = c.execute("""INSERT INTO clients (created, name, email, phone, family, notes, status,
                                       adults, kids) VALUES (?,?,?,?,'',?, 'active', ?, ?)""",
                                    (time.strftime("%Y-%m-%d"), data["name"], data["email"], data["phone"],
                                     f"Booked mini session: {ev['title']}", adults, kids)).lastrowid
                c.execute("""INSERT INTO mini_bookings (created, event_id, slot, name, email, phone, people, notes,
                             client_id, ref, ip, adults, kids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (time.strftime("%Y-%m-%d %H:%M"), ev["id"], data["slot"], data["name"], data["email"],
                           data["phone"], data["people"], data["notes"], cid, ref, ip, adults, kids))
        except sqlite3.IntegrityError:  # someone else just took that slot
            form["slot"] = ""
            return self.mini_page(ev, f"Sorry, {nice_time(data['slot'])} was just booked by someone else. Please pick another time.",
                                  form, 409)
        when = f"{nice_date(ev['date'])} at {nice_time(data['slot'])}"
        site = CFG["server"]["site_url"].rstrip("/")
        b = CFG["business"]
        threading.Thread(target=send_mail, daemon=True, args=(
            f"Mini session booked: {data['name']}, {when}",
            f"{data['name']} booked {ev['title']}.\n\nWhen: {when}\nWhere: {ev['location']}\n"
            f"Email: {data['email']}\nPhone: {data['phone']}\nWho's coming: {data['people']}\nNotes: {data['notes']}\n\n"
            f"Manage it at {site}/admin/minis/{ev['id']}",
            CFG["email"]["notify_address"] or b["email"], data["email"])).start()
        threading.Thread(target=send_mail, daemon=True, args=(
            f"You're booked: {ev['title']}",
            f"Hi {data['name'].split()[0]},\n\nYou're booked for {ev['title']}.\n\nWhen: {when}\n"
            f"Where: {ev['location']}\n\nPlease arrive a few minutes early. If you need to change your time, "
            f"just reply to this email.\n\nSee you soon!\n{b['photographer']}\n{b['name']}",
            data["email"], b["email"]), kwargs={"log": {"kind": "Mini session booked", "client_id": cid}}).start()
        cookie = f"mini_{ev['id']}={ref}; Path=/minis/{slug}; HttpOnly; SameSite=Lax; Max-Age={60 * 60 * 24 * 60}"
        self.redirect(f"/minis/{slug}/booked", {"Set-Cookie": cookie})

    def my_mini_booking(self, ev):
        ref = self.cookies().get(f"mini_{ev['id']}", "")
        if not ref:
            return None
        with db() as c:
            return c.execute("SELECT * FROM mini_bookings WHERE event_id=? AND ref=? AND status='booked'",
                             (ev["id"], ref)).fetchone()

    def mini_confirmation(self, ev):
        bk = self.my_mini_booking(ev)
        if not bk:
            return self.redirect(f"/minis/{ev['slug']}")
        body = f"""
<section class="narrow center">
  <p class="eyebrow">You're booked</p>
  <h1>See you {esc(nice_date(ev['date']).split(',')[0])}!</h1>
  <p class="lede">{esc(bk['name'])}, your {esc(ev['title'])} time is <strong>{nice_time(bk['slot'])}</strong> on
     {esc(nice_date(ev['date']))} at {esc(ev['location'])}.</p>
  <p>Rachel will be in touch with details and what to wear. Need to change something? Email
     <a href="mailto:{{{{email}}}}">{{{{email}}}}</a>.</p>
  <p class="actions centered"><a class="btn" href="/minis/{esc(ev['slug'])}?ics=1">Add to my calendar</a>
     <a class="btn ghost" href="/portfolio">Browse the portfolio</a></p>
</section>"""
        self.page(body, "You're booked", noindex=True)

    def mini_client_ics(self, ev):
        bk = self.my_mini_booking(ev)
        if not bk:
            return self.redirect(f"/minis/{ev['slug']}")
        b = CFG["business"]
        body = ics_calendar([{
            "uid": f"mini-{bk['id']}@rbg", "date": ev["date"], "start": bk["slot"],
            "end": end_of(bk["slot"], ev["slot_minutes"]), "summary": f"{ev['title']} with {b['name']}",
            "location": ev["location"], "description": f"Questions? {b['email']}"}], b["name"])
        self.send(200, body, "text/calendar; charset=utf-8",
                  {"Content-Disposition": 'attachment; filename="mini-session.ics"', "Cache-Control": "no-store"})

    def calendar_feed(self, token):
        """Every booked mini session slot, for Rachel to subscribe to in Google Calendar."""
        if not hmac.compare_digest(token, calendar_token()):
            return self.not_found()
        with db() as c:
            rows = c.execute("""SELECT b.*, e.title, e.date, e.location, e.slot_minutes, e.id AS eid
                                FROM mini_bookings b JOIN mini_events e ON e.id=b.event_id
                                WHERE b.status='booked' ORDER BY e.date, b.slot""").fetchall()
        site = CFG["server"]["site_url"].rstrip("/")
        events = [{"uid": f"mini-{r['id']}@rbg", "date": r["date"], "start": r["slot"],
                   "end": end_of(r["slot"], r["slot_minutes"]), "summary": f"Mini: {r['name']}",
                   "location": r["location"],
                   "description": (f"{r['title']}\nEmail: {r['email']}\nPhone: {r['phone']}\n"
                                   f"Who's coming: {party_of(r)}\nNotes: {r['notes']}\n{site}/admin/minis/{r['eid']}")}
                  for r in rows]
        self.send(200, ics_calendar(events, f"{CFG['business']['name']} mini sessions"),
                  "text/calendar; charset=utf-8", {"Cache-Control": "no-cache"})

    # ---- mini sessions (admin)
    def admin_minis(self, notice):
        with db() as c:
            events = c.execute("SELECT * FROM mini_events ORDER BY date DESC, start_time").fetchall()
        today = time.strftime("%Y-%m-%d")
        rows = []
        for ev in events:
            slots, booked = mini_slots(ev), mini_booked(ev["id"])
            state = "Past" if ev["date"] < today else ev["status"].title()
            cls = "ok" if state == "Open" else ""
            rows.append(f"""<tr>
  <td><a href="/admin/minis/{ev['id']}"><strong>{esc(ev['title'])}</strong></a><br><span class="muted">{esc(ev['location'])}</span></td>
  <td>{esc(nice_date(ev['date']))}<br><span class="muted">{nice_time(ev['start_time'])} to {nice_time(ev['end_time'])}</span></td>
  <td>{len(booked)} of {len(slots)}</td>
  <td><span class="tag {cls}">{state}</span></td>
</tr>""")
        table = (f'<div class="table-wrap"><table class="data"><thead><tr><th>Event</th><th>Date</th><th>Booked</th>'
                 f'<th>Status</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
                 if rows else '<p class="muted">No mini sessions yet. Create one to start taking bookings.</p>')
        site = CFG["server"]["site_url"].rstrip("/")
        feed = f"{site}/calendar/{calendar_token()}.ics"
        body = f"""
  <div class="admin-head"><h1>Mini sessions</h1><a class="btn small" href="/admin/minis/new">New mini session</a></div>
  <p class="muted">Open events are listed for families at <a href="/minis" target="_blank" rel="noopener">{esc(site)}/minis</a>.</p>
  {table}
  <section class="card-pad mt-xl">
    <h2>See bookings in Google Calendar</h2>
    <p>In Google Calendar choose <strong>Other calendars → + → From URL</strong> and paste this private link.
       New bookings then appear in your calendar automatically (Google checks every few hours).</p>
    <textarea readonly id="feed-url" rows="2">{esc(feed)}</textarea>
    <div class="row-actions"><button class="btn small" type="button" data-copy="feed-url">Copy link</button>
      <a class="btn ghost small" href="/calendar/{calendar_token()}.ics">Download calendar file</a></div>
    <p class="muted small mt">Google can only reach this link once the site is public on the internet. Until then,
       download the calendar file and import it in Google Calendar under <strong>Settings → Import</strong>.</p>
  </section>"""
        self.admin_page(body, "Mini sessions", "minis", notice)

    def admin_mini_form(self, event_id, notice):
        ev = None
        if event_id is not None:
            ev = mini_event(event_id)
            if not ev:
                return self.not_found()
        v = (lambda k, d="": esc(ev[k]) if ev else esc(d))
        opts = "".join(f'<option value="{s}"{" selected" if (ev["status"] if ev else "open") == s else ""}>'
                       f'{ {"draft": "Draft (hidden)", "open": "Open for booking", "closed": "Closed"}[s] }</option>'
                       for s in MINI_STATUSES)
        form = f"""
  <form method="post" action="/admin/minis/save" class="form card-pad">
    <input type="hidden" name="id" value="{ev['id'] if ev else ''}">
    <label class="full">Title<input name="title" required maxlength="120" value="{v('title', 'Fall Mini Sessions')}"></label>
    <label>Date<input name="date" type="date" required value="{v('date')}"></label>
    <label>Status<select name="status">{opts}</select></label>
    <label class="full">Location<input name="location" maxlength="200" value="{v('location')}" placeholder="Patapsco Valley State Park, Avalon area"></label>
    <label>First slot starts<input name="start_time" type="time" required value="{v('start_time', '09:00')}"></label>
    <label>Last slot ends by<input name="end_time" type="time" required value="{v('end_time', '12:00')}"></label>
    <label>Minutes per session<input name="slot_minutes" type="number" min="5" max="240" required value="{v('slot_minutes', '20')}"></label>
    <label>Break between sessions <span class="opt">(minutes)</span><input name="gap_minutes" type="number" min="0" max="120" value="{v('gap_minutes', '10')}"></label>
    <label>Price<input name="price" maxlength="60" value="{v('price', '$225')}"></label>
    <label class="full">Details for families <span class="opt">(what's included, deposit, what to wear)</span><textarea name="details" maxlength="3000">{v('details')}</textarea></label>
    <div class="full row-actions"><button class="btn">{'Save changes' if ev else 'Create mini session'}</button>
      <a class="btn ghost" href="/admin/minis">Back to mini sessions</a>
      {f'<a href="/minis/{esc(ev["slug"])}" target="_blank" rel="noopener">View booking page ↗</a>' if ev and ev['status'] != 'draft' else ''}</div>
  </form>"""
        extra = ""
        if ev:
            booked = mini_booked(ev["id"])
            trs = []
            for t in mini_slots(ev):
                bk = booked.get(t)
                if bk:
                    who = (f'<a href="/admin/clients/{bk["client_id"]}"><strong>{esc(bk["name"])}</strong></a>'
                           if bk["client_id"] else f'<strong>{esc(bk["name"])}</strong>')
                    action = (f'<form method="post" action="/admin/minis/{ev["id"]}/cancel" '
                              f'data-confirm="Cancel {esc(bk["name"])}\'s {nice_time(t)} booking? The time opens up again.">'
                              f'<input type="hidden" name="booking" value="{bk["id"]}"><button class="link-btn">Cancel</button></form>')
                    action = (f'<span class="row-actions"><a href="/admin/email?booking={bk["id"]}&amp;template=reminder">Email</a>'
                              f'{action}</span>')
                    trs.append(f"""<tr>
  <td class="nowrap"><strong>{nice_time(t)}</strong></td><td>{who}</td>
  <td class="nowrap">{esc(party_of(bk))}</td>
  <td><a href="mailto:{esc(bk['email'])}">{esc(bk['email'])}</a></td>
  <td class="nowrap">{esc(bk['phone'])}</td>
  <td>{esc(bk['notes'])}</td><td class="num">{action}</td></tr>""")
                else:
                    trs.append(f'<tr class="open-slot"><td class="nowrap"><strong>{nice_time(t)}</strong></td>'
                               f'<td colspan="6">Open</td></tr>')
            slots_n = len(mini_slots(ev))
            extra = f"""
  <section>
    <div class="admin-head"><h2>Schedule <span class="muted">({len(booked)} of {slots_n} booked)</span></h2></div>
    <div class="table-wrap"><table class="data"><thead><tr><th>Time</th><th>Family</th><th>Who's coming</th><th>Email</th><th>Phone</th><th>Notes</th><th></th></tr></thead>
    <tbody>{''.join(trs) or '<tr><td colspan="7" class="muted">No time slots. Check the start, end and length.</td></tr>'}</tbody></table></div>
  </section>
  <form method="post" action="/admin/minis/{ev['id']}/delete" class="mt-xl"
        data-confirm="Delete “{esc(ev['title'])}” and all its bookings? This can't be undone.">
    <button class="btn ghost small danger">Delete mini session</button></form>"""
        title = ev["title"] if ev else "New mini session"
        self.admin_page(f'<div class="admin-head"><h1>{esc(title)}</h1></div>{form}{extra}', title, "minis", notice,
                        crumbs=[("Mini sessions", "/admin/minis"), (title, None)])

    def admin_mini_save(self, form):
        f = {k: (form.get(k) or "").strip() for k in
             ("id", "title", "date", "status", "location", "start_time", "end_time", "slot_minutes",
              "gap_minutes", "price", "details")}
        ok = (f["title"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["date"])
              and re.fullmatch(r"\d{2}:\d{2}", f["start_time"]) and re.fullmatch(r"\d{2}:\d{2}", f["end_time"])
              and f["slot_minutes"].isdigit() and 5 <= int(f["slot_minutes"]) <= 240)
        back = f"/admin/minis/{f['id']}" if f["id"].isdigit() else "/admin/minis/new"
        if not ok:
            return self.redirect(back + "?done=mini-invalid")
        gap = int(f["gap_minutes"]) if f["gap_minutes"].isdigit() else 0
        status = f["status"] if f["status"] in MINI_STATUSES else "draft"
        vals = (f["title"][:120], f["date"], f["location"][:200], f["start_time"], f["end_time"],
                int(f["slot_minutes"]), min(gap, 120), f["price"][:60], f["details"][:3000], status)
        with DB_LOCK, db() as c:
            if f["id"].isdigit():
                eid = int(f["id"])
                c.execute("""UPDATE mini_events SET title=?, date=?, location=?, start_time=?, end_time=?,
                             slot_minutes=?, gap_minutes=?, price=?, details=?, status=? WHERE id=?""", (*vals, eid))
            else:
                base = slugify(f"{f['title']} {f['date']}")
                slug, n = base, 2
                while c.execute("SELECT 1 FROM mini_events WHERE slug=?", (slug,)).fetchone():
                    slug, n = f"{base}-{n}", n + 1
                eid = c.execute("""INSERT INTO mini_events (title, date, location, start_time, end_time, slot_minutes,
                                   gap_minutes, price, details, status, created, slug) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (*vals, time.strftime("%Y-%m-%d"), slug)).lastrowid
        self.redirect(f"/admin/minis/{eid}?done=mini-saved")

    def admin_mini_cancel(self, event_id, form):
        bid = form.get("booking", "")
        if bid.isdigit():
            with DB_LOCK, db() as c:
                c.execute("UPDATE mini_bookings SET status='cancelled' WHERE id=? AND event_id=?", (int(bid), event_id))
        self.redirect(f"/admin/minis/{event_id}?done=booking-cancelled")

    def admin_mini_delete(self, event_id):
        with DB_LOCK, db() as c:
            c.execute("DELETE FROM mini_bookings WHERE event_id=?", (event_id,))
            c.execute("DELETE FROM mini_events WHERE id=?", (event_id,))
        self.redirect("/admin/minis?done=mini-deleted")

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
