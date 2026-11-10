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
import time
from datetime import date, datetime, timedelta, timezone

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

DEFAULT_TOTAL = 60          # число мест по умолчанию
DEFAULT_MAX_PER_USER = 1    # максимум мест на человека
KZ_TZ_HOURS = 5               # Казахстан: единый часовой пояс UTC+5 (с марта 2024 года)
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
    # окно записи: win_mode 0 = выключено, 1 = каждый день, 2 = один раз (от и до)
    db.execute("INSERT OR IGNORE INTO settings VALUES('win_mode', 0)")
    db.execute("INSERT OR IGNORE INTO settings VALUES('win_start', 0)")
    db.execute("INSERT OR IGNORE INTO settings VALUES('win_end', 0)")
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


# --------------------------- ОКНО ЗАПИСИ ------------------------------
def tz() -> timezone:
    return timezone(timedelta(hours=KZ_TZ_HOURS))


def fmt_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz()).strftime("%d.%m %H:%M")


def tz_label() -> str:
    return f"Казахстан (UTC+{KZ_TZ_HOURS})"


def window_state():
    """Возвращает (открыта_ли_запись, строка_статуса_для_списка, причина_для_отказа)."""
    mode = get_setting("win_mode")
    if mode == 0:
        return True, "", ""
    if mode == 3:
        return False, "🔒 Запись закрыта администратором", "Запись закрыта администратором."
    start, end = get_setting("win_start"), get_setting("win_end")
    now = time.time()
    if mode == 1:  # каждый день
        loc = datetime.fromtimestamp(now, tz())
        cur = loc.hour * 60 + loc.minute
        is_open = (start <= cur < end) if start < end else (cur >= start or cur < end)
        if is_open:
            return True, f"🟢 Запись открыта до {fmt_hm(end)}", ""
        return (
            False,
            f"🔒 Запись закрыта. Откроется в {fmt_hm(start)}",
            f"Запись идёт ежедневно с {fmt_hm(start)} до {fmt_hm(end)}.",
        )
    # mode == 2: один раз, от и до
    if now < start:
        return False, f"🔒 Запись откроется {fmt_dt(start)}", f"Запись откроется {fmt_dt(start)}."
    if now >= end:
        return False, "🔒 Запись завершена", "Запись уже завершена."
    return True, f"🟢 Запись открыта до {fmt_dt(end)}", ""


def describe_window() -> str:
    mode = get_setting("win_mode")
    if mode == 0:
        return "выключено (записываться можно всегда)"
    if mode == 3:
        return "закрыта вручную"
    start, end = get_setting("win_start"), get_setting("win_end")
    if mode == 1:
        return f"каждый день с {fmt_hm(start)} до {fmt_hm(end)}"
    return f"с {fmt_dt(start)} до {fmt_dt(end)}"


def parse_hm(s: str):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if h < 24 and mi < 60 else None


def parse_dt(date_s: str, time_s: str):
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", date_s)
    hm = parse_hm(time_s)
    if not m or hm is None:
        return None
    year = datetime.now(tz()).year
    try:
        return int(datetime(year, int(m.group(2)), int(m.group(1)), hm // 60, hm % 60, tzinfo=tz()).timestamp())
    except ValueError:
        return None


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
    if mode == "take" and (len(mine) >= mx or not window_state()[0]):
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
    win_line = window_state()[1]
    if win_line:
        head += "\n" + win_line
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


async def window_watcher():
    """Когда запись открывается/закрывается — обновляет сообщения у всех."""
    last = window_state()[0]
    while True:
        await asyncio.sleep(15)
        cur = window_state()[0]
        if cur != last:
            last = cur
            dirty.set()


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
    text, markup = panel_main()
    await m.answer(text, reply_markup=markup)


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


@router.message(Command("window"))
async def adm_window(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    args = (command.args or "").split()
    if not args:
        now_local = datetime.now(tz()).strftime("%H:%M (%d.%m)")
        status = window_state()[1] or "ограничений нет"
        await m.answer(
            f"🕒 <b>Время записи:</b> {describe_window()}\n"
            f"Статус сейчас: {status}\n"
            f"Часовой пояс: {tz_label()}, у бота сейчас {now_local}\n\n"
            "<b>Как настроить:</b>\n"
            "<code>/window 18:00 20:00</code> — каждый день с 18:00 до 20:00\n"
            "<code>/window 15.11 18:00 15.11 20:00</code> — один раз, от даты и времени до даты и времени\n"
            "<code>/window off</code> — убрать ограничение\n"
            "Проще всего настраивать время кнопками: /admin → «Время записи»."
        )
        return
    if args[0].lower() in ("off", "выкл"):
        set_setting("win_mode", 0)
        dirty.set()
        await m.answer("✅ Ограничение по времени выключено. Записываться можно всегда.")
        return
    if len(args) == 2:
        a, b = parse_hm(args[0]), parse_hm(args[1])
        if a is None or b is None or a == b:
            await m.answer("Формат: /window 18:00 20:00 (начало и конец, время разное)")
            return
        mode = 1
    elif len(args) == 4:
        a, b = parse_dt(args[0], args[1]), parse_dt(args[2], args[3])
        if a is None or b is None:
            await m.answer("Формат: /window 15.11 18:00 15.11 20:00 (дата ДД.ММ и время ЧЧ:ММ)")
            return
        if b <= a:
            await m.answer("Конец должен быть позже начала.")
            return
        mode = 2
    else:
        await m.answer("Напиши /window без параметров — покажу, как настроить.")
        return
    set_setting("win_start", a)
    set_setting("win_end", b)
    set_setting("win_mode", mode)
    dirty.set()
    await m.answer(f"✅ Запись разрешена: {describe_window()}.\n{window_state()[1]}")


# ----------------------------- АДМИН-ПАНЕЛЬ ---------------------------
wiz: dict[int, dict] = {}        # админ -> состояние мастера выбора времени
adm_rename: dict[int, int] = {}  # админ -> id человека, которого переименовываем
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
PAGE = 8


def mk(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def fmt_ord(o: int) -> str:
    return date.fromordinal(o).strftime("%d.%m")


def now_local_str() -> str:
    return datetime.now(tz()).strftime("%H:%M, %d.%m")


def slot_lines() -> str:
    rows = db.execute(
        "SELECT s.n AS n, s.owner AS owner, u.name AS name "
        "FROM slots s LEFT JOIN users u ON u.id = s.owner ORDER BY s.n"
    ).fetchall()
    return "\n".join(
        f"{r['n']}. {esc(r['name'])}" if r["owner"] is not None else f"{r['n']}." for r in rows
    )


def panel_main():
    total, mx = get_setting("total"), get_setting("max_per_user")
    occ = db.execute("SELECT COUNT(*) AS c FROM slots WHERE owner IS NOT NULL").fetchone()["c"]
    users = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    status = window_state()[1] or "🟢 Ограничений по времени нет"
    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        f"Места: занято {occ} из {total}\n"
        f"Максимум на человека: {mx}\n"
        f"Людей в боте: {users}\n\n"
        f"🕒 Запись: {describe_window()}\n{status}\n"
        f"Сейчас {now_local_str()} · {tz_label()}"
    )
    rows = [
        [btn("🕒 Время записи", "a:t"), btn("🧩 Места", "a:s")],
        [btn("👥 Люди", "a:u:0"), btn("⚙️ Настройки", "a:g")],
        [btn("🧹 Очистить очередь", "a:c")],
        [btn("📖 Команды", "a:help"), btn("✖️ Закрыть", "a:x")],
    ]
    return text, mk(rows)


def panel_time():
    status = window_state()[1] or "🟢 Ограничений по времени нет"
    text = (
        "🕒 <b>Время записи</b>\n\n"
        f"Сейчас: {describe_window()}\n{status}\n"
        f"Время по: {tz_label()}, сейчас {now_local_str()}\n\n"
        "Выбери действие:"
    )
    rows = [
        [btn("▶️ Открыть на 30 мин", "a:q:30"), btn("▶️ на 1 час", "a:q:60")],
        [btn("▶️ на 2 часа", "a:q:120"), btn("▶️ на 3 часа", "a:q:180")],
        [btn("⏹ Закрыть запись", "a:cl"), btn("♾ Без ограничений", "a:op")],
        [btn("📅 Каждый день…", "a:wd"), btn("📆 Один раз…", "a:wo")],
        [btn("◀️ Назад", "a:home")],
    ]
    return text, mk(rows)


def panel_wiz(uid: int):
    w = wiz.get(uid)
    if not w:
        return panel_time()
    step = w["step"]
    daily = w["kind"] == "d"
    today = datetime.now(tz()).date().toordinal()

    def part(prefix: str) -> str:
        bits = []
        if not daily and prefix + "d" in w:
            bits.append(fmt_ord(w[prefix + "d"]))
        if prefix + "h" in w:
            minutes = f"{w[prefix + 'm']:02d}" if prefix + "m" in w else "__"
            bits.append(f"{w[prefix + 'h']:02d}:{minutes}")
        return " ".join(bits)

    lines = ["📅 <b>Каждый день</b>" if daily else "📆 <b>Один раз</b>", ""]
    s_txt, e_txt = part("s"), part("e")
    if s_txt:
        lines.append(f"Начало: {s_txt}")
    if e_txt:
        lines.append(f"Конец: {e_txt}")
    if s_txt or e_txt:
        lines.append("")
    prompts = {
        "sd": "Выбери <b>дату начала</b>:", "sh": "Выбери <b>час начала</b>:",
        "sm": "Выбери <b>минуты начала</b>:", "ed": "Выбери <b>дату конца</b>:",
        "eh": "Выбери <b>час конца</b>:", "em": "Выбери <b>минуты конца</b>:",
    }
    lines.append(prompts[step])

    if step in ("sd", "ed"):
        first = today if step == "sd" else w["sd"]
        buttons = []
        for o in range(first, first + 14):
            d = date.fromordinal(o)
            if o == today:
                label = f"Сегодня {d.day:02d}.{d.month:02d}"
            elif o == today + 1:
                label = f"Завтра {d.day:02d}.{d.month:02d}"
            else:
                label = f"{d.day:02d}.{d.month:02d} {WEEKDAYS[d.weekday()]}"
            buttons.append(btn(label, f"a:d:{o}"))
        rows = grid(buttons, 2)
    elif step in ("sh", "eh"):
        rows = grid([btn(f"{h:02d}", f"a:h:{h}") for h in range(24)], 6)
    else:
        rows = grid([btn(f"{mi:02d}", f"a:m:{mi}") for mi in range(0, 60, 5)], 6)
    rows.append([btn("✖️ Отмена", "a:t")])
    return "\n".join(lines), mk(rows)


def wiz_ts(o: int, h: int, mi: int) -> int:
    d = date.fromordinal(o)
    return int(datetime(d.year, d.month, d.day, h, mi, tzinfo=tz()).timestamp())


def wiz_finish(uid: int):
    """Сохраняет окно из мастера. Возвращает текст ошибки или None."""
    w = wiz[uid]
    if w["kind"] == "d":
        a, b = w["sh"] * 60 + w["sm"], w["eh"] * 60 + w["em"]
        if a == b:
            return "Начало и конец совпадают."
        mode = 1
    else:
        a, b = wiz_ts(w["sd"], w["sh"], w["sm"]), wiz_ts(w["ed"], w["eh"], w["em"])
        if b <= a:
            return "Конец должен быть позже начала."
        mode = 2
    set_setting("win_start", a)
    set_setting("win_end", b)
    set_setting("win_mode", mode)
    return None


def panel_slots():
    buttons = []
    for r in db.execute("SELECT n, owner FROM slots ORDER BY n"):
        buttons.append(btn(f"{r['n']}●" if r["owner"] is not None else str(r["n"]), f"a:sl:{r['n']}"))
    text = (
        "🧩 <b>Места</b>\n\n" + slot_lines() +
        "\n\nНажми на номер, чтобы управлять местом (● — занято)."
    )
    return text, mk(grid(buttons) + [[btn("◀️ Назад", "a:home")]])


def panel_slot(n: int):
    if not slot_exists(n):
        return panel_slots()
    owner = slot_owner(n)
    if owner is None:
        who = "свободно"
    else:
        u = get_user(owner)
        who = esc(u["name"]) if u else str(owner)
    text = f"🧩 <b>Место {n}</b>\nСейчас: {who}"
    rows = []
    if owner is not None:
        rows.append([btn("🗑 Освободить", f"a:f:{n}")])
    rows.append([btn("👤 Поставить человека", f"a:pp:{n}:0")])
    rows.append([btn("🔀 Поменять с другим местом", f"a:sw:{n}")])
    rows.append([btn("◀️ К местам", "a:s")])
    return text, mk(rows)


def panel_swap(n: int):
    buttons = [btn(str(r["n"]), f"a:sx:{n}:{r['n']}") for r in db.execute("SELECT n FROM slots WHERE n != ? ORDER BY n", (n,))]
    text = f"🔀 <b>Поменять место {n}</b> с другим местом.\n\n" + slot_lines() + "\n\nВыбери номер:"
    return text, mk(grid(buttons) + [[btn("◀️ Назад", f"a:sl:{n}")]])


def people_picker(page: int, pick_prefix: str, nav_prefix: str, back: str, title: str):
    users = db.execute("SELECT id, name FROM users ORDER BY name").fetchall()
    pages = max(1, (len(users) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for u in users[page * PAGE:(page + 1) * PAGE]:
        sl = own_slots(u["id"])
        label = u["name"] + (" · " + ", ".join(map(str, sl)) if sl else "")
        rows.append([btn(label, f"{pick_prefix}:{u['id']}")])
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"{nav_prefix}:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "a:noop"))
    if page < pages - 1:
        nav.append(btn("▶️", f"{nav_prefix}:{page + 1}"))
    rows.append(nav)
    rows.append([btn("◀️ Назад", back)])
    text = title + "\n\n" + (f"Всего людей: {len(users)}" if users else "Пока никто не зарегистрировался.")
    return text, mk(rows)


def panel_person(uid2: int):
    u = get_user(uid2)
    if not u:
        return people_picker(0, "a:up", "a:u", "a:home", "👥 <b>Люди</b>\nВыбери человека:")
    sl = own_slots(uid2)
    text = (
        f"👤 <b>{esc(u['name'])}</b>\nID: {uid2}\n"
        "Места: " + (", ".join(map(str, sl)) if sl else "нет")
    )
    rows = [[btn("✏️ Переименовать", f"a:ur:{uid2}")]]
    if sl:
        rows.append([btn("🗑 Снять со всех мест", f"a:uf:{uid2}")])
    rows.append([btn("◀️ К людям", "a:u:0")])
    return text, mk(rows)


def panel_settings():
    total, mx = get_setting("total"), get_setting("max_per_user")
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Мест в очереди: {total}\nМаксимум мест на человека: {mx}\n\n"
        "Места нельзя убрать, пока на них кто-то записан."
    )
    rows = [
        [btn("−10", "a:ns:-10"), btn("−1", "a:ns:-1"), btn(f"Мест: {total}", "a:noop"),
         btn("+1", "a:ns:1"), btn("+10", "a:ns:10")],
        [btn("−", "a:nm:-1"), btn(f"Макс. на человека: {mx}", "a:noop"), btn("+", "a:nm:1")],
        [btn("◀️ Назад", "a:home")],
    ]
    return text, mk(rows)


def admin_commands_text() -> str:
    return (
        "📖 <b>Команды админа</b> (всё то же самое есть в кнопках панели)\n\n"
        "/admin — открыть панель\n"
        "/put &lt;место&gt; &lt;имя&gt; — поставить человека на место\n"
        "/free &lt;место&gt; — освободить место\n"
        "/swap &lt;м1&gt; &lt;м2&gt; — поменять два места\n"
        "/rename Старое | Новое — переименовать человека\n"
        "/window 18:00 20:00 — запись каждый день в это время\n"
        "/window 15.11 18:00 15.11 20:00 — один раз\n"
        "/window off — без ограничений\n"
        "/clear yes — очистить очередь\n"
        "/setslots N — число мест\n"
        "/setmax N — максимум мест на человека\n"
        "/users — список людей\n\n"
        "<b>Скрытая для всех:</b> /changename Новое Имя — человек меняет своё имя"
    )


async def panel_edit(msg, view):
    text, markup = view
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logging.warning("panel edit failed: %s", e)


@router.callback_query(F.data.startswith("a:"))
async def on_admin_cb(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    if uid not in ADMIN_IDS or cb.message is None:
        await cb.answer()
        return
    parts = cb.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    nums = [to_int(p) for p in parts[2:]]
    a0 = nums[0] if len(nums) > 0 else None
    a1 = nums[1] if len(nums) > 1 else None
    note, alert, view = None, False, None

    if act != "ur":
        adm_rename.pop(uid, None)
    if act not in ("d", "h", "m", "wd", "wo"):
        wiz.pop(uid, None)

    if act == "noop":
        await cb.answer()
        return

    if act == "x":
        await cb.answer()
        try:
            await cb.message.delete()
        except Exception:
            pass
        return

    if act == "home":
        view = panel_main()

    elif act == "help":
        view = (admin_commands_text(), mk([[btn("◀️ Назад", "a:home")]]))

    # ----- время записи -----
    elif act == "t":
        view = panel_time()

    elif act == "q" and a0 in (30, 60, 120, 180):
        now = int(time.time())
        set_setting("win_start", now)
        set_setting("win_end", now + a0 * 60)
        set_setting("win_mode", 2)
        dirty.set()
        note = f"✅ Запись открыта на {a0} мин"
        view = panel_time()

    elif act == "cl":
        set_setting("win_mode", 3)
        dirty.set()
        note = "🔒 Запись закрыта"
        view = panel_time()

    elif act == "op":
        set_setting("win_mode", 0)
        dirty.set()
        note = "✅ Ограничение по времени выключено"
        view = panel_time()

    elif act in ("wd", "wo"):
        wiz[uid] = {"kind": "d", "step": "sh"} if act == "wd" else {"kind": "o", "step": "sd"}
        view = panel_wiz(uid)

    elif act in ("d", "h", "m"):
        w = wiz.get(uid)
        today = datetime.now(tz()).date().toordinal()
        valid = (
            (act == "d" and a0 is not None and today <= a0 <= today + 40)
            or (act == "h" and a0 is not None and 0 <= a0 <= 23)
            or (act == "m" and a0 is not None and 0 <= a0 <= 59)
        )
        if not w or not valid:
            note, alert, view = "Начни заново.", True, panel_time()
        else:
            step = w["step"]
            if act == "d" and step in ("sd", "ed"):
                w[step] = a0
                w["step"] = "sh" if step == "sd" else "eh"
            elif act == "h" and step in ("sh", "eh"):
                w[step] = a0
                w["step"] = step[0] + "m"
            elif act == "m" and step in ("sm", "em"):
                w[step] = a0
                if step == "sm":
                    w["step"] = "ed" if w["kind"] == "o" else "eh"
                else:
                    err = wiz_finish(uid)
                    if err:
                        w["step"] = "eh"
                        note, alert = err + " Выбери конец ещё раз.", True
                    else:
                        wiz.pop(uid, None)
                        dirty.set()
                        note = "✅ Время записи сохранено"
                        view = panel_time()
            if view is None:
                view = panel_wiz(uid)

    # ----- места -----
    elif act == "s":
        view = panel_slots()

    elif act == "sl" and a0 is not None:
        view = panel_slot(a0)

    elif act == "f" and a0 is not None:
        with db:
            db.execute("UPDATE slots SET owner=NULL WHERE n=?", (a0,))
        dirty.set()
        note = f"Место {a0} освобождено"
        view = panel_slot(a0)

    elif act == "pp" and a0 is not None and a1 is not None:
        view = people_picker(a1, f"a:pu:{a0}", f"a:pp:{a0}", f"a:sl:{a0}", f"👤 <b>Кого поставить на место {a0}?</b>")

    elif act == "pu" and a0 is not None and a1 is not None:
        u = get_user(a1)
        if not slot_exists(a0) or not u:
            note, alert = "Место или человек не найдены.", True
        else:
            with db:
                db.execute("UPDATE slots SET owner=? WHERE n=?", (a1, a0))
            dirty.set()
            note = f"✅ {u['name']} → место {a0}"
            if len(own_slots(a1)) > get_setting("max_per_user"):
                note += " (у человека мест больше лимита)"
        view = panel_slot(a0) if slot_exists(a0) else panel_slots()

    elif act == "sw" and a0 is not None and slot_exists(a0):
        view = panel_swap(a0)

    elif act == "sx" and a0 is not None and a1 is not None:
        if slot_exists(a0) and slot_exists(a1) and a0 != a1:
            swap_owners(a0, a1)
            dirty.set()
            note = f"✅ Места {a0} и {a1} поменялись"
        else:
            note, alert = "Проверь номера мест.", True
        view = panel_slots()

    # ----- люди -----
    elif act == "u":
        view = people_picker(a0 or 0, "a:up", "a:u", "a:home", "👥 <b>Люди</b>\nВыбери человека:")

    elif act == "up" and a0 is not None:
        view = panel_person(a0)

    elif act == "ur" and a0 is not None:
        u = get_user(a0)
        if not u:
            note, alert, view = "Человек не найден.", True, panel_main()
        else:
            adm_rename[uid] = a0
            view = (
                f"✏️ Напиши новое имя и фамилию для <b>{esc(u['name'])}</b> следующим сообщением.",
                mk([[btn("✖️ Отмена", f"a:up:{a0}")]]),
            )

    elif act == "uf" and a0 is not None:
        with db:
            db.execute("UPDATE slots SET owner=NULL WHERE owner=?", (a0,))
        dirty.set()
        note = "Человек снят со всех мест"
        view = panel_person(a0)

    # ----- настройки -----
    elif act == "g":
        view = panel_settings()

    elif act == "ns" and a0 is not None:
        total = get_setting("total")
        new_total = max(1, min(MAX_TOTAL_LIMIT, total + a0))
        if new_total < total:
            busy = [r["n"] for r in db.execute(
                "SELECT n FROM slots WHERE n > ? AND owner IS NOT NULL ORDER BY n", (new_total,))]
            if busy:
                note = "Сначала освободи места: " + ", ".join(map(str, busy[:15]))
                alert = True
                new_total = total
        if new_total != total:
            resize_slots(new_total)
            dirty.set()
        view = panel_settings()

    elif act == "nm" and a0 is not None:
        set_setting("max_per_user", max(1, min(10, get_setting("max_per_user") + a0)))
        dirty.set()
        view = panel_settings()

    # ----- очистка -----
    elif act == "c":
        view = (
            "🧹 <b>Очистить всю очередь?</b>\nВсе люди потеряют свои места. Имена останутся.",
            mk([[btn("✅ Да, очистить", "a:cy")], [btn("◀️ Нет", "a:home")]]),
        )

    elif act == "cy":
        with db:
            db.execute("UPDATE slots SET owner=NULL")
            db.execute("UPDATE offers SET status='void' WHERE status='pending'")
        dirty.set()
        note = "✅ Очередь очищена"
        view = panel_main()

    if view is None:
        view = panel_main()
    await cb.answer(note, show_alert=alert)
    await panel_edit(cb.message, view)


# ----------------------- ТЕКСТ (имя) И КНОПКИ -------------------------
@router.message(F.text, ~F.text.startswith("/"))
async def on_text(m: Message, bot: Bot):
    uid = m.from_user.id
    if uid in adm_rename and uid in ADMIN_IDS:
        target = adm_rename.pop(uid)
        name, err = validate_name(m.text, target)
        if err:
            adm_rename[uid] = target
            await m.answer("⚠️ " + err)
            return
        set_name(target, name)
        dirty.set()
        text, markup = panel_person(target)
        await m.answer("✅ Имя изменено.\n\n" + text, reply_markup=markup)
        return
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
        win_open, _, win_reason = window_state()
        if not win_open:
            note, alert = "🔒 Запись закрыта. " + win_reason, True
        elif len(own_slots(uid)) >= mx:
            note, alert = "У тебя уже есть место. Сначала освободи его.", True
        else:
            views[uid] = {"mode": "take"}

    elif action == "t":
        n = to_int(arg)
        if not window_state()[0]:
            res = "closed"
        else:
            res = take_slot(uid, n) if n is not None else "none"
        if res == "ok":
            views[uid] = {"mode": "main"}
            note = f"✅ Место {n} теперь твоё"
        elif res == "limit":
            views[uid] = {"mode": "main"}
            note, alert = "У тебя уже есть место.", True
        elif res == "closed":
            views[uid] = {"mode": "main"}
            note, alert = "🔒 Запись закрыта. " + window_state()[2], True
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
    tasks = [asyncio.create_task(refresher(bot)), asyncio.create_task(window_watcher())]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
