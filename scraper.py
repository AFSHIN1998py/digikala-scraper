"""
==============================================
اسکرپر حرفه‌ای دیجی‌کالا - نسخه نهایی v2.3
==============================================
قابلیت‌ها:
- پایش قیمت با لینک محصول
- جستجوی محصول با اسم (با مرتب‌سازی و امتیاز)
- هشدار تلگرام (تغییر قیمت + قیمت هدف)
- خروجی CSV
- ذخیره در SQLite
"""

import psycopg2
import os
import sys
import csv
import time
import random
import logging
import argparse
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------- بارگذاری تنظیمات ----------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "21600"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
CSV_PATH = os.getenv("CSV_PATH", "data/prices.csv")
LOG_PATH = os.getenv("LOG_PATH", "data/scraper.log")

# اطمینان از وجود پوشه log
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

# دیکشنری کدهای مرتب‌سازی دیجی‌کالا
SORT_CODES = {
    "relevance": 1,     # مرتبط‌ترین (پیش‌فرض)
    "cheapest": 2,      # ارزان‌ترین
    "expensive": 3,     # گران‌ترین
    "newest": 4,        # جدیدترین
    "bestseller": 5,    # پرفروش‌ترین
    "popular": 6,       # پربازدیدترین
    "rating": 7,        # بالاترین امتیاز
}

SORT_NAMES_FA = {
    "relevance": "مرتبط‌ترین",
    "cheapest": "ارزان‌ترین",
    "expensive": "گران‌ترین",
    "newest": "جدیدترین",
    "bestseller": "پرفروش‌ترین",
    "popular": "پربازدیدترین",
    "rating": "بالاترین امتیاز",
}


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
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("❌ DATABASE_URL تنظیم نشده!")
        return
    conn = psycopg2.connect(db_url)    
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            product_id SERIAL PRIMARY KEY,
            url TEXT UNIQUE,
            title TEXT,
            target_price INTEGER DEFAULT 0,
            created_at TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(product_id),
            price INTEGER,
            timestamp TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def add_product(url: str, title: str, target_price: int = 0) -> int | None:
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO products (url, title, target_price, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (url) DO NOTHING
            RETURNING product_id
        """, (url, title, target_price, datetime.now()))
        result = c.fetchone()
        conn.commit()
        
        if result:
            return result[0]
        
        # اگه محصول از قبل بود، ID رو بگیر
        c.execute("SELECT product_id FROM products WHERE url = %s", (url,))
        row = c.fetchone()
        return row[0] if row else None
    except psycopg2.Error as e:
        logger.error(f"DB Error: {e}")
        return None
    finally:
        conn.close()


def save_price(product_id: int, price: int):
    """ذخیره قیمت — فقط اگه با آخرین قیمت فرق داشته باشه"""
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    c = conn.cursor()
    try:
        # آخرین قیمت ذخیره‌شده رو بگیر
        c.execute("""
            SELECT price FROM price_history
            WHERE product_id = %s
            ORDER BY timestamp DESC LIMIT 1
        """, (product_id,))
        row = c.fetchone()

        # اگه قیمت عوض نشده، ذخیره نکن
        if row and row[0] == price:
            return

        # ذخیره قیمت جدید (اولین بار یا تغییر)
        c.execute("""
            INSERT INTO price_history (product_id, price, timestamp)
            VALUES (%s, %s, %s)
        """, (product_id, price, datetime.now()))
        conn.commit()
    except psycopg2.Error as e:
        logger.error(f"DB Error: {e}")
    finally:
        conn.close()

def get_last_price(product_id: int) -> int | None:
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    c = conn.cursor()
    c.execute("""
        SELECT price FROM price_history
        WHERE product_id = %s
        ORDER BY timestamp DESC LIMIT 1
    """, (product_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def export_to_csv():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    c = conn.cursor()
    c.execute("""
        SELECT p.title, p.url, ph.price, ph.timestamp
        FROM price_history ph
        JOIN products p ON p.product_id = ph.product_id
        ORDER BY ph.timestamp DESC
    """)
    rows = c.fetchall()
    conn.close()

    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["عنوان", "لینک", "قیمت", "زمان"])
        for title, url, price, ts in rows:
            # فرمت‌دهی به زمان
            try:
                if hasattr(ts, "strftime"):
                    time_str = ts.strftime("%Y-%m-%d %H:%M")
                else:
                    time_str = str(ts)[:16]
            except Exception:
                time_str = str(ts)

            writer.writerow([title, url, price, time_str])

    logger.info(f"✅ خروجی CSV ذخیره شد: {CSV_PATH}")


# ============================================
# بخش ۳: اسکرپینگ (API v2 دیجی‌کالا)
# ============================================
def extract_product_id(url: str) -> str | None:
    """استخراج product ID از لینک دیجی‌کالا"""
    import re
    match = re.search(r"dkp-(\d+)", url)
    if match:
        return match.group(1)
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

        selling_price = None
        variant = product.get("default_variant") or {}
        selling_price = variant.get("price", {}).get("selling_price")

        if selling_price is None:
            for v in product.get("variants", []) or []:
                p = v.get("price", {}).get("selling_price")
                if p:
                    selling_price = p
                    break

        if selling_price is None:
            logger.warning("⚠️ قیمت در API v2 پیدا نشد.")
            return None

        price_toman = int(selling_price) // 10

        return title, price_toman

    except Exception as e:
        logger.error(f"❌ خطای پارس API: {e}")
        return None

# ============================================
# بخش ۴.۵: جستجوی ترب (مقایسه قیمت)
# ============================================
def search_torob(query: str, limit: int = 5) -> list[dict]:
    """
    جستجوی محصول در ترب (Torob)
    از curl_cffi استفاده می‌کنه تا بلاک نشه
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        logger.error("❌ curl_cffi نصب نیست. نصب کن: pip install curl_cffi")
        return []

    q = urllib.parse.quote(query)
    url = f"https://api.torob.com/v4/base-product/search/?q={q}&page=0&size={limit}"

    try:
        r = curl_requests.get(url, impersonate="chrome", timeout=20)
        if r.status_code != 200:
            logger.warning(f"⚠️ ترب کد {r.status_code}")
            return []

        data = r.json()
        products = data.get("results", []) or []

        results = []
        for p in products[:limit]:
            random_key = p.get("random_key")
            if not random_key:
                continue

            # قیمت (قبلاً به تومان)
            price = p.get("price")
            if price is not None:
                try:
                    price = int(price)
                except (ValueError, TypeError):
                    price = None

            # URL کامل
            rel_url = p.get("web_client_absolute_url", "")
            full_url = f"https://torob.com{rel_url}" if rel_url else None

            results.append({
                "name": p.get("name1", "نامشخص"),
                "price": price,
                "price_text": p.get("price_text", ""),
                "url": full_url,
                "shop_text": p.get("shop_text", ""),
                "random_key": random_key,
            })

        logger.info(f"🏆 ترب: {len(results)} نتیجه برای «{query}»")
        return results
    except Exception as e:
        logger.error(f"❌ ترب: {e}")
        return []

# ============================================
# بخش ۴: جستجوی محصول با اسم
# ============================================
def search_products(query: str, limit: int = 10, sort: str = "relevance",
                    min_price: int = 0, max_price: int = 0,
                    only_discounted: bool = False,
                    only_available: bool = False,
                    max_pages: int = 3) -> list[dict]:
    """
    جستجوی محصول با فیلترهای سمت پایتون + چند صفحه
    
    Args:
        max_pages: تعداد صفحاتی که از API گرفته می‌شه (هر صفحه ۲۰ محصول)
    """
    q = urllib.parse.quote(query)

    if sort in ("cheapest", "expensive"):
        api_sort = 1  # relevance - خودمون بعداً مرتب می‌کنیم
    else:
        api_sort = SORT_CODES.get(sort, 1)

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
    }

    all_products = []
    for page in range(1, max_pages + 1):
        url = f"https://api.digikala.com/v1/search/?q={q}&sort={api_sort}&page={page}"
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code != 200:
                logger.warning(f"⚠️ صفحه {page}: کد {r.status_code}")
                break
            data = r.json()
            products = data.get("data", {}).get("products", []) or []
            if not products:
                break
            all_products.extend(products)
            logger.info(f"📄 صفحه {page}: {len(products)} محصول")
        except Exception as e:
            logger.error(f"❌ خطای صفحه {page}: {e}")
            break

    logger.info(f"📊 کل: {len(all_products)} محصول خام")

    # ---------- استخراج اطلاعات ----------
    results = []
    seen_ids = set()
    for p in all_products:
        product_id = p.get("id")
        if not product_id or product_id in seen_ids:
            continue
        seen_ids.add(product_id)

        title = p.get("title_fa", "نامشخص")
        variant = p.get("default_variant") or {}
        price_data = variant.get("price", {}) or {}
        price_rial = price_data.get("selling_price")
        rrp_rial = price_data.get("rrp_price")
        discount = price_data.get("discount_percent", 0) or 0
        stock = price_data.get("marketable_stock", 0) or 0

        price_toman = int(price_rial) // 10 if price_rial else None
        rrp_toman = int(rrp_rial) // 10 if rrp_rial else None

        rating = p.get("rating", {}) or {}
        rating_rate = rating.get("rate")
        rating_count = rating.get("count")

        images = p.get("images", {}) or {}
        main_img = images.get("main", {}) if isinstance(images, dict) else {}
        img_urls = main_img.get("url", []) if isinstance(main_img, dict) else []
        if isinstance(img_urls, str):
            img_urls = [img_urls]
        image = img_urls[0] if img_urls else None

        results.append({
            "id": product_id,
            "title": title,
            "url": f"https://www.digikala.com/product/dkp-{product_id}/",
            "price": price_toman,
            "rrp_price": rrp_toman,
            "discount": discount,
            "stock": stock,
            "rating": rating_rate,
            "rating_count": rating_count,
            "image": image,
        })

    logger.info(f"📊 یکتا: {len(results)} محصول")

    # ---------- فیلترها ----------
    filtered = []
    for r in results:
        if min_price > 0 and (r["price"] is None or r["price"] < min_price):
            continue
        if max_price > 0 and (r["price"] is None or r["price"] > max_price):
            continue
        if only_discounted and r["discount"] <= 0:
            continue
        if only_available and r["stock"] <= 0:
            continue
        filtered.append(r)

    logger.info(f"📊 بعد از فیلتر: {len(filtered)}")

    # ---------- مرتب‌سازی ----------
    if sort == "cheapest":
        filtered = [r for r in filtered if r.get("price")]
        filtered.sort(key=lambda x: x["price"])
    elif sort == "expensive":
        filtered = [r for r in filtered if r.get("price")]
        filtered.sort(key=lambda x: x["price"], reverse=True)

    return filtered[:limit]

def resolve_url_or_search(entry: str) -> str | None:
    """اگه لینک بود همون رو برگردون؛ اگه اسم بود، سرچ کن و اولین نتیجه رو بده"""
    entry = entry.strip()
    if entry.startswith("http"):
        return entry

    logger.info(f"🔍 جستجو برای: {entry}")
    results = search_products(entry, limit=1)
    if results and results[0].get("url"):
        logger.info(f"✅ پیدا شد: {results[0]['title']}")
        return results[0]["url"]

    logger.warning(f"⚠️ محصولی با اسم «{entry}» پیدا نشد.")
    return None


# ============================================
# بخش ۵: تلگرام
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


def send_startup_message():
    """ارسال پیام شروع به تلگرام (برای تست اتصال)"""
    msg = (
        "🚀 <b>اسکرپر دیجی‌کالا روشن شد!</b>\n\n"
        "✅ اتصال تلگرام برقرار است.\n"
        "⏰ شروع پایش خودکار..."
    )
    success = send_telegram(msg)
    if success:
        logger.info("✅ پیام شروع تلگرام ارسال شد.")
    else:
        logger.error("❌ پیام شروع تلگرام ارسال نشد!")
    return success


# ============================================
# بخش ۵.۵: گزارش روزانه
# ============================================
def get_daily_report() -> str | None:
    """ساخت متن گزارش روزانه"""
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        c = conn.cursor()

        c.execute("""
            SELECT product_id, title, url, target_price
            FROM products ORDER BY product_id DESC
        """)
        products = c.fetchall()

        if not products:
            conn.close()
            return None

        lines = ["📊 <b>گزارش روزانه قیمت‌ها</b>", ""]

        cheaper = pricier = same = hit_target = 0

        for pid, title, url, target in products:
            # دو رکورد آخر
            c.execute("""
                SELECT price FROM price_history
                WHERE product_id = %s ORDER BY timestamp DESC LIMIT 2
            """, (pid,))
            records = c.fetchall()

            if not records:
                continue

            last_price = records[0][0]

            # مقایسه با رکورد قبلی
            if len(records) >= 2 and records[1][0] != last_price:
                prev = records[1][0]
                diff = last_price - prev
                percent = (diff / prev) * 100 if prev else 0
                if diff < 0:
                    emoji = "🔻"
                    change = f"{abs(diff):,}- ({abs(percent):.1f}%)"
                    cheaper += 1
                else:
                    emoji = "🔺"
                    change = f"{diff:,}+ ({percent:.1f}%)"
                    pricier += 1
            else:
                emoji = "➡️"
                change = "بدون تغییر"
                same += 1

            # چک قیمت هدف
            target_str = ""
            if target and last_price <= target:
                target_str = " ✅ به هدف رسید!"
                hit_target += 1

            title_short = title[:45] if len(title) > 45 else title

            lines.append(f"{emoji} <b>{title_short}</b>")
            lines.append(f"   💰 {last_price:,} تومان — {change}{target_str}")
            lines.append("")

        conn.close()

        # خلاصه
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("📈 <b>خلاصه:</b>")
        if cheaper: lines.append(f"🔻 {cheaper} ارزان‌تر")
        if pricier: lines.append(f"🔺 {pricier} گران‌تر")
        if same:    lines.append(f"➡️ {same} بدون تغییر")
        if hit_target: lines.append(f"🎯 {hit_target} به هدف رسید")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Daily report error: {e}")
        return None


def send_daily_report():
    """ساخت و ارسال گزارش روزانه"""
    logger.info("📊 ساخت گزارش روزانه...")
    report = get_daily_report()
    if not report:
        send_telegram("📊 گزارش روزانه:\n\nمحصولی برای گزارش وجود نداره.")
        return
    success = send_telegram(report)
    if success:
        logger.info("✅ گزارش روزانه ارسال شد.")
    else:
        logger.error("❌ ارسال گزارش روزانه ناموفق!")

# ============================================
# بخش ۶: منطق اصلی
# ============================================
def check_product(url: str, target_price: int = 0) -> dict | None:
    """بررسی یه محصول (اول از API، بعد HTML)"""

    # اگه اسم بود نه لینک، اول سرچ کن
    if url.startswith("SEARCH:"):
        query = url.replace("SEARCH:", "")
        real_url = resolve_url_or_search(query)
        if not real_url:
            return None
        url = real_url

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

    # قبل از ذخیره، آخرین قیمت رو بگیر
    product_id_db = add_product(url, title, target_price)
    last_price = get_last_price(product_id_db) if product_id_db else None

    # ذخیره قیمت جدید
    if product_id_db:
        save_price(product_id_db, price)

    # چک تغییر قیمت
    changed = last_price is not None and last_price != price
    if changed:
        diff = price - last_price
        percent = (diff / last_price) * 100 if last_price else 0
        emoji = "🔻" if diff < 0 else "🔺"
        send_telegram(
            f"{emoji} <b>تغییر قیمت!</b>\n\n"
            f"📦 {title}\n"
            f"💰 قبلی: {last_price:,} تومان\n"
            f"💰 جدید: <b>{price:,} تومان</b>\n"
            f"📊 تغییر: {abs(diff):,} تومان ({abs(percent):.1f}٪)\n"
            f"🔗 {url}"
        )

    # چک قیمت هدف
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
    """خوندن لینک‌ها یا اسم محصولات از فایل"""
    items = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split("|")
                entry = parts[0].strip()
                target = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 0

                if entry.startswith("http"):
                    items.append((entry, target))
                else:
                    items.append((f"SEARCH:{entry}", target))
    except FileNotFoundError:
        logger.error(f"❌ فایل پیدا نشد: {path}")
    return items


def load_products_from_db() -> list[tuple[str, int]]:
    """خوندن محصولات از دیتابیس PostgreSQL"""
    try:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            logger.error("❌ DATABASE_URL تنظیم نشده!")
            return []
        conn = psycopg2.connect(db_url)
        c = conn.cursor()
        c.execute("SELECT url, target_price FROM products")
        rows = c.fetchall()
        conn.close()
        return [(url, target) for url, target in rows if url]
    except Exception as e:
        logger.error(f"DB load error: {e}")
        return []

def monitor(urls_file: str, once: bool = False):
    """حلقه اصلی پایش - هر چرخه از DB و urls.txt می‌خونه"""
    logger.info("🚀 شروع حلقه پایش...")

    while True:
        # هر چرخه دوباره از DB و فایل می‌خونیم (چون محصولات ممکنه جدید اضافه بشن)
        db_items = load_products_from_db()
        file_items = load_urls_from_file(urls_file)

        # ادغام بدون تکرار
        seen = set()
        items = []
        for url, target in db_items:
            if url not in seen:
                seen.add(url)
                items.append((url, target))
        for url, target in file_items:
            if url not in seen:
                seen.add(url)
                items.append((url, target))

        if not items:
            logger.warning("⚠️ هیچ محصولی برای پایش نیست.")
        else:
            logger.info(f"📊 شروع پایش {len(items)} محصول...")
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
# بخش ۷: CLI (خط فرمان)
# ============================================
def main():
    parser = argparse.ArgumentParser(description="اسکرپر دیجی‌کالا")
    parser.add_argument("--url", "-u", help="لینک یه محصول برای تست سریع")
    parser.add_argument("--target", "-t", type=int, default=0, help="قیمت هدف (تومان)")
    parser.add_argument("--file", "-f", default="urls.txt", help="فایل لینک‌ها")
    parser.add_argument("--once", action="store_true", help="فقط یه بار چک کن و خارج شو")
    parser.add_argument("--export", action="store_true", help="خروجی CSV بگیر و خارج شو")
    parser.add_argument("--search", "-s", help="جستجوی محصول با اسم")
    parser.add_argument("--sort", choices=list(SORT_CODES.keys()),
                        default="relevance", help="نحوه مرتب‌سازی نتایج جستجو")

    args = parser.parse_args()

    init_db()

    # جستجو
    if args.search:
        results = search_products(args.search, limit=10, sort=args.sort)
        if not results:
            print("❌ چیزی پیدا نشد.")
            return

        print(f"\n🔍 {len(results)} نتیجه برای: «{args.search}»")
        print(f"📊 مرتب‌سازی: {SORT_NAMES_FA.get(args.sort, args.sort)}\n")

        for i, r in enumerate(results, 1):
            price_str = f"{r['price']:,} تومان" if r.get("price") else "قیمت نامشخص"

            rating_str = ""
            if r.get("rating") and r.get("rating_count", 0) >= 10:
                stars = r['rating'] / 20  # تبدیل 0-100 به 0-5
                rating_str = f" | ⭐ {stars:.1f}/5 ({r['rating_count']} نظر)"

            print(f"{i}. {r['title']}")
            print(f"   💰 {price_str}{rating_str}")
            print(f"   🔗 {r['url']}")
            print()
        return

    # خروجی CSV
    if args.export:
        export_to_csv()
        return

    # تست سریع یه محصول
    if args.url:
        result = check_product(args.url, args.target)
        if result:
            print(f"\n✅ {result['title']}")
            print(f"💰 {result['price']:,} تومان")
        return

    # پایش دائم
    send_startup_message()
    monitor(args.file, once=args.once)


if __name__ == "__main__":
    main()
