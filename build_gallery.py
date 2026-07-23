#!/usr/bin/env python3
"""
Spracuje fotky/videá z norsko/fotky/incoming/ podľa EXIF dátumu a GPS,
zaradí ich k dňu výpravy (trip_data.json), zmenší na web a vygeneruje
fotky/manifest.json, ktorý appka číta a zobrazuje v galérii pri danom dni.

Spustenie: python3 build_gallery.py
Vstup:  ../fotky/incoming/*.{jpg,jpeg,heic,png,mp4,mov}
Výstup: ../fotky/day-N/*.jpg (thumb + full) + ../fotky/manifest.json
Spracované originály sa presunú do ../fotky/done/.
"""
import json, math, os, shutil, sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ExifTags
import pillow_heif
pillow_heif.register_heif_opener()

APP_DIR = Path(__file__).resolve().parent
TRIP_ROOT = APP_DIR.parent
INCOMING = TRIP_ROOT / "fotky" / "incoming"
DONE = TRIP_ROOT / "fotky" / "done"
OUT_ROOT = APP_DIR / "fotky"
MANIFEST_PATH = OUT_ROOT / "manifest.json"

FULL_MAX = 1600
THUMB_MAX = 420
JPEG_QUALITY = 78

with open(APP_DIR / "trip_data.json", encoding="utf-8") as f:
    TRIP = json.load(f)

DAYS_BY_ISO = {d["iso"]: d for d in TRIP["days"]}
WPS = TRIP["waypoints"]


def exif_datetime_and_gps(img):
    """Vráti (datetime alebo None, (lat, lon) alebo None) z EXIF."""
    try:
        exif = img.getexif()
    except Exception:
        return None, None
    if not exif:
        return None, None

    tagmap = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
    exif_ifd = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else None  # Exif sub-IFD
    if exif_ifd:
        tagmap.update({ExifTags.TAGS.get(k, k): v for k, v in exif_ifd.items()})

    dt = None
    for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        raw = tagmap.get(key)
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode(errors="ignore")
            raw = raw.strip("\x00").strip()
            try:
                dt = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
                break
            except Exception:
                pass

    gps_ifd = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else None
    latlon = None
    if gps_ifd:
        def to_deg(val):
            d, m, s = val
            return d + m / 60 + s / 3600
        try:
            lat = to_deg(gps_ifd[2])
            if gps_ifd[1] == "S":
                lat = -lat
            lon = to_deg(gps_ifd[4])
            if gps_ifd[3] == "W":
                lon = -lon
            latlon = (lat, lon)
        except Exception:
            pass
    return dt, latlon


def nearest_waypoint(iso, lat, lon):
    candidates = [w for w in WPS if w["iso"] == iso]
    if not candidates or lat is None:
        return None
    best, best_d = None, 1e9
    for w in candidates:
        d = (w["la"] - lat) ** 2 + (w["lo"] - lon) ** 2
        if d < best_d:
            best, best_d = w, d
    return best["nm"] if best else None


def resize_save(img, path, max_dim):
    img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True)


def load_manifest():
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(m):
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)


IMG_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}


def main():
    if not INCOMING.exists():
        print(f"Chýba priečinok: {INCOMING}")
        return
    files = sorted(p for p in INCOMING.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        print("Nič nové na spracovanie.")
        return

    manifest = load_manifest()
    DONE.mkdir(parents=True, exist_ok=True)
    processed, skipped = 0, 0

    for path in files:
        ext = path.suffix.lower()
        try:
            if ext in IMG_EXT:
                img = Image.open(path)
                dt, latlon = exif_datetime_and_gps(img)
                if dt is None:
                    dt = datetime.fromtimestamp(path.stat().st_mtime)
                iso = dt.strftime("%Y-%m-%d")
                day = DAYS_BY_ISO.get(iso)
                if not day:
                    print(f"  ⚠ {path.name}: dátum {iso} nesedí so žiadnym dňom výpravy — preskočené")
                    skipped += 1
                    continue
                n = day["n"]
                stem = f"{dt.strftime('%H%M%S')}_{path.stem}"
                full_rel = f"day-{n}/{stem}_full.jpg"
                thumb_rel = f"day-{n}/{stem}_thumb.jpg"
                resize_save(img, OUT_ROOT / full_rel, FULL_MAX)
                resize_save(img, OUT_ROOT / thumb_rel, THUMB_MAX)
                caption = None
                if latlon:
                    caption = nearest_waypoint(iso, *latlon)
                manifest.setdefault(str(n), []).append({
                    "type": "photo",
                    "full": full_rel,
                    "thumb": thumb_rel,
                    "time": dt.strftime("%H:%M"),
                    "caption": caption,
                })
                processed += 1
                print(f"  ✓ {path.name} → deň {n} ({day['title']}) {dt.strftime('%H:%M')}")

            elif ext in VIDEO_EXT:
                # Video: EXIF cez PIL nejde -> podľa mtime súboru (menej presné, ale funkčné)
                dt = datetime.fromtimestamp(path.stat().st_mtime)
                iso = dt.strftime("%Y-%m-%d")
                day = DAYS_BY_ISO.get(iso)
                if not day:
                    print(f"  ⚠ {path.name}: dátum {iso} nesedí so žiadnym dňom výpravy — preskočené")
                    skipped += 1
                    continue
                n = day["n"]
                out_dir = OUT_ROOT / f"day-{n}"
                out_dir.mkdir(parents=True, exist_ok=True)
                dest = out_dir / path.name
                shutil.copy2(path, dest)
                manifest.setdefault(str(n), []).append({
                    "type": "video",
                    "full": f"day-{n}/{path.name}",
                    "thumb": None,
                    "time": dt.strftime("%H:%M"),
                    "caption": None,
                })
                processed += 1
                print(f"  ✓ {path.name} (video) → deň {n} ({day['title']})")

            else:
                print(f"  – {path.name}: neznámy formát, preskočené")
                skipped += 1
                continue

            shutil.move(str(path), str(DONE / path.name))

        except Exception as e:
            print(f"  ✗ CHYBA pri {path.name}: {e}")
            skipped += 1

    for n in manifest:
        manifest[n].sort(key=lambda x: x["time"])

    save_manifest(manifest)
    print(f"\nHotovo: {processed} spracovaných, {skipped} preskočených.")
    print(f"Manifest: {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
