from __future__ import annotations

import os
import re
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
            server.ensure_default_kanban_columns(db, user_id, timestamp)
        viewport = {"width": 390, "height": 844} if self._testMethodName.startswith("test_mobile") else {"width": 1366, "height": 900}
        self.context: BrowserContext = self.browser.new_context(viewport=viewport, locale="ru-RU")
        self.context.grant_permissions(["notifications"], origin=self.base_url)
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

        task_row.get_by_role("button", name="Открыть обсуждение").click()
        discussion_dialog = self.page.locator("#discussionDialog")
        discussion_dialog.get_by_label("Новое сообщение").fill("Решили выпускать сегодня")
        discussion_dialog.get_by_role("button", name="Отправить").click()
        expect(discussion_dialog.get_by_text("Решили выпускать сегодня", exact=True)).to_be_visible()
        discussion_dialog.get_by_role("button", name="Изменить").click()
        discussion_dialog.locator(".discussion-edit-form textarea").fill("Решили выпускать завтра")
        discussion_dialog.get_by_role("button", name="Сохранить").click()
        expect(discussion_dialog.get_by_text("Решили выпускать завтра", exact=True)).to_be_visible()
        expect(discussion_dialog.get_by_text("изменено", exact=True)).to_be_visible()
        discussion_dialog.get_by_role("button", name="Закрыть").last.click()

        self.page.locator('[data-view="board"]').first.click()
        board = self.page.get_by_role("region", name="Канбан-доска")
        expect(board.get_by_text("Проверить релиз", exact=True)).to_be_visible()
        board_card = board.locator(".kanban-card").filter(has_text="Проверить релиз")
        expect(board_card.get_by_text("Прогнать smoke-тест", exact=True)).to_be_visible()
        expect(board.locator(".kanban-status")).to_have_count(0)
        board.get_by_role("button", name="Настроить колонки").click()
        columns_dialog = self.page.get_by_role("dialog", name="Настройка колонок")
        add_column = columns_dialog.locator("#columnAddForm")
        add_column.get_by_label("Название").fill("На проверке")
        add_column.get_by_label("Смысл").select_option("in_progress")
        add_column.locator('input[name="color"]').fill("#123456")
        expect(add_column.locator('input[type="color"]')).to_have_count(1)
        add_column.get_by_role("button", name="Добавить колонку").click()
        expect(columns_dialog.locator(".column-settings-row")).to_have_count(4)
        expect(columns_dialog.locator(".column-settings-row").first).to_have_attribute("draggable", "true")
        columns_dialog.get_by_role("button", name="Закрыть").last.click()
        expect(board.locator(".kanban-column h2")).to_have_count(4)
        task_menu_button = board.get_by_role("button", name="Действия задачи «Проверить релиз»")
        task_menu_button.click()
        expect(board.get_by_role("menuitem", name="Обсуждение")).to_be_visible()
        expect(board.get_by_role("menuitem", name="Переместить")).to_be_visible()
        self.page.keyboard.press("Escape")
        expect(task_menu_button).to_be_focused()
        task_menu_button.click()
        board.get_by_role("menuitem", name="Переместить").click()
        move_dialog = self.page.get_by_role("dialog", name="Изменить место и день")
        move_dialog.get_by_role("button", name="Завтра").click()
        move_dialog.get_by_role("button", name="Перенести").click()
        expect(self.page.locator("#toast")).to_contain_text("Задача перенесена")

        self.page.locator('[data-view="notes"]').first.click()
        notes_section = self.page.get_by_role("region", name="Заметки")
        notes_section.get_by_label("Название новой папки").fill("Документация")
        notes_section.get_by_role("button", name="Добавить папку").click()
        notes_section.locator('#newNote').click()
        notes_section.get_by_label("Название заметки").fill("План выпуска")
        notes_section.get_by_label("Текст заметки в Markdown").fill("# Релиз\n\n**Готово** к проверке\n\n<img src=x onerror=alert(1)>\n\n`[[Не ссылка]]`")
        notes_section.get_by_role("button", name="Жирный").click()
        expect(notes_section.get_by_label("Текст заметки в Markdown")).to_have_value(re.compile(r".*\*\*текст\*\*$", re.DOTALL))
        expect(notes_section.locator("#noteSaveStatus")).to_have_text("Сохранено", timeout=8_000)
        notes_section.get_by_role("button", name="Просмотр").click()
        expect(notes_section.get_by_role("heading", name="Релиз", exact=True)).to_be_visible()
        expect(notes_section.locator("#notePreview img")).to_have_count(0)
        expect(notes_section.locator("#notePreview code")).to_have_text("[[Не ссылка]]")
        expect(notes_section.locator("#notePreview .wiki-link")).to_have_count(0)
        notes_section.get_by_role("button", name="Добавить в избранное").click()
        expect(notes_section.get_by_role("button", name="Убрать из избранного")).to_be_visible()
        notes_section.get_by_role("button", name="Избранное").click()
        expect(notes_section.locator(".note-list-item").filter(has_text="План выпуска")).to_be_visible()
        self.page.locator(".sidebar .nav-item[data-filter='today']").click()

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
        import_dialog = self.page.get_by_role("dialog", name="Импортировать данные TaskFlow?")
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

    def test_desktop_manual_order_and_week_planning(self) -> None:
        self.login()
        for title in ("Первая карточка", "Вторая карточка"):
            self.page.locator("#addTask").click()
            dialog = self.page.locator("#taskDialog")
            dialog.get_by_label("Название").fill(title)
            dialog.get_by_role("button", name="Сохранить").click()
            expect(self.page.get_by_text(title, exact=True)).to_be_visible()

        self.page.locator('[data-view="board"]').first.click()
        board = self.page.get_by_role("region", name="Канбан-доска")
        first = board.locator(".kanban-card").filter(has_text="Первая карточка")
        second = board.locator(".kanban-card").filter(has_text="Вторая карточка")
        second.drag_to(first)
        expect(board.locator(".kanban-card h3").first).to_have_text("Вторая карточка")
        self.page.reload()
        self.page.locator('[data-view="board"]').first.click()
        expect(board.locator(".kanban-card h3").first).to_have_text("Вторая карточка")

        self.page.locator('[data-view="calendar"]').first.click()
        calendar = self.page.get_by_role("region", name="Недельный календарь")
        expect(calendar.locator(".calendar-day")).to_have_count(7)
        card = calendar.locator(".calendar-card").filter(has_text="Вторая карточка")
        expect(card).to_be_visible()
        target = calendar.locator(".calendar-day .calendar-dropzone").nth(1)
        planned_date = target.get_attribute("data-date")
        card.drag_to(target)
        expect(target.get_by_text("Вторая карточка", exact=True)).to_be_visible()
        self.page.reload()
        self.page.locator('[data-view="calendar"]').first.click()
        expect(calendar.locator(f'.calendar-dropzone[data-date="{planned_date}"]').get_by_text("Вторая карточка", exact=True)).to_be_visible()
        self.assertEqual(self.page_errors, [])

    def test_desktop_calendar_shows_daily_load_and_capacity(self) -> None:
        self.login()
        self.page.locator("#addTask").click()
        dialog = self.page.locator("#taskDialog")
        dialog.get_by_label("Название").fill("Нагрузка на день")
        dialog.get_by_text("Дополнительные настройки", exact=True).click()
        dialog.get_by_label("Оценка, мин").fill("90")
        dialog.get_by_role("button", name="Сохранить").click()
        self.page.locator('[data-view="calendar"]').first.click()
        calendar = self.page.get_by_role("region", name="Недельный календарь")
        self.page.locator("#dailyCapacity").fill("60")
        self.page.locator("#dailyCapacity").press("Tab")
        expect(calendar.locator(".calendar-day.overloaded")).to_have_count(1)
        expect(self.page.locator("#dailyLoad")).to_contain_text("90 из 60 мин")
        self.assertEqual(self.page_errors, [])

    def test_desktop_focus_timer_starts_task(self) -> None:
        self.login()
        self.page.locator("#addTask").click()
        dialog = self.page.locator("#taskDialog")
        dialog.get_by_label("Название").fill("Фокусная задача")
        dialog.get_by_role("button", name="Сохранить").click()
        self.page.get_by_role("button", name="Фокус").click()
        focus = self.page.locator("#focusDialog")
        focus.get_by_label("Задача").select_option(label="Фокусная задача")
        focus.get_by_role("button", name="Начать").click()
        expect(focus.get_by_role("button", name="Пауза")).to_be_visible()
        self.assertEqual(self.page_errors, [])

    def test_desktop_task_history_opens(self) -> None:
        self.login()
        self.page.locator("#addTask").click()
        dialog = self.page.locator("#taskDialog")
        dialog.get_by_label("Название").fill("Задача с историей")
        dialog.get_by_role("button", name="Сохранить").click()
        task = self.page.locator(".task").filter(has_text="Задача с историей")
        task.get_by_role("button", name="Открыть историю").click()
        history = self.page.locator("#historyDialog")
        expect(history.get_by_text("Задача создана", exact=True)).to_be_visible()
        self.assertEqual(self.page_errors, [])

    def test_desktop_recurring_task_creates_next_instance(self) -> None:
        self.login()
        self.page.locator("#addTask").click()
        dialog = self.page.locator("#taskDialog")
        dialog.get_by_label("Название").fill("Ежедневная проверка")
        dialog.get_by_text("Дополнительные настройки", exact=True).click()
        dialog.get_by_label("Повторение").select_option("daily")
        dialog.get_by_role("button", name="Сохранить").click()
        task = self.page.locator(".task").filter(has_text="Ежедневная проверка")
        expect(task.get_by_text("Повтор: ежедневно", exact=True)).to_be_visible()
        task.locator(".check").click()
        self.page.locator('[data-filter="all"]').first.click()
        expect(self.page.locator(".task").filter(has_text="Ежедневная проверка")).to_have_count(1)
        self.assertEqual(self.page_errors, [])

    def test_desktop_task_reminder_options_are_available(self) -> None:
        self.login()
        self.page.locator("#addTask").click()
        dialog = self.page.locator("#taskDialog")
        dialog.get_by_label("Название").fill("Напомнить о встрече")
        dialog.locator('input[name="due_at"]').fill("2026-12-01T12:00")
        dialog.get_by_text("Дополнительные настройки", exact=True).click()
        dialog.get_by_label("За час").check()
        dialog.locator('input[name="reminder_offsets"][value="0"]').check()
        expect(dialog.get_by_label("За час")).to_be_checked()
        expect(dialog.locator('input[name="reminder_offsets"][value="0"]')).to_be_checked()
        self.assertEqual(self.page_errors, [])

    def test_desktop_task_tags_are_available(self) -> None:
        self.login()
        self.page.locator("#addTask").click()
        dialog = self.page.locator("#taskDialog")
        self.assertFalse(dialog.locator("#taskAdvanced").evaluate("element => element.open"))
        self.assertLessEqual(dialog.evaluate("element => element.scrollHeight"), dialog.evaluate("element => element.clientHeight"))
        dialog.get_by_text("Дополнительные настройки", exact=True).click()
        dialog.get_by_label("Теги").fill("Работа, важное")
        self.assertEqual(dialog.get_by_label("Теги").input_value(), "Работа, важное")
        self.assertEqual(self.page_errors, [])

    def test_desktop_saved_filter_can_be_removed_safely(self) -> None:
        self.login()
        self.page.locator("#filterToggle").click()
        self.page.locator("#savedFilterName").fill("Только важное")
        self.page.locator("#saveFilter").click()
        expect(self.page.locator("#savedFilter")).to_have_value("0")
        self.page.locator("#deleteSavedFilter").click()
        confirmation = self.page.get_by_role("dialog", name="Удалить сохранённый фильтр?")
        confirmation.get_by_role("button", name="Удалить").click()
        expect(self.page.locator("#savedFilter")).not_to_contain_text("Только важное")
        self.page.evaluate("localStorage.setItem('taskflow_saved_filters', '{broken'); localStorage.setItem('taskflow_focus', '{broken')")
        self.page.reload()
        expect(self.page.get_by_role("heading", name="Сегодня", exact=True)).to_be_visible()
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
        self.page.get_by_role("button", name="Включить тёмную тему").click()
        expect(self.page.locator("html")).to_have_attribute("data-theme", "dark")
        mobile_task.get_by_role("button", name="Открыть обсуждение").click()
        mobile_discussion = self.page.locator("#discussionDialog")
        mobile_discussion.get_by_label("Новое сообщение").fill("Сообщение с телефона")
        mobile_discussion.get_by_role("button", name="Отправить").click()
        expect(mobile_discussion.get_by_text("Сообщение с телефона", exact=True)).to_be_visible()
        mobile_discussion.get_by_role("button", name="Закрыть").last.click()
        mobile_task.get_by_role("button", name="Открыть подзадачи").click()
        checklist_dialog = self.page.get_by_role("dialog", name="Мобильная задача")
        checklist_dialog.get_by_label("Новая подзадача").fill("Проверить мобильный вид")
        checklist_dialog.get_by_role("button", name="Добавить").click()
        checklist_dialog.get_by_role("button", name="Закрыть").click()

        mobile_tabs.locator('[data-view="board"]').click()
        mobile_board = self.page.get_by_role("region", name="Канбан-доска")
        expect(mobile_board).to_be_visible()
        mobile_card = mobile_board.locator(".kanban-card").filter(has_text="Мобильная задача")
        mobile_card.locator(".inline-checklist-toggle").click()
        expect(mobile_card.get_by_text("Проверить мобильный вид", exact=True)).to_be_visible()
        mobile_tabs.locator('#mobileMore').click()
        more_dialog = self.page.get_by_role("dialog", name="Ещё")
        more_dialog.locator('[data-filter="all"]').click()
        expect(self.page.locator("#taskList").get_by_text("Мобильная задача", exact=True)).to_be_visible()

        mobile_tabs.locator('[data-view="notes"]').click()
        mobile_notes = self.page.get_by_role("region", name="Заметки")
        mobile_notes.locator('#newNote').click()
        mobile_notes.get_by_label("Название заметки").fill("Мобильная заметка")
        mobile_notes.get_by_label("Текст заметки в Markdown").fill("## На ходу")
        expect(mobile_notes.locator("#noteSaveStatus")).to_have_text("Сохранено", timeout=8_000)
        mobile_notes.get_by_role("button", name="Просмотр").click()
        expect(mobile_notes.get_by_role("heading", name="На ходу")).to_be_visible()
        mobile_notes.get_by_role("button", name="Изменить").click()
        expect(mobile_notes.get_by_label("Текст заметки в Markdown")).to_be_visible()
        mobile_notes.get_by_role("button", name="Вернуться к списку").click()
        expect(mobile_notes.get_by_role("button", name="Мобильная заметка На ходу")).to_be_visible()
        mobile_tabs.locator('#mobileMore').click()
        more_dialog.locator('[data-filter="all"]').click()

        self.page.locator('[data-open-archive]').click()
        expect(self.page.get_by_role("dialog", name="Архив проектов").get_by_text("Архив пуст", exact=True)).to_be_visible()
        self.page.get_by_role("dialog", name="Архив проектов").get_by_role("button", name="Закрыть").last.click()
        mobile_tabs.locator('#mobileMore').click()
        more_dialog.locator("#mobileDataTools").click()
        expect(self.page.get_by_role("dialog", name="Экспорт и импорт")).to_be_visible()
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"),
            self.page.evaluate("document.documentElement.clientWidth"),
        )
        self.assertEqual(self.page_errors, [])


if __name__ == "__main__":
    unittest.main()
