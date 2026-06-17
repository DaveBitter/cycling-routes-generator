#!/usr/bin/env python3
"""
Weekly Cycling Route Generator
Generates 7 GPX files (20-80km) from Cremerstraat, Haarlem every Monday.
Emails map previews + download links to daveybitter@gmail.com.

Config: cycling routes/config.json  (local)
        Environment variables       (GitHub Actions)
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
    from PIL import Image, ImageDraw
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    IMAGING = True
except ImportError:
    IMAGING = False
    print("Warning: imaging libs not available, skipping map images")

# ─── Config ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')

DISTANCES_KM = [20, 30, 40, 50, 60, 70, 80]

DEFAULT_LAT = 52.3862
DEFAULT_LON = 4.6289


def load_config():
    """Load from env vars (GitHub Actions) or config.json (local)."""
    if os.environ.get('ORS_API_KEY'):
        return {
            'ors_api_key':       os.environ['ORS_API_KEY'],
            'resend_api_key':    os.environ['RESEND_API_KEY'],
            'resend_from':       os.environ.get('RESEND_FROM', 'onboarding@resend.dev'),
            'email_to':          os.environ.get('EMAIL_TO', 'daveybitter@gmail.com'),
            'start_address':     os.environ.get('START_ADDRESS', 'Cremerstraat, Haarlem, Netherlands'),
            'github_repo':       os.environ.get('GITHUB_REPOSITORY', ''),
            'geoapify_api_key':  os.environ.get('GEOAPIFY_API_KEY', ''),
        }
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault('github_repo', '')
    cfg.setdefault('geoapify_api_key', '')
    return cfg


# ─── Routing ─────────────────────────────────────────────────────────────────

def geocode_address(address, api_key):
    url = "https://api.openrouteservice.org/geocode/search"
    r = requests.get(url, headers={"Authorization": api_key},
                     params={"text": address, "size": 1, "boundary.country": "NL"},
                     timeout=15)
    r.raise_for_status()
    coords = r.json()['features'][0]['geometry']['coordinates']
    return coords[1], coords[0]


def generate_route(start_lat, start_lon, distance_km, seed, api_key):
    url = "https://api.openrouteservice.org/v2/directions/cycling-regular/geojson"
    body = {
        "coordinates": [[start_lon, start_lat]],
        "options": {
            "round_trip": {
                "length": distance_km * 1000,
                "points": 2,
                "seed": seed,
            },
            "avoid_features": ["ferries", "steps"],
        },
        "preference": "recommended",
    }
    for attempt in range(3):
        try:
            r = requests.post(url,
                              headers={"Authorization": api_key, "Content-Type": "application/json"},
                              json=body, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
                raise
            print(f"  ORS attempt {attempt+1} failed ({e}), retrying...")
            import time; time.sleep(3)


def geojson_to_gpx(geojson, distance_km, date_str):
    coords = geojson['features'][0]['geometry']['coordinates']
    summary = geojson['features'][0]['properties'].get('summary', {})
    actual_km = round(summary.get('distance', distance_km * 1000) / 1000, 1)

    # Use <rte>/<rtept> (course/route format) — Garmin Connect requires this
    # for planned routes. <trk>/<trkpt> is the activity/recording format.
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="WeeklyCyclingRoutes"',
        '  xmlns="http://www.topografix.com/GPX/1/1"',
        '  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '  xsi:schemaLocation="http://www.topografix.com/GPX/1/1'
        ' http://www.topografix.com/GPX/1/1/gpx.xsd">',
        '  <metadata>',
        f'    <name>{distance_km}km Route – {date_str}</name>',
        f'    <desc>Round trip from Cremerstraat, Haarlem. Actual: {actual_km}km</desc>',
        f'    <time>{datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}</time>',
        '  </metadata>',
        '  <rte>',
        f'    <name>{distance_km}km Cycling Route – Haarlem</name>',
    ]
    for c in coords:
        lon, lat = c[0], c[1]
        ele = c[2] if len(c) > 2 else 0
        lines.append(f'    <rtept lat="{lat:.6f}" lon="{lon:.6f}"><ele>{ele:.1f}</ele></rtept>')
    lines += ['  </rte>', '</gpx>']
    return '\n'.join(lines), actual_km


# ─── Map rendering (OSM tiles + PIL) ────────────────────────────────────────

def render_route_image(geojson, distance_km, actual_km, geoapify_key=''):
    coords = geojson['features'][0]['geometry']['coordinates']
    img = _render_osm_tiles(coords, distance_km, actual_km)
    if img:
        return img
    return _render_matplotlib(coords, distance_km, actual_km)


def _osm_tile_xy(lat, lon, zoom):
    lat_r = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return x, y


def _tile_top_left(tx, ty, zoom):
    n = 2 ** zoom
    lon = tx / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    return lat, lon


def _fetch_tile(tx, ty, zoom):
    import urllib.request as urlreq
    url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
    try:
        req = urlreq.Request(url, headers={
            'User-Agent': 'WeeklyCyclingRoutes/1.0 (daveybitter@gmail.com)'
        })
        with urlreq.urlopen(req, timeout=8) as r:
            return Image.open(io.BytesIO(r.read())).convert('RGB')
    except Exception:
        return None


def _render_osm_tiles(coords, distance_km, actual_km):
    if not IMAGING:
        return None
    try:
        lats = [c[1] for c in coords]
        lons = [c[0] for c in coords]
        pad = 0.015
        min_lat, max_lat = min(lats) - pad, max(lats) + pad
        min_lon, max_lon = min(lons) - pad, max(lons) + pad

        # Pick zoom so we get ≤ 16 tiles
        zoom = 13
        for z in range(13, 9, -1):
            tx0, ty0 = _osm_tile_xy(max_lat, min_lon, z)
            tx1, ty1 = _osm_tile_xy(min_lat, max_lon, z)
            if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= 16:
                zoom = z
                break

        tx0, ty0 = _osm_tile_xy(max_lat, min_lon, zoom)
        tx1, ty1 = _osm_tile_xy(min_lat, max_lon, zoom)
        T = 256
        W = (tx1 - tx0 + 1) * T
        H = (ty1 - ty0 + 1) * T
        canvas = Image.new('RGB', (W, H), (200, 200, 200))

        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                tile = _fetch_tile(tx, ty, zoom)
                if tile:
                    canvas.paste(tile, ((tx - tx0) * T, (ty - ty0) * T))

        # lat/lon → pixel
        top_lat, left_lon = _tile_top_left(tx0, ty0, zoom)
        bot_lat, right_lon = _tile_top_left(tx1 + 1, ty1 + 1, zoom)

        def px(lat, lon):
            x = int((lon - left_lon) / (right_lon - left_lon) * W)
            y = int((top_lat - lat) / (top_lat - bot_lat) * H)
            return x, y

        draw = ImageDraw.Draw(canvas)
        pixels = [px(c[1], c[0]) for c in coords]

        # Draw route: dark shadow then red line
        for i in range(len(pixels) - 1):
            draw.line([pixels[i], pixels[i+1]], fill=(0, 0, 0), width=5)
        for i in range(len(pixels) - 1):
            draw.line([pixels[i], pixels[i+1]], fill=(220, 50, 50), width=3)

        # Start dot
        sx, sy = pixels[0]
        draw.ellipse([sx-7, sy-7, sx+7, sy+7], fill='white', outline=(220, 50, 50), width=3)

        # Label
        draw.rectangle([6, 6, 160, 24], fill=(255, 255, 255, 220))
        draw.text((10, 8), f'{distance_km}km · actual {actual_km}km', fill=(40, 40, 40))

        # Crop to 800×560 centred on route
        cx, cy = px((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        FW, FH = 800, 560
        x0 = max(0, min(cx - FW // 2, W - FW))
        y0 = max(0, min(cy - FH // 2, H - FH))
        final = canvas.crop((x0, y0, x0 + FW, y0 + FH))

        buf = io.BytesIO()
        final.save(buf, 'PNG', optimize=True)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"  OSM tile render failed: {e}")
        return None


def _render_matplotlib(coords, distance_km, actual_km):
    """Fallback: clean dark chart when tiles unavailable."""
    if not IMAGING:
        return None
    try:
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        fig, ax = plt.subplots(figsize=(10, 6.5))
        fig.patch.set_facecolor('#1a1a2e')
        ax.set_facecolor('#16213e')
        ax.plot(lons, lats, '-', color='#ff6b6b', linewidth=2.5)
        ax.plot(lons[0], lats[0], 'o', color='white', markersize=10,
                markeredgecolor='#ff6b6b', markeredgewidth=2)
        ax.set_aspect('equal')
        ax.tick_params(colors='#666', labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor('#333')
        ax.grid(True, color='#ffffff', alpha=0.05)
        ax.set_title(f'{distance_km}km · actual {actual_km}km · Haarlem',
                     color='#ccc', fontsize=11, pad=10)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='PNG', dpi=130, bbox_inches='tight',
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"  matplotlib failed: {e}")
        return None


# ─── Email ───────────────────────────────────────────────────────────────────

DOWNLOAD_BTN = (
    '<a href="{url}" style="display:inline-block;margin-top:8px;padding:8px 16px;'
    'background:#E74C3C;color:white;text-decoration:none;border-radius:5px;font-size:13px;">'
    '⬇ Download {distance}km GPX</a>'
)

EMAIL_HEADER = """\
<html><body style="font-family:sans-serif;max-width:680px;margin:0 auto;padding:20px;background:#f9f9f9;">
<h2 style="color:#E74C3C;">🚴 Weekly Cycling Routes – {date}</h2>
<p style="color:#555;">Starting from <strong>Cremerstraat, Haarlem</strong>.
Click a button to download the GPX, then load it on your Garmin, Wahoo, or cycling app.</p>
"""

EMAIL_FOOTER = """\
<p style="color:#aaa;font-size:11px;margin-top:30px;">
Routes rotate each week for variety · Generated every Monday via GitHub Actions
</p></body></html>
"""

ROUTE_BLOCK = """\
<div style="margin-bottom:36px;background:white;padding:16px;border-radius:8px;
            box-shadow:0 1px 4px rgba(0,0,0,0.08);">
  <h3 style="margin:0 0 10px;color:#222;">{distance}km route
    <span style="font-weight:normal;font-size:13px;color:#888;">(actual ~{actual:.1f}km)</span>
  </h3>
  {img_tag}
  {download_btn}
</div>
"""


def send_email(cfg, routes_data, date_str, week_folder):
    blocks = []
    for dist, _gpx, img_bytes, actual_km, dl_url in routes_data:
        img_tag = ''
        if img_bytes:
            b64 = base64.b64encode(img_bytes).decode('ascii')
            img_tag = (f'<img src="data:image/png;base64,{b64}" '
                       f'style="width:100%;border-radius:6px;display:block;margin-bottom:10px;"/>')

        if dl_url:
            btn = DOWNLOAD_BTN.format(url=dl_url, distance=dist)
        else:
            btn = f'<p style="color:#aaa;font-size:12px;">GPX: haarlem_{dist}km.gpx</p>'

        blocks.append(ROUTE_BLOCK.format(
            distance=dist, actual=actual_km, img_tag=img_tag, download_btn=btn))

    html = EMAIL_HEADER.format(date=date_str) + '\n'.join(blocks) + EMAIL_FOOTER

    # Attach GPX files so mobile users can open directly in Garmin Connect
    attachments = []
    for dist, gpx_str, _img, _actual, _url in routes_data:
        attachments.append({
            "filename":     f"haarlem_{dist}km.gpx",
            "content":      base64.b64encode(gpx_str.encode('utf-8')).decode('ascii'),
            "content_type": "application/gpx+xml",
        })

    payload = {
        "from":        cfg['resend_from'],
        "to":          [cfg['email_to']],
        "subject":     f"🚴 Cycling routes – {date_str}",
        "html":        html,
        "attachments": attachments,
    }

    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {cfg['resend_api_key']}",
                 "Content-Type": "application/json"},
        json=payload, timeout=30)
    r.raise_for_status()
    print(f"✓ Email sent (id: {r.json().get('id', '?')})")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    api_key = cfg['ors_api_key']
    github_repo = cfg.get('github_repo', '')

    now = datetime.now()
    week_num = now.isocalendar()[1]
    year = now.year
    date_str = now.strftime("%B %d, %Y")
    week_folder = f"routes/week_{year}-{week_num:02d}"

    print(f"=== Weekly Cycling Routes — week {week_num}/{year} ===")

    try:
        start_lat, start_lon = geocode_address(
            cfg.get('start_address', 'Cremerstraat, Haarlem, Netherlands'), api_key)
        print(f"Start: {start_lat:.5f}, {start_lon:.5f}")
    except Exception as e:
        print(f"Geocoding failed ({e}), using defaults")
        start_lat, start_lon = DEFAULT_LAT, DEFAULT_LON

    gpx_dir = os.path.join(SCRIPT_DIR, week_folder)
    os.makedirs(gpx_dir, exist_ok=True)

    routes_data = []
    for dist in DISTANCES_KM:
        print(f"  {dist}km...", end=' ', flush=True)
        seed = year * 10000 + week_num * 100 + dist
        try:
            geojson = generate_route(start_lat, start_lon, dist, seed, api_key)
            gpx_str, actual_km = geojson_to_gpx(geojson, dist, date_str)
            img_bytes = render_route_image(geojson, dist, actual_km,
                                           cfg.get('geoapify_api_key', ''))

            filename = f"haarlem_{dist}km.gpx"
            gpx_path = os.path.join(gpx_dir, filename)
            with open(gpx_path, 'w') as f:
                f.write(gpx_str)

            # Build GitHub raw download URL if repo is known
            if github_repo:
                dl_url = (f"https://raw.githubusercontent.com/{github_repo}"
                          f"/main/{week_folder}/{filename}")
            else:
                dl_url = None

            routes_data.append((dist, gpx_str, img_bytes, actual_km, dl_url))
            print(f"✓ {actual_km}km {'🗺' if img_bytes else '(no map)'}")
        except Exception as e:
            print(f"✗ {e}")

    if not routes_data:
        print("No routes generated.")
        sys.exit(1)

    print("Sending email...")
    send_email(cfg, routes_data, date_str, week_folder)
    print("Done.")


if __name__ == '__main__':
    main()
