from gevent import monkey
monkey.patch_all()

from flask import Flask, Response, request, make_response
import requests, json, time, random, uuid, re
from urllib.parse import quote_plus, urlparse

app = Flask(__name__)

# ==========================
# CONFIG
# ==========================
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0 (QtEmbedded; U; Linux; C)"
SESSION_TTL = 3600
CHANNEL_CACHE_TTL = 300

# ==========================
# GLOBAL STATE
# ==========================
sessions = {}        # session_id -> {portal, mac, token, last_seen}
channel_cache = {}   # portal -> (ts, mac, channels)

# ==========================
# UTILS
# ==========================
def load_maclist():
    with open(MACLIST_FILE, encoding="utf-8") as f:
        return json.load(f)

def is_valid_stream_url(url):
    try:
        return urlparse(url).scheme in ("http", "https")
    except:
        return False

def extract_stream(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "").split("|")[0]
    for p in cmd.split():
        if p.startswith(("http://", "https://")):
            return p
    return None

def normalize_name(name):
    return re.sub(r'[^a-z0-9ก-๙]', '', name.lower())

def get_group(name):
    n = normalize_name(name)
    if "sport" in n or "บอล" in n: return "Sport"
    if "movie" in n or "หนัง" in n: return "Movies"
    if "news" in n or "ข่าว" in n: return "News"
    if "kid" in n or "เด็ก" in n: return "Kids"
    if "music" in n or "เพลง" in n: return "Music"
    if "thai" in n or "ไทย" in n: return "Thai"
    return "Live TV"

# ==========================
# COOKIE SESSION
# ==========================
def get_session_id():
    sid = request.cookies.get("sid")
    if not sid or sid not in sessions:
        sid = uuid.uuid4().hex
        sessions[sid] = {"last_seen": time.time()}
    return sid

def save_session(sid, data):
    data["last_seen"] = time.time()
    sessions[sid] = data

def get_session(sid):
    s = sessions.get(sid)
    if not s:
        return None
    if time.time() - s["last_seen"] > SESSION_TTL:
        sessions.pop(sid, None)
        return None
    return s

# ==========================
# STALKER HANDSHAKE
# ==========================
def stalker_handshake(portal, mac):
    s = requests.Session()
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}; stb_lang=en; timezone=GMT"
    }

    # 1️⃣ handshake
    r = s.get(
        f"{portal.rstrip('/')}/server/load.php",
        params={"type": "stb", "action": "handshake"},
        headers=headers,
        timeout=8
    )
    token = r.json().get("js", {}).get("token")
    if not token:
        raise Exception("Handshake failed")

    headers["Authorization"] = f"Bearer {token}"

    # 2️⃣ get profile (บาง portal บังคับ)
    s.get(
        f"{portal.rstrip('/')}/server/load.php",
        params={"type": "stb", "action": "get_profile"},
        headers=headers,
        timeout=8
    )

    return s, token

def fetch_channels(portal, mac):
    s, token = stalker_handshake(portal, mac)
    r = s.get(
        f"{portal.rstrip('/')}/server/load.php",
        params={"type": "itv", "action": "get_all_channels"},
        timeout=10
    )
    data = r.json().get("js", {}).get("data", [])
    channels = []

    for ch in data:
        if isinstance(ch, dict):
            channels.append(ch)
        elif isinstance(ch, list) and len(ch) >= 2:
            channels.append({"name": ch[0], "cmd": ch[1]})

    return s, token, channels

def get_channels_cached(portal, macs):
    now = time.time()
    if portal in channel_cache:
        ts, mac, channels = channel_cache[portal]
        if now - ts < CHANNEL_CACHE_TTL:
            return mac, channels

    random.shuffle(macs)
    for mac in macs:
        try:
            _, _, ch = fetch_channels(portal, mac)
            if ch:
                channel_cache[portal] = (now, mac, ch)
                return mac, ch
        except:
            continue
    return None, []

# ==========================
# ROUTES
# ==========================
@app.route("/playlist.m3u")
def playlist():
    maclist = load_maclist()
    sid = get_session_id()
    out = "#EXTM3U\n"

    for portal, macs in maclist.items():
        mac, channels = get_channels_cached(portal, macs)
        if not mac:
            continue

        save_session(sid, {"portal": portal, "mac": mac})

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

            out += f'#EXTINF:-1 group-title="{group}",{name}\n{play_url}\n'

    resp = make_response(out)
    resp.mimetype = "audio/x-mpegurl"
    resp.set_cookie("sid", sid, httponly=True)
    return resp

@app.route("/play")
def play():
    stream = request.args.get("cmd")
    portal = request.args.get("portal")
    if not stream or not is_valid_stream_url(stream):
        return "Invalid stream", 400

    sid = get_session_id()
    sess = get_session(sid)
    maclist = load_maclist()
    macs = maclist.get(portal, [])

    session = requests.Session()

    # 1️⃣ ใช้ MAC เดิมก่อน
    if sess and sess.get("portal") == portal:
        mac = sess.get("mac")
        try:
            s, token = stalker_handshake(portal, mac)
            r = s.get(stream, stream=True, timeout=(5, 5))
            if r.status_code == 200:
                return Response(
                    r.iter_content(16384),
                    content_type="video/mp2t",
                    headers={"X-Accel-Buffering": "no"}
                )
        except:
            pass

    # 2️⃣ MAC เดิมใช้ไม่ได้ → random ใหม่
    random.shuffle(macs)
    for mac in macs:
        try:
            s, token = stalker_handshake(portal, mac)
            r = s.get(stream, stream=True, timeout=(5, 5))
            if r.status_code == 200:
                save_session(sid, {"portal": portal, "mac": mac, "token": token})
                return Response(
                    r.iter_content(16384),
                    content_type="video/mp2t",
                    headers={"X-Accel-Buffering": "no"}
                )
        except:
            continue

    return "All MACs failed", 503

@app.route("/")
def home():
    return "Stalker IPTV Proxy running"
