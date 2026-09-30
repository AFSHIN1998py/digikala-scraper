import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

print("BOT_TOKEN:", BOT_TOKEN[:15] + "..." if BOT_TOKEN else "❌ خالی")
print("CHAT_ID:", CHAT_ID if CHAT_ID else "❌ خالی")
print()

if not BOT_TOKEN or not CHAT_ID:
    print("❌ .env پر نیست! BOT_TOKEN و CHAT_ID رو چک کن.")
    exit()

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

try:
    r = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": "🎉 تست اسکرپر دیجی‌کالا - اگه این پیام رو می‌بینی، تلگرام حله!",
    }, timeout=10)

    print("STATUS:", r.status_code)
    print("RESPONSE:", r.text)

    if r.status_code == 200:
        print("\n✅ پیام ارسال شد! تلگرام رو چک کن.")
    else:
        print("\n❌ ارسال نشد. کد خطا رو بخون.")
except Exception as e:
    print("❌ خطا:", e)