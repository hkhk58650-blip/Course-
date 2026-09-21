import os
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================
# DATABASE
# =========================

def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            channel_id TEXT NOT NULL UNIQUE,
            price INTEGER NOT NULL DEFAULT 0,
            demo_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            utr TEXT,
            payment_id TEXT,
            created_at TEXT NOT NULL,
            verified_at TEXT
        )
    """)

    con.commit()
    con.close()


# =========================
# HELPERS
# =========================

def now():
    return datetime.now(timezone.utc).isoformat()


def save_user(user):
    con = db()
    con.execute("""
        INSERT INTO users
        (user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        now(),
    ))
    con.commit()
    con.close()


def get_courses():
    con = db()
    rows = con.execute("""
        SELECT id, name, channel_id, price, demo_enabled
        FROM courses
        ORDER BY id DESC
    """).fetchall()
    con.close()
    return rows


def get_course(course_id):
    con = db()
    row = con.execute("""
        SELECT id, name, channel_id, price, demo_enabled
        FROM courses
        WHERE id=?
    """, (course_id,)).fetchone()
    con.close()
    return row


# =========================
# START / PUBLIC
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    save_user(user)

    keyboard = [
        [InlineKeyboardButton("📚 Courses", callback_data="courses")],
        [InlineKeyboardButton("🛒 My Purchases", callback_data="purchases")],
    ]

    if user.id == OWNER_ID:
        keyboard.append(
            [InlineKeyboardButton("👑 Owner Panel", callback_data="owner")]
        )

    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "यहाँ से अपना course select करें।\n"
        "पहले Demo देखें और फिर Purchase Now से course खरीदें।",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# COURSE LIST
# =========================

async def show_courses(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    courses = get_courses()

    if not courses:
        await query.edit_message_text(
            "❌ अभी कोई course available नहीं है।"
        )
        return

    buttons = []

    for course in courses:
        cid, name, channel_id, price, demo = course

        price_text = f"₹{price}" if price > 0 else "Price not set"

        buttons.append([
            InlineKeyboardButton(
                f"📚 {name} • {price_text}",
                callback_data=f"course:{cid}"
            )
        ])

    await query.edit_message_text(
        "📚 Available Courses:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================
# COURSE DETAILS
# =========================

async def course_details(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    course_id = int(query.data.split(":")[1])

    course = get_course(course_id)

    if not course:
        await query.edit_message_text("❌ Course नहीं मिला।")
        return

    cid, name, channel_id, price, demo = course

    buttons = []

    if demo:
        buttons.append([
            InlineKeyboardButton(
                "🎁 Demo Access",
                callback_data=f"demo:{cid}"
            )
        ])

    if price > 0:
        buttons.append([
            InlineKeyboardButton(
                f"💳 Purchase Now ₹{price}",
                callback_data=f"buy:{cid}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("⬅️ Back", callback_data="courses")
    ])

    await query.edit_message_text(
        f"📚 <b>{name}</b>\n\n"
        f"💰 Price: ₹{price}\n\n"
        f"🎁 पहले Demo देखें।\n"
        f"💳 पसंद आने पर Purchase Now करें।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================
# DEMO
# =========================

async def demo_access(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    course_id = int(query.data.split(":")[1])
    course = get_course(course_id)

    if not course:
        await query.edit_message_text("❌ Course नहीं मिला।")
        return

    cid, name, channel_id, price, demo = course

    try:
        # Bot must be administrator in the course channel.
        invite = await context.bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1
        )

        keyboard = [[
            InlineKeyboardButton(
                "🎁 Join Demo",
                url=invite.invite_link
            )
        ], [
            InlineKeyboardButton(
                f"💳 Purchase Now ₹{price}",
                callback_data=f"buy:{cid}"
            )
        ]]

        await query.edit_message_text(
            f"🎁 <b>{name} Demo</b>\n\n"
            "Demo access link नीचे है.\n"
            "Demo देखने के बाद Purchase Now से पूरा course खरीद सकते हैं।",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.exception("Demo invite error")

        await query.edit_message_text(
            "❌ Demo link generate नहीं हो पाया।\n"
            "Bot को channel में administrator होना चाहिए।"
        )


# =========================
# BUY
# =========================

async def buy_course(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    course_id = int(query.data.split(":")[1])
    course = get_course(course_id)

    if not course:
        await query.edit_message_text("❌ Course नहीं मिला।")
        return

    cid, name, channel_id, price, demo = course

    if price <= 0:
        await query.edit_message_text(
            "❌ इस course की price अभी set नहीं है।"
        )
        return

    con = db()

    cur = con.execute("""
        INSERT INTO payments
        (user_id, course_id, amount, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
    """, (
        query.from_user.id,
        course_id,
        price,
        now()
    ))

    payment_db_id = cur.lastrowid

    con.commit()
    con.close()

    # IMPORTANT:
    # यहाँ actual Razorpay order/payment-link creation connect करना होगा.
    #
    # अभी user को payment verification page की जगह
    # setup message दिया जा रहा है ताकि fake payment को success
    # न माना जाए.

    await query.edit_message_text(
        f"💳 <b>{name}</b>\n\n"
        f"Amount: ₹{price}\n\n"
        "Payment system configured होने के बाद यहाँ "
        "secure Razorpay payment button आएगा।\n\n"
        f"Payment ID: #{payment_db_id}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"course:{course_id}"
                )
            ]
        ])
    )


# =========================
# OWNER PANEL
# =========================

async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        await query.answer("❌ Access denied", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton("➕ Add Course", callback_data="add_course")],
        [InlineKeyboardButton("💰 Price Panel", callback_data="prices")],
        [InlineKeyboardButton("📚 Manage Courses", callback_data="manage_courses")],
    ]

    await query.edit_message_text(
        "👑 <b>Owner Panel</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# PRICE PANEL
# =========================

async def price_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        return

    courses = get_courses()

    if not courses:
        await query.edit_message_text(
            "❌ कोई course नहीं है।",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back", callback_data="owner")]
            ])
        )
        return

    buttons = []

    for cid, name, channel_id, price, demo in courses:

        buttons.append([
            InlineKeyboardButton(
                f"💰 {name} — ₹{price}",
                callback_data=f"setprice:{cid}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("⬅️ Back", callback_data="owner")
    ])

    await query.edit_message_text(
        "💰 <b>Price Panel</b>\n\n"
        "जिस course की price बदलनी है उसे select करो:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================
# SET PRICE
# =========================

async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        return

    course_id = int(query.data.split(":")[1])

    course = get_course(course_id)

    if not course:
        return

    context.user_data["set_price_course"] = course_id

    await query.edit_message_text(
        f"💰 <b>{course[1]}</b>\n\n"
        "नई price भेजें.\n\n"
        "Example:\n"
        "<code>799</code>",
        parse_mode="HTML"
    )


async def receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_ID:
        return

    course_id = context.user_data.get("set_price_course")

    if not course_id:
        return

    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text(
            "❌ सिर्फ number भेजो.\nExample: 799"
        )
        return

    price = int(text)

    if price <= 0:
        await update.message.reply_text(
            "❌ Price 0 से ज्यादा होनी चाहिए."
        )
        return

    con = db()

    con.execute(
        "UPDATE courses SET price=? WHERE id=?",
        (price, course_id)
    )

    con.commit()
    con.close()

    context.user_data.pop("set_price_course", None)

    await update.message.reply_text(
        f"✅ Price successfully set: ₹{price}"
    )


# =========================
# ADD COURSE
# =========================

async def add_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        return

    context.user_data["adding_course"] = True

    await query.edit_message_text(
        "➕ <b>Add Course</b>\n\n"
        "इस format में भेजो:\n\n"
        "<code>Course Name | -1001234567890</code>\n\n"
        "Price बाद में Price Panel से set कर सकते हो.",
        parse_mode="HTML"
    )


async def receive_course(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_ID:
        return

    if not context.user_data.get("adding_course"):
        return

    text = update.message.text.strip()

    parts = [x.strip() for x in text.split("|")]

    if len(parts) != 2:
        await update.message.reply_text(
            "❌ Format गलत है.\n\n"
            "Example:\n"
            "Ceramic RAS | -1001234567890"
        )
        return

    name, channel_id = parts

    con = db()

    try:
        con.execute("""
            INSERT INTO courses
            (name, channel_id, price, demo_enabled, created_at)
            VALUES (?, ?, 0, 1, ?)
        """, (
            name,
            channel_id,
            now()
        ))

        con.commit()

        await update.message.reply_text(
            f"✅ Course added.\n\n"
            f"📚 {name}\n"
            f"📢 {channel_id}\n\n"
            "अब Owner Panel → Price Panel में जाकर price set कर दो."
        )

    except sqlite3.IntegrityError:
        await update.message.reply_text(
            "❌ यह channel पहले से added है."
        )

    finally:
        con.close()

    context.user_data.pop("adding_course", None)


# =========================
# COMMANDS
# =========================

async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_ID:
        return

    keyboard = [
        [InlineKeyboardButton("➕ Add Course", callback_data="add_course")],
        [InlineKeyboardButton("💰 Price Panel", callback_data="prices")],
        [InlineKeyboardButton("📚 Courses", callback_data="courses")],
    ]

    await update.message.reply_text(
        "👑 Owner Panel",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# CALLBACK ROUTER
# =========================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    data = query.data

    if data == "courses":
        await show_courses(update, context)

    elif data == "owner":
        await owner_panel(update, context)

    elif data == "prices":
        await price_panel(update, context)

    elif data == "add_course":
        await add_course_start(update, context)

    elif data.startswith("setprice:"):
        await set_price(update, context)

    elif data.startswith("course:"):
        await course_details(update, context)

    elif data.startswith("demo:"):
        await demo_access(update, context)

    elif data.startswith("buy:"):
        await buy_course(update, context)


# =========================
# MAIN
# =========================

def main():

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")

    if not OWNER_ID:
        raise RuntimeError("OWNER_ID missing")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("owner", owner_command))

    app.add_handler(
        CallbackQueryHandler(callbacks)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_text
        )
    )

    logger.info("Bot started")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_ID:
        return

    if context.user_data.get("set_price_course"):
        await receive_price(update, context)
        return

    if context.user_data.get("adding_course"):
        await receive_course(update, context)
        return


if __name__ == "__main__":
    main()
