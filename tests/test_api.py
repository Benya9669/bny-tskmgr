import io
import gzip
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

    def test_pwa_manifest_and_icons_are_served_with_expected_types(self):
        status, headers, body = self.client.app.dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": "/manifest.webmanifest"})
        self.assertEqual(status, 200)
        self.assertIn(("Content-Type", "application/manifest+json; charset=utf-8"), headers)
        manifest = json.loads(body)
        self.assertEqual((manifest["display"], manifest["start_url"], manifest["scope"]), ("standalone", "/", "/"))
        self.assertEqual({icon["purpose"] for icon in manifest["icons"]}, {"any", "maskable"})
        for icon in manifest["icons"]:
            status, headers, _ = self.client.app.dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": icon["src"]})
            self.assertEqual(status, 200)
            self.assertIn(("Content-Type", "image/svg+xml"), headers)
        status, headers, worker = self.client.app.dispatch({"REQUEST_METHOD": "GET", "PATH_INFO": "/sw.js"})
        self.assertEqual(status, 200)
        self.assertIn(("Content-Type", "application/javascript; charset=utf-8"), headers)
        self.assertIn(b'url.pathname.startsWith("/api/")', worker)
        self.assertNotIn(b"skipWaiting", worker)

    def test_password_reset_and_account_updates_are_secure(self):
        sent = []
        original_reset_sender = server.send_password_reset_email
        original_change_sender = server.send_email_change_confirmation
        server.send_password_reset_email = lambda email, name, token: sent.append(("reset", email, token))
        server.send_email_change_confirmation = lambda email, name, token: sent.append(("email", email, token))
        try:
            anonymous = ApiClient()
            status, response = anonymous.request("POST", "/api/v1/auth/request-password-reset", {"email": "missing@example.com"})
            self.assertEqual((status, response["sent"]), (202, True))
            status, _ = anonymous.request("POST", "/api/v1/auth/request-password-reset", {"email": "user@example.com"})
            self.assertEqual(status, 202)
            reset_token = sent[-1][2]
            status, _ = anonymous.request("POST", "/api/v1/auth/reset-password", {"token": reset_token, "new_password": "new-correct-horse"})
            self.assertEqual(status, 200)
            status, login = anonymous.request("POST", "/api/v1/auth/login", {"email": "user@example.com", "password": "new-correct-horse"})
            self.assertEqual(status, 200)
            self.assertEqual(login["user"]["timezone"], "UTC")
            self.client.token = login["token"]
            status, denied = self.client.request("PATCH", "/api/v1/account", {"new_password": "another-password"})
            self.assertEqual((status, denied["error"]["code"]), (403, "invalid_current_password"))
            status, updated = self.client.request("PATCH", "/api/v1/account", {"display_name": "Новое имя", "email": "new@example.com", "current_password": "new-correct-horse"})
            self.assertEqual((status, updated["user"]["display_name"], updated["email_change_pending"]), (200, "Новое имя", "new@example.com"))
            email_token = sent[-1][2]
            status, confirmed = anonymous.request("POST", "/api/v1/auth/confirm-email-change", {"token": email_token})
            self.assertEqual((status, confirmed["user"]["email"]), (200, "new@example.com"))
        finally:
            server.send_password_reset_email = original_reset_sender
            server.send_email_change_confirmation = original_change_sender

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

    def test_task_discussion_lifecycle_privacy_sync_and_parent_delete(self):
        _, created = self.client.request("POST", "/api/v1/tasks", {"title": "Обсуждаемая задача"})
        task_id = created["task"]["id"]
        status, created_message = self.client.request("POST", f"/api/v1/tasks/{task_id}/messages", {"body": "Первое решение"})
        self.assertEqual(status, 201)
        message = created_message["message"]
        self.assertEqual((message["kind"], message["version"], message["author_name"]), ("comment", 1, "Тест"))

        status, listed = self.client.request("GET", f"/api/v1/messages?task_id={task_id}")
        self.assertEqual((status, [item["body"] for item in listed["messages"]]), (200, ["Первое решение"]))
        status, updated = self.client.request("PATCH", f"/api/v1/messages/{message['id']}", {"body": "Уточнённое решение", "expected_version": 1})
        self.assertEqual((status, updated["message"]["version"], updated["message"]["body"]), (200, 2, "Уточнённое решение"))
        self.assertIsNotNone(updated["message"]["edited_at"])
        status, stale = self.client.request("PATCH", f"/api/v1/messages/{message['id']}", {"body": "Старая версия", "expected_version": 1})
        self.assertEqual((status, stale["error"]["code"]), (409, "version_conflict"))

        other = ApiClient()
        registration = self.register_and_verify(other, "discussion-other@example.com", "Другой")
        other.token = registration["token"]
        status, _ = other.request("GET", f"/api/v1/messages?task_id={task_id}")
        self.assertEqual(status, 404)
        status, _ = other.request("PATCH", f"/api/v1/messages/{message['id']}", {"body": "Чужое", "expected_version": 2})
        self.assertEqual(status, 404)

        status, _ = self.client.request("DELETE", f"/api/v1/tasks/{task_id}")
        self.assertEqual(status, 200)
        _, sync = self.client.request("GET", "/api/v1/sync?since=1970-01-01T00:00:00.000Z")
        synced = next(item for item in sync["task_messages"] if item["id"] == message["id"])
        self.assertIsNotNone(synced["deleted_at"])

    def test_discussion_message_can_be_soft_deleted(self):
        _, created = self.client.request("POST", "/api/v1/tasks", {"title": "Удаление сообщения"})
        task_id = created["task"]["id"]
        _, created_message = self.client.request("POST", f"/api/v1/tasks/{task_id}/messages", {"body": "Временное"})
        message_id = created_message["message"]["id"]
        status, _ = self.client.request("DELETE", f"/api/v1/messages/{message_id}")
        self.assertEqual(status, 200)
        _, listed = self.client.request("GET", f"/api/v1/messages?task_id={task_id}")
        self.assertEqual(listed["messages"], [])
        _, sync = self.client.request("GET", "/api/v1/sync?since=1970-01-01T00:00:00.000Z")
        synced = next(item for item in sync["task_messages"] if item["id"] == message_id)
        self.assertIsNotNone(synced["deleted_at"])

    def test_markdown_notes_folders_favorites_privacy_and_sync(self):
        status, created_folder = self.client.request("POST", "/api/v1/note-folders", {"name": "Работа"})
        self.assertEqual(status, 201)
        folder = created_folder["folder"]
        status, created_note = self.client.request("POST", "/api/v1/notes", {"title": "План", "content": "# Релиз\n\n- [ ] Проверить", "folder_id": folder["id"], "is_favorite": True})
        self.assertEqual(status, 201)
        note = created_note["note"]
        self.assertEqual((note["is_favorite"], note["version"]), (True, 1))

        status, listed = self.client.request("GET", f"/api/v1/notes?folder_id={folder['id']}&favorite=true")
        self.assertEqual((status, [item["title"] for item in listed["notes"]]), (200, ["План"]))
        status, updated = self.client.request("PATCH", f"/api/v1/notes/{note['id']}", {"content": "## Готово", "is_favorite": False, "expected_version": 1})
        self.assertEqual((status, updated["note"]["content"], updated["note"]["version"]), (200, "## Готово", 2))
        self.assertFalse(updated["note"]["is_favorite"])
        status, stale = self.client.request("PATCH", f"/api/v1/notes/{note['id']}", {"title": "Старая", "expected_version": 1})
        self.assertEqual((status, stale["error"]["code"]), (409, "version_conflict"))
        status, invalid = self.client.request("POST", "/api/v1/notes", {"title": "Чужая папка", "folder_id": "missing"})
        self.assertEqual((status, invalid["error"]["code"]), (422, "invalid_folder"))

        other = ApiClient()
        registration = self.register_and_verify(other, "notes-other@example.com", "Другой")
        other.token = registration["token"]
        status, _ = other.request("PATCH", f"/api/v1/notes/{note['id']}", {"title": "Чужое", "expected_version": 2})
        self.assertEqual(status, 404)
        status, _ = other.request("DELETE", f"/api/v1/note-folders/{folder['id']}")
        self.assertEqual(status, 404)

        status, _ = self.client.request("DELETE", f"/api/v1/note-folders/{folder['id']}")
        self.assertEqual(status, 200)
        _, notes = self.client.request("GET", "/api/v1/notes?folder_id=none")
        detached = next(item for item in notes["notes"] if item["id"] == note["id"])
        self.assertIsNone(detached["folder_id"])
        status, _ = self.client.request("DELETE", f"/api/v1/notes/{note['id']}")
        self.assertEqual(status, 200)
        _, sync = self.client.request("GET", "/api/v1/sync?since=1970-01-01T00:00:00.000Z")
        synced_folder = next(item for item in sync["note_folders"] if item["id"] == folder["id"])
        synced_note = next(item for item in sync["notes"] if item["id"] == note["id"])
        self.assertIsNotNone(synced_folder["deleted_at"])
        self.assertIsNotNone(synced_note["deleted_at"])

    def test_note_links_are_private_searchable_and_synced_as_tombstones(self):
        _, project_data = self.client.request("POST", "/api/v1/projects", {"name": "Документация"})
        _, task_data = self.client.request("POST", "/api/v1/tasks", {"title": "Подготовить релиз", "project_id": project_data["project"]["id"]})
        _, note_data = self.client.request("POST", "/api/v1/notes", {"title": "План релиза", "content": "Проверить миграцию и кириллический Поиск"})
        project, task, note = project_data["project"], task_data["task"], note_data["note"]

        status, task_link_data = self.client.request("POST", "/api/v1/note-links", {"note_id": note["id"], "task_id": task["id"]})
        self.assertEqual(status, 201)
        task_link = task_link_data["note_link"]
        status, project_link_data = self.client.request("POST", "/api/v1/note-links", {"note_id": note["id"], "project_id": project["id"]})
        self.assertEqual(status, 201)
        project_link = project_link_data["note_link"]
        status, duplicate = self.client.request("POST", "/api/v1/note-links", {"note_id": note["id"], "task_id": task["id"]})
        self.assertEqual((status, duplicate["error"]["code"]), (409, "conflict"))
        status, invalid = self.client.request("POST", "/api/v1/note-links", {"note_id": note["id"], "task_id": task["id"], "project_id": project["id"]})
        self.assertEqual((status, invalid["error"]["code"]), (422, "invalid_note_link"))

        status, searched = self.client.request("GET", f"/api/v1/notes?q=КИРИЛЛИЧЕСКИЙ&task_id={task['id']}")
        self.assertEqual((status, [item["id"] for item in searched["notes"]]), (200, [note["id"]]))
        _, no_match = self.client.request("GET", "/api/v1/notes?q=несуществующее")
        self.assertEqual(no_match["notes"], [])
        self.client.request("PATCH", f"/api/v1/notes/{note['id']}", {"content": "Обновлённое содержимое", "expected_version": 1})
        _, old_search = self.client.request("GET", "/api/v1/notes?q=миграцию")
        _, new_search = self.client.request("GET", "/api/v1/notes?q=обновлённое")
        self.assertEqual(old_search["notes"], [])
        self.assertEqual([item["id"] for item in new_search["notes"]], [note["id"]])

        other = ApiClient()
        registration = self.register_and_verify(other, "links-other@example.com", "Другой")
        other.token = registration["token"]
        status, _ = other.request("POST", "/api/v1/note-links", {"note_id": note["id"], "task_id": task["id"]})
        self.assertEqual(status, 404)
        _, other_search = other.request("GET", "/api/v1/notes?q=обновлённое")
        self.assertEqual(other_search["notes"], [])

        self.client.request("DELETE", f"/api/v1/note-links/{task_link['id']}")
        self.client.request("DELETE", f"/api/v1/projects/{project['id']}")
        _, sync = self.client.request("GET", "/api/v1/sync?since=1970-01-01T00:00:00.000Z")
        synced_links = {item["id"]: item for item in sync["note_links"]}
        self.assertIsNotNone(synced_links[task_link["id"]]["deleted_at"])
        self.assertIsNotNone(synced_links[project_link["id"]]["deleted_at"])

    def test_note_links_survive_export_import_with_remapped_references(self):
        _, project_data = self.client.request("POST", "/api/v1/projects", {"name": "Связанный проект"})
        _, task_data = self.client.request("POST", "/api/v1/tasks", {"title": "Связанная задача", "project_id": project_data["project"]["id"]})
        _, note_data = self.client.request("POST", "/api/v1/notes", {"title": "Связанная заметка", "content": "[[Другая заметка]]"})
        self.client.request("POST", "/api/v1/note-links", {"note_id": note_data["note"]["id"], "task_id": task_data["task"]["id"]})
        self.client.request("POST", "/api/v1/note-links", {"note_id": note_data["note"]["id"], "project_id": project_data["project"]["id"]})
        _, exported = self.client.request("GET", "/api/v1/data/export")
        self.assertEqual((exported["version"], len(exported["data"]["note_links"])), (9, 2))

        imported = ApiClient()
        registration = self.register_and_verify(imported, "links-import@example.com", "Импорт")
        imported.token = registration["token"]
        status, report = imported.request("POST", "/api/v1/data/import", exported)
        self.assertEqual((status, report["imported"]["note_links"]), (201, 2))
        _, imported_notes = imported.request("GET", "/api/v1/notes")
        _, imported_links = imported.request("GET", "/api/v1/note-links")
        _, imported_tasks = imported.request("GET", "/api/v1/tasks")
        self.assertEqual({link["note_id"] for link in imported_links["note_links"]}, {imported_notes["notes"][0]["id"]})
        self.assertEqual({link["task_id"] for link in imported_links["note_links"] if link["task_id"]}, {imported_tasks["tasks"][0]["id"]})
        self.assertEqual({link["project_id"] for link in imported_links["note_links"] if link["project_id"]}, {imported_tasks["tasks"][0]["project_id"]})
        self.assertNotEqual(imported_tasks["tasks"][0]["project_id"], project_data["project"]["id"])

    def test_optimistic_lock_rejects_stale_update(self):
        _, created = self.client.request("POST", "/api/v1/tasks", {"title": "Конфликт"})
        task_id = created["task"]["id"]
        self.client.request("PATCH", f"/api/v1/tasks/{task_id}", {"priority": "high", "expected_version": 1})
        status, data = self.client.request("PATCH", f"/api/v1/tasks/{task_id}", {"priority": "low", "expected_version": 1})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "version_conflict")

    def test_task_manual_order_is_atomic_and_syncable(self):
        _, columns = self.client.request("GET", "/api/v1/kanban/columns")
        source, destination = columns["columns"][:2]
        _, first = self.client.request("POST", "/api/v1/tasks", {"title": "Первая", "column_id": source["id"]})
        _, second = self.client.request("POST", "/api/v1/tasks", {"title": "Вторая", "column_id": source["id"]})
        self.assertLess(first["task"]["kanban_position"], second["task"]["kanban_position"])

        status, moved = self.client.request("POST", f"/api/v1/tasks/{second['task']['id']}/move", {"column_id": source["id"], "before_task_id": first["task"]["id"], "expected_version": 1})
        self.assertEqual(status, 200)
        self.assertLess(moved["task"]["kanban_position"], first["task"]["kanban_position"])
        status, stale = self.client.request("POST", f"/api/v1/tasks/{second['task']['id']}/move", {"column_id": destination["id"], "before_task_id": None, "expected_version": 1})
        self.assertEqual((status, stale["error"]["code"]), (409, "version_conflict"))
        _, tasks = self.client.request("GET", "/api/v1/tasks")
        self.assertEqual(next(task for task in tasks["tasks"] if task["id"] == second["task"]["id"])["column_id"], source["id"])
        _, sync = self.client.request("GET", "/api/v1/sync?since=1970-01-01T00:00:00.000Z")
        self.assertIn("kanban_position", next(task for task in sync["tasks"] if task["id"] == second["task"]["id"]))

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
        self.client.request("POST", f"/api/v1/tasks/{task['id']}/messages", {"body": "История решения"})
        _, folder_data = self.client.request("POST", "/api/v1/note-folders", {"name": "Экспорт заметок"})
        self.client.request("POST", "/api/v1/notes", {"title": "Переносимая заметка", "content": "**Markdown**", "folder_id": folder_data["folder"]["id"], "is_favorite": True})
        self.client.request("POST", f"/api/v1/projects/{project['id']}/archive", {"expected_version": 1})
        status, exported = self.client.request("GET", "/api/v1/data/export")
        self.assertEqual(status, 200)
        self.assertEqual((exported["format"], exported["version"]), (server.EXPORT_FORMAT, server.EXPORT_VERSION))
        self.assertNotIn("owner_id", exported["data"]["tasks"][0])

        status, imported = self.client.request("POST", "/api/v1/data/import", exported)
        self.assertEqual(status, 201)
        expected_counts = {key: len(exported["data"][key]) for key in ("projects", "kanban_columns", "tasks", "checklist_items", "task_messages", "note_folders", "notes", "note_links")}
        self.assertEqual(imported["imported"], expected_counts)
        _, all_projects = self.client.request("GET", "/api/v1/projects?include_archived=true")
        _, all_tasks = self.client.request("GET", "/api/v1/tasks")
        _, all_items = self.client.request("GET", "/api/v1/checklist")
        self.assertEqual((len(all_projects["projects"]), len(all_tasks["tasks"]), len(all_items["checklist_items"])), (len(exported["data"]["projects"]) * 2, 2, 2))
        imported_project = next(item for item in all_projects["projects"] if item["id"] != project["id"] and item["name"] == "Экспорт")
        imported_task = next(item for item in all_tasks["tasks"] if item["id"] != task["id"])
        self.assertIsNotNone(imported_project["archived_at"])
        self.assertEqual(imported_task["project_id"], imported_project["id"])
        _, imported_messages = self.client.request("GET", f"/api/v1/messages?task_id={imported_task['id']}")
        self.assertEqual([message["body"] for message in imported_messages["messages"]], ["История решения"])
        _, all_folders = self.client.request("GET", "/api/v1/note-folders")
        imported_folder = next(item for item in all_folders["folders"] if item["id"] != folder_data["folder"]["id"] and item["name"] == "Экспорт заметок")
        _, all_notes = self.client.request("GET", f"/api/v1/notes?folder_id={imported_folder['id']}")
        self.assertEqual((all_notes["notes"][0]["title"], all_notes["notes"][0]["content"], all_notes["notes"][0]["is_favorite"]), ("Переносимая заметка", "**Markdown**", True))

        broken = json.loads(json.dumps(exported))
        broken["data"]["tasks"][0]["project_id"] = "missing-project"
        status, error = self.client.request("POST", "/api/v1/data/import", broken)
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_import_reference"))
        broken["data"]["tasks"][0]["project_id"] = []
        status, error = self.client.request("POST", "/api/v1/data/import", broken)
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_import_reference"))
        _, unchanged = self.client.request("GET", "/api/v1/tasks")
        self.assertEqual(len(unchanged["tasks"]), 2)

    def test_v1_taskflow_export_remains_importable(self):
        legacy_export = {
            "format": "taskflow-export",
            "version": 1,
            "data": {
                "projects": [],
                "tasks": [{"id": "legacy-task", "title": "Из старого экспорта", "status": "todo", "priority": "normal"}],
                "checklist_items": [],
            },
        }
        status, imported = self.client.request("POST", "/api/v1/data/import", legacy_export)
        self.assertEqual(status, 201)
        self.assertEqual(imported["imported"], {"projects": 0, "kanban_columns": 0, "tasks": 1, "checklist_items": 0, "task_messages": 0, "note_folders": 0, "notes": 0, "note_links": 0})
        _, tasks = self.client.request("GET", "/api/v1/tasks")
        task = next(item for item in tasks["tasks"] if item["title"] == "Из старого экспорта")
        _, columns = self.client.request("GET", "/api/v1/kanban/columns")
        column = next(item for item in columns["columns"] if item["id"] == task["column_id"])
        self.assertEqual((task["status"], column["semantic_status"]), ("todo", "todo"))

    def test_v3_taskflow_export_without_notes_remains_importable(self):
        legacy_export = {
            "format": "taskflow-export",
            "version": 3,
            "data": {"projects": [], "kanban_columns": [], "tasks": [], "checklist_items": [], "task_messages": []},
        }
        status, imported = self.client.request("POST", "/api/v1/data/import", legacy_export)
        self.assertEqual(status, 201)
        self.assertEqual(imported["imported"], {"projects": 0, "kanban_columns": 0, "tasks": 0, "checklist_items": 0, "task_messages": 0, "note_folders": 0, "notes": 0, "note_links": 0})

    def test_custom_kanban_columns_can_be_managed_without_losing_tasks(self):
        status, listed = self.client.request("GET", "/api/v1/kanban/columns")
        self.assertEqual((status, len(listed["columns"])), (200, 4))
        destination = listed["columns"][0]
        status, protected = self.client.request("DELETE", f"/api/v1/kanban/columns/{destination['id']}", {"move_to_column_id": listed["columns"][1]["id"], "expected_version": destination["version"]})
        self.assertEqual((status, protected["error"]["code"]), (422, "last_semantic_column"))

        status, created = self.client.request("POST", "/api/v1/kanban/columns", {"name": "На проверке", "color": "#123456", "semantic_status": "in_progress"})
        self.assertEqual(status, 201)
        column = created["column"]
        status, task_data = self.client.request("POST", "/api/v1/tasks", {"title": "Проверить", "column_id": column["id"]})
        self.assertEqual((status, task_data["task"]["status"], task_data["task"]["column_id"]), (201, "in_progress", column["id"]))

        status, changed = self.client.request("PATCH", f"/api/v1/kanban/columns/{column['id']}", {"name": "Проверено", "semantic_status": "done", "expected_version": 1})
        self.assertEqual((status, changed["column"]["version"]), (200, 2))
        _, tasks = self.client.request("GET", "/api/v1/tasks")
        changed_task = next(item for item in tasks["tasks"] if item["id"] == task_data["task"]["id"])
        self.assertEqual((changed_task["status"], changed_task["version"]), ("done", 2))

        reordered_ids = [column["id"], *[item["id"] for item in listed["columns"]]]
        status, reordered = self.client.request("POST", "/api/v1/kanban/columns/reorder", {"column_ids": reordered_ids})
        self.assertEqual((status, reordered["columns"][0]["id"]), (200, column["id"]))

        current_column = reordered["columns"][0]
        status, deleted = self.client.request("DELETE", f"/api/v1/kanban/columns/{column['id']}", {"move_to_column_id": destination["id"], "expected_version": current_column["version"]})
        self.assertEqual((status, deleted["moved_to_column_id"]), (200, destination["id"]))
        _, tasks = self.client.request("GET", "/api/v1/tasks")
        moved_task = next(item for item in tasks["tasks"] if item["id"] == task_data["task"]["id"])
        self.assertEqual((moved_task["column_id"], moved_task["status"]), (destination["id"], destination["semantic_status"]))

    def test_yougile_import_preserves_columns_tasks_and_subtasks_atomically(self):
        export = {
            "title": "Разработка",
            "stickers": [],
            "boards": [{
                "title": "Релиз",
                "stickers": {},
                "columns": [
                    {"title": "Ожидающие", "color": "", "tasks": ["root-todo"]},
                    {"title": "В процессе", "color": "#123456", "tasks": ["root-progress"]},
                    {"title": "Готово", "color": "", "tasks": ["root-done"]},
                ],
            }],
            "tasks": {
                "root-todo": {"title": "Запланировать", "description": "Описание", "subtasks": ["child-one"], "chat": {"messages": {"comment": {"id": "comment", "timestamp": 1722510000000, "dataType": "ChatMessage", "text": "Обсудить сроки", "properties": {}}}}},
                "child-one": {"title": "Первый шаг", "description": "", "subtasks": ["child-nested"], "chat": {"messages": {"move": {"id": "move", "timestamp": 1722511000000, "dataType": "ChatMessage", "text": "", "properties": {"fromSystem": True, "move": True, "from": "uuid-one", "to": "uuid-two"}}}}},
                "child-nested": {"title": "Вложенный шаг", "description": "", "subtasks": []},
                "root-progress": {"title": "Реализовать", "description": "", "subtasks": []},
                "root-done": {"title": "Выпущено", "description": "", "subtasks": []},
            },
        }
        status, imported = self.client.request("POST", "/api/v1/data/import/yougile", export)
        self.assertEqual(status, 201)
        self.assertEqual(imported["imported"], {"projects": 1, "kanban_columns": 3, "tasks": 3, "checklist_items": 2, "task_messages": 2, "note_folders": 0, "notes": 0, "note_links": 0})
        self.assertEqual(imported["skipped"], {"chat_messages": 0, "stickers": 0, "subtask_descriptions": 0})

        _, projects = self.client.request("GET", "/api/v1/projects")
        project = next(item for item in projects["projects"] if item["name"] == "Разработка")
        _, columns = self.client.request("GET", "/api/v1/kanban/columns")
        imported_columns = {item["name"]: item for item in columns["columns"] if item["name"] in {"Ожидающие", "В процессе", "Готово"}}
        self.assertEqual(imported_columns["В процессе"]["color"], "#123456")
        _, tasks = self.client.request("GET", "/api/v1/tasks")
        imported_tasks = {item["title"]: item for item in tasks["tasks"] if item["project_id"] == project["id"]}
        self.assertEqual(imported_tasks["Запланировать"]["status"], "todo")
        self.assertEqual(imported_tasks["Реализовать"]["column_id"], imported_columns["В процессе"]["id"])
        self.assertEqual(imported_tasks["Выпущено"]["status"], "done")
        self.assertIn("YouGile · доска «Релиз» · колонка «Ожидающие»", imported_tasks["Запланировать"]["description"])
        _, checklist = self.client.request("GET", f"/api/v1/checklist?task_id={imported_tasks['Запланировать']['id']}")
        self.assertEqual([item["title"] for item in checklist["checklist_items"]], ["Первый шаг", "↳ Вложенный шаг"])
        _, messages = self.client.request("GET", f"/api/v1/messages?task_id={imported_tasks['Запланировать']['id']}")
        self.assertEqual([message["kind"] for message in messages["messages"]], ["comment", "system"])
        self.assertEqual(messages["messages"][0]["body"], "Обсудить сроки")
        self.assertIn("Подзадача «Первый шаг»", messages["messages"][1]["body"])
        self.assertNotIn("uuid-one", messages["messages"][1]["body"])

        broken = json.loads(json.dumps(export))
        broken["tasks"]["root-todo"]["subtasks"] = ["missing"]
        status, error = self.client.request("POST", "/api/v1/data/import/yougile", broken)
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_yougile_reference"))
        _, unchanged = self.client.request("GET", "/api/v1/projects")
        self.assertEqual(len([item for item in unchanged["projects"] if item["name"] == "Разработка"]), 1)

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

    def test_completing_recurring_task_creates_single_next_instance(self):
        completed_on = server.now_iso()[:10]
        for recurrence, expected_date in (("daily", server.next_recurrence_date("daily", completed_on)), ("weekly", server.next_recurrence_date("weekly", completed_on)), ("monthly", server.next_recurrence_date("monthly", completed_on))):
            status, created = self.client.request("POST", "/api/v1/tasks", {"title": recurrence, "scheduled_date": "2026-08-01", "due_at": "2026-08-01T12:00:00Z", "recurrence": recurrence, "priority": "high", "estimated_minutes": 30})
            self.assertEqual(status, 201)
            status, completed = self.client.request("PATCH", f"/api/v1/tasks/{created['task']['id']}", {"status": "done", "expected_version": 1})
            self.assertEqual(status, 200)
            next_task = completed["next_task"]
            self.assertEqual((next_task["title"], next_task["scheduled_date"], next_task["recurrence"], next_task["due_at"], next_task["status"]), (recurrence, expected_date, recurrence, None, "todo"))
            status, repeated = self.client.request("PATCH", f"/api/v1/tasks/{created['task']['id']}", {"priority": "low", "expected_version": 2})
            self.assertEqual((status, repeated["next_task"]), (200, None))
        _, tasks = self.client.request("GET", "/api/v1/tasks")
        self.assertEqual(len([task for task in tasks["tasks"] if task["title"] in {"daily", "weekly", "monthly"}]), 6)

    def test_recurrence_is_validated_and_survives_export_import(self):
        status, invalid = self.client.request("POST", "/api/v1/tasks", {"title": "Неверное", "recurrence": "yearly"})
        self.assertEqual((status, invalid["error"]["code"]), (422, "invalid_recurrence"))
        _, created = self.client.request("POST", "/api/v1/tasks", {"title": "Каждый день", "recurrence": "daily"})
        _, exported = self.client.request("GET", "/api/v1/data/export")
        self.assertEqual(exported["version"], 9)
        imported = ApiClient()
        registration = self.register_and_verify(imported, "recurrence-import@example.com", "Импорт")
        imported.token = registration["token"]
        self.assertEqual(imported.request("POST", "/api/v1/data/import", exported)[0], 201)
        _, tasks = imported.request("GET", "/api/v1/tasks")
        self.assertEqual(next(task for task in tasks["tasks"] if task["title"] == created["task"]["title"])["recurrence"], "daily")

    def test_reminders_require_due_at_and_survive_export_import(self):
        status, response = self.client.request("POST", "/api/v1/tasks", {"title": "Без срока", "reminder_offsets": [15]})
        self.assertEqual((status, response["error"]["code"]), (422, "reminder_requires_due_at"))
        status, created = self.client.request("POST", "/api/v1/tasks", {"title": "Напомнить", "due_at": "2026-12-01T12:00:00Z", "reminder_offsets": [60, 0, 60]})
        self.assertEqual((status, created["task"]["reminder_offsets"]), (201, [0, 60]))
        _, exported = self.client.request("GET", "/api/v1/data/export")
        imported = ApiClient()
        registration = self.register_and_verify(imported, "reminder-import@example.com", "Импорт")
        imported.token = registration["token"]
        self.assertEqual(imported.request("POST", "/api/v1/data/import", exported)[0], 201)
        _, tasks = imported.request("GET", "/api/v1/tasks")
        self.assertEqual(next(task for task in tasks["tasks"] if task["title"] == "Напомнить")["reminder_offsets"], [0, 60])

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
        self.client.request("POST", "/api/v1/tasks", {"title": "Конец недели", "scheduled_date": "2026-08-07"})
        status, week = self.client.request("GET", "/api/v1/tasks?scheduled_from=2026-08-01&scheduled_to=2026-08-07")
        self.assertEqual((status, {task["title"] for task in week["tasks"]}), (200, {"Сегодня", "Конец недели"}))
        status, invalid = self.client.request("GET", "/api/v1/tasks?scheduled_from=2026-08-08&scheduled_to=2026-08-01")
        self.assertEqual((status, invalid["error"]["code"]), (422, "invalid_date_range"))

    def test_tags_are_normalized_and_survive_export_import(self):
        status, created = self.client.request("POST", "/api/v1/tasks", {"title": "Теги", "tags": ["Работа", "  срочно ", "Работа", ""]})
        self.assertEqual((status, created["task"]["tags"]), (201, ["Работа", "срочно"]))
        status, invalid = self.client.request("POST", "/api/v1/tasks", {"title": "Неверно", "tags": "работа"})
        self.assertEqual((status, invalid["error"]["code"]), (422, "invalid_tags"))
        _, exported = self.client.request("GET", "/api/v1/data/export")
        self.assertEqual(exported["version"], 9)
        imported = ApiClient()
        registration = self.register_and_verify(imported, "tags-import@example.com", "Импорт")
        imported.token = registration["token"]
        self.assertEqual(imported.request("POST", "/api/v1/data/import", exported)[0], 201)
        _, tasks = imported.request("GET", "/api/v1/tasks")
        self.assertEqual(next(task for task in tasks["tasks"] if task["title"] == "Теги")["tags"], ["Работа", "срочно"])

    def test_task_history_records_create_update_and_move(self):
        _, created = self.client.request("POST", "/api/v1/tasks", {"title": "История"})
        task = created["task"]
        self.client.request("PATCH", f'/api/v1/tasks/{task["id"]}', {"title": "История обновлена", "expected_version": task["version"]})
        _, history = self.client.request("GET", f'/api/v1/tasks/{task["id"]}/history')
        self.assertEqual([entry["event_type"] for entry in history["history"]], ["created", "updated"])
        self.assertEqual(history["history"][1]["changes"]["title"], "История обновлена")

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

    def test_schema_v4_migration_assigns_existing_tasks_to_default_columns(self):
        legacy_path = Path(self.temp.name) / "legacy-v4.db"
        with closing(sqlite3.connect(legacy_path)) as db:
            db.execute("CREATE TABLE users(id TEXT PRIMARY KEY,email TEXT,display_name TEXT,password_hash TEXT,created_at TEXT,email_verified_at TEXT)")
            db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),project_id TEXT,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'inbox',priority TEXT NOT NULL DEFAULT 'normal',scheduled_date TEXT,due_at TEXT,estimated_minutes INTEGER,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,deleted_at TEXT)")
            db.execute("INSERT INTO users VALUES ('owner','owner@example.com','Owner','hash','2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z')")
            db.execute("INSERT INTO tasks VALUES ('legacy-task','owner',NULL,'Сохранить задачу','','todo','normal',NULL,NULL,NULL,'2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z',1,NULL)")
            db.execute("PRAGMA user_version=4")
            db.commit()
        server.DB_PATH = legacy_path
        server.init_db()
        with server.connect() as db:
            task = db.execute("SELECT title,status,column_id FROM tasks WHERE id='legacy-task'").fetchone()
            column = db.execute("SELECT semantic_status FROM kanban_columns WHERE id=?", (task["column_id"],)).fetchone()
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual((task["title"], task["status"], column["semantic_status"]), ("Сохранить задачу", "todo", "todo"))
        self.assertEqual(schema_version, server.SCHEMA_VERSION)

    def test_schema_v5_migration_preserves_tasks_and_adds_discussions(self):
        legacy_path = Path(self.temp.name) / "legacy-v5.db"
        server.DB_PATH = legacy_path
        server.init_db()
        timestamp = server.now_iso()
        with server.connect() as db:
            db.execute("INSERT INTO users(id,email,display_name,password_hash,created_at,email_verified_at) VALUES (?,?,?,?,?,?)", ("owner", "owner@example.com", "Owner", "hash", timestamp, timestamp))
            server.ensure_default_kanban_columns(db, "owner", timestamp)
            column_id = db.execute("SELECT id FROM kanban_columns WHERE owner_id='owner' AND semantic_status='todo'").fetchone()[0]
            db.execute("INSERT INTO tasks(id,owner_id,project_id,column_id,title,description,status,priority,created_at,updated_at,version) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("legacy-task", "owner", None, column_id, "Сохранить задачу", "", "todo", "normal", timestamp, timestamp, 1))
            db.execute("DROP TABLE task_messages")
            db.execute("PRAGMA user_version=5")
        server.init_db()
        with server.connect() as db:
            task = db.execute("SELECT title FROM tasks WHERE id='legacy-task'").fetchone()
            message_table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_messages'").fetchone()
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(task["title"], "Сохранить задачу")
        self.assertIsNotNone(message_table)
        self.assertEqual(schema_version, server.SCHEMA_VERSION)

    def test_schema_v6_migration_adds_notes_without_touching_existing_data(self):
        legacy_path = Path(self.temp.name) / "legacy-v6.db"
        server.DB_PATH = legacy_path
        server.init_db()
        timestamp = server.now_iso()
        with server.connect() as db:
            db.execute("INSERT INTO users(id,email,display_name,password_hash,created_at,email_verified_at) VALUES (?,?,?,?,?,?)", ("owner", "owner@example.com", "Owner", "hash", timestamp, timestamp))
            db.execute("DROP TABLE notes")
            db.execute("DROP TABLE note_folders")
            db.execute("PRAGMA user_version=6")
        server.init_db()
        with server.connect() as db:
            user = db.execute("SELECT display_name FROM users WHERE id='owner'").fetchone()
            notes_table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notes'").fetchone()
            folders_table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='note_folders'").fetchone()
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(user["display_name"], "Owner")
        self.assertIsNotNone(notes_table)
        self.assertIsNotNone(folders_table)
        self.assertEqual(schema_version, server.SCHEMA_VERSION)

    def test_schema_v7_migration_indexes_existing_notes_and_adds_links(self):
        server.init_db()
        timestamp = server.now_iso()
        with server.connect() as db:
            owner_id = db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
            db.execute("INSERT INTO notes(id,owner_id,folder_id,title,content,is_favorite,created_at,updated_at,version,deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("legacy-note", owner_id, None, "Старая заметка", "Данные миграции", 0, timestamp, timestamp, 1, None))
            db.execute("DROP TABLE note_links")
            db.execute("DROP TABLE notes_fts")
            db.execute("PRAGMA user_version=7")
        server.init_db()
        with server.connect() as db:
            indexed = db.execute("SELECT note_id FROM notes_fts WHERE notes_fts MATCH 'миграции'").fetchone()[0]
            links_table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='note_links'").fetchone()
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(indexed, "legacy-note")
        self.assertIsNotNone(links_table)
        self.assertEqual(schema_version, server.SCHEMA_VERSION)

    def test_schema_v8_migration_adds_deterministic_kanban_positions(self):
        server.init_db()
        with server.connect() as db:
            tasks = db.execute("SELECT id FROM tasks ORDER BY created_at,id").fetchall()
            db.execute("DROP INDEX IF EXISTS tasks_owner_column_position")
            db.execute("PRAGMA user_version=8")
        server.init_db()
        with server.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            positions = [row[0] for row in db.execute("SELECT kanban_position FROM tasks WHERE deleted_at IS NULL ORDER BY kanban_position").fetchall()]
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("kanban_position", columns)
        self.assertEqual(len(positions), len(tasks))
        self.assertTrue(all(position > 0 for position in positions))
        self.assertEqual(schema_version, server.SCHEMA_VERSION)

    def test_schema_v9_migration_adds_recurrence_without_touching_tasks(self):
        server.init_db()
        with server.connect() as db:
            timestamp = server.now_iso()
            owner_id = db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
            server.ensure_default_kanban_columns(db, owner_id, timestamp)
            column_id = db.execute("SELECT id FROM kanban_columns WHERE owner_id=? LIMIT 1", (owner_id,)).fetchone()[0]
            task_id = "legacy-recurrence-task"
            db.execute("INSERT INTO tasks(id,owner_id,column_id,title,description,status,priority,kanban_position,created_at,updated_at,version,deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, owner_id, column_id, "Старая задача", "", "inbox", "normal", 1024, timestamp, timestamp, 1, None))
            db.execute("PRAGMA user_version=9")
        server.init_db()
        with server.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            recurrence = db.execute("SELECT recurrence FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        self.assertIn("recurrence", columns)
        self.assertIsNone(recurrence)

    def test_schema_v10_migration_adds_reminders_without_touching_tasks(self):
        server.init_db()
        with server.connect() as db:
            timestamp = server.now_iso()
            owner_id = db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
            server.ensure_default_kanban_columns(db, owner_id, timestamp)
            column_id = db.execute("SELECT id FROM kanban_columns WHERE owner_id=? LIMIT 1", (owner_id,)).fetchone()[0]
            task_id = "legacy-reminder-task"
            db.execute("INSERT INTO tasks(id,owner_id,column_id,title,description,status,priority,kanban_position,recurrence,created_at,updated_at,version,deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, owner_id, column_id, "Старая задача", "", "inbox", "normal", 1024, "daily", timestamp, timestamp, 1, None))
            db.execute("PRAGMA user_version=10")
        server.init_db()
        with server.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            task = db.execute("SELECT recurrence,reminder_offsets FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertIn("reminder_offsets", columns)
        self.assertEqual((task["recurrence"], task["reminder_offsets"]), ("daily", None))

    def test_schema_v11_migration_adds_tags_without_touching_tasks(self):
        server.init_db()
        with server.connect() as db:
            timestamp = server.now_iso()
            owner_id = db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
            server.ensure_default_kanban_columns(db, owner_id, timestamp)
            column_id = db.execute("SELECT id FROM kanban_columns WHERE owner_id=? LIMIT 1", (owner_id,)).fetchone()[0]
            task_id = "legacy-tags-task"
            db.execute("INSERT INTO tasks(id,owner_id,column_id,title,description,status,priority,kanban_position,created_at,updated_at,version,deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, owner_id, column_id, "Старая задача", "", "inbox", "normal", 1024, timestamp, timestamp, 1, None))
            db.execute("PRAGMA user_version=11")
        server.init_db()
        with server.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            tags = db.execute("SELECT tags FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        self.assertIn("tags", columns)
        self.assertIsNone(tags)

    def test_schema_v12_migration_adds_task_history(self):
        server.init_db()
        with server.connect() as db:
            db.execute("PRAGMA user_version=12")
            db.execute("DROP TABLE IF EXISTS task_history")
        server.init_db()
        with server.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("task_history", tables)
        self.assertEqual(version, server.SCHEMA_VERSION)

    def test_schema_v13_migration_adds_account_recovery_tokens(self):
        server.init_db()
        with server.connect() as db:
            db.execute("DROP TABLE password_reset_tokens")
            db.execute("DROP TABLE pending_email_change_tokens")
            db.execute("PRAGMA user_version=13")
        server.init_db()
        with server.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue({"password_reset_tokens", "pending_email_change_tokens"}.issubset(tables))
        self.assertEqual(version, server.SCHEMA_VERSION)

    def test_schema_v14_migration_adds_client_mutations(self):
        server.init_db()
        with server.connect() as db:
            db.execute("DROP TABLE client_mutations")
            db.execute("PRAGMA user_version=14")
        server.init_db()
        with server.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("client_mutations", tables)
        self.assertEqual(version, server.SCHEMA_VERSION)

    def test_schema_v15_migration_adds_sessions(self):
        server.init_db()
        with server.connect() as db:
            db.execute("DROP TABLE sessions")
            db.execute("PRAGMA user_version=15")
        server.init_db()
        with server.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("sessions", tables)
        self.assertEqual(version, server.SCHEMA_VERSION)

    def test_schema_v16_migration_adds_timezone_and_validates_updates(self):
        server.init_db()
        with server.connect() as db:
            db.execute("PRAGMA user_version=16")
            db.execute("ALTER TABLE users DROP COLUMN timezone")
        server.init_db()
        status, updated = self.client.request("PATCH", "/api/v1/account", {"timezone": "Europe/Moscow"})
        self.assertEqual((status, updated["user"]["timezone"]), (200, "Europe/Moscow"))
        status, invalid = self.client.request("PATCH", "/api/v1/account", {"timezone": "Mars/Olympus"})
        self.assertEqual((status, invalid["error"]["code"]), (422, "invalid_timezone"))

    def test_sync_is_paginated_bounded_and_gzip_capable(self):
        created = [self.client.request("POST", "/api/v1/tasks", {"title": f"Пагинация {number}"})[1]["task"]["id"] for number in range(3)]
        status, first = self.client.request("GET", "/api/v1/sync?limit=1")
        self.assertEqual((status, first["has_more"]), (200, True))
        task_ids, page = [task["id"] for task in first["tasks"]], first
        while page["has_more"]:
            status, page = self.client.request("GET", f"/api/v1/sync?limit=1&cursor={page['next_cursor']}")
            self.assertEqual((status, page["snapshot"]), (200, first["snapshot"]))
            task_ids.extend(task["id"] for task in page["tasks"])
        self.assertEqual(set(created), set(task_ids))
        self.assertEqual(self.client.request("GET", "/api/v1/sync?limit=501")[0], 422)
        self.assertEqual(self.client.request("GET", "/api/v1/sync?cursor=invalid")[0], 422)

    def test_sync_gzip_response_and_openapi_contract(self):
        for number in range(20):
            self.client.request("POST", "/api/v1/tasks", {"title": f"Сжатый sync {number}", "description": "x" * 100})
        captured = {}
        response = server.Application()({"REQUEST_METHOD": "GET", "PATH_INFO": "/api/v1/sync", "QUERY_STRING": "limit=500", "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(), "HTTP_AUTHORIZATION": f"Bearer {self.client.token}", "HTTP_ACCEPT_ENCODING": "gzip"}, lambda status, headers: captured.update(status=status, headers=dict(headers)))
        body = b"".join(response)
        self.assertEqual((captured["status"].split()[0], captured["headers"].get("Content-Encoding"), len(json.loads(gzip.decompress(body))["tasks"]) >= 20), ("200", "gzip", True))
        spec = json.loads((Path(__file__).parent.parent / "docs" / "openapi.json").read_text(encoding="utf-8"))
        self.assertTrue({"/sync", "/sync/mutations", "/auth/refresh", "/sessions", "/sessions/{sessionId}"}.issubset(spec["paths"]))

    def test_task_mutation_batch_is_idempotent(self):
        mutation_id, task_id = "879bdb7d-7680-4667-9b20-55aceba60be7", "022d0b10-f4f3-4a07-9861-96d747ccd1ed"
        payload = {"mutations": [{"id": mutation_id, "operation": "create", "task_id": task_id, "body": {"title": "Офлайн задача", "scheduled_date": "2026-08-03"}}]}
        status, first = self.client.request("POST", "/api/v1/sync/mutations", payload)
        self.assertEqual((status, first["mutations"][0]["status"], first["mutations"][0]["response"]["task"]["id"]), (200, 201, task_id))
        status, replay = self.client.request("POST", "/api/v1/sync/mutations", payload)
        self.assertEqual((status, replay["mutations"][0]["replayed"]), (200, True))
        status, tasks = self.client.request("GET", "/api/v1/tasks")
        self.assertEqual([task["id"] for task in tasks["tasks"] if task["id"] == task_id], [task_id])

    def test_task_mutation_conflict_includes_current_task(self):
        status, created = self.client.request("POST", "/api/v1/tasks", {"title": "Серверная задача"})
        task = created["task"]
        self.client.request("PATCH", f"/api/v1/tasks/{task['id']}", {"title": "Изменено на сервере", "expected_version": 1})
        payload = {"mutations": [{"id": "c7b2cf10-ddec-4ee0-a6c9-06ab5a9ff4f6", "operation": "update", "task_id": task["id"], "body": {"title": "Офлайн версия", "expected_version": 1}}]}
        status, response = self.client.request("POST", "/api/v1/sync/mutations", payload)
        result = response["mutations"][0]
        self.assertEqual((status, result["status"], result["response"]["current_task"]["title"]), (200, 409, "Изменено на сервере"))

    def test_refresh_rotation_session_listing_and_revoke(self):
        client = ApiClient()
        login = client.request("POST", "/api/v1/auth/login", {"email": "user@example.com", "password": "correct-horse", "device_name": "Тестовый браузер"})[1]
        self.assertIn("refresh_token", login)
        client.token = login["token"]
        status, sessions = client.request("GET", "/api/v1/sessions")
        self.assertEqual((status, sessions["sessions"][0]["label"], sessions["sessions"][0]["current"]), (200, "Тестовый браузер", True))
        status, refreshed = client.request("POST", "/api/v1/auth/refresh", {"refresh_token": login["refresh_token"]})
        self.assertEqual(status, 200)
        self.assertNotEqual(login["refresh_token"], refreshed["refresh_token"])
        self.assertEqual(client.request("POST", "/api/v1/auth/refresh", {"refresh_token": login["refresh_token"]})[0], 401)
        client.token = refreshed["token"]
        self.assertEqual(client.request("DELETE", f"/api/v1/sessions/{refreshed['session_id']}")[0], 200)
        self.assertEqual(client.request("GET", "/api/v1/me")[0], 401)

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
