from flask import Flask, Response, request
import requests
import time
import json
import os
from urllib.parse import quote_plus
from threading import Lock

app = Flask(__name__)

# ==========================
# Config
# ==========================
MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600
RATE_LIMIT_CODES = (458, 429)
COOLDOWN_TIME = 30
MAX_RETRY = 2

# ==========================
# Session
# ==========================
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "X-User-Agent": "Model: MAG254; Link: WiFi",
    "X-User-Device": "MAG254",
})

# ==========================
# Globals (thread-safe)
# ==========================
tokens = {}
cooldowns = {}
tokens_lock = Lock()
cooldown_lock = Lock()

# ==========================
# Helpers
# ==========================
def is_in_cooldown(portal_url, mac):
    key = (portal_url, mac)
    with cooldown_lock:
        until = cooldowns.get(key)
        if until and time.time() < until:
            return True
        cooldowns.pop(key, None)
    return False


def set_cooldown(portal_url, mac):
    with cooldown_lock:
        cooldowns[(portal_url, mac)] = time.time() + COOLDOWN_TIME


# ==========================
# Handshake / Token
# ==========================
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
        raise Exception("Handshake failed")

    with tokens_lock:
        tokens[(portal_url, mac)] = {
            "time": time.time(),
            "headers": {**headers, "Authorization": f"Bearer {token}"}
        }


def check_token(portal_url, mac):
    key = (portal_url, mac)
    with tokens_lock:
        info = tokens.get(key)

    if not info or (time.time() - info["time"]) > TOKEN_LIFETIME:
        handshake(portal_url, mac)

    return tokens[key]["headers"]

# ==========================
# Channels
# ==========================
def get_channels(portal_url, mac):
    if is_in_cooldown(portal_url, mac):
        return []

    url = f"{portal_url}/server/load.php"
    headers = check_token(portal_url, mac)

    for attempt in range(MAX_RETRY + 1):
        resp = session.get(
            url,
            params={"type": "itv", "action": "get_all_channels"},
            headers=headers,
            timeout=10
        )

        if resp.status_code in RATE_LIMIT_CODES:
            set_cooldown(portal_url, mac)
            time.sleep(2 * (attempt + 1))
            continue

        if resp.status_code == 401:
            handshake(portal_url, mac)
            headers = check_token(portal_url, mac)
            continue

        if resp.status_code != 200:
            return []

        try:
            data = resp.json()
        except ValueError:
            return []

        channels = data.get("js", {}).get("data", [])
        fixed = []

        for ch in channels:
            if isinstance(ch, dict):
                fixed.append(ch)
            elif isinstance(ch, list) and len(ch) >= 2:
                fixed.append({"name": ch[0], "cmd": ch[1]})

        return fixed

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
    safe = "".join(c for c in name if c.isalnum())
    return f"{safe}_{mac.replace(':','')}"

# ==========================
# Routes
# ==========================
@app.route("/playlist.m3u")
def playlist():
    if not os.path.exists(MACLIST_FILE):
        return Response("maclist.json not found", 404)

    with open(MACLIST_FILE, "r", encoding="utf-8") as f:
        maclist = json.load(f)

    host = request.host
    output = "#EXTM3U\n"

    for portal_url, macs in maclist.items():
        for mac in macs:
            try:
                channels = get_channels(portal_url, mac)
                for ch in channels:
                    stream = get_stream_url(ch.get("cmd", ""))
                    if not stream:
                        continue

                    logo = get_channel_logo(ch, portal_url)
                    name = ch.get("name", "NoName")
                    tvg_id = get_channel_id(ch, mac)

                    play_url = (
                        f"http://{host}/play?"
                        f"portal={quote_plus(portal_url)}"
                        f"&mac={mac}"
                        f"&cmd={quote_plus(ch.get('cmd',''))}"
                    )

                    logo_attr = f' tvg-logo="{logo}"' if logo else ""
                    output += (
                        f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}"'
                        f'{logo_attr} group-title="Live TV",{name}\n'
                        f'{play_url}\n'
                    )
            except Exception as e:
                print("Playlist error:", e)

    return Response(output, mimetype="audio/x-mpegurl")


@app.route("/play")
def play():
    portal_url = request.args.get("portal")
    mac = request.args.get("mac")
    cmd = request.args.get("cmd")

    if not portal_url or not mac or not cmd:
        return Response("Missing parameters", 400)

    if is_in_cooldown(portal_url, mac):
        return Response("Rate limited", 429)

    stream_url = get_stream_url(cmd)
    if not stream_url:
        return Response("Invalid cmd", 400)

    headers = check_token(portal_url, mac)

    for attempt in range(MAX_RETRY + 1):
        upstream = session.get(
            stream_url,
            headers=headers,
            stream=True,
            timeout=(5, 15)
        )

        if upstream.status_code in RATE_LIMIT_CODES:
            set_cooldown(portal_url, mac)
            time.sleep(2 * (attempt + 1))
            continue

        if upstream.status_code == 401:
            handshake(portal_url, mac)
            headers = check_token(portal_url, mac)
            continue

        if upstream.status_code != 200:
            return Response(
                f"Upstream {upstream.status_code}",
                upstream.status_code
            )

        def generate():
            for chunk in upstream.iter_content(8192):
                if not chunk:
                    break
                yield chunk

        return Response(
            generate(),
            content_type=upstream.headers.get(
                "Content-Type", "video/mp2t"
            )
        )

    return Response("Rate limited", 429)


@app.route("/")
def home():
    return "Live TV Proxy is running"


# ==========================
# Run
# ==========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
