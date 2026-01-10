from flask import Flask, Response, request, jsonify
import requests, json, time
from urllib.parse import quote_plus, urlparse
import os

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
mac_env = os.getenv("MACLIST")
if mac_env:
    try:
        MACLIST_DATA = json.loads(mac_env)
    except Exception as e:
        print(f"Error parsing MACLIST env: {e}")
        MACLIST_DATA = {}
else:
    MACLIST_FILE = "maclist.json"
    try:
        with open(MACLIST_FILE, encoding="utf-8") as f:
            MACLIST_DATA = json.load(f)
    except Exception as e:
        print(f"Error loading maclist.json: {e}")
        MACLIST_DATA = {}

USER_AGENT = "Mozilla/5.0 (Android) IPTV/1.0"
EPG_URL = ""

# --------------------------
# Requests session + FAST retry
# --------------------------
session = requests.Session()

retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET"]
)

adapter = HTTPAdapter(max_retries=retry)
session.mount("http://", adapter)
session.mount("https://", adapter)

# --------------------------
# Cache
# --------------------------
CHANNEL_CACHE = {}
CACHE_TTL = 600  # 10 นาที

# --------------------------
# Utils
# --------------------------
def is_direct_url(url):
    if not url:
        return False
    u = url.lower()
    return "live.php" in u or "/ch/" in u or "localhost" in u

def is_valid_stream_url(url):
    try:
        u = urlparse(url)
        return u.scheme in ("http", "https")
    except Exception:
        return False

def get_channel_id(name, mac):
    safe = "".join(c for c in name.lower() if c.isalnum())
    return f"{safe}_{mac.replace(':','')}"

def clean_name(name):
    bad = ["HD", "FHD", "UHD", "TH", "[", "]", "(", ")"]
    for b in bad:
        name = name.replace(b, "")
    return " ".join(name.split()).strip()

def normalize(name: str) -> str:
    return (
        name.lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
        .replace("|", "")
        .replace(".", "")
    )

def is_dokument_channel(name: str) -> bool:
    n = normalize(name)
    return any(k in n for k in [
        "doc", "discovery", "natgeo",
        "natgeowild", "netgowild", "animalplanet"
    ])

def get_group_title(ch):
    raw = ch.get("name", "")
    n = normalize(raw)
    genre = str(ch.get("tv_genre_id", "")).lower()

    # 18+ / Adult
    if "adult" in n or "xxx" in n or "18" in n or "porn" in n or "sex" in n:
        return "18+ Adult"

    if "sky" in n:
        return "Sky"
    if "dazn" in n:
        return "Sport"
    if "movie" in n or genre == "1":
        return "Movie"
    if "sport" in n or genre == "2":
        return "Sport"
    if is_dokument_channel(raw):
        return "Dokument"
    return "Live TV"


def get_channel_logo(channel, portal):
    logo = channel.get("logo") or channel.get("icon") or ""
    if logo and not logo.startswith("http"):
        logo = portal.rstrip("/") + "/" + logo.lstrip("/")
    return logo

def extract_stream(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "").split("|")[0]
    for part in cmd.split():
        if part.startswith(("http://", "https://")):
            return part
    return None

def get_token():
    for key in ("token", "t", "auth"):
        value = request.args.get(key)
        if value:
            return key, value
    return None, None

# --------------------------
# Portal loader
# --------------------------
def get_channels(portal_url, mac):
    if is_direct_url(portal_url):
        return [{"name": "Live Stream", "cmd": portal_url}]

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}"
    }

    try:
        r = session.get(
            f"{portal_url.rstrip('/')}/server/load.php",
            params={"type": "itv", "action": "get_all_channels"},
            headers=headers,
            timeout=5
        )
        r.raise_for_status()

        js = r.json().get("js", {})
        data = js.get("data", [])

        if isinstance(data, dict):
            return [v for v in data.values() if isinstance(v, dict)]
        if isinstance(data, list):
            return [ch for ch in data if isinstance(ch, dict)]
        return []

    except Exception as e:
        app.logger.error(f"get_channels error: {e}")
        return []

# --------------------------
# Cached version
# --------------------------
def get_channels_cached(portal_url, mac):
    key = f"{portal_url}|{mac}"
    now = time.time()

    if key in CHANNEL_CACHE:
        channels, ts = CHANNEL_CACHE[key]
        if now - ts < CACHE_TTL:
            return channels

    channels = get_channels(portal_url, mac)
    CHANNEL_CACHE[key] = (channels, now)
    return channels

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    token_key, token_value = get_token()
    out = f'#EXTM3U{" x-tvg-url=\"" + EPG_URL + "\"" if EPG_URL else ""}\n'

    for portal, macs in MACLIST_DATA.items():
        if not macs:
            continue
        mac = macs[0]

        for ch in get_channels_cached(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            name = clean_name(ch.get("name", "Live"))
            group = get_group_title(ch)
            logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{logo}"' if logo else ""

            play_url = (
                f"http://{request.host}/play"
                f"?portal={quote_plus(portal)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(stream)}"
            )

            if token_value:
                play_url += f"&{token_key}={quote_plus(token_value)}"

            out += (
                f'#EXTINF:-1 tvg-id="{get_channel_id(name, mac)}" '
                f'tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n'
                f'{play_url}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")

# --------------------------
# 🔥 Refresh cache
# --------------------------
@app.route("/refresh")
def refresh_cache():
    portal = request.args.get("portal")

    if portal:
        removed = [
            k for k in list(CHANNEL_CACHE.keys())
            if k.startswith(portal)
        ]
        for k in removed:
            CHANNEL_CACHE.pop(k, None)

        return jsonify({
            "status": "ok",
            "message": f"Cache cleared for portal: {portal}",
            "removed": len(removed)
        })

    CHANNEL_CACHE.clear()
    return jsonify({
        "status": "ok",
        "message": "All cache cleared"
    })

# --------------------------
@app.route("/play")
def play():
    stream = request.args.get("cmd")
    mac = request.args.get("mac")
    token_key, token_value = get_token()

    if not stream or not is_valid_stream_url(stream):
        return "Invalid stream URL", 400

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}"
    }

    params = {}
    if token_value:
        params[token_key] = token_value

    try:
        r = session.get(
            stream,
            headers=headers,
            params=params,
            stream=True,
            timeout=(3, None)
        )
        r.raise_for_status()
    except Exception as e:
        return f"Stream error: {e}", 500

    def generate():
        for chunk in r.iter_content(8192):
            if chunk:
                yield chunk

    return Response(
        generate(),
        content_type=r.headers.get("Content-Type", "video/mp2t"),
        headers={"Cache-Control": "no-cache"}
    )

# --------------------------
@app.route("/")
def home():
    return "Live TV Proxy running"

# --------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)

