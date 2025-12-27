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
    except:
        return False

def get_channel_id(name, mac):
    safe_name = "".join(c for c in name if c.isalnum())
    mac_clean = mac.replace(":", "")
    return f"{safe_name}_{mac_clean}"

def get_channel_logo(channel, portal):
    logo = channel.get("logo") or channel.get("icon") or ""
    if logo and not logo.startswith("http"):
        logo = portal.rstrip("/") + "/" + logo.lstrip("/")
    return logo

def extract_stream(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "")
    for p in cmd.split():
        if p.startswith(("http://", "https://")):
            return p
    return None

def get_channels(portal_url, mac):
    if is_direct_url(portal_url):
        return [{"name": "Live Stream", "cmd": portal_url}]

    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Cookie": f"mac={mac}"
        }
        url = f"{portal_url.rstrip('/')}/server/load.php"
        r = requests.get(
            url,
            params={"type": "itv", "action": "get_all_channels"},
            headers=headers,
            timeout=10
        )
        r.raise_for_status()

        data = r.json().get("js", {}).get("data", [])
        channels = []

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
        return f"MAC list not found: {e}", 500

    out = "#EXTM3U\n"

    for portal, macs in data.items():
        if not macs:
            continue

        # random MAC เพื่อความเสถียร
        mac = random.choice(macs)

        for ch in get_channels(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            play_url = (
                f"http://{request.host}/play"
                f"?portal={quote_plus(portal)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(stream)}"
            )

            name = ch.get("name", "Live")
            tvg_id = get_channel_id(name, mac)
            tvg_logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{tvg_logo}"' if tvg_logo else ""

            out += (
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}"'
                f'{logo_attr} group-title="Live TV",{name}\n'
                f'{play_url}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    stream = request.args.get("cmd")
    if not stream or not is_valid_stream_url(stream):
        return "Invalid stream URL", 400

    headers = {
        "User-Agent": USER_AGENT,
        "Connection": "keep-alive",
        "Accept": "*/*"
    }

    try:
        # ❗ ไม่มี read timeout (สำคัญมาก)
        r = requests.get(
            stream,
            headers=headers,
            stream=True,
            timeout=5
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        return f"Stream error: {e}", 500

    def generate():
        try:
            with r:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
        except requests.exceptions.ChunkedEncodingError:
            pass
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
