"""Хранилище партий и приглашений: SQLite локально, Postgres на сервере.

Источник истины — база: партия переживает перезапуск процесса, а не только
питается из памяти. Все изменяющие операции идут через транзакцию, поэтому
два одновременных клика не могут сходить дважды.

Какой движок выбрать:
    GameStore("chess_bot.db")                  # SQLite (по умолчанию)
    GameStore("postgresql://user:pw@host/db")  # Postgres (Render, VPS)
    GameStore.from_env()                       # DATABASE_URL или CHESS_DB
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import chess

log = logging.getLogger("chess_bot.store")

# Типы колонок под каждый движок: в Postgres int8 нужен для Telegram ID
# (у пользователей бывают id больше 2^31) и float8 для времени.
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    fen           TEXT    NOT NULL,
    moves         TEXT    NOT NULL DEFAULT '',
    level         INTEGER,
    white_id      INTEGER,
    black_id      INTEGER,
    white_name    TEXT,
    black_name    TEXT,
    host_id       INTEGER NOT NULL,
    result        TEXT,
    chat_id       INTEGER,
    message_id    INTEGER,
    draw_offer_by INTEGER,
    awaiting_engine INTEGER NOT NULL DEFAULT 0,
    time_control  TEXT,
    clock_w       REAL,
    clock_b       REAL,
    turn_started  REAL,
    rated         INTEGER NOT NULL DEFAULT 0,
    rating_w      INTEGER,
    rating_b      INTEGER,
    analysis      TEXT,
    updated_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    token      TEXT PRIMARY KEY,
    game_id    INTEGER NOT NULL,
    host_id    INTEGER NOT NULL,
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    game_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (game_id, user_id)
);

CREATE TABLE IF NOT EXISTS players (
    user_id      INTEGER PRIMARY KEY,
    name         TEXT,
    pve_games    INTEGER NOT NULL DEFAULT 0,
    pve_wins     INTEGER NOT NULL DEFAULT 0,
    pve_losses   INTEGER NOT NULL DEFAULT 0,
    pve_draws    INTEGER NOT NULL DEFAULT 0,
    pvp_games    INTEGER NOT NULL DEFAULT 0,
    pvp_wins     INTEGER NOT NULL DEFAULT 0,
    pvp_losses   INTEGER NOT NULL DEFAULT 0,
    pvp_draws    INTEGER NOT NULL DEFAULT 0,
    resigned     INTEGER NOT NULL DEFAULT 0,
    moves_made   INTEGER NOT NULL DEFAULT 0,
    nick         TEXT,
    rating       INTEGER NOT NULL DEFAULT 1200,
    chat_id      INTEGER,
    last_played  REAL,
    first_played REAL
);

CREATE TABLE IF NOT EXISTS notices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id            BIGSERIAL PRIMARY KEY,
    mode          TEXT   NOT NULL,
    status        TEXT   NOT NULL,
    fen           TEXT   NOT NULL,
    moves         TEXT   NOT NULL DEFAULT '',
    level         INTEGER,
    white_id      BIGINT,
    black_id      BIGINT,
    white_name    TEXT,
    black_name    TEXT,
    host_id       BIGINT NOT NULL,
    result        TEXT,
    chat_id       BIGINT,
    message_id    BIGINT,
    draw_offer_by BIGINT,
    awaiting_engine INTEGER NOT NULL DEFAULT 0,
    time_control  TEXT,
    clock_w       REAL,
    clock_b       REAL,
    turn_started  REAL,
    rated         INTEGER NOT NULL DEFAULT 0,
    rating_w      INTEGER,
    rating_b      INTEGER,
    analysis      TEXT,
    updated_at    DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    token      TEXT PRIMARY KEY,
    game_id    BIGINT NOT NULL,
    host_id    BIGINT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    game_id    BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    chat_id    BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    PRIMARY KEY (game_id, user_id)
);

CREATE TABLE IF NOT EXISTS players (
    user_id      BIGINT PRIMARY KEY,
    name         TEXT,
    pve_games    INTEGER NOT NULL DEFAULT 0,
    pve_wins     INTEGER NOT NULL DEFAULT 0,
    pve_losses   INTEGER NOT NULL DEFAULT 0,
    pve_draws    INTEGER NOT NULL DEFAULT 0,
    pvp_games    INTEGER NOT NULL DEFAULT 0,
    pvp_wins     INTEGER NOT NULL DEFAULT 0,
    pvp_losses   INTEGER NOT NULL DEFAULT 0,
    pvp_draws    INTEGER NOT NULL DEFAULT 0,
    resigned     INTEGER NOT NULL DEFAULT 0,
    moves_made   INTEGER NOT NULL DEFAULT 0,
    nick         TEXT,
    rating       INTEGER NOT NULL DEFAULT 1200,
    chat_id      BIGINT,
    last_played  REAL,
    first_played REAL
);

CREATE TABLE IF NOT EXISTS notices (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    text       TEXT   NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
"""

TABLES = ("games", "invites", "messages", "players")

# контроль времени: "" — без таймера, иначе «минуты+секунды прибавки»
TIME_CONTROLS = ("", "3+2", "5+0", "10+0")
TIME_CONTROL_NAMES = {
    "": "без таймера",
    "3+2": "3 минуты + 2 с",
    "5+0": "5 минут",
    "10+0": "10 минут",
}


def parse_time_control(value: str | None) -> tuple[int | None, int]:
    """"5+0" → (300 секунд, 0 прибавки). Пусто/мусор → без таймера."""
    if not value:
        return None, 0
    try:
        minutes, extra = value.split("+", 1)
        base = int(minutes) * 60
        inc = int(extra)
    except (ValueError, AttributeError):
        return None, 0
    if base <= 0:
        return None, 0
    return base, max(0, inc)


def is_postgres(target: str) -> bool:
    return target.startswith(("postgres://", "postgresql://"))


@dataclass
class Game:
    id: int
    mode: str
    status: str
    fen: str
    moves: str
    level: int | None
    white_id: int | None
    black_id: int | None
    white_name: str | None
    black_name: str | None
    host_id: int
    result: str | None
    chat_id: int | None
    message_id: int | None
    draw_offer_by: int | None
    awaiting_engine: int
    updated_at: float
    time_control: str | None = None
    clock_w: float | None = None
    clock_b: float | None = None
    turn_started: float | None = None
    rated: int = 0
    rating_w: int | None = None
    rating_b: int | None = None
    analysis: str | None = None

    @property
    def board(self) -> chess.Board:
        return chess.Board(self.fen)

    def move_list(self) -> list[str]:
        return self.moves.split() if self.moves else []

    def user_color(self, user_id: int | None) -> bool | None:
        """True — белые, False — чёрные, None — не участник."""
        if user_id is None:
            return None
        if self.white_id == user_id:
            return chess.WHITE
        if self.black_id == user_id:
            return chess.BLACK
        return None

    def draw_board(self, perspective: bool | None = None) -> chess.Board:
        """Доска с историей ходов — так работает правило повторения."""
        board = chess.Board()
        for uci in self.move_list():
            try:
                board.push_uci(uci)
            except ValueError:
                board = chess.Board(self.fen)
                break
        if board.fen(en_passant="fen") != chess.Board(self.fen).fen(en_passant="fen"):
            board = chess.Board(self.fen)
        if perspective is not None:
            board = board.mirror() if board.turn != perspective else board
        return board


_UNSET = object()


class _SqliteConn:
    """Тот же интерфейс, что у обёртки Postgres: execute/fetch и транзакции."""

    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SQLITE_SCHEMA)
        _ensure_extra_columns(self.conn.execute)

    def execute(self, sql: str, params: Sequence[Any] = (), *, fetch: str | None = None):
        cursor = self.conn.execute(sql, tuple(params))
        if fetch == "one":
            return cursor.fetchone()
        if fetch == "all":
            return cursor.fetchall()
        return cursor

    def transaction(self):
        return _Transaction(self.conn, "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")

    def table_rows(self, table: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(f"SELECT * FROM {table}")]

    def close(self) -> None:
        self.conn.close()


class _Transaction:
    def __init__(self, conn, begin: str, commit: str, rollback: str) -> None:
        self._conn = conn
        self._begin, self._commit, self._rollback = begin, commit, rollback

    def __enter__(self):
        self._conn.execute(self._begin)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._conn.execute(self._commit)
        else:
            self._conn.execute(self._rollback)
        return False


_EXTRA_COLUMNS = (
    ("games", "time_control", "TEXT"),
    ("games", "clock_w", "REAL"),
    ("games", "clock_b", "REAL"),
    ("games", "turn_started", "REAL"),
    ("games", "rated", "INTEGER NOT NULL DEFAULT 0"),
    ("games", "rating_w", "INTEGER"),
    ("games", "rating_b", "INTEGER"),
    ("games", "analysis", "TEXT"),
    ("players", "nick", "TEXT"),
    ("players", "rating", "INTEGER NOT NULL DEFAULT 1200"),
    ("players", "chat_id", "INTEGER"),
)


def _ensure_extra_columns(run) -> None:
    """Досоздаёт колонки, появившиеся после первых релизов.

    CREATE TABLE IF NOT EXISTS старую таблицу не меняет, поэтому при каждом
    подключении пробуем ADD COLUMN и молча пропускаем «already exists».
    """
    for table, column, ddl in _EXTRA_COLUMNS:
        try:
            run(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except Exception:  # noqa: BLE001 — колонка уже есть
            pass


_POSTGRES_ID_COLUMNS = (
    ("games", "white_id"),
    ("games", "black_id"),
    ("games", "host_id"),
    ("games", "chat_id"),
    ("games", "message_id"),
    ("games", "draw_offer_by"),
    ("invites", "game_id"),
    ("invites", "host_id"),
    ("messages", "game_id"),
    ("messages", "user_id"),
    ("messages", "chat_id"),
    ("messages", "message_id"),
    ("players", "user_id"),
)


def _widen_id_columns(cur) -> None:
    """Старые базы создавались с int4 — расширяем id-колонки до int8.

    Современные Telegram id больше 2^31, и INSERT падал с
    NumericValueOutOfRange. CREATE TABLE IF NOT EXISTS тип не меняет,
    поэтому при каждом подключении сверяем и при необходимости ALTER.
    """
    for table, column in _POSTGRES_ID_COLUMNS:
        cur.execute(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_schema = current_schema()"
            " AND table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
        if not row:
            continue
        kind = row["data_type"] if isinstance(row, dict) else row[0]
        if kind in ("integer", "smallint"):
            try:
                cur.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT")
            except Exception as error:  # noqa: BLE001 — не роняем старт сервиса
                log.warning("Не удалось расширить %s.%s: %s", table, column, error)


class _PostgresConn:
    """Обёртка psycopg3: ? → %s, транзакция на каждый вызов."""

    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        import psycopg  # импорт здесь, чтобы SQLite-режим не требовал psycopg
        from psycopg.rows import dict_row

        self._psycopg = psycopg
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute(POSTGRES_SCHEMA)
            _widen_id_columns(cur)
            _ensure_extra_columns(cur.execute)

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params: Sequence[Any] = (), *, fetch: str | None = None):
        with self.conn.cursor() as cur:
            cur.execute(self._sql(sql), tuple(params))
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None

    def transaction(self):
        return _Transaction(self.conn, "BEGIN", "COMMIT", "ROLLBACK")

    def table_rows(self, table: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} ORDER BY 1")
            return list(cur.fetchall())

    def close(self) -> None:
        self.conn.close()


def _row_value(row: Any, key: str) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    return row[key]


def _row_to_game(row: Any) -> Game:
    return Game(
        id=_row_value(row, "id"),
        mode=_row_value(row, "mode"),
        status=_row_value(row, "status"),
        fen=_row_value(row, "fen"),
        moves=_row_value(row, "moves"),
        level=_row_value(row, "level"),
        white_id=_row_value(row, "white_id"),
        black_id=_row_value(row, "black_id"),
        white_name=_row_value(row, "white_name"),
        black_name=_row_value(row, "black_name"),
        host_id=_row_value(row, "host_id"),
        result=_row_value(row, "result"),
        chat_id=_row_value(row, "chat_id"),
        message_id=_row_value(row, "message_id"),
        draw_offer_by=_row_value(row, "draw_offer_by"),
        awaiting_engine=_row_value(row, "awaiting_engine"),
        updated_at=float(_row_value(row, "updated_at") or 0),
        time_control=_row_value(row, "time_control"),
        clock_w=_row_value(row, "clock_w"),
        clock_b=_row_value(row, "clock_b"),
        turn_started=_row_value(row, "turn_started"),
        rated=_row_value(row, "rated") or 0,
        rating_w=_row_value(row, "rating_w"),
        rating_b=_row_value(row, "rating_b"),
        analysis=_row_value(row, "analysis"),
    )


class GameStore:
    def __init__(self, target: str = "chess_bot.db") -> None:
        self.target = target
        self.dialect = "postgres" if is_postgres(target) else "sqlite"
        self.conn = _PostgresConn(target) if self.dialect == "postgres" else _SqliteConn(target)

    @classmethod
    def from_env(cls, default: str = "chess_bot.db") -> "GameStore":
        """DATABASE_URL (Render) важнее CHESS_DB (локальный путь к SQLite)."""
        target = os.environ.get("DATABASE_URL") or os.environ.get("CHESS_DB") or default
        return cls(target)

    # ------------------------------------------------------------------ создание

    def _insert_id(self, sql: str, params: Sequence[Any]) -> int:
        if self.conn.dialect == "postgres":
            row = self.conn.execute(sql + " RETURNING id", params, fetch="one")
            return int(_row_value(row, "id"))
        cursor = self.conn.execute(sql, params)
        return int(cursor.lastrowid)

    def create_pve(self, user_id: int, user_name: str, color: bool, level: int,
                   time_control: str | None = None) -> Game:
        board = chess.Board()
        base, _ = parse_time_control(time_control)
        white_id, black_id = (user_id, None) if color else (None, user_id)
        white_name, black_name = (user_name, "Бот") if color else ("Бот", user_name)
        now = time.time()
        game_id = self._insert_id(
            """INSERT INTO games (mode, status, fen, level, white_id, black_id, white_name,
                                  black_name, host_id, updated_at, time_control,
                                  clock_w, clock_b, turn_started)
               VALUES ('pve', 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (board.fen(), level, white_id, black_id, white_name, black_name, user_id, now,
             time_control or None, base, base, now if base else None),
        )
        return self.get(game_id)

    def create_pvp(self, host_id: int, host_name: str, color: bool,
                   time_control: str | None = None) -> tuple[Game, str]:
        board = chess.Board()
        base, _ = parse_time_control(time_control)
        white_id, black_id = (host_id, None) if color else (None, host_id)
        white_name, black_name = (host_name, None) if color else (None, host_name)
        now = time.time()
        game_id = self._insert_id(
            """INSERT INTO games (mode, status, fen, white_id, black_id, white_name,
                                  black_name, host_id, updated_at, time_control,
                                  clock_w, clock_b, turn_started)
               VALUES ('pvp', 'waiting', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (board.fen(), white_id, black_id, white_name, black_name, host_id, now,
             time_control or None, base, base, now if base else None),
        )
        token = secrets.token_urlsafe(12)
        self.conn.execute(
            "INSERT INTO invites (token, game_id, host_id, created_at) VALUES (?, ?, ?, ?)",
            (token, game_id, host_id, time.time()),
        )
        return self.get(game_id), token

    # ------------------------------------------------------------------ чтение

    def get(self, game_id: int) -> Game | None:
        row = self.conn.execute("SELECT * FROM games WHERE id = ?", (game_id,), fetch="one")
        return _row_to_game(row) if row else None

    def invite_by_token(self, token: str) -> Game | None:
        row = self.conn.execute(
            "SELECT g.* FROM invites i JOIN games g ON g.id = i.game_id WHERE i.token = ?",
            (token,),
            fetch="one",
        )
        return _row_to_game(row) if row else None

    def token_for_game(self, game_id: int) -> str | None:
        row = self.conn.execute("SELECT token FROM invites WHERE game_id = ?", (game_id,), fetch="one")
        return _row_value(row, "token") if row else None

    def user_games(self, user_id: int, limit: int = 5, *, finished: bool = False) -> list[Game]:
        statuses = "('finished')" if finished else "('waiting', 'active')"
        rows = self.conn.execute(
            f"""SELECT * FROM games
               WHERE status IN {statuses} AND (white_id = ? OR black_id = ?)
               ORDER BY updated_at DESC LIMIT ?""",
            (user_id, user_id, limit),
            fetch="all",
        )
        return [_row_to_game(row) for row in rows]

    # ------------------------------------------------------------------ PvP

    def join(self, game_id: int, user_id: int, user_name: str) -> tuple[bool, str, Game | None]:
        with self.conn.transaction():
            game = self.get(game_id)
            if game is None:
                return False, "Партия не найдена.", None
            if game.mode != "pvp":
                return False, "Это не партия для двоих.", game
            if user_id in (game.white_id, game.black_id):
                return False, "Вы уже участвуете в этой партии.", game
            if game.status != "waiting":
                return False, "К этой партии уже присоединились.", game
            if game.white_id is None:
                self.conn.execute("UPDATE games SET white_id = ?, white_name = ?, status = 'active', updated_at = ? WHERE id = ?",
                                  (user_id, user_name, time.time(), game_id))
            else:
                self.conn.execute("UPDATE games SET black_id = ?, black_name = ?, status = 'active', updated_at = ? WHERE id = ?",
                                  (user_id, user_name, time.time(), game_id))
            self.touch_player(user_id, user_name)
            return True, "Вы присоединились к партии.", self.get(game_id)

    def cancel(self, game_id: int, user_id: int) -> bool:
        with self.conn.transaction():
            game = self.get(game_id)
            if game is None or game.host_id != user_id or game.status != "waiting":
                return False
            self.conn.execute(
                "UPDATE games SET status = 'finished', result = 'Партия отменена', updated_at = ? WHERE id = ?",
                (time.time(), game_id),
            )
            return True

    # ------------------------------------------------------------------ ходы

    def apply_move(self, game_id: int, user_id: int | None, uci: str) -> tuple[bool, str, Game | None]:
        with self.conn.transaction():
            game = self.get(game_id)
            if game is None:
                return False, "Партия не найдена.", None
            if game.status != "active":
                return False, "Партия не идёт.", game

            board = game.draw_board()
            if game.mode == "pvp":
                color = game.user_color(user_id)
                if color is None:
                    return False, "Вы не участник этой партии.", game
                if board.turn != color:
                    return False, "Сейчас не ваш ход.", game
            else:
                human_color = chess.WHITE if game.white_id else chess.BLACK
                if user_id != game.host_id:
                    return False, "Это чужая партия.", game
                if board.turn != human_color:
                    return False, "Сейчас ходит бот, подождите.", game

            now = time.time()
            mover_side = 0 if board.turn == chess.WHITE else 1
            clocks, turn_started, expired = self._spend_time(game, board.turn, now)
            if expired is not None:
                return False, "Время вышло.", self._finish_time(game_id, expired)

            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                return False, "Неизвестный ход.", game
            if move not in board.legal_moves:
                return False, "Так ходить нельзя.", game

            board.push(move)
            result = self._finish_if_over(board)
            moves = (game.moves + " " + uci).strip()
            if result is None and clocks is not None:
                _, increment = parse_time_control(game.time_control)
                clocks[mover_side] = round(clocks[mover_side] + increment, 1)
            elif result is not None:
                turn_started = None
            self.conn.execute(
                """UPDATE games SET fen = ?, moves = ?, result = ?, status = ?, draw_offer_by = NULL,
                          awaiting_engine = ?, clock_w = ?, clock_b = ?, turn_started = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    board.fen(),
                    moves,
                    result,
                    "finished" if result else "active",
                    1 if (game.mode == "pve" and result is None) else 0,
                    clocks[0] if clocks else game.clock_w,
                    clocks[1] if clocks else game.clock_b,
                    turn_started,
                    now,
                    game_id,
                ),
            )
            self.record_result(self.get(game_id))
            self.bump_player(user_id, "moves_made")
            return True, "ok", self.get(game_id)

    def after_engine_move(self, game_id: int, uci: str) -> Game | None:
        with self.conn.transaction():
            game = self.get(game_id)
            if game is None:
                return None
            board = game.draw_board()
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                return game
            if move not in board.legal_moves:
                return game
            now = time.time()
            mover_side = 0 if board.turn == chess.WHITE else 1
            clocks, turn_started, expired = self._spend_time(game, board.turn, now)
            if expired is not None:
                return self._finish_time(game_id, expired)
            board.push(move)
            result = self._finish_if_over(board)
            if result is None and clocks is not None:
                _, increment = parse_time_control(game.time_control)
                clocks[mover_side] = round(clocks[mover_side] + increment, 1)
            elif result is not None:
                turn_started = None
            self.conn.execute(
                """UPDATE games SET fen = ?, moves = ?, result = ?, status = ?, awaiting_engine = 0,
                          clock_w = ?, clock_b = ?, turn_started = ?, updated_at = ? WHERE id = ?""",
                (
                    board.fen(),
                    (game.moves + " " + uci).strip(),
                    result,
                    "finished" if result else "active",
                    clocks[0] if clocks else game.clock_w,
                    clocks[1] if clocks else game.clock_b,
                    turn_started,
                    now,
                    game_id,
                ),
            )
            self.record_result(self.get(game_id))
            return self.get(game_id)

    # ------------------------------------------------------------------ часы

    def _spend_time(self, game: Game, turn: bool, now: float):
        """Списывает время хода. Возвращает (часы, начало хода, просрочивший)."""
        base, _ = parse_time_control(game.time_control)
        if not base:
            return None, None, None
        clocks = [game.clock_w, game.clock_b]
        if clocks[0] is None or clocks[1] is None:
            clocks = [float(base), float(base)]
        side = 0 if turn == chess.WHITE else 1
        spent = max(0.0, now - (game.turn_started or now))
        clocks[side] = round(clocks[side] - spent, 1)
        if clocks[side] <= 0:
            return clocks, None, side
        return clocks, now, None

    def _finish_time(self, game_id: int, loser_side: int) -> Game | None:
        winner = "Белые" if loser_side == 1 else "Чёрные"
        self.conn.execute(
            """UPDATE games SET status = 'finished', result = ?, awaiting_engine = 0,
                      turn_started = NULL, updated_at = ? WHERE id = ?""",
            (f"Время вышло — победа: {winner}", time.time(), game_id),
        )
        game = self.get(game_id)
        self.record_result(game)
        return game

    def tick_clock(self, game_id: int) -> Game | None:
        """Проверяет флаг: если время вышло — завершает партию."""
        game = self.get(game_id)
        if game is None or game.status != "active":
            return game
        base, _ = parse_time_control(game.time_control)
        if not base:
            return game
        board = game.draw_board()
        clocks, turn_started, expired = self._spend_time(game, board.turn, time.time())
        if expired is not None:
            return self._finish_time(game_id, expired)
        if clocks is None:
            return game
        # остаток сохраняем, иначе часы в интерфейсе стоят до следующего хода
        self.conn.execute(
            "UPDATE games SET clock_w = ?, clock_b = ?, turn_started = ? WHERE id = ?",
            (clocks[0], clocks[1], turn_started, game_id),
        )
        return self.get(game_id)

    def set_time_control(self, game_id: int, time_control: str | None) -> None:
        base, _ = parse_time_control(time_control)
        self.conn.execute(
            "UPDATE games SET time_control = ?, clock_w = ?, clock_b = ?, turn_started = ? WHERE id = ?",
            (time_control or None, base, base, time.time() if base else None, game_id),
        )

    def resign(self, game_id: int, user_id: int) -> tuple[bool, Game | None]:
        with self.conn.transaction():
            game = self.get(game_id)
            if game is None or game.status != "active":
                return False, game
            color = game.user_color(user_id)
            if color is None:
                return False, game
            winner = "Чёрные" if color == chess.WHITE else "Белые"
            self.conn.execute(
                "UPDATE games SET status = 'finished', result = ?, awaiting_engine = 0, updated_at = ? WHERE id = ?",
                (f"Сдача соперника — победа: {winner}", time.time(), game_id),
            )
            self.bump_player(user_id, "resigned")
            self.record_result(self.get(game_id))
            return True, self.get(game_id)

    def offer_draw(self, game_id: int, user_id: int) -> tuple[bool, str, Game | None]:
        with self.conn.transaction():
            game = self.get(game_id)
            if game is None or game.status != "active":
                return False, "Партия не идёт.", game
            if game.user_color(user_id) is None:
                return False, "Вы не участник этой партии.", game
            if game.mode == "pve":
                return False, "Бот не принимает ничьи.", game
            if game.draw_offer_by == user_id:
                return False, "Вы уже предложили ничью.", game
            if game.draw_offer_by is not None and game.draw_offer_by != user_id:
                self.conn.execute(
                    "UPDATE games SET status = 'finished', result = 'Ничья по соглашению', updated_at = ? WHERE id = ?",
                    (time.time(), game_id),
                )
                self.record_result(self.get(game_id))
                return True, "Ничья принята.", self.get(game_id)
            self.conn.execute("UPDATE games SET draw_offer_by = ?, updated_at = ? WHERE id = ?",
                              (user_id, time.time(), game_id))
            return True, "Предложение ничьей отправлено.", self.get(game_id)

    def decline_draw(self, game_id: int, user_id: int) -> tuple[bool, Game | None]:
        game = self.get(game_id)
        if game is None or game.draw_offer_by in (None, user_id):
            return False, game
        self.conn.execute("UPDATE games SET draw_offer_by = NULL, updated_at = ? WHERE id = ?",
                          (time.time(), game_id))
        return True, self.get(game_id)

    def set_message(self, game_id: int, chat_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE games SET chat_id = ?, message_id = ? WHERE id = ?",
                          (chat_id, message_id, game_id))

    def remember_message(self, game_id: int, user_id: int, chat_id: int, message_id: int) -> None:
        if self.conn.dialect == "postgres":
            sql = """INSERT INTO messages (game_id, user_id, chat_id, message_id) VALUES (?, ?, ?, ?)
                     ON CONFLICT (game_id, user_id) DO UPDATE SET chat_id = EXCLUDED.chat_id,
                                                                  message_id = EXCLUDED.message_id"""
        else:
            sql = """INSERT INTO messages (game_id, user_id, chat_id, message_id) VALUES (?, ?, ?, ?)
                     ON CONFLICT(game_id, user_id) DO UPDATE SET chat_id = excluded.chat_id,
                                                                 message_id = excluded.message_id"""
        self.conn.execute(sql, (game_id, user_id, chat_id, message_id))

    def known_messages(self, game_id: int) -> list[tuple[int, int, int]]:
        rows = self.conn.execute(
            "SELECT user_id, chat_id, message_id FROM messages WHERE game_id = ?", (game_id,), fetch="all"
        )
        return [(_row_value(row, "user_id"), _row_value(row, "chat_id"), _row_value(row, "message_id")) for row in rows]

    def pending_engine_games(self) -> list[Game]:
        rows = self.conn.execute(
            "SELECT * FROM games WHERE mode = 'pve' AND status = 'active' AND awaiting_engine = 1", fetch="all"
        )
        return [_row_to_game(row) for row in rows]

    def mark_engine_pending(self, game_id: int) -> None:
        self.conn.execute("UPDATE games SET awaiting_engine = 1 WHERE id = ?", (game_id,))

    def clear_engine_pending(self, game_id: int) -> None:
        self.conn.execute("UPDATE games SET awaiting_engine = 0 WHERE id = ?", (game_id,))

    # ------------------------------------------------------------------ перенос данных

    def export_tables(self) -> dict[str, list[dict[str, Any]]]:
        return {table: self.conn.table_rows(table) for table in TABLES}

    def import_tables(self, data: dict[str, list[dict[str, Any]]], *,
                      replace: bool = True) -> dict[str, int]:
        """Влить дамп в текущую базу. replace=True — сначала очистить таблицы."""
        counts: dict[str, int] = {}
        with self.conn.transaction():
            if replace:
                if self.conn.dialect == "postgres":
                    self.conn.execute("TRUNCATE messages, invites, games, players RESTART IDENTITY")
                else:
                    for table in ("messages", "invites", "games", "players"):
                        self.conn.execute(f"DELETE FROM {table}")
                    self.conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('games')")
            for table in TABLES:
                rows = data.get(table) or []
                if not rows:
                    counts[table] = 0
                    continue
                columns = list(rows[0].keys())
                placeholders = ", ".join("?" for _ in columns)
                sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
                if self.conn.dialect == "sqlite" and table == "games":
                    try:
                        self.conn.execute("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('games', 0)")
                    except sqlite3.OperationalError:
                        pass
                for row in rows:
                    values = [row.get(column) for column in columns]
                    self.conn.execute(sql, values)
                counts[table] = len(rows)
            # в SQLite AUTOINCREMENT требует пересчитать счётчик после ручных id
            if self.conn.dialect == "sqlite":
                max_id = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM games", fetch="one")
                self.conn.execute("DELETE FROM sqlite_sequence WHERE name = 'games'")
                self.conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('games', ?)",
                                  (_row_value(max_id, "m"),))
        return counts

    # ------------------------------------------------------------------ профиль

    def save_chat_id(self, user_id: int | None, chat_id: int | None) -> None:
        if user_id is None or chat_id is None:
            return
        self.touch_player(user_id, None)
        self.conn.execute("UPDATE players SET chat_id = ? WHERE user_id = ?", (chat_id, user_id))

    def player_chat(self, user_id: int) -> int | None:
        row = self.conn.execute("SELECT chat_id FROM players WHERE user_id = ?", (user_id,), fetch="one")
        value = _row_value(row, "chat_id") if row else None
        return int(value) if value else None

    def queue_notice(self, user_id: int | None, text: str) -> None:
        """Уведомление, которое бот отправит при следующем опросе."""
        if user_id is None or not text:
            return
        self.conn.execute(
            "INSERT INTO notices (user_id, text, created_at) VALUES (?, ?, ?)",
            (user_id, text[:400], time.time()),
        )

    def take_notices(self, limit: int = 20) -> list[tuple[int, int, str]]:
        rows = self.conn.execute(
            "SELECT id, user_id, text FROM notices ORDER BY id LIMIT ?", (limit,), fetch="all"
        )
        if not rows:
            return []
        ids = [_row_value(row, "id") for row in rows]
        placeholders = ", ".join("?" for _ in ids)
        self.conn.execute(f"DELETE FROM notices WHERE id IN ({placeholders})", tuple(ids))
        return [(_row_value(row, "user_id"), _row_value(row, "id"), _row_value(row, "text")) for row in rows]

    def set_nick(self, user_id: int, nick: str | None) -> None:
        clean = (nick or "").strip()[:24]
        self.touch_player(user_id, None)
        self.conn.execute(
            "UPDATE players SET nick = ? WHERE user_id = ?",
            (clean or None, user_id),
        )

    def player_rating(self, user_id: int | None) -> int:
        if user_id is None:
            return 1200
        row = self.conn.execute("SELECT rating FROM players WHERE user_id = ?", (user_id,), fetch="one")
        value = _row_value(row, "rating") if row else None
        return int(value) if value else 1200

    def player_profile(self, user_id: int | None, fallback_name: str | None = None) -> dict[str, Any]:
        if user_id is None:
            return {"user_id": None, "name": fallback_name, "nick": None,
                    "rating": 1200, "stats": {}}
        row = self.conn.execute("SELECT * FROM players WHERE user_id = ?", (user_id,), fetch="one")
        stats = self.player_stats(user_id)
        return {
            "user_id": user_id,
            "name": (_row_value(row, "name") if row else None) or fallback_name,
            "nick": _row_value(row, "nick") if row else None,
            "rating": int((_row_value(row, "rating") if row else None) or 1200),
            "stats": stats,
        }

    def _apply_elo(self, game: Game | None) -> None:
        """Рейтинг меняем только в рейтинговых PvP-партиях с двумя игроками."""
        if game is None or game.status != "finished" or game.mode != "pvp":
            return
        if not game.result or game.white_id is None or game.black_id is None or game.rated:
            return
        white_rating = self.player_rating(game.white_id)
        black_rating = self.player_rating(game.black_id)
        if "Ничья" in game.result:
            score = 0.5
        elif "Белые" in game.result:
            score = 1.0
        else:
            score = 0.0
        expected = 1 / (1 + 10 ** ((black_rating - white_rating) / 400))
        k = 24
        new_white = round(white_rating + k * (score - expected))
        new_black = round(black_rating + k * ((1 - score) - (1 - expected)))
        self.touch_player(game.white_id, None)
        self.touch_player(game.black_id, None)
        self.conn.execute("UPDATE players SET rating = ? WHERE user_id = ?", (new_white, game.white_id))
        self.conn.execute("UPDATE players SET rating = ? WHERE user_id = ?", (new_black, game.black_id))
        self.conn.execute(
            "UPDATE games SET rated = 1, rating_w = ?, rating_b = ? WHERE id = ?",
            (new_white, new_black, game.id),
        )

    def save_analysis(self, game_id: int, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE games SET analysis = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), game_id),
        )

    def load_analysis(self, game: Game | None) -> dict[str, Any] | None:
        if game is None or not game.analysis:
            return None
        try:
            return json.loads(game.analysis)
        except (TypeError, ValueError):
            return None

    def rematch(self, game_id: int, user_id: int) -> tuple[bool, str, Game | None]:
        old = self.get(game_id)
        if old is None:
            return False, "Партия не найдена.", None
        if old.status != "finished":
            return False, "Сначала доиграйте партию.", old
        color = old.user_color(user_id)
        if color is None:
            return False, "Вы не участник этой партии.", old
        profile = self.player_profile(user_id)
        name = profile["nick"] or profile["name"] or "Игрок"
        if old.mode == "pve":
            game = self.create_pve(user_id, name, color, old.level or 2, old.time_control)
            return True, "ok", game
        other_id = old.black_id if color == chess.WHITE else old.white_id
        other_profile = self.player_profile(other_id)
        fallback = old.black_name if color == chess.WHITE else old.white_name
        other_name = other_profile["nick"] or other_profile["name"] or fallback
        game, _token = self.create_pvp(user_id, name, color, old.time_control)
        if other_id is not None:
            self.join(game.id, other_id, other_name or "Игрок")
        return True, "ok", self.get(game.id)

    # ------------------------------------------------------------------ статистика

    STAT_COLUMNS = (
        "pve_games", "pve_wins", "pve_losses", "pve_draws",
        "pvp_games", "pvp_wins", "pvp_losses", "pvp_draws",
        "resigned", "moves_made",
    )

    def touch_player(self, user_id: int | None, name: str | None) -> None:
        if user_id is None:
            return
        now = time.time()
        row = self.conn.execute("SELECT * FROM players WHERE user_id = ?", (user_id,), fetch="one")
        if row is None:
            self.conn.execute(
                """INSERT INTO players (user_id, name, first_played, last_played) VALUES (?, ?, ?, ?)""",
                (user_id, name, now, now),
            )
        else:
            self.conn.execute(
                "UPDATE players SET name = COALESCE(?, name), last_played = ? WHERE user_id = ?",
                (name, now, user_id),
            )

    def bump_player(self, user_id: int | None, column: str, amount: int = 1) -> None:
        """Атомарный инкремент счётчика игрока (только из белого списка колонок)."""
        if user_id is None or column not in self.STAT_COLUMNS:
            return
        self.touch_player(user_id, None)
        self.conn.execute(f"UPDATE players SET {column} = {column} + ? WHERE user_id = ?", (amount, user_id))

    def record_result(self, game: Game) -> None:
        """После завершения партии пишем результат обоим игрокам."""
        if not game.result or game.status != "finished":
            return
        result = game.result
        if "Сдача" in result or "мат" in result or "Победа" in result:
            winner_white = "Белые" in result
        else:
            winner_white = None
        draw = "Ничья" in result

        for user_id, color in ((game.white_id, chess.WHITE), (game.black_id, chess.BLACK)):
            if user_id is None:
                continue
            prefix = "pve" if game.mode == "pve" else "pvp"
            self.bump_player(user_id, f"{prefix}_games")
            if draw:
                self.bump_player(user_id, f"{prefix}_draws")
            elif winner_white is not None:
                won = (color == chess.WHITE) == winner_white
                self.bump_player(user_id, f"{prefix}_wins" if won else f"{prefix}_losses")
        self._apply_elo(game)

    def player_stats(self, user_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM players WHERE user_id = ?", (user_id,), fetch="one")
        stats = {key: (_row_value(row, key) or 0) for key in ("user_id", "name", *self.STAT_COLUMNS)}
        stats["name"] = _row_value(row, "name") if row else None
        stats["last_played"] = _row_value(row, "last_played") if row else None
        stats["first_played"] = _row_value(row, "first_played") if row else None
        for prefix in ("pve", "pvp"):
            games = stats[f"{prefix}_games"]
            stats[f"{prefix}_rate"] = round(100 * stats[f"{prefix}_wins"] / games) if games else 0
        total_games = stats["pve_games"] + stats["pvp_games"]
        total_wins = stats["pve_wins"] + stats["pvp_wins"]
        stats["total_games"] = total_games
        stats["total_wins"] = total_wins
        stats["total_draws"] = stats["pve_draws"] + stats["pvp_draws"]
        stats["total_rate"] = round(100 * total_wins / total_games) if total_games else 0
        return stats

    def top_players(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT *, (pve_wins + pvp_wins) AS total_wins,
                      (pve_games + pvp_games) AS total_games
               FROM players WHERE (pve_games + pvp_games) > 0
               ORDER BY total_wins DESC, total_games ASC, user_id ASC LIMIT ?""",
            (limit,),
            fetch="all",
        )
        return [dict(row) if isinstance(row, dict) else {k: row[k] for k in row.keys()} for row in rows]

    def recent_games(self, user_id: int, limit: int = 5) -> list[Game]:
        rows = self.conn.execute(
            """SELECT * FROM games
               WHERE status = 'finished' AND (white_id = ? OR black_id = ?)
               ORDER BY updated_at DESC LIMIT ?""",
            (user_id, user_id, limit),
            fetch="all",
        )
        return [_row_to_game(row) for row in rows]

    # ------------------------------------------------------------------ служебное

    @staticmethod
    def _finish_if_over(board: chess.Board) -> str | None:
        if not board.is_game_over(claim_draw=True):
            return None
        outcome = board.outcome(claim_draw=True)
        if outcome is None:
            return "Партия окончена"
        if outcome.winner is None:
            reason = {
                chess.Termination.STALEMATE: "пат",
                chess.Termination.INSUFFICIENT_MATERIAL: "недостаточно материала",
                chess.Termination.THREEFOLD_REPETITION: "троекратное повторение",
                chess.Termination.FIVEFOLD_REPETITION: "пятикратное повторение",
                chess.Termination.SEVENTYFIVE_MOVES: "правило 75 ходов",
                chess.Termination.FIFTY_MOVES: "правило 50 ходов",
            }.get(outcome.termination, "ничья")
            return f"Ничья ({reason})"
        winner = "Белые" if outcome.winner == chess.WHITE else "Чёрные"
        reason = "мат" if outcome.termination == chess.Termination.CHECKMATE else "игра окончена"
        return f"Победа: {winner} ({reason})"

    def close(self) -> None:
        self.conn.close()
