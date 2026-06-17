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
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    IMAGING = True
except ImportError:
    IMAGING = False
    print("Warning: matplotlib not available, skipping map images")

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


# ─── Map rendering ───────────────────────────────────────────────────────────

def render_route_image(geojson, distance_km, actual_km, geoapify_key=''):
    """Try Geoapify static map first; fall back to matplotlib."""
    coords = geojson['features'][0]['geometry']['coordinates']

    if geoapify_key:
        img = _render_geoapify(coords, distance_km, actual_km, geoapify_key)
        if img:
            return img
        print("  Geoapify failed, falling back to matplotlib")

    return _render_matplotlib(coords, distance_km, actual_km)


def _render_geoapify(coords, distance_km, actual_km, api_key):
    """Fetch a static map image from Geoapify with the route drawn on it."""
    import urllib.request as urlreq
    try:
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]

        # Keep max 20 points
        step = max(1, len(coords) // 20)
        simplified = coords[::step][:20]

        polyline_pts = '|'.join(f'{c[0]:.4f},{c[1]:.4f}' for c in simplified)

        span = max(max(lats) - min(lats), max(lons) - min(lons))
        zoom = 13 if span < 0.05 else 12 if span < 0.1 else 11 if span < 0.2 else 10 if span < 0.4 else 9

        cx = (min(lons) + max(lons)) / 2
        cy = (min(lats) + max(lats)) / 2
        sx, sy = coords[0][0], coords[0][1]

        # Use urllib so the URL is sent as-is (requests re-encodes '|' → '%7C')
        url = (
            f"https://maps.geoapify.com/v1/staticmap"
            f"?style=osm-bright&width=800&height=560&zoom={zoom}"
            f"&center=lonlat:{cx:.4f},{cy:.4f}"
            f"&geometry=polyline:E74C3C,4,1|{polyline_pts}"
            f"&marker=lonlat:{sx:.4f},{sy:.4f};type:circle;color:%23E74C3C;size:medium"
            f"&apiKey={api_key}"
        )

        req = urlreq.Request(url, headers={'User-Agent': 'WeeklyCyclingRoutes/1.0'})
        with urlreq.urlopen(req, timeout=20) as resp:
            if resp.status == 200:
                return resp.read()
            print(f"  Geoapify HTTP {resp.status}")
            return None
    except Exception as e:
        print(f"  Geoapify error: {e}")
        return None


def _render_matplotlib(coords, distance_km, actual_km):
    """Fallback: clean route chart with matplotlib (no tile server needed)."""
    if not IMAGING:
        return None
    try:
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]

        fig, ax = plt.subplots(figsize=(10, 6.5))
        fig.patch.set_facecolor('#1a1a2e')
        ax.set_facecolor('#16213e')
        ax.plot(lons, lats, '-', color='#ff6b6b', linewidth=1.5, alpha=0.3, zorder=2)
        ax.plot(lons, lats, '-', color='#ff6b6b', linewidth=2.5, zorder=3)
        ax.plot(lons[0], lats[0], 'o', color='white', markersize=10,
                markeredgecolor='#ff6b6b', markeredgewidth=2, zorder=5)
        ax.set_aspect('equal')
        ax.tick_params(colors='#666', labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor('#333')
        ax.grid(True, color='#ffffff', alpha=0.05, linewidth=0.5)
        ax.set_title(f'{distance_km}km · actual {actual_km}km · Cremerstraat, Haarlem',
                     color='#cccccc', fontsize=11, pad=10)
        ax.annotate('N ↑', xy=(0.97, 0.97), xycoords='axes fraction',
                    ha='right', va='top', color='#888', fontsize=9)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='PNG', dpi=130, bbox_inches='tight',
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"  matplotlib render failed: {e}")
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
