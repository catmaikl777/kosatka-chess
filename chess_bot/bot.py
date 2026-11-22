"""Telegram-бот «Косатка» — чёрная кошка, которая очень любит шахматы.

Запуск из корня проекта:
    py -3 -m chess_bot.bot

Токен берётся из переменной окружения TELEGRAM_BOT_TOKEN или из файла .env
рядом с этим пакетом. Доска — картинка с фигурами и талисманом-кошкой;
ход делается кнопками: фигура → клетка.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime

import chess
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    WebAppInfo,
)

from .app import board_ui, images
from .app.engine import LEVEL_NAMES, choose_move
from .app.store import Game, GameStore

log = logging.getLogger("chess_bot")

store: GameStore
engine_locks: dict[int, asyncio.Lock] = {}
active_engine_tasks: dict[int, asyncio.Task] = {}

# «Настроение» Косатки для подписи справа на карточке
MOODS = {
    "wait": "Косатка ждёт соперника",
    "think": "Косатка думает",
    "mate": "Косатка мурлычет",
    "draw": "Косатка зевает",
    "lose": "Косатка прикрыла лапой глаза",
    "win": "Косатка довольно жмурится",
    "check": "Косатка насторожилась",
}

PHRASES = {
    "start": "Мяу! Я Косатка. Садитесь за доску — лапы у меня хоть и мягкие, но фигуры держат крепко.",
    "pve": "Сажусь напротив. Сложность: {level}.",
    "pvp": "Желаю красивой партии! Отправьте другу ссылку — и он сядет за доску.",
    "join": "Мур! Вы за доской. {color} — ваш цвет.",
    "your_turn": "Ваш ход.",
    "opponent": "Соперник думает…",
    "check": "Шах! Осторожнее с лапками.",
    "mate": "Мат! Косатка свернулась клубочком.",
    "draw": "Ничья. Косатка зевааает.",
    "bad": "Так ходить нельзя — Косатка покачала ушами.",
    "not_your_turn": "Сейчас не ваш ход — подождите лапку соперника.",
}


def engine_lock_for(game_id: int) -> asyncio.Lock:
    """Своя блокировка на партию: чужие партии не ждут расчёта."""
    return engine_locks.setdefault(game_id, asyncio.Lock())


MENU_TEXT = (
    "🐈‍⬛ <b>Косатка играет в шахматы</b>\n\n"
    "Чёрная кошка с белой маской и пятном на носу ждёт вас за доской.\n\n"
    "• <b>С Косаткой</b> — она умеет думать на четырёх уровнях силы.\n"
    "• <b>С другом</b> — партия по ссылке, каждый видит доску со своей стороны.\n\n"
    "Ход: нажмите фигуру под доской, затем клетку. Ход можно нарисовать стрелкой подсказки."
)

HELP_TEXT = (
    "🐾 <b>Как играть</b>\n\n"
    "1. Под картинкой доски — фигуры, которыми можно ходить. Нажмите одну.\n"
    "2. Появятся клетки — нажмите нужную.\n"
    "3. Превращение пешки: Косатка предложит выбрать фигуру картинками.\n\n"
    "На доске подсвечиваются прошлый ход (тёплый квадрат) и выбранная клетка (рамка).\n"
    "«Сдаться» и «Ничья» — кнопками под доской.\n"
    "Партии хранятся в базе, поэтому перезапуск Косатки не теряет позицию."
)


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
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats:me"),
         InlineKeyboardButton(text="🏆 Топ игроков", callback_data="top:show")],
        [InlineKeyboardButton(text="🐾 Как играть", callback_data="menu:help")],
    ])


# --------------------------------------------------------------------- карточка партии


def viewer_perspective(game: Game, viewer_id: int | None) -> bool:
    color = game.user_color(viewer_id)
    if game.mode == "pve":
        return chess.WHITE if game.white_id else chess.BLACK
    return color if color is not None else chess.WHITE


def game_mood(game: Game, viewer_id: int | None) -> str:
    board = game.draw_board()
    color = game.user_color(viewer_id)
    if game.status == "waiting":
        return MOODS["wait"]
    if game.status == "finished":
        result = game.result or ""
        if "Ничья" in result:
            return MOODS["draw"]
        winner_is_white = "Белые" in result
        if color is None:
            return MOODS["mate"]
        if (color == chess.WHITE) == winner_is_white and "Сдача" not in result:
            return MOODS["win"]
        if "Сдача" in result:
            return MOODS["lose"]
        return MOODS["mate"]
    if game.mode == "pve" and game.awaiting_engine:
        return MOODS["think"]
    if board.is_check():
        return MOODS["check"]
    return "Косатка наблюдает"


def game_caption(game: Game, viewer_id: int | None, highlight: int | None = None) -> str:
    """Подпись под картинкой: коротко, с лапками и без простыни текста."""
    board = game.draw_board()
    color = game.user_color(viewer_id)
    lines: list[str] = []

    if game.mode == "pve":
        title = f"🐈‍⬛ <b>Косатка</b> · {LEVEL_NAMES.get(game.level or 2, 'Любитель')}"
    else:
        title = "🐈‍⬛ <b>Партия с другом</b>"
    lines.append(title)

    if game.status == "waiting":
        lines.append("Ждёт соперника — отправьте другу ссылку-приглашение.")
    elif game.status == "finished":
        lines.append("🏁 " + (game.result or "Партия окончена"))
    else:
        turn = "белых ♙" if board.turn == chess.WHITE else "чёрных ♟"
        if color is None:
            lines.append(f"Ход {turn} · вы наблюдатель")
        elif board.turn == color:
            lines.append(f"<b>Ваш ход</b> · {turn}")
        else:
            lines.append(f"Ход {turn} · ждём соперника")
        if board.is_check():
            lines.append("Шах!")

    if game.status == "active" and game.mode == "pvp" and game.draw_offer_by and game.draw_offer_by != viewer_id:
        lines.append("Соперник предлагает ничью.")

    if color is not None and game.status == "active":
        lines.append("Вы — " + ("белые" if color == chess.WHITE else "чёрные"))

    history = board_ui.move_history_text(game, limit=10)
    if history:
        lines.append("<i>" + history + "</i>")
    if (game.level or 0) and game.mode == "pve":
        pass
    return "\n".join(lines)


def webapp_url(start: str | None = None) -> str | None:
    """Адрес игры из WEBAPP_URL. Telegram требует https, поэтому без него кнопки нет."""
    base = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
    if not base:
        return None
    if not base.endswith("/"):
        base += "/"
    if start:
        base += f"?startapp={start}"
    return base


def keyboard_for(game: Game, viewer_id: int | None) -> InlineKeyboardMarkup:
    url = webapp_url()
    play_row = [InlineKeyboardButton(text="🕹 Играть в вебе", web_app=WebAppInfo(url=url))] if url else []

    if game.status == "waiting":
        return InlineKeyboardMarkup(inline_keyboard=[
            *([play_row] if play_row else []),
            [InlineKeyboardButton(text="🔗 Ссылка-приглашение", callback_data=f"g{game.id}:invite")],
            [InlineKeyboardButton(text="❌ Убрать доску", callback_data=f"g{game.id}:cancel")],
        ])

    if game.status == "finished":
        return InlineKeyboardMarkup(inline_keyboard=[
            *([play_row] if play_row else []),
            [InlineKeyboardButton(text="🐾 Ещё партия", callback_data="menu:root")],
        ])

    board = game.draw_board()
    color = game.user_color(viewer_id)

    if game.mode == "pvp" and game.draw_offer_by and game.draw_offer_by != viewer_id:
        return InlineKeyboardMarkup(inline_keyboard=[
            *([play_row] if play_row else []),
            [InlineKeyboardButton(text="✅ Принять ничью", callback_data=f"g{game.id}:drawaccept"),
             InlineKeyboardButton(text="❌ Отклонить", callback_data=f"g{game.id}:drawdecline")],
            [InlineKeyboardButton(text="🏳️ Сдаться", callback_data=f"g{game.id}:resign")],
        ])

    can_move = (
        color is not None
        and board.turn == color
        and not (game.mode == "pve" and game.awaiting_engine)
    )
    if can_move:
        return board_ui.piece_keyboard(game.id, board, top_row=play_row)

    buttons = [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"g{game.id}:refresh")]
    if color is not None:
        buttons.append(InlineKeyboardButton(text="🏳️ Сдаться", callback_data=f"g{game.id}:resign"))
        if game.mode == "pvp":
            buttons.append(InlineKeyboardButton(text="🤝 Ничья", callback_data=f"g{game.id}:draw"))
    return InlineKeyboardMarkup(inline_keyboard=[*([play_row] if play_row else []), buttons])


def theme_for(user_id: int | None) -> str:
    """Тема хранится в переменных окружения процесса — по умолчанию «Ночная Коска»."""
    return user_theme.get(user_id, images.DEFAULT_THEME) if user_id else images.DEFAULT_THEME


user_theme: dict[int, str] = {}


def board_png(game: Game, viewer_id: int | None, highlight: int | None = None) -> bytes:
    """Карточка партии для конкретного зрителя: своя сторона, тема, последний ход."""
    board = game.draw_board()
    perspective = viewer_perspective(game, viewer_id)
    moves = game.move_list()
    last_move = chess.Move.from_uci(moves[-1]) if moves else None
    title = "Косатка · " + LEVEL_NAMES.get(game.level or 2, "Любитель") if game.mode == "pve" else "Косатка · с другом"
    return images.render_board_bytes(
        board,
        perspective=perspective,
        theme_name=theme_for(viewer_id),
        last_move=last_move,
        caption_left="Косатка",
        subtitle=title,
        caption_right=game_mood(game, viewer_id),
        highlight=highlight,
    )


# --------------------------------------------------------------------- отрисовка


def remember_message(game_id: int, user_id: int, chat_id: int, message_id: int) -> None:
    store.remember_message(game_id, user_id, chat_id, message_id)


def known_messages(game_id: int) -> list[tuple[int, int, int]]:
    return store.known_messages(game_id)


async def send_board(bot: Bot, game: Game, viewer_id: int, chat_id: int) -> None:
    photo = BufferedInputFile(board_png(game, viewer_id), filename=f"kosatka-{game.id}.png")
    sent = await bot.send_photo(
        chat_id,
        photo,
        caption=game_caption(game, viewer_id),
        reply_markup=keyboard_for(game, viewer_id),
    )
    remember_message(game.id, viewer_id, chat_id, sent.message_id)


async def edit_board(bot: Bot, game: Game, user_id: int, chat_id: int, message_id: int,
                     highlight: int | None = None, force: bool = False) -> None:
    """Обновляет карточку. Telegram присылает ошибку, если картинка та же, — это норма."""
    media = InputMediaPhoto(
        media=BufferedInputFile(board_png(game, user_id, highlight), filename=f"kosatka-{game.id}.png"),
        caption=game_caption(game, user_id, highlight),
    )
    try:
        await bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=media,
            reply_markup=keyboard_for(game, user_id),
        )
    except TelegramBadRequest as error:
        message = str(error)
        if "message is not modified" in message and not force:
            return
        if "not found" in message or "can't be edited" in message:
            await send_board(bot, game, user_id, chat_id)
            return
        raise


async def broadcast(bot: Bot, game: Game, force: bool = False) -> None:
    """Обновляет доску у всех, кому её показывали; новому игроку отправляет заново."""
    for user_id, chat_id, message_id in known_messages(game.id):
        try:
            await edit_board(bot, game, user_id, chat_id, message_id, force=force)
        except Exception:  # noqa: BLE001 — чужой чат не должен ломать партию
            log.warning("Не удалось обновить доску game=%s user=%s", game.id, user_id, exc_info=True)


async def broadcast_except(bot: Bot, game: Game, skip_user: int) -> None:
    for user_id, chat_id, message_id in known_messages(game.id):
        if user_id == skip_user:
            continue
        try:
            await edit_board(bot, game, user_id, chat_id, message_id)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось обновить доску game=%s user=%s", game.id, user_id, exc_info=True)


# --------------------------------------------------------------------- движок


async def run_engine(bot: Bot, game_id: int) -> None:
    """Ход бота: считается в отдельном потоке, чтобы не блокировать polling."""
    async with engine_lock:
        game = store.get(game_id)
        if game is None or game.status != "active" or game.mode != "pve":
            return
        board = game.draw_board()
        human_color = chess.WHITE if game.white_id else chess.BLACK
        if board.turn == human_color:
            store.clear_engine_pending(game_id)
            return
        store.mark_engine_pending(game_id)
        try:
            move = await asyncio.to_thread(choose_move, board.copy(), game.level or 2)
        except Exception:  # noqa: BLE001
            log.exception("Движок не смог выбрать ход в партии %s", game_id)
            store.clear_engine_pending(game_id)
            return
        updated = store.after_engine_move(game_id, move.uci())
        if updated:
            await broadcast(bot, updated)


def schedule_engine(bot: Bot, game_id: int) -> None:
    existing = active_engine_tasks.get(game_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(run_engine(bot, game_id))
    active_engine_tasks[game_id] = task
    task.add_done_callback(lambda _: active_engine_tasks.pop(game_id, None))


# --------------------------------------------------------------------- диспетчер


def make_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart(deep_link=True))
    async def start_with_invite(message: Message, command: CommandObject, bot: Bot) -> None:
        user = message.from_user
        game = store.invite_by_token(command.args or "")
        if game is None:
            await message.answer("Приглашение не найдено или устарело.")
            return
        store.touch_player(user.id, user.full_name)
        ok, text, game = store.join(game.id, user.id, user.full_name)
        await message.answer(text)
        if game is None:
            return
        if not ok and game.user_color(user.id) is None:
            return
        await send_board(bot, game, user.id, message.chat.id)
        await broadcast_except(bot, game, user.id)

    @dp.message(CommandStart())
    async def start(message: Message, bot: Bot) -> None:
        await message.answer(MENU_TEXT, reply_markup=menu_keyboard())
        active = store.user_games(message.from_user.id)
        if active:
            game = active[0]
            await message.answer(f"У вас есть незавершённая партия #{game.id}. Показываю доску:")
            await send_board(bot, store.get(game.id), message.from_user.id, message.chat.id)
            if game.mode == "pve" and game.awaiting_engine:
                schedule_engine(bot, game.id)

    @dp.message(Command("help"))
    async def help_cmd(message: Message) -> None:
        await message.answer(HELP_TEXT, reply_markup=menu_keyboard())

    @dp.message(Command("new"))
    async def new_cmd(message: Message) -> None:
        await message.answer(MENU_TEXT, reply_markup=menu_keyboard())

    @dp.message(Command("stats"))
    async def stats_cmd(message: Message) -> None:
        await message.answer(stats_text(message.from_user.id), reply_markup=menu_keyboard())

    @dp.message(Command("top"))
    async def top_cmd(message: Message) -> None:
        await message.answer(top_text(), reply_markup=menu_keyboard())

    @dp.message(Command("history"))
    async def history_cmd(message: Message) -> None:
        await message.answer(history_text(message.from_user.id), reply_markup=menu_keyboard())

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
            [InlineKeyboardButton(text="🎲 Случайный цвет", callback_data=f"pve:{level}:r")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:bot")],
        ]
        await edit_menu(
            call,
            f"<b>{LEVEL_NAMES[level]}</b>\nКаким цветом играете?",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @dp.callback_query(F.data.startswith("pve:"))
    async def start_pve(call: CallbackQuery, bot: Bot) -> None:
        _, level_s, color_s = call.data.split(":")
        level = int(level_s)
        color = random.choice([chess.WHITE, chess.BLACK]) if color_s == "r" else color_s == "w"
        await call.answer()
        store.touch_player(call.from_user.id, call.from_user.full_name)
        game = store.create_pve(call.from_user.id, call.from_user.full_name, color, level)
        await send_board(bot, game, call.from_user.id, call.message.chat.id)
        if game.draw_board().turn == chess.BLACK:
            schedule_engine(bot, game.id)
        url = webapp_url()
        if url:
            await call.message.answer(
                "Партия создана. Играть можно кнопками под доской или открыть доску в вебе — "
                "эта кнопка есть в меню доски.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🕹 Открыть партию в вебе", web_app=WebAppInfo(url=url)),
                ]]),
            )

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
        store.touch_player(call.from_user.id, call.from_user.full_name)
        game, token = store.create_pvp(call.from_user.id, call.from_user.full_name, color)
        await send_board(bot, game, call.from_user.id, call.message.chat.id)
        username = (await bot.get_me()).username
        await call.message.answer(
            "Отправьте другу эту ссылку — он войдёт в партию:\n"
            f"<code>https://t.me/{username}?start={token}</code>"
        )

    @dp.callback_query(F.data.startswith("g"))
    async def game_action(call: CallbackQuery, bot: Bot) -> None:
        try:
            raw_id, action = call.data[1:].split(":", 1)
            game_id = int(raw_id)
        except (ValueError, IndexError):
            await call.answer("Некорректная кнопка")
            return

        game = store.get(game_id)
        if game is None:
            await call.answer("Партия не найдена", show_alert=True)
            return

        user_id = call.from_user.id
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        if action == "invite":
            token = store.token_for_game(game_id)
            await call.answer()
            if token is None:
                await call.message.answer("Приглашение не найдено.")
                return
            username = (await bot.get_me()).username
            await call.message.answer(
                "Ссылка-приглашение:\n" f"<code>https://t.me/{username}?start={token}</code>"
            )
            return

        if action == "refresh":
            await call.answer("Обновлено")
            await edit_board(bot, game, user_id, chat_id, message_id)
            return

        if action == "cancel":
            ok = store.cancel(game_id, user_id)
            await call.answer("Партия отменена" if ok else "Отменить нельзя")
            if ok:
                await broadcast(bot, store.get(game_id))
            return

        if action == "back":
            await call.answer()
            await edit_board(bot, game, user_id, chat_id, message_id)
            return

        if action == "resign":
            ok, updated = store.resign(game_id, user_id)
            await call.answer("Вы сдались" if ok else "Сейчас нельзя сдаться")
            if updated and ok:
                await broadcast(bot, updated)
            return

        if action == "draw":
            ok, text, updated = store.offer_draw(game_id, user_id)
            await call.answer(text, show_alert=not ok)
            if updated:
                await edit_board(bot, updated, user_id, chat_id, message_id)
                await broadcast_except(bot, updated, user_id)
            return

        if action == "drawaccept":
            ok, text, updated = store.offer_draw(game_id, user_id)
            await call.answer(text, show_alert=not ok)
            if updated:
                await broadcast(bot, updated)
            return

        if action == "drawdecline":
            ok, updated = store.decline_draw(game_id, user_id)
            await call.answer("Ничья отклонена" if ok else "Предложения нет")
            if updated and ok:
                await broadcast(bot, updated)
            return

        if action.startswith("p"):
            square = int(action[1:])
            board = game.draw_board()
            if not any(m.from_square == square for m in board.legal_moves):
                await call.answer("Эта фигура не может ходить")
                return
            await call.answer()
            try:
                await call.message.edit_caption(
                    caption=game_caption(game, user_id, square),
                    reply_markup=board_ui.move_keyboard(game.id, board, square),
                )
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error):
                    raise
            return

        if action.startswith("m"):
            uci = resolve_move(action[1:], game)
            if uci is None:
                await call.answer("Так ходить нельзя — Косатка покачала ушами")
                return
            if needs_promotion(game, uci):
                await call.answer()
                try:
                    await call.message.edit_caption(
                        caption=game_caption(game, user_id) + "\n\n🐾 <b>Превращение:</b> выберите фигуру",
                        reply_markup=board_ui.promotion_keyboard(
                            game.id, chess.parse_square(uci[:2]), chess.parse_square(uci[2:4])
                        ),
                    )
                except TelegramBadRequest as error:
                    if "message is not modified" not in str(error):
                        raise
                return
            await apply_and_continue(call, bot, game, uci)
            return

        if action.startswith("q"):
            raw = action[1:]
            if len(raw) != 5 or raw[4] not in "qrbn":
                await call.answer("Некорректный выбор")
                return
            await apply_and_continue(call, bot, game, raw)
            return

        await call.answer()

    return dp


# --------------------------------------------------------------------- помощники


def resolve_move(raw: str, game: Game) -> str | None:
    """Проверяет, что ход действительно легален, и возвращает UCI без фигуры превращения."""
    if len(raw) < 4:
        return None
    try:
        chess.parse_square(raw[:2])
        chess.parse_square(raw[2:4])
    except ValueError:
        return None
    board = game.draw_board()
    matches = [
        m for m in board.legal_moves
        if chess.square_name(m.from_square) == raw[:2] and chess.square_name(m.to_square) == raw[2:4]
    ]
    if not matches:
        return None
    return matches[0].uci()


def needs_promotion(game: Game, uci: str) -> bool:
    board = game.draw_board()
    return any(
        m.from_square == chess.parse_square(uci[:2])
        and m.to_square == chess.parse_square(uci[2:4])
        and m.promotion is not None
        for m in board.legal_moves
    )


async def apply_and_continue(call: CallbackQuery, bot: Bot, game: Game, uci: str) -> None:
    ok, text, updated = store.apply_move(game.id, call.from_user.id, uci)
    if not ok:
        await call.answer(text, show_alert=True)
        fresh = store.get(game.id)
        if fresh:
            await edit_board(bot, fresh, call.from_user.id, call.message.chat.id, call.message.message_id)
        return

    await call.answer("Ход сделан")
    if updated is None or updated.status == "finished":
        if updated:
            await broadcast(bot, updated)
        return

    await edit_board(bot, updated, call.from_user.id, call.message.chat.id, call.message.message_id)
    if updated.mode == "pve":
        schedule_engine(bot, updated.id)
    else:
        await broadcast_except(bot, updated, call.from_user.id)


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
        runner.app["bot_healthy"] = True
        log.info("Web App доступен по %s", webapp_url)
    else:
        log.warning("WEBAPP_URL не задан — веб-версия выключена, работают только доски-картинки")

    for game in store.pending_engine_games():
        log.info("Доигрываю незавершённый ход бота в партии %s", game.id)
        schedule_engine(bot, game.id)

    # ежедневная копия базы в 00:00 (по времени машины) + в Drive, если настроен
    backup_task = None
    if os.environ.get("BACKUP_ENABLED", "1") not in ("0", "false", "no"):
        from .backup import start_daily

        keep = int(os.environ.get("BACKUP_KEEP", "30"))
        backup_task = start_daily(store, keep=keep)
        log.info("Ежедневный бэкап включён (хранить %s копий)", keep)

    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        if backup_task is not None:
            backup_task.cancel()
        if runner is not None:
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        raise
