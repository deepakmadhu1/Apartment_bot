import json
import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from filelock import FileLock
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEBUG = os.environ.get("DEBUG", "0") == "1"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GOOGLE_API_KEY     = os.environ.get("GOOGLE_API_KEY", "")
GIST_TOKEN         = os.environ.get("GIST_TOKEN", "")
GIST_ID            = os.environ.get("GIST_ID", "")

# German search URLs for better results
WG_URL = (
    "https://www.wg-gesucht.de/wohnungen-in-Emden.32.2.1.0.html"
    "?offer_filter=1&city_id=32&sort_order=0&noDeact=1&categories%5B%5D=2"
)
WG_URL_1ZIMMER = (
    "https://www.wg-gesucht.de/1-zimmer-wohnungen-in-Emden.32.1.1.0.html"
    "?offer_filter=1&city_id=32&sort_order=0&noDeact=1&categories%5B%5D=1"
)
WG_URL_HAUS = (
    "https://www.wg-gesucht.de/haeuser-in-Emden.32.3.1.0.html"
    "?offer_filter=1&city_id=32&sort_order=0&noDeact=1&categories%5B%5D=3"
)

# Kleinanzeigen — search "Wohnung mieten Emden"
KLEINANZEIGEN_URL = (
    "https://www.kleinanzeigen.de/s-wohnung-mieten/emden/k0c203l3107"
)

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
            headers=_GIST_HEADERS,
            timeout=15,
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
            headers=_GIST_HEADERS,
            json=payload,
            timeout=15,
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
    raw = _gist_read("seen_listings.json", "[]")
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def save_seen(seen: set):
    _push_gist({"seen_listings.json": json.dumps(sorted(list(seen)), indent=2)})


def load_subscribers() -> set:
    raw = _gist_read("subscribers.json", "[]")
    try:
        data = json.loads(raw)
        return set(str(x) for x in data)
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
    r"wohnung gesucht", r"zimmer in wg", r"\bwg-zimmer\b",
    r"\bvermietet\b",  # already rented — skip
]

_BAD_WORDS_KLEINANZEIGEN_EXTRA = [
    r"\bwg\b", r"^zimmer$", r"\bverkauf\b", r"\bkaufen\b",
]


def _compile_patterns(patterns):
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_COMMON_RE   = _compile_patterns(_BAD_WORDS_COMMON)
_KLEINANZ_RE = _compile_patterns(_BAD_WORDS_KLEINANZEIGEN_EXTRA)


def is_good_listing(item, source=""):
    title    = item.get("title", "")
    patterns = _COMMON_RE[:]
    if source == "Kleinanzeigen":
        patterns += _KLEINANZ_RE
    return not any(p.search(title) for p in patterns)


# ---------------------------------------------------------------------------
# Detail scraping — fetch price, size, rooms from listing page
# ---------------------------------------------------------------------------

def scrape_kleinanzeigen_details(url: str) -> dict:
    """Scrape price, size, rooms from a Kleinanzeigen listing page."""
    details = {"price": None, "size": None, "rooms": None}
    try:
        headers  = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Price — look for € symbol near a number
        price_tag = soup.find(attrs={"data-testid": "price-amount"})
        if not price_tag:
            price_tag = soup.find(class_=re.compile(r"price", re.I))
        if price_tag:
            price_text = price_tag.get_text(" ", strip=True)
            price_match = re.search(r"[\d.,]+\s*€|€\s*[\d.,]+", price_text)
            if price_match:
                details["price"] = price_match.group(0).strip()

        # Size and rooms from detail attributes
        for tag in soup.find_all(class_=re.compile(r"addetail|attribute|detail", re.I)):
            text = tag.get_text(" ", strip=True)
            if not details["size"]:
                size_match = re.search(r"(\d+[\.,]?\d*)\s*m²", text)
                if size_match:
                    details["size"] = f"{size_match.group(1)} m²"
            if not details["rooms"]:
                room_match = re.search(r"(\d+[\.,]?\d*)\s*Zimmer", text, re.I)
                if room_match:
                    details["rooms"] = f"{room_match.group(1)} Zimmer"

        # Fallback: search entire page text
        full_text = soup.get_text(" ")
        if not details["price"]:
            price_match = re.search(r"(\d{2,4})\s*€\s*/?\s*(?:Monat|mtl|kalt|warm)?", full_text)
            if price_match:
                details["price"] = f"{price_match.group(1)} €"
        if not details["size"]:
            size_match = re.search(r"(\d+[\.,]?\d*)\s*m²", full_text)
            if size_match:
                details["size"] = f"{size_match.group(1)} m²"
        if not details["rooms"]:
            room_match = re.search(r"(\d+[\.,]?\d*)\s*Zimmer", full_text, re.I)
            if room_match:
                details["rooms"] = f"{room_match.group(1)} Zimmer"

    except Exception as e:
        log.debug(f"Detail scrape failed for {url}: {e}")

    return details


def scrape_wg_details(url: str, page) -> dict:
    """Extract price, size, rooms from already-loaded WG-Gesucht page content."""
    details = {"price": None, "size": None, "rooms": None}
    try:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        full_text = soup.get_text(" ")

        price_match = re.search(r"(\d{2,4})\s*€", full_text)
        if price_match:
            details["price"] = f"{price_match.group(1)} €"

        size_match = re.search(r"(\d+[\.,]?\d*)\s*m²", full_text)
        if size_match:
            details["size"] = f"{size_match.group(1)} m²"

        room_match = re.search(r"(\d+[\.,]?\d*)\s*Zimmer", full_text, re.I)
        if room_match:
            details["rooms"] = f"{room_match.group(1)} Zimmer"

    except Exception as e:
        log.debug(f"WG detail parse failed: {e}")

    return details


# ---------------------------------------------------------------------------
# Gemini AI summary — gemini-2.0-flash (free tier)
# Includes price/size/rooms in prompt for richer summary
# 4 second delay to stay under 15 req/min free limit
# ---------------------------------------------------------------------------

def get_listing_summary(title: str, link: str, source: str, details: dict) -> dict | None:
    if not GOOGLE_API_KEY:
        return None

    time.sleep(4)  # stay under Gemini free tier rate limit

    price = details.get("price") or "unknown"
    size  = details.get("size")  or "unknown"
    rooms = details.get("rooms") or "unknown"

    try:
        prompt = (
            f"You are a helpful assistant for someone apartment hunting in Emden, Germany.\n\n"
            f"Listing details:\n"
            f"- Source: {source}\n"
            f"- Title: {title}\n"
            f"- Price: {price}\n"
            f"- Size: {size}\n"
            f"- Rooms: {rooms}\n"
            f"- Link: {link}\n\n"
            f"Respond ONLY with a valid JSON object (no markdown, no extra text) "
            f"with exactly these two keys:\n"
            f"1. \"summary\": A 2-3 sentence summary IN ENGLISH describing what this listing offers, "
            f"including price, size and rooms if known. Be specific and helpful.\n"
            f"2. \"contact_message\": A short polite message IN GERMAN (3-5 sentences) "
            f"to send to the landlord expressing interest and asking for a viewing appointment.\n\n"
            f"Example: {{\"summary\": \"...\", \"contact_message\": \"...\"}}"
        )

        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
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

def build_listing_message(item: dict, label: str = "🆕 New Apartment Found in Emden"):
    details = item.get("details", {})
    ai      = get_listing_summary(item["title"], item["link"], item["source"], details)

    # Build detail line from scraped data
    detail_parts = []
    if details.get("rooms"):
        detail_parts.append(f"🛏 {details['rooms']}")
    if details.get("size"):
        detail_parts.append(f"📐 {details['size']}")
    if details.get("price"):
        detail_parts.append(f"💶 {details['price']}")
    detail_line = "  |  ".join(detail_parts) if detail_parts else "Details not available"

    summary_line = ""
    if ai and ai.get("summary"):
        summary_line = f"\n📝 <b>Summary:</b> {ai['summary']}"

    text = (
        f"🏠 <b>{label}</b>\n\n"
        f"🔎 <b>Source:</b> {item.get('source', 'Unknown')}\n"
        f"📌 <b>Title:</b> {item['title']}\n"
        f"{detail_line}"
        f"{summary_line}\n"
        f"🔗 <b>Link:</b> {item['link']}"
    )

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
# Scraping — WG-Gesucht (Playwright)
# Scrapes all 3 category URLs: Wohnungen, 1-Zimmer, Häuser
# ---------------------------------------------------------------------------

_WG_LISTING_RE = re.compile(r"-in-Emden\.")


def _scrape_wg_url(page, url: str) -> list:
    """Scrape one WG-Gesucht URL and return raw listing dicts."""
    listings = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        # Dismiss cookie banner
        for selector in [
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Akzeptieren')",
            "button:has-text('Accept')",
            "button:has-text('Agree')",
            "text=Accept all",
        ]:
            try:
                loc = page.locator(selector)
                if loc.count() > 0:
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        if DEBUG:
            fname = url.split("/")[-1].split("?")[0]
            with open(f"debug_wg_{fname}.html", "w", encoding="utf-8") as f:
                f.write(page.content())

        all_links = page.locator("a[href]")
        count     = all_links.count()

        for i in range(min(count, 300)):
            try:
                a         = all_links.nth(i)
                href      = a.get_attribute("href") or ""
                full_link = normalize_link(url, href)

                if not full_link or not _WG_LISTING_RE.search(full_link):
                    continue
                if not re.search(r"/\d+", full_link):
                    continue

                title = " ".join(a.inner_text().strip().split())
                if not title or len(title) < 10 or title.isdigit():
                    continue

                listings.append({
                    "id":      f"wg::{full_link}",
                    "title":   title,
                    "link":    full_link,
                    "source":  "WG-Gesucht",
                    "details": {},
                })
            except Exception:
                continue

    except Exception as e:
        log.error(f"Failed to scrape WG URL {url}: {e}")

    return listings


def get_wggesucht_listings() -> list:
    all_listings = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()

        for url in [WG_URL, WG_URL_1ZIMMER, WG_URL_HAUS]:
            results = _scrape_wg_url(page, url)
            log.info(f"WG-Gesucht scraped {len(results)} from {url.split('?')[0]}")
            all_listings.extend(results)

        browser.close()

    # Deduplicate and filter
    unique, seen_ids = [], set()
    for item in all_listings:
        if item["id"] not in seen_ids and is_good_listing(item, "WG-Gesucht"):
            unique.append(item)
            seen_ids.add(item["id"])

    log.info(f"WG-Gesucht listings after dedup+filter: {len(unique)}")
    return unique


# ---------------------------------------------------------------------------
# Scraping — Kleinanzeigen (requests + BeautifulSoup)
# Also fetches each listing page to get price/size/rooms
# ---------------------------------------------------------------------------

def get_kleinanzeigen_listings() -> list:
    headers  = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(KLEINANZEIGEN_URL, headers=headers, timeout=30)
    response.raise_for_status()

    if DEBUG:
        with open("debug_kleinanzeigen.html", "w", encoding="utf-8") as f:
            f.write(response.text)

    soup     = BeautifulSoup(response.text, "html.parser")
    listings = []

    links = soup.select("a[href*='/s-anzeige/']")
    log.info(f"Kleinanzeigen raw result links: {len(links)}")

    for a in links:
        href      = a.get("href")
        full_link = normalize_link(KLEINANZEIGEN_URL, href)
        if not full_link:
            continue

        title = " ".join(a.get_text(" ", strip=True).split())
        if not title or len(title) < 5:
            continue

        listings.append({
            "id":      f"ka::{full_link}",
            "title":   title,
            "link":    full_link,
            "source":  "Kleinanzeigen",
            "details": {},
        })

    # Deduplicate and filter first
    unique, seen_ids = [], set()
    for item in listings:
        if item["id"] not in seen_ids and is_good_listing(item, "Kleinanzeigen"):
            unique.append(item)
            seen_ids.add(item["id"])

    # Now scrape details for each unique listing
    log.info(f"Kleinanzeigen: scraping details for {len(unique)} listings...")
    for item in unique:
        try:
            item["details"] = scrape_kleinanzeigen_details(item["link"])
            time.sleep(1)  # be polite to the server
        except Exception as e:
            log.debug(f"Could not get details for {item['title']}: {e}")

    log.info(f"Kleinanzeigen listings after filtering: {len(unique)}")
    return unique


# ---------------------------------------------------------------------------
# Aggregate — newest first ordering
# WG-Gesucht returns newest first by default (sort_order=0)
# Kleinanzeigen also returns newest first by default
# So we just keep source order, WG first then Kleinanzeigen
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

    log.info(f"Total unique listings: {len(unique)}")
    return unique


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
                        "✅ You have been unsubscribed from Emden apartment alerts."
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
    # Send newest first — listings are already in newest-first order
    latest = listings[:limit]
    if not latest:
        send_telegram_message(
            chat_id,
            "✅ You are subscribed to Emden apartment alerts.\n\nNo listings found right now."
        )
        return

    send_telegram_message(
        chat_id,
        f"✅ <b>You are now subscribed to Emden apartment alerts!</b>\n\n"
        f"Here are the latest <b>{len(latest)}</b> listings, newest first:"
    )

    for item in latest:
        text, markup = build_listing_message(item, label="🏠 Current Apartment in Emden")
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
    except Exception:
        log.warning("Another instance is already running. Exiting.")


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
