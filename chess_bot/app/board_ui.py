"""Отрисовка доски и сборка inline-клавиатуры.

Доска — текст с Unicode-фигурами: без картинок и без Pillow.
Один и тот же message редактируется при каждом ходе, поэтому вид не мигает.
"""

from __future__ import annotations

import chess
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

GLYPHS = {
    (chess.WHITE, chess.PAWN): "♙",
    (chess.WHITE, chess.KNIGHT): "♘",
    (chess.WHITE, chess.BISHOP): "♗",
    (chess.WHITE, chess.ROOK): "♖",
    (chess.WHITE, chess.QUEEN): "♕",
    (chess.WHITE, chess.KING): "♔",
    (chess.BLACK, chess.PAWN): "♟",
    (chess.BLACK, chess.KNIGHT): "♞",
    (chess.BLACK, chess.BISHOP): "♝",
    (chess.BLACK, chess.ROOK): "♜",
    (chess.BLACK, chess.QUEEN): "♛",
    (chess.BLACK, chess.KING): "♚",
}

CALLBACK_LIMIT = 64
EMPTY = "·"


def render_board(board: chess.Board, perspective: bool = chess.WHITE, last_move: chess.Move | None = None) -> str:
    """Доска с точки зрения цвета perspective.

    Ширина строки — постоянная, поэтому текст остаётся ровным в <pre>.
    """
    ranks = range(7, -1, -1) if perspective == chess.WHITE else range(0, 8)
    files = range(0, 8) if perspective == chess.WHITE else range(7, -1, -1)
    marks = {last_move.from_square, last_move.to_square} if last_move else set()

    lines: list[str] = []
    for rank in ranks:
        cells: list[str] = []
        for file in files:
            square = chess.square(file, rank)
            piece = board.piece_at(square)
            if piece is not None:
                cells.append(GLYPHS[(piece.color, piece.piece_type)])
            elif square in marks:
                cells.append("×")
            else:
                cells.append(EMPTY)
        lines.append(f"{rank + 1} " + " ".join(cells))
    labels = "abcdefgh" if perspective == chess.WHITE else "hgfedcba"
    lines.append("  " + " ".join(labels))
    return "\n".join(lines)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    if len(data.encode()) > CALLBACK_LIMIT:
        raise ValueError(f"callback_data слишком длинный: {data!r}")
    return InlineKeyboardButton(text=text, callback_data=data)


def piece_keyboard(game_id: int, board: chess.Board,
                   top_row: list[InlineKeyboardButton] | None = None) -> InlineKeyboardMarkup:
    """Фигуры текущего игрока, у которых есть хотя бы один легальный ход."""
    turn = board.turn
    squares = sorted(
        {m.from_square for m in board.legal_moves if board.color_at(m.from_square) == turn},
        key=lambda sq: (-chess.square_rank(sq), chess.square_file(sq)),
    )
    rows: list[list[InlineKeyboardButton]] = []
    if top_row:
        rows.append(top_row)
    current: list[InlineKeyboardButton] = []
    for square in squares:
        piece = board.piece_at(square)
        label = f"{GLYPHS[(piece.color, piece.piece_type)]}{chess.square_name(square)}"
        current.append(_btn(label, f"g{game_id}:p{square}"))
        if len(current) == 4:
            rows.append(current)
            current = []
    if current:
        rows.append(current)

    rows.append([
        _btn("🏳️ Сдаться", f"g{game_id}:resign"),
        _btn("🤝 Ничья", f"g{game_id}:draw"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def move_keyboard(game_id: int, board: chess.Board, from_square: int) -> InlineKeyboardMarkup:
    """Куда может пойти выбранная фигура; превращение — отдельным шагом."""
    targets = sorted(
        {m.to_square for m in board.legal_moves if m.from_square == from_square},
        key=lambda sq: (chess.square_file(sq), -chess.square_rank(sq)),
    )
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for square in targets:
        current.append(_btn("→ " + chess.square_name(square), f"g{game_id}:m{from_square}{square}"))
        if len(current) == 4:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([_btn("↩️ Другая фигура", f"g{game_id}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def promotion_keyboard(game_id: int, from_square: int, to_square: int) -> InlineKeyboardMarkup:
    options = [
        (chess.QUEEN, "♕ Ферзь"),
        (chess.ROOK, "♖ Ладья"),
        (chess.BISHOP, "♗ Слон"),
        (chess.KNIGHT, "♘ Конь"),
    ]
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"g{game_id}:q{from_square}{to_square}{letter}")]
        for letter, (_, title) in zip("qrbn", options)
    ]
    rows.append([_btn("↩️ Отмена", f"g{game_id}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def game_over_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("🆕 Новая партия", "menu:root"),
        _btn("🏠 В меню", "menu:root"),
    ]])


def move_history_text(game, limit: int = 16) -> str:
    """Ходы в шахматной нотации, попарно — удобно читать на телефоне."""
    moves = game.move_list()
    if not moves:
        return "ходов пока нет"
    board = chess.Board()
    numbered: list[str] = []
    for index, uci in enumerate(moves):
        try:
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            board.push(move)
        except ValueError:
            break
        if index % 2 == 0:
            numbered.append(f"{index // 2 + 1}.{san}")
        elif numbered:
            numbered[-1] += f" {san}"
    tail = numbered[-limit:]
    prefix = "… " if len(numbered) > limit else ""
    return prefix + " ".join(tail)
