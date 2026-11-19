"""Berlin apartment alerts: WG-Gesucht and Kleinanzeigen -> Telegram.

Configuration is supplied through GitHub Actions environment/secrets.
Persistence uses a GitHub Gist so scheduled runners can remember past listings.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("berlin_apartment_bot")

CITY = "Berlin"
WG_CITY_ID = 8
WG_BASE = "https://www.wg-gesucht.de"
WG_API = WG_BASE + "/api/v1/ads"
WG_CATEGORIES = {0: "WG-Zimmer", 1: "1-Zimmer-Wohnung", 2: "Wohnung", 3: "Haus"}
KA_SEARCHES = {
    "Wohnung": "https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/k0c203l3331",
    "WG-Zimmer": "https://www.kleinanzeigen.de/s-wg-zimmer/berlin/k0c220l3331",
}
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GIST_TOKEN = os.getenv("GIST_TOKEN", "").strip()
GIST_ID = os.getenv("GIST_ID", "").strip()
MAX_RENT = int(os.getenv("MAX_RENT", "0"))
MAX_PER_RUN = max(1, int(os.getenv("MAX_PER_RUN", "25")))
INITIAL_SEND = os.getenv("INITIAL_SEND", "true").lower() == "true"
ALLOWED_CHAT_IDS = {s.strip() for s in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if s.strip()}
SEEN_FILE = "seen_listings_berlin.json"  # Do not reuse Emden's seen history
SUBSCRIBERS_FILE = "subscribers.json"  # Preserve existing bot subscriptions
OFFSET_FILE = "telegram_offset.txt"

HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
})


def get(url: str, *, params: dict | None = None) -> requests.Response:
    response = HTTP.get(url, params=params, timeout=25)
    response.raise_for_status()
    return response


def gist_load() -> dict[str, str] | None:
    """None means unavailable; do not send alerts without reliable state."""
    if not GIST_ID or not GIST_TOKEN:
        log.error("GIST_ID and GIST_TOKEN are required for persistent deduplication")
        return None
    try:
        response = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"Bearer {GIST_TOKEN}", "Accept": "application/vnd.github+json"},
            timeout=25,
        )
        response.raise_for_status()
        return {name: file.get("content", "") for name, file in response.json().get("files", {}).items()}
    except (requests.RequestException, ValueError) as exc:
        log.error("Gist read failed: %s", exc)
        return None


def gist_save(changes: dict[str, str]) -> bool:
    try:
        response = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"Bearer {GIST_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"files": {name: {"content": value} for name, value in changes.items()}},
            timeout=25,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Gist write failed: %s", exc)
        return False


def json_state(files: dict[str, str], filename: str, default: Any) -> Any:
    try:
        return json.loads(files.get(filename, json.dumps(default)))
    except (ValueError, TypeError):
        log.warning("Invalid %s; using default", filename)
        return default


def telegram(method: str, **params: Any) -> dict:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    response = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=25)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} returned an error")
    return data


def send(chat_id: str, text: str, reply_markup: dict | None = None) -> bool:
    try:
        args: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            args["reply_markup"] = reply_markup
        telegram("sendMessage", **args)
        return True
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        log.error("Telegram delivery to %s failed: %s", chat_id, exc)
        return False


def normalize_price(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("amount", "value", "rent_including_utilities_costs", "rent", "price"):
            if key in value:
                return normalize_price(value[key])
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d[\d.,]*", value)
        if match:
            cleaned = match.group().replace(".", "").split(",")[0]
            try:
                return int(cleaned)
            except ValueError:
                return None
    return None


def accepted(title: str, rent: int | None) -> bool:
    if re.search(r"\b(gesuch(?:t)?|suche|suche(n)?\s+wohnung|wohnungstausch|tauschwohnung|verkauf|zu verkaufen|vermietet)\b", title, re.I):
        return False
    return not (MAX_RENT > 0 and rent is not None and rent > MAX_RENT)


def entry(source: str, category: str, title: str, url: str, price: Any = None, area: str = "") -> dict | None:
    title = " ".join(str(title).split())[:200]
    rent = normalize_price(price)
    if not url.startswith("https://") or not accepted(title, rent):
        return None
    return {"id": f"{source}:{url.split('?')[0]}", "source": source, "category": category,
            "title": title or category, "link": url, "rent": rent, "area": str(area)[:100]}


def wg_api_listings() -> tuple[list[dict], bool]:
    """WG's internal endpoint is unofficial; fallback to HTML on failure."""
    results: list[dict] = []
    working = False
    for category, category_name in WG_CATEGORIES.items():
        try:
            response = get(WG_API, params={
                "city_id": WG_CITY_ID, "category": category, "rent_type": 0,
                "sort_order": 0, "limit": 50, "noDeact": 1,
            })
            payload = response.json()
            ads = payload if isinstance(payload, list) else payload.get("ads", payload.get("data", []))
            if isinstance(ads, dict):
                ads = ads.get("ads", ads.get("items", []))
            if not isinstance(ads, list):
                raise ValueError("Unexpected API response")
            working = True
            for ad in ads:
                if not isinstance(ad, dict):
                    continue
                listing_id = ad.get("id") or ad.get("ad_id")
                url = str(ad.get("url") or "")
                if not url and listing_id:
                    url = f"{WG_BASE}/{listing_id}.html"
                url = urljoin(WG_BASE, url)
                costs = ad.get("costs") or {}
                rent = ad.get("rent") or (costs.get("rent_including_utilities_costs") if isinstance(costs, dict) else None)
                item = entry("WG-Gesucht", category_name, ad.get("title") or ad.get("headline") or category_name,
                             url, rent, ad.get("district") or "")
                if item:
                    results.append(item)
        except (requests.RequestException, ValueError, TypeError) as exc:
            log.warning("WG API (%s) unavailable: %s", category_name, exc)
    return results, working


def wg_html_listings() -> list[dict]:
    results: list[dict] = []
    slugs = {0: "wg-zimmer", 1: "1-zimmer-wohnungen", 2: "wohnungen", 3: "haeuser"}
    for category, label in WG_CATEGORIES.items():
        url = f"{WG_BASE}/{slugs[category]}-in-Berlin.8.{category}.1.0.html"
        try:
            page = BeautifulSoup(get(url, params={"city_id": WG_CITY_ID, "noDeact": 1}).text, "html.parser")
            for a in page.select("a[href]"):
                href = a.get("href", "")
                match = re.search(r"(?:\.8\.\d+\.)?(\d{6,})\.html", href)
                if not match or "berlin" not in (href + " " + a.get_text(" ", strip=True)).lower() and ".8." not in href:
                    continue
                link = f"{WG_BASE}/{match.group(1)}.html"
                title = a.get("title") or a.get_text(" ", strip=True) or label
                parent = a.find_parent("article") or a.find_parent("div", class_=re.compile("card|offer|listing", re.I))
                details = parent.get_text(" ", strip=True) if parent else ""
                price_match = re.search(r"(\d[\d.,]*)\s*€", details)
                item = entry("WG-Gesucht", label, title, link, price_match.group(1) if price_match else None)
                if item:
                    results.append(item)
        except requests.RequestException as exc:
            log.warning("WG HTML (%s) failed: %s", label, exc)
    return results


def kleinanzeigen_listings() -> list[dict]:
    results: list[dict] = []
    for label, url in KA_SEARCHES.items():
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
            for card in soup.select("article.aditem, article[data-adid]"):
                a = card.select_one("a.ellipsis, a[href*='/s-anzeige/']")
                if not a:
                    continue
                link = urljoin("https://www.kleinanzeigen.de", a.get("href", ""))
                if urlparse(link).netloc != "www.kleinanzeigen.de" or "/s-anzeige/" not in link:
                    continue
                title = a.get_text(" ", strip=True) or card.get("data-adid", label)
                price_tag = card.select_one(".aditem-main--middle--price-shipping--price")
                price = price_tag.get_text(" ", strip=True) if price_tag else None
                location_tag = card.select_one(".aditem-main--top--left")
                area = location_tag.get_text(" ", strip=True) if location_tag else ""
                item = entry("Kleinanzeigen", label, title, link, price, area)
                if item:
                    results.append(item)
        except requests.RequestException as exc:
            log.warning("Kleinanzeigen (%s) unavailable: %s", label, exc)
    return results


def all_listings() -> list[dict]:
    wg, api_working = wg_api_listings()
    if not api_working or not wg:
        wg += wg_html_listings()
    items = wg + kleinanzeigen_listings()
    dedup: dict[str, dict] = {}
    for item in items:
        dedup[item["id"]] = item
    log.info("Found %d unique Berlin listings", len(dedup))
    return list(dedup.values())


def listing_message(item: dict, heading: str = "New Berlin apartment listing") -> tuple[str, dict]:
    emoji = {"WG-Zimmer": "🛋", "1-Zimmer-Wohnung": "🏢", "Wohnung": "🏠", "Haus": "🏡"}.get(item["category"], "🏠")
    text = (f"{emoji} <b>{html.escape(heading)}</b>\n\n"
            f"<b>{html.escape(item['title'])}</b>\n"
            f"📍 Berlin {html.escape(item.get('area') or '')}\n"
            f"💶 {item['rent']} €/month\n" if item.get("rent") is not None else
            f"{emoji} <b>{html.escape(heading)}</b>\n\n<b>{html.escape(item['title'])}</b>\n📍 Berlin\n")
    text += f"🏷 {html.escape(item['category'])} · {html.escape(item['source'])}\n"
    return text, {"inline_keyboard": [[{"text": "View listing", "url": item["link"]}]]}


def process_telegram_updates(files: dict[str, str], subscribers: set[str], listings: list[dict]) -> tuple[set[str], dict[str, str]]:
    changes: dict[str, str] = {}
    try:
        previous_offset = int(files.get(OFFSET_FILE, "0") or "0")
    except ValueError:
        previous_offset = 0
    try:
        response = telegram("getUpdates", offset=previous_offset, timeout=0, allowed_updates=["message"])
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        log.warning("Cannot read Telegram commands: %s", exc)
        return subscribers, changes
    highest = previous_offset
    updated = False
    for update in response.get("result", []):
        highest = max(highest, update["update_id"] + 1)
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not chat_id or (ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS):
            continue
        command = (msg.get("text") or "").split(" ")[0].split("@")[0].lower()
        if command in ("/start", "/subscribe"):
            first_time = chat_id not in subscribers
            subscribers.add(chat_id)
            updated = True
            sent = send(chat_id, "✅ Subscribed to Berlin apartment alerts!\nSources: WG-Gesucht and Kleinanzeigen.\nUse /stop to unsubscribe.")
            if sent and first_time and INITIAL_SEND:
                for item in listings[:min(MAX_PER_RUN, 10)]:
                    text, markup = listing_message(item, "Current Berlin listing")
                    send(chat_id, text, markup)
        elif command in ("/stop", "/unsubscribe"):
            subscribers.discard(chat_id)
            updated = True
            send(chat_id, "✅ Unsubscribed from Berlin apartment alerts.")
        elif command == "/help":
            send(chat_id, "Berlin apartment bot\n/start — Subscribe\n/stop — Unsubscribe\n/help — Commands")
    if highest > previous_offset:
        changes[OFFSET_FILE] = str(highest)
    if updated:
        changes[SUBSCRIBERS_FILE] = json.dumps(sorted(subscribers))
    return subscribers, changes


def main() -> None:
    if not TOKEN or not GIST_ID or not GIST_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN, GIST_ID and GIST_TOKEN as GitHub Actions secrets")
    files = gist_load()
    if files is None:
        raise RuntimeError("Cannot load persistent state; refusing to send duplicate notifications")
    subscribers = {str(x) for x in json_state(files, SUBSCRIBERS_FILE, [])}
    listings = all_listings()
    subscribers, command_changes = process_telegram_updates(files, subscribers, listings)

    # First run establishes a baseline (except /start snapshots). Subsequent runs alert new ads.
    existing_seen = {str(x) for x in json_state(files, SEEN_FILE, [])}
    is_first_run = SEEN_FILE not in files
    newly_found = [item for item in listings if item["id"] not in existing_seen]
    if is_first_run:
        log.info("First Berlin run: creating baseline; no bulk alert for previous listings")
    elif subscribers:
        for item in newly_found[:MAX_PER_RUN]:
            text, markup = listing_message(item)
            for chat_id in sorted(subscribers):
                send(chat_id, text, markup)
            time.sleep(0.15)
    else:
        log.info("No Telegram subscribers. Send /start to your bot")
    # Keep bounded history and preserve all seen IDs (including unalerted listings).
    new_seen = sorted(existing_seen | {item["id"] for item in listings})
    if len(new_seen) > 10000:
        new_seen = new_seen[-10000:]
    command_changes[SEEN_FILE] = json.dumps(new_seen, ensure_ascii=False)
    if not gist_save(command_changes):
        raise RuntimeError("Unable to persist updated seen-listing state")
    log.info("Run finished; %d new listings, %d subscribers", len(newly_found), len(subscribers))


if __name__ == "__main__":
    main()
