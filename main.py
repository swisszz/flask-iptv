from flask import Flask, Response
import requests
import time
import json
import os
import datetime
import xml.etree.ElementTree as ET

app = Flask(__name__)

MACLIST_FILE = "maclist.json"
TOKEN_LIFETIME = 3600

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "X-User-Agent": "Model: MAG254; Link: WiFi",
    "X-User-Device": "MAG254",
    "Connection": "Keep-Alive",
})

tokens = {}

# ============================================================================================
# Utility
# ============================================================================================

def build_headers(mac, token=None):
    cookie = f"mac={mac}; stb_lang=en; timezone=UTC;"
    headers = {
        "X-User-Device-Id": mac,
        "X-User-Serial": mac.replace(":", ""),
        "X-Device-Name": "MAG254",
        "Cookie": cookie,
        "Referer": "",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_token(data):
    js = data.get("js", {})

    if "token" in js and isinstance(js["token"], str):
        return js["token"]

    if isinstance(js.get("token"), dict):
        return js["token"].get("token")

    if isinstance(js.get("data"), dict):
        return js["data"].get("token")

    return None


# ============================================================================================
# Handshake / Token
# ============================================================================================

def handshake(portal_url, mac):
    url = f"{portal_url}/server/load.php"

    params = {
        "type": "stb",
        "action": "handshake",
        "token": "",
        "prehash": "0"
    }

    headers = build_headers(mac)

    resp = session.get(url, params=params, headers=headers, timeout=10)

    if resp.status_code != 200:
        raise Exception(f"Handshake failed {mac} @ {portal_url} | HTTP {resp.status_code}")

    data = resp.json()
    token = parse_token(data)

    if not token:
        raise Exception(f"No token returned from portal for {mac} @ {portal_url}")

    tokens[(portal_url, mac)] = {
        "token": token,
        "time": time.time()
    }

    return token


def check_token(portal_url, mac):
    key = (portal_url, mac)
    info = tokens.get(key)

    if not info or (time.time() - info["time"]) > TOKEN_LIFETIME:
        token = handshake(portal_url, mac)
    else:
        token = info["token"]

    return build_headers(mac, token)

# ============================================================================================
# Get Channels
# ============================================================================================

def get_channels(portal_url, mac):
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"

    payload = {
        "type": "itv",
        "action": "get_all_channels"
    }

    resp = session.post(url, data=payload, headers=headers, timeout=10)

    if resp.status_code != 200:
        return []

    try:
        data = resp.json()
    except:
        return []

    channels = data.get("js", {}).get("data", [])

    fixed = []
    for ch in channels:
        if isinstance(ch, dict):
            fixed.append(ch)
        elif isinstance(ch, list) and len(ch) >= 2:
            fixed.append({"name": ch[0], "cmd": ch[1]})

    return fixed


def get_stream_url(cmd):
    if not cmd:
        return None
    for part in cmd.split():
        if part.startswith("http"):
            return part
    return None


# ============================================================================================
# Get EPG
# ============================================================================================

def get_epg_for_portal(portal_url, mac, hours=36):
    """ดึง EPG ของ portal นี้"""
    headers = check_token(portal_url, mac)
    url = f"{portal_url}/server/load.php"

    now = int(time.time())
    after = now + (hours * 3600)

    payload = {
        "type": "itv",
        "action": "get_epg_info",
        "period": f"{now}-{after}"
    }

    resp = session.post(url, data=payload, headers=headers, timeout=10)

    try:
        data = resp.json()
    except:
        return {}

    epg = data.get("js", {}).get("data", {})
    return epg


# ============================================================================================
# Flask Routes
# ============================================================================================

@app.route("/playlist.m3u")
def playlist():
    try:
        all_channels = []

        if not os.path.exists(MACLIST_FILE):
            return Response("maclist.json not found!", mimetype="text/plain")

        with open(MACLIST_FILE, "r") as f:
            maclist = json.load(f)

        for portal_url, macs in maclist.items():
            for mac in macs:
                try:
                    channels = get_channels(portal_url, mac)

                    for ch in channels:
                        url = get_stream_url(ch.get("cmd", ""))

                        if url:
                            name = ch.get("name", "Unknown")
                            all_channels.append({
                                "name": f"{name} ({mac})",
                                "url": url
                            })
                except Exception as e:
                    print(f"Error: {e}")

        output = "#EXTM3U\n"
        for ch in all_channels:
            output += f"#EXTINF:-1,{ch['name']}\n{ch['url']}\n"

        return Response(output, mimetype="audio/x-mpegurl")

    except Exception as e:
        return Response(f"Error: {e}", mimetype="text/plain")


# ============================================================================================
# EPG XMLTV OUTPUT
# ============================================================================================

@app.route("/epg.xml")
def epg_xml():
    if not os.path.exists(MACLIST_FILE):
        return Response("maclist.json not found!", mimetype="text/plain")

    with open(MACLIST_FILE, "r") as f:
        maclist = json.load(f)

    # XMLTV root
    tv = ET.Element("tv")
    tv.set("source-info-name", "MAC Portal Server")

    channel_ids = set()

    # Combine all portal EPGs
    for portal_url, macs in maclist.items():
        for mac in macs:
            epg_data = get_epg_for_portal(portal_url, mac)

            for ch_id, items in epg_data.items():
                # add channel if not added
                if ch_id not in channel_ids:
                    channel_ids.add(ch_id)

                    ch_elem = ET.SubElement(tv, "channel")
                    ch_elem.set("id", ch_id)

                    name = ET.SubElement(ch_elem, "display-name")
                    name.text = ch_id

                # programme
                for ep in items:
                    start = datetime.datetime.fromtimestamp(int(ep["start"]))
                    end = datetime.datetime.fromtimestamp(int(ep["end"]))

                    start_str = start.strftime("%Y%m%d%H%M%S +0000")
                    end_str = end.strftime("%Y%m%d%H%M%S +0000")

                    prog = ET.SubElement(tv, "programme")
                    prog.set("start", start_str)
                    prog.set("stop", end_str)
                    prog.set("channel", ch_id)

                    title = ET.SubElement(prog, "title")
                    title.text = ep.get("name", "No Title")

                    desc = ET.SubElement(prog, "desc")
                    desc.text = ep.get("descr", "")

    xml_data = ET.tostring(tv, encoding="utf-8", method="xml")

    return Response(xml_data, mimetype="application/xml")


@app.route("/")
def home():
    return "MAC Portal IPTV Server (Playlist + EPG XMLTV) is running!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
