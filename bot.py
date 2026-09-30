import asyncio
import io
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

import aiohttp
import static_ffmpeg

static_ffmpeg.add_paths()  # ffmpeg + ffprobe ni PATH ga qo'shadi (Render'da ffmpeg yo'q)

from PIL import Image
from shazamio import Shazam
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputMediaAudio,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import match_filter_func

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("musicbot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID")  # ixtiyoriy: inline uchun fayl yuklanadigan chat
BASE_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render o'zi beradi
PORT = int(os.getenv("PORT", "10000"))
TMP = Path("/tmp")
URL_RE = re.compile(r"https?://\S+")
SEM = asyncio.Semaphore(2)  # bir vaqtda 2 ta og'ir jarayon (xotirani asrash uchun)

# Cookies (Instagram uchun kerak bo'lishi mumkin). Render Secret File: /etc/secrets/cookies.txt
COOKIES = None
_src = os.getenv("COOKIES_FILE")
if _src and os.path.exists(_src):
    COOKIES = str(TMP / "cookies.txt")
    shutil.copy(_src, COOKIES)  # yt-dlp yozishi mumkin, shuning uchun nusxa


# ----------------------------------------------------------------- yordamchilar
def tmp(ext: str) -> str:
    return str(TMP / f"temp_{uuid.uuid4().hex}.{ext}")


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception as e:
            log.warning("cleanup xato: %s", e)


async def ffmpeg(*args):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stderr=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="ignore")[-300:])


def ydl_opts(**kw):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30, "retries": 3}
    if COOKIES:
        o["cookiefile"] = COOKIES
    o.update(kw)
    return o


def make_thumb(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((320, 320))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return b.getvalue()


async def get_thumb(url):
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as s, s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return make_thumb(await r.read())
    except Exception:
        return None


def kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎧 8D", callback_data="fx:8d"),
         InlineKeyboardButton("🏟 Concert Hall", callback_data="fx:hall")],
        [InlineKeyboardButton("🐢 Slowed", callback_data="fx:slow")],
        [InlineKeyboardButton("✏️ Tahrirlash", callback_data="edit"),
         InlineKeyboardButton("🎙 Ovozli xabar qilish", callback_data="voice")],
    ])


# ----------------------------------------------------------------- yuklab olish
def _search(query: str, n: int = 5):
    with YoutubeDL(ydl_opts(extract_flat=True)) as y:
        info = y.extract_info(f"ytsearch{n}:{query}", download=False)
    return [e for e in (info or {}).get("entries", []) if e]


def _download_audio(url: str) -> dict:
    base = f"temp_{uuid.uuid4().hex}"
    opts = ydl_opts(
        format="bestaudio/best",
        outtmpl=str(TMP / f"{base}.%(ext)s"),
        match_filter=match_filter_func("duration < 1200"),
    )
    with YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    if not info or not info.get("requested_downloads"):
        raise LookupError("Yuklab bo'lmadi (juda uzun yoki mavjud emas)")
    performer = info.get("artist") or info.get("uploader") or ""
    return {
        "src": info["requested_downloads"][0]["filepath"],
        "title": info.get("track") or info.get("title") or "Unknown",
        "performer": performer.replace(" - Topic", ""),
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
    }


def _download_video(url: str) -> str:
    base = f"temp_{uuid.uuid4().hex}"
    opts = ydl_opts(format="best[ext=mp4]/best", outtmpl=str(TMP / f"{base}.%(ext)s"))
    with YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    return info["requested_downloads"][0]["filepath"]


async def fetch_song(query_or_url: str, search: bool = False) -> dict:
    url = query_or_url
    if search:
        entries = await asyncio.to_thread(_search, query_or_url, 5)
        entry = next((e for e in entries if (e.get("duration") or 0) <= 1200), None)
        if not entry:
            raise LookupError("Hech narsa topilmadi")
        url = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
    info = await asyncio.to_thread(_download_audio, url)
    mp3 = tmp("mp3")
    try:
        await ffmpeg("-i", info["src"], "-vn", "-c:a", "libmp3lame", "-b:a", "320k", mp3)
    except Exception:
        cleanup(mp3)
        raise
    finally:
        cleanup(info["src"])
    info["path"] = mp3
    info["thumb"] = await get_thumb(info.pop("thumbnail", None))
    return info


async def send_song(bot, chat_id, song: dict, reply_to=None):
    try:
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        with open(song["path"], "rb") as f:
            return await bot.send_audio(
                chat_id, f, title=song["title"], performer=song["performer"],
                duration=song.get("duration") or None, thumbnail=song.get("thumb"),
                reply_markup=kb(), reply_to_message_id=reply_to,
                read_timeout=120, write_timeout=120,
            )
    finally:
        cleanup(song["path"])


async def spotify_query(url: str) -> str:
    if any(x in url for x in ("/playlist/", "/album/", "/artist/")):
        raise LookupError("Faqat Spotify trek havolalari qo'llab-quvvatlanadi")
    async with aiohttp.ClientSession() as s, s.get(
        "https://open.spotify.com/oembed", params={"url": url}, timeout=aiohttp.ClientTimeout(total=15)
    ) as r:
        data = await r.json()
    return data["title"]


# ----------------------------------------------------------------- Shazam
async def identify(media_path: str):
    """Audio/video fayldan qo'shiqni aniqlaydi. (performer, title) yoki None."""
    shazam = Shazam()
    rec = getattr(shazam, "recognize", None) or shazam.recognize_song
    for offset in (0, 15, 30):
        clip = tmp("mp3")
        try:
            await ffmpeg("-ss", str(offset), "-i", media_path, "-t", "20", "-vn", "-ac", "1", "-ar", "44100", clip)
            if not os.path.exists(clip) or os.path.getsize(clip) < 5000:
                continue
            out = await rec(clip)
            track = (out or {}).get("track")
            if track:
                return track.get("subtitle", ""), track.get("title", "")
        except Exception as e:
            log.warning("shazam urinish xato: %s", e)
        finally:
            cleanup(clip)
    return None


async def find_and_send(update: Update, ctx, media_path: str, status):
    res = await identify(media_path)
    if not res:
        await status.edit_text("❌ Qo'shiq aniqlanmadi. Musiqa aniqroq eshitiladigan qismni yuboring.")
        return
    performer, title = res
    await status.edit_text(f"🎵 Topildi: {performer} — {title}\n⏬ Yuklanmoqda...")
    song = await fetch_song(f"{performer} {title}", search=True)
    song["title"], song["performer"] = title, performer
    await send_song(ctx.bot, update.effective_chat.id, song, reply_to=update.message.message_id)
    await status.delete()


# ----------------------------------------------------------------- handlerlar
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Salom! Menga yuboring:\n"
        "• Qo'shiq nomi yoki ijrochi\n"
        "• YouTube / SoundCloud / Spotify havolasi\n"
        "• Instagram havolasi — videodagi qo'shiqni topib beraman\n"
        "• Ovozli xabar yoki video — Shazam orqali aniqlayman\n\n"
        "Istalgan chatda: @bot_username qo'shiq_nomi"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("edit", None)
    await update.message.reply_text("Bekor qilindi.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text.strip()
    if ctx.user_data.get("edit"):
        return await edit_step(update, ctx, text)

    status = await msg.reply_text("🔎 Qidirilmoqda...")
    video = None
    try:
        async with SEM:
            m = URL_RE.search(text)
            if m and "instagram.com" in m.group(0):
                await status.edit_text("📥 Instagramdan video yuklanmoqda...")
                video = await asyncio.to_thread(_download_video, m.group(0))
                await status.edit_text("🎧 Videodagi qo'shiq aniqlanmoqda...")
                await find_and_send(update, ctx, video, status)
                return
            if m and "spotify" in m.group(0):
                song = await fetch_song(await spotify_query(m.group(0)), search=True)
            elif m:
                song = await fetch_song(m.group(0))
            else:
                song = await fetch_song(text, search=True)
            await status.edit_text("📤 Yuborilmoqda...")
            await send_song(ctx.bot, msg.chat_id, song, reply_to=msg.message_id)
            await status.delete()
    except LookupError as e:
        await status.edit_text(f"❌ {e}")
    except Exception as e:
        log.exception("on_text xato")
        hint = "\n(Instagram ba'zan serverlarni bloklaydi — cookies kerak bo'lishi mumkin.)" if "instagram" in text else ""
        await status.edit_text(f"❌ Xatolik yuz berdi: {str(e)[:150]}{hint}")
    finally:
        cleanup(video)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Voice / video / video_note -> Shazam."""
    msg = update.message
    media = msg.voice or msg.video or msg.video_note
    status = await msg.reply_text("🎧 Aniqlanmoqda...")
    path = tmp("bin")
    try:
        async with SEM:
            f = await ctx.bot.get_file(media.file_id)
            await f.download_to_drive(path)
            await find_and_send(update, ctx, path, status)
    except Exception as e:
        log.exception("on_media xato")
        await status.edit_text(f"❌ Xatolik: {str(e)[:150]}")
    finally:
        cleanup(path)


async def on_audio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchi yuborgan audio tagiga tugmalar qo'shib qaytaradi."""
    try:
        await update.message.reply_audio(update.message.audio.file_id, reply_markup=kb())
    except Exception as e:
        log.exception("on_audio xato")
        await update.message.reply_text(f"❌ Xatolik: {str(e)[:150]}")


# ----------------------------------------------------------------- effektlar
FX = {
    "8d": ("8D", "aresample=44100,apulsator=hz=0.125,aecho=0.8:0.7:40:0.2"),
    "hall": ("Concert Hall", "aresample=44100,aecho=0.8:0.9:40|70|110|180:0.4|0.3|0.25|0.2,treble=g=-2"),
    "slow": ("Slowed", "aresample=44100,asetrate=37485,aresample=44100,aecho=0.8:0.88:60:0.35"),
}


def get_source(q, ctx):
    m = q.message
    if m and m.audio:
        a = m.audio
        return {"file_id": a.file_id, "title": a.title or "Audio", "performer": a.performer or "",
                "duration": a.duration, "thumb_id": a.thumbnail.file_id if a.thumbnail else None}
    return ctx.bot_data.get("inline_files", {}).get(q.inline_message_id)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if data == "noop":
        return await q.answer("Yuklanmoqda, kuting...")
    src = get_source(q, ctx)
    if not src:
        return await q.answer("Fayl topilmadi. Qo'shiqni qaytadan yuboring.", show_alert=True)
    chat_id = q.message.chat_id if q.message else q.from_user.id

    if data == "edit":
        await q.answer()
        ctx.user_data["edit"] = {**src, "chat_id": chat_id, "step": "title"}
        return await ctx.bot.send_message(
            chat_id, "✏️ Yangi nomini (Title) yuboring.\nO'zgartirmaslik uchun: -\nBekor qilish: /cancel")

    await q.answer("⏳ Ishlanmoqda...")
    inp, out = tmp("bin"), tmp("mp3" if data.startswith("fx") else "ogg")
    try:
        async with SEM:
            f = await ctx.bot.get_file(src["file_id"])
            await f.download_to_drive(inp)
            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
            if data.startswith("fx:"):
                label, flt = FX[data.split(":")[1]]
                await ffmpeg("-i", inp, "-vn", "-ac", "2", "-af", flt, "-c:a", "libmp3lame", "-b:a", "192k", out)
                with open(out, "rb") as fh:
                    await ctx.bot.send_audio(
                        chat_id, fh, title=f"{src['title']} ({label})", performer=src["performer"],
                        reply_markup=kb(), read_timeout=120, write_timeout=120)
            elif data == "voice":
                await ffmpeg("-i", inp, "-vn", "-c:a", "libopus", "-b:a", "48k", "-f", "ogg", out)
                with open(out, "rb") as fh:
                    await ctx.bot.send_voice(chat_id, fh, read_timeout=120, write_timeout=120)
    except Exception as e:
        log.exception("on_button xato")
        await ctx.bot.send_message(chat_id, f"❌ Xatolik: {str(e)[:150]}")
    finally:
        cleanup(inp, out)


# ----------------------------------------------------------------- tahrirlash
async def edit_step(update: Update, ctx, text: str):
    st = ctx.user_data["edit"]
    skip = text == "-"
    if st["step"] == "title":
        if not skip:
            st["title"] = text
        st["step"] = "performer"
        await update.message.reply_text("🎤 Ijrochini (Performer) yuboring. O'zgartirmaslik: -")
    elif st["step"] == "performer":
        if not skip:
            st["performer"] = text
        st["step"] = "cover"
        await update.message.reply_text("🖼 Muqova rasmini yuboring. O'zgartirmaslik: -")
    elif st["step"] == "cover":
        if skip:
            await finish_edit(update, ctx, None)
        else:
            await update.message.reply_text("Rasm yuboring yoki - yozing.")


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = ctx.user_data.get("edit")
    if not st or st["step"] != "cover":
        return
    try:
        f = await ctx.bot.get_file(update.message.photo[-1].file_id)
        data = bytes(await f.download_as_bytearray())
        await finish_edit(update, ctx, make_thumb(data))
    except Exception as e:
        log.exception("on_photo xato")
        await update.message.reply_text(f"❌ Xatolik: {str(e)[:150]}")


async def finish_edit(update: Update, ctx, new_thumb):
    st = ctx.user_data.pop("edit")
    path = tmp("mp3")
    try:
        async with SEM:
            f = await ctx.bot.get_file(st["file_id"])
            await f.download_to_drive(path)
            thumb = new_thumb
            if not thumb and st.get("thumb_id"):
                tf = await ctx.bot.get_file(st["thumb_id"])
                thumb = make_thumb(bytes(await tf.download_as_bytearray()))
            with open(path, "rb") as fh:
                await ctx.bot.send_audio(
                    st["chat_id"], fh, title=st["title"], performer=st["performer"],
                    duration=st.get("duration"), thumbnail=thumb, reply_markup=kb(),
                    read_timeout=120, write_timeout=120)
    except Exception as e:
        log.exception("finish_edit xato")
        await ctx.bot.send_message(st["chat_id"], f"❌ Xatolik: {str(e)[:150]}")
    finally:
        cleanup(path)


# ----------------------------------------------------------------- inline qidiruv
async def on_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query
    text = q.query.strip()
    if len(text) < 2:
        return await q.answer([], cache_time=1)
    try:
        entries = await asyncio.to_thread(_search, text, 8)
        results = []
        for e in entries:
            dur = int(e.get("duration") or 0)
            if dur > 1200:
                continue
            title = e.get("title", "?")
            results.append(InlineQueryResultArticle(
                id=e["id"], title=title,
                description=f"{e.get('channel') or e.get('uploader') or ''} • {dur // 60}:{dur % 60:02d}",
                thumbnail_url=f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
                input_message_content=InputTextMessageContent(f"⏳ {title}\nYuklanmoqda..."),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Yuklanmoqda...", callback_data="noop")]]),
            ))
        await q.answer(results[:20], cache_time=300)
    except Exception:
        log.exception("inline xato")


async def on_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = update.chosen_inline_result
    imid = c.inline_message_id
    if not imid:
        return
    try:
        async with SEM:
            song = await fetch_song(f"https://www.youtube.com/watch?v={c.result_id}")
            chat = int(STORAGE_CHAT_ID) if STORAGE_CHAT_ID else c.from_user.id
            with open(song["path"], "rb") as f:
                sent = await ctx.bot.send_audio(
                    chat, f, title=song["title"], performer=song["performer"],
                    duration=song["duration"] or None, thumbnail=song.get("thumb"),
                    read_timeout=120, write_timeout=120)
            file_id = sent.audio.file_id
            ctx.bot_data.setdefault("inline_files", {})[imid] = {
                "file_id": file_id, "title": song["title"], "performer": song["performer"],
                "duration": song["duration"], "thumb_id": None}
            await ctx.bot.edit_message_media(
                inline_message_id=imid,
                media=InputMediaAudio(file_id, title=song["title"], performer=song["performer"]),
                reply_markup=kb())
            if not STORAGE_CHAT_ID:
                try:
                    await sent.delete()
                except Exception:
                    pass
    except Exception:
        log.exception("chosen_inline xato")
        try:
            await ctx.bot.edit_message_text("❌ Yuklab bo'lmadi", inline_message_id=imid)
        except Exception:
            pass
    finally:
        try:
            cleanup(song["path"])
        except NameError:
            pass


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Xato: %s", ctx.error, exc_info=ctx.error)


# ----------------------------------------------------------------- ishga tushirish
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN o'rnatilmagan")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(ChosenInlineResultHandler(on_chosen))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE, on_media))
    app.add_handler(MessageHandler(filters.AUDIO, on_audio))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    if BASE_URL:  # Render web service: webhook
        app.run_webhook(
            listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN,
            webhook_url=f"{BASE_URL}/{BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    else:  # lokal test
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
