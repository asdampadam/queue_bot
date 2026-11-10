"""
Телеграм-бот «Очередь»: 60 мест, до 2 мест на человека, обмен местами,
обновление списка в реальном времени, админ-команды и скрытые команды.

Установка и запуск (нужен Python 3.10+):
    pip install "aiogram>=3.7"
    # Windows (PowerShell):  $env:BOT_TOKEN="123456:ABC..."
    # Linux / macOS:         export BOT_TOKEN="123456:ABC..."
    python main.py

Токен выдаёт @BotFather. В код его НЕ вписываем: либо переменная окружения BOT_TOKEN,
либо файл .env рядом с main.py со строкой BOT_TOKEN=123456:ABC...
"""

import asyncio
import html
import logging
import os
import re
import sqlite3

try:  # локально читаем токен из файла .env (на хостинге он не нужен)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ----------------------------- НАСТРОЙКИ -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # токен берётся ТОЛЬКО из переменной окружения / файла .env
ADMIN_IDS = {7010947964}
# Где хранится база. На Railway при подключённом Volume переменная
# RAILWAY_VOLUME_MOUNT_PATH задаётся автоматически — база кладётся на постоянный диск.
_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = os.getenv("DB_PATH") or (os.path.join(_volume, "queue.db") if _volume else "queue.db")

DEFAULT_TOTAL = 42      # число мест по умолчанию
DEFAULT_MAX_PER_USER = 1    # максимум мест на человека
MAX_TOTAL_LIMIT = 80        # потолок для /setslots (лимит кнопок и длины сообщения Telegram)
NAME_MAX = 30
GRID_COLS = 6
# ---------------------------------------------------------------------

router = Router()
router.message.filter(F.chat.type == "private")

os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

# состояние интерфейса (в памяти)
views: dict[int, dict] = {}       # uid -> {"mode": main|take|release|give|get, "give": n}
renaming: set[int] = set()        # кто сейчас вводит новое имя
last_sig: dict[int, tuple] = {}   # uid -> подпись последнего показанного вида
dirty = asyncio.Event()           # сигнал «надо обновить сообщения у всех»

B = InlineKeyboardButton
NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '\-][^\W\d_]+)+$")


# ----------------------------- БАЗА ДАННЫХ ----------------------------
def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS slots(n INTEGER PRIMARY KEY, owner INTEGER);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS live(
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS offers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            from_slot INTEGER NOT NULL,
            to_slot INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        """
    )
    db.execute("INSERT OR IGNORE INTO settings VALUES('total', ?)", (DEFAULT_TOTAL,))
    db.execute("INSERT OR IGNORE INTO settings VALUES('max_per_user', ?)", (DEFAULT_MAX_PER_USER,))
    db.commit()
    resize_slots(get_setting("total"))


def get_setting(key: str) -> int:
    return db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()["value"]


def set_setting(key: str, value: int):
    with db:
        db.execute("UPDATE settings SET value=? WHERE key=?", (value, key))


def resize_slots(total: int):
    with db:
        for n in range(1, total + 1):
            db.execute("INSERT OR IGNORE INTO slots(n, owner) VALUES(?, NULL)", (n,))
        db.execute("DELETE FROM slots WHERE n > ?", (total,))
        db.execute("UPDATE settings SET value=? WHERE key='total'", (total,))


def get_user(uid: int):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def set_name(uid: int, name: str):
    with db:
        db.execute(
            "INSERT INTO users(id, name, name_key) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, name_key=excluded.name_key",
            (uid, name, name.casefold()),
        )


def validate_name(raw: str, uid: int | None = None):
    """Возвращает (имя, None) или (None, текст ошибки)."""
    name = " ".join(raw.split())
    if len(name) > NAME_MAX:
        return None, f"Слишком длинно (максимум {NAME_MAX} символов)."
    if " " not in name or not NAME_RE.match(name):
        return None, "Нужны имя и фамилия через пробел, только буквы. Например: Иван Петров"
    row = db.execute("SELECT id FROM users WHERE name_key=?", (name.casefold(),)).fetchone()
    if row and row["id"] != uid:
        return None, "Такое имя уже занято другим человеком. Напиши другое."
    return name, None


def find_user(query: str):
    key = " ".join(query.split()).casefold()
    row = db.execute("SELECT * FROM users WHERE name_key=?", (key,)).fetchone()
    if row:
        return row, None
    rows = db.execute("SELECT * FROM users WHERE instr(name_key, ?) > 0", (key,)).fetchall()
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, f"Не нашёл человека «{query}»."
    return None, "Несколько совпадений: " + ", ".join(r["name"] for r in rows[:10])


def slot_exists(n: int) -> bool:
    return db.execute("SELECT 1 FROM slots WHERE n=?", (n,)).fetchone() is not None


def slot_owner(n: int):
    row = db.execute("SELECT owner FROM slots WHERE n=?", (n,)).fetchone()
    return row["owner"] if row else None


def own_slots(uid: int) -> list[int]:
    return [r["n"] for r in db.execute("SELECT n FROM slots WHERE owner=? ORDER BY n", (uid,))]


def take_slot(uid: int, n: int) -> str:
    if len(own_slots(uid)) >= get_setting("max_per_user"):
        return "limit"
    with db:
        cur = db.execute("UPDATE slots SET owner=? WHERE n=? AND owner IS NULL", (uid, n))
    if cur.rowcount == 1:
        return "ok"
    return "taken" if slot_exists(n) else "none"


def release_slot(uid: int, n: int) -> bool:
    with db:
        cur = db.execute("UPDATE slots SET owner=NULL WHERE n=? AND owner=?", (n, uid))
    return cur.rowcount == 1


def swap_owners(a: int, b: int):
    oa, ob = slot_owner(a), slot_owner(b)
    with db:
        db.execute("UPDATE slots SET owner=? WHERE n=?", (ob, a))
        db.execute("UPDATE slots SET owner=? WHERE n=?", (oa, b))


def get_live(uid: int):
    row = db.execute("SELECT chat_id, message_id FROM live WHERE user_id=?", (uid,)).fetchone()
    return (row["chat_id"], row["message_id"]) if row else None


def set_live(uid: int, chat_id: int, message_id: int):
    with db:
        db.execute(
            "INSERT INTO live(user_id, chat_id, message_id) VALUES(?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, message_id=excluded.message_id",
            (uid, chat_id, message_id),
        )


def delete_live(uid: int):
    with db:
        db.execute("DELETE FROM live WHERE user_id=?", (uid,))
    last_sig.pop(uid, None)


# ------------------------------ ОТРИСОВКА -----------------------------
def esc(s: str) -> str:
    return html.escape(s or "")


def grid(items: list, cols: int = GRID_COLS) -> list[list]:
    return [items[i:i + cols] for i in range(0, len(items), cols)]


def render(uid: int):
    rows = db.execute(
        "SELECT s.n AS n, s.owner AS owner, u.name AS name "
        "FROM slots s LEFT JOIN users u ON u.id = s.owner ORDER BY s.n"
    ).fetchall()
    mx = get_setting("max_per_user")
    mine = [r["n"] for r in rows if r["owner"] == uid]
    others = [r["n"] for r in rows if r["owner"] is not None and r["owner"] != uid]
    free = [r["n"] for r in rows if r["owner"] is None]

    st = views.get(uid) or {"mode": "main"}
    mode = st["mode"]
    if mode == "take" and len(mine) >= mx:
        mode = "main"
    elif mode in ("release", "give") and not mine:
        mode = "main"
    elif mode == "get" and (st.get("give") not in mine or not others):
        mode = "main"
    views[uid] = {**st, "mode": mode}

    lines = []
    for r in rows:
        if r["owner"] is None:
            lines.append(f"{r['n']}.")
        elif r["owner"] == uid:
            lines.append(f"{r['n']}. ⭐ <b>{esc(r['name'])}</b>")
        else:
            lines.append(f"{r['n']}. {esc(r['name'])}")

    occupied = len(rows) - len(free)
    head = f"📋 <b>Очередь</b> — занято {occupied}/{len(rows)}"
    if mine:
        mine_line = "⭐ Твои места: " + ", ".join(map(str, mine)) + f" (максимум {mx})" if mx > 1 else "⭐ Твоё место: " + str(mine[0])
    else:
        mine_line = f"У тебя пока нет мест (можно занять до {mx})" if mx > 1 else "У тебя пока нет места"
    text = head + "\n\n" + "\n".join(lines) + "\n\n" + mine_line

    back = [B(text="◀️ Назад", callback_data="m")]
    if mode == "take":
        text += "\n\n➕ <b>Выбери свободное место:</b>" if free else "\n\nСвободных мест нет."
        kb = grid([B(text=str(n), callback_data=f"t:{n}") for n in free]) + [back]
    elif mode == "release":
        text += "\n\n➖ <b>Какое место освободить?</b>"
        kb = grid([B(text=f"Место {n}", callback_data=f"r:{n}") for n in mine], 2) + [back]
    elif mode == "give":
        text += "\n\n🔄 <b>Обмен, шаг 1:</b> какое своё место отдаёшь?"
        kb = grid([B(text=f"Моё место {n}", callback_data=f"g:{n}") for n in mine], 2) + [back]
    elif mode == "get":
        text += (
            f"\n\n🔄 <b>Обмен, шаг 2:</b> ты отдаёшь место {views[uid]['give']}. "
            "С каким чужим местом меняемся?"
        )
        kb = grid([B(text=str(n), callback_data=f"x:{n}") for n in others]) + [back]
    else:
        if uid in renaming:
            text += "\n\n✏️ Напиши новое имя и фамилию следующим сообщением."
        kb = [
            [B(text="➕ Занять", callback_data="take"), B(text="➖ Освободить", callback_data="rel")],
            [B(text="🔄 Обмен", callback_data="trd")],
            [B(text="🔃 Обновить", callback_data="ref")],
        ]
        if uid in renaming:
            kb.append([B(text="✖️ Отмена", callback_data="m")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def push_view(bot: Bot, uid: int, force: bool = False):
    """Обновляет «живое» сообщение пользователя, если оно изменилось."""
    live = get_live(uid)
    if not live or get_user(uid) is None:
        return
    chat_id, mid = live
    text, kb = render(uid)
    sig = (mid, text, kb.model_dump_json())
    if not force and last_sig.get(uid) == sig:
        return
    for _ in range(2):
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=mid, reply_markup=kb)
            last_sig[uid] = sig
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                last_sig[uid] = sig
            else:
                delete_live(uid)
            return
        except TelegramForbiddenError:
            delete_live(uid)
            return


async def send_view(bot: Bot, uid: int, chat_id: int):
    """Отправляет новое «живое» сообщение с очередью (старое удаляется)."""
    views[uid] = {"mode": "main"}
    old = get_live(uid)
    text, kb = render(uid)
    msg = await bot.send_message(chat_id, text, reply_markup=kb)
    set_live(uid, chat_id, msg.message_id)
    last_sig[uid] = (msg.message_id, text, kb.model_dump_json())
    if old and old[1] != msg.message_id:
        try:
            await bot.delete_message(old[0], old[1])
        except Exception:
            pass


async def refresher(bot: Bot):
    """Фоновая задача: как только что-то изменилось — обновляет сообщения у всех."""
    while True:
        await dirty.wait()
        await asyncio.sleep(0.25)  # склеиваем быстрые изменения в одно обновление
        dirty.clear()
        try:
            for r in db.execute("SELECT user_id FROM live").fetchall():
                await push_view(bot, r["user_id"])
                await asyncio.sleep(0.04)
        except Exception:
            logging.exception("refresher error")


async def notify(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


# ------------------------------ ОБМЕН ---------------------------------
async def propose(bot: Bot, uid: int, give: int, target: int):
    """Возвращает (текст для уведомления, это alert?)."""
    if slot_owner(give) != uid:
        return "Это место уже не твоё.", True
    to_user = slot_owner(target)
    if to_user is None or to_user == uid:
        return "С этим местом обменяться нельзя (оно свободно или твоё).", True
    dup = db.execute(
        "SELECT 1 FROM offers WHERE status='pending' AND from_user=? AND from_slot=? AND to_slot=?",
        (uid, give, target),
    ).fetchone()
    if dup:
        return "Ты уже отправил такое предложение — ждём ответа.", True
    with db:
        cur = db.execute(
            "INSERT INTO offers(from_user, to_user, from_slot, to_slot) VALUES(?, ?, ?, ?)",
            (uid, to_user, give, target),
        )
    oid = cur.lastrowid
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            B(text="✅ Принять", callback_data=f"o:a:{oid}"),
            B(text="❌ Отклонить", callback_data=f"o:d:{oid}"),
        ]]
    )
    text = (
        f"🔄 <b>{esc(get_user(uid)['name'])}</b> предлагает обмен:\n"
        f"твоё место <b>{target}</b> ⇄ его место <b>{give}</b>"
    )
    try:
        await bot.send_message(to_user, text, reply_markup=kb)
    except (TelegramForbiddenError, TelegramBadRequest):
        with db:
            db.execute("UPDATE offers SET status='void' WHERE id=?", (oid,))
        return "Не удалось отправить предложение (человек недоступен для бота).", True
    return f"📨 Предложение отправлено: твоё место {give} ⇄ место {target}", False


async def safe_edit(msg, text: str):
    try:
        await msg.edit_text(text, reply_markup=None)
    except Exception:
        pass


async def on_offer_reply(cb: CallbackQuery, bot: Bot):
    try:
        _, act, sid = cb.data.split(":")
        oid = int(sid)
    except ValueError:
        await cb.answer()
        return
    offer = db.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone()
    if not offer or offer["to_user"] != cb.from_user.id:
        await cb.answer("Предложение не найдено.", show_alert=True)
        return
    if offer["status"] != "pending":
        await cb.answer("Это предложение уже неактуально.")
        if cb.message:
            await safe_edit(cb.message, "⌛ Предложение уже неактуально.")
        return

    fu, tu, fs, ts = offer["from_user"], offer["to_user"], offer["from_slot"], offer["to_slot"]
    me_name = esc(get_user(tu)["name"]) if get_user(tu) else "Пользователь"

    if act == "d":
        with db:
            db.execute("UPDATE offers SET status='declined' WHERE id=?", (oid,))
        await cb.answer("Отклонено")
        if cb.message:
            await safe_edit(cb.message, f"❌ Ты отклонил обмен: место {ts} ⇄ место {fs}.")
        await notify(bot, fu, f"❌ <b>{me_name}</b> отклонил обмен (твоё место {fs} ⇄ его место {ts}).")
        return

    # принять
    if slot_owner(fs) != fu or slot_owner(ts) != tu:
        with db:
            db.execute("UPDATE offers SET status='void' WHERE id=?", (oid,))
        await cb.answer("Места уже изменились — обмен невозможен.", show_alert=True)
        if cb.message:
            await safe_edit(cb.message, "⚠️ Предложение устарело: места уже изменились.")
        return

    swap_owners(fs, ts)
    with db:
        db.execute("UPDATE offers SET status='accepted' WHERE id=?", (oid,))
    await cb.answer("Обмен выполнен ✅")
    if cb.message:
        await safe_edit(cb.message, f"✅ Обмен выполнен: теперь у тебя место {fs}.")
    await notify(bot, fu, f"✅ <b>{me_name}</b> принял обмен! Теперь у тебя место {ts}.")
    dirty.set()


# ------------------------- ОБЫЧНЫЕ КОМАНДЫ ----------------------------
@router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot):
    uid = m.from_user.id
    renaming.discard(uid)
    if get_user(uid) is None:
        await m.answer(
            "👋 Привет! Это бот для записи в очередь.\n\n"
            "Для начала напиши свои <b>имя и фамилию</b> одним сообщением "
            "(например: Иван Петров)."
        )
        return
    await send_view(bot, uid, m.chat.id)


@router.message(Command("queue"))
async def cmd_queue(m: Message, bot: Bot):
    uid = m.from_user.id
    if get_user(uid) is None:
        await m.answer("Сначала напиши /start и представься.")
        return
    renaming.discard(uid)
    await send_view(bot, uid, m.chat.id)


async def apply_name(m: Message, bot: Bot, raw: str) -> bool:
    uid = m.from_user.id
    name, err = validate_name(raw, uid)
    if err:
        await m.answer("⚠️ " + err)
        return False
    set_name(uid, name)
    renaming.discard(uid)
    dirty.set()
    return True


@router.message(Command("changename", "name"))
async def cmd_name(m: Message, command: CommandObject, bot: Bot):
    uid = m.from_user.id
    if get_user(uid) is None:
        await m.answer("Сначала напиши /start и представься.")
        return
    if command.args:
        if await apply_name(m, bot, command.args):
            await m.answer("✅ Имя изменено.")
            await send_view(bot, uid, m.chat.id)
    else:
        renaming.add(uid)
        await m.answer("✏️ Напиши новое имя и фамилию следующим сообщением.")


# ------------------------- СКРЫТЫЕ КОМАНДЫ ----------------------------
# Нигде не показываются. Работают ТОЛЬКО для админов (ADMIN_IDS).
@router.message(Command("put"))
async def sec_put(m: Message, command: CommandObject):
    """/put <место> <имя> — поставить человека на место (без проверки лимита)."""
    if not is_admin(m):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await m.answer("Формат: /put <место> <имя фамилия>")
        return
    n = int(parts[0])
    if not slot_exists(n):
        await m.answer("Такого места нет.")
        return
    user, err = find_user(parts[1])
    if not user:
        await m.answer(err)
        return
    with db:
        db.execute("UPDATE slots SET owner=? WHERE n=?", (user["id"], n))
    dirty.set()
    await m.answer(f"✅ Место {n} → {user['name']}")


@router.message(Command("free"))
async def sec_free(m: Message, command: CommandObject):
    """/free <место> — освободить место."""
    if not is_admin(m):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or not slot_exists(int(arg)):
        await m.answer("Формат: /free <место>")
        return
    with db:
        db.execute("UPDATE slots SET owner=NULL WHERE n=?", (int(arg),))
    dirty.set()
    await m.answer(f"✅ Место {arg} освобождено.")


@router.message(Command("swap"))
async def sec_swap(m: Message, command: CommandObject):
    """/swap <место1> <место2> — поменять владельцев двух мест (без согласия)."""
    if not is_admin(m):
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await m.answer("Формат: /swap <место1> <место2>")
        return
    a, b = int(parts[0]), int(parts[1])
    if a == b or not slot_exists(a) or not slot_exists(b):
        await m.answer("Проверь номера мест.")
        return
    swap_owners(a, b)
    dirty.set()
    await m.answer(f"✅ Места {a} и {b} поменялись.")


@router.message(Command("rename"))
async def sec_rename(m: Message, command: CommandObject):
    """/rename <старое имя> | <новое имя> — переименовать любого человека."""
    if not is_admin(m):
        return
    if not command.args or "|" not in command.args:
        await m.answer("Формат: /rename Старое Имя | Новое Имя")
        return
    old, new = (s.strip() for s in command.args.split("|", 1))
    user, err = find_user(old)
    if not user:
        await m.answer(err)
        return
    name, err = validate_name(new, user["id"])
    if err:
        await m.answer("⚠️ " + err)
        return
    set_name(user["id"], name)
    dirty.set()
    await m.answer(f"✅ {user['name']} → {name}")


# ------------------------- АДМИН-КОМАНДЫ ------------------------------
def is_admin(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id in ADMIN_IDS)


@router.message(Command("admin"))
async def adm_help(m: Message):
    if not is_admin(m):
        return
    await m.answer(
        "🛠 <b>Админ-команды</b>\n"
        "/clear yes — очистить всю очередь\n"
        f"/setslots N — число мест (1–{MAX_TOTAL_LIMIT})\n"
        "/setmax N — максимум мест на человека\n"
        "/users — список зарегистрированных\n\n"
        "<b>Скрытые админские</b>\n"
        "/put &lt;место&gt; &lt;имя&gt; — поставить человека на место\n"
        "/free &lt;место&gt; — освободить место\n"
        "/swap &lt;м1&gt; &lt;м2&gt; — поменять два места\n"
        "/rename Старое | Новое — переименовать человека\n\n"
        "<b>Скрытая для всех пользователей</b>\n"
        "/changename Новое Имя — человек меняет своё имя"
    )


@router.message(Command("clear"))
async def adm_clear(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    if (command.args or "").strip().lower() != "yes":
        await m.answer("Это уберёт всех из очереди. Подтверди командой: /clear yes")
        return
    with db:
        db.execute("UPDATE slots SET owner=NULL")
        db.execute("UPDATE offers SET status='void' WHERE status='pending'")
    dirty.set()
    await m.answer("✅ Очередь очищена.")


@router.message(Command("setslots"))
async def adm_setslots(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or not (1 <= int(arg) <= MAX_TOTAL_LIMIT):
        await m.answer(f"Формат: /setslots N (от 1 до {MAX_TOTAL_LIMIT})")
        return
    resize_slots(int(arg))
    dirty.set()
    await m.answer(f"✅ Теперь мест: {arg}. (Если уменьшал — люди на убранных местах потеряли их.)")


@router.message(Command("setmax"))
async def adm_setmax(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or not (1 <= int(arg) <= 10):
        await m.answer("Формат: /setmax N (от 1 до 10)")
        return
    set_setting("max_per_user", int(arg))
    dirty.set()
    await m.answer(f"✅ Максимум мест на человека: {arg}.")


@router.message(Command("users"))
async def adm_users(m: Message):
    if not is_admin(m):
        return
    rows = db.execute("SELECT id, name FROM users ORDER BY name").fetchall()
    if not rows:
        await m.answer("Пока никто не зарегистрировался.")
        return
    lines = []
    for r in rows:
        slots = own_slots(r["id"])
        lines.append(f"{r['name']} ({r['id']}) — " + (", ".join(map(str, slots)) if slots else "нет мест"))
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await m.answer(text[i:i + 3500], parse_mode=None)


# ----------------------- ТЕКСТ (имя) И КНОПКИ -------------------------
@router.message(F.text, ~F.text.startswith("/"))
async def on_text(m: Message, bot: Bot):
    uid = m.from_user.id
    if get_user(uid) is None:
        if await apply_name(m, bot, m.text):
            await m.answer(f"✅ Приятно познакомиться, <b>{esc(get_user(uid)['name'])}</b>!")
            await send_view(bot, uid, m.chat.id)
    elif uid in renaming:
        if await apply_name(m, bot, m.text):
            await m.answer("✅ Имя изменено.")
            await send_view(bot, uid, m.chat.id)


def to_int(s: str):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


@router.callback_query(F.data)
async def on_cb(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    data = cb.data

    if data.startswith("o:"):
        await on_offer_reply(cb, bot)
        return
    if get_user(uid) is None:
        await cb.answer("Сначала напиши /start и представься.", show_alert=True)
        return
    if cb.message is None:
        await cb.answer()
        return

    # сообщение, на которое нажали, становится «живым»
    set_live(uid, cb.message.chat.id, cb.message.message_id)

    action, _, arg = data.partition(":")
    mx = get_setting("max_per_user")
    note, alert, force = None, False, False
    if action != "ren":
        renaming.discard(uid)

    if action == "m":
        views[uid] = {"mode": "main"}

    elif action == "ref":
        force = True

    elif action == "ren":
        renaming.add(uid)
        views[uid] = {"mode": "main"}

    elif action == "take":
        if len(own_slots(uid)) >= mx:
            note, alert = "У тебя уже есть место. Сначала освободи его.", True
        else:
            views[uid] = {"mode": "take"}

    elif action == "t":
        n = to_int(arg)
        res = take_slot(uid, n) if n is not None else "none"
        if res == "ok":
            views[uid] = {"mode": "main"}
            note = f"✅ Место {n} теперь твоё"
        elif res == "limit":
            views[uid] = {"mode": "main"}
            note, alert = "У тебя уже есть место.", True
        elif res == "taken":
            note, alert = "😔 Это место только что заняли.", True
        else:
            note, alert = "Такого места нет.", True

    elif action == "rel":
        if not own_slots(uid):
            note, alert = "У тебя нет мест.", True
        else:
            views[uid] = {"mode": "release"}

    elif action == "r":
        n = to_int(arg)
        if n is not None and release_slot(uid, n):
            note = f"Место {n} освобождено"
        else:
            note, alert = "Это место уже не твоё.", True
        views[uid] = {"mode": "main"}

    elif action == "trd":
        mine = own_slots(uid)
        others = db.execute(
            "SELECT 1 FROM slots WHERE owner IS NOT NULL AND owner != ? LIMIT 1", (uid,)
        ).fetchone()
        if not mine:
            note, alert = "Сначала займи место — потом можно меняться.", True
        elif not others:
            note, alert = "Пока нет чужих занятых мест для обмена.", True
        else:
            views[uid] = {"mode": "give"}

    elif action == "g":
        n = to_int(arg)
        if n is not None and slot_owner(n) == uid:
            views[uid] = {"mode": "get", "give": n}
        else:
            views[uid] = {"mode": "main"}
            note, alert = "Это место уже не твоё.", True

    elif action == "x":
        st = views.get(uid) or {}
        target = to_int(arg)
        if st.get("mode") != "get" or st.get("give") is None or target is None:
            views[uid] = {"mode": "main"}
            note, alert = "Начни обмен заново.", True
        else:
            note, alert = await propose(bot, uid, st["give"], target)
            views[uid] = {"mode": "main"}

    await cb.answer(note, show_alert=alert)
    await push_view(bot, uid, force=force)
    dirty.set()


# ------------------------------- ЗАПУСК -------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не найден токен. Задай переменную окружения BOT_TOKEN (или файл .env с BOT_TOKEN=...).")
    init_db()
    logging.info("База данных: %s", os.path.abspath(DB_PATH))
    if any(k.startswith("RAILWAY_") for k in os.environ) and not _volume and not os.getenv("DB_PATH"):
        logging.warning(
            "ВНИМАНИЕ: запуск на Railway без Volume — база сотрётся при следующем деплое! "
            "Подключи Volume (Mount Path, например /data)."
        )
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть очередь"),
        BotCommand(command="queue", description="Показать очередь заново"),
    ])
    task = asyncio.create_task(refresher(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
