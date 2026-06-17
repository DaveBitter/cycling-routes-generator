#!/usr/bin/env python3
"""
Weekly Cycling Route Generator
Generates 7 GPX files (20-80km) from Cremerstraat, Haarlem every Monday.
Emails them with inline map previews to daveybitter@gmail.com.

Config: cycling routes/config.json
"""

import os
import json
import math
import base64
import requests
import io
import sys
from datetime import datetime

try:
    from PIL import Image, ImageDraw, ImageFont
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    IMAGING = True
except ImportError:
    IMAGING = False
    print("Warning: PIL/matplotlib not available, skipping map images")

# ─── Config ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')
OUTPUT_DIR = SCRIPT_DIR  # save GPX files alongside the script in the workspace

DISTANCES_KM = [20, 30, 40, 50, 60, 70, 80]

# Fallback coordinates for Cremerstraat, Haarlem
DEFAULT_LAT = 52.3862
DEFAULT_LON = 4.6289


def load_config():
    """Load config from environment variables (GitHub Actions) or config.json (local)."""
    cfg = {}

    # Try environment variables first (GitHub Actions secrets)
    if os.environ.get('ORS_API_KEY'):
        cfg['ors_api_key'] = os.environ['ORS_API_KEY']
        cfg['resend_api_key'] = os.environ['RESEND_API_KEY']
        cfg['resend_from'] = os.environ.get('RESEND_FROM', 'onboarding@resend.dev')
        cfg['email_to'] = os.environ.get('EMAIL_TO', 'daveybitter@gmail.com')
        cfg['start_address'] = os.environ.get('START_ADDRESS', 'Cremerstraat, Haarlem, Netherlands')
        return cfg

    # Fall back to config.json for local use
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ─── Routing ─────────────────────────────────────────────────────────────────

def geocode_address(address, api_key):
    """Geocode an address via ORS."""
    url = "https://api.openrouteservice.org/geocode/search"
    r = requests.get(url, headers={"Authorization": api_key},
                     params={"text": address, "size": 1, "boundary.country": "NL"},
                     timeout=15)
    r.raise_for_status()
    coords = r.json()['features'][0]['geometry']['coordinates']
    return coords[1], coords[0]  # lat, lon


def generate_route(start_lat, start_lon, distance_km, seed, api_key):
    """Generate a cycling round-trip via ORS."""
    url = "https://api.openrouteservice.org/v2/directions/cycling-regular/geojson"
    body = {
        "coordinates": [[start_lon, start_lat]],
        "options": {
            "round_trip": {
                "length": distance_km * 1000,
                "points": max(3, distance_km // 12),
                "seed": seed
            },
            "avoid_features": ["ferries", "steps"],
        },
        "preference": "recommended",
    }
    r = requests.post(url,
                      headers={"Authorization": api_key, "Content-Type": "application/json"},
                      json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def geojson_to_gpx(geojson, distance_km, date_str):
    """Convert ORS GeoJSON to a minimal GPX string."""
    coords = geojson['features'][0]['geometry']['coordinates']
    summary = geojson['features'][0]['properties'].get('summary', {})
    actual_km = round(summary.get('distance', distance_km * 1000) / 1000, 1)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="WeeklyCyclingRoutes"',
        '  xmlns="http://www.topografix.com/GPX/1/1">',
        '  <metadata>',
        f'    <name>{distance_km}km Route – {date_str}</name>',
        f'    <desc>Round trip from Cremerstraat, Haarlem. Actual distance: {actual_km}km</desc>',
        f'    <time>{datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}</time>',
        '  </metadata>',
        '  <trk>',
        f'    <name>{distance_km}km Cycling Route</name>',
        '    <trkseg>',
    ]
    for c in coords:
        lon, lat = c[0], c[1]
        ele = c[2] if len(c) > 2 else 0
        lines.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>{ele:.1f}</ele></trkpt>')
    lines += ['    </trkseg>', '  </trk>', '</gpx>']
    return '\n'.join(lines)


# ─── Map rendering ───────────────────────────────────────────────────────────

def _tile_coords(lat, lon, zoom):
    """Convert lat/lon to OSM tile x/y."""
    lat_r = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _tile_to_latlon(x, y, zoom):
    """Top-left lat/lon of a tile."""
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat_r = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    lat = math.degrees(lat_r)
    return lat, lon


def fetch_tile(x, y, zoom, session):
    """Fetch one OSM tile as a PIL Image (or None on failure)."""
    url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
    try:
        r = session.get(url, timeout=10,
                        headers={"User-Agent": "WeeklyCyclingRoutes/1.0 daveybitter@gmail.com"})
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        pass
    return None


def render_route_image(geojson, distance_km):
    """Render the route on an OSM tile background. Returns PNG bytes or None."""
    if not IMAGING:
        return None

    coords = geojson['features'][0]['geometry']['coordinates']
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]

    # Padding around route
    pad = 0.02
    min_lat, max_lat = min(lats) - pad, max(lats) + pad
    min_lon, max_lon = min(lons) - pad, max(lons) + pad

    # Pick zoom level so the route fills the image nicely
    for zoom in range(13, 9, -1):
        tx0, ty0 = _tile_coords(max_lat, min_lon, zoom)
        tx1, ty1 = _tile_coords(min_lat, max_lon, zoom)
        num_tiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
        if num_tiles <= 16:
            break

    TILE_SIZE = 256
    session = requests.Session()

    # Assemble tile mosaic
    canvas_w = (tx1 - tx0 + 1) * TILE_SIZE
    canvas_h = (ty1 - ty0 + 1) * TILE_SIZE
    canvas = Image.new("RGB", (canvas_w, canvas_h), (220, 220, 220))

    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            tile = fetch_tile(tx, ty, zoom, session)
            if tile:
                px = (tx - tx0) * TILE_SIZE
                py = (ty - ty0) * TILE_SIZE
                canvas.paste(tile, (px, py))

    # Helper: convert lat/lon → pixel on canvas
    top_lat, left_lon = _tile_to_latlon(tx0, ty0, zoom)
    bot_lat, right_lon = _tile_to_latlon(tx1 + 1, ty1 + 1, zoom)

    def to_pixel(lat, lon):
        x = int((lon - left_lon) / (right_lon - left_lon) * canvas_w)
        y = int((top_lat - lat) / (top_lat - bot_lat) * canvas_h)
        return x, y

    # Draw route polyline
    draw = ImageDraw.Draw(canvas)
    pixels = [to_pixel(c[1], c[0]) for c in coords]

    # Shadow for visibility
    for i in range(len(pixels) - 1):
        draw.line([pixels[i], pixels[i + 1]], fill=(0, 0, 0, 120), width=6)
    for i in range(len(pixels) - 1):
        draw.line([pixels[i], pixels[i + 1]], fill=(220, 50, 50), width=3)

    # Start/end dot
    sx, sy = pixels[0]
    r = 8
    draw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(255, 255, 255), outline=(220, 50, 50), width=3)

    # Label
    label = f"{distance_km} km"
    draw.rectangle([8, 8, 90, 30], fill=(255, 255, 255, 200))
    draw.text((12, 10), label, fill=(40, 40, 40))

    # Crop to a nice 800×560 around the route center
    cx, cy = to_pixel((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
    W, H = 800, 560
    x0 = max(0, min(cx - W // 2, canvas_w - W))
    y0 = max(0, min(cy - H // 2, canvas_h - H))
    x1 = min(canvas_w, x0 + W)
    y1 = min(canvas_h, y0 + H)
    final = canvas.crop((x0, y0, x1, y1))

    buf = io.BytesIO()
    final.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return buf.getvalue()


# ─── Email ────────────────────────────────────────────────────────────────────

EMAIL_HTML_HEADER = """\
<html><body style="font-family:sans-serif;max-width:700px;margin:0 auto;padding:20px;">
<h2 style="color:#E74C3C;">🚴 Weekly Cycling Routes – {date}</h2>
<p>Starting from <strong>Cremerstraat, Haarlem</strong>. Load any GPX on your Garmin, Wahoo, or cycling app.</p>
"""

EMAIL_HTML_FOOTER = """\
<p style="color:#888;font-size:12px;margin-top:30px;">
Routes rotate direction each week so you see different scenery over time.<br>
Generated automatically every Monday.
</p>
</body></html>
"""

ROUTE_BLOCK = """\
<div style="margin-bottom:30px;">
  <h3 style="margin-bottom:6px;">{distance}km route</h3>
  {img_tag}
  <p style="color:#555;font-size:13px;">Actual distance: ~{actual:.1f}km &nbsp;|&nbsp; GPX attached: <code>haarlem_{distance}km.gpx</code></p>
</div>
"""


def send_email(cfg, routes_data, date_str):
    """Send email via Resend API with inline map images and GPX attachments."""

    # Build HTML with base64-embedded images (avoids CID inline issues with some clients)
    blocks = []
    for dist, _gpx, img_bytes, actual_km in routes_data:
        if img_bytes:
            b64 = base64.b64encode(img_bytes).decode('ascii')
            img_tag = (f'<img src="data:image/png;base64,{b64}" '
                       f'style="width:100%;border-radius:8px;display:block;"/>')
        else:
            img_tag = '<p style="color:#aaa;">[map not available]</p>'
        blocks.append(ROUTE_BLOCK.format(
            distance=dist, img_tag=img_tag, actual=actual_km))

    html = EMAIL_HTML_HEADER.format(date=date_str) + '\n'.join(blocks) + EMAIL_HTML_FOOTER

    # Build Resend attachments list
    attachments = []
    for dist, gpx_str, _img, _actual in routes_data:
        attachments.append({
            "filename": f"haarlem_{dist}km.gpx",
            "content": base64.b64encode(gpx_str.encode('utf-8')).decode('ascii'),
            "content_type": "application/gpx+xml"
        })

    payload = {
        "from": cfg['resend_from'],
        "to": [cfg['email_to']],
        "subject": f"🚴 Your Cycling Routes – week of {date_str}",
        "html": html,
        "attachments": attachments
    }

    r = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {cfg['resend_api_key']}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=30
    )
    r.raise_for_status()
    print(f"✓ Email sent to {cfg['email_to']} (id: {r.json().get('id', '?')})")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    api_key = cfg['ors_api_key']

    now = datetime.now()
    week_num = now.isocalendar()[1]
    year = now.year
    date_str = now.strftime("%B %d, %Y")

    print(f"=== Weekly Cycling Routes — week {week_num}/{year} ===")

    # Geocode start
    try:
        start_lat, start_lon = geocode_address(cfg.get('start_address', 'Cremerstraat, Haarlem, Netherlands'), api_key)
        print(f"Start: {start_lat:.5f}, {start_lon:.5f}")
    except Exception as e:
        print(f"Geocoding failed ({e}), using default coords")
        start_lat, start_lon = DEFAULT_LAT, DEFAULT_LON

    routes_data = []
    gpx_dir = os.path.join(OUTPUT_DIR, f"week_{year}-{week_num:02d}")
    os.makedirs(gpx_dir, exist_ok=True)

    for dist in DISTANCES_KM:
        print(f"  Generating {dist}km route...", end=' ', flush=True)
        seed = year * 10000 + week_num * 100 + dist
        try:
            geojson = generate_route(start_lat, start_lon, dist, seed, api_key)
            summary = geojson['features'][0]['properties'].get('summary', {})
            actual_km = round(summary.get('distance', dist * 1000) / 1000, 1)

            gpx_str = geojson_to_gpx(geojson, dist, date_str)
            img_bytes = render_route_image(geojson, dist)

            gpx_path = os.path.join(gpx_dir, f"haarlem_{dist}km.gpx")
            with open(gpx_path, 'w') as f:
                f.write(gpx_str)

            routes_data.append((dist, gpx_str, img_bytes, actual_km))
            img_status = "✓ map" if img_bytes else "no map"
            print(f"✓ {actual_km}km actual, {img_status}")

        except Exception as e:
            print(f"✗ {e}")

    if not routes_data:
        print("No routes generated — check your ORS API key.")
        sys.exit(1)

    print(f"Sending email with {len(routes_data)} routes...")
    send_email(cfg, routes_data, date_str)
    print("Done.")


if __name__ == '__main__':
    main()
