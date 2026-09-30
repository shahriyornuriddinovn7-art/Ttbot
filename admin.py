import asyncio
import html
import json
import logging
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup as M, InlineQueryResultsButton, Update
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import db

log = logging.getLogger("admin")
_tasks: set = set()
_sub_cache: dict = {}

KINDS = {
    "search": "🔎 Nom bilan qidiruv", "link": "🔗 Havola", "instagram": "📸 Instagram",
    "shazam": "🎙 Shazam", "inline": "⌨️ Inline", "effect": "🎚 Effekt",
    "voice": "🎤 Ovozli xabar", "edit": "✏️ Tahrirlash",
}
INPUT_MODES = {"find", "bc", "subadd", "adminadd", "restore"}


def B(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def fmt(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.gmtime((ts or 0) + 5 * 3600)) if ts else "—"  # UTC+5


def esc(s):
    return html.escape(str(s or ""))


def uname(r):
    return f"{esc(r['name'])}" + (f" (@{esc(r['username'])})" if r["username"] else "")


BACK = [B("🔙 Orqaga", "adm:home")]


async def show(update: Update, text: str, kb=None):
    q = update.callback_query
    try:
        if q and q.message:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
        else:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                                      disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e):
            raise


# ------------------------------------------------------------- GATE (ban / texnik rejim / majburiy obuna)
async def channels() -> list:
    try:
        return json.loads(await db.get_setting("channels", "[]"))
    except Exception:
        return []


async def missing_channels(bot, uid):
    miss = []
    for c in await channels():
        key = (uid, c["id"])
        if time.time() - _sub_cache.get(key, 0) < 120:
            continue
        try:
            m = await bot.get_chat_member(c["id"], uid)
            ok = m.status in ("member", "administrator", "creator", "restricted")
        except Exception:
            ok = True  # tekshirib bo'lmasa, bloklamaymiz
        if ok:
            _sub_cache[key] = time.time()
        else:
            miss.append(c)
    return miss


async def deny(update: Update, text: str, kb=None):
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.inline_query:
        await update.inline_query.answer(
            [], cache_time=5, button=InlineQueryResultsButton(text=text[:60], start_parameter="start"))
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=kb)


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.is_bot:
        return
    banned = await db.touch(u)
    if update.chosen_inline_result or await db.is_admin(u.id):
        return
    if banned:
        await deny(update, "🚫 Siz botdan foydalanishdan bloklangansiz.")
        raise ApplicationHandlerStop
    if await db.get_setting("maintenance") == "1":
        await deny(update, "🛠 Bot vaqtincha texnik ishlar uchun to'xtatilgan. Keyinroq urinib ko'ring.")
        raise ApplicationHandlerStop
    miss = await missing_channels(ctx.bot, u.id)
    if miss:
        rows = [[InlineKeyboardButton(f"📢 {c['title']}", url=c["url"])] for c in miss]
        rows.append([B("✅ Tekshirish", "sub:check")])
        await deny(update, "📢 Botdan foydalanish uchun kanallarga obuna bo'ling.", M(rows))
        raise ApplicationHandlerStop


async def sub_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # bu yerga faqat obuna tekshiruvidan o'tganlar yetib keladi
    q = update.callback_query
    await q.answer("✅ Rahmat! Endi foydalanishingiz mumkin.", show_alert=True)
    try:
        await q.message.delete()
    except Exception:
        pass


# ------------------------------------------------------------- PANEL
async def panel_kb():
    maint = await db.get_setting("maintenance") == "1"
    return M([
        [B("📊 Statistika", "adm:stats"), B("👥 Foydalanuvchilar", "adm:users")],
        [B("📣 Xabar yuborish", "adm:bc"), B("📢 Majburiy obuna", "adm:subs")],
        [B(f"🛠 Texnik rejim: {'🔴 YOQIQ' if maint else '🟢 o‘chiq'}", "adm:maint"), B("👮 Adminlar", "adm:admins")],
        [B("💾 Baza nusxasi", "adm:backup"), B("♻️ Tiklash", "adm:restore")],
        [B("❌ Yopish", "adm:close")],
    ])


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await db.is_admin(update.effective_user.id):
        return
    ctx.user_data.pop("adm", None)
    await update.message.reply_text("⚙️ <b>Admin panel</b>", reply_markup=await panel_kb(), parse_mode="HTML")


async def text_stats():
    s = await db.stats()
    t = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{s['total']}</b>\n"
        f"🆕 Bugun: <b>{s['new_day']}</b> • 7 kun: <b>{s['new_week']}</b>\n"
        f"🔥 Faol (24 soat): <b>{s['active_day']}</b>\n"
        f"🚫 Banlangan: <b>{s['banned']}</b> • Botni bloklagan: <b>{s['blocked']}</b>\n\n"
        "<b>So'rovlar (jami / 24 soat):</b>\n"
    )
    total = 0
    for kind, tt, d in s["events"]:
        total += tt
        t += f"{KINDS.get(kind, kind)}: {tt} / {d}\n"
    return t + f"\n📦 Jami: <b>{total}</b>"


def user_text(r):
    return (
        f"👤 <b>{uname(r)}</b>\n🆔 <code>{r['id']}</code>\n"
        f"📅 Qo'shilgan: {fmt(r['joined'])}\n🕒 Oxirgi faollik: {fmt(r['last_seen'])}\n"
        f"🎵 So'rovlar: {r['downloads']}\n"
        f"Holat: {'🚫 Banlangan' if r['banned'] else '✅ Faol'}{' • 📵 bot bloklangan' if r['blocked'] else ''}"
    )


def user_kb(r):
    act = ("✅ Unban", f"adm:unban:{r['id']}") if r["banned"] else ("🚫 Ban", f"adm:ban:{r['id']}")
    return M([[B(*act)], BACK])


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await db.is_admin(uid):
        return await q.answer("⛔ Ruxsat yo'q", show_alert=True)
    parts = q.data.split(":")
    act = parts[1]
    is_owner = uid in db.OWNERS
    await q.answer()

    if act == "home":
        ctx.user_data.pop("adm", None)
        await show(update, "⚙️ <b>Admin panel</b>", await panel_kb())

    elif act == "close":
        ctx.user_data.pop("adm", None)
        await q.message.delete()

    elif act == "stats":
        await show(update, await text_stats(), M([[B("🔄 Yangilash", "adm:stats")], BACK]))

    elif act == "users":
        top, rec = await db.top_users(), await db.recent_users()
        t = "👥 <b>Foydalanuvchilar</b>\n\n<b>🏆 Eng faollar:</b>\n"
        t += "\n".join(f"{i}. {uname(r)} — {r['downloads']}" for i, r in enumerate(top, 1)) or "—"
        t += "\n\n<b>🆕 So'nggi qo'shilganlar:</b>\n"
        t += "\n".join(f"• {uname(r)} <code>{r['id']}</code>" for r in rec) or "—"
        await show(update, t, M([[B("🔍 Qidirish / Ban", "adm:find")], BACK]))

    elif act == "find":
        ctx.user_data["adm"] = {"mode": "find"}
        await show(update, "🔍 Foydalanuvchi ID yoki @username yuboring.\nBekor qilish: /cancel", M([BACK]))

    elif act in ("ban", "unban"):
        target = int(parts[2])
        if act == "ban" and await db.is_admin(target):
            return await q.answer("Adminni banlab bo'lmaydi", show_alert=True)
        await db.set_banned(target, act == "ban")
        r = await db.get_user(target)
        await show(update, user_text(r), user_kb(r))

    elif act == "bc":
        ctx.user_data["adm"] = {"mode": "bc"}
        await show(update, "📣 Yubormoqchi bo'lgan xabarni yuboring (matn, rasm, video, audio — istalgan).\n"
                           "Bekor qilish: /cancel", M([BACK]))

    elif act == "bcgo":
        st = ctx.user_data.pop("adm", None)
        if not st or st.get("mode") != "bc_confirm":
            return await q.answer("Sessiya tugagan", show_alert=True)
        await q.edit_message_text("⏳ Yuborish boshlandi...")
        t = asyncio.create_task(run_broadcast(ctx.bot, q.message.chat_id, st["chat"], st["msg"], q.message.message_id))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)

    elif act == "bccancel":
        ctx.user_data.pop("adm", None)
        await show(update, "❌ Bekor qilindi.", M([BACK]))

    elif act == "subs":
        ch = await channels()
        t = "📢 <b>Majburiy obuna</b>\n\n" + (
            "\n".join(f"{i + 1}. {esc(c['title'])}" for i, c in enumerate(ch)) or "Kanallar yo'q.") + \
            "\n\n⚠️ Bot kanalda <b>admin</b> bo'lishi shart."
        rows = [[B(f"🗑 {c['title'][:25]}", f"adm:subdel:{i}")] for i, c in enumerate(ch)]
        rows += [[B("➕ Kanal qo'shish", "adm:subadd")], BACK]
        await show(update, t, M(rows))

    elif act == "subadd":
        ctx.user_data["adm"] = {"mode": "subadd"}
        await show(update, "➕ Kanal @username yoki ID (-100...) yuboring.\nBekor qilish: /cancel", M([BACK]))

    elif act == "subdel":
        ch = await channels()
        i = int(parts[2])
        if 0 <= i < len(ch):
            ch.pop(i)
            await db.set_setting("channels", json.dumps(ch))
        q.data = "adm:subs"
        await on_cb(update, ctx)

    elif act == "maint":
        cur = await db.get_setting("maintenance") == "1"
        await db.set_setting("maintenance", "0" if cur else "1")
        await show(update, "⚙️ <b>Admin panel</b>", await panel_kb())

    elif act == "admins":
        ids = await db.list_admins()
        t = "👮 <b>Adminlar</b>\n\n👑 Egalari: " + (", ".join(f"<code>{i}</code>" for i in db.OWNERS) or "—")
        t += "\n🛡 Adminlar: " + (", ".join(f"<code>{i}</code>" for i in ids) or "—")
        rows = []
        if is_owner:
            rows = [[B(f"➖ {i}", f"adm:admindel:{i}")] for i in ids] + [[B("➕ Admin qo'shish", "adm:adminadd")]]
        await show(update, t, M(rows + [BACK]))

    elif act == "adminadd" and is_owner:
        ctx.user_data["adm"] = {"mode": "adminadd"}
        await show(update, "➕ Yangi admin Telegram ID sini yuboring.\nBekor qilish: /cancel", M([BACK]))

    elif act == "admindel" and is_owner:
        await db.del_admin(int(parts[2]))
        q.data = "adm:admins"
        await on_cb(update, ctx)

    elif act == "backup":
        await send_backup(ctx.bot, q.message.chat_id)

    elif act == "restore":
        if not is_owner:
            return await q.answer("Faqat egasi uchun", show_alert=True)
        ctx.user_data["adm"] = {"mode": "restore"}
        await show(update, "♻️ Oldin olingan <b>.db</b> zaxira faylini yuboring.\n"
                           "⚠️ Joriy baza almashtiriladi! Bekor qilish: /cancel", M([BACK]))


# ------------------------------------------------------------- matn/fayl kiritish (holatlar)
async def on_state(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = ctx.user_data.get("adm")
    if not st or st["mode"] not in INPUT_MODES:
        return
    msg, uid = update.effective_message, update.effective_user.id
    if not await db.is_admin(uid):
        ctx.user_data.pop("adm", None)
        return
    if msg.text and msg.text.startswith("/"):
        ctx.user_data.pop("adm", None)
        return
    mode = st["mode"]
    try:
        if mode == "find":
            r = await db.find_user(msg.text or "")
            ctx.user_data.pop("adm", None)
            if r:
                await msg.reply_text(user_text(r), reply_markup=user_kb(r), parse_mode="HTML")
            else:
                await msg.reply_text("❌ Topilmadi.", reply_markup=M([BACK]))

        elif mode == "bc":
            ctx.user_data["adm"] = {"mode": "bc_confirm", "chat": msg.chat_id, "msg": msg.message_id}
            await ctx.bot.copy_message(msg.chat_id, msg.chat_id, msg.message_id)
            n = len(await db.broadcast_ids())
            await msg.reply_text(f"👆 Shu xabar <b>{n}</b> ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
                                 parse_mode="HTML",
                                 reply_markup=M([[B("✅ Yuborish", "adm:bcgo"), B("❌ Bekor", "adm:bccancel")]]))

        elif mode == "subadd":
            target = (msg.text or "").strip()
            chat = await ctx.bot.get_chat(int(target) if target.lstrip("-").isdigit() else target)
            me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
            if me.status not in ("administrator", "creator"):
                return await msg.reply_text("❌ Avval botni kanalga admin qiling, keyin qayta yuboring.")
            url = f"https://t.me/{chat.username}" if chat.username else (
                chat.invite_link or await ctx.bot.export_chat_invite_link(chat.id))
            ch = await channels()
            ch = [c for c in ch if c["id"] != chat.id] + [{"id": chat.id, "title": chat.title or target, "url": url}]
            await db.set_setting("channels", json.dumps(ch))
            ctx.user_data.pop("adm", None)
            await msg.reply_text(f"✅ Qo'shildi: {esc(chat.title)}", parse_mode="HTML",
                                 reply_markup=M([[B("📢 Kanallar", "adm:subs")], BACK]))

        elif mode == "adminadd":
            if uid not in db.OWNERS:
                return
            if not (msg.text or "").strip().isdigit():
                return await msg.reply_text("ID raqam bo'lishi kerak.")
            await db.add_admin(int(msg.text.strip()))
            ctx.user_data.pop("adm", None)
            await msg.reply_text("✅ Admin qo'shildi.", reply_markup=M([[B("👮 Adminlar", "adm:admins")], BACK]))

        elif mode == "restore":
            if uid not in db.OWNERS:
                return
            if not msg.document:
                return await msg.reply_text("Iltimos, .db faylini yuboring.")
            path = f"/tmp/temp_restore_{uid}.db"
            try:
                f = await ctx.bot.get_file(msg.document.file_id)
                await f.download_to_drive(path)
                ok = await db.restore(path)
            finally:
                if os.path.exists(path):
                    os.remove(path)
            ctx.user_data.pop("adm", None)
            await msg.reply_text("✅ Baza tiklandi." if ok else "❌ Bu yaroqli zaxira fayl emas.",
                                 reply_markup=M([BACK]))
    except Exception as e:
        log.exception("admin state xato")
        await msg.reply_text(f"❌ Xatolik: {esc(str(e)[:150])}")
    raise ApplicationHandlerStop


# ------------------------------------------------------------- broadcast va zaxira
async def run_broadcast(bot, admin_chat, src_chat, src_msg, status_id):
    ids = await db.broadcast_ids()
    ok = fail = 0
    t0 = time.time()
    for i, uid in enumerate(ids, 1):
        for attempt in range(2):
            try:
                await bot.copy_message(uid, src_chat, src_msg)
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                await db.set_blocked(uid, True)
                fail += 1
                break
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
        if i % 50 == 0:
            try:
                await bot.edit_message_text(f"⏳ {i}/{len(ids)} • ✅ {ok} • ❌ {fail}", admin_chat, status_id)
            except Exception:
                pass
    try:
        await bot.edit_message_text(
            f"✅ <b>Yuborish tugadi</b>\n\nJami: {len(ids)}\nYetkazildi: {ok}\nXato/bloklagan: {fail}\n"
            f"Vaqt: {int(time.time() - t0)} s", admin_chat, status_id, parse_mode="HTML",
            reply_markup=M([BACK]))
    except Exception:
        pass


async def send_backup(bot, chat_id):
    path = await db.snapshot()
    try:
        with open(path, "rb") as f:
            await bot.send_document(chat_id, f, filename=f"backup_{time.strftime('%Y%m%d_%H%M')}.db",
                                    caption="💾 Baza nusxasi. Tiklash: Admin panel → ♻️ Tiklash")
    finally:
        if os.path.exists(path):
            os.remove(path)


async def backup_job(ctx: ContextTypes.DEFAULT_TYPE):
    for oid in db.OWNERS:
        try:
            await send_backup(ctx.bot, oid)
        except Exception as e:
            log.warning("avto-zaxira xato: %s", e)


# ------------------------------------------------------------- ro'yxatdan o'tkazish
def register(app):
    app.add_handler(TypeHandler(Update, gate), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.StatusUpdate.ALL, on_state), group=-1)
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(sub_check, pattern=r"^sub:check$"))
    if app.job_queue and db.OWNERS:
        app.job_queue.run_repeating(backup_job, interval=86400, first=600)  # har 24 soatda zaxira
