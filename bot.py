"""
==============================================
ربات تلگرام اسکرپر دیجی‌کالا - نسخه ۲
==============================================
- صفحه‌بندی نتایج
- صفحه جزئیات محصول (با عکس)
- ویرایش و حذف و لغو
- منوی تعاملی
"""

import os
import html
import sqlite3
import logging
import threading
import time
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

from scraper import (
    search_products, export_to_csv, init_db, monitor,
    send_startup_message, DB_PATH, CSV_PATH, BOT_TOKEN,
    SORT_NAMES_FA,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("bot")

PAGE_SIZE = 10
MAX_RESULTS = 40
FETCH_PER_PAGE = 10


def esc(text) -> str:
    """escape کردن HTML"""
    return html.escape(str(text)) if text else ""


# ============================================
# دیتابیس
# ============================================
def get_all_products() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute("""
        SELECT product_id, url, title, target_price
        FROM products ORDER BY product_id DESC
    """).fetchall()
    result = []
    for pid, url, title, target in rows:
        last = c.execute("""
            SELECT price FROM price_history
            WHERE product_id = ? ORDER BY timestamp DESC LIMIT 1
        """, (pid,)).fetchone()
        result.append({
            "id": pid, "url": url, "title": title,
            "target": target,
            "last_price": last[0] if last else None,
        })
    conn.close()
    return result


def get_product(product_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute("""
        SELECT product_id, url, title, target_price
        FROM products WHERE product_id = ?
    """, (product_id,)).fetchone()
    if not row:
        conn.close()
        return None
    pid, url, title, target = row
    last = c.execute("""
        SELECT price FROM price_history
        WHERE product_id = ? ORDER BY timestamp DESC LIMIT 1
    """, (pid,)).fetchone()
    history = c.execute("""
        SELECT price, timestamp FROM price_history
        WHERE product_id = ? ORDER BY timestamp DESC LIMIT 5
    """, (pid,)).fetchall()
    conn.close()
    return {
        "id": pid, "url": url, "title": title, "target": target,
        "last_price": last[0] if last else None,
        "history": history,
    }


def delete_product(product_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("DELETE FROM price_history WHERE product_id = ?", (product_id,))
        c.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"DB: {e}")
        return False
    finally:
        conn.close()


def update_target_price(product_id: int, target: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("UPDATE products SET target_price = ? WHERE product_id = ?",
                  (target, product_id))
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def insert_product(url: str, title: str, target: int = 0) -> int | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            INSERT OR IGNORE INTO products (url, title, target_price, created_at)
            VALUES (?, ?, ?, ?)
        """, (url, title, target, datetime.now().isoformat()))
        conn.commit()
        row = c.execute("SELECT product_id FROM products WHERE url = ?", (url,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error as e:
        logger.error(f"DB: {e}")
        return None
    finally:
        conn.close()


# ============================================
# کیبوردها
# ============================================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن محصول", callback_data="menu:add")],
        [InlineKeyboardButton("📋 لیست محصولات", callback_data="menu:list")],
        [InlineKeyboardButton("📊 خروجی CSV", callback_data="menu:export")],
        [InlineKeyboardButton("❓ راهنما", callback_data="menu:help")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ لغو", callback_data="cncl")],
    ])


def results_kb(results: list, page: int, total_pages: int,
               total_count: int, active_filters: dict = None) -> InlineKeyboardMarkup:
    kb = []
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_results = results[start:end]

    for i, r in enumerate(page_results):
        abs_idx = start + i
        price_str = f"{r['price']:,}" if r.get('price') else "?"
        title_short = r['title'][:28]
        kb.append([
            InlineKeyboardButton(
                f"{abs_idx+1}. {title_short} — {price_str} ت",
                callback_data=f"det:{abs_idx}"
            )
        ])

    # ناوبری
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"pg:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"pg:{page+1}"))
    kb.append(nav)

    # مرتب‌سازی
    kb.append([
        InlineKeyboardButton("🎯 مرتبط", callback_data="srt:relevance"),
        InlineKeyboardButton("💰 ارزان", callback_data="srt:cheapest"),
        InlineKeyboardButton("💎 گران", callback_data="srt:expensive"),
    ])
    kb.append([
        InlineKeyboardButton("🔥 پرفروش", callback_data="srt:bestseller"),
        InlineKeyboardButton("⭐ امتیاز", callback_data="srt:rating"),
        InlineKeyboardButton("🆕 جدید", callback_data="srt:newest"),
    ])

    # فیلترها
    filters = active_filters or {}
    filter_label = "🔧 فیلترها"
    active_count = sum([
        1 if filters.get("min_price") or filters.get("max_price") else 0,
        1 if filters.get("only_discounted") else 0,
        1 if filters.get("only_available") else 0,
    ])
    if active_count > 0:
        filter_label = f"🔧 فیلترها ({active_count} فعال)"

    kb.append([
        InlineKeyboardButton(filter_label, callback_data="flt:menu"),
        InlineKeyboardButton("❌ لغو", callback_data="cncl"),
    ])
    return InlineKeyboardMarkup(kb)


def filters_menu_kb(active: dict = None) -> InlineKeyboardMarkup:
    active = active or {}
    price_label = "💰 محدوده قیمت"
    if active.get("min_price") or active.get("max_price"):
        mn = f"{active.get('min_price', 0):,}" if active.get("min_price") else "0"
        mx = f"{active.get('max_price', 0):,}" if active.get("max_price") else "∞"
        price_label = f"💰 قیمت: {mn} — {mx} ✅"

    disc_label = "🔥 فقط تخفیف‌دار"
    if active.get("only_discounted"):
        disc_label += " ✅"

    stock_label = "✅ فقط موجود"
    if active.get("only_available"):
        stock_label += " ✅"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(price_label, callback_data="flt:price")],
        [InlineKeyboardButton(disc_label, callback_data="flt:disc")],
        [InlineKeyboardButton(stock_label, callback_data="flt:stock")],
        [InlineKeyboardButton("🔄 حذف همه فیلترها", callback_data="flt:clear")],
        [InlineKeyboardButton("🔙 بازگشت به نتایج", callback_data="back_results")],
    ])

def product_detail_kb(url: str, abs_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن به پایش", callback_data=f"add:{abs_idx}")],
        [InlineKeyboardButton("🛒 خرید در دیجی‌کالا", url=url)],
        [InlineKeyboardButton("🔙 بازگشت به نتایج", callback_data="back_results")],
    ])


def list_item_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ℹ️ جزئیات", callback_data=f"info:{pid}"),
            InlineKeyboardButton("✏️ ویرایش هدف", callback_data=f"edt:{pid}"),
        ],
        [InlineKeyboardButton("🗑 حذف", callback_data=f"del:{pid}")],
    ])


# ============================================
# دستورات
# ============================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = (
        "👋 <b>سلام!</b>\n\n"
        "من ربات پایش قیمت دیجی‌کالا هستم.\n"
        "قیمت محصولات رو دنبال می‌کنم و بهت خبر می‌دم.\n\n"
        "👇 از منوی زیر شروع کن:"
    )
    await update.message.reply_text(text, parse_mode="HTML",
                                     reply_markup=main_menu_kb())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 <b>راهنما</b>\n\n"
        "<b>➕ افزودن محصول:</b>\n"
        "اسم محصول رو بنویس، از نتایج انتخاب کن، قیمت هدف بذار.\n\n"
        "<b>📋 لیست محصولات:</b>\n"
        "همه محصولاتت رو با قیمت فعلی و هدف نشون می‌ده.\n"
        "می‌تونی ویرایش کنی یا حذف کنی.\n\n"
        "<b>📊 خروجی CSV:</b>\n"
        "فایل اکسل تاریخچه قیمت‌ها رو برات می‌فرسته.\n\n"
        "<b>❌ لغو:</b>\n"
        "هر وقت خواستی عملیات رو لغو کنی، /cancel بزن."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu:main")
            ]])
        )
    else:
        await update.message.reply_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu:main")
            ]])
        )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "✅ عملیات لغو شد.",
        reply_markup=main_menu_kb()
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["state"] = "waiting_name"
    await update.message.reply_text(
        "🔍 <b>اسم محصول رو بفرست:</b>\n\n"
        "مثال: <code>ساک ورزشی</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = get_all_products()
    if not products:
        await update.message.reply_text(
            "📭 هیچ محصولی نداری.\nبا /add اضافه کن.",
            reply_markup=main_menu_kb()
        )
        return

    lines = [f"📋 <b>محصولات تو ({len(products)} تا):</b>\n"]
    for i, p in enumerate(products[:10], 1):
        price_str = f"{p['last_price']:,} ت" if p['last_price'] else "—"
        target_str = f"{p['target']:,} ت" if p['target'] else "—"
        lines.append(
            f"{i}. <b>{esc(p['title'][:60])}</b>\n"
            f"   💰 {price_str}   🎯 {target_str}\n"
        )

    text = "\n".join(lines)

    # دکمه‌های مدیریت برای هر محصول
    kb = []
    for p in products[:10]:
        kb.append([
            InlineKeyboardButton(
                f"{esc(p['title'][:30])}",
                callback_data=f"info:{p['id']}"
            )
        ])
    kb.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu:main")])

    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        export_to_csv()
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, "rb") as f:
                await update.message.reply_document(
                    document=f, filename="prices.csv",
                    caption="📊 خروجی CSV محصولات"
                )
        else:
            await update.message.reply_text("❌ فایل CSV ساخته نشد.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")

async def refresh_search(context):
    """جستجو رو با فیلترهای جدید دوباره انجام می‌ده"""
    s = context.user_data.get("search", {})
    q = s.get("query")
    sort = s.get("sort", "relevance")
    filters = s.get("filters", {})
    if not q:
        return
    results = search_products(
        q, limit=MAX_RESULTS, sort=sort,
        min_price=filters.get("min_price", 0),
        max_price=filters.get("max_price", 0),
        only_discounted=filters.get("only_discounted", False),
        only_available=filters.get("only_available", False),
    )
    context.user_data["search"]["results"] = results
    context.user_data["search"]["page"] = 0
# ============================================
# نمایش نتایج جستجو (با صفحه‌بندی)
# ============================================
async def render_results(target, context, edit=False):
    data = context.user_data.get("search", {})
    results = data.get("results", [])
    page = data.get("page", 0)
    query = data.get("query", "")
    sort = data.get("sort", "relevance")
    filters = data.get("filters", {})

    total_pages = max(1, (len(results) + PAGE_SIZE - 1) // PAGE_SIZE)

    lines = [
        f"🔍 <b>نتایج برای:</b> «{esc(query)}»",
        f"📊 <b>مرتب‌سازی:</b> {SORT_NAMES_FA.get(sort, sort)}",
    ]

    # نمایش فیلترهای فعال
    if filters.get("min_price") or filters.get("max_price"):
        mn = f"{filters.get('min_price', 0):,}" if filters.get("min_price") else "0"
        mx = f"{filters.get('max_price', 0):,}" if filters.get("max_price") else "∞"
        lines.append(f"💰 قیمت: {mn} — {mx}")
    if filters.get("only_discounted"):
        lines.append("🔥 فقط تخفیف‌دار")
    if filters.get("only_available"):
        lines.append("✅ فقط موجود")

    lines.append(f"📄 صفحه {page+1} از {total_pages} — {len(results)} نتیجه\n")

    start = page * PAGE_SIZE
    for i, r in enumerate(results[start:start+PAGE_SIZE], start+1):
        price_str = f"{r['price']:,}" if r.get('price') else "?"
        disc_str = f" 🔥{r['discount']}%" if r.get('discount') else ""
        lines.append(f"{i}. {esc(r['title'][:55])}")
        lines.append(f"   💰 {price_str} تومان{disc_str}")

    text = "\n".join(lines)
    kb = results_kb(results, page, total_pages, len(results), filters)

    if edit:
        try:
            await target.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            try:
                await target.edit_message_reply_markup(reply_markup=kb)
            except Exception:
                pass
    else:
        await target.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    data = context.user_data.get("search", {})
    results = data.get("results", [])
    page = data.get("page", 0)
    query = data.get("query", "")
    sort = data.get("sort", "relevance")

    total_pages = (len(results) + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [
        f"🔍 <b>نتایج برای:</b> «{esc(query)}»",
        f"📊 <b>مرتب‌سازی:</b> {SORT_NAMES_FA.get(sort, sort)}",
        f"📄 صفحه {page+1} از {total_pages} — {len(results)} نتیجه\n",
    ]

    start = page * PAGE_SIZE
    for i, r in enumerate(results[start:start+PAGE_SIZE], start+1):
        price_str = f"{r['price']:,}" if r.get('price') else "?"
        lines.append(f"{i}. {esc(r['title'][:55])}")
        lines.append(f"   💰 {price_str} تومان")

    text = "\n".join(lines)
    kb = results_kb(results, page, total_pages, len(results))

    if edit:
        try:
            await target.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.edit_message_reply_markup(reply_markup=kb)
    else:
        await target.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ============================================
# نمایش جزئیات محصول (با عکس)
# ============================================
async def render_detail(query, context, abs_idx: int, from_list=False):
    data = context.user_data.get("search", {})
    results = data.get("results", [])

    if from_list:
        # از دیتابیس
        product = get_product(abs_idx)
        if not product:
            await query.edit_message_text("❌ محصول پیدا نشد.")
            return
        title = product["title"]
        url = product["url"]
        price = product["last_price"]
        rating = None
        rating_count = None
        image = None
    else:
        if abs_idx >= len(results):
            await query.edit_message_text("❌ خطا.")
            return
        r = results[abs_idx]
        title = r["title"]
        url = r["url"]
        price = r.get("price")
        rating = r.get("rating")
        rating_count = r.get("rating_count")
        image = r.get("image")

    # متن جزئیات
    lines = [f"📦 <b>{esc(title)}</b>\n"]
    if price:
        lines.append(f"💰 قیمت فعلی: <b>{price:,} تومان</b>")
    else:
        lines.append("💰 قیمت: نامشخص")
    if rating and rating_count and rating_count >= 10:
        stars = rating / 20
        lines.append(f"⭐ امتیاز: {stars:.1f}/5 ({rating_count} نظر)")
    lines.append(f"\n🔗 <a href='{url}'>لینک دیجی‌کالا</a>")

    text = "\n".join(lines)

    # کیبورد
    if from_list:
        pid = abs_idx
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ ویرایش هدف", callback_data=f"edt:{pid}")],
            [InlineKeyboardButton("🗑 حذف", callback_data=f"del:{pid}")],
            [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="menu:list")],
        ])
    else:
        kb = product_detail_kb(url, abs_idx)

    # ارسال با عکس یا متن
    if image:
        try:
            await query.message.reply_photo(
                photo=image, caption=text,
                parse_mode="HTML", reply_markup=kb
            )
            return
        except Exception as e:
            logger.warning(f"عکس ارسال نشد: {e}")

    # fallback: فقط متن
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ============================================
# پردازش پیام متنی
# ============================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    text = update.message.text.strip() if update.message.text else ""

    # مرحله ۱: منتظر اسم
    if state == "waiting_name":
        if len(text) < 2:
            await update.message.reply_text("❌ اسم کوتاهه. دوباره بفرست.")
            return

        await update.message.reply_text("⏳ در حال جستجو...")

        results = search_products(text, limit=MAX_RESULTS, sort="relevance")
        if not results:
            await update.message.reply_text(
                "❌ چیزی پیدا نشد. دوباره /add بزن."
            )
            context.user_data.clear()
            return

        context.user_data["search"] = {
            "query": text,
            "sort": "relevance",
            "results": results,
            "page": 0,
            "filters": {},
        }
        context.user_data["state"] = "browsing"
        await render_results(update, context, edit=False)
        return

    # مرحله ۲.۵: منتظر محدوده قیمت
    if state == "waiting_price_range":
        if text == "/skip":
            if "search" in context.user_data:
                context.user_data["search"]["filters"] = context.user_data["search"].get("filters", {})
                # حذف قیمت
                f = context.user_data["search"]["filters"]
                f.pop("min_price", None)
                f.pop("max_price", None)
                context.user_data["search"]["filters"] = f
            await refresh_search(context)
            await render_results(update, context, edit=False)
            context.user_data["state"] = "browsing"
            return

        try:
            if "-" not in text:
                raise ValueError("باید خط تیره داشته باشه")

            parts = text.replace("،", ",").replace(",", "").split("-")
            if len(parts) != 2:
                raise ValueError("فرمت اشتباه")

            min_p = int(parts[0].strip()) if parts[0].strip() else 0
            max_p = int(parts[1].strip()) if parts[1].strip() else 0

            if min_p < 0 or max_p < 0:
                raise ValueError("منفی نمی‌شه")
            if min_p > 0 and max_p > 0 and min_p >= max_p:
                raise ValueError("حداقل باید کمتر از حداکثر باشه")

            if "search" in context.user_data:
                f = context.user_data["search"].get("filters", {})
                f["min_price"] = min_p
                f["max_price"] = max_p
                context.user_data["search"]["filters"] = f

            await refresh_search(context)
            await render_results(update, context, edit=False)
            context.user_data["state"] = "browsing"

        except ValueError as e:
            await update.message.reply_text(
                "❌ فرمت اشتباه.\n"
                "مثال: <code>500000-2000000</code>",
                parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {e}")
        return

    # مرحله ۳: منتظر قیمت هدف
    if state == "waiting_target":
        if text == "/skip" or text == "0":
            target = 0
        else:
            try:
                target = int(text.replace(",", "").replace("،", "").replace(" ", ""))
                if target < 1000:
                    await update.message.reply_text(
                        "⚠️ عدد خیلی کمه. یه عدد بزرگ‌تر بفرست یا /skip."
                    )
                    return
            except ValueError:
                await update.message.reply_text("❌ فقط عدد بفرست یا /skip بزن.")
                return

        product = context.user_data.get("pending_product")
        if not product:
            await update.message.reply_text("❌ خطا. /add بزن.")
            context.user_data.clear()
            return

        pid = insert_product(product["url"], product["title"], target)
        if pid:
            target_str = f"{target:,} تومان" if target else "بدون هدف"
            price_str = f"{product.get('price', 0):,}" if product.get('price') else "?"
            await update.message.reply_text(
                f"✅ <b>اضافه شد!</b>\n\n"
                f"📦 {esc(product['title'])}\n"
                f"💰 قیمت فعلی: {price_str} تومان\n"
                f"🎯 هدف: {target_str}\n\n"
                f"از الان تغییرات قیمت رو خبر می‌دم. 🔔",
                parse_mode="HTML",
                reply_markup=main_menu_kb()
            )
        else:
            await update.message.reply_text(
                "❌ خطا در ذخیره. شاید قبلاً اضافه شده.",
                reply_markup=main_menu_kb()
            )
        context.user_data.clear()
        return

    # مرحله ۵: ویرایش هدف
    if state == "editing_target":
        pid = context.user_data.get("edit_pid")
        if not pid:
            await update.message.reply_text("❌ خطا. /list بزن.")
            context.user_data.clear()
            return

        if text == "/skip" or text == "0":
            target = 0
        else:
            try:
                target = int(text.replace(",", "").replace("،", "").replace(" ", ""))
                if target < 1000:
                    await update.message.reply_text("⚠️ عدد کمه. دوباره بفرست.")
                    return
            except ValueError:
                await update.message.reply_text("❌ فقط عدد بفرست یا /skip.")
                return

        if update_target_price(pid, target):
            target_str = f"{target:,} تومان" if target else "بدون هدف"
            await update.message.reply_text(
                f"✅ هدف ویرایش شد: {target_str}",
                reply_markup=main_menu_kb()
            )
        else:
            await update.message.reply_text("❌ خطا در ویرایش.")
        context.user_data.clear()
        return

    await update.message.reply_text(
        "🤔 متوجه نشدم. /start بزن.",
        reply_markup=main_menu_kb()
    )


# ============================================
# پردازش دکمه‌ها
# ============================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # نادیده گرفتن noop
    if data == "noop":
        return

    # منو
    if data == "menu:main":
        context.user_data.clear()
        await query.edit_message_text(
            "👋 <b>منوی اصلی</b>\n\nیه گزینه انتخاب کن:",
            parse_mode="HTML", reply_markup=main_menu_kb()
        )
        return

    if data == "menu:add":
        context.user_data.clear()
        context.user_data["state"] = "waiting_name"
        await query.edit_message_text(
            "🔍 <b>اسم محصول رو بفرست:</b>\n\nمثال: <code>ساک ورزشی</code>",
            parse_mode="HTML", reply_markup=cancel_kb()
        )
        return

    if data == "menu:list":
        context.user_data.clear()
        products = get_all_products()
        if not products:
            await query.edit_message_text(
                "📭 هیچ محصولی نداری.\nبا /add اضافه کن.",
                reply_markup=main_menu_kb()
            )
            return

        lines = [f"📋 <b>محصولات تو ({len(products)} تا):</b>\n"]
        for i, p in enumerate(products[:10], 1):
            price_str = f"{p['last_price']:,} ت" if p['last_price'] else "—"
            target_str = f"{p['target']:,} ت" if p['target'] else "—"
            lines.append(
                f"{i}. <b>{esc(p['title'][:55])}</b>\n"
                f"   💰 {price_str}   🎯 {target_str}\n"
            )

        kb = []
        for p in products[:10]:
            kb.append([InlineKeyboardButton(
                f"ℹ️ {esc(p['title'][:35])}",
                callback_data=f"info:{p['id']}"
            )])
        kb.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu:main")])

        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data == "menu:export":
        try:
            export_to_csv()
            if os.path.exists(CSV_PATH):
                with open(CSV_PATH, "rb") as f:
                    await query.message.reply_document(
                        document=f, filename="prices.csv",
                        caption="📊 خروجی CSV"
                    )
                await query.edit_message_text(
                    "✅ فایل ارسال شد.",
                    reply_markup=main_menu_kb()
                )
        except Exception as e:
            await query.edit_message_text(f"❌ خطا: {e}")
        return

    if data == "menu:help":
        await cmd_help(update, context)
        return

    # لغو
    if data == "cncl":
        context.user_data.clear()
        await query.edit_message_text(
            "✅ لغو شد.",
            reply_markup=main_menu_kb()
        )
        return
        # منوی فیلترها
    if data == "flt:menu":
        filters = context.user_data.get("search", {}).get("filters", {})
        await query.edit_message_reply_markup(
            reply_markup=filters_menu_kb(filters)
        )
        return

    if data == "flt:clear":
        if "search" in context.user_data:
            context.user_data["search"]["filters"] = {}
        await query.answer("✅ فیلترها پاک شد")
        # دوباره جستجو کن
        s = context.user_data.get("search", {})
        q = s.get("query")
        sort = s.get("sort", "relevance")
        if q:
            results = search_products(q, limit=MAX_RESULTS, sort=sort)
            context.user_data["search"]["results"] = results
            context.user_data["search"]["page"] = 0
        await render_results(query, context, edit=True)
        return

    if data == "flt:disc":
        s = context.user_data.get("search", {})
        filters = s.get("filters", {})
        filters["only_discounted"] = not filters.get("only_discounted", False)
        s["filters"] = filters
        context.user_data["search"] = s
        await refresh_search(context)
        await render_results(query, context, edit=True)
        return

    if data == "flt:stock":
        s = context.user_data.get("search", {})
        filters = s.get("filters", {})
        filters["only_available"] = not filters.get("only_available", False)
        s["filters"] = filters
        context.user_data["search"] = s
        await refresh_search(context)
        await render_results(query, context, edit=True)
        return

    if data == "flt:price":
        context.user_data["state"] = "waiting_price_range"
        await query.edit_message_text(
            "💰 <b>محدوده قیمت رو بفرست</b>\n\n"
            "فرمت: <code>min-max</code> به تومان\n\n"
            "مثال‌ها:\n"
            "• <code>500000-2000000</code> — از ۵۰۰ هزار تا ۲ میلیون\n"
            "• <code>0-1000000</code> — تا ۱ میلیون\n"
            "• <code>500000-0</code> — از ۵۰۰ هزار به بالا\n\n"
            "یا /skip بزن تا فیلتر قیمت حذف بشه.",
            parse_mode="HTML", reply_markup=cancel_kb()
        )
        return

    if data == "back_results":
        if "search" in context.user_data:
            await render_results(query, context, edit=True)
        else:
            await query.edit_message_text(
                "❌ نتایج قدیمی. دوباره /add بزن.",
                reply_markup=main_menu_kb()
            )
        return

    # ناوبری صفحات
    if data.startswith("pg:"):
        page = int(data.split(":")[1])
        if "search" in context.user_data:
            context.user_data["search"]["page"] = page
        await render_results(query, context, edit=True)
        return

    # بازگشت به نتایج
    if data == "back_results":
        if "search" in context.user_data:
            await render_results(query, context, edit=True)
        else:
            await query.edit_message_text(
                "❌ نتایج قدیمی. دوباره /add بزن.",
                reply_markup=main_menu_kb()
            )
        return

    # مرتب‌سازی
    if data.startswith("srt:"):
        sort = data.split(":")[1]
        s = context.user_data.get("search", {})
        q = s.get("query")
        if not q:
            await query.edit_message_text("❌ از /add شروع کن.")
            return

        results = search_products(q, limit=MAX_RESULTS, sort=sort)
        if not results:
            await query.edit_message_text("❌ نتیجه‌ای نیست.")
            return

        context.user_data["search"] = {
            "query": q, "sort": sort,
            "results": results, "page": 0,
        }
        await render_results(query, context, edit=True)
        return

    # نمایش جزئیات (از نتایج جستجو)
    if data.startswith("det:"):
        idx = int(data.split(":")[1])
        await render_detail(query, context, idx, from_list=False)
        return

    # نمایش جزئیات (از لیست ذخیره‌شده)
    if data.startswith("info:"):
        pid = int(data.split(":")[1])
        product = get_product(pid)
        if not product:
            await query.edit_message_text("❌ پیدا نشد.")
            return

        lines = [f"📦 <b>{esc(product['title'])}</b>\n"]
        if product['last_price']:
            lines.append(f"💰 قیمت فعلی: <b>{product['last_price']:,} تومان</b>")
        if product['target']:
            lines.append(f"🎯 هدف: {product['target']:,} تومان")
        lines.append(f"\n🔗 <a href='{product['url']}'>لینک دیجی‌کالا</a>")

        if product['history']:
            lines.append("\n📈 <b>آخرین تغییرات:</b>")
            for price, ts in product['history'][:5]:
                try:
                    dt = datetime.fromisoformat(ts).strftime("%m/%d %H:%M")
                except Exception:
                    dt = ts[:16]
                lines.append(f"  • {price:,} ت ({dt})")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ ویرایش هدف", callback_data=f"edt:{pid}")],
            [InlineKeyboardButton("🗑 حذف", callback_data=f"del:{pid}")],
            [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="menu:list")],
        ])
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=kb, disable_web_page_preview=True
        )
        return

    # افزودن محصول از صفحه جزئیات
    if data.startswith("add:"):
        idx = int(data.split(":")[1])
        results = context.user_data.get("search", {}).get("results", [])
        if idx >= len(results):
            await query.edit_message_text("❌ خطا. دوباره /add بزن.")
            return

        product = results[idx]
        context.user_data["pending_product"] = product
        context.user_data["state"] = "waiting_target"

        price_str = f"{product['price']:,}" if product.get('price') else "?"
        await query.edit_message_text(
            f"✅ انتخاب شد:\n\n"
            f"📦 <b>{esc(product['title'])}</b>\n"
            f"💰 قیمت فعلی: {price_str} تومان\n\n"
            f"🎯 <b>قیمت هدف</b> رو بفرست (تومان).\n"
            f"یا /skip بزن.",
            parse_mode="HTML", reply_markup=cancel_kb()
        )
        return

    # ویرایش هدف
    if data.startswith("edt:"):
        pid = int(data.split(":")[1])
        context.user_data["edit_pid"] = pid
        context.user_data["state"] = "editing_target"
        await query.edit_message_text(
            "✏️ <b>قیمت هدف جدید رو بفرست</b>\n\n"
            "برای حذف هدف، /skip یا 0 بزن.",
            parse_mode="HTML", reply_markup=cancel_kb()
        )
        return

    # حذف
    if data.startswith("del:"):
        pid = int(data.split(":")[1])
        # تأیید
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ بله", callback_data=f"delc:{pid}"),
                InlineKeyboardButton("❌ نه", callback_data="menu:list"),
            ]
        ])
        await query.edit_message_text(
            "⚠️ مطمئنی می‌خوای حذف کنی؟",
            reply_markup=kb
        )
        return

    if data.startswith("delc:"):
        pid = int(data.split(":")[1])
        if delete_product(pid):
            await query.edit_message_text(
                "✅ حذف شد.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 لیست", callback_data="menu:list")
                ]])
            )
        else:
            await query.edit_message_text("❌ خطا.")


# ============================================
# اسکرپر در پس‌زمینه
# ============================================
def run_scraper_in_background():
    try:
        time.sleep(10)
        logger.info("🚀 اسکرپر شروع شد...")
        send_startup_message()
        monitor("urls.txt", once=False)
    except Exception as e:
        logger.error(f"Scraper error: {e}")


# ============================================
# Main
# ============================================
def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN نیست!")
        return

    init_db()

    t = threading.Thread(target=run_scraper_in_background, daemon=True)
    t.start()
    logger.info("✅ اسکرپر در پس‌زمینه شروع شد.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("export", cmd_export))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.COMMAND, handle_text))

    logger.info("🤖 ربات روشن شد...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
