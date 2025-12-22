from flask import Flask, Response, request
import requests
import time
import json
import os
from urllib.parse import quote_plus, urlparse, parse_qs

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600  # 1 ชั่วโมง

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
})

tokens = {}
mac_index = {}  # เก็บ index ของ MAC ที่กำลังใช้ต่อ portal

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
            raise Exception(f"No token returned")
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
        # ถ้า portal เป็น live.php, จะไม่ทำ handshake
        if "live.php" not in portal_url:
            check_token(portal_url, mac)
        return mac
    except:
        print(f"[MAC expired] {mac} for {portal_url}")
        idx = (idx + 1) % len(mac_list)
        mac_index[portal_url] = idx
        mac = mac_list[idx]
        try:
            if "live.php" not in portal_url:
                check_token(portal_url, mac)
            return mac
        except:
            print(f"[MAC failed] {mac} for {portal_url}")
            return None

# --------------------------
# Channels
# --------------------------
def get_channels(portal_url, mac):
    if "live.php" in portal_url:
        # สำหรับ live.php, ดึง channel แบบง่ายๆ
        # สมมติว่าต้องมี stream id list ในอนาคต
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
    except Exception as e:
        print(f"[Get Channels Error] {portal_url} MAC {mac}: {e}")
        return []

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
        mac = get_active_mac(portal_url, macs)
        if not mac:
            continue
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
                f"?portal={quote_plus(url)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(ch.get('cmd',''))}"
            )
            logo_attr = f' tvg-logo="{logo}"' if logo else ""
            output += (
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}"{logo_attr} group-title="Live TV",{tvg_name}\n'
                f'{play_url}\n'
            )

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
        # สำหรับ live.php, ไม่ต้อง handshake
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

@app.route("/")
def home():
    return "Live TV Proxy is running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
