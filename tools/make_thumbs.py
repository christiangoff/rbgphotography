#!/usr/bin/env python3
"""Make fast-loading thumbnails for a client gallery (optional).

Galleries work without this, but on a Pi with home internet, thumbnails make
big galleries open much faster. Full-size photos are still what clients download.

    sudo apt install python3-pil          # once
    python3 tools/make_thumbs.py galleries/smith-family
"""
import sys
from pathlib import Path
from PIL import Image, ImageOps

SIZE = 1000  # longest side, in pixels

for folder in map(Path, sys.argv[1:] or ["galleries"]):
    folders = [folder] if (folder / "gallery.ini").exists() else [p for p in folder.iterdir() if (p / "gallery.ini").exists()]
    for g in folders:
        out = g / "thumbs"
        out.mkdir(exist_ok=True)
        for photo in sorted(g.iterdir()):
            if photo.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            dest = out / photo.name
            if dest.exists() and dest.stat().st_mtime >= photo.stat().st_mtime:
                continue
            with Image.open(photo) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((SIZE, SIZE))
                if photo.suffix.lower() in {".jpg", ".jpeg"}:
                    im.convert("RGB").save(dest, quality=82, optimize=True, progressive=True)
                else:
                    im.save(dest)
            print("thumb", dest)
