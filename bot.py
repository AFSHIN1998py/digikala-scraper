"""
==============================================
ربات تلگرام اسکرپر دیجی‌کالا - نسخه نهایی
==============================================
- ربات تعاملی با دکمه
- اسکرپر در thread جدا (راه D)
- دیتابیس مشترک
"""

import os
import sqlite3
import logging
import threading
import time
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from scraper import (
    search_products,
    export_to_csv,
    init_db,
    monitor,
    send_startup_message,
    DB_PATH,
    CSV_PATH,
    BOT_TOKEN,
    SORT_NAMES_FA,
    SORT_CODES,
)

# ---------- لاگ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("bot")


# ============================================
# دیتابیس
# ============================================
def get_all_products() -> list[dict]:
    """لیست همه محصولات ذخیره‌شده"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute("""
        SELECT product_id, url, title, target_price
        FROM products
        ORDER BY product_id DESC
    """).fetchall()

    result = []
    for r in rows:
        pid, url, title, target = r
        last = c.execute("""
            SELECT price FROM price_history
            WHERE product_id = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (pid,)).fetchone()
        last_price = last[0] if last else None

        result.append({
            "id": pid,
            "url": url,
            "title": title,
            "target": target,
            "last_price": last_price,
        })
    conn.close()
    return result


def delete_product(product_id: int) -> bool:
    """حذف محصول از دیتابیس"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("DELETE FROM price_history WHERE product_id = ?", (product_id,))
        c.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"DB Error: {e}")
        return False
    finally:
        conn.close()


def insert_product(url: str, title: str, target: int = 0) -> int | None:
    """اضافه کردن محصول به دیتابیس"""
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
        logger.error(f"DB Error: {e}")
        return None
    finally:
        conn.close()


# ============================================
# دستورات ربات
# ============================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>سلام!</b>\n\n"
        "من ربات پایش قیمت دیجی‌کالا هستم.\n"
        "قیمت محصولات رو دنبال می‌کنم و بهت خبر می‌دم.\n\n"
        "📋 <b>دستورات:</b>\n"
        "🔹 /add — افزودن محصول\n"
        "🔹 /list — لیست محصولات\n"
        "🔹 /remove — حذف محصول\n"
        "🔹 /export — دریافت CSV\n"
        "🔹 /help — راهنما\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 <b>راهنما</b>\n\n"
        "<b>افزودن محصول:</b>\n"
        "/add → اسم محصول رو بنویس → از نتایج انتخاب کن → قیمت هدف رو بده (یا /skip)\n\n"
        "<b>لیست:</b> /list\n"
        "<b>حذف:</b> /remove\n"
        "<b>CSV:</b> /export\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "waiting_for_product_name"
    await update.message.reply_text(
        "🔍 <b>اسم محصول رو بفرست:</b>\n\n"
        "مثلاً: <code>ساک ورزشی</code>",
        parse_mode="HTML"
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = get_all_products()
    if not products:
        await update.message.reply_text("📭 هیچ محصولی نداری. با /add اضافه کن.")
        return

    lines = [f"📋 <b>محصولات تو ({len(products)} تا):</b>\n"]
    for p in products[:20]:
        price_str = f"{p['last_price']:,} ت" if p['last_price'] else "—"
        target_str = f"{p['target']:,} ت" if p['target'] else "—"
        lines.append(
            f"🔸 <b>{p['title'][:60]}</b>\n"
            f"   💰 فعلی: {price_str}   🎯 هدف: {target_str}\n"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (کوتاه شده)"

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = get_all_products()
    if not products:
        await update.message.reply_text("📭 چیزی برای حذف نیست.")
        return

    keyboard = []
    for p in products[:10]:
        keyboard.append([
            InlineKeyboardButton(
                f"🗑 {p['title'][:40]}",
                callback_data=f"del:{p['id']}"
            )
        ])
    await update.message.reply_text(
        "کدوم رو حذف کنم؟",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        export_to_csv()
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="prices.csv",
                    caption="📊 خروجی CSV محصولات"
                )
        else:
            await update.message.reply_text("❌ فایل CSV ساخته نشد.")
    except Exception as e:
        logger.error(f"Export error: {e}")
        await update.message.reply_text(f"❌ خطا: {e}")


# ============================================
# مدیریت پیام متنی
# ============================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    text = update.message.text.strip() if update.message.text else ""

    # مرحله ۱: منتظر اسم محصول
    if state == "waiting_for_product_name":
        if len(text) < 2:
            await update.message.reply_text("❌ اسم خیلی کوتاهه. دوباره بفرست.")
            return

        context.user_data["query"] = text
        await update.message.reply_text("⏳ در حال جستجو...")

        results = search_products(text, limit=10, sort="relevance")
        if not results:
            await update.message.reply_text("❌ چیزی پیدا نشد. دوباره /add بزن.")
            context.user_data["state"] = None
            return

        context.user_data["results"] = results
        context.user_data["state"] = "waiting_for_product_pick"
        await show_search_results(update, context, results)
        return

    # مرحله ۳: منتظر قیمت هدف
    if state == "waiting_for_target_price":
        if text == "/skip" or text == "0":
            target = 0
        else:
            try:
                target = int(text.replace(",", "").replace("،", ""))
                if target < 1000:
                    await update.message.reply_text(
                        "⚠️ قیمت خیلی کمه. یه عدد بزرگ‌تر بفرست یا /skip بزن."
                    )
                    return
            except ValueError:
                await update.message.reply_text("❌ فقط عدد بفرست یا /skip بزن.")
                return

        product = context.user_data.get("pending_product")
        if not product:
            await update.message.reply_text("❌ خطا. از اول /add بزن.")
            context.user_data["state"] = None
            return

        pid = insert_product(product["url"], product["title"], target)
        if pid:
            target_str = f"{target:,} تومان" if target else "بدون هدف"
            price_str = f"{product.get('price', 0):,}" if product.get('price') else "?"
            await update.message.reply_text(
                f"✅ <b>اضافه شد!</b>\n\n"
                f"📦 {product['title']}\n"
                f"💰 قیمت فعلی: {price_str} تومان\n"
                f"🎯 هدف: {target_str}\n\n"
                f"از الان تغییرات قیمت رو بهت خبر می‌دم. 🔔",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("❌ خطا در ذخیره.")

        context.user_data["state"] = None
        context.user_data["pending_product"] = None
        return

    # حالت پیش‌فرض
    await update.message.reply_text(
        "🤔 متوجه نشدم. از /help استفاده کن یا /start رو بزن."
    )


# ============================================
# نمایش نتایج جستجو
# ============================================
async def show_search_results(update_or_query, context: ContextTypes.DEFAULT_TYPE,
                              results: list, sort: str = "relevance",
                              edit: bool = False):
    lines = [f"🔍 <b>نتایج برای:</b> «{context.user_data.get('query', '')}»"]
    lines.append(f"📊 <b>مرتب‌سازی:</b> {SORT_NAMES_FA.get(sort, sort)}\n")

    keyboard = []
    for i, r in enumerate(results[:10], 1):
        price_str = f"{r['price']:,}" if r.get('price') else "?"
        lines.append(f"{i}. {r['title'][:55]}")
        lines.append(f"   💰 {price_str} تومان")

        keyboard.append([
            InlineKeyboardButton(
                f"{i}. {r['title'][:42]} — {price_str} ت",
                callback_data=f"pick:{i-1}"
            )
        ])

    # دکمه‌های مرتب‌سازی
    keyboard.append([
        InlineKeyboardButton("مرتبط", callback_data="sort:relevance"),
        InlineKeyboardButton("ارزان", callback_data="sort:cheapest"),
        InlineKeyboardButton("گران", callback_data="sort:expensive"),
    ])
    keyboard.append([
        InlineKeyboardButton("پرفروش", callback_data="sort:bestseller"),
        InlineKeyboardButton("امتیاز", callback_data="sort:rating"),
        InlineKeyboardButton("جدید", callback_data="sort:newest"),
    ])

    text = "\n".join(lines)
    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit:
        await update_or_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=reply_markup
        )
    else:
        await update_or_query.message.reply_text(
            text, parse_mode="HTML", reply_markup=reply_markup
        )


# ============================================
# مدیریت دکمه‌ها
# ============================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # انتخاب محصول
    if data.startswith("pick:"):
        idx = int(data.split(":")[1])
        results = context.user_data.get("results", [])
        if idx >= len(results):
            await query.edit_message_text("❌ خطا. از اول /add بزن.")
            return

        product = results[idx]
        context.user_data["pending_product"] = product
        context.user_data["state"] = "waiting_for_target_price"

        price_str = f"{product['price']:,}" if product.get('price') else "?"
        await query.edit_message_text(
            f"✅ انتخاب شد:\n\n"
            f"📦 <b>{product['title']}</b>\n"
            f"💰 قیمت فعلی: {price_str} تومان\n\n"
            f"🎯 حالا <b>قیمت هدف</b> رو بفرست (به تومان).\n"
            f"یا اگه نمی‌خوای هدف بذاری، /skip بزن.",
            parse_mode="HTML"
        )
        return

    # مرتب‌سازی
    if data.startswith("sort:"):
        sort = data.split(":")[1]
        query_text = context.user_data.get("query", "")
        if not query_text:
            await query.edit_message_text("❌ از اول /add بزن.")
            return

        results = search_products(query_text, limit=10, sort=sort)
        if not results:
            await query.edit_message_text("❌ چیزی پیدا نشد.")
            return

        context.user_data["results"] = results
        await show_search_results(query, context, results, sort=sort, edit=True)
        return

    # حذف محصول
    if data.startswith("del:"):
        pid = int(data.split(":")[1])
        if delete_product(pid):
            await query.edit_message_text("✅ حذف شد.")
        else:
            await query.edit_message_text("❌ خطا در حذف.")


# ============================================
# اسکرپر در پس‌زمینه
# ============================================
def run_scraper_in_background():
    """اسکرپر رو توی thread جدا اجرا می‌کنه"""
    try:
        time.sleep(10)  # صبر کن ربات بالا بیاد
        logger.info("🚀 شروع اسکرپر در پس‌زمینه...")
        send_startup_message()
        monitor("urls.txt", once=False)
    except Exception as e:
        logger.error(f"❌ خطای اسکرپر: {e}")


# ============================================
# Main
# ============================================
def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN تنظیم نشده!")
        return

    init_db()

    # اسکرپر در thread جدا
    scraper_thread = threading.Thread(target=run_scraper_in_background, daemon=True)
    scraper_thread.start()
    logger.info("✅ اسکرپر در پس‌زمینه شروع شد.")

    # ربات
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("export", cmd_export))

    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.COMMAND, handle_text))

    logger.info("🤖 ربات روشن شد...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
