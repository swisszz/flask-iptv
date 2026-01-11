from flask import Flask, Response, request, jsonify
import requests, json, time, os
from urllib.parse import quote_plus, urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# ==================================================
# CONFIG
# ==================================================
USER_AGENT = "Mozilla/5.0 (QtEmbedded; U; Linux; C)"
EPG_URL = ""

CACHE_TTL = 600  # 10 min
CHANNEL_CACHE = {}

# ==================================================
# LOAD MAC LIST
# ==================================================
def load_maclist():
    env = os.getenv("MACLIST")
    if env:
        try:
            return json.loads(env)
        except:
            pass
    try:
        with open("maclist.json", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

MACLIST_DATA = load_maclist()

# ==================================================
# REQUEST SESSIONS
# ==================================================
# ---- API session (with retry)
api_retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET"]
)

api_session = requests.Session()
api_session.mount("http://", HTTPAdapter(max_retries=api_retry))
api_session.mount("https://", HTTPAdapter(max_retries=api_retry))

# ---- Stream session (NO retry)
stream_session = requests.Session()
stream_session.mount("http://", HTTPAdapter(max_retries=0))
stream_session.mount("https://", HTTPAdapter(max_retries=0))

# ==================================================
# UTILS
# ==================================================
def is_direct_url(url):
    if not url:
        return False
    u = url.lower()
    return any(x in u for x in ("live.php", "/ch/", "localhost"))

def is_valid_url(url):
    try:
        return urlparse(url).scheme in ("http", "https")
    except:
        return False

def extract_stream(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "").split("|")[0]
    for part in cmd.split():
        if part.startswith(("http://", "https://")):
            return part
    return None

def clean_name(name):
    for b in ("HD", "FHD", "UHD", "TH"):
        name = name.replace(b, "")
    return " ".join(name.split()).strip()

def normalize(s):
    return s.lower().replace(" ", "").replace("-", "").replace("_", "")

def get_group(ch):
    n = normalize(ch.get("name", ""))
    g = str(ch.get("tv_genre_id", ""))
    if "movie" in n or g == "1":
        return "Movie"
    if "sport" in n or g == "2":
        return "Sport"
    return "Live TV"

def get_logo(ch, portal):
    logo = ch.get("logo") or ch.get("icon") or ""
    if logo and not logo.startswith("http"):
        return portal.rstrip("/") + "/" + logo.lstrip("/")
    return logo

def get_token():
    for k in ("token", "t", "auth"):
        v = request.args.get(k)
        if v:
            return k, v
    return None, None

# ==================================================
# CHANNEL LOADER
# ==================================================
def load_channels(portal, mac):
    if is_direct_url(portal):
        return [{"name": "Live Stream", "cmd": portal}]

    try:
        r = api_session.get(
            f"{portal.rstrip('/')}/server/load.php",
            params={"type": "itv", "action": "get_all_channels"},
            headers={
                "User-Agent": USER_AGENT,
                "Cookie": f"mac={mac}"
            },
            timeout=5
        )
        r.raise_for_status()
        js = r.json().get("js", {})
        data = js.get("data", [])
        if isinstance(data, dict):
            return list(data.values())
        return data if isinstance(data, list) else []
    except Exception as e:
        app.logger.error(f"CHANNEL LOAD ERROR {portal} | {e}")
        return []

def get_channels_cached(portal, mac):
    key = f"{portal}|{mac}"
    now = time.time()
    if key in CHANNEL_CACHE:
        data, ts = CHANNEL_CACHE[key]
        if now - ts < CACHE_TTL:
            return data
    data = load_channels(portal, mac)
    CHANNEL_CACHE[key] = (data, now)
    return data

# ==================================================
# ROUTES
# ==================================================
@app.route("/playlist.m3u")
def playlist():
    token_key, token_value = get_token()
    out = f'#EXTM3U{" x-tvg-url=\"" + EPG_URL + "\"" if EPG_URL else ""}\n'

    for portal, macs in MACLIST_DATA.items():
        if not macs:
            continue
        mac = macs[0]

        for ch in get_channels_cached(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            name = clean_name(ch.get("name", "Live"))
            group = get_group(ch)
            logo = get_logo(ch, portal)
            logo_attr = f' tvg-logo="{logo}"' if logo else ""

            play = (
                f"http://{request.host}/play"
                f"?portal={quote_plus(portal)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(stream)}"
            )

            if token_value:
                play += f"&{token_key}={quote_plus(token_value)}"

            out += (
                f'#EXTINF:-1 tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n'
                f'{play}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")

# --------------------------------------------------
@app.route("/play")
def play():
    stream = request.args.get("cmd")
    mac = request.args.get("mac")
    token_key, token_value = get_token()

    if not stream or not is_valid_url(stream):
        return "Invalid stream", 400

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}"
    }

    params = {}
    if token_value:
        params[token_key] = token_value

    try:
        r = stream_session.get(
            stream,
            headers=headers,
            params=params,
            stream=True,
            timeout=(6, 15)
        )
        r.raise_for_status()
    except Exception as e:
        app.logger.error(f"STREAM ERROR {stream} | {e}")
        return "Stream unavailable", 502

    def gen():
        for chunk in r.iter_content(8192):
            if chunk:
                yield chunk

    return Response(
        gen(),
        content_type=r.headers.get("Content-Type", "video/mp2t"),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )

# --------------------------------------------------
@app.route("/refresh")
def refresh():
    CHANNEL_CACHE.clear()
    return jsonify({"status": "ok", "cache": "cleared"})

# --------------------------------------------------
@app.route("/")
def home():
    return "Xtream / Stalker IPTV Proxy running"

# ==================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
