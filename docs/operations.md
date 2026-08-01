# Эксплуатация TaskFlow

## Резервное копирование

TaskFlow использует SQLite WAL. Не копируйте работающий файл базы обычной файловой командой: отдельная копия без согласованного WAL может оказаться неполной. Встроенная команда использует SQLite Online Backup API и проверяет результат через `PRAGMA integrity_check`.

При локальном запуске:

```bash
python -m app.backup backups/taskflow-2026-08-01.db
```

В Docker сначала создайте согласованную копию внутри тома, затем скопируйте её на хост:

```bash
docker compose -f compose.release.yaml exec taskflow python -m app.backup /data/backups/taskflow.db
docker cp taskflow:/data/backups/taskflow.db ./taskflow-backup.db
```

Команда намеренно не перезаписывает существующий backup. Используйте уникальное имя для каждого запуска. Храните копии вне Docker-хоста и периодически проверяйте процедуру восстановления.

## Проверка backup

Проверить копию без запуска приложения:

```bash
python -m sqlite3 taskflow-backup.db "PRAGMA integrity_check;"
```

Ожидаемый ответ — `ok`.

## Безопасное восстановление

Восстановление меняет рабочие данные, поэтому сначала проверяйте backup во временном окружении.

1. Остановите TaskFlow, чтобы исключить новые записи.
2. Сохраните отдельную копию текущей базы.
3. Запустите контейнер с копией backup в отдельном временном томе или локально через `TASKFLOW_DB`.
4. Проверьте вход, проекты и несколько задач.
5. Только после проверки замените рабочую базу и запустите сервис.

Не используйте `docker compose down -v`: эта команда удаляет именованный том с данными.

## Обновление

```bash
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d
docker compose -f compose.release.yaml ps
```

Перед обновлением создайте backup. После обновления проверьте:

```bash
curl --fail http://127.0.0.1:8080/api/health
docker compose -f compose.release.yaml logs --tail=100 taskflow
```

Health endpoint возвращает версии приложения и схемы базы. Если база создана более новой несовместимой версией TaskFlow, сервер откажется запускаться вместо попытки понизить схему.

## Reverse proxy

Release Compose по умолчанию публикует приложение только на `127.0.0.1:8080`. Размещайте Caddy, Nginx или другой reverse proxy на том же сервере и завершайте TLS на нём. Передавайте исходные заголовки `Host` и `X-Forwarded-Proto`; не публикуйте HTTP-интерфейс в интернет напрямую.

## Диагностика

- `GET /api/health` — доступность, версия приложения и схема БД.
- HTTP-запросы журналируются отдельными JSON-сообщениями без bearer-токенов и тела запроса.
- `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR` управляет уровнем серверных логов.
