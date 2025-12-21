from flask import Flask, Response
import requests
import time
import json
import os
from threading import Lock

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600

# Session
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "X-User-Agent": "Model: MAG254; Link: WiFi",
    "X-User-Device": "MAG254",
})

# Token cache
tokens = {}
token_lock = Lock()


# -------------------------------
# Handshake / Token
# -------------------------------
def handshake(portal_url, mac):
    url = f"{portal_url}/server/load.php"
    headers = {
        "X-User-Device-Id": mac,
        "Cookie": f"mac={mac}; stb_lang=en"
    }

    resp = session.get(
        url,
        params={"type": "stb", "action": "handshake"},
        headers=headers,
        timeout=10
    )

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
            "headers": {
                **headers,
                "Authorization": f"Bearer {token}"
            }
        }


def check_token(portal_url, mac):
    key = (portal_url, mac)

    with token_lock:
        info = tokens.get(key)
        expired = not info or (time.time() - info["time"]) > TOKEN_LIFETIME

    if expired:
        handshake(portal_url, mac)

    return tokens[key]["headers"]


# -------------------------------
# Channel helpers
# -------------------------------
def get_channels(portal_url, mac):
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"

    resp = session.get(
        url,
        params={"type": "itv", "action": "get_all_channels"},
        headers=headers,
        timeout=10
    )

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


def is_live_tv(channel):
    cmd = channel.get("cmd", "").lower()
    ch_type = channel.get("type", "").lower()

    # ตัด VOD / Series
    if "vod" in cmd or "play_vod" in cmd:
        return False
    if ch_type == "vod":
        return False

    # ต้องเป็น stream สด
    return any(p in cmd for p in ["http://", "https://", "udp://", "rtp://"])


def get_stream_url(cmd):
    if not cmd:
        return None
    for part in cmd.split():
        if part.startswith(("http://", "https://", "udp://", "rtp://")):
            return part
    return None


def get_channel_logo(channel, portal_url):
    logo = (
        channel.get("logo") or
        channel.get("icon") or
        channel.get("logo_url")
    )

    if not logo:
        return None

    if logo.startswith("http"):
        return logo

    return portal_url.rstrip("/") + "/" + logo.lstrip("/")


# -------------------------------
# Routes
# -------------------------------
@app.route("/playlist.m3u")
def playlist():
    if not os.path.exists(MACLIST_FILE):
        return Response("maclist.json not found", mimetype="text/plain")

    with open(MACLIST_FILE, "r", encoding="utf-8") as f:
        maclist_data = json.load(f)

    channels_out = []

    for portal_url, macs in maclist_data.items():
        for mac in macs:
            try:
                channels = get_channels(portal_url, mac)

                for ch in channels:
                    if not is_live_tv(ch):
                        continue

                    stream_url = get_stream_url(ch.get("cmd", ""))
                    if not stream_url:
                        continue

                    channels_out.append({
                        "name": ch.get("name", "NoName"),
                        "url": stream_url,
                        "logo": get_channel_logo(ch, portal_url),
                        "group": "Live TV"
                    })

            except Exception as e:
                print(f"Error {portal_url} {mac}: {e}")

    # Build M3U
    output = "#EXTM3U\n"
    for ch in channels_out:
        logo_attr = f' tvg-logo="{ch["logo"]}"' if ch["logo"] else ""
        output += (
            f'#EXTINF:-1{logo_attr} group-title="{ch["group"]}",{ch["name"]}\n'
            f'{ch["url"]}\n'
        )

    return Response(output, mimetype="audio/x-mpegurl")


@app.route("/")
def home():
    return "Live TV Stream Proxy is running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
