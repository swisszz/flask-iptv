from flask import Flask, Response, request
import requests, json, random
from urllib.parse import quote_plus, urlparse

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0 (Android) IPTV/1.0"

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
    """
    คืนค่า (ชื่อ token, ค่า token) อันแรกที่เจอ
    รองรับ token, t, auth
    """
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
        js_data = json_data.get("js", {})
        data = js_data.get("data", [])

        channels = []

        # ตรวจชนิด data
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
    out = "#EXTM3U\n"

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

            name = ch.get("name", "Live")
            logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{logo}"' if logo else ""

            out += (
                f'#EXTINF:-1 tvg-id="{get_channel_id(name, mac)}" '
                f'tvg-name="{name}"{logo_attr} group-title="Live TV",{name}\n'
                f'{play_url}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")


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
