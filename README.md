# TaskFlow

Self-hosted таск-трекер и дневной планер с адаптивным веб-интерфейсом и REST API для будущего Android-клиента.

## Возможности MVP

- локальные аккаунты с безопасным хешированием паролей;
- обязательное подтверждение email через SMTP перед первым входом;
- проекты, входящие и план на день;
- приоритет, дата планирования, независимый срок выполнения, описание и оценка времени задачи;
- завершение, редактирование и мягкое удаление задач;
- быстрое создание задачи клавишей `N` и сохранение через `Ctrl+Enter` (`⌘+Enter` на macOS);
- поиск по задачам и комбинируемые фильтры по проекту, статусу, приоритету и диапазону дат;
- быстрый перенос задач между проектами и днями из списка или канбан-доски;
- читаемая канбан-доска с увеличенными карточками, drag-and-drop между статусами и фильтрацией по проекту;
- раскрывающиеся подзадачи прямо в списке и канбане с быстрым изменением статуса и переходом к полному чек-листу;
- адаптивный интерфейс для компьютера и телефона;
- светлая и тёмная темы с системным режимом и сохранением выбора;
- встроенные меню, подтверждения, уведомления и палитра проектов в едином стиле TaskFlow без системных `confirm` и color picker;
- единый набор масштабируемых SVG-иконок для действий с задачами;
- публичный адаптивный лендинг для новых посетителей и фирменная иконка Orbit;
- offline-friendly API: UUID от клиента, курсор синхронизации, tombstone удаления и optimistic locking;
- SQLite в WAL-режиме, healthcheck и Docker Compose.

## Быстрый запуск

### Docker Compose

1. Скопируйте `.env.example` в `.env`.
2. Замените `TASKFLOW_SECRET`, укажите публичный URL и рабочие SMTP-параметры.
3. Запустите:

```bash
docker compose up -d --build
```

Откройте `http://localhost:8080`. Данные хранятся в именованном томе `taskflow_data`.

Проверка конфигурации и состояния:

```bash
docker compose config
curl http://localhost:8080/api/health
```

### Быстрый production deploy из GHCR

Файл `compose.release.yaml` использует готовый multi-arch образ и не требует исходников или локальной сборки:

```bash
curl -O https://raw.githubusercontent.com/Benya9669/bny-tskmgr/master/compose.release.yaml
curl -O https://raw.githubusercontent.com/Benya9669/bny-tskmgr/master/.env.example
cp .env.example .env
# Задайте случайный TASKFLOW_SECRET в .env
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d
```

По умолчанию сервис доступен только на `127.0.0.1:8080`, что безопасно для схемы с Caddy/Nginx и HTTPS. Для прямого доступа из LAN задайте `TASKFLOW_BIND_ADDRESS=0.0.0.0`. Обновление выполняется теми же командами `pull` и `up -d`; том с SQLite сохраняется.

Если пакет GHCR ещё приватный, серверу потребуется `docker login ghcr.io`. Для простого публичного развёртывания после первой публикации сделайте package `bny-tskmgr` публичным в настройках GitHub.

## CI/CD

Workflow `.github/workflows/docker.yml` запускает тесты и проверочную multi-arch сборку для pull request, `master` и ручного запуска. Публикация выполняется только неизменяемым подписанным тегом вида `v1.2.3`: CI проверяет совпадение с `VERSION`, извлекает соответствующий раздел `CHANGELOG.md`, публикует SemVer/`latest` в GHCR, подписывает digest через Sigstore и создаёт GitHub Release. Для GHCR и keyless-подписи используются `GITHUB_TOKEN` и GitHub OIDC; дополнительные секреты не нужны.

### Без Docker

Требуется Python 3.11 или новее; сторонние пакеты не нужны.

```powershell
$env:TASKFLOW_SECRET = "replace-with-a-long-random-secret"
python -m app.server
```

База по умолчанию создаётся в `data/taskflow.db`. Путь можно изменить переменной `TASKFLOW_DB`.

## Конфигурация

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `TASKFLOW_SECRET` | небезопасное dev-значение | Подпись bearer-токенов; обязательна в production |
| `TASKFLOW_DB` | `data/taskflow.db` | Путь к SQLite |
| `TASKFLOW_HOST` | `0.0.0.0` | Адрес прослушивания |
| `TASKFLOW_PORT` | `8080` | Порт HTTP |
| `TASKFLOW_TOKEN_TTL_DAYS` | `30` | Срок жизни токена |
| `TASKFLOW_PUBLIC_URL` | `http://localhost:8080` | Внешний URL, используемый в ссылке подтверждения email |
| `TASKFLOW_SMTP_HOST` | пусто | SMTP-сервер; обязателен для регистрации |
| `TASKFLOW_SMTP_PORT` | `587` | Порт SMTP |
| `TASKFLOW_SMTP_MODE` | `starttls` | `starttls`, `ssl` или `plain` для доверенного локального relay |
| `TASKFLOW_SMTP_USERNAME` | пусто | Имя пользователя SMTP, если требуется авторизация |
| `TASKFLOW_SMTP_PASSWORD` | пусто | Пароль SMTP; хранить только в `.env`/secret manager |
| `TASKFLOW_SMTP_FROM` | пусто | Email отправителя; обязателен для регистрации |
| `TASKFLOW_VERIFICATION_TTL_HOURS` | `24` | Срок действия одноразовой ссылки |
| `TASKFLOW_ALLOWED_ORIGIN` | пусто | Разрешённый CORS origin для отдельного web-клиента |

Для публичного размещения ставьте приложение за reverse proxy с HTTPS. Не публикуйте порт напрямую без TLS и сохраните `TASKFLOW_SECRET` отдельно от репозитория.

## API и Android

Все прикладные методы находятся под `/api/v1`. Авторизация после регистрации или входа:

```http
Authorization: Bearer <token>
```

Основные маршруты:

| Метод | Путь | Назначение |
| --- | --- | --- |
| `POST` | `/api/v1/auth/register` | Регистрация |
| `POST` | `/api/v1/auth/login` | Получение токена |
| `POST` | `/api/v1/auth/verify-email` | Подтверждение одноразового токена |
| `POST` | `/api/v1/auth/resend-verification` | Повторная отправка письма |
| `GET` | `/api/v1/me` | Профиль |
| `GET/POST` | `/api/v1/projects` | Проекты |
| `POST/DELETE` | `/api/v1/projects/{id}/archive` | Архивирование и восстановление проекта без отсоединения задач |
| `GET/POST` | `/api/v1/tasks` | Список и создание задач |
| `PATCH/DELETE` | `/api/v1/tasks/{id}` | Изменение и мягкое удаление |
| `GET` | `/api/v1/checklist?task_id={id}` | Все активные подзадачи или чек-лист одной задачи |
| `POST` | `/api/v1/tasks/{id}/checklist` | Добавление одноуровневой подзадачи |
| `PATCH/DELETE` | `/api/v1/checklist/{id}` | Изменение, отметка выполнения и мягкое удаление подзадачи |
| `GET` | `/api/v1/data/export` | Переносимый JSON-экспорт без данных аккаунта |
| `POST` | `/api/v1/data/import` | Атомарное добавление копий из JSON с новыми UUID |
| `GET` | `/api/v1/sync?since={cursor}` | Инкрементальные изменения |

### Архив и перенос данных

Архивирование скрывает проект из боковой панели и списков выбора, но не удаляет проект и не отсоединяет его задачи. Проект можно восстановить через окно «Архив проектов».

Экспорт из окна «Экспорт и импорт» содержит активные и архивные проекты, задачи и элементы чек-листов. Email, пароль, bearer-токены и другие данные аккаунта в файл не включаются. Импорт ограничен 1 МБ, полностью проверяется до записи и добавляет сущности с новыми UUID. Существующие данные не изменяются; при любой ошибке импорт целиком откатывается.

Интерфейс доступен с клавиатуры: `Tab` перемещает видимый фокус, skip-link переводит к основному содержимому, `N` открывает новую задачу, `/` переводит в поиск, `Ctrl+Enter`/`⌘+Enter` сохраняет задачу, а `Escape` закрывает фильтры и возвращает фокус на их кнопку. Статусы операций и прогресс передаются screen reader через live-region и ARIA progressbar.

Рекомендуемая схема Android-клиента:

1. Room хранит задачи, проекты и элементы чек-листов локально.
2. WorkManager периодически вызывает `/sync`, передавая последний `cursor`.
3. Новая offline-задача получает UUID на телефоне; UUID отправляется заголовком `X-Client-ID` при `POST /tasks`.
4. Обновление отправляет `expected_version`. Ответ `409 version_conflict` означает, что нужно скачать серверную версию и предложить разрешение конфликта.
5. Удалённые записи приходят из `/sync` с `deleted_at`, поэтому удаление воспроизводится на всех устройствах. В ответе синхронизации элементы подзадач находятся в `checklist_items`.

`cursor` из ответа нужно сохранять только после успешной транзакции Room. Для первого запуска используйте `since=1970-01-01T00:00:00.000Z`.

## Проверки

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m unittest discover -v
python -m py_compile app/server.py app/backup.py
node --check web/app.js
python -m json.tool docs/openapi.json
```

Browser smoke-тесты запускают отдельный TaskFlow на случайном локальном порту и используют временную SQLite-базу. Они проверяют desktop и mobile сценарии, клавиатурную навигацию, задачи, канбан, чек-листы, архив и переносимый JSON. Playwright trace сохраняется в игнорируемом каталоге `artifacts/browser-smoke`; при ошибке CI загружает его как артефакт на 7 дней. В Linux/CI Chromium устанавливается командой `python -m playwright install --with-deps chromium`.

Roadmap продукта находится в [`ROADMAP.md`](ROADMAP.md), а безопасное резервное копирование, обновление и восстановление описаны в [`docs/operations.md`](docs/operations.md).
Машиночитаемый контракт API опубликован в [`docs/openapi.json`](docs/openapi.json).
Подготовка подписанного тега и автоматического GitHub Release описана в [`docs/releasing.md`](docs/releasing.md).
Настройка обязательного подтверждения email описана в [`docs/smtp.md`](docs/smtp.md).

## Границы текущего MVP

Сейчас это однопользовательские пространства без совместного доступа, вложений, повторяющихся задач и push-уведомлений. Следующий разумный этап — формализовать OpenAPI, добавить refresh-токены/отзыв сессий и реализовать Android-клиент на Kotlin + Jetpack Compose + Room + WorkManager.

## Лицензия

TaskFlow распространяется на условиях [GNU Affero General Public License v3.0 only](LICENSE) (`AGPL-3.0-only`). При предоставлении изменённой версии приложения пользователям по сети необходимо также предложить им соответствующий исходный код согласно условиям лицензии.
