# اسکرپر دیجی‌کالا 🛒
پایش لحظه‌ای قیمت محصولات دیجی‌کالا + هشدار تلگرام

## نصب
pip install -r requirements.txt

## تنظیمات
1. فایل .env.example رو کپی کن به .env
2. توکن تلگرام و چت‌آیدی خودت رو داخل .env بذار
3. لینک محصولات رو در urls.txt بنویس

## اجرا
python scraper.py --url "لینک_محصول" --once
python scraper.py