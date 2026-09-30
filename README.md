# 🎵 Telegram Musiqa Boti

python-telegram-bot (v20+, async), yt-dlp, Shazam va ffmpeg asosidagi musiqa boti.

## Imkoniyatlar

- **Inline qidiruv** — istalgan chatda `@bot_username qo'shiq_nomi`
- **Matn / havola** — qo'shiq nomi yoki YouTube, SoundCloud, Spotify havolasi → 320 kbps mp3
- **Instagram havolasi** — videodagi qo'shiq Shazam orqali aniqlanadi va to'liq mp3 yuboriladi
- **Ovozli xabar / video** — Shazam orqali qo'shiq aniqlanadi
- **Effektlar** — 🎧 8D, 🏟 Concert Hall, 🐢 Slowed (+ reverb)
- **✏️ Tahrirlash** — nom (Title), ijrochi (Performer), muqova rasmi
- **🎙 Ovozli xabar qilish** — mp3 → Telegram Voice (ogg/opus)
- Vaqtinchalik `temp_*` fayllar yuborilgach avtomatik o'chiriladi

## Fayllar

| Fayl | Vazifasi |
|---|---|
| `bot.py` | Bot kodi |
| `requirements.txt` | Kutubxonalar |
| `Procfile` | `web: python bot.py` |

## Lokal ishga tushirish

```bash
pip install -r requirements.txt
export BOT_TOKEN="123456:ABC..."
python bot.py
```

`RENDER_EXTERNAL_URL` o'rnatilmagan bo'lsa, bot avtomatik polling rejimida ishlaydi.

## Render.com'ga joylash (Web Service)

1. Kodni GitHub'ga yuklang (`bot.py`, `requirements.txt`, `Procfile`).
2. Render → **New → Web Service** → repo'ni tanlang.
3. Sozlamalar:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
4. **Environment Variables:**

| Nomi | Majburiy | Izoh |
|---|---|---|
| `BOT_TOKEN` | ✅ | @BotFather bergan token |
| `PYTHON_VERSION` | ✅ | `3.11.9` (shazamio uchun) |
| `STORAGE_CHAT_ID` | ❌ | Inline rejim uchun fayl yuklanadigan chat ID. Bo'sh bo'lsa, foydalanuvchining shaxsiy chatiga yuklanib, keyin o'chiriladi |
| `COOKIES_FILE` | ❌ | Instagram uchun, masalan `/etc/secrets/cookies.txt` |

`RENDER_EXTERNAL_URL` va `PORT` ni Render o'zi beradi. Bot webhook'ni avtomatik o'rnatadi.

## @BotFather sozlamalari

```
/setinline          → placeholder yozing, masalan: Qo'shiq nomi...
/setinlinefeedback  → Enabled (100%)
```

`/setinlinefeedback` yoqilmasa, inline natija tanlanganda musiqa yuborilmaydi.

## Instagram uchun cookies (kerak bo'lsa)

Instagram ko'pincha server IP'larini bloklaydi. Xato chiqsa:

1. Brauzerda Instagram'ga kiring va "Get cookies.txt" kabi kengaytma bilan `cookies.txt` eksport qiling.
2. Render → **Environment → Secret Files** → `cookies.txt` nomi bilan yuklang.
3. `COOKIES_FILE=/etc/secrets/cookies.txt` qo'shing.

> Cookies shaxsiy akkauntingizga kirish beradi. Uni hech kimga bermang, alohida akkaunt ishlatgan ma'qul.

## Muhim cheklovlar

- Telegram bot orqali yuklab olish limiti — **20 MB**, yuborish limiti — **50 MB**.
- 20 daqiqadan uzun videolar yuklanmaydi (xotirani asrash uchun).
- Spotify'da faqat trek havolalari qo'llab-quvvatlanadi (nomi olinib, YouTube'dan qidiriladi).
- Render bepul rejasida xizmat uxlab qoladi, birinchi javob sekin bo'lishi mumkin.
- Bir vaqtda 2 tadan ortiq og'ir jarayon ishlamaydi (bepul rejadagi 512 MB RAM uchun).
- Inline xabarlardagi fayl ma'lumoti xotirada saqlanadi, bot qayta ishga tushsa, eski inline xabarlardagi effekt tugmalari ishlamay qolishi mumkin.

## Foydalanish

1. `/start` bosing.
2. Qo'shiq nomi yoki havola yuboring.
3. Audio tagidagi tugmalar orqali effekt qo'shing, tahrirlang yoki ovozli xabarga aylantiring.
4. Tahrirlashda: nom → ijrochi → rasm ketma-ketligida javob bering. O'zgartirmaslik uchun `-`, bekor qilish uchun `/cancel`.
