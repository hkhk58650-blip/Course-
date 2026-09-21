import import aiohttp
import os
import hmac
import hashlib
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()

# Razorpay dashboard webhook secret
RAZORPAY_WEBHOOK_SECRET = os.getenv(
    "RAZORPAY_WEBHOOK_SECRET", ""
).strip()

# Railway public URL
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

DEMO_MINUTES = int(os.getenv("DEMO_MINUTES", "5"))

# Default price in paise
DEFAULT_PRICE = int(os.getenv("DEFAULT_PRICE", "29900"))

DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID missing")

if not RAZORPAY_KEY_ID:
    raise RuntimeError("RAZORPAY_KEY_ID missing")

if not RAZORPAY_KEY_SECRET:
    raise RuntimeError("RAZORPAY_KEY_SECRET missing")

if not PUBLIC_BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL missing")


bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()

dp.include_router(router)


# =========================================================
# DATABASE
# =========================================================

async def init_db():

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
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                course_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                razorpay_link_id TEXT UNIQUE,
                razorpay_payment_id TEXT,
                reference_id TEXT UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
        """)

        await db.commit()


# =========================================================
# CHANNEL AUTO DETECT
# =========================================================

async def save_channel(
    channel_id: int,
    title: str,
    username: str | None
):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            INSERT OR REPLACE INTO channels
            (
                channel_id,
                channel_name,
                username,
                added_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            channel_id,
            title,
            username,
            datetime.now(timezone.utc).isoformat()
        ))

        await db.execute("""
            INSERT OR IGNORE INTO courses
            (
                channel_id,
                course_name,
                price
            )
            VALUES (?, ?, ?)
        """, (
            channel_id,
            title,
            DEFAULT_PRICE
        ))

        await db.commit()


async def get_channels():

    async with aiosqlite.connect(DB_PATH) as db:

        cur = await db.execute("""
            SELECT
                c.channel_id,
                c.channel_name,
                c.username,
                co.course_name,
                co.price
            FROM channels c
            JOIN courses co
            ON co.channel_id = c.channel_id
            ORDER BY c.rowid DESC
        """)

        return await cur.fetchall()


async def get_channel(channel_id: int):

    async with aiosqlite.connect(DB_PATH) as db:

        cur = await db.execute("""
            SELECT
                c.channel_id,
                c.channel_name,
                c.username,
                co.course_name,
                co.price
            FROM channels c
            JOIN courses co
            ON co.channel_id = c.channel_id
            WHERE c.channel_id = ?
        """, (channel_id,))

        return await cur.fetchone()


# =========================================================
# AUTO CHANNEL ADMIN DETECTION
# =========================================================

@router.my_chat_member()
async def channel_auto_detect(
    event: ChatMemberUpdated
):

    chat = event.chat

    if chat.type != "channel":
        return

    status = event.new_chat_member.status

    # Bot became administrator
    if status == "administrator":

        await save_channel(
            chat.id,
            chat.title or "Unnamed Channel",
            chat.username
        )

        try:

            await bot.send_message(
                ADMIN_ID,

                "✅ <b>CHANNEL AUTO ADDED</b>\n\n"
                f"📚 <b>{chat.title}</b>\n"
                f"📌 <code>{chat.id}</code>\n\n"
                f"💰 Default Price: ₹{DEFAULT_PRICE // 100}\n\n"
                "अब public users इस course का demo "
                "और payment ले सकते हैं।",

                parse_mode="HTML"
            )

        except Exception:
            pass

    # Bot removed from channel
    elif status in ("left", "kicked"):

        async with aiosqlite.connect(DB_PATH) as db:

            await db.execute(
                "DELETE FROM channels WHERE channel_id=?",
                (chat.id,)
            )

            await db.execute(
                "DELETE FROM courses WHERE channel_id=?",
                (chat.id,)
            )

            await db.commit()


# =========================================================
# PUBLIC COURSE BUTTONS
# =========================================================

def course_keyboard(channels):

    rows = []

    for channel_id, channel_name, username, course_name, price in channels:

        rows.append([
            InlineKeyboardButton(
                text=f"📚 {course_name} • ₹{price // 100}",
                callback_data=f"course:{channel_id}"
            )
        ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


# =========================================================
# START
# =========================================================

@router.message(Command("start"))
async def start(message: Message):

    channels = await get_channels()

    if not channels:

        await message.answer(
            "❌ अभी कोई course available नहीं है।"
        )

        return

    await message.answer(

        "👋 <b>Welcome</b>\n\n"

        "📚 नीचे अपना course select करें।\n\n"

        f"🎁 Demo: <b>{DEMO_MINUTES} मिनट</b>\n"

        "💳 Demo के बाद Razorpay से payment करके "
        "permanent access मिलेगा।",

        reply_markup=course_keyboard(channels),

        parse_mode="HTML"
    )


@router.message(Command("courses"))
async def courses(message: Message):

    channels = await get_channels()

    if not channels:

        await message.answer(
            "❌ कोई course available नहीं है।"
        )

        return

    await message.answer(
        "📚 <b>Available Courses</b>",
        reply_markup=course_keyboard(channels),
        parse_mode="HTML"
    )


# =========================================================
# COURSE SELECT
# =========================================================

@router.callback_query(
    F.data.startswith("course:")
)
async def course_selected(
    callback: CallbackQuery
):

    channel_id = int(
        callback.data.split(":")[1]
    )

    course = await get_channel(channel_id)

    if not course:

        await callback.answer(
            "❌ Course नहीं मिला।",
            show_alert=True
        )

        return

    (
        channel_id,
        channel_name,
        username,
        course_name,
        price
    ) = course

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🎁 {DEMO_MINUTES} मिनट Demo",
                    callback_data=f"demo:{channel_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"💳 Buy Now ₹{price // 100}",
                    callback_data=f"buy:{channel_id}"
                )
            ]
        ]
    )

    await callback.message.answer(

        f"📚 <b>{course_name}</b>\n\n"

        f"💰 Price: <b>₹{price // 100}</b>\n\n"

        "पहले demo देख सकते हैं।\n"
        "पसंद आने पर Razorpay से payment करें।",

        reply_markup=keyboard,

        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# DEMO LINK
# =========================================================

@router.callback_query(
    F.data.startswith("demo:")
)
async def create_demo(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    channel_id = int(
        callback.data.split(":")[1]
    )

    course = await get_channel(channel_id)

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
                "❌ Demo temporarily unavailable.",
                show_alert=True
            )

            return

        if (
            hasattr(member, "can_invite_users")
            and not member.can_invite_users
        ):

            await callback.answer(
                "❌ Bot को Invite Users permission दें।",
                show_alert=True
            )

            return

        if (
            hasattr(member, "can_restrict_members")
            and not member.can_restrict_members
        ):

            await callback.answer(
                "❌ Bot को Ban Users permission दें।",
                show_alert=True
            )

            return

    except Exception:

        await callback.answer(
            "❌ Channel verify नहीं हो पाया।",
            show_alert=True
        )

        return

    # Remove old demo
    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            "DELETE FROM demos WHERE user_id=?",
            (user_id,)
        )

        await db.commit()

    try:

        invite = await bot.create_chat_invite_link(

            chat_id=channel_id,

            member_limit=1,

            name=f"Demo-{user_id}"
        )

    except Exception:

        await callback.answer(
            "❌ Demo link generate नहीं हो पाया।",
            show_alert=True
        )

        return

    expires = (
        datetime.now(timezone.utc)
        + timedelta(minutes=DEMO_MINUTES)
    )

    async with aiosqlite.connect(DB_PATH) as db:

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
            expires.isoformat()
        ))

        await db.commit()

    await callback.message.answer(

        f"🎁 <b>Demo Ready</b>\n\n"

        f"📚 {course[3]}\n"

        f"⏱ Time: <b>{DEMO_MINUTES} मिनट</b>\n\n"

        f"🔗 <a href=\"{invite.invite_link}\">"
        "👉 JOIN DEMO"
        "</a>\n\n"

        "⚠️ Demo पूरा होने पर access automatically "
        "remove हो जाएगा।\n\n"

        "Demo पसंद आने पर वापस जाकर "
        "💳 Buy Now दबाएँ।",

        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# RAZORPAY API
# =========================================================

async def razorpay_request(
    method: str,
    endpoint: str,
    data=None
):

    url = (
        "https://api.razorpay.com/v1"
        + endpoint
    )

    auth = aiohttp.BasicAuth(
        RAZORPAY_KEY_ID,
        RAZORPAY_KEY_SECRET
    )

    async with aiohttp.ClientSession(
        auth=auth
    ) as session:

        async with session.request(
            method,
            url,
            json=data
        ) as response:

            text = await response.text()

            if response.status >= 400:

                raise RuntimeError(
                    f"Razorpay API error "
                    f"{response.status}: {text}"
                )

            return await response.json()


# =========================================================
# CREATE RAZORPAY PAYMENT LINK
# =========================================================

@router.callback_query(
    F.data.startswith("buy:")
)
async def buy_course(
    callback: CallbackQuery
):

    user = callback.from_user

    channel_id = int(
        callback.data.split(":")[1]
    )

    course = await get_channel(channel_id)

    if not course:

        await callback.answer(
            "❌ Course नहीं मिला।",
            show_alert=True
        )

        return

    (
        channel_id,
        channel_name,
        username,
        course_name,
        price
    ) = course

    # Unique reference
    reference_id = (
        f"tg{user.id}_"
        f"{int(datetime.now().timestamp())}"
    )[:40]

    try:

        payment_link = await razorpay_request(

            "POST",

            "/payment_links",

            {
                "amount": price,

                "currency": "INR",

                "accept_partial": False,

                "description":
                    f"{course_name} - Telegram Access",

                "reference_id":
                    reference_id,

                "customer": {
                    "name":
                        user.full_name[:100]
                },

                "notify": {
                    "sms": False,
                    "email": False
                },

                "reminder_enable": False,

                "callback_url":
                    f"{PUBLIC_BASE_URL}/razorpay/callback",

                "callback_method": "get",

                "notes": {
                    "telegram_user_id":
                        str(user.id),

                    "channel_id":
                        str(channel_id)
                }
            }
        )

    except Exception as e:

        await callback.message.answer(
            "❌ Payment link create नहीं हो पाया.\n"
            "कुछ समय बाद फिर try करें।"
        )

        try:

            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Razorpay Error\n\n<code>{e}</code>",
                parse_mode="HTML"
            )

        except Exception:
            pass

        await callback.answer()

        return

    link_id = payment_link["id"]

    short_url = payment_link["short_url"]

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            INSERT INTO payments
            (
                user_id,
                channel_id,
                course_name,
                amount,
                razorpay_link_id,
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
                    text=f"💳 Pay ₹{price // 100}",
                    url=short_url
                )
            ],

            [
                InlineKeyboardButton(
                    text="🔄 Check Payment",
                    callback_data=f"check:{link_id}"
                )
            ]

        ]
    )

    await callback.message.answer(

        f"💳 <b>Payment</b>\n\n"

        f"📚 {course_name}\n"

        f"💰 Amount: <b>₹{price // 100}</b>\n\n"

        "नीचे Pay button दबाकर Razorpay payment करें।\n\n"

        "Payment successful होने के बाद "
        "आपको permanent access मिलेगा।",

        reply_markup=keyboard,

        parse_mode="HTML"
    )

    await callback.answer()


# =========================================================
# PAYMENT VERIFICATION
# =========================================================

async def verify_payment_link(
    link_id: str
):

    data = await razorpay_request(
        "GET",
        f"/payment_links/{link_id}"
    )

    return data


async def process_successful_payment(
    link_id: str,
    payment_id: str | None = None
):

    payment_info = await verify_payment_link(
        link_id
    )

    status = payment_info.get(
        "status"
    )

    if status != "paid":
        return False

    async with aiosqlite.connect(DB_PATH) as db:

        cur = await db.execute("""
            SELECT
                id,
                user_id,
                channel_id,
                course_name,
                amount,
                status
            FROM payments
            WHERE razorpay_link_id=?
        """, (link_id,))

        row = await cur.fetchone()

        if not row:
            return False

        (
            db_id,
            user_id,
            channel_id,
            course_name,
            amount,
            old_status
        ) = row

        # Already processed
        if old_status == "paid":
            return True

        await db.execute("""
            UPDATE payments
            SET
                status='paid',
                razorpay_payment_id=?,
                paid_at=?
            WHERE id=?
        """, (
            payment_id,
            datetime.now(
                timezone.utc
            ).isoformat(),
            db_id
        ))

        await db.commit()

    # Generate permanent one-use invite
    try:

        invite = await bot.create_chat_invite_link(

            chat_id=channel_id,

            member_limit=1,

            name=f"PAID-{user_id}"
        )

    except Exception as e:

        await bot.send_message(
            ADMIN_ID,

            "⚠️ <b>PAYMENT SUCCESS लेकिन LINK ERROR</b>\n\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"📚 {course_name}\n"
            f"💰 ₹{amount // 100}\n"
            f"💳 Payment: <code>{payment_id or 'N/A'}</code>\n\n"
            f"❌ Invite error:\n<code>{e}</code>",

            parse_mode="HTML"
        )

        return False

    # USER notification
    try:

        await bot.send_message(

            user_id,

            f"🎉 <b>Payment Successful!</b>\n\n"

            f"📚 <b>{course_name}</b>\n"

            f"💰 Paid: <b>₹{amount // 100}</b>\n\n"

            "✅ आपका permanent access तैयार है।\n\n"

            f"🔗 <a href=\"{invite.invite_link}\">"
            "👉 JOIN PREMIUM CHANNEL"
            "</a>",

            parse_mode="HTML"
        )

    except Exception:
        pass

    # OWNER notification
    try:

        await bot.send_message(

            ADMIN_ID,

            "🎉 <b>PURCHASE SUCCESSFUL</b>\n\n"

            f"👤 Name: <b>{user_id}</b>\n"
            f"🆔 User ID: <code>{user_id}</code>\n\n"

            f"📚 Course: <b>{course_name}</b>\n"

            f"💰 Amount: <b>₹{amount // 100}</b>\n"

            f"💳 Payment ID:\n"
            f"<code>{payment_id or 'N/A'}</code>\n\n"

            f"🔗 Razorpay Link:\n"
            f"<code>{link_id}</code>\n\n"

            "✅ Payment verified successfully.",

            parse_mode="HTML"
        )

    except Exception:
        pass

    return True


# =========================================================
# CHECK PAYMENT BUTTON
# =========================================================

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

    try:

        data = await verify_payment_link(
            link_id
        )

        if data.get("status") != "paid":

            await callback.answer(
                "⏳ Payment अभी verified नहीं हुआ है।",
                show_alert=True
            )

            return

        payment_id = data.get(
            "payments",
            {}
        )

        payment_id = None

        await process_successful_payment(
            link_id,
            payment_id
        )

        await callback.answer(
            "✅ Payment verified!",
            show_alert=True
        )

    except Exception:

        await callback.answer(
            "❌ Payment verification failed.",
            show_alert=True
        )


# =========================================================
# RAZORPAY CALLBACK
# =========================================================

async def razorpay_callback(
    request: web.Request
):

    params = request.rel_url.query

    link_id = params.get(
        "razorpay_payment_link_id"
    )

    status = params.get(
        "razorpay_payment_link_status"
    )

    reference_id = params.get(
        "razorpay_payment_link_reference_id"
    )

    signature = params.get(
        "razorpay_signature"
    )

    # Some Razorpay callback configurations
    # use razorpay_payment_link_sign.
    if not signature:

        signature = params.get(
            "razorpay_payment_link_sign"
        )

    if not link_id:

        return web.Response(
            text="Invalid payment callback."
        )

    # Verify callback signature when supplied
    if signature:

        message = (
            f"{link_id}|"
            f"{reference_id or ''}|"
            f"{status or ''}"
        )

        expected = hmac.new(

            RAZORPAY_KEY_SECRET.encode(),

            message.encode(),

            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            expected,
            signature
        ):

            return web.Response(
                status=400,
                text="Invalid signature."
            )

    if status == "paid":

        await process_successful_payment(
            link_id
        )

        return web.Response(

            text=(
                "Payment Successful! "
                "Telegram bot में वापस जाएँ। "
                "आपको permanent access link मिल गया है."
            ),

            content_type="text/plain"
        )

    return web.Response(
        text=(
            "Payment अभी successful नहीं हुआ। "
            "Telegram bot में वापस जाकर Check Payment दबाएँ."
        ),
        content_type="text/plain"
    )


# =========================================================
# RAZORPAY WEBHOOK
# =========================================================

async def razorpay_webhook(
    request: web.Request
):

    raw_body = await request.read()

    signature = request.headers.get(
        "X-Razorpay-Signature",
        ""
    )

    if RAZORPAY_WEBHOOK_SECRET:

        expected = hmac.new(

            RAZORPAY_WEBHOOK_SECRET.encode(),

            raw_body,

            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            expected,
            signature
        ):

            return web.Response(
                status=400,
                text="Invalid webhook signature."
            )

    try:

        payload = await request.json()

        payment_link_entity = (
            payload
            .get("payload", {})
            .get("payment_link", {})
            .get("entity", {})
        )

        link_id = payment_link_entity.get(
            "id"
        )

        if link_id:

            payment_entity = (
                payload
                .get("payload", {})
                .get("payment", {})
                .get("entity", {})
            )

            payment_id = payment_entity.get(
                "id"
            )

            if payload.get("event") == "payment_link.paid":

                await process_successful_payment(
                    link_id,
                    payment_id
                )

        return web.Response(
            text="OK"
        )

    except Exception as e:

        try:

            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Webhook error:\n<code>{e}</code>",
                parse_mode="HTML"
            )

        except Exception:
            pass

        return web.Response(
            status=500,
            text="Webhook error"
        )


# =========================================================
# DEMO CLEANUP
# =========================================================

async def demo_cleanup_loop():

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

                demos = await cur.fetchall()

            for (
                user_id,
                channel_id,
                expires_text
            ) in demos:

                try:

                    expires = datetime.fromisoformat(
                        expires_text
                    )

                    if expires.tzinfo is None:

                        expires = expires.replace(
                            tzinfo=timezone.utc
                        )

                except Exception:

                    expires = now

                if now < expires:
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

                except Exception:
                    pass

                async with aiosqlite.connect(
                    DB_PATH
                ) as db:

                    await db.execute(
                        "DELETE FROM demos WHERE user_id=?",
                        (user_id,)
                    )

                    await db.commit()

        except Exception:
            pass

        await asyncio.sleep(10)


# =========================================================
# OWNER COMMAND - SET PRICE
# =========================================================

@router.message(Command("setprice"))
async def set_price(
    message: Message
):

    if message.from_user.id != ADMIN_ID:

        await message.answer(
            "❌ Owner only."
        )

        return

    parts = message.text.split(
        maxsplit=2
    )

    if len(parts) != 3:

        await message.answer(
            "Usage:\n"
            "/setprice CHANNEL_ID PRICE\n\n"
            "Example:\n"
            "/setprice -1001234567890 499"
        )

        return

    try:

        channel_id = int(parts[1])

        rupees = int(parts[2])

        price = rupees * 100

    except ValueError:

        await message.answer(
            "❌ Invalid value."
        )

        return

    course = await get_channel(
        channel_id
    )

    if not course:

        await message.answer(
            "❌ Channel auto-detect नहीं हुआ।"
        )

        return

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        await db.execute("""
            UPDATE courses
            SET price=?
            WHERE channel_id=?
        """, (
            price,
            channel_id
        ))

        await db.commit()

    await message.answer(
        f"✅ Price updated: ₹{rupees}"
    )


# =========================================================
# OWNER CHANNEL LIST
# =========================================================

@router.message(Command("channels"))
async def owner_channels(
    message: Message
):

    if message.from_user.id != ADMIN_ID:

        await message.answer(
            "❌ Owner only."
        )

        return

    channels = await get_channels()

    if not channels:

        await message.answer(
            "❌ No channels."
        )

        return

    text = "📚 <b>Auto Detected Channels</b>\n\n"

    for row in channels:

        (
            channel_id,
            channel_name,
            username,
            course_name,
            price
        ) = row

        text += (
            f"📚 <b>{course_name}</b>\n"
            f"📌 <code>{channel_id}</code>\n"
            f"💰 ₹{price // 100}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML"
    )


# =========================================================
# WEB SERVER
# =========================================================

async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/razorpay/callback",
        razorpay_callback
    )

    app.router.add_post(
        "/razorpay/webhook",
        razorpay_webhook
    )

    app.router.add_get(
        "/",
        lambda request: web.Response(
            text="Bot is running."
        )
    )

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.getenv("PORT", "8080")
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"Web server running on port {port}"
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    await init_db()

    await start_web_server()

    cleanup_task = asyncio.create_task(
        demo_cleanup_loop()
    )

    try:

        print(
            "PUBLIC RAZORPAY BOT STARTED"
        )

        await dp.start_polling(
            bot,
            allowed_updates=
                dp.resolve_used_update_types()
        )

    finally:

        cleanup_task.cancel()

        await bot.session.close()


if __name__ == "__main__":

    asyncio.run(main())
