#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MM小舖 指定商品/指定規格 庫存監控 — GitHub Actions 自我接力版
================================================================
只追蹤 5 個指定商品頁面裡,規格為「預購-1個(不可搭其他預購) #不補」的庫存狀態。
只在每天 11:30 ~ 隔天 02:30(台灣時間)之間運作,超出時段自動停止,
隔天 11:30 由排程自動重新啟動,不會在時段外持續耗用資源或發送請求。

環境變數:
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (必填)
  TIME_BUDGET_SECONDS (單次執行最多跑幾秒,預設 20400)
  CHECK_INTERVAL_SECONDS (每輪間隔,預設 30)
  STATE_FILE (預設 mm_variant_state.json)
"""

import json
import os
import random
import re
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TIME_BUDGET_SECONDS = int(os.environ.get("TIME_BUDGET_SECONDS", 20400))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 30))
JITTER_SECONDS = 8

STATE_FILE = Path(__file__).parent / os.environ.get("STATE_FILE", "mm_variant_state.json")

TARGET_VARIANT_KEYWORD = "不可搭其他預購"  # 用來鎖定要追蹤的規格選項

PRODUCTS = [
    {"name": "商品1", "url": "https://mmtoyshop.com/item/Shopee6a3bdc265e88a"},
    {"name": "商品2", "url": "https://mmtoyshop.com/item/Shopee6a3bdbb22415e"},
    {"name": "商品3", "url": "https://mmtoyshop.com/item/Shopee6a3bdb7776072"},
    {"name": "商品4", "url": "https://mmtoyshop.com/item/Shopee6a3bdb4fe2bcb"},
    {"name": "商品5", "url": "https://mmtoyshop.com/item/Shopee6a4620509d97a"},
]

TZ = ZoneInfo("Asia/Taipei")
WINDOW_START = (11, 30)   # 11:30
WINDOW_END = (2, 30)      # 隔天 02:30

FAIL_ALERT_THRESHOLD = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mm_variant_watcher")


def now_tw() -> datetime:
    return datetime.now(TZ)


def is_within_window(dt: datetime) -> bool:
    t = (dt.hour, dt.minute)
    # 時段跨過午夜: 11:30~23:59 或 00:00~02:30
    if t >= WINDOW_START:
        return True
    if t <= WINDOW_END:
        return True
    return False


def compute_window_deadline(dt: datetime) -> datetime:
    """回傳今天(或明天)02:30 的時間點,作為這次執行的上限。"""
    end_h, end_m = WINDOW_END
    if dt.hour < end_h or (dt.hour == end_h and dt.minute < end_m):
        end = dt.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    else:
        end = (dt + timedelta(days=1)).replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return end


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
        log.error("Telegram 未設定")
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


def check_one_product(page, product: dict, state: dict) -> dict:
    name = product["name"]
    url = product["url"]

    try:
        page.goto(url, wait_until="networkidle", timeout=30000)

        # 點選目標規格選項(用關鍵字比對,避免依賴確切排序)
        try:
            page.get_by_text(TARGET_VARIANT_KEYWORD, exact=False).first.click(timeout=8000)
            page.wait_for_timeout(1000)
        except Exception:
            log.warning(f"{name}: 找不到規格選項「{TARGET_VARIANT_KEYWORD}」,改用頁面預設狀態讀取")

        body_text = page.inner_text("body")

        stock_match = re.search(r"商品庫存[:：]\s*(\d+)", body_text)
        stock_qty = int(stock_match.group(1)) if stock_match else None

        is_restocking = ("補貨中" in body_text) or ("已售完" in body_text) or ("缺貨" in body_text)
        is_available = bool(stock_qty and stock_qty > 0 and not is_restocking)

        prev = state.get(url, {})
        was_available = prev.get("is_available", False)

        if is_available and not was_available:
            msg = (
                f"🔔 MM小舖補貨了!(指定規格:不可搭其他預購)\n"
                f"商品: {name}\n"
                f"庫存: {stock_qty}\n"
                f"時間: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"連結: {url}"
            )
            send_telegram(msg)
            log.info(f"[通知已發送] {name}")
        else:
            log.info(f"{name}: 庫存={stock_qty}, 補貨中={is_restocking}, 可下單={is_available}")

        state[url] = {
            "is_available": is_available,
            "stock_qty": stock_qty,
            "fail_count": 0,
            "last_checked": datetime.now(TZ).isoformat(),
        }

    except Exception as e:
        prev = state.get(url, {})
        fail_count = prev.get("fail_count", 0) + 1
        prev["fail_count"] = fail_count
        state[url] = prev
        log.error(f"{name}: 檢查失敗 ({e}),連續失敗 {fail_count} 次")
        if fail_count == FAIL_ALERT_THRESHOLD:
            send_telegram(f"⚠️ MM小舖指定商品監控連續失敗 {FAIL_ALERT_THRESHOLD} 次\n商品: {name}")

    return state


def main():
    start = now_tw()

    if not is_within_window(start):
        log.info(
            f"目前時間 {start.strftime('%H:%M')} 不在監控時段(11:30~隔天02:30)內,"
            f"本次執行不做任何檢查,直接結束"
        )
        return

    window_deadline = compute_window_deadline(start)
    budget_deadline = start + timedelta(seconds=TIME_BUDGET_SECONDS)
    deadline = min(window_deadline, budget_deadline)

    log.info(f"開始監控,預計結束時間: {deadline.strftime('%Y-%m-%d %H:%M:%S')}")

    state = load_state()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
        )

        round_num = 0
        while now_tw() < deadline:
            round_num += 1
            for product in PRODUCTS:
                state = check_one_product(page, product, state)
                save_state(state)
                time.sleep(random.uniform(2, 4))

            remaining = (deadline - now_tw()).total_seconds()
            if remaining <= 0:
                break
            interval = CHECK_INTERVAL_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
            interval = max(10, min(interval, remaining))
            log.info(f"第 {round_num} 輪結束,{interval:.0f} 秒後再檢查(距結束還剩 {remaining:.0f} 秒)")
            time.sleep(interval)

        browser.close()

    log.info("時段結束或時間預算用完,結束執行")


if __name__ == "__main__":
    main()
