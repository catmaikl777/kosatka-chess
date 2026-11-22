"""Небольшой шахматный движок: alpha-beta + простая оценка позиции.

Внешних бинарников не требует: только python-chess, всё считается в Python.
Уровни задаются глубиной поиска и долей случайных ходов.
"""

from __future__ import annotations

import random
import time

import chess

INF = 10**9
MATE = 100_000

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

# Таблицы записаны сверху вниз: первая строка — 8-я горизонталь.
PAWN_PST = [
    0, 0, 0, 0, 0, 0, 0, 0,
    50, 50, 50, 50, 50, 50, 50, 50,
    10, 10, 20, 30, 30, 20, 10, 10,
    5, 5, 10, 25, 25, 10, 5, 5,
    0, 0, 0, 20, 20, 0, 0, 0,
    5, -5, -10, 0, 0, -10, -5, 5,
    5, 10, 10, -20, -20, 10, 10, 5,
    0, 0, 0, 0, 0, 0, 0, 0,
]

KNIGHT_PST = [
    -50, -40, -30, -30, -30, -30, -40, -50,
    -40, -20, 0, 0, 0, 0, -20, -40,
    -30, 0, 10, 15, 15, 10, 0, -30,
    -30, 5, 15, 20, 20, 15, 5, -30,
    -30, 0, 15, 20, 20, 15, 0, -30,
    -30, 5, 10, 15, 15, 10, 5, -30,
    -40, -20, 0, 5, 5, 0, -20, -40,
    -50, -40, -30, -30, -30, -30, -40, -50,
]

BISHOP_PST = [
    -20, -10, -10, -10, -10, -10, -10, -20,
    -10, 0, 0, 0, 0, 0, 0, -10,
    -10, 0, 5, 10, 10, 5, 0, -10,
    -10, 5, 5, 10, 10, 5, 5, -10,
    -10, 0, 10, 10, 10, 10, 0, -10,
    -10, 10, 10, 10, 10, 10, 10, -10,
    -10, 5, 0, 0, 0, 0, 5, -10,
    -20, -10, -10, -10, -10, -10, -10, -20,
]

ROOK_PST = [
    0, 0, 0, 0, 0, 0, 0, 0,
    5, 10, 10, 10, 10, 10, 10, 5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    0, 0, 0, 5, 5, 0, 0, 0,
]

QUEEN_PST = [
    -20, -10, -10, -5, -5, -10, -10, -20,
    -10, 0, 0, 0, 0, 0, 0, -10,
    -10, 0, 5, 5, 5, 5, 0, -10,
    -5, 0, 5, 5, 5, 5, 0, -5,
    0, 0, 5, 5, 5, 5, 0, -5,
    -10, 5, 5, 5, 5, 5, 0, -10,
    -10, 0, 5, 0, 0, 0, 0, -10,
    -20, -10, -10, -5, -5, -10, -10, -20,
]

KING_PST = [
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -20, -30, -30, -40, -40, -30, -30, -20,
    -10, -20, -20, -20, -20, -20, -20, -10,
    20, 20, 0, 0, 0, 20, 20, 20,
    20, 30, 10, 0, 0, 10, 30, 20,
]

PST = {
    chess.PAWN: PAWN_PST,
    chess.KNIGHT: KNIGHT_PST,
    chess.BISHOP: BISHOP_PST,
    chess.ROOK: ROOK_PST,
    chess.QUEEN: QUEEN_PST,
    chess.KING: KING_PST,
}

# Уровень -> (глубина, вероятность случайного хода, лимит времени в секундах)
LEVELS = {
    1: (1, 0.45, 0.5),
    2: (2, 0.15, 1.2),
    3: (3, 0.04, 2.5),
    4: (4, 0.0, 5.0),
}

LEVEL_NAMES = {
    1: "Новичок",
    2: "Любитель",
    3: "Клубный игрок",
    4: "Мастер",
}


class _Timeout(Exception):
    pass


def _pst_value(piece_type: int, square: int, color: bool) -> int:
    table = PST[piece_type]
    index = chess.square_mirror(square) if color == chess.WHITE else square
    return table[index]


def evaluate(board: chess.Board) -> int:
    """Оценка позиции с точки зрения того, чей сейчас ход (в сантипешках)."""
    score = 0
    for square, piece in board.piece_map().items():
        value = PIECE_VALUES[piece.piece_type] + _pst_value(piece.piece_type, square, piece.color)
        score += value if piece.color == chess.WHITE else -value
    return score if board.turn == chess.WHITE else -score


def _order_moves(board: chess.Board, moves: list[chess.Move]) -> list[chess.Move]:
    """Сначала взятия подороже, потом всё остальное."""

    def key(move: chess.Move) -> int:
        victim = board.piece_at(move.to_square)
        if victim is None:
            return 0
        attacker = board.piece_at(move.from_square)
        attacker_value = PIECE_VALUES[attacker.piece_type] if attacker else 0
        return 10_000 + PIECE_VALUES[victim.piece_type] * 10 - attacker_value

    return sorted(moves, key=key, reverse=True)


def _quiescence(board: chess.Board, alpha: int, beta: int, deadline: float, depth: int) -> int:
    """Досчёт только взятий, чтобы не «зевать» размены на границе глубины."""
    if time.monotonic() > deadline:
        raise _Timeout

    stand_pat = evaluate(board)
    if stand_pat >= beta:
        return beta
    if stand_pat > alpha:
        alpha = stand_pat
    if depth <= 0:
        return alpha

    captures = [m for m in board.legal_moves if board.is_capture(m)]
    for move in _order_moves(board, captures):
        board.push(move)
        try:
            score = -_quiescence(board, -beta, -alpha, deadline, depth - 1)
        finally:
            board.pop()
        if score >= beta:
            return beta
        if score > alpha:
            alpha = score
    return alpha


def _search(board: chess.Board, depth: int, alpha: int, beta: int, deadline: float, ply: int) -> int:
    if time.monotonic() > deadline:
        raise _Timeout

    if depth == 0:
        return _quiescence(board, alpha, beta, deadline, 3)

    moves = list(board.legal_moves)
    if not moves:
        if board.is_checkmate():
            return -MATE + ply
        return 0

    for move in _order_moves(board, moves):
        board.push(move)
        try:
            score = -_search(board, depth - 1, -beta, -alpha, deadline, ply + 1)
        finally:
            board.pop()

        if score >= beta:
            return beta
        if score > alpha:
            alpha = score

    return alpha


def choose_move(board: chess.Board, level: int = 2, rng: random.Random | None = None) -> chess.Move:
    """Выбирает ход за сторону, которая сейчас ходит."""
    rng = rng or random.Random()
    depth, randomness, time_limit = LEVELS.get(level, LEVELS[2])
    moves = list(board.legal_moves)
    if not moves:
        raise ValueError("В позиции нет легальных ходов")

    if len(moves) == 1:
        return moves[0]

    if randomness and rng.random() < randomness:
        return rng.choice(moves)

    deadline = time.monotonic() + time_limit
    best_score = -INF
    best_moves: list[chess.Move] = []

    try:
        for move in _order_moves(board, moves):
            board.push(move)
            try:
                score = -_search(board, depth - 1, -INF, INF, deadline, 1)
            finally:
                board.pop()

            if score > best_score:
                best_score = score
                best_moves = [move]
            elif score == best_score:
                best_moves.append(move)
    except _Timeout:
        pass

    if not best_moves:
        return rng.choice(moves)
    return rng.choice(best_moves)
