#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1999.co.jp(ホビーサーチ)BEYBLADE X 補貨監控腳本 — GitHub Actions 自我接力版
================================================================================
這個網站是傳統伺服器渲染,直接 requests 抓取即可,不需要 Playwright,
單輪跑得比 MM小舖版更快。

庫存判斷邏輯:
- 頁面出現「品切れ中」或「在庫なし」→ 缺貨
- 頁面出現「販売中」→ 有現貨可下單
- 只有「有現貨」才觸發通知,狀態沒變化不會重複通知

環境變數:
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  (必填,共用同一組)
  TIME_BUDGET_SECONDS   (預設 20400 = 5小時40分)
  CHECK_INTERVAL_SECONDS (預設 45)
  PRODUCTS_FILE (預設 products-1999.json)
  STATE_FILE    (預設 watcher_state_1999.json)
"""

import json
import os
import random
import re
import time
import logging
from pathlib import Path
from datetime import datetime

import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TIME_BUDGET_SECONDS = int(os.environ.get("TIME_BUDGET_SECONDS", 20400))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 45))
JITTER_SECONDS = 12

PRODUCTS_FILE = Path(__file__).parent / os.environ.get("PRODUCTS_FILE", "products-1999.json")
STATE_FILE = Path(__file__).parent / os.environ.get("STATE_FILE", "watcher_state_1999.json")
START_OFFSET_SECONDS = int(os.environ.get("START_OFFSET_SECONDS", 0))

FAIL_ALERT_THRESHOLD = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("watcher_1999")


def load_products() -> list:
    if not PRODUCTS_FILE.exists():
        log.error(f"找不到 {PRODUCTS_FILE}")
        return []
    return json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram 設定未完成")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False}, timeout=10
        )
        if resp.status_code != 200:
            log.error(f"Telegram 發送失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Telegram 發送例外: {e}")


def check_availability(html: str):
    """回傳 (is_available, debug_snippet)"""
    is_out_of_stock = ("品切れ中" in html) or ("在庫なし" in html)
    is_on_sale = "販売中" in html
    is_available = is_on_sale and not is_out_of_stock

    snippet_bits = []
    if is_out_of_stock:
        snippet_bits.append("缺貨標記")
    if is_on_sale:
        snippet_bits.append("販売中標記")
    return is_available, ",".join(snippet_bits) if snippet_bits else "(無標記)"


def check_product(product: dict, state: dict) -> dict:
    name = product["name"]
    url = product["url"]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")

        is_available, snippet = check_availability(resp.text)

        prev = state.get(url, {})
        was_available = prev.get("is_available", False)

        if is_available and not was_available:
            msg = (
                f"🔔 1999.co.jp 補貨了!\n"
                f"商品: {name}\n"
                f"時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"連結: {url}"
            )
            send_telegram(msg)
            log.info(f"[通知已發送] {name}")
        else:
            log.info(f"{name}: 可下單={is_available} | {snippet}")

        state[url] = {"is_available": is_available, "last_checked": datetime.now().isoformat()}

    except Exception as e:
        prev = state.get(url, {})
        fail_count = prev.get("fail_count", 0) + 1
        prev["fail_count"] = fail_count
        state[url] = prev
        log.error(f"{name}: 抓取失敗 ({e}),連續失敗 {fail_count} 次")
        if fail_count == FAIL_ALERT_THRESHOLD:
            send_telegram(f"⚠️ 1999.co.jp 監控連續失敗 {FAIL_ALERT_THRESHOLD} 次,請檢查\n商品: {name}")

    return state


def main():
    products = load_products()
    if not products:
        return

    log.info(f"本次執行時間預算: {TIME_BUDGET_SECONDS} 秒,商品清單: {PRODUCTS_FILE.name}")

    if START_OFFSET_SECONDS > 0:
        log.info(f"錯開啟動,先等待 {START_OFFSET_SECONDS} 秒")
        time.sleep(START_OFFSET_SECONDS)

    state = load_state()
    deadline = time.monotonic() + TIME_BUDGET_SECONDS

    round_num = 0
    while time.monotonic() < deadline:
        round_num += 1
        for product in products:
            state = check_product(product, state)
            save_state(state)
            time.sleep(random.uniform(1.5, 3.5))

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        interval = CHECK_INTERVAL_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
        interval = max(15, min(interval, remaining))
        log.info(f"第 {round_num} 輪結束,{interval:.0f} 秒後再檢查(剩餘 {remaining:.0f} 秒)")
        time.sleep(interval)

    log.info("時間預算用完,交給 workflow 接力重啟")


if __name__ == "__main__":
    main()
