from flask import Flask, Response, request
import requests, time, json
from urllib.parse import quote_plus

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600  # swisszzchek

tokens = {}
mac_index = {}

# --------------------------
# Utils
# --------------------------
def is_direct_url(url):
    if not url:
        return False
    u = url.lower()
    return "live.php" in u or "/ch/" in u or "localhost" in u

def get_channel_id(name, mac):
    safe_name = "".join(c for c in name if c.isalnum())
    mac_clean = mac.replace(":", "")
    return f"{safe_name}_{mac_clean}"

def get_channel_logo(channel, portal):
    logo = channel.get("logo") or channel.get("icon") or channel.get("logo_url")
    if logo:
        if not logo.startswith("http"):
            logo = portal.rstrip("/") + "/" + logo.lstrip("/")
        return logo
    return ""

# --------------------------
# Handshake / Token
# --------------------------
def handshake(portal_url, mac):
    if is_direct_url(portal_url):
        return None

    url = f"{portal_url}/server/load.php"
    headers = {
        "Cookie": f"mac={mac}; stb_lang=en",
        "X-User-Device-Id": mac,
        "X-User-Agent": "Model: MAG254; Link: WiFi",
        "X-User-Device": "MAG254",
        "User-Agent": "Mozilla/5.0"
    }

    r = requests.get(
        url,
        params={"type": "stb", "action": "handshake"},
        headers=headers,
        timeout=10
    )
    r.raise_for_status()

    token = r.json().get("js", {}).get("token")
    if not token:
        raise Exception("No token")

    tokens[(portal_url, mac)] = {
        "time": time.time(),
        "headers": {**headers, "Authorization": f"Bearer {token}"}
    }

def check_token(portal_url, mac):
    if is_direct_url(portal_url):
        return {"User-Agent": "Mozilla/5.0"}

    key = (portal_url, mac)
    if key not in tokens or time.time() - tokens[key]["time"] > TOKEN_LIFETIME:
        handshake(portal_url, mac)

    return tokens[key]["headers"]

# --------------------------
# MAC rotation
# --------------------------
def get_active_mac(portal_url, macs):
    idx = mac_index.get(portal_url, 0)

    for _ in range(len(macs)):
        mac = macs[idx]
        try:
            check_token(portal_url, mac)
            mac_index[portal_url] = idx
            return mac
        except:
            idx = (idx + 1) % len(macs)

    return None

# --------------------------
# Channels
# --------------------------
def get_channels(portal_url, mac):
    if is_direct_url(portal_url):
        return [{"name": "Live Stream", "cmd": portal_url}]

    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"

    r = requests.get(
        url,
        params={"type": "itv", "action": "get_all_channels"},
        headers=headers,
        timeout=10
    )

    data = r.json().get("js", {}).get("data", [])
    out = []

    for ch in data:
        if isinstance(ch, dict):
            out.append(ch)
        elif isinstance(ch, list) and len(ch) >= 2:
            out.append({"name": ch[0], "cmd": ch[1]})

    return out

def extract_stream(cmd):
    if not cmd:
        return None

    if is_direct_url(cmd):
        return cmd

    cmd = cmd.replace("ffmpeg", "")
    for p in cmd.split():
        if p.startswith(("http://", "https://")):
            return p
    return None

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    data = json.load(open(MACLIST_FILE, encoding="utf-8"))
    out = "#EXTM3U\n"

    for portal, macs in data.items():
        mac = get_active_mac(portal, macs)
        if not mac:
            continue

        for ch in get_channels(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            play_url = (
                f"http://{request.host}/play"
                f"?portal={quote_plus(portal)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(stream)}"
            )

            tvg_id = get_channel_id(ch.get("name", "Live"), mac)
            tvg_logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{tvg_logo}"' if tvg_logo else ""

            out += (
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{ch.get("name","Live")}"'
                f'{logo_attr} group-title="Live TV",{ch.get("name","Live")}\n'
                f'{play_url}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    portal = request.args.get("portal")
    mac = request.args.get("mac")
    stream = request.args.get("cmd")

    headers = check_token(portal, mac)
    last_token_check = time.time()

    def generate():
        nonlocal headers, last_token_check
        while True:
            try:
                # refresh token เฉพาะ portal (ไม่ใช่ direct)
                if not is_direct_url(stream):
                    if time.time() - last_token_check > 60:
                        headers = check_token(portal, mac)
                        last_token_check = time.time()

                with requests.get(
                    stream,
                    headers=headers,
                    stream=True,
                    timeout=(5, None),
                    allow_redirects=True
                ) as r:
                    r.raise_for_status()

                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            yield chunk

                # stream จบเอง → reconnect ใหม่
                time.sleep(0.2)

            except Exception:
                # error ใด ๆ → reconnect ใหม่
                time.sleep(0.5)
                continue

    return Response(
        generate(),
        content_type="video/mp2t",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )

@app.route("/")
def home():
    return "Live TV Proxy running"

# --------------------------
# Run
# --------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
