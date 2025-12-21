from flask import Flask, Response
import requests
import time
import json
import os

app = Flask(__name__)

MACLIST_FILE = "maclist.json"  # ไฟล์ MAC และ URL
TOKEN_LIFETIME = 3600

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "X-User-Agent": "Model: MAG254; Link: WiFi",
    "X-User-Device": "MAG254",
})

tokens = {}

def handshake(portal_url, mac):
    url = f"{portal_url}/server/load.php"
    headers = {
        "X-User-Device-Id": mac,
        "Cookie": f"mac={mac}; stb_lang=en"
    }
    resp = session.get(url, params={"type": "stb", "action": "handshake"}, headers=headers, timeout=10)

    if resp.status_code != 200:
        raise Exception(f"Error: {mac} @ {portal_url} returned status code {resp.status_code}")

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

def get_channels(portal_url, mac):
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"
    resp = session.get(url, params={"type": "itv", "action": "get_all_channels"}, headers=headers, timeout=10)

    if resp.status_code != 200:
        print(f"Error: {mac} @ {portal_url} returned status code {resp.status_code}")
        return []

    data = resp.json()
    channels = data.get("js", {}).get("data", [])
    
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

def get_channel_logo(channel, portal_url):
    """ดึงโลโก้ช่อง"""
    logo = channel.get("logo") or channel.get("icon") or channel.get("logo_url")
    if not logo:
        return None
    if logo.startswith("http"):
        return logo
    # ถ้าเป็น path relative
    return portal_url.rstrip("/") + "/" + logo.lstrip("/")

@app.route("/playlist.m3u")
def playlist():
    if not os.path.exists(MACLIST_FILE):
        return Response(f"Error: {MACLIST_FILE} does not exist!", mimetype="text/plain")
    
    with open(MACLIST_FILE, "r") as f:
        maclist_data = json.load(f)

    all_channels = []

    for portal_url, macs in maclist_data.items():
        for mac in macs:
            try:
                channels = get_channels(portal_url, mac)
                for ch in channels:
                    url = get_stream_url(ch.get("cmd", ""))
                    if url:
                        logo = get_channel_logo(ch, portal_url)
                        all_channels.append({
                            "name": ch.get("name", "NoName"),
                            "cmd": url,
                            "logo": logo
                        })
            except Exception as e:
                print(f"Error fetching channels for {mac} @ {portal_url}: {e}")

    output = "#EXTM3U\n"
    for ch in all_channels:
        logo_attr = f' tvg-logo="{ch["logo"]}"' if ch.get("logo") else ""
        output += f'#EXTINF:-1{logo_attr},{ch["name"]}\n{ch["cmd"]}\n'

    return Response(output, mimetype="audio/x-mpegurl")

@app.route("/")
def home():
    return "Server is running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
