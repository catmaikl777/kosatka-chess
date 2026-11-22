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

import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import chess

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
"""

TABLES = ("games", "invites", "messages")


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

    def create_pve(self, user_id: int, user_name: str, color: bool, level: int) -> Game:
        board = chess.Board()
        white_id, black_id = (user_id, None) if color else (None, user_id)
        white_name, black_name = (user_name, "Бот") if color else ("Бот", user_name)
        game_id = self._insert_id(
            """INSERT INTO games (mode, status, fen, level, white_id, black_id, white_name,
                                  black_name, host_id, updated_at)
               VALUES ('pve', 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (board.fen(), level, white_id, black_id, white_name, black_name, user_id, time.time()),
        )
        return self.get(game_id)

    def create_pvp(self, host_id: int, host_name: str, color: bool) -> tuple[Game, str]:
        board = chess.Board()
        white_id, black_id = (host_id, None) if color else (None, host_id)
        white_name, black_name = (host_name, None) if color else (None, host_name)
        game_id = self._insert_id(
            """INSERT INTO games (mode, status, fen, white_id, black_id, white_name,
                                  black_name, host_id, updated_at)
               VALUES ('pvp', 'waiting', ?, ?, ?, ?, ?, ?, ?)""",
            (board.fen(), white_id, black_id, white_name, black_name, host_id, time.time()),
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

    def user_games(self, user_id: int, limit: int = 5) -> list[Game]:
        rows = self.conn.execute(
            """SELECT * FROM games
               WHERE status IN ('waiting', 'active') AND (white_id = ? OR black_id = ?)
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

            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                return False, "Неизвестный ход.", game
            if move not in board.legal_moves:
                return False, "Так ходить нельзя.", game

            board.push(move)
            result = self._finish_if_over(board)
            moves = (game.moves + " " + uci).strip()
            self.conn.execute(
                """UPDATE games SET fen = ?, moves = ?, result = ?, status = ?, draw_offer_by = NULL,
                          awaiting_engine = ?, updated_at = ? WHERE id = ?""",
                (
                    board.fen(),
                    moves,
                    result,
                    "finished" if result else "active",
                    1 if (game.mode == "pve" and result is None) else 0,
                    time.time(),
                    game_id,
                ),
            )
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
            board.push(move)
            result = self._finish_if_over(board)
            self.conn.execute(
                """UPDATE games SET fen = ?, moves = ?, result = ?, status = ?, awaiting_engine = 0,
                          updated_at = ? WHERE id = ?""",
                (
                    board.fen(),
                    (game.moves + " " + uci).strip(),
                    result,
                    "finished" if result else "active",
                    time.time(),
                    game_id,
                ),
            )
            return self.get(game_id)

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
                    self.conn.execute("TRUNCATE messages, invites, games RESTART IDENTITY")
                else:
                    for table in ("messages", "invites", "games"):
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
