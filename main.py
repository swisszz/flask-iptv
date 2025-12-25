from flask import Flask, Response, request
import requests, time, json
from urllib.parse import quote_plus

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600

tokens = {}

# --------------------------
# Utils
# --------------------------
def is_direct_url(url):
    return url and ("live.php" in url.lower() or "/ch/" in url.lower() or "localhost" in url.lower())

def get_channel_id(name, mac):
    safe_name = "".join(c for c in name if c.isalnum())
    mac_clean = mac.replace(":", "")
    return f"{safe_name}_{mac_clean}"

def get_channel_logo(channel, portal):
    logo = channel.get("logo") or channel.get("icon") or channel.get("logo_url")
    if logo and not logo.startswith("http"):
        logo = portal.rstrip("/") + "/" + logo.lstrip("/")
    return logo or ""

# --------------------------
# Token / Handshake
# --------------------------
def handshake(portal, mac):
    if is_direct_url(portal):
        return {"User-Agent": "Mozilla/5.0"}
    url = f"{portal}/server/load.php"
    headers = {
        "Cookie": f"mac={mac}; stb_lang=en",
        "X-User-Device-Id": mac,
        "X-User-Agent": "Model: MAG254; Link: WiFi",
        "X-User-Device": "MAG254",
        "User-Agent": "Mozilla/5.0"
    }
    r = requests.get(url, params={"type": "stb", "action": "handshake"}, headers=headers, timeout=10)
    r.raise_for_status()
    token = r.json().get("js", {}).get("token")
    if not token:
        raise Exception("No token")
    tokens[(portal, mac)] = {
        "time": time.time(),
        "headers": {**headers, "Authorization": f"Bearer {token}"}
    }
    return tokens[(portal, mac)]["headers"]

def get_headers(portal, mac):
    if is_direct_url(portal):
        return {"User-Agent": "Mozilla/5.0"}
    key = (portal, mac)
    if key not in tokens or time.time() - tokens[key]["time"] > TOKEN_LIFETIME:
        return handshake(portal, mac)
    return tokens[key]["headers"]

# --------------------------
# Channels
# --------------------------
def get_channels(portal, mac):
    if is_direct_url(portal):
        return [{"name": "Live Stream", "cmd": portal}]
    headers = get_headers(portal, mac)
    r = requests.get(f"{portal}/server/load.php", params={"type": "itv", "action": "get_all_channels"}, headers=headers, timeout=10)
    data = r.json().get("js", {}).get("data", [])
    channels = []
    for ch in data:
        if isinstance(ch, dict):
            channels.append(ch)
        elif isinstance(ch, list) and len(ch) >= 2:
            channels.append({"name": ch[0], "cmd": ch[1]})
    return channels

def extract_stream(cmd):
    if not cmd:
        return None
    for p in cmd.replace("ffmpeg", "").split():
        if p.startswith(("http://", "https://")):
            return p
    return None

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    data = json.load(open(MACLIST_FILE, encoding="utf-8"))
    out = "#EXTM3U\n"
    for portal, macs in data.items():
        mac = macs[0]  # ใช้ MAC ตัวแรก / ตัวเดียว
        for ch in get_channels(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue
            play_url = f"http://{request.host}/play?portal={quote_plus(portal)}&mac={mac}&cmd={quote_plus(stream)}"
            tvg_id = get_channel_id(ch.get("name", "Live"), mac)
            tvg_logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{tvg_logo}"' if tvg_logo else ""
            out += f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{ch.get("name","Live")}"{logo_attr} group-title="Live TV",{ch.get("name","Live")}\n{play_url}\n'
    return Response(out, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    portal = request.args.get("portal")
    mac = request.args.get("mac")
    stream = request.args.get("cmd")
    headers = get_headers(portal, mac)
    def generate():
        session = requests.Session()
        while True:
            try:
                with session.get(stream, headers=headers, stream=True, timeout=(5, 10)) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            yield chunk
                time.sleep(0.1)
            except:
                time.sleep(0.5)
                continue
    return Response(generate(), content_type="video/mp2t", headers={"Cache-Control":"no-cache","Connection":"keep-alive"})

@app.route("/")
def home():
    return "Live TV Proxy running (Single MAC)"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
