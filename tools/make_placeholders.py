"""Generate brand-colored placeholder photos (needs Pillow).

Run once to recreate the placeholders. Real photos simply replace these files
using the same names, so you never need to run this on the Pi.
"""
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
PHOTOS = ROOT / "static" / "img" / "photos"
GALLERY = ROOT / "galleries" / "sample-family"

TONES = [  # (top-left, bottom-right) pairs drawn from / near the brand palette
    ((127, 143, 119), (51, 64, 58)),
    ((214, 170, 140), (194, 123, 87)),
    ((232, 222, 205), (127, 143, 119)),
    ((194, 123, 87), (51, 64, 58)),
    ((200, 208, 190), (90, 104, 92)),
    ((240, 226, 210), (194, 150, 120)),
]


def make(path, w, h, seed):
    rnd = random.Random(seed)
    a, b = TONES[seed % len(TONES)]
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(0, w):
            t = (x / w * 0.45 + y / h * 0.55)
            px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    for _ in range(14):
        r = rnd.randint(min(w, h) // 20, min(w, h) // 6)
        cx, cy = rnd.randint(0, w), rnd.randint(0, int(h * 0.7))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 246, 230, rnd.randint(25, 70)))
    glow = glow.filter(ImageFilter.GaussianBlur(min(w, h) // 40))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img)
    # viewfinder corners, echoing the logo
    m, L, s = int(min(w, h) * 0.08), int(min(w, h) * 0.09), max(2, min(w, h) // 220)
    c = (248, 242, 233)
    for (x, y, dx, dy) in [(m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)]:
        d.line([(x, y), (x + dx * L, y)], fill=c, width=s)
        d.line([(x, y), (x, y + dy * L)], fill=c, width=s)
    label = f"placeholder  ·  {path.name}"
    try:
        font = ImageFont.load_default(size=max(14, min(w, h) // 30))
    except TypeError:
        font = ImageFont.load_default()
    tw = d.textlength(label, font=font)
    d.text(((w - tw) / 2, h - m - L / 2), label, fill=c, font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=78, optimize=True, progressive=True)


PLAN = {
    "hero.jpg": (2000, 1250), "og-image.jpg": (1200, 630), "about-rachel.jpg": (900, 1125),
    "session-mini.jpg": (1200, 900), "session-family.jpg": (1200, 900),
    "session-extended.jpg": (1200, 900), "session-newborn.jpg": (1200, 900),
    "location-patapsco.jpg": (1600, 1000), "location-ellicott-city.jpg": (1600, 1000),
    "location-sykesville.jpg": (1600, 1000),
}
for i in range(1, 13):
    PLAN[f"portfolio-{i:02d}.jpg"] = (1200, 1500) if i % 3 == 1 else (1500, 1000)

if __name__ == "__main__":
    for n, (name, (w, h)) in enumerate(PLAN.items()):
        make(PHOTOS / name, w, h, n)
    for i in range(1, 7):
        make(GALLERY / f"family-{i:02d}.jpg", 1500 if i % 2 else 1000, 1000 if i % 2 else 1500, i + 3)
    print("placeholders written")
