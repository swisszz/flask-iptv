from flask import Flask, Response
import requests
import random

app = Flask(__name__)

PORTALS = [
    "http://globalgnet.live:80/c",
    "http://p1.eu58.xyz:8080/c",
    "http://p2.eu58.xyz:8080/c"
]

MACS = [
    "00:1A:79:12:34:56",
    "00:1A:79:73:36:F1",
    "00:1A:79:7C:6A:40"
]

MAX_RETRIES = 3  # retry limit ต่อ portal
session = requests.Session()

def get_channels():
    for portal in PORTALS:
        for attempt in range(MAX_RETRIES):
            mac = random.choice(MACS)
            try:
                resp = session.get(
                    f"{portal}/server/load.php",
                    params={"type": "itv", "action": "get_all_channels"},
                    timeout=10
                )
                data = resp.json()
                
                # เช็คว่า JSON เป็น dict หรือไม่
                if isinstance(data, dict):
                    js_data = data.get("js", {})
                    channels = js_data.get("data", [])
                    if channels:
                        print(f"[INFO] Got {len(channels)} channels from {portal}")
                        return channels
                    else:
                        print(f"[WARN] Empty channel list from {portal}")
                else:
                    print(f"[WARN] Invalid JSON format from {portal}: not a dict")
            except requests.exceptions.RequestException as e:
                print(f"[ERROR] get_channels failed for {portal} attempt {attempt+1}: {e}")
            except ValueError as e:
                print(f"[ERROR] JSON decode failed for {portal} attempt {attempt+1}: {e}")
    print("[ERROR] All portals failed or returned no channels")
    return []

def build_m3u(channels):
    m3u = "#EXTM3U\n"
    for ch in channels:
        name = ch.get("name", "NoName")
        url = ch.get("url", "")
        m3u += f"#EXTINF:-1,{name}\n{url}\n"
    return m3u

@app.route("/playlist.m3u")
def playlist():
    channels = get_channels()
    m3u_content = build_m3u(channels)
    return Response(m3u_content, mimetype="application/x-mpegURL")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
