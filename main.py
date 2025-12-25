from flask import Flask, Response, request
import requests, json
from urllib.parse import quote_plus

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0"

# --------------------------
# Utils
# --------------------------
def is_direct_url(url):
    if not url:
        return False
    u = url.lower()
    return "live.php" in u or "/ch/" in u or "localhost" in u

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
        headers = {"User-Agent": USER_AGENT, "Cookie": f"mac={mac}"}
        url = f"{portal_url}/server/load.php"
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
    except:
        return []

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    try:
        data = json.load(open(MACLIST_FILE, encoding="utf-8"))
    except:
        return "MAC list not found", 500

    out = "#EXTM3U\n"
    for portal, macs in data.items():
        mac = macs[0] if macs else ""
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

            tvg_id = get_channel_id(ch.get("name", "Live"), mac)
            tvg_logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{tvg_logo}"' if tvg_logo else ""

            out += (
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{ch.get("name","Live")}"'
                f'{logo_attr} group-title="Live TV",{ch.get("name","Live")}\n'
                f'{play_url}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    stream = request.args.get("cmd")
    if not stream:
        return "No stream URL", 400

    headers = {"User-Agent": USER_AGENT}

    try:
        r = requests.get(stream, headers=headers, stream=True, timeout=(5, 30))
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        return f"Stream error: {e}", 500

    def generate():
        try:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        except GeneratorExit:
            pass
        except:
            pass

    return Response(generate(), content_type="video/mp2t", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive"
    })

@app.route("/")
def home():
    return "Live TV Proxy running"

# --------------------------
# Run via Gunicorn
# --------------------------
if __name__ == "__main__":
    # สำหรับทดสอบ local
    app.run(host="0.0.0.0", port=5000)
