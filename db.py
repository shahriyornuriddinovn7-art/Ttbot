import asyncio
import os
import shutil
import sqlite3
import time
import uuid

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "bot.db")
OWNERS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, username TEXT,
    joined INTEGER, last_seen INTEGER,
    banned INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0, downloads INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, user_id INTEGER, kind TEXT);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
"""

_db: aiosqlite.Connection | None = None
_lock = asyncio.Lock()


async def init():
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    await _db.commit()


async def close():
    global _db
    if _db:
        await _db.close()
        _db = None


async def _q(sql, args=(), one=False):
    async with _lock:
        cur = await _db.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
    if one:
        return rows[0] if rows else None
    return rows


async def _x(sql, args=()):
    async with _lock:
        await _db.execute(sql, args)
        await _db.commit()


# ---- foydalanuvchilar
async def touch(u) -> bool:
    """Foydalanuvchini yozadi/yangilaydi. Ban bo'lsa True qaytaradi."""
    now = int(time.time())
    name = (u.full_name or "")[:100]
    await _x(
        "INSERT INTO users(id,name,username,joined,last_seen) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, "
        "last_seen=excluded.last_seen, blocked=0",
        (u.id, name, (u.username or "").lower(), now, now),
    )
    row = await _q("SELECT banned FROM users WHERE id=?", (u.id,), one=True)
    return bool(row and row["banned"])


async def find_user(text: str):
    text = text.strip().lstrip("@").lower()
    if text.isdigit():
        return await _q("SELECT * FROM users WHERE id=?", (int(text),), one=True)
    return await _q("SELECT * FROM users WHERE username=?", (text,), one=True)


async def get_user(uid: int):
    return await _q("SELECT * FROM users WHERE id=?", (uid,), one=True)


async def set_banned(uid: int, flag: bool):
    await _x("UPDATE users SET banned=? WHERE id=?", (int(flag), uid))


async def set_blocked(uid: int, flag: bool):
    await _x("UPDATE users SET blocked=? WHERE id=?", (int(flag), uid))


async def broadcast_ids():
    rows = await _q("SELECT id FROM users WHERE banned=0 AND blocked=0")
    return [r["id"] for r in rows]


async def top_users(n=10):
    return await _q("SELECT * FROM users WHERE downloads>0 ORDER BY downloads DESC LIMIT ?", (n,))


async def recent_users(n=10):
    return await _q("SELECT * FROM users ORDER BY joined DESC LIMIT ?", (n,))


# ---- hodisalar / statistika
async def log_event(uid: int, kind: str):
    try:
        await _x("INSERT INTO events(ts,user_id,kind) VALUES(?,?,?)", (int(time.time()), uid, kind))
        await _x("UPDATE users SET downloads=downloads+1 WHERE id=?", (uid,))
    except Exception:
        pass


async def stats() -> dict:
    now = int(time.time())
    day, week = now - 86400, now - 7 * 86400
    r = await _q(
        "SELECT COUNT(*) t, SUM(joined>=?) nd, SUM(joined>=?) nw, SUM(last_seen>=?) a, "
        "SUM(banned) b, SUM(blocked) bl FROM users", (day, week, day), one=True)
    ev = await _q("SELECT kind, COUNT(*) t, SUM(ts>=?) d FROM events GROUP BY kind ORDER BY t DESC", (day,))
    return {
        "total": r["t"] or 0, "new_day": r["nd"] or 0, "new_week": r["nw"] or 0,
        "active_day": r["a"] or 0, "banned": r["b"] or 0, "blocked": r["bl"] or 0,
        "events": [(e["kind"], e["t"], e["d"] or 0) for e in ev],
    }


# ---- sozlamalar
async def get_setting(key: str, default=None):
    r = await _q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default


async def set_setting(key: str, value: str):
    await _x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
             (key, value))


# ---- adminlar
async def is_admin(uid: int) -> bool:
    if uid in OWNERS:
        return True
    return await _q("SELECT 1 FROM admins WHERE id=?", (uid,), one=True) is not None


async def list_admins():
    return [r["id"] for r in await _q("SELECT id FROM admins")]


async def add_admin(uid: int):
    await _x("INSERT OR IGNORE INTO admins(id) VALUES(?)", (uid,))


async def del_admin(uid: int):
    await _x("DELETE FROM admins WHERE id=?", (uid,))


# ---- zaxira / tiklash
async def snapshot() -> str:
    path = f"/tmp/temp_{uuid.uuid4().hex}.db"
    async with _lock:
        await _db.commit()
        await asyncio.to_thread(shutil.copy, DB_PATH, path)
    return path


def _valid_db(path: str) -> bool:
    try:
        con = sqlite3.connect(path)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        return {"users", "events", "settings"} <= names
    except Exception:
        return False


async def restore(path: str) -> bool:
    if not await asyncio.to_thread(_valid_db, path):
        return False
    await close()
    await asyncio.to_thread(shutil.copy, path, DB_PATH)
    await init()
    return True
