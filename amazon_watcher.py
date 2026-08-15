#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon.co.jp 販売元監控腳本 — GitHub Actions 自我接力版
=====================================================
與本機常駐版的差異:
- 不會永遠迴圈,而是跑到「時間預算」用完就自動結束(讓 workflow 有時間收尾、接力重啟)
- Token / chat_id 一律從環境變數讀取(對應 GitHub Secrets),不寫死在檔案裡
- 商品清單改讀外部 products.json,方便日後直接改檔案而不用碰程式碼
- 狀態檔會被 workflow 額外 commit 回 repo,重啟後才記得上次狀態,避免重複通知

環境變數:
  TELEGRAM_BOT_TOKEN   Telegram Bot Token(必填)
  TELEGRAM_CHAT_ID     Telegram chat_id(必填)
  TIME_BUDGET_SECONDS  這次執行最多跑幾秒(預設 20400 秒 = 5 小時 40 分)
  CHECK_INTERVAL_SECONDS  每輪檢查間隔秒數(預設 60)
"""

import json
import os
import random
import time
import logging
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ============ CONFIG ============

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TIME_BUDGET_SECONDS = int(os.environ.get("TIME_BUDGET_SECONDS", 20400))  # 5h40m
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 60))
JITTER_SECONDS = 15

PRODUCTS_FILE = Path(__file__).parent / "products.json"
STATE_FILE = Path(__file__).parent / "watcher_state.json"

FAIL_ALERT_THRESHOLD = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("amazon_watcher")


def load_products() -> list:
    if not PRODUCTS_FILE.exists():
        log.error(f"找不到 {PRODUCTS_FILE},請建立 products.json")
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
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未設定,無法發送通知")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False},
            timeout=10,
        )
        if resp.status_code != 200:
            log.error(f"Telegram 發送失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Telegram 發送發生例外: {e}")


def get_seller_and_stock(html: str):
    soup = BeautifulSoup(html, "html.parser")
    seller_text = None

    merchant_info = soup.find(id="merchant-info")
    if merchant_info:
        seller_text = merchant_info.get_text(strip=True)

    if not seller_text:
        table = soup.find(id="tabular-buybox")
        if table:
            for row in table.find_all("tr"):
                label = row.find(class_="tabular-buybox-text-message")
                if label:
                    seller_text = label.get_text(strip=True)

    if not seller_text:
        candidate = soup.find(string=lambda s: s and "販売元" in s)
        if candidate:
            parent = candidate.find_parent()
            if parent:
                seller_text = parent.get_text(strip=True)

    availability = soup.find(id="availability")
    in_stock = False
    if availability:
        avail_text = availability.get_text(strip=True)
        in_stock = "在庫あり" in avail_text or "在庫切れ" not in avail_text

    return seller_text, in_stock


def check_product(product: dict, state: dict) -> dict:
    name = product["name"]
    url = product["url"]
    key = url

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")

        seller_text, in_stock = get_seller_and_stock(resp.text)
        is_amazon_seller = bool(seller_text and "Amazon.co.jp" in seller_text)

        prev = state.get(key, {})
        was_amazon = prev.get("is_amazon_seller", False)

        if is_amazon_seller and in_stock and not was_amazon:
            msg = (
                f"🔔 販売元變成 Amazon.co.jp 了!\n"
                f"商品: {name}\n"
                f"時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"連結: {url}"
            )
            send_telegram(msg)
            log.info(f"[通知已發送] {name}")
        else:
            log.info(f"{name}: 販売元={seller_text!r}, 符合Amazon={is_amazon_seller}, 有庫存={in_stock}")

        state[key] = {
            "is_amazon_seller": is_amazon_seller,
            "in_stock": in_stock,
            "seller_text": seller_text,
            "fail_count": 0,
            "last_checked": datetime.now().isoformat(),
        }

    except Exception as e:
        prev = state.get(key, {})
        fail_count = prev.get("fail_count", 0) + 1
        prev["fail_count"] = fail_count
        prev["last_checked"] = datetime.now().isoformat()
        state[key] = prev
        log.error(f"{name}: 抓取失敗 ({e}),連續失敗 {fail_count} 次")

        if fail_count == FAIL_ALERT_THRESHOLD:
            send_telegram(
                f"⚠️ 監控腳本連續 {FAIL_ALERT_THRESHOLD} 次抓取失敗\n"
                f"商品: {name}\n可能被 Amazon 擋掉或頁面結構改變,請檢查腳本。"
            )

    return state


def main():
    products = load_products()
    if not products:
        return

    log.info(f"本次執行時間預算: {TIME_BUDGET_SECONDS} 秒")
    state = load_state()
    deadline = time.monotonic() + TIME_BUDGET_SECONDS

    round_num = 0
    while time.monotonic() < deadline:
        round_num += 1
        for product in products:
            state = check_product(product, state)
            save_state(state)
            time.sleep(random.uniform(2, 5))

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        interval = CHECK_INTERVAL_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
        interval = max(20, min(interval, remaining))
        log.info(f"第 {round_num} 輪結束,{interval:.0f} 秒後再檢查(剩餘時間預算 {remaining:.0f} 秒)")
        time.sleep(interval)

    log.info("本次執行時間預算用完,結束,交給 workflow 接力重啟")


if __name__ == "__main__":
    main()
