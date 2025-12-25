from flask import Flask, Response, request
import requests, time, json
from urllib.parse import quote_plus

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0"
TOKEN_LIFETIME = 3600  # อายุ token 1 ชั่วโมง

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
    if logo and not logo.startswith("http"):
        logo = portal.rstrip("/") + "/" + logo.lstrip("/")
    return logo or ""

# --------------------------
# Token / Handshake
# --------------------------
def handshake(portal_url, mac):
    if is_direct_url(portal_url):
        return {"User-Agent": USER_AGENT}

    url = f"{portal_url}/server/load.php"
    headers = {
        "Cookie": f"mac={mac}; stb_lang=en",
        "X-User-Device-Id": mac,
        "X-User-Agent": "Model: MAG254; Link: WiFi",
        "X-User-Device": "MAG254",
        "User-Agent": USER_AGENT
    }

    r = requests.get(url, params={"type": "stb", "action": "handshake"}, headers=headers, timeout=10)
    r.raise_for_status()
    token = r.json().get("js", {}).get("token")
    if not token:
        raise Exception("No token from handshake")

    tokens[(portal_url, mac)] = {
        "time": time.time(),
        "headers": {**headers, "Authorization": f"Bearer {token}"}
    }
    return tokens[(portal_url, mac)]["headers"]

def check_token(portal_url, mac):
    if is_direct_url(portal_url):
        return {"User-Agent": USER_AGENT}

    key = (portal_url, mac)
    if key not in tokens or time.time() - tokens[key]["time"] > TOKEN_LIFETIME:
        return handshake(portal_url, mac)

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
    r = requests.get(url, params={"type": "itv", "action": "get_all_channels"}, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json().get("js", {}).get("data", [])
    channels = []
    for ch in data:
        if isinstance(ch, dict):
            channels.append(ch)
        elif isinstance(ch, list) and len(ch) >= 2:
            channels.append({"name": ch[0], "cmd": ch[1]})
    return channels

def extract_stream(cmd):
    if not cmd:
        return None
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
    try:
        data = json.load(open(MACLIST_FILE, encoding="utf-8"))
    except:
        return "MAC list not found", 500

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
    last_token_check = time.time()

    def generate():
        session = requests.Session()
        nonlocal last_token_check
        while True:
            try:
                # refresh token ทุก 60 วินาที
                if not is_direct_url(stream) and time.time() - last_token_check > 60:
                    headers = check_token(portal, mac)
                    last_token_check = time.time()
                else:
                    headers = {"User-Agent": USER_AGENT}

                with session.get(stream, headers=headers, stream=True, timeout=(5,30)) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            yield chunk

                time.sleep(0.1)

            except requests.exceptions.RequestException as e:
                print(f"[Network Error] reconnecting: {e}")
                time.sleep(0.5)
                continue
            except Exception as e:
                print(f"[Other Error] reconnecting: {e}")
                time.sleep(0.5)
                continue

    return Response(
        generate(),
        content_type="video/mp2t",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )

@app.route("/")
def home():
    return "Live TV Proxy running"

# --------------------------
# Run locally
# --------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
