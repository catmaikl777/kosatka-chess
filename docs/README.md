# 🐈‍⬛ Косатка — шахматный бот и веб-игра

Чёрная кошка Косатка играет в шахматы: в Telegram (доски-картинки с кнопками) и в вебе (Web App на GitHub Pages, бэкенд на Render).

**Деплой**

- Фронт: https://catmaikl777.github.io/kosatka-chess/ (GitHub Pages, ветка `main`, корень)
- Бэкенд: Render, Blueprint из `render.yaml` (`kosatka-api` + бесплатный Postgres `kosatka-db`)
- Репозиторий: https://github.com/catmaikl777/kosatka-chess

**Запуск**

```powershell
py -3 -m pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN = "токен от @BotFather"
py -3 -m chess_bot.bot                    # бот + веб-сервер, если задан WEBAPP_URL
py -3 -m chess_bot.web_server --port 8080 # только веб-версия: http://127.0.0.1:8080/
py -3 -m unittest discover -s tests -v    # тесты
```

**Хранение**

Одно хранилище на оба режима, движок выбирается по адресу:

| Где | Как | Что |
|---|---|---|
| Локально | `CHESS_DB=chess_bot.db` | SQLite-файл |
| Render / VPS | `DATABASE_URL=postgresql://…` | Postgres (переменная важнее `CHESS_DB`) |

Инициализация схемы автоматическая, таблицы `games`, `invites`, `messages`.

**Резервные копии в Google Drive**

Каждый день в 00:00 (по времени сервера) бот выгружает таблицы в SQL-дамп, кладёт его в `backups/` и, если настроен Drive, загружает туда. В Drive хранятся последние 30 копий (настраивается).

Настройка Drive один раз:

1. В Google Cloud Console создайте сервисный аккаунт и включите Google Drive API.
2. Скачайте JSON-ключ сервисного аккаунта.
3. Создайте папку на Google Диске и дайте сервисному аккаунту доступ «Редактор» (поделиться по e-mail из ключа `client_email`).
4. Задайте переменные: `GDRIVE_FOLDER_ID` (id папки из ссылки) и `GDRIVE_SERVICE_ACCOUNT_JSON` (весь JSON одной строкой) или `GDRIVE_SERVICE_ACCOUNT_FILE` (путь к файлу).

Команды:

```powershell
py -3 -m chess_bot.backup                          # копия сейчас (в Drive и backups/)
py -3 -m chess_bot.backup --local-only             # только в backups/
py -3 -m chess_bot.backup --list                   # что уже лежит в Drive
py -3 -m chess_bot.backup --restore backup-….sql   # из файла
py -3 -m chess_bot.backup --restore backup-….sql --target postgresql://…   # в другую базу
py -3 -m chess_bot.backup --restore kosatka-backup-2026-11-23_000000.sql   # прямо из Drive
py -3 -m chess_bot.backup --loop                   # ждать 00:00 и сохранять ежедневно
```

Восстановление заменяет содержимое таблиц и переносит id вместе с партиями, поэтому ссылки на старые партии остаются рабочими. Дамп — обычный SQL: его можно проверить глазами и залить в любую базу вручную.

Переменные бэкапа: `BACKUP_ENABLED=0` — выключить, `BACKUP_KEEP=30` — сколько копий хранить.

**Как играть**

- С Косаткой: уровни «Новичок → Мастер», любой цвет.
- С другом: ссылка-приглашение, у каждого доска под его цвет.
- Рокировка, взятие на проходе, превращение, мат, пат, ничьи по правилам; сдача и предложение ничьей.

**Структура**

```
chess_bot/
  bot.py            aiogram: команды, кнопки, ход бота, ежедневный бэкап
  web_server.py     aiohttp: игра в вебе + API + CORS
  backup.py         SQL-дамп, Google Drive, восстановление, планировщик на 00:00
  webapp/           исходник страницы игры
  app/
    store.py        SQLite и Postgres через один адаптер, экспорт/импорт таблиц
    engine.py       движок alpha-beta, 4 уровня
    images.py       доски-картинки и талисман
    board_ui.py     inline-клавиатуры
index.html          собранная страница для GitHub Pages
tools/build_pages.py  сборка страницы с адресом API
render.yaml         Render: веб-сервис и Postgres
Dockerfile          образ бэкенда
```

**Важно**

- Фронт на GitHub Pages; адрес API вшивается при сборке: `py -3 -m tools.build_pages --api https://kosatka-api.onrender.com`, затем коммит `index.html`.
- На бесплатном плане Render сервис засыпает — первый запрос после паузы дольше обычного.
- `WEBAPP_URL=https://catmaikl777.github.io/kosatka-chess/` — адрес кнопки «Играть в вебе» в боте.
