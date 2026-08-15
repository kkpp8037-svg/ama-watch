#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MM小舖 戰鬥陀螺 補貨監控腳本 — GitHub Actions 自我接力版(Playwright)
====================================================================
這個網站商品是用 JavaScript 動態載入的(Vue.js),不能用單純 requests 抓取,
必須用 Playwright 開無頭瀏覽器實際「打開」頁面、等內容渲染完成再讀取。

監控邏輯:
- 巡覽分類頁(共 3 頁,用點擊分頁按鈕翻頁,不是網址參數)
- 每個商品區塊解析「庫存 數字」與是否有「補貨中」字樣
- 只有「庫存 > 0」且「沒有補貨中」才算是可下單狀態
- 狀態從「不可下單」變成「可下單」才觸發 Telegram 通知(避免重複洗版)

環境變數:
  TELEGRAM_BOT_TOKEN   Telegram Bot Token(必填)
  TELEGRAM_CHAT_ID     Telegram chat_id(必填)
  TIME_BUDGET_SECONDS  這次執行最多跑幾秒(預設 20400 秒 = 5 小時 40 分)
  CHECK_INTERVAL_SECONDS  每輪檢查間隔秒數(預設 30,因為只掃 3 個頁面,可以比 Amazon 版更快)
  STATE_FILE           狀態記錄檔名(預設 mm_watcher_state.json)
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
from playwright.sync_api import sync_playwright

# ============ CONFIG ============

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TIME_BUDGET_SECONDS = int(os.environ.get("TIME_BUDGET_SECONDS", 20400))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 30))
JITTER_SECONDS = 8

STATE_FILE = Path(__file__).parent / os.environ.get("STATE_FILE", "mm_watcher_state.json")

CATEGORY_URL = "https://mmtoyshop.com/category/🌀戰鬥陀螺"
TOTAL_PAGES = 3  # 目前共 3 頁,如果之後商品變多頁數會增加,發現不對再調整

FAIL_ALERT_THRESHOLD = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mm_watcher")


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


def parse_products_from_html(html: str) -> list:
    """
    把渲染完成的 HTML,依「商品連結第一次出現的位置」切成一塊一塊,
    每一塊視為一個商品的完整資訊區塊,再從區塊文字裡找庫存數字跟補貨中字樣。
    這樣不用依賴確切的 CSS class 名稱,對頁面小改版比較有韌性。
    """
    matches = list(re.finditer(r'href="(https://mmtoyshop\.com/item/[^"]+)"', html))
    seen_pos = {}
    for m in matches:
        url = m.group(1)
        if url not in seen_pos:
            seen_pos[url] = m.start()

    boundaries = sorted(seen_pos.items(), key=lambda x: x[1])
    products = []
    for i, (url, pos) in enumerate(boundaries):
        end = boundaries[i + 1][1] if i + 1 < len(boundaries) else len(html)
        block_html = html[pos:end]

        block_text = BeautifulSoup(block_html, "html.parser").get_text(" ", strip=True)

        stock_match = re.search(r"庫存\s*(\d+)", block_text)
        stock_qty = int(stock_match.group(1)) if stock_match else None

        is_restocking = "補貨中" in block_text or "已售完" in block_text or "缺貨" in block_text

        title_match = re.search(r'alt="([^"]+)"', block_html)
        title = title_match.group(1) if title_match else url

        products.append({
            "url": url,
            "title": title,
            "stock_qty": stock_qty,
            "is_restocking": is_restocking,
        })

    return products


def scrape_all_pages(page) -> list:
    all_products = []
    page.goto(CATEGORY_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_selector('a[href*="/item/"]', timeout=15000)

    for page_num in range(1, TOTAL_PAGES + 1):
        if page_num > 1:
            # 點擊分頁按鈕(用精確文字比對數字,避免誤點到價格等其他含數字的元素)
            try:
                page.get_by_text(str(page_num), exact=True).first.click(timeout=5000)
                page.wait_for_timeout(1500)  # 給 JS 一點時間重新渲染
                page.wait_for_selector('a[href*="/item/"]', timeout=15000)
            except Exception as e:
                log.warning(f"點擊第 {page_num} 頁失敗,跳過: {e}")
                continue

        html = page.content()
        products = parse_products_from_html(html)
        log.info(f"第 {page_num} 頁擷取到 {len(products)} 個商品連結")
        all_products.extend(products)

    # 依網址去重(同商品可能因為分頁重疊或多次連結而重複)
    dedup = {}
    for p in all_products:
        dedup[p["url"]] = p
    return list(dedup.values())


def check_all(state: dict, browser) -> dict:
    page = browser.new_page(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="zh-TW",
    )
    try:
        products = scrape_all_pages(page)
    except Exception as e:
        log.error(f"掃描失敗: {e}")
        page.close()
        raise
    page.close()

    if not products:
        log.warning("這輪完全沒抓到任何商品,可能頁面結構改變或載入失敗")
        return state

    for p in products:
        url = p["url"]
        title = p["title"]
        stock_qty = p["stock_qty"]
        is_restocking = p["is_restocking"]

        is_available = bool(stock_qty and stock_qty > 0 and not is_restocking)

        prev = state.get(url, {})
        was_available = prev.get("is_available", False)

        if is_available and not was_available:
            msg = (
                f"🔔 MM小舖補貨了!\n"
                f"商品: {title}\n"
                f"庫存: {stock_qty}\n"
                f"時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"連結: {url}"
            )
            send_telegram(msg)
            log.info(f"[通知已發送] {title}")
        else:
            log.info(
                f"{title[:30]}: 庫存={stock_qty}, 補貨中={is_restocking}, 可下單={is_available}"
            )

        state[url] = {
            "is_available": is_available,
            "stock_qty": stock_qty,
            "is_restocking": is_restocking,
            "title": title,
            "last_checked": datetime.now().isoformat(),
        }

    save_state(state)
    return state


def main():
    log.info(f"本次執行時間預算: {TIME_BUDGET_SECONDS} 秒")
    state = load_state()
    deadline = time.monotonic() + TIME_BUDGET_SECONDS

    fail_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        round_num = 0
        while time.monotonic() < deadline:
            round_num += 1
            try:
                state = check_all(state, browser)
                fail_count = 0
            except Exception as e:
                fail_count += 1
                log.error(f"第 {round_num} 輪執行失敗 ({e}),連續失敗 {fail_count} 次")
                if fail_count == FAIL_ALERT_THRESHOLD:
                    send_telegram(
                        f"⚠️ MM小舖監控腳本連續 {FAIL_ALERT_THRESHOLD} 次失敗,請檢查腳本或網站是否改版"
                    )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            interval = CHECK_INTERVAL_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
            interval = max(15, min(interval, remaining))
            log.info(f"第 {round_num} 輪結束,{interval:.0f} 秒後再檢查(剩餘時間預算 {remaining:.0f} 秒)")
            time.sleep(interval)

        browser.close()

    log.info("本次執行時間預算用完,結束,交給 workflow 接力重啟")


if __name__ == "__main__":
    main()
