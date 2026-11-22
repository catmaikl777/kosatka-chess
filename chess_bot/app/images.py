"""Картинки для бота: шахматная доска в объятиях чёрной кошки Косатки.

Рисуется Pillow'ом на лету: фигуры, подсветка хода, рамка с лапками,
талисман — маленькая чёрная кошка с белой «маской» и носом-пятном
(окрас телячьей табби, как у шиншиллы), как у настоящей Косатки.

Ничего не скачивается: только системные шрифты и встроенная графика.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from io import BytesIO

import chess
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------- палитра

INK = (26, 26, 33)
INK_SOFT = (52, 52, 66)
PAPER = (246, 243, 238)
WHITE = (252, 252, 250)
BLACK = (32, 32, 40)

THEMES = {
    "midnight": {
        "name": "Ночная Коска",
        "bg_top": (24, 22, 38),
        "bg_bottom": (38, 30, 52),
        "light": (233, 219, 196),
        "dark": (122, 88, 78),
        "frame": (20, 18, 30),
        "accent": (232, 155, 74),
        "text": (245, 240, 232),
        "dim": (196, 186, 205),
    },
    "emerald": {
        "name": "Мятная лапка",
        "bg_top": (20, 38, 34),
        "bg_bottom": (28, 52, 46),
        "light": (236, 238, 226),
        "dark": (92, 146, 122),
        "frame": (14, 30, 27),
        "accent": (150, 224, 180),
        "text": (238, 246, 240),
        "dim": (186, 208, 196),
    },
    "sakura": {
        "name": "Розовый нос",
        "bg_top": (46, 28, 40),
        "bg_bottom": (68, 40, 58),
        "light": (250, 240, 238),
        "dark": (196, 138, 152),
        "frame": (40, 22, 34),
        "accent": (255, 176, 196),
        "text": (255, 244, 246),
        "dim": (226, 196, 208),
    },
    "wood": {
        "name": "Деревянная",
        "bg_top": (94, 62, 38),
        "bg_bottom": (58, 36, 22),
        "light": (240, 217, 178),
        "dark": (152, 100, 62),
        "frame": (58, 34, 20),
        "accent": (214, 156, 74),
        "text": (250, 238, 218),
        "dim": (206, 178, 144),
        "wood": True,
    },
}
DEFAULT_THEME = "wood"
THEME_ORDER = ("wood", "midnight", "emerald", "sakura")

BOARD_SIZE = 8
CELL = 96
MARGIN = 34
HEADER = 118
FOOTER = 92
COORD_PAD = 30
SIDE = MARGIN * 2 + CELL * BOARD_SIZE

CACHE_DIR = os.path.join(tempfile.gettempdir(), "gigatool", "kosatka_cards")
os.makedirs(CACHE_DIR, exist_ok=True)

_FONT_CANDIDATES = {
    "bold": ("segoeuib.ttf", "arialbd.ttf"),
    "regular": ("segoeui.ttf", "arial.ttf"),
    "symbol": ("seguisym.ttf", "segoeui.ttf", "arial.ttf"),
}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    fonts_dir = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
    for name in _FONT_CANDIDATES[kind]:
        path = os.path.join(fonts_dir, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _has_glyph(font: ImageFont.FreeTypeFont, char: str) -> bool:
    try:
        mask = font.getmask(char)
        return mask.getbbox() is not None
    except Exception:  # noqa: BLE001
        return False


_SYMBOL_CACHE: dict[int, bool] = {}


def _chess_symbols_available() -> bool:
    if "ok" not in _SYMBOL_CACHE:
        font = _font("symbol", 32)
        _SYMBOL_CACHE["ok"] = all(_has_glyph(font, ch) for ch in "♔♕♖♗♘♙♚♛♜♝♞♟")
    return bool(_SYMBOL_CACHE["ok"])


def _gradient(width: int, height: int, top, bottom) -> Image.Image:
    base = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        base.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return base.resize((width, height))


def _wood_texture(width: int, height: int, top, bottom, seed: int = 7) -> Image.Image:
    """Деревянный фон: вертикальные волокна, сучки и лёгкие тёмные прожилки."""
    import random

    img = _gradient(width, height, top, bottom).convert("RGB")
    pixels = img.load()
    rng = random.Random(seed)
    waves = [(rng.uniform(0.004, 0.02), rng.uniform(0, 6.28), rng.uniform(0.5, 2.0)) for _ in range(5)]
    for x in range(width):
        shift = sum(amplitude * 9 * __import__("math").sin(x * freq + phase)
                    for freq, phase, amplitude in waves)
        for y in range(height):
            r, g, b = pixels[x, y]
            grain = int(shift + 4 * __import__("math").sin((y * 0.35) + x * 0.01))
            factor = 1.0 + grain / 120.0
            pixels[x, y] = (
                max(0, min(255, int(r * factor))),
                max(0, min(255, int(g * factor * 0.985))),
                max(0, min(255, int(b * factor * 0.96))),
            )
    draw = ImageDraw.Draw(img, "RGBA")
    for _ in range(5):
        cx, cy = rng.randint(40, width - 40), rng.randint(40, height - 40)
        radius = rng.randint(6, 16)
        for ring in range(3, 0, -1):
            draw.ellipse([cx - radius * ring, cy - radius * ring * 0.55,
                          cx + radius * ring, cy + radius * ring * 0.55],
                         outline=(40, 24, 14, 46), width=2)
    return img


def _theme_background(width: int, height: int, theme: dict) -> Image.Image:
    if theme.get("wood"):
        return _wood_texture(width, height, theme["bg_top"], theme["bg_bottom"])
    return _gradient(width, height, theme["bg_top"], theme["bg_bottom"]).convert("RGB")


# --------------------------------------------------------------------- фигуры


def _draw_pawn(draw, cx, cy, size, fill):
    r = size * 0.17
    draw.ellipse([cx - r, cy - size * 0.46 - r, cx + r, cy - size * 0.46 + r], fill=fill)
    draw.polygon([
        (cx - size * 0.20, cy - size * 0.30),
        (cx + size * 0.20, cy - size * 0.30),
        (cx + size * 0.30, cy + size * 0.22),
        (cx - size * 0.30, cy + size * 0.22),
    ], fill=fill)
    draw.rounded_rectangle(
        [cx - size * 0.40, cy + size * 0.22, cx + size * 0.40, cy + size * 0.36],
        radius=size * 0.08, fill=fill,
    )


def _draw_rook(draw, cx, cy, size, fill):
    w = size * 0.64
    left, right = cx - w / 2, cx + w / 2
    top = cy - size * 0.44
    draw.rectangle([left, top, right, top + size * 0.16], fill=fill)
    step = w / 5
    for index in (0, 2, 4):
        x0 = left + index * step
        draw.rectangle([x0, top - size * 0.08, x0 + step, top + size * 0.04], fill=fill)
    draw.polygon([
        (left + size * 0.06, top + size * 0.16),
        (right - size * 0.06, top + size * 0.16),
        (right - size * 0.13, cy + size * 0.22),
        (left + size * 0.13, cy + size * 0.22),
    ], fill=fill)
    draw.rounded_rectangle(
        [left - size * 0.04, cy + size * 0.20, right + size * 0.04, cy + size * 0.36],
        radius=size * 0.06, fill=fill,
    )


def _draw_bishop(draw, cx, cy, size, fill):
    draw.ellipse([cx - size * 0.07, cy - size * 0.50, cx + size * 0.07, cy - size * 0.36], fill=fill)
    draw.ellipse([cx - size * 0.19, cy - size * 0.35, cx + size * 0.19, cy - size * 0.02], fill=fill)
    draw.polygon([
        (cx - size * 0.10, cy - size * 0.08),
        (cx + size * 0.10, cy - size * 0.08),
        (cx + size * 0.26, cy + size * 0.24),
        (cx - size * 0.26, cy + size * 0.24),
    ], fill=fill)
    draw.rounded_rectangle(
        [cx - size * 0.36, cy + size * 0.22, cx + size * 0.36, cy + size * 0.36],
        radius=size * 0.07, fill=fill,
    )


def _draw_knight(draw, cx, cy, size, fill):
    left = cx - size * 0.30
    top = cy - size * 0.44
    points = [
        (left + size * 0.02, cy + size * 0.30),
        (left - size * 0.09, cy + size * 0.02),
        (left + size * 0.02, cy - size * 0.16),
        (left + size * 0.06, cy - size * 0.34),
        (left + size * 0.20, cy - size * 0.50),
        (left + size * 0.40, cy - size * 0.44),
        (left + size * 0.52, cy - size * 0.22),
        (left + size * 0.56, cy + size * 0.04),
        (left + size * 0.48, cy + size * 0.28),
    ]
    draw.polygon(points, fill=fill)
    draw.polygon([
        (left + size * 0.20, cy - size * 0.44),
        (left + size * 0.34, cy - size * 0.30),
        (left + size * 0.26, cy - size * 0.12),
    ], fill=None if fill == BLACK else PAPER, outline=fill)
    draw.rounded_rectangle(
        [cx - size * 0.40, cy + size * 0.27, cx + size * 0.34, cy + size * 0.38],
        radius=size * 0.05, fill=fill,
    )


def _draw_queen(draw, cx, cy, size, fill):
    left, right = cx - size * 0.34, cx + size * 0.34
    peak = cy - size * 0.42
    crown = [
        (left, cy - size * 0.06),
        (left + size * 0.10, cy - size * 0.34),
        (cx - size * 0.06, cy - size * 0.10),
        (cx, peak - size * 0.06),
        (cx + size * 0.06, cy - size * 0.10),
        (right - size * 0.10, cy - size * 0.34),
        (right, cy - size * 0.06),
        (right - size * 0.04, cy + size * 0.16),
        (left + size * 0.04, cy + size * 0.16),
    ]
    draw.polygon(crown, fill=fill)
    for px, py in ((left, cy - size * 0.06), (cx, peak - size * 0.08), (right, cy - size * 0.06),
                   (left + size * 0.10, cy - size * 0.34), (right - size * 0.10, cy - size * 0.34)):
        r = size * 0.055
        draw.ellipse([px - r, py - r, px + r, py + r], fill=fill)
    draw.rounded_rectangle(
        [cx - size * 0.38, cy + size * 0.14, cx + size * 0.38, cy + size * 0.34],
        radius=size * 0.08, fill=fill,
    )


def _draw_king(draw, cx, cy, size, fill):
    draw.rectangle([cx - size * 0.035, cy - size * 0.56, cx + size * 0.035, cy - size * 0.40], fill=fill)
    draw.rectangle([cx - size * 0.10, cy - size * 0.505, cx + size * 0.10, cy - size * 0.455], fill=fill)
    draw.polygon([
        (cx - size * 0.20, cy - size * 0.36),
        (cx + size * 0.20, cy - size * 0.36),
        (cx + size * 0.28, cy + size * 0.06),
        (cx - size * 0.28, cy + size * 0.06),
    ], fill=fill)
    draw.rectangle([cx - size * 0.36, cy + size * 0.04, cx + size * 0.36, cy + size * 0.14], fill=fill)
    draw.polygon([
        (cx - size * 0.36, cy + size * 0.13),
        (cx + size * 0.36, cy + size * 0.13),
        (cx + size * 0.30, cy + size * 0.30),
        (cx - size * 0.30, cy + size * 0.30),
    ], fill=fill)
    draw.rounded_rectangle(
        [cx - size * 0.40, cy + size * 0.28, cx + size * 0.40, cy + size * 0.38],
        radius=size * 0.05, fill=fill,
    )


_PAW_POLY = {
    chess.PAWN: _draw_pawn,
    chess.ROOK: _draw_rook,
    chess.BISHOP: _draw_bishop,
    chess.KNIGHT: _draw_knight,
    chess.QUEEN: _draw_queen,
    chess.KING: _draw_king,
}

_UNICODE_GLYPH = {
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


def draw_piece(draw: ImageDraw.ImageDraw, piece: chess.Piece, cx: float, cy: float, size: float) -> None:
    """Векторная фигура. Запасной путь — Unicode, если шрифт есть."""
    color = WHITE if piece.color == chess.WHITE else BLACK
    _PAW_POLY[piece.piece_type](draw, cx, cy, size, color)
    if piece.color == chess.BLACK:
        # тонкая светлая обводка, чтобы фигура читалась на тёмных клетках
        shadow_cy = cy + size * 0.015
        _PAW_POLY[piece.piece_type](draw, cx, shadow_cy, size * 0.99, INK_SOFT)
        _PAW_POLY[piece.piece_type](draw, cx, cy, size, (18, 18, 24))


def piece_image(piece: chess.Piece, size: int, theme: dict, light: bool = True) -> Image.Image:
    """Отдельная плитка с фигурой — для кнопок выбора."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=max(8, size // 8),
                           fill=theme["light"] if light else theme["dark"])
    draw_piece(draw, piece, size / 2, size / 2, size * 0.78)
    return img


# --------------------------------------------------------------------- кошка


def draw_kosatka(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float, accent) -> None:
    """Морда чёрной кошки: ушки, белая маска, пятно на носу, глаза-щёлочки."""
    body = (14, 14, 20)
    white = (245, 244, 240)

    ear_y = cy - size * 0.30
    draw.polygon([(cx - size * 0.42, cy - size * 0.10), (cx - size * 0.34, ear_y - size * 0.26),
                  (cx - size * 0.10, ear_y - size * 0.06)], fill=body)
    draw.polygon([(cx + size * 0.42, cy - size * 0.10), (cx + size * 0.34, ear_y - size * 0.26),
                  (cx + size * 0.10, ear_y - size * 0.06)], fill=body)
    draw.polygon([(cx - size * 0.36, cy - size * 0.14), (cx - size * 0.32, ear_y - size * 0.20),
                  (cx - size * 0.18, ear_y - size * 0.08)], fill=(120, 78, 86))

    head = [cx - size * 0.40, cy - size * 0.24, cx + size * 0.40, cy + size * 0.38]
    draw.ellipse(head, fill=body)
    draw.ellipse([cx - size * 0.24, cy + size * 0.06, cx - size * 0.04, cy + size * 0.30], fill=white)
    draw.ellipse([cx + size * 0.04, cy + size * 0.06, cx + size * 0.24, cy + size * 0.30], fill=white)

    for offset in (-0.19, 0.19):
        ex = cx + size * offset
        draw.ellipse([ex - size * 0.085, cy - size * 0.06, ex + size * 0.085, cy + size * 0.10], fill=(126, 214, 176))
        draw.ellipse([ex - size * 0.016, cy - size * 0.045, ex + size * 0.016, cy + size * 0.085], fill=body)
        draw.ellipse([ex - size * 0.05, cy - size * 0.05, ex - size * 0.02, cy - size * 0.02],
                     fill=(232, 250, 240))
        draw.line([(ex - size * 0.10, cy), (ex + size * 0.10, cy)], fill=body, width=max(1, int(size * 0.03)))

    draw.polygon([(cx - size * 0.055, cy + size * 0.16), (cx + size * 0.055, cy + size * 0.16),
                  (cx, cy + size * 0.24)], fill=accent)
    draw.arc([cx - size * 0.12, cy + size * 0.20, cx, cy + size * 0.34], 10, 80, fill=body, width=max(1, int(size * 0.02)))
    draw.arc([cx, cy + size * 0.20, cx + size * 0.12, cy + size * 0.34], 100, 170, fill=body, width=max(1, int(size * 0.02)))

    for side in (-1, 1):
        base_x = cx + side * size * 0.14
        draw.line([(base_x, cy + size * 0.26), (base_x + side * size * 0.30, cy + size * 0.18)],
                  fill=(226, 226, 232), width=max(1, int(size * 0.012)))
        draw.line([(base_x + side * size * 0.02, cy + size * 0.30), (base_x + side * size * 0.34, cy + size * 0.30)],
                  fill=(226, 226, 232), width=max(1, int(size * 0.012)))


def draw_paw(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float, color) -> None:
    draw.ellipse([cx - size * 0.28, cy - size * 0.06, cx + size * 0.28, cy + size * 0.34], fill=color)
    for dx, dy in ((-0.34, -0.26), (-0.12, -0.40), (0.16, -0.40), (0.36, -0.24)):
        r = size * 0.12
        draw.ellipse([cx + size * dx - r, cy + size * dy - r, cx + size * dx + r, cy + size * dy + r], fill=color)


# --------------------------------------------------------------------- доска


def _board_origin(perspective: bool) -> tuple[int, int]:
    return MARGIN, HEADER


def square_rect(square: int, perspective: bool) -> tuple[int, int, int, int]:
    file = chess.square_file(square)
    rank = chess.square_rank(square)
    if perspective == chess.WHITE:
        col, row = file, 7 - rank
    else:
        col, row = 7 - file, rank
    x = MARGIN + col * CELL
    y = HEADER + row * CELL
    return x, y, x + CELL, y + CELL


def point_to_square(x: int, y: int, perspective: bool) -> int | None:
    """Обратное преобразование — для тестов и отладки."""
    col = (x - MARGIN) // CELL
    row = (y - HEADER) // CELL
    if not (0 <= col < 8 and 0 <= row < 8):
        return None
    if perspective == chess.WHITE:
        file, rank = col, 7 - row
    else:
        file, rank = 7 - col, row
    return chess.square(file, rank)


def board_header_height() -> int:
    return HEADER


def board_footer_height() -> int:
    return FOOTER


def board_side() -> int:
    return SIDE


def board_cell_size() -> int:
    return CELL


def render_board(
    board: chess.Board,
    perspective: bool = chess.WHITE,
    theme_name: str = DEFAULT_THEME,
    last_move: chess.Move | None = None,
    caption_left: str = "",
    caption_right: str = "",
    subtitle: str = "",
    highlight: int | None = None,
    arrows: list[tuple[int, int]] = None,
) -> Image.Image:
    theme = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
    width = SIDE
    height = HEADER + CELL * 8 + FOOTER
    img = _theme_background(width, height, theme)
    draw = ImageDraw.Draw(img, "RGBA")

    # шапка
    draw_kosatka(draw, MARGIN + 34, HEADER - 45, 78, theme["accent"])
    font_bold = _font("bold", 34)
    font_small = _font("regular", 23)
    draw.text((MARGIN + 88, HEADER - 78), caption_left or "Косатка", font=font_bold, fill=theme["text"])
    if subtitle:
        draw.text((MARGIN + 88, HEADER - 38), subtitle, font=font_small, fill=theme["dim"])
    if caption_right:
        w = draw.textlength(caption_right, font=font_small)
        draw.text((width - MARGIN - w, HEADER - 66), caption_right, font=font_small, fill=theme["dim"])

    # рамка доски
    ox, oy = _board_origin(perspective)
    board_span = CELL * 8
    draw.rounded_rectangle(
        [ox - 12, oy - 12, ox + board_span + 12, oy + board_span + 12],
        radius=18, fill=theme["frame"],
    )

    marked = set()
    if last_move:
        marked = {last_move.from_square, last_move.to_square}

    for row in range(8):
        for col in range(8):
            x = ox + col * CELL
            y = oy + row * CELL
            light = (col + row) % 2 == 0
            base = theme["light"] if light else theme["dark"]
            draw.rectangle([x, y, x + CELL, y + CELL], fill=base)
            if perspective == chess.WHITE:
                sq = chess.square(col, 7 - row)
            else:
                sq = chess.square(7 - col, row)
            if sq in marked:
                draw.rectangle([x, y, x + CELL, y + CELL], fill=theme["accent"] + (58,))
            if highlight is not None and sq == highlight:
                draw.rectangle([x + 3, y + 3, x + CELL - 3, y + CELL - 3],
                               outline=theme["accent"], width=5)

    # координаты
    coord_font = _font("bold", 17)
    for index in range(8):
        letter = "abcdefgh"[index] if perspective == chess.WHITE else "hgfedcba"[index]
        draw.text((ox + index * CELL + CELL / 2 - 5, oy + board_span + 12), letter, font=coord_font,
                  fill=theme["dim"])
        number = str(8 - index) if perspective == chess.WHITE else str(index + 1)
        draw.text((ox + board_span + 16, oy + index * CELL + CELL / 2 - 10), number, font=coord_font,
                  fill=theme["dim"])

    # фигуры
    for square, piece in board.piece_map().items():
        x, y, _, _ = square_rect(square, perspective)
        draw_piece(draw, piece, x + CELL / 2, y + CELL / 2 + 2, CELL * 0.86)

    if arrows:
        for start, end in arrows:
            _draw_arrow(draw, start, end, perspective, theme)

    # подвал: лапки-разделители и счёт материала
    draw_paw(draw, MARGIN + 16, height - FOOTER + 8, 26, theme["accent"] + (170,))
    score = material_balance(board)
    if score:
        font_f = _font("bold", 21)
        draw.text((MARGIN + 46, height - FOOTER + 6), score, font=font_f, fill=theme["dim"])
    if board.is_check():
        warn = "⚠ шах!"
        w = draw.textlength(warn, font=font_small)
        draw.text((width - MARGIN - w, height - FOOTER + 4), warn, font=font_small, fill=theme["accent"])
    return img


def _draw_arrow(draw, start: int, end: int, perspective: bool, theme: dict) -> None:
    x1, y1, x2, y2 = square_rect(start, perspective)
    cx1, cy1 = x1 + CELL / 2, y1 + CELL / 2
    cx2, cy2 = x2 + CELL / 2, y2 + CELL / 2
    color = theme["accent"] + (180,)
    draw.line([(cx1, cy1), (cx2, cy2)], fill=color, width=9)
    import math
    angle = math.atan2(cy2 - cy1, cx2 - cx1)
    for delta in (2.6, -2.6):
        draw.line([(cx2, cy2), (cx2 + 26 * math.cos(angle + delta), cy2 + 26 * math.sin(angle + delta))],
                  fill=color, width=9)


def material_balance(board: chess.Board) -> str:
    values = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
    white = sum(values.get(p.piece_type, 0) for p in board.piece_map().values() if p.color == chess.WHITE)
    black = sum(values.get(p.piece_type, 0) for p in board.piece_map().values() if p.color == chess.BLACK)
    diff = white - black
    if diff == 0:
        return "равный материал"
    side = "белые" if diff > 0 else "чёрные"
    return f"+{abs(diff)} у {side}"


# --------------------------------------------------------------------- сохранение


def to_bytes(img: Image.Image) -> bytes:
    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def save_cache(img: Image.Image, prefix: str = "card") -> str:
    path = os.path.join(CACHE_DIR, f"{prefix}-{uuid.uuid4().hex[:10]}.png")
    img.save(path, format="PNG", optimize=True)
    return path


def render_board_bytes(board: chess.Board, perspective: bool = chess.WHITE, theme_name: str = DEFAULT_THEME,
                       last_move: chess.Move | None = None, caption_left: str = "", caption_right: str = "",
                       subtitle: str = "", highlight: int | None = None) -> bytes:
    return to_bytes(render_board(board, perspective, theme_name, last_move, caption_left, caption_right,
                                 subtitle, highlight))


# --------------------------------------------------------------------- обложка


def render_cover(theme_name: str = DEFAULT_THEME) -> Image.Image:
    theme = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
    width, height = SIDE, 420
    img = _theme_background(width, height, theme)
    draw = ImageDraw.Draw(img, "RGBA")
    draw_kosatka(draw, width / 2, 150, 190, theme["accent"])
    font_title = _font("bold", 52)
    title = "Косатка"
    w = draw.textlength(title, font=font_title)
    draw.text(((width - w) / 2, 268), title, font=font_title, fill=theme["text"])
    font_sub = _font("regular", 25)
    sub = "чёрная кошка, которая очень любит шахматы"
    w = draw.textlength(sub, font=font_sub)
    draw.text(((width - w) / 2, 330), sub, font=font_sub, fill=theme["dim"])
    draw_paw(draw, 60, height - 46, 34, theme["accent"] + (140,))
    draw_paw(draw, width - 60, height - 46, 34, theme["accent"] + (140,))
    return img
