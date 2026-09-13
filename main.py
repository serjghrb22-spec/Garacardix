import asyncio
import html
import logging
import math
import os
from datetime import datetime, timezone

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = "8474019540:AAGOJ5GVrmlhzZHGzbUhJAAYko1BadJbwno"
CRYPTO_PAY_TOKEN = "633729:AA3nk2sqed53YyH4u7Z1NQXvX2646ZcWbmV"

ADMIN_IDS = {8206706574}

DB_PATH = "cardix.db"
PAY_ASSET = "USDT"

TOPUP_MIN = 30.0
TOPUP_MAX = 500.0

REQUIRED_CHANNEL = "@CardIxchanel"
SUPPORT_USERNAME = "@CardIxMeneger"

CRYPTO_API = "https://pay.crypt.bot/api"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not CRYPTO_PAY_TOKEN:
    raise RuntimeError("CRYPTO_PAY_TOKEN is not set")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# =========================
# FSM
# =========================

class TopUpState(StatesGroup):
    waiting_amount = State()


# =========================
# DATABASE
# =========================

async def db():
    return await aiosqlite.connect(DB_PATH)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


async def init_db():
    con = await db()

    await con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            balance REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    await con.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT NOT NULL DEFAULT ''
        )
    """)

    await con.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            login TEXT NOT NULL,
            password TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            sold INTEGER NOT NULL DEFAULT 0,
            sold_to INTEGER,
            sold_at TEXT
        )
    """)

    await con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            inventory_id INTEGER NOT NULL,
            price REAL NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    await con.execute("""
        CREATE TABLE IF NOT EXISTS topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            invoice_id INTEGER NOT NULL UNIQUE,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )
    """)

    cur = await con.execute("SELECT COUNT(*) FROM products")
    count = (await cur.fetchone())[0]

    if count == 0:
        await con.executemany(
            """
            INSERT INTO products (name, price, description)
            VALUES (?, ?, ?)
            """,
            [
                ("Сбер", 59.34, "Игровой банк"),
                ("Альфа", 83.08, "Игровой банк"),
                ("Райф", 71.20, "Игровой банк"),
                ("Газпром", 47.48, "Игровой банк"),
            ],
        )

    await con.commit()
    await con.close()


async def ensure_user(user_id: int, username: str = "", first_name: str = ""):
    con = await db()

    await con.execute(
        """
        INSERT INTO users (user_id, username, first_name, balance, created_at)
        VALUES (?, ?, ?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
        """,
        (user_id, username or "", first_name or "", utc_now()),
    )

    await con.commit()
    await con.close()


async def get_user(user_id: int):
    con = await db()
    cur = await con.execute(
        """
        SELECT user_id, username, first_name, balance
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    )
    row = await cur.fetchone()
    await con.close()
    return row


# =========================
# KEYBOARDS
# =========================

def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
                InlineKeyboardButton(text="💰 Баланс", callback_data="balance"),
            ],
            [
                InlineKeyboardButton(text="🛒 Товары", callback_data="products"),
                InlineKeyboardButton(text="🧾 Покупки", callback_data="purchases"),
            ],
            [
                InlineKeyboardButton(text="💳 Пополнить", callback_data="topup"),
            ],
            [
                InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"),
            ],
        ]
    )


def back_keyboard(callback_data="home"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)]
        ]
    )


def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Товары",
                    callback_data="admin_products",
                ),
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="admin_stats",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ В меню",
                    callback_data="home",
                )
            ],
        ]
    )


# =========================
# SUBSCRIPTION
# =========================

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in {"member", "administrator", "creator"}
    except Exception:
        # Если Telegram не дал проверить подписку,
        # не ломаем работу магазина.
        return True


async def require_subscription(message: Message) -> bool:
    if await is_subscribed(message.from_user.id):
        return True

    channel = REQUIRED_CHANNEL.lstrip("@")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Подписаться",
                    url=f"https://t.me/{channel}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Проверить",
                    callback_data="check_sub",
                )
            ],
        ]
    )

    await message.answer(
        "❗ Для использования бота необходимо подписаться на канал.",
        reply_markup=keyboard,
    )
    return False


# =========================
# CRYPTO PAY
# =========================

async def crypto_request(method: str, data=None):
    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN,
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{CRYPTO_API}/{method}",
            headers=headers,
            json=data or {},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            body = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"Crypto Pay HTTP {response.status}: {body}"
                )

            result = await response.json()

            if not result.get("ok"):
                raise RuntimeError(f"Crypto Pay error: {result}")

            return result["result"]


async def create_invoice(user_id: int, amount: float):
    return await crypto_request(
        "createInvoice",
        {
            "asset": PAY_ASSET,
            "amount": f"{amount:.2f}",
            "description": f"Пополнение баланса пользователя {user_id}",
            "payload": f"topup:{user_id}",
            "expires_in": 1800,
        },
    )


async def get_invoice(invoice_id: int):
    result = await crypto_request(
        "getInvoices",
        {"invoice_ids": str(invoice_id)},
    )

    items = result.get("items", [])
    return items[0] if items else None


# =========================
# START / HOME
# =========================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    await ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
    )

    if not await require_subscription(message):
        return

    await message.answer(
        "👋 Добро пожаловать!\n\nВыбери нужный раздел:",
        reply_markup=main_keyboard(),
    )


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()

    await callback.message.edit_text(
        "🏠 Главное меню:",
        reply_markup=main_keyboard(),
    )


# =========================
# PROFILE / BALANCE
# =========================

@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    await callback.answer()

    user = await get_user(callback.from_user.id)

    if not user:
        await callback.message.edit_text(
            "Пользователь не найден.",
            reply_markup=back_keyboard(),
        )
        return

    user_id, username, first_name, balance = user
    username_text = f"@{html.escape(username)}" if username else "—"

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👨‍💻 Имя: {html.escape(first_name or '—')}\n"
        f"🔹 Username: {username_text}\n"
        f"💰 Баланс: <b>{balance:.2f} USDT</b>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_keyboard(),
    )


@dp.callback_query(F.data == "balance")
async def balance(callback: CallbackQuery):
    await callback.answer()

    user = await get_user(callback.from_user.id)
    amount = user[3] if user else 0.0

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Пополнить",
                    callback_data="topup",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="home",
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "💰 <b>Ваш баланс</b>\n\n"
        f"<b>{amount:.2f} USDT</b>",
        reply_markup=keyboard,
    )


# =========================
# TOP UP
# =========================

@dp.callback_query(F.data == "topup")
async def topup_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(TopUpState.waiting_amount)

    await callback.message.edit_text(
        "💳 <b>Пополнение баланса</b>\n\n"
        f"Минимум: {TOPUP_MIN:.2f} USDT\n"
        f"Максимум: {TOPUP_MAX:.2f} USDT\n\n"
        "Отправь сумму одним сообщением.\n"
        "Например: <code>100</code>",
        reply_markup=back_keyboard(),
    )


@dp.message(TopUpState.waiting_amount)
async def topup_amount(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")

    try:
        amount = float(raw)
    except ValueError:
        await message.answer(
            "❌ Введи сумму числом.\nНапример: <code>100</code>"
        )
        return

    if not math.isfinite(amount):
        await message.answer("❌ Некорректная сумма.")
        return

    if amount < TOPUP_MIN or amount > TOPUP_MAX:
        await message.answer(
            f"❌ Сумма должна быть от "
            f"{TOPUP_MIN:.2f} до {TOPUP_MAX:.2f} USDT."
        )
        return

    try:
        invoice = await create_invoice(
            message.from_user.id,
            amount,
        )
    except Exception:
        logging.exception("Failed to create Crypto Pay invoice")
        await message.answer(
            "❌ Не удалось создать счёт.\n"
            "Попробуй ещё раз позже."
        )
        return

    invoice_id = int(invoice["invoice_id"])

    con = await db()
    await con.execute(
        """
        INSERT INTO topups (
            user_id, invoice_id, amount, status, created_at
        )
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (
            message.from_user.id,
            invoice_id,
            amount,
            utc_now(),
        ),
    )
    await con.commit()
    await con.close()

    await state.clear()

    pay_url = invoice.get("pay_url")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить",
                    url=pay_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить оплату",
                    callback_data=f"checkpay:{invoice_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="home",
                )
            ],
        ]
    )

    await message.answer(
        "💳 <b>Счёт создан.</b>\n\n"
        f"Сумма: <b>{amount:.2f} USDT</b>\n"
        f"Номер счёта: <code>{invoice_id}</code>\n\n"
        "После оплаты нажми «Проверить оплату».",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("checkpay:"))
async def check_payment(callback: CallbackQuery):
    await callback.answer()

    try:
        invoice_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.message.answer("❌ Некорректный счёт.")
        return

    con = await db()

    cur = await con.execute(
        """
        SELECT user_id, amount, status
        FROM topups
        WHERE invoice_id = ?
        """,
        (invoice_id,),
    )
    topup = await cur.fetchone()

    if not topup:
        await con.close()
        await callback.message.answer("❌ Счёт не найден.")
        return

    owner_id, amount, local_status = topup

    if owner_id != callback.from_user.id:
        await con.close()
        await callback.message.answer(
            "❌ Этот счёт принадлежит другому пользователю."
        )
        return

    if local_status == "paid":
        await con.close()
        await callback.message.answer(
            "✅ Этот платёж уже зачислен."
        )
        return

    try:
        invoice = await get_invoice(invoice_id)
    except Exception:
        logging.exception("Failed to check Crypto Pay invoice")
        await con.close()
        await callback.message.answer(
            "❌ Не удалось проверить оплату. "
            "Попробуй ещё раз через несколько секунд."
        )
        return

    if not invoice:
        await con.close()
        await callback.message.answer(
            "❌ Счёт не найден в Crypto Pay."
        )
        return

    if invoice.get("status") != "paid":
        await con.close()
        await callback.message.answer(
            "⏳ Оплата пока не подтверждена."
        )
        return

    if invoice.get("payload") != f"topup:{callback.from_user.id}":
        await con.close()
        await callback.message.answer(
            "❌ Ошибка проверки владельца платежа."
        )
        return

    cur = await con.execute(
        """
        UPDATE topups
        SET status = 'paid'
        WHERE invoice_id = ?
          AND user_id = ?
          AND status = 'pending'
        """,
        (invoice_id, callback.from_user.id),
    )

    if cur.rowcount != 1:
        await con.rollback()
        await con.close()
        await callback.message.answer(
            "✅ Этот платёж уже был обработан."
        )
        return

    await con.execute(
        """
        UPDATE users
        SET balance = balance + ?
        WHERE user_id = ?
        """,
        (amount, callback.from_user.id),
    )

    await con.commit()
    await con.close()

    await callback.message.answer(
        "✅ <b>Оплата подтверждена!</b>\n\n"
        f"Зачислено: <b>{amount:.2f} USDT</b>"
    )


# =========================
# PRODUCTS
# =========================

@dp.callback_query(F.data == "products")
async def products(callback: CallbackQuery):
    await callback.answer()

    con = await db()
    cur = await con.execute(
        """
        SELECT
            p.id,
            p.name,
            p.price,
            COUNT(i.id)
        FROM products p
        LEFT JOIN inventory i
            ON i.product_id = p.id
            AND i.sold = 0
        GROUP BY p.id
        ORDER BY p.id
        """
    )
    rows = await cur.fetchall()
    await con.close()

    buttons = []

    for product_id, name, price, stock in rows:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{name} — {price:.2f} USDT ({stock} шт.)",
                    callback_data=f"product:{product_id}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data="home",
            )
        ]
    )

    await callback.message.edit_text(
        "🛒 <b>Товары</b>\n\nВыбери товар:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )


@dp.callback_query(F.data.startswith("product:"))
async def product(callback: CallbackQuery):
    await callback.answer()

    try:
        product_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return

    con = await db()

    cur = await con.execute(
        """
        SELECT id, name, price, description
        FROM products
        WHERE id = ?
        """,
        (product_id,),
    )
    product_row = await cur.fetchone()

    if not product_row:
        await con.close()
        await callback.message.edit_text(
            "❌ Товар не найден.",
            reply_markup=back_keyboard("products"),
        )
        return

    cur = await con.execute(
        """
        SELECT COUNT(*)
        FROM inventory
        WHERE product_id = ?
          AND sold = 0
        """,
        (product_id,),
    )
    stock = (await cur.fetchone())[0]

    await con.close()

    _, name, price, description = product_row

    text = (
        f"🛒 <b>{html.escape(name)}</b>\n\n"
        f"💵 Цена: <b>{price:.2f} USDT</b>\n"
        f"📦 В наличии: <b>{stock}</b>\n\n"
        f"{html.escape(description or '')}"
    )

    buttons = []

    if stock > 0:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🛍 Купить",
                    callback_data=f"buy:{product_id}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data="products",
            )
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )


# =========================
# PURCHASE
# =========================

@dp.callback_query(F.data.startswith("buy:"))
async def buy(callback: CallbackQuery):
    await callback.answer()

    try:
        product_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return

    user_id = callback.from_user.id

        con = await db()

    try:
        await con.execute("BEGIN IMMEDIATE")

        # Проверяем товар
        cur = await con.execute(
            """
            SELECT id, name, price
            FROM products
            WHERE id = ?
            """,
            (product_id,),
        )
        product_row = await cur.fetchone()

        if not product_row:
            await con.rollback()
            await con.close()
            await callback.message.answer("❌ Товар не найден.")
            return

        _, product_name, price = product_row

        # Проверяем баланс
        cur = await con.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        )
        user_row = await cur.fetchone()

        if not user_row:
            await con.rollback()
            await con.close()
            await callback.message.answer(
                "❌ Пользователь не найден. Нажми /start."
            )
            return

        balance = float(user_row[0])

        if balance < price:
            await con.rollback()
            await con.close()
            await callback.message.answer(
                f"❌ Недостаточно средств.\n\n"
                f"Цена: <b>{price:.2f} USDT</b>\n"
                f"Ваш баланс: <b>{balance:.2f} USDT</b>"
            )
            return

        # Берём первый свободный товар
        cur = await con.execute(
            """
            SELECT id, login, password, phone
            FROM inventory
            WHERE product_id = ?
              AND sold = 0
            ORDER BY id
            LIMIT 1
            """,
            (product_id,),
        )
        item = await cur.fetchone()

        if not item:
            await con.rollback()
            await con.close()
            await callback.message.answer(
                "❌ К сожалению, товара больше нет в наличии."
            )
            return

        inventory_id, login, password, phone = item

        # Списываем деньги
        cur = await con.execute(
            """
            UPDATE users
            SET balance = balance - ?
            WHERE user_id = ?
              AND balance >= ?
            """,
            (price, user_id, price),
        )

        if cur.rowcount != 1:
            await con.rollback()
            await con.close()
            await callback.message.answer(
                "❌ Не удалось списать средства. Попробуй ещё раз."
            )
            return

        # Помечаем товар проданным
        await con.execute(
            """
            UPDATE inventory
            SET sold = 1,
                sold_to = ?,
                sold_at = ?
            WHERE id = ?
              AND sold = 0
            """,
            (user_id, utc_now(), inventory_id),
        )

        # Создаём заказ
        await con.execute(
            """
            INSERT INTO orders
                (user_id, product_id, inventory_id, price, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                product_id,
                inventory_id,
                price,
                utc_now(),
            ),
        )

        await con.commit()

    except Exception:
        await con.rollback()
        logging.exception("Purchase error")
        await con.close()

        await callback.message.answer(
            "❌ Произошла ошибка при покупке. Попробуй ещё раз."
        )
        return

    await con.close()

    # Показываем купленный товар
    text = (
        "✅ <b>Покупка успешно совершена!</b>\n\n"
        f"🛒 Товар: <b>{html.escape(product_name)}</b>\n"
        f"💵 Цена: <b>{price:.2f} USDT</b>\n\n"
        "🔐 <b>Данные товара:</b>\n\n"
        f"👤 Логин: <code>{html.escape(login)}</code>\n"
        f"🔑 Пароль: <code>{html.escape(password)}</code>"
    )

    if phone:
        text += f"\n📱 Телефон: <code>{html.escape(phone)}</code>"

    await callback.message.edit_text(
        text,
        reply_markup=back_keyboard("products"),
    )


# =========================
# PURCHASE HISTORY
# =========================

@dp.callback_query(F.data == "purchases")
async def purchases(callback: CallbackQuery):
    await callback.answer()

    con = await db()

    cur = await con.execute(
        """
        SELECT
            o.id,
            p.name,
            o.price,
            o.created_at
        FROM orders o
        JOIN products p ON p.id = o.product_id
        WHERE o.user_id = ?
        ORDER BY o.id DESC
        LIMIT 20
        """,
        (callback.from_user.id,),
    )

    rows = await cur.fetchall()
    await con.close()

    if not rows:
        await callback.message.edit_text(
            "🧾 <b>Покупки</b>\n\n"
            "У тебя пока нет покупок.",
            reply_markup=back_keyboard(),
        )
        return

    text = "🧾 <b>Последние покупки</b>\n\n"

    for order_id, name, price, created_at in rows:
        text += (
            f"#{order_id} — <b>{html.escape(name)}</b>\n"
            f"💵 {price:.2f} USDT\n"
            f"🕐 {html.escape(created_at[:19])}\n\n"
        )

    await callback.message.edit_text(
        text,
        reply_markup=back_keyboard(),
    )


# =========================
# SUPPORT
# =========================

@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    await callback.answer()

    username = SUPPORT_USERNAME.lstrip("@")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🆘 Написать в поддержку",
                    url=f"https://t.me/{username}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="home",
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "🆘 <b>Поддержка</b>\n\n"
        "Если у тебя возникла проблема с ботом или покупкой, "
        "напиши в поддержку.",
        reply_markup=keyboard,
    )


# =========================
# CHECK SUBSCRIPTION
# =========================

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery):
    await callback.answer()

    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Подписка подтверждена!\n\n"
            "Теперь бот доступен.",
            reply_markup=main_keyboard(),
        )
    else:
        await callback.message.answer(
            "❌ Ты ещё не подписался на канал."
        )


# =========================
# ADMIN
# =========================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return

    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        "Выбери раздел:",
        reply_markup=admin_keyboard(),
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    await callback.answer()

    if not is_admin(callback.from_user.id):
        return

    con = await db()

    cur = await con.execute("SELECT COUNT(*) FROM users")
    users_count = (await cur.fetchone())[0]

    cur = await con.execute(
        "SELECT COUNT(*) FROM orders"
    )
    orders_count = (await cur.fetchone())[0]

    cur = await con.execute(
        "SELECT COALESCE(SUM(price), 0) FROM orders"
    )
    revenue = (await cur.fetchone())[0]

    cur = await con.execute(
        "SELECT COALESCE(SUM(balance), 0) FROM users"
    )
    balances = (await cur.fetchone())[0]

    await con.close()

    await callback.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users_count}</b>\n"
        f"🛒 Покупок: <b>{orders_count}</b>\n"
        f"💰 Продаж: <b>{float(revenue):.2f} USDT</b>\n"
        f"💳 Балансы пользователей: <b>{float(balances):.2f} USDT</b>",
        reply_markup=back_keyboard("admin"),
    )


@dp.callback_query(F.data == "admin_products")
async def admin_products(callback: CallbackQuery):
    await callback.answer()

    if not is_admin(callback.from_user.id):
        return

    con = await db()

    cur = await con.execute(
        """
        SELECT
            p.id,
            p.name,
            p.price,
            COUNT(i.id)
        FROM products p
        LEFT JOIN inventory i
            ON i.product_id = p.id
            AND i.sold = 0
        GROUP BY p.id
        ORDER BY p.id
        """
    )

    rows = await cur.fetchall()
    await con.close()

    text = "📦 <b>Товары</b>\n\n"

    for product_id, name, price, stock in rows:
        text += (
            f"#{product_id} <b>{html.escape(name)}</b>\n"
            f"💵 {price:.2f} USDT\n"
            f"📦 В наличии: {stock}\n\n"
        )

    await callback.message.edit_text(
        text,
        reply_markup=back_keyboard("admin"),
    )


@dp.callback_query(F.data == "admin")
async def admin_menu(callback: CallbackQuery):
    await callback.answer()

    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        "🛠 <b>Админ-панель</b>\n\n"
        "Выбери раздел:",
        reply_markup=admin_keyboard(),
    )


# =========================
# ERROR HANDLER
# =========================

@dp.error()
async def error_handler(event):
    logging.exception("Unhandled bot error", exc_info=event.exception)


# =========================
# START BOT
# =========================

async def main():
    await init_db()

    logging.info("Bot starting...")

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

