from gevent import monkey
monkey.patch_all()

from flask import Flask, Response, request
import requests, json, time
from urllib.parse import quote_plus, urlparse

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


group = get_group_title_auto(name)
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
    "Thai": ["thailand", "ไทย", "ช่องไทย", "thaichannel"]
}

def get_group_title_auto(name: str) -> str:
    """
    จัดกลุ่มช่องอัตโนมัติจากชื่อช่อง
    รองรับภาษาอังกฤษและไทย
    """
    n = name.lower()  # ทำ lowercase เพื่อเช็คง่าย
    for group, keywords in GROUP_KEYWORDS.items():
        for kw in keywords:
            if kw in n:
                return group
    return "Live TV"  # default



# --------------------------
# Portal
# --------------------------
def get_token():
    # ดึง token จาก query string แต่จะไม่ใส่ลง playlist
    for key in ("token", "t", "auth"):
        value = request.args.get(key)
        if value:
            return key, value
    return None, None


def get_channels(portal_url, mac):
    if is_direct_url(portal_url):
        return [{"name": "Live Stream", "cmd": portal_url}]

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}"
    }

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
        app.logger.error(f"get_channels error: {e}")
        return []


# --------------------------
# Routes
# --------------------------
@app.route("/playlist.m3u")
def playlist():
    with open(MACLIST_FILE, encoding="utf-8") as f:
        data = json.load(f)

    token_key, token_value = get_token()
    out = "#EXTM3U\n"

    for portal, macs in data.items():
        if not macs:
            continue

        mac = macs[0]

        for ch in get_channels(portal, mac):
            stream = extract_stream(ch.get("cmd"))
            if not stream:
                continue

            # ใส่ token เฉพาะที่ /play เท่านั้น
            play_url = (
                f"http://{request.host}/play"
                f"?portal={quote_plus(portal)}"
                f"&mac={mac}"
                f"&cmd={quote_plus(stream)}"
            )
            if token_value:
                play_url += f"&{token_key}={quote_plus(token_value)}"

            name = ch.get("name", "Live")
            logo = get_channel_logo(ch, portal)
            logo_attr = f' tvg-logo="{logo}"' if logo else ""
            group = get_group_title(name)

            out += (
                f'#EXTINF:-1 tvg-id="{get_channel_id(name, mac)}" '
                f'tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n'
                f'{play_url}\n'
            )

    return Response(out, mimetype="audio/x-mpegurl")


@app.route("/play")
def play():
    stream = request.args.get("cmd")
    mac = request.args.get("mac")
    token_key, token_value = get_token()

    if not stream or not is_valid_stream_url(stream):
        return "Invalid stream URL", 400

    session = requests.Session()

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": f"mac={mac}",
        "Connection": "keep-alive"
    }

    params = {}
    if token_value:
        params[token_key] = token_value

    def generate():
        while True:
            try:
                r = session.get(
                    stream,
                    headers=headers,
                    params=params,
                    stream=True,
                    timeout=(5, 30)
                )

                for chunk in r.iter_content(chunk_size=16384):
                    if chunk:
                        yield chunk

            except Exception as e:
                app.logger.warning(f"Reconnect stream: {e}")
                time.sleep(0.5)

    return Response(
        generate(),
        content_type="video/mp2t",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.route("/")
def home():
    return "Live TV Proxy running"

