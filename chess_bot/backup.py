"""Резервные копии базы в Google Drive и восстановление в любую базу.

Работает с любым движком из app.store (SQLite или Postgres) и не требует
консольных утилит: таблицы выгружаются в обычный SQL-дамп, его можно
закачать в Drive, разобрать глазами и залить в другую базу.

Команды:
    py -3 -m chess_bot.backup                   # сохранить копию сейчас
    py -3 -m chess_bot.backup --local-only      # только в папку backups/
    py -3 -m chess_bot.backup --list            # что уже лежит в Drive
    py -3 -m chess_bot.backup --restore backup-2026-11-23.sql
    py -3 -m chess_bot.backup --restore backup.sql --target postgresql://…
    py -3 -m chess_bot.backup --loop            # ежедневно в 00:00 + сразу при старте
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .app.store import GameStore

log = logging.getLogger("chess_bot.backup")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
BACKUP_PREFIX = "kosatka-backup-"
DEFAULT_KEEP = 30
LOCAL_DIR = Path(__file__).resolve().parent.parent / "backups"


# --------------------------------------------------------------------- SQL-дамп


def _quote(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def to_sql(store: GameStore, created: datetime | None = None) -> str:
    """Полный дамп: схема-совместимые наборы INSERT для games/invites/messages."""
    created = created or datetime.now(timezone.utc)
    data = store.export_tables()
    lines = [
        f"-- Косатка: резервная копия базы {created.isoformat()}",
        f"-- движок-источник: {store.dialect}",
        "-- восстановление: py -3 -m chess_bot.backup --restore <файл> [--target <dsn>]",
        "",
        "DELETE FROM messages;",
        "DELETE FROM invites;",
        "DELETE FROM games;",
        "-- при восстановлении в Postgres счётчик: SELECT setval('games_id_seq', (SELECT MAX(id) FROM games));",
        "",
    ]
    for table in ("games", "invites", "messages"):
        rows = data.get(table) or []
        if not rows:
            continue
        columns = list(rows[0].keys())
        for row in rows:
            values = ", ".join(_quote(row.get(column)) for column in columns)
            lines.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values});")
    lines.append("")
    return "\n".join(lines)


def parse_sql(text: str) -> dict[str, list[dict[str, Any]]]:
    """Минимальный разбор собственного дампа: только наши INSERT-ы."""
    result: dict[str, list[dict[str, Any]]] = {"games": [], "invites": [], "messages": []}
    pattern = re.compile(r"^INSERT INTO (\w+) \(([^)]+)\) VALUES \((.*)\);$")
    for raw in text.splitlines():
        line = raw.strip()
        match = pattern.match(line)
        if not match:
            continue
        table, columns_raw, values_raw = match.groups()
        if table not in result:
            continue
        columns = [name.strip() for name in columns_raw.split(",")]
        values = _split_values(values_raw)
        result[table].append(dict(zip(columns, values)))
    return result


def _split_values(raw: str) -> list[Any]:
    values: list[Any] = []
    buffer = ""
    in_string = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if in_string:
            if char == "'":
                if index + 1 < len(raw) and raw[index + 1] == "'":
                    buffer += "'"
                    index += 2
                    continue
                in_string = False
            else:
                buffer += char
        else:
            if char == "'":
                in_string = True
            elif char == ",":
                values.append(_convert(buffer.strip()))
                buffer = ""
            else:
                buffer += char
        index += 1
    if buffer.strip():
        values.append(_convert(buffer.strip()))
    return values


def _convert(token: str) -> Any:
    if token == "NULL":
        return None
    if token in ("TRUE", "FALSE"):
        return token == "TRUE"
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    return token


def restore(store: GameStore, sql_text: str, *, replace: bool = True) -> dict[str, int]:
    data = parse_sql(sql_text)
    return store.import_tables(data, replace=replace)


# --------------------------------------------------------------------- Google Drive


class Drive:
    """Обёртка над Google Drive API. При отсутствии настроек работает как заглушка."""

    def __init__(self) -> None:
        self.folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
        self.enabled = False
        self._service = None
        self._reason = ""

    def _load_service(self):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
        credentials_file = os.environ.get("GDRIVE_SERVICE_ACCOUNT_FILE", "").strip()
        if credentials_json:
            import json

            info = json.loads(credentials_json)
            credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        elif credentials_file and Path(credentials_file).exists():
            credentials = service_account.Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        else:
            raise RuntimeError("нет GDRIVE_SERVICE_ACCOUNT_JSON или GDRIVE_SERVICE_ACCOUNT_FILE")
        return build("drive", "v3", credentials=credentials, cache_discovery=False)

    @property
    def service(self):
        if self._service is None:
            if not self.folder_id:
                raise RuntimeError("нет GDRIVE_FOLDER_ID")
            self._service = self._load_service()
        return self._service

    def available(self) -> bool:
        try:
            self.service
            self.enabled = True
            return True
        except Exception as error:  # noqa: BLE001
            self._reason = str(error)
            log.warning("Google Drive недоступен: %s", error)
            return False

    def upload(self, name: str, content: bytes) -> str | None:
        from googleapiclient.http import MediaInMemoryUpload

        media = MediaInMemoryUpload(content, mimetype="application/sql", resumable=False)
        created = self.service.files().create(
            body={"name": name, "parents": [self.folder_id]}, media_body=media, fields="id"
        ).execute()
        return created.get("id")

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        query = f"'{self.folder_id}' in parents and name contains '{BACKUP_PREFIX}' and trashed = false"
        result = self.service.files().list(
            q=query, orderBy="createdTime desc", pageSize=limit,
            fields="files(id, name, size, createdTime)",
        ).execute()
        return result.get("files", [])

    def download(self, name: str) -> bytes:
        import io

        from googleapiclient.http import MediaIoBaseDownload

        found = [f for f in self.list(200) if f["name"] == name]
        if not found:
            raise FileNotFoundError(f"в Drive нет файла {name}")
        request = self.service.files().get_media(fileId=found[0]["id"])
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def prune(self, keep: int) -> int:
        files = self.list(500)
        removed = 0
        for file in files[keep:]:
            self.service.files().delete(fileId=file["id"]).execute()
            removed += 1
        return removed


# --------------------------------------------------------------------- сценарии


def _dump_name(created: datetime | None = None) -> str:
    created = created or datetime.now(timezone.utc)
    return f"{BACKUP_PREFIX}{created:%Y-%m-%d_%H%M%S}.sql"


def run_backup(store: GameStore, *, local_only: bool = False, drive: Drive | None = None,
               keep: int = DEFAULT_KEEP, local_dir: Path = LOCAL_DIR) -> dict[str, Any]:
    created = datetime.now(timezone.utc)
    sql_text = to_sql(store, created)
    payload = sql_text.encode("utf-8")
    name = _dump_name(created)

    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / name
    local_path.write_bytes(payload)

    report: dict[str, Any] = {
        "name": name,
        "bytes": len(payload),
        "local": str(local_path),
        "drive": None,
        "pruned": 0,
    }
    if local_only:
        return report

    drive = drive or Drive()
    if not drive.available():
        report["drive_error"] = drive._reason or "не настроен"
        return report
    report["drive"] = drive.upload(name, payload)
    try:
        report["pruned"] = drive.prune(keep)
    except Exception as error:  # noqa: BLE001
        report["prune_error"] = str(error)
        log.warning("Не удалось почистить старые копии: %s", error)
    return report


def resolve_target(explicit: str | None) -> str:
    if explicit:
        return explicit
    return os.environ.get("DATABASE_URL") or os.environ.get("CHESS_DB") or "chess_bot.db"


def run_restore(name_or_path: str, target: str | None = None, *, local_dir: Path = LOCAL_DIR) -> dict[str, Any]:
    path = Path(name_or_path)
    if path.exists():
        sql_text = path.read_text(encoding="utf-8")
        source = str(path)
    else:
        sql_text = Drive().download(Path(name_or_path).name).decode("utf-8")
        source = f"drive:{name_or_path}"

    store = GameStore(resolve_target(target))
    try:
        counts = restore(store, sql_text)
        if store.dialect == "postgres" and counts.get("games"):
            store.conn.execute("SELECT setval('games_id_seq', (SELECT MAX(id) FROM games))")
    finally:
        store.close()
    return {"source": source, "target": resolve_target(target), "counts": counts}


def next_midnight(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow


async def daily_loop(store: GameStore, *, local_only: bool = False, keep: int = DEFAULT_KEEP,
                     hour: int = 0) -> None:
    """Раз в сутки (по умолчанию 00:00) делает копию; при старте — тоже."""
    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        log.info("Следующая копия базы: %s (через %.1f ч)", target.strftime("%Y-%m-%d %H:%M"), wait / 3600)
        await asyncio.sleep(wait)
        try:
            report = await asyncio.to_thread(run_backup, store, local_only=local_only, keep=keep)
            log.info("Копия базы: %s", report)
        except Exception:  # noqa: BLE001
            log.exception("Резервная копия не удалась")


def start_daily(store: GameStore, *, local_only: bool = False, keep: int = DEFAULT_KEEP,
                hour: int = 0) -> asyncio.Task:
    return asyncio.create_task(daily_loop(store, local_only=local_only, keep=keep, hour=hour))


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Копии базы Косатки в Google Drive")
    parser.add_argument("--db", help="источник: путь к SQLite или DSN Postgres")
    parser.add_argument("--local-only", action="store_true", help="не загружать в Drive")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="сколько копий хранить в Drive")
    parser.add_argument("--list", action="store_true", help="показать копии в Drive")
    parser.add_argument("--restore", metavar="ФАЙЛ", help="восстановить из файла или имени в Drive")
    parser.add_argument("--target", help="куда восстановить (DSN или путь), по умолчанию DATABASE_URL/CHESS_DB")
    parser.add_argument("--loop", action="store_true", help="ждать 00:00 и сохранять каждый день")
    parser.add_argument("--once-now", action="store_true", help="сделать копию и выйти")
    args = parser.parse_args(argv)

    if args.list:
        drive = Drive()
        if not drive.available():
            print("Google Drive не настроен")
            return 2
        for file in drive.list():
            print(f"{file['name']}  {int(file.get('size', 0)) // 1024} КБ  {file.get('createdTime')}")
        return 0

    if args.restore:
        report = run_restore(args.restore, args.target)
        print(f"Восстановлено в {report['target']} из {report['source']}: {report['counts']}")
        return 0

    store = GameStore(resolve_target(args.db))
    try:
        if args.loop:
            report = run_backup(store, local_only=args.local_only, keep=args.keep)
            print(f"Копия: {report}")
            asyncio.run(daily_loop(store, local_only=args.local_only, keep=args.keep))
            return 0
        report = run_backup(store, local_only=args.local_only, keep=args.keep)
        print(f"Копия: {report}")
        return 0
    finally:
        if not args.loop:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
