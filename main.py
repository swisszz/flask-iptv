from flask import Flask, Response
import requests
import time
import json
import os

app = Flask(__name__)

PORTAL_URL = "http://globalgnet.live:80/c"
MAC = "00:1A:79:73:36:F1"
TOKEN_LIFETIME = 3600
MACLIST_FILE = "maclist.json"  # ไฟล์ MAC เพิ่มเติม

session = requests.Session()
headers = {
    "User-Agent": "Mozilla/5.0",
    "X-User-Agent": "Model: MAG254; Link: WiFi",
    "X-User-Device": "MAG254",
    "X-User-Device-Id": MAC,
    "Cookie": f"mac={MAC}; stb_lang=en"
}
session.headers.update(headers)

token = None
token_time = 0

def handshake():
    global token, token_time
    url = f"{PORTAL_URL}/server/load.php"
    resp = session.get(url, params={"type": "stb", "action": "handshake"}, timeout=10)
    data = resp.json()
    token = data.get("js", {}).get("token")
    if not token:
        raise Exception("Handshake failed: no token")
    session.headers["Authorization"] = f"Bearer {token}"
    token_time = time.time()

def check_token():
    if not token or (time.time() - token_time) > TOKEN_LIFETIME:
        handshake()

def get_channels():
    check_token()
    url = f"{PORTAL_URL}/server/load.php"
    resp = session.get(url, params={"type": "itv", "action": "get_all_channels"}, timeout=10)
    data = resp.json()
    channels = data.get("js", {}).get("data", [])
    
    # แปลง channels ถ้าเป็น list ของ list
    fixed_channels = []
    for ch in channels:
        if isinstance(ch, dict):
            fixed_channels.append(ch)
        elif isinstance(ch, list) and len(ch) >= 2:
            fixed_channels.append({"name": ch[0], "cmd": ch[1]})
    return fixed_channels

def get_stream_url(cmd):
    if not cmd:
        return None
    for part in cmd.split():
        if part.startswith("http"):
            return part
    return None

@app.route("/playlist.m3u")
def playlist():
    try:
        channels = get_channels()

        # โหลด MAC list เพิ่มเติมจากไฟล์
        if os.path.exists(MACLIST_FILE):
            with open(MACLIST_FILE, "r") as f:
                maclist = json.load(f)

            if isinstance(maclist, dict):
                # maclist เป็น dict ของ portal_url -> list ของ MACs
                for portal_url, macs in maclist.items():
                    for idx, mac in enumerate(macs):
                        name = f"User {idx+1}"
                        url = f"{portal_url}?mac={mac}"
                        channels.append({"name": name, "cmd": url})

            elif isinstance(maclist, list):
                # maclist เป็น list ของ MACs
                for idx, mac in enumerate(maclist):
                    name = f"User {idx+1}"
                    url = f"{PORTAL_URL}?mac={mac}"
                    channels.append({"name": name, "cmd": url})

        output = "#EXTM3U\n"
        for ch in channels:
            name = ch.get("name", "NoName")
            url = get_stream_url(ch.get("cmd", ""))
            if url:
                output += f"#EXTINF:-1,{name}\n{url}\n"

        return Response(output, mimetype="audio/x-mpegurl")

    except Exception as e:
        return Response(f"Error: {e}", mimetype="text/plain")

@app.route("/")
def home():
    return "Server is running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
