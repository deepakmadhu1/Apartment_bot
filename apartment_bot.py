import json
import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from filelock import FileLock, Timeout

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
# Only private rooms in shared flats and self-contained studios.
WG_CATEGORIES = {
    0: "WG-Zimmer",
    1: "1-Zimmer-Wohnung",
}
MAX_RENT_EUR = 600  # Monthly advertised rent; utilities may not be included.

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
        log.info(f"Gist updated: {list(files.keys())}")
    except Exception as e:
        log.error(f"Failed to update Gist: {e}")


def _gist_read(filename: str, default: str = "") -> str:
    return _fetch_gist().get(filename, default)


def load_seen() -> set:
    raw = _gist_read("seen_listings_berlin.json", "[]")
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def save_seen(seen: set):
    _push_gist({"seen_listings_berlin.json": json.dumps(sorted(list(seen)), indent=2)})


def load_subscribers() -> set:
    raw = _gist_read("subscribers.json", "[]")
    try:
        return set(str(x) for x in json.loads(raw))
    except Exception:
        return set()


def save_subscribers(subscribers: set):
    _push_gist({"subscribers.json": json.dumps(sorted(list(subscribers)), indent=2)})


def load_offset():
    raw = _gist_read("telegram_offset.txt", "").strip()
    try:
        return int(raw) if raw else None
    except Exception:
        return None


def save_offset(offset: int):
    _push_gist({"telegram_offset.txt": str(offset)})


def _load_contact_messages() -> dict:
    raw = _gist_read("contact_messages.json", "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_contact_message(safe_id: str, message: str):
    all_msgs = _load_contact_messages()
    all_msgs[safe_id] = message
    if len(all_msgs) > 200:
        keys = list(all_msgs.keys())
        all_msgs = {k: all_msgs[k] for k in keys[-200:]}
    _push_gist({"contact_messages.json": json.dumps(all_msgs, indent=2, ensure_ascii=False)})


def _load_contact_message(safe_id: str):
    return _load_contact_messages().get(safe_id)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def telegram_api(method, params=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def send_telegram_message(chat_id, message, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def normalize_link(base_url, href):
    if not href:
        return None
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        domain = "/".join(base_url.split("/")[:3])
        return domain + href
    return None


_BAD_WORDS_COMMON = [
    r"\bumzug\b", r"\btransport\b", r"\bgesuch\b",
    r"\bsucht\b", r"\bsuche\b", r"\btausch\b",
    r"wohnung gesucht", r"\bvermietet\b",
]
_BAD_WORDS_KA_EXTRA = [
    r"\bverkauf\b", r"\bzu verkaufen\b", r"\bkaufen\b",
]


def _compile_patterns(patterns):
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_COMMON_RE = _compile_patterns(_BAD_WORDS_COMMON)
_KA_RE     = _compile_patterns(_BAD_WORDS_KA_EXTRA)


def is_good_listing(item, source=""):
    title    = item.get("title", "")
    patterns = _COMMON_RE[:]
    if source == "Kleinanzeigen":
        patterns += _KA_RE
    return not any(p.search(title) for p in patterns)


def rent_euros(price):
    """Extract a monthly EUR amount from the existing listing details."""
    if isinstance(price, (int, float)):
        return float(price)
    if not price:
        return None
    match = re.search(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*€", str(price))
    if not match:
        return None
    raw = match.group(1)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    return float(raw)


def in_budget(item):
    """Only alert listings with a detectable advertised monthly price <= €600."""
    amount = rent_euros(item.get("details", {}).get("price"))
    return amount is not None and amount <= MAX_RENT_EUR


def is_studio(item):
    """Reject multi-room listings from Kleinanzeigen's broad Wohnung category."""
    if item.get("category") != "1-Zimmer-Wohnung":
        return True
    if item.get("source") == "WG-Gesucht":
        return True  # Dedicated WG-Gesucht category 1
    title = item.get("title", "")
    return bool(re.search(
        r"\b(?:1[ -]?(?:zimmer|zi\.?|raum)|ein[ -]?zimmer|"
        r"studio|single[ -]?apartment|1[ -]?room|"
        r"1[ -]?bed[ -]?room|1[ -]?zimmerwohnung)\b",
        title, re.I,
    ))


def property_emoji(category: str) -> str:
    return {
        "WG-Zimmer":        "🛋",
        "1-Zimmer-Wohnung": "🏢",
        "Wohnung":          "🏠",
        "Haus":             "🏡",
    }.get(category, "🏠")


# ---------------------------------------------------------------------------
# WG-Gesucht — JSON API (no Playwright, no bot detection)
# ---------------------------------------------------------------------------

def get_wggesucht_listings() -> list:
    """
    Fetches listings from WG-Gesucht's internal API.
    Falls back to HTML scraping with requests if API fails.
    """
    all_listings = []
    headers = {
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept":       "application/json, text/plain, */*",
        "Referer":      "https://www.wg-gesucht.de/",
        "X-Requested-With": "XMLHttpRequest",
    }

    for cat_id, cat_name in WG_CATEGORIES.items():
        try:
            params = {
                "city_id":      8,        # Berlin
                "category":     cat_id,
                "rent_type":    0,         # all rent types
                "noDeact":      1,
                "offer_filter": 1,
                "sort_order":   0,         # newest first
                "page_size":    30,
            }
            r = requests.get(
                WG_API_URL,
                params=params,
                headers=headers,
                timeout=20,
            )

            if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
                data  = r.json()
                ads   = data.get("_embedded", {}).get("ads", []) or data.get("ads", [])
                log.info(f"WG-Gesucht API [{cat_name}]: {len(ads)} ads")

                for ad in ads:
                    ad_id    = ad.get("id") or ad.get("ad_id", "")
                    title    = ad.get("title") or ad.get("headline", "")
                    url_path = ad.get("url") or f"/wg-zimmer-und-1-zimmer-wohnungen-und-wohnungen-und-haeuser-in-Berlin.8.{cat_id}.{ad_id}.html"
                    full_url = WG_BASE + url_path if url_path.startswith("/") else url_path

                    price = None
                    rent  = ad.get("rent") or ad.get("costs", {}).get("rent_including_utilities_costs")
                    if rent:
                        price = f"{rent} €/Monat"

                    size  = None
                    sqm   = ad.get("size") or ad.get("living_size")
                    if sqm:
                        size = f"{sqm} m²"

                    rooms = None
                    rm    = ad.get("wg_size") or ad.get("number_of_rooms")
                    if rm:
                        rooms = f"{rm} Zimmer"

                    if not title or not ad_id:
                        continue

                    all_listings.append({
                        "id":       f"wg::{full_url}",
                        "title":    title.strip(),
                        "link":     full_url,
                        "source":   "WG-Gesucht",
                        "category": cat_name,
                        "details":  {"price": price, "size": size, "rooms": rooms},
                    })
            else:
                # API didn't return JSON — fall back to HTML scraping
                log.warning(f"WG-Gesucht API [{cat_name}] returned {r.status_code}, trying HTML fallback")
                fallback = _wg_html_fallback(cat_id, cat_name, headers)
                all_listings.extend(fallback)

        except Exception as e:
            log.error(f"WG-Gesucht [{cat_name}] failed: {e}")

    # Deduplicate and filter
    unique, seen_ids = [], set()
    for item in all_listings:
        if item["id"] not in seen_ids and is_good_listing(item, "WG-Gesucht"):
            unique.append(item)
            seen_ids.add(item["id"])

    log.info(f"WG-Gesucht total after dedup+filter: {len(unique)}")
    return unique


def _wg_html_fallback(cat_id: int, cat_name: str, headers: dict) -> list:
    """HTML scrape fallback using requests (no Playwright)."""
    listings = []
    cat_slug = {
        0: "wg-zimmer",
        1: "1-zimmer-wohnungen",
        2: "wohnungen",
        3: "haeuser",
    }.get(cat_id, "wohnungen")

    url = (
        f"{WG_BASE}/{cat_slug}-in-Berlin.8.{cat_id}.1.0.html"
        f"?offer_filter=1&city_id=8&sort_order=0&noDeact=1&categories%5B%5D={cat_id}"
    )

    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Match real listing URLs: /xxx-in-Berlin.8.CAT_ID.LISTING_ID.html
        pattern = re.compile(
            rf"/{cat_slug}-in-Berlin\.8\.{cat_id}\.(\d{{5,}})\.html",
            re.IGNORECASE,
        )

        seen_hrefs = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].split("?")[0]
            m    = pattern.match(href)
            if not m or href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            full_link = WG_BASE + href
            title     = " ".join(a.get_text(" ", strip=True).split())
            if not title or len(title) < 5:
                parent = a.find_parent()
                if parent:
                    title = " ".join(parent.get_text(" ", strip=True).split())[:120]
            if not title or len(title) < 5:
                title = f"{cat_name} in Berlin"

            listings.append({
                "id":       f"wg::{full_link}",
                "title":    title,
                "link":     full_link,
                "source":   "WG-Gesucht",
                "category": cat_name,
                "details":  {},
            })

        log.info(f"WG-Gesucht HTML fallback [{cat_name}]: {len(listings)} listings")

    except Exception as e:
        log.error(f"WG-Gesucht HTML fallback [{cat_name}] failed: {e}")

    return listings


# ---------------------------------------------------------------------------
# Kleinanzeigen — requests + BeautifulSoup
# ---------------------------------------------------------------------------

def scrape_kleinanzeigen_details(url: str) -> dict:
    details = {"price": None, "size": None, "rooms": None}
    try:
        headers  = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        full_text = BeautifulSoup(response.text, "html.parser").get_text(" ")

        price_match = re.search(r"(\d{2,4}(?:[.,]\d{1,2})?)\s*€", full_text)
        if price_match:
            details["price"] = f"{price_match.group(1)} €/Monat"

        size_match = re.search(r"(\d+[\.,]?\d*)\s*m²", full_text)
        if size_match:
            details["size"] = f"{size_match.group(1)} m²"

        room_match = re.search(r"(\d+[\.,]?\d*)\s*Zimmer", full_text, re.I)
        if room_match:
            details["rooms"] = f"{room_match.group(1)} Zimmer"

    except Exception as e:
        log.debug(f"Detail scrape failed for {url}: {e}")
    return details


def _scrape_kleinanzeigen_url(url: str, category: str) -> list:
    listings = []
    try:
        headers  = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup  = BeautifulSoup(response.text, "html.parser")
        links = soup.select("a[href*='/s-anzeige/']")
        log.info(f"Kleinanzeigen [{category}]: {len(links)} raw links")

        for a in links:
            full_link = normalize_link(url, a.get("href"))
            if not full_link:
                continue
            title = " ".join(a.get_text(" ", strip=True).split())
            if not title or len(title) < 5:
                continue
            listings.append({
                "id":       f"ka::{full_link}",
                "title":    title,
                "link":     full_link,
                "source":   "Kleinanzeigen",
                "category": category,
                "details":  {},
            })
    except Exception as e:
        log.error(f"Kleinanzeigen scrape failed for {url}: {e}")
    return listings


def get_kleinanzeigen_listings() -> list:
    all_listings = []
    for url, category in [
        (KLEINANZEIGEN_WOHNUNG_URL, "1-Zimmer-Wohnung"),
        (KLEINANZEIGEN_WG_URL,      "WG-Zimmer"),
    ]:
        all_listings.extend(_scrape_kleinanzeigen_url(url, category))

    unique, seen_ids = [], set()
    for item in all_listings:
        if item["id"] not in seen_ids and is_good_listing(item, "Kleinanzeigen"):
            unique.append(item)
            seen_ids.add(item["id"])

    log.info(f"Kleinanzeigen: scraping details for {len(unique)} listings...")
    for item in unique:
        try:
            item["details"] = scrape_kleinanzeigen_details(item["link"])
            time.sleep(1)
        except Exception as e:
            log.debug(f"Details failed for {item['title']}: {e}")

    unique = [item for item in unique if is_studio(item)]
    log.info(f"Kleinanzeigen relevant shared-room/studio listings: {len(unique)}")
    log.info(f"Kleinanzeigen total: {len(unique)}")
    return unique


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def get_all_listings() -> list:
    all_listings = []

    try:
        wg = get_wggesucht_listings()
        all_listings.extend(wg)
    except Exception as e:
        log.error(f"WG-Gesucht failed: {e}")

    try:
        ka = get_kleinanzeigen_listings()
        all_listings.extend(ka)
    except Exception as e:
        log.error(f"Kleinanzeigen failed: {e}")

    unique, seen_ids = [], set()
    for item in all_listings:
        if item["id"] not in seen_ids:
            unique.append(item)
            seen_ids.add(item["id"])

    # Final filter applies to both sources and to initial Telegram /start messages.
    # Missing/unparseable prices are excluded rather than risking > €600 alerts.
    relevant = [item for item in unique if is_studio(item) and in_budget(item)]
    log.info(f"Total listings: {len(unique)}; WG/studio listings at or below €{MAX_RENT_EUR}: {len(relevant)}")
    return relevant


# ---------------------------------------------------------------------------
# Gemini AI summary
# ---------------------------------------------------------------------------

def get_listing_summary(title: str, link: str, source: str, category: str, details: dict) -> dict | None:
    if not GOOGLE_API_KEY:
        return None

    time.sleep(4)

    price = details.get("price") or "not listed"
    size  = details.get("size")  or "not listed"
    rooms = details.get("rooms") or "not listed"
    is_wg = category == "WG-Zimmer"

    try:
        prompt = (
            f"You are a helpful assistant for someone apartment hunting in Berlin, Germany.\n\n"
            f"Listing:\n"
            f"- Type: {category}\n"
            f"- Source: {source}\n"
            f"- Title: {title}\n"
            f"- Price: {price}\n"
            f"- Size: {size}\n"
            f"- Rooms: {rooms}\n"
            f"- Link: {link}\n\n"
            f"Respond ONLY with a valid JSON object with exactly these two keys:\n"
            f"1. \"summary\": 2-3 sentences IN ENGLISH describing the listing. "
            f"Mention type ({'shared room in a flat' if is_wg else 'apartment/house'}), "
            f"price, size, rooms if known.\n"
            f"2. \"contact_message\": 3-5 sentences IN GERMAN to send to the landlord "
            f"expressing interest and requesting a viewing.\n\n"
            f"Example: {{\"summary\": \"...\", \"contact_message\": \"...\"}}"
        )

        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={GOOGLE_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
        )
        response.raise_for_status()
        data     = response.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        clean    = raw_text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(clean)

    except Exception as e:
        log.warning(f"Gemini summary failed for '{title}': {e}")
        return None


# ---------------------------------------------------------------------------
# Telegram message formatting
# ---------------------------------------------------------------------------

def build_listing_message(item: dict, label: str = "🆕 New Listing Found in Berlin"):
    details  = item.get("details", {})
    category = item.get("category", item.get("source", ""))
    ai       = get_listing_summary(
        item["title"], item["link"], item["source"], category, details
    )

    emoji = property_emoji(category)

    detail_parts = []
    if details.get("rooms"):
        detail_parts.append(f"🛏 {details['rooms']}")
    if details.get("size"):
        detail_parts.append(f"📐 {details['size']}")
    if details.get("price"):
        detail_parts.append(f"💶 {details['price']}")
    detail_line = "  |  ".join(detail_parts) if detail_parts else ""

    summary_line = ""
    if ai and ai.get("summary"):
        summary_line = f"\n📝 <b>Summary:</b> {ai['summary']}"

    text = (
        f"{emoji} <b>{label}</b>\n\n"
        f"🔎 <b>Source:</b> {item.get('source', 'Unknown')}\n"
        f"🏷 <b>Type:</b> {category}\n"
        f"📌 <b>Title:</b> {item['title']}\n"
    )
    if detail_line:
        text += f"{detail_line}\n"
    text += f"{summary_line}\n🔗 <b>Link:</b> {item['link']}"

    reply_markup = None
    if ai and ai.get("contact_message"):
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", item["id"])[:50]
        _save_contact_message(safe_id, ai["contact_message"])
        reply_markup = {
            "inline_keyboard": [[
                {"text": "✉️ Get message to send landlord", "callback_data": f"cm::{safe_id}"}
            ]]
        }

    return text, reply_markup


# ---------------------------------------------------------------------------
# Telegram inline button callback handling
# ---------------------------------------------------------------------------

def handle_callback_queries(results: list):
    for item in results:
        cq = item.get("callback_query")
        if not cq:
            continue
        callback_id = cq["id"]
        chat_id     = cq["message"]["chat"]["id"]
        data        = cq.get("data", "")
        try:
            telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
        except Exception:
            pass
        if data.startswith("cm::"):
            safe_id = data[4:]
            msg     = _load_contact_message(safe_id)
            reply   = (
                f"✉️ <b>Ready-to-send message (German):</b>\n\n{msg}"
                if msg else
                "Sorry, the contact message for this listing is no longer available."
            )
            try:
                send_telegram_message(chat_id, reply)
            except Exception as e:
                log.warning(f"Failed to send contact message to {chat_id}: {e}")


# ---------------------------------------------------------------------------
# Subscriber management
# ---------------------------------------------------------------------------

def update_subscribers_from_telegram():
    subscribers = load_subscribers()
    newly_added = []
    offset      = load_offset()

    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset

    data = telegram_api("getUpdates", params=params)
    if not data.get("ok"):
        log.warning("Could not read Telegram updates.")
        return subscribers, newly_added

    results = data.get("result", [])
    log.info(f"Telegram updates found: {len(results)}")
    handle_callback_queries(results)

    new_offset = offset
    for item in results:
        update_id = item.get("update_id")
        if update_id is not None:
            new_offset = update_id + 1

        message = item.get("message") or item.get("edited_message")
        if not message:
            continue

        chat        = message.get("chat", {})
        chat_id     = chat.get("id")
        text        = (message.get("text") or "").strip()

        if chat_id is None or chat.get("type") != "private":
            continue

        chat_id_str = str(chat_id)

        if text.lower() in ["/stop", "stop", "unsubscribe"]:
            if chat_id_str in subscribers:
                subscribers.remove(chat_id_str)
                try:
                    send_telegram_message(
                        chat_id_str,
                        "✅ You have been unsubscribed from Berlin apartment alerts."
                    )
                except Exception:
                    pass
                log.info(f"Removed subscriber: {chat_id_str}")
            continue

        if chat_id_str not in subscribers:
            subscribers.add(chat_id_str)
            newly_added.append(chat_id_str)
            log.info(f"Added subscriber: {chat_id_str}")

    save_subscribers(subscribers)
    if new_offset is not None:
        save_offset(new_offset)

    return subscribers, newly_added


def send_current_listings_to_new_user(chat_id, listings, limit=10):
    latest = listings[:limit]
    if not latest:
        send_telegram_message(
            chat_id,
            "✅ You are subscribed to Berlin apartment alerts.\n\nNo listings found right now."
        )
        return

    send_telegram_message(
        chat_id,
        f"✅ <b>You are now subscribed to Berlin apartment alerts!</b>\n\n"
        f"Searching: 🛋 WG-Zimmer  |  🏢 1-Zimmer  |  🏠 Wohnungen  |  🏡 Häuser\n"
        f"Sources: WG-Gesucht + Kleinanzeigen\n\n"
        f"Here are the latest <b>{len(latest)}</b> listings, newest first:"
    )
    for item in latest:
        text, markup = build_listing_message(item, label="🏠 Current Listing in Berlin")
        try:
            send_telegram_message(chat_id, text, reply_markup=markup)
        except Exception as e:
            log.warning(f"Failed to send listing to {chat_id}: {e}")


def send_listing_to_all(subscribers, listing):
    if not subscribers:
        log.info("No subscribers to notify.")
        return
    text, markup = build_listing_message(listing)
    for chat_id in subscribers:
        try:
            send_telegram_message(chat_id, text, reply_markup=markup)
            log.info(f"Sent to {chat_id}: {listing['title']}")
        except Exception as e:
            log.warning(f"Failed to send to {chat_id}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    lock = FileLock(LOCK_FILE, timeout=0)
    try:
        with lock:
            _run()
    except Timeout:
        log.warning("Another instance is already running. Exiting.")
    except Exception as e:
        log.exception("Bot failed: %s", e)
        raise


def _run():
    listings = get_all_listings()
    log.info(f"Total listings scraped: {len(listings)}")

    subscribers, newly_added = update_subscribers_from_telegram()
    log.info(f"Subscribers: {len(subscribers)} | New this run: {len(newly_added)}")

    for chat_id in newly_added:
        try:
            send_current_listings_to_new_user(chat_id, listings, limit=10)
        except Exception as e:
            log.error(f"Failed initial send to {chat_id}: {e}")

    seen         = load_seen()
    new_listings = [item for item in listings if item["id"] not in seen]
    log.info(f"Already seen: {len(seen)} | New: {len(new_listings)}")

    if not new_listings:
        log.info("No new listings found.")
        return

    for item in new_listings:
        send_listing_to_all(subscribers, item)
        seen.add(item["id"])

    save_seen(seen)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.critical(f"Bot crashed: {e}", exc_info=True)
