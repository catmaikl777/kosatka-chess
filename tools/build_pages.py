"""Сборка статического фронта для GitHub Pages.

    py -3 -m tools.build_pages --api https://kosatka-api.onrender.com

Кладёт в каталог `webapp/` (его и раздаёт GitHub Pages) минифицированную
страницу игры с вшитым адресом API. Без --api берётся переменная KOSATKA_API.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "webapp"
SOURCE_FILES = [ROOT / "chess_bot" / "webapp" / "index.html"]
# Pages отдаёт index.html из корня репозитория, а webapp/ нужен как отдельная
# папка для прямых ссылок. Пишем в оба места, иначе они расходятся и корень
# остаётся с пустым адресом API — веб-апп тогда ищет партию на github.io.
TARGETS = (ROOT / "index.html", OUT_DIR / "index.html")


def build(api_url: str) -> list[Path]:
    source = SOURCE_FILES[0].read_text(encoding="utf-8")
    api = api_url.rstrip("/")
    text = source.replace("window.KOSATKA_API || ''", f"'{api}'")
    if api_url not in text and api:
        raise SystemExit("Не удалось вшить адрес API: маркер window.KOSATKA_API не найден")
    # предзагрузка API, чтобы первый запрос не был холодным стартом дважды
    text = text.replace(
        "</head>",
        f'<link rel="preconnect" href="{api}" crossorigin>\n</head>' if api else "</head>",
        1,
    )
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    OUT_DIR.mkdir(exist_ok=True)
    written: list[Path] = []
    for target in TARGETS:
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Собрать статику для GitHub Pages")
    parser.add_argument("--api", default=os.environ.get("KOSATKA_API", ""), help="адрес бэкенда")
    args = parser.parse_args()
    if not args.api:
        raise SystemExit("Укажите --api https://<адрес-бэкенда> или переменную KOSATKA_API")
    if not args.api.startswith("https://"):
        raise SystemExit("API должен быть по HTTPS: Telegram не примет http")
    target = build(args.api)
    size = target[0].stat().st_size
    places = ", ".join(str(path.relative_to(ROOT)) for path in target)
    print(f"Собрано: {places} ({size} байт), API: {args.api.rstrip('/')}")


if __name__ == "__main__":
    main()
