#!/usr/bin/env python3
"""
Ticket-booking watcher.

Polls a URL (a BookMyShow / District showtimes page, or an internal API
request you grabbed from your browser's DevTools) and sends a Telegram
message the moment a given theatre appears with booking open.

State is tracked in state.json so you get alerted on the *transition*
to "available" instead of on every run.

Everything is driven by config.json (and/or environment variables), so
nothing site-specific is hardcoded -- if BookMyShow/District change their
markup you only edit config, not code.
"""

import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path
import random
import string
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

# Look like a real Chrome on Windows -- BMS rejects obvious bots.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH, default={}) or {}

    # Environment variables override the file (used by GitHub Actions secrets).
    env_map = {
        "TARGET_URL": "target_url",
        "THEATRE": "theatre",
        "MOVIE": "movie",
        "REQUESTED_DATE": "requested_date",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "TELEGRAM_USERNAME": "telegram_username",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    if os.environ.get("HEADERS_JSON"):
        cfg["headers"] = json.loads(os.environ["HEADERS_JSON"])

    # The BMS date is embedded in the URL, so build the URL from the template
    # and the (possibly overridden) requested_date. Set REQUESTED_DATE=20260717
    # to point everything at the 17th for a live end-to-end test.
    if cfg.get("url_template") and cfg.get("requested_date"):
        cfg["target_url"] = cfg["url_template"].format(date=cfg["requested_date"])

    required = ["target_url", "telegram_bot_token", "telegram_chat_id"]
    detector = cfg.get("detector")
    if detector in ("bms_date", "venue_date"):
        required.append("requested_date")
    elif detector != "venue_date":
        required.append("theatre")
    if detector == "venue_date" and not (cfg.get("venue_code") or cfg.get("venue_codes")):
        sys.exit("venue_date detector needs 'venue_code' or 'venue_codes'")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}")
    return cfg


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    resp.raise_for_status()


def _parse_playwright_proxy(proxy_url: str | None) -> dict | None:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    password = parsed.password or ""
    if "iproyal.com" in (parsed.hostname or "") and "_session-" not in password:
        rand_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        password = f"{password}_session-{rand_id}_lifetime-2m"
    return {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        "username": parsed.username or "",
        "password": password,
    }
    

def fetch(cfg):
    raw_proxy = os.environ.get("PROXY_URL")
    last_exc = None

    for attempt in range(1, 3):
        proxy_cfg = _parse_playwright_proxy(raw_proxy)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    proxy=proxy_cfg,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()

                # Block heavy assets to save IPRoyal traffic
                page.route(
                    "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,css}",
                    lambda route: route.abort(),
                )

                page.goto(cfg["target_url"], wait_until="domcontentloaded", timeout=30000)
                # Wait for venue list or Cloudflare challenge auto-resolution
                page.wait_for_timeout(5000)
                html = page.content()
                browser.close()

                if "Just a moment..." in html or len(html) < 5000:
                    raise RuntimeError("Blocked by Cloudflare challenge page")

                return html
        except Exception as exc:
            last_exc = exc
            print(f"[attempt {attempt}/2] fetch error: {exc}")

    raise last_exc


def is_available_bms_date(page_text, cfg):
    """
    BookMyShow-specific detector for "a given date has opened for booking".

    BMS only renders showtimes for the date currently being displayed, and it
    silently falls back to the nearest available date when you request a date
    that hasn't opened yet. So the requested date (e.g. 20260720) sits at a
    low ~3 count (just the date-strip navigation) until it opens, at which
    point its showtimes render and it becomes the *dominant* date token.

    Rule: open when the requested date is the most-referenced date token on
    the page and it clears a small floor (well above strip-only noise).
    """
    requested = cfg["requested_date"]  # e.g. "20260720"
    floor = cfg.get("min_references", 10)

    tokens = re.findall(r"20\d{6}", page_text)
    if not tokens:
        return False

    counts = Counter(tokens)
    top_date, _ = counts.most_common(1)[0]
    requested_count = counts.get(requested, 0)

    return top_date == requested and requested_count >= floor


def is_available_venue_date(page_text, cfg):
    """
    Theatre-specific detector: is a given venue bookable on a given date
    (and optionally at a specific showtime)?
    """
    date = cfg["requested_date"]
    codes = cfg.get("venue_codes") or [cfg["venue_code"]]
    requested_time = (cfg.get("requested_time") or "").strip()

    for code in codes:
        # 1. Confirm this specific venue has opened for the target date
        if f"/{code}/{date}" not in page_text:
            continue

        # 2. If no specific time is required, venue + date match is sufficient
        if not requested_time:
            return True

        print(f"[{code}] Venue open for {date}. Scoping check for requested_time: {requested_time}")

        # 3. Extract ONLY the JSON/HTML slice belonging to this venue code
        # (from "VenueCode":"ALUC" up to the next "VenueCode": key)
        venue_json_blocks = re.findall(
            rf'"VenueCode"\s*:\s*"{re.escape(code)}"(?:(?!"VenueCode"\s*:).)*',
            page_text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        # Also check any DOM/anchor block starting at /<code>/<date> up to the next /<OTHER_CODE>/<date>
        venue_dom_blocks = re.findall(
            rf'/{re.escape(code)}/{re.escape(date)}(?:(?!/[A-Z0-9]{{3,6}}/{re.escape(date)}).)*',
            page_text,
            flags=re.DOTALL,
        )

        scoped_chunks = venue_json_blocks + venue_dom_blocks
        if not scoped_chunks:
            continue

        # Normalize whitespace (e.g., in case BMS uses non-breaking spaces like "04:30\xa0PM")
        norm_target_time = re.sub(r"\s+", " ", requested_time).lower()
        for chunk in scoped_chunks:
            norm_chunk = re.sub(r"\s+", " ", chunk).lower()
            if norm_target_time in norm_chunk:
                return True

    return False


def is_available(page_text, cfg):
    detector = cfg.get("detector")
    if detector == "venue_date":
        return is_available_venue_date(page_text, cfg)
    if detector == "bms_date":
        return is_available_bms_date(page_text, cfg)
    return is_available_generic(page_text, cfg)


def is_available_generic(page_text, cfg):
    """
    Booking is considered OPEN for the target theatre when the theatre name
    is present AND at least one 'booking is live' signal is present.

    Matching is case-insensitive and ignores extra whitespace so small
    formatting differences don't cause misses.
    """
    haystack = re.sub(r"\s+", " ", page_text).lower()

    theatre = re.sub(r"\s+", " ", cfg["theatre"]).lower().strip()
    if theatre not in haystack:
        return False

    # If the movie name is configured, require it too (avoids false hits when
    # the theatre is listed for other movies).
    movie = cfg.get("movie")
    if movie:
        if re.sub(r"\s+", " ", movie).lower().strip() not in haystack:
            return False

    # Signals that booking is actually live rather than "coming soon".
    open_signals = cfg.get(
        "open_signals",
        ["book tickets", "book now", '"showtimes"', "showtime", "select seats"],
    )
    # Signals that it's NOT yet open -- if present near-exclusively, treat as closed.
    closed_signals = cfg.get("closed_signals", ["notify me", "coming soon"])

    has_open = any(s.lower() in haystack for s in open_signals)
    only_closed = any(s.lower() in haystack for s in closed_signals) and not has_open

    return has_open and not only_closed


def trigger_telegram_call(username, text="Alert! Movie tickets are now available!"):
    """Triggers an automated voice call via CallMeBot."""
    if not username:
        return

    # Ensure username includes leading '@'
    clean_username = username.strip()
    if not clean_username.startswith("@"):
        clean_username = f"@{clean_username}"

    safe_text = urllib.parse.quote(text)
    # rpt=3 repeats the spoken message 3 times during the call
    call_url = f"http://api.callmebot.com/start.php?user={clean_username}&text={safe_text}&lang=en-GB-Standard-B&rpt=3"

    try:
        response = requests.get(call_url, timeout=10)
        if response.status_code == 200:
            print(f"[{clean_username}] CallMeBot voice call triggered successfully")
        else:
            print(f"[{clean_username}] CallMeBot return status code {response.status_code}")
    except Exception as exc:
        print(f"[{clean_username}] CallMeBot call failed: {exc}")
        

def main():
    cfg = load_config()
    state = load_json(STATE_PATH, default={"available": False}) or {"available": False}

    target_desc = cfg.get("theatre") or cfg.get("requested_date", "target")
    label = f"{cfg.get('movie', 'movie')} @ {target_desc}"

    try:
        page = fetch(cfg)
    except requests.RequestException as exc:
        # Transient network/blocking errors shouldn't crash the workflow.
        print(f"[{label}] fetch failed: {exc}")
        return 0

    available = is_available(page, cfg)
    print(f"[{label}] available={available} (was {state.get('available')})")

    if available and not state.get("available"):
        req_time = (cfg.get("requested_time") or "").strip()
        time_line = f"Time: {req_time}\n" if req_time else ""

        if cfg.get("detector") in ("bms_date", "venue_date"):
            rd = cfg["requested_date"]
            pretty = f"{rd[6:8]}-{rd[4:6]}-{rd[0:4]}"
            venue = cfg.get("venue_label") or cfg.get("venue_code") or ""
            venue_line = f"Theatre: {venue}\n" if venue else ""
            msg = (
                f"🎬 Booking just OPENED!\n\n"
                f"{cfg.get('movie', 'Movie')}\n"
                f"{venue_line}"
                f"Date: {pretty}\n"
                f"{time_line}\n"
                f"Book here: {cfg['target_url']}"
            )
        else:
            msg = (
                f"🎬 Booking is OPEN!\n\n"
                f"{cfg.get('movie', 'Movie')}\n"
                f"Theatre: {cfg['theatre']}\n"
                f"{time_line}\n"
                f"Book here: {cfg['target_url']}"
            )
        send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], msg)
        # 2. Trigger Actual Telegram Phone Call
        # telegram_username = cfg.get("telegram_username")
        # spoken_alert = f"Alert! Booking just opened for {cfg.get('movie', 'your movie')}. Open BookMyShow now!"
        # trigger_telegram_call(telegram_username, spoken_alert)
        
        print(f"[{label}] notification sent")

    # Persist current state so we don't re-alert every run.
    if available != state.get("available"):
        state["available"] = available
        state["checked_at"] = int(time.time())
        save_json(STATE_PATH, state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
