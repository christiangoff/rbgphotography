# RBG Photography website (Raspberry Pi edition)

A lightweight website for RBG Photography that runs on a Raspberry Pi. It uses
the Viewfinder brand kit (logo, forest/terracotta/sage/cream colors, Cormorant
Garamond + Jost fonts) and needs nothing but the Python 3 that ships with
Raspberry Pi OS. No database server, no npm, no pip.

**What it does**

| Page | What's there |
|---|---|
| `/` | Home: hero, intro, sessions, recent work, reviews, locations, how it works |
| `/sessions` | Session types, prices, FAQ |
| `/portfolio` | Photo grid with a tap-to-enlarge viewer |
| `/locations` + 3 location pages | Patapsco Valley, Ellicott City, Sykesville (written for local Google searches) |
| `/about` | Rachel's bio |
| `/book` | Booking request form (saved on the Pi; optional email alert) |
| `/gallery` | Private client galleries opened with a code; view, download one or all |
| `/admin` | Rachel's dashboard (password protected): booking requests, client list, galleries, site photos |

Also built in: Google-friendly titles and descriptions, a `LocalBusiness`
listing for search engines, `sitemap.xml`, `robots.txt`, social share previews,
favicons, spam protection on the form, and security headers.

---

## 1. Put it on the Pi

Works on any Raspberry Pi running Raspberry Pi OS (Bookworm or newer); a Pi 3B+
or better is plenty.

Copy this `website` folder to the Pi as `~/rbg-website`. Any of these work:

- **USB stick:** copy the folder over, then on the Pi: `cp -r /media/$USER/<stick>/website ~/rbg-website`
- **From another computer:** `scp -r website pi@raspberrypi.local:~/rbg-website`
- **Git (easiest to keep updated):** `git clone https://github.com/christiangoff/rbgphotography ~/rbg-website`, and later `cd ~/rbg-website && git pull && sudo systemctl restart rbg-website` to get updates

## 2. Install and start it

On the Pi, open a terminal:

```bash
cd ~/rbg-website
bash install.sh
```

That creates `config.ini`, and sets the site to start automatically whenever the
Pi boots. It prints the address to open, usually
**http://raspberrypi.local:8080** from any phone or computer on the same Wi-Fi.

Just want to try it without installing? `python3 server.py` then open
http://localhost:8080 (Ctrl+C stops it).

## 3. Fill in the settings

```bash
nano ~/rbg-website/config.ini
sudo systemctl restart rbg-website
```

Set at least:

- `[business]` email, phone, Instagram/Facebook links (these appear across the whole site)
- `[admin] password` to switch on `/admin` (username `rachel`); add more admins under `[admin_users]` as `name = password` (usernames are not case sensitive)
- `[server] site_url` to the real address once the site is public
- `[email]` (optional) so the site can send email: alerts for each booking
  request, mini session confirmations, and the emails you send from the admin.
  With Gmail: turn on 2-Step Verification, create an
  [app password](https://myaccount.google.com/apppasswords), then set
  `enabled = true`, `smtp.gmail.com`, port 587, your Gmail address as
  `smtp_user` and `from_address`, and the app password as `smtp_password`.
  Replies go to the `[business]` email. Restart, then use **Emails → Send me a
  test email** in the admin.

## 4. The admin dashboard

Open **/admin** (username `rachel`, or any login under `[admin_users]`, with the password from `config.ini`). Everything
below works from a phone or laptop on the same network. A sidebar on the left
(a Menu button on phones) takes you between sections, and the **Dashboard**
shows new requests, upcoming sessions, recent galleries and shortcuts for common jobs.

- **Sessions:** every request from the Book page, then the session itself.
  Move each one through New → Contacted → Booked → Completed → Archived. Open
  **Session details** to save the date, time, location, price, deposit and the
  payment link, then tick **Mark deposit paid** and **Mark paid in
  full** as money comes in (the date is recorded; Undo if you clicked by
  mistake). **Confirm booking** emails the family with the date, place and
  payment link filled in and marks the session Booked. **Request payment**
  emails the remaining balance (price less a paid deposit) with the same link. **Create gallery** starts
  a gallery already named and linked to that family. Set a usual deposit amount
  and payment link under `[business]` in `config.ini` (`deposit`,
  `payment_link`) to have them filled in automatically.
- **Mini sessions:** set up a mini session day (date, place, start and end
  time, minutes per family, break between families, price, details) and set it
  to **Open**. Families pick a free time at `/minis` and it's theirs; nobody can
  book the same slot twice. Each booking is added to the client list, shows on
  the event's schedule (where you can cancel it to free the time), and is
  emailed to you if email is set up. Families get a confirmation page with an
  "Add to my calendar" button. Collect deposits as you do today (Venmo, invoice).
  **Google Calendar:** the Mini sessions page has a private calendar link. In
  Google Calendar choose Other calendars → + → From URL and paste it, and
  bookings appear in your calendar automatically. Google can only reach that
  link once the site is public; until then use "Download calendar file" and
  import it.
- **Clients:** your client list with search, a status (lead, active, past),
  family details and private notes. Each client page shows their sessions and
  galleries and has a **New gallery** button.
- **Galleries:** create a gallery, link it to a client, set its access code
  (one is suggested for you), an optional note and an expiry date. Drag photos
  onto the page to upload them. The page gives you a ready-to-send message with
  the link and code. Clients open galleries at `/gallery`.
- **Emails:** send a client an email from the site. **Confirm booking** and
  **Email** on each session, **Send email** on each client, **Email** next to
  each mini session booking and **Email the client** on each gallery open a
  ready-written message (booking confirmation, reply, session reminder, gallery
  ready, thank you) with their name, session, date, place or gallery code
  filled in. Edit it, then press Send. Sending a booking confirmation also marks
  the session Booked and adds them to your clients. Edit the starting text under
  **Emails → Templates**; everything sent is listed there and on each client's
  page. Until `[email]` is set up, Send opens your own email app instead.
- **Site photos:** replace any photo on the site (hero, session cards, location
  pages, Rachel's portrait) and it updates everywhere at once. Add, remove and
  reorder portfolio photos; the first four also appear on the home page.

Nothing is erased: deleted galleries and photos, and the old version of any
replaced photo, are moved to `data/trash/` on the Pi. Empty it now and then.

For faster gallery previews, install Pillow once on the Pi
(`sudo apt install -y python3-pil`) and restart; new uploads then get small
preview copies automatically.

## 5. Replace the placeholders (before launch)

Placeholder images are soft brand-colored gradients labeled with their file
name, so it's obvious what goes where.

- [ ] **Photos:** use **Site photos** in the admin to replace each one. Export
      JPEGs about 2000 px on the long side (hero) or 1500 px (everything else)
      so pages load fast. `og-image.jpg` (1200×630) is the preview shown when
      the site is shared on Facebook or iMessage.
- [ ] **Reviews:** the three quotes on the home page are marked "Sample". Swap in
      real client reviews (with permission) in `pages/index.html`.
- [ ] **Prices:** sample prices in `pages/sessions.html` and the home page cards.
- [ ] **Bio:** `pages/about.html` has a draft bio and a `[bracketed]` line to finish.
- [ ] **Delete the sample gallery** in **Galleries** (its code is `sample-2026`).

Page text is plain HTML in `pages/`. Edit the words between the tags; the
header, menu and footer come from `templates/layout.html`, and colors and fonts
from `static/css/site.css`. Changes show up on refresh, no restart needed. To
add a page, create `pages/new-page.html` (copy the comment block at the top for
its title and description) and it appears at `/new-page`.

## 6. Backups

Booking requests and the client list live in `data/inquiries.db`; galleries
live in `galleries/`. Photos live on the Pi's SD card, so keep originals
backed up elsewhere too (see Everyday commands below).

## 7. Making it public (when you're ready)

Out of the box the site is only reachable on your home network. To put it on
the internet you'll need a domain name and one of:

- **Cloudflare Tunnel (recommended for home hosting):** free, no router port
  forwarding, hides your home IP, and gives HTTPS automatically. Install
  `cloudflared` on the Pi and point a tunnel at `http://localhost:8080`.
- **Port forwarding + Caddy:** forward ports 80 and 443 on your router to the Pi,
  install Caddy (`sudo apt install caddy`) and use `deploy/Caddyfile`. Caddy
  gets an HTTPS certificate automatically. Many home internet plans change your
  IP address, so you'd also need dynamic DNS.

Either way, then set in `config.ini`: `site_url = https://yourdomain.com` and
`trust_proxy = true`, and restart. Use HTTPS before giving out gallery codes or
the admin password over the internet.

A Pi on home internet is fine for a small local business site. Upload speed is
the usual limit: large gallery downloads may be slow on a basic home plan.

## Everyday commands

```bash
sudo systemctl status rbg-website      # is it running?
sudo systemctl restart rbg-website     # after editing config.ini
journalctl -u rbg-website -f           # live log (Ctrl+C to exit)
```

**Back up** `config.ini`, `data/` (booking requests) and `galleries/` regularly,
for example to a USB stick: `cp -r config.ini data galleries /media/$USER/<stick>/`.

## What's in the folder

```
server.py               the whole web server (Python standard library only)
config.example.ini      settings template (install.sh copies it to config.ini)
install.sh              one-time Pi setup + start-on-boot service
pages/                  page content (HTML)
templates/layout.html   header, menu, footer shared by every page
static/css, js, fonts   styling, small scripts, self-hosted brand fonts
static/img/brand/       logos and icons from the Viewfinder kit
static/img/photos/      site photos (placeholders to replace)
galleries/              private client galleries (one folder each)
data/                   created on first run: booking requests, secret key
deploy/                 service file and optional Caddy config
tools/                  thumbnail maker; placeholder generator
```
