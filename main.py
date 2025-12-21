from flask import Flask, Response, request
import requests
import time
import json
import os
from threading import Lock, Thread
from urllib.parse import quote_plus
import random

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600           # 1 ชั่วโมง
TOKEN_REFRESH_INTERVAL = 3600*2 # รีเฟรช token ทุก 2 ชั่วโมง

# -------------------------------
# Session
# -------------------------------
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "X-User-Agent": "Model: MAG254; Link: WiFi",
    "X-User-Device": "MAG254",
    "Accept-Encoding": "identity"   # ปิด gzip
})

# -------------------------------
# Token & cache
# -------------------------------
tokens = {}
channels_cache = {}
epg_cache = {}
token_lock = Lock()
cache_lock = Lock()

# -------------------------------
# Random delay helper
# -------------------------------
def random_delay(min_sec=0.1, max_sec=0.5):
    time.sleep(random.uniform(min_sec, max_sec))

# -------------------------------
# Handshake / Token
# -------------------------------
def handshake(portal_url, mac):
    random_delay()
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
        raise Exception("Handshake failed (no token)")
    with token_lock:
        tokens[(portal_url, mac)] = {
            "token": token,
            "time": time.time(),
            "headers": {**headers, "Authorization": f"Bearer {token}"}
        }

def check_token(portal_url, mac):
    key = (portal_url, mac)
    now = time.time()
    with token_lock:
        info = tokens.get(key)
        expired = not info or (now - info["time"]) > TOKEN_LIFETIME
    if expired:
        handshake(portal_url, mac)
    return tokens[key]["headers"]

# -------------------------------
# Background refresher & prefetch
# -------------------------------
def refresh_cache_loop():
    while True:
        if not os.path.exists(MACLIST_FILE):
            time.sleep(10)
            continue
        with open(MACLIST_FILE, "r", encoding="utf-8") as f:
            maclist_data = json.load(f)
        for portal_url, macs in maclist_data.items():
            for mac in macs:
                try:
                    handshake(portal_url, mac)
                    ch_list = get_channels(portal_url, mac)
                    epg_list = get_epg(portal_url, mac)
                    with cache_lock:
                        channels_cache[(portal_url, mac)] = ch_list
                        epg_cache[(portal_url, mac)] = epg_list
                except Exception as e:
                    print(f"Prefetch error {mac} @ {portal_url}: {e}")
        time.sleep(TOKEN_REFRESH_INTERVAL)

Thread(target=refresh_cache_loop, daemon=True).start()

# -------------------------------
# Portal GET / Stream
# -------------------------------
def portal_get(portal_url, mac, params, timeout=10):
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"
    resp = session.get(url, params=params, headers=headers, timeout=timeout)
    if resp.status_code == 401:
        handshake(portal_url, mac)
        headers = check_token(portal_url, mac)
        resp = session.get(url, params=params, headers=headers, timeout=timeout)
    return resp

def portal_stream(portal_url, mac, stream_url):
    headers = check_token(portal_url, mac)
    resp = session.get(stream_url, headers=headers, stream=True, timeout=10)
    if resp.status_code == 401:
        handshake(portal_url, mac)
        headers = check_token(portal_url, mac)
        resp = session.get(stream_url, headers=headers, stream=True, timeout=10)
    return resp

# -------------------------------
# Channel & EPG helpers
# -------------------------------
def get_channels(portal_url, mac):
    with cache_lock:
        if (portal_url, mac) in channels_cache:
            return channels_cache[(portal_url, mac)]
    resp = portal_get(portal_url, mac, params={"type": "itv", "action": "get_all_channels"})
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
    with cache_lock:
        channels_cache[(portal_url, mac)] = fixed
    return fixed

def is_live_tv(channel):
    cmd = channel.get("cmd", "").lower()
    ch_type = channel.get("type", "").lower()
    if "vod" in cmd or "play_vod" in cmd: return False
    if ch_type == "vod": return False
    return any(p in cmd for p in ["http://", "https://", "udp://", "rtp://"])

def get_stream_url(cmd):
    if not cmd: return None
    cmd = cmd.replace("ffmpeg", "")
    for part in cmd.split():
        if part.startswith(("http://","https://","udp://","rtp://")):
            return part
    return None

def get_channel_logo(channel, portal_url):
    logo = channel.get("logo") or channel.get("icon") or channel.get("logo_url")
    if not logo: return None
    return logo if logo.startswith("http") else portal_url.rstrip("/") + "/" + logo.lstrip("/")

def get_epg(portal_url, mac):
    with cache_lock:
        if (portal_url, mac) in epg_cache:
            return epg_cache[(portal_url, mac)]
    resp = portal_get(portal_url, mac, params={"type":"itv","action":"get_epg"})
    if resp.status_code != 200:
        return []
    data = resp.json()
    epg_data = data.get("js", {}).get("data", [])
    with cache_lock:
        epg_cache[(portal_url, mac)] = epg_data
    return epg_data

def generate_epg_xml(maclist_data):
    output = '<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n'
    for portal_url, macs in maclist_data.items():
        for mac in macs:
            try:
                epg_data = get_epg(portal_url, mac)
                for ch_epg in epg_data:
                    channel_id = ch_epg.get("channel_id")
                    output += f'  <channel id="{channel_id}">\n'
                    output += f'    <display-name>{ch_epg.get("name","NoName")}</display-name>\n'
                    output += '  </channel>\n'
                    for event in ch_epg.get("events", []):
                        start = event.get("start")
                        stop = event.get("stop")
                        title = event.get("title","")
                        desc = event.get("description","")
                        output += f'  <programme start="{start}" stop="{stop}" channel="{channel_id}">\n'
                        output += f'    <title>{title}</title>\n'
                        if desc:
                            output += f'    <desc>{desc}</desc>\n'
                        output += '  </programme>\n'
            except Exception as e:
                print(f"EPG error {portal_url} {mac}: {e}")
    output += "</tv>\n"
    return output

# -------------------------------
# Routes
# -------------------------------
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
                    if not is_live_tv(ch): continue
                    logo = get_channel_logo(ch, portal_url)
                    logo_attr = f' tvg-logo="{logo}"' if logo else ""
                    host = request.host
                    play_url = f"http://{host}/play?portal={quote_plus(portal_url)}&mac={mac}&cmd={quote_plus(ch.get('cmd',''))}"
                    output += f'#EXTINF:-1{logo_attr} group-title="Live TV",{ch.get("name","NoName")}\n{play_url}\n'
            except Exception as e:
                print(f"Playlist error {portal_url} {mac}: {e}")
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
        upstream = portal_stream(portal_url, mac, stream_url)
        if upstream.status_code != 200:
            return Response(f"Upstream error {upstream.status_code}", status=upstream.status_code)
        def generate():
            for chunk in upstream.iter_content(chunk_size=262144):  # 256 KB
                if chunk: yield chunk
        return Response(generate(), content_type=upstream.headers.get("Content-Type", "video/mp2t"))
    except Exception as e:
        return Response(str(e), status=500)

@app.route("/epg.xml")
def epg():
    if not os.path.exists(MACLIST_FILE):
        return Response("maclist.json not found", mimetype="text/plain")
    with open(MACLIST_FILE, "r", encoding="utf-8") as f:
        maclist_data = json.load(f)
    xml_output = generate_epg_xml(maclist_data)
    return Response(xml_output, mimetype="application/xml")

@app.route("/")
def home():
    return "Live TV Stream Proxy is running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
