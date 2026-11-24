"""Telegram-бот «Косатка» — чёрная кошка, которая очень любит шахматы.

Запуск из корня проекта:
    py -3 -m chess_bot.bot

Токен берётся из переменной окружения TELEGRAM_BOT_TOKEN или из файла .env
рядом с этим пакетом. Бот — лобби: создаёт партию и присылает кнопку с веб-доской,
где и делаются ходы. Картинок доски в чате больше нет.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from urllib.parse import quote

import chess
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from .app.engine import LEVEL_NAMES
from .app.store import TIME_CONTROL_NAMES, GameStore

log = logging.getLogger("chess_bot")

store: GameStore
engine_locks: dict[int, asyncio.Lock] = {}
active_engine_tasks: dict[int, asyncio.Task] = {}

MENU_TEXT = (
    "🐈‍⬛ <b>Косатка играет в шахматы</b>\n\n"
    "Чёрная кошка с белой маской и пятном на носу ждёт вас за доской.\n\n"
    "• <b>С Косаткой</b> — она умеет думать на четырёх уровнях силы.\n"
    "• <b>С другом</b> — партия по ссылке, каждый видит доску со своей стороны.\n\n"
    "Партия открывается в вебе: доска, ходы, сдача и ничья — всё там."
)

HELP_TEXT = (
    "🐾 <b>Как играть</b>\n\n"
    "1. В меню выберите «Играть с Косаткой» или «Позвать друга».\n"
    "2. Нажмите «♟ Открыть партию» — доска откроется в вебе.\n"
    "3. Ход: нажмите фигуру, затем клетку.\n"
    "4. Превращение пешки: выберите фигуру из подсказки.\n\n"
    "«Сдаться» и «Ничья» — кнопками под доской в вебе.\n"
    "Партии хранятся в базе, поэтому перезапуск Косатки не теряет позицию.\n\n"
    "Профиль, статистика, история и ник — кнопками в меню. "
    "Команда нужна одна: /start."
)


def is_admin(user_id: int) -> bool:
    admin = int(os.environ.get("ADMIN_ID", "0") or 0)
    return bool(admin) and user_id == admin


PENDING_NICK: set[int] = set()


def player_name(user_id: int, fallback: str) -> str:
    profile = store.player_profile(user_id, fallback)
    return profile.get("nick") or profile.get("name") or fallback


def card_text(game, user_id: int) -> str:
    """Короткая карточка партии для чата."""
    color = game.user_color(user_id)
    side = "белые" if color else "чёрные"
    if game.mode == "pve":
        head = f"♟ <b>Партия #{game.id}</b> · с Косаткой ({LEVEL_NAMES.get(game.level or 2, 'Любитель')})"
    else:
        head = f"♟ <b>Партия #{game.id}</b> · с другом"
    lines = [head, f"Вы: {side}"]
    opponent = game.black_name if color else game.white_name
    if opponent:
        lines.append(f"Соперник: {opponent}")
    speed = TIME_CONTROL_NAMES.get(game.time_control or "", "без таймера")
    lines.append(f"Время: {speed} · рейтинг: {store.player_rating(user_id)}")
    return "\n".join(lines)


def _pct(part: int, total: int) -> str:
    return f"{round(100 * part / total)}%" if total else "—"


def stats_text(user_id: int) -> str:
    s = store.player_stats(user_id)
    name = s.get("name") or "Игрок"
    lines = [
        f"📊 <b>Статистика · {name}</b>",
        "",
        f"Всего партий: <b>{s['total_games']}</b> · побед <b>{s['total_wins']}</b> "
        f"({_pct(s['total_wins'], s['total_games'])}) · ничьих {s['total_draws']}",
        "",
        "<b>С Косаткой</b>",
        f"Партий: {s['pve_games']} · побед {s['pve_wins']} ({_pct(s['pve_wins'], s['pve_games'])}) · "
        f"поражений {s['pve_losses']} · ничьих {s['pve_draws']}",
        "",
        "<b>С другом</b>",
        f"Партий: {s['pvp_games']} · побед {s['pvp_wins']} ({_pct(s['pvp_wins'], s['pvp_games'])}) · "
        f"поражений {s['pvp_losses']} · ничьих {s['pvp_draws']}",
        "",
        f"Ходов сделано: {s['moves_made']} · сдач: {s['resigned']}",
    ]
    if s.get("first_played"):
        first = datetime.fromtimestamp(s["first_played"]).strftime("%d.%m.%Y")
        last = datetime.fromtimestamp(s["last_played"]).strftime("%d.%m.%Y") if s.get("last_played") else "—"
        lines.append(f"Первая партия: {first} · последняя: {last}")
    if not s["total_games"]:
        lines.append("")
        lines.append("Партий пока нет — самое время начать.")
    return "\n".join(lines)


def top_text(limit: int = 5) -> str:
    rows = store.top_players(limit)
    if not rows:
        return "🏆 <b>Топ игроков</b>\n\nПока никто не сыграл ни одной партии."
    medals = ("🥇", "🥈", "🥉", "4)", "5)")
    lines = ["🏆 <b>Топ игроков по победам</b>", ""]
    for index, row in enumerate(rows):
        name = row.get("name") or f"Игрок {row.get('user_id')}"
        wins = row.get("total_wins") or 0
        games = row.get("total_games") or 0
        lines.append(f"{medals[index] if index < len(medals) else '•'} <b>{name}</b> — {wins} побед из {games}")
    return "\n".join(lines)


def history_text(user_id: int, limit: int = 5) -> str:
    games = store.recent_games(user_id, limit)
    if not games:
        return "🕘 <b>История партий</b>\n\nЗавершённых партий пока нет."
    lines = ["🕘 <b>Последние партии</b>", ""]
    for game in games:
        color = game.user_color(user_id)
        side = "белыми" if color == chess.WHITE else "чёрными"
        with_whom = "с Косаткой" if game.mode == "pve" else "с другом"
        moves_count = (len(game.move_list()) + 1) // 2
        lines.append(f"• #{game.id} · {with_whom} {side} · {game.result} · {moves_count} ходов")
    return "\n".join(lines)

def back_markup(data: str = "menu:root") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=data)]])


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐈‍⬛ Играть с Косаткой", callback_data="menu:bot")],
        [InlineKeyboardButton(text="👥 Позвать друга", callback_data="menu:friend")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="me:profile"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="stats:me")],
        [InlineKeyboardButton(text="📜 История", callback_data="history:show"),
         InlineKeyboardButton(text="🏆 Топ игроков", callback_data="top:show")],
        [InlineKeyboardButton(text="✏️ Сменить ник", callback_data="me:nick"),
         InlineKeyboardButton(text="🐾 Как играть", callback_data="menu:help")],
    ])


# --------------------------------------------------------------------- карточка партии


def webapp_url(start: str | None = None, game_id: int | None = None) -> str | None:
    """Адрес игры из WEBAPP_URL. Telegram требует https, поэтому без него кнопки нет."""
    base = os.environ.get("WEBAPP_URL", "").strip()
    if not base:
        return None
    if not base.endswith("/") and "?" not in base:
        base += "/"
    params: list[str] = []
    if game_id is not None:
        params.append(f"game={game_id}")
    if start:
        params.append(f"startapp={quote(start)}")
    if params:
        base += ("&" if "?" in base else "?") + "&".join(params)
    return base


def play_markup(game_id: int, *extra_rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup | None:
    """Клавиатура с кнопкой веб-доски. None, если WEBAPP_URL не настроен."""
    url = webapp_url(game_id=game_id)
    if not url:
        return None
    rows = [[InlineKeyboardButton(text="♟ Открыть партию", web_app=WebAppInfo(url=url))]]
    rows.extend(extra_rows)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def make_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart(deep_link=True))
    async def start_with_invite(message: Message, command: CommandObject) -> None:
        user = message.from_user
        game = store.invite_by_token(command.args or "")
        if game is None:
            await message.answer("Приглашение не найдено или устарело.")
            return
        store.save_chat_id(user.id, message.chat.id)
        name = player_name(user.id, user.full_name)
        store.touch_player(user.id, name)
        ok, text, game = store.join(game.id, user.id, name)
        await message.answer(text)
        if game is None or (not ok and game.user_color(user.id) is None):
            return
        markup = play_markup(game.id, [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:root")])
        if markup:
            await message.answer("Доска открывается в вебе.", reply_markup=markup)

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        store.save_chat_id(message.from_user.id, message.chat.id)
        await message.answer(MENU_TEXT, reply_markup=menu_keyboard())
        active = store.user_games(message.from_user.id)
        if active:
            game = active[0]
            markup = play_markup(game.id, [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:root")])
            if markup:
                await message.answer(f"У вас есть незавершённая партия #{game.id}.", reply_markup=markup)

    @dp.callback_query(F.data == "me:profile")
    async def profile_cb(call: CallbackQuery) -> None:
        await call.answer()
        user_id = call.from_user.id
        profile = store.player_profile(user_id, call.from_user.full_name)
        stats = profile["stats"]
        lines = [
            f"👤 <b>{profile['name'] or 'Игрок'}</b>",
            f"Ник в партиях: <b>{profile['nick'] or 'как в Telegram'}</b>",
            f"Рейтинг PvP: <b>{profile['rating']}</b>",
            f"Партий: {stats.get('total_games', 0)} · побед {stats.get('total_wins', 0)} "
            f"· ничьих {stats.get('total_draws', 0)}",
        ]
        rows = [[InlineKeyboardButton(text="✏️ Сменить ник", callback_data="me:nick")]]
        if is_admin(user_id):
            rows.append([InlineKeyboardButton(text="🧹 Очистить базу", callback_data="admin:wipe")])
        rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:root")])
        await edit_menu(call, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.callback_query(F.data == "me:nick")
    async def nick_prompt(call: CallbackQuery) -> None:
        await call.answer()
        PENDING_NICK.add(call.from_user.id)
        current = store.player_profile(call.from_user.id)["nick"]
        await edit_menu(
            call,
            f"Текущий ник: <b>{current or 'как в Telegram'}</b>\n\n"
            "Пришлите новое имя сообщением — до 24 знаков.",
            back_markup("me:profile"),
        )

    @dp.message(F.text & ~F.text.startswith("/"))
    async def nick_input(message: Message) -> None:
        user = message.from_user
        if user.id not in PENDING_NICK:
            await message.answer("Выберите действие кнопками в меню.", reply_markup=menu_keyboard())
            return
        PENDING_NICK.discard(user.id)
        store.save_chat_id(user.id, message.chat.id)
        value = (message.text or "").strip()
        if not value:
            await message.answer("Ник пустой — попробуйте ещё раз.", reply_markup=menu_keyboard())
            return
        store.set_nick(user.id, value)
        profile = store.player_profile(user.id, user.full_name)
        await message.answer(f"Готово. В партиях вы теперь <b>{profile['nick']}</b>.",
                             reply_markup=menu_keyboard())

    @dp.callback_query(F.data == "history:show")
    async def history_cb(call: CallbackQuery) -> None:
        await call.answer()
        await edit_menu(call, history_text(call.from_user.id), back_markup())

    @dp.callback_query(F.data == "admin:wipe")
    async def wipe_ask(call: CallbackQuery) -> None:
        await call.answer()
        if not is_admin(call.from_user.id):
            return
        await edit_menu(
            call,
            "🧹 <b>Очистить базу?</b>\n\n"
            "Партии, игроки и приглашения удалятся. Перед очисткой сохраню JSON-копию.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Да, очистить", callback_data="admin:wipe:yes")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="me:profile")],
            ]),
        )

    @dp.callback_query(F.data == "admin:wipe:yes")
    async def wipe_do(call: CallbackQuery) -> None:
        if not is_admin(call.from_user.id):
            await call.answer("Только администратор", show_alert=True)
            return
        await call.answer()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.abspath(f"wipe-{stamp}.json")
        payload = store.export_tables()
        with open(backup_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        store.import_tables({}, replace=True)
        log.warning("База очищена, копия в %s", backup_path)
        games = len(payload.get("games") or [])
        players = len(payload.get("players") or [])
        await edit_menu(
            call,
            "🧹 <b>База очищена</b>\n"
            f"Копия: <code>{backup_path}</code>\n"
            f"Было партий: {games}, игроков: {players}.",
            back_markup(),
        )

    @dp.callback_query(F.data == "stats:me")
    async def stats_cb(call: CallbackQuery) -> None:
        await call.answer()
        await edit_menu(call, stats_text(call.from_user.id), back_markup())

    @dp.callback_query(F.data == "top:show")
    async def top_cb(call: CallbackQuery) -> None:
        await call.answer()
        await edit_menu(call, top_text(), back_markup())

    async def edit_menu(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
        try:
            await call.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error):
                raise

    @dp.callback_query(F.data == "menu:root")
    async def menu_root(call: CallbackQuery) -> None:
        await call.answer()
        await edit_menu(call, MENU_TEXT, menu_keyboard())

    @dp.callback_query(F.data == "menu:help")
    async def menu_help(call: CallbackQuery) -> None:
        await call.answer()
        await edit_menu(call, HELP_TEXT, back_markup())

    @dp.callback_query(F.data == "menu:bot")
    async def menu_bot(call: CallbackQuery) -> None:
        await call.answer()
        rows = [
            [InlineKeyboardButton(text=f"{level} · {LEVEL_NAMES[level]}", callback_data=f"level:{level}")]
            for level in sorted(LEVEL_NAMES)
        ]
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:root")])
        await edit_menu(call, "<b>Сложность бота</b>\nВыберите уровень:", InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.callback_query(F.data.startswith("level:"))
    async def pick_level(call: CallbackQuery) -> None:
        level = int(call.data.split(":")[1])
        await call.answer()
        rows = [
            [
                InlineKeyboardButton(text="♙ Белыми", callback_data=f"pve:{level}:w"),
                InlineKeyboardButton(text="♟ Чёрными", callback_data=f"pve:{level}:b"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:bot")],
        ]
        await edit_menu(
            call,
            f"<b>{LEVEL_NAMES[level]}</b>\nКаким цветом играете?",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @dp.callback_query(F.data.startswith("pve:"))
    async def start_pve(call: CallbackQuery) -> None:
        _, level_s, color_s = call.data.split(":")
        level = int(level_s)
        color = color_s != "b"
        await call.answer("Играем " + ("белыми ♙" if color else "чёрными ♟"))
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        store.save_chat_id(call.from_user.id, call.message.chat.id)
        name = player_name(call.from_user.id, call.from_user.full_name)
        store.touch_player(call.from_user.id, name)
        game = store.create_pve(call.from_user.id, name, color, level)
        markup = play_markup(game.id, [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:root")])
        text = card_text(game, call.from_user.id) + "\n\nСоздана. Доска откроется по кнопке ниже."
        if markup:
            await call.message.answer(text, reply_markup=markup)
        else:
            await call.message.answer(text + "\n\nВеб-версия не настроена: нужен WEBAPP_URL.")

    @dp.callback_query(F.data == "menu:friend")
    async def menu_friend(call: CallbackQuery) -> None:
        await call.answer()
        rows = [
            [
                InlineKeyboardButton(text="♙ Белыми", callback_data="pvp:w"),
                InlineKeyboardButton(text="♟ Чёрными", callback_data="pvp:b"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:root")],
        ]
        await edit_menu(call, "<b>Партия с другом</b>\nКаким цветом играете?", InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.callback_query(F.data.startswith("pvp:"))
    async def create_pvp(call: CallbackQuery, bot: Bot) -> None:
        color = call.data.split(":")[1] == "w"
        await call.answer()
        store.save_chat_id(call.from_user.id, call.message.chat.id)
        name = player_name(call.from_user.id, call.from_user.full_name)
        store.touch_player(call.from_user.id, name)
        game, token = store.create_pvp(call.from_user.id, name, color)
        username = (await bot.get_me()).username
        link = f"https://t.me/{username}?start={token}"
        markup = play_markup(game.id, [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:root")])
        text = (
            card_text(game, call.from_user.id) + "\n\n"
            "Отправьте другу ссылку — он войдёт в партию и получит кнопку с доской:\n"
            f"<code>{link}</code>"
        )
        if markup:
            await call.message.answer(text, reply_markup=markup)
        else:
            await call.message.answer(text + "\n\nВеб-версия не настроена: нужен WEBAPP_URL.")

    return dp


# --------------------------------------------------------------------- запуск


def read_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if key.strip() == "TELEGRAM_BOT_TOKEN" and value.strip():
                    return value.strip().strip('"').strip("'")
    raise SystemExit(
        "Не задан токен бота.\n"
        "1. Откройте @BotFather в Telegram и получите токен (/newbot).\n"
        "2. Задайте переменную TELEGRAM_BOT_TOKEN или создайте .env в папке проекта:\n"
        "   TELEGRAM_BOT_TOKEN=123456789:ABCdef..."
    )


async def notify_loop(bot: Bot) -> None:
    """Раз в 5 секунд рассылает уведомления, поставленные веб-сервером."""
    while True:
        try:
            for user_id, notice_id, text in store.take_notices():
                chat = store.player_chat(user_id)
                if chat is None:
                    continue
                try:
                    await bot.send_message(chat, text, reply_markup=menu_keyboard())
                except TelegramBadRequest as error:
                    log.warning("Уведомление %s не отправлено: %s", notice_id, error)
        except Exception:  # noqa: BLE001 — рассылка не должна ронять бота
            log.exception("Ошибка рассылки уведомлений")
        await asyncio.sleep(5)


async def main() -> None:
    global store
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db_path = os.environ.get("CHESS_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chess_bot.db"))
    # DATABASE_URL (Postgres на Render) важнее локального пути
    store = GameStore.from_env(default=os.path.abspath(db_path))
    log.info("Хранилище партий: %s", store.dialect)
    token = read_token()
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = make_dispatcher()

    runner = None
    webapp_url = os.environ.get("WEBAPP_URL", "").strip()
    if webapp_url:
        from .web_server import run_server

        host = os.environ.get("WEBAPP_HOST", "0.0.0.0")
        port = int(os.environ.get("WEBAPP_PORT", "8080"))
        runner = await run_server(host, port, store, token)
        log.info("Web App доступен по %s", webapp_url)
    else:
        log.warning("WEBAPP_URL не задан — играть негде: бот только создаёт партии")

    from .web_server import RUNTIME

    @dp.update.outer_middleware()
    async def track_updates(handler, event, data):
        RUNTIME["last_update"] = time.time()
        try:
            result = await handler(event, data)
            RUNTIME["handler_error"] = ""
            return result
        except Exception as error:  # noqa: BLE001 — видно в /api/health
            RUNTIME["handler_error"] = f"{type(error).__name__}: {error}"[:200]
            raise
        finally:
            RUNTIME["handled"] += 1

    try:
        hook = await bot.get_webhook_info()
        if hook.url:
            log.warning("Был установлен вебхук %s — удаляю, работаем на поллинге", hook.url)
            await bot.delete_webhook()
        me = await bot.get_me()
        log.info("Telegram: @%s", me.username)
        # в списке команд у пользователя остаётся только /start
        await bot.set_my_commands([BotCommand(command="start", description="Меню")])
        if runner is not None:
            runner.app["bot_healthy"] = True
    except Exception as error:  # noqa: BLE001 — сеть может моргнуть, поллинг попробуем всё равно
        log.exception("Не удалось проверить вебхук и бот-аккаунт")
        if runner is not None:
            runner.app["startup_error"] = f"{type(error).__name__}: {error}"[:200]

    # Ход бота считается в вебе при открытии партии. Флаги, оставшиеся от
    # прерванного расчёта, снимаем, иначе партия повиснет в «бот думает».
    for game in store.pending_engine_games():
        log.info("Сбрасываю зависший ход бота в партии %s", game.id)
        store.clear_engine_pending(game.id)

    # ежедневная копия базы в 00:00 (по времени машины) + в Drive, если настроен
    backup_task = None
    if os.environ.get("BACKUP_ENABLED", "1") not in ("0", "false", "no"):
        from .backup import start_daily

        keep = int(os.environ.get("BACKUP_KEEP", "30"))
        backup_task = start_daily(store, keep=keep)
        log.info("Ежедневный бэкап включён (хранить %s копий)", keep)

    notify_task = asyncio.create_task(notify_loop(bot))
    log.info("Бот запущен")
    conflicts = 0
    try:
        while True:
            try:
                await dp.start_polling(bot)
                break
            except TelegramConflictError as error:
                # второй экземпляр бота: перекрытие деплоя или вебхук из прошлого запуска
                conflicts += 1
                log.error("Конфликт getUpdates (%s/5): %s", conflicts, error)
                if conflicts >= 5:
                    raise
                await asyncio.sleep(20)
    finally:
        notify_task.cancel()
        if backup_task is not None:
            backup_task.cancel()
        if runner is not None:
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        raise
