import asyncio
import os
import base64
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated, InlineQuery,
    InlineQueryResultArticle, InputTextMessageContent,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_TEXT = os.getenv("ADMIN_ID", "").strip()
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()

DEMO_MINUTES = int(os.getenv("DEMO_MINUTES", "5"))
DEFAULT_PRICE = int(os.getenv("DEFAULT_PRICE", "29900"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

try:
    ADMIN_ID = int(ADMIN_ID_TEXT)
except ValueError:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID")

if not RAZORPAY_KEY_ID:
    raise RuntimeError("RAZORPAY_KEY_ID is missing")

if not RAZORPAY_KEY_SECRET:
    raise RuntimeError("RAZORPAY_KEY_SECRET is missing")

if DEMO_MINUTES < 1:
    raise RuntimeError("DEMO_MINUTES must be 1 or more")

if DEFAULT_PRICE < 100:
    raise RuntimeError("DEFAULT_PRICE must be in paise")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ============================================================
# DATABASE
# ============================================================

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id INTEGER PRIMARY KEY,
                channel_name TEXT NOT NULL,
                username TEXT,
                added_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                channel_id INTEGER PRIMARY KEY,
                course_name TEXT NOT NULL,
                price INTEGER NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS demos (
                user_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS demo_invites (
                user_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                invite_link TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
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
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                demo_count INTEGER NOT NULL DEFAULT 0,
                purchase_count INTEGER NOT NULL DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS access_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                course_name TEXT NOT NULL,
                invite_link TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                granted_at TEXT NOT NULL,
                revoked_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('demo_minutes', ?)
        """, (str(DEMO_MINUTES),))

        await db.commit()


async def db_get_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT
                c.channel_id,
                c.channel_name,
                c.username,
                co.course_name,
                co.price
            FROM channels c
            JOIN courses co ON co.channel_id = c.channel_id
            ORDER BY c.rowid DESC
        """)
        return await cur.fetchall()


async def db_get_channel(channel_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT
                c.channel_id,
                c.channel_name,
                c.username,
                co.course_name,
                co.price
            FROM channels c
            JOIN courses co ON co.channel_id = c.channel_id
            WHERE c.channel_id = ?
        """, (channel_id,))
        return await cur.fetchone()


async def db_add_channel(chat):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM channels WHERE channel_id = ?",
            (chat.id,)
        )
        exists = await cur.fetchone() is not None

        await db.execute("""
            INSERT OR REPLACE INTO channels
            (channel_id, channel_name, username, added_at)
            VALUES (?, ?, ?, ?)
        """, (
            chat.id,
            chat.title or "Unnamed Channel",
            chat.username,
            datetime.now(timezone.utc).isoformat()
        ))

        await db.execute("""
            INSERT OR IGNORE INTO courses
            (channel_id, course_name, price)
            VALUES (?, ?, ?)
        """, (
            chat.id,
            chat.title or "Unnamed Channel",
            DEFAULT_PRICE
        ))

        await db.commit()

    return not exists


async def db_remove_channel(channel_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM channels WHERE channel_id = ?",
            (channel_id,)
        )
        await db.execute(
            "DELETE FROM courses WHERE channel_id = ?",
            (channel_id,)
        )
        await db.commit()


async def db_set_price(channel_id, price_paise):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE courses
            SET price = ?
            WHERE channel_id = ?
        """, (price_paise, channel_id))
        await db.commit()


async def get_demo_minutes():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT value FROM settings WHERE key='demo_minutes'"
        )
        row = await cur.fetchone()

        if not row:
            return DEMO_MINUTES

        try:
            return max(1, int(row[0]))
        except ValueError:
            return DEMO_MINUTES


async def set_demo_minutes(minutes):
    minutes = max(1, int(minutes))

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO settings (key, value)
            VALUES ('demo_minutes', ?)
        """, (str(minutes),))

        await db.commit()


async def upsert_user(user):
    now = datetime.now(timezone.utc).isoformat()
    username = user.username or ""
    full_name = (user.full_name or "")[:200]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users
            (user_id, username, full_name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                last_seen=excluded.last_seen
        """, (
            user.id,
            username,
            full_name,
            now,
            now
        ))

        await db.commit()


async def increment_user_demo(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users
            SET demo_count = demo_count + 1
            WHERE user_id = ?
        """, (user_id,))

        await db.commit()


async def increment_user_purchase(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users
            SET purchase_count = purchase_count + 1
            WHERE user_id = ?
        """, (user_id,))

        await db.commit()


async def owner_purchasers(limit=50):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT
                p.user_id,
                u.username,
                u.full_name,
                p.course_name,
                p.amount,
                p.paid_at
            FROM payments p
            LEFT JOIN users u ON u.user_id = p.user_id
            WHERE p.status = 'paid'
            ORDER BY p.id DESC
            LIMIT ?
        """, (limit,))

        return await cur.fetchall()


async def add_access_grant(
    user_id,
    channel_id,
    course_name,
    invite_link
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO access_grants
            (
                user_id,
                channel_id,
                course_name,
                invite_link,
                status,
                granted_at
            )
            VALUES (?, ?, ?, ?, 'active', ?)
        """, (
            user_id,
            channel_id,
            course_name,
            invite_link,
            datetime.now(timezone.utc).isoformat()
        ))

        await db.commit()


async def get_access_grants(limit=50, status=None):
    async with aiosqlite.connect(DB_PATH) as db:

        if status:
            cur = await db.execute("""
                SELECT
                    id,
                    user_id,
                    channel_id,
                    course_name,
                    invite_link,
                    status,
                    granted_at,
                    revoked_at
                FROM access_grants
                WHERE status = ?
                ORDER BY id DESC
                LIMIT ?
            """, (status, limit))

        else:
            cur = await db.execute("""
                SELECT
                    id,
                    user_id,
                    channel_id,
                    course_name,
                    invite_link,
                    status,
                    granted_at,
                    revoked_at
                FROM access_grants
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))

        return await cur.fetchall()


async def get_access_grant(grant_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT
                id,
                user_id,
                channel_id,
                course_name,
                invite_link,
                status,
                granted_at,
                revoked_at
            FROM access_grants
            WHERE id = ?
        """, (grant_id,))

        return await cur.fetchone()


async def mark_access_revoked(grant_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE access_grants
            SET status='revoked', revoked_at=?
            WHERE id=?
        """, (
            datetime.now(timezone.utc).isoformat(),
            grant_id
        ))

        await db.commit()


async def mark_access_active(grant_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE access_grants
            SET status='active', revoked_at=NULL
            WHERE id=?
        """, (grant_id,))

        await db.commit()


async def revoke_invite(channel_id, invite_link):
    if not invite_link:
        return

    try:
        await bot.revoke_chat_invite_link(
            chat_id=channel_id,
            invite_link=invite_link
        )

    except Exception as exc:
        print("INVITE REVOKE ERROR:", exc)


async def owner_stats_granted_count():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM access_grants "
            "WHERE status='active'"
        )

        return (await cur.fetchone())[0]


async def owner_stats():
    async with aiosqlite.connect(DB_PATH) as db:

        cur = await db.execute(
            "SELECT COUNT(*) FROM users"
        )
        users = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM payments "
            "WHERE status='paid'"
        )
        paid_orders = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) "
            "FROM payments WHERE status='paid'"
        )
        revenue_paise = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM demos"
        )
        active_demos = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM channels"
        )
        channels = (await cur.fetchone())[0]

        return (
            users,
            paid_orders,
            revenue_paise,
            active_demos,
            channels
        )


async def owner_users(limit=30, offset=0):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT
                user_id,
                username,
                full_name,
                first_seen,
                last_seen,
                demo_count,
                purchase_count
            FROM users
            ORDER BY last_seen DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))

        return await cur.fetchall()


async def owner_purchases(limit=50):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT
                user_id,
                course_name,
                amount,
                status,
                created_at,
                paid_at,
                link_id
            FROM payments
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))

        return await cur.fetchall()


# ============================================================
# OWNER NOTIFICATION
# ============================================================

async def owner_notify(text):
    try:
        await bot.send_message(
            ADMIN_ID,
            text,
            parse_mode="HTML"
        )

    except Exception as exc:
        print("OWNER NOTIFICATION ERROR:", exc)


# ============================================================
# CHANNEL AUTO DETECT
# ============================================================

async def detect_channel(chat):
    if chat.type != "channel":
        return

    is_new = await db_add_channel(chat)

    print(
        f"Channel detected: {chat.title} | "
        f"{chat.id} | new={is_new}"
    )

    if is_new:

        username_text = (
            f"@{chat.username}"
            if chat.username
            else "Private Channel"
        )

        await owner_notify(
            "✅ <b>CHANNEL AUTO ADDED</b>\n\n"
            f"📚 <b>{chat.title or 'Unnamed Channel'}</b>\n"
            f"📌 <code>{chat.id}</code>\n"
            f"🔗 {username_text}\n\n"
            "🎁 Demo system: ON\n"
            "💳 Payment system: ON"
        )


@router.my_chat_member()
async def my_chat_member_update(event: ChatMemberUpdated):

    chat = event.chat

    if chat.type != "channel":
        return

    status = event.new_chat_member.status

    print(
        f"my_chat_member: {chat.id} "
        f"{chat.title} -> {status}"
    )

    if status == "administrator":
        await detect_channel(chat)

    elif status in ("left", "kicked"):
        await db_remove_channel(chat.id)


@router.channel_post()
async def channel_post_update(message: Message):

    chat = message.chat

    if chat.type != "channel":
        return

    try:
        me = await bot.get_me()

        member = await bot.get_chat_member(
            chat.id,
            me.id
        )

        if member.status == "administrator":
            await detect_channel(chat)

    except Exception as exc:
        print(
            "CHANNEL POST DETECT ERROR:",
            exc
        )


# ============================================================
# PUBLIC COURSE UI
# ============================================================

def courses_keyboard(channels):

    rows = []

    for (
        channel_id,
        _,
        _,
        course_name,
        price
    ) in channels:

        rows.append([
            InlineKeyboardButton(
                text=(
                    f"📚 {course_name} "
                    f"• ₹{price // 100}"
                ),
                callback_data=f"course:{channel_id}"
            )
        ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


@router.message(Command("start"))
async def start(message: Message):

    await upsert_user(
        message.from_user
    )

    parts = (
        message.text or ""
    ).split(
        maxsplit=1
    )

    # --------------------------------------------------------
    # DEEP LINK
    # /start course_<channel_id>
    # --------------------------------------------------------

    if (
        len(parts) == 2
        and parts[1].startswith("course_")
    ):

        try:
            channel_id = int(
                parts[1].split(
                    "_",
                    1
                )[1]
            )

        except ValueError:
            channel_id = 0

        course = await db_get_channel(
            channel_id
        )

        if course:

            _, _, _, course_name, price = course

            demo_minutes = (
                await get_demo_minutes()
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=(
                                f"🎁 {demo_minutes} "
                                "Min Demo"
                            ),
                            callback_data=(
                                f"demo:{channel_id}"
                            )
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=(
                                f"💳 Buy Now "
                                f"₹{price // 100}"
                            ),
                            callback_data=(
                                f"buy:{channel_id}"
                            )
                        )
                    ]
                ]
            )

            await message.answer(
                f"📚 <b>{course_name}</b>\n\n"
                f"💰 Price: "
                f"<b>₹{price // 100}</b>\n"
                f"🎁 Demo: "
                f"<b>{demo_minutes} मिनट</b>\n\n"
                "पहले Demo देखें या "
                "सीधे payment करें।",
                reply_markup=keyboard,
                parse_mode="HTML"
            )

            return

    # --------------------------------------------------------
    # PUBLIC START
    # ONLY SEARCH BUTTON
    # --------------------------------------------------------

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 चैनल सर्च करें",
                    switch_inline_query_current_chat=""
                )
            ]
        ]
    )

    await message.answer(
        "👋 <b>COURSE DEMO BOT</b>\n\n"
        "🔎 नीचे से channel/course search करें।",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@router.message(Command("courses"))
async def courses(message: Message):

    await upsert_user(
        message.from_user
    )

    channels = await db_get_channels()

    if not channels:
        await message.answer(
            "❌ कोई course available नहीं है।"
        )
        return

    await message.answer(
        "📚 <b>Available Courses</b>",
        reply_markup=courses_keyboard(
            channels
        ),
        parse_mode="HTML"
    )


@router.callback_query(
    F.data.startswith("course:")
)
async def course_selected(
    callback: CallbackQuery
):

    await upsert_user(
        callback.from_user
    )

    channel_id = int(
        callback.data.split(
            ":",
            1
        )[1]
    )

    course = await db_get_channel(
        channel_id
    )

    if not course:

        await callback.answer(
            "❌ Course नहीं मिला।",
            show_alert=True
        )

        return

    _, _, _, course_name, price = course

    demo_minutes = (
        await get_demo_minutes()
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"🎁 {demo_minutes} "
                        "Minute Demo"
                    ),
                    callback_data=(
                        f"demo:{channel_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        f"💳 Buy Now "
                        f"₹{price // 100}"
                    ),
                    callback_data=(
                        f"buy:{channel_id}"
                    )
                )
            ]
        ]
    )

    await callback.message.answer(
        f"📚 <b>{course_name}</b>\n\n"
        f"💰 Price: "
        f"<b>₹{price // 100}</b>\n\n"
        "पहले Demo देखें। "
        "पसंद आने पर Buy Now दबाएँ।",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# TELEGRAM INLINE SEARCH
# ============================================================

BOT_USERNAME = ""


@router.inline_query()
async def inline_search(
    query: InlineQuery
):

    await upsert_user(
        query.from_user
    )

    search = (
        query.query or ""
    ).strip().lower()

    channels = await db_get_channels()

    if search:

        channels = [
            row
            for row in channels
            if (
                search in (
                    row[3] or ""
                ).lower()
                or
                search in (
                    row[1] or ""
                ).lower()
            )
        ]

    results = []

    for (
        channel_id,
        channel_name,
        username,
        course_name,
        price
    ) in channels[:50]:

        if not BOT_USERNAME:
            continue

        deep_link = (
            f"https://t.me/"
            f"{BOT_USERNAME}"
            f"?start=course_{channel_id}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎁 Demo",
                        url=deep_link
                    ),
                    InlineKeyboardButton(
                        text=f"💳 ₹{price // 100}",
                        url=deep_link
                    )
                ]
            ]
        )

        results.append(
            InlineQueryResultArticle(
                id=f"course_{channel_id}",
                title=course_name,
                description=(
                    f"₹{price // 100} "
                    "• Demo available"
                ),
                input_message_content=(
                    InputTextMessageContent(
                        message_text=(
                            f"📚 <b>"
                            f"{course_name}"
                            f"</b>\n"
                            f"💰 ₹{price // 100}\n\n"
                            f"🎁 Demo / Buy: "
                            f"{deep_link}"
                        ),
                        parse_mode="HTML"
                    )
                ),
                reply_markup=keyboard
            )
        )

    await query.answer(
        results=results,
        cache_time=1,
        is_personal=True
    )


# ============================================================
# DEMO
# ============================================================

@router.callback_query(
    F.data.startswith("demo:")
)
async def create_demo(
    callback: CallbackQuery
):

    await upsert_user(
        callback.from_user
    )

    user_id = callback.from_user.id

    channel_id = int(
        callback.data.split(
            ":",
            1
        )[1]
    )

    course = await db_get_channel(
        channel_id
    )

    if not course:

        await callback.answer(
            "❌ Course उपलब्ध नहीं है।",
            show_alert=True
        )

        return

    try:

        me = await bot.get_me()

        member = await bot.get_chat_member(
            channel_id,
            me.id
        )

        if member.status != "administrator":

            await callback.answer(
                "❌ Demo अभी available नहीं है।",
                show_alert=True
            )

            return

        if hasattr(
            member,
            "can_invite_users"
        ):

            if not member.can_invite_users:

                await callback.answer(
                    "❌ Bot को Invite Users "
                    "permission दें।",
                    show_alert=True
                )

                return

        if hasattr(
            member,
            "can_restrict_members"
        ):

            if not member.can_restrict_members:

                await callback.answer(
                    "❌ Bot को Ban Users "
                    "permission दें।",
                    show_alert=True
                )

                return

    except Exception as exc:

        print(
            "DEMO PERMISSION ERROR:",
            exc
        )

        await callback.answer(
            "❌ Channel verify नहीं हो पाया।",
            show_alert=True
        )

        return

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        await db.execute(
            "DELETE FROM demos "
            "WHERE user_id = ?",
            (user_id,)
        )

        await db.commit()

    try:

        invite = (
            await bot.create_chat_invite_link(
                chat_id=channel_id,
                member_limit=1,
                name=f"Demo-{user_id}"
            )
        )

    except Exception as exc:

        print(
            "DEMO LINK ERROR:",
            exc
        )

        await callback.answer(
            "❌ Demo link generate "
            "नहीं हो पाया।",
            show_alert=True
        )

        return

    demo_minutes = (
        await get_demo_minutes()
    )

    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(
            minutes=demo_minutes
        )
    )

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        await db.execute("""
            INSERT OR REPLACE INTO demo_invites
            (
                user_id,
                channel_id,
                invite_link,
                expires_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            channel_id,
            invite.invite_link,
            expires_at.isoformat()
        ))

        await db.commit()

    await increment_user_demo(
        user_id
    )

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        await db.execute("""
            INSERT OR REPLACE INTO demos
            (
                user_id,
                channel_id,
                expires_at
            )
            VALUES (?, ?, ?)
        """, (
            user_id,
            channel_id,
            expires_at.isoformat()
        ))

        await db.commit()

    await callback.message.answer(
        f"🎁 <b>DEMO READY</b>\n\n"
        f"📚 <b>{course[3]}</b>\n"
        f"⏱ Time: "
        f"<b>{demo_minutes} मिनट</b>\n\n"
        f"🔗 <a href=\""
        f"{invite.invite_link}"
        f"\">"
        "👉 JOIN DEMO CHANNEL"
        "</a>\n\n"
        "⚠️ Demo खत्म होने पर "
        "access automatically remove होगा।",
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# RAZORPAY API
# ============================================================

def razorpay_sync(
    method,
    endpoint,
    payload=None
):

    url = (
        "https://api.razorpay.com/v1"
        + endpoint
    )

    raw_auth = (
        f"{RAZORPAY_KEY_ID}:"
        f"{RAZORPAY_KEY_SECRET}"
    ).encode()

    auth = base64.b64encode(
        raw_auth
    ).decode()

    headers = {
        "Authorization": (
            f"Basic {auth}"
        ),
        "Content-Type": (
            "application/json"
        )
    }

    body = None

    if payload is not None:

        body = json.dumps(
            payload
        ).encode()

    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method=method
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            return json.loads(
                response.read().decode()
            )

    except urllib.error.HTTPError as exc:

        error_text = exc.read().decode(
            errors="ignore"
        )

        raise RuntimeError(
            f"Razorpay HTTP "
            f"{exc.code}: "
            f"{error_text}"
        )

    except Exception as exc:

        raise RuntimeError(
            str(exc)
        )


async def razorpay(
    method,
    endpoint,
    payload=None
):

    return await asyncio.to_thread(
        razorpay_sync,
        method,
        endpoint,
        payload
    )


# ============================================================
# BUY / PAYMENT LINK
# ============================================================

@router.callback_query(
    F.data.startswith("buy:")
)
async def buy_course(
    callback: CallbackQuery
):

    await upsert_user(
        callback.from_user
    )

    user = callback.from_user

    channel_id = int(
        callback.data.split(
            ":",
            1
        )[1]
    )

    course = await db_get_channel(
        channel_id
    )

    if not course:

        await callback.answer(
            "❌ Course नहीं मिला।",
            show_alert=True
        )

        return

    _, _, _, course_name, price = course

    reference_id = (
        f"TG{user.id}_"
        f"{int(datetime.now().timestamp())}"
    )[:40]

    try:

        payment_link = await razorpay(
            "POST",
            "/payment_links",
            {
                "amount": price,
                "currency": "INR",
                "accept_partial": False,
                "description": (
                    f"{course_name} "
                    "Telegram Access"
                ),
                "reference_id": reference_id,
                "customer": {
                    "name": (
                        user.full_name[:100]
                    )
                },
                "notify": {
                    "sms": False,
                    "email": False
                },
                "reminder_enable": False,
                "notes": {
                    "telegram_user_id": (
                        str(user.id)
                    ),
                    "channel_id": (
                        str(channel_id)
                    )
                }
            }
        )

    except Exception as exc:

        print(
            "RAZORPAY CREATE ERROR:",
            exc
        )

        await callback.message.answer(
            "❌ Payment link नहीं बन पाया।"
        )

        await owner_notify(
            "⚠️ <b>RAZORPAY ERROR</b>\n\n"
            f"<code>"
            f"{str(exc)[:3000]}"
            f"</code>"
        )

        await callback.answer()

        return

    link_id = payment_link.get(
        "id"
    )

    short_url = payment_link.get(
        "short_url"
    )

    if not link_id or not short_url:

        await callback.message.answer(
            "❌ Razorpay ने payment "
            "link नहीं दिया।"
        )

        await callback.answer()

        return

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        await db.execute("""
            INSERT INTO payments
            (
                user_id,
                channel_id,
                course_name,
                amount,
                link_id,
                reference_id,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user.id,
            channel_id,
            course_name,
            price,
            link_id,
            reference_id,
            "created",
            datetime.now(
                timezone.utc
            ).isoformat()
        ))

        await db.commit()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"💳 PAY "
                        f"₹{price // 100}"
                    ),
                    url=short_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Check Payment",
                    callback_data=(
                        f"check:{link_id}"
                    )
                )
            ]
        ]
    )

    await callback.message.answer(
        "💳 <b>PAYMENT</b>\n\n"
        f"📚 {course_name}\n"
        f"💰 Amount: "
        f"<b>₹{price // 100}</b>\n\n"
        "नीचे PAY button दबाकर "
        "Razorpay checkout खोलें।\n"
        "Payment successful होने के बाद "
        "bot access भेज देगा।",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# PAYMENT VERIFICATION + ACCESS
# ============================================================

async def process_payment(
    link_id
):

    try:

        payment_link = await razorpay(
            "GET",
            f"/payment_links/{link_id}"
        )

    except Exception as exc:

        print(
            "PAYMENT VERIFY ERROR:",
            exc
        )

        return False

    if payment_link.get(
        "status"
    ) != "paid":

        return False

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        cur = await db.execute("""
            SELECT
                id,
                user_id,
                channel_id,
                course_name,
                amount,
                status
            FROM payments
            WHERE link_id = ?
        """, (link_id,))

        row = await cur.fetchone()

        if not row:

            print(
                "Payment link not found "
                "in DB:",
                link_id
            )

            return False

        (
            db_id,
            user_id,
            channel_id,
            course_name,
            amount,
            old_status
        ) = row

        if old_status == "paid":
            return True

        await db.execute("""
            UPDATE payments
            SET
                status = 'paid',
                paid_at = ?
            WHERE id = ?
        """, (
            datetime.now(
                timezone.utc
            ).isoformat(),
            db_id
        ))

        await db.commit()

    # --------------------------------------------------------
    # ONE-USE PREMIUM INVITE
    # --------------------------------------------------------

    try:

        invite = (
            await bot.create_chat_invite_link(
                chat_id=channel_id,
                member_limit=1,
                expire_date=(
                    datetime.now(
                        timezone.utc
                    )
                    + timedelta(
                        minutes=10
                    )
                ),
                name=f"PAID-{user_id}"
            )
        )

    except Exception as exc:

        await owner_notify(
            "⚠️ <b>PAYMENT SUCCESS — "
            "INVITE ERROR</b>\n\n"
            f"👤 User: "
            f"<code>{user_id}</code>\n"
            f"📚 Course: "
            f"<b>{course_name}</b>\n"
            f"💰 ₹{amount // 100}\n"
            f"❌ <code>"
            f"{str(exc)[:2500]}"
            f"</code>"
        )

        return False

    await add_access_grant(
        user_id,
        channel_id,
        course_name,
        invite.invite_link
    )

    # --------------------------------------------------------
    # BUYER MESSAGE
    # --------------------------------------------------------

    try:

        await bot.send_message(
            user_id,
            "🎉 <b>PAYMENT SUCCESSFUL</b>\n\n"
            f"📚 <b>{course_name}</b>\n"
            f"💰 Paid: "
            f"<b>₹{amount // 100}</b>\n\n"
            "✅ आपका permanent access "
            "तैयार है।\n\n"
            f"🔗 <a href=\""
            f"{invite.invite_link}"
            f"\">"
            "👉 JOIN PREMIUM CHANNEL"
            "</a>",
            parse_mode="HTML"
        )

    except Exception as exc:

        print(
            "BUYER NOTIFICATION ERROR:",
            exc
        )

    await increment_user_purchase(
        user_id
    )

    # --------------------------------------------------------
    # OWNER NOTIFICATION
    # --------------------------------------------------------

    await owner_notify(
        "🎉 <b>PURCHASE SUCCESSFUL</b>\n\n"
        f"👤 Telegram User ID:\n"
        f"<code>{user_id}</code>\n\n"
        f"📚 Course:\n"
        f"<b>{course_name}</b>\n\n"
        f"💰 Amount:\n"
        f"<b>₹{amount // 100}</b>\n\n"
        f"💳 Razorpay Link:\n"
        f"<code>{link_id}</code>\n\n"
        "✅ Payment verified successfully."
    )

    return True


@router.callback_query(
    F.data.startswith("check:")
)
async def check_payment(
    callback: CallbackQuery
):

    link_id = callback.data.split(
        ":",
        1
    )[1]

    success = await process_payment(
        link_id
    )

    if success:

        await callback.answer(
            "✅ Payment verified!",
            show_alert=True
        )

    else:

        await callback.answer(
            "⏳ Payment अभी verify "
            "नहीं हुआ।",
            show_alert=True
        )


async def payment_checker():

    while True:

        try:

            async with aiosqlite.connect(
                DB_PATH
            ) as db:

                cur = await db.execute("""
                    SELECT link_id
                    FROM payments
                    WHERE status = 'created'
                    ORDER BY id DESC
                    LIMIT 100
                """)

                rows = await cur.fetchall()

            for (link_id,) in rows:

                await process_payment(
                    link_id
                )

                await asyncio.sleep(
                    0.2
                )

        except Exception as exc:

            print(
                "PAYMENT CHECKER ERROR:",
                exc
            )

        await asyncio.sleep(
            15
        )


# ============================================================
# DEMO EXPIRY
# ============================================================

async def demo_cleaner():

    while True:

        try:

            now = datetime.now(
                timezone.utc
            )

            async with aiosqlite.connect(
                DB_PATH
            ) as db:

                cur = await db.execute("""
                    SELECT
                        user_id,
                        channel_id,
                        expires_at
                    FROM demos
                """)

                rows = await cur.fetchall()

            for (
                user_id,
                channel_id,
                expires_text
            ) in rows:

                try:

                    expires_at = (
                        datetime.fromisoformat(
                            expires_text
                        )
                    )

                    if (
                        expires_at.tzinfo
                        is None
                    ):
                        expires_at = (
                            expires_at.replace(
                                tzinfo=timezone.utc
                            )
                        )

                except Exception:

                    expires_at = now

                if now < expires_at:
                    continue

                try:

                    await bot.ban_chat_member(
                        chat_id=channel_id,
                        user_id=user_id
                    )

                    await bot.unban_chat_member(
                        chat_id=channel_id,
                        user_id=user_id,
                        only_if_banned=True
                    )

                    print(
                        "Demo expired:",
                        f"user={user_id}",
                        f"channel={channel_id}"
                    )

                except Exception as exc:

                    print(
                        "DEMO REMOVE ERROR:",
                        exc
                    )

                async with aiosqlite.connect(
                    DB_PATH
                ) as db:

                    cur2 = await db.execute("""
                        SELECT invite_link
                        FROM demo_invites
                        WHERE user_id=?
                    """, (user_id,))

                    demo_row = (
                        await cur2.fetchone()
                    )

                    await db.execute(
                        "DELETE FROM demos "
                        "WHERE user_id = ?",
                        (user_id,)
                    )

                    await db.execute(
                        "DELETE FROM demo_invites "
                        "WHERE user_id = ?",
                        (user_id,)
                    )

                    await db.commit()

                if demo_row:

                    await revoke_invite(
                        channel_id,
                        demo_row[0]
                    )

        except Exception as exc:

            print(
                "DEMO CLEANER ERROR:",
                exc
            )

        await asyncio.sleep(
            10
        )


# ============================================================
# MEMBER JOIN -> AUTO EXPIRE ONE-USE INVITE
# ============================================================

@router.chat_member()
async def member_joined(
    event: ChatMemberUpdated
):

    if event.chat.type != "channel":
        return

    new_status = (
        event.new_chat_member.status
    )

    old_status = (
        event.old_chat_member.status
    )

    joined = (
        new_status in (
            "member",
            "administrator"
        )
        and old_status in (
            "left",
            "kicked"
        )
    )

    if not joined:
        return

    user_id = (
        event.new_chat_member.user.id
    )

    invite = getattr(
        event,
        "invite_link",
        None
    )

    # Revoke exact invite after join.
    if (
        invite
        and invite.invite_link
    ):

        await revoke_invite(
            event.chat.id,
            invite.invite_link
        )

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        if (
            invite
            and invite.invite_link
        ):

            await db.execute("""
                UPDATE access_grants
                SET status='active'
                WHERE user_id=?
                  AND channel_id=?
                  AND invite_link=?
            """, (
                user_id,
                event.chat.id,
                invite.invite_link
            ))

        await db.execute("""
            DELETE FROM demo_invites
            WHERE user_id=?
              AND channel_id=?
        """, (
            user_id,
            event.chat.id
        ))

        await db.commit()


# ============================================================
# OWNER INLINE PANEL
# ============================================================

def owner_panel_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Users Data",
                    callback_data="owner:users"
                ),
                InlineKeyboardButton(
                    text="⏱ Demo Time",
                    callback_data="owner:demo"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 Course Prices",
                    callback_data="owner:prices"
                ),
                InlineKeyboardButton(
                    text="📊 My Reports",
                    callback_data="owner:reports"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 Manage Members",
                    callback_data="owner:members"
                )
            ]
        ]
    )


def owner_back_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Owner Panel",
                    callback_data="owner:home"
                )
            ]
        ]
    )


def owner_users_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Purchased Users",
                    callback_data="owner:purchasers"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Owner Panel",
                    callback_data="owner:home"
                )
            ]
        ]
    )


def owner_demo_keyboard(
    minutes
):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➖ 1 Min",
                    callback_data=(
                        "demo_setting:-1"
                    )
                ),
                InlineKeyboardButton(
                    text=f"⏱ {minutes} Min",
                    callback_data=(
                        "demo_setting:show"
                    )
                ),
                InlineKeyboardButton(
                    text="➕ 1 Min",
                    callback_data=(
                        "demo_setting:1"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="➖ 5 Min",
                    callback_data=(
                        "demo_setting:-5"
                    )
                ),
                InlineKeyboardButton(
                    text="➕ 5 Min",
                    callback_data=(
                        "demo_setting:5"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Owner Panel",
                    callback_data="owner:home"
                )
            ]
        ]
    )


def owner_prices_keyboard(
    channels
):

    rows = []

    for (
        channel_id,
        _,
        _,
        course_name,
        price
    ) in channels:

        rows.append([
            InlineKeyboardButton(
                text=(
                    f"📚 {course_name}"
                    f" • ₹{price // 100}"
                ),
                callback_data=(
                    f"price_course:"
                    f"{channel_id}"
                )
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="⬅️ Owner Panel",
            callback_data="owner:home"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def owner_price_edit_keyboard(
    channel_id,
    price_rupees
):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➖ ₹50",
                    callback_data=(
                        f"price_change:"
                        f"{channel_id}:-50"
                    )
                ),
                InlineKeyboardButton(
                    text=f"₹{price_rupees}",
                    callback_data=(
                        "price_change:"
                        "show:0"
                    )
                ),
                InlineKeyboardButton(
                    text="➕ ₹50",
                    callback_data=(
                        f"price_change:"
                        f"{channel_id}:50"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="➖ ₹100",
                    callback_data=(
                        f"price_change:"
                        f"{channel_id}:-100"
                    )
                ),
                InlineKeyboardButton(
                    text="➕ ₹100",
                    callback_data=(
                        f"price_change:"
                        f"{channel_id}:100"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ All Course Prices",
                    callback_data="owner:prices"
                )
            ]
        ]
    )


async def is_owner(
    user_id
):

    return user_id == ADMIN_ID


@router.message(
    Command("owner")
)
async def owner_command(
    message: Message
):

    await owner_panel_command(
        message
    )


@router.message(
    Command("panel")
)
async def owner_panel_command(
    message: Message
):

    if not await is_owner(
        message.from_user.id
    ):

        await message.answer(
            "❌ Owner only."
        )

        return

    await message.answer(
        "🛠 <b>OWNER PANEL</b>\n\n"
        "नीचे से option चुनें:",
        reply_markup=owner_panel_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(
    F.data.startswith("owner:")
)
async def owner_panel_callback(
    callback: CallbackQuery
):

    if not await is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Owner only.",
            show_alert=True
        )

        return

    action = callback.data.split(
        ":",
        1
    )[1]

    if action == "home":

        await callback.message.edit_text(
            "🛠 <b>OWNER PANEL</b>\n\n"
            "नीचे से option चुनें:",
            reply_markup=owner_panel_keyboard(),
            parse_mode="HTML"
        )

        await callback.answer()

        return

    if action == "users":

        users = await owner_users(
            limit=25,
            offset=0
        )

        if not users:

            text = (
                "👥 <b>USERS DATA</b>\n\n"
                "❌ अभी कोई user नहीं है।"
            )

        else:

            text = (
                "👥 <b>USERS DATA</b>\n\n"
            )

            for (
                user_id,
                username,
                full_name,
                first_seen,
                last_seen,
                demos,
                purchases
            ) in users:

                name = (
                    full_name
                    or "Unknown"
                ).replace(
                    "<",
                    ""
                ).replace(
                    ">",
                    ""
                )

                handle = (
                    f"@{username}"
                    if username
                    else "No username"
                )

                text += (
                    f"👤 <b>{name}</b>\n"
                    f"🆔 <code>{user_id}</code>\n"
                    f"🔗 {handle}\n"
                    f"🎁 Demos: {demos} "
                    f"| 🛒 Purchases: "
                    f"{purchases}\n"
                    f"🕐 Last: "
                    f"{last_seen[:19].replace('T', ' ')}"
                    f"\n\n"
                )

        await callback.message.edit_text(
            text[:4000],
            reply_markup=owner_users_keyboard(),
            parse_mode="HTML"
        )

        await callback.answer()

        return

    if action == "purchasers":

        rows = await owner_purchasers(
            limit=50
        )

        if not rows:

            text = (
                "🛒 <b>PURCHASED USERS</b>\n\n"
                "❌ अभी कोई successful "
                "purchase नहीं है।"
            )

        else:

            text = (
                "🛒 <b>PURCHASED USERS</b>\n\n"
            )

            for (
                user_id,
                username,
                full_name,
                course_name,
                amount,
                paid_at
            ) in rows:

                name = (
                    full_name
                    or "Unknown"
                ).replace(
                    "<",
                    ""
                ).replace(
                    ">",
                    ""
                )

                handle = (
                    f"@{username}"
                    if username
                    else "No username"
                )

                paid = (
                    paid_at or "-"
                )[:19].replace(
                    "T",
                    " "
                )

                text += (
                    f"👤 <b>{name}</b>\n"
                    f"🆔 <code>{user_id}</code>\n"
                    f"🔗 {handle}\n"
                    f"📚 {course_name}\n"
                    f"💰 ₹{amount // 100}\n"
                    f"🕐 {paid}\n\n"
                )

        await callback.message.edit_text(
            text[:4000],
            reply_markup=owner_users_keyboard(),
            parse_mode="HTML"
        )

        await callback.answer()

        return

    if action == "members":

        grants = await get_access_grants(
            limit=30
        )

        if not grants:

            text = (
                "👤 <b>MANAGE MEMBERS</b>\n\n"
                "❌ अभी bot द्वारा कोई "
                "paid member add नहीं हुआ।"
            )

            kb = owner_back_keyboard()

        else:

            text = (
                "👤 <b>MANAGE MEMBERS</b>\n\n"
            )

            rows = []

            for (
                grant_id,
                user_id,
                channel_id,
                course_name,
                invite_link,
                status,
                granted_at,
                revoked_at
            ) in grants:

                icon = (
                    "🟢"
                    if status == "active"
                    else "🔴"
                )

                text += (
                    f"{icon} <b>{course_name}</b>\n"
                    f"👤 <code>{user_id}</code>\n"
                    f"📌 <code>{channel_id}</code>\n"
                    f"📅 "
                    f"{granted_at[:19].replace('T', ' ')}"
                    f"\n"
                    f"📍 Status: "
                    f"<b>{status}</b>\n\n"
                )

                if status == "active":

                    rows.append([
                        InlineKeyboardButton(
                            text=(
                                f"🚫 Ban {user_id}"
                            ),
                            callback_data=(
                                f"member_ban:"
                                f"{grant_id}"
                            )
                        )
                    ])

                else:

                    rows.append([
                        InlineKeyboardButton(
                            text=(
                                f"♻️ Unban "
                                f"{user_id}"
                            ),
                            callback_data=(
                                f"member_unban:"
                                f"{grant_id}"
                            )
                        )
                    ])

            rows.append([
                InlineKeyboardButton(
                    text="⬅️ Owner Panel",
                    callback_data="owner:home"
                )
            ])

            kb = InlineKeyboardMarkup(
                inline_keyboard=rows
            )

        await callback.message.edit_text(
            text[:4000],
            reply_markup=kb,
            parse_mode="HTML"
        )

        await callback.answer()

        return

    if action == "demo":

        minutes = (
            await get_demo_minutes()
        )

        await callback.message.edit_text(
            "⏱ <b>DEMO TIME CONTROL</b>\n\n"
            f"Current demo time: "
            f"<b>{minutes} मिनट</b>\n\n"
            "➕/➖ buttons से time बदलें।\n"
            "यह नया default demo time "
            "आगे आने वाले demos पर लागू होगा।",
            reply_markup=owner_demo_keyboard(
                minutes
            ),
            parse_mode="HTML"
        )

        await callback.answer()

        return

    if action == "prices":

        channels = await db_get_channels()

        await callback.message.edit_text(
            "💰 <b>COURSE PRICES</b>\n\n"
            "जिस course की price बदलनी है "
            "उसे चुनें:",
            reply_markup=owner_prices_keyboard(
                channels
            ),
            parse_mode="HTML"
        )

        await callback.answer()

        return

    if action == "reports":

        (
            users,
            paid_orders,
            revenue_paise,
            active_demos,
            channels
        ) = await owner_stats()

        await callback.message.edit_text(
            "📊 <b>MY REPORTS</b>\n\n"
            f"👥 Total Users: "
            f"<b>{users}</b>\n"
            f"🛒 Successful Purchases: "
            f"<b>{paid_orders}</b>\n"
            f"💰 Total Revenue: "
            f"<b>₹{revenue_paise // 100}</b>\n"
            f"🎁 Active Demos: "
            f"<b>{active_demos}</b>\n"
            f"📚 Auto-Detected Courses: "
            f"<b>{channels}</b>\n"
            f"👤 Bot-Granted Members: "
            f"<b>{await owner_stats_granted_count()}</b>",
            reply_markup=owner_back_keyboard(),
            parse_mode="HTML"
        )

        await callback.answer()

        return


@router.callback_query(
    F.data.startswith("demo_setting:")
)
async def owner_demo_setting(
    callback: CallbackQuery
):

    if not await is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Owner only.",
            show_alert=True
        )

        return

    parts = callback.data.split(
        ":"
    )

    delta = (
        int(parts[1])
        if parts[1] != "show"
        else 0
    )

    if delta:

        current = (
            await get_demo_minutes()
        )

        new_value = max(
            1,
            min(
                1440,
                current + delta
            )
        )

        await set_demo_minutes(
            new_value
        )

    else:

        new_value = (
            await get_demo_minutes()
        )

    await callback.message.edit_text(
        "⏱ <b>DEMO TIME CONTROL</b>\n\n"
        f"Current demo time: "
        f"<b>{new_value} मिनट</b>\n\n"
        "➕/➖ buttons से time बदलें।",
        reply_markup=owner_demo_keyboard(
            new_value
        ),
        parse_mode="HTML"
    )

    await callback.answer(
        f"Demo time: {new_value} min"
    )


@router.callback_query(
    F.data.startswith("price_course:")
)
async def owner_price_course(
    callback: CallbackQuery
):

    if not await is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Owner only.",
            show_alert=True
        )

        return

    channel_id = int(
        callback.data.split(
            ":",
            1
        )[1]
    )

    course = await db_get_channel(
        channel_id
    )

    if not course:

        await callback.answer(
            "❌ Course नहीं मिला।",
            show_alert=True
        )

        return

    price_rupees = (
        course[4] // 100
    )

    await callback.message.edit_text(
        "💰 <b>COURSE PRICE CONTROL</b>\n\n"
        f"📚 <b>{course[3]}</b>\n"
        f"Current Price: "
        f"<b>₹{price_rupees}</b>\n\n"
        "Buttons से price बढ़ाएँ "
        "या घटाएँ:",
        reply_markup=owner_price_edit_keyboard(
            channel_id,
            price_rupees
        ),
        parse_mode="HTML"
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith("price_change:")
)
async def owner_price_change(
    callback: CallbackQuery
):

    if not await is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Owner only.",
            show_alert=True
        )

        return

    parts = callback.data.split(
        ":"
    )

    if (
        len(parts) != 3
        or parts[1] == "show"
    ):

        await callback.answer()

        return

    channel_id = int(
        parts[1]
    )

    delta_rupees = int(
        parts[2]
    )

    course = await db_get_channel(
        channel_id
    )

    if not course:

        await callback.answer(
            "❌ Course नहीं मिला।",
            show_alert=True
        )

        return

    current_rupees = (
        course[4] // 100
    )

    new_rupees = max(
        1,
        current_rupees + delta_rupees
    )

    await db_set_price(
        channel_id,
        new_rupees * 100
    )

    await callback.message.edit_text(
        "💰 <b>COURSE PRICE CONTROL</b>\n\n"
        f"📚 <b>{course[3]}</b>\n"
        f"Current Price: "
        f"<b>₹{new_rupees}</b>\n\n"
        "Price successfully updated.",
        reply_markup=owner_price_edit_keyboard(
            channel_id,
            new_rupees
        ),
        parse_mode="HTML"
    )

    await callback.answer(
        f"Price updated: ₹{new_rupees}"
    )


@router.callback_query(
    F.data == "owner:purchase_list"
)
async def owner_purchase_list(
    callback: CallbackQuery
):

    if not await is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Owner only.",
            show_alert=True
        )

        return

    rows = await owner_purchases(
        limit=30
    )

    if not rows:

        text = (
            "🛒 <b>PURCHASE LIST</b>\n\n"
            "❌ No purchases yet."
        )

    else:

        text = (
            "🛒 <b>PURCHASE LIST</b>\n\n"
        )

        for (
            user_id,
            course_name,
            amount,
            status,
            created_at,
            paid_at,
            link_id
        ) in rows:

            text += (
                f"👤 <code>{user_id}</code>\n"
                f"📚 {course_name}\n"
                f"💰 ₹{amount // 100}\n"
                f"📌 {status}\n"
                f"🕐 "
                f"{created_at[:19].replace('T', ' ')}"
                f"\n\n"
            )

    await callback.message.edit_text(
        text[:4000],
        reply_markup=owner_back_keyboard(),
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# OWNER MEMBER ACTIONS
# ============================================================

@router.callback_query(
    F.data.startswith("member_ban:")
)
async def owner_member_ban(
    callback: CallbackQuery
):

    if not await is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Owner only.",
            show_alert=True
        )

        return

    grant_id = int(
        callback.data.split(
            ":",
            1
        )[1]
    )

    grant = await get_access_grant(
        grant_id
    )

    if not grant:

        await callback.answer(
            "❌ Member record नहीं मिला।",
            show_alert=True
        )

        return

    (
        _,
        user_id,
        channel_id,
        course_name,
        invite_link,
        status,
        _,
        _
    ) = grant

    try:

        await bot.ban_chat_member(
            chat_id=channel_id,
            user_id=user_id
        )

        await revoke_invite(
            channel_id,
            invite_link
        )

        await mark_access_revoked(
            grant_id
        )

        await owner_notify(
            "🚫 <b>MEMBER BANNED</b>\n\n"
            f"👤 User: "
            f"<code>{user_id}</code>\n"
            f"📚 Course: "
            f"<b>{course_name}</b>\n"
            f"📌 Channel: "
            f"<code>{channel_id}</code>"
        )

        await callback.answer(
            "🚫 Member banned."
        )

        grants = await get_access_grants(
            limit=30
        )

        text = (
            "👤 <b>MANAGE MEMBERS</b>\n\n"
        )

        rows = []

        for (
            gid,
            uid,
            cid,
            cname,
            ilink,
            st,
            ga,
            ra
        ) in grants:

            icon = (
                "🟢"
                if st == "active"
                else "🔴"
            )

            text += (
                f"{icon} <b>{cname}</b>\n"
                f"👤 <code>{uid}</code>\n"
                f"📌 <code>{cid}</code>\n"
                f"📍 Status: "
                f"<b>{st}</b>\n\n"
            )

            rows.append([
                InlineKeyboardButton(
                    text=(
                        f"🚫 Ban {uid}"
                        if st == "active"
                        else f"♻️ Unban {uid}"
                    ),
                    callback_data=(
                        f"member_ban:{gid}"
                        if st == "active"
                        else f"member_unban:{gid}"
                    )
                )
            ])

        rows.append([
            InlineKeyboardButton(
                text="⬅️ Owner Panel",
                callback_data="owner:home"
            )
        ])

        await callback.message.edit_text(
            text[:4000],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=rows
            ),
            parse_mode="HTML"
        )

    except Exception as exc:

        await callback.answer(
            "❌ Ban नहीं हो पाया। "
            "Bot को Ban Users permission दें।",
            show_alert=True
        )

        print(
            "OWNER BAN ERROR:",
            exc
        )


@router.callback_query(
    F.data.startswith("member_unban:")
)
async def owner_member_unban(
    callback: CallbackQuery
):

    if not await is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "❌ Owner only.",
            show_alert=True
        )

        return

    grant_id = int(
        callback.data.split(
            ":",
            1
        )[1]
    )

    grant = await get_access_grant(
        grant_id
    )

    if not grant:

        await callback.answer(
            "❌ Member record नहीं मिला।",
            show_alert=True
        )

        return

    (
        _,
        user_id,
        channel_id,
        course_name,
        invite_link,
        status,
        _,
        _
    ) = grant

    try:

        await bot.unban_chat_member(
            chat_id=channel_id,
            user_id=user_id,
            only_if_banned=True
        )

        await mark_access_active(
            grant_id
        )

        await callback.answer(
            "♻️ Member unbanned."
        )

        grants = await get_access_grants(
            limit=30
        )

        text = (
            "👤 <b>MANAGE MEMBERS</b>\n\n"
        )

        rows = []

        for (
            gid,
            uid,
            cid,
            cname,
            ilink,
            st,
            ga,
            ra
        ) in grants:

            icon = (
                "🟢"
                if st == "active"
                else "🔴"
            )

            text += (
                f"{icon} <b>{cname}</b>\n"
                f"👤 <code>{uid}</code>\n"
                f"📌 <code>{cid}</code>\n"
                f"📍 Status: "
                f"<b>{st}</b>\n\n"
            )

            rows.append([
                InlineKeyboardButton(
                    text=(
                        f"🚫 Ban {uid}"
                        if st == "active"
                        else f"♻️ Unban {uid}"
                    ),
                    callback_data=(
                        f"member_ban:{gid}"
                        if st == "active"
                        else f"member_unban:{gid}"
                    )
                )
            ])

        rows.append([
            InlineKeyboardButton(
                text="⬅️ Owner Panel",
                callback_data="owner:home"
            )
        ])

        await callback.message.edit_text(
            text[:4000],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=rows
            ),
            parse_mode="HTML"
        )

    except Exception as exc:

        await callback.answer(
            "❌ Unban नहीं हो पाया।",
            show_alert=True
        )

        print(
            "OWNER UNBAN ERROR:",
            exc
        )


# ============================================================
# OWNER COMMANDS
# ============================================================

@router.message(
    Command("channels")
)
async def owner_channels(
    message: Message
):

    if message.from_user.id != ADMIN_ID:

        await message.answer(
            "❌ Owner only."
        )

        return

    channels = await db_get_channels()

    if not channels:

        await message.answer(
            "❌ अभी कोई channel "
            "auto-detect नहीं हुआ।"
        )

        return

    text = (
        "📚 <b>AUTO DETECTED CHANNELS</b>\n\n"
    )

    for (
        channel_id,
        _,
        _,
        course_name,
        price
    ) in channels:

        text += (
            f"📚 <b>{course_name}</b>\n"
            f"📌 <code>{channel_id}</code>\n"
            f"💰 ₹{price // 100}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML"
    )


@router.message(
    Command("setprice")
)
async def owner_set_price(
    message: Message
):

    if message.from_user.id != ADMIN_ID:

        await message.answer(
            "❌ Owner only."
        )

        return

    parts = (
        message.text
        .split(
            maxsplit=2
        )
    )

    if len(parts) != 3:

        await message.answer(
            "Format:\n"
            "/setprice CHANNEL_ID PRICE\n\n"
            "Example:\n"
            "/setprice "
            "-1001234567890 499"
        )

        return

    try:

        channel_id = int(
            parts[1]
        )

        rupees = int(
            parts[2]
        )

    except ValueError:

        await message.answer(
            "❌ Channel ID या "
            "price गलत है।"
        )

        return

    if rupees < 1:

        await message.answer(
            "❌ Price सही डालें।"
        )

        return

    course = await db_get_channel(
        channel_id
    )

    if not course:

        await message.answer(
            "❌ Channel auto-detect "
            "नहीं हुआ।"
        )

        return

    await db_set_price(
        channel_id,
        rupees * 100
    )

    await message.answer(
        "✅ <b>PRICE UPDATED</b>\n\n"
        f"📚 {course[3]}\n"
        f"💰 ₹{rupees}",
        parse_mode="HTML"
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    await db_init()

    try:

        await bot.delete_webhook(
            drop_pending_updates=False
        )

    except Exception as exc:

        print(
            "WEBHOOK CLEANUP:",
            exc
        )

    demo_task = asyncio.create_task(
        demo_cleaner()
    )

    payment_task = asyncio.create_task(
        payment_checker()
    )

    try:

        print(
            "--------------------------------------------"
        )

        print(
            "PUBLIC COURSE BOT STARTED"
        )

        print(
            "AUTO CHANNEL DETECTION : ON"
        )

        print(
            "PUBLIC DEMO            : ON"
        )

        print(
            "RAZORPAY               : ON"
        )

        print(
            "PAYMENT CHECKER        : ON"
        )

        print(
            "--------------------------------------------"
        )

        global BOT_USERNAME

        me = await bot.get_me()

        BOT_USERNAME = (
            me.username or ""
        )

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
