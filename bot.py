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
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated
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
        member = await bot.get_chat_member(chat.id, me.id)

        if member.status == "administrator":
            await detect_channel(chat)

    except Exception as exc:
        print("CHANNEL POST DETECT ERROR:", exc)


# ============================================================
# PUBLIC COURSE UI
# ============================================================

def courses_keyboard(channels):
    rows = []

    for channel_id, _, _, course_name, price in channels:
        rows.append([
            InlineKeyboardButton(
                text=f"📚 {course_name} • ₹{price // 100}",
                callback_data=f"course:{channel_id}"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def start(message: Message):
    channels = await db_get_channels()

    if not channels:
        await message.answer(
            "❌ अभी कोई course available नहीं है।"
        )
        return

    await message.answer(
        "👋 <b>Welcome</b>\n\n"
        "📚 अपना course चुनें।\n"
        f"🎁 Demo: <b>{DEMO_MINUTES} मिनट</b>\n"
        "💳 Payment के बाद permanent access मिलेगा।",
        reply_markup=courses_keyboard(channels),
        parse_mode="HTML"
    )


@router.message(Command("courses"))
async def courses(message: Message):
    channels = await db_get_channels()

    if not channels:
        await message.answer("❌ कोई course available नहीं है।")
        return

    await message.answer(
        "📚 <b>Available Courses</b>",
        reply_markup=courses_keyboard(channels),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("course:"))
async def course_selected(callback: CallbackQuery):
    channel_id = int(callback.data.split(":", 1)[1])
    course = await db_get_channel(channel_id)

    if not course:
        await callback.answer(
            "❌ Course नहीं मिला।",
            show_alert=True
        )
        return

    _, _, _, course_name, price = course

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"🎁 {DEMO_MINUTES} Minute Demo",
                callback_data=f"demo:{channel_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"💳 Buy Now ₹{price // 100}",
                callback_data=f"buy:{channel_id}"
            )
        ]
    ])

    await callback.message.answer(
        f"📚 <b>{course_name}</b>\n\n"
        f"💰 Price: <b>₹{price // 100}</b>\n\n"
        "पहले Demo देखें। पसंद आने पर Buy Now दबाएँ।",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================================
# DEMO
# ============================================================

@router.callback_query(F.data.startswith("demo:"))
async def create_demo(callback: CallbackQuery):
    user_id = callback.from_user.id
    channel_id = int(callback.data.split(":", 1)[1])

    course = await db_get_channel(channel_id)

    if not course:
        await callback.answer(
            "❌ Course उपलब्ध नहीं है।",
            show_alert=True
        )
        return

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(channel_id, me.id)

        if member.status != "administrator":
            await callback.answer(
                "❌ Demo अभी available नहीं है।",
                show_alert=True
            )
            return

        if hasattr(member, "can_invite_users"):
            if not member.can_invite_users:
                await callback.answer(
                    "❌ Bot को Invite Users permission दें।",
                    show_alert=True
                )
                return

        if hasattr(member, "can_restrict_members"):
            if not member.can_restrict_members:
                await callback.answer(
                    "❌ Bot को Ban Users permission दें।",
                    show_alert=True
                )
                return

    except Exception as exc:
        print("DEMO PERMISSION ERROR:", exc)
        await callback.answer(
            "❌ Channel verify नहीं हो पाया।",
            show_alert=True
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM demos WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1,
            name=f"Demo-{user_id}"
        )
    except Exception as exc:
        print("DEMO LINK ERROR:", exc)
        await callback.answer(
            "❌ Demo link generate नहीं हो पाया।",
            show_alert=True
        )
        return

    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(minutes=DEMO_MINUTES)
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO demos
            (user_id, channel_id, expires_at)
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
        f"⏱ Time: <b>{DEMO_MINUTES} मिनट</b>\n\n"
        f"🔗 <a href=\"{invite.invite_link}\">"
        "👉 JOIN DEMO CHANNEL</a>\n\n"
        "⚠️ Demo खत्म होने पर access automatically remove होगा।",
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================================
# RAZORPAY API
# ============================================================

def razorpay_sync(method, endpoint, payload=None):
    url = "https://api.razorpay.com/v1" + endpoint

    raw_auth = (
        f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}"
    ).encode()

    auth = base64.b64encode(raw_auth).decode()

    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json"
    }

    body = None
    if payload is not None:
        body = json.dumps(payload).encode()

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
            f"Razorpay HTTP {exc.code}: {error_text}"
        )

    except Exception as exc:
        raise RuntimeError(str(exc))


async def razorpay(method, endpoint, payload=None):
    return await asyncio.to_thread(
        razorpay_sync,
        method,
        endpoint,
        payload
    )


# ============================================================
# BUY / PAYMENT LINK
# ============================================================

@router.callback_query(F.data.startswith("buy:"))
async def buy_course(callback: CallbackQuery):
    user = callback.from_user
    channel_id = int(callback.data.split(":", 1)[1])

    course = await db_get_channel(channel_id)

    if not course:
        await callback.answer(
            "❌ Course नहीं मिला।",
            show_alert=True
        )
        return

    _, _, _, course_name, price = course

    reference_id = (
        f"TG{user.id}_{int(datetime.now().timestamp())}"
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
                    f"{course_name} Telegram Access"
                ),
                "reference_id": reference_id,
                "customer": {
                    "name": user.full_name[:100]
                },
                "notify": {
                    "sms": False,
                    "email": False
                },
                "reminder_enable": False,
                "notes": {
                    "telegram_user_id": str(user.id),
                    "channel_id": str(channel_id)
                }
            }
        )

    except Exception as exc:
        print("RAZORPAY CREATE ERROR:", exc)

        await callback.message.answer(
            "❌ Payment link नहीं बन पाया।"
        )

        await owner_notify(
            "⚠️ <b>RAZORPAY ERROR</b>\n\n"
            f"<code>{str(exc)[:3000]}</code>"
        )

        await callback.answer()
        return

    link_id = payment_link.get("id")
    short_url = payment_link.get("short_url")

    if not link_id or not short_url:
        await callback.message.answer(
            "❌ Razorpay ने payment link नहीं दिया।"
        )
        await callback.answer()
        return

    async with aiosqlite.connect(DB_PATH) as db:
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
            datetime.now(timezone.utc).isoformat()
        ))
        await db.commit()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"💳 PAY ₹{price // 100}",
                url=short_url
            )
        ],
        [
            InlineKeyboardButton(
                text="🔄 Check Payment",
                callback_data=f"check:{link_id}"
            )
        ]
    ])

    await callback.message.answer(
        "💳 <b>PAYMENT</b>\n\n"
        f"📚 {course_name}\n"
        f"💰 Amount: <b>₹{price // 100}</b>\n\n"
        "नीचे PAY button दबाकर Razorpay checkout खोलें।\n"
        "Payment successful होने के बाद bot access भेज देगा।",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================================
# PAYMENT VERIFICATION + ACCESS
# ============================================================

async def process_payment(link_id):
    try:
        payment_link = await razorpay(
            "GET",
            f"/payment_links/{link_id}"
        )
    except Exception as exc:
        print("PAYMENT VERIFY ERROR:", exc)
        return False

    if payment_link.get("status") != "paid":
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
            WHERE link_id = ?
        """, (link_id,))

        row = await cur.fetchone()

        if not row:
            print("Payment link not found in DB:", link_id)
            return False

        db_id, user_id, channel_id, course_name, amount, old_status = row

        if old_status == "paid":
            return True

        await db.execute("""
            UPDATE payments
            SET status = 'paid', paid_at = ?
            WHERE id = ?
        """, (
            datetime.now(timezone.utc).isoformat(),
            db_id
        ))
        await db.commit()

    # Create a one-use permanent invite.
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1,
            name=f"PAID-{user_id}"
        )
    except Exception as exc:
        await owner_notify(
            "⚠️ <b>PAYMENT SUCCESS — INVITE ERROR</b>\n\n"
            f"👤 User: <code>{user_id}</code>\n"
            f"📚 Course: <b>{course_name}</b>\n"
            f"💰 ₹{amount // 100}\n"
            f"❌ <code>{str(exc)[:2500]}</code>"
        )
        return False

    # Buyer message.
    try:
        await bot.send_message(
            user_id,
            "🎉 <b>PAYMENT SUCCESSFUL</b>\n\n"
            f"📚 <b>{course_name}</b>\n"
            f"💰 Paid: <b>₹{amount // 100}</b>\n\n"
            "✅ आपका permanent access तैयार है।\n\n"
            f"🔗 <a href=\"{invite.invite_link}\">"
            "👉 JOIN PREMIUM CHANNEL</a>",
            parse_mode="HTML"
        )
    except Exception as exc:
        print("BUYER NOTIFICATION ERROR:", exc)

    # Owner notification.
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


@router.callback_query(F.data.startswith("check:"))
async def check_payment(callback: CallbackQuery):
    link_id = callback.data.split(":", 1)[1]

    success = await process_payment(link_id)

    if success:
        await callback.answer(
            "✅ Payment verified!",
            show_alert=True
        )
    else:
        await callback.answer(
            "⏳ Payment अभी verify नहीं हुआ।",
            show_alert=True
        )


async def payment_checker():
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("""
                    SELECT link_id
                    FROM payments
                    WHERE status = 'created'
                    ORDER BY id DESC
                    LIMIT 100
                """)
                rows = await cur.fetchall()

            for (link_id,) in rows:
                await process_payment(link_id)
                await asyncio.sleep(0.2)

        except Exception as exc:
            print("PAYMENT CHECKER ERROR:", exc)

        await asyncio.sleep(15)


# ============================================================
# DEMO EXPIRY
# ============================================================

async def demo_cleaner():
    while True:
        try:
            now = datetime.now(timezone.utc)

            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("""
                    SELECT user_id, channel_id, expires_at
                    FROM demos
                """)
                rows = await cur.fetchall()

            for user_id, channel_id, expires_text in rows:
                try:
                    expires_at = datetime.fromisoformat(
                        expires_text
                    )
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(
                            tzinfo=timezone.utc
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
                        f"Demo expired: "
                        f"user={user_id} channel={channel_id}"
                    )

                except Exception as exc:
                    print(
                        "DEMO REMOVE ERROR:",
                        exc
                    )

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "DELETE FROM demos WHERE user_id = ?",
                        (user_id,)
                    )
                    await db.commit()

        except Exception as exc:
            print("DEMO CLEANER ERROR:", exc)

        await asyncio.sleep(10)


# ============================================================
# OWNER COMMANDS
# ============================================================

@router.message(Command("channels"))
async def owner_channels(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Owner only.")
        return

    channels = await db_get_channels()

    if not channels:
        await message.answer(
            "❌ अभी कोई channel auto-detect नहीं हुआ।"
        )
        return

    text = "📚 <b>AUTO DETECTED CHANNELS</b>\n\n"

    for channel_id, _, _, course_name, price in channels:
        text += (
            f"📚 <b>{course_name}</b>\n"
            f"📌 <code>{channel_id}</code>\n"
            f"💰 ₹{price // 100}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML"
    )


@router.message(Command("setprice"))
async def owner_set_price(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Owner only.")
        return

    parts = message.text.split(maxsplit=2)

    if len(parts) != 3:
        await message.answer(
            "Format:\n"
            "/setprice CHANNEL_ID PRICE\n\n"
            "Example:\n"
            "/setprice -1001234567890 499"
        )
        return

    try:
        channel_id = int(parts[1])
        rupees = int(parts[2])
    except ValueError:
        await message.answer(
            "❌ Channel ID या price गलत है।"
        )
        return

    if rupees < 1:
        await message.answer(
            "❌ Price सही डालें।"
        )
        return

    course = await db_get_channel(channel_id)

    if not course:
        await message.answer(
            "❌ Channel auto-detect नहीं हुआ।"
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

    # Polling mode. Remove an old webhook so polling can receive updates.
    try:
        await bot.delete_webhook(
            drop_pending_updates=False
        )
    except Exception as exc:
        print("WEBHOOK CLEANUP:", exc)

    demo_task = asyncio.create_task(
        demo_cleaner()
    )

    payment_task = asyncio.create_task(
        payment_checker()
    )

    try:
        print("--------------------------------------------")
        print("PUBLIC COURSE BOT STARTED")
        print("AUTO CHANNEL DETECTION : ON")
        print("PUBLIC DEMO            : ON")
        print("RAZORPAY               : ON")
        print("PAYMENT CHECKER        : ON")
        print("--------------------------------------------")

        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query",
                "my_chat_member",
                "channel_post"
            ]
        )

    finally:
        demo_task.cancel()
        payment_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
