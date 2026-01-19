from gevent import monkey
monkey.patch_all()

from flask import Flask, Response, request
import requests, json, time, random, re
from urllib.parse import quote_plus, urlparse

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0 (Android) IPTV/1.0"

SESSION_TTL = 3600          # 1 ชั่วโมง
CHANNEL_CACHE_TTL = 600    # 10 นาที

STALL_TIMEOUT = 8          # วินาทีที่ไม่มี data → reconnect
FORCE_REFRESH = 900        # รีเฟรชทุก 15 นาที
CHUNK_SIZE = 4096

# --------------------------
# Global state
# --------------------------
client_sessions = {}   # client_id -> {portal, mac, last_seen}
channel_cache = {}     # portal -> (timestamp, mac, channels)

# --------------------------
# Utils
# --------------------------
def load_maclist():
    with open(MACLIST_FILE, encoding="utf-8") as f:
        return json.load(f)

def is_valid_stream_url(url):
    try:
        u = urlparse(url)
        return u.scheme in ("http", "https")
    except:
        return False

def extract_stream(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "").strip().split("|")[0]
    for p in cmd.split():
        if p.startswith(("http://", "https://")):
            return p
    return None

def normalize_name(name):
    return re.sub(r'[^a-z0-9ก-๙]', '', name.lower())

GROUP_KEYWORDS = {
    "Sport": ["sport","football","soccer","f1","bein","กีฬา","ฟุตบอล","บอล"],
    "Movies": ["movie","cinema","hbo","star","หนัง","ภาพยนตร์","ซีรีส์"],
    "Music": ["music","mtv","radio","เพลง","ดนตรี"],
    "Documentary": ["doc","discovery","natgeo","history","wild","earth","สารคดี"],
    "News": ["news","ข่าว"],
    "Kids": ["cartoon","kids","เด็ก","การ์ตูน"],
    "Thai": ["thailand","ไทย","thaichannel"]
}

def get_group_title_auto(name):
    n = normalize_name(name)
    for group, kws in GROUP_KEYWORDS.items():
        for kw in kws:
            if kw in n:
                return group
    return "Live TV"

def get_channel_id(name, mac):
    safe = "".join(c for c in name if c.isalnum())
    return f"{safe}_{mac.replace(':','')}"

def get_channel_logo(channel, portal):
    logo = channel.get("logo") or channel.get("icon") or ""
    if logo and not logo.startswith("http"):
        logo = portal.rstrip("/") + "/" + logo.lstrip("/")
    return logo

# --------------------------
# Session helpers
# --------------------------
def get_client_id():
    return request.remote_addr

def get_saved_mac(client_id, portal):
    s = client_sessions.get(client_id)
    if not s:
        return None
    if s["portal"] != portal:
        return None
    if time.time() - s["last_seen"] > SESSION_TTL:
        client_sessions.pop(client_id, None)
        return None
    return s["mac"]

def save_mac(client_id, portal, mac):
    client_sessions[client_id] = {
        "portal": portal,
        "mac": mac,
        "last_seen": time.time()
    }

# --------------------------
# Portal helpers
# --------------------------
def pick_mac(macs, tried=None):
    tried = tried or set()
    available = [m for m in macs if m not in tried]
    return random.choice(available) if available else None

def get_channels(portal, macs):
    now = time.time()

    if portal in channel_cache:
        ts, mac, channels = channel_cache[portal]
        if now - ts < CHANNEL_CACHE_TTL:
            return mac, channels

    random.shuffle(macs)
    for mac in macs:
        try:
            headers = {"User-Agent": USER_AGENT, "Cookie": f"mac={mac}"}
            r = requests.get(
                f"{portal.rstrip('/')}/server/load.php",
                params={"type":"itv","action":"get_all_channels"},
                headers=headers,
                timeout=10
            )
            data = r.json().get("js", {}).get("data", [])
            channels = []

            if isinstance(data, dict):
                channels = list(data.values())
            elif isinstance(data, list):
                for ch in data:
                    if isinstance(ch, dict):
                        channels.append(ch)
                    elif isinstance(ch, list) and len(ch) >= 2:
                        channels.append({"name": ch[0], "cmd": ch[1]})

            if channels:
                channel_cache[portal] = (now, mac, channels)
                return mac, channels
        except Exception as e:
            app.logger.warning(f"Channel fetch failed {mac}: {e}")

    return None, []

# --------------------------
# Streaming (ANTI FREEZE)
# --------------------------
def stream_response(session, stream, mac):
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}",
        "Connection": "keep-alive"
    }

    def generate():
        last_chunk = time.time()
        start_time = time.time()

        try:
            with session.get(stream, headers=headers, stream=True, timeout=(5, 10)) as r:
                for chunk in r.iter_content(CHUNK_SIZE):
                    now = time.time()

                    if chunk:
                        last_chunk = now
                        yield chunk
                        continue

                    # ไม่มีข้อมูลเกินกำหนด → reconnect
                    if now - last_chunk > STALL_TIMEOUT:
                        app.logger.warning("Stream stalled, reconnect")
                        break

                    # รีเฟรชป้องกัน IPTV freeze
                    if now - start_time > FORCE_REFRESH:
                        app.logger.info("Force stream refresh")
                        break

        except Exception as e:
            app.logger.warning(f"Stream error: {e}")

    return Response(
        generate(),
        content_type="video/mp2t",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    maclist = load_maclist()
    out = "#EXTM3U\n"

    for portal, macs in maclist.items():
        if not macs:
            continue

        mac, channels = get_channels(portal, macs)
        if not mac:
            continue

        for ch in channels:
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            play_url = (
                f"http://{request.host}/play"
                f"?cmd={quote_plus(stream)}"
                f"&portal={quote_plus(portal)}"
            )

            name = ch.get("name", "Live")
            logo = get_channel_logo(ch, portal)
            group = get_group_title_auto(name)

            logo_attr = f' tvg-logo="{logo}"' if logo else ""

            out += (
                f'#EXTINF:-1 tvg-id="{get_channel_id(name, mac)}" '
                f'tvg-name="{name}"{logo_attr} '
                f'group-title="{group}",{name}\n'
                f'{play_url}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    stream = request.args.get("cmd")
    portal = request.args.get("portal")

    if not stream or not is_valid_stream_url(stream):
        return "Invalid stream", 400

    maclist = load_maclist()
    macs = maclist.get(portal, [])
    if not macs:
        return "No MACs", 503

    client_id = get_client_id()
    session = requests.Session()

    # ใช้ MAC เดิมก่อน
    mac = get_saved_mac(client_id, portal)
    if mac and mac in macs:
        try:
            r = session.get(stream, headers={"Cookie": f"mac={mac}"}, stream=True, timeout=(5,5))
            if r.status_code == 200:
                save_mac(client_id, portal, mac)
                return stream_response(session, stream, mac)
        except:
            pass

    # หา MAC ใหม่
    tried = set()
    while len(tried) < len(macs):
        mac = pick_mac(macs, tried)
        if not mac:
            break
        tried.add(mac)
        try:
            r = session.get(stream, headers={"Cookie": f"mac={mac}"}, stream=True, timeout=(5,5))
            if r.status_code == 200:
                save_mac(client_id, portal, mac)
                return stream_response(session, stream, mac)
        except:
            continue

    return "All MACs failed", 503

@app.route("/")
def home():
    return "Live TV Proxy (ANTI FREEZE) running"
