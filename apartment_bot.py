import json
import os
import re
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

WG_URL = (
    "https://www.wg-gesucht.de/1-zimmer-wohnungen-und-wohnungen-und-haeuser-in-Emden.32.1+2+3.1.0.html"
    "?offer_filter=1&city_id=32&sort_order=0&noDeact=1"
    "&categories%5B%5D=1&categories%5B%5D=2&categories%5B%5D=3"
)
KLEINANZEIGEN_URL = (
    "https://www.kleinanzeigen.de/s-wohnung-mieten/26721/wohnung-in-emden/"
    "k0c203l3107+options:wohnung_mieten.built_in_kitchen_b"
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
# Replaces all local file reads/writes — Gist is the permanent database.
# ---------------------------------------------------------------------------

_GIST_HEADERS = {
    "Authorization": f"token {GIST_TOKEN}",
    "Accept": "application/vnd.github+json",
}

_gist_cache: dict = {}   # in-memory cache so we only fetch the Gist once per run


def _fetch_gist() -> dict:
    """Fetch the full Gist and cache it. Returns dict of filename -> content."""
    global _gist_cache
    if _gist_cache:
        return _gist_cache

    if not GIST_TOKEN or not GIST_ID:
        log.warning("GIST_TOKEN or GIST_ID not set — falling back to local files.")
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
    """
    Push updated files to the Gist.
    files = { "filename.json": "content string", ... }
    """
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
    """Read a single file from the Gist."""
    return _fetch_gist().get(filename, default)


# ------ seen listings -------------------------------------------------------

def load_seen() -> set:
    raw = _gist_read("seen_listings.json", "[]")
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def save_seen(seen: set):
    _push_gist({"seen_listings.json": json.dumps(sorted(list(seen)), indent=2)})


# ------ subscribers ---------------------------------------------------------

def load_subscribers() -> set:
    raw = _gist_read("subscribers.json", "[]")
    try:
        data = json.loads(raw)
        return set(str(x) for x in data)
    except Exception:
        return set()


def save_subscribers(subscribers: set):
    _push_gist({"subscribers.json": json.dumps(sorted(list(subscribers)), indent=2)})


# ------ telegram offset -----------------------------------------------------

def load_offset():
    raw = _gist_read("telegram_offset.txt", "").strip()
    try:
        return int(raw) if raw else None
    except Exception:
        return None


def save_offset(offset: int):
    _push_gist({"telegram_offset.txt": str(offset)})


# ------ contact messages ----------------------------------------------------
# Stored as a single JSON object in the Gist: { safe_id: message, ... }

def _load_contact_messages() -> dict:
    raw = _gist_read("contact_messages.json", "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_contact_message(safe_id: str, message: str):
    all_msgs = _load_contact_messages()
    all_msgs[safe_id] = message
    # Keep only the most recent 200 to avoid the file growing forever
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
# Gemini AI summary (free tier)
# ---------------------------------------------------------------------------

def get_listing_summary(title: str, link: str, source: str) -> dict | None:
    if not GOOGLE_API_KEY:
        return None

    try:
        prompt = (
            f"You are a helpful assistant for someone apartment hunting in Emden, Germany.\n\n"
            f"Listing details:\n"
            f"- Source: {source}\n"
            f"- Title: {title}\n"
            f"- Link: {link}\n\n"
            f"Respond ONLY with a valid JSON object (no markdown, no extra text) "
            f"with exactly these two keys:\n"
            f"1. \"summary\": A 1-2 sentence summary of what this listing likely offers.\n"
            f"2. \"contact_message\": A short polite message in German (3-5 sentences) "
            f"to send to the landlord to express interest and ask for a viewing.\n\n"
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

def build_listing_message(item: dict, label: str = "New apartment found in Emden"):
    ai = get_listing_summary(item["title"], item["link"], item["source"])

    summary_line = ""
    if ai and ai.get("summary"):
        summary_line = f"\n📝 <b>Summary:</b> {ai['summary']}"

    text = (
        f"🏠 <b>{label}</b>\n\n"
        f"🔎 <b>Source:</b> {item.get('source', 'Unknown')}\n"
        f"📌 <b>Title:</b> {item['title']}"
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
# ---------------------------------------------------------------------------

def get_wggesucht_listings():
    listings = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()
        page.goto(WG_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        for selector in [
            "text=Accept", "text=I agree", "text=Agree", "text=Accept all",
            "button:has-text('Accept')", "button:has-text('Agree')",
        ]:
            try:
                loc = page.locator(selector)
                if loc.count() > 0:
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                pass

        if DEBUG:
            with open("debug_wg.html", "w", encoding="utf-8") as f:
                f.write(page.content())

        links = page.locator("a[href*='-in-Emden.'][href*='.html']")
        count = links.count()
        log.info(f"WG-Gesucht Emden links found: {count}")

        for i in range(min(count, 100)):
            try:
                a         = links.nth(i)
                href      = a.get_attribute("href")
                title     = " ".join(a.inner_text().strip().split())
                full_link = normalize_link(WG_URL, href)

                if not full_link or not title or "-in-Emden." not in full_link:
                    continue

                listings.append({
                    "id": f"wg::{full_link}", "title": title,
                    "link": full_link, "source": "WG-Gesucht",
                })
            except Exception:
                continue

        browser.close()

    unique, seen_ids = [], set()
    for item in listings:
        if item["id"] not in seen_ids and is_good_listing(item, "WG-Gesucht"):
            unique.append(item)
            seen_ids.add(item["id"])
    return unique

# ---------------------------------------------------------------------------
# Scraping — Kleinanzeigen (requests + BeautifulSoup)
# ---------------------------------------------------------------------------

def get_kleinanzeigen_listings():
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
        if not title:
            continue
        listings.append({
            "id": f"ka::{full_link}", "title": title,
            "link": full_link, "source": "Kleinanzeigen",
        })

    unique, seen_ids = [], set()
    for item in listings:
        if item["id"] not in seen_ids and is_good_listing(item, "Kleinanzeigen"):
            unique.append(item)
            seen_ids.add(item["id"])
    return unique

# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def get_all_listings():
    all_listings = []

    try:
        wg = get_wggesucht_listings()
        log.info(f"WG-Gesucht listings: {len(wg)}")
        all_listings.extend(wg)
    except Exception as e:
        log.error(f"WG-Gesucht failed: {e}")

    try:
        ka = get_kleinanzeigen_listings()
        log.info(f"Kleinanzeigen listings: {len(ka)}")
        all_listings.extend(ka)
    except Exception as e:
        log.error(f"Kleinanzeigen failed: {e}")

    unique, seen_ids = [], set()
    for item in all_listings:
        if item["id"] not in seen_ids:
            unique.append(item)
            seen_ids.add(item["id"])
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
                    send_telegram_message(chat_id_str, "You have been unsubscribed from apartment alerts.")
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
            "You are subscribed to Emden apartment alerts.\n\nNo listings found right now."
        )
        return

    send_telegram_message(
        chat_id,
        f"✅ You are now subscribed to Emden apartment alerts.\n\nHere are the latest {len(latest)} listings:"
    )
    for item in latest:
        text, markup = build_listing_message(item, label="Current apartment in Emden")
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
