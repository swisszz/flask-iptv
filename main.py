from flask import Flask, Response, request
import requests, json, random
from urllib.parse import quote_plus, urlparse

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0 (Android) IPTV/1.0"

# Allowed countries keywords
ALLOWED_COUNTRIES = {
    "UK": ["UK", "UNITED KINGDOM", "BRITAIN", "ENGLAND"],
    "DE": ["DE", "GERMANY", "DEUTSCHLAND"],
    "TH": ["TH", "THAILAND", "THAI", "ไทย"],
    "AT": ["AT", "AUSTRIA", "ÖSTERREICH", "OESTERREICH"],
    "CH": ["CH", "SWISS", "SCHWEIZ", "SUISSE", "SVIZZERA"],
}

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
    safe = "".join(c for c in name if c.isalnum())
    return f"{safe}_{mac.replace(':','')}"

def get_channel_logo(channel, portal):
    logo = channel.get("logo") or channel.get("icon") or ""
    if logo and not logo.startswith("http"):
        logo = portal.rstrip("/") + "/" + logo.lstrip("/")
    return logo

def extract_stream(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "").strip()
    cmd = cmd.split("|")[0]
    for part in cmd.split():
        if part.startswith(("http://", "https://")):
            return part
    return None

# --------------------------
# Country filter helpers
# --------------------------
def extract_country_from_group(group):
    """
    ตรวจชื่อประเทศจาก group field เช่น TV#####SWISS#####
    """
    group = (group or "").upper()
    for country_key, keywords in ALLOWED_COUNTRIES.items():
        for kw in keywords:
            if kw.upper() in group:
                return country_key
    return None

def channel_allowed(channel):
    country = extract_country_from_group(channel.get("group"))
    return country is not None

def detect_country(channel):
    country = extract_country_from_group(channel.get("group"))
    return country or "OTHER"

# --------------------------
# Token helper
# --------------------------
def get_token():
    for key in ("token", "t", "auth"):
        value = request.args.get(key)
        if value:
            return key, value
    return None, None

# --------------------------
# Portal
# --------------------------
def get_channels(portal_url, mac):
    if is_direct_url(portal_url):
        return [{"name": "Live Stream", "cmd": portal_url}]

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}"
    }

    try:
        r = requests.get(
            f"{portal_url.rstrip('/')}/server/load.php",
            params={"type": "itv", "action": "get_all_channels"},
            headers=headers,
            timeout=10
        )
        r.raise_for_status()
        json_data = r.json()
        data = json_data.get("js", {}).get("data", [])

        channels = []

        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, dict):
                    channels.append(v)
        elif isinstance(data, list):
            for ch in data:
                if isinstance(ch, dict):
                    channels.append(ch)

        return channels

    except Exception as e:
        app.logger.error(f"get_channels error: {e}")
        return []

# --------------------------
# Playlist generator
# --------------------------
def generate_playlist(country_filter=None):
    try:
        with open(MACLIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"MAC list error: {e}", 500

    token_key, token_value = get_token()
    out = "#EXTM3U\n"

    for portal, macs in data.items():
        if not macs:
            continue
        mac = random.choice(macs)

        for ch in get_channels(portal, mac):
            if not channel_allowed(ch):
                continue

            country = detect_country(ch)
            if country_filter and country != country_filter:
                continue

            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            play_url = (
                f"http://{request.host}/play"
                f"?portal={quote_plus(portal)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(stream)}"
            )
            if token_value:
                play_url += f"&{token_key}={quote_plus(token_value)}"

            name = ch.get("name") or "Live"
            logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{logo}"' if logo else ""

            out += (
                f'#EXTINF:-1 tvg-id="{get_channel_id(name, mac)}" '
                f'tvg-name="{name}"{logo_attr} group-title="{country}",{name}\n'
                f'{play_url}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    return generate_playlist()  # playlist รวมทุกประเทศ

@app.route("/<country>.m3u")
def playlist_country(country):
    country = country.upper()
    if country not in ALLOWED_COUNTRIES:
        return "Invalid country", 400
    return generate_playlist(country_filter=country)

@app.route("/play")
def play():
    stream = request.args.get("cmd")
    mac = request.args.get("mac")
    token_key, token_value = get_token()

    if not stream or not is_valid_stream_url(stream):
        return "Invalid stream URL", 400

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}",
        "Accept": "*/*",
        "Connection": "keep-alive"
    }

    params = {}
    if token_value:
        params[token_key] = token_value

    try:
        r = requests.get(
            stream,
            headers=headers,
            params=params,
            stream=True,
            timeout=(5, None)
        )
        r.raise_for_status()
    except Exception as e:
        return f"Stream error: {e}", 500

    def generate():
        try:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        except Exception:
            pass

    return Response(
        generate(),
        content_type=r.headers.get("Content-Type", "video/mp2t"),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )

@app.route("/")
def home():
    return "Live TV Proxy running (UK / DE / TH / AT / CH)"

# --------------------------
# Run
# --------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
