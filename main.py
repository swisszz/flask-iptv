from flask import Flask, Response, request
import requests
import time
import json
import os
from urllib.parse import quote_plus

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600  # 1 ชั่วโมง

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "X-User-Agent": "Model: MAG254; Link: WiFi",
    "X-User-Device": "MAG254",
})

tokens = {}

# --------------------------
# Handshake / Token
# --------------------------
def handshake(portal_url, mac):
    url = f"{portal_url}/server/load.php"
    headers = {
        "X-User-Device-Id": mac,
        "Cookie": f"mac={mac}; stb_lang=en"
    }
    resp = session.get(url, params={"type": "stb", "action": "handshake"}, headers=headers, timeout=10)
    if resp.status_code != 200:
        raise Exception(f"Handshake HTTP {resp.status_code}")

    data = resp.json()
    token = data.get("js", {}).get("token")
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

# --------------------------
# Channels
# --------------------------
def get_channels(portal_url, mac):
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"
    resp = session.get(url, params={"type": "itv", "action": "get_all_channels"}, headers=headers, timeout=10)
    if resp.status_code != 200:
        return []

    data = resp.json()
    channels = data.get("js", {}).get("data", [])

    fixed = []
    for ch in channels:
        if isinstance(ch, dict):
            fixed.append(ch)
        elif isinstance(ch, list) and len(ch) >= 2:
            fixed.append({"name": ch[0], "cmd": ch[1]})
    return fixed

def get_stream_url(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "")
    for part in cmd.split():
        if part.startswith(("http://", "https://", "udp://", "rtp://")):
            return part
    return None

def get_channel_logo(channel, portal_url):
    logo = channel.get("logo") or channel.get("icon") or channel.get("logo_url")
    if not logo:
        return None
    if logo.startswith("http"):
        return logo
    return portal_url.rstrip("/") + "/" + logo.lstrip("/")

def get_channel_id(channel, mac):
    name = channel.get("name", "NoName")
    safe_name = "".join(c for c in name if c.isalnum())
    mac_clean = mac.replace(":", "")
    return f"{safe_name}_{mac_clean}"

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    if not os.path.exists(MACLIST_FILE):
        return Response("maclist.json not found", mimetype="text/plain")
    
    with open(MACLIST_FILE, "r", encoding="utf-8") as f:
        maclist_data = json.load(f)

    output = "#EXTM3U\n"

    for portal_url, macs in maclist_data.items():
        for mac in macs:
            try:
                channels = get_channels(portal_url, mac)
                for ch in channels:
                    url = get_stream_url(ch.get("cmd",""))
                    if not url:
                        continue
                    logo = get_channel_logo(ch, portal_url)
                    tvg_id = get_channel_id(ch, mac)
                    tvg_name = ch.get("name","NoName")
                    host = request.host
                    play_url = (
                        f"http://{host}/play"
                        f"?portal={quote_plus(portal_url)}"
                        f"&mac={mac}"
                        f"&cmd={quote_plus(ch.get('cmd',''))}"
                    )
                    logo_attr = f' tvg-logo="{logo}"' if logo else ""
                    output += (
                        f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}"{logo_attr} group-title="Live TV",{tvg_name}\n'
                        f'{play_url}\n'
                    )
            except Exception as e:
                print(f"Error {portal_url} {mac}: {e}")

    return Response(output, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    portal_url = request.args.get("portal")
    mac = request.args.get("mac")
    cmd = request.args.get("cmd")
    if not portal_url or not mac or not cmd:
        return Response("Missing parameters", status=400)

    stream_url = get_stream_url(cmd)
    if not stream_url:
        return Response("Invalid stream cmd", status=400)

    try:
        headers = check_token(portal_url, mac)
        upstream = session.get(stream_url, headers=headers, stream=True, timeout=10)
        if upstream.status_code != 200:
            return Response(f"Upstream error {upstream.status_code}", status=upstream.status_code)

        def generate():
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return Response(generate(), content_type=upstream.headers.get("Content-Type", "video/mp2t"))
    except Exception as e:
        return Response(str(e), status=500)

@app.route("/")
def home():
    return "Live TV Proxy is running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
