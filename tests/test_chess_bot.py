"""Тесты шахматной части: правила, хранилище партий, клавиатуры."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

import chess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chess_bot.app import board_ui  # noqa: E402
from chess_bot.app.engine import choose_move  # noqa: E402
from chess_bot.app.store import GameStore  # noqa: E402


class TestEngine(unittest.TestCase):
    def test_returns_legal_move_at_every_level(self):
        for level in (1, 2, 3, 4):
            board = chess.Board()
            move = choose_move(board, level)
            self.assertIn(move, board.legal_moves, f"уровень {level}")

    def test_finds_mate_in_one(self):
        # Ферзь f7, король g6, чёрный король h8: Qg7# — единственный мат в 1.
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        move = choose_move(board, 4)
        board.push(move)
        self.assertTrue(board.is_checkmate(), f"движок сыграл {move.uci()}")

    def test_does_not_explode_board(self):
        board = chess.Board()
        before = board.fen()
        choose_move(board, 3)
        self.assertEqual(board.fen(), before)


class TestStore(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.store = GameStore(self.path)

    def tearDown(self):
        self.store.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_pvp_join_and_turn_order(self):
        game, token = self.store.create_pvp(host_id=1, host_name="Хозяин", color=chess.WHITE)
        self.assertEqual(game.status, "waiting")
        self.assertIsNotNone(self.store.invite_by_token(token))

        ok, text, game = self.store.join(game.id, 2, "Гость")
        self.assertTrue(ok, text)
        self.assertEqual(game.status, "active")
        self.assertEqual(game.black_id, 2)

        ok, _, game = self.store.apply_move(game.id, 1, "e2e4")
        self.assertTrue(ok)
        self.assertEqual(game.moves, "e2e4")

        ok, text, _ = self.store.apply_move(game.id, 1, "d2d4")
        self.assertFalse(ok, "два хода подряд невозможны")
        self.assertIn("не ваш ход", text)

        ok, _, game = self.store.apply_move(game.id, 2, "e7e5")
        self.assertTrue(ok)
        self.assertEqual(game.moves, "e2e4 e7e5")

    def test_illegal_move_rejected(self):
        game = self.store.create_pve(user_id=10, user_name="Игрок", color=chess.WHITE, level=2)
        ok, text, _ = self.store.apply_move(game.id, 10, "e2e5")
        self.assertFalse(ok)
        self.assertEqual(text, "Так ходить нельзя.")

    def test_outsider_cannot_move_in_pvp(self):
        game, _ = self.store.create_pvp(1, "Хозяин", chess.WHITE)
        self.store.join(game.id, 2, "Гость")
        ok, text, _ = self.store.apply_move(game.id, 999, "e2e4")
        self.assertFalse(ok)
        self.assertIn("не участник", text)

    def test_promotion_and_game_end(self):
        game = self.store.create_pve(user_id=1, user_name="Игрок", color=chess.WHITE, level=1)
        # подставляем позицию: пешка на b7 идёт в ферзи
        self.store.conn.execute(
            "UPDATE games SET fen = ?, moves = '' WHERE id = ?",
            ("8/P7/8/8/8/8/8/K6k w - - 0 1", game.id),
        )
        ok, text, game = self.store.apply_move(game.id, 1, "a7a8q")
        self.assertTrue(ok, text)
        self.assertEqual(game.move_list(), ["a7a8q"])
        self.assertEqual(game.draw_board().piece_at(chess.A8).piece_type, chess.QUEEN)

    def test_checkmate_finishes_game(self):
        game = self.store.create_pve(user_id=1, user_name="Игрок", color=chess.WHITE, level=1)
        self.store.conn.execute(
            "UPDATE games SET fen = ?, moves = '' WHERE id = ?",
            ("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1", game.id),
        )
        ok, _, game = self.store.apply_move(game.id, 1, "f7g7")
        self.assertTrue(ok)
        self.assertEqual(game.status, "finished")
        self.assertIn("Победа: Белые", game.result or "")

    def test_history_rebuild_keeps_repetition_info(self):
        game = self.store.create_pve(1, "Игрок", chess.WHITE, 1)
        for uci, mover in (("g1f3", 1),):
            ok, text, _ = self.store.apply_move(game.id, mover, uci)
            self.assertTrue(ok, text)
        rebuilt = self.store.get(game.id).draw_board()
        self.assertEqual(len(rebuilt.move_stack), 1)
        self.assertEqual(rebuilt.fen(), self.store.get(game.id).fen)

    def test_resign_and_draw(self):
        game, _ = self.store.create_pvp(1, "Хозяин", chess.WHITE)
        self.store.join(game.id, 2, "Гость")
        ok, updated = self.store.resign(game.id, 1)
        self.assertTrue(ok)
        self.assertEqual(updated.status, "finished")
        self.assertIn("Чёрные", updated.result)

        game2, _ = self.store.create_pvp(3, "A", chess.WHITE)
        self.store.join(game2.id, 4, "B")
        ok, text, updated = self.store.offer_draw(game2.id, 3)
        self.assertTrue(ok)
        self.assertEqual(updated.draw_offer_by, 3)
        ok, text, updated = self.store.offer_draw(game2.id, 4)
        self.assertTrue(ok)
        self.assertEqual(updated.status, "finished")
        self.assertEqual(updated.result, "Ничья по соглашению")

    def test_no_draw_against_engine(self):
        game = self.store.create_pve(1, "Игрок", chess.WHITE, 2)
        ok, text, _ = self.store.offer_draw(game.id, 1)
        self.assertFalse(ok)
        self.assertIn("Бот не принимает", text)


class TestBoardUi(unittest.TestCase):
    def test_board_rendering_has_all_files(self):
        text = board_ui.render_board(chess.Board())
        for letter in "abcdefgh":
            self.assertIn(letter, text)
        self.assertEqual(len(text.splitlines()), 9)

    def test_flipped_board_starts_with_white_pieces(self):
        # с точки зрения чёрных повёрнутая доска начинается с 1-й линии,
        # где стоят белые фигуры
        text = board_ui.render_board(chess.Board(), chess.BLACK)
        first_line = text.splitlines()[0]
        self.assertTrue(first_line.startswith("1 "))
        self.assertIn("♖", first_line)

    def test_white_board_starts_with_black_pieces(self):
        text = board_ui.render_board(chess.Board(), chess.WHITE)
        self.assertIn("♜", text.splitlines()[0])

    def test_callback_data_fits_telegram_limit(self):
        board = chess.Board()
        for markup in (
            board_ui.piece_keyboard(123456789, board),
            board_ui.move_keyboard(123456789, board, chess.E2),
            board_ui.promotion_keyboard(123456789, chess.E7, chess.E8),
        ):
            for row in markup.inline_keyboard:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode()), 64, button.callback_data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
