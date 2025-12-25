from flask import Flask, Response, request
import requests, time, json
from urllib.parse import quote_plus

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
PLAYLIST_CACHE = {"time": 0, "data": ""}  # cache playlist 5 นาที
CACHE_DURATION = 300  # 5 นาที

# --------------------------
# Helpers
# --------------------------
def is_direct_url(url):
    if not url:
        return False
    u = url.lower()
    return "live.php" in u or "/ch/" in u or "localhost" in u

def get_channels_from_portal(portal, mac):
    """ดึง channels จาก portal แบบง่าย"""
    if is_direct_url(portal):
        return [{"name": "Live Stream", "cmd": portal, "logo": ""}]
    
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(
            f"{portal}/server/load.php",
            params={"type": "itv", "action": "get_all_channels"},
            headers=headers,
            timeout=10
        )
        data = r.json().get("js", {}).get("data", [])
        channels = []
        for ch in data:
            if isinstance(ch, dict):
                channels.append({
                    "name": ch.get("name", "Live"),
                    "cmd": ch.get("cmd") or ch.get("stream") or "",
                    "logo": ch.get("logo") or ch.get("icon") or ""
                })
            elif isinstance(ch, list) and len(ch) >= 2:
                channels.append({"name": ch[0], "cmd": ch[1], "logo": ""})
        return channels
    except:
        return []

def extract_stream(cmd):
    """ดึง URL จาก command"""
    if not cmd:
        return None
    for p in cmd.split():
        if p.startswith("http://") or p.startswith("https://"):
            return p
    return None

def get_channel_id(name, mac):
    """สร้าง tvg-id จากชื่อช่องและ mac"""
    safe_name = "".join(c for c in name if c.isalnum())
    mac_clean = mac.replace(":", "")
    return f"{safe_name}_{mac_clean}"

def get_channel_logo(channel, portal):
    """ได้ logo แบบ full URL"""
    logo = channel.get("logo") or ""
    if logo and not logo.startswith("http"):
        logo = portal.rstrip("/") + "/" + logo.lstrip("/")
    return logo

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    # ใช้ cache
    if time.time() - PLAYLIST_CACHE["time"] < CACHE_DURATION:
        return Response(PLAYLIST_CACHE["data"], mimetype="audio/x-mpegurl")

    out = "#EXTM3U\n"
    try:
        data = json.load(open(MACLIST_FILE, encoding="utf-8"))
    except:
        data = {}

    for portal, macs in data.items():
        mac = macs[0] if macs else ""
        for ch in get_channels_from_portal(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            play_url = f"http://{request.host}/play?url={quote_plus(stream)}"
            tvg_id = get_channel_id(ch.get("name", "Live"), mac)
            tvg_logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{tvg_logo}"' if tvg_logo else ""

            out += (
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{ch.get("name","Live")}"'
                f'{logo_attr} group-title="Live TV",{ch.get("name","Live")}\n'
                f'{play_url}\n'
            )

    PLAYLIST_CACHE["time"] = time.time()
    PLAYLIST_CACHE["data"] = out
    return Response(out, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    url = request.args.get("url")
    if not url:
        return "No URL", 400

    def generate():
        session = requests.Session()
        while True:
            try:
                with session.get(url, stream=True, timeout=(5, 30)) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            yield chunk
                # reconnect ถ้า stream จบ
                time.sleep(0.1)
            except:
                time.sleep(0.5)
                continue

    return Response(generate(), content_type="video/mp2t", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

@app.route("/")
def home():
    return "Simple Live TV Proxy with Logo Running"

# --------------------------
# Run
# --------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
