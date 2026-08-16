import asyncio
import json
import logging
import os
import random
import re
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import List, Set, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from dotenv import load_dotenv
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Browser,
    BrowserContext,
    Page,
)

load_dotenv()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    filename="monitor.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


def log(message: str) -> None:
    """Log to both file and terminal."""
    print(message, flush=True)
    logger.info(message)


# ============================================================
# CONFIG
# ============================================================

@dataclass
class Config:
    home_url: str = (
        "https://reservation.frontdesksuite.com/kkvielse/raadhuset/Home/Index"
        "?pageId=6bffdce0-29ab-4353-bdce-9392b1298063&culture=en&uiCulture=en"
    )

    go_to_time_selection_link_prefix: str = (
        "Select date and time for the"
    )

    # How often to check.
    # I'd recommend 15-30 seconds rather than 5.
    interval_seconds: int = 5

    # Random extra delay.
    jitter_seconds: int = 0

    cutoff_year: int = 2026
    cutoff_month: int = 6
    cutoff_day: int = 1

    wanted_start_date: date = date(2026, 9, 18)
    wanted_end_date: date = date(2026, 10, 3)

    seen_file: str = "seen_slots.json"

    headless: bool = True

    telegram_min_interval_seconds: int = 30 * 60
    telegram_max_items: int = 10

    # Maximum amount of time one complete browser check
    # is allowed to take.
    check_timeout_seconds: int = 60

    # Browser/page timeouts.
    navigation_timeout_ms: int = 30_000
    selector_timeout_ms: int = 20_000
    click_timeout_ms: int = 15_000

    # Restart browser after this many consecutive failures.
    max_consecutive_failures: int = 3


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message: str) -> bool:
    """
    Send Telegram message.

    Returns:
        True  = Telegram accepted the message
        False = something failed
    """

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        logger.warning(
            "Telegram is not configured: missing TELEGRAM_BOT_TOKEN "
            "or TELEGRAM_CHAT_ID"
        )
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=10,
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("ok"):
            logger.error(
                "Telegram returned an unsuccessful response: %s",
                data,
            )
            return False

        logger.info("Telegram message sent successfully.")
        return True

    except requests.RequestException as e:
        logger.error("Telegram request failed: %s", e)
        return False

    except Exception as e:
        logger.exception("Unexpected Telegram error: %s", e)
        return False


# ============================================================
# AVAILABILITY FINGERPRINT
# ============================================================

def slots_fingerprint(slots: List[datetime]) -> str:
    """
    Stable fingerprint of the current availability list.
    """

    payload = "|".join(
        s.isoformat(timespec="minutes")
        for s in slots
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


# ============================================================
# SEEN SLOTS
# ============================================================

def load_seen(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(data)

        return set()

    except Exception as e:
        logger.error(
            "Could not load %s: %s",
            path,
            e,
        )
        return set()


def save_seen(path: str, seen: Set[str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                sorted(seen),
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as e:
        logger.error(
            "Could not save %s: %s",
            path,
            e,
        )


# ============================================================
# DATE FILTERING
# ============================================================

def cutoff_date(cfg: Config) -> date:
    return date(
        cfg.cutoff_year,
        cfg.cutoff_month,
        cfg.cutoff_day,
    )


def is_before_cutoff(
    dt: datetime,
    cfg: Config,
) -> bool:
    return dt.date() < cutoff_date(cfg)


def is_wanted_date(
    dt: datetime,
    cfg: Config,
) -> bool:
    return (
        cfg.wanted_start_date
        <= dt.date()
        <= cfg.wanted_end_date
    )


# ============================================================
# HTML PARSING
# ============================================================

def parse_times_from_html(
    html: str,
) -> List[datetime]:

    soup = BeautifulSoup(html, "lxml")

    out: List[datetime] = []

    for day_div in soup.select(
        "div.date.one-queue"
    ):

        header = day_div.select_one(
            "span.header-text"
        )

        if not header:
            continue

        day_text = header.get_text(
            strip=True
        )

        time_spans = day_div.select(
            "span.available-time"
        )

        if not time_spans:
            continue

        try:
            day = dtparser.parse(
                day_text,
                fuzzy=True,
            ).date()

        except Exception:
            continue

        for ts in time_spans:

            ttxt = ts.get_text(
                strip=True
            )

            try:
                dt = dtparser.parse(
                    f"{day.isoformat()} {ttxt}",
                    fuzzy=True,
                )

                dt = dt.replace(
                    second=0,
                    microsecond=0,
                )

                out.append(dt)

            except Exception:
                continue

    # Remove duplicates.
    uniq = {
        x.isoformat(): x
        for x in out
    }

    return sorted(uniq.values())


# ============================================================
# PLAYWRIGHT MONITOR
# ============================================================

class ReservationMonitor:

    def __init__(self, cfg: Config):
        self.cfg = cfg

        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    # --------------------------------------------------------
    # START BROWSER
    # --------------------------------------------------------

    async def start_browser(self):

        log("Starting Playwright browser...")

        self.playwright = await async_playwright().start()

        self.browser = await self.playwright.chromium.launch(
            headless=self.cfg.headless,
        )

        self.context = await self.browser.new_context()

        self.page = await self.context.new_page()

        self.page.set_default_timeout(
            self.cfg.selector_timeout_ms
        )

        self.page.set_default_navigation_timeout(
            self.cfg.navigation_timeout_ms
        )

        log("Browser started successfully.")

    # --------------------------------------------------------
    # CLOSE BROWSER
    # --------------------------------------------------------

    async def close_browser(self):

        log("Closing browser...")

        try:
            if self.context:
                await self.context.close()
        except Exception as e:
            logger.warning(
                "Error closing context: %s",
                e,
            )

        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.warning(
                "Error closing browser: %s",
                e,
            )

        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.warning(
                "Error stopping Playwright: %s",
                e,
            )

        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    # --------------------------------------------------------
    # RESTART BROWSER
    # --------------------------------------------------------

    async def restart_browser(self):

        log("Restarting browser...")

        await self.close_browser()

        await asyncio.sleep(2)

        await self.start_browser()

    # --------------------------------------------------------
    # CHECK AVAILABILITY
    # --------------------------------------------------------

    async def get_available_slots(
        self,
    ) -> List[datetime]:

        if not self.page:
            raise RuntimeError(
                "Browser page does not exist."
            )

        page = self.page

        log("Opening reservation page...")

        await page.goto(
            self.cfg.home_url,
            wait_until="domcontentloaded",
            timeout=self.cfg.navigation_timeout_ms,
        )

        log("Reservation home page loaded.")

        link = page.get_by_role(
            "link",
            name=re.compile(
                rf"^{re.escape(self.cfg.go_to_time_selection_link_prefix)}",
                re.I,
            ),
        )

        log("Looking for time-selection link...")

        await link.first.click(
            timeout=self.cfg.click_timeout_ms
        )

        log("Clicked time-selection link.")

        try:

            await page.wait_for_selector(
                "div.date.one-queue",
                timeout=self.cfg.selector_timeout_ms,
            )

        except PlaywrightTimeoutError:

            log(
                "Time-selection selector did not appear. "
                "Attempting to parse current page anyway."
            )

        html = await page.content()

        slots = parse_times_from_html(html)

        log(
            f"Parsed {len(slots)} total slot(s)."
        )

        return slots


# ============================================================
# MAIN MONITOR LOOP
# ============================================================

async def main_async():

    cfg = Config()

    seen = load_seen(
        cfg.seen_file
    )

    last_telegram_sent_at = 0.0

    last_fingerprint = ""

    consecutive_failures = 0

    monitor = ReservationMonitor(cfg)

    log("=" * 60)
    log("Reservation monitor starting")
    log(
        f"Target dates: "
        f"{cfg.wanted_start_date} → "
        f"{cfg.wanted_end_date}"
    )
    log(
        f"Check interval: "
        f"{cfg.interval_seconds}s"
    )
    log(
        f"Check timeout: "
        f"{cfg.check_timeout_seconds}s"
    )
    log("=" * 60)

    # --------------------------------------------------------
    # Start browser
    # --------------------------------------------------------

    try:

        await monitor.start_browser()

    except Exception as e:

        logger.exception(
            "Initial browser startup failed: %s",
            e,
        )

        # Try again after a short delay.
        await asyncio.sleep(5)

        await monitor.start_browser()

    # --------------------------------------------------------
    # Main infinite loop
    # --------------------------------------------------------

    try:

        while True:

            check_started = time.time()

            log(
                "--------------------------------------------------"
            )

            log(
                "Checking availability..."
            )

            try:

                # ------------------------------------------------
                # HARD TIMEOUT
                # ------------------------------------------------

                slots = await asyncio.wait_for(
                    monitor.get_available_slots(),
                    timeout=cfg.check_timeout_seconds,
                )

                # We successfully completed a check.
                consecutive_failures = 0

                # ------------------------------------------------
                # FILTER WANTED DATES
                # ------------------------------------------------

                good = [
                    s
                    for s in slots
                    if is_wanted_date(s, cfg)
                ]

                outside_range = [
                    s
                    for s in slots
                    if not is_wanted_date(s, cfg)
                ]

                now_str = datetime.now().isoformat(
                    sep=" ",
                    timespec="seconds",
                )

                log(
                    f"{now_str} — availability snapshot"
                )

                log(
                    f"  wanted range: "
                    f"{len(good)} slot(s)"
                )

                for s in good:
                    log(
                        "    "
                        + s.isoformat(
                            sep=" "
                        )
                    )

                log(
                    f"  outside range: "
                    f"{len(outside_range)} slot(s)"
                )

                # ------------------------------------------------
                # NEW SLOT TRACKING
                # ------------------------------------------------

                newly_found = []

                for s in good:

                    key = s.isoformat()

                    if key not in seen:

                        seen.add(key)
                        newly_found.append(s)

                if newly_found:

                    log(
                        f"New slots found: "
                        f"{len(newly_found)}"
                    )

                    save_seen(
                        cfg.seen_file,
                        seen,
                    )

                # ------------------------------------------------
                # TELEGRAM CHANGE DETECTION
                # ------------------------------------------------

                fp = slots_fingerprint(good)

                now_ts = time.time()

                should_notify_change = (
                    bool(good)
                    and fp != last_fingerprint
                )

                should_notify_reminder = (
                    bool(good)
                    and fp == last_fingerprint
                    and (
                        now_ts
                        - last_telegram_sent_at
                        >= cfg.telegram_min_interval_seconds
                    )
                )

                if (
                    should_notify_change
                    or should_notify_reminder
                ):

                    if should_notify_change:
                        header = (
                            "🚨 Slots available "
                            "(changed):"
                        )
                    else:
                        header = (
                            "⏰ Slots still available:"
                        )

                    lines = [header]

                    lines += [
                        "- "
                        + s.isoformat(
                            sep=" ",
                            timespec="minutes",
                        )
                        for s in good[
                            :cfg.telegram_max_items
                        ]
                    ]

                    if (
                        len(good)
                        > cfg.telegram_max_items
                    ):
                        lines.append(
                            f"(+{len(good) - cfg.telegram_max_items} more)"
                        )

                    message = "\n".join(lines)

                    log(
                        "Attempting to send Telegram notification..."
                    )

                    telegram_ok = telegram_send(
                        message
                    )

                    if telegram_ok:

                        last_telegram_sent_at = (
                            now_ts
                        )

                        last_fingerprint = fp

                        log(
                            "Telegram notification "
                            "confirmed."
                        )

                    else:

                        # IMPORTANT:
                        # Do NOT update notification state
                        # if Telegram failed.
                        log(
                            "Telegram notification "
                            "FAILED."
                        )

                else:

                    # Update fingerprint even when there
                    # are no slots, so that when slots appear
                    # we correctly detect the change.
                    last_fingerprint = fp

                # ------------------------------------------------
                # HEARTBEAT
                # ------------------------------------------------

                elapsed = (
                    time.time()
                    - check_started
                )

                log(
                    f"Check completed successfully "
                    f"in {elapsed:.1f}s."
                )

            # ----------------------------------------------------
            # TIMEOUT
            # ----------------------------------------------------

            except asyncio.TimeoutError:

                consecutive_failures += 1

                logger.error(
                    "CHECK TIMEOUT "
                    "(failure %d/%d)",
                    consecutive_failures,
                    cfg.max_consecutive_failures,
                )

                log(
                    "WARNING: availability check "
                    "timed out."
                )

                # A timeout can leave Playwright in a bad state.
                # Restart it.
                try:

                    await monitor.restart_browser()

                    consecutive_failures = 0

                except Exception as e:

                    logger.exception(
                        "Browser restart failed: %s",
                        e,
                    )

            # ----------------------------------------------------
            # PLAYWRIGHT / OTHER ERROR
            # ----------------------------------------------------

            except Exception as e:

                consecutive_failures += 1

                logger.exception(
                    "Availability check failed "
                    "(failure %d/%d): %s",
                    consecutive_failures,
                    cfg.max_consecutive_failures,
                    e,
                )

                log(
                    f"ERROR during check: {e}"
                )

                # Restart browser after an error.
                if (
                    consecutive_failures
                    >= cfg.max_consecutive_failures
                ):

                    log(
                        "Too many consecutive failures. "
                        "Restarting browser."
                    )

                    try:

                        await monitor.restart_browser()

                        consecutive_failures = 0

                    except Exception as restart_error:

                        logger.exception(
                            "Browser restart failed: %s",
                            restart_error,
                        )

                        log(
                            "Browser restart failed. "
                            "Waiting before retry..."
                        )

                        await asyncio.sleep(10)

                else:

                    # Even after one failure, restarting is
                    # generally safer for a browser monitor.
                    try:

                        await monitor.restart_browser()

                    except Exception as restart_error:

                        logger.exception(
                            "Browser restart failed: %s",
                            restart_error,
                        )

            # ----------------------------------------------------
            # WAIT
            # ----------------------------------------------------

            delay = (
                cfg.interval_seconds
                + random.randint(
                    0,
                    cfg.jitter_seconds,
                )
            )

            log(
                f"Next check in {delay}s..."
            )

            await asyncio.sleep(delay)

    # --------------------------------------------------------
    # CLEAN SHUTDOWN
    # --------------------------------------------------------

    except asyncio.CancelledError:

        log(
            "Monitor cancelled."
        )

        raise

    finally:

        await monitor.close_browser()

        log(
            "Reservation monitor stopped."
        )


# ============================================================
# ENTRY POINT
# ============================================================

def run():

    try:

        asyncio.run(
            main_async()
        )

    except KeyboardInterrupt:

        print(
            "\nMonitor stopped by user.",
            flush=True,
        )


if __name__ == "__main__":
    run()