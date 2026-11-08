
import asyncio
import logging
import random
import sqlite3
import os
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CopyTextButton,
)

from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from config import BOT_TOKEN, ADMIN_USERNAME
from database import (
    init_sms_database,
    save_number_owner,
    get_number_owner,
    normalize_number,
)
from sms_forwarder import sms_poll_loop
from augestel import (
    get_all_numbers,
    get_statistics,
    start_cache,
    stop_cache,
)

# =====================================
# SETTINGS
# =====================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

NUMBERS_PER_REQUEST = 3
DATABASE = "users.db"

REQUIRED_GROUP_ID = int(
    os.getenv("REQUIRED_GROUP_ID", "-1004387643125")
)

REQUIRED_GROUP_LINK = os.getenv(
    "REQUIRED_GROUP_LINK",
    "https://t.me/+2xpSHUjQfmM3ZWM1",
)

COUNTRIES = {
    "indonesia": "INDONESIA",
    "malaysia": "MALAYSIA",
    "philippines": "PHILIPPINES",
    "myanmar": "MYANMAR",
}


# =====================================
# SAFE TELEGRAM MESSAGE EDIT
# =====================================

async def safe_edit_message(query, text, **kwargs):
    try:
        return await query.edit_message_text(
            text=text,
            **kwargs
        )

    except BadRequest as error:
        if "Message is not modified" in str(error):
            logger.info("Pesan tidak mengalami perubahan.")
            return None

        raise


# =====================================
# GROUP MEMBERSHIP
# =====================================

def membership_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 JOIN GROUP",
                url=REQUIRED_GROUP_LINK,
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 CEK KEANGGOTAAN",
                callback_data="check_membership",
            )
        ],
    ])


async def check_group_membership(bot, user_id):
    try:
        member = await bot.get_chat_member(
            chat_id=REQUIRED_GROUP_ID,
            user_id=user_id,
        )

        if member.status in (
            "creator",
            "administrator",
            "member",
        ):
            return True

        if member.status == "restricted":
            return bool(member.is_member)

        return False

    except TelegramError:
        logger.exception(
            "Gagal memeriksa keanggotaan grup."
        )
        return None


async def send_membership_message(message):
    await message.reply_text(
        "🔒 <b>AKSES BOT TERKUNCI</b>\n\n"
        "Untuk menggunakan JING OTP BOT, kamu wajib "
        "bergabung ke grup Telegram kami.\n\n"
        "Silakan tekan JOIN GROUP terlebih dahulu, "
        "kemudian tekan CEK KEANGGOTAAN.",
        parse_mode="HTML",
        reply_markup=membership_keyboard(),
    )


async def verify_user_access(update, context):
    user = update.effective_user

    if not user:
        return False

    is_member = await check_group_membership(
        context.bot,
        user.id,
    )

    if is_member is True:
        return True

    if update.callback_query:
        await safe_edit_message(
            update.callback_query,
            "🔒 <b>AKSES BOT TERKUNCI</b>\n\n"
            "Kamu harus bergabung ke grup terlebih dahulu "
            "untuk menggunakan bot.",
            parse_mode="HTML",
            reply_markup=membership_keyboard(),
        )

    elif update.effective_message:
        if is_member is None:
            await update.effective_message.reply_text(
                "⚠️ Tidak dapat memeriksa keanggotaan grup.\n"
                "Pastikan bot merupakan anggota grup dan coba lagi."
            )
        else:
            await send_membership_message(
                update.effective_message
            )

    return False


# =====================================
# DATABASE
# =====================================

def init_database():
    with sqlite3.connect(DATABASE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT
            )
        """)

        columns = [
            row[1]
            for row in db.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        ]

        if "username" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN username TEXT"
            )

        if "first_name" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN first_name TEXT"
            )

        if "joined_at" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN joined_at TEXT"
            )

        db.commit()


def register_user(user):
    if not user:
        return

    with sqlite3.connect(DATABASE) as db:
        db.execute("""
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, joined_at)
            VALUES (?, ?, ?, ?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            datetime.now().isoformat(),
        ))

        db.execute("""
            UPDATE users
            SET username = ?, first_name = ?
            WHERE user_id = ?
        """, (
            user.username or "",
            user.first_name or "",
            user.id,
        ))

        db.commit()


def get_total_users():
    with sqlite3.connect(DATABASE) as db:
        return db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]


# =====================================
# STATISTICS
# =====================================

async def get_total_sms():
    try:
        result = await get_statistics()
        summary = result.get("summary", {})

        return summary.get("total_messages", 0)

    except Exception:
        logger.exception("Gagal mengambil total SMS")
        return "Tidak tersedia"


# =====================================
# MAIN MENU
# =====================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📱 GET NUMBER",
                callback_data="get_number",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ CANCEL",
                callback_data="cancel",
            )
        ],
        [
            InlineKeyboardButton(
                "💬 SUPPORT",
                url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}",
            )
        ],
    ])


def service_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "WhatsApp",
                callback_data="service_whatsapp",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Kembali",
                callback_data="home",
            )
        ],
    ])


def country_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🇮🇩 Indonesia",
                callback_data="country:indonesia",
            )
        ],
        [
            InlineKeyboardButton(
                "🇲🇾 Malaysia",
                callback_data="country:malaysia",
            )
        ],
        [
            InlineKeyboardButton(
                "🇵🇭 Philippines",
                callback_data="country:philippines",
            )
        ],
        [
            InlineKeyboardButton(
                "🇲🇲 Myanmar",
                callback_data="country:myanmar",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Kembali",
                callback_data="get_number",
            )
        ],
    ])


# =====================================
# NUMBER KEYBOARD
# =====================================

def build_number_keyboard(numbers):
    keyboard = []

    for item in numbers:
        number = normalize_number(
            item.get("number", "")
        )

        if not number:
            continue

        keyboard.append([
            InlineKeyboardButton(
                f"📱 {item.get('number', number)}",
                callback_data=f"select_number:{number}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "🟢 GANTI NOMOR",
            callback_data="next_numbers",
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            "🔵 MENU UTAMA",
            callback_data="home",
        )
    ])

    return InlineKeyboardMarkup(keyboard)


# =====================================
# FILTER COUNTRY
# =====================================

def filter_numbers(numbers, country):
    keyword = COUNTRIES.get(country, "").upper()

    aliases = {
        "INDONESIA": ["INDONESIA"],
        "MALAYSIA": ["MALAYSIA"],
        "PHILIPPINES": [
            "PHILIPPINES",
            "PHILIPPINE",
        ],
        "MYANMAR": [
            "MYANMAR",
            "BURMA",
        ],
    }

    keywords = aliases.get(keyword, [keyword])
    filtered = []

    for item in numbers:
        if not isinstance(item, dict):
            continue

        range_name = str(
            item.get("range_name", "")
        ).upper()

        if any(k in range_name for k in keywords):
            filtered.append(item)

    return filtered


# =====================================
# START
# =====================================

async def start(update, context):
    if not await verify_user_access(update, context):
        return

    user = update.effective_user
    register_user(user)

    total_users = get_total_users()
    total_sms = await get_total_sms()

    text = (
        "👋 <b>SELAMAT DATANG DI JING OTP BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 <b>STATISTIK BOT</b>\n"
        f"👥 Total pengguna: <b>{total_users:,}</b>\n"
        f"💬 Total SMS: <b>{total_sms}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Silakan pilih menu di bawah."
    )

    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# =====================================
# CANCEL
# =====================================

async def cancel_command(update, context):
    if not await verify_user_access(update, context):
        return

    context.user_data.clear()

    await update.effective_message.reply_text(
        "✅ Proses dibatalkan.",
        reply_markup=main_menu(),
    )


# =====================================
# SEND NUMBER LIST
# =====================================

async def send_number_list(
    update,
    context,
    country,
):
    query = update.callback_query

    try:
        await safe_edit_message(
            query,
            "⏳ Loading...",
        )

        all_numbers = await get_all_numbers()

        filtered = filter_numbers(
            all_numbers,
            country,
        )

        if not filtered:
            await safe_edit_message(
                query,
                f"❌ Tidak ada nomor tersedia untuk "
                f"{COUNTRIES[country]}.",
                reply_markup=country_menu(),
            )
            return

        context.user_data["numbers"] = filtered
        context.user_data["selected_country"] = country

        await send_next_three(
            query,
            context,
            edit_message=True,
        )

    except Exception:
        logger.exception("Gagal mengambil nomor")

        try:
            await safe_edit_message(
                query,
                "❌ Gagal mengambil nomor.\n"
                "Silakan coba kembali.",
                reply_markup=country_menu(),
            )
        except TelegramError:
            pass


# =====================================
# SEND / CHANGE RANDOM NUMBERS
# =====================================

async def send_next_three(
    query,
    context,
    edit_message=True,
):
    all_numbers = context.user_data.get(
        "numbers",
        [],
    )

    if not all_numbers:
        await safe_edit_message(
            query,
            "Silakan pilih negara kembali.",
            reply_markup=country_menu(),
        )
        return

    if len(all_numbers) <= NUMBERS_PER_REQUEST:
        selected = list(all_numbers)

    else:
        previous = {
            str(item.get("number", ""))
            for item in context.user_data.get(
                "current_numbers",
                [],
            )
        }

        available = [
            item for item in all_numbers
            if str(item.get("number", "")) not in previous
        ]

        if len(available) < NUMBERS_PER_REQUEST:
            available = all_numbers

        selected = random.sample(
            available,
            NUMBERS_PER_REQUEST,
        )

    context.user_data["current_numbers"] = selected

    country = context.user_data.get(
        "selected_country",
        "",
    )

    country_name = COUNTRIES.get(
        country,
        country.upper(),
    )

    remaining = max(
        len(all_numbers) - len(selected),
        0,
    )

    message = (
        f"🌍 <b>NOMOR {country_name}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📱 Jumlah nomor: <b>{len(selected)}</b>\n"
        f"📦 Total tersedia: <b>{len(all_numbers)}</b>\n"
        f"🔢 Sisa pilihan: <b>{remaining}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 <i>Tekan PILIH pada nomor yang ingin kamu gunakan.\n"
        "Nomor akan ditautkan ke akun Telegram kamu.</i>"
    )

    keyboard = build_number_keyboard(selected)

    await safe_edit_message(
        query,
        message,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =====================================
# MEMBERSHIP CHECK CALLBACK
# =====================================

async def handle_membership_check(update, context):
    query = update.callback_query
    user = update.effective_user

    is_member = await check_group_membership(
        context.bot,
        user.id,
    )

    if is_member is True:
        register_user(user)

        total_users = get_total_users()
        total_sms = await get_total_sms()

        welcome_text = (
            "👋 <b>SELAMAT DATANG DI JING OTP BOT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 <b>STATISTIK BOT</b>\n"
            f"👥 Total pengguna: <b>{total_users:,}</b>\n"
            f"💬 Total SMS: <b>{total_sms}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Silakan pilih menu di bawah."
        )

        await safe_edit_message(
            query,
            welcome_text,
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

    elif is_member is False:
        await safe_edit_message(
            query,
            "🔒 <b>AKSES BOT TERKUNCI</b>\n\n"
            "Kamu belum bergabung ke grup kami.\n"
            "Silakan bergabung terlebih dahulu.",
            parse_mode="HTML",
            reply_markup=membership_keyboard(),
        )

    else:
        await safe_edit_message(
            query,
            "⚠️ Tidak dapat memeriksa keanggotaan grup.\n"
            "Pastikan bot merupakan anggota grup dan coba lagi.",
            reply_markup=membership_keyboard(),
        )


# =====================================
# BUTTON HANDLER
# =====================================

async def button_handler(update, context):
    query = update.callback_query
    data = query.data

    try:
        await query.answer()
    except TelegramError:
        logger.warning(
            "Callback sudah kedaluwarsa: %s",
            data,
        )

    # Tombol pengecekan grup harus diproses sebelum gate.
    if data == "check_membership":
        await handle_membership_check(
            update,
            context,
        )
        return

    # Semua tombol lainnya wajib melewati pengecekan grup.
    if not await verify_user_access(update, context):
        return

    if data == "home":
        context.user_data.clear()

        await safe_edit_message(
            query,
            "🏠 <b>MENU UTAMA</b>\n\n"
            "Silakan pilih menu:",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

    elif data == "cancel":
        context.user_data.clear()

        await safe_edit_message(
            query,
            "✅ Proses dibatalkan.",
            reply_markup=main_menu(),
        )

    elif data == "get_number":
        await safe_edit_message(
            query,
            "📱 SILAKAN PILIH LAYANAN:",
            reply_markup=service_menu(),
        )

    elif data == "service_whatsapp":
        await safe_edit_message(
            query,
            "🌍 PILIH NEGARA:",
            reply_markup=country_menu(),
        )

    elif data.startswith("country:"):
        country = data.split(":", 1)[1]

        if country not in COUNTRIES:
            return

        await send_number_list(
            update,
            context,
            country,
        )

    elif data.startswith("select_number:"):
        number = normalize_number(
            data.split(":", 1)[1]
        )

        user_id = update.effective_user.id

        owner_id = save_number_owner(
            user_id,
            number,
        )

        if owner_id != user_id:
            await query.message.reply_text(
                "❌ Nomor ini sudah ditautkan ke "
                "pengguna lain. Silakan pilih nomor berbeda."
            )
            return

        await query.message.reply_text(
            f"✅ Nomor <b>{number}</b> berhasil ditautkan "
            "ke akun kamu.\n\n"
            "SMS masuk untuk nomor ini akan diteruskan "
            "ke chat bot ini.\n"
            "Pastikan kamu sudah menekan /start.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📋 SALIN NOMOR",
                        copy_text=CopyTextButton(text=number),
                    )
                ]
            ]),
        )

    elif data == "next_numbers":
        await send_next_three(
            query,
            context,
            edit_message=True,
        )


# =====================================
# ERROR HANDLER
# =====================================

async def error_handler(update, context):
    logger.error(
        "Terjadi kesalahan:",
        exc_info=context.error,
    )


# =====================================
# POST INIT / SHUTDOWN
# =====================================

async def post_init(application):
    # ID grup wajib tersedia sebelum SMS forwarder mulai berjalan.
    application.bot_data["required_group_id"] = REQUIRED_GROUP_ID

    init_database()
    init_sms_database()

    try:
        await start_cache(application)
    except Exception:
        logger.exception(
            "Cache awal gagal dimuat"
        )

    application.bot_data["sms_poll_task"] = asyncio.create_task(
        sms_poll_loop(application),
        name="sms-forwarder",
    )


async def post_shutdown(application):
    task = application.bot_data.get(
        "sms_poll_task"
    )

    if task is not None:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    await stop_cache(application)


# =====================================
# MAIN
# =====================================

def main():
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN belum diatur di .env"
        )

    init_database()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_command)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "JING OTP Bot berjalan..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
