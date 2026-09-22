import asyncio
import os
import base64
import json
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated, InlineQuery,
    InlineQueryResultArticle, InputTextMessageContent,
    InlineKeyboardButton, InlineKeyboardMarkup
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()

DB_PATH = os.getenv("DB_PATH", "bot.db")
DEMO_MINUTES = max(1, int(os.getenv("DEMO_MINUTES", "5")))
DEFAULT_PRICE = max(1, int(os.getenv("DEFAULT_PRICE", "299")))  # rupees
FORCE_JOIN_CHANNEL_ID = os.getenv("FORCE_JOIN_CHANNEL_ID", "").strip()
FORCE_JOIN_LINK = os.getenv("FORCE_JOIN_LINK", "https://t.me/+Cr5OKRJHfHwwYjNl").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "raj_bro").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing")
if not RAZORPAY_KEY_ID:
    raise RuntimeError("RAZORPAY_KEY_ID is missing")
if not RAZORPAY_KEY_SECRET:
    raise RuntimeError("RAZORPAY_KEY_SECRET is missing")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

BOT_USERNAME = ""
manual_price_mode = set()
all_price_mode = set()
processing_payments = set()


# ============================================================
# HELPERS
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def money(paise):
    return f"₹{int(paise) // 100}"

def clean_html(value):
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def owner_only(user_id):
    return user_id == ADMIN_ID


# ============================================================
# DATABASE
# ============================================================

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels(
                channel_id INTEGER PRIMARY KEY,
                channel_name TEXT NOT NULL,
                username TEXT,
                added_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS courses(
                channel_id INTEGER PRIMARY KEY,
                course_name TEXT NOT NULL,
                price INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                demo_count INTEGER DEFAULT 0,
                purchase_count INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS demos(
                user_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                invite_link TEXT,
                expires_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                course_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                link_id TEXT UNIQUE NOT NULL,
                reference_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS access_grants(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                course_name TEXT NOT NULL,
                invite_link TEXT,
                status TEXT DEFAULT 'active',
                granted_at TEXT NOT NULL,
                revoked_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('demo_minutes',?)",
            (str(DEMO_MINUTES),)
        )
        await db.commit()


async def get_demo_minutes():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key='demo_minutes'")
        row = await cur.fetchone()
        try:
            return max(1, int(row[0])) if row else DEMO_MINUTES
        except Exception:
            return DEMO_MINUTES


async def set_demo_minutes(value):
    value = max(1, min(1440, int(value)))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('demo_minutes',?)",
            (str(value),)
        )
        await db.commit()


async def upsert_user(user):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(user_id,username,full_name,first_seen,last_seen)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                last_seen=excluded.last_seen
        """, (
            user.id, user.username or "", user.full_name or "",
            now_iso(), now_iso()
        ))
        await db.commit()


async def add_channel(chat):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM channels WHERE channel_id=?", (chat.id,))
        existed = await cur.fetchone() is not None
        await db.execute("""
            INSERT OR REPLACE INTO channels(channel_id,channel_name,username,added_at)
            VALUES(?,?,?,?)
        """, (chat.id, chat.title or "Unnamed", chat.username, now_iso()))
        await db.execute("""
            INSERT OR IGNORE INTO courses(channel_id,course_name,price)
            VALUES(?,?,?)
        """, (chat.id, chat.title or "Unnamed", DEFAULT_PRICE * 100))
        await db.execute("""
            UPDATE courses SET course_name=? WHERE channel_id=?
        """, (chat.title or "Unnamed", chat.id))
        await db.commit()
        return not existed


async def remove_channel(channel_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
        await db.execute("DELETE FROM courses WHERE channel_id=?", (channel_id,))
        await db.commit()


async def get_courses():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT c.channel_id,c.channel_name,c.username,
                   co.course_name,co.price
            FROM channels c
            JOIN courses co ON co.channel_id=c.channel_id
            ORDER BY c.rowid DESC
        """)
        return await cur.fetchall()


async def get_course(channel_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT c.channel_id,c.channel_name,c.username,
                   co.course_name,co.price
            FROM channels c
            JOIN courses co ON co.channel_id=c.channel_id
            WHERE c.channel_id=?
        """, (channel_id,))
        return await cur.fetchone()


async def set_course_price(channel_id, rupees):
    rupees = max(1, int(rupees))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE courses SET price=? WHERE channel_id=?",
            (rupees * 100, channel_id)
        )
        await db.commit()


async def set_all_course_prices(rupees):
    rupees = max(1, int(rupees))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM courses")
        count = (await cur.fetchone())[0]
        await db.execute("UPDATE courses SET price=?", (rupees * 100,))
        await db.commit()
        return count


async def change_all_prices(delta_rupees):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT channel_id,price FROM courses")
        rows = await cur.fetchall()
        changed = 0
        for channel_id, price in rows:
            new_price = max(100, int(price) + int(delta_rupees) * 100)
            await db.execute(
                "UPDATE courses SET price=? WHERE channel_id=?",
                (new_price, channel_id)
            )
            changed += 1
        await db.commit()
        return changed


async def change_course_price(channel_id, delta_rupees):
    course = await get_course(channel_id)
    if not course:
        return None
    new_price = max(100, course[4] + int(delta_rupees) * 100)
    await set_course_price(channel_id, new_price // 100)
    return new_price


# ============================================================
# OWNER REPORTS / MEMBERS
# ============================================================

async def stats():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM payments WHERE status='paid'")
        purchases = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'")
        revenue = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM channels")
        channels = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM access_grants WHERE status='active'")
        members = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM demos")
        demos = (await cur.fetchone())[0]
        return users, purchases, revenue, channels, members, demos


async def get_members(limit=30):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,channel_id,course_name,invite_link,
                   status,granted_at,revoked_at
            FROM access_grants
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        return await cur.fetchall()


async def get_member(grant_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,channel_id,course_name,invite_link,
                   status,granted_at,revoked_at
            FROM access_grants WHERE id=?
        """, (grant_id,))
        return await cur.fetchone()


async def mark_member(grant_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE access_grants
            SET status=?,revoked_at=?
            WHERE id=?
        """, (status, now_iso() if status == "revoked" else None, grant_id))
        await db.commit()


async def add_grant(user_id, channel_id, course_name, invite_link):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO access_grants(
                user_id,channel_id,course_name,invite_link,status,granted_at
            ) VALUES(?,?,?,?,?,?)
        """, (user_id, channel_id, course_name, invite_link, "active", now_iso()))
        await db.commit()


async def owner_notify(text):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        print("OWNER NOTIFY:", e)


# ============================================================
# RAZORPAY
# ============================================================

def razorpay_sync(method, endpoint, payload=None):
    """Call Razorpay API and return JSON, with readable API errors."""
    url = "https://api.razorpay.com/v1" + endpoint

    raw_auth = f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()
    auth = base64.b64encode(raw_auth).decode()

    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Telegram-Course-Bot/2.0"
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url=url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            data = json.loads(raw)
            msg = data.get("error", {}).get("description") or data.get("error", {}).get("reason") or raw
        except Exception:
            msg = raw
        raise RuntimeError(f"Razorpay HTTP {exc.code}: {msg}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Razorpay network error: {exc.reason}")
    except Exception as exc:
        raise RuntimeError(str(exc))


async def razorpay(method, endpoint, payload=None):
    return await asyncio.to_thread(razorpay_sync, method, endpoint, payload)


# ============================================================
# PUBLIC START / INLINE
# ============================================================
# ============================================================
# OPTIONAL FORCE JOIN / USER MENU
# ============================================================

async def is_force_joined(user_id):
    if not FORCE_JOIN_CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(int(FORCE_JOIN_CHANNEL_ID), user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        print("FORCE JOIN CHECK:", e)
        return False


def join_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Official Channel Join करें", url=FORCE_JOIN_LINK)]
    ])


def user_home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 चैनल खोजें / Channel Search", switch_inline_query_current_chat="")],
        [InlineKeyboardButton(text="📖 कैसे खरीदें? / How to Buy", callback_data="how_to_buy")],
        [InlineKeyboardButton(text="🧾 खरीदारी इतिहास / Purchase History", callback_data="purchase_history")],
        [InlineKeyboardButton(text="👨‍💻 एडमिन संपर्क / Admin Contact", url=f"https://t.me/{ADMIN_USERNAME}")]
    ])


async def send_join_screen(message):
    await message.answer(
        "👋 <b>Welcome!</b>\n\n"
        "🎓 <b>Course Purchase Bot</b>\n\n"
        "आगे बढ़ने के लिए पहले Official Channel Join करें।\n"
        "<b>Channel Join होते ही आपका Course Menu automatically open हो जाएगा.</b>\n\n"
        "👇 नीचे दिए गए button से Channel Join करें।",
        reply_markup=join_kb(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "verify_join")
async def verify_join(callback: CallbackQuery):
    # Backward compatibility for old messages. New join screen has no Verify button.
    if await is_force_joined(callback.from_user.id):
        await callback.answer("✅ Verified", show_alert=True)
        await callback.message.edit_text(
            "🎓 <b>Course Purchase Bot</b>\n\n"
            "अब आप Course Search, Demo और Purchase कर सकते हैं।",
            reply_markup=user_home_kb(),
            parse_mode="HTML"
        )
    else:
        await callback.answer("❌ पहले Official Channel Join करें।", show_alert=True)


@router.callback_query(F.data == "how_to_buy")
async def how_to_buy(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "📖 <b>कैसे खरीदें? / How to Buy?</b>\n\n"
        "1️⃣ चैनल खोजें / Channel Search दबाएँ।\n"
        "2️⃣ अपना Course चुनें।\n"
        "3️⃣ 🎬 Demo देखें।\n"
        "4️⃣ 🛒 Buy Now / Purchase पर क्लिक करें।\n"
        "5️⃣ Razorpay Payment पूरा करें।\n"
        "6️⃣ Payment successful होने के बाद Premium Access मिलेगा।",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 वापस / Back", callback_data="user_home")]]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "user_home")
async def user_home(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "🎓 <b>Course Purchase Bot</b>\n\n👇 Choose an option / विकल्प चुनें:",
        reply_markup=user_home_kb(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "purchase_history")
async def purchase_history(callback: CallbackQuery):
    await callback.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT course_name,amount,status,created_at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (callback.from_user.id,)
        )
        rows = await cur.fetchall()
    if not rows:
        text = "🧾 <b>खरीदारी इतिहास / Purchase History</b>\n\nअभी कोई purchase नहीं है।"
    else:
        lines = ["🧾 <b>खरीदारी इतिहास / Purchase History</b>", ""]
        for name, amount, status, created in rows:
            icon = "✅" if status == "paid" else "⏳"
            lines.append(f"{icon} <b>{clean_html(name)}</b> — {money(amount)} — {status}")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 वापस / Back", callback_data="user_home")]]), parse_mode="HTML")



@router.message(Command("start"))
async def start(message: Message):
    await upsert_user(message.from_user)

    if not await is_force_joined(message.from_user.id):
        await send_join_screen(message)
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("course_"):
        try:
            channel_id = int(parts[1].split("_", 1)[1])
        except ValueError:
            channel_id = 0
        course = await get_course(channel_id)
        if course:
            demo = await get_demo_minutes()
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🎬 डेमो देखें / Watch Demo ({demo} Min)", callback_data=f"demo:{channel_id}")],
                [InlineKeyboardButton(text=f"🛒 कोर्स खरीदें / Purchase {money(course[4])}", callback_data=f"buy:{channel_id}")]
            ])
            await message.answer(
                f"📚 <b>{clean_html(course[3])}</b>\n\n"
                f"💰 Price / कीमत: <b>{money(course[4])}</b>\n"
                f"🎁 Demo: <b>{demo} मिनट</b>",
                reply_markup=kb, parse_mode="HTML"
            )
            return

    await message.answer(
        "🎓 <b>Welcome / स्वागत है!</b>\n\n"
        "Course/Channel खोजें, Demo देखें और आसानी से Purchase करें।",
        reply_markup=user_home_kb(),
        parse_mode="HTML"
    )


@router.inline_query()
async def inline_search(query: InlineQuery):
    await upsert_user(query.from_user)
    if not await is_force_joined(query.from_user.id):
        await query.answer([], cache_time=0, is_personal=True, switch_pm_text="📢 पहले Channel Join करें / Join Channel", switch_pm_parameter="join")
        return
    q = (query.query or "").strip().lower()
    courses = await get_courses()
    if q:
        courses = [
            x for x in courses
            if q in (x[3] or "").lower() or q in (x[1] or "").lower()
        ]

    results = []
    for channel_id, channel_name, username, course_name, price in courses[:50]:
        link = f"https://t.me/{BOT_USERNAME}?start=course_{channel_id}"
        results.append(
            InlineQueryResultArticle(
                id=f"course_{channel_id}",
                title=course_name,
                description=f"{money(price)} • Demo available",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        f"📚 <b>{clean_html(course_name)}</b>\n"
                        f"💰 <b>{money(price)}</b>\n\n"
                        f"🎁 Demo / Buy: {link}"
                    ),
                    parse_mode="HTML"
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🎁 Demo / Buy", url=link)
                ]])
            )
        )

    await query.answer(results, cache_time=1, is_personal=True)


# ============================================================
# AUTO CHANNEL DETECT
# ============================================================

async def detect_channel(chat):
    if chat.type != "channel":
        return
    new = await add_channel(chat)
    if new:
        await owner_notify(
            "✅ <b>CHANNEL AUTO ADDED</b>\n\n"
            f"📚 <b>{clean_html(chat.title)}</b>\n"
            f"📌 <code>{chat.id}</code>\n"
            "🎁 Demo: ON\n"
            "💳 Payment: ON"
        )


@router.my_chat_member()
async def my_chat_member_update(event: ChatMemberUpdated):
    if event.chat.type != "channel":
        return
    status = event.new_chat_member.status
    if status == "administrator":
        await detect_channel(event.chat)
    elif status in ("left", "kicked"):
        await remove_channel(event.chat.id)


@router.channel_post()
async def channel_post(message: Message):
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(message.chat.id, me.id)
        if member.status == "administrator":
            await detect_channel(message.chat)
    except Exception as e:
        print("CHANNEL POST DETECT:", e)


# ============================================================
# DEMO
# ============================================================

@router.callback_query(F.data.startswith("demo:"))
async def demo(callback: CallbackQuery):
    await upsert_user(callback.from_user)
    channel_id = int(callback.data.split(":", 1)[1])
    course = await get_course(channel_id)

    if not course:
        await callback.answer("❌ Course नहीं मिला", show_alert=True)
        return

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(channel_id, me.id)
        if member.status != "administrator":
            raise RuntimeError("Bot is not administrator")
    except Exception as e:
        print("DEMO ADMIN:", e)
        await callback.answer(
            "❌ Bot को channel में administrator बनाओ और Invite Users + Ban Users permission दो।",
            show_alert=True
        )
        return

    demo_minutes = await get_demo_minutes()
    expires = datetime.now(timezone.utc) + timedelta(minutes=demo_minutes)

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1,
            name=f"Demo-{callback.from_user.id}"
        )
    except Exception as e:
        print("DEMO LINK:", e)
        await callback.answer("❌ Demo link generate नहीं हुआ।", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO demos(user_id,channel_id,invite_link,expires_at)
            VALUES(?,?,?,?)
        """, (callback.from_user.id, channel_id, invite.invite_link, expires.isoformat()))
        await db.execute(
            "UPDATE users SET demo_count=demo_count+1 WHERE user_id=?",
            (callback.from_user.id,)
        )
        await db.commit()

    await callback.message.answer(
        f"🎁 <b>DEMO READY</b>\n\n"
        f"📚 {clean_html(course[3])}\n"
        f"⏱ {demo_minutes} मिनट\n\n"
        f"🔗 <a href=\"{invite.invite_link}\">👉 JOIN DEMO CHANNEL</a>\n\n"
        "⚠️ Demo खत्म होने पर access automatically remove होगा।",
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================================
# BUY
# ============================================================

@router.callback_query(F.data.startswith("buy:"))
async def buy(callback: CallbackQuery):
    # Answer immediately so Telegram never leaves the button in a spinner/frozen state.
    await callback.answer()

    await upsert_user(callback.from_user)
    try:
        channel_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.message.answer("❌ Invalid course.")
        return

    course = await get_course(channel_id)
    if not course:
        await callback.message.answer("❌ Course नहीं मिला।")
        return

    user = callback.from_user
    amount = int(course[4])
    if amount < 100:
        await callback.message.answer("❌ Course price ₹1 से कम नहीं हो सकती। Owner Panel में price ठीक करें।")
        return

    # Razorpay reference_id must be unique. UUID avoids duplicate clicks in the same second.
    reference = f"TG{user.id}_{uuid.uuid4().hex[:18]}"[:40]

    payload = {
        "amount": amount,
        "currency": "INR",
        "accept_partial": False,
        "description": f"{str(course[3])[:240]} Telegram Access",
        "reference_id": reference,
        "reminder_enable": False,
        "notes": {
            "telegram_user_id": str(user.id),
            "channel_id": str(channel_id)
        }
    }

    try:
        payment_link = await razorpay("POST", "/payment_links", payload)
    except Exception as e:
        print("RAZORPAY CREATE ERROR:", e)
        await owner_notify(
            "⚠️ <b>RAZORPAY PAYMENT LINK ERROR</b>\n\n"
            f"👤 User: <code>{user.id}</code>\n"
            f"📚 Course: <b>{clean_html(course[3])}</b>\n"
            f"💰 Amount: <b>{money(amount)}</b>\n"
            f"❌ <code>{clean_html(str(e)[:2500])}</code>"
        )
        await callback.message.answer(
            "❌ Payment link नहीं बन पाया।\n\n"
            "Owner को exact Razorpay error भेज दिया गया है।"
        )
        return

    link_id = payment_link.get("id")
    short_url = payment_link.get("short_url")
    if not link_id or not short_url:
        await owner_notify(
            "⚠️ <b>RAZORPAY INVALID RESPONSE</b>\n\n"
            f"<code>{clean_html(json.dumps(payment_link)[:3000])}</code>"
        )
        await callback.message.answer("❌ Razorpay ने valid payment link नहीं दिया।")
        return

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO payments(
                    user_id,channel_id,course_name,amount,link_id,reference_id,status,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
            """, (
                user.id, channel_id, course[3], amount,
                link_id, reference, "created", now_iso()
            ))
            await db.commit()
    except Exception as e:
        print("PAYMENT DB ERROR:", e)
        await callback.message.answer("❌ Payment record save नहीं हो पाया।")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 PAY {money(amount)} / भुगतान करें", url=short_url)],
        [InlineKeyboardButton(text="🔄 Payment Check / भुगतान जाँचें", callback_data=f"check:{link_id}")]
    ])

    await callback.message.answer(
        "💳 <b>PAYMENT / भुगतान</b>\n\n"
        f"📚 <b>{clean_html(course[3])}</b>\n"
        f"💰 Amount: <b>{money(amount)}</b>\n\n"
        "नीचे <b>PAY</b> button दबाकर Razorpay payment पूरा करें।\n"
        "Payment के बाद <b>Payment Check</b> दबाएँ।",
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def process_payment(link_id):
    if link_id in processing_payments:
        return False

    processing_payments.add(link_id)
    try:
        try:
            pl = await razorpay("GET", f"/payment_links/{link_id}")
        except Exception as e:
            print("PAYMENT VERIFY:", e)
            return False

        if pl.get("status") != "paid":
            return False

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT id,user_id,channel_id,course_name,amount,status
                FROM payments WHERE link_id=?
            """, (link_id,))
            row = await cur.fetchone()
            if not row:
                print("PAYMENT DB RECORD NOT FOUND:", link_id)
                return False

            db_id, user_id, channel_id, course_name, amount, status = row
            if status == "paid":
                return True

            await db.execute(
                "UPDATE payments SET status='paid',paid_at=? WHERE id=? AND status='created'",
                (now_iso(), db_id)
            )
            await db.execute(
                "UPDATE users SET purchase_count=purchase_count+1 WHERE user_id=?",
                (user_id,)
            )
            await db.commit()

        try:
            invite = await bot.create_chat_invite_link(
                chat_id=channel_id,
                member_limit=1,
                expire_date=datetime.now(timezone.utc) + timedelta(minutes=10),
                name=f"Paid-{user_id}"
            )
        except Exception as e:
            await owner_notify(
                "⚠️ <b>PAYMENT SUCCESS / INVITE ERROR</b>\n\n"
                f"👤 <code>{user_id}</code>\n"
                f"📚 {clean_html(course_name)}\n"
                f"💰 {money(amount)}\n"
                f"❌ <code>{clean_html(str(e)[:1500])}</code>"
            )
            return False

        await add_grant(user_id, channel_id, course_name, invite.invite_link)

        try:
            await bot.send_message(
                user_id,
                "🎉 <b>PAYMENT SUCCESSFUL</b>\n\n"
                f"📚 {clean_html(course_name)}\n"
                f"💰 Paid: <b>{money(amount)}</b>\n\n"
                f"🔗 <a href=\"{invite.invite_link}\">👉 JOIN PREMIUM CHANNEL</a>",
                parse_mode="HTML"
            )
        except Exception as e:
            print("BUYER MESSAGE:", e)

        await owner_notify(
            "🎉 <b>PURCHASE SUCCESSFUL</b>\n\n"
            f"👤 User: <code>{user_id}</code>\n"
            f"📚 <b>{clean_html(course_name)}</b>\n"
            f"💰 <b>{money(amount)}</b>\n"
            f"💳 <code>{link_id}</code>"
        )
        return True
    finally:
        processing_payments.discard(link_id)


@router.callback_query(F.data.startswith("check:"))
async def check_payment(callback: CallbackQuery):
    # Immediately stop Telegram's loading spinner.
    await callback.answer("⏳ Payment check हो रहा है…")
    link_id = callback.data.split(":", 1)[1]
    ok = await process_payment(link_id)
    await callback.message.answer(
        "✅ Payment verified! Access link भेज दिया गया है।" if ok
        else "⏳ Payment अभी verified नहीं हुआ। Payment complete होने के बाद फिर Check करें।"
    )


async def payment_checker():
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT link_id FROM payments WHERE status='created' ORDER BY id DESC LIMIT 100"
                )
                rows = await cur.fetchall()
            for (link_id,) in rows:
                await process_payment(link_id)
                await asyncio.sleep(0.1)
        except Exception as e:
            print("PAYMENT CHECKER:", e)
        await asyncio.sleep(15)


# ============================================================
# DEMO CLEANER
# ============================================================

async def demo_cleaner():
    while True:
        try:
            now = datetime.now(timezone.utc)
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT user_id,channel_id,invite_link,expires_at FROM demos"
                )
                rows = await cur.fetchall()

            for user_id, channel_id, invite_link, expires_text in rows:
                try:
                    expires = datetime.fromisoformat(expires_text)
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                except Exception:
                    expires = now

                if now < expires:
                    continue

                try:
                    await bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
                    await bot.unban_chat_member(
                        chat_id=channel_id,
                        user_id=user_id,
                        only_if_banned=True
                    )
                except Exception as e:
                    print("DEMO REMOVE:", e)

                if invite_link:
                    try:
                        await bot.revoke_chat_invite_link(
                            chat_id=channel_id,
                            invite_link=invite_link
                        )
                    except Exception:
                        pass

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "DELETE FROM demos WHERE user_id=?",
                        (user_id,)
                    )
                    await db.commit()

        except Exception as e:
            print("DEMO CLEANER:", e)

        await asyncio.sleep(10)


# ============================================================
# MEMBER JOIN -> REVOKE USED INVITE
# ============================================================

@router.chat_member()
async def member_join(event: ChatMemberUpdated):
    if event.chat.type != "channel":
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    joined = (
        new_status in ("member", "administrator")
        and old_status in ("left", "kicked")
    )

    if not joined:
        return

    invite = getattr(event, "invite_link", None)
    if invite and invite.invite_link:
        try:
            await bot.revoke_chat_invite_link(
                chat_id=event.chat.id,
                invite_link=invite.invite_link
            )
        except Exception as e:
            print("REVOKE USED INVITE:", e)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM demos WHERE user_id=? AND channel_id=?",
                (event.new_chat_member.user.id, event.chat.id)
            )
            await db.commit()


# ============================================================
# FORCE JOIN -> AUTO OPEN USER INTERFACE
# ============================================================

@router.chat_member()
async def force_join_auto_open(event: ChatMemberUpdated):
    if not FORCE_JOIN_CHANNEL_ID:
        return

    try:
        force_channel_id = int(FORCE_JOIN_CHANNEL_ID)
    except ValueError:
        return

    if event.chat.id != force_channel_id:
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    joined = (
        new_status in ("member", "administrator", "creator")
        and old_status in ("left", "kicked", "restricted")
    )

    if not joined:
        return

    user = event.new_chat_member.user
    if not user:
        return

    try:
        await upsert_user(user)
        await bot.send_message(
            user.id,
            "🎉 <b>Channel Join Verified!</b>\n\n"
            "अब आपका Course Menu automatically open हो गया है।\n\n"
            "🔎 <b>Channel Search</b> से अपना course चुनें।",
            reply_markup=user_home_kb(),
            parse_mode="HTML"
        )
        print(f"FORCE JOIN AUTO OPEN: user={user.id}")
    except Exception as exc:
        print("FORCE JOIN AUTO OPEN ERROR:", exc)


# ============================================================
# OWNER PANEL
# ============================================================

def owner_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Users Data", callback_data="own:users"),
            InlineKeyboardButton(text="⏱ Demo Time", callback_data="own:demo")
        ],
        [
            InlineKeyboardButton(text="💰 Course Prices", callback_data="own:prices"),
            InlineKeyboardButton(text="📊 My Reports", callback_data="own:reports")
        ],
        [
            InlineKeyboardButton(text="👤 Manage Members", callback_data="own:members")
        ]
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Owner Panel", callback_data="own:home")]
    ])


def prices_kb(courses, page=0):
    # Keep the keyboard small so Telegram edit/send never hangs with many channels.
    per_page = 8
    total_pages = max(1, (len(courses) + per_page - 1) // per_page)
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page
    visible = courses[start:start + per_page]

    rows = [
        [InlineKeyboardButton(
            text="🌐 सभी चैनल — एक ही Price / Set Same Price",
            callback_data="prices:all"
        )],
        [
            InlineKeyboardButton(text="📈 सभी +₹50 / All +₹50", callback_data="prices:all_delta:50"),
            InlineKeyboardButton(text="📉 सभी -₹50 / All -₹50", callback_data="prices:all_delta:-50")
        ],
        [InlineKeyboardButton(
            text="✏️ Manual Price / मैनुअल Price",
            callback_data="prices:manual"
        )]
    ]

    for channel_id, _, _, course_name, price in visible:
        label = str(course_name or "Course")
        if len(label) > 35:
            label = label[:32] + "..."
        rows.append([InlineKeyboardButton(
            text=f"📚 {label} • {money(price)}",
            callback_data=f"price:course:{channel_id}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"own:prices:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"📄 {page+1}/{total_pages}", callback_data="price:nop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"own:prices:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Owner Panel", callback_data="own:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def course_price_kb(channel_id, price_rupees):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="➖ ₹50",
                callback_data=f"price:delta:{channel_id}:-50"
            ),
            InlineKeyboardButton(
                text=f"₹{price_rupees}",
                callback_data="price:nop"
            ),
            InlineKeyboardButton(
                text="➕ ₹50",
                callback_data=f"price:delta:{channel_id}:50"
            )
        ],
        [
            InlineKeyboardButton(
                text="➖ ₹100",
                callback_data=f"price:delta:{channel_id}:-100"
            ),
            InlineKeyboardButton(
                text="➕ ₹100",
                callback_data=f"price:delta:{channel_id}:100"
            )
        ],
        [
            InlineKeyboardButton(
                text="✏️ MANUAL PRICE",
                callback_data=f"price:manual_course:{channel_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ All Prices",
                callback_data="own:prices"
            )
        ]
    ])


def demo_kb(minutes):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➖ 5", callback_data="demo_time:-5"),
            InlineKeyboardButton(text=f"⏱ {minutes} Min", callback_data="demo_time:0"),
            InlineKeyboardButton(text="➕ 5", callback_data="demo_time:5")
        ],
        [
            InlineKeyboardButton(text="➖ 1", callback_data="demo_time:-1"),
            InlineKeyboardButton(text="➕ 1", callback_data="demo_time:1")
        ],
        [
            InlineKeyboardButton(text="⬅️ Owner Panel", callback_data="own:home")
        ]
    ])


@router.message(Command("owner"))
async def owner(message: Message):
    if not await owner_only(message.from_user.id):
        await message.answer("❌ Owner only.")
        return

    await message.answer(
        "👑 <b>OWNER PANEL</b>\n\n"
        "नीचे से option चुनें:",
        reply_markup=owner_main_kb(),
        parse_mode="HTML"
    )


@router.message(Command("panel"))
async def panel(message: Message):
    await owner(message)


@router.callback_query(F.data.startswith("own:"))
async def owner_menu(callback: CallbackQuery):
    if not await owner_only(callback.from_user.id):
        await callback.answer("❌ Owner only.", show_alert=True)
        return

    # Acknowledge immediately to prevent Telegram's callback spinner/freeze.
    await callback.answer()
    action = callback.data.split(":", 1)[1]

    if action == "home":
        await callback.message.edit_text(
            "👑 <b>OWNER PANEL</b>\n\nनीचे से option चुनें:",
            reply_markup=owner_main_kb(),
            parse_mode="HTML"
        )

    elif action == "prices" or action.startswith("prices:"):
        courses = await get_courses()
        page = 0
        if action.startswith("prices:"):
            try:
                page = int(action.split(":", 1)[1])
            except ValueError:
                page = 0
        await callback.message.edit_text(
            "💰 <b>COURSE PRICE MANAGER</b>\n\n"
            "🌐 सभी channels की price एक साथ बदल सकते हो।\n"
            "✏️ किसी एक course की exact price भी बदल सकते हो।\n\n"
            f"📚 Total Courses: <b>{len(courses)}</b>",
            reply_markup=prices_kb(courses, page),
            parse_mode="HTML"
        )

    elif action == "demo":
        minutes = await get_demo_minutes()
        await callback.message.edit_text(
            f"⏱ <b>DEMO TIME</b>\n\n"
            f"Current: <b>{minutes} मिनट</b>",
            reply_markup=demo_kb(minutes),
            parse_mode="HTML"
        )

    elif action == "reports":
        users, purchases, revenue, channels, members, demos = await stats()
        await callback.message.edit_text(
            "📊 <b>MY REPORTS</b>\n\n"
            f"👥 Users: <b>{users}</b>\n"
            f"🛒 Successful Purchases: <b>{purchases}</b>\n"
            f"💰 Revenue: <b>{money(revenue)}</b>\n"
            f"📚 Channels: <b>{channels}</b>\n"
            f"👤 Active Granted Members: <b>{members}</b>\n"
            f"🎁 Active Demos: <b>{demos}</b>",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )

    elif action == "users":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT user_id,username,full_name,demo_count,purchase_count,last_seen
                FROM users ORDER BY last_seen DESC LIMIT 30
            """)
            rows = await cur.fetchall()

        text = "👥 <b>USERS DATA</b>\n\n"
        if not rows:
            text += "No users yet."
        else:
            for uid, username, full_name, demos, purchases, last_seen in rows:
                text += (
                    f"👤 <b>{clean_html(full_name or 'Unknown')}</b>\n"
                    f"🆔 <code>{uid}</code>\n"
                    f"🔗 @{clean_html(username or 'none')}\n"
                    f"🎁 Demo: {demos} | 🛒 Purchases: {purchases}\n\n"
                )

        await callback.message.edit_text(
            text[:4000],
            reply_markup=back_kb(),
            parse_mode="HTML"
        )

    elif action == "members":
        rows = await get_members()
        text = "👤 <b>MANAGE MEMBERS</b>\n\n"
        buttons = []

        if not rows:
            text += "No bot-granted members yet."
        else:
            for gid, uid, cid, cname, invite, status, granted, revoked in rows:
                icon = "🟢" if status == "active" else "🔴"
                text += (
                    f"{icon} <b>{clean_html(cname)}</b>\n"
                    f"👤 <code>{uid}</code>\n"
                    f"📌 <code>{cid}</code>\n"
                    f"Status: <b>{status}</b>\n\n"
                )
                if status == "active":
                    buttons.append([
                        InlineKeyboardButton(
                            text=f"🚫 Ban {uid}",
                            callback_data=f"member:ban:{gid}"
                        )
                    ])
                else:
                    buttons.append([
                        InlineKeyboardButton(
                            text=f"♻️ Unban {uid}",
                            callback_data=f"member:unban:{gid}"
                        )
                    ])

        buttons.append([
            InlineKeyboardButton(text="⬅️ Owner Panel", callback_data="own:home")
        ])
        await callback.message.edit_text(
            text[:4000],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )


# ============================================================
# ALL / MANUAL PRICE SYSTEM
# ============================================================

@router.callback_query(F.data == "prices:all")
async def all_price_start(callback: CallbackQuery):
    if not await owner_only(callback.from_user.id):
        await callback.answer("❌ Owner only.", show_alert=True)
        return
    await callback.answer()

    uid = callback.from_user.id
    all_price_mode.add(uid)
    manual_price_mode.discard(uid)
    for item in list(manual_price_mode):
        if isinstance(item, tuple) and item[0] == uid:
            manual_price_mode.discard(item)

    await callback.message.answer(
        "🌐 <b>ALL CHANNELS PRICE</b>\n\n"
        "अब सिर्फ एक message में नई price भेजो।\n\n"
        "Example:\n"
        "<code>499</code>\n\n"
        "इससे <b>सभी channels</b> की price ₹499 हो जाएगी।",
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("prices:all_delta:"))
async def all_delta(callback: CallbackQuery):
    if not await owner_only(callback.from_user.id):
        await callback.answer("❌ Owner only.", show_alert=True)
        return
    await callback.answer()

    delta = int(callback.data.rsplit(":", 1)[1])
    count = await change_all_prices(delta)

    courses = await get_courses()
    await callback.message.edit_text(
        f"✅ <b>ALL CHANNEL PRICES UPDATED</b>\n\n"
        f"Action: {'+' if delta > 0 else ''}{delta}₹\n"
        f"Channels: <b>{count}</b>",
        reply_markup=prices_kb(courses),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "prices:manual")
async def manual_price_start(callback: CallbackQuery):
    if not await owner_only(callback.from_user.id):
        await callback.answer("❌ Owner only.", show_alert=True)
        return
    await callback.answer()

    uid = callback.from_user.id
    all_price_mode.discard(uid)
    for item in list(manual_price_mode):
        if isinstance(item, tuple) and item[0] == uid:
            manual_price_mode.discard(item)
    manual_price_mode.add(uid)

    await callback.message.answer(
        "✏️ <b>MANUAL PRICE</b>\n\n"
        "इस format में भेजो:\n\n"
        "<code>CHANNEL_ID PRICE</code>\n\n"
        "Example:\n"
        "<code>-1001234567890 599</code>\n\n"
        "इससे सिर्फ उस course की exact price ₹599 होगी।",
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("price:course:"))
async def course_price(callback: CallbackQuery):
    if not await owner_only(callback.from_user.id):
        await callback.answer("❌ Owner only.", show_alert=True)
        return
    await callback.answer()

    channel_id = int(callback.data.rsplit(":", 1)[1])
    course = await get_course(channel_id)

    if not course:
        await callback.message.answer("❌ Course नहीं मिला।")
        return

    await callback.message.edit_text(
        "💰 <b>COURSE PRICE</b>\n\n"
        f"📚 <b>{clean_html(course[3])}</b>\n"
        f"Current: <b>{money(course[4])}</b>\n\n"
        "➕/➖ से price बदलें या Manual Price चुनें।",
        reply_markup=course_price_kb(channel_id, course[4] // 100),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("price:delta:"))
async def course_delta(callback: CallbackQuery):
    if not await owner_only(callback.from_user.id):
        await callback.answer("❌ Owner only.", show_alert=True)
        return
    await callback.answer()

    _, _, channel_id, delta = callback.data.split(":")
    channel_id = int(channel_id)
    delta = int(delta)

    new_price = await change_course_price(channel_id, delta)
    if new_price is None:
        await callback.answer("❌ Course नहीं मिला.", show_alert=True)
        return

    course = await get_course(channel_id)
    await callback.message.edit_text(
        "💰 <b>COURSE PRICE</b>\n\n"
        f"📚 <b>{clean_html(course[3])}</b>\n"
        f"Current: <b>{money(new_price)}</b>",
        reply_markup=course_price_kb(channel_id, new_price // 100),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("price:manual_course:"))
async def manual_course_start(callback: CallbackQuery):
    if not await owner_only(callback.from_user.id):
        await callback.answer("❌ Owner only.", show_alert=True)
        return
    await callback.answer()

    channel_id = int(callback.data.rsplit(":", 1)[1])
    uid = callback.from_user.id
    all_price_mode.discard(uid)
    manual_price_mode.discard(uid)
    for item in list(manual_price_mode):
        if isinstance(item, tuple) and item[0] == uid:
            manual_price_mode.discard(item)
    manual_price_mode.add((uid, channel_id))

    await callback.message.answer(
        "✏️ <b>MANUAL COURSE PRICE</b>\n\n"
        "अब सिर्फ नई price भेजो।\n\n"
        "Example:\n"
        "<code>799</code>",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "price:nop")
async def price_nop(callback: CallbackQuery):
    await callback.answer()


@router.message()
async def owner_price_text(message: Message):
    uid = message.from_user.id

    if uid != ADMIN_ID:
        return

    text = (message.text or "").strip()
    if not text:
        return

    # --------------------------------------------------------
    # ALL CHANNELS EXACT PRICE
    # --------------------------------------------------------
    if uid in all_price_mode:
        try:
            rupees = int(text)
            if rupees < 1:
                raise ValueError
        except ValueError:
            await message.answer("❌ सिर्फ valid rupee amount भेजो। Example: 499")
            return

        count = await set_all_course_prices(rupees)
        all_price_mode.discard(uid)

        await message.answer(
            "✅ <b>ALL CHANNELS PRICE UPDATED</b>\n\n"
            f"💰 New Price: <b>₹{rupees}</b>\n"
            f"📚 Channels Updated: <b>{count}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Course Prices", callback_data="own:prices")]]),
            parse_mode="HTML"
        )
        return

    # --------------------------------------------------------
    # MANUAL SINGLE COURSE: channel_id price
    # --------------------------------------------------------
    if uid in manual_price_mode:
        try:
            parts = text.split()
            if len(parts) != 2:
                raise ValueError
            channel_id = int(parts[0])
            rupees = int(parts[1])
            if rupees < 1:
                raise ValueError
        except ValueError:
            await message.answer(
                "❌ Format गलत है.\n\n"
                "Example:\n"
                "<code>-1001234567890 599</code>",
                parse_mode="HTML"
            )
            return

        course = await get_course(channel_id)
        if not course:
            await message.answer("❌ यह channel auto-detected नहीं है।")
            return

        await set_course_price(channel_id, rupees)
        manual_price_mode.discard(uid)

        await message.answer(
            "✅ <b>MANUAL PRICE UPDATED</b>\n\n"
            f"📚 {clean_html(course[3])}\n"
            f"💰 New Price: <b>₹{rupees}</b>",
            reply_markup=back_kb(),
            parse_mode="HTML"
        )
        return

    # --------------------------------------------------------
    # MANUAL PRICE FOR A SPECIFIC COURSE
    # --------------------------------------------------------
    key = (uid, None)
    target = None
    for item in list(manual_price_mode):
        if isinstance(item, tuple) and item[0] == uid:
            target = item
            break

    if target:
        channel_id = target[1]
        try:
            rupees = int(text)
            if rupees < 1:
                raise ValueError
        except ValueError:
            await message.answer("❌ सिर्फ valid price भेजो। Example: 799")
            return

        course = await get_course(channel_id)
        if not course:
            manual_price_mode.discard(target)
            await message.answer("❌ Course नहीं मिला।")
            return

        await set_course_price(channel_id, rupees)
        manual_price_mode.discard(target)

        await message.answer(
            "✅ <b>COURSE PRICE UPDATED</b>\n\n"
            f"📚 {clean_html(course[3])}\n"
            f"💰 New Price: <b>₹{rupees}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Course Prices", callback_data="own:prices")]]),
            parse_mode="HTML"
        )


# ============================================================
# MEMBER BAN / UNBAN
# ============================================================

@router.callback_query(F.data.startswith("member:"))
async def member_action(callback: CallbackQuery):
    if not await owner_only(callback.from_user.id):
        await callback.answer("❌ Owner only.", show_alert=True)
        return

    _, action, gid = callback.data.split(":")
    grant = await get_member(int(gid))

    if not grant:
        await callback.answer("❌ Member record नहीं मिला.", show_alert=True)
        return

    _, user_id, channel_id, course_name, invite_link, status, _, _ = grant

    try:
        if action == "ban":
            await bot.ban_chat_member(channel_id, user_id)
            if invite_link:
                try:
                    await bot.revoke_chat_invite_link(channel_id, invite_link)
                except Exception:
                    pass
            await mark_member(int(gid), "revoked")
            await callback.answer("🚫 Member banned.", show_alert=True)

        elif action == "unban":
            await bot.unban_chat_member(channel_id, user_id, only_if_banned=True)
            await mark_member(int(gid), "active")
            await callback.answer("♻️ Member unbanned.", show_alert=True)

    except Exception as e:
        print("MEMBER ACTION:", e)
        await callback.answer(
            "❌ Action failed. Bot को channel में Ban Users permission दें।",
            show_alert=True
        )
        return

    await owner_menu(
        CallbackQuery(
            id=callback.id,
            from_user=callback.from_user,
            chat_instance=callback.chat_instance,
            message=callback.message,
            data="own:members"
        )
    )


# ============================================================
# COMMAND: CHANNELS / SETPRICE
# ============================================================

@router.message(Command("channels"))
async def channels_cmd(message: Message):
    if not await owner_only(message.from_user.id):
        return
    courses = await get_courses()
    if not courses:
        await message.answer("❌ कोई channel auto-detect नहीं हुआ।")
        return

    text = "📚 <b>CHANNELS</b>\n\n"
    for cid, _, _, name, price in courses:
        text += f"📚 <b>{clean_html(name)}</b>\n🆔 <code>{cid}</code>\n💰 {money(price)}\n\n"
    await message.answer(text[:4000], parse_mode="HTML")


@router.message(Command("setprice"))
async def setprice_cmd(message: Message):
    if not await owner_only(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer(
            "Format:\n<code>/setprice CHANNEL_ID PRICE</code>",
            parse_mode="HTML"
        )
        return
    try:
        cid = int(parts[1])
        price = int(parts[2])
    except ValueError:
        await message.answer("❌ Invalid data.")
        return

    course = await get_course(cid)
    if not course:
        await message.answer("❌ Channel नहीं मिला.")
        return

    await set_course_price(cid, price)
    await message.answer(
        f"✅ {clean_html(course[3])}\n💰 ₹{price}",
        parse_mode="HTML"
    )


@router.message(Command("razorpay_test"))
async def razorpay_test(message: Message):
    if not await owner_only(message.from_user.id):
        return
    try:
        result = await razorpay("GET", "/payment_links?count=1")
        await message.answer(
            "✅ <b>Razorpay API Connected</b>\n\n"
            f"Response: <code>{clean_html(json.dumps(result)[:2500])}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(
            "❌ <b>Razorpay API Error</b>\n\n"
            f"<code>{clean_html(str(e)[:3000])}</code>",
            parse_mode="HTML"
        )


# ============================================================
# MAIN
# ============================================================

async def main():
    await db_init()

    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""

    if FORCE_JOIN_CHANNEL_ID:
        try:
            force_chat = await bot.get_chat(int(FORCE_JOIN_CHANNEL_ID))
            force_member = await bot.get_chat_member(force_chat.id, me.id)
            print(f"FORCE JOIN CHANNEL: {force_chat.title} | BOT STATUS: {force_member.status}")
            if force_member.status != "administrator":
                print("WARNING: Bot must be ADMIN in the Official Channel to receive automatic join updates and verify members.")
        except Exception as exc:
            print("FORCE JOIN STARTUP CHECK ERROR:", exc)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        print("WEBHOOK CLEANUP:", e)

    demo_task = asyncio.create_task(demo_cleaner())
    payment_task = asyncio.create_task(payment_checker())

    print("COURSE BOT STARTED")
    print("AUTO CHANNEL DETECT: ON")
    print("INLINE SEARCH: ON")
    print("DEMO: ON")
    print("RAZORPAY: ON")
    print("OWNER PRICE MANAGER: ON")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query",
                "inline_query",
                "my_chat_member",
                "chat_member",
                "channel_post"
            ]
        )
    finally:
        demo_task.cancel()
        payment_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
