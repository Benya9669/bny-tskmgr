# Выпуск TaskFlow

Релизы TaskFlow создаются только из основной ветки через неизменяемые подписанные теги `vX.Y.Z`. Обычный push и ручной запуск CI выполняют тестовую сборку, но ничего не публикуют.

## 1. Подготовка версии

1. Закройте release gate версии в `ROADMAP.md`.
2. Перенесите пользовательские изменения из `## Unreleased` в раздел, заголовок которого точно совпадает с будущим тегом:

   ```markdown
   ## v0.0.1
   ```

3. Укажите точную версию без `v` и без суффикса `-dev` в `VERSION`:

   ```text
   0.0.1
   ```

4. Проверьте извлечение release notes локально:

   ```bash
   bash scripts/extract-release-notes.sh v0.0.1 CHANGELOG.md release-notes.md
   ```

5. Выполните полный gate из `AGENTS.md`, Docker smoke-тест и проверьте backup/restore при изменении данных.

## 2. Release commit

Проверьте, что в commit не попали `.env`, `data/`, `AGENTS.md` и локальные дизайн-макеты. Затем создайте отдельный release commit:

```bash
git add CHANGELOG.md ROADMAP.md VERSION
git commit -m "release: TaskFlow 0.0.1"
```

Команда приведена как пример; commit выполняется только после явного подтверждения владельца проекта.

Для первой публикации пустого репозитория release commit включает весь проект:

```bash
git add .
git status --short
git commit -m "release: TaskFlow 0.0.1"
```

Перед commit нужно отдельно убедиться, что правила `.gitignore` исключили `AGENTS.md`, `.env`, `data/` и локальные макеты.

## 3. Подписанный тег

На release commit создаётся аннотированный GPG-подписанный тег:

```bash
git tag -s v0.0.1 -m "TaskFlow 0.0.1"
git tag -v v0.0.1
```

Lightweight и неподписанные теги workflow отклоняет. Уже опубликованный тег нельзя перемещать, пересоздавать или force-push-ить.

## 4. Публикация

Сначала публикуется commit основной ветки, затем отдельным действием тег:

```bash
git push origin master
git push origin v0.0.1
```

После push тега CI:

1. проверяет формат и PGP-подпись тега;
2. сверяет тег с `VERSION`;
3. извлекает соответствующий раздел `CHANGELOG.md`;
4. выполняет тесты и собирает `linux/amd64` + `linux/arm64`;
5. публикует `0.0.1`, `0.0` и `latest` в GHCR;
6. подписывает digest через Sigstore/GitHub OIDC;
7. публикует provenance attestation;
8. создаёт GitHub Release с подготовленными заметками.

Workflow откажется продолжать, если GitHub Release или точный контейнерный тег уже существует.

## 5. Проверка релиза

```bash
docker pull ghcr.io/benya9669/bny-tskmgr:0.0.1
docker inspect ghcr.io/benya9669/bny-tskmgr:0.0.1
cosign verify ghcr.io/benya9669/bny-tskmgr:0.0.1 \
  --certificate-identity-regexp='github.com/Benya9669/bny-tskmgr' \
  --certificate-oidc-issuer='https://token.actions.githubusercontent.com'
```

Затем разверните release Compose во временном окружении, проверьте `/api/health`, вход и сохранение данных после перезапуска.

## Исправление неудачного релиза

Опубликованные теги и образы не перезаписываются. Ошибка исправляется новым patch-релизом, например `v0.0.2`. Если workflow упал до публикации образа и GitHub Release, исправьте причину в новом commit и создайте новый тег; не перемещайте уже отправленный тег.
