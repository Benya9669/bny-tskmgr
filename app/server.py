from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlparse
from wsgiref.simple_server import WSGIRequestHandler, make_server


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "web"
DB_PATH = Path(os.getenv("TASKFLOW_DB", ROOT / "data" / "taskflow.db"))
TOKEN_SECRET = os.getenv("TASKFLOW_SECRET", "")
TOKEN_TTL_DAYS = int(os.getenv("TASKFLOW_TOKEN_TTL_DAYS", "30"))
HOST = os.getenv("TASKFLOW_HOST", "0.0.0.0")
PORT = int(os.getenv("TASKFLOW_PORT", "8080"))
ALLOWED_ORIGIN = os.getenv("TASKFLOW_ALLOWED_ORIGIN", "")
PUBLIC_URL = os.getenv("TASKFLOW_PUBLIC_URL", "http://localhost:8080").rstrip("/")
SMTP_HOST = os.getenv("TASKFLOW_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("TASKFLOW_SMTP_PORT", "587"))
SMTP_MODE = os.getenv("TASKFLOW_SMTP_MODE", "starttls").lower()
SMTP_USERNAME = os.getenv("TASKFLOW_SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("TASKFLOW_SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("TASKFLOW_SMTP_FROM", "")
VERIFICATION_TTL_HOURS = int(os.getenv("TASKFLOW_VERIFICATION_TTL_HOURS", "24"))
PASSWORD_RESET_TTL_HOURS = int(os.getenv("TASKFLOW_PASSWORD_RESET_TTL_HOURS", "1"))
EMAIL_CHANGE_TTL_HOURS = int(os.getenv("TASKFLOW_EMAIL_CHANGE_TTL_HOURS", "24"))
VERSION_FILE = ROOT / "VERSION"
APP_VERSION = os.getenv("TASKFLOW_VERSION", VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "development")
SCHEMA_VERSION = 14
EXPORT_FORMAT = "taskflow-export"
EXPORT_VERSION = 9

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("taskflow")

TASK_FIELDS = {
    "title",
    "description",
    "status",
    "priority",
    "scheduled_date",
    "due_at",
    "estimated_minutes",
    "project_id",
    "column_id",
    "recurrence",
    "reminder_offsets",
    "tags",
}
STATUSES = {"inbox", "todo", "in_progress", "done"}
PRIORITIES = {"low", "normal", "high", "urgent"}
RECURRENCES = {"daily", "weekly", "monthly"}
DEFAULT_KANBAN_COLUMNS = (
    ("Входящие", "#64748b", "inbox"),
    ("Запланировано", "#6d5dfc", "todo"),
    ("В работе", "#e8a126", "in_progress"),
    ("Выполнено", "#22a447", "done"),
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ensure_default_kanban_columns(db: sqlite3.Connection, user_id: str, timestamp: str) -> None:
    if db.execute("SELECT 1 FROM kanban_columns WHERE owner_id=? AND deleted_at IS NULL LIMIT 1", (user_id,)).fetchone():
        return
    db.executemany(
        "INSERT INTO kanban_columns(id,owner_id,name,color,semantic_status,position,created_at,updated_at,version,deleted_at) VALUES (?,?,?,?,?,?,?,?,1,NULL)",
        [(str(uuid.uuid4()), user_id, name, color, status, position, timestamp, timestamp) for position, (name, color, status) in enumerate(DEFAULT_KANBAN_COLUMNS)],
    )


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with connect() as db:
        current_version = db.execute("PRAGMA user_version").fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {current_version} is newer than supported schema {SCHEMA_VERSION}")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                email_verified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#6d5dfc',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                archived_at TEXT
            );
            CREATE TABLE IF NOT EXISTS kanban_columns (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#6d5dfc',
                semantic_status TEXT NOT NULL DEFAULT 'todo',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                CHECK(semantic_status IN ('inbox','todo','in_progress','done')),
                CHECK(position >= 0)
            );
            CREATE INDEX IF NOT EXISTS kanban_columns_owner_position ON kanban_columns(owner_id, position, created_at);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                project_id TEXT REFERENCES projects(id),
                column_id TEXT REFERENCES kanban_columns(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'inbox',
                priority TEXT NOT NULL DEFAULT 'normal',
                scheduled_date TEXT,
                due_at TEXT,
                estimated_minutes INTEGER,
                kanban_position INTEGER NOT NULL DEFAULT 0,
                recurrence TEXT,
                reminder_offsets TEXT,
                tags TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                CHECK(status IN ('inbox','todo','in_progress','done')),
                CHECK(priority IN ('low','normal','high','urgent'))
            );
            CREATE INDEX IF NOT EXISTS tasks_owner_updated ON tasks(owner_id, updated_at);
            CREATE INDEX IF NOT EXISTS tasks_owner_schedule ON tasks(owner_id, scheduled_date);
            CREATE TABLE IF NOT EXISTS task_history (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                task_id TEXT NOT NULL REFERENCES tasks(id),
                event_type TEXT NOT NULL,
                changes TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_history_owner_created ON task_history(owner_id, created_at);
            CREATE INDEX IF NOT EXISTS task_history_task_created ON task_history(task_id, created_at);
            CREATE TABLE IF NOT EXISTS checklist_items (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                task_id TEXT NOT NULL REFERENCES tasks(id),
                title TEXT NOT NULL,
                is_done INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                CHECK(is_done IN (0,1)),
                CHECK(position >= 0)
            );
            CREATE INDEX IF NOT EXISTS checklist_owner_updated ON checklist_items(owner_id, updated_at);
            CREATE INDEX IF NOT EXISTS checklist_task_position ON checklist_items(task_id, position, created_at);
            CREATE TABLE IF NOT EXISTS task_messages (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                task_id TEXT NOT NULL REFERENCES tasks(id),
                author_id TEXT NOT NULL REFERENCES users(id),
                body TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'comment',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                edited_at TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                CHECK(kind IN ('comment','system'))
            );
            CREATE INDEX IF NOT EXISTS task_messages_owner_updated ON task_messages(owner_id, updated_at);
            CREATE INDEX IF NOT EXISTS task_messages_task_created ON task_messages(task_id, created_at, id);
            CREATE TABLE IF NOT EXISTS note_folders (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                CHECK(position >= 0)
            );
            CREATE INDEX IF NOT EXISTS note_folders_owner_position ON note_folders(owner_id, position, created_at);
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                folder_id TEXT REFERENCES note_folders(id),
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                is_favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                CHECK(is_favorite IN (0,1))
            );
            CREATE INDEX IF NOT EXISTS notes_owner_updated ON notes(owner_id, updated_at);
            CREATE INDEX IF NOT EXISTS notes_owner_folder ON notes(owner_id, folder_id, is_favorite, updated_at);
            CREATE TABLE IF NOT EXISTS note_links (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                note_id TEXT NOT NULL REFERENCES notes(id),
                task_id TEXT REFERENCES tasks(id),
                project_id TEXT REFERENCES projects(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                CHECK((task_id IS NOT NULL AND project_id IS NULL) OR (task_id IS NULL AND project_id IS NOT NULL))
            );
            CREATE INDEX IF NOT EXISTS note_links_owner_updated ON note_links(owner_id, updated_at);
            CREATE INDEX IF NOT EXISTS note_links_note ON note_links(owner_id, note_id, deleted_at);
            CREATE INDEX IF NOT EXISTS note_links_task ON note_links(owner_id, task_id, deleted_at);
            CREATE INDEX IF NOT EXISTS note_links_project ON note_links(owner_id, project_id, deleted_at);
            CREATE UNIQUE INDEX IF NOT EXISTS note_links_active_task ON note_links(owner_id, note_id, task_id) WHERE deleted_at IS NULL AND task_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS note_links_active_project ON note_links(owner_id, note_id, project_id) WHERE deleted_at IS NULL AND project_id IS NOT NULL;
            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(note_id UNINDEXED, owner_id UNINDEXED, title, content, tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_email_change_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                new_email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        if current_version < 2:
            user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
            if "email_verified_at" not in user_columns:
                db.execute("ALTER TABLE users ADD COLUMN email_verified_at TEXT")
            # Accounts created before email verification existed remain usable.
            db.execute("UPDATE users SET email_verified_at=created_at WHERE email_verified_at IS NULL")
        if current_version < 4:
            project_columns = {row[1] for row in db.execute("PRAGMA table_info(projects)").fetchall()}
            if "archived_at" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT")
        if current_version < 5:
            task_columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            if "column_id" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN column_id TEXT REFERENCES kanban_columns(id)")
            for user in db.execute("SELECT id,created_at FROM users").fetchall():
                ensure_default_kanban_columns(db, user["id"], user["created_at"] or now_iso())
                db.execute(
                    "UPDATE tasks SET column_id=(SELECT id FROM kanban_columns WHERE owner_id=? AND semantic_status=tasks.status AND deleted_at IS NULL ORDER BY position,created_at LIMIT 1) WHERE owner_id=? AND column_id IS NULL",
                    (user["id"], user["id"]),
                )
        if current_version < 8:
            db.execute("DELETE FROM notes_fts")
            db.execute("INSERT INTO notes_fts(note_id,owner_id,title,content) SELECT id,owner_id,title,content FROM notes WHERE deleted_at IS NULL")
        if current_version < 9:
            task_columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            if "kanban_position" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN kanban_position INTEGER NOT NULL DEFAULT 0")
            rows = db.execute("SELECT id,owner_id,column_id FROM tasks WHERE deleted_at IS NULL ORDER BY owner_id,column_id,CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,created_at DESC,id").fetchall()
            positions: dict[tuple[str, str | None], int] = {}
            for row in rows:
                key = (row["owner_id"], row["column_id"])
                positions[key] = positions.get(key, 0) + 1024
                db.execute("UPDATE tasks SET kanban_position=? WHERE id=?", (positions[key], row["id"]))
        if current_version < 10:
            task_columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            if "recurrence" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT")
        if current_version < 11:
            task_columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            if "reminder_offsets" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN reminder_offsets TEXT")
        if current_version < 12:
            task_columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            if "tags" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN tags TEXT")
        if current_version < 14:
            db.execute("DELETE FROM password_reset_tokens WHERE expires_at <= ?", (now_iso(),))
            db.execute("DELETE FROM pending_email_change_tokens WHERE expires_at <= ?", (now_iso(),))
        db.execute("CREATE INDEX IF NOT EXISTS tasks_owner_column_position ON tasks(owner_id,column_id,kanban_position,created_at,id)")
        if current_version < SCHEMA_VERSION:
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt, expected = encoded.split("$", 3)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def secret() -> bytes:
    value = TOKEN_SECRET or "development-only-change-me"
    return value.encode()


def issue_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": int(time.time()) + TOKEN_TTL_DAYS * 86400}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = base64.urlsafe_b64encode(hmac.new(secret(), body, hashlib.sha256).digest()).rstrip(b"=")
    return f"{body.decode()}.{signature.decode()}"


def verify_token(token: str) -> str | None:
    try:
        body, supplied = token.split(".", 1)
        expected = base64.urlsafe_b64encode(hmac.new(secret(), body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return payload["sub"] if payload["exp"] > time.time() else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def checklist_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["is_done"] = bool(item["is_done"])
    return item


def note_dict(row: sqlite3.Row) -> dict[str, Any]:
    note = dict(row)
    note["is_favorite"] = bool(note["is_favorite"])
    return note


def task_dict(row: sqlite3.Row) -> dict[str, Any]:
    task = dict(row)
    task["reminder_offsets"] = json.loads(task["reminder_offsets"]) if task.get("reminder_offsets") else []
    task["tags"] = json.loads(task["tags"]) if task.get("tags") else []
    return task


def add_task_history(db: sqlite3.Connection, user_id: str, task_id: str, event_type: str, changes: dict[str, Any], timestamp: str) -> None:
    db.execute(
        "INSERT INTO task_history(id,owner_id,task_id,event_type,changes,created_at) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), user_id, task_id, event_type, json.dumps(changes, ensure_ascii=False, separators=(",", ":")), timestamp),
    )


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class MailDeliveryError(Exception):
    pass


def verification_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def send_email(email: str, subject: str, content: str) -> None:
    if not SMTP_HOST or not SMTP_FROM:
        raise MailDeliveryError("SMTP host and sender must be configured")
    if SMTP_MODE not in {"starttls", "ssl", "plain"}:
        raise MailDeliveryError("TASKFLOW_SMTP_MODE must be starttls, ssl or plain")
    if not PUBLIC_URL.startswith(("http://", "https://")):
        raise MailDeliveryError("TASKFLOW_PUBLIC_URL must be an HTTP(S) URL")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    message["To"] = email
    message.set_content(content)

    try:
        smtp_factory = smtplib.SMTP_SSL if SMTP_MODE == "ssl" else smtplib.SMTP
        with smtp_factory(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            if SMTP_MODE == "starttls":
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailDeliveryError("SMTP delivery failed") from exc


def send_verification_email(email: str, display_name: str, token: str) -> None:
    # The fragment is never sent in the initial HTTP request or access logs.
    send_email(
        email,
        "Подтвердите email для TaskFlow",
        f"Здравствуйте, {display_name}!\n\n"
        "Подтвердите email, чтобы завершить регистрацию в TaskFlow:\n"
        f"{PUBLIC_URL}/#verify={token}\n\n"
        f"Ссылка действует {VERIFICATION_TTL_HOURS} ч. Если вы не регистрировались, просто проигнорируйте письмо.",
    )


def send_password_reset_email(email: str, display_name: str, token: str) -> None:
    send_email(
        email,
        "Сброс пароля TaskFlow",
        f"Здравствуйте, {display_name}!\n\n"
        "Чтобы установить новый пароль, откройте ссылку:\n"
        f"{PUBLIC_URL}/#reset-password={token}\n\n"
        f"Ссылка действует {PASSWORD_RESET_TTL_HOURS} ч. Если это были не вы, просто проигнорируйте письмо.",
    )


def send_email_change_confirmation(email: str, display_name: str, token: str) -> None:
    send_email(
        email,
        "Подтвердите новый email TaskFlow",
        f"Здравствуйте, {display_name}!\n\n"
        "Подтвердите изменение email по ссылке:\n"
        f"{PUBLIC_URL}/#confirm-email-change={token}\n\n"
        f"Ссылка действует {EMAIL_CHANGE_TTL_HOURS} ч. Если это были не вы, просто проигнорируйте письмо.",
    )


MAIL_SENDER: Callable[[str, str, str], None] = send_verification_email


def smtp_ready() -> bool:
    return bool(SMTP_HOST and SMTP_FROM and PUBLIC_URL.startswith(("http://", "https://")) and SMTP_MODE in {"starttls", "ssl", "plain"})


class RequestHandler(WSGIRequestHandler):
    def log_message(self, message_format: str, *args: Any) -> None:
        safe_request = f"{getattr(self, 'command', '')} {urlparse(getattr(self, 'path', '')).path} {getattr(self, 'request_version', '')}".strip()
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "client": self.client_address[0],
                    "request": safe_request,
                    "message": message_format % args,
                },
                ensure_ascii=False,
            )
        )


def validate_task(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    clean = {key: data[key] for key in TASK_FIELDS if key in data}
    if not partial or "title" in clean:
        title = str(clean.get("title", "")).strip()
        if not title or len(title) > 240:
            raise ApiError(422, "invalid_title", "Название должно содержать от 1 до 240 символов")
        clean["title"] = title
    if "status" in clean and clean["status"] not in STATUSES:
        raise ApiError(422, "invalid_status", "Неизвестный статус задачи")
    if "priority" in clean and clean["priority"] not in PRIORITIES:
        raise ApiError(422, "invalid_priority", "Неизвестный приоритет")
    if "recurrence" in clean and clean["recurrence"] is not None and clean["recurrence"] not in RECURRENCES:
        raise ApiError(422, "invalid_recurrence", "Повторение может быть ежедневным, еженедельным или ежемесячным")
    if "reminder_offsets" in clean:
        offsets = clean["reminder_offsets"]
        if offsets is None:
            clean["reminder_offsets"] = None
        elif not isinstance(offsets, list) or any(isinstance(offset, bool) or offset not in {0, 15, 60, 1440} for offset in offsets):
            raise ApiError(422, "invalid_reminders", "Напоминания могут быть в срок, за 15 минут, час или день")
        else:
            clean["reminder_offsets"] = json.dumps(sorted(set(offsets))) if offsets else None
    if "tags" in clean:
        tags = clean["tags"]
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ApiError(422, "invalid_tags", "Теги должны быть списком названий")
        tags = sorted({tag.strip() for tag in tags if tag.strip()})
        if len(tags) > 20 or any(len(tag) > 40 for tag in tags):
            raise ApiError(422, "invalid_tags", "Можно указать до 20 тегов длиной до 40 символов")
        clean["tags"] = json.dumps(tags, ensure_ascii=False) if tags else None
    if "description" in clean:
        clean["description"] = "" if clean["description"] is None else str(clean["description"]).strip()
    if "scheduled_date" in clean and clean["scheduled_date"]:
        try:
            datetime.strptime(clean["scheduled_date"], "%Y-%m-%d")
        except (ValueError, TypeError) as exc:
            raise ApiError(422, "invalid_date", "Дата должна быть в формате YYYY-MM-DD") from exc
    if "due_at" in clean and clean["due_at"]:
        try:
            due_at = datetime.fromisoformat(str(clean["due_at"]).replace("Z", "+00:00"))
            if due_at.tzinfo is None:
                raise ValueError
            clean["due_at"] = due_at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except (ValueError, TypeError) as exc:
            raise ApiError(422, "invalid_due_at", "Срок должен быть датой и временем ISO 8601 с часовым поясом") from exc
    if "estimated_minutes" in clean and clean["estimated_minutes"] is not None:
        try:
            clean["estimated_minutes"] = int(clean["estimated_minutes"])
        except (ValueError, TypeError) as exc:
            raise ApiError(422, "invalid_estimate", "Оценка должна быть целым числом") from exc
        if not 1 <= clean["estimated_minutes"] <= 10080:
            raise ApiError(422, "invalid_estimate", "Оценка должна быть от 1 до 10080 минут")
    return clean


def next_recurrence_date(recurrence: str, completed_on: str) -> str:
    date = datetime.strptime(completed_on, "%Y-%m-%d").date()
    if recurrence == "daily":
        return (date + timedelta(days=1)).isoformat()
    if recurrence == "weekly":
        return (date + timedelta(days=7)).isoformat()
    year, month = (date.year + (date.month == 12), date.month % 12 + 1)
    return date.replace(year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1])).isoformat()


class Application:
    def __call__(self, environ: dict[str, Any], start_response: Any):
        try:
            status, headers, body = self.dispatch(environ)
        except ApiError as exc:
            status, headers, body = self.json_response(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except Exception:
            logger.exception("Unhandled request error")
            status, headers, body = self.json_response(500, {"error": {"code": "internal_error", "message": "Внутренняя ошибка сервера"}})
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]

    def json_response(self, status: int, payload: Any):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")]
        if ALLOWED_ORIGIN:
            headers.extend([("Access-Control-Allow-Origin", ALLOWED_ORIGIN), ("Vary", "Origin")])
        return status, headers, body

    def body(self, environ: dict[str, Any]) -> dict[str, Any]:
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
            if size > 1_000_000:
                raise ApiError(413, "payload_too_large", "Тело запроса слишком большое")
            payload = json.loads(environ["wsgi.input"].read(size) or b"{}")
            if not isinstance(payload, dict):
                raise ApiError(400, "invalid_json_type", "Тело запроса должно быть JSON-объектом")
            return payload
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_json", "Некорректный JSON") from exc

    def user_id(self, environ: dict[str, Any]) -> str:
        auth = environ.get("HTTP_AUTHORIZATION", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        user_id = verify_token(token)
        if not user_id:
            raise ApiError(401, "unauthorized", "Требуется авторизация")
        return user_id

    def dispatch(self, environ: dict[str, Any]):
        method = environ["REQUEST_METHOD"]
        path = urlparse(environ.get("PATH_INFO", "/")).path
        if method == "OPTIONS":
            status, headers, body = self.json_response(204, {})
            headers.extend([("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Client-ID"), ("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")])
            return status, headers, body
        if path == "/api/health":
            return self.json_response(200, {"status": "ok", "version": APP_VERSION, "schema_version": SCHEMA_VERSION, "registration_ready": smtp_ready(), "time": now_iso()})
        if path == "/api/v1/auth/register" and method == "POST":
            return self.register(self.body(environ))
        if path == "/api/v1/auth/login" and method == "POST":
            return self.login(self.body(environ))
        if path == "/api/v1/auth/verify-email" and method == "POST":
            return self.verify_email(self.body(environ))
        if path == "/api/v1/auth/resend-verification" and method == "POST":
            return self.resend_verification(self.body(environ))
        if path == "/api/v1/auth/request-password-reset" and method == "POST":
            return self.request_password_reset(self.body(environ))
        if path == "/api/v1/auth/reset-password" and method == "POST":
            return self.reset_password(self.body(environ))
        if path == "/api/v1/auth/confirm-email-change" and method == "POST":
            return self.confirm_email_change(self.body(environ))
        if path.startswith("/api/v1/"):
            user_id = self.user_id(environ)
            return self.api(method, path, user_id, environ)
        return self.static(path)

    def register(self, data: dict[str, Any]):
        email = str(data.get("email", "")).strip().lower()
        name = str(data.get("display_name", "")).strip()
        password = str(data.get("password", ""))
        if not EMAIL_PATTERN.fullmatch(email) or len(email) > 254:
            raise ApiError(422, "invalid_email", "Укажите корректный email")
        if not name or len(name) > 80:
            raise ApiError(422, "invalid_name", "Укажите имя до 80 символов")
        if len(password) < 8:
            raise ApiError(422, "weak_password", "Пароль должен содержать минимум 8 символов")
        user_id, created = str(uuid.uuid4()), now_iso()
        verification_token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(UTC) + timedelta(hours=VERIFICATION_TTL_HOURS)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            with connect() as db:
                db.execute(
                    "INSERT INTO users(id,email,display_name,password_hash,created_at,email_verified_at) VALUES (?,?,?,?,?,NULL)",
                    (user_id, email, name, password_hash(password), created),
                )
                ensure_default_kanban_columns(db, user_id, created)
                project_id = str(uuid.uuid4())
                db.execute("INSERT INTO projects(id,owner_id,name,color,created_at,updated_at) VALUES (?,?,?,?,?,?)", (project_id, user_id, "Личное", "#6d5dfc", created, created))
                db.execute(
                    "INSERT INTO email_verification_tokens(token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                    (verification_token_hash(verification_token), user_id, expires_at, created),
                )
                MAIL_SENDER(email, name, verification_token)
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "email_exists", "Пользователь с таким email уже существует") from exc
        except MailDeliveryError as exc:
            logger.warning("Verification email delivery failed during registration")
            raise ApiError(503, "email_delivery_failed", "Не удалось отправить письмо. Проверьте настройки SMTP и повторите регистрацию") from exc
        return self.json_response(201, {"verification_required": True, "email": email})

    def login(self, data: dict[str, Any]):
        email, password = str(data.get("email", "")).strip().lower(), str(data.get("password", ""))
        with connect() as db:
            user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not password_matches(password, user["password_hash"]):
            raise ApiError(401, "invalid_credentials", "Неверный email или пароль")
        if not user["email_verified_at"]:
            raise ApiError(403, "email_not_verified", "Сначала подтвердите email по ссылке из письма")
        return self.json_response(200, {"token": issue_token(user["id"]), "user": {"id": user["id"], "email": user["email"], "display_name": user["display_name"]}})

    def verify_email(self, data: dict[str, Any]):
        token = str(data.get("token", "")).strip()
        if len(token) < 32:
            raise ApiError(422, "invalid_verification_token", "Некорректная ссылка подтверждения")
        timestamp = now_iso()
        with connect() as db:
            record = db.execute(
                "SELECT t.*,u.email,u.display_name FROM email_verification_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=?",
                (verification_token_hash(token),),
            ).fetchone()
            if not record:
                raise ApiError(422, "invalid_verification_token", "Ссылка подтверждения недействительна или уже использована")
            if record["expires_at"] <= timestamp:
                raise ApiError(422, "verification_token_expired", "Срок действия ссылки истёк. Запросите новое письмо")
            db.execute("UPDATE users SET email_verified_at=? WHERE id=?", (timestamp, record["user_id"]))
            db.execute("DELETE FROM email_verification_tokens WHERE user_id=?", (record["user_id"],))
        return self.json_response(
            200,
            {
                "token": issue_token(record["user_id"]),
                "user": {"id": record["user_id"], "email": record["email"], "display_name": record["display_name"], "email_verified_at": timestamp},
            },
        )

    def resend_verification(self, data: dict[str, Any]):
        email = str(data.get("email", "")).strip().lower()
        generic_response = {"sent": True, "message": "Если аккаунт существует и ещё не подтверждён, письмо будет отправлено"}
        if not EMAIL_PATTERN.fullmatch(email) or len(email) > 254:
            return self.json_response(202, generic_response)
        timestamp = now_iso()
        resend_after = (datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with connect() as db:
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not user or user["email_verified_at"]:
                return self.json_response(202, generic_response)
            existing = db.execute("SELECT created_at FROM email_verification_tokens WHERE user_id=?", (user["id"],)).fetchone()
            if existing and existing["created_at"] > resend_after:
                return self.json_response(202, generic_response)
            verification_token = secrets.token_urlsafe(32)
            expires_at = (datetime.now(UTC) + timedelta(hours=VERIFICATION_TTL_HOURS)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            db.execute("DELETE FROM email_verification_tokens WHERE user_id=?", (user["id"],))
            db.execute(
                "INSERT INTO email_verification_tokens(token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                (verification_token_hash(verification_token), user["id"], expires_at, timestamp),
            )
            try:
                MAIL_SENDER(user["email"], user["display_name"], verification_token)
            except MailDeliveryError as exc:
                logger.warning("Verification email redelivery failed")
                raise ApiError(503, "email_delivery_failed", "Не удалось отправить письмо. Повторите позже") from exc
        return self.json_response(202, generic_response)

    def request_password_reset(self, data: dict[str, Any]):
        email = str(data.get("email", "")).strip().lower()
        generic_response = {"sent": True, "message": "Если аккаунт существует, письмо для сброса пароля будет отправлено"}
        if not EMAIL_PATTERN.fullmatch(email) or len(email) > 254:
            return self.json_response(202, generic_response)
        timestamp = now_iso()
        reset_token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(UTC) + timedelta(hours=PASSWORD_RESET_TTL_HOURS)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            with connect() as db:
                user = db.execute("SELECT id,email,display_name FROM users WHERE email=? AND email_verified_at IS NOT NULL", (email,)).fetchone()
                if not user:
                    return self.json_response(202, generic_response)
                db.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (user["id"],))
                db.execute(
                    "INSERT INTO password_reset_tokens(token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                    (verification_token_hash(reset_token), user["id"], expires_at, timestamp),
                )
                send_password_reset_email(user["email"], user["display_name"], reset_token)
        except MailDeliveryError:
            logger.warning("Password reset email delivery failed")
            with connect() as db:
                db.execute("DELETE FROM password_reset_tokens WHERE token_hash=?", (verification_token_hash(reset_token),))
        return self.json_response(202, generic_response)

    def reset_password(self, data: dict[str, Any]):
        token = str(data.get("token", "")).strip()
        password = str(data.get("new_password", ""))
        if len(token) < 32:
            raise ApiError(422, "invalid_reset_token", "Ссылка сброса недействительна или уже использована")
        if len(password) < 8:
            raise ApiError(422, "weak_password", "Пароль должен содержать минимум 8 символов")
        timestamp = now_iso()
        with connect() as db:
            record = db.execute("SELECT * FROM password_reset_tokens WHERE token_hash=?", (verification_token_hash(token),)).fetchone()
            if not record:
                raise ApiError(422, "invalid_reset_token", "Ссылка сброса недействительна или уже использована")
            if record["expires_at"] <= timestamp:
                db.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (record["user_id"],))
                raise ApiError(422, "reset_token_expired", "Срок действия ссылки истёк. Запросите новое письмо")
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash(password), record["user_id"]))
            db.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (record["user_id"],))
        return self.json_response(200, {"reset": True})

    def confirm_email_change(self, data: dict[str, Any]):
        token = str(data.get("token", "")).strip()
        if len(token) < 32:
            raise ApiError(422, "invalid_email_change_token", "Ссылка подтверждения недействительна или уже использована")
        timestamp = now_iso()
        with connect() as db:
            record = db.execute("SELECT * FROM pending_email_change_tokens WHERE token_hash=?", (verification_token_hash(token),)).fetchone()
            if not record:
                raise ApiError(422, "invalid_email_change_token", "Ссылка подтверждения недействительна или уже использована")
            if record["expires_at"] <= timestamp:
                db.execute("DELETE FROM pending_email_change_tokens WHERE user_id=?", (record["user_id"],))
                raise ApiError(422, "email_change_token_expired", "Срок действия ссылки истёк. Запросите новое письмо")
            try:
                db.execute("UPDATE users SET email=?,email_verified_at=? WHERE id=?", (record["new_email"], timestamp, record["user_id"]))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "email_exists", "Пользователь с таким email уже существует") from exc
            db.execute("DELETE FROM pending_email_change_tokens WHERE user_id=?", (record["user_id"],))
            user = db.execute("SELECT id,email,display_name,email_verified_at FROM users WHERE id=?", (record["user_id"],)).fetchone()
        return self.json_response(200, {"user": row_dict(user)})

    def update_account(self, user_id: str, data: dict[str, Any]):
        allowed = {"display_name", "current_password", "new_password", "email"}
        unknown = set(data) - allowed
        if unknown or not set(data) & {"display_name", "new_password", "email"}:
            raise ApiError(422, "invalid_account_update", "Укажите поля аккаунта для изменения")
        timestamp = now_iso()
        with connect() as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                raise ApiError(401, "unauthorized", "Требуется авторизация")
            password_change = "new_password" in data
            email_change = "email" in data
            if password_change or email_change:
                current_password = str(data.get("current_password", ""))
                if not password_matches(current_password, user["password_hash"]):
                    raise ApiError(403, "invalid_current_password", "Текущий пароль указан неверно")
            updates: dict[str, Any] = {}
            if "display_name" in data:
                name = str(data["display_name"]).strip()
                if not name or len(name) > 80:
                    raise ApiError(422, "invalid_name", "Укажите имя до 80 символов")
                updates["display_name"] = name
            if password_change:
                new_password = str(data.get("new_password", ""))
                if len(new_password) < 8:
                    raise ApiError(422, "weak_password", "Пароль должен содержать минимум 8 символов")
                updates["password_hash"] = password_hash(new_password)
            pending_email = None
            if email_change:
                new_email = str(data["email"]).strip().lower()
                if not EMAIL_PATTERN.fullmatch(new_email) or len(new_email) > 254:
                    raise ApiError(422, "invalid_email", "Укажите корректный email")
                if new_email == user["email"].lower():
                    raise ApiError(422, "email_unchanged", "Укажите новый email")
                if db.execute("SELECT 1 FROM users WHERE email=? AND id<>?", (new_email, user_id)).fetchone():
                    raise ApiError(409, "email_exists", "Пользователь с таким email уже существует")
                change_token = secrets.token_urlsafe(32)
                expires_at = (datetime.now(UTC) + timedelta(hours=EMAIL_CHANGE_TTL_HOURS)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                db.execute("DELETE FROM pending_email_change_tokens WHERE user_id=?", (user_id,))
                try:
                    db.execute(
                        "INSERT INTO pending_email_change_tokens(token_hash,user_id,new_email,expires_at,created_at) VALUES (?,?,?,?,?)",
                        (verification_token_hash(change_token), user_id, new_email, expires_at, timestamp),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ApiError(409, "email_change_pending", "Этот email уже ожидает подтверждения") from exc
                pending_email = new_email
                try:
                    send_email_change_confirmation(new_email, updates.get("display_name", user["display_name"]), change_token)
                except MailDeliveryError as exc:
                    db.execute("DELETE FROM pending_email_change_tokens WHERE user_id=?", (user_id,))
                    logger.warning("Email change confirmation delivery failed")
                    raise ApiError(503, "email_delivery_failed", "Не удалось отправить письмо. Повторите позже") from exc
            if updates:
                db.execute(f"UPDATE users SET {','.join(f'{field}=?' for field in updates)} WHERE id=?", (*updates.values(), user_id))
            updated = db.execute("SELECT id,email,display_name,created_at,email_verified_at FROM users WHERE id=?", (user_id,)).fetchone()
        response: dict[str, Any] = {"user": row_dict(updated)}
        if pending_email:
            response["email_change_pending"] = pending_email
        return self.json_response(200, response)

    def api(self, method: str, path: str, user_id: str, environ: dict[str, Any]):
        if path == "/api/v1/me" and method == "GET":
            with connect() as db:
                user = db.execute("SELECT id,email,display_name,created_at,email_verified_at FROM users WHERE id=?", (user_id,)).fetchone()
            return self.json_response(200, {"user": row_dict(user)})
        if path == "/api/v1/account" and method == "PATCH":
            return self.update_account(user_id, self.body(environ))
        if path == "/api/v1/projects" and method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", ""))
            archive_filter = "" if query.get("include_archived") == ["true"] else (" AND archived_at IS NOT NULL" if query.get("archived") == ["true"] else " AND archived_at IS NULL")
            with connect() as db:
                rows = db.execute(f"SELECT * FROM projects WHERE owner_id=? AND deleted_at IS NULL{archive_filter} ORDER BY name", (user_id,)).fetchall()
            return self.json_response(200, {"projects": [dict(row) for row in rows]})
        if path == "/api/v1/projects" and method == "POST":
            data, timestamp = self.body(environ), now_iso()
            project_data = self.validate_project(data)
            project = {"id": str(uuid.uuid4()), "owner_id": user_id, **project_data, "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None, "archived_at": None}
            with connect() as db:
                db.execute("INSERT INTO projects(id,owner_id,name,color,created_at,updated_at,version,deleted_at,archived_at) VALUES (:id,:owner_id,:name,:color,:created_at,:updated_at,:version,:deleted_at,:archived_at)", project)
            return self.json_response(201, {"project": project})
        if path == "/api/v1/kanban/columns" and method == "GET":
            with connect() as db:
                rows = db.execute("SELECT * FROM kanban_columns WHERE owner_id=? AND deleted_at IS NULL ORDER BY position,created_at", (user_id,)).fetchall()
            return self.json_response(200, {"columns": [dict(row) for row in rows]})
        if path == "/api/v1/kanban/columns" and method == "POST":
            data = self.validate_kanban_column(self.body(environ))
            timestamp = now_iso()
            with connect() as db:
                if "position" not in data:
                    data["position"] = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM kanban_columns WHERE owner_id=? AND deleted_at IS NULL", (user_id,)).fetchone()[0]
                column = {"id": str(uuid.uuid4()), "owner_id": user_id, **data, "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None}
                db.execute("INSERT INTO kanban_columns VALUES (:id,:owner_id,:name,:color,:semantic_status,:position,:created_at,:updated_at,:version,:deleted_at)", column)
            return self.json_response(201, {"column": column})
        if path == "/api/v1/kanban/columns/reorder" and method == "POST":
            return self.reorder_kanban_columns(user_id, self.body(environ))
        kanban_column = re.fullmatch(r"/api/v1/kanban/columns/([^/]+)", path)
        if kanban_column:
            if method == "PATCH":
                return self.update_kanban_column(user_id, kanban_column.group(1), self.body(environ))
            if method == "DELETE":
                return self.delete_kanban_column(user_id, kanban_column.group(1), self.body(environ))
        project_archive = re.fullmatch(r"/api/v1/projects/([^/]+)/archive", path)
        if project_archive:
            if method == "POST":
                return self.set_project_archived(user_id, project_archive.group(1), True, self.body(environ))
            if method == "DELETE":
                return self.set_project_archived(user_id, project_archive.group(1), False, self.body(environ))
        if path == "/api/v1/data/export" and method == "GET":
            return self.export_data(user_id)
        if path == "/api/v1/data/import" and method == "POST":
            return self.import_data(user_id, self.body(environ))
        if path == "/api/v1/data/import/yougile" and method == "POST":
            return self.import_yougile_data(user_id, self.body(environ))
        if path == "/api/v1/note-folders" and method == "GET":
            with connect() as db:
                rows = db.execute("SELECT * FROM note_folders WHERE owner_id=? AND deleted_at IS NULL ORDER BY position,created_at", (user_id,)).fetchall()
            return self.json_response(200, {"folders": [dict(row) for row in rows]})
        if path == "/api/v1/note-folders" and method == "POST":
            return self.create_note_folder(user_id, environ)
        note_folder = re.fullmatch(r"/api/v1/note-folders/([^/]+)", path)
        if note_folder:
            if method == "PATCH":
                return self.update_note_folder(user_id, note_folder.group(1), self.body(environ))
            if method == "DELETE":
                return self.delete_note_folder(user_id, note_folder.group(1))
        if path == "/api/v1/notes" and method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", ""))
            where, params = ["owner_id=?", "deleted_at IS NULL"], [user_id]
            if query.get("folder_id"):
                if query["folder_id"][0] == "none":
                    where.append("folder_id IS NULL")
                else:
                    where.append("folder_id=?")
                    params.append(query["folder_id"][0])
            if query.get("favorite") == ["true"]:
                where.append("is_favorite=1")
            for target in ("task_id", "project_id"):
                if query.get(target):
                    where.append(f"EXISTS (SELECT 1 FROM note_links nl WHERE nl.note_id=notes.id AND nl.owner_id=notes.owner_id AND nl.{target}=? AND nl.deleted_at IS NULL)")
                    params.append(query[target][0])
            search = query.get("q", [""])[0].strip()
            if len(search) > 200:
                raise ApiError(422, "invalid_search", "Поисковый запрос должен быть не длиннее 200 символов")
            if search:
                terms = re.findall(r"[\w]+", search, flags=re.UNICODE)
                if not terms:
                    return self.json_response(200, {"notes": []})
                where.append("id IN (SELECT note_id FROM notes_fts WHERE notes_fts MATCH ?)")
                params.append(" AND ".join(f'"{term}"*' for term in terms))
            with connect() as db:
                rows = db.execute(f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY is_favorite DESC,updated_at DESC", params).fetchall()
            return self.json_response(200, {"notes": [note_dict(row) for row in rows]})
        if path == "/api/v1/notes" and method == "POST":
            return self.create_note(user_id, environ)
        if path == "/api/v1/note-links" and method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", ""))
            where, params = ["owner_id=?", "deleted_at IS NULL"], [user_id]
            for field in ("note_id", "task_id", "project_id"):
                if query.get(field):
                    where.append(f"{field}=?")
                    params.append(query[field][0])
            with connect() as db:
                rows = db.execute(f"SELECT * FROM note_links WHERE {' AND '.join(where)} ORDER BY created_at,id", params).fetchall()
            return self.json_response(200, {"note_links": [dict(row) for row in rows]})
        if path == "/api/v1/note-links" and method == "POST":
            return self.create_note_link(user_id, environ)
        note_link = re.fullmatch(r"/api/v1/note-links/([^/]+)", path)
        if note_link and method == "DELETE":
            return self.delete_note_link(user_id, note_link.group(1))
        note_item = re.fullmatch(r"/api/v1/notes/([^/]+)", path)
        if note_item:
            if method == "PATCH":
                return self.update_note(user_id, note_item.group(1), self.body(environ))
            if method == "DELETE":
                return self.delete_note(user_id, note_item.group(1))
        if path == "/api/v1/tasks" and method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", ""))
            where, params = ["owner_id=?", "deleted_at IS NULL"], [user_id]
            for field in ("status", "project_id", "scheduled_date"):
                if query.get(field):
                    where.append(f"{field}=?")
                    params.append(query[field][0])
            scheduled_from = query.get("scheduled_from", [None])[0]
            scheduled_to = query.get("scheduled_to", [None])[0]
            if query.get("scheduled_date") and (scheduled_from or scheduled_to):
                raise ApiError(422, "invalid_date_range", "Точная дата не сочетается с диапазоном")
            for value in (scheduled_from, scheduled_to):
                if value:
                    try:
                        datetime.strptime(value, "%Y-%m-%d")
                    except (ValueError, TypeError) as exc:
                        raise ApiError(422, "invalid_date", "Дата должна быть в формате YYYY-MM-DD") from exc
            if scheduled_from and scheduled_to and scheduled_from > scheduled_to:
                raise ApiError(422, "invalid_date_range", "Начало диапазона должно быть не позже окончания")
            if scheduled_from:
                where.append("scheduled_date>=?")
                params.append(scheduled_from)
            if scheduled_to:
                where.append("scheduled_date<=?")
                params.append(scheduled_to)
            with connect() as db:
                rows = db.execute(f"SELECT * FROM tasks WHERE {' AND '.join(where)} ORDER BY kanban_position,created_at,id", params).fetchall()
            return self.json_response(200, {"tasks": [task_dict(row) for row in rows]})
        if path == "/api/v1/tasks" and method == "POST":
            data, timestamp = validate_task(self.body(environ)), now_iso()
            if data.get("reminder_offsets") and not data.get("due_at"):
                raise ApiError(422, "reminder_requires_due_at", "Для напоминания укажите срок выполнения")
            self.ensure_project(user_id, data.get("project_id"))
            column_id, status = self.resolve_task_column(user_id, data.get("column_id"), data.get("status", "inbox"))
            task = {"id": str(self.body_id(environ, data) or uuid.uuid4()), "owner_id": user_id, "project_id": data.get("project_id"), "column_id": column_id, "title": data["title"], "description": data.get("description", ""), "status": status, "priority": data.get("priority", "normal"), "scheduled_date": data.get("scheduled_date"), "due_at": data.get("due_at"), "estimated_minutes": data.get("estimated_minutes"), "kanban_position": 0, "recurrence": data.get("recurrence"), "reminder_offsets": data.get("reminder_offsets"), "tags": data.get("tags"), "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None}
            try:
                with connect() as db:
                    task["kanban_position"] = db.execute("SELECT COALESCE(MAX(kanban_position),0)+1024 FROM tasks WHERE owner_id=? AND column_id=? AND deleted_at IS NULL", (user_id, column_id)).fetchone()[0]
                    db.execute("INSERT INTO tasks(id,owner_id,project_id,column_id,title,description,status,priority,scheduled_date,due_at,estimated_minutes,kanban_position,recurrence,reminder_offsets,tags,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:project_id,:column_id,:title,:description,:status,:priority,:scheduled_date,:due_at,:estimated_minutes,:kanban_position,:recurrence,:reminder_offsets,:tags,:created_at,:updated_at,:version,:deleted_at)", task)
                    add_task_history(db, user_id, task["id"], "created", {"title": task["title"]}, timestamp)
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "conflict", "Идентификатор уже используется или проект не существует") from exc
            task["reminder_offsets"] = json.loads(task["reminder_offsets"]) if task["reminder_offsets"] else []
            task["tags"] = json.loads(task["tags"]) if task["tags"] else []
            return self.json_response(201, {"task": task})
        if path == "/api/v1/checklist" and method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", ""))
            where, params = ["owner_id=?", "deleted_at IS NULL"], [user_id]
            if query.get("task_id"):
                where.append("task_id=?")
                params.append(query["task_id"][0])
            with connect() as db:
                rows = db.execute(f"SELECT * FROM checklist_items WHERE {' AND '.join(where)} ORDER BY task_id,position,created_at", params).fetchall()
            return self.json_response(200, {"checklist_items": [checklist_dict(row) for row in rows]})
        if path == "/api/v1/messages" and method == "GET":
            task_id = parse_qs(environ.get("QUERY_STRING", "")).get("task_id", [None])[0]
            if not task_id:
                raise ApiError(422, "task_id_required", "Укажите задачу для загрузки обсуждения")
            with connect() as db:
                task = db.execute("SELECT 1 FROM tasks WHERE id=? AND owner_id=? AND deleted_at IS NULL", (task_id, user_id)).fetchone()
                if not task:
                    raise ApiError(404, "not_found", "Задача не найдена")
                rows = db.execute("SELECT m.*,u.display_name AS author_name FROM task_messages m JOIN users u ON u.id=m.author_id WHERE m.task_id=? AND m.owner_id=? AND m.deleted_at IS NULL ORDER BY m.created_at,m.id", (task_id, user_id)).fetchall()
            return self.json_response(200, {"messages": [dict(row) for row in rows]})
        task_history = re.fullmatch(r"/api/v1/tasks/([^/]+)/history", path)
        if task_history and method == "GET":
            with connect() as db:
                task = db.execute("SELECT 1 FROM tasks WHERE id=? AND owner_id=?", (task_history.group(1), user_id)).fetchone()
                if not task:
                    raise ApiError(404, "not_found", "Задача не найдена")
                rows = db.execute("SELECT * FROM task_history WHERE task_id=? AND owner_id=? ORDER BY created_at,id", (task_history.group(1), user_id)).fetchall()
            return self.json_response(200, {"history": [{**dict(row), "changes": json.loads(row["changes"])} for row in rows]})
        message_create = re.fullmatch(r"/api/v1/tasks/([^/]+)/messages", path)
        if message_create and method == "POST":
            return self.create_task_message(user_id, message_create.group(1), environ)
        message_item = re.fullmatch(r"/api/v1/messages/([^/]+)", path)
        if message_item:
            if method == "PATCH":
                return self.update_task_message(user_id, message_item.group(1), self.body(environ))
            if method == "DELETE":
                return self.delete_task_message(user_id, message_item.group(1))
        checklist_create = re.fullmatch(r"/api/v1/tasks/([^/]+)/checklist", path)
        if checklist_create and method == "POST":
            return self.create_checklist_item(user_id, checklist_create.group(1), environ)
        checklist_item = re.fullmatch(r"/api/v1/checklist/([^/]+)", path)
        if checklist_item:
            item_id = checklist_item.group(1)
            if method == "PATCH":
                return self.update_checklist_item(user_id, item_id, self.body(environ))
            if method == "DELETE":
                return self.delete_checklist_item(user_id, item_id)
        task_move = re.fullmatch(r"/api/v1/tasks/([^/]+)/move", path)
        if task_move and method == "POST":
            return self.move_task(user_id, task_move.group(1), self.body(environ))
        if path == "/api/v1/sync" and method == "GET":
            since = parse_qs(environ.get("QUERY_STRING", "")).get("since", ["1970-01-01T00:00:00.000Z"])[0]
            try:
                parsed_since = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if parsed_since.tzinfo is None:
                    raise ValueError
                since = parsed_since.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            except (ValueError, TypeError) as exc:
                raise ApiError(422, "invalid_cursor", "Параметр since должен быть датой ISO 8601 с часовым поясом") from exc
            server_time = now_iso()
            with connect() as db:
                tasks = db.execute("SELECT * FROM tasks WHERE owner_id=? AND updated_at>? AND updated_at<=? ORDER BY updated_at", (user_id, since, server_time)).fetchall()
                projects = db.execute("SELECT * FROM projects WHERE owner_id=? AND updated_at>? AND updated_at<=? ORDER BY updated_at", (user_id, since, server_time)).fetchall()
                columns = db.execute("SELECT * FROM kanban_columns WHERE owner_id=? AND updated_at>? AND updated_at<=? ORDER BY updated_at", (user_id, since, server_time)).fetchall()
                checklist_items = db.execute("SELECT * FROM checklist_items WHERE owner_id=? AND updated_at>? AND updated_at<=? ORDER BY updated_at", (user_id, since, server_time)).fetchall()
                messages = db.execute("SELECT m.*,u.display_name AS author_name FROM task_messages m JOIN users u ON u.id=m.author_id WHERE m.owner_id=? AND m.updated_at>? AND m.updated_at<=? ORDER BY m.updated_at", (user_id, since, server_time)).fetchall()
                note_folders = db.execute("SELECT * FROM note_folders WHERE owner_id=? AND updated_at>? AND updated_at<=? ORDER BY updated_at", (user_id, since, server_time)).fetchall()
                notes = db.execute("SELECT * FROM notes WHERE owner_id=? AND updated_at>? AND updated_at<=? ORDER BY updated_at", (user_id, since, server_time)).fetchall()
                note_links = db.execute("SELECT * FROM note_links WHERE owner_id=? AND updated_at>? AND updated_at<=? ORDER BY updated_at", (user_id, since, server_time)).fetchall()
                history = db.execute("SELECT * FROM task_history WHERE owner_id=? AND created_at>? AND created_at<=? ORDER BY created_at,id", (user_id, since, server_time)).fetchall()
            return self.json_response(200, {"cursor": server_time, "tasks": [task_dict(row) for row in tasks], "projects": [dict(row) for row in projects], "kanban_columns": [dict(row) for row in columns], "checklist_items": [checklist_dict(row) for row in checklist_items], "task_messages": [dict(row) for row in messages], "note_folders": [dict(row) for row in note_folders], "notes": [note_dict(row) for row in notes], "note_links": [dict(row) for row in note_links], "task_history": [{**dict(row), "changes": json.loads(row["changes"])} for row in history]})
        if path.startswith("/api/v1/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            if method == "PATCH":
                return self.update_task(user_id, task_id, self.body(environ))
            if method == "DELETE":
                return self.delete_task(user_id, task_id, environ)
        if path.startswith("/api/v1/projects/"):
            project_id = path.rsplit("/", 1)[-1]
            if method == "PATCH":
                return self.update_project(user_id, project_id, self.body(environ))
            if method == "DELETE":
                return self.delete_project(user_id, project_id)
        raise ApiError(404, "not_found", "Ресурс не найден")

    def body_id(self, environ: dict[str, Any], data: dict[str, Any]) -> str | None:
        value = environ.get("HTTP_X_CLIENT_ID")
        if not value:
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ApiError(422, "invalid_client_id", "X-Client-ID должен быть UUID") from exc

    def update_task(self, user_id: str, task_id: str, payload: dict[str, Any]):
        expected = payload.pop("expected_version", None)
        data = validate_task(payload, partial=True)
        if not data:
            raise ApiError(422, "empty_update", "Нет полей для обновления")
        if "project_id" in data:
            self.ensure_project(user_id, data["project_id"])
        if "column_id" in data or "status" in data:
            column_id, status = self.resolve_task_column(user_id, data.get("column_id"), data.get("status", "inbox"))
            data.update({"column_id": column_id, "status": status})
        with connect() as db:
            current = db.execute("SELECT * FROM tasks WHERE id=? AND owner_id=? AND deleted_at IS NULL", (task_id, user_id)).fetchone()
            if not current:
                raise ApiError(404, "not_found", "Задача не найдена")
            self.ensure_version(expected, current["version"], "Задача")
            if data.get("reminder_offsets", current["reminder_offsets"]) and not data.get("due_at", current["due_at"]):
                raise ApiError(422, "reminder_requires_due_at", "Для напоминания укажите срок выполнения")
            if "column_id" in data and data["column_id"] != current["column_id"]:
                data["kanban_position"] = db.execute("SELECT COALESCE(MAX(kanban_position),0)+1024 FROM tasks WHERE owner_id=? AND column_id=? AND deleted_at IS NULL", (user_id, data["column_id"])).fetchone()[0]
            timestamp = now_iso()
            data.update({"updated_at": timestamp, "version": current["version"] + 1})
            assignments = ",".join(f"{key}=?" for key in data)
            db.execute(f"UPDATE tasks SET {assignments} WHERE id=? AND owner_id=?", (*data.values(), task_id, user_id))
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            add_task_history(db, user_id, task_id, "updated", {key: task_dict(updated)[key] for key in data if key not in {"updated_at", "version", "kanban_position"}}, timestamp)
            recurrence = data.get("recurrence", current["recurrence"])
            next_task = None
            if current["status"] != "done" and updated["status"] == "done" and recurrence:
                column_id, status = self.resolve_task_column(user_id, None, "todo")
                position = db.execute("SELECT COALESCE(MAX(kanban_position),0)+1024 FROM tasks WHERE owner_id=? AND column_id=? AND deleted_at IS NULL", (user_id, column_id)).fetchone()[0]
                next_task = {
                    "id": str(uuid.uuid4()), "owner_id": user_id, "project_id": updated["project_id"], "column_id": column_id,
                    "title": updated["title"], "description": updated["description"], "status": status, "priority": updated["priority"],
                    "scheduled_date": next_recurrence_date(recurrence, timestamp[:10]), "due_at": None,
                    "estimated_minutes": updated["estimated_minutes"], "kanban_position": position, "recurrence": recurrence, "reminder_offsets": updated["reminder_offsets"], "tags": updated["tags"],
                    "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None,
                }
                db.execute("INSERT INTO tasks(id,owner_id,project_id,column_id,title,description,status,priority,scheduled_date,due_at,estimated_minutes,kanban_position,recurrence,reminder_offsets,tags,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:project_id,:column_id,:title,:description,:status,:priority,:scheduled_date,:due_at,:estimated_minutes,:kanban_position,:recurrence,:reminder_offsets,:tags,:created_at,:updated_at,:version,:deleted_at)", next_task)
        return self.json_response(200, {"task": task_dict(updated), "next_task": task_dict(next_task) if next_task else None})

    def move_task(self, user_id: str, task_id: str, payload: dict[str, Any]):
        expected = payload.get("expected_version")
        column_id = payload.get("column_id")
        before_task_id = payload.get("before_task_id")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise ApiError(422, "expected_version_required", "Для перемещения укажите актуальную версию задачи")
        if not isinstance(column_id, str) or (before_task_id is not None and not isinstance(before_task_id, str)):
            raise ApiError(422, "invalid_task_move", "Укажите корректную колонку и позицию")
        timestamp = now_iso()
        with connect() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=? AND owner_id=? AND deleted_at IS NULL", (task_id, user_id)).fetchone()
            column = db.execute("SELECT * FROM kanban_columns WHERE id=? AND owner_id=? AND deleted_at IS NULL", (column_id, user_id)).fetchone()
            if not task or not column:
                raise ApiError(404, "not_found", "Задача или колонка не найдена")
            self.ensure_version(expected, task["version"], "Задача")
            if before_task_id == task_id:
                raise ApiError(422, "invalid_task_move", "Задачу нельзя разместить перед самой собой")
            target_rows = db.execute("SELECT * FROM tasks WHERE owner_id=? AND column_id=? AND deleted_at IS NULL AND id<>? ORDER BY kanban_position,created_at,id", (user_id, column_id, task_id)).fetchall()
            if before_task_id is None:
                insert_at = len(target_rows)
            else:
                insert_at = next((index for index, row in enumerate(target_rows) if row["id"] == before_task_id), -1)
                if insert_at < 0:
                    raise ApiError(422, "invalid_task_move", "Опорная задача отсутствует в целевой колонке")
            previous_position = target_rows[insert_at - 1]["kanban_position"] if insert_at else 0
            next_position = target_rows[insert_at]["kanban_position"] if insert_at < len(target_rows) else previous_position + 2048
            affected_ids: list[str] = []
            if next_position - previous_position <= 1:
                for position, row in enumerate(target_rows, 1):
                    normalized = position * 1024
                    if row["kanban_position"] != normalized:
                        db.execute("UPDATE tasks SET kanban_position=?,updated_at=?,version=version+1 WHERE id=?", (normalized, timestamp, row["id"]))
                        affected_ids.append(row["id"])
                previous_position = insert_at * 1024
                next_position = (insert_at + 1) * 1024 if insert_at < len(target_rows) else previous_position + 2048
            position = (previous_position + next_position) // 2
            db.execute("UPDATE tasks SET column_id=?,status=?,kanban_position=?,updated_at=?,version=version+1 WHERE id=?", (column_id, column["semantic_status"], position, timestamp, task_id))
            moved = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            add_task_history(db, user_id, task_id, "moved", {"column_id": column_id, "status": column["semantic_status"]}, timestamp)
            affected = db.execute(f"SELECT * FROM tasks WHERE id IN ({','.join('?' for _ in affected_ids)}) ORDER BY kanban_position,created_at,id", affected_ids).fetchall() if affected_ids else []
        return self.json_response(200, {"task": task_dict(moved), "affected_tasks": [task_dict(row) for row in affected]})

    def validate_kanban_column(self, data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if not partial or "name" in data:
            name = str(data.get("name", "")).strip()
            if not name or len(name) > 80:
                raise ApiError(422, "invalid_column_name", "Название колонки должно содержать от 1 до 80 символов")
            clean["name"] = name
        if not partial or "color" in data:
            color = str(data.get("color", "#6d5dfc")).strip().lower()
            if not re.fullmatch(r"#[0-9a-f]{6}", color):
                raise ApiError(422, "invalid_column_color", "Цвет колонки должен быть в формате #RRGGBB")
            clean["color"] = color
        if not partial or "semantic_status" in data:
            status = data.get("semantic_status", "todo")
            if status not in STATUSES:
                raise ApiError(422, "invalid_column_status", "Неизвестный системный статус колонки")
            clean["semantic_status"] = status
        if "position" in data:
            position = data["position"]
            if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= 1_000_000:
                raise ApiError(422, "invalid_column_position", "Позиция колонки должна быть целым числом от 0 до 1000000")
            clean["position"] = position
        return clean

    def resolve_task_column(self, user_id: str, column_id: Any, status: str) -> tuple[str, str]:
        with connect() as db:
            if column_id is not None:
                if not isinstance(column_id, str):
                    raise ApiError(422, "invalid_column", "Некорректная ссылка на колонку")
                column = db.execute("SELECT id,semantic_status FROM kanban_columns WHERE id=? AND owner_id=? AND deleted_at IS NULL", (column_id, user_id)).fetchone()
            else:
                column = db.execute("SELECT id,semantic_status FROM kanban_columns WHERE owner_id=? AND semantic_status=? AND deleted_at IS NULL ORDER BY position,created_at LIMIT 1", (user_id, status)).fetchone()
            if not column:
                raise ApiError(422, "invalid_column", "Подходящая колонка не существует")
            return column["id"], column["semantic_status"]

    def update_kanban_column(self, user_id: str, column_id: str, payload: dict[str, Any]):
        expected = payload.pop("expected_version", None)
        data = self.validate_kanban_column(payload, partial=True)
        if not data:
            raise ApiError(422, "empty_update", "Нет полей для обновления")
        with connect() as db:
            current = db.execute("SELECT * FROM kanban_columns WHERE id=? AND owner_id=? AND deleted_at IS NULL", (column_id, user_id)).fetchone()
            if not current:
                raise ApiError(404, "not_found", "Колонка не найдена")
            self.ensure_version(expected, current["version"], "Колонка")
            if "semantic_status" in data and data["semantic_status"] != current["semantic_status"]:
                same_status_count = db.execute("SELECT COUNT(*) FROM kanban_columns WHERE owner_id=? AND semantic_status=? AND deleted_at IS NULL", (user_id, current["semantic_status"])).fetchone()[0]
                if same_status_count <= 1:
                    raise ApiError(422, "last_semantic_column", "Для каждого системного статуса должна оставаться хотя бы одна колонка")
            timestamp = now_iso()
            data.update({"updated_at": timestamp, "version": current["version"] + 1})
            assignments = ",".join(f"{key}=?" for key in data)
            db.execute(f"UPDATE kanban_columns SET {assignments} WHERE id=? AND owner_id=?", (*data.values(), column_id, user_id))
            if "semantic_status" in data:
                db.execute("UPDATE tasks SET status=?,updated_at=?,version=version+1 WHERE column_id=? AND owner_id=? AND deleted_at IS NULL", (data["semantic_status"], timestamp, column_id, user_id))
            updated = db.execute("SELECT * FROM kanban_columns WHERE id=?", (column_id,)).fetchone()
        return self.json_response(200, {"column": dict(updated)})

    def reorder_kanban_columns(self, user_id: str, payload: dict[str, Any]):
        column_ids = payload.get("column_ids")
        if not isinstance(column_ids, list) or not column_ids or any(not isinstance(item, str) for item in column_ids) or len(set(column_ids)) != len(column_ids):
            raise ApiError(422, "invalid_column_order", "Передан некорректный порядок колонок")
        timestamp = now_iso()
        with connect() as db:
            existing = {row["id"] for row in db.execute("SELECT id FROM kanban_columns WHERE owner_id=? AND deleted_at IS NULL", (user_id,)).fetchall()}
            if set(column_ids) != existing:
                raise ApiError(422, "invalid_column_order", "Порядок должен содержать все активные колонки")
            db.executemany("UPDATE kanban_columns SET position=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=?", [(position, timestamp, column_id, user_id) for position, column_id in enumerate(column_ids)])
            rows = db.execute("SELECT * FROM kanban_columns WHERE owner_id=? AND deleted_at IS NULL ORDER BY position,created_at", (user_id,)).fetchall()
        return self.json_response(200, {"columns": [dict(row) for row in rows]})

    def delete_kanban_column(self, user_id: str, column_id: str, payload: dict[str, Any]):
        expected = payload.get("expected_version")
        destination_id = payload.get("move_to_column_id")
        if not isinstance(destination_id, str) or destination_id == column_id:
            raise ApiError(422, "invalid_column_destination", "Выберите другую колонку для переноса задач")
        with connect() as db:
            current = db.execute("SELECT * FROM kanban_columns WHERE id=? AND owner_id=? AND deleted_at IS NULL", (column_id, user_id)).fetchone()
            destination = db.execute("SELECT * FROM kanban_columns WHERE id=? AND owner_id=? AND deleted_at IS NULL", (destination_id, user_id)).fetchone()
            if not current or not destination:
                raise ApiError(404, "not_found", "Колонка не найдена")
            self.ensure_version(expected, current["version"], "Колонка")
            if db.execute("SELECT COUNT(*) FROM kanban_columns WHERE owner_id=? AND deleted_at IS NULL", (user_id,)).fetchone()[0] <= 1:
                raise ApiError(422, "last_column", "Нельзя удалить единственную колонку")
            same_status_count = db.execute("SELECT COUNT(*) FROM kanban_columns WHERE owner_id=? AND semantic_status=? AND deleted_at IS NULL", (user_id, current["semantic_status"])).fetchone()[0]
            if same_status_count <= 1:
                raise ApiError(422, "last_semantic_column", "Для каждого системного статуса должна оставаться хотя бы одна колонка")
            timestamp = now_iso()
            position = db.execute("SELECT COALESCE(MAX(kanban_position),0) FROM tasks WHERE column_id=? AND owner_id=? AND deleted_at IS NULL", (destination_id, user_id)).fetchone()[0]
            moving = db.execute("SELECT id FROM tasks WHERE column_id=? AND owner_id=? AND deleted_at IS NULL ORDER BY kanban_position,created_at,id", (column_id, user_id)).fetchall()
            for row in moving:
                position += 1024
                db.execute("UPDATE tasks SET column_id=?,status=?,kanban_position=?,updated_at=?,version=version+1 WHERE id=?", (destination_id, destination["semantic_status"], position, timestamp, row["id"]))
            db.execute("UPDATE kanban_columns SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=?", (timestamp, timestamp, column_id, user_id))
        return self.json_response(200, {"deleted": True, "id": column_id, "moved_to_column_id": destination_id})

    @staticmethod
    def validate_note_folder(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if not partial or "name" in data:
            name = data.get("name")
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
                raise ApiError(422, "invalid_folder_name", "Название папки должно содержать от 1 до 100 символов")
            clean["name"] = name.strip()
        if "position" in data:
            if isinstance(data["position"], bool) or not isinstance(data["position"], int) or not 0 <= data["position"] <= 1_000_000:
                raise ApiError(422, "invalid_position", "position должен быть целым числом от 0 до 1000000")
            clean["position"] = data["position"]
        return clean

    @staticmethod
    def validate_note(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if not partial or "title" in data:
            title = data.get("title")
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 240:
                raise ApiError(422, "invalid_note_title", "Название заметки должно содержать от 1 до 240 символов")
            clean["title"] = title.strip()
        if "content" in data:
            content = data["content"]
            if not isinstance(content, str) or len(content) > 200_000:
                raise ApiError(422, "invalid_note_content", "Текст заметки должен быть строкой до 200000 символов")
            clean["content"] = content
        if "folder_id" in data:
            folder_id = data["folder_id"]
            if folder_id is not None and not isinstance(folder_id, str):
                raise ApiError(422, "invalid_folder", "Некорректная ссылка на папку")
            clean["folder_id"] = folder_id
        if "is_favorite" in data:
            if not isinstance(data["is_favorite"], bool):
                raise ApiError(422, "invalid_favorite", "is_favorite должен быть логическим значением")
            clean["is_favorite"] = int(data["is_favorite"])
        return clean

    @staticmethod
    def ensure_note_folder(db: sqlite3.Connection, user_id: str, folder_id: str | None) -> None:
        if folder_id is None:
            return
        if not db.execute("SELECT 1 FROM note_folders WHERE id=? AND owner_id=? AND deleted_at IS NULL", (folder_id, user_id)).fetchone():
            raise ApiError(422, "invalid_folder", "Папка заметок не найдена")

    def create_note_folder(self, user_id: str, environ: dict[str, Any]):
        data = self.validate_note_folder(self.body(environ))
        timestamp = now_iso()
        with connect() as db:
            if "position" not in data:
                data["position"] = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM note_folders WHERE owner_id=? AND deleted_at IS NULL", (user_id,)).fetchone()[0]
            folder = {"id": str(self.body_id(environ, data) or uuid.uuid4()), "owner_id": user_id, **data, "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None}
            try:
                db.execute("INSERT INTO note_folders VALUES (:id,:owner_id,:name,:position,:created_at,:updated_at,:version,:deleted_at)", folder)
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "conflict", "Идентификатор папки уже используется") from exc
        return self.json_response(201, {"folder": folder})

    def update_note_folder(self, user_id: str, folder_id: str, payload: dict[str, Any]):
        expected = payload.pop("expected_version", None)
        data = self.validate_note_folder(payload, partial=True)
        if not data:
            raise ApiError(422, "empty_update", "Нет полей для обновления")
        with connect() as db:
            current = db.execute("SELECT * FROM note_folders WHERE id=? AND owner_id=? AND deleted_at IS NULL", (folder_id, user_id)).fetchone()
            if not current:
                raise ApiError(404, "not_found", "Папка не найдена")
            self.ensure_version(expected, current["version"], "Папка")
            data.update({"updated_at": now_iso(), "version": current["version"] + 1})
            db.execute(f"UPDATE note_folders SET {','.join(f'{key}=?' for key in data)} WHERE id=? AND owner_id=?", (*data.values(), folder_id, user_id))
            updated = db.execute("SELECT * FROM note_folders WHERE id=?", (folder_id,)).fetchone()
        return self.json_response(200, {"folder": dict(updated)})

    def delete_note_folder(self, user_id: str, folder_id: str):
        timestamp = now_iso()
        with connect() as db:
            result = db.execute("UPDATE note_folders SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, folder_id, user_id))
            if result.rowcount:
                db.execute("UPDATE notes SET folder_id=NULL,updated_at=?,version=version+1 WHERE folder_id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, folder_id, user_id))
        if not result.rowcount:
            raise ApiError(404, "not_found", "Папка не найдена")
        return self.json_response(200, {"deleted": True, "id": folder_id, "updated_at": timestamp})

    def create_note(self, user_id: str, environ: dict[str, Any]):
        data = self.validate_note(self.body(environ))
        timestamp = now_iso()
        with connect() as db:
            self.ensure_note_folder(db, user_id, data.get("folder_id"))
            note = {"id": str(self.body_id(environ, data) or uuid.uuid4()), "owner_id": user_id, "folder_id": data.get("folder_id"), "title": data["title"], "content": data.get("content", ""), "is_favorite": data.get("is_favorite", 0), "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None}
            try:
                db.execute("INSERT INTO notes VALUES (:id,:owner_id,:folder_id,:title,:content,:is_favorite,:created_at,:updated_at,:version,:deleted_at)", note)
                db.execute("INSERT INTO notes_fts(note_id,owner_id,title,content) VALUES (?,?,?,?)", (note["id"], user_id, note["title"], note["content"]))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "conflict", "Идентификатор заметки уже используется") from exc
        return self.json_response(201, {"note": {**note, "is_favorite": bool(note["is_favorite"])}})

    def update_note(self, user_id: str, note_id: str, payload: dict[str, Any]):
        expected = payload.pop("expected_version", None)
        data = self.validate_note(payload, partial=True)
        if not data:
            raise ApiError(422, "empty_update", "Нет полей для обновления")
        with connect() as db:
            current = db.execute("SELECT * FROM notes WHERE id=? AND owner_id=? AND deleted_at IS NULL", (note_id, user_id)).fetchone()
            if not current:
                raise ApiError(404, "not_found", "Заметка не найдена")
            self.ensure_version(expected, current["version"], "Заметка")
            self.ensure_note_folder(db, user_id, data.get("folder_id", current["folder_id"]))
            data.update({"updated_at": now_iso(), "version": current["version"] + 1})
            db.execute(f"UPDATE notes SET {','.join(f'{key}=?' for key in data)} WHERE id=? AND owner_id=?", (*data.values(), note_id, user_id))
            updated = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            if "title" in data or "content" in data:
                db.execute("DELETE FROM notes_fts WHERE note_id=? AND owner_id=?", (note_id, user_id))
                db.execute("INSERT INTO notes_fts(note_id,owner_id,title,content) VALUES (?,?,?,?)", (note_id, user_id, updated["title"], updated["content"]))
        return self.json_response(200, {"note": note_dict(updated)})

    def delete_note(self, user_id: str, note_id: str):
        timestamp = now_iso()
        with connect() as db:
            result = db.execute("UPDATE notes SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, note_id, user_id))
            if result.rowcount:
                db.execute("DELETE FROM notes_fts WHERE note_id=? AND owner_id=?", (note_id, user_id))
                db.execute("UPDATE note_links SET deleted_at=?,updated_at=?,version=version+1 WHERE note_id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, note_id, user_id))
        if not result.rowcount:
            raise ApiError(404, "not_found", "Заметка не найдена")
        return self.json_response(200, {"deleted": True, "id": note_id, "updated_at": timestamp})

    def create_note_link(self, user_id: str, environ: dict[str, Any]):
        data = self.body(environ)
        note_id, task_id, project_id = data.get("note_id"), data.get("task_id"), data.get("project_id")
        if not isinstance(note_id, str) or (task_id is None) == (project_id is None) or (task_id is not None and not isinstance(task_id, str)) or (project_id is not None and not isinstance(project_id, str)):
            raise ApiError(422, "invalid_note_link", "Укажите заметку и ровно одну задачу или проект")
        timestamp = now_iso()
        link = {"id": str(self.body_id(environ, data) or uuid.uuid4()), "owner_id": user_id, "note_id": note_id, "task_id": task_id, "project_id": project_id, "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None}
        with connect() as db:
            if not db.execute("SELECT 1 FROM notes WHERE id=? AND owner_id=? AND deleted_at IS NULL", (note_id, user_id)).fetchone():
                raise ApiError(404, "not_found", "Заметка не найдена")
            table, target_id = ("tasks", task_id) if task_id is not None else ("projects", project_id)
            if not db.execute(f"SELECT 1 FROM {table} WHERE id=? AND owner_id=? AND deleted_at IS NULL", (target_id, user_id)).fetchone():
                raise ApiError(404, "not_found", "Связываемый объект не найден")
            try:
                db.execute("INSERT INTO note_links VALUES (:id,:owner_id,:note_id,:task_id,:project_id,:created_at,:updated_at,:version,:deleted_at)", link)
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "conflict", "Такая связь уже существует или идентификатор занят") from exc
        return self.json_response(201, {"note_link": link})

    def delete_note_link(self, user_id: str, link_id: str):
        timestamp = now_iso()
        with connect() as db:
            result = db.execute("UPDATE note_links SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, link_id, user_id))
        if not result.rowcount:
            raise ApiError(404, "not_found", "Связь не найдена")
        return self.json_response(200, {"deleted": True, "id": link_id, "updated_at": timestamp})

    def validate_checklist_item(self, data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if not partial or "title" in data:
            title = str(data.get("title", "")).strip()
            if not title or len(title) > 240:
                raise ApiError(422, "invalid_title", "Название подзадачи должно содержать от 1 до 240 символов")
            clean["title"] = title
        if "is_done" in data:
            if not isinstance(data["is_done"], bool):
                raise ApiError(422, "invalid_is_done", "is_done должен быть логическим значением")
            clean["is_done"] = int(data["is_done"])
        if "position" in data:
            if isinstance(data["position"], bool) or not isinstance(data["position"], int) or not 0 <= data["position"] <= 1_000_000:
                raise ApiError(422, "invalid_position", "position должен быть целым числом от 0 до 1000000")
            clean["position"] = data["position"]
        return clean

    @staticmethod
    def validate_message_body(data: dict[str, Any]) -> str:
        body = data.get("body")
        if not isinstance(body, str):
            raise ApiError(422, "invalid_message", "Сообщение должно быть строкой")
        body = body.strip()
        if not body or len(body) > 5_000:
            raise ApiError(422, "invalid_message", "Сообщение должно содержать от 1 до 5000 символов")
        return body

    def create_task_message(self, user_id: str, task_id: str, environ: dict[str, Any]):
        data = self.body(environ)
        body = self.validate_message_body(data)
        timestamp = now_iso()
        message = {"id": str(self.body_id(environ, data) or uuid.uuid4()), "owner_id": user_id, "task_id": task_id, "author_id": user_id, "body": body, "kind": "comment", "created_at": timestamp, "updated_at": timestamp, "edited_at": None, "version": 1, "deleted_at": None}
        with connect() as db:
            task = db.execute("SELECT 1 FROM tasks WHERE id=? AND owner_id=? AND deleted_at IS NULL", (task_id, user_id)).fetchone()
            if not task:
                raise ApiError(404, "not_found", "Задача не найдена")
            try:
                db.execute("INSERT INTO task_messages VALUES (:id,:owner_id,:task_id,:author_id,:body,:kind,:created_at,:updated_at,:edited_at,:version,:deleted_at)", message)
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "conflict", "Идентификатор сообщения уже используется") from exc
            author_name = db.execute("SELECT display_name FROM users WHERE id=?", (user_id,)).fetchone()[0]
        return self.json_response(201, {"message": {**message, "author_name": author_name}})

    def update_task_message(self, user_id: str, message_id: str, payload: dict[str, Any]):
        expected = payload.pop("expected_version", None)
        body = self.validate_message_body(payload)
        timestamp = now_iso()
        with connect() as db:
            current = db.execute("SELECT * FROM task_messages WHERE id=? AND owner_id=? AND deleted_at IS NULL", (message_id, user_id)).fetchone()
            if not current:
                raise ApiError(404, "not_found", "Сообщение не найдено")
            if current["kind"] != "comment":
                raise ApiError(422, "system_message_read_only", "Системную запись истории нельзя редактировать")
            self.ensure_version(expected, current["version"], "Сообщение")
            db.execute("UPDATE task_messages SET body=?,edited_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=?", (body, timestamp, timestamp, message_id, user_id))
            updated = db.execute("SELECT m.*,u.display_name AS author_name FROM task_messages m JOIN users u ON u.id=m.author_id WHERE m.id=?", (message_id,)).fetchone()
        return self.json_response(200, {"message": dict(updated)})

    def delete_task_message(self, user_id: str, message_id: str):
        timestamp = now_iso()
        with connect() as db:
            result = db.execute("UPDATE task_messages SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, message_id, user_id))
        if not result.rowcount:
            raise ApiError(404, "not_found", "Сообщение не найдено")
        return self.json_response(200, {"deleted": True, "id": message_id, "updated_at": timestamp})

    def create_checklist_item(self, user_id: str, task_id: str, environ: dict[str, Any]):
        data = self.validate_checklist_item(self.body(environ))
        timestamp = now_iso()
        with connect() as db:
            task = db.execute("SELECT 1 FROM tasks WHERE id=? AND owner_id=? AND deleted_at IS NULL", (task_id, user_id)).fetchone()
            if not task:
                raise ApiError(404, "not_found", "Задача не найдена")
            if "position" not in data:
                row = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM checklist_items WHERE task_id=? AND owner_id=? AND deleted_at IS NULL", (task_id, user_id)).fetchone()
                data["position"] = row[0]
            item = {"id": str(self.body_id(environ, data) or uuid.uuid4()), "owner_id": user_id, "task_id": task_id, "title": data["title"], "is_done": data.get("is_done", 0), "position": data["position"], "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None}
            try:
                db.execute("INSERT INTO checklist_items VALUES (:id,:owner_id,:task_id,:title,:is_done,:position,:created_at,:updated_at,:version,:deleted_at)", item)
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "conflict", "Идентификатор подзадачи уже используется") from exc
        item["is_done"] = bool(item["is_done"])
        return self.json_response(201, {"checklist_item": item})

    def update_checklist_item(self, user_id: str, item_id: str, payload: dict[str, Any]):
        expected = payload.pop("expected_version", None)
        data = self.validate_checklist_item(payload, partial=True)
        if not data:
            raise ApiError(422, "empty_update", "Нет полей для обновления")
        with connect() as db:
            current = db.execute("SELECT * FROM checklist_items WHERE id=? AND owner_id=? AND deleted_at IS NULL", (item_id, user_id)).fetchone()
            if not current:
                raise ApiError(404, "not_found", "Подзадача не найдена")
            self.ensure_version(expected, current["version"], "Подзадача")
            data.update({"updated_at": now_iso(), "version": current["version"] + 1})
            assignments = ",".join(f"{key}=?" for key in data)
            db.execute(f"UPDATE checklist_items SET {assignments} WHERE id=? AND owner_id=?", (*data.values(), item_id, user_id))
            updated = db.execute("SELECT * FROM checklist_items WHERE id=?", (item_id,)).fetchone()
        item = dict(updated)
        item["is_done"] = bool(item["is_done"])
        return self.json_response(200, {"checklist_item": item})

    def delete_checklist_item(self, user_id: str, item_id: str):
        timestamp = now_iso()
        with connect() as db:
            result = db.execute("UPDATE checklist_items SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, item_id, user_id))
        if not result.rowcount:
            raise ApiError(404, "not_found", "Подзадача не найдена")
        return self.json_response(200, {"deleted": True, "id": item_id, "updated_at": timestamp})

    def ensure_project(self, user_id: str, project_id: str | None) -> None:
        if not project_id:
            return
        with connect() as db:
            exists = db.execute("SELECT 1 FROM projects WHERE id=? AND owner_id=? AND deleted_at IS NULL AND archived_at IS NULL", (project_id, user_id)).fetchone()
        if not exists:
            raise ApiError(422, "invalid_project", "Проект не существует")

    def ensure_version(self, expected: Any, actual: int, resource_name: str) -> None:
        if expected is None:
            return
        try:
            matches = int(expected) == actual
        except (ValueError, TypeError) as exc:
            raise ApiError(422, "invalid_version", "expected_version должен быть целым числом") from exc
        if not matches:
            raise ApiError(409, "version_conflict", f"{resource_name} была изменена на другом устройстве")

    def validate_project(self, data: dict[str, Any], partial: bool = False) -> dict[str, str]:
        clean: dict[str, str] = {}
        if not partial or "name" in data:
            name = str(data.get("name", "")).strip()
            if not name or len(name) > 100:
                raise ApiError(422, "invalid_name", "Название проекта должно содержать от 1 до 100 символов")
            clean["name"] = name
        if not partial or "color" in data:
            color = str(data.get("color", "#6d5dfc")).strip().lower()
            if not re.fullmatch(r"#[0-9a-f]{6}", color):
                raise ApiError(422, "invalid_color", "Цвет проекта должен быть в формате #RRGGBB")
            clean["color"] = color
        return clean

    def update_project(self, user_id: str, project_id: str, payload: dict[str, Any]):
        expected = payload.pop("expected_version", None)
        data = self.validate_project(payload, partial=True)
        if not data:
            raise ApiError(422, "empty_update", "Нет полей для обновления")
        with connect() as db:
            current = db.execute("SELECT * FROM projects WHERE id=? AND owner_id=? AND deleted_at IS NULL AND archived_at IS NULL", (project_id, user_id)).fetchone()
            if not current:
                raise ApiError(404, "not_found", "Проект не найден")
            self.ensure_version(expected, current["version"], "Запись проекта")
            data.update({"updated_at": now_iso(), "version": current["version"] + 1})
            assignments = ",".join(f"{key}=?" for key in data)
            db.execute(f"UPDATE projects SET {assignments} WHERE id=? AND owner_id=?", (*data.values(), project_id, user_id))
            updated = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self.json_response(200, {"project": dict(updated)})

    def set_project_archived(self, user_id: str, project_id: str, archived: bool, payload: dict[str, Any]):
        expected = payload.pop("expected_version", None)
        if payload:
            raise ApiError(422, "invalid_archive_request", "Запрос архива содержит неизвестные поля")
        with connect() as db:
            current = db.execute("SELECT * FROM projects WHERE id=? AND owner_id=? AND deleted_at IS NULL", (project_id, user_id)).fetchone()
            if not current:
                raise ApiError(404, "not_found", "Проект не найден")
            self.ensure_version(expected, current["version"], "Проект")
            if bool(current["archived_at"]) == archived:
                return self.json_response(200, {"project": dict(current)})
            timestamp = now_iso()
            db.execute("UPDATE projects SET archived_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=?", (timestamp if archived else None, timestamp, project_id, user_id))
            updated = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self.json_response(200, {"project": dict(updated)})

    def export_data(self, user_id: str):
        with connect() as db:
            projects = db.execute("SELECT id,name,color,archived_at FROM projects WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at", (user_id,)).fetchall()
            columns = db.execute("SELECT id,name,color,semantic_status,position FROM kanban_columns WHERE owner_id=? AND deleted_at IS NULL ORDER BY position,created_at", (user_id,)).fetchall()
            tasks = db.execute("SELECT id,project_id,column_id,title,description,status,priority,scheduled_date,due_at,estimated_minutes,kanban_position,recurrence,reminder_offsets,tags FROM tasks WHERE owner_id=? AND deleted_at IS NULL ORDER BY column_id,kanban_position,created_at,id", (user_id,)).fetchall()
            checklist_items = db.execute("SELECT id,task_id,title,is_done,position FROM checklist_items WHERE owner_id=? AND deleted_at IS NULL ORDER BY task_id,position,created_at", (user_id,)).fetchall()
            messages = db.execute("SELECT id,task_id,body,kind,created_at,edited_at FROM task_messages WHERE owner_id=? AND deleted_at IS NULL ORDER BY task_id,created_at,id", (user_id,)).fetchall()
            note_folders = db.execute("SELECT id,name,position FROM note_folders WHERE owner_id=? AND deleted_at IS NULL ORDER BY position,created_at", (user_id,)).fetchall()
            notes = db.execute("SELECT id,folder_id,title,content,is_favorite,created_at FROM notes WHERE owner_id=? AND deleted_at IS NULL ORDER BY updated_at", (user_id,)).fetchall()
            note_links = db.execute("SELECT id,note_id,task_id,project_id FROM note_links WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at,id", (user_id,)).fetchall()
        return self.json_response(200, {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "exported_at": now_iso(), "data": {"projects": [dict(row) for row in projects], "kanban_columns": [dict(row) for row in columns], "tasks": [task_dict(row) for row in tasks], "checklist_items": [checklist_dict(row) for row in checklist_items], "task_messages": [dict(row) for row in messages], "note_folders": [dict(row) for row in note_folders], "notes": [note_dict(row) for row in notes], "note_links": [dict(row) for row in note_links]}})

    def import_data(self, user_id: str, payload: dict[str, Any], reuse_columns_by_name: bool = False):
        import_version = payload.get("version")
        if payload.get("format") != EXPORT_FORMAT or isinstance(import_version, bool) or not isinstance(import_version, int) or import_version not in range(1, EXPORT_VERSION + 1) or not isinstance(payload.get("data"), dict):
            raise ApiError(422, "invalid_import_format", "Файл не является совместимым экспортом TaskFlow")
        data = payload["data"]
        projects, tasks, checklist_items = data.get("projects"), data.get("tasks"), data.get("checklist_items")
        columns = data.get("kanban_columns", [])
        messages = data.get("task_messages", [])
        note_folders = data.get("note_folders", [])
        notes = data.get("notes", [])
        note_links = data.get("note_links", [])
        if not all(isinstance(items, list) for items in (projects, columns, tasks, checklist_items, messages, note_folders, notes, note_links)) or (import_version >= 2 and "kanban_columns" not in data) or (import_version >= 3 and "task_messages" not in data) or (import_version >= 4 and ("note_folders" not in data or "notes" not in data)) or (import_version >= 5 and "note_links" not in data):
            raise ApiError(422, "invalid_import_data", "В экспорте отсутствуют обязательные коллекции")
        if len(projects) > 1_000 or len(columns) > 1_000 or len(tasks) > 10_000 or len(checklist_items) > 50_000 or len(messages) > 100_000 or len(note_folders) > 10_000 or len(notes) > 100_000 or len(note_links) > 500_000:
            raise ApiError(422, "import_limit_exceeded", "Экспорт превышает допустимое число записей")
        timestamp = now_iso()
        project_ids: dict[str, str] = {}
        column_ids: dict[str, str] = {}
        column_semantics: dict[str, str] = {}
        task_ids: dict[str, str] = {}
        note_folder_ids: dict[str, str] = {}
        note_ids: dict[str, str] = {}
        clean_projects: list[dict[str, Any]] = []
        clean_columns: list[dict[str, Any]] = []
        clean_tasks: list[dict[str, Any]] = []
        clean_checklist: list[dict[str, Any]] = []
        clean_messages: list[dict[str, Any]] = []
        clean_note_folders: list[dict[str, Any]] = []
        clean_notes: list[dict[str, Any]] = []
        clean_note_links: list[dict[str, Any]] = []
        with connect() as db:
            existing_columns = db.execute("SELECT id,name,semantic_status FROM kanban_columns WHERE owner_id=? AND deleted_at IS NULL ORDER BY position,created_at", (user_id,)).fetchall()
            default_columns: dict[str, str] = {}
            for row in existing_columns:
                default_columns.setdefault(row["semantic_status"], row["id"])
            existing_columns_by_key = {(row["name"].casefold(), row["semantic_status"]): row["id"] for row in existing_columns}
            existing_max_position = db.execute("SELECT COALESCE(MAX(position),-1) FROM kanban_columns WHERE owner_id=? AND deleted_at IS NULL", (user_id,)).fetchone()[0]
            task_position_by_column = {row["column_id"]: row["position"] for row in db.execute("SELECT column_id,COALESCE(MAX(kanban_position),0) AS position FROM tasks WHERE owner_id=? AND deleted_at IS NULL GROUP BY column_id", (user_id,)).fetchall()}
        for source in projects:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in project_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор проекта")
            clean = self.validate_project(source)
            archived_at = source.get("archived_at")
            if archived_at is not None:
                try:
                    archived_date = datetime.fromisoformat(str(archived_at).replace("Z", "+00:00"))
                    if archived_date.tzinfo is None:
                        raise ValueError
                except (ValueError, TypeError) as exc:
                    raise ApiError(422, "invalid_import_data", "Некорректная дата архивации проекта") from exc
            new_id = str(uuid.uuid4())
            project_ids[source["id"]] = new_id
            clean_projects.append({"id": new_id, "owner_id": user_id, **clean, "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None, "archived_at": timestamp if archived_at else None})
        for source in columns:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in column_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор колонки")
            clean = self.validate_kanban_column(source)
            existing_id = existing_columns_by_key.get((clean["name"].casefold(), clean["semantic_status"])) if reuse_columns_by_name else None
            if existing_id:
                column_ids[source["id"]] = existing_id
                column_semantics[source["id"]] = clean["semantic_status"]
                continue
            clean["position"] = existing_max_position + 1 + clean.get("position", len(clean_columns))
            new_id = str(uuid.uuid4())
            column_ids[source["id"]] = new_id
            column_semantics[source["id"]] = clean["semantic_status"]
            if reuse_columns_by_name:
                existing_columns_by_key[(clean["name"].casefold(), clean["semantic_status"])] = new_id
            clean_columns.append({"id": new_id, "owner_id": user_id, **clean, "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None})
        for source in tasks:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in task_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор задачи")
            clean = validate_task(source)
            old_project_id = clean.get("project_id")
            if old_project_id is not None and not isinstance(old_project_id, str):
                raise ApiError(422, "invalid_import_reference", "Некорректная ссылка задачи на проект")
            if old_project_id is not None and old_project_id not in project_ids:
                raise ApiError(422, "invalid_import_reference", "Задача ссылается на отсутствующий проект")
            old_column_id = clean.get("column_id")
            if old_column_id is not None and not isinstance(old_column_id, str):
                raise ApiError(422, "invalid_import_reference", "Некорректная ссылка задачи на колонку")
            if import_version >= 2 and old_column_id is not None and old_column_id not in column_ids:
                raise ApiError(422, "invalid_import_reference", "Задача ссылается на отсутствующую колонку")
            status = column_semantics.get(old_column_id, clean.get("status", "inbox"))
            new_column_id = column_ids.get(old_column_id) if import_version >= 2 else default_columns.get(status)
            if new_column_id is None:
                new_column_id = default_columns.get(status)
            if new_column_id is None:
                raise ApiError(422, "invalid_import_reference", "Для задачи отсутствует подходящая колонка")
            source_position = source.get("kanban_position")
            if import_version >= 6 and (isinstance(source_position, bool) or not isinstance(source_position, int) or source_position < 0):
                raise ApiError(422, "invalid_import_data", "Задача содержит некорректную позицию в канбане")
            task_position_by_column[new_column_id] = task_position_by_column.get(new_column_id, 0) + 1024
            new_id = str(uuid.uuid4())
            task_ids[source["id"]] = new_id
            clean_tasks.append({"id": new_id, "owner_id": user_id, "project_id": project_ids.get(old_project_id), "column_id": new_column_id, "title": clean["title"], "description": clean.get("description", ""), "status": status, "priority": clean.get("priority", "normal"), "scheduled_date": clean.get("scheduled_date"), "due_at": clean.get("due_at"), "estimated_minutes": clean.get("estimated_minutes"), "kanban_position": task_position_by_column[new_column_id], "recurrence": clean.get("recurrence"), "reminder_offsets": clean.get("reminder_offsets"), "tags": clean.get("tags"), "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None})
        seen_checklist_ids: set[str] = set()
        for source in checklist_items:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in seen_checklist_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор подзадачи")
            seen_checklist_ids.add(source["id"])
            if not isinstance(source.get("task_id"), str) or source.get("task_id") not in task_ids:
                raise ApiError(422, "invalid_import_reference", "Подзадача ссылается на отсутствующую задачу")
            clean = self.validate_checklist_item(source)
            clean_checklist.append({"id": str(uuid.uuid4()), "owner_id": user_id, "task_id": task_ids[source["task_id"]], "title": clean["title"], "is_done": clean.get("is_done", 0), "position": clean.get("position", 0), "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None})
        seen_message_ids: set[str] = set()
        for source in messages:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in seen_message_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор сообщения")
            seen_message_ids.add(source["id"])
            if not isinstance(source.get("task_id"), str) or source["task_id"] not in task_ids:
                raise ApiError(422, "invalid_import_reference", "Сообщение ссылается на отсутствующую задачу")
            body = self.validate_message_body(source)
            kind = source.get("kind", "comment")
            if kind not in {"comment", "system"}:
                raise ApiError(422, "invalid_import_data", "Неизвестный тип сообщения")
            try:
                created_at = datetime.fromisoformat(str(source.get("created_at", timestamp)).replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    raise ValueError
                created_at_value = created_at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                edited_at_value = None
                if source.get("edited_at") is not None:
                    edited_at = datetime.fromisoformat(str(source["edited_at"]).replace("Z", "+00:00"))
                    if edited_at.tzinfo is None:
                        raise ValueError
                    edited_at_value = edited_at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            except (ValueError, TypeError) as exc:
                raise ApiError(422, "invalid_import_data", "Сообщение содержит некорректную дату") from exc
            clean_messages.append({"id": str(uuid.uuid4()), "owner_id": user_id, "task_id": task_ids[source["task_id"]], "author_id": user_id, "body": body, "kind": kind, "created_at": created_at_value, "updated_at": timestamp, "edited_at": edited_at_value, "version": 1, "deleted_at": None})
        for source in note_folders:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in note_folder_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор папки заметок")
            clean = self.validate_note_folder(source)
            new_id = str(uuid.uuid4())
            note_folder_ids[source["id"]] = new_id
            clean_note_folders.append({"id": new_id, "owner_id": user_id, "name": clean["name"], "position": clean.get("position", len(clean_note_folders)), "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None})
        seen_note_ids: set[str] = set()
        for source in notes:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in seen_note_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор заметки")
            seen_note_ids.add(source["id"])
            old_folder_id = source.get("folder_id")
            if old_folder_id is not None and (not isinstance(old_folder_id, str) or old_folder_id not in note_folder_ids):
                raise ApiError(422, "invalid_import_reference", "Заметка ссылается на отсутствующую папку")
            clean = self.validate_note(source)
            created_at_value = timestamp
            if source.get("created_at") is not None:
                try:
                    created_at = datetime.fromisoformat(str(source["created_at"]).replace("Z", "+00:00"))
                    if created_at.tzinfo is None:
                        raise ValueError
                    created_at_value = created_at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                except (ValueError, TypeError) as exc:
                    raise ApiError(422, "invalid_import_data", "Заметка содержит некорректную дату") from exc
            new_id = str(uuid.uuid4())
            note_ids[source["id"]] = new_id
            clean_notes.append({"id": new_id, "owner_id": user_id, "folder_id": note_folder_ids.get(old_folder_id), "title": clean["title"], "content": clean.get("content", ""), "is_favorite": clean.get("is_favorite", 0), "created_at": created_at_value, "updated_at": timestamp, "version": 1, "deleted_at": None})
        seen_link_ids: set[str] = set()
        seen_link_targets: set[tuple[str, str, str]] = set()
        for source in note_links:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in seen_link_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор связи заметки")
            seen_link_ids.add(source["id"])
            old_note_id, old_task_id, old_project_id = source.get("note_id"), source.get("task_id"), source.get("project_id")
            if old_note_id not in note_ids or (old_task_id is None) == (old_project_id is None) or (old_task_id is not None and old_task_id not in task_ids) or (old_project_id is not None and old_project_id not in project_ids):
                raise ApiError(422, "invalid_import_reference", "Связь заметки ссылается на отсутствующий объект")
            target_type, target_id = ("task", old_task_id) if old_task_id is not None else ("project", old_project_id)
            key = (old_note_id, target_type, target_id)
            if key in seen_link_targets:
                raise ApiError(422, "invalid_import_data", "Экспорт содержит повторяющуюся связь заметки")
            seen_link_targets.add(key)
            clean_note_links.append({"id": str(uuid.uuid4()), "owner_id": user_id, "note_id": note_ids[old_note_id], "task_id": task_ids.get(old_task_id), "project_id": project_ids.get(old_project_id), "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None})
        with connect() as db:
            db.executemany("INSERT INTO projects(id,owner_id,name,color,created_at,updated_at,version,deleted_at,archived_at) VALUES (:id,:owner_id,:name,:color,:created_at,:updated_at,:version,:deleted_at,:archived_at)", clean_projects)
            db.executemany("INSERT INTO kanban_columns(id,owner_id,name,color,semantic_status,position,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:name,:color,:semantic_status,:position,:created_at,:updated_at,:version,:deleted_at)", clean_columns)
            db.executemany("INSERT INTO tasks(id,owner_id,project_id,column_id,title,description,status,priority,scheduled_date,due_at,estimated_minutes,kanban_position,recurrence,reminder_offsets,tags,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:project_id,:column_id,:title,:description,:status,:priority,:scheduled_date,:due_at,:estimated_minutes,:kanban_position,:recurrence,:reminder_offsets,:tags,:created_at,:updated_at,:version,:deleted_at)", clean_tasks)
            db.executemany("INSERT INTO checklist_items(id,owner_id,task_id,title,is_done,position,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:task_id,:title,:is_done,:position,:created_at,:updated_at,:version,:deleted_at)", clean_checklist)
            db.executemany("INSERT INTO task_messages(id,owner_id,task_id,author_id,body,kind,created_at,updated_at,edited_at,version,deleted_at) VALUES (:id,:owner_id,:task_id,:author_id,:body,:kind,:created_at,:updated_at,:edited_at,:version,:deleted_at)", clean_messages)
            db.executemany("INSERT INTO note_folders(id,owner_id,name,position,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:name,:position,:created_at,:updated_at,:version,:deleted_at)", clean_note_folders)
            db.executemany("INSERT INTO notes(id,owner_id,folder_id,title,content,is_favorite,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:folder_id,:title,:content,:is_favorite,:created_at,:updated_at,:version,:deleted_at)", clean_notes)
            db.executemany("INSERT INTO note_links(id,owner_id,note_id,task_id,project_id,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:note_id,:task_id,:project_id,:created_at,:updated_at,:version,:deleted_at)", clean_note_links)
            db.executemany("INSERT INTO notes_fts(note_id,owner_id,title,content) VALUES (:id,:owner_id,:title,:content)", clean_notes)
        return self.json_response(201, {"imported": {"projects": len(clean_projects), "kanban_columns": len(clean_columns), "tasks": len(clean_tasks), "checklist_items": len(clean_checklist), "task_messages": len(clean_messages), "note_folders": len(clean_note_folders), "notes": len(clean_notes), "note_links": len(clean_note_links)}})

    def import_yougile_data(self, user_id: str, payload: dict[str, Any]):
        project_title = payload.get("title")
        boards = payload.get("boards")
        source_tasks = payload.get("tasks")
        if not isinstance(project_title, str) or not project_title.strip() or len(project_title.strip()) > 100:
            raise ApiError(422, "invalid_yougile_export", "В экспорте YouGile отсутствует корректное название проекта")
        if not isinstance(boards, list) or not boards or not isinstance(source_tasks, dict):
            raise ApiError(422, "invalid_yougile_export", "Файл не является совместимым экспортом YouGile")
        if len(boards) > 100 or len(source_tasks) > 10_000:
            raise ApiError(422, "import_limit_exceeded", "Экспорт YouGile превышает допустимое число записей")

        for task_id, task in source_tasks.items():
            if not task_id or len(task_id) > 128 or not isinstance(task, dict):
                raise ApiError(422, "invalid_yougile_export", "Экспорт YouGile содержит некорректную задачу")
            title = task.get("title")
            description = task.get("description", "")
            subtasks = task.get("subtasks", [])
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 240:
                raise ApiError(422, "invalid_yougile_export", "Экспорт YouGile содержит задачу с некорректным названием")
            if description is not None and not isinstance(description, str):
                raise ApiError(422, "invalid_yougile_export", "Экспорт YouGile содержит некорректное описание задачи")
            if not isinstance(subtasks, list) or any(not isinstance(item, str) or not item or len(item) > 128 for item in subtasks):
                raise ApiError(422, "invalid_yougile_export", "Экспорт YouGile содержит некорректные ссылки на подзадачи")

        roots: list[tuple[str, str, str, str]] = []
        imported_columns: list[dict[str, Any]] = []
        root_ids: set[str] = set()
        column_count = 0
        for board in boards:
            if not isinstance(board, dict) or not isinstance(board.get("title"), str) or not board["title"].strip() or len(board["title"].strip()) > 100:
                raise ApiError(422, "invalid_yougile_export", "Экспорт YouGile содержит доску с некорректным названием")
            columns = board.get("columns")
            if not isinstance(columns, list):
                raise ApiError(422, "invalid_yougile_export", "Экспорт YouGile содержит некорректный список колонок")
            column_count += len(columns)
            if column_count > 1_000:
                raise ApiError(422, "import_limit_exceeded", "Экспорт YouGile содержит слишком много колонок")
            for column in columns:
                if not isinstance(column, dict) or not isinstance(column.get("title"), str) or not column["title"].strip() or len(column["title"].strip()) > 100:
                    raise ApiError(422, "invalid_yougile_export", "Экспорт YouGile содержит колонку с некорректным названием")
                task_refs = column.get("tasks")
                if not isinstance(task_refs, list):
                    raise ApiError(422, "invalid_yougile_export", "Экспорт YouGile содержит некорректный список задач колонки")
                column_title = column["title"].strip()
                column_id = f"yougile-column-{column_count}-{len(imported_columns)}"
                column_status = self.yougile_status(column_title)
                imported_columns.append({"id": column_id, "name": column_title, "color": self.yougile_column_color(column.get("color")), "semantic_status": column_status, "position": len(imported_columns)})
                for task_id in task_refs:
                    if not isinstance(task_id, str) or task_id not in source_tasks:
                        raise ApiError(422, "invalid_yougile_reference", "Колонка YouGile ссылается на отсутствующую задачу")
                    if task_id in root_ids:
                        raise ApiError(422, "invalid_yougile_reference", "Задача YouGile находится более чем в одной колонке")
                    root_ids.add(task_id)
                    roots.append((task_id, board["title"].strip(), column_title, column_id))

        imported_tasks: list[dict[str, Any]] = []
        imported_checklist: list[dict[str, Any]] = []
        visited: set[str] = set()
        child_owner: dict[str, str] = {}
        checklist_positions: dict[str, int] = {}

        def add_subtasks(task_id: str, root_id: str, stack: set[str], depth: int = 0) -> None:
            if task_id in stack:
                raise ApiError(422, "invalid_yougile_reference", "Экспорт YouGile содержит цикл подзадач")
            stack.add(task_id)
            for child_id in source_tasks[task_id].get("subtasks", []):
                if child_id not in source_tasks:
                    raise ApiError(422, "invalid_yougile_reference", "Задача YouGile ссылается на отсутствующую подзадачу")
                if child_id in root_ids:
                    raise ApiError(422, "invalid_yougile_reference", "Задача YouGile одновременно является карточкой и подзадачей")
                if child_id in child_owner and child_owner[child_id] != root_id:
                    raise ApiError(422, "invalid_yougile_reference", "Подзадача YouGile связана с несколькими карточками")
                child_owner[child_id] = root_id
                child = source_tasks[child_id]
                prefix = "↳ " * depth
                position = checklist_positions.get(root_id, 0)
                checklist_positions[root_id] = position + 1
                imported_checklist.append({
                    "id": child_id,
                    "task_id": root_id,
                    "title": f"{prefix}{child['title'].strip()}",
                    "is_done": False,
                    "position": position,
                })
                visited.add(child_id)
                add_subtasks(child_id, root_id, stack, depth + 1)
            stack.remove(task_id)

        for task_id, board_title, column_title, column_id in roots:
            source = source_tasks[task_id]
            description = (source.get("description") or "").strip()
            source_context = f"YouGile · доска «{board_title}» · колонка «{column_title}»"
            imported_tasks.append({
                "id": task_id,
                "project_id": "yougile-project",
                "column_id": column_id,
                "title": source["title"].strip(),
                "description": f"{description}\n\n---\n{source_context}" if description else source_context,
                "status": self.yougile_status(column_title),
                "priority": "normal",
                "kanban_position": (len(imported_tasks) + 1) * 1024,
            })
            visited.add(task_id)
            add_subtasks(task_id, task_id, set())

        if visited != set(source_tasks):
            raise ApiError(422, "invalid_yougile_reference", "Экспорт YouGile содержит задачи вне досок или дерева подзадач")
        if len(imported_checklist) > 50_000:
            raise ApiError(422, "import_limit_exceeded", "Экспорт YouGile содержит слишком много подзадач")

        imported_messages: list[dict[str, Any]] = []
        skipped_chat_messages = 0
        for source_task_id, source_task in source_tasks.items():
            chat = source_task.get("chat")
            messages = chat.get("messages") if isinstance(chat, dict) else None
            if not isinstance(messages, dict):
                continue
            target_task_id = source_task_id if source_task_id in root_ids else child_owner.get(source_task_id)
            for message_id, message in messages.items():
                body = self.yougile_message_body(message)
                timestamp = message.get("timestamp") if isinstance(message, dict) else None
                if not target_task_id or not body or not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
                    skipped_chat_messages += 1
                    continue
                try:
                    created_at = datetime.fromtimestamp(timestamp / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                except (OverflowError, OSError, ValueError):
                    skipped_chat_messages += 1
                    continue
                if source_task_id not in root_ids:
                    body = f"Подзадача «{source_task['title'].strip()}»: {body}"
                if len(body) > 5_000:
                    skipped_chat_messages += 1
                    continue
                properties = message.get("properties")
                is_system = isinstance(properties, dict) and properties.get("fromSystem") is True
                imported_messages.append({
                    "id": str(message_id),
                    "task_id": target_task_id,
                    "body": body,
                    "kind": "system" if is_system else "comment",
                    "created_at": created_at,
                    "edited_at": None,
                })

        converted = {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "data": {
                "projects": [{"id": "yougile-project", "name": project_title.strip(), "color": "#6d5dfc", "archived_at": None}],
                "kanban_columns": imported_columns,
                "tasks": imported_tasks,
                "checklist_items": imported_checklist,
                "task_messages": imported_messages,
                "note_folders": [],
                "notes": [],
                "note_links": [],
            },
        }
        skipped_stickers = sum(len(task.get("stickers", {})) for task in source_tasks.values() if isinstance(task.get("stickers"), dict))
        skipped_stickers += sum(len(board.get("stickers", {})) for board in boards if isinstance(board.get("stickers"), dict))
        skipped_subtask_descriptions = sum(1 for task_id in child_owner if (source_tasks[task_id].get("description") or "").strip())
        status, _, body = self.import_data(user_id, converted, reuse_columns_by_name=True)
        response = json.loads(body)
        response["skipped"] = {"chat_messages": skipped_chat_messages, "stickers": skipped_stickers, "subtask_descriptions": skipped_subtask_descriptions}
        return self.json_response(status, response)

    @staticmethod
    def yougile_message_body(message: Any) -> str | None:
        if not isinstance(message, dict) or message.get("dataType") not in (None, "ChatMessage"):
            return None
        text = message.get("text")
        properties = message.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        if properties.get("fromSystem") is not True:
            return text.strip() if isinstance(text, str) and text.strip() else None
        if properties.get("move") is True:
            return "YouGile: задача перемещена между колонками."
        if properties.get("gtd") is True:
            before = properties.get("before")
            after = properties.get("after")
            if isinstance(before, str) and before and isinstance(after, str) and after:
                return f"YouGile: состояние изменено «{before}» → «{after}»."
            if isinstance(after, str) and after:
                return f"YouGile: установлено состояние «{after}»."
        return text.strip() if isinstance(text, str) and text.strip() else None

    @staticmethod
    def yougile_status(column_title: str) -> str:
        normalized = column_title.casefold()
        if any(marker in normalized for marker in ("готов", "выполн", "заверш", "done", "complete", "closed")):
            return "done"
        if any(marker in normalized for marker in ("процесс", "работ", "progress", "doing")):
            return "in_progress"
        if any(marker in normalized for marker in ("вход", "inbox")):
            return "inbox"
        return "todo"

    @staticmethod
    def yougile_column_color(value: Any) -> str:
        if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
            return value.strip().lower()
        return "#6d5dfc"

    def delete_project(self, user_id: str, project_id: str):
        timestamp = now_iso()
        with connect() as db:
            project = db.execute("SELECT * FROM projects WHERE id=? AND owner_id=? AND deleted_at IS NULL", (project_id, user_id)).fetchone()
            if not project:
                raise ApiError(404, "not_found", "Проект не найден")
            db.execute("UPDATE tasks SET project_id=NULL,updated_at=?,version=version+1 WHERE owner_id=? AND project_id=? AND deleted_at IS NULL", (timestamp, user_id, project_id))
            db.execute("UPDATE projects SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=?", (timestamp, timestamp, project_id, user_id))
            db.execute("UPDATE note_links SET deleted_at=?,updated_at=?,version=version+1 WHERE project_id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, project_id, user_id))
        return self.json_response(200, {"deleted": True, "id": project_id, "updated_at": timestamp})

    def delete_task(self, user_id: str, task_id: str, environ: dict[str, Any]):
        timestamp = now_iso()
        with connect() as db:
            result = db.execute("UPDATE tasks SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, task_id, user_id))
            if result.rowcount:
                add_task_history(db, user_id, task_id, "deleted", {}, timestamp)
                db.execute("UPDATE checklist_items SET deleted_at=?,updated_at=?,version=version+1 WHERE task_id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, task_id, user_id))
                db.execute("UPDATE task_messages SET deleted_at=?,updated_at=?,version=version+1 WHERE task_id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, task_id, user_id))
                db.execute("UPDATE note_links SET deleted_at=?,updated_at=?,version=version+1 WHERE task_id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, task_id, user_id))
        if not result.rowcount:
            raise ApiError(404, "not_found", "Задача не найдена")
        return self.json_response(200, {"deleted": True, "id": task_id, "updated_at": timestamp})

    def static(self, path: str):
        filename = "index.html" if path == "/" else path.lstrip("/")
        file_path = (STATIC_DIR / filename).resolve()
        if STATIC_DIR.resolve() not in file_path.parents or not file_path.is_file():
            file_path = STATIC_DIR / "index.html"
        mime = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}.get(file_path.suffix, "application/octet-stream")
        body = file_path.read_bytes()
        return 200, [("Content-Type", mime), ("Content-Length", str(len(body)))], body


def main() -> None:
    init_db()
    if not TOKEN_SECRET:
        logger.warning("TASKFLOW_SECRET is not set; using an insecure development secret")
    if not smtp_ready():
        logger.warning("SMTP is not configured; new account registration will be unavailable")
    logger.info("TaskFlow listening on http://%s:%s", HOST, PORT)
    with make_server(HOST, PORT, Application(), handler_class=RequestHandler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
