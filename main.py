from flask import Flask, Response, request
import requests, time, json, os
from urllib.parse import quote_plus, urlparse

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

tokens = {}
mac_index = {}

# --------------------------
# Utils
# --------------------------
def is_direct_url(url):
    """URL ที่ไม่ต้อง handshake"""
    if not url:
        return False
    u = url.lower()
    return (
        "live.php" in u or
        "/ch/" in u or
        "localhost" in u
    )

# --------------------------
# Handshake
# --------------------------
def handshake(portal_url, mac):
    if is_direct_url(portal_url):
        return None

    url = f"{portal_url}/server/load.php"
    headers = {
        "Cookie": f"mac={mac}; stb_lang=en",
        "X-User-Device-Id": mac,
        "X-User-Agent": "Model: MAG254; Link: WiFi",
        "X-User-Device": "MAG254",
        "User-Agent": "Mozilla/5.0"
    }

    r = session.get(url, params={
        "type": "stb",
        "action": "handshake"
    }, headers=headers, timeout=10)

    r.raise_for_status()
    token = r.json().get("js", {}).get("token")
    if not token:
        raise Exception("No token")

    tokens[(portal_url, mac)] = {
        "time": time.time(),
        "headers": {**headers, "Authorization": f"Bearer {token}"}
    }

def check_token(portal_url, mac):
    if is_direct_url(portal_url):
        return {"User-Agent": "Mozilla/5.0"}

    key = (portal_url, mac)
    if key not in tokens or time.time() - tokens[key]["time"] > TOKEN_LIFETIME:
        handshake(portal_url, mac)
    return tokens[key]["headers"]

# --------------------------
# MAC rotation
# --------------------------
def get_active_mac(portal_url, macs):
    idx = mac_index.get(portal_url, 0)
    for _ in range(len(macs)):
        mac = macs[idx]
        try:
            check_token(portal_url, mac)
            mac_index[portal_url] = idx
            return mac
        except:
            idx = (idx + 1) % len(macs)
    return None

# --------------------------
# Channels
# --------------------------
def get_channels(portal_url, mac):
    if is_direct_url(portal_url):
        return [{"name": "Live Stream", "cmd": portal_url}]

    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"

    r = session.get(url, params={
        "type": "itv",
        "action": "get_all_channels"
    }, headers=headers, timeout=10)

    data = r.json().get("js", {}).get("data", [])
    out = []
    for ch in data:
        if isinstance(ch, dict):
            out.append(ch)
        elif isinstance(ch, list) and len(ch) >= 2:
            out.append({"name": ch[0], "cmd": ch[1]})
    return out

def extract_stream(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "")
    for p in cmd.split():
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
        mac = get_active_mac(portal, macs)
        if not mac:
            continue

        for ch in get_channels(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            play = (
                f"http://{request.host}/play"
                f"?portal={quote_plus(portal)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(stream)}"
            )

            out += (
                f'#EXTINF:-1,{ch.get("name","Live")}\n'
                f'{play}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    portal = request.args.get("portal")
    mac = request.args.get("mac")
    stream = request.args.get("cmd")

    headers = check_token(portal, mac)
    r = session.get(stream, headers=headers, stream=True, timeout=30)

    if r.status_code != 200:
        return Response(f"Upstream {r.status_code}", status=500)

    return Response(
        r.iter_content(65536),
        content_type=r.headers.get("Content-Type", "video/mp2t")
    )

@app.route("/")
def home():
    return "Live TV Proxy running"

# --------------------------
# Run on port 80
# --------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, threaded=True)
