from flask import Flask, Response
import requests
import time
import json
import os

app = Flask(__name__)

# ไฟล์ MAC และ URL
MACLIST_FILE = "maclist.json"

# เวลา token หมดอายุ (วินาที)
TOKEN_LIFETIME = 3600

# session สำหรับ requests
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "X-User-Agent": "Model: MAG254; Link: WiFi",
    "X-User-Device": "MAG254",
})

# เก็บ token ของแต่ละ MAC/URL
tokens = {}

def handshake(portal_url, mac):
    """ทำ handshake สำหรับ MAC และ portal ที่ระบุ"""
    url = f"{portal_url}/server/load.php"
    headers = {
        "X-User-Device-Id": mac,
        "Cookie": f"mac={mac}; stb_lang=en"
    }
    resp = requests.get(url, params={"type": "stb", "action": "handshake"}, headers=headers, timeout=10)

    if resp.status_code != 200:
        raise Exception(f"Error: {mac} @ {portal_url} returned status code {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as e:
        raise Exception(f"Error parsing JSON response for {mac} @ {portal_url}: {e}")

    token = data.get("js", {}).get("token")
    if not token:
        raise Exception(f"Handshake failed for {mac} @ {portal_url}")

    tokens[(portal_url, mac)] = {
        "token": token,
        "time": time.time(),
        "headers": {
            **headers,
            "Authorization": f"Bearer {token}"
        }
    }

def check_token(portal_url, mac):
    """เช็ค token ถ้าเก่าหรือไม่มีให้ handshake ใหม่"""
    key = (portal_url, mac)
    info = tokens.get(key)
    if not info or (time.time() - info["time"]) > TOKEN_LIFETIME:
        handshake(portal_url, mac)
    return tokens[key]["headers"]

def get_channels(portal_url, mac):
    """ดึง channels ของ MAC นั้น"""
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"
    resp = requests.get(url, params={"type": "itv", "action": "get_all_channels"}, headers=headers, timeout=10)

    if resp.status_code != 200:
        print(f"Error: {mac} @ {portal_url} returned status code {resp.status_code}")
        return []

    try:
        data = resp.json()
    except ValueError as e:
        print(f"Error parsing JSON response for {mac} @ {portal_url}: {e}")
        return []

    # 🔹 FIX — รองรับ dict และ list
    if isinstance(data, dict):
        channels = data.get("js", {}).get("data", [])
    elif isinstance(data, list):
        channels = data
    else:
        print(f"Unexpected JSON format: {type(data)}")
        channels = []

    # แปลงเป็นรูปแบบมาตรฐาน
    fixed_channels = []
    for ch in channels:
        if isinstance(ch, dict):
            fixed_channels.append(ch)
        elif isinstance(ch, list) and len(ch) >= 2:
            fixed_channels.append({"name": ch[0], "cmd": ch[1]})
        else:
            print(f"Unexpected channel format: {ch}")

    return fixed_channels

def get_stream_url(cmd):
    """ดึง URL ของ stream จาก cmd"""
    if not cmd:
        return None
    for part in cmd.split():
        if part.startswith("http"):
            return part
    return None

@app.route("/playlist.m3u")
def playlist():
    try:
        all_channels = []

        # โหลด MAC list จากไฟล์
        if not os.path.exists(MACLIST_FILE):
            return Response(f"Error: {MACLIST_FILE} does not exist!", mimetype="text/plain")

        with open(MACLIST_FILE, "r") as f:
            maclist_data = json.load(f)

        for portal_url, macs in maclist_data.items():
            for mac in macs:
                try:
                    channels = get_channels(portal_url, mac)
                    # เพิ่ม prefix ชื่อ MAC เพื่อแยกช่อง
                    for ch in channels:
                        name = ch.get("name", "NoName")
                        url = get_stream_url(ch.get("cmd", ""))
                        if url:
                            all_channels.append({
                                "name": f"{name} ({mac})",
                                "cmd": url
                            })
                except Exception as e:
                    print(f"Error fetching channels for {mac} @ {portal_url}: {e}")

        # สร้าง M3U
        output = "#EXTM3U\n"
        for ch in all_channels:
            output += f"#EXTINF:-1,{ch['name']}\n{ch['cmd']}\n"

        return Response(output, mimetype="audio/x-mpegurl")

    except Exception as e:
        return Response(f"Error: {e}", mimetype="text/plain")

@app.route("/")
def home():
    return "Server is running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

