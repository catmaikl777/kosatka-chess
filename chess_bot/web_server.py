"""Веб-сервер: API игры, режим без Telegram и статика Web App.

Запуск отдельно:
    py -3 -m chess_bot.web_server --host 0.0.0.0 --port 8080

Адрес, на который Telegram должен открыть игру, задаётся в WEBAPP_URL
(например, https://kot.example.com/webapp/). Пока WEBAPP_URL не задан,
бот работает только в режиме карточек.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import chess
from aiohttp import web

from .app.engine import LEVEL_NAMES, choose_move
from .app.store import Game, GameStore

log = logging.getLogger("chess_bot.web")

WEBAPP_DIR = Path(__file__).parent.parent

LEVEL_DESC = {
    1: "Новичок",
    2: "Любитель",
    3: "Клубный игрок",
    4: "Мастер",
}

MATERIAL = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


# --------------------------------------------------------------------- сессии


@dataclass
class Viewer:
    user_id: int
    name: str
    authed: bool
    chat_id: int | None = None


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict[str, Any] | None:
    """Проверка подписи Telegram WebApp initData (HMAC-SHA256 по bot token)."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return None
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        return None
    auth_date = int(pairs.get("auth_date", "0") or 0)
    if auth_date and time.time() - auth_date > max_age:
        return None
    try:
        pairs["user"] = json.loads(pairs["user"]) if pairs.get("user") else None
    except json.JSONDecodeError:
        return None
    return pairs


def make_viewer(request: web.Request) -> Viewer:
    bot_token = request.app.get("bot_token", "")
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if init_data and bot_token:
        data = validate_init_data(init_data, bot_token)
        if data and data.get("user"):
            user = data["user"]
            return Viewer(int(user["id"]), user.get("first_name") or "Игрок", True)
        log.warning("Отклонена подпись initData")
        raise web.HTTPUnauthorized(text=json.dumps({"error": "bad init data"}), content_type="application/json")

    # режим без Telegram (локальная игра / отладка) — отдельный «игрок браузера»
    cookie = request.cookies.get("kosatka_sid")
    if cookie and cookie.startswith("web:"):
        return Viewer(int(cookie[4:]) or 0, "Гость", False)
    name = request.query.get("as") or "Гость"
    user_id = -int.from_bytes(os.urandom(4), "big") // 2
    viewer = Viewer(user_id, name, False)
    request["new_viewer"] = viewer
    request["guest"] = True
    return viewer


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
    "Access-Control-Max-Age": "86400",
}


@web.middleware
async def viewer_middleware(request: web.Request, handler):
    # GitHub Pages живёт на другом домене: preflight и обычные ответы нужны с CORS
    if request.method == "OPTIONS" and request.path.startswith("/api/"):
        return web.Response(status=204, headers=CORS_HEADERS)

    if request.path.startswith("/api/"):
        viewer = make_viewer(request)
        request["viewer"] = viewer
    response = await handler(request)
    if request.path.startswith("/api/"):
        response.headers.update(CORS_HEADERS)
    new_viewer = request.get("new_viewer")
    if new_viewer is not None and isinstance(response, web.Response):
        response.set_cookie("kosatka_sid", f"web:{new_viewer.user_id}", max_age=30 * 86400,
                            httponly=True, samesite="None", secure=True)
    return response


# --------------------------------------------------------------------- модель


def material_text(board: chess.Board) -> str:
    white = sum(MATERIAL.get(p.piece_type, 0) for p in board.piece_map().values() if p.color == chess.WHITE)
    black = sum(MATERIAL.get(p.piece_type, 0) for p in board.piece_map().values() if p.color == chess.BLACK)
    diff = white - black
    if diff == 0:
        return "материал равный"
    return f"+{abs(diff)} у {'белых' if diff > 0 else 'чёрных'}"


def board_map(board: chess.Board) -> dict[str, str]:
    result = {}
    for square, piece in board.piece_map().items():
        color = "w" if piece.color == chess.WHITE else "b"
        result[chess.square_name(square)] = color + piece.symbol().upper()
    return result


def moves_san(game: Game) -> list[str]:
    board = chess.Board()
    result: list[str] = []
    for uci in game.move_list():
        try:
            move = chess.Move.from_uci(uci)
            result.append(board.san(move))
            board.push(move)
        except ValueError:
            break
    return result


def find_check_square(board: chess.Board) -> str | None:
    if not board.is_check():
        return None
    for square, piece in board.piece_map().items():
        if piece.piece_type == chess.KING and piece.color == board.turn:
            return chess.square_name(square)
    return None


def mood_for(game: Game, color: bool | None) -> str:
    board = game.draw_board()
    if game.status == "waiting":
        return "Ждёт второго игрока"
    if game.status == "finished":
        result = game.result or ""
        if "Ничья" in result:
            return "Косатка зевает"
        if color is None:
            return "Косатка свернулась клубочком"
        if "Сдача" in result:
            return "Косатка прикрыла лапой глаза"
        winner_white = "Белые" in result
        return "Косатка жмурится" if (color == chess.WHITE) == winner_white else "Косатка мурлычет"
    if game.mode == "pve" and game.awaiting_engine:
        return "Косатка думает"
    if board.is_check():
        return "Косатка насторожилась"
    return "Косатка наблюдает"


def game_payload(game: Game, viewer: Viewer, *, join_url: str | None = None,
                 notice: str | None = None) -> dict[str, Any]:
    board = game.draw_board()
    color = game.user_color(viewer.user_id)
    moves = game.move_list()
    legal = []
    if game.status == "active" and color is not None and board.turn == color:
        legal = [m.uci() for m in board.legal_moves]

    # WEBAPP_URL указывает на статический фронт (GitHub Pages):
    # https://<user>.github.io/<repo>/  → ссылка-приглашение ?startapp=<token>
    base = os.environ.get("WEBAPP_URL", "").strip()
    if base and not base.endswith("/"):
        base = base + "/"
    if join_url is None and game.status == "waiting":
        token_row = None
        try:
            token_row = token_for_game(game)
        except Exception:  # noqa: BLE001
            token_row = None
        if token_row and base:
            join_url = f"{base}?startapp={token_row}"

    payload: dict[str, Any] = {
        "id": game.id,
        "mode": game.mode,
        "status": game.status,
        "board": board_map(board),
        "turn": "w" if board.turn == chess.WHITE else "b",
        "you": ("w" if color == chess.WHITE else "b") if color is not None else None,
        "yourTurn": bool(color is not None and board.turn == color and game.status == "active"
                         and not (game.mode == "pve" and game.awaiting_engine)),
        "legal": legal,
        "moves": moves,
        "movesSan": moves_san(game),
        "lastMove": moves[-1] if moves else None,
        "isCheck": board.is_check(),
        "checkSquare": find_check_square(board),
        "result": game.result,
        "resultKind": "draw" if (game.result or "").startswith("Ничья") else "win",
        "mood": mood_for(game, color),
        "material": material_text(board),
        "level": game.level,
        "levelName": LEVEL_NAMES.get(game.level or 2, "Любитель"),
        "players": {"white": game.white_name, "black": game.black_name},
        "joinUrl": join_url,
        "drawOfferBy": game.draw_offer_by,
    }
    if notice:
        payload["notice"] = notice
    return payload


# --------------------------------------------------------------------- ключи


def token_for_game(game: Game) -> str | None:
    return STORE.token_for_game(game.id)


def game_by_key(key: str) -> Game | None:
    """Ключ игры в API: внутренний id или токен приглашения (id × 10^6 + timestamp)."""
    try:
        number = int(key)
    except ValueError:
        return STORE.invite_by_token(key)
    return STORE.get(number)


STORE: GameStore
# Партии, ход бота для которых уже считается в этом процессе. Флаг
# awaiting_engine в базе означает «ход за ботом», а не «уже считаю», поэтому
# отдельный набор: иначе после хода человека бот молча ждал бы сам себя.
ENGINE_BUSY: set[int] = set()


async def engine_move_async(game_id: int) -> Game | None:
    game = STORE.get(game_id)
    if game is None or game.mode != "pve" or game.status != "active":
        return game
    board = game.draw_board()
    human_color = chess.WHITE if game.white_id else chess.BLACK
    if board.turn == human_color:
        STORE.clear_engine_pending(game_id)
        return game
    if game_id in ENGINE_BUSY:
        # уже считаем этот ход в другом запросе — второй расчёт не нужен
        return game
    import asyncio

    ENGINE_BUSY.add(game_id)
    STORE.mark_engine_pending(game_id)
    try:
        move = await asyncio.to_thread(choose_move, board.copy(), game.level or 2)
    except Exception:  # noqa: BLE001 — бот не должен вешать партию
        log.exception("Движок не смог выбрать ход в партии %s", game_id)
        STORE.clear_engine_pending(game_id)
        return game
    finally:
        ENGINE_BUSY.discard(game_id)
    return STORE.after_engine_move(game_id, move.uci())


async def maybe_engine_move(game: Game | None) -> Game | None:
    """Досчитывает ход бота, если сейчас его очередь (партию создали в боте)."""
    if game is None or game.mode != "pve" or game.status != "active":
        return game
    board = game.draw_board()
    human_color = chess.WHITE if game.white_id else chess.BLACK
    if board.turn == human_color:
        return game
    return await engine_move_async(game.id) or game


# --------------------------------------------------------------------- маршруты


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def health(request: web.Request) -> web.Response:
    bot_ok = bool(request.app.get("bot_healthy", False))
    return web.json_response({"ok": True, "telegram": bool(request.app.get("bot_token")), "bot": bot_ok})


async def api_new(request: web.Request) -> web.Response:
    viewer: Viewer = request["viewer"]
    data = await request.json()
    mode = data.get("mode", "pve")
    color_pick = data.get("color", "w")
    color = random.choice([chess.WHITE, chess.BLACK]) if color_pick == "r" else color_pick == "w"
    name = (data.get("playerName") or viewer.name)[:64]

    if mode == "pvp":
        game, _token = STORE.create_pvp(viewer.user_id, name, color)
    else:
        level = int(data.get("level", 2))
        if level not in LEVEL_NAMES:
            level = 2
        game = STORE.create_pve(viewer.user_id, name, color, level)

    if game.mode == "pve":
        # Если человек выбрал чёрных, первый ход за Косаткой — считаем сразу,
        # иначе доска откроется на позиции, где никто не может ходить.
        game = await maybe_engine_move(game) or game
    return web.json_response(game_payload(game, viewer))


async def api_join(request: web.Request) -> web.Response:
    viewer: Viewer = request["viewer"]
    token = request.match_info["token"]
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    name = (data.get("playerName") or viewer.name)[:64]
    game = STORE.invite_by_token(token)
    if game is None:
        return web.json_response({"error": "Приглашение не найдено"}, status=404)
    ok, text, game = STORE.join(game.id, viewer.user_id, name)
    if game is None:
        return web.json_response({"error": text}, status=404)
    return web.json_response(game_payload(game, viewer, notice=text if ok else None))


async def api_last(request: web.Request) -> web.Response:
    viewer: Viewer = request["viewer"]
    games = STORE.user_games(viewer.user_id, limit=1)
    if not games:
        return web.json_response({})
    game = await maybe_engine_move(STORE.get(games[0].id))
    return web.json_response(game_payload(game, viewer))


async def api_game(request: web.Request) -> web.Response:
    viewer: Viewer = request["viewer"]
    game = game_by_key(request.match_info["key"])
    if game is None:
        return web.json_response({"error": "Партия не найдена"}, status=404)
    game = await maybe_engine_move(game)
    return web.json_response(game_payload(game, viewer))


async def api_move(request: web.Request) -> web.Response:
    viewer: Viewer = request["viewer"]
    game = game_by_key(request.match_info["key"])
    if game is None:
        return web.json_response({"error": "Партия не найдена"}, status=404)
    data = await request.json()
    uci = str(data.get("uci", ""))
    ok, text, updated = STORE.apply_move(game.id, viewer.user_id, uci)
    if not ok:
        return web.json_response({"error": text}, status=400)
    if updated and updated.mode == "pve" and updated.status == "active":
        updated = await engine_move_async(updated.id) or updated
    return web.json_response(game_payload(updated, viewer))


async def api_resign(request: web.Request) -> web.Response:
    viewer: Viewer = request["viewer"]
    game = game_by_key(request.match_info["key"])
    if game is None:
        return web.json_response({"error": "Партия не найдена"}, status=404)
    ok, updated = STORE.resign(game.id, viewer.user_id)
    if not ok:
        return web.json_response({"error": "Сейчас нельзя сдаться"}, status=400)
    return web.json_response(game_payload(updated, viewer))


async def api_draw(request: web.Request) -> web.Response:
    viewer: Viewer = request["viewer"]
    game = game_by_key(request.match_info["key"])
    if game is None:
        return web.json_response({"error": "Партия не найдена"}, status=404)
    ok, text, updated = STORE.offer_draw(game.id, viewer.user_id)
    if not ok:
        return web.json_response({"error": text}, status=400)
    return web.json_response(game_payload(updated, viewer, notice=text))


def create_app(store: GameStore, bot_token: str = "") -> web.Application:
    global STORE
    STORE = store
    app = web.Application(middlewares=[viewer_middleware])
    app["bot_token"] = bot_token
    app.router.add_get("/", index)
    app.router.add_get("/webapp/", index)
    app.router.add_static("/static/", WEBAPP_DIR, show_index=False)
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/game/new", api_new)
    app.router.add_post("/api/join/{token}", api_join)
    app.router.add_get("/api/last", api_last)
    app.router.add_get("/api/game/{key}", api_game)
    app.router.add_post("/api/game/{key}/move", api_move)
    app.router.add_post("/api/game/{key}/resign", api_resign)
    app.router.add_post("/api/game/{key}/draw", api_draw)
    return app


async def run_server(host: str, port: int, store: GameStore, bot_token: str = "") -> web.AppRunner:
    app = create_app(store, bot_token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Web App на http://%s:%s/", host, port)
    return runner


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Веб-версия шахмат «Косатка»")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=os.environ.get("CHESS_DB", "chess_bot.db"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    store = GameStore.from_env(default=os.path.abspath(args.db))

    async def _main():
        await run_server(args.host, args.port, store, os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        await asyncio.Event().wait()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        store.close()
