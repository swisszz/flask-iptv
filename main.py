from flask import Flask, Response
import requests
import time
import random

app = Flask(__name__)

PORTAL_LIST = [
    "http://p1.eu58.xyz:8080/c",
    "http://p2.eu58.xyz:8080/c",
    "http://globalgnet.live:80/c",
]

MAC_LIST = [
    "00:1A:79:7C:6A:40",
    "00:1A:79:12:34:56",
    "00:1A:79:73:36:F1",
]

portal_index = 0
mac_index = 0

def get_current_portal():
    return PORTAL_LIST[portal_index]

def get_current_mac():
    return MAC_LIST[mac_index]

session = requests.Session()
token = None
token_time = 0
TOKEN_LIFETIME = 3600

def update_headers(mac=None):
    if mac is None:
        mac = get_current_mac()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "X-User-Agent": "Model: MAG254; Link: WiFi",
        "X-User-Device": "MAG254",
        "X-User-Device-Id": mac,
        "Cookie": f"mac={mac}; stb_lang=en"
    })

update_headers()

def switch_portal():
    global portal_index
    portal_index = (portal_index + 1) % len(PORTAL_LIST)
    print(f"[INFO] Switching portal → {get_current_portal()}")

def switch_mac(randomize=False):
    global mac_index
    if randomize:
        mac_index = random.randint(0, len(MAC_LIST) - 1)
    else:
        mac_index = (mac_index + 1) % len(MAC_LIST)
    print(f"[INFO] Switching MAC → {get_current_mac()}")
    update_headers()

def handshake():
    global token, token_time
    portal = get_current_portal()
    try:
        resp = session.get(
            f"{portal}/server/load.php",
            params={"type": "stb", "action": "handshake"},
            timeout=10
        )
        data = resp.json()
        token = data.get("js", {}).get("token")
        if not token:
            raise Exception("No token returned")

        session.headers["Authorization"] = f"Bearer {token}"
        token_time = time.time()
        print(f"[INFO] Handshake successful. Token set.")

    except Exception as e:
        print(f"[ERROR] Handshake failed: {e}")
        switch_portal()
        switch_mac(randomize=True)
        return handshake()

def check_token():
    if not token or (time.time() - token_time) > TOKEN_LIFETIME:
        handshake()

def get_channels():
    check_token()
    portal = get_current_portal()
    try:
        resp = session.get(
            f"{portal}/server/load.php",
            params={"type": "itv", "action": "get_all_channels"},
            timeout=10
        )
        data = resp.json()
        js_data = data.get("js", {}) if isinstance(data, dict) else {}
        return js_data.get("data", [])

    except Exception as e:
        print(f"[ERROR] get_channels failed: {e}")
        switch_portal()
        switch_mac(randomize=True)
        return get_channels()

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
    return f"""
    Server is running!<br>
    Active portal: {get_current_portal()}<br>
    Active MAC: {get_current_mac()}
    """

@app.route("/test")
def test():
    try:
        handshake()
        channels = get_channels()
        return {"token": token, "channels_count": len(channels)}
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
