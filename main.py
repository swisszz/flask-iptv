from quart import Quart, Response, request
import aiohttp
import asyncio
import time
import json
import os
from threading import Lock, Thread
from urllib.parse import quote_plus
import random

app = Quart(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600
TOKEN_REFRESH_INTERVAL = 3600*2  # รีเฟรช token ทุก 2 ชั่วโมง

# -------------------------------
# Async session
# -------------------------------
session = aiohttp.ClientSession(headers={
    "User-Agent": "Mozilla/5.0",
    "X-User-Agent": "Model: MAG254; Link: WiFi",
    "X-User-Device": "MAG254",
    "Accept-Encoding": "identity"
})

# -------------------------------
# Token & cache
# -------------------------------
tokens = {}
channels_cache = {}
epg_cache = {}
token_lock = Lock()
cache_lock = Lock()

# -------------------------------
# Random delay helper
# -------------------------------
async def random_delay(min_sec=0.1, max_sec=0.5):
    await asyncio.sleep(random.uniform(min_sec, max_sec))

# -------------------------------
# Handshake / Token
# -------------------------------
async def handshake(portal_url, mac):
    await random_delay()
    url = f"{portal_url}/server/load.php"
    headers = {
        "X-User-Device-Id": mac,
        "Cookie": f"mac={mac}; stb_lang=en"
    }
    async with session.get(url, params={"type":"stb","action":"handshake"}, headers=headers, timeout=10) as resp:
        if resp.status != 200:
            raise Exception(f"Handshake HTTP {resp.status}")
        data = await resp.json()
        token = data.get("js", {}).get("token")
        if not token:
            raise Exception("Handshake failed (no token)")
        with token_lock:
            tokens[(portal_url, mac)] = {
                "token": token,
                "time": time.time(),
                "headers": {**headers, "Authorization": f"Bearer {token}"}
            }

async def check_token(portal_url, mac):
    key = (portal_url, mac)
    now = time.time()
    with token_lock:
        info = tokens.get(key)
        expired = not info or (now - info["time"]) > TOKEN_LIFETIME
    if expired:
        await handshake(portal_url, mac)
    return tokens[key]["headers"]

# -------------------------------
# Prefetch cache loop
# -------------------------------
async def refresh_cache_loop():
    while True:
        if not os.path.exists(MACLIST_FILE):
            await asyncio.sleep(10)
            continue
        with open(MACLIST_FILE, "r", encoding="utf-8") as f:
            maclist_data = json.load(f)
        for portal_url, macs in maclist_data.items():
            for mac in macs:
                try:
                    await handshake(portal_url, mac)
                    ch_list = await get_channels(portal_url, mac)
                    epg_list = await get_epg(portal_url, mac)
                    with cache_lock:
                        channels_cache[(portal_url, mac)] = ch_list
                        epg_cache[(portal_url, mac)] = epg_list
                except Exception as e:
                    print(f"Prefetch error {mac} @ {portal_url}: {e}")
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL)

# Run background prefetch loop
asyncio.create_task(refresh_cache_loop())

# -------------------------------
# Portal helpers
# -------------------------------
async def portal_get(portal_url, mac, params):
    headers = await check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"
    async with session.get(url, params=params, headers=headers, timeout=10) as resp:
        if resp.status == 401:
            await handshake(portal_url, mac)
            headers = await check_token(portal_url, mac)
            async with session.get(url, params=params, headers=headers, timeout=10) as resp2:
                return resp2
        return resp

async def portal_stream(portal_url, mac, stream_url):
    headers = await check_token(portal_url, mac)
    async with session.get(stream_url, headers=headers, timeout=10) as resp:
        if resp.status == 401:
            await handshake(portal_url, mac)
            headers = await check_token(portal_url, mac)
            async with session.get(stream_url, headers=headers, timeout=10) as resp2:
                return resp2
        return resp

# -------------------------------
# Channel & EPG helpers
# -------------------------------
async def get_channels(portal_url, mac):
    with cache_lock:
        if (portal_url, mac) in channels_cache:
            return channels_cache[(portal_url, mac)]
    resp = await portal_get(portal_url, mac, params={"type":"itv","action":"get_all_channels"})
    if resp.status != 200:
        return []
    data = await resp.json()
    channels = data.get("js", {}).get("data", [])
    fixed = []
    for ch in channels:
        if isinstance(ch, dict): fixed.append(ch)
        elif isinstance(ch, list) and len(ch)>=2: fixed.append({"name":ch[0], "cmd":ch[1]})
    with cache_lock:
        channels_cache[(portal_url, mac)] = fixed
    return fixed

def is_live_tv(channel):
    cmd = channel.get("cmd","").lower()
    ch_type = channel.get("type","").lower()
    if "vod" in cmd or "play_vod" in cmd: return False
    if ch_type == "vod": return False
    return any(p in cmd for p in ["http://","https://","udp://","rtp://"])

def get_stream_url(cmd):
    if not cmd: return None
    cmd = cmd.replace("ffmpeg","")
    for part in cmd.split():
        if part.startswith(("http://","https://","udp://","rtp://")):
            return part
    return None

def get_channel_logo(channel, portal_url):
    logo = channel.get("logo") or channel.get("icon") or channel.get("logo_url")
    if not logo: return None
    return logo if logo.startswith("http") else portal_url.rstrip("/") + "/" + logo.lstrip("/")

async def get_epg(portal_url, mac):
    with cache_lock:
        if (portal_url, mac) in epg_cache:
            return epg_cache[(portal_url, mac)]
    resp = await portal_get(portal_url, mac, params={"type":"itv","action":"get_epg"})
    if resp.status != 200: return []
    data = await resp.json()
    epg_data = data.get("js", {}).get("data", [])
    with cache_lock:
        epg_cache[(portal_url, mac)] = epg_data
    return epg_data

def generate_epg_xml(maclist_data):
    output = '<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n'
    for portal_url, macs in maclist_data.items():
        for mac in macs:
            try:
                epg_data = epg_cache.get((portal_url, mac), [])
                for ch_epg in epg_data:
                    channel_id = ch_epg.get("channel_id")
                    output += f'  <channel id="{channel_id}">\n'
                    output += f'    <display-name>{ch_epg.get("name","NoName")}</display-name>\n'
                    output += '  </channel>\n'
                    for event in ch_epg.get("events", []):
                        start = event.get("start")
                        stop = event.get("stop")
                        title = event.get("title","")
                        desc = event.get("description","")
                        output += f'  <programme start="{start}" stop="{stop}" channel="{channel_id}">\n'
                        output += f'    <title>{title}</title>\n'
                        if desc: output += f'    <desc>{desc}</desc>\n'
                        output += '  </programme>\n'
            except Exception as e:
                print(f"EPG error {portal_url} {mac}: {e}")
    output += "</tv>\n"
    return output

# -------------------------------
# Routes
# -------------------------------
@app.route("/playlist.m3u")
async def playlist():
    if not os.path.exists(MACLIST_FILE):
        return Response("maclist.json not found", mimetype="text/plain")
    with open(MACLIST_FILE, "r", encoding="utf-8") as f:
        maclist_data = json.load(f)
    output = "#EXTM3U\n"
    for portal_url, macs in maclist_data.items():
        for mac in macs:
            try:
                channels = await get_channels(portal_url, mac)
                for ch in channels:
                    if not is_live_tv(ch): continue
                    logo = get_channel_logo(ch, portal_url)
                    logo_attr = f' tvg-logo="{logo}"' if logo else ""
                    host = request.host
                    play_url = f"http://{host}/play?portal={quote_plus(portal_url)}&mac={mac}&cmd={quote_plus(ch.get('cmd',''))}"
                    output += f'#EXTINF:-1{logo_attr} group-title="Live TV",{ch.get("name","NoName")}\n{play_url}\n'
            except Exception as e:
                print(f"Playlist error {portal_url} {mac}: {e}")
    return Response(output, mimetype="audio/x-mpegurl")

@app.route("/play")
async def play():
    portal_url = request.args.get("portal")
    mac = request.args.get("mac")
    cmd = request.args.get("cmd")
    if not portal_url or not mac or not cmd:
        return Response("Missing parameters", status=400)
    stream_url = get_stream_url(cmd)
    if not stream_url:
        return Response("Invalid stream cmd", status=400)
    try:
        async with await portal_stream(portal_url, mac, stream_url) as upstream:
            async def generator():
                async for chunk in upstream.content.iter_chunked(262144):
                    yield chunk
            return Response(generator(), content_type=upstream.content_type)
    except Exception as e:
        return Response(str(e), status=500)

@app.route("/epg.xml")
async def epg():
    if not os.path.exists(MACLIST_FILE):
        return Response("maclist.json not found", mimetype="text/plain")
    with open(MACLIST_FILE, "r", encoding="utf-8") as f:
        maclist_data = json.load(f)
    xml_output = generate_epg_xml(maclist_data)
    return Response(xml_output, mimetype="application/xml")

@app.route("/")
async def home():
    return "Live TV Stream Proxy (Async) is running"

if __name__ == "__main__":
    import hypercorn.asyncio
    import hypercorn.config
    config = hypercorn.config.Config()
    config.bind = ["0.0.0.0:10000"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
