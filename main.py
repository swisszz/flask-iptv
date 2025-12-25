from flask import Flask, Response, request
import requests, time, json
from urllib.parse import quote_plus

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0"
TOKEN_LIFETIME = 3600  # 1 ชั่วโมง

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

    r = requests.get(
        url,
        params={"type": "stb", "action": "handshake"},
        headers=headers,
        timeout=10
    )
    r.raise_for_status()

    token = r.json().get("js", {}).get("token")
    if not token:
        raise Exception("Handshake failed (no token)")

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

    r = requests.get(
        url,
        params={"type": "itv", "action": "get_all_channels"},
        headers=headers,
        timeout=10
    )
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
# 🔑 create_link (เพิ่มใหม่)
# --------------------------
def create_link(portal_url, mac, cmd):
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"

    r = requests.get(
        url,
        params={
            "type": "itv",
            "action": "create_link",
            "cmd": cmd,
            "forced_storage": 0
        },
        headers=headers,
        timeout=10
    )
    r.raise_for_status()

    js = r.json().get("js", {})
    stream_cmd = js.get("cmd")
    if not stream_cmd:
        raise Exception("create_link failed")

    return extract_stream(stream_cmd)

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    try:
        with open(MACLIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except:
        return "MAC list not found", 500

    out = "#EXTM3U\n"

    for portal, macs in data.items():
        mac = get_active_mac(portal, macs)
        if not mac:
            continue

        for ch in get_channels(portal, mac):
            cmd = ch.get("cmd")
            if not cmd:
                continue

            play_url = (
                f"http://{request.host}/play"
                f"?portal={quote_plus(portal)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(cmd)}"
            )

            name = ch.get("name", "Live")
            tvg_id = get_channel_id(name, mac)
            tvg_logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{tvg_logo}"' if tvg_logo else ""

            out += (
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}"'
                f'{logo_attr} group-title="Live TV",{name}\n'
                f'{play_url}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    portal = request.args.get("portal")
    mac = request.args.get("mac")
    cmd = request.args.get("cmd")

    def generate():
        session = requests.Session()

        try:
            # ถ้าไม่ใช่ direct → พยายาม create_link
            if not is_direct_url(cmd):
                try:
                    stream = create_link(portal, mac, cmd)
                    if not stream:
                        raise Exception("empty stream from create_link")
                except Exception as e:
                    # fallback ใช้ cmd เดิม
                    print("[WARN] create_link failed, fallback to cmd:", e)
                    stream = extract_stream(cmd) or cmd
            else:
                stream = cmd

            headers = {"User-Agent": USER_AGENT}

            with session.get(stream, headers=headers, stream=True, timeout=(5, 30)) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk

        except Exception as e:
            print("[PLAY ERROR]", e)
            return

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
    app.run(host="0.0.0.0", port=5000)

