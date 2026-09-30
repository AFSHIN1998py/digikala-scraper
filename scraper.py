"""
==============================================
اسکرپر حرفه‌ای دیجی‌کالا - نسخه نهایی v2.1
==============================================
"""

import os
import sys
import csv
import time
import random
import sqlite3
import logging
import argparse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------- بارگذاری تنظیمات ----------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
DB_PATH = os.getenv("DB_PATH", "data/prices.db")
CSV_PATH = os.getenv("CSV_PATH", "data/prices.csv")
LOG_PATH = os.getenv("LOG_PATH", "data/scraper.log")

# ساخت پوشه data اگه وجود نداره
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)

# ---------- لاگ‌گیری حرفه‌ای ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ============================================
# بخش ۱: User-Agent های تصادفی
# ============================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


def get_headers() -> dict:
    """هدرهای تصادفی برای هر درخواست"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


# ============================================
# بخش ۲: دیتابیس
# ============================================
def init_db():
    """ساخت جدول‌ها اگه وجود نداشته باشه"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            title TEXT,
            target_price INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            price INTEGER,
            timestamp TEXT,
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        )
    """)

    conn.commit()
    conn.close()


def add_product(url: str, title: str, target_price: int = 0) -> int | None:
    """اضافه کردن محصول به دیتابیس و برگرداندن ID"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            INSERT OR IGNORE INTO products (url, title, target_price, created_at)
            VALUES (?, ?, ?, ?)
        """, (url, title, target_price, datetime.now().isoformat()))
        conn.commit()

        row = c.execute("SELECT product_id FROM products WHERE url = ?", (url,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error as e:
        logger.error(f"DB Error: {e}")
        return None
    finally:
        conn.close()


def save_price(product_id: int, price: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO price_history (product_id, price, timestamp)
            VALUES (?, ?, ?)
        """, (product_id, price, datetime.now().isoformat()))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"DB Error: {e}")
    finally:
        conn.close()


def get_last_price(product_id: int) -> int | None:
    """آخرین قیمت ثبت‌شده"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute("""
        SELECT price FROM price_history
        WHERE product_id = ?
        ORDER BY timestamp DESC LIMIT 1
    """, (product_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def export_to_csv():
    """خروجی اکسل از همه تاریخچه"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute("""
        SELECT p.title, p.url, ph.price, ph.timestamp
        FROM price_history ph
        JOIN products p ON p.product_id = ph.product_id
        ORDER BY ph.timestamp DESC
    """).fetchall()
    conn.close()

    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["عنوان", "لینک", "قیمت", "زمان"])
        writer.writerows(rows)

    logger.info(f"✅ خروجی CSV ذخیره شد: {CSV_PATH}")


# ============================================
# بخش ۳: اسکرپینگ (مبتنی بر API دیجی‌کالا)
# ============================================
def extract_product_id(url: str) -> str | None:
    """استخراج product ID از لینک دیجی‌کالا"""
    import re
    match = re.search(r"dkp-(\d+)", url)
    if match:
        return match.group(1)
    # اگه لینک به شکل /product/12345678/ بود
    match = re.search(r"/product/(\d+)", url)
    if match:
        return match.group(1)
    return None


def fetch_product_api(product_id: str) -> dict | None:
    """دریافت اطلاعات محصول از API نسخه ۲ دیجی‌کالا"""
    api_url = f"https://api.digikala.com/v2/product/{product_id}/"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(api_url, headers=headers, timeout=20)
            if r.status_code == 200:
                return r.json()
            else:
                logger.warning(f"⚠️ API v2 کد {r.status_code} (تلاش {attempt}/{MAX_RETRIES})")
                time.sleep(3)
        except requests.exceptions.Timeout:
            logger.warning(f"⏱ Timeout (تلاش {attempt}/{MAX_RETRIES})")
            time.sleep(3)
        except Exception as e:
            logger.error(f"❌ خطای API: {e}")
            time.sleep(3)

    return None


def fetch_page(url: str) -> str | None:
    """دریافت HTML با Retry و پروکسی (روش جایگزین)"""
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                url,
                headers=get_headers(),
                proxies=proxies,
                timeout=15,
            )
            if r.status_code == 200:
                return r.text
            elif r.status_code == 429:
                logger.warning(f"⚠️ Rate limit! صبر می‌کنیم... (تلاش {attempt})")
                time.sleep(30 * attempt)
            else:
                logger.warning(f"⚠️ کد {r.status_code} (تلاش {attempt}/{MAX_RETRIES})")
                time.sleep(5)
        except requests.exceptions.Timeout:
            logger.warning(f"⏱ Timeout (تلاش {attempt}/{MAX_RETRIES})")
            time.sleep(5)
        except requests.exceptions.ProxyError:
            logger.error("❌ پروکسی کار نمی‌کنه!")
            return None
        except Exception as e:
            logger.error(f"❌ خطا: {e}")
            time.sleep(5)

    logger.error(f"❌ بعد از {MAX_RETRIES} تلاش، صفحه دریافت نشد.")
    return None


def extract_price(html: str) -> tuple[str, int] | None:
    """استخراج عنوان و قیمت از HTML (روش جایگزین)"""
    try:
        soup = BeautifulSoup(html, "lxml")

        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "نامشخص"

        price_tag = soup.find("div", {"data-testid": "price-final"})
        if price_tag:
            price_text = price_tag.get_text(strip=True)
        else:
            price_text = ""
            for tag in soup.find_all(["span", "div", "p"]):
                text = tag.get_text(strip=True)
                if "تومان" in text and any(c.isdigit() for c in text):
                    price_text = text
                    break

        digits = "".join(c for c in price_text if c.isdigit())
        if not digits:
            logger.warning("⚠️ قیمت پیدا نشد.")
            return None

        price = int(digits)
        if price < 1000:
            logger.warning(f"⚠️ قیمت مشکوک: {price}")
            return None

        return title, price

    except Exception as e:
        logger.error(f"❌ خطای پارس: {e}")
        return None


def parse_api_response(data: dict) -> tuple[str, int] | None:
    """استخراج عنوان و قیمت از پاسخ JSON نسخه ۲ دیجی‌کالا"""
    try:
        product = data.get("data", {}).get("product", {})
        title = product.get("title_fa", "نامشخص")

        # تلاش از default_variant
        selling_price = None
        variant = product.get("default_variant") or {}
        selling_price = variant.get("price", {}).get("selling_price")

        # اگه default_variant قیمت نداشت، از variants بگرد
        if selling_price is None:
            for v in product.get("variants", []) or []:
                p = v.get("price", {}).get("selling_price")
                if p:
                    selling_price = p
                    break

        if selling_price is None:
            logger.warning("⚠️ قیمت در API v2 پیدا نشد.")
            return None

        # قیمت API به ریال هست → تبدیل به تومان
        price_toman = int(selling_price) // 10

        return title, price_toman

    except Exception as e:
        logger.error(f"❌ خطای پارس API: {e}")
        return None


# ============================================
# بخش ۴: تلگرام
# ============================================
def send_telegram(message: str) -> bool:
    """ارسال پیام به تلگرام"""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("⚠️ توکن/چت‌آیدی تنظیم نشده - پیام ارسال نشد.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)

        if r.status_code == 200:
            logger.info("✅ پیام تلگرام ارسال شد.")
            return True
        else:
            logger.error(f"❌ تلگرام خطا: {r.status_code} - {r.text}")
            return False
    except Exception as e:
        logger.error(f"❌ تلگرام: {e}")
        return False


# ============================================
# بخش ۵: منطق اصلی
# ============================================
def check_product(url: str, target_price: int = 0) -> dict | None:
    """بررسی یه محصول (اول از API، بعد HTML)"""
    logger.info(f"🔍 چک می‌کنم: {url}")

    title, price = None, None

    # روش ۱: API دیجی‌کالا
    product_id = extract_product_id(url)
    if product_id:
        data = fetch_product_api(product_id)
        if data:
            result = parse_api_response(data)
            if result:
                title, price = result
                logger.info(f"✅ از API دریافت شد: {title}")

    # روش ۲: اگه API جواب نداد، HTML
    if title is None:
        logger.info("🔄 API جواب نداد، تلاش با HTML...")
        html = fetch_page(url)
        if html:
            result = extract_price(html)
            if result:
                title, price = result

    if title is None or price is None:
        logger.error(f"❌ نتونستم اطلاعات {url} رو بگیرم.")
        return None

    product_id_db = add_product(url, title, target_price)
    if product_id_db:
        save_price(product_id_db, price)

    last = get_last_price(product_id_db) if product_id_db else None
    changed = last is not None and last != price

    if changed:
        diff = price - last
        emoji = "🔻" if diff < 0 else "🔺"
        send_telegram(
            f"{emoji} <b>تغییر قیمت!</b>\n\n"
            f"📦 {title}\n"
            f"💰 قبلی: {last:,} تومان\n"
            f"💰 جدید: <b>{price:,} تومان</b>\n"
            f"📊 تغییر: {abs(diff):,} تومان\n"
            f"🔗 {url}"
        )

    if target_price > 0 and price <= target_price:
        send_telegram(
            f"🎯 <b>قیمت به هدف رسید!</b>\n\n"
            f"📦 {title}\n"
            f"💰 قیمت: <b>{price:,} تومان</b>\n"
            f"🎯 هدف: {target_price:,} تومان\n"
            f"🔗 {url}"
        )

    logger.info(f"✅ {title}: {price:,} تومان")
    return {"title": title, "price": price, "changed": changed}


def load_urls_from_file(path: str) -> list[tuple[str, int]]:
    """خوندن لینک‌ها از فایل متنی"""
    items = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                url = parts[0].strip()
                target = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 0
                items.append((url, target))
    except FileNotFoundError:
        logger.error(f"❌ فایل پیدا نشد: {path}")
    return items


def monitor(urls_file: str, once: bool = False):
    """حلقه اصلی پایش"""
    items = load_urls_from_file(urls_file)

    if not items:
        logger.error("❌ هیچ لینکی برای پایش پیدا نشد.")
        return

    logger.info(f"🚀 شروع پایش {len(items)} محصول...")

    while True:
        for url, target in items:
            try:
                check_product(url, target)
            except Exception as e:
                logger.error(f"❌ خطا در {url}: {e}")
            time.sleep(random.uniform(3, 8))

        export_to_csv()

        if once:
            logger.info("✅ یک دور تموم شد. (حالت --once)")
            break

        logger.info(f"⏸ خواب {CHECK_INTERVAL} ثانیه...")
        time.sleep(CHECK_INTERVAL)


# ============================================
# بخش ۶: CLI (خط فرمان)
# ============================================
def main():
    parser = argparse.ArgumentParser(description="اسکرپر دیجی‌کالا")
    parser.add_argument("--url", "-u", help="لینک یه محصول برای تست سریع")
    parser.add_argument("--target", "-t", type=int, default=0, help="قیمت هدف (تومان)")
    parser.add_argument("--file", "-f", default="urls.txt", help="فایل لینک‌ها")
    parser.add_argument("--once", action="store_true", help="فقط یه بار چک کن و خارج شو")
    parser.add_argument("--export", action="store_true", help="خروجی CSV بگیر و خارج شو")

    args = parser.parse_args()

    init_db()

    if args.export:
        export_to_csv()
        return

    if args.url:
        result = check_product(args.url, args.target)
        if result:
            print(f"\n✅ {result['title']}")
            print(f"💰 {result['price']:,} تومان")
        return

    monitor(args.file, once=args.once)


if __name__ == "__main__":
    main()