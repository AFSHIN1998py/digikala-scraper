# 📊 ربات پایش قیمت دیجی‌کالا

<div align="center">

![Python](https://img.shields.io/badge/Python-3.12-blue?style=for-the-badge&logo=python)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue?style=for-the-badge&logo=telegram)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon-green?style=for-the-badge&logo=postgresql)
![Railway](https://img.shields.io/badge/Deployed-Railway-purple?style=for-the-badge&logo=railway)

**پایش خودکار قیمت محصولات دیجی‌کالا + هشدار فوری + مقایسه با ترب**

[🎬 تماشای دمو](DEMO.md) · [📞 درخواست مشاوره](https://t.me/your_username)

</div>

---

## 💡 این ربات چیکار می‌کنه؟

اگه فروشنده دیجی‌کالا هستی، می‌دونی که **قیمت رقبا لحظه‌ای عوض می‌شه**. اگه دیر بفهمی، فروشت می‌ریزه.

این ربات **۲۴ ساعته برات پایش می‌کنه** و:

- 🔔 هر تغییر قیمت رو **فوری** به تلگرامت می‌فرسته
- 🎯 وقتی قیمت به هدف تو رسید، خبر می‌ده
- 🏆 **مقایسه با ترب** می‌کنه (ارزان‌ترین قیمت بازار رو نشون می‌ده)
- 📊 **گزارش روزانه** از همه تغییرات می‌ده
- 📁 خروجی **CSV** برای تحلیل توی اکسل

---

## ✨ امکانات

### 🤖 ربات تعاملی تلگرام

- منوی دکمه‌ای ساده (بدون نیاز به دانش فنی)
- افزودن محصول با **اسم** یا **لینک**
- جستجوی هوشمند توی ۶۰ محصول
- فیلتر قیمت، تخفیف، موجودی
- صفحه‌بندی، مرتب‌سازی، جزئیات کامل

### 📈 پایش خودکار

- هر ۶ ساعت (قابل تنظیم)
- تغییرات قیمت با درصد
- تشخیص رسیدن به قیمت هدف
- ذخیره دائمی روی PostgreSQL

### 🏆 مقایسه با ترب

- جستجوی خودکار در ترب
- نمایش ارزان‌ترین قیمت بازار
- لینک خرید مستقیم

### 📊 گزارش و خروجی

- گزارش روزانه خودکار
- فایل CSV از تاریخچه
- نمودار تغییرات (به‌زودی)

---

## 🎬 دموی تصویری

[![دمو](https://img.shields.io/badge/▶️_تماشای_دمو-YouTube-red?style=for-the-badge)](لینک_ویدیو_اینجا)

نمونه پیام‌های ربات:

> 🔻 **تغییر قیمت!**
>
> 📦 ساک ورزشی مدل Alpha-3
> 💰 قبلی: 4,998,000 تومان
> 💰 جدید: **4,950,000 تومان**
> 📊 تغییر: 48,000- تومان (1.0%)

> 🎯 **قیمت به هدف رسید!**
>
> 📦 ساک ورزشی مدل 1778
> 💰 قیمت: **2,290,000 تومان**
> 🎯 هدف: 2,300,000 تومان

---

## 🚀 راه‌اندازی سریع

### پیش‌نیاز

- Python 3.12
- یه ربات تلگرام (از [@BotFather](https://t.me/BotFather))
- یه دیتابیس PostgreSQL رایگان از [Neon](https://neon.tech)

### نصب

```bash
git clone https://github.com/AFSHIN1998py/digikala-scraper.git
cd digikala-scraper
pip install -r requirements.txt -i https://mirror-pypi.runflare.com/simple/
تنظیمات
فایل .env بساز (از .env.example کپی کن):

env
BOT_TOKEN=توکن_ربات_تلگرام
CHAT_ID=آیدی_عددی_تو
DATABASE_URL=postgresql://...
CHECK_INTERVAL=21600
REPORT_HOUR=21
اجرا
bash
python bot.py
دیپلوی روی Railway
پروژه رو از گیت‌هاب به Railway وصل کن

Variables رو ست کن

Railway خودکار bot.py رو اجرا می‌کنه

🛠 تکنولوژی‌ها
بخش	تکنولوژی
زبان	Python 3.12
ربات	python-telegram-bot 22.3
دیتابیس	PostgreSQL (Neon)
HTTP	requests, curl_cffi
پارس	BeautifulSoup4, lxml
استقرار	Railway
📂 ساختار پروژه
text
digikala-scraper/
├── scraper.py          # هسته پایش + جستجو + ترب
├── bot.py              # ربات تلگرام
├── requirements.txt
├── Procfile
├── .python-version
├── PROJECT_STATE.md    # وضعیت پروژه
├── DEMO.md             # دموی تصویری
└── README.md
💰 تعرفه فروش
پلن	تعداد محصول	نصب + اشتراک ماهانه
پایه	۵ محصول	۱.۵ میلیون + ۲۰۰ هزار/ماه
حرفه‌ای	۲۰ محصول	۳ میلیون + ۴۰۰ هزار/ماه
سازمانی	۵۰+ محصول	۶ میلیون + ۸۰۰ هزار/ماه
شامل: نصب + آموزش + پشتیبانی + آپدیت رایگان

📞 تماس
تلگرام: @AFSHIN77AM

گیت‌هاب: AFSHIN1998py

⭐ اگه این پروژه برات مفید بود، یه ستاره بده!
