from gevent import monkey
monkey.patch_all()

from flask import Flask, Response, request
import requests, json, time
from urllib.parse import quote_plus, urlparse
import re
import random

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
USER_AGENT = "Mozilla/5.0 (Android) IPTV/1.0"

# --------------------------
# Utils
# --------------------------
def is_direct_url(url):
    if not url:
        return False
    u = url.lower()
    return "live.php" in u or "/ch/" in u or "localhost" in u

def is_valid_stream_url(url):
    try:
        u = urlparse(url)
        return u.scheme in ("http", "https")
    except Exception:
        return False

def get_channel_id(name, mac):
    safe = "".join(c for c in name if c.isalnum())
    return f"{safe}_{mac.replace(':','')}"

def get_channel_logo(channel, portal):
    logo = channel.get("logo") or channel.get("icon") or ""
    if logo and not logo.startswith("http"):
        logo = portal.rstrip("/") + "/" + logo.lstrip("/")
    return logo

def extract_stream(cmd):
    if not cmd:
        return None
    cmd = cmd.replace("ffmpeg", "").strip()
    cmd = cmd.split("|")[0]
    for part in cmd.split():
        if part.startswith(("http://", "https://")):
            return part
    return None

# --------------------------
# Auto Grouping
# --------------------------
GROUP_KEYWORDS = {
    "Sport": ["sport", "football", "soccer", "f1", "bein", "กีฬา", "ฟุตบอล", "บอล"],
    "Movies": ["movie", "cinema", "hbo", "star", "หนัง", "ภาพยนตร์", "ซีรีส์"],
    "Music": ["music", "mtv", "radio", "เพลง", "ดนตรี"],
    "Dokumentary": ["doc", "discovery", "natgeo", "history", "wild", "earth", "สารคดี", "ธรรมชาติ"],
    "News": ["news", "ข่าว", "new"],
    "Kids": ["cartoon", "kids", "เด็ก", "การ์ตูน"],
    "Thai": ["thailand", "thailande", "ไทย", "ช่อง", "thaichannel"]
}

def normalize_name(name: str) -> str:
    return re.sub(r'[^a-z0-9ก-๙]', '', name.lower())

def get_group_title_auto(name: str) -> str:
    raw = name.lower()
    n = normalize_name(name)
    if ("thailand" in n or "thailande" in n or raw.endswith(".th") or n.startswith("th")):
        return "Thai"
    for group, keywords in GROUP_KEYWORDS.items():
        for kw in keywords:
            if kw in n:
                return group
    return "Live TV"

# --------------------------
# Portal helpers
# --------------------------
def get_token():
    for key in ("token", "t", "auth"):
        value = request.args.get(key)
        if value:
            return key, value
    return None, None

def load_maclist():
    with open(MACLIST_FILE, encoding="utf-8") as f:
        return json.load(f)

def pick_mac(macs, tried_macs=None):
    if not macs:
        return None
    if tried_macs is None:
        tried_macs = set()
    available = [m for m in macs if m not in tried_macs]
    return random.choice(available) if available else None

def get_channels(portal_url, mac):
    if is_direct_url(portal_url):
        return [{"name": "Live Stream", "cmd": portal_url}]
    headers = {"User-Agent": USER_AGENT, "Cookie": f"mac={mac}"}
    try:
        r = requests.get(
            f"{portal_url.rstrip('/')}/server/load.php",
            params={"type": "itv", "action": "get_all_channels"},
            headers=headers,
            timeout=10
        )
        r.raise_for_status()
        data = r.json().get("js", {}).get("data", [])
        channels = []
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, dict):
                    channels.append(v)
                elif isinstance(v, list) and len(v) >= 2:
                    channels.append({"name": v[0], "cmd": v[1]})
        elif isinstance(data, list):
            for ch in data:
                if isinstance(ch, dict):
                    channels.append(ch)
                elif isinstance(ch, list) and len(ch) >= 2:
                    channels.append({"name": ch[0], "cmd": ch[1]})
        return channels
    except Exception as e:
        app.logger.error(f"get_channels error for {portal_url}: {e}")
        return []

# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    maclist = load_maclist()
    token_key, token_value = get_token()
    out = "#EXTM3U\n"
    for portal, macs in maclist.items():
        if not macs:
            continue
        mac = macs[0]  # ตัวแรก → โหลดเร็ว
        for ch in get_channels(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue
            play_url = f"http://{request.host}/play?cmd={quote_plus(stream)}&portal={quote_plus(portal)}"
            if token_value:
                play_url += f"&{token_key}={quote_plus(token_value)}"
            name = ch.get("name", "Live")
            logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{logo}"' if logo else ""
            group = get_group_title_auto(name)
            out += (
                f'#EXTINF:-1 tvg-id="{get_channel_id(name, mac)}" '
                f'tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n'
                f'{play_url}\n'
            )
    return Response(out, mimetype="audio/x-mpegurl")

@app.route("/play")
def play():
    stream = request.args.get("cmd")
    portal_req = request.args.get("portal")
    token_key, token_value = get_token()
    if not stream or not is_valid_stream_url(stream):
        return "Invalid stream URL", 400

    maclist = load_maclist()
    session = requests.Session()

    MAX_PORTAL_TRIES = len(maclist)
    tried_portals = set()

    while len(tried_portals) < MAX_PORTAL_TRIES:
        portal = portal_req or next((p for p in maclist if p not in tried_portals), None)
        if not portal or not maclist.get(portal):
            tried_portals.add(portal)
            continue
        tried_portals.add(portal)

        tried_macs = set()
        MAX_MAC_TRIES = len(maclist[portal])

        while len(tried_macs) < MAX_MAC_TRIES:
            mac = pick_mac(maclist[portal], tried_macs)
            if not mac:
                break
            tried_macs.add(mac)

            headers = {"User-Agent": USER_AGENT, "Cookie": f"mac={mac}", "Connection": "keep-alive"}
            params = {}
            if token_value:
                params[token_key] = token_value

            try:
                r_test = session.get(stream, headers=headers, params=params, stream=True, timeout=(5,5))
                if r_test.status_code != 200:
                    app.logger.warning(f"Portal {portal} MAC {mac} returned {r_test.status_code}, trying next MAC")
                    continue

                def generate():
                    try:
                        with session.get(stream, headers=headers, params=params, stream=True, timeout=(5,30)) as r_stream:
                            for chunk in r_stream.iter_content(chunk_size=16384):
                                if chunk:
                                    yield chunk
                    except Exception as e:
                        app.logger.warning(f"Stream broken: {e}")
                        return

                return Response(
                    generate(),
                    content_type="video/mp2t",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
                )

            except Exception as e:
                app.logger.warning(f"Failed MAC {mac} on portal {portal}: {e}")
                continue

    return "All portals failed", 503

@app.route("/")
def home():
    return "Live TV Proxy running"
