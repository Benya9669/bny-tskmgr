# TaskFlow

Self-hosted таск-трекер и дневной планер с адаптивным веб-интерфейсом и REST API для будущего Android-клиента.

## Возможности MVP

- локальные аккаунты с безопасным хешированием паролей;
- обязательное подтверждение email через SMTP перед первым входом;
- проекты, входящие и план на день;
- приоритет, дата, описание и оценка времени задачи;
- завершение, редактирование и мягкое удаление задач;
- канбан-доска с drag-and-drop между статусами и фильтрацией по проекту;
- адаптивный интерфейс для компьютера и телефона;
- светлая и тёмная темы с системным режимом и сохранением выбора;
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
| `GET/POST` | `/api/v1/tasks` | Список и создание задач |
| `PATCH/DELETE` | `/api/v1/tasks/{id}` | Изменение и мягкое удаление |
| `GET` | `/api/v1/sync?since={cursor}` | Инкрементальные изменения |

Рекомендуемая схема Android-клиента:

1. Room хранит задачи и проекты локально.
2. WorkManager периодически вызывает `/sync`, передавая последний `cursor`.
3. Новая offline-задача получает UUID на телефоне; UUID отправляется заголовком `X-Client-ID` при `POST /tasks`.
4. Обновление отправляет `expected_version`. Ответ `409 version_conflict` означает, что нужно скачать серверную версию и предложить разрешение конфликта.
5. Удалённые записи приходят из `/sync` с `deleted_at`, поэтому удаление воспроизводится на всех устройствах.

`cursor` из ответа нужно сохранять только после успешной транзакции Room. Для первого запуска используйте `since=1970-01-01T00:00:00.000Z`.

## Проверки

```bash
python -m unittest discover -v
python -m py_compile app/server.py
```

Roadmap продукта находится в [`ROADMAP.md`](ROADMAP.md), а безопасное резервное копирование, обновление и восстановление описаны в [`docs/operations.md`](docs/operations.md).
Машиночитаемый контракт API опубликован в [`docs/openapi.json`](docs/openapi.json).
Подготовка подписанного тега и автоматического GitHub Release описана в [`docs/releasing.md`](docs/releasing.md).
Настройка обязательного подтверждения email описана в [`docs/smtp.md`](docs/smtp.md).

## Границы текущего MVP

Сейчас это однопользовательские пространства без совместного доступа, вложений, повторяющихся задач и push-уведомлений. Следующий разумный этап — формализовать OpenAPI, добавить refresh-токены/отзыв сессий и реализовать Android-клиент на Kotlin + Jetpack Compose + Room + WorkManager.

## Лицензия

TaskFlow распространяется на условиях [GNU Affero General Public License v3.0 only](LICENSE) (`AGPL-3.0-only`). При предоставлении изменённой версии приложения пользователям по сети необходимо также предложить им соответствующий исходный код согласно условиям лицензии.
