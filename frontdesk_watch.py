import argparse
import asyncio
import json
import logging
import os
import requests
import random
import re
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import List, Set, Tuple
import hashlib
import time

log = logging.getLogger("frontdesk_watch")

from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

STATE_DIR = os.getenv("STATE_DIR", ".")
os.makedirs(STATE_DIR, exist_ok=True)
SUBSCRIBERS_FILE = os.path.join(STATE_DIR, "subscribers.json")

BOT_COMMANDS = [
    {"command": "start", "description": "Show available commands"},
    {"command": "subscribe", "description": "Get alerts when slots open up"},
    {"command": "unsubscribe", "description": "Stop getting alerts"},
    {"command": "get", "description": "Show the latest scraped availability"},
    {"command": "detailed", "description": "Show the full per-day breakdown"},
    {"command": "hello", "description": "Test that the bot is alive"},
]


# Most recent scrape result, published by main_async, read by /get and /detailed.
_latest_state: dict = {
    "checked_at": None,   # datetime
    "good": [],           # List[datetime] before cutoff
    "after_cutoff": [],   # List[datetime] on/after cutoff
    "days": [],           # List[Tuple[str, int]] every day container observed + slot count
    "error": None,        # str
}


def _format_latest(max_items: int = 20) -> str:
    state = _latest_state
    when = state["checked_at"]
    if when is None:
        return "No data yet — first scrape hasn't completed."

    when_str = when.isoformat(sep=" ", timespec="seconds")
    if state["error"]:
        return f"Last check ({when_str}) failed: {state['error']}"

    good = state["good"]
    after = state["after_cutoff"]
    lines = [f"Last check: {when_str}", f"Before cutoff: {len(good)} slot(s)"]
    for s in good[:max_items]:
        lines.append(f"  {s.isoformat(sep=' ', timespec='minutes')}")
    if len(good) > max_items:
        lines.append(f"  (+{len(good) - max_items} more)")
    lines.append(f"On/after cutoff: {len(after)} slot(s)")
    return "\n".join(lines)


def _format_detailed(max_days: int = 100) -> str:
    state = _latest_state
    when = state["checked_at"]
    if when is None:
        return "No data yet — first scrape hasn't completed."

    when_str = when.isoformat(sep=" ", timespec="seconds")
    if state["error"]:
        return f"Last check ({when_str}) failed: {state['error']}"

    good = state["good"]
    after = state["after_cutoff"]
    days = state["days"]

    lines = [f"Last check: {when_str}"]
    lines.append(f"Before cutoff: {len(good)} slot(s)")
    for s in good:
        lines.append(f"  {s.isoformat(sep=' ', timespec='minutes')}")
    lines.append(f"On/after cutoff: {len(after)} slot(s)")
    for s in after:
        lines.append(f"  {s.isoformat(sep=' ', timespec='minutes')}")

    lines.append("")
    lines.append(f"Days observed: {len(days)}")
    for day_text, count in days[:max_days]:
        lines.append(f"  {day_text} — {count} slot(s)")
    if len(days) > max_days:
        lines.append(f"  (+{len(days) - max_days} more days)")
    return "\n".join(lines)


def load_subscribers() -> Set[int]:
    if not os.path.exists(SUBSCRIBERS_FILE):
        return set()
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(x) for x in data} if isinstance(data, list) else set()
    except Exception:
        return set()


def save_subscribers(subs: Set[int]) -> None:
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(subs), f)


def add_subscriber(chat_id: int) -> bool:
    subs = load_subscribers()
    if chat_id in subs:
        return False
    subs.add(chat_id)
    save_subscribers(subs)
    return True


def remove_subscriber(chat_id: int) -> bool:
    subs = load_subscribers()
    if chat_id not in subs:
        return False
    subs.discard(chat_id)
    save_subscribers(subs)
    return True


def bootstrap_env_subscriber() -> None:
    """If TELEGRAM_CHAT_ID is set, ensure it's in subscribers.json."""
    raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not raw:
        return
    try:
        chat_id = int(raw)
    except ValueError:
        log.warning("TELEGRAM_CHAT_ID=%r is not a numeric chat id; ignoring.", raw)
        return
    if add_subscriber(chat_id):
        log.info("Bootstrapped subscriber from env: %s", chat_id)


def telegram_set_commands() -> None:
    """Register the / menu shown in Telegram clients."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/setMyCommands",
            json={"commands": BOT_COMMANDS},
            timeout=10,
        )
    except Exception:
        pass


def telegram_send(message: str) -> None:
    """Broadcast to every subscriber in subscribers.json."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    subscribers = load_subscribers()
    if not subscribers:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in subscribers:
        try:
            requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception:
            pass


def _start_message() -> str:
    lines = ["frontdesk-watch — available commands:"]
    for c in BOT_COMMANDS:
        lines.append(f"/{c['command']} — {c['description']}")
    return "\n".join(lines)


async def telegram_command_loop() -> None:
    """Long-poll getUpdates and handle /start, /subscribe, /unsubscribe, /hello."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    base = f"https://api.telegram.org/bot{token}"
    offset = 0
    while True:
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{base}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            for upd in resp.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                if not chat_id:
                    continue
                cmd = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
                reply = None
                if cmd in ("/start", "/help"):
                    reply = _start_message()
                elif cmd == "/hello":
                    reply = "hello! frontdesk-watch is alive."
                elif cmd == "/get":
                    reply = _format_latest()
                elif cmd == "/detailed":
                    reply = _format_detailed()
                elif cmd == "/subscribe":
                    added = await asyncio.to_thread(add_subscriber, chat_id)
                    reply = (
                        "Subscribed — you'll get alerts when slots open up."
                        if added else "You're already subscribed."
                    )
                elif cmd == "/unsubscribe":
                    removed = await asyncio.to_thread(remove_subscriber, chat_id)
                    reply = (
                        "Unsubscribed — you won't get further alerts."
                        if removed else "You weren't subscribed."
                    )
                if reply:
                    await asyncio.to_thread(
                        requests.post,
                        f"{base}/sendMessage",
                        json={"chat_id": chat_id, "text": reply},
                        timeout=10,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Telegram poll error: %s", e)
            await asyncio.sleep(5)

def slots_fingerprint(slots: List[datetime]) -> str:
    """
    Stable fingerprint of the current availability list.
    """
    payload = "|".join(s.isoformat(timespec="minutes") for s in slots)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Config:
    home_url: str = (
        "https://reservation.frontdesksuite.com/aabenraavielse/vielse/Home/Index"
        "?pageId=b373305a-e1ef-4f58-8e27-fbfbf65b417a&culture=en&uiculture=en"
    )

    # Service option on the English Aabenraa page — matched as a prefix.
    go_to_time_selection_link_prefix: str = "Yes - we will bring along our own witnesses"

    interval_seconds: int = 30
    jitter_seconds: int = 10

    cutoff_year: int = 2026
    cutoff_month: int = 6
    cutoff_day: int = 1  # before June 1

    seen_file: str = os.path.join(STATE_DIR, "seen_slots.json")
    headless: bool = True

    # Telegram rate limiting / change detection
    telegram_min_interval_seconds: int = 30 * 60   # 30 minutes
    telegram_max_items: int = 10                   # cap message length

    # When true, skip Playwright and emit fake slots (for testing alerts).
    dummy: bool = False



def cutoff_date(cfg: Config) -> date:
    return date(cfg.cutoff_year, cfg.cutoff_month, cfg.cutoff_day)


def load_seen(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_seen(path: str, seen: Set[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def parse_times_from_html(html: str) -> Tuple[List[datetime], List[Tuple[str, int]]]:
    """
    Parse the TimeSelection page.
      - day container: div.date.one-queue
      - date label: span.header-text
      - available times: span.available-time
    Returns (sorted unique slots, per-day [(day_text, slot_count), ...]).
    """
    soup = BeautifulSoup(html, "lxml")
    out: List[datetime] = []
    days: List[Tuple[str, int]] = []

    day_divs = soup.select("div.date.one-queue")
    log.debug("parse: %d day container(s) on page", len(day_divs))

    for day_div in day_divs:
        header = day_div.select_one("span.header-text")
        if not header:
            continue
        day_text = header.get_text(strip=True)

        time_spans = day_div.select("span.available-time")
        days.append((day_text, len(time_spans)))
        log.debug("parse: %r → %d available slot(s)", day_text, len(time_spans))
        if not time_spans:
            continue

        try:
            day = dtparser.parse(day_text, fuzzy=True).date()
        except Exception:
            continue

        for ts in time_spans:
            ttxt = ts.get_text(strip=True)
            try:
                dt = dtparser.parse(f"{day.isoformat()} {ttxt}", fuzzy=True)
                out.append(dt.replace(second=0, microsecond=0))
            except Exception:
                continue

    uniq = {x.isoformat(): x for x in out}
    return sorted(uniq.values()), days


def is_before_cutoff(dt: datetime, cfg: Config) -> bool:
    return dt.date() < cutoff_date(cfg)


def make_dummy_slots(cfg: Config, n: int = 20) -> List[datetime]:
    """Generate n fake slots before the cutoff, one per day at 10:00."""
    cutoff = cutoff_date(cfg)
    start = datetime(cutoff.year, cutoff.month, cutoff.day) - timedelta(days=n + 1)
    start = start.replace(hour=10, minute=0)
    return [start + timedelta(days=i) for i in range(n)]


async def get_available_slots(cfg: Config) -> Tuple[List[datetime], List[Tuple[str, int]]]:
    if cfg.dummy:
        await asyncio.sleep(0.1)
        slots = make_dummy_slots(cfg)
        days = [(s.strftime("%A %B %d, %Y"), 1) for s in slots]
        return slots, days

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(cfg.home_url, wait_until="domcontentloaded", timeout=30000)

        # The service option is a <div> (no semantic role), so match by text.
        # Prefix regex avoids trailing ellipsis / whitespace differences.
        option = page.get_by_text(
            re.compile(rf"^{re.escape(cfg.go_to_time_selection_link_prefix)}", re.I)
        )
        await option.first.click(timeout=15000)

        # Wait for TimeSelection content
        try:
            await page.wait_for_selector("div.date.one-queue", timeout=20000)
        except PlaywrightTimeoutError:
            # If this fails, dump the current HTML anyway for debugging.
            html = await page.content()
            await context.close()
            await browser.close()
            return parse_times_from_html(html)

        html = await page.content()
        result = parse_times_from_html(html)

        await context.close()
        await browser.close()
        return result


async def main_async(dummy: bool = False, interval_seconds: int = 30):
    cfg = Config(dummy=dummy, interval_seconds=interval_seconds)
    if dummy:
        # In dummy mode, fire a reminder every poll so testing is observable.
        cfg.telegram_min_interval_seconds = interval_seconds
    seen = load_seen(cfg.seen_file)
    last_telegram_sent_at = 0.0
    last_fingerprint = ""


    log.info("Cutoff: before %s (year=%d)", cutoff_date(cfg).isoformat(), cfg.cutoff_year)
    log.info("Polling every %ds (+ up to %ds jitter). dummy=%s", cfg.interval_seconds, cfg.jitter_seconds, cfg.dummy)

    while True:
        try:
            slots, days_summary = await get_available_slots(cfg)
            good = [s for s in slots if is_before_cutoff(s, cfg)]
            after_cutoff = [s for s in slots if not is_before_cutoff(s, cfg)]

            _latest_state.update(
                checked_at=datetime.now(),
                good=good,
                after_cutoff=after_cutoff,
                days=days_summary,
                error=None,
            )

            log.debug(
                "Poll observed: before-cutoff=%d %s | on/after-cutoff=%d %s",
                len(good),
                [s.isoformat(sep=" ", timespec="minutes") for s in good],
                len(after_cutoff),
                [s.isoformat(sep=" ", timespec="minutes") for s in after_cutoff],
            )

            newly_found = []
            for s in good:
                k = s.isoformat()
                if k not in seen:
                    seen.add(k)
                    newly_found.append(s)

            if newly_found:
                log.info("New slots since last run: %d", len(newly_found))
                save_seen(cfg.seen_file, seen)

            # --- Telegram: notify on changes OR periodic reminder while availability exists ---
            fp = slots_fingerprint(good)
            now_ts = time.time()

            should_notify_change = bool(good) and (fp != last_fingerprint)
            should_notify_reminder = bool(good) and (fp == last_fingerprint) and (
                (now_ts - last_telegram_sent_at) >= cfg.telegram_min_interval_seconds
            )

            if should_notify_change or should_notify_reminder:
                kind = "change" if should_notify_change else "reminder"
                header = "Slots available (changed):" if should_notify_change else "Slots still available:"
                lines = [header]
                lines += [f"- {s.isoformat(sep=' ', timespec='minutes')}" for s in good[: cfg.telegram_max_items]]
                if len(good) > cfg.telegram_max_items:
                    lines.append(f"(+{len(good) - cfg.telegram_max_items} more)")

                telegram_send("\n".join(lines))
                log.info("Telegram %s broadcast: %d slot(s).", kind, len(good))

                last_telegram_sent_at = now_ts
                last_fingerprint = fp

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Scrape error: %s", e)
            _latest_state.update(checked_at=datetime.now(), error=str(e))

        await asyncio.sleep(cfg.interval_seconds + random.randint(0, cfg.jitter_seconds))


async def _run_all(dummy: bool = False, interval_seconds: int = 30):
    bootstrap_env_subscriber()
    telegram_set_commands()
    await asyncio.gather(
        main_async(dummy=dummy, interval_seconds=interval_seconds),
        telegram_command_loop(),
    )


def run():
    parser = argparse.ArgumentParser(prog="frontdesk-watch")
    parser.add_argument(
        "--dummy-mode",
        action="store_true",
        help="Skip scraping and emit 20 fake slots, to test the Telegram alert path.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Polling interval in seconds (default: 30).",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Log level: DEBUG, INFO, WARNING, ERROR (default: INFO, or $LOG_LEVEL).",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        asyncio.run(_run_all(dummy=args.dummy_mode, interval_seconds=args.interval))
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down.")


if __name__ == "__main__":
    run()

