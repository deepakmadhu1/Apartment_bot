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
