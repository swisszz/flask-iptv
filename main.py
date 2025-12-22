from flask import Flask, Response, request, render_template_string
import requests
import time
import json
import os
from urllib.parse import quote_plus

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

tokens = {}
mac_index = {}

# --------------------------
# Handshake / Token
# --------------------------
def handshake(portal_url, mac):
    url = f"{portal_url}/server/load.php"
    headers = {
        "X-User-Device-Id": mac,
        "Cookie": f"mac={mac}; stb_lang=en",
        "User-Agent": "Mozilla/5.0",
        "X-User-Agent": "Model: MAG254; Link: WiFi",
        "X-User-Device": "MAG254",
    }
    try:
        resp = session.get(url, params={"type": "stb", "action": "handshake"}, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("js", {}).get("token")
        if not token:
            raise Exception("No token returned")
        tokens[(portal_url, mac)] = {
            "token": token,
            "time": time.time(),
            "headers": {**headers, "Authorization": f"Bearer {token}"}
        }
    except Exception as e:
        print(f"[Handshake Error] {portal_url} MAC {mac}: {e}")
        raise

def check_token(portal_url, mac):
    key = (portal_url, mac)
    info = tokens.get(key)
    if not info or (time.time() - info["time"]) > TOKEN_LIFETIME:
        handshake(portal_url, mac)
    return tokens[key]["headers"]

# --------------------------
# MAC management
# --------------------------
def get_active_mac(portal_url, mac_list):
    idx = mac_index.get(portal_url, 0)
    mac = mac_list[idx]
    try:
        if "live.php" not in portal_url:
            check_token(portal_url, mac)
        return mac
    except:
        idx = (idx + 1) % len(mac_list)
        mac_index[portal_url] = idx
        mac = mac_list[idx]
        try:
            if "live.php" not in portal_url:
                check_token(portal_url, mac)
            return mac
        except:
            return None

# --------------------------
# Channels
# --------------------------
def get_channels(portal_url, mac):
    if "live.php" in portal_url:
        return [{"name": "Live Channel", "cmd": portal_url}]
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"
    try:
        resp = session.get(url, params={"type": "itv", "action": "get_all_channels"}, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        channels = data.get("js", {}).get("data", [])
        fixed = []
        for ch in channels:
            if isinstance(ch, dict):
                fixed.append(ch)
            elif isinstance(ch, list) and len(ch) >= 2:
                fixed.append({"name": ch[0], "cmd": ch[1]})
        return fixed
    except:
        return []

def get_stream_url(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "")
    for part in cmd.split():
        if part.startswith(("http://", "https://", "udp://", "rtp://")):
            return part
    return None

# --------------------------
# Routes
# --------------------------
@app.route("/")
def home():
    if not os.path.exists(MACLIST_FILE):
        return "maclist.json not found"

    with open(MACLIST_FILE, "r", encoding="utf-8") as f:
        maclist_data = json.load(f)

    all_channels = []

    for portal_url, macs in maclist_data.items():
        mac = get_active_mac(portal_url, macs)
        if not mac:
            continue
        channels = get_channels(portal_url, mac)
        for ch in channels:
            url = get_stream_url(ch.get("cmd",""))
            if not url:
                continue
            all_channels.append({
                "name": ch.get("name","NoName"),
                "url": f"/play?portal={quote_plus(url)}&mac={mac}&cmd={quote_plus(ch.get('cmd',''))}"
            })

    html = """
    <html>
    <head><title>Live TV Proxy</title></head>
    <body>
    <h1>Live TV Channels</h1>
    {% for ch in channels %}
        <div>
            <h3>{{ ch.name }}</h3>
            <video width="480" height="270" controls>
                <source src="{{ ch.url }}" type="application/vnd.apple.mpegurl">
            </video>
        </div>
        <hr>
    {% endfor %}
    </body>
    </html>
    """
    return render_template_string(html, channels=all_channels)

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
        if "live.php" in stream_url:
            headers = {"User-Agent": "Mozilla/5.0"}
        else:
            headers = check_token(portal_url, mac)

        upstream = session.get(stream_url, headers=headers, stream=True, timeout=30)
        if upstream.status_code != 200:
            return Response(f"Upstream error {upstream.status_code}", status=upstream.status_code)

        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        content_type = upstream.headers.get("Content-Type", "video/mp2t")
        return Response(generate(), content_type=content_type)
    except Exception as e:
        print(f"[Play Error] {e}")
        return Response(str(e), status=500)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
