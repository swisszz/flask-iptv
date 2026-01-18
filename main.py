from gevent import monkey
monkey.patch_all()

from flask import Flask, Response, request
import requests, json, time, random, re
from urllib.parse import quote_plus, urlparse

app = Flask(__name__)

# ==========================
# CONFIG
# ==========================
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0 (Android) IPTV/1.0"
SESSION_TTL = 3600        # 1 ชั่วโมง
CHANNEL_CACHE_TTL = 300   # 5 นาที

# ==========================
# GLOBAL STATE
# ==========================
client_sessions = {}   # client_ip -> {portal, mac, last_seen}
channel_cache = {}     # portal -> (timestamp, mac, channels)

# ==========================
# UTILS
# ==========================
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
    "Sport": ["sport", "football", "bein", "บอล", "กีฬา"],
    "Movies": ["movie", "cinema", "hbo", "หนัง"],
    "News": ["news", "ข่าว"],
    "Kids": ["kids", "cartoon", "เด็ก"],
    "Music": ["music", "เพลง"],
    "Thai": ["thai", "ไทย"]
}

def get_group(name):
    n = normalize_name(name)
    for g, kws in GROUP_KEYWORDS.items():
        for k in kws:
            if k in n:
                return g
    return "Live TV"

# ==========================
# STALKER HELPERS
# ==========================
def fetch_channels(portal, mac):
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}"
    }
    r = requests.get(
        f"{portal.rstrip('/')}/server/load.php",
        params={"type": "itv", "action": "get_all_channels"},
        headers=headers,
        timeout=8
    )
    r.raise_for_status()
    data = r.json().get("js", {}).get("data", [])
    channels = []

    if isinstance(data, list):
        for ch in data:
            if isinstance(ch, dict):
                channels.append(ch)
            elif isinstance(ch, list) and len(ch) >= 2:
                channels.append({"name": ch[0], "cmd": ch[1]})
    return channels

def pick_working_mac(portal, macs):
    macs = macs[:]
    random.shuffle(macs)
    for mac in macs:
        try:
            ch = fetch_channels(portal, mac)
            if ch:
                return mac, ch
        except:
            continue
    return None, []

def get_channels_cached(portal, macs):
    now = time.time()
    if portal in channel_cache:
        ts, mac, ch = channel_cache[portal]
        if now - ts < CHANNEL_CACHE_TTL:
            return mac, ch

    mac, ch = pick_working_mac(portal, macs)
    if mac:
        channel_cache[portal] = (now, mac, ch)
    return mac, ch

# ==========================
# SESSION HELPERS
# ==========================
def get_client_id():
    return request.remote_addr

def get_client_mac(client_id, portal):
    s = client_sessions.get(client_id)
    if not s:
        return None
    if s["portal"] != portal:
        return None
    if time.time() - s["last_seen"] > SESSION_TTL:
        client_sessions.pop(client_id, None)
        return None
    return s["mac"]

def save_client_mac(client_id, portal, mac):
    client_sessions[client_id] = {
        "portal": portal,
        "mac": mac,
        "last_seen": time.time()
    }

# ==========================
# STREAM HELPERS
# ==========================
def test_stream(session, stream, mac):
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}"
    }
    r = session.get(stream, headers=headers, stream=True, timeout=(5, 5))
    return r.status_code == 200

def stream_response(session, stream, mac):
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}",
        "Connection": "keep-alive"
    }

    def generate():
        with session.get(stream, headers=headers, stream=True) as r:
            for chunk in r.iter_content(16384):
                if chunk:
                    yield chunk

    return Response(
        generate(),
        content_type="video/mp2t",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

# ==========================
# ROUTES
# ==========================
@app.route("/playlist.m3u")
def playlist():
    maclist = load_maclist()
    out = "#EXTM3U\n"

    for portal, macs in maclist.items():
        mac, channels = get_channels_cached(portal, macs)
        if not mac:
            continue

        for ch in channels:
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            name = ch.get("name", "Live")
            group = get_group(name)

            play_url = (
                f"http://{request.host}/play?"
                f"cmd={quote_plus(stream)}&portal={quote_plus(portal)}"
            )

            out += (
                f'#EXTINF:-1 group-title="{group}",{name}\n'
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
        return "No MAC", 503

    client_id = get_client_id()
    session = requests.Session()

    # 1️⃣ ลอง MAC เดิมก่อน
    mac = get_client_mac(client_id, portal)
    if mac and mac in macs:
        try:
            if test_stream(session, stream, mac):
                save_client_mac(client_id, portal, mac)
                return stream_response(session, stream, mac)
        except:
            pass

    # 2️⃣ ถ้าไม่ได้ → random ใหม่
    tried = set()
    while len(tried) < len(macs):
        mac = random.choice([m for m in macs if m not in tried])
        tried.add(mac)

        try:
            if test_stream(session, stream, mac):
                save_client_mac(client_id, portal, mac)
                return stream_response(session, stream, mac)
        except:
            continue

    return "All MACs failed", 503

@app.route("/")
def home():
    return "Live TV Proxy running"
