"""Хранилище партий и приглашений в SQLite.

Источник истины — база: партия переживает перезапуск процесса, а не только
питается из памяти. Все изменяющие операции идут через транзакцию, поэтому
два одновременных клика не могут сходить дважды.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass

import chess

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT    NOT NULL,              -- pvp | pve
    status        TEXT    NOT NULL,              -- waiting | active | finished
    fen           TEXT    NOT NULL,
    moves         TEXT    NOT NULL DEFAULT '',   -- UCI через пробел, нужен для ничьей по повторению
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
    token     TEXT PRIMARY KEY,
    game_id   INTEGER NOT NULL,
    host_id   INTEGER NOT NULL,
    created_at REAL   NOT NULL
);

-- у каждого игрока своя доска в своём чате, поэтому и message_id свой
CREATE TABLE IF NOT EXISTS messages (
    game_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (game_id, user_id)
);
"""


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
        board = chess.Board(self.fen)
        return board

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


def _row_to_game(row: sqlite3.Row) -> Game:
    return Game(
        id=row["id"],
        mode=row["mode"],
        status=row["status"],
        fen=row["fen"],
        moves=row["moves"],
        level=row["level"],
        white_id=row["white_id"],
        black_id=row["black_id"],
        white_name=row["white_name"],
        black_name=row["black_name"],
        host_id=row["host_id"],
        result=row["result"],
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        draw_offer_by=row["draw_offer_by"],
        awaiting_engine=row["awaiting_engine"],
        updated_at=row["updated_at"],
    )


class GameStore:
    def __init__(self, path: str = "chess_bot.db") -> None:
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ создание

    def create_pve(self, user_id: int, user_name: str, color: bool, level: int) -> Game:
        board = chess.Board()
        white_id, black_id = (user_id, None) if color else (None, user_id)
        white_name, black_name = (user_name, "Бот") if color else ("Бот", user_name)
        cursor = self.conn.execute(
            """INSERT INTO games (mode, status, fen, level, white_id, black_id, white_name,
                                  black_name, host_id, updated_at)
               VALUES ('pve', 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (board.fen(), level, white_id, black_id, white_name, black_name, user_id, time.time()),
        )
        return self.get(cursor.lastrowid)

    def create_pvp(self, host_id: int, host_name: str, color: bool) -> tuple[Game, str]:
        board = chess.Board()
        white_id, black_id = (host_id, None) if color else (None, host_id)
        white_name, black_name = (host_name, None) if color else (None, host_name)
        cursor = self.conn.execute(
            """INSERT INTO games (mode, status, fen, white_id, black_id, white_name,
                                  black_name, host_id, updated_at)
               VALUES ('pvp', 'waiting', ?, ?, ?, ?, ?, ?, ?)""",
            (board.fen(), white_id, black_id, white_name, black_name, host_id, time.time()),
        )
        game_id = cursor.lastrowid
        token = secrets.token_urlsafe(12)
        self.conn.execute(
            "INSERT INTO invites (token, game_id, host_id, created_at) VALUES (?, ?, ?, ?)",
            (token, game_id, host_id, time.time()),
        )
        return self.get(game_id), token

    # ------------------------------------------------------------------ чтение

    def get(self, game_id: int) -> Game | None:
        row = self.conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return _row_to_game(row) if row else None

    def invite_by_token(self, token: str) -> Game | None:
        row = self.conn.execute(
            "SELECT g.* FROM invites i JOIN games g ON g.id = i.game_id WHERE i.token = ?", (token,)
        ).fetchone()
        return _row_to_game(row) if row else None

    def user_games(self, user_id: int, limit: int = 5) -> list[Game]:
        rows = self.conn.execute(
            """SELECT * FROM games
               WHERE status IN ('waiting', 'active') AND (white_id = ? OR black_id = ?)
               ORDER BY updated_at DESC LIMIT ?""",
            (user_id, user_id, limit),
        ).fetchall()
        return [_row_to_game(row) for row in rows]

    # ------------------------------------------------------------------ PvP

    def join(self, game_id: int, user_id: int, user_name: str) -> tuple[bool, str, Game | None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
            if row is None:
                return False, "Партия не найдена.", None
            game = _row_to_game(row)
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
        finally:
            self.conn.execute("COMMIT")

    def cancel(self, game_id: int, user_id: int) -> bool:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            game = self.get(game_id)
            if game is None or game.host_id != user_id or game.status != "waiting":
                return False
            self.conn.execute(
                "UPDATE games SET status = 'finished', result = 'Партия отменена', updated_at = ? WHERE id = ?",
                (time.time(), game_id),
            )
            return True
        finally:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ ходы

    def apply_move(self, game_id: int, user_id: int | None, uci: str) -> tuple[bool, str, Game | None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
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
        finally:
            self.conn.execute("COMMIT")

    def after_engine_move(self, game_id: int, uci: str) -> Game | None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            game = self.get(game_id)
            if game is None:
                return None
            board = game.draw_board()
            move = chess.Move.from_uci(uci)
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
        finally:
            self.conn.execute("COMMIT")

    def resign(self, game_id: int, user_id: int) -> tuple[bool, Game | None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
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
        finally:
            self.conn.execute("COMMIT")

    def offer_draw(self, game_id: int, user_id: int) -> tuple[bool, str, Game | None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
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
        finally:
            self.conn.execute("COMMIT")

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

    def pending_engine_games(self) -> list[Game]:
        rows = self.conn.execute(
            "SELECT * FROM games WHERE mode = 'pve' AND status = 'active' AND awaiting_engine = 1"
        ).fetchall()
        return [_row_to_game(row) for row in rows]

    def mark_engine_pending(self, game_id: int) -> None:
        self.conn.execute("UPDATE games SET awaiting_engine = 1 WHERE id = ?", (game_id,))

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
