import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import server
from app.backup import create_backup


class ApiClient:
    def __init__(self):
        self.app = server.Application()
        self.token = ""

    def request(self, method, path, body=None, headers=None):
        raw = json.dumps(body if body is not None else {}).encode()
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path.split("?", 1)[0],
            "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
            "CONTENT_LENGTH": str(len(raw)) if body is not None else "0",
            "wsgi.input": io.BytesIO(raw),
            "HTTP_AUTHORIZATION": f"Bearer {self.token}" if self.token else "",
        }
        environ.update(headers or {})
        captured = {}
        response = self.app(environ, lambda status, response_headers: captured.update(status=status, headers=response_headers))
        return int(captured["status"].split()[0]), json.loads(b"".join(response) or b"{}")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.temp.name) / "test.db"
        server.init_db()
        self.sent_emails = []
        self.original_mail_sender = server.MAIL_SENDER
        server.MAIL_SENDER = lambda email, display_name, token: self.sent_emails.append((email, display_name, token))
        self.client = ApiClient()
        data = self.register_and_verify(self.client, "user@example.com", "Тест")
        self.client.token = data["token"]

    def register_and_verify(self, client, email, display_name):
        status, registration = client.request("POST", "/api/v1/auth/register", {"email": email, "display_name": display_name, "password": "correct-horse"})
        self.assertEqual(status, 201)
        self.assertTrue(registration["verification_required"])
        verification_token = self.sent_emails[-1][2]
        status, verified = client.request("POST", "/api/v1/auth/verify-email", {"token": verification_token})
        self.assertEqual(status, 200)
        return verified

    def tearDown(self):
        server.MAIL_SENDER = self.original_mail_sender
        self.temp.cleanup()

    def test_task_lifecycle_and_sync_tombstone(self):
        status, created = self.client.request("POST", "/api/v1/tasks", {"title": "Первая задача", "scheduled_date": "2026-08-01"})
        self.assertEqual(status, 201)
        task = created["task"]
        status, updated = self.client.request("PATCH", f"/api/v1/tasks/{task['id']}", {"status": "done", "expected_version": 1})
        self.assertEqual((status, updated["task"]["version"]), (200, 2))
        status, _ = self.client.request("DELETE", f"/api/v1/tasks/{task['id']}")
        self.assertEqual(status, 200)
        status, sync = self.client.request("GET", "/api/v1/sync?since=1970-01-01T00:00:00.000Z")
        self.assertEqual(status, 200)
        self.assertIsNotNone(sync["tasks"][0]["deleted_at"])

    def test_checklist_lifecycle_sync_and_parent_delete(self):
        _, created = self.client.request("POST", "/api/v1/tasks", {"title": "Релиз"})
        task_id = created["task"]["id"]
        status, first = self.client.request("POST", f"/api/v1/tasks/{task_id}/checklist", {"title": "Проверить тесты"})
        self.assertEqual(status, 201)
        item = first["checklist_item"]
        self.assertEqual((item["position"], item["is_done"], item["version"]), (0, False, 1))

        status, second = self.client.request("POST", f"/api/v1/tasks/{task_id}/checklist", {"title": "Обновить changelog"})
        self.assertEqual((status, second["checklist_item"]["position"]), (201, 1))
        status, listed = self.client.request("GET", f"/api/v1/checklist?task_id={task_id}")
        self.assertEqual(status, 200)
        self.assertEqual([entry["title"] for entry in listed["checklist_items"]], ["Проверить тесты", "Обновить changelog"])

        status, updated = self.client.request("PATCH", f"/api/v1/checklist/{item['id']}", {"title": "Прогнать тесты", "is_done": True, "expected_version": 1})
        self.assertEqual(status, 200)
        self.assertTrue(updated["checklist_item"]["is_done"])
        self.assertEqual(updated["checklist_item"]["version"], 2)

        status, stale = self.client.request("PATCH", f"/api/v1/checklist/{item['id']}", {"is_done": False, "expected_version": 1})
        self.assertEqual((status, stale["error"]["code"]), (409, "version_conflict"))
        status, _ = self.client.request("DELETE", f"/api/v1/tasks/{task_id}")
        self.assertEqual(status, 200)
        status, sync = self.client.request("GET", "/api/v1/sync?since=1970-01-01T00:00:00.000Z")
        self.assertEqual(status, 200)
        synced_items = {entry["id"]: entry for entry in sync["checklist_items"]}
        self.assertIsNotNone(synced_items[item["id"]]["deleted_at"])
        self.assertIsNotNone(synced_items[second["checklist_item"]["id"]]["deleted_at"])

    def test_checklist_is_private_and_validated(self):
        _, created = self.client.request("POST", "/api/v1/tasks", {"title": "Личная"})
        task_id = created["task"]["id"]
        _, created_item = self.client.request("POST", f"/api/v1/tasks/{task_id}/checklist", {"title": "Секретный шаг"})
        item_id = created_item["checklist_item"]["id"]
        status, invalid = self.client.request("POST", f"/api/v1/tasks/{task_id}/checklist", {"title": "   "})
        self.assertEqual((status, invalid["error"]["code"]), (422, "invalid_title"))
        status, invalid = self.client.request("PATCH", f"/api/v1/checklist/{item_id}", {"is_done": 1})
        self.assertEqual((status, invalid["error"]["code"]), (422, "invalid_is_done"))

        other = ApiClient()
        registration = self.register_and_verify(other, "checklist-other@example.com", "Другой")
        other.token = registration["token"]
        status, _ = other.request("POST", f"/api/v1/tasks/{task_id}/checklist", {"title": "Чужая"})
        self.assertEqual(status, 404)
        status, _ = other.request("PATCH", f"/api/v1/checklist/{item_id}", {"is_done": True})
        self.assertEqual(status, 404)
        status, listed = other.request("GET", "/api/v1/checklist")
        self.assertEqual((status, listed["checklist_items"]), (200, []))

    def test_optimistic_lock_rejects_stale_update(self):
        _, created = self.client.request("POST", "/api/v1/tasks", {"title": "Конфликт"})
        task_id = created["task"]["id"]
        self.client.request("PATCH", f"/api/v1/tasks/{task_id}", {"priority": "high", "expected_version": 1})
        status, data = self.client.request("PATCH", f"/api/v1/tasks/{task_id}", {"priority": "low", "expected_version": 1})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "version_conflict")

    def test_requires_authentication(self):
        self.client.token = ""
        status, _ = self.client.request("GET", "/api/v1/tasks")
        self.assertEqual(status, 401)

    def test_project_lifecycle_keeps_tasks(self):
        status, created = self.client.request("POST", "/api/v1/projects", {"name": "Работа", "color": "#112233"})
        self.assertEqual(status, 201)
        project = created["project"]
        status, changed = self.client.request("PATCH", f"/api/v1/projects/{project['id']}", {"name": "Продукт", "color": "#334455", "expected_version": 1})
        self.assertEqual((status, changed["project"]["version"]), (200, 2))
        _, created_task = self.client.request("POST", "/api/v1/tasks", {"title": "Задача проекта", "project_id": project["id"]})
        status, _ = self.client.request("DELETE", f"/api/v1/projects/{project['id']}")
        self.assertEqual(status, 200)
        status, tasks = self.client.request("GET", "/api/v1/tasks")
        self.assertEqual(status, 200)
        task = next(item for item in tasks["tasks"] if item["id"] == created_task["task"]["id"])
        self.assertIsNone(task["project_id"])
        self.assertEqual(task["version"], 2)

    def test_project_archive_hides_restores_and_keeps_tasks(self):
        _, created = self.client.request("POST", "/api/v1/projects", {"name": "В архив"})
        project = created["project"]
        _, created_task = self.client.request("POST", "/api/v1/tasks", {"title": "Остаётся в проекте", "project_id": project["id"]})
        status, archived = self.client.request("POST", f"/api/v1/projects/{project['id']}/archive", {"expected_version": 1})
        self.assertEqual(status, 200)
        self.assertIsNotNone(archived["project"]["archived_at"])
        self.assertEqual(archived["project"]["version"], 2)
        _, active = self.client.request("GET", "/api/v1/projects")
        self.assertNotIn(project["id"], [item["id"] for item in active["projects"]])
        _, archived_list = self.client.request("GET", "/api/v1/projects?archived=true")
        self.assertEqual([item["id"] for item in archived_list["projects"]], [project["id"]])
        _, sync = self.client.request("GET", "/api/v1/sync?since=1970-01-01T00:00:00.000Z")
        synced_project = next(item for item in sync["projects"] if item["id"] == project["id"])
        self.assertIsNotNone(synced_project["archived_at"])
        _, tasks = self.client.request("GET", "/api/v1/tasks")
        task = next(item for item in tasks["tasks"] if item["id"] == created_task["task"]["id"])
        self.assertEqual(task["project_id"], project["id"])
        status, invalid = self.client.request("POST", "/api/v1/tasks", {"title": "Нельзя назначить", "project_id": project["id"]})
        self.assertEqual((status, invalid["error"]["code"]), (422, "invalid_project"))
        status, restored = self.client.request("DELETE", f"/api/v1/projects/{project['id']}/archive", {"expected_version": 2})
        self.assertEqual(status, 200)
        self.assertIsNone(restored["project"]["archived_at"])
        _, active = self.client.request("GET", "/api/v1/projects")
        self.assertIn(project["id"], [item["id"] for item in active["projects"]])

    def test_json_export_and_atomic_copy_import(self):
        _, project_data = self.client.request("POST", "/api/v1/projects", {"name": "Экспорт", "color": "#123456"})
        project = project_data["project"]
        _, task_data = self.client.request("POST", "/api/v1/tasks", {"title": "Перенести", "description": "Данные", "project_id": project["id"], "status": "todo"})
        task = task_data["task"]
        self.client.request("POST", f"/api/v1/tasks/{task['id']}/checklist", {"title": "Пункт"})
        self.client.request("POST", f"/api/v1/projects/{project['id']}/archive", {"expected_version": 1})
        status, exported = self.client.request("GET", "/api/v1/data/export")
        self.assertEqual(status, 200)
        self.assertEqual((exported["format"], exported["version"]), (server.EXPORT_FORMAT, server.EXPORT_VERSION))
        self.assertNotIn("owner_id", exported["data"]["tasks"][0])

        status, imported = self.client.request("POST", "/api/v1/data/import", exported)
        self.assertEqual(status, 201)
        expected_counts = {key: len(exported["data"][key]) for key in ("projects", "tasks", "checklist_items")}
        self.assertEqual(imported["imported"], expected_counts)
        _, all_projects = self.client.request("GET", "/api/v1/projects?include_archived=true")
        _, all_tasks = self.client.request("GET", "/api/v1/tasks")
        _, all_items = self.client.request("GET", "/api/v1/checklist")
        self.assertEqual((len(all_projects["projects"]), len(all_tasks["tasks"]), len(all_items["checklist_items"])), (len(exported["data"]["projects"]) * 2, 2, 2))
        imported_project = next(item for item in all_projects["projects"] if item["id"] != project["id"] and item["name"] == "Экспорт")
        imported_task = next(item for item in all_tasks["tasks"] if item["id"] != task["id"])
        self.assertIsNotNone(imported_project["archived_at"])
        self.assertEqual(imported_task["project_id"], imported_project["id"])

        broken = json.loads(json.dumps(exported))
        broken["data"]["tasks"][0]["project_id"] = "missing-project"
        status, error = self.client.request("POST", "/api/v1/data/import", broken)
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_import_reference"))
        broken["data"]["tasks"][0]["project_id"] = []
        status, error = self.client.request("POST", "/api/v1/data/import", broken)
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_import_reference"))
        _, unchanged = self.client.request("GET", "/api/v1/tasks")
        self.assertEqual(len(unchanged["tasks"]), 2)

    def test_task_can_move_between_projects_and_days(self):
        _, first = self.client.request("POST", "/api/v1/projects", {"name": "Первый"})
        _, second = self.client.request("POST", "/api/v1/projects", {"name": "Второй"})
        _, created = self.client.request("POST", "/api/v1/tasks", {"title": "Перенос", "project_id": first["project"]["id"], "scheduled_date": "2026-08-01"})

        status, moved = self.client.request("PATCH", f"/api/v1/tasks/{created['task']['id']}", {"project_id": second["project"]["id"], "scheduled_date": "2026-08-02", "expected_version": 1})
        self.assertEqual(status, 200)
        self.assertEqual(moved["task"]["project_id"], second["project"]["id"])
        self.assertEqual(moved["task"]["scheduled_date"], "2026-08-02")
        self.assertEqual(moved["task"]["version"], 2)

        status, cleared = self.client.request("PATCH", f"/api/v1/tasks/{created['task']['id']}", {"project_id": None, "scheduled_date": None, "expected_version": 2})
        self.assertEqual(status, 200)
        self.assertIsNone(cleared["task"]["project_id"])
        self.assertIsNone(cleared["task"]["scheduled_date"])
        self.assertEqual(cleared["task"]["version"], 3)

    def test_due_at_is_independent_from_scheduled_date(self):
        status, created = self.client.request("POST", "/api/v1/tasks", {"title": "Отдельный срок", "scheduled_date": "2026-08-01", "due_at": "2026-08-03T12:30:00+03:00"})
        self.assertEqual(status, 201)
        self.assertEqual(created["task"]["scheduled_date"], "2026-08-01")
        self.assertEqual(created["task"]["due_at"], "2026-08-03T09:30:00.000Z")

        status, rescheduled = self.client.request("PATCH", f"/api/v1/tasks/{created['task']['id']}", {"scheduled_date": "2026-08-02", "expected_version": 1})
        self.assertEqual(status, 200)
        self.assertEqual(rescheduled["task"]["scheduled_date"], "2026-08-02")
        self.assertEqual(rescheduled["task"]["due_at"], "2026-08-03T09:30:00.000Z")

        status, cleared_due = self.client.request("PATCH", f"/api/v1/tasks/{created['task']['id']}", {"due_at": None, "expected_version": 2})
        self.assertEqual(status, 200)
        self.assertEqual(cleared_due["task"]["scheduled_date"], "2026-08-02")
        self.assertIsNone(cleared_due["task"]["due_at"])

    def test_users_cannot_access_each_others_projects(self):
        _, created = self.client.request("POST", "/api/v1/projects", {"name": "Приватный"})
        project = created["project"]
        other = ApiClient()
        registration = self.register_and_verify(other, "other@example.com", "Другой")
        other.token = registration["token"]
        status, _ = other.request("PATCH", f"/api/v1/projects/{project['id']}", {"name": "Чужое"})
        self.assertEqual(status, 404)
        status, data = other.request("POST", "/api/v1/tasks", {"title": "Чужая задача", "project_id": project["id"]})
        self.assertEqual(status, 422)
        self.assertEqual(data["error"]["code"], "invalid_project")

    def test_external_values_are_validated(self):
        status, created = self.client.request("POST", "/api/v1/tasks", {"title": "Без описания", "description": None})
        self.assertEqual(status, 201)
        self.assertEqual(created["task"]["description"], "")
        status, data = self.client.request("POST", "/api/v1/tasks", {"title": "Дата", "scheduled_date": "01.08.2026"})
        self.assertEqual((status, data["error"]["code"]), (422, "invalid_date"))
        status, data = self.client.request("POST", "/api/v1/tasks", {"title": "Срок", "due_at": "2026-08-01T12:00:00"})
        self.assertEqual((status, data["error"]["code"]), (422, "invalid_due_at"))
        status, data = self.client.request("GET", "/api/v1/sync?since=not-a-date")
        self.assertEqual((status, data["error"]["code"]), (422, "invalid_cursor"))
        status, data = self.client.request("POST", "/api/v1/projects", {"name": "Цвет", "color": "purple"})
        self.assertEqual((status, data["error"]["code"]), (422, "invalid_color"))
        status, data = self.client.request("POST", "/api/v1/tasks", [])
        self.assertEqual((status, data["error"]["code"]), (400, "invalid_json_type"))

    def test_task_filters_return_only_matching_rows(self):
        self.client.request("POST", "/api/v1/tasks", {"title": "Сегодня", "status": "todo", "scheduled_date": "2026-08-01"})
        self.client.request("POST", "/api/v1/tasks", {"title": "Входящие", "status": "inbox"})
        status, by_status = self.client.request("GET", "/api/v1/tasks?status=inbox")
        self.assertEqual(status, 200)
        self.assertEqual([task["title"] for task in by_status["tasks"]], ["Входящие"])
        status, by_date = self.client.request("GET", "/api/v1/tasks?scheduled_date=2026-08-01")
        self.assertEqual(status, 200)
        self.assertEqual([task["title"] for task in by_date["tasks"]], ["Сегодня"])

    def test_health_reports_application_and_schema_versions(self):
        status, data = self.client.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], server.APP_VERSION)
        self.assertEqual(data["schema_version"], server.SCHEMA_VERSION)

    def test_registration_requires_email_verification(self):
        client = ApiClient()
        status, registration = client.request("POST", "/api/v1/auth/register", {"email": "pending@example.com", "display_name": "Ожидает", "password": "correct-horse"})
        self.assertEqual(status, 201)
        self.assertTrue(registration["verification_required"])
        status, error = client.request("POST", "/api/v1/auth/login", {"email": "pending@example.com", "password": "correct-horse"})
        self.assertEqual((status, error["error"]["code"]), (403, "email_not_verified"))
        verification_token = self.sent_emails[-1][2]
        status, verified = client.request("POST", "/api/v1/auth/verify-email", {"token": verification_token})
        self.assertEqual(status, 200)
        self.assertIn("token", verified)
        status, reused = client.request("POST", "/api/v1/auth/verify-email", {"token": verification_token})
        self.assertEqual((status, reused["error"]["code"]), (422, "invalid_verification_token"))

    def test_expired_verification_can_be_resent(self):
        client = ApiClient()
        client.request("POST", "/api/v1/auth/register", {"email": "expired@example.com", "display_name": "Истёк", "password": "correct-horse"})
        expired_token = self.sent_emails[-1][2]
        with server.connect() as db:
            db.execute("UPDATE email_verification_tokens SET expires_at='2000-01-01T00:00:00.000Z',created_at='2000-01-01T00:00:00.000Z'")
        status, expired = client.request("POST", "/api/v1/auth/verify-email", {"token": expired_token})
        self.assertEqual((status, expired["error"]["code"]), (422, "verification_token_expired"))
        status, _ = client.request("POST", "/api/v1/auth/resend-verification", {"email": "expired@example.com"})
        self.assertEqual(status, 202)
        new_token = self.sent_emails[-1][2]
        self.assertNotEqual(expired_token, new_token)
        status, _ = client.request("POST", "/api/v1/auth/verify-email", {"token": new_token})
        self.assertEqual(status, 200)

    def test_mail_failure_rolls_back_registration(self):
        def fail_delivery(email, display_name, token):
            raise server.MailDeliveryError("test failure")

        server.MAIL_SENDER = fail_delivery
        status, error = self.client.request("POST", "/api/v1/auth/register", {"email": "retry@example.com", "display_name": "Повтор", "password": "correct-horse"})
        self.assertEqual((status, error["error"]["code"]), (503, "email_delivery_failed"))
        server.MAIL_SENDER = lambda email, display_name, token: self.sent_emails.append((email, display_name, token))
        status, _ = self.client.request("POST", "/api/v1/auth/register", {"email": "retry@example.com", "display_name": "Повтор", "password": "correct-horse"})
        self.assertEqual(status, 201)

    def test_schema_v1_users_are_migrated_as_verified(self):
        legacy_path = Path(self.temp.name) / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as db:
            db.execute("CREATE TABLE users(id TEXT PRIMARY KEY,email TEXT,display_name TEXT,password_hash TEXT,created_at TEXT)")
            db.execute("INSERT INTO users VALUES ('legacy','legacy@example.com','Legacy','hash','2026-01-01T00:00:00.000Z')")
            db.execute("PRAGMA user_version=1")
            db.commit()
        server.DB_PATH = legacy_path
        server.init_db()
        with server.connect() as db:
            verified_at = db.execute("SELECT email_verified_at FROM users WHERE id='legacy'").fetchone()[0]
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(verified_at, "2026-01-01T00:00:00.000Z")
        self.assertEqual(schema_version, server.SCHEMA_VERSION)

    def test_schema_v2_migration_preserves_tasks_and_adds_checklists(self):
        with server.connect() as db:
            owner_id = db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
            timestamp = server.now_iso()
            db.execute("INSERT INTO tasks(id,owner_id,title,description,status,priority,created_at,updated_at,version) VALUES (?,?,?,?,?,?,?,?,?)", ("legacy-task", owner_id, "Сохранить меня", "", "todo", "normal", timestamp, timestamp, 1))
            db.execute("DROP TABLE checklist_items")
            db.execute("PRAGMA user_version=2")
        server.init_db()
        with server.connect() as db:
            task_title = db.execute("SELECT title FROM tasks WHERE id='legacy-task'").fetchone()[0]
            checklist_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checklist_items'").fetchone()
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(task_title, "Сохранить меня")
        self.assertIsNotNone(checklist_exists)
        self.assertEqual(schema_version, server.SCHEMA_VERSION)

    def test_schema_v3_migration_preserves_projects_and_adds_archive(self):
        legacy_path = Path(self.temp.name) / "legacy-v3.db"
        with closing(sqlite3.connect(legacy_path)) as db:
            db.execute("CREATE TABLE users(id TEXT PRIMARY KEY,email TEXT,display_name TEXT,password_hash TEXT,created_at TEXT,email_verified_at TEXT)")
            db.execute("CREATE TABLE projects(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),name TEXT NOT NULL,color TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER NOT NULL,deleted_at TEXT)")
            db.execute("INSERT INTO users VALUES ('owner','owner@example.com','Owner','hash','2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z')")
            db.execute("INSERT INTO projects VALUES ('project','owner','Старый проект','#112233','2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z',1,NULL)")
            db.execute("PRAGMA user_version=3")
            db.commit()
        server.DB_PATH = legacy_path
        server.init_db()
        with server.connect() as db:
            project = db.execute("SELECT name,archived_at FROM projects WHERE id='project'").fetchone()
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual((project["name"], project["archived_at"]), ("Старый проект", None))
        self.assertEqual(schema_version, server.SCHEMA_VERSION)

    def test_smtp_sender_uses_starttls_and_keeps_token_out_of_request_url(self):
        previous = (
            server.SMTP_HOST,
            server.SMTP_PORT,
            server.SMTP_MODE,
            server.SMTP_USERNAME,
            server.SMTP_PASSWORD,
            server.SMTP_FROM,
            server.PUBLIC_URL,
        )
        smtp_context = MagicMock()
        smtp_client = MagicMock()
        smtp_context.__enter__.return_value = smtp_client
        try:
            server.SMTP_HOST = "smtp.example.com"
            server.SMTP_PORT = 587
            server.SMTP_MODE = "starttls"
            server.SMTP_USERNAME = "mailer@example.com"
            server.SMTP_PASSWORD = "test-only-password"
            server.SMTP_FROM = "mailer@example.com"
            server.PUBLIC_URL = "https://tasks.example.com"
            with patch.object(server.smtplib, "SMTP", return_value=smtp_context) as smtp_factory:
                server.send_verification_email("user@example.com", "Тест", "secret-token-value")
            smtp_factory.assert_called_once_with("smtp.example.com", 587, timeout=10)
            smtp_client.starttls.assert_called_once_with()
            smtp_client.login.assert_called_once_with("mailer@example.com", "test-only-password")
            message = smtp_client.send_message.call_args.args[0]
            self.assertIn("https://tasks.example.com/#verify=secret-token-value", message.get_content())
            self.assertNotIn("?verify=", message.get_content())
            self.assertTrue(server.smtp_ready())
        finally:
            (
                server.SMTP_HOST,
                server.SMTP_PORT,
                server.SMTP_MODE,
                server.SMTP_USERNAME,
                server.SMTP_PASSWORD,
                server.SMTP_FROM,
                server.PUBLIC_URL,
            ) = previous

    def test_consistent_backup_is_created_and_not_overwritten(self):
        destination = Path(self.temp.name) / "backup.db"
        create_backup(server.DB_PATH, destination)
        with closing(sqlite3.connect(destination)) as backup_db:
            self.assertEqual(backup_db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup_db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        with self.assertRaises(FileExistsError):
            create_backup(server.DB_PATH, destination)


if __name__ == "__main__":
    unittest.main()
