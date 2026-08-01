from __future__ import annotations

import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect, sync_playwright

from app import server


class QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, message_format: str, *args: object) -> None:
        return


class BrowserSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.original_db_path = server.DB_PATH
        server.DB_PATH = Path(cls.temp.name) / "browser-smoke.db"
        server.init_db()
        cls.httpd = make_server("127.0.0.1", 0, server.Application(), handler_class=QuietRequestHandler)
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.playwright: Playwright = sync_playwright().start()
        cls.browser: Browser = cls.playwright.chromium.launch(headless=True)
        cls.artifact_dir = Path(os.getenv("TASKFLOW_BROWSER_ARTIFACTS", "artifacts/browser-smoke"))
        cls.artifact_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.httpd.shutdown()
        cls.server_thread.join(timeout=5)
        cls.httpd.server_close()
        server.DB_PATH = cls.original_db_path
        cls.temp.cleanup()

    def setUp(self) -> None:
        user_id = str(uuid.uuid4())
        self.email = f"smoke-{user_id}@example.test"
        self.password = "browser-smoke-password"
        timestamp = server.now_iso()
        with server.connect() as db:
            db.execute(
                "INSERT INTO users(id,email,display_name,password_hash,created_at,email_verified_at) VALUES (?,?,?,?,?,?)",
                (user_id, self.email, "Browser Smoke", server.password_hash(self.password), timestamp, timestamp),
            )
        viewport = {"width": 390, "height": 844} if self._testMethodName.startswith("test_mobile") else {"width": 1366, "height": 900}
        self.context: BrowserContext = self.browser.new_context(viewport=viewport, locale="ru-RU")
        self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self.page: Page = self.context.new_page()
        self.page.set_default_timeout(7_000)
        self.page_errors: list[str] = []
        self.page.on("pageerror", lambda error: self.page_errors.append(getattr(error, "stack", None) or str(error)))

    def tearDown(self) -> None:
        trace_path = self.artifact_dir / f"{self.id().rsplit('.', 1)[-1]}.zip"
        self.context.tracing.stop(path=trace_path)
        self.context.close()

    def login(self) -> None:
        self.page.goto(self.base_url)
        expect(self.page.get_by_role("heading", name="Спокойное место для важных дел.")).to_be_visible()
        self.page.get_by_role("button", name="У меня уже есть аккаунт").click()
        self.page.get_by_label("Email").fill(self.email)
        self.page.get_by_label("Пароль").fill(self.password)
        self.page.get_by_role("button", name="Войти", exact=True).click()
        expect(self.page.get_by_role("heading", name="Сегодня", exact=True)).to_be_visible()

    def test_desktop_core_workflow_and_portable_data(self) -> None:
        self.login()

        self.page.get_by_role("button", name="Новый проект").click()
        project_dialog = self.page.locator("#projectDialog")
        project_dialog.get_by_label("Название").fill("Smoke-проект")
        project_dialog.get_by_role("radio", name="Бирюзовый").click()
        expect(project_dialog.locator("#projectColorValue")).to_have_value("#06a5a5")
        project_dialog.get_by_role("button", name="Сохранить").click()
        expect(self.page.get_by_role("button", name="Smoke-проект")).to_be_visible()

        self.page.keyboard.press("n")
        task_dialog = self.page.locator("#taskDialog")
        expect(task_dialog.get_by_label("Название")).to_be_focused()
        task_dialog.get_by_label("Название").fill("Проверить релиз")
        task_dialog.get_by_label("Проект").select_option(label="Smoke-проект")
        self.page.keyboard.press("Control+Enter")
        expect(self.page.get_by_text("Проверить релиз", exact=True)).to_be_visible()

        self.page.get_by_role("button", name="Открыть подзадачи").click()
        checklist_dialog = self.page.get_by_role("dialog", name="Проверить релиз")
        checklist_dialog.get_by_label("Новая подзадача").fill("Прогнать smoke-тест")
        checklist_dialog.get_by_role("button", name="Добавить").click()
        checklist_dialog.get_by_role("button", name="Выполнить подзадачу").click()
        expect(checklist_dialog.get_by_text("1 из 1 выполнено")).to_be_visible()
        checklist_dialog.get_by_role("button", name="Удалить подзадачу").click()
        confirm_dialog = self.page.get_by_role("dialog", name="Удалить подзадачу?")
        expect(confirm_dialog).to_be_visible()
        confirm_dialog.get_by_role("button", name="Отмена").click()
        expect(checklist_dialog.get_by_text("1 из 1 выполнено")).to_be_visible()
        checklist_dialog.get_by_role("button", name="Закрыть").click()
        task_row = self.page.locator(".task").filter(has_text="Проверить релиз")
        task_row.locator(".inline-checklist-toggle").click()
        expect(task_row.get_by_text("Прогнать smoke-тест", exact=True)).to_be_visible()

        self.page.get_by_role("button", name="▦ Доска").first.click()
        board = self.page.get_by_role("region", name="Канбан-доска")
        expect(board.get_by_text("Проверить релиз", exact=True)).to_be_visible()
        board_card = board.locator(".kanban-card").filter(has_text="Проверить релиз")
        expect(board_card.get_by_text("Прогнать smoke-тест", exact=True)).to_be_visible()
        expect(board.locator(".kanban-status")).to_have_count(0)
        task_menu_button = board.get_by_role("button", name="Действия задачи «Проверить релиз»")
        task_menu_button.click()
        expect(board.get_by_role("menuitem", name="Переместить")).to_be_visible()
        self.page.keyboard.press("Escape")
        expect(task_menu_button).to_be_focused()
        task_menu_button.click()
        board.get_by_role("menuitem", name="Переместить").click()
        move_dialog = self.page.get_by_role("dialog", name="Изменить место и день")
        move_dialog.get_by_role("button", name="Завтра").click()
        move_dialog.get_by_role("button", name="Перенести").click()
        expect(self.page.locator("#toast")).to_contain_text("Задача перенесена")

        self.page.get_by_role("button", name="Настроить проект").click()
        self.page.locator("#projectDialog").get_by_role("button", name="В архив").click()
        expect(self.page.get_by_role("button", name="Smoke-проект")).to_have_count(0)
        self.page.get_by_role("button", name="Открыть архив проектов").click()
        archive_dialog = self.page.get_by_role("dialog", name="Архив проектов")
        expect(archive_dialog.get_by_text("Smoke-проект", exact=True)).to_be_visible()
        archive_dialog.get_by_role("button", name="Восстановить").click()
        expect(archive_dialog.get_by_text("Архив пуст", exact=True)).to_be_visible()
        archive_dialog.get_by_role("button", name="Закрыть").last.click()

        self.page.get_by_role("button", name="Экспорт и импорт данных").click()
        data_dialog = self.page.get_by_role("dialog", name="Экспорт и импорт")
        with self.page.expect_download() as download_info:
            data_dialog.get_by_role("button", name="Экспортировать").click()
        download = download_info.value
        self.assertTrue(download.suggested_filename.startswith("taskflow-export-"))
        export_path = download.path()
        self.assertIsNotNone(export_path)
        data_dialog.get_by_label("Выбрать файл").set_input_files(export_path)
        import_dialog = self.page.get_by_role("dialog", name="Импортировать данные?")
        expect(import_dialog).to_be_visible()
        import_dialog.get_by_role("button", name="Импортировать").click()
        expect(self.page.locator("#toast")).to_contain_text("Импортировано")

        self.page.reload()
        self.page.keyboard.press("Tab")
        skip_link = self.page.get_by_role("link", name="Перейти к основному содержимому")
        expect(skip_link).to_be_focused()
        self.page.keyboard.press("Enter")
        expect(self.page.locator("#appMain")).to_be_focused()
        self.assertEqual(self.page_errors, [])

    def test_mobile_navigation_empty_archive_and_data_dialog(self) -> None:
        self.login()

        mobile_tabs = self.page.get_by_role("navigation", name="Основная навигация")
        expect(mobile_tabs).to_be_visible()
        expect(self.page.get_by_text("Здесь пока тихо", exact=True)).to_be_visible()
        self.page.locator("#addTask").click()
        task_dialog = self.page.locator("#taskDialog")
        task_dialog.get_by_label("Название").fill("Мобильная задача")
        task_dialog.get_by_role("button", name="Сохранить").click()
        mobile_task = self.page.locator(".task").filter(has_text="Мобильная задача")
        mobile_task.get_by_role("button", name="Открыть подзадачи").click()
        checklist_dialog = self.page.get_by_role("dialog", name="Мобильная задача")
        checklist_dialog.get_by_label("Новая подзадача").fill("Проверить мобильный вид")
        checklist_dialog.get_by_role("button", name="Добавить").click()
        checklist_dialog.get_by_role("button", name="Закрыть").click()

        mobile_tabs.get_by_role("button", name="▦ Доска").click()
        mobile_board = self.page.get_by_role("region", name="Канбан-доска")
        expect(mobile_board).to_be_visible()
        mobile_card = mobile_board.locator(".kanban-card").filter(has_text="Мобильная задача")
        mobile_card.locator(".inline-checklist-toggle").click()
        expect(mobile_card.get_by_text("Проверить мобильный вид", exact=True)).to_be_visible()
        mobile_tabs.get_by_role("button", name="☷ Все").click()
        expect(self.page.locator("#taskList").get_by_text("Мобильная задача", exact=True)).to_be_visible()

        self.page.get_by_role("button", name="⌂ Архив").click()
        expect(self.page.get_by_role("dialog", name="Архив проектов").get_by_text("Архив пуст", exact=True)).to_be_visible()
        self.page.get_by_role("dialog", name="Архив проектов").get_by_role("button", name="Закрыть").last.click()
        self.page.get_by_role("button", name="Экспорт и импорт данных").click()
        expect(self.page.get_by_role("dialog", name="Экспорт и импорт")).to_be_visible()
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"),
            self.page.evaluate("document.documentElement.clientWidth"),
        )
        self.assertEqual(self.page_errors, [])


if __name__ == "__main__":
    unittest.main()
