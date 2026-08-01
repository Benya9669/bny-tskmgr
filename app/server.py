from __future__ import annotations

import base64
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
VERSION_FILE = ROOT / "VERSION"
APP_VERSION = os.getenv("TASKFLOW_VERSION", VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "development")
SCHEMA_VERSION = 4
EXPORT_FORMAT = "taskflow-export"
EXPORT_VERSION = 1

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
}
STATUSES = {"inbox", "todo", "in_progress", "done"}
PRIORITIES = {"low", "normal", "high", "urgent"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                project_id TEXT REFERENCES projects(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'inbox',
                priority TEXT NOT NULL DEFAULT 'normal',
                scheduled_date TEXT,
                due_at TEXT,
                estimated_minutes INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                CHECK(status IN ('inbox','todo','in_progress','done')),
                CHECK(priority IN ('low','normal','high','urgent'))
            );
            CREATE INDEX IF NOT EXISTS tasks_owner_updated ON tasks(owner_id, updated_at);
            CREATE INDEX IF NOT EXISTS tasks_owner_schedule ON tasks(owner_id, scheduled_date);
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
            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
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


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class MailDeliveryError(Exception):
    pass


def verification_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def send_verification_email(email: str, display_name: str, token: str) -> None:
    if not SMTP_HOST or not SMTP_FROM:
        raise MailDeliveryError("SMTP host and sender must be configured")
    if SMTP_MODE not in {"starttls", "ssl", "plain"}:
        raise MailDeliveryError("TASKFLOW_SMTP_MODE must be starttls, ssl or plain")
    if not PUBLIC_URL.startswith(("http://", "https://")):
        raise MailDeliveryError("TASKFLOW_PUBLIC_URL must be an HTTP(S) URL")

    # The fragment is never sent in the initial HTTP request or access logs.
    verification_url = f"{PUBLIC_URL}/#verify={token}"
    message = EmailMessage()
    message["Subject"] = "Подтвердите email для TaskFlow"
    message["From"] = SMTP_FROM
    message["To"] = email
    message.set_content(
        f"Здравствуйте, {display_name}!\n\n"
        "Подтвердите email, чтобы завершить регистрацию в TaskFlow:\n"
        f"{verification_url}\n\n"
        f"Ссылка действует {VERIFICATION_TTL_HOURS} ч. Если вы не регистрировались, просто проигнорируйте письмо."
    )

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

    def api(self, method: str, path: str, user_id: str, environ: dict[str, Any]):
        if path == "/api/v1/me" and method == "GET":
            with connect() as db:
                user = db.execute("SELECT id,email,display_name,created_at,email_verified_at FROM users WHERE id=?", (user_id,)).fetchone()
            return self.json_response(200, {"user": row_dict(user)})
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
        if path == "/api/v1/tasks" and method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", ""))
            where, params = ["owner_id=?", "deleted_at IS NULL"], [user_id]
            for field in ("status", "project_id", "scheduled_date"):
                if query.get(field):
                    where.append(f"{field}=?")
                    params.append(query[field][0])
            with connect() as db:
                rows = db.execute(f"SELECT * FROM tasks WHERE {' AND '.join(where)} ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, created_at DESC", params).fetchall()
            return self.json_response(200, {"tasks": [dict(row) for row in rows]})
        if path == "/api/v1/tasks" and method == "POST":
            data, timestamp = validate_task(self.body(environ)), now_iso()
            self.ensure_project(user_id, data.get("project_id"))
            task = {"id": str(self.body_id(environ, data) or uuid.uuid4()), "owner_id": user_id, "project_id": data.get("project_id"), "title": data["title"], "description": data.get("description", ""), "status": data.get("status", "inbox"), "priority": data.get("priority", "normal"), "scheduled_date": data.get("scheduled_date"), "due_at": data.get("due_at"), "estimated_minutes": data.get("estimated_minutes"), "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None}
            try:
                with connect() as db:
                    db.execute("INSERT INTO tasks VALUES (:id,:owner_id,:project_id,:title,:description,:status,:priority,:scheduled_date,:due_at,:estimated_minutes,:created_at,:updated_at,:version,:deleted_at)", task)
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "conflict", "Идентификатор уже используется или проект не существует") from exc
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
                checklist_items = db.execute("SELECT * FROM checklist_items WHERE owner_id=? AND updated_at>? AND updated_at<=? ORDER BY updated_at", (user_id, since, server_time)).fetchall()
            return self.json_response(200, {"cursor": server_time, "tasks": [dict(row) for row in tasks], "projects": [dict(row) for row in projects], "checklist_items": [checklist_dict(row) for row in checklist_items]})
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
        with connect() as db:
            current = db.execute("SELECT * FROM tasks WHERE id=? AND owner_id=? AND deleted_at IS NULL", (task_id, user_id)).fetchone()
            if not current:
                raise ApiError(404, "not_found", "Задача не найдена")
            self.ensure_version(expected, current["version"], "Задача")
            data.update({"updated_at": now_iso(), "version": current["version"] + 1})
            assignments = ",".join(f"{key}=?" for key in data)
            db.execute(f"UPDATE tasks SET {assignments} WHERE id=? AND owner_id=?", (*data.values(), task_id, user_id))
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self.json_response(200, {"task": dict(updated)})

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
            tasks = db.execute("SELECT id,project_id,title,description,status,priority,scheduled_date,due_at,estimated_minutes FROM tasks WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at", (user_id,)).fetchall()
            checklist_items = db.execute("SELECT id,task_id,title,is_done,position FROM checklist_items WHERE owner_id=? AND deleted_at IS NULL ORDER BY task_id,position,created_at", (user_id,)).fetchall()
        return self.json_response(200, {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "exported_at": now_iso(), "data": {"projects": [dict(row) for row in projects], "tasks": [dict(row) for row in tasks], "checklist_items": [checklist_dict(row) for row in checklist_items]}})

    def import_data(self, user_id: str, payload: dict[str, Any]):
        if payload.get("format") != EXPORT_FORMAT or isinstance(payload.get("version"), bool) or payload.get("version") != EXPORT_VERSION or not isinstance(payload.get("data"), dict):
            raise ApiError(422, "invalid_import_format", "Файл не является совместимым экспортом TaskFlow")
        data = payload["data"]
        projects, tasks, checklist_items = data.get("projects"), data.get("tasks"), data.get("checklist_items")
        if not all(isinstance(items, list) for items in (projects, tasks, checklist_items)):
            raise ApiError(422, "invalid_import_data", "В экспорте отсутствуют обязательные коллекции")
        if len(projects) > 1_000 or len(tasks) > 10_000 or len(checklist_items) > 50_000:
            raise ApiError(422, "import_limit_exceeded", "Экспорт превышает допустимое число записей")
        timestamp = now_iso()
        project_ids: dict[str, str] = {}
        task_ids: dict[str, str] = {}
        clean_projects: list[dict[str, Any]] = []
        clean_tasks: list[dict[str, Any]] = []
        clean_checklist: list[dict[str, Any]] = []
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
        for source in tasks:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in task_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор задачи")
            clean = validate_task(source)
            old_project_id = clean.get("project_id")
            if old_project_id is not None and not isinstance(old_project_id, str):
                raise ApiError(422, "invalid_import_reference", "Некорректная ссылка задачи на проект")
            if old_project_id is not None and old_project_id not in project_ids:
                raise ApiError(422, "invalid_import_reference", "Задача ссылается на отсутствующий проект")
            new_id = str(uuid.uuid4())
            task_ids[source["id"]] = new_id
            clean_tasks.append({"id": new_id, "owner_id": user_id, "project_id": project_ids.get(old_project_id), "title": clean["title"], "description": clean.get("description", ""), "status": clean.get("status", "inbox"), "priority": clean.get("priority", "normal"), "scheduled_date": clean.get("scheduled_date"), "due_at": clean.get("due_at"), "estimated_minutes": clean.get("estimated_minutes"), "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None})
        seen_checklist_ids: set[str] = set()
        for source in checklist_items:
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"] or len(source["id"]) > 128 or source["id"] in seen_checklist_ids:
                raise ApiError(422, "invalid_import_data", "Некорректный или повторяющийся идентификатор подзадачи")
            seen_checklist_ids.add(source["id"])
            if not isinstance(source.get("task_id"), str) or source.get("task_id") not in task_ids:
                raise ApiError(422, "invalid_import_reference", "Подзадача ссылается на отсутствующую задачу")
            clean = self.validate_checklist_item(source)
            clean_checklist.append({"id": str(uuid.uuid4()), "owner_id": user_id, "task_id": task_ids[source["task_id"]], "title": clean["title"], "is_done": clean.get("is_done", 0), "position": clean.get("position", 0), "created_at": timestamp, "updated_at": timestamp, "version": 1, "deleted_at": None})
        with connect() as db:
            db.executemany("INSERT INTO projects(id,owner_id,name,color,created_at,updated_at,version,deleted_at,archived_at) VALUES (:id,:owner_id,:name,:color,:created_at,:updated_at,:version,:deleted_at,:archived_at)", clean_projects)
            db.executemany("INSERT INTO tasks(id,owner_id,project_id,title,description,status,priority,scheduled_date,due_at,estimated_minutes,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:project_id,:title,:description,:status,:priority,:scheduled_date,:due_at,:estimated_minutes,:created_at,:updated_at,:version,:deleted_at)", clean_tasks)
            db.executemany("INSERT INTO checklist_items(id,owner_id,task_id,title,is_done,position,created_at,updated_at,version,deleted_at) VALUES (:id,:owner_id,:task_id,:title,:is_done,:position,:created_at,:updated_at,:version,:deleted_at)", clean_checklist)
        return self.json_response(201, {"imported": {"projects": len(clean_projects), "tasks": len(clean_tasks), "checklist_items": len(clean_checklist)}})

    def delete_project(self, user_id: str, project_id: str):
        timestamp = now_iso()
        with connect() as db:
            project = db.execute("SELECT * FROM projects WHERE id=? AND owner_id=? AND deleted_at IS NULL", (project_id, user_id)).fetchone()
            if not project:
                raise ApiError(404, "not_found", "Проект не найден")
            db.execute("UPDATE tasks SET project_id=NULL,updated_at=?,version=version+1 WHERE owner_id=? AND project_id=? AND deleted_at IS NULL", (timestamp, user_id, project_id))
            db.execute("UPDATE projects SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=?", (timestamp, timestamp, project_id, user_id))
        return self.json_response(200, {"deleted": True, "id": project_id, "updated_at": timestamp})

    def delete_task(self, user_id: str, task_id: str, environ: dict[str, Any]):
        timestamp = now_iso()
        with connect() as db:
            result = db.execute("UPDATE tasks SET deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, task_id, user_id))
            if result.rowcount:
                db.execute("UPDATE checklist_items SET deleted_at=?,updated_at=?,version=version+1 WHERE task_id=? AND owner_id=? AND deleted_at IS NULL", (timestamp, timestamp, task_id, user_id))
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
