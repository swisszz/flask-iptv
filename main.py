from flask import Flask, Response, request
import requests, json
from urllib.parse import quote_plus, urlparse

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
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

def get_group_title(ch):
    """แยกประเภท + แยกประเทศสำหรับทุกกลุ่ม"""
    genre = str(ch.get("tv_genre_id", "")).lower()
    name = ch.get("name", "").lower()

    # ตรวจ country
    country = ch.get("country", "")
    if not country:
        # ตัวอย่าง detect จากชื่อช่อง
        if "deutsch" in name or "german" in name or "ard" in name:
            country = "DE"
        elif "swiss" in name or "sf" in name:
            country = "SWISS"
        else:
            country = ""

    country = country.upper() if country else ""

    # ตรวจประเภทช่อง
    group = "Live TV"

    # ✅ แก้ตรงนี้ให้ Sky ครอบคลุมมากขึ้น
    if "sky" in name:  # แค่ชื่อช่องมีคำว่า "sky"
        group = "Sky"
    elif "movie" in name or genre == "1":
        group = "Movie"
    elif "sport" in name or genre == "2":
        group = "Sport"
    elif "news" in name or genre == "3":
        group = "News"
    elif "doc" in name or "discovery" in name:
        group = "Dokument"

    # เพิ่ม country ต่อท้ายทุกกลุ่ม
    if country:
        group = f"{group} - {country}"

    return group


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
    out = f'#EXTM3U{" x-tvg-url=\"" + EPG_URL + "\"" if EPG_URL else ""}\n'

    for portal, macs in data.items():
        if not macs:
            continue
        mac = macs[0]  # ใช้ MAC เดียว

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


