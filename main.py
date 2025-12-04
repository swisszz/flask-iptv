from flask import Flask, Response
import requests
import time

app = Flask(__name__)

PORTAL_URL = "http://p1.eu58.xyz:8080/c"
MAC = "00:1A:79:7C:6A:40"
TOKEN_LIFETIME = 3600

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
    return data.get("js", {}).get("data", [])

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
    return "Server is running!"


