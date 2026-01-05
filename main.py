from flask import Flask, Response, request
import requests, json
from urllib.parse import quote_plus, urlparse
import os  # เพิ่มสำหรับ Environment Variable

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
# อ่าน MAC จาก Environment Variable ก่อน ถ้าไม่มีถึงอ่านไฟล์
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
EPG_URL = ""  # ใส่ URL EPG หรือปล่อยว่าง

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

# --------------------------
# Normalize helper
# --------------------------
def normalize(name: str) -> str:
    return (
        name.lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
        .replace("|", "")
        .replace(".", "")
    )

# --------------------------
# Dokument helper
# --------------------------
def is_dokument_channel(name: str) -> bool:
    n = normalize(name)
    dokument_keywords = [
        "doc",
        "discovery",
        "natgeo",
        "natgeowild",
        "netgowild",
        "animalplanet"
    ]
    return any(k in n for k in dokument_keywords)

# --------------------------
# Group title
# --------------------------
def get_group_title(ch):
    """แยกประเภทช่อง (ไม่ต่อท้าย country)"""

    raw_name = ch.get("name", "")
    n = normalize(raw_name)
    genre = str(ch.get("tv_genre_id", "")).lower()

    group = "Live TV"

    if "sky" in n:
        group = "Sky"
    elif "dazn" in n:
        group = "Sport"
    elif "movie" in n or genre == "1":
        group = "Movie"
    elif "sport" in n or genre == "2":
        group = "Sport"
    elif is_dokument_channel(raw_name):
        group = "Dokument"

    return group

# --------------------------
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
        js = r.json().get("js", {})
        data = js.get("data", [])

        channels = []
        if isinstance(data, dict):
            channels.extend(v for v in data.values() if isinstance(v, dict))
        elif isinstance(data, list):
            channels.extend(ch for ch in data if isinstance(ch, dict))

        return channels
    except Exception as e:
        app.logger.error(f"get_channels error: {e}")
        return []

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    data = MACLIST_DATA  # ✅ ใช้ Environment Variable หรือไฟล์เดิม

    token_key, token_value = get_token()
    out = f'#EXTM3U{" x-tvg-url=\"" + EPG_URL + "\"" if EPG_URL else ""}\n'

    for portal, macs in data.items():
        if not macs:
            continue
        mac = macs[0]

        for ch in get_channels(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            name_raw = ch.get("name", "Live")
            name = clean_name(name_raw)
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
        for chunk in r.iter_content(chunk_size=8192):
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
