from flask import Flask, Response, request
import requests, json, random, os, gzip, boto3
from urllib.parse import quote_plus, urlparse
from datetime import datetime
from dateutil.parser import parse

app = Flask(__name__)

# --------------------------
# Config
# --------------------------
MACLIST_FILE = "maclist.json"
GUIDE_FILE = "guide.xml"
USER_AGENT = "Mozilla/5.0 (Android) IPTV/1.0"

# Cloudflare R2 config
ACCOUNT_ID = "145ef3f7a9832804bef0e31548db8a83"
R2_ACCESS_KEY = "YOUR_ACCESS_KEY"
R2_SECRET_KEY = "YOUR_SECRET_KEY"
R2_BUCKET_NAME = "stbemu"
R2_OBJECT_KEY = "stbemu.csv.gz"
R2_ENDPOINT_URL = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"

ALLOWED_COUNTRIES = {
    "UK": ["UK", "United Kingdom", "Britain", "England"],
    "DE": ["DE", "Germany", "Deutschland"],
    "TH": ["TH", "Thailand", "Thai", "ไทย"],
    "AT": ["AT", "Austria", "Österreich", "Osterreich"],
    "CH": ["CH", "Switzerland", "Swiss", "Schweiz", "Suisse", "Svizzera"],
}

# --------------------------
# Utilities
# --------------------------
def update_maclist():
    """ดึง MAC list จาก R2 และสร้าง maclist.json"""
    s3_client = boto3.client("s3",
                             aws_access_key_id=R2_ACCESS_KEY,
                             aws_secret_access_key=R2_SECRET_KEY,
                             endpoint_url=R2_ENDPOINT_URL)
    response = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=R2_OBJECT_KEY)
    gzip_file = gzip.GzipFile(fileobj=response["Body"])
    csv_content = gzip_file.read().decode("utf-8").splitlines()

    alllist = {}
    timestamp = datetime.timestamp(datetime.now())

    for line in csv_content:
        if "," in line:
            url, mac, *expire_parts = line.split(",")
            try:
                expire_time = datetime.timestamp(parse(",".join(expire_parts), fuzzy=True))
                if timestamp >= expire_time: 
                    continue
            except:
                pass
            url = url.strip().rstrip("/").replace(":80/c", "/c")
            if not url.endswith("/c"): url += "/c"
            alllist.setdefault(url, [])
            if mac not in alllist[url]:
                alllist[url].append(mac)

    with open(MACLIST_FILE, "w") as f:
        json.dump(alllist, f, indent=4)
    app.logger.info("MAC list updated")


def update_guide_xml():
    """สร้าง guide.xml ตัวอย่างจาก TVDigital"""
    epg = ['<?xml version="1.0" encoding="UTF-8" ?>\n<tv>\n']
    try:
        tvdDE_header = {'user-agent': 'PIT-TVdigital-Android/14', 'accept-encoding': 'gzip'}
        tvdDE_channels = requests.get(
            'https://mobile.tvdigital.de/appdata?appVersion=50&bundleId=de.funke.tvdigital',
            headers=tvdDE_header, timeout=10
        ).json()["channels"]
        for ch in tvdDE_channels:
            epg.append(f'  <channel id="{ch["id"]}"><display-name lang="de">{ch["name"]}</display-name></channel>\n')
        epg.append('</tv>')
        with open(GUIDE_FILE, "w", encoding="utf-8") as f:
            f.writelines(epg)
        app.logger.info("guide.xml created")
    except Exception as e:
        app.logger.error(f"update_guide_xml error: {e}")

# --------------------------
# Flask Routes (เหมือนเดิม)
# --------------------------
# ... ใส่โค้ด Flask /playlist.m3u, /play, / ตามที่คุณมีเดิม ...

# --------------------------
# Startup
# --------------------------
if __name__ == "__main__":
    # อัปเดต maclist และ guide ก่อนเริ่ม Flask
    update_maclist()
    update_guide_xml()
    app.run(host="0.0.0.0", port=5000, threaded=True)
