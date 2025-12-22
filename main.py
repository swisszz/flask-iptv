from flask import Flask, Response, request
import requests
import json
import os
import time
from urllib.parse import quote_plus, unquote_plus

app = Flask(__name__)
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
})

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600
CHUNK_SIZE = 256 * 1024  # 256KB per chunk

# -------------------------
# Token management
# -------------------------
tokens = {}

def handshake(portal_url, mac):
    url = f"{portal_url}/server/load.php"
    headers = {"X-User-Device-Id": mac, "Cookie": f"mac={mac}; stb_lang=en"}
    resp = session.get(url, params={"type": "stb", "action": "handshake"}, headers=headers, timeout=10)
    resp.raise_for_status()
    token = resp.json().get("js", {}).get("token")
    if not token:
        raise Exception(f"Handshake failed for {mac} @ {portal_url}")
    tokens[(portal_url, mac)] = {
        "token": token,
        "time": time.time(),
        "headers": {**headers, "Authorization": f"Bearer {token}"}
    }

def check_token(portal_url, mac):
    key = (portal_url, mac)
    info = tokens.get(key)
    if not info or (time.time() - info["time"]) > TOKEN_LIFETIME:
        handshake(portal_url, mac)
    return tokens[key]["headers"]

# -------------------------
# Channel fetching
# -------------------------
def get_channels(portal_url, mac):
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"
    resp = session.get(url, params={"type": "itv", "action": "get_all_channels"}, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("js", {}).get("data", [])
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
    for p in cmd.split():
        if p.startswith("http://") or p.startswith("https://"):
            return p
    return None

# -------------------------
# Playlist
# -------------------------
@app.route("/playlist.m3u")
def playlist():
    if not os.path.exists(MACLIST_FILE):
        return Response(f"Error: {MACLIST_FILE} does not exist!", mimetype="text/plain")

    with open(MACLIST_FILE, "r") as f:
        maclist_data = json.load(f)

    output = "#EXTM3U\n"
    for portal_url, macs in maclist_data.items():
        for mac in macs:
            try:
                channels = get_channels(portal_url, mac)
                for ch in channels:
                    name = ch.get("name", "NoName")
                    url = extract_stream(ch.get("cmd", ""))
                    if url:
                        play_url = f"http://{request.host}/play?portal={quote_plus(portal_url)}&mac={mac}&cmd={quote_plus(url)}"
                        output += f"#EXTINF:-1,{name} ({mac})\n{play_url}\n"
            except Exception as e:
                print(f"Error fetching channels for {mac} @ {portal_url}: {e}")

    return Response(output, mimetype="audio/x-mpegurl")

# -------------------------
# Proxy streaming
# -------------------------
def stream_generator(url, headers=None):
    with session.get(url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        for chunk in r.iter_content(CHUNK_SIZE):
            if chunk:
                yield chunk

@app.route("/play")
def play():
    portal = request.args.get("portal")
    mac = request.args.get("mac")
    cmd = request.args.get("cmd")

    if not portal or not mac or not cmd:
        return "Missing parameters", 400

    portal = unquote_plus(portal)
    cmd = unquote_plus(cmd)

    try:
        headers = check_token(portal, mac)
    except Exception as e:
        return f"Token error: {e}", 500

    try:
        return Response(stream_generator(cmd, headers=headers), content_type="video/mp2t")
    except requests.RequestException as e:
        return f"Upstream error: {e}", 500

# -------------------------
# Home
# -------------------------
@app.route("/")
def home():
    return "Live TV Proxy running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, threaded=True)
