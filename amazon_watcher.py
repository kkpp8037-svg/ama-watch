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
  PRODUCTS_FILE        要讀取的商品清單檔名(預設 products.json,可指定 products-1.json 等)
  STATE_FILE           狀態記錄檔名(預設 watcher_state.json,建議跟 PRODUCTS_FILE 對應成組)
  START_OFFSET_SECONDS 啟動後先等待幾秒才開始第一輪(用來跟其他 workflow 錯開請求時間,預設 0)
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
from bs4 import BeautifulSoup

# ============ CONFIG ============

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TIME_BUDGET_SECONDS = int(os.environ.get("TIME_BUDGET_SECONDS", 20400))  # 5h40m
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 60))
JITTER_SECONDS = 15
START_OFFSET_SECONDS = int(os.environ.get("START_OFFSET_SECONDS", 0))

PRODUCTS_FILE = Path(__file__).parent / os.environ.get("PRODUCTS_FILE", "products.json")
STATE_FILE = Path(__file__).parent / os.environ.get("STATE_FILE", "watcher_state.json")

FAIL_ALERT_THRESHOLD = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

# 帶上地區/幣別 cookie,避免被導去「請選擇送貨地址」的過渡頁,盡量拿到真正的商品頁
COOKIES = {
    "i18n-prefs": "JPY",
    "lc-acbjp": "ja_JP",
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
    """
    回傳 (is_amazon_seller, debug_snippet, in_stock)

    Amazon.co.jp 頁面實際上有兩種常見格式,必須都涵蓋:
    1. 句子式(常見於 id="merchant-info"):
       「この商品は、Amazon.co.jpが販売および発送します。」
       「この商品は、○○○が販売、Amazon.co.jpが発送します。」(混合出荷)
    2. 表格式(常見於 id="tabular-buybox"):
       「出荷元」「販売元」各自一列,標籤與數值分開,
       不能用「抓到最後一筆」這種寫法,否則會抓到價格等其他列。
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    merchant_info = soup.find(id="merchant-info")
    if merchant_info:
        candidates.append(merchant_info.get_text(" ", strip=True))

    tabular = soup.find(id="tabular-buybox")
    if tabular:
        for row in tabular.find_all("tr"):
            row_text = row.get_text(" ", strip=True)
            if "販売元" in row_text or "出荷元" in row_text:
                candidates.append(row_text)

    # 保底:整個 buybox 區域的文字都納入候選,避免上面兩種 id 都抓不到
    buybox = soup.find(id="buybox") or soup.find(id="rightCol") or soup.find(id="desktop_buybox_group_1")
    if buybox:
        candidates.append(buybox.get_text(" ", strip=True))

    combined = " | ".join(candidates)
    debug_snippet = combined[:400]

    # 只有「販売元」明確是 Amazon.co.jp 才算數,出荷元不算(出荷元只是物流,販売元才是誰在賣)
    is_amazon_seller = bool(
        re.search(r"Amazon\.co\.jp\s*が\s*販売", combined)
        or re.search(r"販売元\s*[:：]?\s*Amazon\.co\.jp", combined)
        or re.search(r"販売元\s*Amazon\.co\.jp", combined)
    )

    availability = soup.find(id="availability")
    in_stock = False
    if availability:
        avail_text = availability.get_text(strip=True)
        in_stock = "在庫あり" in avail_text or "在庫切れ" not in avail_text

    return is_amazon_seller, debug_snippet, in_stock


def check_product(product: dict, state: dict) -> dict:
    name = product["name"]
    url = product["url"]
    key = url

    try:
        resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")

        html = resp.text
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
        page_title = title_match.group(1).strip() if title_match else "(無標題)"

        is_amazon_seller, debug_snippet, in_stock = get_seller_and_stock(html)

        if not debug_snippet:
            # 完全抓不到任何候選文字,通常代表拿到的不是真正商品頁(可能被導去驗證頁/地區選擇頁)
            log.warning(
                f"{name}: 疑似未取得正常商品頁 | HTTP={resp.status_code} "
                f"內容長度={len(html)} 頁面標題={page_title!r}"
            )

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
            log.info(
                f"{name}: 符合Amazon={is_amazon_seller}, 有庫存={in_stock} | 擷取片段: {debug_snippet!r}"
            )

        state[key] = {
            "is_amazon_seller": is_amazon_seller,
            "in_stock": in_stock,
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
