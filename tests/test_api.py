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
        status, data = self.client.request("POST", "/api/v1/tasks", {"title": "Дата", "scheduled_date": "01.08.2026"})
        self.assertEqual((status, data["error"]["code"]), (422, "invalid_date"))
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
        self.assertEqual(schema_version, 2)

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
