import asyncio
import json
import os
import requests
import random
import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import List, Set
import hashlib
import time

from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

load_dotenv()

def telegram_send(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return  # silently do nothing if not configured

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

def slots_fingerprint(slots: List[datetime]) -> str:
    """
    Stable fingerprint of the current availability list.
    """
    payload = "|".join(s.isoformat(timespec="minutes") for s in slots)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Config:
    home_url: str = (
        "https://reservation.frontdesksuite.com/kkvielse/raadhuset/Home/Index"
        "?pageId=6bffdce0-29ab-4353-bdce-9392b1298063&culture=en&uiCulture=en"
    )

    # The exact link text codegen found (we'll match as a prefix to be safe)
    go_to_time_selection_link_prefix: str = "Select date and time for the"

    interval_seconds: int = 5
    jitter_seconds: int = 0

    cutoff_year: int = 2026
    cutoff_month: int = 6
    cutoff_day: int = 1  # before June 1
    wanted_start_date = date(2026, 9, 18)
    wanted_end_date = date(2026, 10, 3)

    seen_file: str = "seen_slots.json"
    headless: bool = True

    # Telegram rate limiting / change detection
    telegram_min_interval_seconds: int = 30 * 60   # 30 minutes
    telegram_max_items: int = 10                   # cap message length



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


def parse_times_from_html(html: str) -> List[datetime]:
    """
    Parse the TimeSelection page:
      - day container: div.date.one-queue
      - date label: span.header-text
      - available times: span.available-time
    """
    soup = BeautifulSoup(html, "lxml")
    out: List[datetime] = []

    for day_div in soup.select("div.date.one-queue"):
        header = day_div.select_one("span.header-text")
        if not header:
            continue
        day_text = header.get_text(strip=True)

        time_spans = day_div.select("span.available-time")
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
    return sorted(uniq.values())


def is_before_cutoff(dt: datetime, cfg: Config) -> bool:
    return dt.date() < cutoff_date(cfg)

def is_wanted_date(dt: datetime, cfg: Config) -> bool:
    return cfg.wanted_start_date <= dt.date() <= cfg.wanted_end_date

async def get_available_slots(cfg: Config) -> List[datetime]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(cfg.home_url, wait_until="domcontentloaded", timeout=30000)

        # Click the link codegen found.
        # Use regex "starts with" match to avoid trailing ellipsis differences.
        link = page.get_by_role(
            "link",
            name=re.compile(rf"^{re.escape(cfg.go_to_time_selection_link_prefix)}", re.I),
        )
        await link.first.click(timeout=15000)

        # Wait for TimeSelection content
        try:
            await page.wait_for_selector("div.date.one-queue", timeout=20000)
        except PlaywrightTimeoutError:
            # If this fails, dump the current HTML anyway for debugging.
            html = await page.content()
            await context.close()
            await browser.close()
            # Try parsing anyway; will likely return []
            return parse_times_from_html(html)

        html = await page.content()
        slots = parse_times_from_html(html)

        await context.close()
        await browser.close()
        return slots


async def main_async():
    cfg = Config()
    seen = load_seen(cfg.seen_file)
    last_telegram_sent_at = 0.0
    last_fingerprint = ""


    print(f"Cutoff: before {cutoff_date(cfg).isoformat()} (year={cfg.cutoff_year})")
    print(f"Checking every {cfg.interval_seconds}s (+ up to {cfg.jitter_seconds}s jitter).")

    while True:
        try:
            slots = await get_available_slots(cfg)
            good = [s for s in slots if is_wanted_date(s, cfg)]
            outside_range = [s for s in slots if not is_wanted_date(s, cfg)]
    
            now_str = datetime.now().isoformat(sep=" ", timespec="seconds")
    
            print(f"\n{now_str} — availability snapshot:")

            print(f"  wanted date range: {len(good)} slot(s)")
            for s in good:
                print("    ", s.isoformat(sep=" "))
            
            print(f"  outside range: {len(outside_range)} slot(s)")
            # --- "new since last run" tracking (optional) ---
            newly_found = []
            for s in good:
                k = s.isoformat()
                if k not in seen:
                    seen.add(k)
                    newly_found.append(s)
    
            if newly_found:
                print("  (new since last run)")
                save_seen(cfg.seen_file, seen)
    
            # --- Telegram: notify on changes OR periodic reminder while availability exists ---
            fp = slots_fingerprint(good)
            now_ts = time.time()
    
            should_notify_change = bool(good) and (fp != last_fingerprint)
            should_notify_reminder = bool(good) and (fp == last_fingerprint) and (
                (now_ts - last_telegram_sent_at) >= cfg.telegram_min_interval_seconds
            )
    
            if should_notify_change or should_notify_reminder:
                header = "Slots available (changed):" if should_notify_change else "Slots still available:"
                lines = [header]
                lines += [f"- {s.isoformat(sep=' ', timespec='minutes')}" for s in good[: cfg.telegram_max_items]]
                if len(good) > cfg.telegram_max_items:
                    lines.append(f"(+{len(good) - cfg.telegram_max_items} more)")
    
                telegram_send("\n".join(lines))
    
                last_telegram_sent_at = now_ts
                last_fingerprint = fp
    
        except Exception as e:
            print(f"{datetime.now().isoformat(sep=' ', timespec='seconds')} — error: {e}")
    
        await asyncio.sleep(cfg.interval_seconds + random.randint(0, cfg.jitter_seconds))


def run():
    asyncio.run(main_async())


if __name__ == "__main__":
    run()

