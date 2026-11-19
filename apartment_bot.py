import json
import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from filelock import FileLock

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEBUG = os.environ.get("DEBUG", "0") == "1"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GOOGLE_API_KEY     = os.environ.get("GOOGLE_API_KEY", "")
GIST_TOKEN         = os.environ.get("GIST_TOKEN", "")
GIST_ID            = os.environ.get("GIST_ID", "")

WG_BASE = "https://www.wg-gesucht.de"

# WG-Gesucht internal API — returns JSON listings directly, no Playwright needed
# city_id=8 = Berlin
# category: 0=WG-Zimmer, 1=1-Zimmer, 2=Wohnung, 3=Haus
WG_API_URL = "https://www.wg-gesucht.de/api/v1/ads"
WG_CATEGORIES = {
    0: "WG-Zimmer",
    1: "1-Zimmer-Wohnung",
    2: "Wohnung",
    3: "Haus",
}

KLEINANZEIGEN_WOHNUNG_URL = "https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/k0c203l3331"
KLEINANZEIGEN_WG_URL      = "https://www.kleinanzeigen.de/s-wg-zimmer/berlin/k0c220l3331"

LOCK_FILE = "bot.lock"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.DEBUG if DEBUG else logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub Gist persistence
# ---------------------------------------------------------------------------

_GIST_HEADERS = {
    "Authorization": f"token {GIST_TOKEN}",
    "Accept": "application/vnd.github+json",
}
_gist_cache: dict = {}


def _fetch_gist() -> dict:
    global _gist_cache
    if _gist_cache:
        return _gist_cache
    if not GIST_TOKEN or not GIST_ID:
        log.warning("GIST_TOKEN or GIST_ID not set.")
        return {}
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_GIST_HEADERS, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        _gist_cache = {
            name: file_data.get("content", "")
            for name, file_data in data.get("files", {}).items()
        }
        log.info("Gist fetched successfully.")
        return _gist_cache
    except Exception as e:
        log.error(f"Failed to fetch Gist: {e}")
        return {}


def _push_gist(files: dict):
    if not GIST_TOKEN or not GIST_ID:
        return
    payload = {"files": {name: {"content": content} for name, content in files.items()}}
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_GIST_HEADERS, json=payload, timeout=15,
        )
        r.raise_for_status()
        global _gist_cache
        _gist_cache = {}
