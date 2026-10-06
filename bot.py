"""
ربات تلگرام اسکرپر دیجی‌کالا - v3.2
==============================================
- افزودن با اسم OR لینک
- ذخیره قیمت اولیه هنگام افزودن
- فیلترهای چندگانه
- صفحه‌بندی 60 محصول
"""
import os
import html
import psycopg2
import logging
import threading
import time
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

from scraper import (
    search_products, search_torob, export_to_csv, init_db, monitor,
    send_startup_message, send_daily_report, CSV_PATH, BOT_TOKEN,
    SORT_NAMES_FA, extract_product_id, fetch_product_api,
    parse_api_response,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("bot")

PAGE_SIZE = 10
MAX_RESULTS = 60


def esc(text) -> str:
    return html.escape(str(text)) if text else ""


# ============================================
# دیتابیس (PostgreSQL / Neon)
# ============================================
def get_all_products() -> list[dict]:
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    c = conn.cursor()
    c.execute("""
        SELECT product_id, url, title, target_price
        FROM products ORDER BY product_id DESC
    """)
    rows = c.fetchall()
    result = []
    for pid, url, title, target in rows:
        c.execute("""
            SELECT price FROM price_history
            WHERE product_id = %s ORDER BY timestamp DESC LIMIT 1
        """, (pid,))
        last = c.fetchone()
        result.append({
            "id": pid, "url": url, "title": title,
            "target": target,
            "last_price": last[0] if last else None,
        })
    conn.close()
    return result


def get_product(product_id: int) -> dict | None:
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    c = conn.cursor()
    c.execute("""
        SELECT product_id, url, title, target_price
        FROM products WHERE product_id = %s
    """, (product_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    pid, url, title, target = row
    c.execute("""
        SELECT price FROM price_history
        WHERE product_id = %s ORDER BY timestamp DESC LIMIT 1
    """, (pid,))
    last = c.fetchone()
    c.execute("""
        SELECT price, timestamp FROM price_history
        WHERE product_id = %s ORDER BY timestamp DESC LIMIT 5
    """, (pid,))
    history = c.fetchall()
    conn.close()
    return {
        "id": pid, "url": url, "title": title, "target": target,
        "last_price": last[0] if last else None,
        "history": history,
    }


def delete_product(product_id: int) -> bool:
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    c = conn.cursor()
    try:
        c.execute("DELETE FROM price_history WHERE product_id = %s", (product_id,))
        c.execute("DELETE FROM products WHERE product_id = %s", (product_id,))
        conn.commit()
        return True
    except psycopg2.Error:
        return False
    finally:
        conn.close()


def update_target_price(product_id: int, target: int) -> bool:
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    c = conn.cursor()
    try:
        c.execute("UPDATE products SET target_price = %s WHERE product_id = %s",
                  (target, product_id))
        conn.commit()
        return True
    except psycopg2.Error:
        return False
    finally:
        conn.close()


def insert_product(url: str, title: str, target: int = 0, price: int = None) -> int | None:
    """اضافه کردن محصول + ذخیره قیمت اولیه اگه داشتیم"""
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO products (url, title, target_price, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (url) DO NOTHING
            RETURNING product_id
        """, (url, title, target, datetime.now()))
        result = c.fetchone()
        conn.commit()

        if result:
            pid = result[0]
        else:
            c.execute("SELECT product_id FROM products WHERE url = %s", (url,))
            row = c.fetchone()
            pid = row[0] if row else None

        # ذخیره قیمت اولیه (فقط برای محصول جدید)
        if pid and price:
            c.execute("""
                SELECT COUNT(*) FROM price_history WHERE product_id = %s
            """, (pid,))
            existing = c.fetchone()[0]
            if existing == 0:
                c.execute("""
                    INSERT INTO price_history (product_id, price, timestamp)
                    VALUES (%s, %s, %s)
                """, (pid, price, datetime.now()))
                conn.commit()
                logger.info(f"💰 قیمت اولیه ذخیره شد: {price:,} برای {title[:30]}")

        return pid
    except psycopg2.Error as e:
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


def results_kb(results, page, total_pages, total_count, active_filters=None):
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

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"pg:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"pg:{page+1}"))
    kb.append(nav)

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


def filters_menu_kb(active=None):
    active = active or {}
    price_label = "💰 محدوده قیمت"
    if active.get("min_price") or active.get("max_price"):
        mn = f"{active.get('min_price', 0):,}" if active.get("min_price") else "0"
        mx = f"{active.get('max_price', 0):,}" if active.get("max_price") else "∞"
        price_label = f"💰 قیمت: {mn} — {mx} ✅"

    disc_label = "🔥 فقط تخفیف‌دار" + (" ✅" if active.get("only_discounted") else "")
    stock_label = "✅ فقط موجود" + (" ✅" if active.get("only_available") else "")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(price_label, callback_data="flt:price")],
        [InlineKeyboardButton(disc_label, callback_data="flt:disc")],
        [InlineKeyboardButton(stock_label, callback_data="flt:stock")],
        [InlineKeyboardButton("🔄 حذف همه", callback_data="flt:clear")],
        [InlineKeyboardButton("🔙 نمایش نتایج", callback_data="flt:apply")],
    ])


def product_detail_kb(url: str, abs_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن به پایش", callback_data=f"add:{abs_idx}")],
        [InlineKeyboardButton("🏆 مقایسه در ترب", callback_data=f"torob:{abs_idx}")],
        [InlineKeyboardButton("🛒 خرید در دیجی‌کالا", url=url)],
        [InlineKeyboardButton("🔙 بازگشت به نتایج", callback_data="back_results")],
    ])


# ============================================
# دستورات
# ============================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 <b>سلام!</b>\n\n"
        "من ربات پایش قیمت دیجی‌کالا هستم.\n\n"
        "👇 از منوی زیر شروع کن:",
        parse_mode="HTML", reply_markup=main_menu_kb()
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 <b>راهنما</b>\n\n"
        "<b>➕ افزودن:</b>\n"
        "اسم محصول یا لینکش رو بفرست:\n"
        "• اسم: <code>ساک ورزشی</code>\n"
        "• لینک: <code>https://www.digikala.com/product/dkp-...</code>\n\n"
        "<b>📋 لیست:</b> مدیریت محصولات\n"
        "<b>📊 CSV:</b> دریافت فایل اکسل\n"
        "<b>🔧 فیلترها:</b> می‌تونی چند فیلتر رو با هم انتخاب کنی، بعد «نمایش نتایج» رو بزنی.\n"
        "<b>❌ لغو:</b> /cancel"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu:main")
    ]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ لغو شد.", reply_markup=main_menu_kb())

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ارسال گزارش فوری (برای تست)"""
    await update.message.reply_text("⏳ در حال ساخت گزارش...")
    send_daily_report()

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["state"] = "waiting_name"
    await update.message.reply_text(
        "🔍 <b>اسم محصول یا لینکش رو بفرست:</b>\n\n"
        "مثال‌ها:\n"
        "• <code>ساک ورزشی</code> (جستجو با اسم)\n"
        "• <code>https://www.digikala.com/product/dkp-22214723/</code>\n"
        "  (افزودن مستقیم با لینک)",
        parse_mode="HTML", reply_markup=cancel_kb()
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = get_all_products()
    if not products:
        await update.message.reply_text("📭 محصولی نداری. /add بزن.",
                                        reply_markup=main_menu_kb())
        return
    await _render_list(update.message, products)


async def _render_list(target, products):
    lines = [f"📋 <b>محصولات ({len(products)}):</b>\n"]
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
            f"ℹ️ {esc(p['title'][:35])}", callback_data=f"info:{p['id']}"
        )])
    kb.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu:main")])
    await target.reply_text("\n".join(lines), parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(kb))


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        export_to_csv()
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, "rb") as f:
                await update.message.reply_document(
                    document=f, filename="prices.csv", caption="📊 CSV"
                )
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")


# ============================================
# جستجو و نمایش
# ============================================
def _do_search(context):
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


async def render_results(target, context, edit=False):
    data = context.user_data.get("search", {})
    results = data.get("results", [])
    page = data.get("page", 0)
    query = data.get("query", "")
    sort = data.get("sort", "relevance")
    filters = data.get("filters", {})

    total_pages = max(1, (len(results) + PAGE_SIZE - 1) // PAGE_SIZE)

    lines = [
        f"🔍 <b>نتایج:</b> «{esc(query)}»",
        f"📊 {SORT_NAMES_FA.get(sort, sort)}",
    ]
    if filters.get("min_price") or filters.get("max_price"):
        mn = f"{filters.get('min_price', 0):,}" if filters.get("min_price") else "0"
        mx = f"{filters.get('max_price', 0):,}" if filters.get("max_price") else "∞"
        lines.append(f"💰 {mn} — {mx}")
    if filters.get("only_discounted"):
        lines.append("🔥 فقط تخفیف‌دار")
    if filters.get("only_available"):
        lines.append("✅ فقط موجود")

    lines.append(f"📄 {page+1}/{total_pages} — {len(results)} نتیجه\n")

    start = page * PAGE_SIZE
    for i, r in enumerate(results[start:start+PAGE_SIZE], start+1):
        price_str = f"{r['price']:,}" if r.get('price') else "?"
        disc_str = f" 🔥{r['discount']}%" if r.get('discount') else ""
        lines.append(f"{i}. {esc(r['title'][:55])}")
        lines.append(f"   💰 {price_str} ت{disc_str}")

    kb = results_kb(results, page, total_pages, len(results), filters)
    text = "\n".join(lines)

    if edit:
        try:
            await target.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass

    if hasattr(target, "message"):
        await target.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def render_detail(query, context, abs_idx: int, from_list=False):
    if from_list:
        product = get_product(abs_idx)
        if not product:
            await query.message.reply_text("❌ پیدا نشد.")
            return
        title, url = product["title"], product["url"]
        price = product["last_price"]
        rating = rating_count = image = None
    else:
        results = context.user_data.get("search", {}).get("results", [])
        if abs_idx >= len(results):
            await query.message.reply_text("❌ خطا.")
            return
        r = results[abs_idx]
        title, url = r["title"], r["url"]
        price = r.get("price")
        rating = r.get("rating")
        rating_count = r.get("rating_count")
        image = r.get("image")

    lines = [f"📦 <b>{esc(title)}</b>\n"]
    if price:
        lines.append(f"💰 قیمت: <b>{price:,} تومان</b>")
    else:
        lines.append("💰 قیمت: نامشخص")
    if rating and rating_count:
        stars = rating / 20
        lines.append(f"⭐ {stars:.1f}/5 ({rating_count} نظر)")
    lines.append(f"\n🔗 <a href='{url}'>لینک دیجی‌کالا</a>")

    text = "\n".join(lines)

    if from_list:
        pid = abs_idx
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ ویرایش هدف", callback_data=f"edt:{pid}")],
            [InlineKeyboardButton("🗑 حذف", callback_data=f"del:{pid}")],
            [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="menu:list")],
        ])
    else:
        kb = product_detail_kb(url, abs_idx)

    if image:
        try:
            await query.message.reply_photo(photo=image, caption=text,
                                             parse_mode="HTML", reply_markup=kb)
            return
        except Exception as e:
            logger.warning(f"عکس: {e}")

    await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ============================================
# پیام متنی
# ============================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    text = update.message.text.strip() if update.message.text else ""

    # ---------- منتظر اسم یا لینک ----------
    if state == "waiting_name":
        if len(text) < 2:
            await update.message.reply_text("❌ کوتاهه. دوباره بفرست.")
            return

        # ============ حالت ۱: کاربر لینک فرستاده ============
        if text.startswith("http"):
            msg = await update.message.reply_text("⏳ در حال دریافت اطلاعات از لینک...")

            pid_str = extract_product_id(text)
            if not pid_str:
                await msg.edit_text(
                    "❌ لینک معتبر نیست.\n"
                    "لطفاً لینک دیجی‌کالا بفرست یا اسم محصول رو تایپ کن."
                )
                return

            data = fetch_product_api(pid_str)
            if not data:
                await msg.edit_text("❌ نتونستم اطلاعات محصول رو بگیرم. دوباره امتحان کن.")
                return

            result = parse_api_response(data)
            if not result:
                await msg.edit_text("❌ قیمت محصول پیدا نشد.")
                return

            title, price = result
            url = f"https://www.digikala.com/product/dkp-{pid_str}/"

            # عکس
            product_data = data.get("data", {}).get("product", {})
            images = product_data.get("images", {}) or {}
            main_img = images.get("main", {}) if isinstance(images, dict) else {}
            img_urls = main_img.get("url", []) if isinstance(main_img, dict) else []
            if isinstance(img_urls, str):
                img_urls = [img_urls]
            image = img_urls[0] if img_urls else None

            product = {
                "id": pid_str,
                "title": title,
                "url": url,
                "price": price,
                "image": image,
            }
            context.user_data["pending_product"] = product
            context.user_data["state"] = "waiting_target"

            try:
                await msg.delete()
            except Exception:
                pass

            text_out = (
                f"✅ <b>محصول پیدا شد:</b>\n\n"
                f"📦 {esc(title)}\n"
                f"💰 قیمت فعلی: <b>{price:,} تومان</b>\n\n"
                f"🎯 <b>قیمت هدف</b> رو بفرست (تومان) یا /skip."
            )

            if image:
                try:
                    await update.message.reply_photo(
                        photo=image, caption=text_out,
                        parse_mode="HTML", reply_markup=cancel_kb()
                    )
                    return
                except Exception as e:
                    logger.warning(f"عکس: {e}")

            await update.message.reply_text(
                text_out, parse_mode="HTML", reply_markup=cancel_kb()
            )
            return

        # ============ حالت ۲: کاربر اسم فرستاده (جستجو) ============
        msg = await update.message.reply_text("⏳ در حال جستجو (3 صفحه)...")

        results = search_products(text, limit=MAX_RESULTS, sort="relevance")
        if not results:
            await msg.edit_text("❌ چیزی پیدا نشد. /add بزن.")
            context.user_data.clear()
            return

        context.user_data["search"] = {
            "query": text, "sort": "relevance",
            "results": results, "page": 0, "filters": {},
        }
        context.user_data["state"] = "browsing"
        try:
            await msg.delete()
        except Exception:
            pass
        await render_results(update, context, edit=False)
        return

    # ---------- منتظر قیمت هدف ----------
    if state == "waiting_target":
        if text == "/skip" or text == "0":
            target = 0
        else:
            try:
                target = int(text.replace(",", "").replace("،", "").replace(" ", ""))
                if target < 1000:
                    await update.message.reply_text("⚠️ عدد کمه. یا /skip.")
                    return
            except ValueError:
                await update.message.reply_text("❌ فقط عدد یا /skip.")
                return

        product = context.user_data.get("pending_product")
        if not product:
            await update.message.reply_text("❌ خطا. /add بزن.")
            context.user_data.clear()
            return

        # پاس دادن price برای ذخیره قیمت اولیه
        pid = insert_product(product["url"], product["title"], target,
                             price=product.get("price"))
        if pid:
            target_str = f"{target:,} تومان" if target else "بدون هدف"
            price_str = f"{product.get('price', 0):,}" if product.get('price') else "?"
            await update.message.reply_text(
                f"✅ <b>اضافه شد!</b>\n\n"
                f"📦 {esc(product['title'])}\n"
                f"💰 {price_str} تومان\n"
                f"🎯 {target_str}\n\n"
                f"از الان تغییرات رو خبر می‌دم. 🔔",
                parse_mode="HTML", reply_markup=main_menu_kb()
            )
        else:
            await update.message.reply_text("❌ خطا در ذخیره.",
                                            reply_markup=main_menu_kb())
        context.user_data.clear()
        return

    # ---------- منتظر ویرایش هدف ----------
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
                    await update.message.reply_text("⚠️ عدد کمه.")
                    return
            except ValueError:
                await update.message.reply_text("❌ فقط عدد یا /skip.")
                return

        if update_target_price(pid, target):
            target_str = f"{target:,} تومان" if target else "بدون هدف"
            await update.message.reply_text(f"✅ هدف: {target_str}",
                                            reply_markup=main_menu_kb())
        else:
            await update.message.reply_text("❌ خطا.")
        context.user_data.clear()
        return

    # ---------- منتظر محدوده قیمت ----------
    if state == "waiting_price_range":
        if text == "/skip":
            f = context.user_data.get("search", {}).get("filters", {})
            f.pop("min_price", None)
            f.pop("max_price", None)
            context.user_data["search"]["filters"] = f
            context.user_data["state"] = "browsing"
            try:
                await update.message.reply_text(
                    "✅ فیلتر قیمت حذف شد.\n"
                    "می‌تونی فیلتر دیگه انتخاب کنی یا «نمایش نتایج» رو بزنی:",
                    reply_markup=filters_menu_kb(f)
                )
            except Exception as e:
                logger.error(f"price skip: {e}")
            return

        try:
            if "-" not in text:
                raise ValueError("خط تیره لازمه")
            parts = text.replace("،", ",").replace(",", "").split("-")
            if len(parts) != 2:
                raise ValueError("فرمت اشتباه")
            min_p = int(parts[0].strip()) if parts[0].strip() else 0
            max_p = int(parts[1].strip()) if parts[1].strip() else 0
            if min_p < 0 or max_p < 0:
                raise ValueError("منفی نمی‌شه")
            if min_p > 0 and max_p > 0 and min_p >= max_p:
                raise ValueError("min باید < max باشه")

            f = context.user_data.get("search", {}).get("filters", {})
            f["min_price"] = min_p
            f["max_price"] = max_p
            context.user_data["search"]["filters"] = f
            context.user_data["state"] = "browsing"

            await update.message.reply_text(
                f"✅ محدوده قیمت تنظیم شد: {min_p:,} — {max_p:,} تومان\n\n"
                f"حالا می‌تونی فیلتر دیگه‌ای هم انتخاب کنی یا نتایج رو ببینی:",
                reply_markup=filters_menu_kb(f)
            )
        except ValueError:
            await update.message.reply_text(
                "❌ فرمت اشتباه.\n"
                "مثال: <code>500000-2000000</code>\n"
                "برای لغو: /cancel",
                parse_mode="HTML"
            )
        return

    await update.message.reply_text("🤔 متوجه نشدم. /start",
                                    reply_markup=main_menu_kb())


# ============================================
# دکمه‌ها
# ============================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "noop":
        return

    # ---------- منوی اصلی ----------
    if data == "menu:main":
        context.user_data.clear()
        try:
            await query.edit_message_text("👋 <b>منوی اصلی</b>",
                                          parse_mode="HTML", reply_markup=main_menu_kb())
        except Exception:
            await query.message.reply_text("👋 <b>منوی اصلی</b>",
                                           parse_mode="HTML", reply_markup=main_menu_kb())
        return

    # ---------- منوی افزودن ----------
    if data == "menu:add":
        context.user_data.clear()
        context.user_data["state"] = "waiting_name"
        try:
            await query.edit_message_text(
                "🔍 <b>اسم محصول یا لینکش رو بفرست:</b>\n\n"
                "• اسم: <code>ساک ورزشی</code>\n"
                "• لینک: <code>https://www.digikala.com/product/dkp-22214723/</code>",
                parse_mode="HTML", reply_markup=cancel_kb()
            )
        except Exception:
            await query.message.reply_text(
                "🔍 <b>اسم محصول یا لینکش رو بفرست:</b>\n\n"
                "• اسم: <code>ساک ورزشی</code>\n"
                "• لینک: <code>https://www.digikala.com/product/dkp-22214723/</code>",
                parse_mode="HTML", reply_markup=cancel_kb()
            )
        return

    # ---------- لیست ----------
    if data == "menu:list":
        context.user_data.clear()
        products = get_all_products()
        if not products:
            try:
                await query.edit_message_text("📭 محصولی نداری.",
                                              reply_markup=main_menu_kb())
            except Exception:
                await query.message.reply_text("📭 محصولی نداری.",
                                               reply_markup=main_menu_kb())
            return
        try:
            await query.message.delete()
        except Exception:
            pass
        await _render_list(query.message, products)
        return

    # ---------- خروجی CSV ----------
    if data == "menu:export":
        try:
            export_to_csv()
            if os.path.exists(CSV_PATH):
                with open(CSV_PATH, "rb") as f:
                    await query.message.reply_document(
                        document=f, filename="prices.csv", caption="📊 CSV"
                    )
        except Exception as e:
            await query.message.reply_text(f"❌ خطا: {e}")
        return

    # ---------- راهنما ----------
    if data == "menu:help":
        await cmd_help(update, context)
        return

    # ---------- لغو ----------
    if data == "cncl":
        context.user_data.clear()
        try:
            await query.edit_message_text("✅ لغو شد.", reply_markup=main_menu_kb())
        except Exception:
            await query.message.reply_text("✅ لغو شد.", reply_markup=main_menu_kb())
        return

    # ---------- منوی فیلترها ----------
    if data == "flt:menu":
        filters = context.user_data.get("search", {}).get("filters", {})
        try:
            await query.edit_message_reply_markup(reply_markup=filters_menu_kb(filters))
        except Exception as e:
            logger.error(f"filters menu: {e}")
        return

    # ---------- فقط تخفیف‌دار ----------
    if data == "flt:disc":
        s = context.user_data.get("search", {})
        f = s.get("filters", {})
        f["only_discounted"] = not f.get("only_discounted", False)
        s["filters"] = f
        context.user_data["search"] = s
        try:
            await query.edit_message_reply_markup(reply_markup=filters_menu_kb(f))
        except Exception as e:
            logger.error(f"flt:disc: {e}")
        return

    # ---------- فقط موجود ----------
    if data == "flt:stock":
        s = context.user_data.get("search", {})
        f = s.get("filters", {})
        f["only_available"] = not f.get("only_available", False)
        s["filters"] = f
        context.user_data["search"] = s
        try:
            await query.edit_message_reply_markup(reply_markup=filters_menu_kb(f))
        except Exception as e:
            logger.error(f"flt:stock: {e}")
        return

    # ---------- حذف همه فیلترها ----------
    if data == "flt:clear":
        if "search" in context.user_data:
            context.user_data["search"]["filters"] = {}
        try:
            await query.edit_message_reply_markup(reply_markup=filters_menu_kb({}))
        except Exception as e:
            logger.error(f"flt:clear: {e}")
        return

    # ---------- اعمال فیلترها ----------
    if data == "flt:apply":
        _do_search(context)
        await render_results(query, context, edit=True)
        return

    # ---------- محدوده قیمت ----------
    if data == "flt:price":
        context.user_data["state"] = "waiting_price_range"
        try:
            await query.edit_message_text(
                "💰 <b>محدوده قیمت (تومان):</b>\n\n"
                "فرمت: <code>min-max</code>\n\n"
                "مثال:\n"
                "• <code>500000-2000000</code>\n"
                "• <code>0-1000000</code> (تا ۱ میلیون)\n"
                "• <code>500000-0</code> (از ۵۰۰ هزار به بالا)\n\n"
                "برای حذف فیلتر قیمت: /skip",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ لغو", callback_data="flt:menu_back")
                ]])
            )
        except Exception as e:
            logger.error(f"flt:price: {e}")
        return

    # ---------- بازگشت به منوی فیلترها ----------
    if data == "flt:menu_back":
        f = context.user_data.get("search", {}).get("filters", {})
        context.user_data["state"] = "browsing"
        try:
            await query.edit_message_text(
                "🔧 <b>فیلترها</b>\n\n"
                "چند تا فیلتر رو با هم می‌تونی روشن کنی،\n"
                "بعد «🔙 نمایش نتایج» رو بزن:",
                parse_mode="HTML",
                reply_markup=filters_menu_kb(f)
            )
        except Exception as e:
            logger.error(f"flt:menu_back: {e}")
        return

    # ---------- ناوبری صفحات ----------
    if data.startswith("pg:"):
        page = int(data.split(":")[1])
        if "search" in context.user_data:
            context.user_data["search"]["page"] = page
        await render_results(query, context, edit=True)
        return

    # ---------- بازگشت به نتایج ----------
    if data == "back_results":
        if "search" in context.user_data:
            await render_results(query, context, edit=True)
        else:
            try:
                await query.edit_message_text("❌ نتایج قدیمی. /add",
                                              reply_markup=main_menu_kb())
            except Exception:
                await query.message.reply_text("❌ نتایج قدیمی. /add",
                                               reply_markup=main_menu_kb())
        return

    # ---------- مرتب‌سازی ----------
    if data.startswith("srt:"):
        sort = data.split(":")[1]
        s = context.user_data.get("search", {})
        if not s.get("query"):
            await query.message.reply_text("❌ از /add شروع کن.")
            return
        s["sort"] = sort
        context.user_data["search"] = s
        _do_search(context)
        await render_results(query, context, edit=True)
        return

    # ---------- صفحه جزئیات ----------
    if data.startswith("det:"):
        idx = int(data.split(":")[1])
        await render_detail(query, context, idx, from_list=False)
        return

    # ---------- اطلاعات محصول ----------
    if data.startswith("info:"):
        pid = int(data.split(":")[1])
        product = get_product(pid)
        if not product:
            await query.message.reply_text("❌ پیدا نشد.")
            return

        lines = [f"📦 <b>{esc(product['title'])}</b>\n"]
        if product['last_price']:
            lines.append(f"💰 <b>{product['last_price']:,} تومان</b>")
        if product['target']:
            lines.append(f"🎯 هدف: {product['target']:,} تومان")
        lines.append(f"\n🔗 <a href='{product['url']}'>لینک دیجی‌کالا</a>")

        if product['history']:
            lines.append("\n📈 <b>تاریخچه:</b>")
            for price, ts in product['history'][:5]:
                try:
                    # اگه ts از نوع datetime بود (PostgreSQL)
                    if hasattr(ts, "strftime"):
                        dt = ts.strftime("%m/%d %H:%M")
                    else:
                        # اگه string بود (SQLite قدیمی)
                        dt = datetime.fromisoformat(str(ts)).strftime("%m/%d %H:%M")
                except Exception:
                    dt = str(ts)[:16]
                lines.append(f"  • {price:,} ت ({dt})")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ ویرایش هدف", callback_data=f"edt:{pid}")],
            [InlineKeyboardButton("🗑 حذف", callback_data=f"del:{pid}")],
            [InlineKeyboardButton("🔙 لیست", callback_data="menu:list")],
        ])
        try:
            await query.edit_message_text("\n".join(lines), parse_mode="HTML",
                                          reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            await query.message.reply_text("\n".join(lines), parse_mode="HTML",
                                           reply_markup=kb, disable_web_page_preview=True)
        return

        # ---------- مقایسه در ترب ----------
    if data.startswith("torob:"):
        idx = int(data.split(":")[1])
        results = context.user_data.get("search", {}).get("results", [])
        if idx >= len(results):
            await query.message.reply_text("❌ خطا. دوباره /add بزن.")
            return

        product = results[idx]
        title = product["title"]

        msg = await query.message.reply_text(
            f"🔍 در حال جستجو در ترب برای:\n«{esc(title[:50])}»..."
        )

        torob_results = search_torob(title, limit=10)

        if not torob_results:
            await msg.edit_text(
                "❌ نتیجه‌ای در ترب پیدا نشد.\n"
                "شاید عنوان خیلی خاصه. دوباره امتحان کن."
            )
            return

        lines = [f"🏆 <b>مقایسه در ترب</b>\n"]
        lines.append(f"📦 «{esc(title[:60])}»\n")

        # ارزان‌ترین
        valid = [r for r in torob_results if r.get("price")]
        if valid:
            cheapest = min(valid, key=lambda x: x["price"])
            lines.append(f"💰 <b>ارزان‌ترین در ترب:</b> {cheapest['price']:,} تومان")
            if cheapest.get("shop_text"):
                lines.append(f"🏪 {esc(cheapest['shop_text'])}")
            lines.append("")

        # مقایسه با دیجی‌کالا
        dk_price = product.get("price")
        if dk_price and valid:
            diff = cheapest["price"] - dk_price
            if diff < 0:
                lines.append(f"📉 <b>ترب {abs(diff):,} تومان ارزان‌تره!</b>")
            elif diff > 0:
                lines.append(f"📈 <b>دیجی‌کالا {abs(diff):,} تومان ارزان‌تره</b>")
            else:
                lines.append("🤝 <b>قیمت‌ها برابره</b>")
            lines.append("")

            lines.append(f"<b>نتایج برتر ترب ({len(torob_results)}):</b>\n")

        kb = []
        for i, r in enumerate(torob_results[:10], 1):
            price_str = f"{r['price']:,}" if r.get("price") else "?"
            lines.append(f"{i}. {esc(r['name'][:50])}")
            lines.append(f"   💰 {price_str} تومان")
            if r.get("shop_text"):
                lines.append(f"   🏪 {esc(r['shop_text'])}")
            if r.get("url"):
                kb.append([InlineKeyboardButton(
                    f"🛒 {i}. {r['name'][:32]} — {price_str} ت",
                    url=r["url"]
                )])

        kb.append([InlineKeyboardButton("🔙 بازگشت به نتایج", callback_data="back_results")])

        try:
            await msg.edit_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"torob edit: {e}")
        return

    # ---------- افزودن به پایش ----------
    if data.startswith("add:"):
        idx = int(data.split(":")[1])
        results = context.user_data.get("search", {}).get("results", [])
        logger.info(f"add clicked: idx={idx}, total={len(results)}")

        if idx >= len(results):
            await query.message.reply_text("❌ خطا. /add بزن.")
            return

        product = results[idx]
        context.user_data["pending_product"] = product
        context.user_data["state"] = "waiting_target"

        price_str = f"{product['price']:,}" if product.get('price') else "?"
        await query.message.reply_text(
            f"✅ <b>انتخاب شد:</b>\n\n"
            f"📦 {esc(product['title'])}\n"
            f"💰 {price_str} تومان\n\n"
            f"🎯 <b>قیمت هدف</b> رو بفرست (تومان) یا /skip.",
            parse_mode="HTML", reply_markup=cancel_kb()
        )
        return

    # ---------- ویرایش هدف ----------
    if data.startswith("edt:"):
        pid = int(data.split(":")[1])
        context.user_data["edit_pid"] = pid
        context.user_data["state"] = "editing_target"
        try:
            await query.edit_message_text(
                "✏️ <b>قیمت هدف جدید (تومان):</b>\n\n"
                "برای حذف هدف: /skip یا 0",
                parse_mode="HTML", reply_markup=cancel_kb()
            )
        except Exception:
            await query.message.reply_text(
                "✏️ <b>قیمت هدف جدید:</b>\n/skip = حذف",
                parse_mode="HTML", reply_markup=cancel_kb()
            )
        return

    # ---------- حذف با تأیید ----------
    if data.startswith("del:"):
        pid = int(data.split(":")[1])
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ بله", callback_data=f"delc:{pid}"),
            InlineKeyboardButton("❌ نه", callback_data="menu:list"),
        ]])
        try:
            await query.edit_message_text("⚠️ مطمئنی؟", reply_markup=kb)
        except Exception:
            await query.message.reply_text("⚠️ مطمئنی؟", reply_markup=kb)
        return

    if data.startswith("delc:"):
        pid = int(data.split(":")[1])
        if delete_product(pid):
            try:
                await query.edit_message_text(
                    "✅ حذف شد.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 لیست", callback_data="menu:list")
                    ]])
                )
            except Exception:
                await query.message.reply_text("✅ حذف شد.")
        else:
            await query.message.reply_text("❌ خطا.")


# ============================================
# گزارش روزانه (Thread جدا)
# ============================================
def daily_report_thread():
    """هر روز ساعت مشخص، گزارش می‌فرسته"""
    from datetime import timezone, timedelta

    TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

    # ساعت گزارش (به وقت تهران) — از env یا پیش‌فرض ۲۱
    report_hour = int(os.getenv("REPORT_HOUR", "21"))
    report_minute = int(os.getenv("REPORT_MINUTE", "0"))

    logger.info(f"📊 گزارش روزانه تنظیم شد برای {report_hour}:{report_minute:02d} تهران")

    while True:
        try:
            now = datetime.now(TEHRAN_TZ)
            target = now.replace(
                hour=report_hour,
                minute=report_minute,
                second=0,
                microsecond=0,
            )
            if target <= now:
                target += timedelta(days=1)

            wait_sec = (target - now).total_seconds()
            logger.info(f"📊 گزارش بعدی: {target.strftime('%Y-%m-%d %H:%M')} "
                        f"(تا {wait_sec/3600:.1f} ساعت دیگه)")

            time.sleep(wait_sec)

            send_daily_report()
        except Exception as e:
            logger.error(f"Report thread: {e}")
            time.sleep(3600)

# ============================================
# اسکرپر پس‌زمینه
# ============================================
def run_scraper_in_background():
    try:
        time.sleep(10)
        logger.info("🚀 اسکرپر شروع شد...")
        send_startup_message()
        monitor("urls.txt", once=False)
    except Exception as e:
        logger.error(f"Scraper: {e}")


def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN نیست!")
        return
    init_db()
    t = threading.Thread(target=run_scraper_in_background, daemon=True)
    t.start()
    logger.info("✅ اسکرپر شروع شد.")

    # گزارش روزانه
    report_thread = threading.Thread(target=daily_report_thread, daemon=True)
    report_thread.start()
    logger.info("✅ گزارش روزانه فعال شد.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.COMMAND, handle_text))

    logger.info("🤖 ربات روشن شد...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
