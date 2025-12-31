from flask import Flask, Response, request
import requests, json, random
from urllib.parse import quote_plus, urlparse
import re

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0 (Android) IPTV/1.0"

# --------------------------
# Country Mapping
# --------------------------
COUNTRY_ALIASES = {
    "TH", "JP", "US", "UK", "KR", "CN", "TW", "HK",
    "FR", "DE", "IT", "ES", "RU", "IN", "VN", "ID",
    "MY", "SG", "PH", "AU", "AT", "CH"
}

COUNTRY_NAMES = {
    "THAILAND": "TH",
    "JAPAN": "JP",
    "UNITED STATES": "US",
    "USA": "US",
    "UNITED KINGDOM": "UK",
    "KOREA": "KR",
    "SOUTH KOREA": "KR",
    "CHINA": "CN",
    "TAIWAN": "TW",
    "HONG KONG": "HK",
    "FRANCE": "FR",
    "GERMANY": "DE",
    "ITALY": "IT",
    "SPAIN": "ES",
    "RUSSIA": "RU",
    "INDIA": "IN",
    "VIETNAM": "VN",
    "INDONESIA": "ID",
    "MALAYSIA": "MY",
    "SINGAPORE": "SG",
    "PHILIPPINES": "PH",
    "AUSTRALIA": "AU",
    "AUSTRIA": "AT",
    "SWISS": "CH",
    "SWITZERLAND": "CH"
}

COUNTRY_KEYWORDS = {
    "THAI": "TH",
    "JAPAN": "JP",
    "NHK": "JP",
    "KOREA": "KR",
    "USA": "US",
    "CNN": "US",
    "BBC": "UK",
    "FRANCE": "FR",
    "GERMAN": "DE",
    "VIET": "VN"
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
# Token helper
# --------------------------
def get_token():
    for key in ("token", "t", "auth"):
        value = request.args.get(key)
        if value:
            return key, value
    return None, None

# --------------------------
# Country detection
# --------------------------
def normalize_country(value):
    if not value:
        return None
    u = value.upper().strip()
    u = re.sub(r"[^A-Z ]", "", u)

    for name, code in COUNTRY_NAMES.items():
        if name in u:
            return code

    if u in COUNTRY_ALIASES:
        return u

    return None

def detect_country(ch):
    name = ch.get("name", "")
    if isinstance(name, str):
        # pattern ######COUNTRY######
        m = re.search(r"#{5,}\s*([A-Z ]+?)\s*#{5,}", name.upper())
        if m:
            c = normalize_country(m.group(1))
            if c:
                return c

    # field จาก portal
    for key in ("country", "country_code", "category", "group", "lang"):
        val = ch.get(key)
        if isinstance(val, str) and val.strip():
            c = normalize_country(val)
            if c:
                return c

    # fallback จากชื่อช่อง
    if isinstance(name, str):
        u = name.upper()
        for code in COUNTRY_ALIASES:
            if f"[{code}]" in u or f"({code})" in u or u.endswith(f" {code}") or f"-{code}" in u:
                return code
        for key, code in COUNTRY_KEYWORDS.items():
            if key in u:
                return code

    return None

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
        js_data = json_data.get("js", {})
        data = js_data.get("data", [])

        channels = []

        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    channels.append(v)
                elif isinstance(v, list) and len(v) >= 2:
                    channels.append({"name": v[0], "cmd": v[1]})
        elif isinstance(data, list):
            for ch in data:
                if isinstance(ch, dict):
                    channels.append(ch)
                elif isinstance(ch, list) and len(ch) >= 2:
                    channels.append({"name": ch[0], "cmd": ch[1]})

        return channels

    except Exception as e:
        app.logger.error(f"get_channels error: {e}")
        return []

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    try:
        with open(MACLIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"MAC list error: {e}", 500

    token_key, token_value = get_token()

    # แยก channel ตามประเทศ
    playlists = {}  # country_code -> m3u text

    for portal, macs in data.items():
        if not macs:
            continue

        for ch in get_channels(portal, random.choice(macs)):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            mac = random.choice(macs)
            play_url = (
                f"http://{request.host}/play"
                f"?portal={quote_plus(portal)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(stream)}"
            )

            if token_value:
                play_url += f"&{token_key}={quote_plus(token_value)}"

            country = detect_country(ch)
            name = ch.get("name", "Live")
            if country:
                display_name = f"[{country}] {name}"
            else:
                display_name = name

            logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{logo}"' if logo else ""

            m3u_line = (
                f'#EXTINF:-1 tvg-id="{get_channel_id(display_name, mac)}" '
                f'tvg-name="{display_name}"{logo_attr} group-title="Live TV",{display_name}\n'
                f'{play_url}\n'
            )

            key = country or "ALL"
            if key not in playlists:
                playlists[key] = "#EXTM3U\n"
            playlists[key] += m3u_line

    # เลือก country ผ่าน query string ?country=CH
    req_country = request.args.get("country")
    if req_country:
        content = playlists.get(req_country.upper())
        if not content:
            return f"No channels for country {req_country}", 404
        return Response(content, mimetype="audio/x-mpegurl")

    # ถ้าไม่มี country query จะรวมทุกประเทศ (เหมือนเดิม)
    combined = "".join(playlists.values())
    return Response(combined, mimetype="audio/x-mpegurl")

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
    return "Live TV Proxy running"

# --------------------------
# Run
# --------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
